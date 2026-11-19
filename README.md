# Code Review & Refactoring Agent with LangGraph

- **Repository**: [Insert URL]

An agent that receives GitHub pull-request webhooks, lints the changed Python files in a
sandbox, asks an LLM for a refactor, **pauses for a human decision**, and only then commits
the approved code back to the PR branch.

```
START -> fetch_pr_node -> lint_code_node -> propose_refactor_node --(no feedback)--> [interrupt] apply_changes_node -> END
                 |                                  ^        |
          (no .py files) -> END                     +--------+ reviewer rejects: feedback written to state,
                                                      thread rolled back, draft regenerated
```

## Installation

1. `cp .env.example .env` and set `GITHUB_TOKEN` (needs *Contents: write*) and `OPENAI_API_KEY`.
2. `pip install -r requirements.txt`
3. `uvicorn main:app --reload`
4. `pytest`

Interactive docs: http://127.0.0.1:8000/docs. Static spec: `openapi.json`
(regenerate with `python scripts/export_openapi.py`).

**Offline demo (no keys):** `MOCK_MODE=true uvicorn main:app --reload` uses a fake GitHub
repo and a fake LLM, so the three verification cases below run as-is.

## Verification test cases

```bash
# 1. Webhook -> fetch -> sandbox lint -> pause at the approval gate
curl -s -X POST localhost:8000/api/webhooks/github -H 'content-type: application/json' \
  -d '{"repository":{"html_url":"https://github.com/OWNER/REPO"},"branch":"my-branch"}'
# -> {"status":"accepted","thread_id":"<ID>"}   (server log: "thread <ID> -> awaiting_review")
curl -s localhost:8000/api/reviews/<ID>          # status: awaiting_review

# 2. Reject with feedback -> draft regenerated -> paused again
curl -s -X POST localhost:8000/api/reviews/<ID>/approve -H 'content-type: application/json' \
  -d '{"approved":false,"feedback":"Use list comprehensions instead of loops"}'

# 3. Approve -> apply_changes_node commits -> thread completes
curl -s -X POST localhost:8000/api/reviews/<ID>/approve -H 'content-type: application/json' \
  -d '{"approved":true}'
```
The same flows are automated in `tests/test_api.py`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/webhooks/github` | Starts a thread (202). Accepts a real `pull_request` event or `{repository, branch}`. Validates `X-Hub-Signature-256` when `GITHUB_WEBHOOK_SECRET` is set. |
| GET | `/api/reviews/{thread_id}` | Status, lint report, current draft, history. |
| POST | `/api/reviews/{thread_id}/approve` | `{approved, feedback?}` - resume or roll back. 409 if the thread is not awaiting review. |

## Design notes

- **State** (`app/state.py`): `pr_url, code_contents, lint_errors, refactored_code, is_approved,
  reviewer_feedback, history` plus a few supporting keys. `history` is an append-only reducer.
- **Checkpointer**: `SqliteSaver` by default (survives restarts), `MemorySaver` with `CHECKPOINTER=memory`.
- **HITL gate**: `compile(..., interrupt_before=["apply_changes_node"])`. `apply_changes_node`
  also refuses to run if `is_approved` is false.
- **Rollback**: a rejection calls `update_state(..., as_node="propose_refactor_node")`, which forks
  the thread from the paused checkpoint, discards the pending write step, and lets the conditional
  edge route back to `propose_refactor_node`. Every draft stays inspectable via `get_state_history`.
- **Sandbox** (`app/tools/sandbox.py`): `@tool run_linter_tool` writes code to a temp dir and runs
  `flake8` and `black --check` with a scrubbed environment, timeout, and CPU/file-size rlimits.
  Submitted code is analysed, never executed. This is process-level isolation; for fully untrusted
  workloads run the service in the provided `Dockerfile` with no secrets mounted beyond the two keys.
- **Safety**: LLM output is syntax-checked (one retry); fork PRs and the agent's own commits are ignored
  (prevents review loops); revisions are capped by `MAX_REVISIONS`.

## Known limitations

- Webhook runs execute in FastAPI background tasks (in-process); use a queue for multi-worker deployments.
- The set of agent-created commit SHAs is in memory, so loop protection resets on restart.
- Files over 200 KB are skipped.

## Docker

`docker build -t review-agent . && docker run --env-file .env -p 8000:8000 review-agent`
