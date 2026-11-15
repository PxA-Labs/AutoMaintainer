"""Paired comparison of two eval result files (e.g. two prompt versions).

Usage (from backend/)::

    python -m evals.compare before.json after.json

Both runs must cover the same tasks with the same task contents
(``taskset_hash``); otherwise the comparison is refused, because a different
task mix changes the rate without any change in the agent. Each task is a
pair (resolved in A?, resolved in B?). Only the discordant pairs carry
information about the difference, which the exact McNemar test evaluates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .stats import mcnemar_exact_p, wilson_interval

# Settings that must match for the difference to be attributable to the
# change under test. A mismatch is reported, not fatal.
_CONTROLLED = ("mode", "model", "temperature", "seed", "timeout_s")


class ComparisonError(ValueError):
    pass


def compare(a: dict, b: dict) -> dict:
    meta_a, meta_b = a["metadata"], b["metadata"]
    if meta_a["taskset_hash"] != meta_b["taskset_hash"]:
        raise ComparisonError(
            "results were produced on different task sets "
            f"({meta_a['taskset_hash']} vs {meta_b['taskset_hash']}); "
            "re-run both on the same tasks"
        )
    res_a = {r["task_id"]: bool(r["resolved"]) for r in a["tasks"]}
    res_b = {r["task_id"]: bool(r["resolved"]) for r in b["tasks"]}
    errored = sorted(r["task_id"] for r in a["tasks"] + b["tasks"] if r.get("error"))
    if errored:
        raise ComparisonError(f"runs contain harness errors for {errored}")

    only_a = sorted(t for t in res_a if res_a[t] and not res_b[t])
    only_b = sorted(t for t in res_a if res_b[t] and not res_a[t])
    n = len(res_a)
    k_a, k_b = sum(res_a.values()), sum(res_b.values())
    return {
        "n": n,
        "a": {"resolved": k_a, "ci95": [round(x, 4) for x in wilson_interval(k_a, n)]},
        "b": {"resolved": k_b, "ci95": [round(x, 4) for x in wilson_interval(k_b, n)]},
        "difference": round((k_b - k_a) / n, 4) if n else None,
        "only_a_resolved": only_a,
        "only_b_resolved": only_b,
        "mcnemar_exact_p": round(mcnemar_exact_p(len(only_a), len(only_b)), 4),
        "uncontrolled_settings": {
            key: [meta_a.get(key), meta_b.get(key)]
            for key in _CONTROLLED
            if meta_a.get(key) != meta_b.get(key)
        },
        "prompt_versions": [meta_a.get("prompt_version"), meta_b.get("prompt_version")],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.compare", description=__doc__)
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    args = p.parse_args(argv)
    a = json.loads(args.a.read_text(encoding="utf-8"))
    b = json.loads(args.b.read_text(encoding="utf-8"))
    try:
        report = compare(a, b)
    except ComparisonError as e:
        print(f"cannot compare: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
