from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import main


class FakeBranchRepo:
    default_branch = "main"

    def __init__(self):
        self.requested_refs = []

    def get_branch(self, branch_name):
        assert branch_name == "feature/demo"
        return SimpleNamespace(commit=SimpleNamespace(sha="branch-sha"))

    def get_git_tree(self, sha, recursive=True):
        assert sha == "branch-sha"
        assert recursive is True
        return SimpleNamespace(
            truncated=False,
            tree=[
                SimpleNamespace(type="tree", path="src"),
                SimpleNamespace(type="blob", path="src/index.py"),
                SimpleNamespace(type="blob", path="README.md"),
            ],
        )

    def get_contents(self, file_path, ref=None):
        self.requested_refs.append((file_path, ref))
        return SimpleNamespace(
            decoded_content=(
                b"print('feature branch')\n"
                if file_path == "src/index.py"
                else b"feature branch README\n"
            )
        )


def install_fake_github(monkeypatch):
    repo = FakeBranchRepo()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        main, "Github", lambda token: SimpleNamespace(get_repo=lambda name: repo)
    )
    return repo


def test_branch_tree_uses_github_feature_branch(monkeypatch):
    install_fake_github(monkeypatch)
    response = TestClient(main.app).get(
        "/repo/example/repo/tree", params={"branch_name": "feature/demo"}
    )

    assert response.status_code == 200
    assert response.json()["children"][0]["path"] == "src"
    assert response.json()["children"][0]["children"][0]["path"] == "src/index.py"


def test_branch_file_read_uses_github_feature_branch(monkeypatch):
    repo = install_fake_github(monkeypatch)
    response = TestClient(main.app).get(
        "/repo/example/repo/file",
        params={"file_path": "src/index.py", "branch_name": "feature/demo"},
    )

    assert response.status_code == 200
    assert response.json() == {"content": "print('feature branch')\n"}
    assert repo.requested_refs == [("src/index.py", "feature/demo")]


def test_branch_search_reads_files_from_github_feature_branch(monkeypatch):
    repo = install_fake_github(monkeypatch)
    response = TestClient(main.app).get(
        "/repo/example/repo/search",
        params={"q": "feature", "branch_name": "feature/demo"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["file"] == "src/index.py"
    assert all(ref == "feature/demo" for _, ref in repo.requested_refs)
