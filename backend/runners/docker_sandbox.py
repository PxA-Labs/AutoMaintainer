"""Docker sandbox backend for change verification (epic #411).

Select it with ``SANDBOX_BACKEND=runners.docker_sandbox:create``. The factory
receives the host path of a working copy; every ``exec_command`` then runs in
a fresh, ephemeral container started through the ``docker`` CLI:

- the working copy is copied to a throwaway directory that is bind-mounted at
  ``/workspace`` and deleted afterwards, so nothing the command writes reaches
  the host clone (or a later step that reads it);
- ``--network none`` (``SANDBOX_NETWORK`` names another network, e.g. one
  that only reaches a package mirror), read-only root filesystem with a
  ``tmpfs`` ``/tmp``, ``--cap-drop ALL``, ``no-new-privileges``, a pids,
  memory and CPU limit, a non-root user and an explicit minimal environment
  (nothing from the host);
- optional ``--runtime`` (``SANDBOX_RUNTIME=runsc`` for gVisor);
- a unique container name per run, ``--rm``, and a forced removal on
  timeout, error or cancellation; the wall-clock timeout is enforced here,
  on the host, not trusted to the container;
- output is capped to ``VERIFY_MAX_OUTPUT_BYTES`` (the tail) while reading.

Only the docker CLI is used (argument lists, never a shell), so no Docker SDK
dependency is needed and ``SANDBOX_DOCKER`` can point at a compatible CLI.

Security note: whoever can talk to the Docker daemon is root-equivalent on
the daemon's host. Run this on a dedicated sandbox host, with rootless Docker
and/or gVisor; see docs/VERIFICATION.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .base import CommandTimeoutError, RunnerError
from .local import LocalRunner, _to_argv

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_NETWORK = "none"
DEFAULT_MEMORY = "1g"
DEFAULT_CPUS = "1"
DEFAULT_PIDS_LIMIT = 256
DEFAULT_TMPFS_SIZE = "256m"
# "nobody"; numeric so the image needs no passwd entry. Used when the worker
# runs as root or on Windows; see default_user().
NOBODY = "65534:65534"
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
WORKDIR = "/workspace"
CONTAINER_PREFIX = "automaintainer-sandbox-"
# docker run exits 125 when the daemon or CLI failed (bad flag, missing
# image, unknown runtime): the command never ran, so it is not a test result.
DOCKER_ERROR_EXIT = 125
# Seconds allowed for `docker rm --force` during cleanup.
CLEANUP_TIMEOUT = 30

# The whole environment of the sandboxed command. Nothing is taken from the
# host; PATH comes from the image.
SANDBOX_ENV: Dict[str, str] = {
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "CI": "true",
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # The copy is owned by the worker's uid, not the sandbox user; without
    # this git refuses to work in it ("dubious ownership").
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": WORKDIR,
}

# Variables the docker CLI process itself may inherit (to find the daemon).
# The CLI does not forward its environment into the container, but keeping
# secrets out of it costs nothing.
_CLI_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATHEXT",
    "USERPROFILE",
    "PROGRAMDATA",
    "APPDATA",
    "LOCALAPPDATA",
)

_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SIZE_RE = re.compile(r"[0-9]+[bkmgBKMG]?")
_CPUS_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
_USER_RE = re.compile(r"[0-9]+(:[0-9]+)?")


def default_user() -> str:
    """The worker's own uid:gid when it is not root, else ``nobody``.

    Files the command creates in the bind-mounted copy belong to this uid; a
    non-root worker must own them to delete the copy afterwards. A root
    worker can delete anything, so the sandbox runs as nobody instead.
    """
    if os.name == "posix" and os.getuid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return NOBODY


def _setting(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


def _checked(name: str, value: str, pattern: re.Pattern, hint: str) -> str:
    if not pattern.fullmatch(value):
        raise RunnerError(f"{name}={value!r} is invalid: expected {hint}")
    return value


@dataclass(frozen=True)
class DockerSandboxConfig:
    """Sandbox settings, read from the environment by ``from_env``.

    Every value is validated, and passed as ``--flag=value``, so a setting can
    never smuggle in an extra docker flag.
    """

    image: str = DEFAULT_IMAGE
    network: str = DEFAULT_NETWORK
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    pids_limit: int = DEFAULT_PIDS_LIMIT
    tmpfs_size: str = DEFAULT_TMPFS_SIZE
    user: str = NOBODY
    runtime: Optional[str] = None
    docker: str = "docker"
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> DockerSandboxConfig:
        env = os.environ if env is None else env
        image = _checked(
            "SANDBOX_IMAGE",
            _setting(env, "SANDBOX_IMAGE", DEFAULT_IMAGE),
            _IMAGE_RE,
            "an image reference such as python:3.12-slim or name@sha256:...",
        )
        network = _checked(
            "SANDBOX_NETWORK",
            _setting(env, "SANDBOX_NETWORK", DEFAULT_NETWORK),
            _NAME_RE,
            "'none' or the name of a docker network",
        )
        if network.lower() == "host":
            # The host network namespace would undo the isolation entirely.
            raise RunnerError("SANDBOX_NETWORK=host is not allowed")
        runtime = (env.get("SANDBOX_RUNTIME") or "").strip() or None
        if runtime is not None:
            _checked("SANDBOX_RUNTIME", runtime, _NAME_RE, "a runtime name, e.g. runsc")
        pids = _setting(env, "SANDBOX_PIDS_LIMIT", str(DEFAULT_PIDS_LIMIT))
        if not pids.isdigit() or int(pids) <= 0:
            raise RunnerError(f"SANDBOX_PIDS_LIMIT={pids!r} must be a positive integer")
        try:
            max_output = int(
                env.get("VERIFY_MAX_OUTPUT_BYTES") or DEFAULT_MAX_OUTPUT_BYTES
            )
        except ValueError:
            max_output = DEFAULT_MAX_OUTPUT_BYTES
        return cls(
            image=image,
            network=network,
            memory=_checked(
                "SANDBOX_MEMORY",
                _setting(env, "SANDBOX_MEMORY", DEFAULT_MEMORY),
                _SIZE_RE,
                "a size such as 1g or 512m",
            ),
            cpus=_checked(
                "SANDBOX_CPUS",
                _setting(env, "SANDBOX_CPUS", DEFAULT_CPUS),
                _CPUS_RE,
                "a number such as 1 or 1.5",
            ),
            pids_limit=int(pids),
            tmpfs_size=_checked(
                "SANDBOX_TMPFS_SIZE",
                _setting(env, "SANDBOX_TMPFS_SIZE", DEFAULT_TMPFS_SIZE),
                _SIZE_RE,
                "a size such as 256m",
            ),
            user=_checked(
                "SANDBOX_USER",
                _setting(env, "SANDBOX_USER", default_user()),
                _USER_RE,
                "a numeric uid[:gid] (never 0)",
            ),
            runtime=runtime,
            docker=_setting(env, "SANDBOX_DOCKER", "docker"),
            # Same floor as verification.py.
            max_output_bytes=max(1024, max_output),
        )

    def __post_init__(self) -> None:
        if self.user.split(":")[0] == "0":
            raise RunnerError("SANDBOX_USER must not be root (uid 0)")


def docker_run_argv(
    config: DockerSandboxConfig,
    name: str,
    workspace: Union[str, Path],
    command: Sequence[str],
) -> List[str]:
    """The exact ``docker run`` argument list for one sandboxed command."""
    argv = [
        config.docker,
        "run",
        "--rm",
        f"--name={name}",
        f"--network={config.network}",
        "--read-only",
        f"--tmpfs=/tmp:rw,nosuid,nodev,size={config.tmpfs_size}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={config.pids_limit}",
        f"--memory={config.memory}",
        # Equal to --memory: no swap on top of the limit.
        f"--memory-swap={config.memory}",
        f"--cpus={config.cpus}",
        f"--user={config.user}",
        f"--workdir={WORKDIR}",
        f"--mount=type=bind,source={workspace},target={WORKDIR}",
    ]
    if config.runtime:
        argv.append(f"--runtime={config.runtime}")
    argv += [f"--env={k}={v}" for k, v in SANDBOX_ENV.items()]
    # Options end at the image; everything after it is the command, verbatim.
    argv.append(config.image)
    argv += list(command)
    return argv


def _cli_env(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if source is None else source
    return {k: source[k] for k in _CLI_ENV_ALLOWLIST if k in source}


def _copy_workspace(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` and make the copy writable by the sandbox user.

    Symlinks are copied as links, never followed: following one could copy a
    host file (``link -> /etc/shadow``) into the sandbox. Inside the
    container such a link points into the container's own filesystem.
    """
    shutil.copytree(src, dest, symlinks=True)
    # SANDBOX_USER may differ from the worker's uid; the copy is throwaway and
    # lives in a private (0700) temp dir, so making it writable is harmless.
    for root, _dirs, files in os.walk(dest):
        os.chmod(root, os.stat(root).st_mode | 0o777)
        for f in files:
            path = os.path.join(root, f)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            os.chmod(path, mode | 0o666 | (0o111 if mode & stat.S_IXUSR else 0))


