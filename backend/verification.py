"""Verify an agent change by running the target repository's tests.

Given a working copy of the repository with the change applied, this module
picks the test command (``.automaintainer.yml`` at the repository root, else a
detected pytest or ``npm test``), runs it through the runner abstraction and
returns a ``VerificationResult``. The agent pipeline attaches that result to
the pull request (body section and commit status), so ``MERGE_POLICY=ci``
merges only a head whose verification passed.

Security model (see docs/VERIFICATION.md): the test command runs code from the
repository, which is untrusted. In production it must run in a sandbox
(``SANDBOX_BACKEND``); local execution is refused there unless
``VERIFY_ALLOW_LOCAL=true``. The local runner is defense in depth only: an
argument list (no shell), a scrubbed environment without the server's secrets,
a wall-clock timeout and an output cap. It does not stop the tests from
reading files the server user can read or from using the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import yaml

from runners.base import CommandTimeoutError, RunnerError
from runners.sandbox import SandboxRunner, load_sandbox_backend_factory

logger = logging.getLogger(__name__)

CONFIG_FILE = ".automaintainer.yml"

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
CLONE_TIMEOUT_SECONDS = 300
# Characters of output tail shown in the PR body.
PR_OUTPUT_TAIL_CHARS = 3000

STATUS_CONTEXT = "automaintainer/verification"
SECTION_START = "<!-- automaintainer:verification:start -->"
SECTION_END = "<!-- automaintainer:verification:end -->"

# The only variables the local test process inherits. Everything else (API
# keys, database URLs, GitHub tokens) is dropped.
_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "TZ",
)
_NPM_PLACEHOLDER_TEST = "no test specified"
_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass
class VerificationResult:
    """Outcome of one verification.

    ``status`` is passed (command exited 0), failed (non-zero exit), error
    (could not get a trustworthy answer: timeout, refused, misconfigured) or
    skipped (nothing was run: disabled or no tests detected).
    """

    status: str
    summary: str
    command: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    exit_code: Optional[int] = None
    output_tail: str = ""
    truncated: bool = False
    runner: Optional[str] = None
    head_sha: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def to_dict(self) -> dict:
        return asdict(self)


class VerificationConfigError(ValueError):
    """``.automaintainer.yml`` is present but unusable."""


@dataclass
class TestPlan:
    command: list[str]
    timeout_seconds: int
    source: str  # "config", "pytest" or "npm"


# --- Settings -----------------------------------------------------------------


def _env_flag(name: str) -> Optional[bool]:
    value = (os.getenv(name) or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, os.getenv(name))
        return default


def verification_enabled() -> bool:
    """VERIFY_CHANGES: unset or true runs verification whenever a test
    command is found; false turns it off (the result is then ``skipped``)."""
    return _env_flag("VERIFY_CHANGES") is not False


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


def local_execution_refusal() -> Optional[str]:
    """Why the local runner may not be used here, or None when it may."""
    if _is_production() and _env_flag("VERIFY_ALLOW_LOCAL") is not True:
        return (
            "refused to run repository tests on the API/worker host with "
            "ENVIRONMENT=production; configure SANDBOX_BACKEND "
            "(or set VERIFY_ALLOW_LOCAL=true to accept the risk)"
        )
    return None


# --- Test command detection ---------------------------------------------------


def _load_config(repo_dir: Path) -> Optional[dict]:
    path = repo_dir / CONFIG_FILE
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise VerificationConfigError(f"{CONFIG_FILE} is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise VerificationConfigError(f"{CONFIG_FILE} must be a mapping")
    return data


def _config_timeout(config: dict) -> int:
    default = _env_int("VERIFY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    ceiling = _env_int("VERIFY_MAX_TIMEOUT_SECONDS", MAX_TIMEOUT_SECONDS)
    value = config.get("timeout_seconds", default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise VerificationConfigError("timeout_seconds must be a positive integer")
    # The repository chooses its timeout, the operator caps it.
    return min(value, ceiling)


def _has_pytest_config(repo_dir: Path) -> bool:
    if (repo_dir / "pytest.ini").is_file() or (repo_dir / "tests").is_dir():
        return True
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = {}
        if "pytest" in (data.get("tool") or {}):
            return True
    setup_cfg = repo_dir / "setup.cfg"
    if setup_cfg.is_file():
        try:
            return "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
    return False


def _has_npm_test(repo_dir: Path) -> bool:
    package_json = repo_dir / "package.json"
    if not package_json.is_file():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    # `npm init` writes a test script that always fails; it is not a test suite.
    return (
        isinstance(test, str)
        and bool(test.strip())
        and (_NPM_PLACEHOLDER_TEST not in test)
    )


def detect_test_plan(repo_dir: Path) -> Optional[TestPlan]:
    """The test command for ``repo_dir``, or None when no tests are detected.

    Raises VerificationConfigError for an invalid ``.automaintainer.yml``; a
    broken config fails verification rather than falling back to detection.
    """
    repo_dir = Path(repo_dir)
    config = _load_config(repo_dir)
    timeout = _config_timeout(config or {})
    if config and "test_command" in config:
        command = config["test_command"]
        # A list only: a string would need a shell to split it.
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(a, str) and a for a in command)
        ):
            raise VerificationConfigError(
                "test_command must be a non-empty list of strings, "
                'e.g. ["python", "-m", "pytest", "-q"]'
            )
        return TestPlan(list(command), timeout, "config")
    if _has_pytest_config(repo_dir):
        python = os.getenv("VERIFY_PYTHON") or "python"
        return TestPlan([python, "-m", "pytest", "-q"], timeout, "pytest")
    if _has_npm_test(repo_dir):
        return TestPlan(["npm", "test", "--silent"], timeout, "npm")
    return None


# --- Runners ------------------------------------------------------------------


def _cap_output(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text, False
    return data[-max_bytes:].decode("utf-8", errors="ignore"), True


def _local_env(home: str) -> dict:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.update(
        {
            # Point home directories at an empty scratch dir so tools do not
            # pick up the server user's credentials (~/.netrc, ~/.npmrc, ...).
            "HOME": home,
            "USERPROFILE": home,
            "TMPDIR": home,
            "TEMP": home,
            "TMP": home,
            "CI": "true",
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "nt":
            # proc.kill() would leave grandchildren (e.g. node under npm) alive.
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        logger.warning("Verification process %s did not exit after kill", proc.pid)


async def run_local(
    command: Sequence[str], cwd: Path, timeout: float, max_output_bytes: int
) -> tuple[int, str, bool]:
    """Run ``command`` (no shell) in ``cwd`` with a scrubbed environment.

    Returns (exit_code, output, truncated) where output is stdout and stderr
    interleaved, cut to its last ``max_output_bytes``. Raises
    CommandTimeoutError after ``timeout`` seconds (the process tree is killed)
    and RunnerError when the program cannot be started.
    """
    with tempfile.TemporaryDirectory(
        prefix="verify-home-", ignore_cleanup_errors=True
    ) as home:
        env = _local_env(home)
        program = shutil.which(command[0], path=env.get("PATH")) or command[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                program,
                *command[1:],
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        except OSError as e:
            raise RunnerError(f"cannot start {command[0]!r}: {e}") from e

        tail = bytearray()
        total = 0

        async def pump() -> None:
            nonlocal total
            while chunk := await proc.stdout.read(65536):
                # Keep draining past the cap so the child never blocks on a
                # full pipe, but only remember the last max_output_bytes.
                total += len(chunk)
                tail.extend(chunk)
                if len(tail) > max_output_bytes:
                    del tail[: len(tail) - max_output_bytes]
            await proc.wait()

        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            raise CommandTimeoutError(
                f"tests did not finish within {timeout:g} seconds"
            ) from None
        output = bytes(tail).decode("utf-8", errors="replace")
        return proc.returncode, output, total > max_output_bytes


async def _run_plan(
    plan: TestPlan,
    repo_dir: Path,
    sandbox_factory: Optional[Callable],
    max_output_bytes: int,
) -> tuple[int, str, bool, str]:
    if sandbox_factory is not None:
        runner = SandboxRunner(repo_path=repo_dir, backend=sandbox_factory(repo_dir))
        code, out, err = await runner.exec_command(
            list(plan.command), timeout=plan.timeout_seconds
        )
        output, truncated = _cap_output(out + err, max_output_bytes)
        return code, output, truncated, "sandbox"
    code, output, truncated = await run_local(
        plan.command, repo_dir, plan.timeout_seconds, max_output_bytes
    )
    return code, output, truncated, "local"


_UNSET = object()


async def verify_working_copy(
    repo_dir: Path | str,
    *,
    changed_paths: Iterable[str] = (),
    sandbox_factory=_UNSET,
) -> VerificationResult:
    """Run the repository's tests in ``repo_dir`` and report the outcome.

    Never raises: every problem becomes an ``error`` result, so callers can
    gate on ``status == "passed"`` and fail closed on anything else.
    ``sandbox_factory`` defaults to the one named by ``SANDBOX_BACKEND``.
    """
    repo_dir = Path(repo_dir)
    if not verification_enabled():
        return VerificationResult(
            SKIPPED, "verification disabled (VERIFY_CHANGES=false)"
        )
    changed = {p.replace("\\", "/").lstrip("/") for p in changed_paths}
    if CONFIG_FILE in changed:
        # The change could otherwise pick the command that judges it.
        return VerificationResult(
            ERROR, f"the change modifies {CONFIG_FILE}; it cannot verify itself"
        )
    try:
        if sandbox_factory is _UNSET:
            sandbox_factory = load_sandbox_backend_factory()
        plan = detect_test_plan(repo_dir)
    except (RunnerError, VerificationConfigError) as e:
        return VerificationResult(ERROR, str(e))
    if plan is None:
        return VerificationResult(SKIPPED, "no tests detected")
    if sandbox_factory is None:
        refusal = local_execution_refusal()
        if refusal:
            return VerificationResult(ERROR, refusal, command=plan.command)

    max_output = max(
        1024, _env_int("VERIFY_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
    )
    started = time.monotonic()
    runner = "sandbox" if sandbox_factory is not None else "local"

    def elapsed() -> float:
        return round(time.monotonic() - started, 2)

    try:
        code, output, truncated, runner = await _run_plan(
            plan, repo_dir, sandbox_factory, max_output
        )
    except CommandTimeoutError:
        return VerificationResult(
            ERROR,
            f"timed out after {plan.timeout_seconds}s",
            command=plan.command,
            duration_seconds=elapsed(),
            runner=runner,
        )
    except Exception as e:  # RunnerError or a sandbox backend failure
        logger.warning("Verification could not run %s: %s", plan.command, e)
        return VerificationResult(
            ERROR,
            f"could not run the tests: {e}",
            command=plan.command,
            duration_seconds=elapsed(),
            runner=runner,
        )
    status = PASSED if code == 0 else FAILED
    return VerificationResult(
        status,
        f"tests {'passed' if code == 0 else 'failed'} (exit code {code})",
        command=plan.command,
        duration_seconds=elapsed(),
        exit_code=code,
        output_tail=output,
        truncated=truncated,
        runner=runner,
    )


# --- Pull request branches ----------------------------------------------------


async def _git(args: list[str], cwd: Optional[str], env: dict, timeout: float):
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RunnerError(f"git {args[0]} timed out after {timeout:g}s") from None
    if proc.returncode != 0:
        raise RunnerError(
            f"git {args[0]} failed: "
            + err.decode("utf-8", errors="replace").strip()[-300:]
        )
    return out.decode("utf-8", errors="replace").strip()


async def verify_pr_branch(
    clone_url,
    branch: str,
    head_sha,
    token: Optional[str],
    *,
    changed_paths: Iterable[str] = (),
) -> VerificationResult:
    """Clone ``branch`` at exactly ``head_sha`` into a temporary directory and
    verify it; the clone is deleted afterwards. Never raises.

    The token reaches git through its environment only (see
    workspace.git_auth_env) and never the test process.
    """
    from workspace import git_auth_env

    if not verification_enabled():
        return VerificationResult(
            SKIPPED, "verification disabled (VERIFY_CHANGES=false)"
        )
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
        return VerificationResult(ERROR, "could not resolve the PR head commit")
    if not isinstance(clone_url, str) or not clone_url:
        return VerificationResult(
            ERROR, "could not resolve the repository clone URL", head_sha=head_sha
        )
    # Refuse before spending a clone when the result is known to be a refusal.
    try:
        sandbox_factory = load_sandbox_backend_factory()
    except RunnerError as e:
        return VerificationResult(ERROR, str(e), head_sha=head_sha)
    refusal = local_execution_refusal() if sandbox_factory is None else None
    if refusal:
        return VerificationResult(ERROR, refusal, head_sha=head_sha)

    workdir = tempfile.mkdtemp(prefix="automaintainer-verify-")
    repo_dir = os.path.join(workdir, "repo")
    try:
        env = {**git_auth_env(token), "GIT_LFS_SKIP_SMUDGE": "1"}
        try:
            await _git(
                [
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--no-tags",
                    "--branch",
                    branch,
                    "--",
                    clone_url,
                    repo_dir,
                ],
                None,
                env,
                CLONE_TIMEOUT_SECONDS,
            )
            cloned_sha = await _git(["rev-parse", "HEAD"], repo_dir, env, 30)
        except (RunnerError, OSError) as e:
            return VerificationResult(
                ERROR, f"could not check out the change: {e}", head_sha=head_sha
            )
        if cloned_sha != head_sha:
            # Never report a result for a different commit than the one gated.
            return VerificationResult(
                ERROR,
                f"branch moved during verification ({cloned_sha[:12]} != {head_sha[:12]})",
                head_sha=head_sha,
            )
        result = await verify_working_copy(
            repo_dir, changed_paths=changed_paths, sandbox_factory=sandbox_factory
        )
        result.head_sha = head_sha
        return result
    finally:
        await asyncio.to_thread(_rmtree, workdir)


def _rmtree(path: str) -> None:
    def on_error(func, p, _exc):
        # git marks pack files read-only, which Windows refuses to delete.
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(path, onerror=on_error)


# --- Reporting ----------------------------------------------------------------

_STATUS_LABEL = {
    PASSED: "Passed",
    FAILED: "Failed",
    ERROR: "Error",
    SKIPPED: "Skipped",
}


def _fence(text: str) -> str:
    """A code fence longer than any backtick run in ``text``, so test output
    cannot close the block and inject Markdown (or @mentions) into the PR."""
    return "`" * max(3, _longest_backtick_run(text) + 1)


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text)), default=0)


def render_pr_section(result: VerificationResult) -> str:
    """Markdown section summarising ``result`` for the PR body."""
    lines = [
        SECTION_START,
        "### Verification",
        "",
        f"**{_STATUS_LABEL.get(result.status, result.status)}**: {result.summary}",
    ]
    details = []
    if result.command:
        command = " ".join(result.command)
        tick = "`" * (_longest_backtick_run(command) + 1)
        details.append(f"- Command: {tick} {command} {tick}")
    if result.runner:
        details.append(f"- Runner: {result.runner}")
    if result.duration_seconds:
        details.append(f"- Duration: {result.duration_seconds:.1f}s")
    if result.head_sha:
        details.append(f"- Commit: {result.head_sha}")
    if details:
        lines += [""] + details
    if result.output_tail:
        tail = result.output_tail[-PR_OUTPUT_TAIL_CHARS:]
        cut = result.truncated or len(result.output_tail) > len(tail)
        fence = _fence(tail)
        lines += [
            "",
            "<details><summary>Output"
            + (" (last lines only)" if cut else "")
            + "</summary>",
            "",
            fence,
            tail.rstrip(),
            fence,
            "",
            "</details>",
        ]
    if result.status == PASSED:
        lines += [
            "",
            "_Passing tests are evidence, not proof: they cover only what the "
            "repository's tests exercise._",
        ]
    lines.append(SECTION_END)
    return "\n".join(lines)


def upsert_pr_section(body, section: str) -> str:
    """``body`` with its verification section replaced by ``section`` (or
    ``section`` appended when the body has none yet)."""
    body = body if isinstance(body, str) else ""
    start, end = body.find(SECTION_START), body.find(SECTION_END)
    if start != -1 and end > start:
        return body[:start] + section + body[end + len(SECTION_END) :]
    return (body.rstrip() + "\n\n" + section) if body.strip() else section


def commit_status(result: VerificationResult) -> tuple[str, str]:
    """(state, description) for a GitHub commit status on the verified head.

    Only ``passed`` maps to success; error and skipped fail closed, so a
    ``MERGE_POLICY=ci`` gate that requires every check to pass never merges
    an unverified head.
    """
    state = {PASSED: "success", FAILED: "failure"}.get(result.status, "error")
    description = f"{_STATUS_LABEL.get(result.status, result.status)}: {result.summary}"
    # GitHub rejects descriptions longer than 140 characters.
    return state, description[:140]
