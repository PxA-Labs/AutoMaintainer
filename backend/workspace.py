import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# owner/repo or a bare name. Used with re.fullmatch: re.match with "$" would
# also accept a trailing newline ("owner/repo\n").
REPO_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?")

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


# Clones keyed by GitHub repository id live in this subdirectory of the
# workspace. Name-keyed directories are always "<slug>-<12 hex>", so no
# repository name can map onto it.
REPO_ID_DIR = "_by_id"


def _repo_id_key(github_repo_id) -> Optional[str]:
    """The id as a directory name, or None when no id is known.

    Anything but a positive integer is rejected: the id becomes a path
    component.
    """
    if github_repo_id is None:
        return None
    if isinstance(github_repo_id, bool) or not isinstance(github_repo_id, int):
        raise HTTPException(status_code=400, detail="Invalid repository id")
    if github_repo_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid repository id")
    return str(github_repo_id)


def get_safe_repo_dir(repo_name: str, github_repo_id: Optional[int] = None) -> Path:
    """Safely resolves and creates the directory path for a target repository.

    Validates repository name against strict allowlist and prevents path traversal.

    With ``github_repo_id`` (``repositories.id``, GitHub's repository id) the
    directory is ``<base>/_by_id/<id>``. The id survives a rename or a
    transfer, so the repository keeps one clone, and a repository deleted and
    recreated under the same name gets a new id and therefore a fresh clone
    rather than the old repository's working tree. The name is still
    validated, because callers pass it to git.

    Without an id the directory is a readable slug plus a hash of the full
    name, so the mapping is still one-to-one: flattening "/" alone made
    "a/b_c" and "a_b/c" share one clone (and therefore one tenant could read
    another's working tree). The hash is over the lower-cased name because
    GitHub names are case-insensitive -- "Owner/Repo" and "owner/repo" are
    the same repository. Callers that know the id should pass it; the name
    key cannot tell a recreated repository from the old one.
    """
    if not repo_name or not isinstance(repo_name, str):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    # Strict allowlist check: owner/repo or single name
    if not REPO_NAME_PATTERN.fullmatch(repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    # GitHub repo names are case-insensitive: one clone per repo on every OS.
    slug = repo_name.lower().replace("/", "_")
    if ".." in slug or slug.strip(".") == "":
        raise HTTPException(status_code=400, detail="Invalid repository name")

    id_key = _repo_id_key(github_repo_id)
    if id_key is not None:
        clean_name = os.path.join(REPO_ID_DIR, id_key)
    else:
        digest = hashlib.sha256(repo_name.lower().encode("utf-8")).hexdigest()[:12]
        clean_name = f"{slug}-{digest}"

    base_dir = get_base_workspace_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    if id_key is not None:
        (base_dir / REPO_ID_DIR).mkdir(exist_ok=True)

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


# --- Server-side git clone/sync ---------------------------------------------

GIT_TIMEOUT_SECONDS = float(os.getenv("GIT_TIMEOUT_SECONDS", "300"))

# The only URL prefix the auth header is sent to. Scoping it means a remote
# pointing anywhere else never receives the token.
_GITHUB_URL_PREFIX = "https://github.com/"

_workspace_scrubbed = False


def git_auth_env(token: Optional[str]) -> dict:
    """Environment for a git child process that authenticates to GitHub.

    The token is passed as configuration through GIT_CONFIG_COUNT /
    GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n (git >= 2.31). Unlike
    ``git clone -c http.extraheader=...``, which writes the header into the new
    clone's .git/config, environment configuration is never persisted, and
    unlike any ``-c`` it does not appear in the process argv (``ps``).
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        # Overrides any GIT_CONFIG_COUNT inherited from the server environment;
        # the backend does not rely on one.
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{_GITHUB_URL_PREFIX}.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return env


async def _run_git(
    args: list, cwd: Optional[str], env: dict, timeout: float, quiet: bool = False
) -> Optional[int]:
    """Run git and return its exit code, or None if it timed out (and was killed)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("git %s timed out after %ss", args[0], timeout)
        return None
    if proc.returncode != 0 and not quiet:
        logger.warning(
            "git %s failed (exit %s): %s",
            args[0],
            proc.returncode,
            stderr.decode("utf-8", errors="replace")[-500:].strip(),
        )
    return proc.returncode


async def scrub_clone_credentials(repo_dir: str) -> None:
    """Remove a persisted http.extraheader (the clone token) from .git/config.

    Clones made before credentials moved to the environment carry
    ``AUTHORIZATION: basic <token>`` in plaintext in their config, readable by
    anything with filesystem access to the workspace (including the web
    terminal).
    """
    config = os.path.join(repo_dir, ".git", "config")
    try:
        with open(config, "r", encoding="utf-8", errors="replace") as f:
            if "extraheader" not in f.read().lower():
                return
    except OSError:
        return
    # Exit code 5 ("no such key") is expected for whichever key is absent.
    for key in ("http.extraheader", f"http.{_GITHUB_URL_PREFIX}.extraheader"):
        await _run_git(
            ["config", "--local", "--unset-all", key],
            cwd=repo_dir,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=30,
            quiet=True,
        )


async def scrub_workspace_credentials() -> None:
    """Scrub persisted tokens from every clone in the workspace, once per process.

    This also reaches clones under the previous directory naming scheme, which
    get_safe_repo_dir no longer returns and so would otherwise never be touched.
    """
    global _workspace_scrubbed
    if _workspace_scrubbed:
        return
    _workspace_scrubbed = True
    base = get_base_workspace_dir()
    if not base.is_dir():
        return
    for entry in base.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            await scrub_clone_credentials(str(entry))


async def sync_repo_clone(
    repo_name: str,
    repo_dir: str,
    token: Optional[str],
    *,
    remote_url: Optional[str] = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bool:
    """Clone ``repo_name`` into ``repo_dir``, or bring an existing clone up to date.

    Returns True on success. On failure the reason is logged and an existing
    clone is left as it was, so callers can continue with the cached copy.

    The clone is a server-managed mirror of the default branch, so an existing
    one is reset to the remote's HEAD instead of ``pull --ff-only``. Local edits
    there are either already committed upstream (IDE saves) or must not be
    there at all (unmerged PR code written by older Implementer runs, which
    made every later fast-forward fail and left the clone permanently stale).
    """
    remote = remote_url or f"{_GITHUB_URL_PREFIX}{repo_name}.git"
    env = git_auth_env(token)
    await scrub_workspace_credentials()

    if not os.path.exists(repo_dir):
        rc = await _run_git(
            ["clone", "--quiet", "--", remote, repo_dir], None, env, timeout
        )
        if rc != 0:
            # A killed or failed clone can leave a partial directory behind,
            # which the next run would mistake for a usable clone.
            shutil.rmtree(repo_dir, ignore_errors=True)
            return False
        return True

    await scrub_clone_credentials(repo_dir)
    # Fetch from the repository's current URL, not the clone's "origin": a
    # clone keyed by repository id outlives a rename or transfer, and its
    # origin still names the old location.
    rc = await _run_git(
        ["fetch", "--quiet", "--", remote, "HEAD"], repo_dir, env, timeout
    )
    if rc != 0:
        return False
    # Untracked files (e.g. the .gitnexus index) are deliberately kept.
    rc = await _run_git(
        ["reset", "--hard", "--quiet", "FETCH_HEAD"], repo_dir, env, timeout
    )
    return rc == 0
