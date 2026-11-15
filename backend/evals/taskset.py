"""Task schema and loader.

A task lives in ``evals/tasks/<task_id>/``::

    task.json        id, title, body, target_file, kind, category, difficulty,
                     provenance
    repo/            the fixture repository (what the agent is allowed to see)
    hidden_tests/    acceptance tests; NEVER shown to the model
    solution/        reference version of the changed file(s), for the oracle

Pre-existing tests live inside ``repo/`` (conventionally ``repo/tests/``) and
are used to detect regressions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TASKS_DIR = Path(__file__).resolve().parent / "tasks"
REQUIRED_KEYS = (
    "id",
    "title",
    "body",
    "target_file",
    "kind",
    "category",
    "difficulty",
)
# Closed vocabularies, so a typo cannot silently create a new bucket in the
# per-kind / per-category breakdowns (docs/EVALS.md describes each value).
KINDS = ("bugfix", "feature", "refactor")
CATEGORIES = (
    "logic",
    "edge-case",
    "input-validation",
    "error-handling",
    "data-structure",
    "api-change",
)
DIFFICULTIES = ("easy", "medium", "hard")


class TaskError(ValueError):
    """A task directory is malformed."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    body: str
    target_file: str
    kind: str
    category: str
    difficulty: str
    provenance: str
    path: Path

    @property
    def repo_dir(self) -> Path:
        return self.path / "repo"

    @property
    def hidden_tests_dir(self) -> Path:
        return self.path / "hidden_tests"

    @property
    def solution_dir(self) -> Path:
        return self.path / "solution"

    @property
    def issue_text(self) -> str:
        """The task text handed to the Implementer (the pipeline's ``idea``)."""
        return f"{self.title}\n\n{self.body}"

    def read_target(self) -> str:
        # Text mode normalises CRLF checkouts so prompts (and therefore
        # cassette prompt hashes) are identical on every OS.
        return (self.repo_dir / self.target_file).read_text(encoding="utf-8")


def _check_relative(path: str) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path or not path:
        raise TaskError(f"target_file must be a relative POSIX path: {path!r}")


def load_task(task_dir: Path) -> Task:
    try:
        meta = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TaskError(f"{task_dir.name}: unreadable task.json: {e}") from e
    missing = [k for k in REQUIRED_KEYS if not meta.get(k)]
    if missing:
        raise TaskError(f"{task_dir.name}: task.json misses {missing}")
    if meta["id"] != task_dir.name:
        raise TaskError(f"{task_dir.name}: id {meta['id']!r} != directory name")
    _check_relative(meta["target_file"])
    for key, allowed in (
        ("kind", KINDS),
        ("category", CATEGORIES),
        ("difficulty", DIFFICULTIES),
    ):
        if meta[key] not in allowed:
            raise TaskError(
                f"{task_dir.name}: {key} {meta[key]!r} is not one of {allowed}"
            )
    task = Task(
        id=meta["id"],
        title=meta["title"],
        body=meta["body"],
        target_file=meta["target_file"],
        kind=meta["kind"],
        category=meta["category"],
        difficulty=meta["difficulty"],
        provenance=meta.get("provenance", ""),
        path=task_dir,
    )
    if not (task.repo_dir / task.target_file).is_file():
        raise TaskError(f"{task.id}: repo/{task.target_file} does not exist")
    if not any(task.hidden_tests_dir.glob("test_*.py")):
        raise TaskError(f"{task.id}: no hidden_tests/test_*.py")
    if not (task.solution_dir / task.target_file).is_file():
        raise TaskError(f"{task.id}: solution/{task.target_file} does not exist")
    return task


def load_tasks(tasks_dir: Path = TASKS_DIR, ids: list[str] | None = None) -> list:
    """Load tasks sorted by id; ``ids`` restricts (and must all exist)."""
    available = {p.name: p for p in tasks_dir.iterdir() if (p / "task.json").is_file()}
    if ids:
        unknown = sorted(set(ids) - set(available))
        if unknown:
            raise TaskError(f"unknown task ids: {unknown}")
        names = sorted(set(ids))
    else:
        names = sorted(available)
    return [load_task(available[name]) for name in names]


def taskset_hash(tasks: list[Task]) -> str:
    """Content hash of every file of the given tasks (line endings normalised),
    so two result files can prove they were scored on the same task versions."""
    digest = hashlib.sha256()
    for task in sorted(tasks, key=lambda t: t.id):
        for file in sorted(p for p in task.path.rglob("*") if p.is_file()):
            if "__pycache__" in file.parts:
                continue
            rel = file.relative_to(task.path.parent).as_posix()
            digest.update(rel.encode() + b"\0")
            digest.update(file.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]
