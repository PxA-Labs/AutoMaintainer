"""Tests for automaintainer.config.AutoMaintainerConfig (#183, epic #157).

Hermetic: from_env always gets an explicit dict, nothing touches the network,
and the runtime modules are only read as text (the parity test), never
imported.
"""

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

BACKEND = Path(__file__).resolve().parent.parent
# The engine runtime modules (agents, verification, ...) live here (#187).
RUNTIME = BACKEND / "automaintainer" / "_runtime"
sys.path.insert(0, str(BACKEND))

from automaintainer import AutoMaintainerConfig  # noqa: E402
from automaintainer import config as config_module  # noqa: E402
from automaintainer.config import (  # noqa: E402
    COVERED_ENV_VARS,
    ENV_VARS,
    GROQ_KEY_POOL_ENV,
    ModelRouting,
    SecretProvider,
)

GROQ = "gsk_test_SECRET_groq_value"
PAT = "ghp_test_SECRET_pat_value"
APP_ID = "424242"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nSECRET_PEM_BODY\n-----END RSA PRIVATE KEY-----"
SECRETS = (GROQ, PAT, PEM, "SECRET_PEM_BODY")


def make(**overrides):
    values = {"groq_api_keys": [GROQ]}
    values.update(overrides)
    return AutoMaintainerConfig(**values)


def absolute(name: str) -> str:
    return os.path.abspath(name)


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# --- Defaults ---------------------------------------------------------------------


def test_defaults_match_the_runtime():
    config = make()

    assert config.github_token is None
    assert config.github_app_id is None
    assert config.github_private_key is None
    assert config.github_private_key_path is None
    assert config.github_auth_mode is None
    assert [k.get_secret_value() for k in config.groq_api_keys] == [GROQ]
    assert config.llm_provider == "groq"
    assert config.groq_requests_per_minute == 30
    assert config.groq_tokens_per_minute == 6000
    assert config.runner_type == "sandbox"
    assert config.sandbox_backend is None
    assert config.workspace_dir is None
    assert config.dry_run is False
    assert config.max_iterations == 3
    assert config.timeout_seconds == 300
    assert config.max_timeout_seconds == 900
    assert config.git_timeout_seconds == 300.0
    assert config.verify_changes is True
    assert config.merge_policy == "human"
    assert config.enable_hitl is True
    assert config.change_max_files == 10
    assert config.change_max_lines == 500
    assert config.forbidden_paths is None
    assert config.implementer_max_file_chars == 60000
    assert config.implementer_min_size_ratio == 0.4
    assert config.implementer_shrink_guard_min_bytes == 2048


def test_default_model_routing_is_what_the_agents_use():
    routing = make().model_routing

    for role in (
        "architect",
        "brainstormer",
        "pm",
        "implementer",
        "maintainer",
        "inline_assist",
    ):
        assert routing.model_for(role) == "llama-3.3-70b-versatile"
    assert routing.fallback == "llama-3.1-8b-instant"


def test_default_models_appear_in_the_runtime_source():
    """Drift guard: the defaults are the literals agents.py passes to ChatGroq."""
    agents = (RUNTIME / "agents.py").read_text(encoding="utf-8")
    assert f'model="{config_module.DEFAULT_MODEL}"' in agents
    assert f'ChatGroq(model="{config_module.DEFAULT_FALLBACK_MODEL}"' in agents
    assert (
        f'state.get("iteration", 0) >= {config_module.DEFAULT_MAX_ITERATIONS}' in agents
    )


def test_merge_policies_match_merge_policy_module():
    source = (RUNTIME / "merge_policy.py").read_text(encoding="utf-8")
    quoted = ", ".join(f'"{p}"' for p in config_module.MERGE_POLICIES)
    assert f"POLICIES = ({quoted})" in source
    assert 'DEFAULT_POLICY = "human"' in source


def test_unknown_role_is_refused():
    with pytest.raises(ValueError, match="Unknown agent role"):
        make().model_routing.model_for("fallback")
    with pytest.raises(ValueError, match="Unknown agent role"):
        make().model_routing.model_for("reviewer")


# --- Overrides and immutability ------------------------------------------------------


