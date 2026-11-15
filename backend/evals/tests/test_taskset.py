import json
import re
import shutil

import pytest

from agents import extract_target_file
from evals.prompt import build_implementer_prompt
from evals.taskset import (
    CATEGORIES,
    DIFFICULTIES,
    KINDS,
    TASKS_DIR,
    TaskError,
    load_task,
    load_tasks,
    taskset_hash,
)

TASKS = load_tasks()


def test_task_set_size_and_mix():
    # At least 30 tasks, so a resolve rate's 95% Wilson interval is at most
    # about +-17 points instead of +-25 at n=10 (docs/EVALS.md).
    assert len(TASKS) >= 30
    # Every kind, category and difficulty is represented, none dominates.
    for attr, allowed in (
        ("kind", KINDS),
        ("category", CATEGORIES),
        ("difficulty", DIFFICULTIES),
    ):
        values = [getattr(t, attr) for t in TASKS]
        assert set(values) == set(allowed), attr
        assert max(values.count(v) for v in allowed) <= 0.7 * len(TASKS), attr


# Ways a test could look at files instead of behaviour. A test that reads the
# target's source (or any file in the fixture) can be steered by the candidate,
# which controls the target file: e.g. a comment that satisfies a grep.
_FILE_ACCESS = re.compile(
    r"\bopen\(|read_text|read_bytes|\binspect\b|getsource|__file__|"
    r"\bPath\(|\bos\.path\b|\bglob\b|importlib|__spec__|__loader__|"
    r"\bsubprocess\b|\bexec\(|\beval\("
)


def _test_files(task):
    return [
        *task.hidden_tests_dir.glob("test_*.py"),
        *(task.repo_dir / "tests").glob("test_*.py"),
    ]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_task_is_well_formed(task):
    # The production pipeline finds the target in the task text, so every
    # issue must name it in a form extract_target_file understands.
    assert extract_target_file(task.issue_text) == task.target_file
    assert task.provenance.startswith("original")
    assert any((task.repo_dir / "tests").glob("test_*.py")), "no pass-to-pass tests"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_tests_check_behaviour_not_files(task):
    for path in _test_files(task):
        source = path.read_text(encoding="utf-8")
        match = _FILE_ACCESS.search(source)
        assert match is None, f"{path.name} uses {match and match.group()!r}"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_hidden_tests_exercise_the_target(task):
    # Hidden tests import the target module (not a helper the candidate could
    # leave alone) and hold several tests, so neither a stub nor a single
    # hard-coded answer satisfies them. Noop/oracle baselines check the rest.
    module = task.target_file[: -len(".py")].replace("/", ".")
    package, _, name = module.rpartition(".")
    imports = [rf"from {re.escape(module)} import ", rf"import {re.escape(module)}\b"]
    if package:
        imports.append(rf"from {re.escape(package)} import .*\b{name}\b")
    for hidden in task.hidden_tests_dir.glob("test_*.py"):
        source = hidden.read_text(encoding="utf-8")
        assert any(re.search(p, source) for p in imports), hidden.name
        assert len(re.findall(r"^def test_", source, re.MULTILINE)) >= 2


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


@pytest.mark.parametrize(
    "key, value",
    [("kind", "chore"), ("category", "misc"), ("difficulty", "trivial")],
)
def test_loader_rejects_unknown_vocabulary(tmp_path, key, value):
    dest = _copy_task(tmp_path)
    meta = json.loads((dest / "task.json").read_text(encoding="utf-8"))
    meta[key] = value
    (dest / "task.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(TaskError, match=key):
        load_task(dest)


def test_loader_requires_category(tmp_path):
    dest = _copy_task(tmp_path)
    meta = json.loads((dest / "task.json").read_text(encoding="utf-8"))
    del meta["category"]
    (dest / "task.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(TaskError, match="category"):
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
