"""
Unified Runner Interface for AutoMaintainer Core Engine.
Part of Epic #157 (Core Engine Decoupling, Instantiable Runner Abstraction, and SDK Foundation).
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple, Union


class RunnerError(Exception):
    """Base exception for all runner-related errors."""

    pass


class PathTraversalError(RunnerError):
    """Raised when a file path escapes the safe workspace/repository root boundaries."""

    pass


class CommandTimeoutError(RunnerError):
    """Raised when an executed command exceeds its timeout duration."""

    pass


class WorkspaceError(RunnerError):
    """Raised when a workspace or repository is invalid or inaccessible."""

    pass


class BaseRunner(ABC):
    """
    Abstract Base Class defining the unified execution primitives needed
    by the core engine across all environments (Local, Container, Sandbox).
    """

    def __init__(self, repo_path: Union[str, Path], dry_run: bool = False):
        self.repo_path = Path(repo_path).resolve()
        self.dry_run = dry_run

    @abstractmethod
    async def read_file(self, rel_path: str) -> str:
        """
        Read text content from a file relative to the repository root.

        Args:
            rel_path: Relative file path within repository.
        Returns:
            The string content of the file.
        """
        pass

    @abstractmethod
    async def write_file(self, rel_path: str, content: str) -> None:
        """
        Write or overwrite a file with content relative to the repository root.

        Args:
            rel_path: Relative file path within repository.
            content: Text content to write.
        """
        pass

    @abstractmethod
    async def delete_file(self, rel_path: str) -> None:
        """
        Delete a file relative to the repository root.

        Args:
            rel_path: Relative file path within repository.
        """
        pass

    @abstractmethod
    async def list_files(self, pattern: Optional[str] = None) -> List[str]:
        """
        List relative paths of files in the repository, optionally matching a glob pattern.

        Args:
            pattern: Optional glob pattern (e.g. "*.py", "src/**").
        Returns:
            List of relative path strings.
        """
        pass

    @abstractmethod
    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]:
        """
        Execute a shell or subprocess command within the repository workspace.

        Args:
            cmd: Command string or list of argument strings.
            timeout: Maximum execution duration in seconds.
        Returns:
            Tuple of (returncode, stdout, stderr).
        """
        pass

    @abstractmethod
    async def get_git_diff(self) -> str:
        """
        Retrieve git diff for unstaged and staged changes in the workspace.

        Returns:
            Diff output string.
        """
        pass

    @abstractmethod
    async def commit_and_push(self, branch: str, message: str) -> str:
        """
        Commit workspace changes and push to remote repository (bypassed if dry_run=True).

        Args:
            branch: Target branch name.
            message: Commit message.
        Returns:
            Commit hash or reference identifier.
        """
        pass
