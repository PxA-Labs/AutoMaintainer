"""The end-to-end task set and the Reviewer change set.

A task lives in ``evals/e2e/tasks/<task_id>/`` in the layout of
``evals/taskset.py`` (task.json, repo/, hidden_tests/, solution/). Its
task.json adds:

- ``source``: ``project``, ``repository`` (upstream URL), ``commit`` (the
  pinned upstream commit), ``path`` (the file's upstream path), ``license``
  (SPDX id) and ``license_file`` (the upstream license, copied into the
  snapshot as ``repo/LICENSE``);
- ``defect``: the regression injected into the snapshot. The oracle fix is
  the unmodified upstream file, so ``solution/`` reverts exactly this;
- ``harness_files``: snapshot files written for the eval rather than taken
  from upstream (the visible tests and empty package markers).

A Reviewer case lives in ``evals/e2e/reviewer/<case_id>/case.json``:
``task_id``, ``expected_verdict`` (``reject`` for a defective change,
``approve`` for a correct one), ``defect_class``, ``description``,
``changed_files`` (full new contents under ``files/``), ``deleted_files``
and, for correct cases, ``use_solution`` (the task's oracle fix).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evals.taskset import Task, TaskError, load_tasks

E2E_DIR = Path(__file__).resolve().parent
TASKS_DIR = E2E_DIR / "tasks"
REVIEWER_DIR = E2E_DIR / "reviewer"
ALLOWED_LICENSES = ("MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0")
DEFECT_CLASSES = (
    "wrong-logic",
    "missing-edge-case",
    "special-cased-values",
    "broken-api",
    "deleted-test",
    "injected-instruction",
    "swallowed-exception",
)
VERDICTS = ("approve", "reject")


@dataclass(frozen=True)
class E2ETask:
    task: Task
    source: dict
    defect: str
    harness_files: tuple

    @property
    def id(self) -> str:
        return self.task.id


def _load_e2e(task: Task) -> E2ETask:
    meta = json.loads((task.path / "task.json").read_text(encoding="utf-8"))
    source = meta.get("source") or {}
    missing = [
        k
        for k in ("project", "repository", "commit", "path", "license")
        if not source.get(k)
    ]
    if missing:
        raise TaskError(f"{task.id}: source misses {missing}")
    if source["license"] not in ALLOWED_LICENSES:
        raise TaskError(f"{task.id}: license {source['license']!r} is not allowed")
    if not meta.get("defect"):
        raise TaskError(f"{task.id}: task.json misses defect")
    return E2ETask(
        task=task,
        source=source,
        defect=meta["defect"],
        harness_files=tuple(meta.get("harness_files") or ()),
    )


def load_e2e_tasks(ids: list[str] | None = None, tasks_dir: Path = TASKS_DIR):
    """The e2e tasks sorted by id; ``ids`` restricts (all must exist)."""
    return [_load_e2e(t) for t in load_tasks(tasks_dir, ids)]


@dataclass(frozen=True)
class ReviewCase:
    id: str
    task_id: str
    expected_verdict: str
    defect_class: str | None
    description: str
    changed_files: tuple
    deleted_files: tuple
    use_solution: bool
    path: Path

    def changes(self, task: Task) -> dict:
        """{path: new text, or None to delete} applied on the snapshot."""
        if self.use_solution:
            solution = task.solution_dir / task.target_file
            return {task.target_file: solution.read_text(encoding="utf-8")}
        changes = {
            p: (self.path / "files" / p).read_text(encoding="utf-8")
            for p in self.changed_files
        }
        changes.update({p: None for p in self.deleted_files})
        return changes


def load_review_cases(
    ids: list[str] | None = None, reviewer_dir: Path = REVIEWER_DIR
) -> list:
    available = {
        p.name: p for p in reviewer_dir.iterdir() if (p / "case.json").is_file()
    }
    if ids:
        unknown = sorted(set(ids) - set(available))
        if unknown:
            raise TaskError(f"unknown review case ids: {unknown}")
    cases = []
    for name in sorted(set(ids) if ids else available):
        meta = json.loads((available[name] / "case.json").read_text(encoding="utf-8"))
        if meta.get("id") != name:
            raise TaskError(f"{name}: id {meta.get('id')!r} != directory name")
        if meta.get("expected_verdict") not in VERDICTS:
            raise TaskError(f"{name}: expected_verdict must be one of {VERDICTS}")
        defect_class = meta.get("defect_class")
        if meta["expected_verdict"] == "reject" and defect_class not in DEFECT_CLASSES:
            raise TaskError(f"{name}: defect_class {defect_class!r} is not known")
        case = ReviewCase(
            id=name,
            task_id=meta["task_id"],
            expected_verdict=meta["expected_verdict"],
            defect_class=defect_class,
            description=meta.get("description", ""),
            changed_files=tuple(meta.get("changed_files") or ()),
            deleted_files=tuple(meta.get("deleted_files") or ()),
            use_solution=bool(meta.get("use_solution")),
            path=available[name],
        )
        if not (case.use_solution or case.changed_files or case.deleted_files):
            raise TaskError(f"{name}: the case changes nothing")
        cases.append(case)
    return cases


def tree_size_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
