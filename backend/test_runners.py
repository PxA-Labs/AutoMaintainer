import os
import shutil
import subprocess
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from runners import (
    BaseRunner,
    LocalRunner,
    SandboxRunner,
    SandboxBackend,
    PathTraversalError,
    CommandTimeoutError,
    WorkspaceError,
    RunnerError,
)


@pytest.fixture
def temp_workspace(tmp_path):
    """Creates an isolated git workspace for runner testing."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "runner@automaintainer.ai"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # Initial commit
    init_file = repo_dir / "README.md"
    init_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    return repo_dir


# --- BaseRunner Tests ---


def test_base_runner_cannot_be_instantiated():
    """Verify BaseRunner is an abstract class and cannot be instantiated."""
    with pytest.raises(TypeError):
        BaseRunner("/tmp")


# --- LocalRunner Tests ---


def test_local_runner_invalid_workspace(tmp_path):
    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(WorkspaceError):
        LocalRunner(non_existent)


@pytest.mark.asyncio
async def test_local_runner_file_operations(temp_workspace):
    runner = LocalRunner(temp_workspace)

    # Write file
    await runner.write_file("src/app.py", "print('hello world')")
    assert (temp_workspace / "src" / "app.py").exists()

    # Read file
    content = await runner.read_file("src/app.py")
    assert content == "print('hello world')"

    # List files
    files = await runner.list_files()
    assert "src/app.py" in files
    assert "README.md" in files

    # List files with pattern
    py_files = await runner.list_files(pattern="*.py")
    assert "src/app.py" in py_files
    assert "README.md" not in py_files

    # Delete file
    await runner.delete_file("src/app.py")
    assert not (temp_workspace / "src" / "app.py").exists()


@pytest.mark.asyncio
async def test_local_runner_path_traversal_rejection(temp_workspace):
    runner = LocalRunner(temp_workspace)

    # Test various traversal patterns
    with pytest.raises(PathTraversalError):
        await runner.read_file("../../etc/passwd")

    with pytest.raises(PathTraversalError):
        await runner.write_file("../escape.txt", "hacked")

    with pytest.raises(PathTraversalError):
        await runner.delete_file("sub/../../outside.txt")


@pytest.mark.asyncio
async def test_local_runner_exec_command(temp_workspace):
    runner = LocalRunner(temp_workspace)

    code, stdout, stderr = await runner.exec_command(["git", "status"])
    assert code == 0
    assert "On branch" in stdout or "HEAD" in stdout


@pytest.mark.asyncio
async def test_local_runner_exec_command_timeout(temp_workspace):
    runner = LocalRunner(temp_workspace)

    # Command taking longer than 1s timeout
    with pytest.raises(CommandTimeoutError):
        await runner.exec_command(
            "python -c \"import time; time.sleep(3)\"", timeout=1
        )


@pytest.mark.asyncio
async def test_local_runner_git_diff(temp_workspace):
    runner = LocalRunner(temp_workspace)
    await runner.write_file("README.md", "# Modified Title\n")

    diff = await runner.get_git_diff()
    assert "-# Test Repo" in diff
    assert "+# Modified Title" in diff


@pytest.mark.asyncio
async def test_local_runner_dry_run_safety(temp_workspace, monkeypatch):
    """
    CRITICAL: Verify dry_run=True creates an isolated patch branch and NEVER calls git push.
    """
    runner = LocalRunner(temp_workspace, dry_run=True)

    await runner.write_file("feature.py", "# new feature\n")
    commit_res = await runner.commit_and_push(
        branch="feature/test", message="Add feature"
    )

    # Result should indicate dry-run
    assert commit_res.startswith("dry-run:")
    assert runner.active_patch_branch is not None
    assert runner.active_patch_branch.startswith("automaintainer/patch-")

    # Verify commit exists on the patch branch locally
    code, stdout, _ = await runner.exec_command(["git", "branch", "--show-current"])
    assert stdout.strip() == runner.active_patch_branch


@pytest.mark.asyncio
async def test_local_runner_non_dry_run_pushes(temp_workspace, monkeypatch):
    """Verify dry_run=False attempts remote push."""
    runner = LocalRunner(temp_workspace, dry_run=False)

    await runner.write_file("prod.py", "# prod code\n")

    # Mock exec_command for the git push step
    original_exec = runner.exec_command
    push_called = False

    async def mock_exec(cmd, timeout=60):
        nonlocal push_called
        if (
            isinstance(cmd, list)
            and len(cmd) >= 2
            and cmd[0] == "git"
            and cmd[1] == "push"
        ):
            push_called = True
            return (0, "Push successful", "")
        return await original_exec(cmd, timeout)

    monkeypatch.setattr(runner, "exec_command", mock_exec)

    res = await runner.commit_and_push(branch="main", message="Production commit")
    assert push_called is True
    assert not res.startswith("dry-run:")


# --- SandboxRunner Tests ---


@pytest.mark.asyncio
async def test_sandbox_runner_unconfigured_backend():
    runner = SandboxRunner()
    with pytest.raises(RunnerError):
        await runner.read_file("test.txt")


@pytest.mark.asyncio
async def test_sandbox_runner_delegation_to_mock_backend():
    mock_backend = MagicMock(spec=SandboxBackend)
    mock_backend.read_file = AsyncMock(return_value="sandbox content")
    mock_backend.write_file = AsyncMock()
    mock_backend.delete_file = AsyncMock()
    mock_backend.list_files = AsyncMock(return_value=["app.py"])
    mock_backend.exec_command = AsyncMock(return_value=(0, "ok", ""))
    mock_backend.get_git_diff = AsyncMock(return_value="diff")
    mock_backend.commit_and_push = AsyncMock(return_value="sandbox-sha-123")

    runner = SandboxRunner(repo_path="/workspace", backend=mock_backend, dry_run=True)

    # Test all primitives delegate to mock backend
    assert await runner.read_file("app.py") == "sandbox content"
    mock_backend.read_file.assert_awaited_once_with("app.py")

    await runner.write_file("app.py", "new content")
    mock_backend.write_file.assert_awaited_once_with("app.py", "new content")

    await runner.delete_file("app.py")
    mock_backend.delete_file.assert_awaited_once_with("app.py")

    assert await runner.list_files("*.py") == ["app.py"]
    mock_backend.list_files.assert_awaited_once_with("*.py")

    assert await runner.exec_command("ls -la") == (0, "ok", "")
    mock_backend.exec_command.assert_awaited_once_with("ls -la", timeout=60)

    assert await runner.get_git_diff() == "diff"
    mock_backend.get_git_diff.assert_awaited_once()

    commit_res = await runner.commit_and_push(branch="main", message="test commit")
    assert commit_res == "sandbox-sha-123"
    mock_backend.commit_and_push.assert_awaited_once_with(
        branch="main", message="test commit", dry_run=True
    )
