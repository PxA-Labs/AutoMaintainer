"""
SandboxRunner adapter for executing operations in an isolated sandbox or container.
"""

import importlib
import os
from pathlib import Path
from typing import (
    Callable,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    runtime_checkable,
)

from .base import BaseRunner, RunnerError


@runtime_checkable
class SandboxBackend(Protocol):
    """
    Protocol defining the execution boundary between AutoMaintainer core engine
    and arbitrary sandbox providers (Docker, Firecracker, gVisor, cloud micro-VMs).
    """

    async def read_file(self, rel_path: str) -> str: ...

    async def write_file(self, rel_path: str, content: str) -> None: ...

    async def delete_file(self, rel_path: str) -> None: ...

    async def list_files(self, pattern: Optional[str] = None) -> List[str]: ...

    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]: ...

    async def get_git_diff(self) -> str: ...

    async def commit_and_push(
        self, branch: str, message: str, dry_run: bool
    ) -> str: ...


class SandboxRunner(BaseRunner):
    """
    Runner adapter for sandboxed / containerized execution.
    Delegates all primitives through the SandboxBackend interface.
    """

    def __init__(
        self,
        repo_path: Union[str, Path] = "/workspace",
        backend: Optional[SandboxBackend] = None,
        dry_run: bool = False,
    ):
        super().__init__(repo_path, dry_run=dry_run)
        self.backend = backend

    def _ensure_backend(self) -> SandboxBackend:
        if self.backend is None:
            raise RunnerError(
                "SandboxBackend is not configured. Provide an active backend adapter or test mock."
            )
        return self.backend

    async def read_file(self, rel_path: str) -> str:
        backend = self._ensure_backend()
        return await backend.read_file(rel_path)

    async def write_file(self, rel_path: str, content: str) -> None:
        backend = self._ensure_backend()
        await backend.write_file(rel_path, content)

    async def delete_file(self, rel_path: str) -> None:
        backend = self._ensure_backend()
        await backend.delete_file(rel_path)

    async def list_files(self, pattern: Optional[str] = None) -> List[str]:
        backend = self._ensure_backend()
        return await backend.list_files(pattern)

    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]:
        backend = self._ensure_backend()
        return await backend.exec_command(cmd, timeout=timeout)

    async def get_git_diff(self) -> str:
        backend = self._ensure_backend()
        return await backend.get_git_diff()

    async def commit_and_push(self, branch: str, message: str) -> str:
        backend = self._ensure_backend()
        return await backend.commit_and_push(
            branch=branch, message=message, dry_run=self.dry_run
        )


# Operator setting naming the sandbox provider, as "package.module:factory".
# The factory is called with the host path of a working copy and returns a
# SandboxBackend that executes commands against that copy in isolation.
SANDBOX_BACKEND_ENV = "SANDBOX_BACKEND"

SandboxBackendFactory = Callable[[Path], SandboxBackend]


def load_sandbox_backend_factory() -> Optional[SandboxBackendFactory]:
    """Return the factory named by ``SANDBOX_BACKEND``, or None when unset.

    A set but unusable value raises RunnerError instead of returning None:
    callers that need isolation must not silently fall back to running
    untrusted code on the host because of a typo in the setting.
    """
    spec = (os.getenv(SANDBOX_BACKEND_ENV) or "").strip()
    if not spec:
        return None
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise RunnerError(
            f"{SANDBOX_BACKEND_ENV} must look like 'package.module:factory', got {spec!r}"
        )
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise RunnerError(f"Cannot load {SANDBOX_BACKEND_ENV}={spec!r}: {e}") from e
    if not callable(factory):
        raise RunnerError(f"{SANDBOX_BACKEND_ENV}={spec!r} is not callable")
    return factory
