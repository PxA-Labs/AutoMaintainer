"""Score one Implementer attempt against one task.

For every task the harness:

1. resolves the target file from the issue text with
   ``agents.extract_target_file``, as ``implementer_node`` does when no
   target was planned; a missing or different path is a ``target_mismatch``
   (the pipeline would fail or edit the wrong file), not a harness error,
2. obtains a candidate file (``noop``: none; ``oracle``: the reference
   solution; ``replay``: a recorded cassette; ``live``: the model),
3. passes model-shaped output through ``agents.normalize_generated_code``
   exactly like ``implementer_node`` does (a rejection there is
   ``output_rejected``; nothing is applied),
4. writes the file into a throw-away copy of the fixture repository and runs
   the repository's own tests (regression check),
5. writes it again into a FRESH copy that also holds the hidden tests and
   runs those (acceptance check). The hidden tests never exist on disk while
   the candidate runs in step 4, and nothing that run leaves behind (a
   planted ``conftest.py``/``sitecustomize.py``, edited files) reaches step 5.

``resolved`` means the hidden tests pass AND the pre-existing tests still
pass (fail-to-pass and pass-to-pass, as defined in epic #414).

"Pass" is verified, not inferred from pytest's exit code: candidate code can
call ``os._exit(0)`` at import time and make pytest "succeed" without running
a test. A run passes only when pytest exits 0, its JUnit report exists, and
exactly the number of tests collected on the reference solution passed (no
failure, error or skip). See ``run_pytest``.

Isolation: the subprocess gets a minimal environment (no API keys or other
secrets), plugin autoloading off and a fixed hash seed. It is NOT a sandbox:
model-written code runs with the user's file-system and network access and
could, for example, read the parent process's environment
(``/proc/<ppid>/environ`` on Linux) or forge a report it can find. Only run
live evals on a machine/CI runner you are willing to expose to that code, and
keep secrets out of the scoring process (``--generate-only`` + replay).
"""

from __future__ import annotations

import difflib
import os
import re
import shutil

# Runs pytest on fixture copies with a fixed argv, never through a shell.
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from xml.etree import ElementTree  # nosec B405 - parses pytest's own report

from agents import extract_target_file, normalize_generated_code

from .llm import Completer, Completion, cassette_completion, load_cassette
from .prompt import build_implementer_prompt, prompt_sha256
from .stats import wilson_interval
from .taskset import Task, taskset_hash

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
    passed_count: int = 0


