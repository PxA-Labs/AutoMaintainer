"""Regression tests: target-file extraction and Implementer output validation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

import agents
from agents import (
    GeneratedCodeError,
    LLMResponse,
    extract_target_file,
    normalize_generated_code,
)

# --- extract_target_file -------------------------------------------------


@pytest.mark.parametrize(
    "directive, expected",
    [
        ("Fix backend/main.py:10-25", "backend/main.py"),
        ("Fix backend/main.py:10–25", "backend/main.py"),  # en dash
        ("Fix backend/main.py:10—25", "backend/main.py"),  # em dash
        ("Fix backend/main.py:10 - 25 now", "backend/main.py"),
        ("Fix backend/main.py:42.", "backend/main.py"),
        ("See backend/main.py#L10-L25", "backend/main.py"),
        ("See backend/main.py#L7", "backend/main.py"),
        ("Edit `backend/main.py:10-25` please", "backend/main.py"),
        ("Edit `backend/main.py`:10-25 please", "backend/main.py"),
        ("Update the handler in `backend/main.py`.", "backend/main.py"),
        ("Update ./src/app.ts:3-4", "src/app.ts"),
        (
            "See https://docs.python.org/3/library/re.html:1-2 then fix app.py:1-5",
            "app.py",
        ),
        ("Bump to python 3.11:1-2 and edit app.py:3", "app.py"),
        ("Call example.com:8080 from client.js:4-9", "client.js"),
        ("Fix backend/main.py:L10-L25", "backend/main.py"),  # AST context form
        ("Use `Node.js` to fix `app/page.js`", "app/page.js"),
        ("`Next.js` build breaks in `app/page.js`", "app/page.js"),
        ("Rename `main.py` (see `Node.js` docs)", "main.py"),
        ("See https://x.org/a?file=src/app.py#L10-L20 and fix b.py:1", "b.py"),
        ("Call foo.local:8080 from client.js:4-9", "client.js"),
        ("Point redis.cache:6379 at worker.py:12", "worker.py"),
        ("Fix nginx.conf:80", "nginx.conf"),
        ("See config/api.v2:10", "config/api.v2"),  # has a directory
        ("See api.v2#L10", "api.v2"),  # GitHub anchors are unambiguous
    ],
)
def test_extract_target_file_formats(directive, expected):
    assert extract_target_file(directive) == expected


@pytest.mark.parametrize(
    "directive",
    [
        "Improve the onboarding experience.",
        "Read https://github.com/o/r/blob/main/app.py#L1-L5",
        "Upgrade to v1.2.3:4-5",
        "Point the client at example.com:8080",
        "Update ../secrets.env:1-2",
        "Update .github/workflows/ci.yml:1-2",
        "`Next.js` build breaks on the home page",
        "See https://x.org/a?file=src/app.py#L10-L20",
        "See https://x.org/a?path~src/app.py:10",
        "Point the client at foo.local:8080",
        "Clone into ~/src/app.py:3",
        "Point the cache at redis.cache:6379",
        "Bump api.v2:10 to v3",
    ],
)
def test_extract_target_file_rejects_non_targets(directive):
    assert extract_target_file(directive) is None


# --- normalize_generated_code --------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Here is the updated file:\n```python\nx = 2\n```",
        "```python\nx = 2\n```\n\nThis change sets x to 2.",
        "x = 2",
    ],
)
def test_prose_around_fence_is_stripped(raw):
    assert normalize_generated_code(raw, path="a.py") == "x = 2"


def test_block_matching_file_language_is_preferred():
    raw = "Run:\n```bash\npip install x\n```\nThen:\n```python\nx = 2\n```"
    assert normalize_generated_code(raw, path="a.py") == "x = 2"
    # No block in the file's language: fail closed rather than commit the
    # shell command as Go source.
    with pytest.raises(GeneratedCodeError, match="language"):
        normalize_generated_code(raw, path="a.go")
    # Unknown extensions and untagged blocks keep the first-block rule.
    assert normalize_generated_code(raw, path="a.zig") == "pip install x"
    assert normalize_generated_code("Run:\n```\nx = 2\n```", path="a.py") == "x = 2"


@pytest.mark.parametrize(
    "raw",
    [
        "Run:\n```bash\npip install x\n```\nThen:\n```js\nx=1\n```",
        "```bash\npip install x\n```",
        '```bash\nFENCE = """\n```\n"""\n```',  # whole-file shape
    ],
)
def test_blocks_only_in_another_language_are_rejected(raw):
    with pytest.raises(GeneratedCodeError, match="language"):
        normalize_generated_code(raw, path="src/a.py")


