import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

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
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name == IDE_VIEWS_DIR:
            # IDE views are worktrees; their config is the clone's.
            continue
        if entry.name == REPO_ID_DIR:
            # Id-keyed clones never persisted a token, but scrub them too so
            # this stays true for every clone the workspace holds.
            for clone in entry.iterdir():
                if clone.is_dir() and not clone.is_symlink():
                    await scrub_clone_credentials(str(clone))
        else:
            await scrub_clone_credentials(str(entry))


def _without_userinfo(url: str) -> str:
    """``url`` with any ``user:password@`` removed (http(s) URLs only)."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit(parts._replace(netloc=host))
    return url


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
    there must not be there at all (unmerged PR code written by older
    Implementer runs, which made every later fast-forward fail and left the
    clone permanently stale; or IDE saves mirrored by releases before #505).
    The IDE never writes into the clone: it reads per-user views (worktrees
    of this clone, see refresh_ide_view) and commits through GitHub.
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
    # A clone keyed by repository id outlives a rename or transfer, so its
    # "origin" may still name the old owner/name. Point origin at the current
    # URL before fetching: runners push to "origin", and if someone recreates
    # a repository at the old name, a stale origin would send this
    # repository's branch there. Never written with credentials in it.
    origin = _without_userinfo(remote)
    rc = await _run_git(
        ["remote", "set-url", "origin", origin], repo_dir, env, timeout, quiet=True
    )
    if rc != 0:
        rc = await _run_git(["remote", "add", "origin", origin], repo_dir, env, timeout)
        if rc != 0:
            return False
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


# --- Per-user IDE views (#505) ----------------------------------------------
#
# IDE writes commit to the caller's working branch (automaintainer/ide-<key>,
# see main._ide_working_branch), so the IDE must also *read* that branch.
# Reading the shared clone showed one user's mirrored edits and created files
# to every other user of the repository. Each reader therefore gets a "view":
# a detached git worktree of the shared clone, kept outside the clone at
#
#     <workspace>/_ide_views/<clone dir relative to workspace>/<key>
#
# and checked out at the user's working branch, or the "_default" view (the
# default branch) for users without one. Worktrees share the clone's object
# store, so a view costs one checkout, not one clone. Only git writes into a
# view: IDE saves go to GitHub and the view is then refreshed from there, so
# a view never holds untracked files and is never shared between users.
#
# Fetched commits are pinned at refs/automaintainer-ide/<key>/{base,head} in
# the shared clone ("base" is the default branch, fetched together with
# "head"). The refs keep the commits alive across a gc and give the branch
# diff that restores the IDE's staged list.
#
# Views are disposable caches: views unused for IDE_VIEW_TTL_HOURS are
# removed, and beyond IDE_VIEW_MAX views the least recently used go first.

IDE_VIEWS_DIR = "_ide_views"
DEFAULT_VIEW = "_default"
IDE_VIEW_REF_NAMESPACE = "refs/automaintainer-ide"
_VIEW_KEY_RE = re.compile(r"[0-9a-f]{12}")
# A process sweeps for expired views at most this often (seconds).
_PRUNE_INTERVAL_SECONDS = 60.0
# How long "this user has no working branch" is trusted by reads that do not
# ask GitHub (file, search). The tree, the changes route and every write ask.
NO_BRANCH_CACHE_SECONDS = 300.0

_locks_guard = threading.Lock()
_clone_locks: dict = {}
_no_branch_seen: dict = {}
_last_prune = 0.0


def ide_user_key(org_id: str, user_id: str) -> str:
    """12 hex chars identifying one user of one org (the working-branch suffix).

    Hashed so Supabase ids never appear in branch names or paths.
    """
    return hashlib.sha256(f"{org_id}:{user_id}".encode("utf-8")).hexdigest()[:12]


def github_remote_url(repo_name: str) -> str:
    """The URL views are fetched from. Tests point this at a local repository."""
    return f"{_GITHUB_URL_PREFIX}{repo_name}.git"


def _env_number(name: str, default, cast):
    """``cast(os.environ[name])``, or ``default`` (with a warning) when it is
    unset or malformed: a typo must not turn every IDE read into a 500."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def ide_view_ttl_seconds() -> float:
    return _env_number("IDE_VIEW_TTL_HOURS", 24.0, float) * 3600


def ide_view_max() -> int:
    return _env_number("IDE_VIEW_MAX", 50, int)


def _views_root() -> Path:
    return get_base_workspace_dir() / IDE_VIEWS_DIR


def is_git_clone(clone_dir: Path) -> bool:
    """True for a real clone. A directory without one is served as it is."""
    return (Path(clone_dir) / ".git" / "HEAD").is_file()


