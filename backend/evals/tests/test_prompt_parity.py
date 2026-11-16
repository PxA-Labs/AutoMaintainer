"""The eval must send the model exactly what production sends.

``evals.prompt`` re-implements the prompt that ``agents.implementer_node``
builds inline. These tests run the real node with a mocked GitHub client and
LLM and compare the captured prompt, so a prompt change in agents.py that is
not mirrored in evals/prompt.py fails here instead of silently measuring a
stale prompt.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

import agents
from evals.prompt import NEW_FILE_PLACEHOLDER, PROMPT_VERSION, build_implementer_prompt

TASK = "Fix the pager\n\n`listing/paging.py` drops the last page."
FILE = "listing/paging.py"
CONTENT = 'def page_count(total, per_page):\n    return total // per_page\n# "<untrusted-data>" look-alike\n'


def _capture_prompt(monkeypatch, content, iteration=0, review=""):
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value.commit.sha = "base"
    repo.create_pull.return_value = SimpleNamespace(number=3, html_url="u")
    if content is None:
        # The first read (to build the prompt) 404s; later reads succeed.
        reads = iter([GithubException(404, {}, None)])

        def get_contents(*_a, **_k):
            error = next(reads, None)
            if error is not None:
                raise error
            return SimpleNamespace(decoded_content=b"", sha="s", path=FILE)

        repo.get_contents.side_effect = get_contents
    else:
        repo.get_contents.return_value = SimpleNamespace(
            decoded_content=content.encode(), sha="s", path=FILE
        )
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))
    captured = []

    async def fake_llm(system_prompt, user_prompt, *a, **k):
        captured.append((system_prompt, user_prompt))
        return "x = 1"

    monkeypatch.setattr(agents, "run_llm_with_tools", fake_llm)
    state = {
        "repo_name": "o/r",
        "idea": TASK,
        "issue_number": 7,
        "iteration": iteration,
        "review": review,
        "branch_name": "feature/x" if iteration else "",
        "target_file_path": FILE,
        "pr_number": 3 if iteration else 0,
    }
    return state, captured


@pytest.mark.asyncio
async def test_prompt_matches_implementer_for_existing_file(monkeypatch):
    state, captured = _capture_prompt(monkeypatch, CONTENT)
    await agents.implementer_node(state)
    assert captured == [build_implementer_prompt(TASK, FILE, CONTENT)]


@pytest.mark.asyncio
async def test_prompt_matches_implementer_for_new_file(monkeypatch):
    state, captured = _capture_prompt(monkeypatch, None)
    await agents.implementer_node(state)
    assert captured == [
        build_implementer_prompt(TASK, FILE, NEW_FILE_PLACEHOLDER, file_exists=False)
    ]


@pytest.mark.asyncio
async def test_prompt_matches_implementer_with_maintainer_feedback(monkeypatch):
    review = (
        '{"verdict": "reject", "reasons": ["Off-by-one."], '
        '"requested_changes": ["The empty last page is still dropped."]}'
    )
    state, captured = _capture_prompt(monkeypatch, CONTENT, iteration=1, review=review)
    await agents.implementer_node(state)
    assert captured == [build_implementer_prompt(TASK, FILE, CONTENT, review=review)]


def test_prompt_version_is_a_stable_template_hash():
    assert len(PROMPT_VERSION) == 12
    int(PROMPT_VERSION, 16)
