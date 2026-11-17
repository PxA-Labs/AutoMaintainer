"""Trusted-trigger gate for automatic merges (#487).

Under ``MERGE_POLICY=ci`` or ``auto``, agents.maintainer_node merges an
approved PR only when the run's trigger is trusted: every GitHub user who
caused the run has ``write``, ``maintain`` or ``admin`` permission on the
repository. Tests passing and the Maintainer's approval are not enough,
because a prompt-injected run controls both the change and the approval.

Who caused the run, per trigger path (``trigger_source``):

* ``api`` (POST /start from the dashboard or the API): the caller's numeric
  GitHub user id, taken from the GitHub identity of their Supabase account
  (main.github_id_of), plus the login stored with it. A caller without a
  linked GitHub identity (e.g. the local dev-auth bypass) is an unknown actor.
* ``label`` (AUTOMAINTAINER_TRIGGER_LABEL added to an open issue,
  webhook_processor.py): the GitHub user who added the label (the
  delivery's ``sender``), by numeric id, plus the login in that delivery.
  Users with triage access can add labels without being able to write, so
  their runs start and open a PR that is never merged automatically.
* A run that targets an issue (``target_issue``, always set for ``label``)
  also needs the issue's author to be trusted: the issue body is the task
  the models follow, so an outsider's issue must not steer an auto-merged
  change even when a writer started the run.
* A future trigger (a commenter, say) must pass its actor's id as
  ``trigger_github_id`` with its own ``trigger_source``.

Every actor is identified by its numeric GitHub user id, never by a login
alone (#491). Logins can change: the one Supabase stored at the caller's
last sign-in goes stale when the account is renamed, and the freed name can
be registered by someone else. So each id is resolved to its current login
with ``GET /user/{account_id}`` and the permission is checked for that login;
a stored login that differs is only logged.

The decision is deterministic: it reads GitHub's
``GET /repos/{owner}/{repo}/collaborators/{username}/permission`` and
``GET /user/{account_id}`` through the run's installation-scoped client and
nothing a model wrote. GitHub's "Permissions required for GitHub Apps" page
lists the permission endpoint under *Metadata: read*, which every App has,
and "Endpoints available for GitHub App installation access tokens" lists
``GET /user/{account_id}`` (no App permission is listed for it); a refusal
fails closed like any other error. It fails closed:

* a missing or malformed id (a message queued before #491, an identity
  without an id), a bot account, a non-collaborator or a deleted account is
  an ``untrusted trigger``: nothing identifies a writer, and asking again
  would not change that;
* a lookup that errors (HTTP error, network) is a ``trigger permission check
  failed``: the actor may well be trusted, GitHub just did not say so.

Either way there is no automatic merge; the PR stays open for a human. There
is no bot allowlist: a bot that should merge unattended can open the run as a
human writer would, through /start.

Known limit: a trusted trigger vouches for the run, not for the repository
content it reads (README, code comments), which only a writer can change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from github import GithubException

from . import merge_audit

logger = logging.getLogger("trust")  # legacy name, kept for log filters

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


def valid_github_id(github_id) -> bool:
    """A GitHub user id: a positive int (``bool`` is not one)."""
    return (
        isinstance(github_id, int) and not isinstance(github_id, bool) and github_id > 0
    )


def permission_of(gh_repo, login: str) -> str:
    """``login``'s base permission on ``gh_repo`` (admin, write, read, none).
    Raises what PyGithub raises."""
    return gh_repo.get_collaborator_permission(login)


def current_login(users, github_id: int) -> Optional[str]:
    """The current login of GitHub user ``github_id`` (``GET
    /user/{account_id}``), or None for a bot account. ``users`` is a PyGithub
    ``Github`` client. Raises what PyGithub raises."""
    user = users.get_user_by_id(github_id)
    if getattr(user, "type", None) == "Bot":
        return None
    login = getattr(user, "login", None)
    return login if isinstance(login, str) else None


def _lookup_failed(role: str, what: str, e: Exception) -> TriggerTrust:
    logger.warning("Looking up the %s (%s) failed: %s", role, what, e)
    status = getattr(e, "status", None)
    cause = f"HTTP {status}" if isinstance(e, GithubException) else type(e).__name__
    return _check_failed(f"looking up the {role} {what} failed ({cause})")


def _resolve(users, role: str, github_id, stored_login) -> TriggerTrust | str:
    """The current login of actor ``github_id``, or the TriggerTrust that
    holds the PR."""
    if not valid_github_id(github_id):
        if stored_login:
            return _untrusted(f"the {role} {stored_login!r} has no GitHub user id")
        return _untrusted(f"the {role} is unknown or a bot")
    try:
        login = current_login(users, github_id)
    except GithubException as e:
        if e.status == 404:
            return _untrusted(f"the {role} (GitHub user {github_id}) does not exist")
        return _lookup_failed(role, f"GitHub user {github_id}", e)
    except Exception as e:
        return _lookup_failed(role, f"GitHub user {github_id}", e)
    if not valid_login(login):
        return _untrusted(f"the {role} (GitHub user {github_id}) is a bot")
    if isinstance(stored_login, str) and stored_login.lower() != login.lower():
        # Renamed since the stored login was recorded; the id decides.
        logger.info(
            "The %s GitHub user %s is now %s (stored login %r)",
            role,
            github_id,
            login,
            stored_login,
        )
    return login


def check_actors(
    gh_repo, users, actors: Iterable[tuple[str, Optional[int], Optional[str]]]
) -> TriggerTrust:
    """Trusted only when every ``(role, github_id, stored_login)`` in
    ``actors`` has write, maintain or admin permission on ``gh_repo``.

    Each id is resolved to its current login through ``users`` (a PyGithub
    ``Github`` client) and the permission is looked up for that login; the
    stored login is only compared and logged. Stops at the first actor that
    is not trusted; each distinct id is looked up once."""
    actors = list(actors)
    if not actors:
        return _untrusted("the run has no trigger actor")
    seen: dict[int, tuple[str, str]] = {}
    for role, github_id, stored_login in actors:
        if valid_github_id(github_id) and github_id in seen:
            login, permission = seen[github_id]
        else:
            login = _resolve(users, role, github_id, stored_login)
            if isinstance(login, TriggerTrust):
                return login
            try:
                permission = str(permission_of(gh_repo, login))
            except GithubException as e:
                if e.status == 404:
                    return _untrusted(
                        f"the {role} {login} is not a collaborator of the repository"
                    )
                return _lookup_failed(role, login, e)
            except Exception as e:
                return _lookup_failed(role, login, e)
            seen[github_id] = (login, permission)
        if permission not in TRUSTED_PERMISSIONS:
            return _untrusted(f"the {role} {login} has {permission} permission")
    names = ", ".join(
        f"{role} {seen[gid][0]} ({seen[gid][1]})" for role, gid, _ in actors
    )
    return TriggerTrust(True, merge_audit.REASON_TRUSTED_TRIGGER, names)


def issue_author(gh_repo, number: int) -> tuple[Optional[int], Optional[str]]:
    """``(id, login)`` of the author of issue ``number``; ``(None, None)``
    for a bot account. Raises what PyGithub raises."""
    user = getattr(gh_repo.get_issue(number=number), "user", None)
    if getattr(user, "type", None) == "Bot":
        return None, None
    github_id = getattr(user, "id", None)
    login = getattr(user, "login", None)
    return (
        github_id if valid_github_id(github_id) else None,
        login if isinstance(login, str) else None,
    )


def check_trigger(
    gh_repo, trigger: Optional[dict], target_issue: Optional[int], users
) -> TriggerTrust:
    """The trusted-trigger gate for one run (see the module docstring).

    ``trigger`` is the run's ``{"source", "login", "github_id"}``
    (AgentState.trigger); ``target_issue`` the issue the run was started on,
    or None; ``users`` the run's PyGithub ``Github`` client, which resolves
    user ids to current logins.
    """
    trigger = trigger or {}
    source = trigger.get("source") or "unknown"
    actors: list[tuple[str, Optional[int], Optional[str]]] = [
        (f"{source} trigger", trigger.get("github_id"), trigger.get("login"))
    ]
    if target_issue:
        try:
            author_id, author_login = issue_author(gh_repo, target_issue)
        except Exception as e:
            logger.warning("Could not read the author of #%s: %s", target_issue, e)
            return _check_failed(
                f"reading the author of issue #{target_issue} failed "
                f"({type(e).__name__})"
            )
        actors.append((f"author of issue #{target_issue}", author_id, author_login))
    return check_actors(gh_repo, users, actors)
