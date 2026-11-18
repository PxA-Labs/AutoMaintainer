"""Retriever protocol, the pgvector retriever over the Supabase RPC, the
in-memory retriever, and the untrusted-data formatting of results (#526)."""

import json

import pytest
from fake_supabase import FakeSupabase

import agents
from automaintainer._runtime.retrieval import (
    CodeChunk,
    HashingEmbedder,
    InMemoryRetriever,
    PgvectorRetriever,
    RetrievedChunk,
    Retriever,
    format_untrusted_chunks,
)

ORG = "aaaaaaaa-0000-4000-8000-0000000000a0"
OTHER_ORG = "bbbbbbbb-0000-4000-8000-0000000000b0"

CHUNKS = [
    CodeChunk(
        "q.py", "PriorityQueue.add", 10, 20, "def add(self, task, priority): ..."
    ),
    CodeChunk("ints.py", "parse_int_list", 1, 9, "def parse_int_list(s): ..."),
]


def _store(sb, org, repo, chunks, embedder):
    vectors = embedder.embed_documents([c.embedding_text for c in chunks])
    sb.tables.setdefault("code_chunks", []).extend(
        {
            "org_id": org,
            "repository_id": repo,
            "commit_sha": "c1",
            "path": c.path,
            "symbol": c.symbol,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "content": c.content,
            "content_sha": c.content_sha,
            "embedding": "[" + ",".join(map(str, v)) + "]",
        }
        for c, v in zip(chunks, vectors)
    )


def test_pgvector_retriever_calls_the_rpc_scoped_to_its_org_and_repository():
    sb, e = FakeSupabase(), HashingEmbedder()
    _store(sb, ORG, 1, CHUNKS, e)
    _store(sb, OTHER_ORG, 2, [CodeChunk("leak.py", "add", 1, 1, "def add(task)")], e)
    r = PgvectorRetriever(sb, e, org_id=ORG, repository_id=1)
    assert isinstance(r, Retriever)

    hits = r.retrieve("priority queue add task", k=5)
    assert [h.path for h in hits] == ["q.py", "ints.py"]
    assert isinstance(hits[0], RetrievedChunk)
    assert hits[0].symbol == "PriorityQueue.add"
    assert (hits[0].start_line, hits[0].end_line) == (10, 20)
    assert hits[0].similarity > hits[1].similarity

    ((name, params),) = sb.rpc_calls
    assert name == "match_code_chunks"
    assert (params["p_org_id"], params["p_repository_id"], params["p_k"]) == (ORG, 1, 5)
    assert len(params["p_query_embedding"]) == 768
    json.dumps(params)  # PostgREST body must be JSON-serialisable


def test_pgvector_retriever_scope_is_fixed_at_construction():
    """The caller (an agent tool) cannot pick another tenant per query."""
    r = PgvectorRetriever(
        FakeSupabase(), HashingEmbedder(), org_id=ORG, repository_id=1
    )
    with pytest.raises(TypeError):
        r.retrieve("x", k=1, org_id=OTHER_ORG)


@pytest.mark.parametrize("k, sent", [(0, None), (-3, None), (100, 50)])
def test_pgvector_retriever_clamps_k(k, sent):
    sb = FakeSupabase()
    r = PgvectorRetriever(sb, HashingEmbedder(), org_id=ORG, repository_id=1)
    assert r.retrieve("x", k=k) == []
    if sent is None:
        assert sb.rpc_calls == []
    else:
        assert sb.rpc_calls[0][1]["p_k"] == sent


@pytest.mark.parametrize("org_id, repository_id", [("", 1), (ORG, 0), (None, 1)])
def test_pgvector_retriever_requires_a_scope(org_id, repository_id):
    with pytest.raises(ValueError):
        PgvectorRetriever(
            FakeSupabase(),
            HashingEmbedder(),
            org_id=org_id,
            repository_id=repository_id,
        )


def test_in_memory_retriever_ranks_like_cosine_and_honours_k():
    r = InMemoryRetriever(CHUNKS, HashingEmbedder())
    assert isinstance(r, Retriever)
    hits = r.retrieve("parse an int list", k=1)
    assert [h.path for h in hits] == ["ints.py"]
    assert r.retrieve("x", k=0) == []


def test_in_memory_retriever_breaks_ties_deterministically():
    same = [CodeChunk(p, None, 1, 1, "same") for p in ("b.py", "a.py")]
    hits = InMemoryRetriever(same, HashingEmbedder()).retrieve("same", k=2)
    assert [h.path for h in hits] == ["a.py", "b.py"]


def test_format_untrusted_chunks_wraps_each_chunk_with_the_existing_notice():
    hits = [
        RetrievedChunk("q.py", "PriorityQueue.add", 10, 20, "def add(): ...", 0.9),
        RetrievedChunk("README.md", None, 1, 3, "# Title", 0.5),
    ]
    text = format_untrusted_chunks(hits)
    assert text.startswith(agents.UNTRUSTED_DATA_NOTICE)
    blocks = text[len(agents.UNTRUSTED_DATA_NOTICE) :]
    assert blocks.count("<untrusted-data ") == 2
    assert blocks.count("</untrusted-data>") == 2
    assert '<untrusted-data source="q.py#L10-L20">' in text
    assert "PriorityQueue.add" in text and "def add(): ..." in text
    assert '<untrusted-data source="README.md#L1-L3">' in text


def test_format_untrusted_chunks_of_nothing_is_empty():
    assert format_untrusted_chunks([]) == ""


def test_format_untrusted_chunks_caps_each_chunk():
    hits = [RetrievedChunk("a.py", None, 1, 1, "x" * 50, 1.0)]
    text = format_untrusted_chunks(hits, max_chars_per_chunk=10)
    assert "x" * 11 not in text and "[truncated]" in text
