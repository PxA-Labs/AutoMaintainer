"""Regression tests (#394, #416): a redelivered or retried run never duplicates
GitHub side effects.

Celery acknowledges late, so after a worker crash the same run_id runs the
graph again. These tests crash a run at each side-effecting step, run it again
with the same run_id and check that GitHub ends up with exactly one issue, one
branch, one commit and one PR, with and without the runs.checkpoint store.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

import agents
from events import NullEmitter

RUN_ID = "3f2c9a1e-7b4d-4c8e-9a01-5d6e7f809abc"
CODE = "x = 2\n"
APPROVE = '{"verdict": "approve", "reasons": ["Looks correct."]}'
# The GitHub login that starts the runs: a writer of FakeRepo (trust.py, #487).
WRITER = "octo-writer"


class WorkerCrash(BaseException):
    """Stands in for the worker process dying: nothing in the run catches it."""


# --- In-memory fakes -----------------------------------------------------------


class FakePR:
    def __init__(self, repo, number, head, body):
        self.repo = repo
        self.number = number
        self.html_url = f"https://github.com/o/r/pull/{number}"
        self.head = SimpleNamespace(ref=head, sha=f"sha-{head}")
        self.body = body
        self.merged = False
        self.user = SimpleNamespace(login="automaintainer-bot")
        self.comments = []

    def get_files(self):
        content = self.repo.files.get((self.head.ref, "app.py"), "")
        return [SimpleNamespace(filename="app.py", status="modified", patch=content)]

    def create_issue_comment(self, body):
        self.comments.append(body)

    def create_review_request(self, reviewers):
        pass

    def merge(self, commit_message, sha):
        self.merged = True
        self.repo.merges += 1
        self.repo.trip("merge")
        return SimpleNamespace(merged=True, message="Merged")


class FakeRepo:
    """Just enough of PyGithub's Repository, with state that survives a crash."""

    def __init__(self):
        self.default_branch = "main"
        self.owner = SimpleNamespace(login="o", type="Organization")
        self.issues = []
        self.branches = {"main": "base-sha"}
        self.files = {("main", "app.py"): "x = 1\n"}
        self.commits = []
        self.pulls = []
        self.merges = 0
        self.crash_after = None  # name of the call after which to "crash"
        # GET .../collaborators/{login}/permission (trust.py, #487): a login's
        # permission, or an exception to raise for it.
        self.permissions = {WRITER: "write"}
        self.permission_lookups = []

    def trip(self, step):
        if self.crash_after == step:
            self.crash_after = None
            raise WorkerCrash(step)

    # contents / tree
    def get_contents(self, path, ref=None):
        if path == "":
            return [SimpleNamespace(path="app.py")]
        key = (ref or self.default_branch, path)
        if key not in self.files:
            raise GithubException(404, {"message": "Not Found"}, None)
        return SimpleNamespace(
            decoded_content=self.files[key].encode("utf-8"), sha=f"blob-{key}"
        )

    def get_readme(self):
        raise GithubException(404, {"message": "Not Found"}, None)

    def get_collaborator_permission(self, login):
        self.permission_lookups.append(login)
        permission = self.permissions.get(login, "none")
        if isinstance(permission, Exception):
            raise permission
        return permission

    def _commit(self, path, content, branch):
        self.files[(branch, path)] = content
        self.commits.append((branch, path))
        self.trip("commit")

    def create_file(self, path, message, content, branch):
        self._commit(path, content, branch)

    def update_file(self, path, message, content, sha, branch):
        self._commit(path, content, branch)

    # issues
    def create_issue(self, title, body):
        issue = SimpleNamespace(
            number=len(self.issues) + len(self.pulls) + 1,
            title=title,
            body=body,
            pull_request=None,
            assignees=[],
            comments=[],
            state="open",
            # Issues the run opens are the App's; a test sets another author.
            user=SimpleNamespace(login="automaintainer[bot]", type="Bot"),
        )
        issue.create_comment = issue.comments.append
        issue.edit = lambda state: setattr(issue, "state", state)
        self.issues.append(issue)
        self.trip("create_issue")
        return issue

    def get_issue(self, number):
        return next(i for i in self.issues if i.number == number)

    def get_issues(self, state, sort, direction):
        assert (state, sort, direction) == ("all", "created", "desc")
        return list(reversed(self.issues))

    # branches
    def get_branch(self, name):
        if name not in self.branches:
            raise GithubException(404, {"message": "Branch not found"}, None)
        return SimpleNamespace(commit=SimpleNamespace(sha=self.branches[name]))

    def create_git_ref(self, ref, sha):
        name = ref.removeprefix("refs/heads/")
        if name in self.branches:
            raise GithubException(422, {"message": "Reference already exists"}, None)
        self.branches[name] = sha
        for (branch, path), content in list(self.files.items()):
            if branch == self.default_branch:
                self.files[(name, path)] = content
        self.trip("create_git_ref")

    # pull requests
    def create_pull(self, title, body, head, base):
        pr = FakePR(self, len(self.issues) + len(self.pulls) + 1, head, body)
        self.pulls.append(pr)
        self.trip("create_pull")
        return pr

    def get_pulls(self, state, head):
        assert state == "all"
        owner, _, branch = head.partition(":")
        assert owner == "o", "head must be qualified with the repo owner"
        return [p for p in self.pulls if p.head.ref == branch]

    def get_pull(self, number):
        return next(p for p in self.pulls if p.number == number)


