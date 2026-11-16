"""Dashboard <-> API contract (epic #419).

Every backend call the dashboard makes must exist in the API schema with the
same method and path template, send only parameters/body fields the API
declares (and every field it requires), and -- where the API declares a
response model -- read only fields that model has.

How it works:

1. ``discover_calls()`` scans dashboard/src (tests excluded) for backend
   calls: ``fetch(`${backendUrl}/...`)``, ``fetch(`${getBackendUrl()}/...`)``,
   ``authedFetch(`/...`)`` and the terminal WebSocket URL. The HTTP method is
   read from the call's ``method:`` option (GET when absent).
2. ``DASHBOARD_CALLS`` below is the hand-maintained description of what each
   call sends and reads. A test keeps it in step with the discovered calls, so
   a new or removed dashboard call fails until it is described here.
3. Each call is checked against the committed OpenAPI snapshot
   (test_openapi_snapshot.py guarantees the snapshot equals app.openapi()).

Known limitation: no backend route declares ``response_model`` today, so the
schema types every response as ``{}`` and response-field checks are vacuous.
They switch on automatically, per route, as response models are added.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.routing import APIWebSocketRoute

from main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_SRC = REPO_ROOT / "dashboard" / "src"
SNAPSHOT_PATH = Path(__file__).with_name("openapi.snapshot.json")

pytestmark = pytest.mark.skipif(
    not DASHBOARD_SRC.is_dir(), reason="dashboard/src is not present"
)


@dataclass(frozen=True)
class Call:
    """What one dashboard call sends and reads.

    ``body`` maps each JSON body field to ``None`` (scalar) or to a nested
    dict of the same shape for objects / array items, e.g.
    ``{"selection": {"startLine": None, ...}}``.
    ``reads`` lists the top-level response fields the dashboard uses.
    """

    query: frozenset = frozenset()
    body: dict = field(default_factory=dict)
    reads: frozenset = frozenset()


# (METHOD, normalised path) -> Call. Paths use "{}" for every path parameter.
# Keep the source location in the comment so a reviewer can check the entry.
DASHBOARD_CALLS = {
    # lib/hooks/use-health.ts: only res.ok is used.
    ("GET", "/healthz/supabase"): Call(),
    # lib/hooks/use-agent-run.ts startStop()
    ("POST", "/start"): Call(
        body={"repo_name": None, "target_issue": None},
        reads=frozenset({"run_id"}),
    ),
    # lib/hooks/use-agent-run.ts startStop() (always sends a run_id)
    ("POST", "/stop"): Call(body={"run_id": None}),
    # lib/hooks/use-agent-run.ts resumeRun() and the status poll
    ("GET", "/status/{}"): Call(reads=frozenset({"status", "repo_name"})),
    # components/dashboard/DashboardShell.tsx (data.runs; RunHistory reads
    # id/status/repo_name/created_at of each run row)
    ("GET", "/runs"): Call(reads=frozenset({"runs"})),
    # app/admin/page.tsx fetchMetrics(), typed as AdminMetrics
    ("GET", "/admin/metrics"): Call(
        reads=frozenset({"orgs", "organizations", "runs", "usage", "repos", "system"})
    ),
    # app/admin/page.tsx RunsTab
    ("GET", "/admin/runs"): Call(reads=frozenset({"runs"})),
    # components/InlineAssist.tsx (response is a text stream)
    ("POST", "/assist/inline"): Call(
        body={
            "repo_name": None,
            "file_path": None,
            "prompt": None,
            "selected_code": None,
            "prefix_code": None,
            "suffix_code": None,
            "selection": {
                "startLine": None,
                "startColumn": None,
                "endLine": None,
                "endColumn": None,
            },
        }
    ),
    # components/WebIDE.tsx fetchTree() (the whole tree object is rendered)
    ("GET", "/repo/{}/tree"): Call(),
    # components/WebIDE.tsx openFile()
    ("GET", "/repo/{}/file"): Call(
        query=frozenset({"file_path"}), reads=frozenset({"content"})
    ),
    # components/WebIDE.tsx saveFile()
    ("POST", "/repo/{}/file"): Call(body={"file_path": None, "content": None}),
    # components/WebIDE.tsx create file / folder
    ("POST", "/repo/{}/file/create"): Call(
        body={"file_path": None, "is_dir": None, "content": None}
    ),
    # components/WebIDE.tsx delete
    ("DELETE", "/repo/{}/file"): Call(query=frozenset({"file_path"})),
    # components/WebIDE.tsx staged-list restore (#505)
    ("GET", "/repo/{}/working-changes"): Call(reads=frozenset({"branch", "changes"})),
    # components/WebIDE.tsx search
    ("GET", "/repo/{}/search"): Call(
        query=frozenset({"q"}), reads=frozenset({"results"})
    ),
    # components/WebIDE.tsx handleProposeChanges()
    ("POST", "/repo/{}/propose-changes"): Call(
        body={
            "title": None,
            "description": None,
            "changes": {"path": None, "content": None, "status": None},
        },
        reads=frozenset({"branch_name", "pr_number", "pr_url"}),
    ),
    # lib/github-app.ts requestInstallUrl() (no body; only the Supabase
    # bearer token, no GitHub token header since #481)
    ("POST", "/github/app/install-url"): Call(reads=frozenset({"install_url"})),
    # lib/merge-policy.ts fetchMergePolicy(), typed as RepositoryMergePolicy
    ("GET", "/repositories/{}/merge-policy"): Call(
        reads=frozenset({"setting", "ceiling", "effective", "can_edit"})
    ),
    # lib/merge-policy.ts updateMergePolicy()
    ("PUT", "/repositories/{}/merge-policy"): Call(
        body={"merge_policy": None},
        reads=frozenset({"setting", "ceiling", "effective", "can_edit"}),
    ),
    # lib/terminal.ts buildTerminalWsUrl() (WebSockets are not in OpenAPI)
    ("WEBSOCKET", "/api/terminal/ws"): Call(query=frozenset({"repo_url"})),
}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

_HTTP_URL_PATTERNS = (
    # fetch(`${backendUrl}/x`) and fetch(`${getBackendUrl()}/x`)
    re.compile(r"`\$\{(?:backendUrl|getBackendUrl\(\))\}(/[^`]*)`"),
    # authedFetch(`/x`) / authedFetch("/x") -- backend-relative paths
    re.compile(r"authedFetch\(\s*[`\"'](/[^`\"']*)[`\"']"),
)
_WS_URL_PATTERN = re.compile(r"`\$\{protocol\}//\$\{host\}(/[^`]*)`")
_METHOD_PATTERN = re.compile(r"""\bmethod\s*:\s*["'`]([A-Za-z]+)["'`]""")
_QUERY_NAME_PATTERN = re.compile(r"[?&]([A-Za-z_][A-Za-z0-9_]*)=")


def normalise_path(path: str) -> str:
    """Strip the query string and collapse every path parameter to ``{}``."""
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)  # JS template expressions
    return re.sub(r"\{[^}]*\}", "{}", path)  # OpenAPI {param}


