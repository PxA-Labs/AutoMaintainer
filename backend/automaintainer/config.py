"""Typed, immutable configuration for the AutoMaintainer engine (#183, epic #157).

``AutoMaintainerConfig`` gathers in one validated object the settings the
engine reads today from scattered ``os.getenv`` calls (agents.py,
rate_limiter.py, github_app.py, workspace.py, verification.py,
change_limits.py, merge_policy.py). It is not wired into the runtime yet:
the runtime keeps reading the environment itself until the engine refactor
(#186) passes a config object through instead.

Every value is resolved in this order:

1. an explicit argument (``AutoMaintainerConfig(...)`` or the keyword
   overrides of ``from_env``);
2. the environment variable the runtime reads today (``from_env``), from the
   mapping it is given, never from anything else;
3. for credentials only, an optional ``SecretProvider``: any callable that
   takes the environment variable name and returns the value or None
   (a vault lookup, a file-based secret store, a test stub);
4. the default, which equals the runtime's current behaviour.

Credentials are ``SecretStr``: ``repr``, ``str``, ``model_dump(mode="json")``
and ``model_dump_json`` show ``**********``, and validation errors never
contain input values (see ``_redacted``).

Importing this module has no side effects: no network, no file access, no
environment reads or writes. In particular the default ``workspace_dir`` is
None ("the runtime default, ``<tmp>/automaintainer``") rather than a path
computed with ``tempfile.gettempdir()``, which probes the disk.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

__all__ = [
    "AutoMaintainerConfig",
    "ModelRouting",
    "SecretProvider",
    "ENV_VARS",
    "GROQ_KEY_POOL_ENV",
    "COVERED_ENV_VARS",
]

# --- Runtime defaults -----------------------------------------------------------
# Each mirrors a value in the runtime module named next to it. The parity tests
# (tests/test_engine_config.py) compare them so the two cannot drift silently.

# agents.py: run_llm, run_llm_with_tools and stream_inline_assist.
DEFAULT_MODEL = "llama-3.3-70b-versatile"
# agents.py: the silent ChatGroq fallback on the tool path.
DEFAULT_FALLBACK_MODEL = "llama-3.1-8b-instant"
# agents.should_iterate: the Implementer/Maintainer loop stops at iteration 3.
DEFAULT_MAX_ITERATIONS = 3
# merge_policy.POLICIES, strictest first, and merge_policy.DEFAULT_POLICY.
MERGE_POLICIES = ("human", "ci", "auto")
# runners: LocalRunner and SandboxRunner.
RUNNER_TYPES = ("local", "sandbox")
# The Maintainer's review (#525): the single-shot LangGraph prompt, or the
# ADK Reviewer specialist (automaintainer/_specialists).
REVIEWER_BACKENDS = ("langgraph", "adk")
# Retrieval over the target repository (#526): retrieval.DEFAULT_TOP_K and
# retrieval.MAX_K (match_code_chunks returns at most 50 rows).
DEFAULT_RAG_TOP_K = 8
MAX_RAG_TOP_K = 50
# What the ``adk`` extra installs and the ADK Reviewer imports.
_ADK_MODULES = ("google.adk", "litellm")

# rate_limiter.RateLimitManager.initialize: GROQ_API_KEY, then GROQ_API_KEY_1
# to GROQ_API_KEY_19, in this order. (agents.get_all_groq_keys stops at _9, but
# every call goes through the rate limiter's pool, so that is the contract.)
GROQ_KEY_POOL_ENV: tuple[str, ...] = ("GROQ_API_KEY",) + tuple(
    f"GROQ_API_KEY_{i}" for i in range(1, 20)
)

# Field name -> the environment variable the runtime reads today.
# runner_type has no variable of its own; from_env derives it from
# SANDBOX_BACKEND and VERIFY_ALLOW_LOCAL as verification.py does.
ENV_VARS: Mapping[str, str] = {
    "github_token": "GITHUB_TOKEN",
    "github_app_id": "GITHUB_APP_ID",
    "github_private_key": "GITHUB_APP_PRIVATE_KEY",
    "github_private_key_path": "GITHUB_APP_PRIVATE_KEY_PATH",
    "github_auth_mode": "GITHUB_AUTH_MODE",
    "groq_requests_per_minute": "GROQ_REQUESTS_PER_MINUTE",
    "groq_tokens_per_minute": "GROQ_TOKENS_PER_MINUTE",
    "sandbox_backend": "SANDBOX_BACKEND",
    "workspace_dir": "AUTOMAINTAINER_WORKSPACE_DIR",
    "timeout_seconds": "VERIFY_TIMEOUT_SECONDS",
    "max_timeout_seconds": "VERIFY_MAX_TIMEOUT_SECONDS",
    "git_timeout_seconds": "GIT_TIMEOUT_SECONDS",
    "verify_changes": "VERIFY_CHANGES",
    "merge_policy": "MERGE_POLICY",
    "change_max_files": "CHANGE_LIMIT_MAX_FILES",
    "change_max_lines": "CHANGE_LIMIT_MAX_LINES",
    "forbidden_paths": "CHANGE_LIMIT_DENYLIST",
    "implementer_max_file_chars": "IMPLEMENTER_MAX_FILE_CHARS",
    "implementer_min_size_ratio": "IMPLEMENTER_MIN_SIZE_RATIO",
    "implementer_shrink_guard_min_bytes": "IMPLEMENTER_SHRINK_GUARD_MIN_BYTES",
    "reviewer_backend": "REVIEWER_BACKEND",
    "rag_retrieval_enabled": "RAG_RETRIEVAL_ENABLED",
    "rag_top_k": "RAG_TOP_K",
}
_ALLOW_LOCAL_ENV = "VERIFY_ALLOW_LOCAL"

# Every environment variable from_env reads.
COVERED_ENV_VARS: frozenset[str] = frozenset(
    {*ENV_VARS.values(), *GROQ_KEY_POOL_ENV, _ALLOW_LOCAL_ENV}
)

# Credentials a SecretProvider is asked for when neither an argument nor the
# environment supplies them (the Groq pool names are asked for as well).
_SECRET_FIELDS = ("github_token", "github_app_id", "github_private_key")

# verification._env_flag: the only spellings of a boolean variable the runtime
# understands. Anything else is "unset" there (VERIFY_CHANGES then keeps
# verification on), so pydantic's lax bool ("f", "n", "t", "y") must not be
# used for these: it would read "f" as False and turn the gate off.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_FLAG_SPELLINGS = ", ".join(sorted(_TRUE | _FALSE))
# runners.sandbox.load_sandbox_backend_factory: "package.module:factory".
_FACTORY_SPEC_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_REDACTED = "[redacted]"
_CONFIG_ERROR = "config_error"


@runtime_checkable
class SecretProvider(Protocol):
    """Looks up a credential by its environment variable name.

    Returns the value (a ``str``), or None when it has none; any other type
    makes ``from_env`` raise ``TypeError``. Exceptions it raises propagate
    unwrapped, so a vault outage surfaces as the vault's own error.
    Implementations must not log the value. No vault client ships with the
    project; this is the hook a deployment plugs one into.
    """

    def __call__(self, name: str) -> Optional[str]: ...


class ModelRouting(BaseModel):
    """Which model each agent role uses.

    The defaults are the models the agents use today: every graph node and
    inline assist call ``llama-3.3-70b-versatile`` on Groq, and the tool
    path falls back to ``llama-3.1-8b-instant`` when a call raises.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    architect: str = Field(DEFAULT_MODEL, min_length=1)
    brainstormer: str = Field(DEFAULT_MODEL, min_length=1)
    pm: str = Field(DEFAULT_MODEL, min_length=1)
    implementer: str = Field(DEFAULT_MODEL, min_length=1)
    maintainer: str = Field(DEFAULT_MODEL, min_length=1)
    inline_assist: str = Field(DEFAULT_MODEL, min_length=1)
    # None disables the fallback model.
    fallback: Optional[str] = Field(DEFAULT_FALLBACK_MODEL, min_length=1)

    def model_for(self, role: str) -> str:
        """The model routed to ``role``; ValueError for an unknown role."""
        if role == "fallback" or role not in type(self).model_fields:
            raise ValueError(f"Unknown agent role {role!r}")
        return getattr(self, role)


