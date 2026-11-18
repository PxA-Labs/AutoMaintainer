"""Cloud Run Jobs sandbox backend for change verification (#158, #527).

Select it with ``SANDBOX_BACKEND=cloud_run_job`` (or the full
``automaintainer.runners.cloud_run_job_sandbox:create``). It has the same
contract as ``docker_sandbox``: the factory receives the host path of a
working copy, file operations act on that copy (refusing paths that escape
it), and every ``exec_command`` runs in a fresh, ephemeral environment, here
one execution of a Cloud Run job that ``infra/terraform/sandbox.tf`` creates:

1. the working copy is packed into a tar archive on the worker (symlinks kept
   as links, never followed) and uploaded to the sandbox bucket under
   ``executions/<128 random bits>/``;
2. the worker signs two V4 URLs, valid only for the run's duration: a GET of
   that archive and a PUT of the result object, whose size is capped by a
   signed ``x-goog-content-length-range`` header. The job's own service
   account has no role at all, so these URLs are the only storage it can
   reach, and only for this run;
3. the job execution is started with per-execution overrides: the
   entrypoint (``ENTRYPOINT``, standard library only, run as
   ``python3 -I -c``) followed by the test command as plain arguments, the
   two URLs and the limits as environment variables, and a task timeout
   that kills the task even if the worker dies;
4. the entrypoint downloads and unpacks the archive (refusing members that
   escape the workspace), runs the command without a shell, with a minimal
   environment and the timeout, keeps the tail of its output, and uploads
   ``<json header>\\n<output tail>`` before exiting with the command's code;
5. the worker polls the execution, reads the capped result, and checks that
   it agrees with the task's own outcome as Cloud Run saw it (exit code 0
   if and only if the task succeeded);
6. on timeout, error or cancellation the execution is cancelled, and in
   every case both objects are deleted. The bucket's 1-day lifecycle rule
   removes whatever a crashed worker leaves behind.

Timing: the command gets ``timeout`` seconds inside the job; the worker
waits ``timeout + start_seconds`` (``CLOUD_RUN_SANDBOX_START_SECONDS``,
default 20) counted from the start of packing, so a cold start does not eat
the test's own budget. verification.py stops waiting 30 seconds after the
timeout, so keep ``start_seconds`` below that.

All Google API calls go through a small client (``CloudRunJobClient``); the
default, ``GcpRestClient``, calls the REST APIs (Cloud Run Admin v2, Cloud
Storage JSON, IAM Credentials ``signBlob``) with httpx and application
default credentials from google-auth, so no Google Cloud client library is
needed. Tests use a fake. See docs/VERIFICATION.md ("Cloud Run Jobs sandbox
backend").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import tarfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    IO,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import quote

import httpx

from .base import CommandTimeoutError, RunnerError
from .docker_sandbox import DEFAULT_MAX_OUTPUT_BYTES, DockerSandbox, _archive_workspace

logger = logging.getLogger(__name__)

DEFAULT_START_SECONDS = 20
# Upper bound for CLOUD_RUN_SANDBOX_START_SECONDS: beyond verification's own
# 30-second grace the worker would be cancelled before giving up itself.
MAX_START_SECONDS = 25
# Added to the task timeout so the platform, not only the worker, ends a run
# whose worker died: start, download and upload happen inside the task too.
TASK_TIMEOUT_MARGIN = 60
# Signed URLs outlive the worker's deadline by this much (clock skew, upload).
SIGNED_URL_MARGIN = 300
# Longest expiry a V4 signed URL may have.
MAX_SIGNED_URL_SECONDS = 7 * 24 * 3600
# Seconds between execution status reads.
POLL_INTERVAL = 2.0
# Seconds allowed for each cleanup call (cancel, delete).
CLEANUP_TIMEOUT = 10
# Room for the JSON header in front of the output tail in the result object.
RESULT_HEADER_BYTES = 1024
# Exit codes the entrypoint uses for itself, as `docker run` and timeout(1) do.
SETUP_ERROR_EXIT = 125
TIMEOUT_EXIT = 124
OBJECT_PREFIX = "executions/"

INPUT_URL_ENV = "AM_SANDBOX_INPUT_URL"
OUTPUT_URL_ENV = "AM_SANDBOX_OUTPUT_URL"
TIMEOUT_ENV = "AM_SANDBOX_TIMEOUT"
MAX_OUTPUT_ENV = "AM_SANDBOX_MAX_OUTPUT"
OUTPUT_RANGE_ENV = "AM_SANDBOX_OUTPUT_RANGE"

RUN_API = "https://run.googleapis.com/v2/"
STORAGE_API = "https://storage.googleapis.com"
STORAGE_HOST = "storage.googleapis.com"
IAM_CREDENTIALS_API = "https://iamcredentials.googleapis.com/v1/"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
HTTP_TIMEOUT = 60.0

# The job's entrypoint, sent with every execution as `python3 -I -c` so the
# job image only needs a Python 3.11.4+ interpreter (tarfile filters) and
# never drifts from the worker's code. Standard library only. Portable to
# Windows only so that the tests can run it on any developer machine.
ENTRYPOINT = r"""
import json, os, shutil, signal, subprocess, sys, tarfile, tempfile, threading
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
env = os.environ
out_url = env["AM_SANDBOX_OUTPUT_URL"]
limit = int(env["AM_SANDBOX_MAX_OUTPUT"])
argv = sys.argv[1:]
tail = bytearray()
total = 0
result = {"exit": 125, "timed_out": False, "total": 0, "error": None}