def _child_env(pythonpath: Path) -> dict:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(
        PYTHONPATH=str(pythonpath),
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return env


def _pytest_cmd(workdir: Path, target: Path, ini: Path, *extra: str) -> list:
    return [
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
        *extra,
        str(target),
    ]


def _count_passed(report: Path) -> int | None:
    """Test cases in a JUnit XML report that did not fail, error or skip;
    None when the report is missing or unreadable."""
    try:
        # Written by the pytest run we just started, into a directory created
        # for that run alone.
        root = ElementTree.parse(report).getroot()  # nosec B314
    except (OSError, ElementTree.ParseError):
        return None
    bad = {"failure", "error", "skipped"}
    return sum(
        1 for case in root.iter("testcase") if not any(c.tag in bad for c in case)
    )


def run_pytest(
    workdir: Path,
    target: Path,
    ini: Path,
    timeout: float,
    expected: int | None = None,
) -> TestRun:
    """Run pytest on ``target`` with ``workdir`` importable; never raises.

    The exit code alone is NOT trusted. A run passes only when pytest exited
    0, wrote its JUnit report, and exactly ``expected`` test cases passed
    (at least one when ``expected`` is None) with none failed, errored or
    skipped. Without this, ``os._exit(0)`` in the candidate scores as a pass.
    """
    with tempfile.TemporaryDirectory(prefix="eval-report-") as report_dir:
        report = Path(report_dir) / "junit.xml"
        try:
            proc = subprocess.run(  # nosec B603
                _pytest_cmd(workdir, target, ini, f"--junitxml={report}"),
                cwd=workdir,
                env=_child_env(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            return TestRun(False, True, True, f"TIMEOUT after {timeout}s\n{out}")
        passed_count = _count_passed(report)
    output = (proc.stdout + proc.stderr)[-MAX_OUTPUT_CHARS:]
    if proc.returncode == 5 and expected in (None, 0):
        return TestRun(False, False, False, output)
    if passed_count is None:
        problem = "no JUnit report (the interpreter exited before pytest finished)"
    elif expected is not None and passed_count != expected:
        problem = f"{passed_count} of {expected} expected tests passed"
    elif expected is None and passed_count == 0:
        problem = "no test passed"
    else:
        problem = None
    if proc.returncode == 0 and problem is None:
        return TestRun(True, True, False, output, passed_count)
    if problem is not None:
        output = f"HARNESS: {problem}; pytest exit code {proc.returncode}\n{output}"
    return TestRun(False, True, False, output, passed_count or 0)


_COLLECTED_RE = re.compile(r"^(\d+) tests? collected", re.MULTILINE)
_expected_cache: dict = {}
_expected_lock = threading.Lock()


def _collect_count(workdir: Path, target: Path, ini: Path, timeout: float) -> int:
    proc = subprocess.run(  # nosec B603
        _pytest_cmd(workdir, target, ini, "--collect-only"),
        cwd=workdir,
        env=_child_env(workdir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode == 5:
        return 0
    match = _COLLECTED_RE.search(proc.stdout)
    if proc.returncode != 0 or not match:
        tail = (proc.stdout + proc.stderr)[-500:]
        raise RuntimeError(f"cannot collect {target.name}: {tail}")
    return int(match.group(1))


def _stage(tmp_path: Path, task: Task, target_text: str | None) -> Path:
    """Copy the fixture repo to ``tmp_path/repo`` (optionally replacing the
    target file) and write an empty pytest.ini; returns the repo path."""
    work = tmp_path / "repo"
    shutil.copytree(task.repo_dir, work)
    if target_text is not None:
        target = work / task.target_file
        target.parent.mkdir(parents=True, exist_ok=True)
        # Committed verbatim by implementer_node, so written verbatim here.
        target.write_text(target_text, encoding="utf-8", newline="")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return work


def expected_test_counts(task: Task, timeout: float = 60.0) -> tuple[int, int]:
    """``(repo tests, hidden tests)`` collected on the reference solution.

    Collected (never executed) once per task version, keyed by the task's
    content hash, so no candidate can influence the expectation.
    """
    key = (str(task.path), taskset_hash([task]))
    with _expected_lock:
        if key in _expected_cache:
            return _expected_cache[key]
    solution = (task.solution_dir / task.target_file).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix=f"eval-collect-{task.id}-") as tmp:
        tmp_path = Path(tmp)
        work = _stage(tmp_path, task, solution)
        hidden = tmp_path / "hidden_tests"
        shutil.copytree(task.hidden_tests_dir, hidden)
        ini = tmp_path / "pytest.ini"
        counts = (
            _collect_count(work, work, ini, timeout),
            _collect_count(work, hidden, ini, timeout),
        )
    if counts[1] == 0:
        raise RuntimeError(f"task {task.id} has no collectable hidden tests")
    with _expected_lock:
        _expected_cache[key] = counts
    return counts


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
    score: bool = True,
) -> dict:
    """Evaluate one task; returns a JSON-serialisable per-task result.

    ``on_completion(task, completion, system_prompt, user_prompt)`` is called
    after a live/replay model call (used to record cassettes). With
    ``score=False`` the run stops right after that call: no candidate code is
    executed (``--generate-only``, so the process holding the API key never
    runs model-written code).
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
        "target_resolved": None,
        "target_mismatch": False,
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

    def done() -> dict:
        result["eval_seconds"] = round(time.perf_counter() - started, 3)
        return result

    # implementer_node: state["target_file_path"] or extract_target_file(idea).
    # The eval has no planner, so the issue text alone must name the file.
    resolved_target = extract_target_file(task.issue_text)
    result["target_resolved"] = resolved_target
    if resolved_target != task.target_file:
        result["target_mismatch"] = True
        return done()

    original = task.read_target()
    system_prompt, user_prompt = build_implementer_prompt(
        task.issue_text, task.target_file, original
    )

    try:
        completion = _candidate(
            task, mode, system_prompt, user_prompt, completer, cassettes_dir, result
        )
        expected_repo, expected_hidden = (
            expected_test_counts(task, timeout) if score else (None, None)
        )
    except Exception as e:  # infra failure (missing cassette, network, ...)
        result["error"] = f"{type(e).__name__}: {e}"
        return done()

    code = None
    if completion is not None:
        result["finish_reason"] = completion.finish_reason
        result["latency_ms"] = completion.latency_ms
        result["usage"] = completion.usage
        if on_completion is not None:
            on_completion(task, completion, system_prompt, user_prompt)
        if not score:
            return done()
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
            return done()
    if not score:
        return done()

    if code is not None:
        result["applied"] = True
        result["patch"] = _patch(task.target_file, original, code)

    with tempfile.TemporaryDirectory(prefix=f"eval-{task.id}-") as tmp:
        work = _stage(Path(tmp), task, code)
        existing = run_pytest(
            work, work, Path(tmp) / "pytest.ini", timeout, expected_repo or None
        )
    # A fresh copy: the hidden tests did not exist while the candidate ran
    # above, and whatever that run wrote to disk is gone.
    with tempfile.TemporaryDirectory(prefix=f"eval-{task.id}-hidden-") as tmp:
        work = _stage(Path(tmp), task, code)
        hidden = Path(tmp) / "hidden_tests"
        shutil.copytree(task.hidden_tests_dir, hidden)
        accepted = run_pytest(
            work, hidden, Path(tmp) / "pytest.ini", timeout, expected_hidden
        )

    result["regression"] = (not existing.passed) if existing.collected else None
    result["hidden_tests_passed"] = accepted.passed
    result["timed_out"] = existing.timed_out or accepted.timed_out
    result["resolved"] = accepted.passed and not result["regression"]
    failures = [r.output for r in (existing, accepted) if not r.passed]
    result["test_output"] = "\n".join(failures)[-MAX_OUTPUT_CHARS:]
    return done()


def run_tasks(tasks: list[Task], mode: str, *, jobs: int = 1, **kwargs) -> list:
    """Evaluate tasks (optionally in parallel); results are sorted by id."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda t: evaluate_task(t, mode, **kwargs), tasks))
    return sorted(results, key=lambda r: r["task_id"])


def aggregate(results: list[dict]) -> dict:
    """Aggregate metrics; the resolve rate carries a 95% Wilson interval.

    When any task errored (missing cassette, LLM failure, ...) the run is
    incomplete: ``resolve_rate`` and its interval are None rather than a rate
    deflated by unscored tasks (or inflated by dropping them, since errors
    need not be random). Counts are still reported.
    """
    n = len(results)
    errors = sum(1 for r in results if r["error"])
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
        "resolve_rate": None if errors else rate(resolved),
        "resolve_rate_ci95": None if errors else [round(lo, 4), round(hi, 4)],
        "output_rejected": rejected,
        "output_rejected_rate": rate(rejected),
        "regressions": regressions,
        "regression_rate": rate(regressions),
        "target_mismatches": sum(1 for r in results if r.get("target_mismatch")),
        "timed_out": sum(1 for r in results if r["timed_out"]),
        "errors": errors,
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