def test_explicit_overrides():
    workspace = absolute("ws")
    config = make(
        github_token=PAT,
        merge_policy="CI",
        runner_type="local",
        workspace_dir=workspace,
        dry_run=True,
        max_iterations=5,
        forbidden_paths=[" *.secret ", "infra/**"],
        model_routing={"implementer": "llama-3.1-8b-instant", "fallback": None},
    )

    assert config.github_token.get_secret_value() == PAT
    assert config.merge_policy == "ci"
    assert config.enable_hitl is False
    assert config.runner_type == "local"
    assert config.workspace_dir == Path(workspace)
    assert config.dry_run is True
    assert config.max_iterations == 5
    assert config.forbidden_paths == ("*.secret", "infra/**")
    assert config.model_routing.implementer == "llama-3.1-8b-instant"
    assert config.model_routing.architect == "llama-3.3-70b-versatile"
    assert config.model_routing.fallback is None


def test_config_is_frozen():
    config = make()
    with pytest.raises(ValidationError):
        config.merge_policy = "auto"
    with pytest.raises(ValidationError):
        config.model_routing.implementer = "other"
    assert isinstance(config.groq_api_keys, tuple)


def test_blank_credentials_count_as_unset():
    config = make(github_token="  ", github_app_id="")
    assert config.github_token is None
    assert config.github_app_id is None


def test_github_app_with_key_path():
    config = make(github_app_id=APP_ID, github_private_key_path=absolute("app.pem"))
    assert config.github_app_id.get_secret_value() == APP_ID


# --- from_env ----------------------------------------------------------------------


def test_from_env_reads_only_the_given_mapping(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-process-env")
    monkeypatch.setenv("MERGE_POLICY", "auto")

    config = AutoMaintainerConfig.from_env({"GROQ_API_KEY": GROQ})

    assert [k.get_secret_value() for k in config.groq_api_keys] == [GROQ]
    assert config.merge_policy == "human"


def test_from_env_defaults_to_os_environ(monkeypatch):
    for name in COVERED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", GROQ)
    monkeypatch.setenv("CHANGE_LIMIT_MAX_FILES", "4")

    config = AutoMaintainerConfig.from_env()

    assert config.change_max_files == 4


def test_from_env_maps_every_variable():
    workspace = absolute("workspaces")
    config = AutoMaintainerConfig.from_env(
        {
            "GITHUB_TOKEN": PAT,
            "GITHUB_APP_ID": APP_ID,
            "GITHUB_APP_PRIVATE_KEY": PEM,
            "GITHUB_APP_PRIVATE_KEY_PATH": absolute("unused.pem"),
            "GITHUB_AUTH_MODE": "App",
            "GROQ_API_KEY": GROQ,
            "GROQ_REQUESTS_PER_MINUTE": "15",
            "GROQ_TOKENS_PER_MINUTE": "3000",
            "SANDBOX_BACKEND": "runners.docker_sandbox:create",
            "AUTOMAINTAINER_WORKSPACE_DIR": workspace,
            "VERIFY_TIMEOUT_SECONDS": "120",
            "VERIFY_MAX_TIMEOUT_SECONDS": "600",
            "GIT_TIMEOUT_SECONDS": "45.5",
            "VERIFY_CHANGES": "false",
            "MERGE_POLICY": " AUTO ",
            "CHANGE_LIMIT_MAX_FILES": "3",
            "CHANGE_LIMIT_MAX_LINES": "200",
            "CHANGE_LIMIT_DENYLIST": "*.pem, secrets/** ,",
            "IMPLEMENTER_MAX_FILE_CHARS": "1000",
            "IMPLEMENTER_MIN_SIZE_RATIO": "0.5",
            "IMPLEMENTER_SHRINK_GUARD_MIN_BYTES": "0",
        }
    )

    assert config.github_token.get_secret_value() == PAT
    assert config.github_app_id.get_secret_value() == APP_ID
    assert config.github_private_key.get_secret_value() == PEM
    assert config.github_private_key_path == Path(absolute("unused.pem"))
    assert config.github_auth_mode == "app"
    assert config.groq_requests_per_minute == 15
    assert config.groq_tokens_per_minute == 3000
    assert config.sandbox_backend == "runners.docker_sandbox:create"
    assert config.runner_type == "sandbox"
    assert config.workspace_dir == Path(workspace)
    assert config.timeout_seconds == 120
    assert config.max_timeout_seconds == 600
    assert config.git_timeout_seconds == 45.5
    assert config.verify_changes is False
    assert config.merge_policy == "auto"
    assert config.change_max_files == 3
    assert config.change_max_lines == 200
    assert config.forbidden_paths == ("*.pem", "secrets/**")
    assert config.implementer_max_file_chars == 1000
    assert config.implementer_min_size_ratio == 0.5
    assert config.implementer_shrink_guard_min_bytes == 0


def test_from_env_groq_key_pool_order_and_range():
    """Same names and order as rate_limiter.RateLimitManager.initialize."""
    env = {f"GROQ_API_KEY_{i}": f"k{i}" for i in (19, 2, 1)}
    env.update(
        {"GROQ_API_KEY": "k0", "GROQ_API_KEY_20": "ignored", "GROQ_API_KEY_5": ""}
    )

    config = AutoMaintainerConfig.from_env(env)

    assert [k.get_secret_value() for k in config.groq_api_keys] == [
        "k0",
        "k1",
        "k2",
        "k19",
    ]


def test_key_pool_names_match_rate_limiter():
    source = (RUNTIME / "rate_limiter.py").read_text(encoding="utf-8")
    assert 'os.getenv("GROQ_API_KEY")' in source
    assert "for i in range(1, 20):" in source
    assert GROQ_KEY_POOL_ENV[0] == "GROQ_API_KEY"
    assert GROQ_KEY_POOL_ENV[-1] == "GROQ_API_KEY_19"
    assert len(GROQ_KEY_POOL_ENV) == 20


def test_from_env_blank_values_are_unset():
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "MERGE_POLICY": "  ", "GITHUB_TOKEN": ""}
    )
    assert config.merge_policy == "human"
    assert config.github_token is None


