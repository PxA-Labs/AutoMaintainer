"""Offline retrieval recall on the e2e task set (#526)."""

import json
import math

import pytest

from automaintainer._runtime.retrieval import CodeChunk, HashingEmbedder
from evals import rag_recall
from evals.e2e.taskset import load_e2e_tasks


def test_changed_lines_cover_replacements_deletions_and_insertions():
    old = ["a", "b", "c", "d"]
    assert rag_recall.changed_lines(old, ["a", "B", "c", "d"]) == {2}
    assert rag_recall.changed_lines(old, ["a", "c", "d"]) == {2}
    # An insertion between lines 2 and 3 touches both neighbours.
    assert rag_recall.changed_lines(old, ["a", "b", "x", "c", "d"]) == {2, 3}
    assert rag_recall.changed_lines(old, ["x"] + old) == {1}
    assert rag_recall.changed_lines(old, old + ["x"]) == {4}
    assert rag_recall.changed_lines(old, old) == set()


def test_gold_chunks_are_the_target_file_chunks_touching_the_fix():
    chunks = [
        CodeChunk("t.py", "f", 1, 3, "def f"),
        CodeChunk("t.py", "g", 5, 9, "def g"),
        CodeChunk("other.py", "g", 5, 9, "def g"),
    ]
    gold = rag_recall.gold_chunks(chunks, "t.py", {6})
    assert [(c.path, c.symbol) for c in gold] == [("t.py", "g")]


def test_expected_random_recall():
    # 1 gold among 4, k = 2: 1 - C(3,2)/C(4,2) = 1 - 3/6.
    assert rag_recall.random_recall(n=4, gold=1, k=2) == pytest.approx(0.5)
    assert rag_recall.random_recall(n=3, gold=1, k=5) == 1.0
    assert rag_recall.random_recall(n=10, gold=0, k=5) == 0.0


def test_every_e2e_task_has_a_gold_function():
    for task in load_e2e_tasks():
        case = rag_recall.build_case(task)
        assert case.gold, task.id
        assert all(c.path == task.task.target_file for c in case.gold)


def test_evaluate_one_task_with_the_hashing_embedder():
    (task,) = load_e2e_tasks(["boltons_pq_readd"])
    row = rag_recall.evaluate_task(task, HashingEmbedder(), k=5)
    assert row["task_id"] == "boltons_pq_readd"
    assert row["gold_file"] == "boltons/queueutils.py"
    assert row["gold_symbols"]
    assert isinstance(row["file_hit"], bool) and isinstance(row["function_hit"], bool)
    # A function hit is a file hit.
    assert row["file_hit"] or not row["function_hit"]
    assert len(row["top_k"]) == 5
    assert 0.0 < row["random_function_recall"] <= 1.0


def test_report_aggregates_and_the_cli_writes_json(tmp_path, capsys):
    out = tmp_path / "recall.json"
    assert rag_recall.main(["--k", "5", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    rows = report["tasks"]
    assert report["embedder"] == "hashing" and report["k"] == 5
    assert report["n_tasks"] == len(rows) == len(load_e2e_tasks())
    file_recall = sum(r["file_hit"] for r in rows) / len(rows)
    function_recall = sum(r["function_hit"] for r in rows) / len(rows)
    assert math.isclose(report["file_recall_at_k"], file_recall)
    assert math.isclose(report["function_recall_at_k"], function_recall)
    printed = capsys.readouterr().out
    assert "function recall@5" in printed and "file recall@5" in printed


def test_the_cli_is_deterministic(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert (
        rag_recall.main(["--k", "3", "--out", str(a), "--tasks", "boltons_pq_readd"])
        == 0
    )
    assert (
        rag_recall.main(["--k", "3", "--out", str(b), "--tasks", "boltons_pq_readd"])
        == 0
    )
    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")


def test_vertex_embedder_is_opt_in_and_built_only_when_asked(monkeypatch):
    built = []

    class Fake(HashingEmbedder):
        def __init__(self):
            built.append(True)
            super().__init__()

    monkeypatch.setattr(rag_recall, "VertexEmbedder", Fake)
    assert rag_recall.make_embedder("hashing").__class__ is HashingEmbedder
    assert built == []
    assert isinstance(rag_recall.make_embedder("vertex"), Fake)
    assert built == [True]
