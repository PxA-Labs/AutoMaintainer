"""Merge decision audit rows (epic #411).

agents.maintainer_node records one ``merge_decisions`` row each time it
decides what happens to an agent PR after a review: merged, held open for a
human, or not merged. The row names the MERGE_POLICY in force, who decided
(the Maintainer's verdict or the policy and its safety gates), short reasons,
and evidence links (the PR, the reviewed commit, the verification of that
commit). Mirrors verification.record_result: written with the service-role
client, best effort, never raises. See docs/VERIFICATION.md, "Merge decisions
audit".
"""

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

AUDIT_TABLE = "merge_decisions"

# decision: must match the CHECK in supabase/migrations/*_merge_decisions.sql.
MERGED = "merged"  # AutoMaintainer merged the PR.
HELD = "held"  # The Maintainer approved; the PR is left open for a human.
NOT_MERGED = "not_merged"  # The Maintainer rejected it, or GitHub refused.

# actor
ACTOR_AGENT = "agent"  # The Maintainer's verdict decided (actor_role names it).
ACTOR_POLICY = "policy"  # MERGE_POLICY or a safety gate decided after approval.

# reasons recorded by the trusted-trigger gate (trust.py, #487) and the
# protected-path refusal before a commit (#488).
REASON_TRUSTED_TRIGGER = "trusted trigger"
REASON_UNTRUSTED_TRIGGER = "untrusted trigger"
REASON_TRIGGER_CHECK_FAILED = "trigger permission check failed"
REASON_CHANGE_LIMITS = "change limits exceeded"
REASON_NOT_COMMITTED = "protected change not committed"

# Caps below the table's CHECKs (20 reasons, 4000 characters of JSON each for
# reasons and evidence), so a long CI detail can never make the insert fail.
MAX_REASONS = 10
MAX_REASON_CHARS = 200
MAX_URL_CHARS = 1000


def _text(value, limit: int) -> str:
    """``value`` as text without NUL bytes (PostgreSQL rejects them), cut."""
    return str(value).replace("\x00", "")[:limit]


def _url(value) -> Optional[str]:
    if isinstance(value, str) and value.startswith("https://"):
        if len(value) <= MAX_URL_CHARS:  # a cut URL is a wrong link
            return value
    return None


def evidence(
    *,
    pr_url: Optional[str],
    head_sha: str,
    verification: Optional[dict] = None,
    verification_result_id: Optional[str] = None,
) -> dict:
    """Links and ids behind a decision.

    The automaintainer/verification commit status is posted without a
    target_url, so the link to it is the reviewed commit's page, where GitHub
    lists its statuses; ``verification`` is the result the Maintainer saw.
    """
    import verification as verification_module

    found: dict = {}
    pr_url = _url(pr_url)
    if pr_url:
        found["pr_url"] = pr_url
        if head_sha and "/pull/" in pr_url:
            commit_url = _url(f"{pr_url.rsplit('/pull/', 1)[0]}/commit/{head_sha}")
            if commit_url:
                found["commit_url"] = commit_url
    if verification:
        found["verification"] = {
            "context": verification_module.STATUS_CONTEXT,
            "status": _text(verification.get("status") or "", 20),
            "head_sha": _text(verification.get("head_sha") or "", 64),
        }
    if verification_result_id:
        found["verification_result_id"] = _text(verification_result_id, 64)
    return found


def decision_row(
    run: dict,
    *,
    pr_number: Optional[int],
    head_sha: Optional[str],
    iteration: Optional[int],
    policy: str,
    decision: str,
    actor: str,
    actor_role: Optional[str],
    reasons: Iterable[str],
    evidence: dict,
) -> dict:
    """The ``merge_decisions`` row for a decision on ``run`` (a runs row with
    id, org_id, repository_id and repo_name)."""
    return {
        "org_id": run["org_id"],
        "run_id": run["id"],
        "repository_id": run.get("repository_id"),
        "repo_name": run.get("repo_name"),
        "pr_number": pr_number,
        "head_sha": _text(head_sha or "", 64),  # NOT NULL: '' when unknown
        "iteration": iteration,
        "policy": policy,
        "decision": decision,
        "actor": actor,
        "actor_role": _text(actor_role, 100) if actor_role else None,
        "reasons": [_text(r, MAX_REASON_CHARS) for r in reasons][:MAX_REASONS],
        "evidence": evidence,
    }


def _verification_result_id(client, run_id: str, head_sha: str) -> Optional[str]:
    """Id of the newest verification_results row of ``run_id`` for
    ``head_sha``; None when there is none or it cannot be read."""
    if not head_sha:
        return None
    try:
        found = (
            client.table("verification_results")
            .select("id")
            .eq("run_id", run_id)
            .eq("head_sha", head_sha)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = found.data or []
        return str(rows[0]["id"]) if rows else None
    except Exception as e:
        logger.warning(
            "Could not find the verification row of run %s for the merge decision: %s",
            run_id,
            e,
        )
        return None


def record_decision(
    client,
    run_id: Optional[str],
    *,
    pr_number: Optional[int],
    head_sha: Optional[str],
    iteration: Optional[int],
    policy: str,
    decision: str,
    actor: str,
    reasons: Iterable[str],
    actor_role: Optional[str] = None,
    pr_url: Optional[str] = None,
    verification: Optional[dict] = None,
) -> bool:
    """Write one audit row for a merge decision with the service-role
    ``client``.

    Best effort and never raises: the row records the decision, it is not
    part of it, so a database failure is logged with the run id and the
    caller's merge, review request and run status are unaffected. Every call
    writes a new row (a resumed run that reviews again decided again).
    Returns whether a row was written.
    """
    if client is None or not run_id:
        return False
    try:
        found = (
            client.table("runs")
            .select("id, org_id, repository_id, repo_name")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        rows = found.data or []
        if not rows or not rows[0].get("org_id"):
            logger.warning(
                "Not recording the merge decision of run %s: the run has no "
                "organization row",
                run_id,
            )
            return False
        row = decision_row(
            rows[0],
            pr_number=pr_number,
            head_sha=head_sha,
            iteration=iteration,
            policy=policy,
            decision=decision,
            actor=actor,
            actor_role=actor_role,
            reasons=reasons,
            evidence=evidence(
                pr_url=pr_url,
                head_sha=head_sha or "",
                verification=verification,
                verification_result_id=_verification_result_id(
                    client, run_id, head_sha or ""
                ),
            ),
        )
        client.table(AUDIT_TABLE).insert(row).execute()
        return True
    except Exception as e:
        logger.warning("Could not record the merge decision of run %s: %s", run_id, e)
        return False
