"""ADK Reviewer reviews inside engine runs (#525, design note rule 5).

Two engines with different model routing and key pools review two
repositories at once, both reviews provably in flight together (a barrier
inside the fake model). Each review must use its own engine's model and
key, see only its own checkout and request, report its tokens to its own
run, and reach its own verdict. A run stopped mid-review never gets an
approval, whether the stop is cooperative (``engine.cancel``) or a task
cancellation (``grace_seconds=0``, ``aclose``).

The review node calls ``reviewer_adk.review_pull_request`` (the maintainer
node's entry point) with in-memory PR objects; the checkout is a directory
per repository, and the model a fake ADK model (no network, no Groq).
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SPECIALISTS = Path(__file__).resolve().parents[1] / "specialists"
if str(SPECIALISTS) not in sys.path:
    sys.path.insert(0, str(SPECIALISTS))

import agents  # noqa: E402
from adk_fakes import APPROVE, REJECT, ModelFactory, calls, needs_adk  # noqa: E402
from automaintainer.config import ModelRouting  # noqa: E402
from automaintainer.engine import AutoMaintainerEngine  # noqa: E402
from automaintainer._specialists import reviewer_adk  # noqa: E402
from events import TelemetryEvent  # noqa: E402

from concurrency_world import (  # noqa: E402
    NOOP_NODES,
    Collector,
    FakeSupabase,
    make_config,
    run_statuses,
    wait_until,
)

pytestmark = needs_adk


class ReviewWorld:
    """Per-repository PRs and checkouts, and the decisions each run got."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.decisions: dict = {}
        self.checkouts: list = []

    def pr_for(self, repo_name):
        tag = repo_name.split("/")[1]
        files = [
            SimpleNamespace(
                filename="app.py", status="modified", patch=f"+owner_{tag} = 1"
            )
        ]
        pr = SimpleNamespace(
            number=1,
            head=SimpleNamespace(sha=f"sha-{tag}", ref=f"branch-{tag}"),
            get_files=lambda: files,
        )
        gh_repo = SimpleNamespace(clone_url=f"file:///{tag}.git")
        return gh_repo, pr

    @asynccontextmanager
    async def checkout(self, clone_url, branch, head_sha):
        tag = branch.split("-", 1)[1]
        root = self.tmp_path / f"head-{tag}-{len(self.checkouts)}"
        root.mkdir()
        (root / "app.py").write_text(f"owner_{tag} = 1\n", encoding="utf-8")
        self.checkouts.append((clone_url, branch, head_sha))
        yield root

    async def node(self, state):
        run_id = agents.current_run_id.get()
        gh_repo, pr = self.pr_for(state["repo_name"])
        decision, _ = await reviewer_adk.review_pull_request(
            {**state, "pr_number": 1, "idea": f"review {state['repo_name']}"},
            gh_repo,
            pr,
            "".join(f.patch for f in pr.get_files()),
        )
        self.decisions[run_id] = decision
        return {}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = ReviewWorld(tmp_path)
    monkeypatch.setattr(reviewer_adk, "checkout_head", w.checkout)
    return w


def engine(tmp_path, world, name, model, **kwargs):
    config = make_config(
        tmp_path,
        name,
        reviewer_backend="adk",
        model_routing=ModelRouting(maintainer=model),
    )
    return AutoMaintainerEngine(
        config,
        supabase=FakeSupabase(),
        emitter=Collector(),
        nodes={**NOOP_NODES, "architect": world.node},
        **kwargs,
    )