# Reviewer repros: chat-style lead-ins that are not "<= 3 lines ending in ':'"
# used to be committed verbatim, prose and fence lines included.
@pytest.mark.parametrize(
    "raw",
    [
        "Here's the updated `src/a.py` with error handling added.\n\n"
        "```python\nx = 2\n```\n\nLet me know if you need anything else!",
        "Sure! Below is the complete file\n```python\nx = 2\n```",
        "**Updated file:**\n```python\nx = 2\n```",
        "Here is the updated file.\n```python\nx = 2\n```",
        "I rewrote the handler. It now validates input.\n"
        "Errors are logged.\nTests still pass.\nSee the file below.\n"
        "```python\nx = 2\n```",
        "## Changes\n\n- Set `x = 2` instead of 1.\n\n```python\nx = 2\n```\nThanks.",
    ],
)
def test_prose_lead_ins_are_unwrapped(raw):
    assert normalize_generated_code(raw, path="src/a.py") == "x = 2"


@pytest.mark.parametrize(
    "raw",
    [
        "Here is the fix.\nCall setup() before x = 2 runs.\n```python\nx = 2\n```",
        "Updated below.\nimport os\n```\nx = 2\n```",
    ],
)
def test_prose_mixed_with_code_before_a_fence_is_ambiguous(raw):
    with pytest.raises(GeneratedCodeError, match="ambiguous"):
        normalize_generated_code(raw, path="src/a.py")


@pytest.mark.parametrize(
    "src",
    [
        # Module docstring holding a fenced example: the opener line is code.
        '"""Utilities.\n\nExample usage:\n\n```python\nfoo()\n```\n"""\nimport x',
        # Ruby heredoc / shell heredoc opening a string that contains fences.
        "doc = <<~MD\n```ruby\nx\n```\nMD",
        "cat <<EOF\n```bash\nx\n```\nEOF",
    ],
)
def test_bare_files_whose_fences_follow_code_are_kept(src):
    path = {'"': "a.py", "d": "a.rb", "c": "a.sh"}[src[0]]
    assert normalize_generated_code(src, path=path) == src


def test_plain_text_targets_are_not_unwrapped_on_prose_alone():
    doc = "Usage notes.\nRun the tool like this.\n```bash\ntool --x\n```\nDone."
    assert normalize_generated_code(doc, path="NOTES.txt") == doc


def test_refusal_inside_fence_is_rejected():
    with pytest.raises(GeneratedCodeError, match="refused"):
        normalize_generated_code("```\nI cannot do this.\n```", path="a.py")


@pytest.mark.parametrize(
    "raw",
    [
        "```python\ndef f():\n    return dele",
        "Sure:\n```python\ndef f(:\n",
    ],
)
def test_unterminated_fence_is_rejected_as_truncated(raw):
    with pytest.raises(GeneratedCodeError, match="unterminated"):
        normalize_generated_code(raw, path="a.py")


def test_finish_reason_length_is_rejected():
    with pytest.raises(GeneratedCodeError, match="truncated"):
        normalize_generated_code("x = 1", path="a.py", finish_reason="length")


@pytest.mark.parametrize(
    "raw",
    [
        "I'm sorry, but I can't help with that.",
        "I cannot modify this file without more context.",
        "As an AI, I am unable to do that.",
        "Sorry, I won’t do that.",
    ],
)
def test_refusals_are_rejected(raw):
    with pytest.raises(GeneratedCodeError, match="refused"):
        normalize_generated_code(raw, path="a.py")


def test_markdown_targets_keep_inner_fences():
    doc = "# Title\n\n```python\nx = 1\n```\n\nMore text."
    assert normalize_generated_code(doc, path="README.md") == doc
    assert normalize_generated_code(f"```markdown\n{doc}\n```", path="docs/a.md") == doc


