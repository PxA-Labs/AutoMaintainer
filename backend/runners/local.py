"""
LocalRunner adapter for executing operations directly against a local workspace.
"""

import asyncio
import fnmatch
import os
import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .base import (
    BaseRunner,
    CommandTimeoutError,
    PathTraversalError,
    RunnerError,
    WorkspaceError,
)


class LocalRunner(BaseRunner):
    """
    Adapter for local repository execution with path validation and dry-run isolation.
    """

    def __init__(
        self,
        repo_path: Union[str, Path],
        dry_run: bool = False,
        patch_branch_prefix: str = "automaintainer/patch",
    ):
        super().__init__(repo_path, dry_run=dry_run)
        self.patch_branch_prefix = patch_branch_prefix
        self.active_patch_branch: Optional[str] = None
        self._validate_workspace()

    def _validate_workspace(self) -> None:
        """Validates that the target directory exists and is a directory."""
        if not self.repo_path.exists():
            raise WorkspaceError(f"Workspace path does not exist: {self.repo_path}")
        if not self.repo_path.is_dir():
            raise WorkspaceError(f"Workspace path is not a directory: {self.repo_path}")

    def get_safe_target_path(self, rel_path: str) -> Path:
        """
        Validates and resolves a relative path to ensure it stays strictly within repo_path.
        Rejects directory traversal (e.g. '../', absolute paths, escaping symlinks).
        """
        clean_path = rel_path.lstrip("/").lstrip("\\")
        # Check components for path traversal
        parts = clean_path.replace("\\", "/").split("/")
        if ".." in parts:
            raise PathTraversalError(f"Path traversal detected: {rel_path}")

        target_path = (self.repo_path / clean_path).resolve()
        try:
            # Check containment
            if not target_path.is_relative_to(self.repo_path):
                raise PathTraversalError(
                    f"Path escapes repository boundary: {rel_path}"
                )
        except AttributeError:
            try:
                target_path.relative_to(self.repo_path)
            except ValueError:
                raise PathTraversalError(
                    f"Path escapes repository boundary: {rel_path}"
                )
        return target_path

    async def read_file(self, rel_path: str) -> str:
        """Reads file content safely from local workspace."""
        target_path = self.get_safe_target_path(rel_path)
        if not target_path.exists() or not target_path.is_file():
            raise RunnerError(f"File not found: {rel_path}")

        try:
            return target_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise RunnerError(f"Cannot read binary or non-utf-8 file: {rel_path}") from e
        except Exception as e:
            raise RunnerError(f"Failed to read file {rel_path}: {e}") from e

    async def write_file(self, rel_path: str, content: str) -> None:
        """Writes file content safely to local workspace."""
        target_path = self.get_safe_target_path(rel_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        except Exception as e:
            raise RunnerError(f"Failed to write file {rel_path}: {e}") from e

    async def delete_file(self, rel_path: str) -> None:
        """Deletes a file or directory safely."""
        target_path = self.get_safe_target_path(rel_path)
        if not target_path.exists():
            return
        try:
            if target_path.is_file() or target_path.is_symlink():
                target_path.unlink()
            elif target_path.is_dir():
                shutil.rmtree(target_path)
        except Exception as e:
            raise RunnerError(f"Failed to delete {rel_path}: {e}") from e

    async def list_files(self, pattern: Optional[str] = None) -> List[str]:
        """Lists files relative to repository root with ignore filtering."""
        ignored_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            "venv",
            ".venv",
            "env",
            "build",
            "dist",
            ".next",
        }
        results: List[str] = []
        try:
            for root, dirs, files in os.walk(self.repo_path):
                # Filter out ignored directories
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in ignored_dirs
                    and not os.path.islink(os.path.join(root, d))
                ]
                for file in files:
                    full_path = Path(root) / file
                    if os.path.islink(full_path):
                        continue
                    rel_posix = full_path.relative_to(self.repo_path).as_posix()
                    if pattern:
                        if fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(
                            file, pattern
                        ):
                            results.append(rel_posix)
                    else:
                        results.append(rel_posix)
        except Exception as e:
            raise RunnerError(f"Failed to list files: {e}") from e

        results.sort()
        return results

    async def exec_command(
        self, cmd: Union[str, List[str]], timeout: int = 60
    ) -> Tuple[int, str, str]:
        """
        Executes a command asynchronously in the workspace directory.
        """
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            use_shell = False
            program = cmd[0]
            args = cmd[1:]
        else:
            cmd_str = cmd
            use_shell = True

        try:
            if use_shell:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str,
                    cwd=str(self.repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    program,
                    *args,
                    cwd=str(self.repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            return (proc.returncode or 0, stdout, stderr)

        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise CommandTimeoutError(
                f"Command '{cmd_str}' timed out after {timeout} seconds"
            )
        except Exception as e:
            raise RunnerError(f"Failed to execute command '{cmd_str}': {e}") from e

    async def init_dry_run_patch_branch(self) -> str:
        """
        Creates and checks out an isolated local patch branch for dry-run isolation.
        Preserves original working state without touching main/working branch.
        """
        branch_name = f"{self.patch_branch_prefix}-{uuid.uuid4().hex[:8]}"
        code, out, err = await self.exec_command(["git", "checkout", "-b", branch_name])
        if code != 0:
            raise RunnerError(f"Failed to create isolated patch branch: {err or out}")
        self.active_patch_branch = branch_name
        return branch_name

    async def get_git_diff(self) -> str:
        """Retrieves git diff for staged and unstaged changes."""
        code, out, err = await self.exec_command(["git", "diff", "HEAD"])
        if code != 0:
            code, out, err = await self.exec_command(["git", "diff"])
        return out

    async def commit_and_push(self, branch: str, message: str) -> str:
        """
        Commits changes. If dry_run=True, isolates to local patch branch and prevents push.
        If dry_run=False, pushes commit to remote.
        """
        if self.dry_run:
            if not self.active_patch_branch:
                await self.init_dry_run_patch_branch()

        # Stage all changes
        code, out, err = await self.exec_command(["git", "add", "-A"])
        if code != 0:
            raise RunnerError(f"git add failed: {err or out}")

        # Commit
        code, out, err = await self.exec_command(["git", "commit", "-m", message])
        if code != 0 and "nothing to commit" not in (out + err):
            raise RunnerError(f"git commit failed: {err or out}")

        # Retrieve commit SHA
        code, sha, _ = await self.exec_command(["git", "rev-parse", "HEAD"])
        commit_sha = sha.strip() if code == 0 else "dry-run-commit"

        if self.dry_run:
            # STRICT DRY-RUN SAFETY: remote push is skipped completely
            return f"dry-run:{commit_sha}"

        # Real push when dry_run=False
        code, out, err = await self.exec_command(["git", "push", "origin", branch])
        if code != 0:
            raise RunnerError(f"git push failed: {err or out}")

        return commit_sha
