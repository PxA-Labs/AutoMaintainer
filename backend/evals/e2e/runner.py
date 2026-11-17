"""Run the whole AutoMaintainerEngine on one task (or one Reviewer case).

``run_pipeline_task`` builds the task's offline world (world.py), runs a
targeted run on its issue with the real graph, then scores the final PR head
with the hidden tests. ``run_review_case`` measures the Maintainer step alone:
the harness opens a PR with a known change and the real ``maintainer`` node
reviews it, with the engine's real verification result in its prompt.

The engine configuration mirrors the default deployment where it matters:
``merge_policy="human"`` (an approval leaves the PR for a human, so "merged
without help" is measured as *approved and resolved*), verification on with
the local runner (``runner_type="local"``, the documented opt-in to running
repository tests on the host), the default change limits and three
iterations. The run is started by a repository writer.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

import agents
from automaintainer import AutoMaintainerConfig, AutoMaintainerEngine
from automaintainer.config import ModelRouting
from events import BaseEvent, EventEmitter, NodeTransitionEvent

from .pricing import DEFAULT_PRICES, cost_usd
from .scoring import score_tree
from .taskset import E2ETask, ReviewCase
from .world import MAINTAINER_LOGIN, World, build_world

ORG_ID = "org-eval"
OFFLINE_KEY = "eval-offline-key"
# Offline transports answer instantly; the engine's default client-side
# budgets (30 requests / 6000 tokens per minute) would only add waiting.
OFFLINE_BUDGET = {"requests_per_minute": 100_000, "tokens_per_minute": 100_000_000}
APPROVED_STATUSES = ("approved", "awaiting_human_review", "merged")
_RUN_NAMESPACE = uuid.UUID("6f1c2a52-3d3e-4b8c-9a55-2f0f8a1d9e01")
MAX_OUTPUT_CHARS = 4000


class Collector(EventEmitter):
    def __init__(self):
        self.events: list[BaseEvent] = []

    async def emit(self, event: BaseEvent) -> None:
        self.events.append(event)


def run_id_for(kind: str, case_id: str, repeat: int) -> str:
    """Deterministic, so branch names (and recorded prompts) repeat exactly."""
    return str(uuid.uuid5(_RUN_NAMESPACE, f"{kind}:{case_id}:{repeat}"))


def make_config(
    root: Path,
    *,
    model: Optional[str] = None,
    api_keys: tuple = (OFFLINE_KEY,),
    max_iterations: int = 3,
    requests_per_minute: int = OFFLINE_BUDGET["requests_per_minute"],
    tokens_per_minute: int = OFFLINE_BUDGET["tokens_per_minute"],
) -> AutoMaintainerConfig:
    routing = ModelRouting()
    if model:
        routing = ModelRouting(
            **{
                role: model
                for role in ("architect", "brainstormer", "pm", "implementer")
                + ("maintainer", "inline_assist")
            }
        )
    return AutoMaintainerConfig(
        groq_api_keys=list(api_keys),
        github_auth_mode="pat",
        merge_policy="human",
        verify_changes=True,
        runner_type="local",
        max_iterations=max_iterations,
        workspace_dir=Path(root).resolve() / "workspace",
        model_routing=routing,
        groq_requests_per_minute=requests_per_minute,
        groq_tokens_per_minute=tokens_per_minute,
    )


def _usage_totals(calls: list, prices: dict) -> dict:
    usages = [c.get("usage") for c in calls]
    if not calls or any(not u for u in usages):
        return {"prompt_tokens": None, "completion_tokens": None, "cost_usd": None}
    prompt = sum(int(u.get("input_tokens") or 0) for u in usages)
    completion = sum(int(u.get("output_tokens") or 0) for u in usages)
    costs = [
        cost_usd(
            c.get("model"),
            int(u.get("input_tokens") or 0),
            int(u.get("output_tokens") or 0),
            prices,
        )
        for c, u in zip(calls, usages)
    ]
    total = None if any(c is None for c in costs) else round(sum(costs), 8)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cost_usd": total}


async def _run_engine(world: World, transport, config, run_id, nodes=None):
    world.supabase.table("runs").insert(
        {
            "id": run_id,
            "org_id": ORG_ID,
            "repository_id": 1,
            "repo_name": world.repo_name,
        }
    ).execute()
    events = Collector()
    started = time.perf_counter()
    async with AutoMaintainerEngine(
        config,
        supabase=world.supabase,
        llm=transport,
        github=world.credentials.factory(),
        emitter=events,
        nodes=nodes,
    ) as engine:
        result = await engine.run(
            world.repo_name,
            target_issue=world.issue_number,
            run_id=run_id,
            repository_id=1,
            trigger={"source": "api", "login": MAINTAINER_LOGIN, "github_id": 1},
        )
    return result, events, time.perf_counter() - started


def _base_record(kind: str, case_id: str, repeat: int, run_id: str) -> dict:
    return {
        "kind": kind,
        "id": case_id,
        "repeat": repeat,
        "run_id": run_id,
        "status": None,
        "error_code": None,
        "error": None,
        "llm_calls": 0,
        "stale_calls": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
        "latency_s": None,
        "verification": None,
        "test_output": "",
    }


def _verification_status(world: World) -> Optional[str]:
    rows = world.supabase.rows("verification_results")
    return rows[-1].get("status") if rows else None


def _finish(record, transport, result, latency, prices):
    record["status"] = result.status
    record["error_code"] = result.error_code
    record["latency_s"] = round(latency, 3)
    record["llm_calls"] = len(transport.calls)
    record["stale_calls"] = sum(1 for c in transport.calls if c.get("stale"))
    record.update(_usage_totals(transport.calls, prices))
    if transport.harness_error:
        record["error"] = transport.harness_error


async def run_pipeline_task(
    item: E2ETask,
    *,
    transport,
    root: Path,
    repeat: int = 0,
    model: Optional[str] = None,
    prices: dict = DEFAULT_PRICES,
    timeout: float = 120.0,
    **config_kwargs,
) -> dict:
    """One targeted run of the whole engine on ``item``; a JSON record."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = run_id_for("pipeline", item.id, repeat)
    record = _base_record("pipeline", item.id, repeat, run_id)
    record.update(
        attempted=False,
        pr_opened=False,
        fix_iterations=0,
        maintainer_approved=False,
        resolved=False,
        hidden_tests_passed=None,
        regression=None,
        merged_without_help=False,
        changed_files=[],
    )
    try:
        world = build_world(item, root)
        config = make_config(root, model=model, **config_kwargs)
        result, events, latency = await _run_engine(world, transport, config, run_id)
    except Exception as e:  # harness failure, not an agent outcome
        record["error"] = f"{type(e).__name__}: {e}"
        return record
    _finish(record, transport, result, latency, prices)
    raw = result.raw or {}
    record["fix_iterations"] = sum(
        1
        for e in events.events
        if isinstance(e, NodeTransitionEvent) and e.to_node == "implementer"
    )
    record["attempted"] = record["fix_iterations"] > 0
    record["maintainer_approved"] = raw.get("merge_status") in APPROVED_STATUSES
    record["verification"] = _verification_status(world)
    pr = final_pull(world.repo)
    if pr is not None:
        record["pr_opened"] = True
        record["changed_files"] = [f.filename for f in pr.get_files()]
        tree = world.repo.tree(pr.head.sha)
        score = await asyncio.to_thread(score_tree, item.task, tree, timeout)
        record["resolved"] = score["resolved"]
        record["hidden_tests_passed"] = score["hidden_tests_passed"]
        record["regression"] = score["regression"]
        record["test_output"] = score["test_output"][-MAX_OUTPUT_CHARS:]
    record["merged_without_help"] = bool(
        record["maintainer_approved"] and record["resolved"]
    )
    return record