# A file that itself contains fence lines (docstring example). The reviewer's
# repro: the bare file used to be replaced by the inner snippet "f()".
FILE_WITH_INNER_FENCES = (
    'def f():\n    """Example:\n\n```python\nf()\n```\n"""\n    return 1'
)


@pytest.mark.parametrize(
    "raw",
    [
        FILE_WITH_INNER_FENCES + "\n",  # bare file, as the prompt asks
        f"```python\n{FILE_WITH_INNER_FENCES}\n```",  # wrapped in a fence
        f"Here is the updated file:\n```python\n{FILE_WITH_INNER_FENCES}\n```\nDone.",
        f"```\n{FILE_WITH_INNER_FENCES}\n```",
    ],
)
def test_file_containing_fences_is_kept_whole(raw):
    assert normalize_generated_code(raw, path="a.py") == FILE_WITH_INNER_FENCES


def test_bare_js_file_with_fenced_template_string_is_kept_whole():
    src = "const t = `\n```js\nx()\n```\n`;\nexport default t;"
    assert normalize_generated_code(src, path="a.js") == src


def test_bare_file_with_odd_inner_fences_is_not_called_truncated():
    src = 'FENCE = """\n```\n"""\nprint(FENCE)'
    assert normalize_generated_code(src, path="a.py") == src


def test_wrapped_file_cut_off_after_inner_fence_is_truncated():
    raw = '```python\ndef f():\n    """Example:\n\n```python\nf()\n```\n"""\n    ret'
    with pytest.raises(GeneratedCodeError, match="unterminated"):
        normalize_generated_code(raw, path="a.py")


def test_tilde_fences_are_unwrapped():
    assert normalize_generated_code("~~~python\nx = 2\n~~~", path="a.py") == "x = 2"


def test_comment_starting_with_i_cant_is_not_a_refusal():
    src = "# I can't use f-strings here (py2 compat)\nx = '%s' % 1"
    assert normalize_generated_code(src, path="a.py") == src


def test_truncated_markdown_with_unbalanced_fence_is_rejected():
    with pytest.raises(GeneratedCodeError, match="unterminated"):
        normalize_generated_code("# T\n\n```python\nx = 1", path="README.md")


# --- implementer_node ----------------------------------------------------


def _repo_with_file(content: bytes | None):
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value.commit.sha = "base"
    if content is None:
        repo.get_contents.side_effect = GithubException(404, {}, None)
    else:
        repo.get_contents.return_value = SimpleNamespace(
            decoded_content=content, sha="s", path="app.py"
        )
    repo.create_pull.return_value = SimpleNamespace(number=1, html_url="u")
    return repo


def _state(idea):
    return {
        "repo_name": "o/r",
        "idea": idea,
        "issue_number": 7,
        "iteration": 0,
        "review": "",
        "branch_name": "",
        "target_file_path": "",
        "pr_number": 0,
    }


@pytest.mark.asyncio
async def test_implementer_without_target_fails_instead_of_inventing_file(monkeypatch):
    repo = _repo_with_file(None)
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return "x = 1"

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.implementer_node(_state("Add a dark mode toggle."))
    assert exc.value.error_code == "NO_TARGET_FILE"
    repo.create_file.assert_not_called()
    repo.create_git_ref.assert_not_called()


@pytest.mark.asyncio
async def test_implementer_rejects_truncated_output_before_committing(monkeypatch):
    repo = _repo_with_file(b"x = 1\n")
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return LLMResponse("x = 2\ndef f(", finish_reason="length")

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.implementer_node(_state("Fix app.py:1-1"))
    assert exc.value.error_code == "INVALID_GENERATED_CODE"
    repo.create_git_ref.assert_not_called()
    repo.update_file.assert_not_called()


@pytest.mark.asyncio
async def test_implementer_skips_files_too_large_to_rewrite(monkeypatch):
    monkeypatch.setattr(agents, "IMPLEMENTER_MAX_FILE_CHARS", 100)
    repo = _repo_with_file(b"x" * 101)
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))
    called = []

    async def llm(*a, **k):
        called.append(1)
        return "x = 2"

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.implementer_node(_state("Fix app.py:1-1"))
    assert exc.value.error_code == "TARGET_FILE_TOO_LARGE"
    assert not called
    repo.create_git_ref.assert_not_called()


