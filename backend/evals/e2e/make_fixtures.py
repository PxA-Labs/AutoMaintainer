"""Regenerate the committed replay fixtures: ``python -m evals.e2e.make_fixtures``.

The fixtures are recorded from the scripted ``oracle`` and ``noop`` agents
(no model is involved), mixed by a fixed rule so a replay has a known,
non-trivial score:

- pipeline: every third task (by sorted id, index % 3 == 2) replays the
  no-op agent, the others the oracle, so 2/3 of the tasks resolve;
- reviewer: every fourth defective change (index % 4 == 3) replays the
  rubber-stamp Reviewer (a false approval), all other cases the oracle.

Run it after a change to an agent prompt makes ``--fail-on-stale`` fail,
then review the diff: only prompt hashes should change. Real-model cassettes
are recorded with ``python -m evals.e2e.run --mode live --record DIR``.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import cassettes, run
from .taskset import load_e2e_tasks, load_review_cases


def pick_noop_pipeline(task_ids: list) -> set:
    return {tid for i, tid in enumerate(sorted(task_ids)) if i % 3 == 2}


def pick_noop_reviewer(cases) -> set:
    bad = sorted(c.id for c in cases if c.expected_verdict == "reject")
    return {cid for i, cid in enumerate(bad) if i % 4 == 3}


def expected_scores(task_ids: list, cases) -> dict:
    """What a full replay of the fixtures must score."""
    noop_tasks = pick_noop_pipeline(task_ids)
    noop_cases = pick_noop_reviewer(cases)
    bad = [c for c in cases if c.expected_verdict == "reject"]
    return {
        "resolved_rate": (len(task_ids) - len(noop_tasks)) / len(task_ids),
        "false_approval_rate": len(noop_cases) / len(bad),
        "false_rejection_rate": 0.0,
        "noop_tasks": sorted(noop_tasks),
        "noop_cases": sorted(noop_cases),
    }


def main(argv=None) -> int:
    target = Path(argv[0]) if argv else cassettes.FIXTURES_DIR
    tasks = [t.id for t in load_e2e_tasks()]
    cases = load_review_cases()
    with tempfile.TemporaryDirectory(prefix="e2e-fixtures-") as tmp:
        for mode in ("oracle", "noop"):
            code = run.main(
                [
                    "--mode", mode, "--jobs", "4",
                    "--record", str(Path(tmp) / mode),
                    "--out", str(Path(tmp) / f"{mode}.json"),
                ]
            )  # fmt: skip
            if code not in (0, 1):
                print(f"recording {mode} failed with exit code {code}", file=sys.stderr)
                return code
        if target.exists():
            shutil.rmtree(target)
        noop_tasks = pick_noop_pipeline(tasks)
        noop_cases = pick_noop_reviewer(cases)
        for kind, ids, noop in (
            ("pipeline", tasks, noop_tasks),
            ("reviewer", [c.id for c in cases], noop_cases),
        ):
            for cid in ids:
                source = "noop" if cid in noop else "oracle"
                cassette = cassettes.load_for(Path(tmp) / source, kind, cid)
                cassette["note"] = (
                    f"synthetic: scripted {source} agent, recorded by "
                    "python -m evals.e2e.make_fixtures (no model output)"
                )
                cassettes.save(target, cassette)
    scores = expected_scores(tasks, cases)
    print(json.dumps(scores, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