def ide_view_dir(clone_dir: Path, key: str) -> Path:
    """Where ``key``'s view of ``clone_dir`` lives.

    ``key`` becomes a path component, so only a user key (12 hex) or
    DEFAULT_VIEW is accepted.
    """
    if key != DEFAULT_VIEW and not _VIEW_KEY_RE.fullmatch(key or ""):
        raise ValueError("invalid IDE view key")
    rel = Path(os.path.abspath(str(clone_dir))).relative_to(get_base_workspace_dir())
    if not rel.parts or rel.parts[0] == IDE_VIEWS_DIR:
        raise ValueError("not a repository clone")
    return _views_root() / rel / key


def _clone_of_view(view: Path) -> Path:
    return get_base_workspace_dir() / view.parent.relative_to(_views_root())


def _view_ref(key: str, which: str) -> str:
    return f"{IDE_VIEW_REF_NAMESPACE}/{key}/{which}"


def _clone_lock(clone_dir: Path) -> threading.Lock:
    """Serialises view changes in one clone (git keeps worktree metadata in the
    clone's .git/worktrees). Per process only: a second API process racing on
    the same view gets a git error and falls back like any failed refresh."""
    key = os.path.abspath(str(clone_dir))
    with _locks_guard:
        return _clone_locks.setdefault(key, threading.Lock())


# Command-line config wins over the clone's .git/config. Anything that can
# write the shared clone's .git (the web terminal, a LocalRunner running repo
# code) must not gain code execution in the API process through the view
# operations: no hooks (worktree add runs post-checkout, fetch and update-ref
# run reference-transaction) and no fsmonitor command.
_SAFE_GIT_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")


def _git(args: list, cwd: Path, env: Optional[dict] = None, timeout: float = 60):
    """Run git synchronously and return (exit code, stdout).

    A git failure is not an exception; a timeout or a missing git binary is
    exit code None. Hooks and fsmonitor are disabled (_SAFE_GIT_CONFIG).
    """
    try:
        proc = subprocess.run(
            ["git", *_SAFE_GIT_CONFIG, *args],
            cwd=str(cwd),
            env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("git %s did not complete: %s", args[0], e)
        return None, ""
    if proc.returncode != 0:
        logger.info(
            "git %s exited %s: %s",
            args[0],
            proc.returncode,
            proc.stderr.decode("utf-8", errors="replace")[-300:].strip(),
        )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def _rev(cwd: Path, rev: str) -> Optional[str]:
    rc, out = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd)
    return out.strip() if rc == 0 and out.strip() else None


def _view_is_valid(view: Path) -> bool:
    return (view / ".git").is_file() and _rev(view, "HEAD") is not None


def _touch(view: Path) -> None:
    """Record a use; the directory's mtime orders views for eviction."""
    try:
        os.utime(view)
    except OSError:
        pass


def _remove_view_locked(clone_dir: Path, view: Path, key: str) -> None:
    if view.exists():
        rc, _ = _git(["worktree", "remove", "--force", str(view)], clone_dir)
        if rc != 0:
            shutil.rmtree(view, ignore_errors=True)
    _git(["worktree", "prune"], clone_dir)
    for which in ("head", "base"):
        _git(["update-ref", "-d", _view_ref(key, which)], clone_dir)


def _checkout_view_locked(clone_dir: Path, view: Path, ref: str) -> bool:
    """Point ``view`` at ``ref`` (a ref of the clone), creating it if needed."""
    if _view_is_valid(view):
        rc, _ = _git(["reset", "--hard", "--quiet", ref], view)
        if rc == 0:
            # Defense in depth: nothing is supposed to create files in a view.
            _git(["clean", "-ffdxq"], view)
            _touch(view)
            return True
    shutil.rmtree(view, ignore_errors=True)
    _git(["worktree", "prune"], clone_dir)
    view.parent.mkdir(parents=True, exist_ok=True)
    rc, _ = _git(
        ["worktree", "add", "--detach", "--force", "--quiet", str(view), ref],
        clone_dir,
    )
    if rc != 0:
        shutil.rmtree(view, ignore_errors=True)
        return False
    _touch(view)
    return True


def _ls_remote(clone_dir: Path, remote: str, branch: str, env: dict, timeout: float):
    """(default branch sha, working branch sha or None); None on failure."""
    rc, out = _git(
        ["ls-remote", "--", remote, "HEAD", f"refs/heads/{branch}"],
        clone_dir,
        env,
        timeout,
    )
    if rc != 0:
        return None
    shas = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        shas[name.strip()] = sha.strip()
    if "HEAD" not in shas:
        return None
    return shas["HEAD"], shas.get(f"refs/heads/{branch}")


