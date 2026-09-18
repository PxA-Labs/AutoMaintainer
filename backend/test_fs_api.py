import os
import shutil
import uuid
import pytest
from fastapi.testclient import TestClient
from main import app, get_safe_repo_dir

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_dummy_repo():
    repo_name = f"PxA-Labs/AutoMaintainerTestSandbox-{uuid.uuid4().hex}"
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


def test_tree_endpoint(setup_dummy_repo):
    repo_name = setup_dummy_repo
    response = client.get(f"/repo/{repo_name}/tree")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == repo_name
    assert "children" in data


def test_file_endpoint_valid(setup_dummy_repo):
    repo_name = setup_dummy_repo
    url = f"/repo/{repo_name}/file?file_path=src/index.py"
    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == {"content": "print('hello world')"}


def test_file_endpoint_path_traversal(setup_dummy_repo):
    repo_name = setup_dummy_repo
    url = f"/repo/{repo_name}/file?file_path=../../../../etc/passwd"
    response = client.get(url)
    assert response.status_code in [400, 403, 404]
