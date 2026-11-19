"""Request/response models and webhook payload parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

_REPO_RE = re.compile(
    r"^(?:https?://)?(?:(?:www\.)?github\.com/|api\.github\.com/repos/)?"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


class PayloadError(ValueError):
    """The webhook body is missing the repository or branch."""


@dataclass(frozen=True)
class PRTarget:
    repo: str
    branch: str
    pr_number: int | None
    pr_url: str
    head_sha: str | None = None


def normalize_repo(ref: str) -> str:
    m = _REPO_RE.match(ref.strip())
    if not m:
        raise PayloadError(f"cannot understand repository reference: {ref!r}")
    return f"{m.group(1)}/{m.group(2)}"


def is_fork_pr(payload: dict[str, Any]) -> bool:
    pr = payload.get("pull_request") or {}
    head = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    return bool(head and base and head != base)


def parse_webhook_payload(payload: dict[str, Any]) -> PRTarget:
    """Accepts a real GitHub `pull_request` event or the simplified
    `{"repository": {"html_url": ...}, "branch": ...}` form used in the brief."""
    pr = payload.get("pull_request") or {}
    repo_field = payload.get("repository")
    if isinstance(repo_field, dict):
        repo_ref = repo_field.get("full_name") or repo_field.get("html_url")
    else:
        repo_ref = repo_field
    repo_ref = repo_ref or payload.get("repository_url") or payload.get("repo_url")
    if not repo_ref:
        raise PayloadError(
            "payload must include a repository (full_name, html_url or repository_url)"
        )
    repo = normalize_repo(str(repo_ref))

    branch = (pr.get("head") or {}).get("ref") or payload.get("branch") or payload.get("ref")
    if not branch:
        raise PayloadError("payload must include a branch (pull_request.head.ref or 'branch')")
    branch = str(branch).removeprefix("refs/heads/")

    number = pr.get("number") or payload.get("number")
    pr_url = pr.get("html_url") or f"https://github.com/{repo}/tree/{branch}"
    return PRTarget(
        repo, branch, int(number) if number else None, pr_url, (pr.get("head") or {}).get("sha")
    )


# ---------------------------------------------------------------- API models
class WebhookResponse(BaseModel):
    status: Literal["accepted", "ignored", "pong"]
    thread_id: str | None = None
    detail: str | None = None


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str | None = Field(
        default=None,
        max_length=2000,
        description="Reviewer instructions used to regenerate the draft",
    )


ReviewStatusName = Literal["running", "awaiting_review", "completed", "failed"]


class ReviewStatus(BaseModel):
    thread_id: str
    status: ReviewStatusName
    outcome: str | None = Field(None, description="committed | no_changes | no_python_files | ...")
    error: str | None = None
    pr_url: str | None = None
    repo: str | None = None
    branch: str | None = None
    lint_errors: dict[str, str] = {}
    refactored_code: dict[str, str] = {}
    reviewer_feedback: str = ""
    revision_count: int = 0
    commit_shas: list[str] = []
    history: list[dict[str, Any]] = []


class ErrorResponse(BaseModel):
    detail: str
