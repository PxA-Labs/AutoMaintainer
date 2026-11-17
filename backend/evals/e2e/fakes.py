"""Offline stand-ins for GitHub and Supabase that the real engine runs on.

``GitRepository`` is a bare git repository holding only a task's repository
snapshot. ``FakeGitHub`` exposes it through the subset of the PyGithub API the
agent nodes use (issues, contents, branches, commits, pull requests, commit
statuses), so every write the agents make is a real git commit. Its
``clone_url`` is a ``file:`` URL, so the engine's verification step clones and
tests the pushed head exactly as it would a GitHub repository.

The hidden tests and the oracle fix of a task never enter the repository, so
nothing the agents can reach (the API or the clone) contains them; the
data-validation tests check this for every task.

``InMemorySupabase`` is a minimal PostgREST-style table store (select,
insert, upsert, update, delete with ``eq``/``neq``/``in_`` filters) for the
run row, checkpoints and audit rows the engine writes. ``select`` returns
whole rows: no caller in the engine depends on column projection.

Not supported (the eval runs only targeted runs): the root directory listing
(``get_contents("")``), README lookups and user permission checks, which only
autonomous runs and MERGE_POLICY=ci/auto need.
"""

from __future__ import annotations

import itertools
import os
import posixpath
import subprocess  # nosec B404 - runs git with a fixed argv, never a shell
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from github import GithubException

DEFAULT_BRANCH = "main"
# Fixed identity and dates make commit SHAs a function of content and parent
# only, so recorded prompts (which can quote SHAs) are identical across runs.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "automaintainer-eval",
    "GIT_AUTHOR_EMAIL": "eval@automaintainer.invalid",
    "GIT_COMMITTER_NAME": "automaintainer-eval",
    "GIT_COMMITTER_EMAIL": "eval@automaintainer.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def _not_found(what: str) -> GithubException:
    return GithubException(404, {"message": f"Not Found: {what}"}, None)


def safe_path(path: str) -> Optional[str]:
    """``path`` as a normalised repository path, or None when it is empty,
    absolute, escapes the repository or names git internals."""
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return None
    if path.startswith("/"):
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        return None
    return posixpath.normpath(path)


class GitRepository:
    """A bare git repository plus the GitHub-side state of one fake repo."""

    def __init__(self, git_dir: Path, full_name: str):
        self.git_dir = Path(git_dir)
        self.full_name = full_name
        self.lock = threading.RLock()
        self.issues: dict[int, "FakeIssue"] = {}
        self.pulls: dict[int, "FakePullRequest"] = {}
        self.statuses: dict[str, list] = {}
        self.comments: list[tuple[int, str]] = []
        self.review_requests: list[tuple[int, list]] = []
        self._numbers = itertools.count(1)

    # --- git plumbing -----------------------------------------------------------

    def git(self, *args: str, data: bytes | None = None, env: dict | None = None):
        proc = subprocess.run(  # nosec B603 B607
            ["git", "--git-dir", str(self.git_dir), *args],
            input=data,
            capture_output=True,
            env={**os.environ, **_GIT_ENV, **(env or {})},
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {args[0]} failed: {proc.stderr.decode(errors='replace')[-400:]}"
            )
        return proc.stdout

    @classmethod
    def create(cls, git_dir: Path, full_name: str, files: dict) -> "GitRepository":
        git_dir = Path(git_dir)
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(  # nosec B603 B607
            ["git", "init", "--quiet", "--bare", str(git_dir)],
            capture_output=True,
            env={**os.environ, **_GIT_ENV},
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git init failed: {proc.stderr.decode()[-400:]}")
        repo = cls(git_dir, full_name)
        repo.git("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}")
        repo.commit(DEFAULT_BRANCH, files, "Repository snapshot", parent=None)
        return repo

    @classmethod
    def from_directory(cls, git_dir: Path, full_name: str, source: Path):
        files = {
            p.relative_to(source).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(source.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }
        return cls.create(git_dir, full_name, files)

    @property
    def clone_url(self) -> str:
        return self.git_dir.resolve().as_uri()

    def head(self, branch: str) -> Optional[str]:
        """The commit SHA ``branch`` points to, or None when it does not exist."""
        try:
            out = self.git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
        except RuntimeError:
            return None
        return out.decode().strip()

    _head = head

    def commit(
        self, branch: str, changes: dict, message: str, parent: Optional[str] = ""
    ) -> str:
        """Commit ``changes`` ({path: text, or None to delete}) on ``branch``;
        returns the new commit SHA. ``parent=""`` means the branch head."""
        with self.lock:
            if parent == "":
                parent = self._head(branch)
            with tempfile.TemporaryDirectory(prefix="eval-index-") as tmp:
                env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                if parent:
                    self.git("read-tree", parent, env=env)
                else:
                    self.git("read-tree", "--empty", env=env)
                for path, text in changes.items():
                    if text is None:
                        self.git("update-index", "--force-remove", "--", path, env=env)
                        continue
                    blob = self.git(
                        "hash-object", "-w", "--stdin", data=text.encode("utf-8")
                    )
                    self.git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob.decode().strip()},{path}",
                        env=env,
                    )
                tree = self.git("write-tree", env=env).decode().strip()
            args = ["commit-tree", tree, "-m", message]
            if parent:
                args += ["-p", parent]
            sha = self.git(*args).decode().strip()
            self.git("update-ref", f"refs/heads/{branch}", sha)
            return sha

    def tree(self, ref: str) -> dict:
        """Every file at ``ref`` as {path: text}."""
        out = self.git("ls-tree", "-r", "-z", "--full-tree", ref).decode()
        files = {}
        for entry in filter(None, out.split("\0")):
            meta, path = entry.split("\t", 1)
            _mode, kind, sha = meta.split()
            if kind == "blob":
                files[path] = self.git("cat-file", "blob", sha).decode("utf-8")
        return files

    def all_blobs(self) -> list[str]:
        """The content of every blob in the object store, reachable or not."""
        out = self.git("cat-file", "--batch-all-objects", "--batch-check").decode()
        blobs = []
        for line in out.splitlines():
            sha, kind, _size = line.split()
            if kind == "blob":
                blobs.append(self.git("cat-file", "blob", sha).decode("utf-8"))
        return blobs

    def all_paths(self) -> set:
        """Every path of every commit in the repository."""
        out = self.git("rev-list", "--objects", "--all").decode()
        return {line.split(" ", 1)[1] for line in out.splitlines() if " " in line}

    def _entry(self, ref: str, path: str):
        """(type, sha) of ``path`` at ``ref`` or None."""
        out = self.git("ls-tree", "--full-tree", ref, "--", path).decode()
        for line in out.splitlines():
            meta, name = line.split("\t", 1)
            if name == path:
                _mode, kind, sha = meta.split()
                return kind, sha
        return None

    # --- GitHub-side state -----------------------------------------------------

    def next_number(self) -> int:
        return next(self._numbers)

    def add_issue(self, title: str, body: str, author: str = "eval-maintainer"):
        number = self.next_number()
        issue = FakeIssue(self, number, title, body, author)
        self.issues[number] = issue
        return issue


