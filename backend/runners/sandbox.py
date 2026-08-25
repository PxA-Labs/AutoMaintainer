"""
SandboxRunner adapter for executing operations in an isolated sandbox or container.
"""

from pathlib import Path
from typing import List, Optional, Protocol, Tuple, Union, runtime_checkable

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
