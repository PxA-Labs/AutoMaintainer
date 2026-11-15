"""OpenAPI snapshot: the HTTP contract may only change on purpose (epic #419).

``openapi.snapshot.json`` next to this file is the committed copy of
``app.openapi()``. Any change to a route, parameter or request/response model
changes the generated schema and fails this test until the snapshot is
regenerated, so API changes show up as a reviewable diff in the pull request
instead of being discovered by the dashboard at runtime.

Regenerate after an intentional API change (from backend/, with the locked
dependencies, since the FastAPI/Pydantic versions shape the output):

    UPDATE_OPENAPI_SNAPSHOT=1 uv run --frozen pytest tests/contract -q

then commit the updated snapshot together with the API change. Review the
diff: removed paths, renamed fields and newly required parameters break the
dashboard (see test_dashboard_contract.py) and any other client.
"""

import copy
import difflib
import json
import os
from pathlib import Path

from main import app

SNAPSHOT_PATH = Path(__file__).with_name("openapi.snapshot.json")
UPDATE_ENV = "UPDATE_OPENAPI_SNAPSHOT"
REGENERATE_HINT = (
    f"If the change is intentional, regenerate the snapshot from backend/ with\n"
    f"  {UPDATE_ENV}=1 uv run --frozen pytest tests/contract -q\n"
    f"and commit {SNAPSHOT_PATH.name} with the API change."
)


def render_schema(schema: dict) -> str:
    """Stable text form: sorted keys, LF line endings, trailing newline."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def current_schema() -> dict:
    # app.openapi() caches and returns the same dict on every call; copy it so
    # a test that mutates the result cannot leak into another.
    return copy.deepcopy(app.openapi())


def read_snapshot() -> str:
    # Normalise line endings: a Windows checkout with core.autocrlf=true
    # rewrites the committed LF file as CRLF.
    return SNAPSHOT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_openapi_schema_matches_snapshot():
    rendered = render_schema(current_schema())

    if os.environ.get(UPDATE_ENV) == "1":
        SNAPSHOT_PATH.write_text(rendered, encoding="utf-8", newline="\n")

    assert (
        SNAPSHOT_PATH.exists()
    ), f"{SNAPSHOT_PATH.name} is missing.\n{REGENERATE_HINT}"
    committed = read_snapshot()
    if rendered != committed:
        diff = "".join(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{SNAPSHOT_PATH.name} (committed)",
                tofile="app.openapi() (current)",
                n=2,
            )
        )
        lines = diff.splitlines()
        if len(lines) > 200:
            diff = "\n".join(lines[:200]) + f"\n... ({len(lines) - 200} more lines)"
        raise AssertionError(
            "The OpenAPI schema changed without updating the snapshot.\n\n"
            f"{diff}\n\n{REGENERATE_HINT}"
        )


def test_snapshot_is_valid_openapi_document():
    """Guards against a hand-edited or truncated snapshot being committed."""
    snapshot = json.loads(read_snapshot())
    assert snapshot["openapi"].startswith("3.")
    assert snapshot["info"]["title"] == app.title
    assert snapshot["paths"], "snapshot has no paths"


def test_snapshot_render_is_deterministic():
    """Two renders of the schema are byte-identical (no ordering drift)."""
    assert render_schema(current_schema()) == render_schema(current_schema())
