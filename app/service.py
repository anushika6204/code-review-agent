"""ReviewService: owns the compiled graph and the thread lifecycle used by the API."""

from __future__ import annotations

import logging
import threading
import uuid

from app.graph.nodes import APPLY, REFACTOR
from app.schemas import PRTarget, ReviewStatus
from app.state import history_entry

log = logging.getLogger(__name__)


class ThreadNotFound(LookupError):
    pass


class ThreadNotAwaitingReview(RuntimeError):
    pass


class RevisionLimitReached(RuntimeError):
    pass


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class ReviewService:
    def __init__(self, graph, max_revisions: int = 5):
        self.graph = graph
        self.max_revisions = max_revisions
        self.agent_commit_shas: set[str] = set()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------ lifecycle
    def create_thread(self) -> str:
        thread_id = uuid.uuid4().hex
        with self._lock:
            self._in_flight.add(thread_id)
        return thread_id

    def _finish(self, thread_id: str, error: Exception | None = None) -> None:
        with self._lock:
            self._in_flight.discard(thread_id)
            if error is not None:
                self._errors[thread_id] = f"{type(error).__name__}: {error}"
        status = self.get_status(thread_id)
        if status:
            log.info(
                "thread %s -> %s%s",
                thread_id,
                status.status,
                f" ({status.error})" if status.error else "",
            )

    def run_initial(self, thread_id: str, target: PRTarget) -> None:
        initial = {
            "pr_url": target.pr_url,
            "repo": target.repo,
            "branch": target.branch,
            "pr_number": target.pr_number,
            "code_contents": {},
            "file_shas": {},
            "lint_errors": {},
            "refactored_code": {},
            "is_approved": False,
            "reviewer_feedback": "",
            "revision_requested": False,
            "revision_count": 0,
            "commit_shas": [],
            "status": "started",
            "history": [],
        }
        try:
            self.graph.invoke(initial, _cfg(thread_id))
            self._finish(thread_id)
        except Exception as exc:  # noqa: BLE001 - surfaced through the status endpoint
            log.exception("thread %s failed", thread_id)
            self._finish(thread_id, exc)

    def run_resume(self, thread_id: str) -> None:
        try:
            self.graph.invoke(None, _cfg(thread_id))
            self._finish(thread_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("thread %s failed while resuming", thread_id)
            self._finish(thread_id, exc)

    # ------------------------------------------------------------ HITL decision
    def submit_decision(self, thread_id: str, approved: bool, feedback: str | None) -> None:
        """Writes the reviewer's decision into the checkpointed state.

        The pending (not yet executed) write step is discarded by forking the thread
        *as if* `propose_refactor_node` had just produced the new state; the graph's
        conditional edge then routes to either the gate (approve) or a regeneration
        (reject). Call `run_resume` afterwards to continue execution.
        """
        with self._lock:
            if thread_id in self._in_flight:
                raise ThreadNotAwaitingReview("thread is still running")
            snap = self.graph.get_state(_cfg(thread_id))
            if not snap.values:
                raise ThreadNotFound(thread_id)
            if thread_id in self._errors or snap.next != (APPLY,):
                raise ThreadNotAwaitingReview("thread is not waiting for a review decision")
            if not approved and snap.values.get("revision_count", 0) >= self.max_revisions:
                raise RevisionLimitReached(f"maximum of {self.max_revisions} revisions reached")
            self._in_flight.add(thread_id)

        feedback = (feedback or "").strip()
        try:
            if approved:
                update = {
                    "is_approved": True,
                    "revision_requested": False,
                    "reviewer_feedback": feedback,
                }
                note = "reviewer approved"
            else:
                update = {
                    "is_approved": False,
                    "revision_requested": True,
                    "reviewer_feedback": feedback
                    or "Reviewer rejected the draft; propose a different approach.",
                }
                note = "reviewer rejected draft; rolled back to propose_refactor_node"
            update["history"] = [history_entry("human_review", note, feedback=feedback or None)]
            self.graph.update_state(_cfg(thread_id), update, as_node=REFACTOR)
        except Exception:
            with self._lock:
                self._in_flight.discard(thread_id)
            raise

    # ------------------------------------------------------------ queries
    def get_status(self, thread_id: str) -> ReviewStatus | None:
        snap = self.graph.get_state(_cfg(thread_id))
        values = snap.values or {}
        error = self._errors.get(thread_id)
        with self._lock:
            running = thread_id in self._in_flight
        if not values and not running and not error:
            return None
        if error:
            status = "failed"
        elif running:
            status = "running"
        elif snap.next == (APPLY,):
            status = "awaiting_review"
        elif snap.next:
            status, error = "failed", "run was interrupted before completing"
        else:
            status = "completed"
        return ReviewStatus(
            thread_id=thread_id,
            status=status,
            outcome=values.get("status"),
            error=error,
            pr_url=values.get("pr_url"),
            repo=values.get("repo"),
            branch=values.get("branch"),
            lint_errors=values.get("lint_errors", {}),
            refactored_code=values.get("refactored_code", {}),
            reviewer_feedback=values.get("reviewer_feedback", ""),
            revision_count=values.get("revision_count", 0),
            commit_shas=values.get("commit_shas", []),
            history=values.get("history", []),
        )
