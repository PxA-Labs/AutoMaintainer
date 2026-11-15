"""Run the Implementer eval and write a JSON results file.

Usage (from backend/)::

    python -m evals.run --mode replay --out results.json
    python -m evals.run --mode oracle --min-resolve-rate 1.0
    python -m evals.run --mode noop --max-resolve-rate 0.0
    GROQ_API_KEY=... python -m evals.run --mode live --tasks slug_hyphens,page_count

Exit codes: 0 success, 1 a --min/--max-resolve-rate gate failed,
2 harness/infra errors (missing API key or cassette, LLM call failed).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil

# Read-only git queries with a fixed argv, never through a shell.
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import HARNESS_VERSION
from .harness import MODES, aggregate, run_tasks
from .llm import CASSETTES_DIR, DEFAULT_MODEL, groq_completer, save_cassette
from .prompt import PROMPT_VERSION, prompt_sha256
from .taskset import TASKS_DIR, load_tasks, taskset_hash


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(  # nosec B603
            [git, *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _display_path(path: Path) -> str:
    """``path`` relative to backend/ when inside it (keeps results portable)."""
    backend = Path(__file__).resolve().parent.parent
    try:
        return path.resolve().relative_to(backend).as_posix()
    except ValueError:
        return path.as_posix()


def _cassette_settings(cassettes_dir: Path, task_ids: list[str]) -> dict:
    """Model settings recorded in the replayed cassettes (sorted unique values)."""
    seen: dict[str, set] = {"model": set(), "temperature": set(), "seed": set()}
    for task_id in task_ids:
        path = cassettes_dir / f"{task_id}.json"
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            for key in seen:
                seen[key].add(json.dumps(record.get(key)))
    return {k: [json.loads(v) for v in sorted(vals)] for k, vals in seen.items()}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__)
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--tasks", default="all", help="comma-separated task ids, or 'all'")
    p.add_argument("--tasks-dir", type=Path, default=TASKS_DIR)
    p.add_argument(
        "--cassettes",
        type=Path,
        default=CASSETTES_DIR,
        help="cassette directory replayed in --mode replay",
    )
    p.add_argument(
        "--record",
        type=Path,
        default=None,
        help="live mode: also write a cassette per task into this directory",
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--timeout", type=float, default=60.0, help="seconds per pytest")
    p.add_argument("--jobs", type=int, default=1, help="tasks evaluated in parallel")
    p.add_argument("--out", type=Path, default=Path("results.json"))
    p.add_argument("--min-resolve-rate", type=float, default=None)
    p.add_argument("--max-resolve-rate", type=float, default=None)
    return p.parse_args(argv)


def main(argv=None, completer=None) -> int:
    """CLI entry point. ``completer`` overrides the live LLM client (tests)."""
    args = parse_args(argv)
    ids = None if args.tasks == "all" else [t for t in args.tasks.split(",") if t]
    tasks = load_tasks(args.tasks_dir, ids)

    if args.mode == "live" and completer is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            print("GROQ_API_KEY is not set; live mode needs it.", file=sys.stderr)
            return 2
        completer = groq_completer(api_key, args.model, args.temperature, args.seed)

    def record_cassette(task, completion, system_prompt, user_prompt):
        save_cassette(
            args.record,
            task.id,
            completion,
            prompt_sha256=prompt_sha256(system_prompt, user_prompt),
            prompt_version=PROMPT_VERSION,
            model=args.model,
            temperature=args.temperature,
            seed=args.seed,
        )

    on_completion = record_cassette if args.mode == "live" and args.record else None
    started = datetime.now(timezone.utc)
    results = run_tasks(
        tasks,
        args.mode,
        jobs=args.jobs,
        completer=completer,
        cassettes_dir=args.cassettes,
        timeout=args.timeout,
        on_completion=on_completion,
    )
    summary = aggregate(results)

    metadata = {
        "harness_version": HARNESS_VERSION,
        "mode": args.mode,
        "prompt_version": PROMPT_VERSION,
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "task_ids": [t.id for t in tasks],
        "taskset_hash": taskset_hash(tasks),
        "timeout_s": args.timeout,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": started.isoformat(timespec="seconds"),
    }
    if args.mode == "live":
        metadata.update(model=args.model, temperature=args.temperature, seed=args.seed)
    elif args.mode == "replay":
        metadata["cassettes"] = _display_path(args.cassettes)
        metadata["synthetic_cassettes"] = any(r["synthetic"] for r in results)
        metadata.update(_cassette_settings(args.cassettes, metadata["task_ids"]))
    else:
        metadata["baseline"] = args.mode  # no model involved

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"metadata": metadata, "aggregate": summary, "tasks": results}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )

    for r in results:
        status = (
            "ERROR"
            if r["error"]
            else (
                "RESOLVED"
                if r["resolved"]
                else "REJECTED" if r["output_rejected"] else "failed"
            )
        )
        extra = " regression" if r["regression"] else ""
        extra += " stale-cassette" if r["stale_cassette"] else ""
        print(f"{r['task_id']:<24} {status}{extra}")
    lo, hi = summary["resolve_rate_ci95"]
    print(
        f"{args.mode}: resolved {summary['resolved']}/{summary['n']} "
        f"(rate {summary['resolve_rate']}, 95% Wilson CI [{lo}, {hi}]); "
        f"rejected {summary['output_rejected']}, regressions "
        f"{summary['regressions']}, errors {summary['errors']} -> {args.out}"
    )
    if metadata.get("synthetic_cassettes"):
        print(
            "NOTE: replayed cassettes are synthetic fixtures, not real model "
            "outputs; these numbers measure the harness, not the model."
        )

    if summary["errors"]:
        for r in results:
            if r["error"]:
                print(f"error in {r['task_id']}: {r['error']}", file=sys.stderr)
        return 2
    rate = summary["resolve_rate"] or 0.0
    if args.min_resolve_rate is not None and rate < args.min_resolve_rate:
        print(f"FAIL: resolve rate {rate} < {args.min_resolve_rate}", file=sys.stderr)
        return 1
    if args.max_resolve_rate is not None and rate > args.max_resolve_rate:
        print(f"FAIL: resolve rate {rate} > {args.max_resolve_rate}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
