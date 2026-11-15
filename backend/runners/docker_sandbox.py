"""Docker sandbox backend for change verification (epic #411).

Select it with ``SANDBOX_BACKEND=runners.docker_sandbox:create``. The factory
receives the host path of a working copy; every ``exec_command`` then runs in
a fresh, ephemeral container started through the ``docker`` CLI:

- the working copy is streamed into the container as a tar archive on stdin
  and unpacked into a ``tmpfs`` at ``/workspace``. Nothing is bind-mounted,
  so this works with any daemon the CLI can reach (the local socket, a
  remote ``DOCKER_HOST``, or a daemon outside the worker's own container),
  and nothing the command writes reaches the host clone (or a later step
  that reads it);
- ``--network none`` (``SANDBOX_NETWORK`` names another network, e.g. one
  that only reaches a package mirror), read-only root filesystem with a
  ``tmpfs`` ``/tmp``, ``--cap-drop ALL``, ``no-new-privileges``, a pids,
  memory and CPU limit, a non-root user and an explicit minimal environment
  (nothing from the host);
- optional ``--runtime`` (``SANDBOX_RUNTIME=runsc`` for gVisor);
- a unique container name per run, ``--rm``, and on timeout, error or
  cancellation the CLI is killed first and the container then
  force-removed; the wall-clock timeout (which also covers packing the copy)
  is enforced here, on the host, not trusted to the container;
- output is capped to ``VERIFY_MAX_OUTPUT_BYTES`` (the tail) while reading.

Only the docker CLI is used (argument lists, never a shell on the host), so
no Docker SDK dependency is needed and ``SANDBOX_DOCKER`` can point at a
compatible CLI. The image must provide ``/bin/sh`` and ``tar``.

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
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .base import CommandTimeoutError, RunnerError
from .local import LocalRunner, _to_argv

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_NETWORK = "none"
DEFAULT_MEMORY = "1g"
DEFAULT_CPUS = "1"
DEFAULT_PIDS_LIMIT = 256
DEFAULT_TMPFS_SIZE = "256m"
DEFAULT_WORKSPACE_SIZE = "1g"
# "nobody"; numeric so the image needs no passwd entry.
NOBODY = "65534:65534"
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
WORKDIR = "/workspace"
CONTAINER_PREFIX = "automaintainer-sandbox-"
# docker run exits 125 when the daemon or CLI failed (bad flag, missing
# image, unknown runtime): the command never ran, so it is not a test result.
# SETUP_SCRIPT uses it too when unpacking the copy fails.
DOCKER_ERROR_EXIT = 125
# Seconds allowed for each cleanup step (CLI exit, `docker rm --force`).
CLEANUP_TIMEOUT = 30
# Pause before the single retry of `docker rm --force`.
REMOVE_RETRY_DELAY = 1.0
# The archive of the working copy stays in memory up to this size, then
# spills to an anonymous temporary file (gone once closed or collected).
ARCHIVE_SPOOL_BYTES = 8 * 1024 * 1024
# Runs as the container's entrypoint (/bin/sh -c): unpack the archive from
# stdin into the tmpfs workspace, then replace itself with the test command,
# which arrives as positional arguments (never interpolated into the script).
SETUP_SCRIPT = (
    f'tar -xf - -C {WORKDIR} || exit {DOCKER_ERROR_EXIT}; exec "$@" </dev/null'
)
SETUP_ARG0 = "automaintainer-sandbox"

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
    # /workspace (the tmpfs root) is owned by root, not the sandbox user;
    # without this git refuses to work in it ("dubious ownership").
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": WORKDIR,
}

# Variables the docker CLI process itself may inherit (to find the daemon).
# The CLI does not forward its environment into the container, but keeping
# secrets out of it costs nothing. SSH_AUTH_SOCK (and USER, HOME for ~/.ssh)
# serve DOCKER_HOST=ssh://...; the proxy variables a daemon behind a proxy.
_CLI_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
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


def _setting(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


def _checked(name: str, value: str, pattern: re.Pattern, hint: str) -> str:
    if not pattern.fullmatch(value):
        raise RunnerError(f"{name}={value!r} is invalid: expected {hint}")
    return value


def _positive(name: str, value: str, pattern: re.Pattern, hint: str) -> str:
    """``_checked``, and zero is refused too: docker reads ``--memory=0`` or
    ``--cpus=0`` as "no limit"."""
    _checked(name, value, pattern, hint)
    if float(value.rstrip("bkmgBKMG")) <= 0:
        raise RunnerError(f"{name}={value!r} must be greater than zero")
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
    workspace_size: str = DEFAULT_WORKSPACE_SIZE
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
            memory=_positive(
                "SANDBOX_MEMORY",
                _setting(env, "SANDBOX_MEMORY", DEFAULT_MEMORY),
                _SIZE_RE,
                "a size such as 1g or 512m",
            ),
            cpus=_positive(
                "SANDBOX_CPUS",
                _setting(env, "SANDBOX_CPUS", DEFAULT_CPUS),
                _CPUS_RE,
                "a number such as 1 or 1.5",
            ),
            pids_limit=int(pids),
            tmpfs_size=_positive(
                "SANDBOX_TMPFS_SIZE",
                _setting(env, "SANDBOX_TMPFS_SIZE", DEFAULT_TMPFS_SIZE),
                _SIZE_RE,
                "a size such as 256m",
            ),
            workspace_size=_positive(
                "SANDBOX_WORKSPACE_SIZE",
                _setting(env, "SANDBOX_WORKSPACE_SIZE", DEFAULT_WORKSPACE_SIZE),
                _SIZE_RE,
                "a size such as 1g",
            ),
            user=_checked(
                "SANDBOX_USER",
                _setting(env, "SANDBOX_USER", NOBODY),
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
    command: Sequence[str],
) -> List[str]:
    """The exact ``docker run`` argument list for one sandboxed command.

    The working copy arrives as a tar archive on stdin (``--interactive``);
    ``SETUP_SCRIPT`` unpacks it into the ``/workspace`` tmpfs and execs
    ``command``. No host path appears in the argv, so the daemon does not
    need to see the worker's filesystem.
    """
    argv = [
        config.docker,
        "run",
        "--rm",
        "--interactive",
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
        # tmpfs pages count against --memory.
        f"--tmpfs={WORKDIR}:rw,exec,nosuid,nodev,mode=1777,"
        f"size={config.workspace_size}",
        # A fixed entrypoint, whatever the image declares.
        "--entrypoint=/bin/sh",
    ]
    if config.runtime:
        argv.append(f"--runtime={config.runtime}")
    argv += [f"--env={k}={v}" for k, v in SANDBOX_ENV.items()]
    # Options end at the image; what follows goes to /bin/sh, and the command
    # reaches the script as "$@", verbatim.
    argv += [config.image, "-c", SETUP_SCRIPT, SETUP_ARG0]
    argv += list(command)
    return argv


def _cli_env(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if source is None else source
    return {k: source[k] for k in _CLI_ENV_ALLOWLIST if k in source}


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    # Host account names and ids mean nothing in the container.
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _archive_workspace(src: Path) -> IO[bytes]:
    """Pack ``src`` into a tar archive, rewound and ready to stream.

    Symlinks are stored as links, never followed: following one could pull a
    host file (``link -> /etc/shadow``) into the sandbox. Inside the
    container such a link points into the container's own filesystem. Only
    the children of ``src`` are archived, not ``src`` itself, so unpacking
    never tries to change the mode of the root-owned ``/workspace``.
    """
    archive = tempfile.SpooledTemporaryFile(max_size=ARCHIVE_SPOOL_BYTES)
    try:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for entry in sorted(os.listdir(src)):
                tar.add(src / entry, arcname=entry, filter=_anonymous)
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise


class DockerSandbox:
    """``runners.sandbox.SandboxBackend`` running commands in ephemeral
    Docker containers, each with its own copy of ``repo_path``.

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
        CommandTimeoutError after ``timeout`` seconds, counted from the start
        of packing the copy (the container is force-removed), and RunnerError
        when docker itself, or unpacking the copy, fails (exit 125).
        """
        argv = _to_argv(cmd)
        if not timeout or timeout <= 0:
            timeout = 60
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            # On timeout or cancellation the packing thread runs on, but it
            # only writes to an anonymous spool file that goes away with it.
            archive = await asyncio.wait_for(
                asyncio.to_thread(_archive_workspace, self.repo_path), timeout
            )
        except asyncio.TimeoutError:
            raise CommandTimeoutError(
                f"could not pack the working copy within {timeout:g} seconds"
            ) from None
        except (OSError, tarfile.TarError) as e:
            raise RunnerError(f"could not pack the working copy: {e}") from e
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CommandTimeoutError(
                    f"could not pack the working copy within {timeout:g} seconds"
                )
            docker = shutil.which(self.config.docker) or self.config.docker
            name = CONTAINER_PREFIX + uuid.uuid4().hex
            full = docker_run_argv(self.config, name, argv)
            full[0] = docker
            return await self._run_container(full, name, archive, remaining, timeout)
        finally:
            archive.close()

    async def _run_container(
        self,
        argv: List[str],
        name: str,
        archive: IO[bytes],
        remaining: float,
        timeout: float,
    ) -> Tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=_cli_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            raise RunnerError(f"cannot start the docker CLI {argv[0]!r}: {e}") from e

        limit = self.config.max_output_bytes
        tail = bytearray()
        total = 0

        async def feed() -> None:
            try:
                while chunk := archive.read(65536):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except OSError:
                # BrokenPipeError/ConnectionResetError: the container exited
                # before reading everything (docker or tar failed); its exit
                # code says what happened.
                pass

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

        feeder = asyncio.ensure_future(feed())
        finished = False
        try:
            await asyncio.wait_for(pump(), remaining)
            finished = True
        except asyncio.TimeoutError:
            raise CommandTimeoutError(
                f"sandboxed command did not finish within {timeout:g} seconds"
            ) from None
        finally:
            if not finished:
                # Timeout, error or cancellation. --rm only fires once the
                # container stops, so remove it explicitly. Kill the CLI
                # first: while it lives it may still pull the image or
                # create and start the container, which a removal issued
                # before would miss.
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), CLEANUP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning("docker CLI %s did not exit", proc.pid)
                await self._remove_container(name)
            if finished:
                # The CLI exited, so the pipe is closed and the feeder ends
                # promptly (done, or a broken pipe); the wait is a backstop.
                await asyncio.wait({feeder}, timeout=CLEANUP_TIMEOUT)
            # Never leave it running against the archive, which the caller
            # closes next.
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

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
        """``docker rm --force``: kills the container if running, removes it.

        Retried once on failure: "no such container" usually means the CLI
        died before creating it, but a create request it already sent may
        still complete in the daemon just after the first attempt.
        """
        docker = shutil.which(self.config.docker) or self.config.docker
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(REMOVE_RETRY_DELAY)
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
                if await asyncio.wait_for(proc.wait(), CLEANUP_TIMEOUT) == 0:
                    return
            except (OSError, asyncio.TimeoutError) as e:
                logger.warning("could not remove sandbox container %s: %s", name, e)
        logger.info("sandbox container %s was not found or not removed", name)


def create(repo_path: Union[str, Path]) -> DockerSandbox:
    """``SANDBOX_BACKEND`` factory: ``runners.docker_sandbox:create``."""
    return DockerSandbox(repo_path)
