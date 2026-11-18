"""Embedders: the deterministic HashingEmbedder and the Vertex AI one (#526).

The Vertex embedder is never called for real here: every test hands it a
fake google-genai client.
"""

import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from automaintainer._runtime import retrieval
from automaintainer._runtime.retrieval import (
    EMBEDDING_DIMENSIONS,
    Embedder,
    HashingEmbedder,
    VertexEmbedder,
)

BACKEND = Path(__file__).resolve().parents[2]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# HashingEmbedder
# --------------------------------------------------------------------------


def test_hashing_embedder_is_deterministic_normalised_and_768_wide():
    e = HashingEmbedder()
    assert isinstance(e, Embedder)
    (a,) = e.embed_documents(["def add_task(self, priority): pass"])
    assert a == e.embed_documents(["def add_task(self, priority): pass"])[0]
    assert len(a) == EMBEDDING_DIMENSIONS
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)


def test_hashing_embedder_ranks_shared_identifiers_higher():
    e = HashingEmbedder()
    query = e.embed_query("re-adding a task to the priority queue keeps a stale entry")
    near, far = e.embed_documents(
        [
            "class PriorityQueue:\n    def add(self, task, priority): ...",
            "def parse_int_list(range_string, delim=','): ...",
        ]
    )
    assert _cos(query, near) > _cos(query, far)


def test_hashing_embedder_splits_camel_and_snake_case():
    e = HashingEmbedder()
    q = e.embed_query("priority queue")
    assert _cos(q, e.embed_query("PriorityQueue")) > 0.5
    assert _cos(q, e.embed_query("priority_queue")) > 0.5


def test_hashing_embedder_handles_empty_text():
    (v,) = HashingEmbedder().embed_documents([""])
    assert len(v) == EMBEDDING_DIMENSIONS and any(v)


# --------------------------------------------------------------------------
# VertexEmbedder
# --------------------------------------------------------------------------


class FakeModels:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)

    def embed_content(self, *, model, contents, config):
        self.calls.append(
            SimpleNamespace(model=model, contents=list(contents), config=config)
        )
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(
                    values=[float(len(t))] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
                )
                for t in contents
            ]
        )


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def _vertex(models, **kwargs):
    sleeps = []
    embedder = VertexEmbedder(
        client=SimpleNamespace(models=models),
        project="p",
        sleep=sleeps.append,
        **kwargs,
    )
    return embedder, sleeps


def test_vertex_embedder_batches_and_keeps_order():
    models = FakeModels()
    e, _ = _vertex(models, batch_size=2)
    vectors = e.embed_documents(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [c.contents for c in models.calls] == [
        ["a", "bb"],
        ["ccc", "dddd"],
        ["eeeee"],
    ]
    config = models.calls[0].config
    assert config["task_type"] == "RETRIEVAL_DOCUMENT"
    assert config["output_dimensionality"] == EMBEDDING_DIMENSIONS
    assert models.calls[0].model == "text-embedding-005"


def test_vertex_embedder_uses_the_code_query_task_type_for_queries():
    models = FakeModels()
    e, _ = _vertex(models)
    e.embed_query("why does pop fail")
    assert models.calls[0].config["task_type"] == "CODE_RETRIEVAL_QUERY"


def test_vertex_embedder_reads_model_project_and_location_from_env(monkeypatch):
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "text-embedding-xyz")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("RAG_EMBEDDING_BATCH_SIZE", "7")
    e = VertexEmbedder()
    assert (e.model, e.project, e.location, e.batch_size) == (
        "text-embedding-xyz",
        "proj-1",
        "europe-west4",
        7,
    )


def test_vertex_embedder_defaults(monkeypatch):
    for name in (
        "RAG_EMBEDDING_MODEL",
        "GOOGLE_CLOUD_LOCATION",
        "RAG_EMBEDDING_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    e = VertexEmbedder()
    assert (e.model, e.location) == ("text-embedding-005", "us-central1")
    assert 1 <= e.batch_size <= 250


def test_vertex_embedder_without_a_project_fails_before_any_call(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        VertexEmbedder().embed_query("x")


@pytest.mark.parametrize(
    "error", [FakeAPIError(429), FakeAPIError(503), ConnectionError("reset")]
)
def test_vertex_embedder_retries_transient_errors_with_backoff(error):
    models = FakeModels(failures=[error, error])
    e, sleeps = _vertex(models, max_retries=3)
    assert e.embed_query("x")[0] == 1.0
    assert len(models.calls) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


def test_vertex_embedder_gives_up_after_max_retries():
    models = FakeModels(failures=[FakeAPIError(503)] * 5)
    e, sleeps = _vertex(models, max_retries=2)
    with pytest.raises(FakeAPIError):
        e.embed_query("x")
    assert len(models.calls) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("code", [400, 403, 404])
def test_vertex_embedder_does_not_retry_client_errors(code):
    models = FakeModels(failures=[FakeAPIError(code)])
    e, sleeps = _vertex(models)
    with pytest.raises(FakeAPIError):
        e.embed_query("x")
    assert len(models.calls) == 1 and sleeps == []


def test_vertex_embedder_rejects_a_wrong_sized_response():
    class Short(FakeModels):
        def embed_content(self, *, model, contents, config):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    e, _ = _vertex(Short())
    with pytest.raises(ValueError, match="768"):
        e.embed_query("x")


def test_vertex_embedder_rejects_a_missing_embedding():
    class Missing(FakeModels):
        def embed_content(self, *, model, contents, config):
            return SimpleNamespace(embeddings=[])

    e, _ = _vertex(Missing())
    with pytest.raises(ValueError):
        e.embed_documents(["a"])


def test_importing_retrieval_does_not_import_google_genai():
    """google-genai is the optional rag extra: imported only when embedding."""
    probe = (
        "import sys\n"
        "from automaintainer._runtime import retrieval\n"
        "print('google.genai' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "False"


def test_retrieval_module_has_no_vertex_client_at_import():
    assert not hasattr(retrieval, "genai")
