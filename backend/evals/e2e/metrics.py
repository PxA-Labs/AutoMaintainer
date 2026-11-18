"""Metrics over repeated e2e runs.

Pipeline, per task (over its repeats): attempted, resolved and merged
without help as rates; fix iterations, tokens, cost and wall-clock latency as
mean and sample standard deviation.

Pipeline, overall: each metric is first computed per repeat across all tasks
(a rate, or a total for tokens and cost, or a mean for iterations and
latency), then reported as mean and standard deviation over the repeats, so
the spread shows run-to-run variation of the whole set. ``resolved_ci95`` is
a Wilson interval over all runs pooled; repeats of one task are not
independent, so it is narrower than the truth when repeats agree.

Reviewer: the false-approval rate (defective changes approved / defective
changes) and false-rejection rate (correct changes rejected / correct
changes), per repeat, then mean and standard deviation.

Any harness error makes the affected rates None: an incomplete run has no
honest rate (docs/EVALS.md).
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean, stdev
from typing import Iterable, Optional

from evals.stats import wilson_interval


def mean_std(values: Iterable) -> dict:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"mean": None, "std": None, "n": 0}
    return {
        "mean": round(fmean(vals), 6),
        "std": round(stdev(vals), 6) if len(vals) > 1 else None,
        "n": len(vals),
    }


def _rate(flags: list) -> Optional[float]:
    return round(sum(1 for f in flags if f) / len(flags), 6) if flags else None


def _total(values: list) -> Optional[float]:
    return None if not values or any(v is None for v in values) else sum(values)


def _by(records, key):
    groups = defaultdict(list)
    for r in records:
        groups[r[key]].append(r)
    return dict(sorted(groups.items()))


def summarize_pipeline(records: list) -> dict:
    per_task = {}
    for task_id, runs in _by(records, "id").items():
        per_task[task_id] = {
            "runs": len(runs),
            "errors": sum(1 for r in runs if r.get("error")),
            "attempted_rate": _rate([r["attempted"] for r in runs]),
            "resolved_rate": _rate([r["resolved"] for r in runs]),
            "merged_without_help_rate": _rate([r["merged_without_help"] for r in runs]),
            "fix_iterations": mean_std(r["fix_iterations"] for r in runs),
            "prompt_tokens": mean_std(r.get("prompt_tokens") for r in runs),
            "completion_tokens": mean_std(r.get("completion_tokens") for r in runs),
            "cost_usd": mean_std(r.get("cost_usd") for r in runs),
            "latency_s": mean_std(r.get("latency_s") for r in runs),
            "stale_calls": sum(r.get("stale_calls") or 0 for r in runs),
        }
    errors = sum(1 for r in records if r.get("error"))
    repeats = _by(records, "repeat")
    per_repeat = defaultdict(list)
    for runs in repeats.values():
        per_repeat["attempted_rate"].append(_rate([r["attempted"] for r in runs]))
        per_repeat["resolved_rate"].append(_rate([r["resolved"] for r in runs]))
        per_repeat["merged_without_help_rate"].append(
            _rate([r["merged_without_help"] for r in runs])
        )
        per_repeat["fix_iterations"].append(
            mean_std(r["fix_iterations"] for r in runs)["mean"]
        )
        per_repeat["latency_s"].append(
            mean_std(r.get("latency_s") for r in runs)["mean"]
        )
        for key in ("prompt_tokens", "completion_tokens", "cost_usd"):
            per_repeat[key].append(_total([r.get(key) for r in runs]))
    resolved = sum(1 for r in records if r["resolved"])
    lo, hi = wilson_interval(resolved, len(records)) if records else (0.0, 1.0)
    rate_keys = ("attempted_rate", "resolved_rate", "merged_without_help_rate")
    overall = {
        "tasks": len(per_task),
        "repeats": len(repeats),
        "runs": len(records),
        "errors": errors,
        "stale_calls": sum(r.get("stale_calls") or 0 for r in records),
        "resolved_runs": resolved,
        "resolved_ci95": None if errors else [round(lo, 4), round(hi, 4)],
    }
    for key, values in per_repeat.items():
        if key in rate_keys and errors:
            overall[key] = None
        elif key in ("prompt_tokens", "completion_tokens", "cost_usd") and any(
            v is None for v in values
        ):
            overall[key] = mean_std([])
        else:
            overall[key] = mean_std(values)
    return {"per_task": per_task, "overall": overall}


def summarize_reviewer(records: list) -> dict:
    per_case = {}
    for case_id, runs in _by(records, "id").items():
        per_case[case_id] = {
            "expected_verdict": runs[0]["expected_verdict"],
            "defect_class": runs[0].get("defect_class"),
            "runs": len(runs),
            "approvals": sum(1 for r in runs if r.get("verdict") == "approve"),
            "errors": sum(1 for r in runs if r.get("error")),
        }
    by_class = defaultdict(lambda: {"runs": 0, "approved": 0})
    for r in records:
        if r["expected_verdict"] == "reject":
            entry = by_class[r.get("defect_class")]
            entry["runs"] += 1
            entry["approved"] += r.get("verdict") == "approve"
    errors = sum(1 for r in records if r.get("error"))
    far, frr = [], []
    for runs in _by(records, "repeat").values():
        bad = [r for r in runs if r["expected_verdict"] == "reject"]
        good = [r for r in runs if r["expected_verdict"] == "approve"]
        far.append(_rate([r.get("verdict") == "approve" for r in bad]))
        frr.append(_rate([r.get("verdict") == "reject" for r in good]))
    return {
        "per_case": per_case,
        "by_defect_class": dict(sorted(by_class.items())),
        "overall": {
            "bad_changes": sum(
                1 for c in per_case.values() if c["expected_verdict"] == "reject"
            ),
            "correct_changes": sum(
                1 for c in per_case.values() if c["expected_verdict"] == "approve"
            ),
            "repeats": len({r["repeat"] for r in records}),
            "runs": len(records),
            "errors": errors,
            "false_approval_rate": None if errors else mean_std(far),
            "false_rejection_rate": None if errors else mean_std(frr),
            "latency_s": mean_std(r.get("latency_s") for r in records),
            "cost_usd": mean_std(
                [_total([r.get("cost_usd") for r in records])] if records else []
            ),
        },
    }
