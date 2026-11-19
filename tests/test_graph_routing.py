"""Routing, HITL gate and checkpointer-rollback assertions (no HTTP involved)."""

from langgraph.graph import END

from app.graph.builder import route_after_fetch, route_after_refactor
from app.graph.nodes import APPLY, LINT, REFACTOR
from app.mocks import FakeGitHubClient
from tests.conftest import CFG, initial_state


def reject(graph, feedback):
    graph.update_state(
        CFG,
        {"is_approved": False, "reviewer_feedback": feedback, "revision_requested": True},
        as_node=REFACTOR,
    )


def approve(graph):
    graph.update_state(CFG, {"is_approved": True, "revision_requested": False}, as_node=REFACTOR)


def test_pipeline_pauses_before_apply_and_writes_nothing(graph, github):
    graph.invoke(initial_state(), CFG)
    snap = graph.get_state(CFG)
    assert snap.next == (APPLY,)  # interrupt_before gate
    assert github.commits == []  # nothing written yet
    assert snap.values["lint_errors"]  # lint ran in the sandbox
    assert (
        snap.values["refactored_code"]["src/utils.py"]
        != snap.values["code_contents"]["src/utils.py"]
    )
    assert [h["node"] for h in snap.values["history"]] == [
        "fetch_pr_node",
        "lint_code_node",
        "propose_refactor_node",
    ]


def test_rejection_rolls_back_to_refactor_node_and_regenerates(graph, github, refactorer):
    graph.invoke(initial_state(), CFG)
    reject(graph, "Use list comprehensions instead of loops")

    # rollback: the next step is the refactor node again, NOT the write node
    assert graph.get_state(CFG).next == (REFACTOR,)
    assert graph.get_state(CFG).values["reviewer_feedback"].startswith("Use list")

    graph.invoke(None, CFG)
    snap = graph.get_state(CFG)
    assert snap.next == (APPLY,)  # paused at the gate again
    assert github.commits == []
    assert [c["feedback"] for c in refactorer.calls] == [
        "",
        "Use list comprehensions instead of loops",
    ]
    assert refactorer.calls[1]["had_previous"]  # previous draft shown to the LLM
    assert "reviewer feedback applied" in snap.values["refactored_code"]["src/utils.py"]
    assert snap.values["revision_count"] == 1


def test_multiple_rejections_loop_until_approved(graph, github, refactorer):
    graph.invoke(initial_state(), CFG)
    for i in range(3):
        reject(graph, f"round {i}")
        graph.invoke(None, CFG)
        assert graph.get_state(CFG).next == (APPLY,)
    assert graph.get_state(CFG).values["revision_count"] == 3
    approve(graph)
    graph.invoke(None, CFG)
    assert graph.get_state(CFG).next == ()
    assert len(github.commits) == 1


def test_approval_commits_refactored_code_to_branch(graph, github):
    graph.invoke(initial_state(), CFG)
    approve(graph)
    graph.invoke(None, CFG)
    snap = graph.get_state(CFG)
    assert snap.next == () and snap.values["status"] == "committed"
    (commit,) = github.commits
    assert commit["branch"] == "feat" and commit["path"] == "src/utils.py"
    assert commit["sha"] == "blob0"  # blob sha forwarded for the contents API
    assert commit["content"] == snap.values["refactored_code"]["src/utils.py"]


def test_history_checkpoints_allow_inspecting_the_rolled_back_draft(graph):
    graph.invoke(initial_state(), CFG)
    first_draft = graph.get_state(CFG).values["refactored_code"]["src/utils.py"]
    reject(graph, "try again")
    graph.invoke(None, CFG)
    drafts = {
        s.values["refactored_code"]["src/utils.py"]
        for s in graph.get_state_history(CFG)
        if s.values.get("refactored_code")
    }
    assert first_draft in drafts and len(drafts) == 2  # both drafts live in the checkpoint history


def test_apply_node_refuses_to_run_without_approval(graph):
    graph.invoke(initial_state(), CFG)
    import pytest

    with pytest.raises(RuntimeError, match="without human approval"):
        graph.invoke(None, CFG)  # resume WITHOUT a decision


def test_clean_file_is_not_modified_or_committed(refactorer):
    from langgraph.checkpoint.memory import MemorySaver
    from app.graph.builder import build_graph

    gh = FakeGitHubClient({"ok.py": "def f(a):\n    return a\n"})
    g = build_graph(gh, refactorer, MemorySaver())
    g.invoke(initial_state(), CFG)
    assert refactorer.calls == []
    approve(g)
    g.invoke(None, CFG)
    assert g.get_state(CFG).values["status"] == "no_changes" and gh.commits == []


def test_no_python_files_ends_before_linting(refactorer):
    from langgraph.checkpoint.memory import MemorySaver
    from app.graph.builder import build_graph

    g = build_graph(FakeGitHubClient({}), refactorer, MemorySaver())
    g.invoke(initial_state(), CFG)
    snap = g.get_state(CFG)
    assert snap.next == () and snap.values["status"] == "no_python_files"


def test_route_functions():
    assert route_after_fetch({"code_contents": {"a.py": "x"}}) == LINT
    assert route_after_fetch({"code_contents": {}}) == END
    assert route_after_refactor({"revision_requested": True, "is_approved": False}) == REFACTOR
    assert route_after_refactor({"revision_requested": True, "is_approved": True}) == APPLY
    assert route_after_refactor({"revision_requested": False}) == APPLY
