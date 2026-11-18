"""The ADK Reviewer's read-only tools (#525), tested without ADK.

Every tool is scoped to one clone at the PR head: paths are resolved to real
paths and refused when they leave the clone (``..``, absolute paths, drive
letters, symlinks that point outside), ``.git`` is never served, and every
output is capped. ``search_code`` passes the query to ripgrep after ``--``
as a fixed string, so a query cannot become a flag.
"""

import os
import shutil
import subprocess

import pytest

from automaintainer._runtime.specialists import tools
from automaintainer._runtime.specialists.tools import (
    ReviewTools,
    ToolLimits,
    ToolRefused,
    ToolUnavailable,
)

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep missing")

DIFF_FILES = [
    ("app.py", "--- app.py (modified)\n@@ -1 +1 @@\n-x = 1\n+x = 2"),
    ("lib/util.py", "--- lib/util.py (added)\n@@ -0,0 +1 @@\n+def f(): pass"),
]


@pytest.fixture
def clone(tmp_path):
    root = tmp_path / "clone"
    (root / "lib").mkdir(parents=True)
    (root / "app.py").write_text("x = 2\nprint(x)\n", encoding="utf-8")
    (root / "lib" / "util.py").write_text("def f(): pass\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[remote] token=secret\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return root


def make_tools(root, **kwargs):
    kwargs.setdefault("diff_files", DIFF_FILES)
    kwargs.setdefault("verification_log", "status=passed\n1 passed")
    return ReviewTools(root, **kwargs)


def symlink_or_skip(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as e:  # Windows without privilege
        pytest.skip(f"cannot create symlinks here: {e}")


# --- read_file -------------------------------------------------------------------


def test_read_file_returns_numbered_lines(clone):
    out = make_tools(clone).read_file("app.py")
    assert out.splitlines() == ["app.py lines 1-2 of 2", "1| x = 2", "2| print(x)"]


def test_read_file_range_and_nested_path(clone):
    t = make_tools(clone)
    assert t.read_file("app.py", 2, 2).splitlines()[1:] == ["2| print(x)"]
    assert "1| def f(): pass" in t.read_file("lib/util.py")
    assert "1| def f(): pass" in t.read_file("lib\\util.py")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "lib/../../outside.txt",
        "..\\outside.txt",
        "/etc/passwd",
        "\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "C:outside.txt",
        "",
        "   ",
        ".",
        "app.py\x00.txt",
    ],
)
def test_read_file_refuses_paths_outside_the_clone(clone, path):
    with pytest.raises(ToolRefused):
        make_tools(clone).read_file(path)


def test_read_file_refuses_an_absolute_path_into_the_clone(clone):
    with pytest.raises(ToolRefused):
        make_tools(clone).read_file(str(clone / "app.py"))


@pytest.mark.parametrize("path", [".git/config", ".GIT/config", "lib/../.git/config"])
def test_read_file_never_serves_git_metadata(clone, path):
    with pytest.raises(ToolRefused):
        make_tools(clone).read_file(path)


def test_read_file_refuses_a_symlink_that_escapes(clone):
    symlink_or_skip(clone / "leak.txt", clone.parent / "outside.txt")
    with pytest.raises(ToolRefused, match="outside"):
        make_tools(clone).read_file("leak.txt")


def test_read_file_refuses_a_directory_symlink_that_escapes(clone):
    symlink_or_skip(clone / "up", clone.parent, directory=True)
    with pytest.raises(ToolRefused, match="outside"):
        make_tools(clone).read_file("up/outside.txt")


def test_read_file_checks_the_real_path_not_the_spelling(clone, monkeypatch):
    """Where symlinks cannot be created (Windows without the privilege), the
    resolution is simulated: a link inside the clone whose real path is
    outside is refused, because the check runs on the real path."""
    t = make_tools(clone)
    real = tools.os.path.realpath
    outside = str(clone.parent / "outside.txt")

    def realpath(p, *a, **k):
        return outside if os.path.basename(str(p)) == "leak.txt" else real(p, *a, **k)

    monkeypatch.setattr(tools.os.path, "realpath", realpath)
    with pytest.raises(ToolRefused, match="outside"):
        t.read_file("leak.txt")
    with pytest.raises(ToolRefused, match="outside"):
        t.read_file("lib/leak.txt")


def test_read_file_follows_a_symlink_inside_the_clone(clone):
    symlink_or_skip(clone / "alias.py", clone / "app.py")
    assert "1| x = 2" in make_tools(clone).read_file("alias.py")


def test_read_file_missing_directory_and_binary(clone):
    t = make_tools(clone)
    with pytest.raises(ToolRefused, match="no such file"):
        t.read_file("nope.py")
    with pytest.raises(ToolRefused, match="not a file"):
        t.read_file("lib")
    (clone / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ToolRefused, match="binary"):
        t.read_file("blob.bin")


@pytest.mark.parametrize("start,end", [(0, 1), (3, 2), (5, None)])
def test_read_file_rejects_bad_ranges(clone, start, end):
    with pytest.raises(ToolRefused):
        make_tools(clone).read_file("app.py", start, end)


def test_read_file_caps_lines_and_characters(clone):
    (clone / "big.py").write_text(
        "".join(f"line {i} " + "y" * 50 + "\n" for i in range(1, 1001)),
        encoding="utf-8",
    )
    limits = ToolLimits(max_file_lines=10, max_output_chars=400)
    out = make_tools(clone, limits=limits).read_file("big.py")
    assert len(out) <= 400 + len(tools.TRUNCATED)
    assert out.endswith(tools.TRUNCATED)
    assert "big.py lines 1-10 of 1000" in out
    assert "11| " not in out


