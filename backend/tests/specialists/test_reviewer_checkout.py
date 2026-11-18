"""``reviewer_adk.checkout_head`` against a real local git repository (#525):
the review reads exactly the head it judges, and the checkout is deleted."""

import shutil
import subprocess

import pytest

import agents
from automaintainer._runtime.specialists.reviewer_adk import (
    CheckoutError,
    checkout_head,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_run_github_token", lambda: None)
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "run-branch")
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "change")
    return repo


async def test_checks_out_the_head_and_deletes_it(origin):
    head = git(origin, "rev-parse", "HEAD")

    async with checkout_head(origin.as_uri(), "run-branch", head) as root:
        assert (root / "app.py").read_text(encoding="utf-8") == "x = 2\n"
        seen = root

    assert not seen.exists()


async def test_a_branch_that_moved_is_refused(origin):
    old = git(origin, "rev-parse", "HEAD")
    (origin / "app.py").write_text("x = 3\n", encoding="utf-8")
    git(origin, "commit", "-q", "-am", "pushed after the head was read")

    with pytest.raises(CheckoutError, match="moved"):
        async with checkout_head(origin.as_uri(), "run-branch", old):
            pytest.fail("must not review another commit")  # pragma: no cover


async def test_unknown_branch_or_url_is_refused(origin):
    with pytest.raises(CheckoutError):
        async with checkout_head(origin.as_uri(), "nope", "0" * 40):
            pass  # pragma: no cover
    with pytest.raises(CheckoutError):
        async with checkout_head(None, "run-branch", "0" * 40):
            pass  # pragma: no cover
