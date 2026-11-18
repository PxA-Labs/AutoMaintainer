"""The e2e report: one JSON document and a Markdown summary of it."""

from __future__ import annotations

from typing import Optional

from .metrics import summarize_pipeline, summarize_reviewer


def build_report(metadata: dict, pipeline: Optional[list], reviewer: Optional[list]):
    return {
        "metadata": metadata,
        "pipeline": (
            None
            if pipeline is None
            else {"summary": summarize_pipeline(pipeline), "runs": pipeline}
        ),
        "reviewer": (
            None
            if reviewer is None
            else {"summary": summarize_reviewer(reviewer), "runs": reviewer}
        ),
    }


def _ms(value: Optional[dict], digits: int = 2, pct: bool = False) -> str:
    if not value or value.get("mean") is None:
        return "n/a"
    scale = 100 if pct else 1
    unit = "%" if pct else ""
    text = f"{value['mean'] * scale:.{digits}f}{unit}"
    if value.get("std") is not None:
        text += f" ± {value['std'] * scale:.{digits}f}{unit}"
    return text


def _pct(rate: Optional[float]) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def render_markdown(report: dict) -> str:
    meta = report["metadata"]
    lines = ["# AutoMaintainer end-to-end evaluation", ""]
    for key in ("mode", "model", "repeats", "git_sha", "taskset_hash", "started_at"):
        if meta.get(key) is not None:
            lines.append(f"- **{key}**: {meta[key]}")
    if meta.get("price_source"):
        lines.append(f"- **prices**: {meta['price_source']}")
    if meta.get("synthetic"):
        lines.append(
            "- **note**: scripted or synthetic answers; these numbers check the "
            "harness, not a model"
        )
    lines.append("")

    pipeline = report.get("pipeline")
    if pipeline:
        overall = pipeline["summary"]["overall"]
        lines += [
            "## Pipeline",
            "",
            f"{overall['tasks']} tasks x {overall['repeats']} repeats = "
            f"{overall['runs']} runs, {overall['errors']} harness errors, "
            f"{overall['stale_calls']} stale cassette calls. Mean ± standard "
            "deviation over repeats.",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Attempted | {_ms(overall.get('attempted_rate'), 1, True)} |",
            f"| Resolved (hidden tests pass on the PR head) | {_ms(overall.get('resolved_rate'), 1, True)} |",
            f"| Merged without help (approved and resolved) | {_ms(overall.get('merged_without_help_rate'), 1, True)} |",
            f"| Fix iterations per run | {_ms(overall.get('fix_iterations'))} |",
            f"| Prompt tokens per repeat | {_ms(overall.get('prompt_tokens'), 0)} |",
            f"| Completion tokens per repeat | {_ms(overall.get('completion_tokens'), 0)} |",
            f"| Cost per repeat (USD) | {_ms(overall.get('cost_usd'), 4)} |",
            f"| Wall-clock latency per run (s) | {_ms(overall.get('latency_s'), 1)} |",
            "",
            "| Task | Runs | Resolved | Merged w/o help | Iterations | Latency (s) | Cost (USD) |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for task_id, s in pipeline["summary"]["per_task"].items():
            lines.append(
                f"| {task_id} | {s['runs']} | {_pct(s['resolved_rate'])} | "
                f"{_pct(s['merged_without_help_rate'])} | {_ms(s['fix_iterations'], 1)} | "
                f"{_ms(s['latency_s'], 1)} | {_ms(s['cost_usd'], 4)} |"
            )
        lines.append("")

    reviewer = report.get("reviewer")
    if reviewer:
        overall = reviewer["summary"]["overall"]
        lines += [
            "## Reviewer (Maintainer step)",
            "",
            f"{overall['bad_changes']} defective and {overall['correct_changes']} "
            f"correct changes x {overall['repeats']} repeats, "
            f"{overall['errors']} harness errors.",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| False-approval rate | {_ms(overall.get('false_approval_rate'), 1, True)} |",
            f"| False-rejection rate | {_ms(overall.get('false_rejection_rate'), 1, True)} |",
            "",
            "| Defect class | Runs | Approved |",
            "| --- | --- | --- |",
        ]
        for name, entry in reviewer["summary"]["by_defect_class"].items():
            lines.append(f"| {name} | {entry['runs']} | {entry['approved']} |")
        lines.append("")
    return "\n".join(lines)
