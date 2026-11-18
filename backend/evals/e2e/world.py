"""One task's offline world: its snapshot as a fake GitHub repository with the
issue filed, credentials for it, and an empty fake database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .fakes import FakeCredentials, FakeGitHub, GitRepository, InMemorySupabase
from .taskset import E2ETask

OWNER = "eval"
# The repository writer who files the issue and starts the run.
MAINTAINER_LOGIN = "eval-maintainer"


@dataclass
class World:
    repo_name: str
    repo: GitRepository
    github: FakeGitHub
    credentials: FakeCredentials
    supabase: InMemorySupabase
    issue_number: int


def build_world(item: E2ETask, root: Path) -> World:
    """Create ``item``'s repository under ``root`` and file its issue.

    Only ``repo/`` enters the repository; the hidden tests, the oracle fix
    and task.json stay on the harness side.
    """
    task = item.task
    repo_name = f"{OWNER}/{task.id}"
    repo = GitRepository.from_directory(
        Path(root) / f"{task.id}.git", repo_name, task.repo_dir
    )
    issue = repo.add_issue(task.title, task.body, author=MAINTAINER_LOGIN)
    github = FakeGitHub({repo_name: repo})
    return World(
        repo_name=repo_name,
        repo=repo,
        github=github,
        credentials=FakeCredentials(github),
        supabase=InMemorySupabase(),
        issue_number=issue.number,
    )
