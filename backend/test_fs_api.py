import os
import shutil
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from main import app, get_safe_repo_dir, require_repo_access

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_dummy_repo():
    repo_name = f"PxA-Labs/AutoMaintainerTestSandbox-{uuid.uuid4().hex[:8]}"
    repo_dir = get_safe_repo_dir(repo_name)

    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)

    os.makedirs(os.path.join(repo_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)

    with open(os.path.join(repo_dir, "src", "index.py"), "w") as f:
        f.write("print('hello world')")

    with open(os.path.join(repo_dir, "README.md"), "w") as f:
        f.write("# AutoMaintainer")

    yield repo_name

    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)


@pytest.fixture
def authorized():
    """Stand in for a caller who is authenticated and owns the repo."""
    app.dependency_overrides[require_repo_access] = lambda: "test-org-id"
    yield
    app.dependency_overrides.pop(require_repo_access, None)


@pytest.fixture
def anonymous(monkeypatch):
    """Supabase is configured, but the caller presents no credentials.

    Without this the auth backend short-circuits to 503 ("not configured"),
    which would hide whether authorization is actually enforced.
    """
    import agents

    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setattr(agents, "supabase", MagicMock(), raising=False)
    yield


# --- Authorization: these endpoints read and mutate repository contents and
# must never serve an unauthenticated caller. ---


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/repo/{repo}/tree", {}),
        ("get", "/repo/{repo}/file?file_path=README.md", {}),
        ("get", "/repo/{repo}/search?q=hello", {}),
        ("post", "/repo/{repo}/file", {"json": {"file_path": "a.py", "content": "x"}}),
        (
            "post",
            "/repo/{repo}/file/create",
            {"json": {"file_path": "b.py", "content": "x"}},
        ),
        ("delete", "/repo/{repo}/file?file_path=README.md", {}),
    ],
)
def test_repo_endpoints_reject_anonymous(
    setup_dummy_repo, anonymous, method, path, kwargs
):
    url = path.format(repo=setup_dummy_repo)
    response = getattr(client, method)(url, **kwargs)

    assert response.status_code == 401, (
        f"{method.upper()} {url} returned {response.status_code}; "
        "unauthenticated callers must be rejected"
    )


def test_inline_assist_rejects_anonymous(anonymous):
    response = client.post(
        "/assist/inline",
        json={"prompt": "hi", "file_path": "a.py", "selected_code": ""},
    )
    assert response.status_code == 401


# --- Functional behaviour, exercised as an authorized caller. ---


def test_tree_endpoint(setup_dummy_repo, authorized):
    repo_name = setup_dummy_repo
    response = client.get(f"/repo/{repo_name}/tree")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == repo_name
    assert "children" in data


def test_file_endpoint_valid(setup_dummy_repo, authorized):
    repo_name = setup_dummy_repo
    url = f"/repo/{repo_name}/file?file_path=src/index.py"
    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == {"content": "print('hello world')"}


def test_file_endpoint_path_traversal(setup_dummy_repo, authorized):
    repo_name = setup_dummy_repo
    url = f"/repo/{repo_name}/file?file_path=../../../../etc/passwd"
    response = client.get(url)
    assert response.status_code in [400, 403, 404]


@pytest.mark.parametrize(
    "denied",
    [".git/config", ".git/HEAD", ".GIT/config", ".git/refs/heads/main"],
)
def test_file_endpoint_blocks_git_metadata(setup_dummy_repo, authorized, denied):
    """.git/config can contain the credentials used to clone the repo."""
    repo_name = setup_dummy_repo
    response = client.get(f"/repo/{repo_name}/file?file_path={denied}")
    assert response.status_code == 403, (
        f"{denied} returned {response.status_code}; repository metadata "
        "must not be readable through the filesystem API"
    )
