"""Data validation for the end-to-end task set and the Reviewer change set.

For every task: the oracle fix passes the hidden tests, the unfixed snapshot
fails them without breaking its own tests, the issue names the target file,
and neither the hidden tests nor the fix can be reached through the fake
GitHub API or the clone the agents see. For every Reviewer case: a defective
change fails the hidden tests (or breaks the repository's tests), a correct
one resolves the task.
"""

import re

import pytest
from github import GithubException

from agents import extract_target_file
from evals.e2e.taskset import (
    ALLOWED_LICENSES,
    DEFECT_CLASSES,
    TASKS_DIR,
    load_e2e_tasks,
    load_review_cases,
    tree_size_bytes,
)
from evals.e2e.world import build_world
from evals.harness import evaluate_task
from evals.e2e.scoring import score_changes

TASKS = load_e2e_tasks()
CASES = load_review_cases()
_FILE_ACCESS = re.compile(
    r"\bopen\(|read_text|read_bytes|\binspect\b|getsource|__file__|"
    r"\bPath\(|\bos\.path\b|\bglob\b|importlib|__spec__|__loader__|"
    r"\bsubprocess\b|\bexec\(|\beval\("
)


def test_task_set_size_and_footprint():
    assert 20 <= len(TASKS) <= 30
    assert tree_size_bytes(TASKS_DIR) < 5 * 1024 * 1024
    assert len({t.task.id for t in TASKS}) == len(TASKS)


@pytest.mark.parametrize("item", TASKS, ids=lambda t: t.task.id)
def test_task_metadata(item):
    task = item.task
    assert item.source["license"] in ALLOWED_LICENSES
    assert re.fullmatch(
        r"https://github\.com/[\w.-]+/[\w.-]+", item.source["repository"]
    )
    assert re.fullmatch(r"[0-9a-f]{40}", item.source["commit"])
    assert item.source["path"].endswith(".py")
    assert (task.repo_dir / "LICENSE").is_file()
    assert item.defect
    assert extract_target_file(task.issue_text) == task.target_file
    for test_file in [
        *task.hidden_tests_dir.glob("*.py"),
        *(task.repo_dir / "tests").glob("*.py"),
    ]:
        source = test_file.read_text(encoding="utf-8")
        assert _FILE_ACCESS.search(source) is None, test_file.name
    for hidden in task.hidden_tests_dir.glob("test_*.py"):
        assert hidden.name.startswith("test_hidden_")
        assert (
            len(re.findall(r"^def test_", hidden.read_text(encoding="utf-8"), re.M))
            >= 2
        )


@pytest.mark.parametrize("item", TASKS, ids=lambda t: t.task.id)
def test_oracle_fix_passes_and_snapshot_fails(item):
    oracle = evaluate_task(item.task, "oracle")
    assert oracle["error"] is None
    assert oracle["resolved"], oracle["test_output"]
    noop = evaluate_task(item.task, "noop")
    assert noop["error"] is None
    assert noop["hidden_tests_passed"] is False
    assert noop["regression"] is False, noop["test_output"]


@pytest.mark.parametrize("item", TASKS, ids=lambda t: t.task.id)
def test_hidden_tests_and_fix_are_unreachable(item, tmp_path):
    task = item.task
    world = build_world(item, tmp_path)
    repo = world.repo
    hidden = {
        p.name: p.read_text(encoding="utf-8")
        for p in task.hidden_tests_dir.glob("*.py")
    }
    solution = (task.solution_dir / task.target_file).read_text(encoding="utf-8")
    # The clone: every blob and every path in the repository the agents and
    # the verification step clone.
    blobs = repo.all_blobs()
    for text in hidden.values():
        assert text not in blobs
    assert solution not in blobs
    paths = repo.all_paths()
    assert not any("hidden" in p or p.startswith("solution") for p in paths)
    for name in hidden:
        assert not any(p.endswith(name) for p in paths)
    # The API: the eval's task files are not addressable, even by traversal.
    api = world.github.get_repo(world.repo_name)
    for probe in (
        f"hidden_tests/{next(iter(hidden))}",
        f"../hidden_tests/{next(iter(hidden))}",
        f"../solution/{task.target_file}",
        "../task.json",
        "task.json",
    ):
        with pytest.raises(GithubException):
            api.get_contents(probe, ref="main")
    # The issue the agents read is the task's issue text and nothing else.
    issue = api.get_issue(world.issue_number)
    assert issue.title == task.title and issue.body == task.body
    assert item.defect not in issue.body


def test_review_case_set_shape():
    bad = [c for c in CASES if c.expected_verdict == "reject"]
    good = [c for c in CASES if c.expected_verdict == "approve"]
    assert 10 <= len(bad) <= 20
    assert len(good) >= 10
    assert {c.task_id for c in good} == {c.task_id for c in bad}
    assert all(c.defect_class in DEFECT_CLASSES for c in bad)
    assert len({c.defect_class for c in bad}) >= 5


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.defect_class == "deleted-test"], ids=lambda c: c.id
)
def test_a_deleted_test_case_also_claims_a_fix(case):
    # A test deletion next to an untouched target file is a no-op patch: the
    # hidden tests would catch it for the original regression alone. Pair
    # the deletion or weakening with a (partial) change to the target file,
    # so the case measures whether the Reviewer notices the test change.
    item = next(t for t in TASKS if t.task.id == case.task_id)
    target = item.task.target_file
    assert target in case.changed_files
    changed = case.changes(item.task)[target]
    assert changed not in (None, item.task.read_target())
    assert any(p.startswith("tests/") for p in case.changed_files + case.deleted_files)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_review_case_is_caught_or_resolves(case):
    item = next(t for t in TASKS if t.task.id == case.task_id)
    result = score_changes(item.task, case.changes(item.task))
    if case.expected_verdict == "reject":
        # The hidden tests themselves catch every defective change.
        assert result["hidden_tests_passed"] is False
        assert not result["resolved"]
    else:
        assert result["resolved"], result["test_output"]