def finish(code):
    result["exit"] = code
    result["total"] = total
    body = json.dumps(result).encode() + b"\n" + bytes(tail[-limit:])
    request = urllib.request.Request(
        out_url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": "application/octet-stream",
            "x-goog-content-length-range": env["AM_SANDBOX_OUTPUT_RANGE"],
        },
    )
    opener.open(request, timeout=120).close()
    sys.exit(code)


try:
    timeout = float(env["AM_SANDBOX_TIMEOUT"])
    work = tempfile.mkdtemp(prefix="workspace-")
    with tempfile.TemporaryFile() as archive:
        with opener.open(env["AM_SANDBOX_INPUT_URL"], timeout=120) as response:
            shutil.copyfileobj(response, archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            # "tar": nothing outside the workspace, links kept as links.
            tar.extractall(work, filter="tar")
except Exception as e:
    result["error"] = ("could not unpack the working copy: %s" % e)[:300]
    finish(125)

tmp = tempfile.gettempdir()
child_env = {
    "HOME": tmp,
    "TMPDIR": tmp,
    "CI": "true",
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": work,
}
for name in ("PATH", "SYSTEMROOT"):
    if name in env:
        child_env[name] = env[name]
posix = os.name == "posix"
try:
    proc = subprocess.Popen(
        argv,
        cwd=work,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **({"start_new_session": True} if posix else {}),
    )
except OSError as e:
    tail += ("%s: %s\n" % (argv[0], e.strerror or e)).encode()
    total = len(tail)
    finish(126 if isinstance(e, PermissionError) else 127)


def pump():
    global total
    while True:
        chunk = proc.stdout.read1(65536)
        if not chunk:
            return
        total += len(chunk)
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[: len(tail) - limit]


def kill():
    try:
        if posix:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


reader = threading.Thread(target=pump, daemon=True)
reader.start()
try:
    code = proc.wait(timeout)
except subprocess.TimeoutExpired:
    kill()
    proc.wait()
    result["timed_out"] = True
    code = 124
kill()  # whatever the command left running in its process group
reader.join(10)
if code < 0:
    code = 128 - code
finish(min(code, 255))
"""
ENTRYPOINT_ARGS = ("-I", "-c", ENTRYPOINT)

_PROJECT_RE = re.compile(r"[a-z][a-z0-9.:-]*[a-z0-9]")
_REGION_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+")
_JOB_RE = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
_EMAIL_RE = re.compile(r"[^@\s/]+@[^@\s/]+")
_PROJECT_ENV = "CLOUD_RUN_SANDBOX_PROJECT or GOOGLE_CLOUD_PROJECT"
_FIELDS = (
    ("project", _PROJECT_ENV, _PROJECT_RE),
    ("region", "CLOUD_RUN_SANDBOX_REGION", _REGION_RE),
    ("job", "CLOUD_RUN_SANDBOX_JOB", _JOB_RE),
    ("bucket", "CLOUD_RUN_SANDBOX_BUCKET", _BUCKET_RE),
)


def _setting(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


@dataclass(frozen=True)
class CloudRunJobSandboxConfig:
    """Where the sandbox job and bucket are, read from the environment by
    ``from_env``. Every name is validated: they end up in API URLs."""

    project: str
    region: str
    job: str
    bucket: str
    # Service account that signs the URLs; None: the credentials' own.
    signer: Optional[str] = None
    start_seconds: int = DEFAULT_START_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        for field, name, pattern in _FIELDS:
            value = getattr(self, field)
            if not value:
                raise RunnerError(f"{name} is required for the Cloud Run sandbox")
            if not pattern.fullmatch(value):
                raise RunnerError(f"{name}={value!r} is invalid")
        if self.signer is not None and not _EMAIL_RE.fullmatch(self.signer):
            raise RunnerError(
                f"CLOUD_RUN_SANDBOX_SIGNER={self.signer!r} is not a service account email"
            )
        if not 0 <= self.start_seconds <= MAX_START_SECONDS:
            raise RunnerError(
                f"CLOUD_RUN_SANDBOX_START_SECONDS={self.start_seconds!r} must be "
                f"between 0 and {MAX_START_SECONDS}"
            )

    @property
    def job_name(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/jobs/{self.job}"

    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> CloudRunJobSandboxConfig:
        env = os.environ if env is None else env
        start = _setting(env, "CLOUD_RUN_SANDBOX_START_SECONDS") or str(
            DEFAULT_START_SECONDS
        )
        if not start.isdigit():
            raise RunnerError(
                f"CLOUD_RUN_SANDBOX_START_SECONDS={start!r} must be a whole number"
            )
        try:
            max_output = int(
                env.get("VERIFY_MAX_OUTPUT_BYTES") or DEFAULT_MAX_OUTPUT_BYTES
            )
        except ValueError:
            max_output = DEFAULT_MAX_OUTPUT_BYTES
        project_var = (
            "CLOUD_RUN_SANDBOX_PROJECT"
            if _setting(env, "CLOUD_RUN_SANDBOX_PROJECT")
            else "GOOGLE_CLOUD_PROJECT"
        )
        try:
            return cls(
                project=_setting(env, project_var),
                region=_setting(env, "CLOUD_RUN_SANDBOX_REGION"),
                job=_setting(env, "CLOUD_RUN_SANDBOX_JOB"),
                bucket=_setting(env, "CLOUD_RUN_SANDBOX_BUCKET"),
                signer=_setting(env, "CLOUD_RUN_SANDBOX_SIGNER") or None,
                start_seconds=int(start),
                # Same floor as verification.py.
                max_output_bytes=max(1024, max_output),
            )
        except RunnerError as e:
            # Name the variable that was actually read.
            raise RunnerError(str(e).replace(_PROJECT_ENV, project_var)) from None


@dataclass(frozen=True)
class ExecutionStatus:
    """What Cloud Run reports about one execution (one task)."""

    done: bool
    succeeded: bool
    message: str = ""


class CloudRunJobClient(Protocol):
    """The Google Cloud calls the sandbox makes; ``GcpRestClient`` in
    production, a fake in tests. Used as an async context manager around
    one ``exec_command``."""

    async def __aenter__(self) -> CloudRunJobClient: ...

    async def __aexit__(self, *exc: Any) -> Any: ...

    async def sign_url(
        self,
        name: str,
        method: str,
        expires_seconds: int,
        headers: Optional[Mapping[str, str]] = None,
    ) -> str: ...

    async def upload_object(self, name: str, data: IO[bytes]) -> None: ...

    async def run_job(
        self, args: Sequence[str], env: Mapping[str, str], task_timeout_seconds: int
    ) -> str: ...

    async def get_execution(self, execution: str) -> ExecutionStatus: ...

    async def cancel_execution(self, execution: str) -> None: ...

    async def read_object(self, name: str, max_bytes: int) -> Optional[bytes]: ...

    async def delete_object(self, name: str) -> None: ...


@dataclass
class _Run:
    prefix: str
    execution: Optional[str] = None
    finished: bool = False

    @property
    def input_name(self) -> str:
        return self.prefix + "workspace.tar"

    @property
    def output_name(self) -> str:
        return self.prefix + "output"


def _parse_result(data: bytes) -> Tuple[Dict[str, Any], bytes]:
    head, sep, body = data.partition(b"\n")
    try:
        header = json.loads(head) if sep else None
    except ValueError:
        header = None
    if not (
        isinstance(header, dict)
        and type(header.get("exit")) is int
        and 0 <= header["exit"] <= 255
        and type(header.get("timed_out")) is bool
        and type(header.get("total")) is int
        and isinstance(header.get("error"), (str, type(None)))
    ):
        raise RunnerError("the sandbox job returned a malformed result")
    return header, body


class CloudRunJobSandbox(DockerSandbox):
    """``runners.sandbox.SandboxBackend`` running each command in one
    execution of a Cloud Run job, against its own copy of ``repo_path``.

    File operations, ``get_git_diff`` and the refusal of ``commit_and_push``
    are DockerSandbox's: they act on the host working copy and refuse paths
    that escape it. Only ``exec_command`` differs.
    """

    def __init__(
        self,
        repo_path: Union[str, Path],
        config: Optional[CloudRunJobSandboxConfig] = None,
        *,
        client: Optional[CloudRunJobClient] = None,
    ):
        self.config = config or CloudRunJobSandboxConfig.from_env()
        super().__init__(repo_path, config=self.config)  # type: ignore[arg-type]
        self._client = client

    async def commit_and_push(self, branch: str, message: str, dry_run: bool) -> str:
        raise NotImplementedError(
            "CloudRunJobSandbox is a verification backend: it runs commands in "
            "isolation and never commits or pushes. Use the workspace/GitHub "
            "integration to publish changes."
        )

    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]:
        """Run ``cmd`` in a new job execution; returns (exit_code, output, note).

        ``output`` is stdout and stderr interleaved, cut to the last
        ``max_output_bytes``; ``note`` is empty or a truncation notice.
        Raises CommandTimeoutError when the command times out in the job, or
        the job does not finish within ``timeout + start_seconds`` (counted
        from the start of packing; the execution is then cancelled), and
        RunnerError when the job could not run the command or a Google API
        call failed. Both objects are deleted in every case.
        """
        from .local import _to_argv

        argv = _to_argv(cmd)
        if not timeout or timeout <= 0:
            timeout = 60
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
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
            if loop.time() - started >= timeout:
                raise CommandTimeoutError(
                    f"could not pack the working copy within {timeout:g} seconds"
                )
            deadline = started + timeout + self.config.start_seconds
            run = _Run(prefix=OBJECT_PREFIX + secrets.token_hex(16) + "/")
            client = self._client or GcpRestClient(self.config)
            async with client:
                try:
                    return await asyncio.wait_for(
                        self._execute(client, run, argv, timeout, archive),
                        deadline - loop.time(),
                    )
                except asyncio.TimeoutError:
                    raise CommandTimeoutError(
                        f"sandboxed command did not finish within {timeout:g} "
                        f"seconds (+{self.config.start_seconds}s to start the job)"
                    ) from None
                finally:
                    await self._cleanup(client, run)
        finally:
            archive.close()

    async def _execute(
        self,
        client: CloudRunJobClient,
        run: _Run,
        argv: List[str],
        timeout: float,
        archive: IO[bytes],
    ) -> Tuple[int, str, str]:
        seconds = math.ceil(timeout)
        limit = self.config.max_output_bytes
        size_range = f"0,{limit + RESULT_HEADER_BYTES}"
        expires = seconds + self.config.start_seconds + SIGNED_URL_MARGIN
        get_url = await client.sign_url(run.input_name, "GET", expires)
        put_url = await client.sign_url(
            run.output_name,
            "PUT",
            expires,
            {"x-goog-content-length-range": size_range},
        )
        await client.upload_object(run.input_name, archive)
        env = {
            INPUT_URL_ENV: get_url,
            OUTPUT_URL_ENV: put_url,
            TIMEOUT_ENV: str(seconds),
            MAX_OUTPUT_ENV: str(limit),
            OUTPUT_RANGE_ENV: size_range,
        }
        run.execution = await client.run_job(
            [*ENTRYPOINT_ARGS, *argv],
            env,
            seconds + self.config.start_seconds + TASK_TIMEOUT_MARGIN,
        )
        while True:
            status = await client.get_execution(run.execution)
            if status.done:
                break
            await asyncio.sleep(POLL_INTERVAL)
        run.finished = True
        data = await client.read_object(run.output_name, limit + RESULT_HEADER_BYTES)
        return self._result(data, status, timeout)

    def _result(
        self, data: Optional[bytes], status: ExecutionStatus, timeout: float
    ) -> Tuple[int, str, str]:
        if data is None:
            detail = f": {status.message}" if status.message else ""
            raise RunnerError(f"the sandbox job ended without a result{detail}")
        header, body = _parse_result(data)
        if header["error"]:
            raise RunnerError(
                f"the sandbox job could not run the command: {header['error']}"
            )
        if header["timed_out"]:
            raise CommandTimeoutError(
                f"sandboxed command did not finish within {timeout:g} seconds"
            )
        code = header["exit"]
        if (code == 0) != status.succeeded:
            raise RunnerError(
                f"the sandbox result (exit {code}) contradicts the job execution "
                f"({'succeeded' if status.succeeded else 'failed'})"
            )
        limit = self.config.max_output_bytes
        total = max(header["total"], len(body))
        if len(body) > limit:
            body = body[-limit:]
        note = ""
        if total > limit:
            note = (
                f"\n[sandbox output truncated: showing the last {limit} "
                f"of {total} bytes]\n"
            )
        return code, body.decode("utf-8", errors="replace"), note

    async def _cleanup(self, client: CloudRunJobClient, run: _Run) -> None:
        """Cancel an unfinished execution, then delete both objects. Failures
        are logged, never raised: they must not hide the run's result. The
        task timeout and the bucket lifecycle rule are the backstops."""
        if run.execution and not run.finished:
            try:
                await asyncio.wait_for(
                    client.cancel_execution(run.execution), CLEANUP_TIMEOUT
                )
            except Exception as e:
                logger.warning("could not cancel %s: %s", run.execution, e)
        for name in (run.input_name, run.output_name):
            try:
                await asyncio.wait_for(client.delete_object(name), CLEANUP_TIMEOUT)
            except Exception as e:
                logger.warning("could not delete sandbox object %s: %s", name, e)


# --- Google REST APIs -----------------------------------------------------------


def v4_string_to_sign(
    *,
    bucket: str,
    name: str,
    method: str,
    signer: str,
    when: datetime,
    expires_seconds: int,
    headers: Mapping[str, str],
) -> Tuple[str, str, str]:
    """Cloud Storage V4 signing: (path, canonical query, string to sign).

    Follows https://cloud.google.com/storage/docs/authentication/signatures.
    The payload is not signed (``UNSIGNED-PAYLOAD``); every header passed
    here, plus ``host``, is, so a client must send exactly those values.
    """
    path = f"/{bucket}/{quote(name, safe='/~')}"
    timestamp = when.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{when.strftime('%Y%m%d')}/auto/storage/goog4_request"
    canonical_headers = {
        k.strip().lower(): " ".join(str(v).split())
        for k, v in {**headers, "host": STORAGE_HOST}.items()
    }
    signed_headers = ";".join(sorted(canonical_headers))
    query = {
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        "X-Goog-Credential": f"{signer}/{scope}",
        "X-Goog-Date": timestamp,
        "X-Goog-Expires": str(expires_seconds),
        "X-Goog-SignedHeaders": signed_headers,
    }
    canonical_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(query.items())
    )
    canonical_request = "\n".join(
        [
            method,
            path,
            canonical_query,
            "".join(f"{k}:{canonical_headers[k]}\n" for k in sorted(canonical_headers)),
            signed_headers,
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "GOOG4-RSA-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    return path, canonical_query, string_to_sign


class GcpRestClient:
    """``CloudRunJobClient`` over the Google REST APIs, with httpx.

    Authenticates with application default credentials (google-auth, part of
    the server extra), which on the worker VM is its service account; that
    account needs ``run.jobs.runWithOverrides`` and execution read/cancel on
    the job, object create/read/delete on the bucket, and
    ``iam.serviceAccounts.signBlob`` on itself (sandbox.tf grants exactly
    those).
    """

    def __init__(
        self,
        config: CloudRunJobSandboxConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        token: Optional[Callable[[], Awaitable[str]]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.config = config
        self._transport = transport
        self._token_source = token
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._http: Optional[httpx.AsyncClient] = None
        self._credentials: Any = None
        self._lock = threading.Lock()

    async def __aenter__(self) -> GcpRestClient:
        self._http = httpx.AsyncClient(transport=self._transport, timeout=HTTP_TIMEOUT)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- credentials ---------------------------------------------------------

    def _refreshed_credentials(self) -> Any:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as e:  # the SDK without the server extra
            raise RunnerError(
                f"the Cloud Run sandbox needs google-auth (server extra): {e}"
            ) from e
        with self._lock:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(
                    scopes=[CLOUD_PLATFORM_SCOPE]
                )
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            return self._credentials

    async def _token(self) -> str:
        if self._token_source is not None:
            return await self._token_source()
        try:
            credentials = await asyncio.to_thread(self._refreshed_credentials)
        except RunnerError:
            raise
        except Exception as e:
            raise RunnerError(f"no Google credentials for the sandbox: {e}") from e
        return credentials.token

    async def _signer(self) -> str:
        if self.config.signer:
            return self.config.signer
        if self._token_source is None:
            credentials = await asyncio.to_thread(self._refreshed_credentials)
            email = getattr(credentials, "service_account_email", None)
            if email and _EMAIL_RE.fullmatch(email):
                return email
        raise RunnerError(
            "set CLOUD_RUN_SANDBOX_SIGNER to the worker's service account email"
        )

    # --- HTTP ----------------------------------------------------------------

    async def _request(
        self, method: str, url: str, *, ok: Sequence[int] = (), **kwargs: Any
    ) -> httpx.Response:
        assert self._http is not None, "use GcpRestClient as an async context manager"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        headers.update(kwargs.pop("headers", {}))
        where = url.split("?", 1)[0]
        try:
            response = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise RunnerError(f"{method} {where} failed: {e}") from e
        if response.status_code >= 400 and response.status_code not in ok:
            raise RunnerError(
                f"{method} {where} failed: HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    def _object_url(self, name: str) -> str:
        return (
            f"{STORAGE_API}/storage/v1/b/{self.config.bucket}/o/{quote(name, safe='')}"
        )

    # --- CloudRunJobClient -----------------------------------------------------

    async def sign_url(
        self,
        name: str,
        method: str,
        expires_seconds: int,
        headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        if not 1 <= expires_seconds <= MAX_SIGNED_URL_SECONDS:
            raise RunnerError(
                f"signed URL expiry {expires_seconds}s must be between 1 and "
                f"{MAX_SIGNED_URL_SECONDS} seconds"
            )
        signer = await self._signer()
        path, query, string_to_sign = v4_string_to_sign(
            bucket=self.config.bucket,
            name=name,
            method=method,
            signer=signer,
            when=self._now(),
            expires_seconds=expires_seconds,
            headers=headers or {},
        )
        response = await self._request(
            "POST",
            f"{IAM_CREDENTIALS_API}projects/-/serviceAccounts/{signer}:signBlob",
            json={"payload": base64.b64encode(string_to_sign.encode()).decode()},
        )
        try:
            signature = base64.b64decode(response.json()["signedBlob"]).hex()
        except (ValueError, KeyError, TypeError) as e:
            raise RunnerError(f"signBlob returned no signature: {e}") from e
        return f"https://{STORAGE_HOST}{path}?{query}&X-Goog-Signature={signature}"

    async def upload_object(self, name: str, data: IO[bytes]) -> None:
        data.seek(0, os.SEEK_END)
        size = data.tell()
        data.seek(0)

        async def chunks():
            while chunk := data.read(1024 * 1024):
                yield chunk

        await self._request(
            "POST",
            f"{STORAGE_API}/upload/storage/v1/b/{self.config.bucket}/o"
            f"?uploadType=media&name={quote(name, safe='')}",
            content=chunks(),
            headers={
                "Content-Type": "application/x-tar",
                "Content-Length": str(size),
            },
        )

    async def run_job(
        self, args: Sequence[str], env: Mapping[str, str], task_timeout_seconds: int
    ) -> str:
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "args": list(args),
                        "env": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ],
                "taskCount": 1,
                "timeout": f"{int(task_timeout_seconds)}s",
            }
        }
        response = await self._request(
            "POST", f"{RUN_API}{self.config.job_name}:run", json=body
        )
        execution = ((response.json() or {}).get("metadata") or {}).get("name")
        if not isinstance(execution, str) or not execution.startswith(
            self.config.job_name + "/executions/"
        ):
            raise RunnerError(f"the job run returned no execution: {execution!r}")
        return execution

    def _check_execution(self, execution: str) -> None:
        if not re.fullmatch(
            re.escape(self.config.job_name) + r"/executions/[a-z0-9-]+", execution
        ):
            raise RunnerError(f"unexpected execution name {execution!r}")

    async def get_execution(self, execution: str) -> ExecutionStatus:
        self._check_execution(execution)
        body = (await self._request("GET", f"{RUN_API}{execution}")).json()
        if not body.get("completionTime"):
            return ExecutionStatus(done=False, succeeded=False)
        succeeded = int(body.get("succeededCount") or 0) >= 1 and not int(
            body.get("failedCount") or 0
        )
        message = ""
        if not succeeded:
            for condition in body.get("conditions") or []:
                if condition.get("type") == "Completed":
                    message = str(condition.get("message") or "")[:300]
        return ExecutionStatus(done=True, succeeded=succeeded, message=message)

    async def cancel_execution(self, execution: str) -> None:
        self._check_execution(execution)
        # 400 (FAILED_PRECONDITION) and 404: it already finished or is gone.
        await self._request("POST", f"{RUN_API}{execution}:cancel", ok=(400, 404))

    async def read_object(self, name: str, max_bytes: int) -> Optional[bytes]:
        assert self._http is not None, "use GcpRestClient as an async context manager"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        url = self._object_url(name) + "?alt=media"
        data = bytearray()
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    return None
                if response.status_code >= 400:
                    await response.aread()
                    raise RunnerError(
                        f"GET {url.split('?', 1)[0]} failed: HTTP "
                        f"{response.status_code}: {response.text[:300]}"
                    )
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) >= max_bytes:
                        break
        except httpx.HTTPError as e:
            raise RunnerError(f"reading {name} failed: {e}") from e
        return bytes(data[:max_bytes])

    async def delete_object(self, name: str) -> None:
        await self._request("DELETE", self._object_url(name), ok=(404,))


def create(repo_path: Union[str, Path]) -> CloudRunJobSandbox:
    """``SANDBOX_BACKEND`` factory: ``cloud_run_job``, or
    ``automaintainer.runners.cloud_run_job_sandbox:create``."""
    return CloudRunJobSandbox(repo_path)