def _call_text(source: str, url_start: int) -> str:
    """Text of the fetch(...) call that contains the URL at ``url_start``.

    Walks back to the nearest ``fetch(`` / ``authedFetch(`` and forward to
    its matching paren, skipping quoted strings so parentheses inside
    messages do not count.
    """
    call = max(
        source.rfind("fetch(", 0, url_start), source.rfind("Fetch(", 0, url_start)
    )
    assert call != -1, "URL outside a fetch call"
    open_paren = call + len("fetch")
    depth, i, quote = 0, open_paren, None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren : i + 1]
        i += 1
    return source[open_paren:]


@dataclass
class Found:
    method: str
    path: str
    query: set
    call_text: str
    locations: list


def _dashboard_sources():
    for path in sorted(DASHBOARD_SRC.rglob("*")):
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
            continue
        yield path, path.read_text(encoding="utf-8")


def discover_calls() -> dict:
    """(METHOD, normalised path) -> Found, for every backend call in the UI."""
    found = {}

    def record(method, raw_path, call_text, where, query_text=None):
        key = (method, normalise_path(raw_path))
        entry = found.setdefault(key, Found(method, key[1], set(), "", []))
        query_source = raw_path if query_text is None else query_text
        entry.query |= set(_QUERY_NAME_PATTERN.findall(query_source))
        entry.call_text += call_text + "\n"
        entry.locations.append(where)

    for path, source in _dashboard_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for pattern in _HTTP_URL_PATTERNS:
            for match in pattern.finditer(source):
                text = _call_text(source, match.start(1))
                method_match = _METHOD_PATTERN.search(text)
                method = method_match.group(1).upper() if method_match else "GET"
                line = source.count("\n", 0, match.start()) + 1
                record(method, match.group(1), text, f"{rel}:{line}")
        for match in _WS_URL_PATTERN.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            # terminal.ts appends the query string in a later statement, so
            # look for query names in the rest of the enclosing function.
            end = source.find("\n}", match.end())
            tail = source[match.start() : end if end != -1 else len(source)]
            record("WEBSOCKET", match.group(1), "", f"{rel}:{line}", tail)
    return found


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def _schema() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _operations(schema: dict) -> dict:
    """(METHOD, normalised path) -> operation object."""
    ops = {}
    for path, item in schema["paths"].items():
        for method, op in item.items():
            ops[(method.upper(), normalise_path(path))] = op
    return ops


