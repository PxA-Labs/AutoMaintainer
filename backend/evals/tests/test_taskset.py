import json
import shutil

import pytest

from agents import extract_target_file
from evals.prompt import build_implementer_prompt
from evals.taskset import TASKS_DIR, TaskError, load_task, load_tasks, taskset_hash

TASKS = load_tasks()


def test_task_set_size_and_mix():
    assert 8 <= len(TASKS) <= 12
    assert {t.kind for t in TASKS} == {"bugfix", "feature"}
    assert {t.difficulty for t in TASKS} == {"easy", "medium", "hard"}


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_task_is_well_formed(task):
    # The production pipeline finds the target in the task text, so every
    # issue must name it in a form extract_target_file understands.
    assert extract_target_file(task.issue_text) == task.target_file
    assert task.provenance.startswith("original")
    assert any((task.repo_dir / "tests").glob("test_*.py")), "no pass-to-pass tests"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_hidden_tests_never_reach_the_prompt(task):
    system, user = build_implementer_prompt(
        task.issue_text, task.target_file, task.read_target()
    )
    prompt = system + user
    for hidden in task.hidden_tests_dir.glob("test_*.py"):
        assert hidden.stem not in prompt
        for line in hidden.read_text(encoding="utf-8").splitlines():
            if line.startswith("def test_"):
                assert line[4 : line.index("(")] not in prompt
    # The reference solution must not leak either.
    solution = (task.solution_dir / task.target_file).read_text(encoding="utf-8")
    assert solution not in prompt


def test_unknown_task_id_is_rejected():
    with pytest.raises(TaskError, match="unknown task ids"):
        load_tasks(ids=["does_not_exist"])


def test_subset_is_sorted_and_deduplicated():
    ids = [t.id for t in load_tasks(ids=["page_count", "backoff_cap", "page_count"])]
    assert ids == ["backoff_cap", "page_count"]


def _copy_task(tmp_path, task_id="page_count"):
    dest = tmp_path / task_id
    shutil.copytree(TASKS_DIR / task_id, dest)
    return dest


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "a\\b.py"])
def test_loader_rejects_unsafe_target_paths(tmp_path, bad):
    dest = _copy_task(tmp_path)
    meta = json.loads((dest / "task.json").read_text(encoding="utf-8"))
    meta["target_file"] = bad
    (dest / "task.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(TaskError):
        load_task(dest)


def test_loader_requires_hidden_tests_and_solution(tmp_path):
    dest = _copy_task(tmp_path)
    shutil.rmtree(dest / "hidden_tests")
    with pytest.raises(TaskError, match="hidden_tests"):
        load_task(dest)
    dest = _copy_task(tmp_path / "x")
    shutil.rmtree(dest / "solution")
    with pytest.raises(TaskError, match="solution"):
        load_task(dest)


def test_taskset_hash_ignores_line_endings_but_not_content(tmp_path):
    dest = _copy_task(tmp_path)
    before = taskset_hash([load_task(dest)])
    target = dest / "repo" / "listing" / "paging.py"
    # Start from an explicit LF file: a checkout with core.autocrlf=true is
    # already CRLF, and blindly replacing "\n" would produce "\r\r\n".
    lf = target.read_bytes().replace(b"\r\n", b"\n")
    target.write_bytes(lf)
    assert taskset_hash([load_task(dest)]) == before
    target.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert taskset_hash([load_task(dest)]) == before
    target.write_bytes(lf + b"# changed\n")
    assert taskset_hash([load_task(dest)]) != before
