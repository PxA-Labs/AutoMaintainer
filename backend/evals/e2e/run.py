"""Command line: ``python -m evals.e2e.run`` (see docs/EVALS.md, "End-to-end").

Modes (agent side of every run; the engine, GitHub and Supabase fakes are
the same in all of them):

- ``oracle``: scripted agents that approve and write the reference fix (the
  Reviewer: always the right verdict). Must resolve everything.
- ``noop``: the Implementer returns the file unchanged (the Reviewer:
  approves everything). Must resolve nothing.
- ``replay``: answers from cassettes (``--cassettes``, default the committed
  ``cassettes/fixtures``). Offline and deterministic; used in CI.
- ``live``: Groq, with the keys in ``GROQ_API_KEY`` and
  ``GROQ_API_KEY_1``..``_19``. Manual only. It runs model-written code
  (verification and hidden tests) in the process that holds the key; see
  docs/EVALS.md before running it.

Exit codes: 0 success; 1 a gate failed (rates, ``--fail-on-stale``);
2 a harness error (missing key or cassette, a run that could not be scored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess  # nosec B404 - read-only git queries, fixed argv
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from evals.taskset import TaskError, taskset_hash

from . import cassettes
from .policies import (
    CassetteError,
    LiveTransport,
    ReplayTransport,
    ScriptedTransport,
    noop_pipeline,
    noop_reviewer,
    oracle_pipeline,
    oracle_reviewer,
)
from .pricing import load_prices
from .report import build_report, render_markdown
from .runner import run_pipeline_task, run_review_case
from .taskset import load_e2e_tasks, load_review_cases

MODES = ("oracle", "noop", "replay", "live")
SUITES = ("pipeline", "reviewer", "all")
LIVE_MODEL = "llama-3.3-70b-versatile"
KEY_VARIABLES = ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(1, 20)]


def _ids(value: str | None):
    if not value or value == "all":
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.e2e.run", description=__doc__)
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--suite", choices=SUITES, default="all")
    p.add_argument("--tasks", default="all", help="comma-separated task ids or 'all'")
    p.add_argument("--cases", default="all", help="Reviewer case ids or 'all'")
    p.add_argument(
        "--repeats",
        type=int,
        default=None,
        help="default: 3 live, the recorded runs in replay, 1 otherwise",
    )
    p.add_argument("--jobs", type=int, default=2, help="runs in flight at once")
    p.add_argument("--cassettes", type=Path, default=cassettes.FIXTURES_DIR)
    p.add_argument("--record", type=Path, default=None, help="save cassettes here")
    p.add_argument(
        "--model", default=None, help=f"every role's model (live: {LIVE_MODEL})"
    )
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--requests-per-minute", type=int, default=None)
    p.add_argument("--tokens-per-minute", type=int, default=None)
    p.add_argument("--prices", type=Path, default=None, help="JSON price table")
    p.add_argument("--timeout", type=float, default=120.0, help="per pytest run")
    p.add_argument("--out", type=Path, default=Path("e2e-results.json"))
    p.add_argument(
        "--markdown", type=Path, default=None, help="default: --out with .md"
    )
    p.add_argument("--min-resolve-rate", type=float, default=None)
    p.add_argument("--max-resolve-rate", type=float, default=None)
    p.add_argument("--min-merged-rate", type=float, default=None)
    p.add_argument("--max-false-approval-rate", type=float, default=None)
    p.add_argument("--min-false-approval-rate", type=float, default=None)
    p.add_argument("--max-false-rejection-rate", type=float, default=None)
    p.add_argument("--fail-on-stale", action="store_true")
    args = p.parse_args(argv)
    if args.repeats is None and args.mode != "replay":
        # Replay resolves it from the cassettes (_replay_repeats).
        args.repeats = 3 if args.mode == "live" else 1
    if (args.repeats is not None and args.repeats < 1) or args.jobs < 1:
        p.error("--repeats and --jobs must be at least 1")
    if args.mode == "live" and args.model is None:
        args.model = LIVE_MODEL
    return args


def _git(*args: str):
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(  # nosec B603
            [git, *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _live_keys() -> list:
    return [
        os.environ[name].strip()
        for name in KEY_VARIABLES
        if (os.environ.get(name) or "").strip()
    ]


class _Agents:
    """Builds the transport for each run of the chosen mode."""

    def __init__(self, args, live_transport_factory=None):
        self.args = args
        self.live_factory = live_transport_factory or (
            lambda a: LiveTransport(temperature=a.temperature, seed=a.seed)
        )

    def pipeline(self, item, repeat):
        mode = self.args.mode
        if mode == "oracle":
            return ScriptedTransport(oracle_pipeline(item))
        if mode == "noop":
            return ScriptedTransport(noop_pipeline(item))
        if mode == "replay":
            cassette = cassettes.load_for(self.args.cassettes, "pipeline", item.id)
            return ReplayTransport(cassette, repeat)
        return self.live_factory(self.args)

    def reviewer(self, case, repeat):
        mode = self.args.mode
        if mode == "oracle":
            return ScriptedTransport(oracle_reviewer(case))
        if mode == "noop":
            return ScriptedTransport(noop_reviewer(case))
        if mode == "replay":
            cassette = cassettes.load_for(self.args.cassettes, "reviewer", case.id)
            return ReplayTransport(cassette, repeat)
        return self.live_factory(self.args)


def _replay_repeats(args, tasks, cases) -> int:
    """The number of repeats a replay runs: the runs the cassettes recorded.

    Every selected cassette must hold the same number of runs, and an
    explicit ``--repeats`` must equal it. Replaying fewer would silently drop
    recorded samples and their spread; replaying more would count one
    recording several times. A missing cassette is skipped here and becomes
    a harness error in its own run. Raises ``CassetteError``.
    """
    counts: dict = {}
    selected = [("pipeline", t.id) for t in tasks] + [("reviewer", c.id) for c in cases]
    for kind, case_id in selected:
        try:
            cassette = cassettes.load_for(args.cassettes, kind, case_id)
        except FileNotFoundError:
            continue
        counts.setdefault(len(cassette.get("runs") or []), []).append(
            f"{kind}/{case_id}"
        )
    if len(counts) > 1:
        detail = "; ".join(
            f"{n}: {', '.join(ids[:3])}{' ...' if len(ids) > 3 else ''}"
            for n, ids in sorted(counts.items())
        )
        raise CassetteError(
            f"the selected cassettes recorded different numbers of runs ({detail}); "
            "replay them in separate invocations"
        )
    recorded = next(iter(counts), None)
    if recorded is None:
        return args.repeats or 1
    if recorded == 0:
        raise CassetteError("the selected cassettes have no runs")
    if args.repeats is not None and args.repeats != recorded:
        noun = "run" if recorded == 1 else "runs"
        raise CassetteError(
            f"--repeats {args.repeats}, but the cassettes recorded {recorded} {noun}; "
            "omit --repeats to replay every recorded run"
        )
    return recorded


def _error_record(kind, case_id, repeat, error, **extra):
    from .runner import _base_record, run_id_for

    record = _base_record(kind, case_id, repeat, run_id_for(kind, case_id, repeat))
    record["error"] = error
    if kind == "pipeline":
        record.update(
            attempted=False, pr_opened=False, fix_iterations=0, maintainer_approved=False,
            resolved=False, hidden_tests_passed=None, regression=None,
            merged_without_help=False, changed_files=[],
        )  # fmt: skip
    else:
        record.update(verdict=None, correct=None, **extra)
    return record


async def _run_all(args, tasks, cases, by_id, agents, prices, run_kwargs):
    semaphore = asyncio.Semaphore(args.jobs)
    recorded: dict = {}

    async def one(kind, key, repeat, build_transport, start):
        async with semaphore:
            try:
                transport = build_transport()
            except (FileNotFoundError, CassetteError) as e:
                return None, f"{type(e).__name__}: {e}"
            with tempfile.TemporaryDirectory(
                prefix=f"e2e-{key}-", ignore_cleanup_errors=True
            ) as tmp:
                record = await start(transport, Path(tmp))
            recorded[(kind, key, repeat)] = transport
            return record, None

    jobs = []
    for repeat in range(args.repeats):
        for item in tasks:
            jobs.append(("pipeline", item.id, repeat, item, None))
        for case in cases:
            jobs.append(("reviewer", case.id, repeat, by_id[case.task_id], case))

    async def launch(kind, key, repeat, item, case):
        if kind == "pipeline":
            record, error = await one(
                kind, key, repeat,
                lambda: agents.pipeline(item, repeat),
                lambda t, root: run_pipeline_task(
                    item, transport=t, root=root, repeat=repeat, prices=prices, **run_kwargs
                ),
            )  # fmt: skip
        else:
            run_kwargs_review = {k: v for k, v in run_kwargs.items() if k != "timeout"}
            record, error = await one(
                kind, key, repeat,
                lambda: agents.reviewer(case, repeat),
                lambda t, root: run_review_case(
                    item, case, transport=t, root=root, repeat=repeat, prices=prices,
                    **run_kwargs_review,
                ),
            )  # fmt: skip
        if record is None:
            extra = {}
            if kind == "reviewer":
                extra = dict(
                    task_id=case.task_id, expected_verdict=case.expected_verdict,
                    defect_class=case.defect_class,
                )  # fmt: skip
            record = _error_record(kind, key, repeat, error, **extra)
        return record

    records = await asyncio.gather(*(launch(*job) for job in jobs))
    order = {"pipeline": 0, "reviewer": 1}
    records = sorted(records, key=lambda r: (order[r["kind"]], r["id"], r["repeat"]))
    return records, recorded


def _save_cassettes(args, recorded):
    source = args.mode
    groups: dict = {}
    for (kind, key, repeat), transport in recorded.items():
        groups.setdefault((kind, key), {})[repeat] = [
            {k: v for k, v in call.items() if k not in ("stale", "recorded_latency_ms")}
            for call in transport.calls
        ]
    for (kind, key), runs in sorted(groups.items()):
        ordered = [runs[r] for r in sorted(runs)]
        note = f"recorded with python -m evals.e2e.run --mode {source} --record"
        make = (
            cassettes.pipeline_cassette
            if kind == "pipeline"
            else cassettes.reviewer_cassette
        )
        cassettes.save(
            args.record, make(key, ordered, source, model=args.model, note=note)
        )


def _gates(args, report) -> list:
    failures = []
    pipeline = report["pipeline"]["summary"]["overall"] if report["pipeline"] else None
    reviewer = report["reviewer"]["summary"]["overall"] if report["reviewer"] else None

    def check(summary, key, bound, cmp, label):
        if bound is None or summary is None:
            return
        value = (summary.get(key) or {}).get("mean")
        if value is None or not cmp(value, bound):
            failures.append(f"{label}: {value} vs {bound}")

    ge, le = (lambda v, b: v >= b), (lambda v, b: v <= b)
    check(pipeline, "resolved_rate", args.min_resolve_rate, ge, "min resolve rate")
    check(pipeline, "resolved_rate", args.max_resolve_rate, le, "max resolve rate")
    check(
        pipeline,
        "merged_without_help_rate",
        args.min_merged_rate,
        ge,
        "min merged rate",
    )
    check(
        reviewer,
        "false_approval_rate",
        args.max_false_approval_rate,
        le,
        "max false approval",
    )
    check(
        reviewer,
        "false_approval_rate",
        args.min_false_approval_rate,
        ge,
        "min false approval",
    )
    check(
        reviewer,
        "false_rejection_rate",
        args.max_false_rejection_rate,
        le,
        "max false rejection",
    )
    if args.fail_on_stale:
        stale = sum(r.get("stale_calls") or 0 for part in ("pipeline", "reviewer")
                    if report[part] for r in report[part]["runs"])  # fmt: skip
        if stale:
            failures.append(f"{stale} stale cassette calls")
    return failures


def main(argv=None, live_transport_factory=None) -> int:
    args = parse_args(argv)
    # verification.py starts repository tests with VERIFY_PYTHON (a host
    # setting, not per engine); default to this interpreter, which has pytest.
    os.environ.setdefault("VERIFY_PYTHON", sys.executable)
    try:
        pipeline_tasks = (
            load_e2e_tasks(_ids(args.tasks)) if args.suite != "reviewer" else []
        )
        cases = load_review_cases(_ids(args.cases)) if args.suite != "pipeline" else []
        task_by_id = {t.id: t for t in load_e2e_tasks()}
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.mode == "replay":
        try:
            args.repeats = _replay_repeats(args, pipeline_tasks, cases)
        except CassetteError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    used = {t.id for t in pipeline_tasks} | {c.task_id for c in cases}
    tasks = [task_by_id[i] for i in sorted(used)]
    run_kwargs = {"model": args.model, "timeout": args.timeout}
    if args.mode == "live":
        keys = _live_keys()
        if not keys:
            print("error: --mode live needs GROQ_API_KEY", file=sys.stderr)
            return 2
        run_kwargs["api_keys"] = tuple(keys)
        run_kwargs["requests_per_minute"] = args.requests_per_minute or 30
        run_kwargs["tokens_per_minute"] = args.tokens_per_minute or 6000
    else:
        for key, value in (
            ("requests_per_minute", args.requests_per_minute),
            ("tokens_per_minute", args.tokens_per_minute),
        ):
            if value:
                run_kwargs[key] = value
    try:
        prices, price_source = load_prices(args.prices)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    agents = _Agents(args, live_transport_factory)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records, recorded = asyncio.run(
        _run_all(args, pipeline_tasks, cases, task_by_id, agents, prices, run_kwargs)
    )
    synthetic = args.mode in ("oracle", "noop")
    if args.mode == "replay":
        synthetic = _replayed_synthetic(args, records)
    metadata = {
        "mode": args.mode,
        "suite": args.suite,
        "model": args.model,
        "repeats": args.repeats,
        "task_ids": [t.id for t in pipeline_tasks],
        "case_ids": [c.id for c in cases],
        "taskset_hash": taskset_hash([t.task for t in tasks]),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": started,
        "price_source": price_source,
        "synthetic": synthetic,
        "cassettes": str(args.cassettes) if args.mode == "replay" else None,
        "temperature": args.temperature if args.mode == "live" else None,
        "seed": args.seed if args.mode == "live" else None,
        "engine": {
            "merge_policy": "human",
            "max_iterations": 3,
            "verification": "local runner",
            "github": "git-backed fake",
            "supabase": "in-memory fake",
        },
    }
    pipeline = [r for r in records if r["kind"] == "pipeline"]
    reviewer = [r for r in records if r["kind"] == "reviewer"]
    report = build_report(
        metadata,
        pipeline if args.suite != "reviewer" else None,
        reviewer if args.suite != "pipeline" else None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = args.markdown or args.out.with_suffix(".md")
    markdown.write_text(render_markdown(report) + "\n", encoding="utf-8")
    if args.record is not None:
        _save_cassettes(args, recorded)
    print(render_markdown(report))
    errors = [r for r in records if r.get("error")]
    if errors:
        for r in errors:
            print(
                f"harness error in {r['kind']} {r['id']}#{r['repeat']}: {r['error']}",
                file=sys.stderr,
            )
        return 2
    failures = _gates(args, report)
    for failure in failures:
        print(f"gate failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _replayed_synthetic(args, records) -> bool:
    flags = []
    for r in records:
        try:
            flags.append(
                bool(
                    cassettes.load_for(args.cassettes, r["kind"], r["id"]).get(
                        "synthetic"
                    )
                )
            )
        except FileNotFoundError:
            continue
    return all(flags) if flags else True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
