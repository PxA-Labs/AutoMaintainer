"""Score one Implementer attempt against one task.

For every task the harness:

1. obtains a candidate file (``noop``: none; ``oracle``: the reference
   solution; ``replay``: a recorded cassette; ``live``: the model),
2. passes model-shaped output through ``agents.normalize_generated_code``
   exactly like ``implementer_node`` does (a rejection there is
   ``output_rejected``; nothing is applied),
3. writes the file into a throw-away copy of the fixture repository,
4. runs the repository's own tests (regression check) and then the hidden
   tests (acceptance check) in separate ``pytest`` subprocesses with a timeout.

``resolved`` means the hidden tests pass AND the pre-existing tests still
pass (fail-to-pass and pass-to-pass, as defined in epic #414).

Isolation: the subprocess gets a minimal environment (no API keys or other
secrets), plugin autoloading off and a fixed hash seed. It is NOT a sandbox:
model-written code runs with the user's file-system and network access. Only
run live evals on a machine/CI runner you are willing to expose to that code.
"""

from __future__ import annotations

import difflib
import os
import shutil

# Runs pytest on fixture copies with a fixed argv, never through a shell.
import subprocess  # nosec B404
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median

from agents import normalize_generated_code

from .llm import Completer, Completion, cassette_completion, load_cassette
from .prompt import build_implementer_prompt, prompt_sha256
from .stats import wilson_interval
from .taskset import Task

MODES = ("replay", "live", "oracle", "noop")
MAX_OUTPUT_CHARS = 4000
MAX_PATCH_CHARS = 20000
# Variables the child needs to start Python on Windows/POSIX. Everything else
# (GROQ_API_KEY, GITHUB_TOKEN, SUPABASE_*, ...) is dropped on purpose.
_ENV_PASSTHROUGH = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
)


@dataclass
class TestRun:
    passed: bool
    collected: bool  # False when pytest found no tests (exit code 5)
    timed_out: bool
    output: str


