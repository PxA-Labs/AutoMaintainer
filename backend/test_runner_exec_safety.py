"""LocalRunner command execution safety (#405).

Commands run from a target repository must not be interpreted by a shell and
must not see the backend's secrets.
"""

import inspect
import json
import sys

import pytest

from runners import LocalRunner, RunnerError
from runners.base import DEFAULT_COMMAND_TIMEOUT
from runners.local import child_environment

PY = sys.executable
PRINT_ARGV = "import sys, json; print(json.dumps(sys.argv[1:]))"
DUMP_ENV = "import os, json; print(json.dumps(sorted(os.environ)))"


@pytest.fixture
def runner(tmp_path):
    return LocalRunner(tmp_path)


def _quoted(path: str) -> str:
    # Double quotes keep Windows backslashes literal under POSIX shlex rules.
    return f'"{path}"'


@pytest.mark.asyncio
async def test_string_command_metacharacters_are_not_interpreted(runner, tmp_path):
    marker = tmp_path / "pwned"
    create_marker = f"open(r'{marker}', 'w').close()"
    cmd = (
        f'{_quoted(PY)} -c "{PRINT_ARGV}" '
        f'"a; b" $(whoami) `whoami` '
        f'&& {_quoted(PY)} -c "{create_marker}" '
        f'; {_quoted(PY)} -c "{create_marker}"'
    )

    code, out, err = await runner.exec_command(cmd)

    assert code == 0, err
    args = json.loads(out)
    # Every metacharacter reached the program as a literal argument.
    assert args[:4] == ["a; b", "$(whoami)", "`whoami`", "&&"]
    assert ";" in args
    # ...and no second command ran.
    assert not marker.exists()


@pytest.mark.asyncio
async def test_list_command_arguments_are_passed_verbatim(runner):
    payload = ["$(id)", "; rm -rf /", "| cat", "> out.txt"]
    code, out, _ = await runner.exec_command([PY, "-c", PRINT_ARGV, *payload])
    assert code == 0
    assert json.loads(out) == payload


@pytest.mark.asyncio
async def test_child_env_contains_no_secrets(runner, monkeypatch):
    secrets = {
        "SUPABASE_SERVICE_KEY": "s1",
        "SUPABASE_URL": "https://x.supabase.co",
        "GITHUB_TOKEN": "s2",
        "GITHUB_APP_PRIVATE_KEY": "s3",
        "GROQ_API_KEY": "s4",
        "OPENAI_API_KEY": "s5",
        "SOME_SERVICE_SECRET": "s6",
        "REDIS_URL": "redis://:pw@host",
        "DATABASE_PASSWORD": "s7",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    code, out, err = await runner.exec_command([PY, "-c", DUMP_ENV])

    assert code == 0, err
    child_names = {n.upper() for n in json.loads(out)}
    assert not child_names & set(secrets)
    assert "PATH" in child_names


def test_child_environment_is_allowlisted_and_drops_secret_names():
    env = child_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/app",
            "LANG": "C.UTF-8",
            "SUPABASE_SERVICE_ROLE_KEY": "x",
            "GITHUB_TOKEN": "x",
            "GROQ_API_KEY": "x",
            "CELERY_BROKER_URL": "redis://secret@host",
            "ARBITRARY": "x",
        }
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/app",
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
    }


@pytest.mark.parametrize("bad", ["", "   ", [], ["git", 1], 'echo "unclosed'])
@pytest.mark.asyncio
async def test_invalid_commands_are_rejected(runner, bad):
    with pytest.raises(RunnerError):
        await runner.exec_command(bad)


def test_exec_command_has_a_default_timeout():
    sig = inspect.signature(LocalRunner.exec_command)
    assert sig.parameters["timeout"].default == DEFAULT_COMMAND_TIMEOUT
    assert DEFAULT_COMMAND_TIMEOUT > 0


@pytest.mark.parametrize("timeout", [None, 0, -5])
@pytest.mark.asyncio
async def test_missing_or_non_positive_timeout_uses_default(runner, timeout):
    code, out, _ = await runner.exec_command([PY, "-c", "print('ok')"], timeout=timeout)
    assert code == 0 and out.strip() == "ok"