def _adk_missing() -> Optional[str]:
    """The first module of the ``adk`` extra that is not installed, or None.

    ``find_spec`` locates a module without importing it (it imports only the
    parent package), so validation stays cheap and runs no ADK code.
    """
    for name in _ADK_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                return name
        except (ImportError, ValueError):
            return name
    return None


def _redacted(
    exc: ValidationError, title: str, extra: tuple[dict, ...] = ()
) -> ValidationError:
    """``exc`` with every input value replaced.

    ``hide_input_in_errors`` only hides inputs from ``str(exc)``;
    ``exc.errors()`` and ``exc.json()`` would still carry them, and the input
    of a model-level error is the whole argument dict, credentials included.
    """
    details = []
    for err in exc.errors(include_url=False, include_input=False):
        if err["type"] == _CONFIG_ERROR:
            # Raised by this module: rebuilt from its already formatted message.
            detail = {
                "type": PydanticCustomError(_CONFIG_ERROR, err["msg"]),
                "loc": err["loc"],
                "input": _REDACTED,
            }
        else:
            detail = {"type": err["type"], "loc": err["loc"], "input": _REDACTED}
            if "ctx" in err:
                detail["ctx"] = err["ctx"]
        details.append(detail)
    details.extend(extra)
    return ValidationError.from_exception_data(title, details, hide_input=True)