def _child_env(pythonpath: Path) -> dict:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(
        PYTHONPATH=str(pythonpath),
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return env


def run_pytest(workdir: Path, target: Path, ini: Path, timeout: float) -> TestRun:
    """Run pytest on ``target`` with ``workdir`` importable; never raises."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(ini),  # ignore any pytest config above the temp dir
        "--rootdir",
        str(workdir),
        str(target),
    ]
    try:
        proc = subprocess.run(  # nosec B603
            cmd,
            cwd=workdir,
            env=_child_env(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return TestRun(False, True, True, f"TIMEOUT after {timeout}s\n{out}")
    output = (proc.stdout + proc.stderr)[-MAX_OUTPUT_CHARS:]
    return TestRun(proc.returncode == 0, proc.returncode != 5, False, output)


def _patch(path: str, before: str, after: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return diff[:MAX_PATCH_CHARS]


def _candidate(
    task: Task,
    mode: str,
    system_prompt: str,
    user_prompt: str,
    completer: Completer | None,
    cassettes_dir: Path | None,
    result: dict,
) -> Completion | None:
    """The raw model-shaped output for ``mode`` (None for the no-op)."""
    if mode == "noop":
        return None
    if mode == "oracle":
        # The reference file is fed through the same normaliser as model
        # output, so the oracle also proves normalisation accepts a bare file.
        text = (task.solution_dir / task.target_file).read_text(encoding="utf-8")
        return Completion(text=text, finish_reason="stop")
    if mode == "replay":
        record = load_cassette(cassettes_dir, task.id)
        result["synthetic"] = bool(record.get("synthetic"))
        result["stale_cassette"] = record.get("prompt_sha256") != prompt_sha256(
            system_prompt, user_prompt
        )
        return cassette_completion(record)
    if mode == "live":
        return completer(system_prompt, user_prompt)
    raise ValueError(f"unknown mode {mode!r}")


def evaluate_task(
    task: Task,
    mode: str,
    *,
    completer: Completer | None = None,
    cassettes_dir: Path | None = None,
    timeout: float = 60.0,
    on_completion=None,
) -> dict:
    """Evaluate one task; returns a JSON-serialisable per-task result.

    ``on_completion(task, completion, system_prompt, user_prompt)`` is called
    after a live/replay model call (used to record cassettes).
    """
    started = time.perf_counter()
    result = {
        "task_id": task.id,
        "kind": task.kind,
        "difficulty": task.difficulty,
        "mode": mode,
        "resolved": False,
        "hidden_tests_passed": None,
        "regression": None,
        "output_rejected": False,
        "rejection_reason": None,
        "applied": False,
        "timed_out": False,
        "finish_reason": None,
        "latency_ms": None,
        "usage": None,
        "stale_cassette": None,
        "synthetic": None,
        "error": None,
        "patch": "",
        "test_output": "",
        "eval_seconds": None,
    }
    original = task.read_target()
    system_prompt, user_prompt = build_implementer_prompt(
        task.issue_text, task.target_file, original
    )

    try:
        completion = _candidate(
            task, mode, system_prompt, user_prompt, completer, cassettes_dir, result
        )
    except Exception as e:  # infra failure (missing cassette, network, ...)
        result["error"] = f"{type(e).__name__}: {e}"
        result["eval_seconds"] = round(time.perf_counter() - started, 3)
        return result

    code = None
    if completion is not None:
        result["finish_reason"] = completion.finish_reason
        result["latency_ms"] = completion.latency_ms
        result["usage"] = completion.usage
        if on_completion is not None:
            on_completion(task, completion, system_prompt, user_prompt)
        try:
            # Same call and same exception classes as implementer_node.
            code = normalize_generated_code(
                completion.text,
                path=task.target_file,
                finish_reason=completion.finish_reason,
            )
        except (ValueError, RuntimeError) as e:
            result["output_rejected"] = True
            result["rejection_reason"] = f"{type(e).__name__}: {e}"
            result["eval_seconds"] = round(time.perf_counter() - started, 3)
            return result

    with tempfile.TemporaryDirectory(prefix=f"eval-{task.id}-") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / "repo"
        hidden = tmp_path / "hidden_tests"
        shutil.copytree(task.repo_dir, work)
        shutil.copytree(task.hidden_tests_dir, hidden)
        ini = tmp_path / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        if code is not None:
            target = work / task.target_file
            target.parent.mkdir(parents=True, exist_ok=True)
            # Committed verbatim by implementer_node, so written verbatim here.
            target.write_text(code, encoding="utf-8", newline="")
            result["applied"] = True
            result["patch"] = _patch(task.target_file, original, code)

        existing = run_pytest(work, work, ini, timeout)
        accepted = run_pytest(work, hidden, ini, timeout)

    result["regression"] = (not existing.passed) if existing.collected else None
    result["hidden_tests_passed"] = accepted.passed
    result["timed_out"] = existing.timed_out or accepted.timed_out
    result["resolved"] = accepted.passed and not result["regression"]
    failures = [r.output for r in (existing, accepted) if not r.passed]
    result["test_output"] = "\n".join(failures)[-MAX_OUTPUT_CHARS:]
    result["eval_seconds"] = round(time.perf_counter() - started, 3)
    return result


def run_tasks(tasks: list[Task], mode: str, *, jobs: int = 1, **kwargs) -> list:
    """Evaluate tasks (optionally in parallel); results are sorted by id."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda t: evaluate_task(t, mode, **kwargs), tasks))
    return sorted(results, key=lambda r: r["task_id"])


def aggregate(results: list[dict]) -> dict:
    """Aggregate metrics; the resolve rate carries a 95% Wilson interval."""
    n = len(results)
    resolved = sum(1 for r in results if r["resolved"])
    rejected = sum(1 for r in results if r["output_rejected"])
    regressions = sum(1 for r in results if r["regression"])
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    usages = [r["usage"] for r in results if r["usage"]]
    lo, hi = wilson_interval(resolved, n)

    def rate(k: int):
        return round(k / n, 4) if n else None

    return {
        "n": n,
        "resolved": resolved,
        "resolve_rate": rate(resolved),
        "resolve_rate_ci95": [round(lo, 4), round(hi, 4)],
        "output_rejected": rejected,
        "output_rejected_rate": rate(rejected),
        "regressions": regressions,
        "regression_rate": rate(regressions),
        "timed_out": sum(1 for r in results if r["timed_out"]),
        "errors": sum(1 for r in results if r["error"]),
        "stale_cassettes": sum(1 for r in results if r["stale_cassette"]),
        "latency_ms_mean": round(mean(latencies)) if latencies else None,
        "latency_ms_median": round(median(latencies)) if latencies else None,
        "tokens": (
            {
                key: sum(u.get(key) or 0 for u in usages)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
            if usages
            else None
        ),
    }
