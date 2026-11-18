"""Offline retrieval recall@k on the e2e task set (#526).

Usage (from backend/)::

    python -m evals.rag_recall                      # HashingEmbedder, k=5
    python -m evals.rag_recall --k 10 --out rag_recall.json
    # Real embeddings: calls Vertex AI, sends the snapshots' code to Google
    # and costs money. Owner action; see docs/RAG.md.
    GOOGLE_CLOUD_PROJECT=... python -m evals.rag_recall --embedder vertex

For every task in evals/e2e/tasks the query is the issue text (title and
body, exactly what the pipeline receives) and the corpus is the task's
``repo/`` snapshot, chunked by retrieval.chunk_repository. The gold is
derived, not hand-labelled: the gold file is the task's ``target_file`` and
the gold chunks are that file's chunks overlapping the lines the oracle fix
(``solution/``) changes. Two hits are counted per task:

* file hit: a chunk of the gold file is in the top k;
* function hit: a gold chunk is in the top k.

``random_*_recall`` is what drawing k chunks uniformly at random would score
on the same snapshot, the chance baseline the numbers must be read against:
the snapshots are small (a handful of files), so file recall is easy.

Exit code: 0, or 2 on a task set error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from math import comb
from pathlib import Path

from automaintainer._runtime.retrieval import (
    CodeChunk,
    Embedder,
    HashingEmbedder,
    InMemoryRetriever,
    VertexEmbedder,
    chunk_repository,
)
from evals.e2e.taskset import TASKS_DIR, E2ETask, load_e2e_tasks
from evals.taskset import TaskError

EMBEDDERS = ("hashing", "vertex")


def changed_lines(old: list[str], new: list[str]) -> set[int]:
    """1-based lines of ``old`` that the change from old to new touches.

    A pure insertion touches the lines on both sides of the insertion point.
    """
    touched: set[int] = set()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:  # insertion before old line i1 + 1
            touched.update(n for n in (i1, i1 + 1) if 1 <= n <= len(old))
        else:
            touched.update(range(i1 + 1, i2 + 1))
    return touched


def gold_chunks(
    chunks: list[CodeChunk], target: str, lines: set[int]
) -> list[CodeChunk]:
    return [
        c
        for c in chunks
        if c.path == target and any(c.start_line <= n <= c.end_line for n in lines)
    ]


def random_recall(*, n: int, gold: int, k: int) -> float:
    """P(at least one of ``gold`` items among k drawn from n without replacement)."""
    if gold <= 0 or n <= 0:
        return 0.0
    k = min(k, n)
    return 1.0 - comb(n - gold, k) / comb(n, k)


@dataclass(frozen=True)
class Case:
    task: E2ETask
    chunks: list
    gold: list
    query: str


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()


def build_case(task: E2ETask) -> Case:
    t = task.task
    chunks = chunk_repository(t.repo_dir)
    lines = changed_lines(
        _lines(t.repo_dir / t.target_file), _lines(t.solution_dir / t.target_file)
    )
    return Case(task, chunks, gold_chunks(chunks, t.target_file, lines), t.issue_text)


def evaluate_task(task: E2ETask, embedder: Embedder, *, k: int) -> dict:
    case = build_case(task)
    target = task.task.target_file
    hits = InMemoryRetriever(case.chunks, embedder).retrieve(case.query, k=k)
    gold_keys = {(c.path, c.start_line) for c in case.gold}
    in_file = sum(1 for c in case.chunks if c.path == target)
    return {
        "task_id": task.id,
        "gold_file": target,
        "gold_symbols": sorted({c.symbol or "<file>" for c in case.gold}),
        "n_chunks": len(case.chunks),
        "file_hit": any(h.path == target for h in hits),
        "function_hit": any((h.path, h.start_line) in gold_keys for h in hits),
        "random_file_recall": random_recall(n=len(case.chunks), gold=in_file, k=k),
        "random_function_recall": random_recall(
            n=len(case.chunks), gold=len(case.gold), k=k
        ),
        "top_k": [
            {
                "path": h.path,
                "symbol": h.symbol,
                "lines": [h.start_line, h.end_line],
                "similarity": round(h.similarity, 6),
            }
            for h in hits
        ],
    }


def make_embedder(name: str) -> Embedder:
    if name == "vertex":
        return VertexEmbedder()
    return HashingEmbedder()


def report(rows: list[dict], *, embedder: str, k: int) -> dict:
    n = len(rows)

    def mean(key):
        return sum(float(r[key]) for r in rows) / n if n else 0.0

    return {
        "embedder": embedder,
        "k": k,
        "n_tasks": n,
        "file_recall_at_k": mean("file_hit"),
        "function_recall_at_k": mean("function_hit"),
        "random_file_recall_at_k": mean("random_file_recall"),
        "random_function_recall_at_k": mean("random_function_recall"),
        "tasks": rows,
    }


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.rag_recall", description=__doc__)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--embedder", choices=EMBEDDERS, default="hashing")
    p.add_argument("--tasks", default="all", help="comma-separated task ids, or 'all'")
    p.add_argument("--tasks-dir", type=Path, default=TASKS_DIR)
    p.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ids = None if args.tasks == "all" else [t for t in args.tasks.split(",") if t]
    try:
        tasks = load_e2e_tasks(ids, tasks_dir=args.tasks_dir)
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    embedder = make_embedder(args.embedder)
    rows = [evaluate_task(t, embedder, k=args.k) for t in tasks]
    result = report(rows, embedder=args.embedder, k=args.k)
    if args.out:
        args.out.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for r in rows:
        print(
            f"{r['task_id']:<36} file={'hit ' if r['file_hit'] else 'miss'} "
            f"function={'hit ' if r['function_hit'] else 'miss'} "
            f"chunks={r['n_chunks']} gold={','.join(r['gold_symbols'])}"
        )
    k = args.k
    print(
        f"\n{args.embedder} embedder, {result['n_tasks']} tasks: "
        f"file recall@{k} {result['file_recall_at_k']:.2f} "
        f"(random {result['random_file_recall_at_k']:.2f}), "
        f"function recall@{k} {result['function_recall_at_k']:.2f} "
        f"(random {result['random_function_recall_at_k']:.2f})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
