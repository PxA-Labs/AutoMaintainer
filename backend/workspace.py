import os
import re
import tempfile
from pathlib import Path
from fastapi import HTTPException

# Top-level entries inside a repository that are never served or written
# through the filesystem API.
DENIED_TOP_LEVEL = {".git"}


def get_base_workspace_dir() -> Path:
    """Returns the base directory for cloned repositories and local workspaces.

    Prefers AUTOMAINTAINER_WORKSPACE_DIR if set, otherwise falls back to the
    OS-standard temporary directory (e.g., %TEMP%\\automaintainer on Windows or
    /tmp/automaintainer on Linux/macOS).
    """
    env_path = os.getenv("AUTOMAINTAINER_WORKSPACE_DIR")
    if env_path:
        return Path(os.path.abspath(env_path))
    return Path(os.path.abspath(os.path.join(tempfile.gettempdir(), "automaintainer")))


def get_safe_repo_dir(repo_name: str) -> Path:
    """Safely resolves and creates the directory path for a target repository.

    Validates repository name against strict allowlist and prevents path traversal.
    """
    if not repo_name or not isinstance(repo_name, str):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    # Strict allowlist check: owner/repo or single name
    if not re.match(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    clean_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", repo_name)
    if ".." in clean_name or not clean_name:
        raise HTTPException(status_code=400, detail="Invalid repository name")

    base_dir = get_base_workspace_dir()
    base_dir.mkdir(parents=True, exist_ok=True)

    base_str = os.path.abspath(str(base_dir))
    target_str = os.path.abspath(os.path.join(base_str, clean_name))

    if not target_str.startswith(base_str + os.path.sep) and target_str != base_str:
        raise HTTPException(status_code=400, detail="Invalid repository name")

    repo_dir = Path(target_str)
    if not repo_dir.is_relative_to(base_dir) or repo_dir == base_dir:
        raise HTTPException(status_code=400, detail="Invalid repository name")

    return repo_dir


def get_safe_target_path(repo_dir: Path, file_path: str) -> Path:
    """Validates that file_path resides strictly within repo_dir, resolving any intermediate symlinks."""
    if not file_path or not isinstance(file_path, str):
        raise HTTPException(status_code=400, detail="Invalid file path")

    clean_path = file_path.lstrip("/").lstrip("\\")
    if ".." in clean_path.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    norm_path = os.path.normpath(clean_path)
    if norm_path.startswith("..") or os.path.isabs(norm_path):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    # The repository metadata directory is not application content. `.git/config`
    # can carry the credentials used for the clone, and `.git/` exposes full
    # history regardless of what the working tree shows.
    first_component = norm_path.replace("\\", "/").split("/")[0]
    if first_component.lower() in DENIED_TOP_LEVEL:
        raise HTTPException(
            status_code=403, detail="Access to this path is not allowed"
        )

    repo_base_str = os.path.abspath(str(repo_dir))
    target_full_str = os.path.abspath(os.path.join(repo_base_str, norm_path))

    if not target_full_str.startswith(repo_base_str + os.path.sep):
        raise HTTPException(status_code=403, detail="Invalid file path")

    real_repo = os.path.realpath(repo_base_str)
    real_target = os.path.realpath(target_full_str)

    if not real_target.startswith(real_repo + os.path.sep):
        raise HTTPException(status_code=403, detail="Invalid file path")

    # Re-check the denylist against the RESOLVED path. The check above only saw
    # the path the caller sent, and symlink resolution happens after it, so a
    # symlink committed to the repository (leak -> .git/config) would otherwise
    # pass: it is not named ".git", yet it resolves inside .git and stays within
    # real_repo. Repository contents are tenant-controlled, and the write routes
    # share this helper, so the same gap would allow overwriting .git/config
    # (core.hooksPath) and turning the next server-side git call into RCE.
    resolved_first = (
        os.path.relpath(real_target, real_repo).replace("\\", "/").split("/")[0]
    )
    if resolved_first.lower() in DENIED_TOP_LEVEL:
        raise HTTPException(
            status_code=403, detail="Access to this path is not allowed"
        )

    return Path(real_target)