async def test_two_engines_running_adk_reviews_stay_isolated(
    tmp_path, world, monkeypatch
):
    both_in_flight = asyncio.Barrier(2)

    async def policy(turn, turns):
        if len(turns) == 1:
            await asyncio.wait_for(both_in_flight.wait(), 10)
            return calls(("read_file", {"path": "app.py"}))
        # Each engine's verdict depends on what its own checkout says.
        return APPROVE if "owner_a = 1" in turn.tool_results[-1] else REJECT

    factory = ModelFactory(policy=policy)
    monkeypatch.setattr(reviewer_adk, "build_model", factory)
    engines = {
        "a": engine(tmp_path, world, "ea", "model-a"),
        "b": engine(tmp_path, world, "eb", "model-b"),
    }

    results = await asyncio.gather(
        *(e.run(f"o/{tag}", run_id=f"run-{tag}") for tag, e in engines.items())
    )
    for e in engines.values():
        await e.aclose()

    assert [r.status for r in results] == ["completed", "completed"]
    assert world.decisions["run-a"].approved
    assert world.decisions["run-b"].verdict == "reject"
    assert sorted(c[1:] for c in world.checkouts) == [
        ("branch-a", "sha-a"),
        ("branch-b", "sha-b"),
    ]
    by_model = {m.model: m for m in factory.built}
    assert set(by_model) == {"model-a", "model-b"}
    for tag in ("a", "b"):
        other = "b" if tag == "a" else "a"
        model = by_model[f"model-{tag}"]
        # Credentials from the engine's own key pool (its rate limiter).
        assert model.api_key in (f"e{tag}-key-1", f"e{tag}-key-2")
        seen = "\n".join(t.everything for t in model.turns)
        assert f"o/{tag}" in seen and f"owner_{tag} = 1" in seen
        assert f"o/{other}" not in seen and f"owner_{other} = 1" not in seen
        # Tokens reach the run's own event stream, under its model.
        telemetry = [
            ev for ev in engines[tag]._emitter.events if isinstance(ev, TelemetryEvent)
        ]
        assert [(ev.run_id, ev.model_name, ev.tokens_used) for ev in telemetry] == [
            (f"run-{tag}", f"model-{tag}", 200)
        ]


async def test_engine_cancel_during_an_adk_review_never_approves(
    tmp_path, world, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def policy(turn, turns):
        entered.set()
        await release.wait()
        return APPROVE  # what the model would say if the review went on

    monkeypatch.setattr(reviewer_adk, "build_model", ModelFactory(policy=policy))
    e = engine(tmp_path, world, "ec", "model-c")
    run = asyncio.create_task(e.run("o/c", run_id="run-c"))
    await asyncio.wait_for(entered.wait(), 10)

    assert e.cancel("run-c")
    release.set()
    result = await run
    await e.aclose()

    assert result.status == "cancelled"
    decision = world.decisions["run-c"]
    assert decision.verdict == "reject"
    assert "cancelled" in decision.reasons[0]


@pytest.mark.parametrize("how", ["grace_zero", "aclose"])
async def test_task_cancellation_during_an_adk_review_never_decides(
    tmp_path, world, monkeypatch, how
):
    entered = asyncio.Event()

    async def policy(turn, turns):
        entered.set()
        await asyncio.sleep(30)
        return APPROVE  # pragma: no cover - cancelled before this

    monkeypatch.setattr(reviewer_adk, "build_model", ModelFactory(policy=policy))
    db = FakeSupabase()
    e = AutoMaintainerEngine(
        make_config(
            tmp_path,
            "ed",
            reviewer_backend="adk",
            model_routing=ModelRouting(maintainer="model-d"),
        ),
        supabase=db,
        emitter=Collector(),
        nodes={**NOOP_NODES, "architect": world.node},
    )
    run = asyncio.create_task(e.run("o/d", run_id="run-d"))
    await asyncio.wait_for(entered.wait(), 10)

    if how == "grace_zero":
        assert e.cancel("run-d", grace_seconds=0)
        result = await run
        await e.aclose()
    else:
        await e.aclose()
        result = await run

    assert result.status == "cancelled"
    assert "run-d" not in world.decisions  # no verdict at all, so no approval
    await wait_until(lambda: run_statuses(db, "run-d")[-1:] == ["cancelled"])
