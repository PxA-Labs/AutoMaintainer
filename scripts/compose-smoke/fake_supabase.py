"""In-memory stand-in for Supabase's PostgREST API. For the compose smoke test only.

scripts/compose-smoke.sh runs the real stack (API, Celery worker, beat, Redis)
without any real credentials. The API and the worker still need a database to
create a run row, mark it running and record why it ended, so this server
answers the small subset of PostgREST that those code paths use:

- ``GET    /rest/v1/<table>``  select, with ``eq``/``neq``/``is``/``in`` filters
  and ``limit``/``offset``. ``select`` column lists and ``order`` are ignored
  (every column is returned, in insertion order).
- ``POST   /rest/v1/<table>``  insert, or upsert with
  ``Prefer: resolution=merge-duplicates|ignore-duplicates`` and ``on_conflict``.
- ``PATCH  /rest/v1/<table>``  update the rows matching the filters.
- ``DELETE /rest/v1/<table>``  delete the rows matching the filters.
- ``GET    /health``           liveness for the compose healthcheck.

An unsupported filter operator is answered with an error (HTTP 501) instead of
being ignored, so a code path the smoke test was not written for fails loudly
rather than reading the wrong rows. There is no auth, no RLS and no schema:
any table name is accepted. Never point a real deployment at it.

Only the standard library is used, so it runs from the application image
without installing anything. Environment:

- ``FAKE_SUPABASE_PORT``  listen port (default 54321).
- ``FAKE_SUPABASE_SEED``  JSON object ``{"table": [row, ...]}`` loaded at start.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

REST_PREFIX = "/rest/v1/"
# Query parameters that are not column filters.
RESERVED_PARAMS = {"select", "order", "limit", "offset", "on_conflict", "columns"}
SINGLE_OBJECT = "application/vnd.pgrst.object+json"


class PostgrestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "details": None,
            "hint": None,
        }


def _as_text(value) -> str:
    """Render a stored value the way it appears in a PostgREST filter."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _matches(row: dict, column: str, expression: str) -> bool:
    op, _, operand = expression.partition(".")
    actual = row.get(column)
    if op == "eq":
        return actual is not None and _as_text(actual) == operand
    if op == "neq":
        return actual is not None and _as_text(actual) != operand
    if op == "is":
        return _as_text(actual) == operand.lower()
    if op == "in":
        values = [v.strip().strip('"') for v in operand.strip("()").split(",")]
        return actual is not None and _as_text(actual) in values
    raise PostgrestError(501, "PGRST_FAKE", f"unsupported operator '{op}'")


SUPPORTED_OPERATORS = {"eq", "neq", "is", "in"}


