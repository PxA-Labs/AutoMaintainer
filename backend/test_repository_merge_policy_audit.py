"""Epic #411: audit of repository merge-policy changes.

PUT /repositories/{id}/merge-policy writes the change and its
repository_merge_policy_changes row through one database function
(set_repository_merge_policy, migration *_repository_merge_policy_audit.sql),
with the actor taken from the caller's token. GET .../merge-policy/history
lists the rows. The fake client in test_repository_merge_policy.py emulates
the function; its transactional behaviour against PostgreSQL is checked in
tests/rls/test_rls_policies.py.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
from test_repository_merge_policy import (  # noqa: F401 - api is a fixture
    CHANGES,
    OTHER_REPO_ID,
    REPO_ID,
    URL,
    api,
)

HISTORY = f"{URL}/history"


def _rows(api):
    return api.db.tables[CHANGES]


def test_a_change_writes_one_row_with_the_callers_id(api):
    response = api.as_role("admin").put(
        # A client-supplied actor is not a field of the body and is ignored.
        URL,
        json={"merge_policy": "human", "changed_by": "someone-else"},
    )

    assert response.status_code == 200, response.text
    (row,) = _rows(api)
    assert row["changed_by"] == "u1"  # the token's user (see _user)
    assert row["org_id"] == "org-1"
    assert (row["repository_id"], row["old_policy"], row["new_policy"]) == (
        REPO_ID,
        None,
        "human",
    )
    assert response.json()["last_change"] == {
        "changed_by": "u1",
        "changed_at": row["changed_at"],
    }


def test_clearing_the_setting_records_the_old_value(api):
    api.db.repository()["merge_policy"] = "ci"

    assert api.as_role("owner").put(URL, json={"merge_policy": None}).status_code == 200

    (row,) = _rows(api)
    assert (row["old_policy"], row["new_policy"]) == ("ci", None)


@pytest.mark.parametrize("value", ["human", None])
def test_setting_the_current_value_writes_no_row(api, value):
    api.db.repository()["merge_policy"] = value

    response = api.as_role("owner").put(URL, json={"merge_policy": value})

    assert response.status_code == 200
    assert response.json()["setting"] == value
    assert _rows(api) == []
    assert api.db.rpcs == []  # not even a call that could write


def test_repeating_a_change_writes_one_row(api):
    owner = api.as_role("owner")
    first = owner.put(URL, json={"merge_policy": "human"})
    second = owner.put(URL, json={"merge_policy": "human"})

    assert len(_rows(api)) == 1
    # The no-op still reports who made the change in force.
    assert second.json()["last_change"] == first.json()["last_change"]


@pytest.mark.parametrize("role", ["member", "viewer", None])
def test_non_admins_are_refused_and_write_no_row(api, role):
    response = api.as_role(role).put(URL, json={"merge_policy": "human"})

    assert response.status_code == 403
    assert _rows(api) == []
    assert api.db.rpcs == []


def test_a_setting_above_the_ceiling_writes_no_row(api):
    assert (
        api.as_role("owner").put(URL, json={"merge_policy": "auto"}).status_code == 422
    )
    assert _rows(api) == []


def test_another_orgs_repository_is_not_found_and_not_written(api):
    admin = api.as_role("owner")
    other = f"/repositories/{OTHER_REPO_ID}/merge-policy"

    put = admin.put(other, json={"merge_policy": "human"})
    history = admin.get(f"{other}/history")
    missing = admin.get("/repositories/1/merge-policy/history")

    assert put.status_code == history.status_code == missing.status_code == 404
    assert history.json() == missing.json()
    assert api.db.rpcs == []
    assert _rows(api) == []
    assert api.db.repository(OTHER_REPO_ID)["merge_policy"] == "auto"


def test_an_audit_failure_leaves_the_policy_unchanged(api):
    api.db.fail.add(CHANGES)

    response = api.as_role("owner").put(URL, json={"merge_policy": "human"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Could not change the merge policy"}
    assert api.db.repository()["merge_policy"] is None
    assert _rows(api) == []
    # No separate, non-transactional write of the column either.
    assert not [q for q in api.db.queries if q[2] is not None]


def test_a_repository_deleted_before_the_write_is_not_found(api):
    real_rpc = api.db.rpc

    def deleted_meanwhile(name, params):
        api.db.tables["repositories"].remove(api.db.repository())
        return real_rpc(name, params)

    api.db.rpc = deleted_meanwhile
    response = api.as_role("owner").put(URL, json={"merge_policy": "human"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Repository not found"}


# --- GET .../merge-policy/history -------------------------------------------


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_members_read_the_history_newest_first(api, role):
    owner = api.as_role("owner")
    for value in ("human", "ci", None):
        assert owner.put(URL, json={"merge_policy": value}).status_code == 200

    response = api.as_role(role).get(HISTORY)

    assert response.status_code == 200
    body = response.json()
    assert body["repository_id"] == REPO_ID
    assert [(c["old_policy"], c["new_policy"]) for c in body["changes"]] == [
        ("ci", None),
        ("human", "ci"),
        (None, "human"),
    ]
    assert {c["changed_by"] for c in body["changes"]} == {"u1"}


def test_history_is_filtered_by_the_callers_org(api):
    # A row naming this repository id but another org (ids are GitHub's, so
    # this is the only way one could disagree) never shows up.
    _rows(api).append(
        {
            "id": "forged",
            "org_id": "org-2",
            "repository_id": REPO_ID,
            "old_policy": None,
            "new_policy": "auto",
            "changed_by": "u2",
            "changed_at": "2026-11-17T11:00:00Z",
        }
    )

    assert api.as_role("member").get(HISTORY).json()["changes"] == []
    assert api.as_role("member").get(URL).json()["last_change"] is None


@pytest.mark.parametrize("limit, status", [(1, 200), (100, 200), (0, 422), (101, 422)])
def test_history_limit_is_bounded(api, limit, status):
    response = api.as_role("member").get(HISTORY, params={"limit": limit})

    assert response.status_code == status
    if status == 200:
        assert api.db.limits[-1] == limit


def test_history_limit_defaults_to_50(api):
    assert api.as_role("member").get(HISTORY).status_code == 200
    assert api.db.limits[-1] == 50


def test_get_reports_the_latest_change(api):
    api.as_role("owner").put(URL, json={"merge_policy": "human"})
    api.as_role("admin").put(URL, json={"merge_policy": "ci"})

    last_change = api.as_role("viewer").get(URL).json()["last_change"]

    assert last_change == {
        "changed_by": "u1",
        "changed_at": _rows(api)[-1]["changed_at"],
    }


def test_get_still_answers_when_the_history_is_unreadable(api):
    api.db.fail.add(CHANGES)

    response = api.as_role("member").get(URL)

    assert response.status_code == 200
    assert response.json()["last_change"] is None


def test_history_without_a_database_is_unavailable(api, monkeypatch):
    api.as_role("member")
    monkeypatch.setattr(agents, "supabase", None)
    assert api.get(HISTORY).status_code == 503


def test_history_requires_authentication(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    main.app.dependency_overrides.clear()
    assert TestClient(main.app).get(HISTORY).status_code == 401