def final_pull(repo):
    """The pull request whose head is scored: the newest one.

    The engine reuses one branch and PR across iterations, so there is one;
    if an iteration ever opened another, the newest holds the final head.
    """
    return max(repo.pulls.values(), key=lambda pr: pr.number, default=None)


# --- Reviewer --------------------------------------------------------------------


def _review_nodes(world: World, item: E2ETask, case: ReviewCase):
    """Replacement ``pm`` and ``implementer`` nodes: approve, then open the PR
    with the case's change and verify it as the real Implementer does."""
    task = item.task
    changes = case.changes(task)

    async def pm(state):
        return {
            "pm_decision": agents.AgentDecision(
                verdict="approve", reasons=["eval"]
            ).model_dump_json()
        }

    async def implementer(state):
        gh_repo = world.github.get_repo(world.repo_name)
        run_id = agents.current_run_id.get(None)
        branch = agents.run_branch_name(run_id, state["issue_number"])
        base = gh_repo.get_branch(gh_repo.default_branch).commit.sha
        gh_repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base)
        world.repo.commit(branch, changes, f"Fix issue #{state['issue_number']}")
        pr = gh_repo.create_pull(
            title=f"Implement Issue #{state['issue_number']}",
            body=f"Closes #{state['issue_number']}",
            head=branch,
            base=gh_repo.default_branch,
        )
        path = task.target_file
        verify_log = await agents._verify_and_report(gh_repo, pr.number, branch, path)
        code = world.repo.tree(pr.head.sha).get(path, "")
        return {
            "code": code,
            "branch_name": branch,
            "target_file_path": path,
            "pr_number": pr.number,
            "verification": agents._verification_state(verify_log),
            "change_limit_violations": [],
            "log_messages": [],
        }

    return {"pm": pm, "implementer": implementer}


async def run_review_case(
    item: E2ETask,
    case: ReviewCase,
    *,
    transport,
    root: Path,
    repeat: int = 0,
    model: Optional[str] = None,
    prices: dict = DEFAULT_PRICES,
    **config_kwargs,
) -> dict:
    """One Maintainer review of ``case``'s change; a JSON record."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = run_id_for("reviewer", case.id, repeat)
    record = _base_record("reviewer", case.id, repeat, run_id)
    record.update(
        task_id=case.task_id,
        expected_verdict=case.expected_verdict,
        defect_class=case.defect_class,
        verdict=None,
        correct=None,
    )
    try:
        world = build_world(item, root)
        config = make_config(root, model=model, max_iterations=1, **config_kwargs)
        nodes = _review_nodes(world, item, case)
        result, _events, latency = await _run_engine(
            world, transport, config, run_id, nodes=nodes
        )
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        return record
    _finish(record, transport, result, latency, prices)
    record["verification"] = _verification_status(world)
    merge_status = (result.raw or {}).get("merge_status")
    if result.status == "completed" and merge_status:
        record["verdict"] = "approve" if merge_status in APPROVED_STATUSES else "reject"
        record["correct"] = record["verdict"] == case.expected_verdict
    elif record["error"] is None:
        record["error"] = f"review did not complete: {result.error_code} {result.error}"
    return record