def test_from_env_denylist_of_only_commas_denies_nothing():
    """As change_limits.default_limits: set but no globs = empty denylist."""
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "CHANGE_LIMIT_DENYLIST": " , ,"}
    )
    assert config.forbidden_paths == ()


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "sandbox"),
        ({"VERIFY_ALLOW_LOCAL": "true"}, "local"),
        ({"VERIFY_ALLOW_LOCAL": "off"}, "sandbox"),
        # verification.py prefers a configured sandbox over the host.
        (
            {"VERIFY_ALLOW_LOCAL": "true", "SANDBOX_BACKEND": "pkg.mod:factory"},
            "sandbox",
        ),
    ],
)
def test_from_env_runner_type(env, expected):
    config = AutoMaintainerConfig.from_env({"GROQ_API_KEY": GROQ, **env})
    assert config.runner_type == expected


def test_from_env_invalid_allow_local_is_refused():
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env(
            {"GROQ_API_KEY": GROQ, "VERIFY_ALLOW_LOCAL": "maybe"}
        )
    assert [e["loc"] for e in info.value.errors()] == [("runner_type",)]
    assert "VERIFY_ALLOW_LOCAL must be one of" in str(info.value)
    assert "maybe" not in str(info.value) + info.value.json()


@pytest.mark.parametrize(
    "env, other",
    [
        # A field error (cross-field checks only run once fields are valid).
        ({"GROQ_API_KEY": GROQ, "CHANGE_LIMIT_MAX_FILES": "ten"}, "change_max_files"),
        # A cross-field error.
        ({}, "groq_api_keys"),
    ],
)
def test_from_env_invalid_allow_local_is_reported_with_other_problems(env, other):
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env({"VERIFY_ALLOW_LOCAL": "maybe", **env})
    locs = {e["loc"][0] for e in info.value.errors()}
    assert locs == {"runner_type", other}
    assert_no_secret(str(info.value) + info.value.json())


# --- Boolean flags: exactly the runtime's spellings ------------------------------------


