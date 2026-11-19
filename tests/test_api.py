"""Executes the three verification test cases from the brief against the FastAPI app."""

import hashlib
import hmac
import json

from app.mocks import FakeGitHubClient

PAYLOAD = {"repository": {"html_url": "https://github.com/octocat/hello"}, "branch": "feat/cleanup"}
FEEDBACK = "Use list comprehensions instead of loops"


def start(client, payload=PAYLOAD, **kw):
    r = client.post("/api/webhooks/github", json=payload, **kw)
    assert r.status_code == 202, r.text
    return r.json()["thread_id"]


def test_case_1_webhook_ingestion_lint_and_hitl_pause(client_bundle):
    client, github, _ = client_bundle
    tid = start(client)
    s = client.get(f"/api/reviews/{tid}").json()
    assert s["status"] == "awaiting_review"
    assert s["repo"] == "octocat/hello" and s["branch"] == "feat/cleanup"
    assert "src/utils.py" in s["lint_errors"] and "F401" in s["lint_errors"]["src/utils.py"]
    assert s["refactored_code"] and github.commits == []


def test_case_2_rejection_regenerates_draft_and_pauses_again(client_bundle):
    client, github, refactorer = client_bundle
    tid = start(client)
    r = client.post(f"/api/reviews/{tid}/approve", json={"approved": False, "feedback": FEEDBACK})
    assert r.status_code == 202
    s = client.get(f"/api/reviews/{tid}").json()
    assert s["status"] == "awaiting_review" and s["revision_count"] == 1
    assert s["reviewer_feedback"] == FEEDBACK
    assert FEEDBACK in s["refactored_code"]["src/utils.py"]
    assert refactorer.calls[-1]["feedback"] == FEEDBACK
    assert github.commits == []


def test_case_3_approval_commits_and_completes(client_bundle):
    client, github, _ = client_bundle
    tid = start(client)
    client.post(f"/api/reviews/{tid}/approve", json={"approved": False, "feedback": FEEDBACK})
    r = client.post(f"/api/reviews/{tid}/approve", json={"approved": True})
    assert r.status_code == 202
    s = client.get(f"/api/reviews/{tid}").json()
    assert s["status"] == "completed" and s["outcome"] == "committed"
    assert s["commit_shas"] == ["fakecommit0001"]
    assert github.commits[0]["branch"] == "feat/cleanup"


# ---------------------------------------------------------------- validation & errors
def test_invalid_payloads_return_422(client_bundle):
    client, *_ = client_bundle
    assert client.post("/api/webhooks/github", json={"branch": "x"}).status_code == 422
    assert (
        client.post(
            "/api/webhooks/github", json={"repository": "https://github.com/o/r"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/webhooks/github", json={"repository": "not a repo", "branch": "x"}
        ).status_code
        == 422
    )


def test_approval_body_validation(client_bundle):
    client, *_ = client_bundle
    tid = start(client)
    assert client.post(f"/api/reviews/{tid}/approve", json={}).status_code == 422
    assert client.post(f"/api/reviews/{tid}/approve", json={"approved": "maybe"}).status_code == 422
    assert (
        client.post(
            f"/api/reviews/{tid}/approve", json={"approved": False, "feedback": "x" * 2001}
        ).status_code
        == 422
    )


def test_unknown_thread_is_404(client_bundle):
    client, *_ = client_bundle
    assert client.get("/api/reviews/nope").status_code == 404
    assert client.post("/api/reviews/nope/approve", json={"approved": True}).status_code == 404


def test_cannot_decide_twice_on_a_finished_thread(client_bundle):
    client, github, _ = client_bundle
    tid = start(client)
    client.post(f"/api/reviews/{tid}/approve", json={"approved": True})
    r = client.post(f"/api/reviews/{tid}/approve", json={"approved": True})
    assert r.status_code == 409 and len(github.commits) == 1


def test_revision_limit(make_client):
    client, *_ = make_client(max_revisions=1)
    tid = start(client)
    assert (
        client.post(
            f"/api/reviews/{tid}/approve", json={"approved": False, "feedback": "a"}
        ).status_code
        == 202
    )
    assert (
        client.post(
            f"/api/reviews/{tid}/approve", json={"approved": False, "feedback": "b"}
        ).status_code
        == 409
    )


def test_github_failure_surfaces_as_failed_thread(make_client):
    client, *_ = make_client(github=FakeGitHubClient(fail_fetch="Not Found"))
    tid = start(client)
    s = client.get(f"/api/reviews/{tid}").json()
    assert s["status"] == "failed" and "Not Found" in s["error"]
    assert client.post(f"/api/reviews/{tid}/approve", json={"approved": True}).status_code == 409


# ---------------------------------------------------------------- webhook semantics
def test_real_github_pull_request_payload(client_bundle):
    client, *_ = client_bundle
    payload = {
        "action": "opened",
        "number": 7,
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 7,
            "html_url": "https://github.com/o/r/pull/7",
            "head": {"ref": "fix", "sha": "abc", "repo": {"full_name": "o/r"}},
            "base": {"repo": {"full_name": "o/r"}},
        },
    }
    s = client.get(f"/api/reviews/{start(client, payload)}").json()
    assert s["pr_url"].endswith("/pull/7") and s["branch"] == "fix"


def test_irrelevant_events_are_ignored(client_bundle):
    client, *_ = client_bundle
    r = client.post("/api/webhooks/github", json=PAYLOAD, headers={"X-GitHub-Event": "push"})
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    r = client.post(
        "/api/webhooks/github",
        json={**PAYLOAD, "action": "closed"},
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert r.json()["status"] == "ignored"
    r = client.post("/api/webhooks/github", json={}, headers={"X-GitHub-Event": "ping"})
    assert r.json()["status"] == "pong"


def test_fork_prs_are_ignored(client_bundle):
    client, *_ = client_bundle
    payload = {
        **PAYLOAD,
        "pull_request": {
            "head": {"ref": "x", "repo": {"full_name": "evil/r"}},
            "base": {"repo": {"full_name": "o/r"}},
        },
    }
    assert client.post("/api/webhooks/github", json=payload).json()["status"] == "ignored"


def test_agent_own_commits_do_not_retrigger_reviews(client_bundle):
    client, *_ = client_bundle
    tid = start(client)
    client.post(f"/api/reviews/{tid}/approve", json={"approved": True})
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "o/r"},
        "pull_request": {"head": {"ref": "feat", "sha": "fakecommit0001"}},
    }
    assert client.post("/api/webhooks/github", json=payload).json()["status"] == "ignored"


def test_signature_verification(make_client):
    client, *_ = make_client(github_webhook_secret="s3cret")
    body = json.dumps(PAYLOAD).encode()
    hdr = {"Content-Type": "application/json"}
    assert client.post("/api/webhooks/github", content=body, headers=hdr).status_code == 401
    bad = {**hdr, "X-Hub-Signature-256": "sha256=deadbeef"}
    assert client.post("/api/webhooks/github", content=body, headers=bad).status_code == 401
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    ok = client.post(
        "/api/webhooks/github", content=body, headers={**hdr, "X-Hub-Signature-256": sig}
    )
    assert ok.status_code == 202