def _resolve(schema: dict, node: dict) -> dict:
    """Follow $ref, and unwrap Optional (anyOf [X, null]) to X."""
    while True:
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            node = schema["components"]["schemas"][name]
            continue
        variants = [v for v in node.get("anyOf", []) if v.get("type") != "null"]
        if len(variants) == 1:
            node = variants[0]
            continue
        if node.get("type") == "array" and "items" in node:
            node = node["items"]
            continue
        return node


def _body_mismatches(schema: dict, model: dict, sent: dict, where: str) -> list:
    model = _resolve(schema, model)
    props = model.get("properties")
    if props is None:
        return []  # untyped body: nothing to check against
    problems = []
    for name in sorted(set(model.get("required", [])) - set(sent)):
        problems.append(f"{where}: required field '{name}' is not sent")
    for name, nested in sent.items():
        if name not in props:
            problems.append(f"{where}: sends '{name}', which the API does not declare")
        elif isinstance(nested, dict):
            problems += _body_mismatches(schema, props[name], nested, f"{where}.{name}")
    return problems


def _response_properties(schema: dict, op: dict):
    """Declared fields of the 200 JSON response, or None when untyped."""
    content = op.get("responses", {}).get("200", {}).get("content", {})
    body = content.get("application/json", {}).get("schema")
    if not body:
        return None
    return _resolve(schema, body).get("properties")


def _sent_query(found: dict, key) -> set:
    return found[key].query if key in found else set()


def _where(found: dict, key) -> list:
    return found[key].locations if key in found else ["<not found in dashboard/src>"]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_discovery_finds_the_known_call_sites():
    """Sanity check on the scanner itself, so an empty scan cannot pass."""
    found = discover_calls()
    assert ("POST", "/start") in found
    assert ("DELETE", "/repo/{}/file") in found
    assert ("WEBSOCKET", "/api/terminal/ws") in found
    assert found[("GET", "/repo/{}/file")].query == {"file_path"}


def test_contract_table_matches_dashboard_source():
    found = set(discover_calls())
    described = set(DASHBOARD_CALLS)
    undescribed = sorted(found - described)
    stale = sorted(described - found)
    assert not undescribed and not stale, (
        "DASHBOARD_CALLS is out of step with dashboard/src.\n"
        f"  calls with no entry: {undescribed}\n"
        f"  entries with no call: {stale}\n"
        "Describe what each new call sends and reads, or delete stale entries."
    )