def runtime_flag_spellings() -> tuple[set[str], set[str]]:
    """The true/false spellings of verification._env_flag, read as text."""
    source = (RUNTIME / "verification.py").read_text(encoding="utf-8")
    body = source.split("def _env_flag", 1)[1].split("\ndef ", 1)[0]
    true_part, false_part = re.findall(r"in \(([^)]*)\)", body)[:2]
    return (
        set(re.findall(r'"([^"]*)"', true_part)),
        set(re.findall(r'"([^"]*)"', false_part)),
    )


def test_flag_spellings_match_verification_env_flag():
    assert runtime_flag_spellings() == (
        set(config_module._TRUE),
        set(config_module._FALSE),
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", True),
        ("true", True),
        (" Yes ", True),
        ("ON", True),
        ("0", False),
        ("false", False),
        ("No", False),
        (" off", False),
    ],
)
def test_from_env_verify_changes_runtime_spellings(raw, expected):
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "VERIFY_CHANGES": raw}
    )
    assert config.verify_changes is expected


# verification._env_flag returns None for these, and verification_enabled()
# then keeps verification ON. Pydantic's lax bool would read 'f'/'n' as False
# and silently turn the gate off, so they must be refused instead.
@pytest.mark.parametrize("raw", ["f", "n", "t", "y", "maybe", "2", "disabled"])
def test_from_env_verify_changes_other_spellings_are_refused(raw):
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env({"GROQ_API_KEY": GROQ, "VERIFY_CHANGES": raw})
    errors = info.value.errors()
    assert [e["loc"] for e in errors] == [("verify_changes",)]
    assert "VERIFY_CHANGES" in str(info.value)


@pytest.mark.parametrize("value", ["f", "n", 0, 1, None])
def test_explicit_verify_changes_must_be_a_bool_or_runtime_spelling(value):
    with pytest.raises(ValidationError) as info:
        make(verify_changes=value)
    assert info.value.errors()[0]["loc"] == ("verify_changes",)
    assert make(verify_changes="off").verify_changes is False
    assert make(verify_changes=False).verify_changes is False


def test_explicit_argument_beats_environment():
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "MERGE_POLICY": "auto", "VERIFY_ALLOW_LOCAL": "true"},
        merge_policy="human",
        runner_type="sandbox",
        groq_api_keys=["explicit"],
    )
    assert config.merge_policy == "human"
    assert config.runner_type == "sandbox"
    assert [k.get_secret_value() for k in config.groq_api_keys] == ["explicit"]