def _fetch(clone_dir: Path, remote: str, refspecs: list, env: dict, timeout: float):
    # --no-write-fetch-head: the shared clone's FETCH_HEAD belongs to
    # sync_repo_clone (fetch HEAD, then reset --hard FETCH_HEAD), which does
    # not hold the clone lock; a view fetch must not change what it resets to.
    rc, _ = _git(
        ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--"]
        + [remote, *refspecs],
        clone_dir,
        env,
        timeout,
    )
    return rc == 0


def _offline_view_locked(clone_dir: Path, key: str) -> Optional[Path]:
    """The best view without GitHub: the user's branch as last fetched, else
    the default branch as last fetched (else as the clone has it)."""
    view = ide_view_dir(clone_dir, key)
    if _view_is_valid(view):
        _touch(view)
        return view
    head = _view_ref(key, "head")
    if _rev(clone_dir, head) and _checkout_view_locked(clone_dir, view, head):
        return view
    default = ide_view_dir(clone_dir, DEFAULT_VIEW)
    if _view_is_valid(default):
        _touch(default)
        return default
    ref = _view_ref(DEFAULT_VIEW, "base")
    if not _rev(clone_dir, ref):
        ref = "HEAD"
    return default if _checkout_view_locked(clone_dir, default, ref) else None


def _note_no_branch(clone_dir: Path, key: str) -> None:
    """Remember that ``key`` has no working branch; expired entries are
    dropped here, so the cache stays bounded by recent readers."""
    now = time.monotonic()
    with _locks_guard:
        for k in [
            k for k, t in _no_branch_seen.items() if now - t >= NO_BRANCH_CACHE_SECONDS
        ]:
            del _no_branch_seen[k]
        _no_branch_seen[(str(clone_dir), key)] = now


def _refresh_locked(clone_dir, key, branch, remote, env, timeout, remote_state):
    if remote_state is None:
        return _offline_view_locked(clone_dir, key)
    default_sha, branch_sha = remote_state

    if branch_sha:
        _no_branch_seen.pop((str(clone_dir), key), None)
        view = ide_view_dir(clone_dir, key)
        head, base = _view_ref(key, "head"), _view_ref(key, "base")
        fetched = (
            _rev(clone_dir, head) == branch_sha and _rev(clone_dir, base) == default_sha
        )
        if fetched and _view_is_valid(view) and _rev(view, "HEAD") == branch_sha:
            _touch(view)
            return view
        if not fetched and not _fetch(
            clone_dir,
            remote,
            [f"+HEAD:{base}", f"+refs/heads/{branch}:{head}"],
            env,
            timeout,
        ):
            return _offline_view_locked(clone_dir, key)
        return view if _checkout_view_locked(clone_dir, view, head) else None

    # No working branch (never created, or deleted once its pull request
    # merged): drop any stale view of it and serve the default branch.
    _note_no_branch(clone_dir, key)
    stale = ide_view_dir(clone_dir, key)
    if stale.exists() or _rev(clone_dir, _view_ref(key, "head")):
        _remove_view_locked(clone_dir, stale, key)
    view = ide_view_dir(clone_dir, DEFAULT_VIEW)
    base = _view_ref(DEFAULT_VIEW, "base")
    if _rev(clone_dir, base) != default_sha and not _fetch(
        clone_dir, remote, [f"+HEAD:{base}"], env, timeout
    ):
        return _offline_view_locked(clone_dir, key)
    if _view_is_valid(view) and _rev(view, "HEAD") == default_sha:
        _touch(view)
        return view
    return view if _checkout_view_locked(clone_dir, view, base) else None


