"""N engines with different configurations, two runs each, in one event loop.

Every run waits at a barrier inside its probe node until all 2N runs are
there, so each sample is taken while every other engine is mid-run. A leak
of any per-engine client or setting (Supabase client, rate limiter, App
identity, installation token, workspace root and clone directory, merge
ceiling, change limits, events, DB rows) shows up as a sample or a recorded
write that names another engine.
"""

import asyncio
from pathlib import Path

import pytest

import agents
import github_app
import rate_limiter
import workspace
from automaintainer.context import active_engine
from events import TelemetryEvent

from concurrency_world import (
    INSTALLATION_ID,
    REPO,
    ProbeWorld,
    _run_of,
    build_engines,
    snapshot,
)

RUNS_PER_ENGINE = 2


@pytest.mark.parametrize("count", [4, 8])
async def test_engines_running_concurrently_share_nothing(tmp_path, count):
    world = ProbeWorld(parties=count * RUNS_PER_ENGINE)
    specs = build_engines(tmp_path, count, world.nodes())
    outside_before = snapshot()

    async def run_all():
        calls = []
        for spec in specs:
            for j in range(RUNS_PER_ENGINE):
                run_id = f"e{spec.index}-run{j}"
                spec.run_ids.append(run_id)
                calls.append(
                    spec.engine.run(
                        REPO,
                        run_id=run_id,
                        installation_id=INSTALLATION_ID,
                        repository_id=42,
                    )
                )
        return await asyncio.gather(*calls)

    results = await run_all()
    limiters = {spec.index: spec.engine._rate_limiter for spec in specs}
    for spec in specs:
        await spec.engine.aclose()

    assert [r.status for r in results] == ["completed"] * len(results)
    assert set(world.records) == {r for s in specs for r in s.run_ids}

    for spec in specs:
        expected = {
            "engine": spec.engine.engine_id,
            "supabase": spec.db,
            "rate_limiter": limiters[spec.index],
            "app_id": spec.app_id,
            "token": spec.expected_token,
            "slug": f"app-{spec.app_id}",
            "workspace": spec.workspace,
            "clone_dir": str(Path(spec.workspace) / workspace.REPO_ID_DIR / "42"),
            "merge_ceiling": spec.merge_policy,
            "max_files": spec.max_files,
            "verify": spec.verify,
            "keys": spec.keys,
        }
        for run_id in spec.run_ids:
            record = world.records[run_id]
            for when in ("before", "after", "child"):
                seen = dict(record[when])
                assert seen.pop("run_id") == run_id, (run_id, when)
                assert seen == expected, (run_id, when)
            assert record["thread"] == {
                "engine": spec.engine.engine_id,
                "run_id": run_id,
                "supabase": spec.db,
                "app_id": spec.app_id,
                "workspace": spec.workspace,
                "token": spec.expected_token,
            }
            assert record["answer"] == f"answer from {spec.model}"

        # Every DB write (runs row, logs, the probe row) is this engine's.
        assert {_run_of(w) for w in spec.db.writes} == set(spec.run_ids)
        probe_rows = [w[2] for w in spec.db.writes if w[0] == "probe"]
        assert sorted(r["run_id"] for r in probe_rows) == sorted(spec.run_ids)
        assert {r["engine"] for r in probe_rows} == {spec.engine.engine_id}
        completed = [w for w in spec.db.writes if w[0] == "runs" and w[1] == "update"]
        assert sorted(_run_of(w) for w in completed) == sorted(spec.run_ids)
        assert {w[2]["status"] for w in completed} == {"completed"}

        # Events reached only this engine's emitter.
        assert {e.run_id for e in spec.events.events} == set(spec.run_ids)
        telemetry = [e for e in spec.events.events if isinstance(e, TelemetryEvent)]
        assert [e.model_name for e in telemetry] == [
            f"answer from {spec.model}"
        ] * RUNS_PER_ENGINE

        # LLM: routed model, keys from the engine's own pool only.
        assert [m for m, _ in spec.llm.calls] == [spec.model] * RUNS_PER_ENGINE
        assert {k for _, k in spec.llm.calls} <= set(spec.keys)

        # Injected credentials factory: called by its own engine's runs only.
        if spec.injected_github:
            assert spec.factory_calls == [(INSTALLATION_ID, REPO)] * RUNS_PER_ENGINE
        else:
            assert spec.factory_calls == []

        # Per-engine caches.
        assert spec.engine._context.slug_cache == {"slug": f"app-{spec.app_id}"}
        assert spec.engine._rate_limiter is None  # released by aclose
        assert spec.engine._runs == {}

    # One rate limiter per engine, none shared, the process one untouched.
    assert len({id(m) for m in limiters.values()}) == count
    assert all(m is not None for m in limiters.values())
    assert rate_limiter._rate_limit_manager is None
    # Installation tokens: one cache entry per minting App, none for engines
    # with an injected factory, all for the same installation id.
    minting = sorted(s.app_id for s in specs if not s.injected_github)
    keys = list(github_app._installation_token_cache)
    assert sorted(k[3] for k in keys) == minting
    assert {k[0] for k in keys} == {INSTALLATION_ID}
    for key, (token, _expires) in github_app._installation_token_cache.items():
        assert token == f"tok-{key[3]}-{INSTALLATION_ID}"
    # Process-wide fallbacks were never used.
    assert agents._github_app_slug_cache == {}
    assert workspace._scrubbed_roots == {s.workspace for s in specs}
    # Nothing bound by the runs is visible here afterwards.
    assert snapshot() == outside_before
    assert active_engine() is None
    assert await asyncio.to_thread(active_engine) is None


async def test_same_run_id_on_two_engines_stays_isolated(tmp_path):
    """run_id is unique per engine, not per process: two engines may run the
    same id at once, each against its own database and emitter."""
    world = ProbeWorld(parties=2)
    a, b = build_engines(tmp_path, 2, world.nodes())

    results = await asyncio.gather(
        a.engine.run(REPO, run_id="shared", installation_id=INSTALLATION_ID),
        b.engine.run(REPO, run_id="shared", installation_id=INSTALLATION_ID),
    )

    assert [r.status for r in results] == ["completed", "completed"]
    for spec in (a, b):
        assert {_run_of(w) for w in spec.db.writes} == {"shared"}
        assert len([w for w in spec.db.writes if w[0] == "probe"]) == 1
        assert [w[2]["engine"] for w in spec.db.writes if w[0] == "probe"] == [
            spec.engine.engine_id
        ]
        assert len(spec.llm.calls) == 1