def test_secret_provider_is_last_in_the_chain():
    asked = []

    def provider(name):
        asked.append(name)
        return {
            "GITHUB_TOKEN": "from-provider",
            "GROQ_API_KEY_3": "provided-groq",
            "GITHUB_APP_ID": "  ",
        }.get(name)

    assert isinstance(provider, SecretProvider)
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "GITHUB_TOKEN": PAT},
        secret_provider=provider,
    )

    # The environment wins over the provider.
    assert config.github_token.get_secret_value() == PAT
    assert "GITHUB_TOKEN" not in asked
    # The provider fills what the environment lacks, credentials only.
    assert [k.get_secret_value() for k in config.groq_api_keys] == [
        GROQ,
        "provided-groq",
    ]
    assert config.github_app_id is None
    assert "MERGE_POLICY" not in asked
    assert set(asked) <= {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", *GROQ_KEY_POOL_ENV}


def test_secret_provider_not_asked_for_explicit_values():
    def provider(name):
        raise AssertionError(f"provider asked for {name}")

    AutoMaintainerConfig.from_env(
        {},
        secret_provider=provider,
        github_token=PAT,
        github_app_id=None,
        github_private_key=None,
        groq_api_keys=[GROQ],
    )


@pytest.mark.parametrize("returned", [12345, b"bytes", ["list"]])
def test_secret_provider_must_return_str_or_none(returned):
    with pytest.raises(TypeError) as info:
        AutoMaintainerConfig.from_env({}, secret_provider=lambda name: returned)
    message = str(info.value)
    assert "GITHUB_TOKEN" in message
    assert type(returned).__name__ in message
    assert "12345" not in message


def test_secret_provider_exceptions_propagate_unwrapped():
    class VaultDown(Exception):
        pass

    def provider(name):
        raise VaultDown(name)

    with pytest.raises(VaultDown):
        AutoMaintainerConfig.from_env({}, secret_provider=provider)


def test_from_env_unknown_override_is_refused():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AutoMaintainerConfig.from_env({"GROQ_API_KEY": GROQ}, max_cost_limit=5)


# --- Invalid configurations -----------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, loc, message",
    [
        ({"github_app_id": APP_ID}, "github_app_id", "neither github_private_key"),
        (
            {"github_private_key": PEM},
            "github_app_id",
            r"github_app_id \(GITHUB_APP_ID\)",
        ),
        ({"github_auth_mode": "app"}, "github_auth_mode", "needs github_app_id"),
        ({"github_auth_mode": "oauth"}, "github_auth_mode", "'app' or 'pat'"),
        ({"groq_api_keys": []}, "groq_api_keys", "routes the agents to Groq"),
        (
            {"groq_api_keys": [GROQ, " "]},
            "groq_api_keys",
            r"groq_api_keys\[1\] is empty",
        ),
        ({"groq_api_keys": GROQ}, "groq_api_keys", "not one string"),
        ({"workspace_dir": "relative/ws"}, "workspace_dir", "absolute path"),
        (
            {"sandbox_backend": "not a spec"},
            "sandbox_backend",
            "package.module:factory",
        ),
        (
            {"runner_type": "local", "sandbox_backend": "pkg.mod:factory"},
            "runner_type",
            "conflicts with sandbox_backend",
        ),
        ({"runner_type": "docker"}, "runner_type", "'local' or 'sandbox'"),
        ({"merge_policy": "yolo"}, "merge_policy", "'human', 'ci' or 'auto'"),
        ({"timeout_seconds": 1000}, "timeout_seconds", "exceeds max_timeout_seconds"),
        ({"max_iterations": 0}, "max_iterations", "greater than 0"),
        ({"change_max_files": -1}, "change_max_files", "greater than 0"),
        (
            {"implementer_min_size_ratio": 1.5},
            "implementer_min_size_ratio",
            "less than",
        ),
        ({"forbidden_paths": ["ok", " "]}, "forbidden_paths", "non-empty globs"),
        ({"model_routing": {"pm": ""}}, "model_routing", "at least 1 character"),
        ({"model_routing": {"reviewer": "x"}}, "model_routing", "Extra inputs"),
        ({"llm_provider": "openai"}, "llm_provider", "'groq'"),
        ({"max_cost_limit": 10}, "max_cost_limit", "Extra inputs"),
    ],
)
def test_invalid_configurations(overrides, loc, message):
    with pytest.raises(ValidationError) as info:
        make(**overrides)

    errors = info.value.errors()
    assert any(e["loc"][0] == loc for e in errors), errors
    assert re.search(message, str(info.value))


def test_from_env_malformed_number_is_refused():
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env(
            {"GROQ_API_KEY": GROQ, "CHANGE_LIMIT_MAX_FILES": "ten"}
        )
    assert info.value.errors()[0]["loc"] == ("change_max_files",)


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity"])
def test_from_env_non_finite_git_timeout_is_refused(raw):
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env(
            {"GROQ_API_KEY": GROQ, "GIT_TIMEOUT_SECONDS": raw}
        )
    assert [e["loc"] for e in info.value.errors()] == [("git_timeout_seconds",)]


def test_non_finite_ratio_is_refused():
    with pytest.raises(ValidationError):
        make(implementer_min_size_ratio=float("nan"))


def test_from_env_without_any_groq_key_is_refused():
    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        AutoMaintainerConfig.from_env({})


def test_all_problems_are_reported_together():
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig(github_app_id=APP_ID, workspace_dir="rel")
    locs = {e["loc"][0] for e in info.value.errors()}
    assert locs == {"github_app_id", "groq_api_keys", "workspace_dir"}


# --- Secret redaction -------------------------------------------------------------------


def secret_config():
    return make(
        github_token=PAT,
        github_app_id=APP_ID,
        github_private_key=PEM,
        groq_api_keys=[GROQ, GROQ + "_2"],
    )


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda c: repr(c.model_dump()),
        lambda c: json.dumps(c.model_dump(mode="json")),
        lambda c: c.model_dump_json(),
        lambda c: repr(c.groq_api_keys),
    ],
    ids=["repr", "str", "dump", "dump-json-mode", "dump-json", "keys"],
)
def test_secrets_are_redacted(render):
    text = render(secret_config())
    assert_no_secret(text)
    assert APP_ID not in text
    assert "**********" in text


