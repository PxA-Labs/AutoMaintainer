import os
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END
from litellm import completion
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent
import httpx
from typing import TypedDict, Annotated
import asyncio
from github import Github
import uuid
import json
from ast_indexer import CodebaseMapper
from contextvars import ContextVar

current_ws = ContextVar("current_ws")

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
gh = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None


def get_all_groq_keys():
    keys = []
    if GROQ_API_KEY:
        keys.append(GROQ_API_KEY)
    for i in range(1, 10):
        k = os.getenv(f"GROQ_API_KEY_{i}")
        if k:
            keys.append(k)
    return keys


import operator


class AgentState(TypedDict):
    repo_name: str
    target_issue: int | None
    architect_directive: str
    idea: str
    pm_decision: str
    code: str
    review: str
    issue_number: int
    pr_number: int
    branch_name: str
    iteration: int
    log_messages: Annotated[list, operator.add]


def run_llm(system_prompt: str, user_prompt: str):
    from litellm.exceptions import RateLimitError

    keys = get_all_groq_keys()
    if not keys:
        raise ValueError("No GROQ_API_KEY found in environment")

    for idx, key in enumerate(keys):
        for model_name in ["groq/llama-3.3-70b-versatile", "groq/llama-3.1-8b-instant"]:
            try:
                response = completion(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    api_key=key,
                )
                return response.choices[0].message.content
            except RateLimitError:
                print(f"Key {idx} rate limited on {model_name}, falling back...")
                continue
    raise Exception("All Groq keys and models are currently rate limited. Please wait a minute.")


async def run_llm_with_tools(system_prompt: str, user_prompt: str):
    try:
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession
        from langchain_mcp_adapters.tools import load_mcp_tools

        server_params = StdioServerParameters(command="gitnexus", args=["mcp"])

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await load_mcp_tools(session)

                keys = get_all_groq_keys()
                if not keys:
                    ws = current_ws.get(None)
                    if ws:
                        await ws.broadcast(
                            {
                                "agent": "System",
                                "msg": "[ERROR] No GROQ_API_KEY found in environment. Agents cannot run.",
                                "color": "text-red-500",
                            }
                        )
                    raise ValueError("No GROQ_API_KEY found in environment")

                llms = [
                    ChatGroq(model="llama-3.3-70b-versatile", api_key=k) for k in keys
                ] + [
                    ChatGroq(model="llama-3.1-8b-instant", api_key=k) for k in keys
                ]
                if len(llms) > 1:
                    llm = llms[0].with_fallbacks(llms[1:])
                else:
                    llm = llms[0]

                agent = create_react_agent(llm, tools=tools)

                final_res = None
                async for chunk in agent.astream(
                    {"messages": [("system", system_prompt), ("user", user_prompt)]},
                    stream_mode="updates",
                ):
                    ws = current_ws.get(None)
                    if "tools" in chunk and ws:
                        for tm in chunk["tools"].get("messages", []):
                            await ws.broadcast(
                                {
                                    "agent": "GitNexus",
                                    "msg": f"🔍 Searched code graph using '{tm.name}'...",
                                    "color": "text-purple-400",
                                }
                            )
                    if "agent" in chunk:
                        final_res = chunk["agent"]

                return final_res["messages"][-1].content
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"MCP Tool execution fallback: {e}")
        try:
            return run_llm(system_prompt, user_prompt)
        except Exception as e2:
            print(f"LLM execution completely failed: {e2}")
            ws = current_ws.get(None)
            if ws:
                asyncio.create_task(ws.broadcast({
                    "agent": "System",
                    "msg": f"LLM Rate Limit Reached: {str(e2)}. Please wait a minute.",
                    "color": "text-red-500"
                }))
            return f"[ERROR] LLM execution failed: {e2}"


