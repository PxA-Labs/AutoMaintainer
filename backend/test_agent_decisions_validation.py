"""Implementer output is validated before it is committed (#393): syntax
checks for more languages and a guard against truncated rewrites."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents
from agents import GeneratedCodeError, validate_generated_code

# --- syntax checks ------------------------------------------------------------


@pytest.mark.parametrize(
    "path, code",
    [
        ("app.py", "def f(x):\n    return x + 1\n"),
        ("stubs.pyi", "def f(x: int) -> int: ...\n"),
        ("package.json", '﻿{"name": "x", "version": "1.0.0"}'),
        ("pyproject.toml", '[project]\nname = "x"\ndependencies = ["a"]\n'),
        ("ci.yml", "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"),
        ("multi.yaml", "a: 1\n---\nb: 2\n"),
        ("cfn.yaml", "Value: !Ref MyBucket\n"),  # application tags are fine
        ("tsconfig.json", '{\n  // comments are allowed here\n  "strict": true\n}'),
        (".vscode/settings.json", '{ /* jsonc */ "a": 1 }'),
        ("README.md", "# Title\n\n```python\ndef f(\n```\n"),  # not checked
        ("script.sh", "if [ -f x ]; then\n"),  # no checker for shell
    ],
)
def test_valid_or_unchecked_files_pass(path, code):
    validate_generated_code(code, path)


@pytest.mark.parametrize(
    "path, code, language",
    [
        ("app.py", "def f(x):\n    return (x + 1\n", "Python"),
        ("package.json", '{"name": "x",', "JSON"),
        ("package.json", "{'name': 'x'}", "JSON"),
        ("pyproject.toml", '[project\nname = "x"\n', "TOML"),
        ("ci.yml", "jobs:\n  build: [a, b\n", "YAML"),
        ("ci.yaml", "a: 1\n b: 2\n", "YAML"),
        ("index.js", "function f() {\n  return [1, 2;\n}\n", "JavaScript"),
        ("index.ts", "export function f(): void {\n  if (x) {\n", "JavaScript"),
        ("App.tsx", "const s = `value ${x`;\n", "JavaScript"),
        ("a.mjs", "/* unterminated\nconst x = 1;\n", "JavaScript"),
        ("a.cjs", "module.exports = { a: 1 ]\n", "JavaScript"),
    ],
)
def test_invalid_syntax_is_rejected(path, code, language):
    with pytest.raises(GeneratedCodeError, match=f"not valid {language}"):
        validate_generated_code(code, path)


@pytest.mark.parametrize(
    "code",
    [
        # Brackets inside strings, comments, templates and regex literals.
        "const a = \"(\";\nconst b = '[';\n// } stray in a comment\n/* { */\n",
        "const t = `a ${ {k: [1, 2]}.k } b ${`nested ${x}`} )`;\n",
        "const re = /[(\\]]+/g;\nconst ok = x.match(/\\(/);\nif (a / b > 1) {}\n",
        "function f(){ return /[}]/.test(s); }\n",
        # JSX text with apostrophes and closing tags.
        "export const C = () => (\n  <p>Don't {name}'s</p>\n);\n",
        "const el = <div className={cls}><br />{a && <span>x</span>}</div>;\n",
        "const el = <List items={[a, b]}/>;\nconst f = <Foo a={b}/><Bar c={d}/>;\n",
        # TypeScript generics and type annotations.
        "function id<T>(x: Array<T>): Map<string, T[]> { return new Map(); }\n",
    ],
)
def test_js_heuristic_accepts_valid_code(code):
    validate_generated_code(code, "src/app.tsx")
    validate_generated_code(code, "src/app.js")


def test_js_regex_literals_with_slashes_in_classes():
    code = "const parts = p.split(/[/\\\\]/);\nconst seg = /^[^/]+/.exec(p);\n"
    validate_generated_code(code, "src/path.ts")


def test_js_heuristic_is_not_quadratic_on_unclosed_regex_candidates():
    # Each "/" after an operator starts a regex candidate that never closes.
    # The old scan walked to the end of the line for every candidate: ~114 s
    # on this 60,002-character line. Generous bound to avoid CI flakiness.
    hostile = "x=" + "(/[" * 20000
    started = time.perf_counter()
    error = agents._js_balance_error(hostile)
    assert time.perf_counter() - started < 5
    assert error is not None  # the "(" are never closed


@pytest.mark.asyncio
async def test_implementer_validates_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    repo = _repo_with_file(b"export const a = 1;\n")
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return "export const a = 2;\n"

    threads = []

    def spy(*args, **kwargs):
        threads.append(threading.current_thread())

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    monkeypatch.setattr(agents, "validate_generated_code", spy)
    await agents.implementer_node(_state("Update src/app.js:1-1"))
    assert threads and threads[0] is not threading.main_thread()


def test_existing_syntax_problems_do_not_block_a_change():
    # The checker only refuses errors the Implementer introduces: a file that
    # already fails (another dialect, or pre-existing breakage) is not checked.
    old = "print 'python 2'\n"
    validate_generated_code("print 'python 2, fixed'\n", "legacy.py", original=old)
    with pytest.raises(GeneratedCodeError):
        validate_generated_code("def f(:\n", "legacy.py", original="x = 1\n")


@pytest.mark.parametrize("path", ["deep.json", "deep.yaml", "deep.toml"])
def test_too_deep_nesting_is_a_syntax_error_not_a_crash(path):
    deep = ("a = " if path.endswith(".toml") else "") + "[" * 50000
    with pytest.raises(GeneratedCodeError, match="not valid"):
        validate_generated_code(deep, path)
    # An original that already hits the limit takes the "already fails" path.
    validate_generated_code(deep + "1", path, original=deep)


# --- truncated rewrites ---------------------------------------------------------

BIG = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(150))


def test_large_shrink_is_rejected():
    assert len(BIG.encode()) > 2048
    truncated = BIG[: len(BIG) // 4]
    truncated = truncated[: truncated.rfind("\n\n\n")] + "\n"  # still valid Python
    with pytest.raises(GeneratedCodeError, match="shrinks the file"):
        validate_generated_code(truncated, "mod.py", original=BIG, directive="Fix f3")


SMALL = "def f0(x):\n    return x\n"


@pytest.mark.parametrize(
    "directive",
    [
        "Remove the unused helpers f1-f149.",
        "Please delete dead code.",
        "Clean up mod.py: drop every helper except f0",
        "Refactor mod.py\n\n- Remove f1-f149\n- Keep f0",
        "1. Get rid of the legacy helpers",
        "Prune `f1`..`f149` from mod.py",
    ],
)
def test_shrink_allowed_when_task_asks_for_removal(directive):
    assert agents.asks_for_removal(directive)
    validate_generated_code(SMALL, "mod.py", original=BIG, directive=directive)


@pytest.mark.parametrize(
    "directive",
    [
        # Mentions of removal words that are not a request to remove code.
        "Fix the drop-down menu closing",
        "Use .trim() on input",
        "The delete button crashes",
        "Pressing remove on a cart item does nothing",
        "We removed the cache last week and now f3 is slow",
        "Trim whitespace from user input",
        "Fix f3; the removal flow is broken",
        "",
    ],
)
def test_mentions_of_removal_do_not_lift_the_shrink_guard(directive):
    assert not agents.asks_for_removal(directive)
    with pytest.raises(GeneratedCodeError, match="shrinks the file"):
        validate_generated_code(SMALL, "mod.py", original=BIG, directive=directive)


def test_shrink_guard_boundaries(monkeypatch):
    # At or above the ratio is fine; small files and new files are not guarded.
    keep = BIG[: int(len(BIG) * 0.41)]
    keep = keep[: keep.rfind("\n\n\n")] + "\n"
    keep += "#" * (int(len(BIG) * 0.4) + 1 - len(keep))
    validate_generated_code(keep, "notes.txt", original=BIG)
    validate_generated_code("x", "notes.txt", original="y" * 2048)  # not > 2 KB
    validate_generated_code("x", "notes.txt", original=None)

    monkeypatch.setattr(agents, "IMPLEMENTER_MIN_SIZE_RATIO", 0.9)
    with pytest.raises(GeneratedCodeError, match="below 90%"):
        validate_generated_code(keep, "notes.txt", original=BIG)
    monkeypatch.setattr(agents, "IMPLEMENTER_SHRINK_GUARD_MIN_BYTES", 10**9)
    validate_generated_code(keep, "notes.txt", original=BIG)


# --- implementer_node -----------------------------------------------------------


def _repo_with_file(content: bytes):
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value.commit.sha = "base"
    repo.get_contents.return_value = SimpleNamespace(
        decoded_content=content, sha="s", path="x"
    )
    repo.create_pull.return_value = SimpleNamespace(number=1, html_url="u")
    return repo


def _state(idea, review=""):
    return {
        "repo_name": "o/r",
        "idea": idea,
        "issue_number": 7,
        "iteration": 0,
        "review": review,
        "branch_name": "",
        "target_file_path": "",
        "pr_number": 0,
    }


@pytest.mark.parametrize(
    "target, original, reply",
    [
        ("config/app.json", b'{"a": 1}\n', '{"a": 2,\n'),
        ("config/app.yaml", b"a: 1\n", "a: [2\n"),
        ("pyproject.toml", b"[tool]\nx = 1\n", "[tool\nx = 2\n"),
        ("src/app.js", b"export const a = 1;\n", "export function f() {\n"),
        ("src/app.py", b"x = 1\n", "def f(:\n    pass\n"),
        ("src/app.py", BIG.encode(), "def f0(x):\n    return x\n"),
    ],
)
@pytest.mark.asyncio
async def test_implementer_refuses_invalid_output_before_committing(
    monkeypatch, target, original, reply
):
    monkeypatch.setattr(agents, "supabase", None)
    repo = _repo_with_file(original)
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return reply

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.implementer_node(_state(f"Update {target}:1-1"))
    assert exc.value.error_code == "INVALID_GENERATED_CODE"
    repo.create_git_ref.assert_not_called()
    repo.update_file.assert_not_called()
    repo.create_file.assert_not_called()


@pytest.mark.asyncio
async def test_maintainer_feedback_does_not_lift_the_shrink_guard(monkeypatch):
    # Only the task can ask for removal: a review mentioning "remove the debug
    # print" must not let a truncated rewrite through on a later iteration.
    monkeypatch.setattr(agents, "supabase", None)
    repo = _repo_with_file(BIG.encode())
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return SMALL

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    review = '{"verdict": "reject", "requested_changes": ["Remove the debug print."]}'
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.implementer_node(_state("Fix f3 in src/app.py:1-600", review))
    assert exc.value.error_code == "INVALID_GENERATED_CODE"
    repo.update_file.assert_not_called()


@pytest.mark.asyncio
async def test_implementer_commits_requested_removal(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    repo = _repo_with_file(BIG.encode())
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: repo))

    async def llm(*a, **k):
        return "def f0(x):\n    return x\n"

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await agents.implementer_node(
        _state("Remove every helper except f0 from src/app.py:1-600")
    )
    assert result["code"] == "def f0(x):\n    return x"
    repo.update_file.assert_called_once()