def _config_error(loc: str, message: str) -> dict:
    return {
        "type": PydanticCustomError(_CONFIG_ERROR, message),
        "loc": (loc,),
        "input": _REDACTED,
    }


class AutoMaintainerConfig(BaseModel):
    """Engine configuration: credentials, model routing, execution and safety.

    Immutable (``frozen``) and strict about its keys (``extra="forbid"``).
    Build it directly with keyword arguments, or with ``from_env``.

    Derive a variant with ``AutoMaintainerConfig(**{**c.model_dump(), ...})``
    or ``model_validate``, not ``model_copy(update=...)``: pydantic's
    ``model_copy`` skips validation, so it would accept, say, a relative
    ``workspace_dir`` or an empty Groq key pool.

    Not covered on purpose:

    - OpenAI or Anthropic keys: the runtime only constructs ``ChatGroq``
      clients, so such a key could not be used. ``llm_provider`` is the
      extension point for when a second provider exists.
    - A command denylist: runners never use a shell and scrub the child
      environment, but have no per-command denylist to map to.
    - A cost ceiling: the runtime has request and token rate budgets
      (``groq_requests_per_minute``, ``groq_tokens_per_minute``), not a
      cost meter.
    - Hosting settings (Supabase, Redis/Celery, CORS, auth, the GitHub App
      install flow, the Web IDE): they configure the API and workers around
      the engine, not the engine.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
        # "inf"/"nan" parse as floats; a timeout or ratio must be finite.
        allow_inf_nan=False,
    )

    # --- Credentials -----------------------------------------------------------
    # Personal access token, the single-tenant ``pat`` mode (GITHUB_TOKEN).
    github_token: Optional[SecretStr] = None
    github_app_id: Optional[SecretStr] = None
    # PEM text of the App's private key (GITHUB_APP_PRIVATE_KEY).
    github_private_key: Optional[SecretStr] = None
    # A PEM file instead (GITHUB_APP_PRIVATE_KEY_PATH). Not opened here.
    github_private_key_path: Optional[Path] = None
    # None: ``app`` when App credentials are set, else ``pat`` (as today).
    github_auth_mode: Optional[Literal["app", "pat"]] = None
    # The rotation pool, in rate_limiter order. Keys are stripped and a blank
    # one is an error; rate_limiter uses them raw and would count a
    # whitespace-only key as a (useless) key.
    groq_api_keys: tuple[SecretStr, ...] = ()

    # --- LLM -------------------------------------------------------------------
    # The only provider the runtime can call today.
    llm_provider: Literal["groq"] = "groq"
    model_routing: ModelRouting = ModelRouting()
    # Who reviews a pull request in the maintainer node: ``langgraph`` (one
    # prompt, the behaviour before #525) or ``adk``, the read-only ADK
    # Reviewer specialist, which needs the ``adk`` extra. Selecting ``adk``
    # without it is a validation error, never a silent fallback.
    reviewer_backend: Literal["langgraph", "adk"] = "langgraph"
    # Retrieval over the target repository (#526, docs/RAG.md): when on, the
    # PM prompt gets the ``rag_top_k`` code chunks nearest to the issue, and
    # the ADK Reviewer's search_code asks the index when it holds the PR
    # head. Off by default: the query (issue text) is sent to the embedder.
    rag_retrieval_enabled: bool = False
    rag_top_k: int = Field(DEFAULT_RAG_TOP_K, ge=1, le=MAX_RAG_TOP_K)
    groq_requests_per_minute: PositiveInt = 30
    groq_tokens_per_minute: PositiveInt = 6000

    # --- Execution -------------------------------------------------------------
    # Where repository tests run. ``local`` is the explicit opt-in to running
    # untrusted code on the host (VERIFY_ALLOW_LOCAL=true today).
    runner_type: Literal["local", "sandbox"] = "sandbox"
    # Sandbox provider as "package.module:factory" (SANDBOX_BACKEND). Only the
    # syntax is checked; nothing is imported.
    sandbox_backend: Optional[str] = None
    # Clone directory. None: the runtime default, <tmp>/automaintainer.
    workspace_dir: Optional[Path] = None
    # Commit to an isolated local branch and never push (runners' dry_run).
    dry_run: bool = False
    max_iterations: PositiveInt = DEFAULT_MAX_ITERATIONS
    # Default and operator ceiling of a verification test run.
    timeout_seconds: PositiveInt = 300
    max_timeout_seconds: PositiveInt = 900
    git_timeout_seconds: PositiveFloat = 300.0
    # VERIFY_CHANGES: a bool, or one of the runtime's spellings (_TRUE/_FALSE).
    verify_changes: bool = True
    # Deployment ceiling; repositories may only be stricter.
    merge_policy: Literal["human", "ci", "auto"] = "human"

    # --- Safety ----------------------------------------------------------------
    change_max_files: PositiveInt = 10
    change_max_lines: PositiveInt = 500
    # Globs that replace the built-in denylist (change_limits.DEFAULT_DENYLIST).
    # None keeps the built-in list; it is not copied here so the two cannot
    # drift. ``.automaintainer.yml`` is always denied whatever this says.
    forbidden_paths: Optional[tuple[str, ...]] = None
    implementer_max_file_chars: PositiveInt = 60000
    implementer_min_size_ratio: float = Field(0.4, gt=0, le=1)
    implementer_shrink_guard_min_bytes: NonNegativeInt = 2048

    @computed_field  # type: ignore[prop-decorator]
    @property
    def enable_hitl(self) -> bool:
        """Whether a human decides every merge.

        Derived rather than stored: in the runtime, human-in-the-loop *is*
        ``merge_policy="human"``, and a separate flag could contradict it.
        """
        return self.merge_policy == "human"

    # --- Field normalisation ---------------------------------------------------

    @field_validator(
        "github_token", "github_app_id", "github_private_key", mode="before"
    )
    @classmethod
    def _blank_secret_is_unset(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("groq_api_keys", mode="before")
    @classmethod
    def _strip_keys(cls, value: Any) -> Any:
        if isinstance(value, (str, SecretStr)):
            # A lone string would otherwise be split into characters.
            raise PydanticCustomError(
                _CONFIG_ERROR, "groq_api_keys must be a list of keys, not one string"
            )
        if isinstance(value, (list, tuple)):
            keys = []
            for i, key in enumerate(value):
                raw = key.get_secret_value() if isinstance(key, SecretStr) else key
                if isinstance(raw, str):
                    raw = raw.strip()
                    if not raw:
                        raise PydanticCustomError(
                            _CONFIG_ERROR, f"groq_api_keys[{i}] is empty"
                        )
                keys.append(raw)
            return tuple(keys)
        return value

    @field_validator("verify_changes", "rag_retrieval_enabled", mode="before")
    @classmethod
    def _runtime_flag(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            flag = value.strip().lower()
            if flag in _TRUE:
                return True
            if flag in _FALSE:
                return False
        raise PydanticCustomError(
            _CONFIG_ERROR,
            f"{info.field_name} ({ENV_VARS[info.field_name]}) must be a bool or "
            f"one of {_FLAG_SPELLINGS}",
        )

    @field_validator(
        "merge_policy",
        "github_auth_mode",
        "runner_type",
        "reviewer_backend",
        mode="before",
    )
    @classmethod
    def _lowercase(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("forbidden_paths")
    @classmethod
    def _non_empty_globs(cls, value: Optional[tuple[str, ...]]):
        if value is None:
            return value
        globs = tuple(p.strip() for p in value)
        if not all(globs):
            raise ValueError("forbidden_paths entries must be non-empty globs")
        return globs

    # --- Cross-field validation ------------------------------------------------

    @model_validator(mode="wrap")
    @classmethod
    def _validate_redacted(cls, data: Any, handler):
        # A wrap validator (not "after") so every error, field-level or
        # cross-field, leaves with its input values redacted.
        try:
            config = handler(data)
        except ValidationError as exc:
            raise _redacted(exc, cls.__name__) from None
        problems = config._problems()
        if problems:
            raise ValidationError.from_exception_data(
                cls.__name__, problems, hide_input=True
            )
        return config

    def _problems(self) -> list[dict]:
        problems = []
        has_key = self.github_private_key is not None or (
            self.github_private_key_path is not None
        )
        if self.github_app_id is not None and not has_key:
            problems.append(
                _config_error(
                    "github_app_id",
                    "github_app_id is set but neither github_private_key "
                    "(GITHUB_APP_PRIVATE_KEY) nor github_private_key_path "
                    "(GITHUB_APP_PRIVATE_KEY_PATH) is; a GitHub App needs both",
                )
            )
        if has_key and self.github_app_id is None:
            problems.append(
                _config_error(
                    "github_app_id",
                    "a GitHub App private key is set but github_app_id "
                    "(GITHUB_APP_ID) is not; a GitHub App needs both",
                )
            )
        if self.github_auth_mode == "app" and (
            self.github_app_id is None or not has_key
        ):
            problems.append(
                _config_error(
                    "github_auth_mode",
                    "github_auth_mode='app' needs github_app_id and a private key",
                )
            )
        if self.llm_provider == "groq" and not self.groq_api_keys:
            problems.append(
                _config_error(
                    "groq_api_keys",
                    "groq_api_keys is empty but model_routing routes the agents to "
                    "Groq models; set GROQ_API_KEY (and optionally GROQ_API_KEY_1 "
                    "to GROQ_API_KEY_19)",
                )
            )
        if self.workspace_dir is not None and not self.workspace_dir.is_absolute():
            problems.append(
                _config_error(
                    "workspace_dir",
                    "workspace_dir must be an absolute path: the API and the "
                    "workers must agree on it, and a relative path depends on "
                    "each process's working directory",
                )
            )
        if self.sandbox_backend is not None and not _FACTORY_SPEC_RE.match(
            self.sandbox_backend
        ):
            problems.append(
                _config_error(
                    "sandbox_backend",
                    "sandbox_backend must look like 'package.module:factory'",
                )
            )
        if self.runner_type == "local" and self.sandbox_backend is not None:
            problems.append(
                _config_error(
                    "runner_type",
                    "runner_type='local' conflicts with sandbox_backend: the "
                    "runtime always prefers a configured sandbox",
                )
            )
        if self.reviewer_backend == "adk":
            missing = _adk_missing()
            if missing is not None:
                problems.append(
                    _config_error(
                        "reviewer_backend",
                        "reviewer_backend='adk' (REVIEWER_BACKEND) needs the adk "
                        f"extra, but {missing} is not installed; run "
                        "pip install 'automaintainer[adk]' or select 'langgraph'",
                    )
                )
        if self.timeout_seconds > self.max_timeout_seconds:
            problems.append(
                _config_error(
                    "timeout_seconds",
                    "timeout_seconds exceeds max_timeout_seconds",
                )
            )
        return problems

    # --- Construction from the environment -------------------------------------

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        secret_provider: Optional[SecretProvider] = None,
        **overrides: Any,
    ) -> "AutoMaintainerConfig":
        """Build a config from ``environ`` (default ``os.environ``).

        Reads only ``environ`` and only the variables in ``COVERED_ENV_VARS``;
        ``overrides`` win over it, and ``secret_provider`` is asked for
        credentials that neither supplies. Empty or blank variables count as
        unset, as in the runtime. Unlike the runtime, which logs a warning
        and keeps the default for a malformed value (or falls back to
        ``human`` for an unknown MERGE_POLICY), this raises
        ``ValidationError``, listing every problem at once: a typo in a
        safety setting should stop a deployment, not be ignored. Boolean
        variables accept exactly the runtime's spellings (1/true/yes/on,
        0/false/no/off). A ``secret_provider`` returning neither ``str`` nor
        None raises ``TypeError``; its own exceptions propagate unwrapped.
        """
        env = os.environ if environ is None else environ

        def lookup(name: str) -> Optional[str]:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return None
            return raw.strip()

        def secret(name: str) -> Optional[str]:
            value = lookup(name)
            if value is None and secret_provider is not None:
                value = secret_provider(name)
                if value is not None and not isinstance(value, str):
                    # The type only: the value may be a secret.
                    raise TypeError(
                        f"secret_provider returned {type(value).__name__} for "
                        f"{name}; expected str or None"
                    )
                if value is not None and not value.strip():
                    value = None
            return value

        values: dict[str, Any] = {}
        for field, name in ENV_VARS.items():
            if field in overrides:
                continue
            value = secret(name) if field in _SECRET_FIELDS else lookup(name)
            if value is None:
                continue
            if field == "forbidden_paths":
                # As change_limits.default_limits: comma-separated, and a list
                # of only commas means "deny nothing".
                value = tuple(p.strip() for p in value.split(",") if p.strip())
            values[field] = value

        if "groq_api_keys" not in overrides:
            keys = [k for k in map(secret, GROQ_KEY_POOL_ENV) if k is not None]
            if keys:
                values["groq_api_keys"] = keys

        pre_problems: list[dict] = []
        if "runner_type" not in overrides:
            # verification.py: a configured sandbox wins; the host runs tests
            # only with VERIFY_ALLOW_LOCAL=true; otherwise sandbox (which
            # reports an error until a backend is configured).
            allow_local = lookup(_ALLOW_LOCAL_ENV)
            if allow_local is not None and allow_local.lower() not in (_TRUE | _FALSE):
                # Reported with the model's own errors below. Not echoed.
                pre_problems.append(
                    _config_error(
                        "runner_type",
                        f"{_ALLOW_LOCAL_ENV} must be one of {_FLAG_SPELLINGS}",
                    )
                )
            local = allow_local is not None and allow_local.lower() in _TRUE
            sandbox_set = "sandbox_backend" in values or (
                overrides.get("sandbox_backend") is not None
            )
            values["runner_type"] = "local" if local and not sandbox_set else "sandbox"

        values.update(overrides)
        try:
            config = cls(**values)
        except ValidationError as exc:
            if not pre_problems:
                raise
            # Already redacted by _validate_redacted; add ours alongside.
            raise _redacted(exc, cls.__name__, tuple(pre_problems)) from None
        if pre_problems:
            raise ValidationError.from_exception_data(
                cls.__name__, pre_problems, hide_input=True
            )
        return config
