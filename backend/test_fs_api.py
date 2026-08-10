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


if __name__ == "__main__":
    repo_name = setup_dummy_repo()

    print("--- Testing /tree Endpoint ---")
    response = client.get(f"/repo/{repo_name}/tree")
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2))

    print("\n--- Testing /file Endpoint (Valid File) ---")
    response2 = client.get(f"/repo/{repo_name}/file?file_path=src/index.py")
    print(f"Status: {response2.status_code}")
    print(response2.json())

    print("\n--- Testing /file Endpoint (Path Traversal Attack) ---")
    response3 = client.get(f"/repo/{repo_name}/file?file_path=../../../../etc/passwd")
    print(f"Status: {response3.status_code}")
    print(response3.json())