class FakeSupabase:
    """The runs table, plus the merge_decisions rows inserted (merge_audit.py);
    other tables accept writes and return nothing."""

    def __init__(self):
        self.runs = {}
        self.decisions = []

    def table(self, name):
        return _Query(self, name)


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.id = (
            db,
            table,
            None,
            None,
            None,
        )

    def select(self, *_):
        self.op = "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload, self.id = "upsert", payload, payload["id"]
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, value):
        self.id = value
        self.filters = {**getattr(self, "filters", {}), col: value}
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        if self.table == "merge_decisions" and self.op == "insert":
            self.db.decisions.append(self.payload)
        if self.table == "merge_decisions" and self.op == "select":
            filters = getattr(self, "filters", {})
            return SimpleNamespace(
                data=[
                    dict(row)
                    for row in self.db.decisions
                    if all(row.get(k) == v for k, v in filters.items())
                ]
            )
        if self.table != "runs":
            return SimpleNamespace(data=[])
        row = self.db.runs.get(self.id)
        if self.op == "select":
            return SimpleNamespace(data=[dict(row)] if row else [])
        if self.op == "upsert":
            self.db.runs.setdefault(self.id, {}).update(self.payload)
        elif self.op == "update" and row is not None:
            row.update(self.payload)
        return SimpleNamespace(data=[])


# --- Fixtures --------------------------------------------------------------------

_ROLES = {
    "You are the Principal Architect": "Architect",
    "You are the Visionary": "Visionary",
    "You are the Product Manager": "Product Manager",
    "You are the Implementer": "Implementer",
    "You are the Maintainer": "Maintainer",
}


