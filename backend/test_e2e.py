from agents import AgentState, should_implement, should_iterate, END


def test_agent_conditional_edges():
    approved_state: AgentState = {
        "repo_name": "test/repo",
        "target_issue": None,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "APPROVED: Looks great",
        "code": "",
        "review": "",
        "issue_number": 1,
        "pr_number": 1,
        "branch_name": "feature/test",
        "iteration": 0,
        "log_messages": [],
    }
    assert should_implement(approved_state) == "implementer"

    rejected_state: AgentState = {
        "repo_name": "test/repo",
        "target_issue": None,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "REJECTED: Out of scope",
        "code": "",
        "review": "",
        "issue_number": 1,
        "pr_number": 1,
        "branch_name": "feature/test",
        "iteration": 0,
        "log_messages": [],
    }
    assert should_implement(rejected_state) == END


def test_maintainer_iteration_logic():
    lgtm_state: AgentState = {
        "repo_name": "test/repo",
        "target_issue": None,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "",
        "code": "",
        "review": "LGTM! Code is clean and well tested.",
        "issue_number": 1,
        "pr_number": 1,
        "branch_name": "feature/test",
        "iteration": 0,
        "log_messages": [],
    }
    assert should_iterate(lgtm_state) == END

    bug_state: AgentState = {
        "repo_name": "test/repo",
        "target_issue": None,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "",
        "code": "",
        "review": "SyntaxError on line 12",
        "issue_number": 1,
        "pr_number": 1,
        "branch_name": "feature/test",
        "iteration": 1,
        "log_messages": [],
    }
    assert should_iterate(bug_state) == "implementer"

    max_iter_state: AgentState = {
        "repo_name": "test/repo",
        "target_issue": None,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "",
        "code": "",
        "review": "Still failing tests",
        "issue_number": 1,
        "pr_number": 1,
        "branch_name": "feature/test",
        "iteration": 3,
        "log_messages": [],
    }
    assert should_iterate(max_iter_state) == END
