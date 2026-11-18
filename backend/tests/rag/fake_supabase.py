"""A small in-memory stand-in for the supabase-py client used by the RAG tests.

Supports what retrieval.py and rag_tasks.py call: table().select/eq/neq/in_/
limit/execute, upsert(on_conflict=..., ignore_duplicates=True), delete(), and
rpc("match_code_chunks", ...) computed in Python with the same filters and
ordering as the SQL function. Embeddings round-trip as pgvector text
("[0.1,0.2]"), as PostgREST returns them.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace


def _vector_text(values) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _parse(value):
    return json.loads(value) if isinstance(value, str) else list(value)


class _Query:
    def __init__(self, db: "FakeSupabase", table: str):
        self.db, self.table = db, table
        self.op = "select"
        self.filters = []
        self.payload = None
        self.on_conflict = None
        self.ignore_duplicates = False
        self.limit_n = None

    # builders
    def select(self, *columns, **_):
        self.op = "select"
        return self

    def upsert(self, rows, *, on_conflict="", ignore_duplicates=False, **_):
        self.op, self.payload = "upsert", list(rows)
        self.on_conflict = [c.strip() for c in on_conflict.split(",") if c.strip()]
        self.ignore_duplicates = ignore_duplicates
        return self

    def delete(self, **_):
        self.op = "delete"
        return self

    def eq(self, column, value):
        self.filters.append(lambda r: r.get(column) == value)
        return self

    def neq(self, column, value):
        self.filters.append(lambda r: r.get(column) != value)
        return self

    def in_(self, column, values):
        values = list(values)
        self.filters.append(lambda r: r.get(column) in values)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def _match(self, row):
        return all(f(row) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.calls.append((self.table, self.op))
        if self.op == "select":
            found = [dict(r) for r in rows if self._match(r)]
            if self.limit_n is not None:
                found = found[: self.limit_n]
            return SimpleNamespace(data=found)
        if self.op == "delete":
            kept = [r for r in rows if not self._match(r)]
            deleted = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = kept
            return SimpleNamespace(data=deleted)
        if self.op == "upsert":
            assert self.ignore_duplicates, "the indexer must not overwrite rows"
            inserted = []
            for row in self.payload:
                row = dict(row)
                if "embedding" in row and not isinstance(row["embedding"], str):
                    row["embedding"] = _vector_text(row["embedding"])
                key = tuple(row[c] for c in self.on_conflict)
                if any(tuple(r[c] for c in self.on_conflict) == key for r in rows):
                    continue
                rows.append(row)
                inserted.append(row)
            return SimpleNamespace(data=inserted)
        raise AssertionError(self.op)


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        assert self.name == "match_code_chunks", self.name
        p = self.params
        query = _parse(p["p_query_embedding"])
        k = max(0, min(int(p["p_k"]), 50))
        scored = []
        for r in self.db.tables.get("code_chunks", []):
            if (
                r["org_id"] != p["p_org_id"]
                or r["repository_id"] != p["p_repository_id"]
            ):
                continue
            emb = _parse(r["embedding"])
            na = math.sqrt(sum(x * x for x in emb)) or 1.0
            nb = math.sqrt(sum(x * x for x in query)) or 1.0
            sim = sum(x * y for x, y in zip(emb, query)) / (na * nb)
            scored.append((sim, r))
        scored.sort(key=lambda t: -t[0])
        data = [
            {
                "path": r["path"],
                "symbol": r["symbol"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "content": r["content"],
                "similarity": sim,
            }
            for sim, r in scored[:k]
        ]
        return SimpleNamespace(data=data)


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.calls = []
        self.rpc_calls = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name, params or {})
