from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def github_repo(monkeypatch):
    repo = MagicMock()
    repo.default_branch = "main"
    github = MagicMock()
    github.get_repo.return_value = repo
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(main, "Github", MagicMock(return_value=github))
    return repo


def test_update_file_uses_feature_branch(github_repo):
    source_file = MagicMock(path="src/index.py", sha="old-sha")
    github_repo.get_contents.return_value = source_file

    response = TestClient(main.app).post(
        "/repo/example/repo/file",
        json={
            "file_path": "src/index.py",
            "content": "print('updated')",
            "branch_name": "feature/issue-139-abcd",
        },
    )

    assert response.status_code == 200
    github_repo.get_contents.assert_called_once_with(
        "src/index.py", ref="feature/issue-139-abcd"
    )
    github_repo.update_file.assert_called_once_with(
        "src/index.py",
        "Update src/index.py via AutoMaintainer IDE",
        "print('updated')",
        "old-sha",
        branch="feature/issue-139-abcd",
    )


def test_create_file_uses_feature_branch(github_repo):
    response = TestClient(main.app).post(
        "/repo/example/repo/file/create",
        json={
            "file_path": "src/new.py",
            "content": "print('new')",
            "branch_name": "feature/issue-139-abcd",
        },
    )

    assert response.status_code == 200
    github_repo.create_file.assert_called_once_with(
        "src/new.py",
        "Create src/new.py via AutoMaintainer IDE",
        "print('new')",
        branch="feature/issue-139-abcd",
    )


def test_delete_file_uses_feature_branch(github_repo):
    source_file = MagicMock(path="src/old.py", sha="old-sha")
    github_repo.get_contents.return_value = source_file

    response = TestClient(main.app).delete(
        "/repo/example/repo/file",
        params={
            "file_path": "src/old.py",
            "branch_name": "feature/issue-139-abcd",
        },
    )

    assert response.status_code == 200
    github_repo.get_contents.assert_called_once_with(
        "src/old.py", ref="feature/issue-139-abcd"
    )
    github_repo.delete_file.assert_called_once_with(
        "src/old.py",
        "Delete src/old.py via AutoMaintainer IDE",
        "old-sha",
        branch="feature/issue-139-abcd",
    )


def test_mutations_reject_default_branch(github_repo):
    source_file = MagicMock(path="src/index.py", sha="old-sha")
    github_repo.get_contents.return_value = source_file

    response = TestClient(main.app).post(
        "/repo/example/repo/file",
        json={
            "file_path": "src/index.py",
            "content": "print('unsafe')",
            "branch_name": "main",
        },
    )

    assert response.status_code == 409
    assert "feature branch" in response.json()["detail"]
    github_repo.get_contents.assert_not_called()
    github_repo.update_file.assert_not_called()