def parse_filters(params: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Column filters of a query string; unsupported operators are refused.

    Checked up front, not per row, so the error is raised even when the
    table is empty.
    """
    filters = [(k, v) for k, v in params if k not in RESERVED_PARAMS]
    for _, expression in filters:
        op = expression.partition(".")[0]
        if op not in SUPPORTED_OPERATORS:
            raise PostgrestError(
                501, "PGRST_FAKE", f"fake_supabase does not support the '{op}' operator"
            )
    return filters


class Store:
    """Tables of plain dict rows, guarded by one lock."""

    def __init__(self, seed: dict | None = None):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.tables: dict[str, list[dict]] = {
            table: [dict(row) for row in rows] for table, rows in (seed or {}).items()
        }

    def _rows(self, table: str) -> list[dict]:
        return self.tables.setdefault(table, [])

    def _filtered(self, table: str, filters) -> list[dict]:
        return [
            row
            for row in self._rows(table)
            if all(_matches(row, col, expr) for col, expr in filters)
        ]

    def select(self, table, filters, limit=None, offset=0) -> list[dict]:
        with self._lock:
            rows = self._filtered(table, filters)[offset:]
            if limit is not None:
                rows = rows[:limit]
            return [dict(r) for r in rows]

    def insert(self, table, rows, on_conflict=None, resolution=None) -> list[dict]:
        keys = [c.strip() for c in (on_conflict or "id").split(",")]
        out = []
        with self._lock:
            stored = self._rows(table)
            for row in rows:
                row = dict(row)
                existing = None
                if all(k in row for k in keys):
                    existing = next(
                        (r for r in stored if all(r.get(k) == row[k] for k in keys)),
                        None,
                    )
                if existing is not None:
                    if resolution == "merge-duplicates":
                        existing.update(row)
                        out.append(dict(existing))
                        continue
                    if resolution == "ignore-duplicates":
                        continue
                    raise PostgrestError(
                        409,
                        "23505",
                        f"duplicate key value violates unique constraint on {table}",
                    )
                row.setdefault("id", next(self._ids))
                stored.append(row)
                out.append(dict(row))
        return out

    def update(self, table, filters, patch: dict) -> list[dict]:
        with self._lock:
            rows = self._filtered(table, filters)
            for row in rows:
                row.update(patch)
            return [dict(r) for r in rows]

    def delete(self, table, filters) -> list[dict]:
        with self._lock:
            doomed = self._filtered(table, filters)
            doomed_ids = {id(r) for r in doomed}
            self.tables[table] = [
                r for r in self._rows(table) if id(r) not in doomed_ids
            ]
            return [dict(r) for r in doomed]


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # stdout, so `docker compose logs` shows it
            if self.path == "/health":  # the healthcheck would drown the log
                return
            sys.stdout.write("fake_supabase: " + (fmt % args) + "\n")
            sys.stdout.flush()

        def _send(self, status: int, payload=None, headers=None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            if not self._raw_body:
                return None
            try:
                return json.loads(self._raw_body)
            except ValueError as exc:
                raise PostgrestError(400, "PGRST102", f"invalid JSON body: {exc}")

        def _prefer(self) -> dict:
            prefs = {}
            for part in (self.headers.get("Prefer") or "").split(","):
                key, _, value = part.strip().partition("=")
                if key:
                    prefs[key] = value
            return prefs

        def _respond_rows(self, status: int, rows: list[dict]):
            headers = {
                "Content-Range": (f"0-{len(rows) - 1}/{len(rows)}" if rows else "*/0")
            }
            if SINGLE_OBJECT in (self.headers.get("Accept") or ""):
                if len(rows) != 1:
                    raise PostgrestError(
                        406,
                        "PGRST116",
                        f"JSON object requested, multiple (or no) rows returned: {len(rows)}",
                    )
                return self._send(status, rows[0], headers)
            if self._prefer().get("return") == "minimal":
                return self._send(204 if status == 200 else status, None, headers)
            return self._send(status, rows, headers)

        def _dispatch(self, method: str):
            parts = urlsplit(self.path)
            if parts.path == "/health" and method == "GET":
                return self._send(200, {"status": "ok"})
            if not parts.path.startswith(REST_PREFIX):
                raise PostgrestError(404, "PGRST_FAKE", f"no route for {parts.path}")
            table = unquote(parts.path[len(REST_PREFIX) :]).strip("/")
            if not table or "/" in table:
                raise PostgrestError(404, "PGRST202", f"unsupported resource {table!r}")

            params = parse_qsl(parts.query, keep_blank_values=True)
            query = dict(params)
            filters = parse_filters(params)

            if method == "GET":
                limit = int(query["limit"]) if "limit" in query else None
                rows = store.select(table, filters, limit, int(query.get("offset", 0)))
                return self._respond_rows(200, rows)
            if method == "POST":
                body = self._json_body()
                rows = body if isinstance(body, list) else [body or {}]
                resolution = self._prefer().get("resolution")
                return self._respond_rows(
                    201, store.insert(table, rows, query.get("on_conflict"), resolution)
                )
            if method == "PATCH":
                return self._respond_rows(
                    200, store.update(table, filters, self._json_body() or {})
                )
            if method == "DELETE":
                return self._respond_rows(200, store.delete(table, filters))
            raise PostgrestError(405, "PGRST_FAKE", f"method {method} not supported")

        def _handle(self, method: str):
            # Always consume the body, even for methods that ignore it (the
            # client sends "{}" with DELETE): leftover bytes on a keep-alive
            # connection would be parsed as the start of the next request.
            length = int(self.headers.get("Content-Length") or 0)
            self._raw_body = self.rfile.read(length) if length else b""
            try:
                self._dispatch(method)
            except PostgrestError as exc:
                self._send(exc.status, exc.body())
            except Exception as exc:  # keep serving; report the bug to the client
                self._send(500, PostgrestError(500, "PGRST_FAKE", repr(exc)).body())

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def make_server(host: str, port: int, seed: dict | None = None) -> ThreadingHTTPServer:
    store = Store(seed)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    server.store = store
    return server


def main() -> None:
    port = int(os.getenv("FAKE_SUPABASE_PORT", "54321"))
    seed = json.loads(os.getenv("FAKE_SUPABASE_SEED") or "{}")
    server = make_server("0.0.0.0", port, seed)
    print(f"fake_supabase listening on :{port} with tables {sorted(seed)}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
