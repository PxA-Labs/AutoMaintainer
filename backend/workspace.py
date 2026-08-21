import os
import tempfile
from pathlib import Path
from fastapi import HTTPException


def get_base_workspace_dir() -> Path:
    """Returns the base directory for cloned repositories and local workspaces.

    Prefers AUTOMAINTAINER_WORKSPACE_DIR if set, otherwise falls back to the
    OS-standard temporary directory (e.g., %TEMP%\\automaintainer on Windows or
    /tmp/automaintainer on Linux/macOS).
    """
    env_path = os.getenv("AUTOMAINTAINER_WORKSPACE_DIR")
    if env_path:
        return Path(env_path).resolve()
    return (Path(tempfile.gettempdir()) / "automaintainer").resolve()


def get_safe_repo_dir(repo_name: str) -> Path:
    """Safely resolves and creates the directory path for a target repository.

    Prevents directory traversal and creates parent directories if needed.
    """
    base_dir = get_base_workspace_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    clean_name = repo_name.replace("/", "_").replace("\\", "_")
    if ".." in clean_name:
        raise HTTPException(status_code=400, detail="Invalid repository name")
    repo_dir = (base_dir / clean_name).resolve()
    if not repo_dir.is_relative_to(base_dir) or repo_dir == base_dir:
        raise HTTPException(status_code=400, detail="Invalid repository name")
    return repo_dir


def get_safe_target_path(repo_dir: Path, file_path: str) -> Path:
    """Validates that file_path resides strictly within repo_dir."""
    clean_path = file_path.lstrip("/").lstrip("\\")
    if ".." in clean_path.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    target_path = (repo_dir / clean_path).resolve()
    if not target_path.is_relative_to(repo_dir):
        raise HTTPException(status_code=403, detail="Invalid file path")
    return target_path
