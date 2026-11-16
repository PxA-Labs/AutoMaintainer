"""IDE file writes never commit to the default or a protected branch (#139).

POST /repo/{name}/file, POST /repo/{name}/file/create and DELETE
/repo/{name}/file used to call PyGithub without a branch, which commits to the
repository's default branch. Every test here checks the branch each GitHub
write was actually sent to.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from github import GithubException, UnknownObjectException

import agents
import main
from main import app

client = TestClient(app)

ORG, USER = "org-1", "user-1"


@pytest.fixture(autouse=True)
def member(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(agents, "supabase", MagicMock())

    async def user():
        return {"user_id": USER, "org_id": ORG, "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    app.dependency_overrides[main.require_repo_access] = lambda: ORG
    yield
    app.dependency_overrides.clear()


def _missing_branch(*_a, **_k):
    raise UnknownObjectException(404, {"message": "Branch not found"}, None)


@pytest.fixture
def repo(monkeypatch):
    """A GitHub repo whose default branch is main and has no other branches."""
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.return_value = SimpleNamespace(path="a.py", sha="s")
    repo.get_branch.side_effect = _missing_branch
    repo.get_git_ref.return_value = SimpleNamespace(
        object=SimpleNamespace(sha="base-sha")
    )
    monkeypatch.setattr(main, "Github", lambda *_: MagicMock(get_repo=lambda _: repo))
    return repo


WORKING_BRANCH = main._ide_working_branch(ORG, USER)

# What the dashboard IDE sends today (dashboard/src/components/WebIDE.tsx):
# no branch field at all.
SAVE = ("post", "/repo/o/r/file", {"json": {"file_path": "a.py", "content": "x"}})
CREATE = ("post", "/repo/o/r/file/create", {"json": {"file_path": "b.py"}})
DELETE = ("delete", "/repo/o/r/file?file_path=a.py", {})
WRITES = [SAVE, CREATE, DELETE]


def _send(method, path, kwargs, branch=None):
    kwargs = {k: dict(v) for k, v in kwargs.items()}
    if branch is not None:
        if method == "delete":
            path = f"{path}&branch={branch}"
        else:
            kwargs["json"]["branch"] = branch
    return getattr(client, method)(path, **kwargs)


def _write_branches(repo) -> list:
    """The ``branch`` keyword of every GitHub write that was made."""
    calls = (
        repo.update_file.call_args_list
        + repo.create_file.call_args_list
        + repo.delete_file.call_args_list
    )
    return [call.kwargs.get("branch") for call in calls]


def test_working_branch_is_stable_per_user_and_hides_ids():
    assert WORKING_BRANCH == main._ide_working_branch(ORG, USER)
    assert WORKING_BRANCH != main._ide_working_branch(ORG, "someone-else")
    assert WORKING_BRANCH.startswith("automaintainer/ide-")
    assert USER not in WORKING_BRANCH and ORG not in WORKING_BRANCH


@pytest.mark.parametrize("method,path,kwargs", WRITES)
def test_dashboard_call_without_branch_commits_to_working_branch(
    repo, method, path, kwargs
):
    """The IDE keeps working unchanged, but its writes no longer reach main."""
    response = _send(method, path, kwargs)

    assert response.status_code == 200, response.text
    assert response.json()["branch"] == WORKING_BRANCH
    assert _write_branches(repo) == [WORKING_BRANCH]
    # Created from the default branch's head, since it did not exist yet.
    repo.get_git_ref.assert_called_once_with("heads/main")
    repo.create_git_ref.assert_called_once_with(
        ref=f"refs/heads/{WORKING_BRANCH}", sha="base-sha"
    )


def test_save_reads_the_file_from_the_target_branch(repo):
    """The blob sha must come from the branch being updated, not from main."""
    _send(*SAVE)

    repo.get_contents.assert_called_once_with("a.py", ref=WORKING_BRANCH)


@pytest.mark.parametrize("method,path,kwargs", WRITES)
def test_explicit_existing_branch_is_used_as_is(repo, method, path, kwargs):
    repo.get_branch.side_effect = None
    repo.get_branch.return_value = SimpleNamespace(protected=False)

    response = _send(method, path, kwargs, branch="feature/issue-12-abc")

    assert response.status_code == 200, response.text
    assert _write_branches(repo) == ["feature/issue-12-abc"]
    repo.create_git_ref.assert_not_called()


@pytest.mark.parametrize("method,path,kwargs", WRITES)
@pytest.mark.parametrize("branch", ["main", "Main", "MAIN"])
def test_default_branch_is_refused(repo, method, path, kwargs, branch):
    """Compared case-insensitively: "Main" must not slip past the guard."""
    response = _send(method, path, kwargs, branch=branch)

    assert response.status_code == 409, response.text
    assert _write_branches(repo) == []
    repo.create_git_ref.assert_not_called()


@pytest.mark.parametrize("method,path,kwargs", WRITES)
@pytest.mark.parametrize("protected", [True, None, MagicMock()])
def test_protected_branch_is_refused(repo, method, path, kwargs, protected):
    """Anything but protected=False is treated as protected (fails closed)."""
    repo.get_branch.side_effect = None
    repo.get_branch.return_value = SimpleNamespace(protected=protected)

    response = _send(method, path, kwargs, branch="release")

    assert response.status_code == 409, response.text
    assert _write_branches(repo) == []


@pytest.mark.parametrize(
    "branch",
    [
        "../main",
        "a..b",
        "-x",
        ".hidden",
        "x.lock",
        "a/.b",
        "a b",
        "a~1",
        "x" * 201,
        # Ref-qualified or symbolic names would resolve to the default
        # branch on GitHub and bypass the default-branch comparison.
        "refs/heads/main",
        "REFS/heads/main",
        "heads/main",
        "Heads/main",
        "tags/v1",
        "remotes/origin/main",
        "HEAD",
        "head",
        "FETCH_HEAD",
        "orig_head",
    ],
)
def test_invalid_branch_name_is_rejected_before_github(monkeypatch, branch):
    def no_github(*_a):
        raise AssertionError("GitHub must not be contacted")

    monkeypatch.setattr(main, "Github", no_github)

    response = _send(*SAVE, branch=branch)

    assert response.status_code == 400, response.text


@pytest.mark.parametrize("method,path,kwargs", WRITES)
@pytest.mark.parametrize("branch", ["refs/heads/main", "heads/main", "HEAD"])
def test_ref_qualified_default_branch_is_never_written(
    repo, method, path, kwargs, branch
):
    """The reviewer's bypass: no ref is created and nothing is committed."""
    response = _send(method, path, kwargs, branch=branch)

    assert response.status_code == 400, response.text
    assert _write_branches(repo) == []
    repo.create_git_ref.assert_not_called()


def test_branch_names_merely_containing_head_are_allowed():
    for name in ("feature/head-fix", "headless", "my_heads/x", "fix/HEAD_cleanup"):
        main._validate_branch_name(name)


@pytest.mark.parametrize("default", [None, MagicMock()])
def test_unknown_default_branch_fails_closed(repo, default):
    repo.default_branch = default

    response = _send(*SAVE)

    assert response.status_code == 409, response.text
    assert _write_branches(repo) == []


def test_branch_lookup_failure_is_not_treated_as_missing(repo):
    """Only a 404 creates the branch; another error must not write anywhere."""
    repo.get_branch.side_effect = GithubException(500, {"message": "boom"}, None)

    response = _send(*SAVE)

    assert response.status_code == 502
    assert _write_branches(repo) == []
    repo.create_git_ref.assert_not_called()


def test_concurrent_branch_creation_is_tolerated(repo):
    """Two saves racing to create the working branch: the loser proceeds."""
    repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, None
    )

    response = _send(*SAVE)

    assert response.status_code == 200, response.text
    assert _write_branches(repo) == [WORKING_BRANCH]
