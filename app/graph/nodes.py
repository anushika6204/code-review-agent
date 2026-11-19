"""Pipeline nodes. Each factory closes over its collaborators so the graph is easy to test."""

from __future__ import annotations

import logging
from typing import Callable

from app.github_client import GitHubGateway
from app.llm import Refactorer
from app.state import ReviewState, history_entry
from app.tools.sandbox import LINT_CLEAN, SNIPPET_NAME, run_linter_tool

log = logging.getLogger(__name__)

FETCH, LINT, REFACTOR, APPLY = (
    "fetch_pr_node",
    "lint_code_node",
    "propose_refactor_node",
    "apply_changes_node",
)


def _lint(code: str, path: str) -> str | None:
    report = run_linter_tool.invoke({"code_string": code})
    return None if report == LINT_CLEAN else report.replace(SNIPPET_NAME, path)


def make_fetch_pr_node(github: GitHubGateway, max_files: int):
    def fetch_pr_node(state: ReviewState) -> dict:
        snaps = github.fetch_code(state["repo"], state["branch"], state.get("pr_number"), max_files)
        log.info("fetched %d python file(s) from %s@%s", len(snaps), state["repo"], state["branch"])
        return {
            "code_contents": {p: s.content for p, s in snaps.items()},
            "file_shas": {p: s.sha for p, s in snaps.items()},
            "status": "fetched" if snaps else "no_python_files",
            "history": [
                history_entry(FETCH, f"fetched {len(snaps)} python file(s)", files=sorted(snaps))
            ],
        }

    return fetch_pr_node


def lint_code_node(state: ReviewState) -> dict:
    errors = {}
    for path, code in state["code_contents"].items():
        report = _lint(code, path)
        if report:
            errors[path] = report
    log.info("lint finished: %d/%d file(s) with findings", len(errors), len(state["code_contents"]))
    return {
        "lint_errors": errors,
        "status": "linted",
        "history": [
            history_entry(
                LINT, f"{len(errors)} of {len(state['code_contents'])} file(s) have lint findings"
            )
        ],
    }


def make_propose_refactor_node(refactorer: Refactorer):
    def propose_refactor_node(state: ReviewState) -> dict:
        feedback = state.get("reviewer_feedback", "") if state.get("revision_requested") else ""
        previous = state.get("refactored_code", {})
        lint_errors = state.get("lint_errors", {})
        refactored: dict[str, str] = {}
        for path, code in state["code_contents"].items():
            # first pass: files with findings only; revision pass: every file
            if path in lint_errors or feedback:
                refactored[path] = refactorer.propose(
                    path,
                    code,
                    lint_errors.get(path, ""),
                    feedback,
                    previous.get(path) if feedback else None,
                )
            else:
                refactored[path] = code
        residual = sum(
            1 for p, c in refactored.items() if c != state["code_contents"][p] and _lint(c, p)
        )
        count = state.get("revision_count", 0) + (1 if feedback else 0)
        msg = (
            f"regenerated draft #{count + 1} using reviewer feedback"
            if feedback
            else "generated initial refactor draft"
        )
        log.info("%s (%d draft file(s) still have lint findings)", msg, residual)
        return {
            "refactored_code": refactored,
            "is_approved": False,
            "revision_requested": False,
            "revision_count": count,
            "status": "awaiting_review",
            "history": [
                history_entry(
                    REFACTOR, msg, feedback=feedback or None, residual_lint_files=residual
                )
            ],
        }

    return propose_refactor_node


def make_apply_changes_node(github: GitHubGateway, on_commit: Callable[[str], None] | None):
    def apply_changes_node(state: ReviewState) -> dict:
        if not state.get("is_approved"):  # defence in depth: the gate should make this unreachable
            raise RuntimeError("apply_changes_node reached without human approval")
        shas: list[str] = []
        for path, new in state["refactored_code"].items():
            if new == state["code_contents"][path]:
                continue
            res = github.commit_file(
                state["repo"],
                path,
                new,
                state["branch"],
                f"refactor: apply reviewed lint fixes to {path} [agent-refactor]",
                state["file_shas"][path],
            )
            shas.append(res.sha)
            if on_commit:
                on_commit(res.sha)  # lets the webhook ignore the push events we cause ourselves
        log.info("committed %d file(s) to %s@%s", len(shas), state["repo"], state["branch"])
        return {
            "commit_shas": shas,
            "status": "committed" if shas else "no_changes",
            "history": [history_entry(APPLY, f"committed {len(shas)} file(s)", commits=shas)],
        }

    return apply_changes_node