def test_secret_values_are_still_reachable_on_purpose():
    config = secret_config()
    assert config.github_private_key.get_secret_value() == PEM


@pytest.mark.parametrize(
    "kwargs",
    [
        # Cross-field error: its input would be the whole argument dict.
        {"github_token": PAT, "github_private_key": PEM, "groq_api_keys": [GROQ]},
        # Field errors whose input is the secret itself.
        {"github_token": PAT, "groq_api_keys": GROQ},
        {"github_token": 12345, "groq_api_keys": [GROQ]},
        {"github_token": PAT, "groq_api_keys": [GROQ], "unknown": PAT},
        {"github_token": PAT, "groq_api_keys": [GROQ], "merge_policy": PAT},
        {"github_token": PAT, "groq_api_keys": [GROQ, ""]},
    ],
)
def test_validation_errors_never_echo_secrets(kwargs):
    for build in (
        lambda: AutoMaintainerConfig(**kwargs),
        lambda: AutoMaintainerConfig.model_validate(kwargs),
    ):
        with pytest.raises(ValidationError) as info:
            build()
        exc = info.value
        rendered = [str(exc), repr(exc), exc.json(), repr(exc.errors())]
        for text in rendered:
            assert_no_secret(text)
            assert "12345" not in text
        assert all(e["input"] == "[redacted]" for e in exc.errors())


def test_from_env_errors_never_echo_secrets():
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env(
            {"GROQ_API_KEY": GROQ, "GITHUB_APP_PRIVATE_KEY": PEM, "GITHUB_TOKEN": PAT}
        )
    assert_no_secret(str(info.value) + info.value.json())


# --- No side effects at import ------------------------------------------------------------

_IMPORT_PROBE = textwrap.dedent("""
    import json, os, sys

    events = []
    WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def hook(event, args):
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in str(mode) for c in "wax+")) or (flags & WRITE_FLAGS):
                events.append([event, str(path)])
        elif event.startswith("socket.") or event in (
            "os.putenv", "os.unsetenv", "os.mkdir", "os.remove", "os.rename",
            "os.rmdir", "shutil.rmtree", "subprocess.Popen", "os.system",
            "tempfile.mkstemp", "tempfile.mkdtemp",
        ):
            events.append([event, repr(args)[:200]])

    before = dict(os.environ)
    sys.addaudithook(hook)
    import automaintainer
    import automaintainer.config
    sys.stdout.write(json.dumps({
        "events": events,
        "env_changed": dict(os.environ) != before,
        "runtime_imported": sorted(
            m for m in ("agents", "rate_limiter", "github_app", "workspace",
                        "verification", "change_limits", "merge_policy",
                        "tasks", "celery_app", "main", "runners")
            if m in sys.modules
        ) + sorted(
            m for m in sys.modules
            if m.startswith(("automaintainer._runtime", "automaintainer.runners"))
        ),
    }))
    """)


def test_import_has_no_side_effects():
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "SYSTEMROOT", "PYTHONHASHSEED")
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", _IMPORT_PROBE],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

    assert report["events"] == []
    assert report["env_changed"] is False
    # No runtime module either: they read the environment (and load .env
    # files) at import.
    assert report["runtime_imported"] == []


# --- Parity with the environment variables the runtime reads ---------------------------------