def test_described_body_fields_appear_in_the_call():
    """Each body field listed in DASHBOARD_CALLS is named in the fetch call."""
    found = discover_calls()
    missing = []
    for key, call in DASHBOARD_CALLS.items():
        text = found[key].call_text if key in found else ""

        def walk(fields, prefix=""):
            for name, nested in fields.items():
                if not re.search(rf"\b{re.escape(name)}\b", text):
                    missing.append(f"{key}: {prefix}{name}")
                if isinstance(nested, dict):
                    walk(nested, f"{prefix}{name}.")

        walk(call.body)
    assert not missing, f"Body fields not found in the dashboard call: {missing}"


def test_every_dashboard_http_call_exists_in_the_api():
    ops = _operations(_schema())
    found = discover_calls()
    missing = [
        f"{method} {path}  (used at {', '.join(_where(found, (method, path)))})"
        for method, path in sorted(DASHBOARD_CALLS)
        if method != "WEBSOCKET" and (method, path) not in ops
    ]
    assert not missing, "Dashboard calls with no matching API route:\n  " + "\n  ".join(
        missing
    )


def test_every_dashboard_websocket_exists_in_the_app():
    ws_routes = {
        normalise_path(route.path): route
        for route in app.routes
        if isinstance(route, APIWebSocketRoute)
    }
    found = discover_calls()
    problems = []
    for (method, path), call in sorted(DASHBOARD_CALLS.items()):
        if method != "WEBSOCKET":
            continue
        if _sent_query(found, (method, path)) != set(call.query):
            problems.append(
                f"{path}: source sends {_sent_query(found, (method, path))}"
            )
        route = ws_routes.get(path)
        if route is None:
            problems.append(f"{path}: no such WebSocket route")
            continue
        declared = {param.name for param in route.dependant.query_params}
        for name in sorted(set(call.query) - declared):
            problems.append(f"{path}: sends query '{name}', which the route ignores")
    assert not problems, "\n".join(problems)


def test_query_parameters_match():
    ops = _operations(_schema())
    found = discover_calls()
    problems = []
    for key, call in sorted(DASHBOARD_CALLS.items()):
        if key[0] == "WEBSOCKET" or key not in ops:
            continue
        if _sent_query(found, key) != set(call.query):
            problems.append(
                f"{key}: source sends {_sent_query(found, key)}, table says {set(call.query)}"
            )
        params = [p for p in ops[key].get("parameters", []) if p["in"] == "query"]
        declared = {p["name"] for p in params}
        required = {p["name"] for p in params if p.get("required")}
        for name in sorted(set(call.query) - declared):
            problems.append(
                f"{key}: sends query '{name}', which the API does not declare"
            )
        for name in sorted(required - set(call.query)):
            problems.append(f"{key}: required query '{name}' is not sent")
    assert not problems, "\n".join(problems)


def test_request_bodies_match():
    schema = _schema()
    ops = _operations(schema)
    problems = []
    for key, call in sorted(DASHBOARD_CALLS.items()):
        if key not in ops:
            continue
        request = ops[key].get("requestBody")
        if request is None:
            if call.body:
                problems.append(f"{key}: sends a body but the API declares none")
            continue
        model = request["content"]["application/json"]["schema"]
        if request.get("required") and not call.body:
            problems.append(f"{key}: the API requires a body that is not sent")
        problems += _body_mismatches(schema, model, call.body, f"{key[0]} {key[1]}")
    assert not problems, "\n".join(problems)


def test_response_fields_read_exist_where_a_response_model_is_declared():
    schema = _schema()
    ops = _operations(schema)
    problems = []
    for key, call in sorted(DASHBOARD_CALLS.items()):
        if key not in ops or not call.reads:
            continue
        props = _response_properties(schema, ops[key])
        if props is None:
            continue  # no response_model: see the module docstring
        for name in sorted(call.reads - set(props)):
            problems.append(f"{key}: reads '{name}', absent from the response model")
    assert not problems, "\n".join(problems)
