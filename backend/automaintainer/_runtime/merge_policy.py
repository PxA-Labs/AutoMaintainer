"""Per-repository merge policy (epic #411).

The deployment's ``MERGE_POLICY`` is the ceiling: the loosest policy any
repository of the deployment may use. A repository can only tighten it,
through ``repositories.merge_policy`` (NULL means "inherit the ceiling"):

    effective = min(ceiling, repository setting)    with human < ci < auto

so nothing stored in the database can make a run merge more readily than the
operator allowed. The setting is written only by the backend (service role),
for org owners and admins, through ``PUT /repositories/{id}/merge-policy``;
the API refuses a setting looser than the ceiling (422).

``resolve_for_run`` is what agents.maintainer_node uses at the merge
decision. It fails closed: when the setting cannot be read the run uses
``human`` and says why in its merge_decisions reasons. "Cannot be read"
covers a database error, a value outside the allowed set, and a missing
``runs`` or ``repositories`` row (or a run without a repository): through
PostgREST a row hidden by RLS, a wrong key and a deleted row all come back
as an empty result, not an error, and that absence could hide a stricter
stored setting. Only an existing repository row whose ``merge_policy`` is
NULL inherits the ceiling. A valid run never takes the missing-row path:
``runs.repository_id`` is NOT NULL with a foreign key and ``/start`` always
inserts the run row. See ARCHITECTURE.md, "Merge policy".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from automaintainer.context import active_config

logger = logging.getLogger("merge_policy")  # legacy name, kept for log filters

# Strictest first; the order is what "stricter" means.
POLICIES = ("human", "ci", "auto")
DEFAULT_POLICY = "human"

REASON_UNREADABLE = "repository merge policy unreadable (fail closed)"
REASON_MISSING = "repository merge policy row missing (fail closed)"


class SettingMissing(LookupError):
    """The run or its repository row is absent, so the setting is unknown."""


def _rank(policy: str) -> int:
    return POLICIES.index(policy)


def deployment_ceiling() -> str:
    """``MERGE_POLICY`` from the environment; unknown values mean ``human``.

    Inside an engine run, the engine's ``merge_policy`` (#186), which the
    config already validated."""
    config = active_config()
    if config is not None:
        return config.merge_policy
    policy = (os.getenv("MERGE_POLICY") or DEFAULT_POLICY).strip().lower()
    if policy not in POLICIES:
        logger.warning("Unknown MERGE_POLICY %r; falling back to 'human'.", policy)
        return DEFAULT_POLICY
    return policy


def allowed_settings(ceiling: str) -> list[str]:
    """The repository settings the API accepts under ``ceiling``."""
    return list(POLICIES[: _rank(ceiling) + 1])


def is_allowed(setting: Optional[str], ceiling: str) -> bool:
    """Whether a repository may store ``setting`` (None = inherit)."""
    return setting is None or setting in allowed_settings(ceiling)


def effective(ceiling: str, setting: Optional[str]) -> str:
    """The stricter of ``ceiling`` and the repository ``setting``.

    A ``setting`` looser than the ceiling (stored before the operator
    lowered ``MERGE_POLICY``) is never effective. An unknown ``setting`` is
    treated as ``human`` (fail closed).
    """
    if ceiling not in POLICIES:
        return DEFAULT_POLICY
    if setting is None:
        return ceiling
    if setting not in POLICIES:
        return DEFAULT_POLICY
    return min(ceiling, setting, key=_rank)


@dataclass(frozen=True)
class Resolution:
    """The effective policy of a run and how it was decided."""

    policy: str
    ceiling: str
    setting: Optional[str] = None
    # Extra merge_decisions reasons: why the policy is below the ceiling.
    reasons: list[str] = field(default_factory=list)


def _first_row(result) -> Optional[dict]:
    rows = getattr(result, "data", None)
    if not isinstance(rows, list):
        raise ValueError(f"unexpected response {type(rows).__name__}")
    if not rows:
        return None
    if not isinstance(rows[0], dict):
        raise ValueError(f"unexpected row {type(rows[0]).__name__}")
    return rows[0]


def read_setting(client, run_id: str) -> Optional[str]:
    """The ``merge_policy`` of the run's repository (None: inherit).

    Reads the run's repository and organization from ``runs`` and the
    setting from that organization's ``repositories`` row. Raises
    SettingMissing when either row is absent (or the run has no
    repository), and another exception on a database error or a malformed
    response; callers fail closed.
    """
    run = _first_row(
        client.table("runs")
        .select("org_id, repository_id")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )
    if not run:
        raise SettingMissing(f"no runs row {run_id}")
    if run.get("repository_id") is None:
        raise SettingMissing(f"run {run_id} has no repository_id")
    query = (
        client.table("repositories")
        .select("merge_policy")
        .eq("id", run["repository_id"])
    )
    if run.get("org_id"):
        query = query.eq("org_id", run["org_id"])
    repository = _first_row(query.limit(1).execute())
    if not repository:
        raise SettingMissing(
            f"no repositories row {run['repository_id']} in org {run.get('org_id')}"
        )
    setting = repository.get("merge_policy")
    if setting is not None and setting not in POLICIES:
        raise ValueError(f"invalid repositories.merge_policy {setting!r}")
    return setting


def resolve_for_run(client, run_id: Optional[str], ceiling: str) -> Resolution:
    """The effective merge policy of ``run_id`` under ``ceiling``.

    No database read when the ceiling is already ``human`` (nothing is
    stricter) or there is no database (no repository settings exist). Never
    raises: a missing run or repository row (or no run id to look one up)
    yields ``human`` with REASON_MISSING, any other unreadable setting
    ``human`` with REASON_UNREADABLE.
    """
    if ceiling == DEFAULT_POLICY or client is None:
        return Resolution(policy=ceiling, ceiling=ceiling)
    try:
        if not run_id:
            raise SettingMissing("no current run id")
        setting = read_setting(client, run_id)
    except SettingMissing as e:
        logger.warning(
            "Repository merge policy of run %s not found; using 'human' for "
            "this run: %s",
            run_id,
            e,
        )
        return Resolution(
            policy=DEFAULT_POLICY, ceiling=ceiling, reasons=[REASON_MISSING]
        )
    except Exception as e:
        logger.warning(
            "Could not read the repository merge policy of run %s; using "
            "'human' for this run: %s",
            run_id,
            e,
        )
        return Resolution(
            policy=DEFAULT_POLICY, ceiling=ceiling, reasons=[REASON_UNREADABLE]
        )
    policy = effective(ceiling, setting)
    reasons = []
    if _rank(policy) < _rank(ceiling):
        reasons.append(f"repository policy {policy} (ceiling {ceiling})")
    return Resolution(policy=policy, ceiling=ceiling, setting=setting, reasons=reasons)