async def architect_node(state: AgentState):
    new_logs = []
    repo = state["repo_name"]
    target_issue = state.get("target_issue")
    tree_content = ""
    readme_content = ""

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Architect": "active"}}
    )

    if target_issue and gh:
        try:
            gh_repo = gh.get_repo(repo)
            issue = gh_repo.get_issue(number=target_issue)
            directive = f"Targeted Issue #{target_issue}: {issue.title}\n{issue.body}"
            state["architect_directive"] = directive

            new_logs.append(
                {
                    "type": "ui_update",
                    "pipeline": {
                        "id": f"#{target_issue}",
                        "title": issue.title[:30] + "...",
                        "status": "architecting",
                        "agent": "Architect",
                    },
                }
            )
            new_logs.append(
                {
                    "agent": "Architect",
                    "msg": f"Targeting specific issue #{target_issue}: {issue.title}",
                    "color": "text-rose-400",
                }
            )
            new_logs.append(
                {"type": "ui_update", "agentStatus": {"Architect": "idle"}}
            )
            return {"architect_directive": directive, "log_messages": new_logs} # ARCHITECT EARLY RETURN
        except Exception as e:
            new_logs.append(
                {
                    "agent": "Architect",
                    "msg": f"Failed to fetch issue #{target_issue}: {str(e)}",
                    "color": "text-red-500",
                }
            )

    if gh:
        try:
            gh_repo = gh.get_repo(repo)
            contents = gh_repo.get_contents("")
            tree_content = "\n".join([c.path for c in contents])
            try:
                readme = gh_repo.get_readme()
                readme_content = readme.decoded_content.decode("utf-8")
            except Exception:
                readme_content = "No README found."
        except Exception as e:
            tree_content = "Unable to fetch repo tree."
            readme_content = "Inaccessible."

    # Clone the repo locally and analyze it with GitNexus so the MCP server has data
    import subprocess
    import shutil

    repo_dir = f"/tmp/{repo.replace('/', '_')}"
    repo_url = (
        f"https://x-access-token:{GITHUB_TOKEN}@github.com/{repo}.git"
        if GITHUB_TOKEN
        else f"https://github.com/{repo}.git"
    )
    safe_repo_url = f"https://github.com/{repo}.git"

    if not os.path.exists(repo_dir):
        try:
            clone_proc = await asyncio.create_subprocess_exec(
                "git", "clone", repo_url, repo_dir
            )
            await clone_proc.communicate()
        except Exception as e:
            print(f"Failed to clone repo {safe_repo_url}: {e}")
    else:
        try:
            pull_proc = await asyncio.create_subprocess_exec(
                "git", "pull", "--ff-only", cwd=repo_dir
            )
            await pull_proc.communicate()
        except Exception as e:
            print(f"Failed to pull repo {safe_repo_url}, continuing with cache: {e}")

    if not os.path.exists(f"{repo_dir}/.gitnexus"):
        try:
            # Run gitnexus asynchronously so it doesn't block the FastAPI event loop
            analyze_proc = await asyncio.create_subprocess_exec(
                "gitnexus", "analyze", cwd=repo_dir
            )
            await analyze_proc.communicate()

            index_proc = await asyncio.create_subprocess_exec(
                "gitnexus", "index", cwd=repo_dir
            )
            await index_proc.communicate()
        except Exception as e:
            warn_msg = (
                f"⚠️ GitNexus indexing failed (agents will use raw LLM context): {e}"
            )
            new_logs.append(
                {"agent": "System", "msg": warn_msg, "color": "text-amber-400"}
            )
            ws = current_ws.get(None)
            if ws:
                await ws.broadcast(
                    {"agent": "System", "msg": warn_msg, "color": "text-amber-400"}
                )
            print(f"Failed to analyze repo with GitNexus: {e}")

            # Clean up partially created .gitnexus cache
            gitnexus_dir = os.path.join(repo_dir, ".gitnexus")
            if os.path.exists(gitnexus_dir):
                try:
                    shutil.rmtree(gitnexus_dir)
                    info_msg = "ℹ️ Cleaned up partial GitNexus cache directory."
                    new_logs.append(
                        {"agent": "System", "msg": info_msg, "color": "text-zinc-500"}
                    )
                    if ws:
                        await ws.broadcast(
                            {
                                "agent": "System",
                                "msg": info_msg,
                                "color": "text-zinc-500",
                            }
                        )
                except Exception as clean_err:
                    print(f"Failed to clean up partial GitNexus cache: {clean_err}")

    # Generate AST Map for LLM Context
    ast_context_str = "No AST available."
    if os.path.isdir(repo_dir):
        try:
            mapper = CodebaseMapper(repo_dir)
            arch_map = mapper.generate_architecture_map()

            MAX_CHARS = 15000
            current_chars = 0
            ast_lines = ["\nRepository AST Structure:"]

            for file_path, data in arch_map.items():
                classes = data.get("classes", [])
                functions = data.get("functions", [])
                if not classes and not functions:
                    continue

                # Check if adding this file will exceed the limit
                file_chunk = f"File: {file_path}\n"
                if classes:
                    file_chunk += "  Classes:\n"
                    for c in classes:
                        file_chunk += (
                            f"    - {c['name']} (L{c['start_line']}-L{c['end_line']})\n"
                        )
                if functions:
                    file_chunk += "  Functions:\n"
                    for f in functions:
                        file_chunk += (
                            f"    - {f['name']} (L{f['start_line']}-L{f['end_line']})\n"
                        )

                if current_chars + len(file_chunk) > MAX_CHARS:
                    ast_lines.append("\n...AST truncated due to token limit")
                    break

                ast_lines.append(file_chunk.strip())
                current_chars += len(file_chunk)

            ast_context_str = "\n".join(ast_lines)
        except Exception as e:
            print(f"AST parsing failed locally: {e}")
            new_logs.append(
                {
                    "agent": "Architect",
                    "msg": f"AST parsing failed: {str(e)}",
                    "color": "text-amber-500",
                }
            )
    else:
        new_logs.append(
            {
                "agent": "Architect",
                "msg": f"AST generation skipped: repo_dir {repo_dir} not found.",
                "color": "text-amber-500",
            }
        )

    system_prompt = 'You are the Principal Architect. Analyze the provided repository root file structure, AST structure, and README context. Assess the current state of the project (is it working, what tech stack is it using) and give a strict 2-sentence directive on what the team should build or fix next. You MUST explicitly include precise file paths and start–end line ranges (e.g., "file.py:10-25") for any delegated changes. Ensure these exact line-number citations are present in the generated directive.'
    user_prompt = f"Repo: {repo}\n\nFiles:\n{tree_content}\n\nREADME:\n{readme_content[:1000]}\n\n{ast_context_str}\n\nGenerate the architect_directive."

    directive = await run_llm_with_tools(system_prompt, user_prompt)
    state["architect_directive"] = directive

    new_logs.append(
        {
            "type": "ui_update",
            "pipeline": {
                "id": "#NEW",
                "title": directive[:30] + "...",
                "status": "architecting",
                "agent": "Architect",
            },
        }
    )
    new_logs.append(
        {
            "agent": "Architect",
            "msg": f"Directive: {directive}",
            "color": "text-rose-400",
        }
    )
    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Architect": "idle"}}
    )
    return {"architect_directive": state.get("architect_directive", ""), "log_messages": new_logs}

