"""FastAPI application: GitHub webhook ingestion + human review endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Request, Response

from app.config import Settings
from app.github_client import GitHubClient
from app.graph.builder import build_graph, make_checkpointer
from app.llm import LLMRefactorer
from app.mocks import FakeGitHubClient, FakeRefactorer
from app.schemas import (
    ApprovalRequest,
    ErrorResponse,
    PayloadError,
    ReviewStatus,
    WebhookResponse,
    is_fork_pr,
    parse_webhook_payload,
)
from app.service import RevisionLimitReached, ReviewService, ThreadNotAwaitingReview, ThreadNotFound

log = logging.getLogger("app.api")
HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

WEBHOOK_EXAMPLE = {
    "repository": {"html_url": "https://github.com/octocat/hello-world"},
    "branch": "feature/cleanup",
}


def _verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def create_app(
    settings: Settings | None = None, *, github=None, refactorer=None, checkpointer=None
) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.mock_mode:
        github, refactorer = github or FakeGitHubClient(), refactorer or FakeRefactorer()
    github = github or GitHubClient(settings.github_token, settings.github_api_url)
    refactorer = refactorer or LLMRefactorer(settings.openai_api_key, settings.openai_model)
    checkpointer = checkpointer or make_checkpointer(settings)

    shas: set[str] = set()
    graph = build_graph(
        github, refactorer, checkpointer, max_files=settings.max_files, on_commit=shas.add
    )
    service = ReviewService(graph, settings.max_revisions)
    service.agent_commit_shas = shas

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info(
            "review agent started (mock_mode=%s, checkpointer=%s)",
            settings.mock_mode,
            settings.checkpointer,
        )
        yield

    app = FastAPI(
        title="Autonomous Code Review & Refactoring Agent",
        version="1.0.0",
        description=(
            "Receives GitHub pull-request webhooks, lints the changed Python files in a "
            "sandbox, proposes an LLM refactor and **pauses for human approval** before "
            "committing anything back to the branch."
        ),
        lifespan=lifespan,
    )
    app.state.service, app.state.settings = service, settings

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/webhooks/github",
        status_code=202,
        response_model=WebhookResponse,
        tags=["webhooks"],
        summary="Receive a GitHub pull_request event",
        responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    async def github_webhook(
        request: Request,
        response: Response,
        background: BackgroundTasks,
        payload: dict[str, Any] = Body(..., examples=[WEBHOOK_EXAMPLE]),
        x_github_event: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> WebhookResponse:
        """Starts a new LangGraph thread (fetch -> lint -> refactor) and returns its
        `thread_id` immediately; the run continues in the background and halts at the
        approval gate. Poll `GET /api/reviews/{thread_id}`."""
        if settings.github_webhook_secret and not _verify_signature(
            settings.github_webhook_secret, await request.body(), x_hub_signature_256
        ):
            raise HTTPException(401, "invalid webhook signature")

        def ignored(why: str) -> WebhookResponse:
            response.status_code = 200
            log.info("webhook ignored: %s", why)
            return WebhookResponse(status="ignored", detail=why)

        if x_github_event == "ping":
            response.status_code = 200
            return WebhookResponse(status="pong")
        if x_github_event and x_github_event != "pull_request":
            return ignored(f"event '{x_github_event}' is not handled")
        action = payload.get("action")
        if action and action not in HANDLED_ACTIONS:
            return ignored(f"pull_request action '{action}' is not handled")
        if is_fork_pr(payload):
            return ignored("pull requests from forks are not supported")
        try:
            target = parse_webhook_payload(payload)
        except PayloadError as exc:
            raise HTTPException(422, str(exc)) from exc
        if target.head_sha and target.head_sha in service.agent_commit_shas:
            return ignored("head commit was created by this agent")

        thread_id = service.create_thread()
        background.add_task(service.run_initial, thread_id, target)
        log.info(
            "webhook accepted: thread=%s repo=%s branch=%s", thread_id, target.repo, target.branch
        )
        return WebhookResponse(
            status="accepted",
            thread_id=thread_id,
            detail=f"reviewing {target.repo}@{target.branch}",
        )

    @app.get(
        "/api/reviews/{thread_id}",
        response_model=ReviewStatus,
        tags=["reviews"],
        summary="Inspect a review thread",
        responses={404: {"model": ErrorResponse}},
    )
    def get_review(thread_id: str) -> ReviewStatus:
        status = service.get_status(thread_id)
        if status is None:
            raise HTTPException(404, f"unknown thread '{thread_id}'")
        return status

    @app.post(
        "/api/reviews/{thread_id}/approve",
        status_code=202,
        response_model=ReviewStatus,
        tags=["reviews"],
        summary="Approve or reject the proposed refactor",
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def review_decision(
        thread_id: str, body: ApprovalRequest, background: BackgroundTasks
    ) -> ReviewStatus:
        """`approved: true` resumes the graph and commits. `approved: false` writes the
        feedback into state, rolls the thread back to `propose_refactor_node`, regenerates
        the draft and pauses at the gate again."""
        try:
            service.submit_decision(thread_id, body.approved, body.feedback)
        except ThreadNotFound as exc:
            raise HTTPException(404, f"unknown thread '{thread_id}'") from exc
        except (ThreadNotAwaitingReview, RevisionLimitReached) as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.run_resume, thread_id)
        status = service.get_status(thread_id)
        assert status is not None
        return status

    return app