# Environment variables the runtime reads today that the engine configuration
# intentionally does not cover, with the reason.
NOT_COVERED = {
    "PATH": "program lookup for child processes, inherited from the host",
    # Persistence and hosting around the engine.
    "SUPABASE_URL": "persistence layer (runs/logs tables), not engine behaviour",
    "SUPABASE_SERVICE_KEY": "persistence layer credential, not engine behaviour",
    "REDIS_URL": "Celery broker, locks and quotas: worker infrastructure",
    "REDIS_SSL_CERT_REQS": "broker TLS: worker infrastructure",
    "CELERY_BROKER_POOL_LIMIT": "broker tuning: worker infrastructure",
    "REPO_LOCK_TTL_SECONDS": "per-repo run lock of the Celery deployment",
    "WORKER_PING_TIMEOUT_SECONDS": "API /start probe of the workers",
    "WORKER_PING_CACHE_SECONDS": "API /start probe of the workers",
    "AGENT_WORKER_CHECK": "API /start probe of the workers",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "observability exporter of the API process",
    "OTEL_CLOUD_TRACE_ENABLED": "Cloud Trace exporter switch of the API process",
    "GOOGLE_CLOUD_PROJECT": "project the Cloud Trace exporter writes to",
    # API server, authentication and dashboard.
    "ENVIRONMENT": "deployment-wide production gates of the API",
    "AUTH_DEV_BYPASS": "API authentication (local development only)",
    "AUTH_DEV_USER_ID": "API authentication (local development only)",
    "AUTH_DEV_ORG_ID": "API authentication (local development only)",
    "AUTH_DEV_ROLE": "API authentication (local development only)",
    "ALLOWED_ORIGINS": "API CORS",
    "CORS_ORIGINS": "API CORS (alias of ALLOWED_ORIGINS)",
    "CORS_ORIGIN_REGEX": "API CORS",
    "DASHBOARD_URL": "GitHub App install callback redirect of the API",
    "ENABLE_TERMINAL": "Web IDE terminal of the API",
    "TERMINAL_ADMIN_USER_IDS": "Web IDE terminal of the API",
    "IDE_VIEW_TTL_HOURS": "Web IDE per-user views",
    "IDE_VIEW_MAX": "Web IDE per-user views",
    "ASSIST_RATE_PER_MINUTE": "API quota of /assist/inline",
    "ASSIST_ORG_RATE_PER_MINUTE": "API quota of /assist/inline",
    "ASSIST_DAILY_REQUESTS_PER_ORG": "API quota of /assist/inline",
    # GitHub App webhook and install flow (API), not run credentials.
    "GITHUB_WEBHOOK_SECRET": "verifies inbound webhooks in the API",
    "AUTOMAINTAINER_TRIGGER_LABEL": "which issue label starts a run (webhook processor)",
    "GITHUB_APP_SLUG": "GitHub App install URL of the API",
    "GITHUB_APP_CLIENT_ID": "GitHub App OAuth install flow of the API",
    "GITHUB_APP_CLIENT_SECRET": "GitHub App OAuth install flow of the API",
    # Verification and sandbox internals with their own typed loaders.
    "VERIFY_PYTHON": "interpreter of the detected pytest command (verification.py)",
    "VERIFY_MAX_OUTPUT_BYTES": "output cap of verification and the docker sandbox",
    "SANDBOX_IMAGE": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_NETWORK": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_RUNTIME": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_PIDS_LIMIT": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_MEMORY": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_CPUS": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_TMPFS_SIZE": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_WORKSPACE_SIZE": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_USER": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
    "SANDBOX_DOCKER": "docker sandbox backend setting (DockerSandboxConfig.from_env)",
}

# A literal variable name passed to the ways the runtime reads its settings:
# os.getenv / os.environ[...] / environ.get / env.get, the per-module _env_*
# helpers and docker_sandbox's _setting(env, ...). f-strings are captured up
# to the first placeholder (GROQ_API_KEY_{i} -> GROQ_API_KEY_).
_ENV_READ_RE = re.compile(
    r"(?:getenv|environ\.get|environ\[|\benv\.get|_env_\w+|_setting\(\s*env,)"
    r"\(?\s*f?[\"']([A-Z][A-Z0-9_]*)"
)
# The same reads through a module-level constant, e.g. runners/sandbox.py's
# os.getenv(SANDBOX_BACKEND_ENV) with SANDBOX_BACKEND_ENV = "SANDBOX_BACKEND".
_ENV_READ_CONST_RE = re.compile(
    r"(?:getenv|environ\.get|environ\[|\benv\.get|_env_\w+)\(?\s*([A-Z][A-Z0-9_]*)\b"
)
_STR_CONST_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*)\s*=\s*[\"']([A-Z][A-Z0-9_]*)[\"']", re.M
)