async def brainstormer_node(state: AgentState):
    new_logs = []
    repo = state["repo_name"]
    directive = state.get("architect_directive", "")
    target_issue = state.get("target_issue")

    if target_issue:
        state["idea"] = directive
        state["issue_number"] = target_issue
        new_logs.append(
            {"type": "ui_update", "agentStatus": {"Visionary": "active"}}
        )
        new_logs.append(
            {
                "agent": "Visionary",
                "msg": f"Bypassing brainstorm. Focusing on Issue #{target_issue}",
                "color": "text-emerald-400",
            }
        )
        new_logs.append(
            {"type": "ui_update", "agentStatus": {"Visionary": "idle"}}
        )
        return {"idea": directive, "issue_number": target_issue, "log_messages": new_logs}

    system_prompt = "You are the Visionary Agent. Your job is to brainstorm one single, highly innovative feature that fulfills the Architect's directive."
    user_prompt = f"Architect Directive:\n{directive}\n\nBrainstorm a new feature for {repo}. Keep it under 3 sentences."

    idea = await run_llm_with_tools(system_prompt, user_prompt)
    state["idea"] = idea

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Visionary": "active"}}
    )
    new_logs.append(
        {
            "type": "ui_update",
            "pipeline": {
                "id": "#NEW",
                "title": idea[:30] + "...",
                "status": "brainstorming",
                "agent": "Visionary",
            },
        }
    )
    new_logs.append(
        {
            "agent": "Visionary",
            "msg": f"Proposed Feature: {idea}",
            "color": "text-emerald-400",
        }
    )

    if gh:
        try:
            gh_repo = gh.get_repo(repo)
            issue = gh_repo.create_issue(
                title="[Feature Request] Implement proposed architecture enhancements",
                body=f"### Architect Directive\n{directive}\n\n### Proposed Feature\n{idea}",
            )
            state["issue_number"] = issue.number
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Created GitHub Issue #{issue.number}",
                    "color": "text-emerald-500",
                }
            )
        except Exception as e:
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Failed to create Issue: {str(e)}",
                    "color": "text-red-500",
                }
            )

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Visionary": "idle"}}
    )
    return {"idea": idea, "issue_number": state.get("issue_number", 0), "log_messages": new_logs}

