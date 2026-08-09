import json
import os
import shutil
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def setup_dummy_repo():
    repo_name = "PxA-Labs/AutoMaintainer"
    repo_dir = f"/tmp/{repo_name.replace('/', '_')}"

    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)

    os.makedirs(os.path.join(repo_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)

    with open(os.path.join(repo_dir, "src", "index.py"), "w") as f:
        f.write("print('hello world')")

    with open(os.path.join(repo_dir, "README.md"), "w") as f:
        f.write("# AutoMaintainer")

    return repo_name


def test_get_repo_tree():
    repo_name = setup_dummy_repo()
    response = client.get(f"/repo/{repo_name}/tree")
    assert response.status_code == 200
    assert response.json()["name"] == repo_name


def test_get_repo_file_valid():
    repo_name = "PxA-Labs/AutoMaintainer"
    response = client.get(f"/repo/{repo_name}/file?file_path=src/index.py")
    assert response.status_code == 200
    assert response.json()["content"] == "print('hello world')"


def test_get_repo_file_path_traversal():
    repo_name = "PxA-Labs/AutoMaintainer"
    response = client.get(f"/repo/{repo_name}/file?file_path=../../../../etc/passwd")
    assert response.status_code in (400, 403)