def _role(system_prompt):
    return next(r for p, r in _ROLES.items() if system_prompt.startswith(p))


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A fake GitHub repo, LLM and (optionally) runs table for full graph runs."""
    import workspace

    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("MERGE_POLICY", raising=False)

    async def no_sync(*a, **k):
        return True

    monkeypatch.setattr(workspace, "sync_repo_clone", no_sync)

    async def fake_exec(*a, **k):
        proc = MagicMock()

        async def communicate():
            return (b"", b"")

        proc.communicate = communicate
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    repo = FakeRepo()
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))
    db = FakeSupabase()
    monkeypatch.setattr(agents, "supabase", db)

    calls = []
    crash_in = set()

    async def llm(system, user):
        role = _role(system)
        calls.append(role)
        if role in crash_in:
            crash_in.discard(role)
            raise WorkerCrash(role)
        return {
            "Architect": "Make app.py:1-1 return 2.",
            "Visionary": f"Idea {len(calls)}: set x to 2 in app.py:1-1.",
            "Product Manager": APPROVE,
            "Implementer": CODE,
            "Maintainer": APPROVE,
        }[role]

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    return SimpleNamespace(repo=repo, db=db, calls=calls, crash_in=crash_in)


async def _run(trigger_login=WRITER):
    return await agents.run_agent_loop(
        "o/r",
        run_id=RUN_ID,
        emitter=NullEmitter(),
        trigger_login=trigger_login,
        trigger_source="api",
    )


async def _crash_then_rerun(world):
    with pytest.raises(WorkerCrash):
        await _run()
    return await _run()


def _assert_single_artifacts(repo):
    assert len(repo.issues) == 1
    assert agents.run_marker(RUN_ID) in repo.issues[0].body
    run_branches = [b for b in repo.branches if b != "main"]
    assert run_branches == ["automaintainer/run-3f2c9a1e7b4d"]
    assert repo.commits == [("automaintainer/run-3f2c9a1e7b4d", "app.py")]
    assert len(repo.pulls) == 1
    assert repo.pulls[0].head.ref == "automaintainer/run-3f2c9a1e7b4d"


# --- Crash-and-rerun scenarios ---------------------------------------------------


@pytest.mark.parametrize("checkpoints", [True, False], ids=["checkpoint", "no-db"])
@pytest.mark.parametrize(
    "crash_after", ["create_issue", "create_git_ref", "commit", "create_pull"]
)
async def test_rerun_after_github_side_effect_creates_no_duplicates(
    world, monkeypatch, crash_after, checkpoints
):
    """The crash lands right after the GitHub call, before its checkpoint is
    written, so the re-run must find the artifact on GitHub itself."""
    if not checkpoints:
        monkeypatch.setattr(agents, "supabase", None)
    world.repo.crash_after = crash_after

    result = await _crash_then_rerun(world)

    assert result["status"] == "completed"
    _assert_single_artifacts(world.repo)
    assert result["pr_number"] == world.repo.pulls[0].number
    assert result["issue_number"] == world.repo.issues[0].number
    assert result["branch_name"] == "automaintainer/run-3f2c9a1e7b4d"


@pytest.mark.parametrize("crash_in", ["Product Manager", "Implementer", "Maintainer"])
async def test_rerun_after_checkpointed_step_resumes_without_repeating_it(
    world, crash_in
):
    world.crash_in.add(crash_in)

    result = await _crash_then_rerun(world)

    assert result["status"] == "completed"
    assert result["merge_status"] == "awaiting_human_review"
    _assert_single_artifacts(world.repo)
    issue, pr = world.repo.issues[0], world.repo.pulls[0]
    # Brainstorming and the PM verdict happen once; the verdict is posted once.
    assert world.calls.count("Visionary") == 1
    assert world.calls.count("Product Manager") == (
        2 if crash_in == "Product Manager" else 1
    )
    assert len(issue.comments) == 1
    assert len([c for c in pr.comments if "Maintainer Review" in c]) == 1
    # The issue keeps the idea it was opened with.
    assert issue.body.split("### Proposed Feature\n")[1].startswith("Idea 2:")
    checkpoint = world.db.runs[RUN_ID]["checkpoint"]
    assert checkpoint["issue_number"] == issue.number
    assert checkpoint["pr_number"] == pr.number
    assert checkpoint["branch_name"] == "automaintainer/run-3f2c9a1e7b4d"
    assert checkpoint["merge_status"] == "awaiting_human_review"


async def test_rerun_after_pr_created_skips_llm_work_already_done(world):
    """With a checkpoint, a crash after the PR exists re-runs only the
    Architect (not owned by the checkpoint) and the Maintainer."""
    world.crash_in.add("Maintainer")

    await _crash_then_rerun(world)

    assert world.calls.count("Implementer") == 1
    assert world.calls.count("Maintainer") == 2


async def test_completed_run_replayed_from_checkpoint_does_nothing_new(world):
    first = await _run()
    calls_before = list(world.calls)

    second = await _run()

    assert second == first
    _assert_single_artifacts(world.repo)
    # Only the Architect runs again; every later step replays its checkpoint.
    assert world.calls[len(calls_before) :] == ["Architect"]
    assert len(world.repo.pulls[0].comments) == 1


async def test_crash_after_merge_does_not_merge_again(world, monkeypatch):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.repo.crash_after = "merge"

    result = await _crash_then_rerun(world)

    assert result["status"] == "completed"
    assert result["merge_status"] == "merged"
    assert world.repo.merges == 1
    _assert_single_artifacts(world.repo)


async def test_rerun_continues_maintainer_iterations_from_checkpoint(
    world, monkeypatch
):
    """A crash after a "changes requested" review resumes at the next
    iteration instead of starting the review loop again."""
    reviews = iter(
        [
            '{"verdict": "reject", "reasons": ["Handle None."], "blocking_issues": ["Handle None."]}',
            APPROVE,
        ]
    )
    codes = iter(["x = 2\n", "x = 3\n"])
    real_llm = agents.run_llm_with_tools

    async def llm(system, user):
        role = _role(system)
        if role == "Maintainer":
            world.calls.append(role)
            return next(reviews)
        if role == "Implementer":
            world.calls.append(role)
            if world.calls.count(role) == 2:
                raise WorkerCrash("implementer iteration 1")
            return next(codes)
        return await real_llm(system, user)

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await _crash_then_rerun(world)

    assert result["status"] == "completed"
    assert world.calls.count("Maintainer") == 2  # iteration 0 and 1, once each
    assert len(world.repo.pulls) == 1 and len(world.repo.issues) == 1
    # normalize_generated_code strips the trailing newline.
    assert world.repo.files[("automaintainer/run-3f2c9a1e7b4d", "app.py")] == "x = 3"


# --- Unit tests for the helpers -------------------------------------------------


def test_branch_name_is_derived_from_run_id():
    assert agents.run_branch_name(RUN_ID, 5) == "automaintainer/run-3f2c9a1e7b4d"
    assert agents.run_branch_name(RUN_ID, 9) == agents.run_branch_name(RUN_ID, 5)
    # Outside a run there is nothing to be idempotent against.
    assert agents.run_branch_name(None, 5).startswith("feature/issue-5-")


def test_run_issue_lookup_ignores_pull_requests_and_other_runs():
    repo = FakeRepo()
    repo.create_issue("other run", "body\n\n" + agents.run_marker("another-run"))
    mine = repo.create_issue("mine", "body\n\n" + agents.run_marker(RUN_ID))
    pr_like = repo.create_issue("pr", "body\n\n" + agents.run_marker(RUN_ID))
    pr_like.pull_request = object()
    repo.issues.reverse()  # newest first would be the PR-like entry

    assert agents._find_run_issue(repo, RUN_ID) is mine
    assert agents._find_run_issue(repo, "unknown-run") is None


def test_idea_is_recovered_from_run_issue_body():
    body = "### Architect Directive\nd\n\n### Proposed Feature\nThe idea.\n\n" + (
        agents.run_marker(RUN_ID)
    )
    assert agents._idea_from_issue_body(body) == "The idea."
    assert agents._idea_from_issue_body("free text") == ""


def test_commit_is_skipped_when_branch_already_has_content():
    repo = FakeRepo()
    repo.files[("b", "app.py")] = CODE

    assert agents._commit_file_if_changed(repo, "app.py", CODE, "m", "b") is False
    assert repo.commits == []
    assert agents._commit_file_if_changed(repo, "app.py", "y\n", "m", "b") is True
    assert agents._commit_file_if_changed(repo, "new.py", "z\n", "m", "b") is True
    assert repo.commits == [("b", "app.py"), ("b", "new.py")]


def test_branch_creation_race_reuses_existing_branch():
    repo = FakeRepo()
    real_get_branch = repo.get_branch

    def racing_get_branch(name):
        if name == "run-branch":
            # Another attempt created it between our lookup and create.
            repo.branches.setdefault("run-branch", "base-sha")
            raise GithubException(404, {"message": "Branch not found"}, None)
        return real_get_branch(name)

    repo.get_branch = racing_get_branch
    assert agents._ensure_branch(repo, "run-branch", "main") is False
    assert list(repo.branches) == ["main", "run-branch"]


async def test_checkpoint_write_failure_is_not_fatal(monkeypatch):
    broken = MagicMock()
    broken.table.side_effect = RuntimeError("supabase down")
    monkeypatch.setattr(agents, "supabase", broken)

    await agents._save_checkpoint(RUN_ID, issue_number=1)
    assert await agents._load_checkpoint(RUN_ID) == {}
