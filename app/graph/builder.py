"""Assembles the StateGraph with a persistent checkpointer and the HITL interrupt gate."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.github_client import GitHubGateway
from app.graph import nodes
from app.graph.nodes import APPLY, FETCH, LINT, REFACTOR
from app.llm import Refactorer
from app.state import ReviewState


def route_after_fetch(state: ReviewState) -> str:
    return LINT if state.get("code_contents") else END


def route_after_refactor(state: ReviewState) -> str:
    """Rejected-with-feedback -> loop back for a new draft; otherwise proceed to the gate."""
    if state.get("revision_requested") and not state.get("is_approved"):
        return REFACTOR
    return APPLY


def make_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    if settings.checkpointer == "memory":
        return MemorySaver()
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(settings.sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def build_graph(
    github: GitHubGateway,
    refactorer: Refactorer,
    checkpointer: BaseCheckpointSaver,
    *,
    max_files: int = 20,
    on_commit: Callable[[str], None] | None = None,
):
    g = StateGraph(ReviewState)
    g.add_node(FETCH, nodes.make_fetch_pr_node(github, max_files))
    g.add_node(LINT, nodes.lint_code_node)
    g.add_node(REFACTOR, nodes.make_propose_refactor_node(refactorer))
    g.add_node(APPLY, nodes.make_apply_changes_node(github, on_commit))

    g.add_edge(START, FETCH)
    g.add_conditional_edges(FETCH, route_after_fetch, {LINT: LINT, END: END})
    g.add_edge(LINT, REFACTOR)
    g.add_conditional_edges(REFACTOR, route_after_refactor, {REFACTOR: REFACTOR, APPLY: APPLY})
    g.add_edge(APPLY, END)

    # HITL gate: execution halts *before* any write to the repository.
    return g.compile(checkpointer=checkpointer, interrupt_before=[APPLY])
