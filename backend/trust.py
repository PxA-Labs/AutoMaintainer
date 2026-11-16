"""Trusted-trigger gate for automatic merges (#487).

Under ``MERGE_POLICY=ci`` or ``auto``, agents.maintainer_node merges an
approved PR only when the run's trigger is trusted: every GitHub user who
caused the run has ``write``, ``maintain`` or ``admin`` permission on the
repository. Tests passing and the Maintainer's approval are not enough,
because a prompt-injected run controls both the change and the approval.

Who caused the run, per trigger path (the only one today is ``api``):

* ``api`` (POST /start from the dashboard or the API): the caller's linked
  GitHub login, taken from the GitHub identity of their Supabase account
  (main.github_login_of). A caller without a linked GitHub identity (e.g. the
  local dev-auth bypass) is an unknown actor.
* A run that targets an issue (``target_issue``) also needs the issue's
  author to be trusted: the issue body is the task the models follow, so an
  outsider's issue must not steer an auto-merged change even when a writer
  started the run.
* There is no webhook-started run (webhook_processor.py only handles
  installation events). A future trigger must pass its actor (issue author,
  labeler, commenter) as ``trigger_login`` with its own ``trigger_source``.

The decision is deterministic: it reads GitHub's
``GET /repos/{owner}/{repo}/collaborators/{username}/permission`` through
the run's installation-scoped client and nothing a model wrote. GitHub's REST
reference lists no App permission for that endpoint beyond the mandatory
Metadata read, which every run token has; a refusal fails closed like any
other error. It fails closed: an
unknown or malformed login, a bot account, a 404, any other API error or an
unreadable issue author means no automatic merge; the PR stays open for a
human. There is no bot allowlist: a bot that should merge unattended can open
the run as a human writer would, through /start.

Known limits: the login is the one Supabase stored at the caller's last
GitHub sign-in, so a renamed account is checked under its old name until the
user signs in again; and a trusted trigger vouches for the run, not for the
repository content it reads (README, code comments), which only a writer can
change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from github import GithubException

import merge_audit

logger = logging.getLogger(__name__)

# ``permission`` is GitHub's legacy base role: maintain is reported as write
# and triage as read, so this set covers write, maintain and admin.
TRUSTED_PERMISSIONS = frozenset({"admin", "write"})

# GitHub logins: alphanumerics and single hyphens, at most 39 characters. A
# bot's login ("name[bot]") does not match, so bots are never looked up.
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


@dataclass(frozen=True)
class TriggerTrust:
    """Outcome of the gate: ``reason`` is the merge_decisions reason and
    ``detail`` the log text (which actor, which permission)."""

    trusted: bool
    reason: str
    detail: str


def _untrusted(detail: str) -> TriggerTrust:
    return TriggerTrust(False, merge_audit.REASON_UNTRUSTED_TRIGGER, detail)


def _check_failed(detail: str) -> TriggerTrust:
    return TriggerTrust(False, merge_audit.REASON_TRIGGER_CHECK_FAILED, detail)


def valid_login(login) -> bool:
    return isinstance(login, str) and _LOGIN_RE.fullmatch(login) is not None


def permission_of(gh_repo, login: str) -> str:
    """``login``'s base permission on ``gh_repo`` (admin, write, read, none).
    Raises what PyGithub raises."""
    return gh_repo.get_collaborator_permission(login)


def check_actors(gh_repo, actors: Iterable[tuple[str, Optional[str]]]) -> TriggerTrust:
    """Trusted only when every ``(role, login)`` in ``actors`` has write,
    maintain or admin permission on ``gh_repo``. Stops at the first actor
    that is not; each distinct login is looked up once."""
    actors = list(actors)
    if not actors:
        return _untrusted("the run has no trigger actor")
    seen: dict[str, str] = {}
    for role, login in actors:
        if not valid_login(login):
            if login:
                return _untrusted(f"the {role} {login!r} is a bot or malformed login")
            return _untrusted(f"the {role} is unknown or a bot")
        if login not in seen:
            try:
                seen[login] = str(permission_of(gh_repo, login))
            except GithubException as e:
                if e.status == 404:
                    return _untrusted(
                        f"the {role} {login} is not a collaborator of the repository"
                    )
                logger.warning("Permission lookup for %s failed: %s", login, e)
                return _check_failed(
                    f"looking up the {role} {login} failed (HTTP {e.status})"
                )
            except Exception as e:
                logger.warning("Permission lookup for %s failed: %s", login, e)
                return _check_failed(
                    f"looking up the {role} {login} failed ({type(e).__name__})"
                )
        if seen[login] not in TRUSTED_PERMISSIONS:
            return _untrusted(f"the {role} {login} has {seen[login]} permission")
    names = ", ".join(f"{role} {login} ({seen[login]})" for role, login in actors)
    return TriggerTrust(True, merge_audit.REASON_TRUSTED_TRIGGER, names)


def issue_author(gh_repo, number: int) -> Optional[str]:
    """Login of the author of issue ``number``; None for a bot account.
    Raises what PyGithub raises."""
    user = getattr(gh_repo.get_issue(number=number), "user", None)
    if getattr(user, "type", None) == "Bot":
        return None
    login = getattr(user, "login", None)
    return login if isinstance(login, str) else None


def check_trigger(
    gh_repo, trigger: Optional[dict], target_issue: Optional[int]
) -> TriggerTrust:
    """The trusted-trigger gate for one run (see the module docstring).

    ``trigger`` is the run's ``{"source", "login"}`` (AgentState.trigger);
    ``target_issue`` the issue the run was started on, or None.
    """
    trigger = trigger or {}
    source = trigger.get("source") or "unknown"
    actors: list[tuple[str, Optional[str]]] = [
        (f"{source} trigger", trigger.get("login"))
    ]
    if target_issue:
        try:
            author = issue_author(gh_repo, target_issue)
        except Exception as e:
            logger.warning("Could not read the author of #%s: %s", target_issue, e)
            return _check_failed(
                f"reading the author of issue #{target_issue} failed "
                f"({type(e).__name__})"
            )
        actors.append((f"author of issue #{target_issue}", author))
    return check_actors(gh_repo, actors)
