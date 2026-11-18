"""Score a repository state against a task's hidden tests.

The same rules as ``evals.harness.evaluate_task``: the repository's own tests
run in one fresh copy (regression check), the hidden tests in a second fresh
copy that never existed on disk while the first ran, and a run passes only
when pytest exits 0, writes its JUnit report and exactly as many tests pass
as ``pytest --collect-only`` finds on the reference solution
(``evals.harness.run_pytest``).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from evals.harness import MAX_OUTPUT_CHARS, expected_test_counts, run_pytest
from evals.taskset import Task

from .fakes import safe_path


def snapshot_files(task: Task) -> dict:
    return {
        p.relative_to(task.repo_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(task.repo_dir.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def apply_changes(files: dict, changes: dict) -> dict:
    tree = dict(files)
    for path, text in changes.items():
        if text is None:
            tree.pop(path, None)
        else:
            tree[path] = text
    return tree


def _write_tree(root: Path, tree: dict) -> Path:
    work = root / "repo"
    work.mkdir()
    for path, text in tree.items():
        clean = safe_path(path)
        if clean is None:
            raise ValueError(f"unsafe path in the scored tree: {path!r}")
        target = work / clean
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return work


def score_tree(task: Task, tree: dict, timeout: float = 60.0) -> dict:
    """Score the full repository ``tree`` ({path: text}) against ``task``."""
    expected_repo, expected_hidden = expected_test_counts(task, timeout)
    with tempfile.TemporaryDirectory(prefix=f"e2e-{task.id}-") as tmp:
        work = _write_tree(Path(tmp), tree)
        existing = run_pytest(
            work, work, Path(tmp) / "pytest.ini", timeout, expected_repo or None
        )
    with tempfile.TemporaryDirectory(prefix=f"e2e-{task.id}-hidden-") as tmp:
        work = _write_tree(Path(tmp), tree)
        hidden = Path(tmp) / "hidden_tests"
        shutil.copytree(task.hidden_tests_dir, hidden)
        accepted = run_pytest(
            work, hidden, Path(tmp) / "pytest.ini", timeout, expected_hidden
        )
    regression = (not existing.passed) if existing.collected else None
    failures = [r.output for r in (existing, accepted) if not r.passed]
    return {
        "resolved": accepted.passed and not regression,
        "hidden_tests_passed": accepted.passed,
        "regression": regression,
        "timed_out": existing.timed_out or accepted.timed_out,
        "test_output": "\n".join(failures)[-MAX_OUTPUT_CHARS:],
    }


def score_changes(task: Task, changes: dict, timeout: float = 60.0) -> dict:
    """Score the snapshot with ``changes`` ({path: text or None}) applied."""
    return score_tree(task, apply_changes(snapshot_files(task), changes), timeout)