async def pm_node(state: AgentState):
    new_logs = []
    idea = state["idea"]
    directive = state.get("architect_directive", "")
    repo = state["repo_name"]
    issue_number = state.get("issue_number")
    target_issue = state.get("target_issue")

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Reviewer": "active"}}
    )
    new_logs.append(
        {
            "type": "ui_update",
            "pipeline": {
                "id": f"#{issue_number}" if issue_number else "#NEW",
                "title": idea.replace("\n", " ")[:30] + "...",
                "status": "reviewing",
                "agent": "Reviewer",
            },
        }
    )

    if target_issue:
        decision = "APPROVED (Auto-approved by Targeted Issue Mode)"
        state["pm_decision"] = decision
        new_logs.append(
            {
                "agent": "Reviewer",
                "msg": f"Decision: {decision}",
                "color": "text-amber-400",
            }
        )
        new_logs.append(
            {"type": "ui_update", "agentStatus": {"Reviewer": "idle"}}
        )
        return {"pm_decision": decision, "log_messages": new_logs}

    system_prompt = "You are the Product Manager. Review the proposed feature against the Architect's directive. Decide if we should build it ('APPROVED') or not ('REJECTED'). Start your response with APPROVED or REJECTED, then give a 1 sentence reason."

    decision = await run_llm_with_tools(
        system_prompt, f"Directive: {directive}\n\nReview this idea: {idea}"
    )
    state["pm_decision"] = decision

    is_approved = decision.strip().upper().startswith("APPROVED")
    msg_color = "text-amber-400" if is_approved else "text-red-400"
    new_logs.append(
        {"agent": "Reviewer", "msg": f"Decision: {decision}", "color": msg_color}
    )

    if gh and issue_number:
        try:
            gh_repo = gh.get_repo(repo)
            issue = gh_repo.get_issue(number=issue_number)
            issue.create_comment(f"**Reviewer Decision:** {decision}")
            if not is_approved:
                issue.edit(state="closed")
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Commented on Issue #{issue_number}",
                    "color": "text-zinc-500",
                }
            )
        except Exception as e:
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Failed to comment on Issue: {str(e)}",
                    "color": "text-red-500",
                }
            )

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Reviewer": "idle"}}
    )
    return {"pm_decision": decision, "log_messages": new_logs}

def should_implement(state: AgentState):
    return (
        "implementer"
        if state.get("pm_decision", "").strip().upper().startswith("APPROVED")
        else END
    )


