"""Size and path limits for an agent change, checked before its PR is opened.

A change that exceeds a limit is not refused: the PR is opened (or kept) as a
draft with the ``needs-human`` label and a PR-body section listing the
violations, and the run never auto-merges it under any ``MERGE_POLICY``. The
agent pipeline (``agents.implementer_node``) applies that; this module only
decides.

Limits come from three places, later ones overriding earlier ones:

1. Built-in defaults (``DEFAULT_MAX_FILES``, ``DEFAULT_MAX_LINES``,
   ``DEFAULT_DENYLIST``).
2. Server settings: ``CHANGE_LIMIT_MAX_FILES``, ``CHANGE_LIMIT_MAX_LINES`` and
   ``CHANGE_LIMIT_DENYLIST`` (comma-separated globs that replace the built-in
   denylist).
3. The ``change_limits`` mapping of ``.automaintainer.yml`` **on the base
   branch** (``max_files``, ``max_lines``, ``deny`` adds globs, ``allow``
   exempts globs from the denylist). The caller must read it from the base
   branch, never from the change, so a change cannot relax its own limits.

``.automaintainer.yml`` itself is always denied; ``allow`` cannot exempt it,
because it holds both these limits and the verification command.

Glob syntax: ``*`` and ``?`` do not cross ``/``; ``**`` matches any number of
directories. A pattern without ``/`` matches the file name in any directory
(``Dockerfile*`` matches ``deploy/Dockerfile.prod``); a pattern with ``/`` is
anchored at the repository root. Matching ignores case, so a denylist entry
also covers case variants on case-insensitive checkouts.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

CONFIG_FILE = ".automaintainer.yml"
CONFIG_KEY = "change_limits"
NEEDS_HUMAN_LABEL = "needs-human"

# The Implementer rewrites one file per iteration, so these leave room for a
# real fix while flagging a sprawling rewrite.
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_LINES = 500
# Files whose change affects CI, the build, dependencies, secrets or this
# tool's own configuration.
DEFAULT_DENYLIST = (
    ".github/workflows/**",
    "Dockerfile*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "uv.lock",
    "package-lock.json",
    "poetry.lock",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    ".env*",
    "*.pem",
    "*.key",
    CONFIG_FILE,
)
# Denied whatever the denylist or allowlist say.
PROTECTED = (CONFIG_FILE,)

SECTION_START = "<!-- automaintainer:change-limits:start -->"
SECTION_END = "<!-- automaintainer:change-limits:end -->"


class ChangeLimitsConfigError(ValueError):
    """The ``change_limits`` settings of ``.automaintainer.yml`` are unusable."""


@dataclass(frozen=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    # Old path of a rename; checked against the denylist too, so moving a
    # workflow file away counts as touching it.
    previous_path: Optional[str] = None


@dataclass(frozen=True)
class ChangeLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_lines: int = DEFAULT_MAX_LINES
    deny: tuple[str, ...] = DEFAULT_DENYLIST
    allow: tuple[str, ...] = ()


# --- Loading ------------------------------------------------------------------


def _env_positive_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    return value


def default_limits() -> ChangeLimits:
    """Built-in defaults overridden by the CHANGE_LIMIT_* server settings."""
    deny = DEFAULT_DENYLIST
    raw_deny = os.getenv("CHANGE_LIMIT_DENYLIST")
    if raw_deny is not None and raw_deny.strip():
        deny = tuple(p.strip() for p in raw_deny.split(",") if p.strip())
    return ChangeLimits(
        max_files=_env_positive_int("CHANGE_LIMIT_MAX_FILES", DEFAULT_MAX_FILES),
        max_lines=_env_positive_int("CHANGE_LIMIT_MAX_LINES", DEFAULT_MAX_LINES),
        deny=deny,
    )


def _positive_int(settings: dict, key: str, default: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ChangeLimitsConfigError(f"{CONFIG_KEY}.{key} must be a positive integer")
    return value


def _globs(settings: dict, key: str) -> tuple[str, ...]:
    value = settings.get(key, [])
    if isinstance(value, str) or not isinstance(value, list):
        raise ChangeLimitsConfigError(f"{CONFIG_KEY}.{key} must be a list of globs")
    if not all(isinstance(p, str) and p.strip() for p in value):
        raise ChangeLimitsConfigError(
            f"{CONFIG_KEY}.{key} entries must be non-empty strings"
        )
    return tuple(p.strip() for p in value)


def limits_from_config(
    config_text: Optional[str], defaults: Optional[ChangeLimits] = None
) -> ChangeLimits:
    """Limits for a repository whose base-branch ``.automaintainer.yml`` has
    ``config_text`` (None when the file does not exist).

    Raises ChangeLimitsConfigError for an unusable file, so the caller can
    fail closed instead of silently applying looser defaults.
    """
    import yaml

    base = defaults or default_limits()
    if config_text is None:
        return base
    try:
        data = yaml.safe_load(config_text)
    except yaml.YAMLError as e:
        raise ChangeLimitsConfigError(f"{CONFIG_FILE} is not valid YAML: {e}") from e
    if data is None:
        return base
    if not isinstance(data, dict):
        raise ChangeLimitsConfigError(f"{CONFIG_FILE} must be a mapping")
    settings = data.get(CONFIG_KEY)
    if settings is None:
        return base
    if not isinstance(settings, dict):
        raise ChangeLimitsConfigError(f"{CONFIG_KEY} must be a mapping")
    return ChangeLimits(
        max_files=_positive_int(settings, "max_files", base.max_files),
        max_lines=_positive_int(settings, "max_lines", base.max_lines),
        deny=base.deny + _globs(settings, "deny"),
        allow=base.allow + _globs(settings, "allow"),
    )


# --- Matching -----------------------------------------------------------------


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern:
    pattern = pattern.strip().lstrip("/")
    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts), re.IGNORECASE)


def path_matches(path: str, pattern: str) -> bool:
    """True when the repository-relative ``path`` matches the glob ``pattern``."""
    path = path.replace("\\", "/").lstrip("/")
    if "/" not in pattern.strip().lstrip("/"):
        path = path.rsplit("/", 1)[-1]
    return _glob_regex(pattern).fullmatch(path) is not None


def denied_by(path: str, limits: ChangeLimits) -> Optional[str]:
    """The denylist glob that ``path`` matches, or None when it is allowed."""
    for pattern in PROTECTED:
        if path_matches(path, pattern):
            return pattern
    if any(path_matches(path, p) for p in limits.allow):
        return None
    return next((p for p in limits.deny if path_matches(path, p)), None)


# --- Checking -----------------------------------------------------------------


def count_line_changes(old: str, new: str) -> tuple[int, int]:
    """(added, removed) lines between two versions of a file, as a unified
    diff would count them."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(
        None, old.splitlines(), new.splitlines(), autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _code(text: str) -> str:
    """``text`` as a Markdown code span its own backticks cannot close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    tick = "`" * (longest + 1)
    return f"{tick} {text} {tick}"


def find_violations(changes: Sequence[FileChange], limits: ChangeLimits) -> list[str]:
    """Human-readable descriptions of every limit ``changes`` exceeds."""
    violations = []
    paths = list(dict.fromkeys(c.path for c in changes))
    if len(paths) > limits.max_files:
        violations.append(
            f"{len(paths)} files changed, above the limit of {limits.max_files}."
        )
    lines = sum(c.additions + c.deletions for c in changes)
    if lines > limits.max_lines:
        violations.append(
            f"{lines} lines changed, above the limit of {limits.max_lines}."
        )
    for change in changes:
        for path in dict.fromkeys(filter(None, (change.path, change.previous_path))):
            pattern = denied_by(path, limits)
            if pattern is not None:
                violations.append(
                    f"{_code(path)} matches the protected path pattern "
                    f"{_code(pattern)}."
                )
    return violations


# --- Reporting ----------------------------------------------------------------


def render_pr_section(violations: Iterable[str]) -> str:
    lines = [
        SECTION_START,
        "### Change limits exceeded",
        "",
        "This change needs a human: it is a draft, labelled "
        f"`{NEEDS_HUMAN_LABEL}`, and AutoMaintainer will not merge it under "
        "any merge policy.",
        "",
    ]
    lines += [f"- {v}" for v in violations]
    lines += [
        "",
        f"_Limits come from the server settings and the `{CONFIG_KEY}` section "
        f"of `{CONFIG_FILE}` on the base branch._",
        SECTION_END,
    ]
    return "\n".join(lines)


def upsert_pr_section(body, section: str) -> str:
    """``body`` with its change-limits section replaced by ``section``."""
    body = body if isinstance(body, str) else ""
    start, end = body.find(SECTION_START), body.find(SECTION_END)
    if start != -1 and end > start:
        return body[:start] + section + body[end + len(SECTION_END) :]
    return (body.rstrip() + "\n\n" + section) if body.strip() else section