def refresh_ide_view(
    clone_dir: Path,
    key: str,
    branch: str,
    token: Optional[str],
    repo_name: str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Optional[Path]:
    """Bring ``key``'s view of ``clone_dir`` up to date with GitHub; return it.

    The view is the user's working ``branch`` when it exists on GitHub, else
    the shared default-branch view. Returns None when ``clone_dir`` is not a
    git clone (callers serve that directory as before) or no view could be
    checked out. When GitHub cannot be reached, the last fetched state is
    served. One ``ls-remote`` per call; a fetch and checkout only when
    something changed.

    Blocking (git subprocesses); call from a worker thread.
    """
    clone_dir = Path(clone_dir)
    if not is_git_clone(clone_dir):
        return None
    ide_view_dir(clone_dir, key)  # validates key and clone_dir
    remote = github_remote_url(repo_name)
    env = git_auth_env(token)
    remote_state = _ls_remote(clone_dir, remote, branch, env, timeout)
    with _clone_lock(clone_dir):
        view = _refresh_locked(
            clone_dir, key, branch, remote, env, timeout, remote_state
        )
    prune_ide_views(keep={view} if view else ())
    return view


def current_ide_view(clone_dir: Path, key: str) -> Optional[Path]:
    """The view to read without asking GitHub, or None when a refresh is needed.

    The user's own view when it exists. The default view only while a recent
    refresh found no working branch for this user, so a user whose view was
    pruned is never shown the default branch in place of their own edits.
    """
    clone_dir = Path(clone_dir)
    if not is_git_clone(clone_dir):
        return None
    view = ide_view_dir(clone_dir, key)
    if _view_is_valid(view):
        _touch(view)
        return view
    seen = _no_branch_seen.get((str(clone_dir), key))
    if seen is not None and time.monotonic() - seen < NO_BRANCH_CACHE_SECONDS:
        default = ide_view_dir(clone_dir, DEFAULT_VIEW)
        if _view_is_valid(default):
            _touch(default)
            return default
    return None


_STATUS_NAMES = {"A": "created", "M": "modified", "T": "modified", "D": "deleted"}


def ide_branch_changes(clone_dir: Path, key: str) -> list:
    """Files the user's working branch changes relative to the default branch.

    A file is listed when the branch changed it since the merge base
    (``base...head``), so commits that reached the default branch after the
    branch was last synced are not the user's, AND it still differs from the
    default branch (``base head``), so a change that already landed there
    (its pull request merged) drops out. Uses the refs pinned by the last
    refresh_ide_view.

    Blocking (git subprocess); call from a worker thread.
    """
    clone_dir = Path(clone_dir)
    if not is_git_clone(clone_dir):
        return []
    head, base = _view_ref(key, "head"), _view_ref(key, "base")
    if not (_rev(clone_dir, head) and _rev(clone_dir, base)):
        return []
    since_fork = _name_status(clone_dir, f"{base}...{head}")
    differs_now = _name_status(clone_dir, base, head)
    if since_fork is None or differs_now is None:
        return []
    return [
        {"path": path, "status": status}
        for path, status in since_fork.items()
        if path in differs_now
    ]


def _name_status(clone_dir: Path, *revs: str) -> Optional[dict]:
    rc, out = _git(
        ["diff", "--name-status", "--no-renames", "-z", *revs, "--"], clone_dir
    )
    if rc != 0:
        return None
    fields = out.split("\0")
    changes = {}
    for i in range(0, len(fields) - 1, 2):
        status, path = fields[i], fields[i + 1]
        if status[:1] in _STATUS_NAMES and path:
            changes[path] = _STATUS_NAMES[status[:1]]
    return changes


def _all_views() -> list:
    root = _views_root()
    if not root.is_dir():
        return []
    parents = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name == REPO_ID_DIR:
            parents.extend(
                c for c in entry.iterdir() if c.is_dir() and not c.is_symlink()
            )
        else:
            parents.append(entry)
    views = []
    for parent in parents:
        for view in parent.iterdir():
            if view.is_symlink() or not view.is_dir():
                continue
            if view.name == DEFAULT_VIEW or _VIEW_KEY_RE.fullmatch(view.name):
                views.append(view)
    return views


def prune_ide_views(keep=(), *, now: Optional[float] = None, force: bool = False):
    """Remove views unused for the TTL, then the least recently used beyond
    IDE_VIEW_MAX. A view in ``keep`` (the one being served) is never removed.
    Runs at most every _PRUNE_INTERVAL_SECONDS per process unless ``force``.

    Returns the removed views. Blocking; call from a worker thread.
    """
    global _last_prune
    if not force and time.monotonic() - _last_prune < _PRUNE_INTERVAL_SECONDS:
        return []
    _last_prune = time.monotonic()
    now = time.time() if now is None else now
    keep = {os.path.abspath(str(k)) for k in keep}

    def last_used(view: Path) -> float:
        try:
            return view.stat().st_mtime
        except OSError:
            return 0.0

    ttl, cap = ide_view_ttl_seconds(), max(ide_view_max(), 1)
    doomed, kept = [], 0
    for view in sorted(_all_views(), key=last_used, reverse=True):
        if os.path.abspath(str(view)) in keep:
            kept += 1
        elif now - last_used(view) > ttl or kept >= cap:
            doomed.append(view)
        else:
            kept += 1
    for view in doomed:
        clone = _clone_of_view(view)
        with _clone_lock(clone):
            if is_git_clone(clone):
                _remove_view_locked(clone, view, view.name)
            else:
                shutil.rmtree(view, ignore_errors=True)
    return doomed
