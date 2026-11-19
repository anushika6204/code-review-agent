"""LangGraph state schema for one review session (one thread == one PR review)."""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict


class ReviewState(TypedDict, total=False):
    # --- keys required by the brief ---
    pr_url: str
    code_contents: dict[str, str]  # path -> original source
    lint_errors: dict[str, str]  # path -> linter report (clean files omitted)
    refactored_code: dict[str, str]  # path -> proposed source
    is_approved: bool
    reviewer_feedback: str
    history: Annotated[list[dict[str, Any]], operator.add]  # append-only audit log
    # --- supporting keys ---
    repo: str  # "owner/name"
    branch: str
    pr_number: int | None
    file_shas: dict[str, str]  # blob sha per path (needed for the contents API)
    revision_requested: bool  # set by a rejection, consumed by propose_refactor_node
    revision_count: int
    commit_shas: list[str]
    status: str


def history_entry(node: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "node": node,
        "message": message,
        **extra,
    }