async def implementer_node(state: AgentState):
    new_logs = []
    idea = state["idea"]
    issue_number = state.get("issue_number")
    iteration = state.get("iteration", 0)
    prev_code = state.get("code", "")
    review = state.get("review", "")

    if iteration > 0:
        system_prompt = "You are the Implementer Agent. Your previous code was rejected by the Maintainer. Fix it based on their feedback. Output ONLY the fixed Python script."
        user_prompt = f"Previous Code:\n{prev_code}\n\nMaintainer Feedback:\n{review}"
    else:
        system_prompt = "You are the Implementer Agent. Write a tiny python script that implements the core logic of the idea. Keep it very short. Output ONLY the Python script."
        user_prompt = f"Write code for: {idea}"

    code = await run_llm_with_tools(system_prompt, user_prompt)
    state["code"] = code

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Implementer": "active"}}
    )

    if iteration > 0:
        new_logs.append(
            {
                "agent": "Implementer",
                "msg": f"Fixed code based on feedback (Iteration {iteration}).",
                "color": "text-blue-400",
            }
        )
    else:
        new_logs.append(
            {
                "agent": "Implementer",
                "msg": "Generated initial code implementation.",
                "color": "text-blue-400",
            }
        )
        new_logs.append(
            {
                "type": "ui_update",
                "pipeline": {
                    "id": f"#{issue_number}" if issue_number else "#NEW",
                    "title": idea.replace("\n", " ")[:30] + "...",
                    "status": "implementing",
                    "agent": "Implementer",
                },
            }
        )

    if gh and issue_number:
        try:
            gh_repo = gh.get_repo(state["repo_name"])

            code_to_commit = code
            if "```python" in code:
                code_to_commit = code.split("```python")[1].split("```")[0].strip()
            elif "```" in code:
                code_to_commit = code.split("```")[1].split("```")[0].strip()

            path = f"feature_issue_{issue_number}.py"

            if iteration == 0:
                default_branch = gh_repo.default_branch
                sb = gh_repo.get_branch(default_branch)
                branch_name = f"feature/issue-{issue_number}-{uuid.uuid4().hex[:4]}"
                state["branch_name"] = branch_name
                gh_repo.create_git_ref(
                    ref=f"refs/heads/{branch_name}", sha=sb.commit.sha
                )

                gh_repo.create_file(
                    path=path,
                    message=f"Implement Feature for Issue #{issue_number}",
                    content=code_to_commit,
                    branch=branch_name,
                )

                pr = gh_repo.create_pull(
                    title=f"Implement Feature Request #{issue_number}",
                    body=f"This PR resolves #{issue_number}.\n\nCloses #{issue_number}",
                    head=branch_name,
                    base=default_branch,
                )
                state["pr_number"] = pr.number
                new_logs.append(
                    {
                        "agent": "System",
                        "msg": f"Created PR #{pr.number}: {pr.html_url}",
                        "color": "text-emerald-500",
                    }
                )
            else:
                branch_name = state["branch_name"]
                file = gh_repo.get_contents(path, ref=branch_name)
                gh_repo.update_file(
                    path=file.path,
                    message=f"Fix: Address maintainer feedback (Iteration {iteration})",
                    content=code_to_commit,
                    sha=file.sha,
                    branch=branch_name,
                )
                pr_number = state["pr_number"]
                pr = gh_repo.get_pull(pr_number)
                pr.create_issue_comment(
                    f"I have pushed a new commit to address the feedback. (Iteration {iteration})"
                )
                new_logs.append(
                    {
                        "agent": "System",
                        "msg": f"Pushed fix to PR #{pr_number}",
                        "color": "text-emerald-500",
                    }
                )

        except Exception as e:
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Failed GitHub API Action: {str(e)}",
                    "color": "text-red-500",
                }
            )

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Implementer": "idle"}}
    )
    return {"code": code, "branch_name": state.get("branch_name", ""), "pr_number": state.get("pr_number", 0), "log_messages": new_logs}