class DockerSandbox:
    """``runners.sandbox.SandboxBackend`` running commands in ephemeral
    Docker containers against a throwaway copy of ``repo_path``.

    File operations act on the host working copy and refuse paths that
    escape it (delegated to ``LocalRunner``'s checks).
    """

    def __init__(
        self, repo_path: Union[str, Path], config: Optional[DockerSandboxConfig] = None
    ):
        self.repo_path = Path(repo_path).resolve()
        self.config = config or DockerSandboxConfig.from_env()
        self._files = LocalRunner(self.repo_path)

    # --- file operations (host working copy, confined) -----------------------

    async def read_file(self, rel_path: str) -> str:
        return await self._files.read_file(rel_path)

    async def write_file(self, rel_path: str, content: str) -> None:
        await self._files.write_file(rel_path, content)

    async def delete_file(self, rel_path: str) -> None:
        await self._files.delete_file(rel_path)

    async def list_files(self, pattern: Optional[str] = None) -> List[str]:
        return await self._files.list_files(pattern)

    async def get_git_diff(self) -> str:
        """``git diff HEAD`` of the host working copy.

        Runs git on the host, but never repository code: the sandboxed
        commands only ever see a copy, so they cannot plant git config
        (``diff.external``, ``core.fsmonitor``) in this clone, and external
        diff drivers and textconv are disabled anyway.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            cwd=str(self.repo_path),
            env={**_cli_env(), "GIT_TERMINAL_PROMPT": "0"},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RunnerError(
                "git diff failed: " + err.decode("utf-8", errors="replace").strip()
            )
        return out.decode("utf-8", errors="replace")

    async def commit_and_push(self, branch: str, message: str, dry_run: bool) -> str:
        raise NotImplementedError(
            "DockerSandbox is a verification backend: it runs commands in "
            "isolation and never commits or pushes. Use the workspace/GitHub "
            "integration to publish changes."
        )

    # --- command execution ----------------------------------------------------

    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]:
        """Run ``cmd`` in a new container; returns (exit_code, output, note).

        stdout and stderr are interleaved in ``output`` and cut to the last
        ``max_output_bytes``; ``note`` is empty, or a truncation notice. Raises
        CommandTimeoutError after ``timeout`` seconds (the container is
        force-removed) and RunnerError when docker itself fails.
        """
        argv = _to_argv(cmd)
        if not timeout or timeout <= 0:
            timeout = 60
        docker = shutil.which(self.config.docker) or self.config.docker
        name = CONTAINER_PREFIX + uuid.uuid4().hex
        tmp = tempfile.mkdtemp(prefix="automaintainer-sandbox-")
        try:
            workspace = Path(tmp) / "workspace"
            try:
                await asyncio.to_thread(_copy_workspace, self.repo_path, workspace)
            except OSError as e:
                raise RunnerError(f"could not copy the working copy: {e}") from e
            full = docker_run_argv(self.config, name, workspace, argv)
            full[0] = docker
            return await self._run_container(full, name, timeout)
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, True)

    async def _run_container(
        self, argv: List[str], name: str, timeout: float
    ) -> Tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=_cli_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            raise RunnerError(f"cannot start the docker CLI {argv[0]!r}: {e}") from e

        limit = self.config.max_output_bytes
        tail = bytearray()
        total = 0

        async def pump() -> None:
            nonlocal total
            while chunk := await proc.stdout.read(65536):
                # Keep draining past the cap so the container never blocks on
                # a full pipe, but only remember the last `limit` bytes.
                total += len(chunk)
                tail.extend(chunk)
                if len(tail) > limit:
                    del tail[: len(tail) - limit]
            await proc.wait()

        finished = False
        try:
            await asyncio.wait_for(pump(), timeout)
            finished = True
        except asyncio.TimeoutError:
            raise CommandTimeoutError(
                f"sandboxed command did not finish within {timeout:g} seconds"
            ) from None
        finally:
            if not finished:
                # Timeout, error or cancellation: --rm only fires once the
                # container stops, so kill and remove it explicitly.
                await self._remove_container(name)
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), CLEANUP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning("docker CLI %s did not exit", proc.pid)

        output = bytes(tail).decode("utf-8", errors="replace")
        if proc.returncode == DOCKER_ERROR_EXIT:
            raise RunnerError(f"docker could not run the sandbox: {output[-500:]}")
        note = ""
        if total > limit:
            note = (
                f"\n[sandbox output truncated: showing the last {limit} "
                f"of {total} bytes]\n"
            )
        return proc.returncode, output, note

    async def _remove_container(self, name: str) -> None:
        """``docker rm --force``: kills the container if running, removes it."""
        docker = shutil.which(self.config.docker) or self.config.docker
        try:
            proc = await asyncio.create_subprocess_exec(
                docker,
                "rm",
                "--force",
                name,
                env=_cli_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), CLEANUP_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("could not remove sandbox container %s: %s", name, e)


def create(repo_path: Union[str, Path]) -> DockerSandbox:
    """``SANDBOX_BACKEND`` factory: ``runners.docker_sandbox:create``."""
    return DockerSandbox(repo_path)