# --- get_diff --------------------------------------------------------------------


def test_get_diff_whole_and_per_file(clone):
    t = make_tools(clone)
    whole = t.get_diff()
    assert "+x = 2" in whole and "+def f(): pass" in whole
    assert whole.startswith("page 1 of 1\n")
    one = t.get_diff("lib/util.py")
    assert "+def f(): pass" in one and "+x = 2" not in one
    assert t.get_diff("lib\\util.py") == one
    assert "no changes" in t.get_diff("other.py")


def test_get_diff_pages(clone):
    files = [("big.py", "--- big.py (modified)\n" + "+z\n" * 300)]
    t = make_tools(clone, diff_files=files, limits=ToolLimits(diff_page_chars=100))
    first = t.get_diff()
    assert first.startswith("page 1 of ")
    pages = int(first.split("\n", 1)[0].rsplit(" ", 1)[1])
    assert pages > 1
    assert t.get_diff(page=pages).startswith(f"page {pages} of {pages}")
    with pytest.raises(ToolRefused):
        t.get_diff(page=pages + 1)
    with pytest.raises(ToolRefused):
        t.get_diff(page=0)


# --- get_verification_log --------------------------------------------------------


def test_verification_log_is_tail_capped(clone):
    log = "head-line\n" + "x" * 1000 + "\nlast-line"
    t = make_tools(clone, verification_log=log, limits=ToolLimits(max_log_chars=50))
    out = t.get_verification_log()
    assert out.endswith("last-line")
    assert "head-line" not in out
    assert out.startswith(tools.TRUNCATED_HEAD)


def test_verification_log_when_nothing_ran(clone):
    assert make_tools(clone, verification_log="").get_verification_log() == (
        "(no verification output)"
    )


# --- search_code -----------------------------------------------------------------


def test_search_argv_is_fixed_string_with_the_query_after_double_dash(clone):
    argv = make_tools(clone).search_argv("--pre=sh", rg="rg")
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["--pre=sh", "."]
    assert "--fixed-strings" in argv[:sep]
    assert "--no-config" in argv[:sep]
    assert "--follow" not in argv and "-L" not in argv
    regex = make_tools(clone).search_argv("a.*b", rg="rg", regex=True)
    assert "--fixed-strings" not in regex and regex[-2:] == ["a.*b", "."]


@pytest.mark.parametrize("query", ["", "a\nb", "a\x00b", "q" * 1001])
def test_search_refuses_bad_queries(clone, query):
    with pytest.raises(ToolRefused):
        make_tools(clone).search_code(query)


def test_search_without_ripgrep_is_unavailable(clone, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(ToolUnavailable):
        make_tools(clone).search_code("x")


@needs_rg
def test_search_finds_fixed_strings(clone):
    out = make_tools(clone).search_code("print(x)")
    assert "app.py:2:print(x)" in out.replace("\\", "/")
    assert "no matches" in make_tools(clone).search_code("x.*nothing")


@needs_rg
@pytest.mark.parametrize("query", ["--files", "--pre=sh", "-e", "--version"])
def test_search_flag_injection_is_a_literal_query(clone, query):
    (clone / "flags.txt").write_text(f"literal {query} here\n", encoding="utf-8")
    out = make_tools(clone).search_code(query).replace("\\", "/")
    assert f"flags.txt:1:literal {query} here" in out
    # --files would list every file; --version would print ripgrep's version.
    assert "app.py" not in out and "ripgrep" not in out


@needs_rg
def test_search_skips_git_metadata_and_outside_files(clone):
    out = make_tools(clone).search_code("secret")
    assert "no matches" in out
    assert "no matches" in make_tools(clone).search_code("TOP SECRET")


@needs_rg
def test_search_does_not_follow_symlinks_out_of_the_clone(clone):
    symlink_or_skip(clone / "up", clone.parent, directory=True)
    assert "no matches" in make_tools(clone).search_code("TOP SECRET")


@needs_rg
def test_search_ignores_repository_ignore_files(clone):
    """An attacker's .ignore/.rgignore cannot hide a file from the review."""
    (clone / ".rgignore").write_text("app.py\n", encoding="utf-8")
    (clone / ".ignore").write_text("app.py\n", encoding="utf-8")
    assert "app.py:2:print(x)" in make_tools(clone).search_code("print(x)").replace(
        "\\", "/"
    )


@needs_rg
def test_search_output_is_capped(clone):
    (clone / "many.txt").write_text("needle\n" * 500, encoding="utf-8")
    limits = ToolLimits(max_search_results=5, max_output_chars=10_000)
    out = make_tools(clone, limits=limits).search_code("needle")
    assert out.count("needle") == 5
    assert out.endswith(tools.TRUNCATED)


@needs_rg
def test_search_times_out(clone, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="rg", timeout=1)

    monkeypatch.setattr(tools.subprocess, "run", slow)
    with pytest.raises(ToolUnavailable, match="timed out"):
        make_tools(clone).search_code("x")


def test_tools_need_an_existing_clone(tmp_path):
    with pytest.raises(ValueError):
        ReviewTools(tmp_path / "missing", diff_files=[], verification_log="")