@dataclass
class FakeContent:
    path: str
    sha: str
    decoded_content: bytes
    type: str = "file"


class FakeIssue:
    def __init__(self, repo: GitRepository, number, title, body, author):
        self._repo = repo
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"
        self.pull_request = None
        self.assignees = []
        self.user = SimpleNamespace(login=author, id=1000 + number, type="User")

    def create_comment(self, body: str):
        self._repo.comments.append((self.number, body))
        return SimpleNamespace(body=body)

    def edit(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _Head:
    def __init__(self, repo: GitRepository, ref: str):
        self._repo, self.ref = repo, ref

    @property
    def sha(self) -> Optional[str]:
        return self._repo._head(self.ref)


@dataclass
class _ChangedFile:
    filename: str
    status: str
    patch: str


_STATUS = {"M": "modified", "A": "added", "D": "removed"}


class FakePullRequest:
    def __init__(self, repo: GitRepository, number, title, body, head, base, draft):
        self._repo = repo
        self.number = number
        self.title = title
        self.body = body
        self.head = _Head(repo, head)
        self.base = SimpleNamespace(ref=base)
        self.draft = draft
        self.merged = False
        self.state = "open"
        self.labels = []
        self.user = SimpleNamespace(login="automaintainer-eval[bot]", type="Bot")
        self.html_url = f"https://github.invalid/{repo.full_name}/pull/{number}"

    def get_files(self):
        base, head = self.base.ref, self.head.ref
        spec = f"refs/heads/{base}...refs/heads/{head}"
        out = self._repo.git("diff", "--name-status", "--no-renames", spec).decode()
        files = []
        for line in out.splitlines():
            status, path = line.split("\t", 1)
            diff = self._repo.git("diff", "--no-renames", spec, "--", path).decode()
            hunk = diff.find("@@")
            files.append(
                _ChangedFile(path, _STATUS.get(status[0], "modified"), diff[hunk:])
            )
        return files

    def edit(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def create_issue_comment(self, body: str):
        self._repo.comments.append((self.number, body))
        return SimpleNamespace(body=body)

    def create_review_request(self, reviewers=()):
        self._repo.review_requests.append((self.number, list(reviewers)))

    def add_to_labels(self, *labels):
        self.labels.extend(SimpleNamespace(name=label) for label in labels)

    def convert_to_draft(self):
        self.draft = True

    def merge(self, commit_message="", sha=None, **_):
        head = self.head.sha
        if sha is not None and sha != head:
            return SimpleNamespace(merged=False, message="Head branch was modified")
        self._repo.git("update-ref", f"refs/heads/{self.base.ref}", head)
        self.merged = True
        self.state = "closed"
        return SimpleNamespace(merged=True, message="merged")


class _Commit:
    def __init__(self, repo: GitRepository, sha: str):
        self._repo, self.sha = repo, sha

    def create_status(self, state, description="", context="default", **_):
        self._repo.statuses.setdefault(self.sha, []).append((context, state))

    def get_combined_status(self):
        posted = self._repo.statuses.get(self.sha, [])
        return SimpleNamespace(
            statuses=[SimpleNamespace(context=c, state=s) for c, s in posted]
        )

    def get_check_runs(self):
        return []


class FakeRepoAPI:
    """The PyGithub ``Repository`` surface of one GitRepository."""

    default_branch = DEFAULT_BRANCH

    def __init__(self, repo: GitRepository):
        self._repo = repo
        self.full_name = repo.full_name
        self.clone_url = repo.clone_url
        self.html_url = f"https://github.invalid/{repo.full_name}"
        self.owner = SimpleNamespace(
            login=repo.full_name.split("/")[0], type="Organization"
        )

    # Issues ------------------------------------------------------------------

    def get_issue(self, number):
        try:
            return self._repo.issues[int(number)]
        except (KeyError, ValueError):
            raise _not_found(f"issue {number}") from None

    def get_issues(self, **_):
        return [self._repo.issues[n] for n in sorted(self._repo.issues, reverse=True)]

    def create_issue(self, title, body="", **_):
        return self._repo.add_issue(title, body, author="automaintainer-eval[bot]")

    # Contents and branches -----------------------------------------------------

    def get_branch(self, name):
        sha = self._repo._head(name)
        if sha is None:
            raise _not_found(f"branch {name}")
        return SimpleNamespace(name=name, commit=SimpleNamespace(sha=sha))

    def create_git_ref(self, ref, sha):
        if not ref.startswith("refs/heads/"):
            raise GithubException(422, {"message": "Reference must be a branch"}, None)
        branch = ref[len("refs/heads/") :]
        if self._repo._head(branch) is not None:
            raise GithubException(422, {"message": "Reference already exists"}, None)
        self._repo.git("update-ref", ref, sha)
        return SimpleNamespace(ref=ref)

    def get_contents(self, path, ref=None):
        clean = safe_path(path)
        ref = ref or self.default_branch
        commit = self._repo._head(ref)
        if clean is None or commit is None:
            raise _not_found(path)
        entry = self._repo._entry(commit, clean)
        if entry is None:
            raise _not_found(path)
        kind, sha = entry
        if kind == "tree":
            out = self._repo.git("ls-tree", "--full-tree", commit, "--", clean + "/")
            listing = []
            for line in out.decode().splitlines():
                meta, name = line.split("\t", 1)
                _mode, sub_kind, sub_sha = meta.split()
                listing.append(
                    FakeContent(
                        name, sub_sha, b"", "dir" if sub_kind == "tree" else "file"
                    )
                )
            return listing
        data = self._repo.git("cat-file", "blob", sha)
        return FakeContent(clean, sha, data)

    def _write(self, path, content, branch, message):
        clean = safe_path(path)
        if clean is None:
            raise GithubException(422, {"message": f"invalid path {path!r}"}, None)
        if self._repo._head(branch) is None:
            raise _not_found(f"branch {branch}")
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        sha = self._repo.commit(branch, {clean: content}, message)
        return {"commit": SimpleNamespace(sha=sha)}

    def create_file(self, path, message, content, branch=None, **_):
        branch = branch or self.default_branch
        commit = self._repo._head(branch)
        if commit and self._repo._entry(commit, safe_path(path) or ""):
            raise GithubException(422, {"message": "sha wasn't supplied"}, None)
        return self._write(path, content, branch, message)

    def update_file(self, path, message, content, sha, branch=None, **_):
        branch = branch or self.default_branch
        current = self.get_contents(path, ref=branch)
        if isinstance(current, list) or current.sha != sha:
            raise GithubException(409, {"message": "sha does not match"}, None)
        return self._write(path, content, branch, message)

    def delete_file(self, path, message, sha, branch=None, **_):
        branch = branch or self.default_branch
        current = self.get_contents(path, ref=branch)
        if isinstance(current, list) or current.sha != sha:
            raise GithubException(409, {"message": "sha does not match"}, None)
        sha = self._repo.commit(branch, {safe_path(path): None}, message)
        return {"commit": SimpleNamespace(sha=sha)}

    # Pull requests and commits -------------------------------------------------

    def get_pulls(self, state="open", head=None, **_):
        branch = head.split(":", 1)[1] if head and ":" in head else head
        return [
            pr
            for _, pr in sorted(self._repo.pulls.items())
            if branch is None or pr.head.ref == branch
        ]

    def get_pull(self, number):
        try:
            return self._repo.pulls[int(number)]
        except (KeyError, ValueError):
            raise _not_found(f"pull {number}") from None

    def create_pull(self, title, body, head, base, draft=False, **_):
        head_sha, base_sha = self._repo._head(head), self._repo._head(base)
        if head_sha is None or base_sha is None:
            raise GithubException(422, {"message": "Validation Failed"}, None)
        ahead = self._repo.git("rev-list", "--count", f"{base_sha}..{head_sha}")
        if int(ahead.decode().strip()) == 0:
            raise GithubException(
                422, {"message": f"No commits between {base} and {head}"}, None
            )
        pr = FakePullRequest(
            self._repo, self._repo.next_number(), title, body, head, base, draft
        )
        self._repo.pulls[pr.number] = pr
        return pr

    def get_commit(self, sha):
        return _Commit(self._repo, sha)


class FakeGitHub:
    """A PyGithub ``Github`` client over local GitRepository objects."""

    def __init__(self, repos: dict):
        self._repos = dict(repos)

    def get_repo(self, full_name):
        try:
            return FakeRepoAPI(self._repos[full_name])
        except KeyError:
            raise _not_found(f"repository {full_name}") from None

    def get_user(self, *_):
        return SimpleNamespace(login="automaintainer-eval", type="User")


@dataclass
class FakeCredentials:
    """What github_app.credentials_for_installation returns, for a PAT."""

    github: FakeGitHub
    mode: str = "pat"
    calls: list = field(default_factory=list)

    def token(self) -> str:
        return "eval-offline-token"

    def client(self) -> FakeGitHub:
        return self.github

    def factory(self):
        """An engine ``github=`` credentials factory returning these."""

        def credentials(installation_id, *, purpose, repository):
            self.calls.append((installation_id, repository))
            return self

        return credentials


# --- Supabase --------------------------------------------------------------------


class InMemorySupabase:
    """Tables of dict rows behind a PostgREST-like query builder."""

    def __init__(self):
        self._tables: dict[str, list] = {}
        self._lock = threading.Lock()

    def table(self, name: str) -> "_Query":
        return _Query(self, name)

    def rows(self, name: str) -> list:
        with self._lock:
            return [dict(r) for r in self._tables.get(name, [])]


class _Query:
    def __init__(self, db: InMemorySupabase, name: str):
        self._db, self._name = db, name
        self._op, self._payload, self._filters = "select", None, []
        self._on_conflict = "id"
        self._limit = None

    def _set(self, op, payload=None):
        self._op, self._payload = op, payload
        return self

    def select(self, *_a, **_k):
        return self._set("select")

    def insert(self, payload, **_):
        return self._set("insert", payload)

    def upsert(self, payload, on_conflict="id", **_):
        self._on_conflict = on_conflict
        return self._set("upsert", payload)

    def update(self, payload):
        return self._set("update", payload)

    def delete(self):
        return self._set("delete")

    def eq(self, column, value):
        self._filters.append(lambda row: row.get(column) == value)
        return self

    def neq(self, column, value):
        self._filters.append(lambda row: row.get(column) != value)
        return self

    def in_(self, column, values):
        values = list(values)
        self._filters.append(lambda row: row.get(column) in values)
        return self

    def is_(self, column, value):
        expected = None if value in (None, "null") else value
        self._filters.append(lambda row: row.get(column) is expected)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, count, *_a, **_k):
        self._limit = count
        return self

    def single(self):
        return self

    maybe_single = single

    def _match(self, row) -> bool:
        return all(f(row) for f in self._filters)

    def execute(self):
        with self._db._lock:
            table = self._db._tables.setdefault(self._name, [])
            payload = self._payload
            rows = payload if isinstance(payload, list) else [payload]
            if self._op == "select":
                data = [dict(r) for r in table if self._match(r)]
                if self._limit is not None:
                    data = data[: self._limit]
            elif self._op == "insert":
                data = [dict(r) for r in rows]
                table.extend(dict(r) for r in rows)
            elif self._op == "upsert":
                data = []
                for row in rows:
                    key = row.get(self._on_conflict)
                    existing = next(
                        (r for r in table if r.get(self._on_conflict) == key), None
                    )
                    if existing is not None and key is not None:
                        existing.update(row)
                        data.append(dict(existing))
                    else:
                        table.append(dict(row))
                        data.append(dict(row))
            elif self._op == "update":
                data = []
                for row in table:
                    if self._match(row):
                        row.update(payload)
                        data.append(dict(row))
            else:  # delete
                data = [dict(r) for r in table if self._match(r)]
                table[:] = [r for r in table if not self._match(r)]
            return SimpleNamespace(data=data, count=len(data))