# --- architect: missing targets end the run ------------------------------


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(agents, "supabase", None)

    async def fake_exec(*a, **k):
        p = MagicMock(returncode=0)

        async def communicate():
            return (b"", b"")

        p.communicate = communicate
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.side_effect = lambda p, ref=None: (
        [MagicMock(path="a")] if p == "" else MagicMock(sha="s", decoded_content=b"x")
    )
    pr = MagicMock()
    repo.get_pull.return_value = pr
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))
    return repo


@pytest.mark.asyncio
async def test_missing_target_issue_aborts(offline, monkeypatch):
    offline.get_issue.side_effect = GithubException(404, {"message": "Not Found"}, None)

    async def llm(s, u, estimated_tokens=3000):
        if "Architect" in s:
            return "Fix app.py:1-5"
        return (
            '{"verdict": "approve"}'
            if ("Maintainer" in s or "Product" in s)
            else "x = 2"
        )

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await agents.run_agent_loop("o/r", target_issue=99999, run_id="r")
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_TARGET_ISSUE"
    offline.create_git_ref.assert_not_called()
    assert not offline.get_pull.return_value.merge.called


@pytest.mark.asyncio
async def test_target_that_is_a_pull_request_aborts(offline, monkeypatch):
    offline.get_issue.return_value = SimpleNamespace(
        title="Refactor",
        body="Edit app.py:1-5",
        pull_request=MagicMock(spec=agents.IssuePullRequest),
    )

    async def llm(s, u, estimated_tokens=3000):
        return (
            '{"verdict": "approve"}'
            if ("Maintainer" in s or "Product" in s)
            else "x = 2"
        )

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await agents.run_agent_loop("o/r", target_issue=5, run_id="r")
    assert result["status"] == "failed"
    assert result["error_code"] == "TARGET_IS_PULL_REQUEST"
    offline.create_git_ref.assert_not_called()


@pytest.mark.asyncio
async def test_directive_without_file_target_fails_run_before_issue(
    offline, monkeypatch
):
    async def llm(s, u, estimated_tokens=3000):
        return "Improve the developer experience overall."

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await agents.run_agent_loop("o/r", run_id="r")
    assert result["status"] == "failed"
    assert result["error_code"] == "NO_TARGET_FILE"
    offline.create_issue.assert_not_called()
    offline.create_file.assert_not_called()


# --- Lead-ins that quote code (review blocker on fix/agent-pipeline-safety) ---
import pytest as _pytest

from agents import GeneratedCodeError as _GCE, normalize_generated_code as _norm


@_pytest.mark.parametrize(
    "reply",
    [
        "Changed parse() to handle empty input:\n```python\nx = 1\n```",
        "Updated config[key] handling as requested.\n```python\nx = 1\n```\nDone.",
        "Fixed the a < b comparison.\n\n```\nx = 1\n```",
    ],
)
def test_python_reply_with_code_quoting_lead_in_is_unwrapped(reply):
    assert _norm(reply, "a.py") == "x = 1"


def test_python_bare_file_with_fence_in_docstring_is_kept_verbatim():
    src = 'def f():\n    """Example:\n\n```python\nf()\n```\n"""\n    return 1'
    assert _norm(src, "a.py") == src


def test_python_unparseable_reply_without_fenced_block_fails_closed():
    with _pytest.raises(_GCE):
        _norm("Changed parse() so it works\n```", "a.py")


@_pytest.mark.parametrize(
    "reply,path",
    [
        ("Changed parse() to handle empty input:\n```js\nconst x = 1;\n```", "a.js"),
        ("Updated the handler (see below)\n```ts\nlet x = 1;\n```", "a.ts"),
    ],
)
def test_non_python_sentence_lead_in_with_fence_is_unwrapped_or_rejected(reply, path):
    # Either a clean extraction or a refusal is acceptable; committing the
    # prose and fence lines verbatim is not.
    try:
        out = _norm(reply, path)
    except _GCE:
        return
    assert "```" not in out and not out.startswith(("Changed", "Updated"))


def test_non_python_bare_file_is_kept_verbatim():
    src = "import x from 'y';\nconst t = 1;\nexport default t;"
    assert _norm(src, "a.js") == src