def env_reads_in(source: str) -> set[str]:
    names = set(_ENV_READ_RE.findall(source))
    constants = dict(_STR_CONST_RE.findall(source))
    for const in _ENV_READ_CONST_RE.findall(source):
        if const in constants:
            names.add(constants[const])
    return names


def test_parity_scanner_resolves_named_constants():
    source = 'FOO_ENV = "FOO"\nx = os.getenv(FOO_ENV)\ny = os.getenv("BAR")\n'
    assert env_reads_in(source) == {"FOO", "BAR"}


def runtime_env_vars() -> set[str]:
    """Variables the runtime reads (backend/*.py, automaintainer/_runtime, runners).

    evals/ is not scanned: it is a developer tool (it reads GROQ_API_KEY to
    run the evaluation harness), not part of the engine. A name built at run
    time other than the GROQ_API_KEY_{i} f-string would still be missed.
    """
    names = set()
    sources = (
        list(BACKEND.glob("*.py"))
        + list(RUNTIME.glob("*.py"))
        + list((BACKEND / "automaintainer" / "runners").glob("*.py"))
    )
    for path in sources:
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        names.update(env_reads_in(path.read_text(encoding="utf-8")))
    return names


def test_parity_with_runtime_environment_variables():
    found = runtime_env_vars()
    # The pool's f-string shows up as its prefix.
    assert "GROQ_API_KEY_" in found
    found.discard("GROQ_API_KEY_")

    unaccounted = found - COVERED_ENV_VARS - NOT_COVERED.keys()
    assert not unaccounted, (
        "The runtime reads environment variables the engine config neither "
        f"covers nor lists in NOT_COVERED: {sorted(unaccounted)}"
    )
    # Nothing is both covered and excluded, and both lists are current.
    assert not COVERED_ENV_VARS & NOT_COVERED.keys()
    covered_single = COVERED_ENV_VARS - set(GROQ_KEY_POOL_ENV[1:])
    assert covered_single <= found, sorted(covered_single - found)
    assert set(NOT_COVERED) <= found, sorted(set(NOT_COVERED) - found)


def test_env_vars_name_real_fields():
    fields = set(AutoMaintainerConfig.model_fields)
    assert set(ENV_VARS) <= fields
    # Fields with no environment variable today: configurable only explicitly.
    assert fields - set(ENV_VARS) == {
        "groq_api_keys",  # GROQ_API_KEY pool, handled separately
        "runner_type",  # derived from SANDBOX_BACKEND and VERIFY_ALLOW_LOCAL
        "llm_provider",
        "model_routing",
        "dry_run",
        "max_iterations",
    }


def _graph_node_names(source: str) -> set:
    """Node names of the agent graph, read from agents.py's source.

    Covers both the literal ``workflow.add_node("name", ...)`` calls and the
    ``GRAPH_NODE_NAMES`` tuple that build_graph() loops over.
    """
    nodes = set(re.findall(r'workflow\.add_node\("(\w+)"', source))
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "GRAPH_NODE_NAMES"
                for t in node.targets
            )
            and isinstance(node.value, ast.Tuple)
        ):
            nodes |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return nodes


def test_model_routing_roles_are_the_graph_nodes():
    agents = (RUNTIME / "agents.py").read_text(encoding="utf-8")
    nodes = _graph_node_names(agents)
    assert nodes, "could not find the graph nodes in agents.py"
    roles = set(ModelRouting.model_fields) - {"fallback", "inline_assist"}
    assert roles == nodes


def test_change_limit_defaults_match_change_limits_module():
    source = (RUNTIME / "change_limits.py").read_text(encoding="utf-8")
    defaults = make()
    assert f"DEFAULT_MAX_FILES = {defaults.change_max_files}" in source
    assert f"DEFAULT_MAX_LINES = {defaults.change_max_lines}" in source


def test_secret_type_is_secretstr():
    config = secret_config()
    assert isinstance(config.github_token, SecretStr)
    assert isinstance(config.github_app_id, SecretStr)
    assert isinstance(config.github_private_key, SecretStr)
    assert all(isinstance(k, SecretStr) for k in config.groq_api_keys)
