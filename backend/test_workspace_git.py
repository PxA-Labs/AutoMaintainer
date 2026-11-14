"""Workspace naming, server-side git credential handling, and the Implementer's
relationship with the shared local clone."""

import pytest
from fastapi import HTTPException

from workspace import get_safe_repo_dir


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "ws"))
    yield


# --- get_safe_repo_dir ---------------------------------------------------------


@pytest.mark.parametrize("name", ["owner/repo\n", "a/b\n", "repo\n", "owner/repo\r\n"])
def test_repo_name_rejects_trailing_newline(name):
    """re.match with "$" accepted a trailing newline and mapped it elsewhere."""
    with pytest.raises(HTTPException):
        get_safe_repo_dir(name)


@pytest.mark.parametrize(
    "first,second", [("a/b_c", "a_b/c"), ("a/b", "a_b"), ("x/y.z", "x.y/z")]
)
def test_distinct_repos_get_distinct_dirs(first, second):
    """Flattening "/" to "_" alone made two repositories share one clone."""
    assert get_safe_repo_dir(first) != get_safe_repo_dir(second)


def test_repo_dir_is_stable_and_case_insensitive():
    """GitHub names are case-insensitive: one repository, one clone."""
    assert get_safe_repo_dir("Owner/Repo") == get_safe_repo_dir("owner/repo")
    assert get_safe_repo_dir("owner/repo") == get_safe_repo_dir("owner/repo")


@pytest.mark.parametrize("name", ["../x", "a/..", "..", ".", "a/b/c", "", "a b/c"])
def test_repo_name_rejects_invalid(name):
    with pytest.raises(HTTPException):
        get_safe_repo_dir(name)
