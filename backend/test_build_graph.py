"""build_graph() factory and the lazy module-level clients of agents (#186).

The expected structure below is the graph agents.py compiled at import time
before build_graph() existed; it must not change without a deliberate edit
here.
"""

import inspect
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

import agents

EXPECTED_NODES = ["architect", "brainstormer", "pm", "implementer", "maintainer"]
EXPECTED_EDGES = {
    ("__start__", "architect"),
    ("architect", "brainstormer"),
    ("brainstormer", "pm"),
    ("implementer", "maintainer"),
}
# source node -> {branch name: (routing function, explicit destinations)}
EXPECTED_BRANCHES = {
    "pm": {"should_implement": (agents.should_implement, None)},
    "maintainer": {"should_iterate": (agents.should_iterate, None)},
}
APPROVE = json.dumps({"verdict": "approve"})


def _structure(graph):
    builder = graph.builder
    nodes = list(builder.nodes)
    edges = set(builder.edges)
    branches = {
        source: {name: (b.path.func, b.ends) for name, b in by_name.items()}
        for source, by_name in builder.branches.items()
    }
    return nodes, edges, branches


def test_build_graph_matches_the_previous_module_level_graph():
    nodes, edges, branches = _structure(agents.build_graph())

    assert nodes == EXPECTED_NODES
    assert edges == EXPECTED_EDGES
    assert branches == EXPECTED_BRANCHES


def test_build_graph_compiles_without_checkpointer_or_interrupts():
    # The graph was compiled with a bare compile(); changing that (e.g. a
    # checkpointer for resumable runs) must be a deliberate edit here.
    graph = agents.build_graph()

    assert graph.checkpointer is None
    assert not graph.interrupt_before_nodes
    assert not graph.interrupt_after_nodes


def test_build_graph_uses_the_default_node_functions():
    builder = agents.build_graph().builder
    defaults = {
        "architect": agents.architect_node,
        "brainstormer": agents.brainstormer_node,
        "pm": agents.pm_node,
        "implementer": agents.implementer_node,
        "maintainer": agents.maintainer_node,
    }
    for name, fn in defaults.items():
        runnable = builder.nodes[name].runnable
        # Each node is wrapped by the engine's cancel check (_stoppable).
        assert inspect.unwrap(runnable.afunc or runnable.func) is fn


def test_build_graph_returns_a_new_graph_per_call():
    assert agents.build_graph() is not agents.build_graph()


def test_default_graph_is_built_once_and_is_agents_app(monkeypatch):
    monkeypatch.delitem(vars(agents), "app", raising=False)
    built = []
    real_build = agents.build_graph

    def counting_build():
        built.append(1)
        return real_build()

    monkeypatch.setattr(agents, "build_graph", counting_build)

    graph = agents.get_default_graph()

    assert agents.app is graph
    assert agents.get_default_graph() is graph
    assert len(built) == 1
    assert _structure(graph) == (EXPECTED_NODES, EXPECTED_EDGES, EXPECTED_BRANCHES)


def test_assigned_agents_app_replaces_the_default_graph(monkeypatch):
    fake = object()
    monkeypatch.setattr(agents, "app", fake)

    assert agents.get_default_graph() is fake


async def test_node_overrides_run_through_the_fixed_routing():
    visited = []

    def node(name, **update):
        async def run(state):
            visited.append(name)
            return {"log_messages": [name], **update}

        return run

    graph = agents.build_graph(
        {
            "architect": node("architect"),
            "brainstormer": node("brainstormer"),
            "pm": node("pm", pm_decision=APPROVE),
            "implementer": node("implementer"),
            "maintainer": node("maintainer", review=APPROVE),
        }
    )
    final = await graph.ainvoke({"repo_name": "o/r", "log_messages": []})

    assert visited == EXPECTED_NODES
    assert final["log_messages"] == EXPECTED_NODES


async def test_partial_override_keeps_the_other_default_nodes():
    async def pm(state):
        return {"pm_decision": "reject", "log_messages": []}

    builder = agents.build_graph({"pm": pm}).builder

    assert inspect.unwrap(builder.nodes["pm"].runnable.afunc) is pm
    arch = builder.nodes["architect"].runnable
    assert inspect.unwrap(arch.afunc or arch.func) is agents.architect_node


def test_unknown_node_override_is_rejected():
    with pytest.raises(ValueError, match="reviewer"):
        agents.build_graph({"reviewer": lambda state: {}})


def test_get_supabase_is_none_without_configuration(monkeypatch):
    monkeypatch.delitem(vars(agents), "supabase", raising=False)
    monkeypatch.setattr(agents, "SUPABASE_URL", None)
    monkeypatch.setattr(agents, "SUPABASE_SERVICE_KEY", None)
    create = MagicMock()
    monkeypatch.setattr(agents, "create_client", create)

    assert agents.get_supabase() is None
    assert agents.supabase is None
    create.assert_not_called()


def test_get_supabase_creates_the_client_once_across_threads(monkeypatch):
    monkeypatch.delitem(vars(agents), "supabase", raising=False)
    monkeypatch.setattr(agents, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(agents, "SUPABASE_SERVICE_KEY", "service-key")
    created = []

    def slow_create(url, key):
        time.sleep(0.05)  # widen the race window
        client = object()
        created.append((url, key, client))
        return client

    monkeypatch.setattr(agents, "create_client", slow_create)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(agents.get_supabase()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1
    url, key, client = created[0]
    assert (url, key) == ("https://example.supabase.co", "service-key")
    assert results == [client] * 8
    assert agents.supabase is client
    from agents import supabase as imported

    assert imported is client


def test_assigned_agents_supabase_is_what_the_module_uses(monkeypatch):
    sb = MagicMock()
    monkeypatch.setattr(agents, "supabase", sb)

    assert agents.get_supabase() is sb


def test_unknown_module_attribute_still_raises():
    with pytest.raises(AttributeError):
        agents.no_such_attribute  # noqa: B018
