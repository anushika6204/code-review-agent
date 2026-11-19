import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.api import create_app
from app.config import Settings
from app.graph.builder import build_graph
from app.mocks import FakeGitHubClient, FakeRefactorer

CFG = {"configurable": {"thread_id": "t-1"}}


def initial_state(**over):
    base = {
        "pr_url": "https://github.com/o/r/pull/1",
        "repo": "o/r",
        "branch": "feat",
        "pr_number": 1,
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
    return {**base, **over}


@pytest.fixture
def github():
    return FakeGitHubClient()


@pytest.fixture
def refactorer():
    return FakeRefactorer()


@pytest.fixture
def graph(github, refactorer):
    return build_graph(github, refactorer, MemorySaver())


@pytest.fixture
def make_client():
    def _make(github=None, refactorer=None, **settings):
        github = github or FakeGitHubClient()
        refactorer = refactorer or FakeRefactorer()
        app = create_app(
            Settings(checkpointer="memory", **settings), github=github, refactorer=refactorer
        )
        return TestClient(app), github, refactorer

    return _make


@pytest.fixture
def client_bundle(make_client):
    return make_client()
