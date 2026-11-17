"""The fake GitHub (git-backed) and fake Supabase the e2e harness runs on."""

import pytest
from github import GithubException

from evals.e2e.fakes import FakeGitHub, GitRepository, InMemorySupabase

FILES = {"pkg/mod.py": "x = 1\n", "tests/test_mod.py": "def test_x():\n    pass\n"}


@pytest.fixture
def gh(tmp_path):
    repo = GitRepository.create(tmp_path / "origin.git", "eval/demo", FILES)
    return FakeGitHub({"eval/demo": repo}), repo


def test_contents_come_from_the_default_branch(gh):
    client, repo = gh
    gh_repo = client.get_repo("eval/demo")
    assert gh_repo.default_branch == "main"
    found = gh_repo.get_contents("pkg/mod.py", ref="main")
    assert found.decoded_content == b"x = 1\n"
    assert repo.tree("main") == FILES


@pytest.mark.parametrize(
    "path", ["missing.py", "../outside.py", "pkg/../../x", "/pkg/mod.py", ""]
)
def test_unknown_or_escaping_paths_are_404(gh, path):
    client, _ = gh
    with pytest.raises(GithubException) as err:
        client.get_repo("eval/demo").get_contents(path, ref="main")
    assert err.value.status == 404


def test_unknown_repository_is_404(gh):
    client, _ = gh
    with pytest.raises(GithubException) as err:
        client.get_repo("eval/other")
    assert err.value.status == 404


def test_branch_commit_and_pull_request(gh):
    client, repo = gh
    gh_repo = client.get_repo("eval/demo")
    base = gh_repo.get_branch("main").commit.sha
    gh_repo.create_git_ref(ref="refs/heads/fix", sha=base)
    current = gh_repo.get_contents("pkg/mod.py", ref="fix")
    gh_repo.update_file(
        path="pkg/mod.py",
        message="fix",
        content="x = 2\n",
        sha=current.sha,
        branch="fix",
    )
    head = gh_repo.get_branch("fix").commit.sha
    assert head != base and len(head) == 40
    assert gh_repo.get_contents("pkg/mod.py", ref="main").decoded_content == b"x = 1\n"

    pr = gh_repo.create_pull(title="t", body="b", head="fix", base="main")
    assert gh_repo.get_pull(pr.number) is pr
    assert pr.head.sha == head and pr.head.ref == "fix"
    [changed] = pr.get_files()
    assert changed.filename == "pkg/mod.py" and changed.status == "modified"
    assert "-x = 1" in changed.patch and "+x = 2" in changed.patch
    assert list(gh_repo.get_pulls(state="all", head="eval:fix")) == [pr]
    pr.create_issue_comment("hello")
    assert repo.comments == [(pr.number, "hello")]


def test_scoring_takes_the_newest_pull_request_as_the_final_head(gh):
    # The engine reuses one PR across iterations today; if an iteration ever
    # opened another, scoring the first one would score a stale head.
    from evals.e2e.runner import final_pull

    client, repo = gh
    gh_repo = client.get_repo("eval/demo")
    assert final_pull(repo) is None
    base = gh_repo.get_branch("main").commit.sha
    opened = []
    for branch, value in (("first", "2"), ("second", "3")):
        gh_repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base)
        current = gh_repo.get_contents("pkg/mod.py", ref=branch)
        gh_repo.update_file(
            path="pkg/mod.py",
            message=branch,
            content=f"x = {value}\n",
            sha=current.sha,
            branch=branch,
        )
        opened.append(
            gh_repo.create_pull(title=branch, body="b", head=branch, base="main")
        )
    assert final_pull(repo) is opened[1]


def test_pull_request_without_changes_is_refused(gh):
    client, _ = gh
    gh_repo = client.get_repo("eval/demo")
    gh_repo.create_git_ref(
        ref="refs/heads/same", sha=gh_repo.get_branch("main").commit.sha
    )
    with pytest.raises(GithubException) as err:
        gh_repo.create_pull(title="t", body="b", head="same", base="main")
    assert err.value.status == 422


def test_commit_can_delete_a_file(gh):
    _, repo = gh
    repo.commit("main", {"tests/test_mod.py": None}, "drop the tests")
    assert repo.tree("main") == {"pkg/mod.py": "x = 1\n"}


def test_commits_are_deterministic(tmp_path):
    a = GitRepository.create(tmp_path / "a.git", "eval/demo", FILES)
    b = GitRepository.create(tmp_path / "b.git", "eval/demo", FILES)
    assert a.head("main") == b.head("main")


def test_issue_and_statuses(gh):
    client, repo = gh
    gh_repo = client.get_repo("eval/demo")
    issue = repo.add_issue("Title", "Body `pkg/mod.py`", author="writer")
    assert gh_repo.get_issue(number=issue.number).body == "Body `pkg/mod.py`"
    gh_repo.get_issue(number=issue.number).create_comment("ok")
    assert repo.comments == [(issue.number, "ok")]
    sha = gh_repo.get_branch("main").commit.sha
    gh_repo.get_commit(sha).create_status(state="success", description="d", context="c")
    assert [
        (s.context, s.state)
        for s in gh_repo.get_commit(sha).get_combined_status().statuses
    ] == [("c", "success")]


def test_clone_url_is_a_local_git_url(gh):
    _, repo = gh
    assert repo.clone_url.startswith("file:")


def test_in_memory_supabase_round_trip():
    db = InMemorySupabase()
    db.table("runs").upsert(
        {"id": "r1", "status": "running"}, on_conflict="id"
    ).execute()
    db.table("runs").update({"checkpoint": {"a": 1}}).eq("id", "r1").execute()
    rows = db.table("runs").select("checkpoint").eq("id", "r1").limit(1).execute().data
    assert rows == [{"id": "r1", "status": "running", "checkpoint": {"a": 1}}]
    db.table("logs").insert([{"x": 1}, {"x": 2}]).execute()
    assert len(db.rows("logs")) == 2
    # Inserted rows get a generated primary key, as PostgREST returns it.
    assert all(row["id"] for row in db.rows("logs"))
    assert db.table("runs").select("*").eq("id", "nope").execute().data == []