async def maintainer_node(state: AgentState):
    new_logs = []
    code = state["code"]
    repo_name = state["repo_name"]
    pr_number = state.get("pr_number")
    iteration = state.get("iteration", 0)

    system_prompt = "You are the Maintainer. Review the code. Say 'LGTM' if it looks okay, or point out a flaw."
    review = await run_llm_with_tools(system_prompt, f"Review this code:\n{code}")
    state["review"] = review

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Maintainer": "active"}}
    )
    new_logs.append(
        {
            "agent": "Maintainer",
            "msg": f"Code Review: {review}",
            "color": "text-purple-400",
        }
    )

    is_lgtm = "LGTM" in review.upper()

    if gh and pr_number:
        try:
            gh_repo = gh.get_repo(repo_name)
            pr = gh_repo.get_pull(pr_number)
            pr.create_issue_comment(f"**Maintainer Review:**\n{review}")

            if is_lgtm:
                pr.merge(commit_message=f"Merged PR #{pr_number}")
                new_logs.append(
                    {
                        "type": "ui_update",
                        "activity": {
                            "title": f"Merged PR #{pr_number}",
                            "time": "Just now",
                            "type": "merge",
                        },
                    }
                )
                new_logs.append(
                    {
                        "agent": "System",
                        "msg": f"Successfully merged PR #{pr_number}!",
                        "color": "text-emerald-500",
                    }
                )
        except Exception as e:
            new_logs.append(
                {
                    "agent": "System",
                    "msg": f"Failed to review/merge PR: {str(e)}",
                    "color": "text-red-500",
                }
            )

    if not is_lgtm:
        state["iteration"] = iteration + 1

    new_logs.append(
        {"type": "ui_update", "agentStatus": {"Maintainer": "idle"}}
    )
    return {"review": review, "iteration": state.get("iteration", 0), "log_messages": new_logs}

def should_iterate(state: AgentState):
    if "LGTM" in state.get("review", "").upper():
        return END
    if state.get("iteration", 0) >= 3:
        return END
    return "implementer"


# Build the Graph
workflow = StateGraph(AgentState)

workflow.add_node("architect", architect_node)
workflow.add_node("brainstormer", brainstormer_node)
workflow.add_node("pm", pm_node)
workflow.add_node("implementer", implementer_node)
workflow.add_node("maintainer", maintainer_node)

workflow.set_entry_point("architect")
workflow.add_edge("architect", "brainstormer")
workflow.add_edge("brainstormer", "pm")
workflow.add_conditional_edges("pm", should_implement)
workflow.add_edge("implementer", "maintainer")
workflow.add_conditional_edges("maintainer", should_iterate)

app = workflow.compile()


async def run_agent_loop(repo_name: str, ws_manager, target_issue: int | None = None):
    current_ws.set(ws_manager)

    if repo_name.startswith("http://") or repo_name.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(repo_name)
        if parsed.netloc in ["github.com", "www.github.com"]:
            repo_name = parsed.path.strip("/")

    initial_state = {
        "repo_name": repo_name,
        "target_issue": target_issue,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "",
        "code": "",
        "review": "",
        "issue_number": 0,
        "pr_number": 0,
        "branch_name": "",
        "iteration": 0,
        "log_messages": [],
    }

    await ws_manager.broadcast(
        {
            "agent": "System",
            "msg": f"Starting loop for repo: {repo_name}...",
            "color": "text-zinc-500",
        }
    )

    last_idx = 0
    async for state in app.astream(initial_state, stream_mode="values"):

        new_msgs = state["log_messages"][last_idx:]
        for msg in new_msgs:
            await ws_manager.broadcast(msg)
            await asyncio.sleep(0.5)

        last_idx = len(state["log_messages"])

    await ws_manager.broadcast(
        {"agent": "System", "msg": "Agent loop complete.", "color": "text-zinc-500"}
    )
