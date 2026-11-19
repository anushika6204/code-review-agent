import base64
import json

import httpx
import pytest

from app.config import ConfigurationError
from app.github_client import GitHubClient, GitHubError


def make_client(handler, token="tok"):
    return GitHubClient(token, transport=httpx.MockTransport(handler))


def b64(s):
    return base64.b64encode(s.encode()).decode()


def test_fetch_code_for_pr_filters_to_existing_python_files():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path == "/repos/o/r/pulls/5/files":
            return httpx.Response(
                200,
                json=[
                    {"filename": "a.py", "status": "modified"},
                    {"filename": "README.md", "status": "modified"},
                    {"filename": "gone.py", "status": "removed"},
                ],
            )
        if req.url.path == "/repos/o/r/contents/a.py":
            return httpx.Response(
                200, json={"encoding": "base64", "content": b64("x = 1\n"), "sha": "S1", "size": 6}
            )
        return httpx.Response(404, json={"message": "nope"})

    snaps = make_client(handler).fetch_code("o/r", "feat", 5)
    assert (
        list(snaps) == ["a.py"] and snaps["a.py"].content == "x = 1\n" and snaps["a.py"].sha == "S1"
    )
    assert ("GET", "/repos/o/r/contents/a.py", {"ref": "feat"}) in seen


def test_fetch_code_without_pr_walks_branch_tree():
    def handler(req):
        if req.url.raw_path.split(b"?")[0] == b"/repos/o/r/git/trees/feat%2Fx":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "pkg/m.py"},
                        {"type": "tree", "path": "pkg"},
                        {"type": "blob", "path": "notes.txt"},
                    ]
                },
            )
        return httpx.Response(
            200, json={"encoding": "base64", "content": b64("y"), "sha": "S", "size": 1}
        )

    assert list(make_client(handler).fetch_code("o/r", "feat/x", None)) == ["pkg/m.py"]


def test_commit_file_puts_base64_content_with_sha_and_branch():
    captured = {}

    def handler(req):
        captured.update(method=req.method, path=req.url.path, body=json.loads(req.content))
        return httpx.Response(200, json={"commit": {"sha": "c0ffee", "html_url": "http://c"}})

    res = make_client(handler).commit_file("o/r", "src/a.py", "print(1)\n", "feat", "msg", "S1")
    assert res.sha == "c0ffee"
    assert captured["method"] == "PUT" and captured["path"] == "/repos/o/r/contents/src/a.py"
    assert captured["body"] == {
        "message": "msg",
        "content": b64("print(1)\n"),
        "sha": "S1",
        "branch": "feat",
    }


def test_http_errors_become_githuberror():
    client = make_client(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubError, match="404.*Not Found") as ei:
        client.fetch_code("o/r", "b", 1)
    assert ei.value.status_code == 404


def test_missing_token_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        make_client(lambda r: httpx.Response(200, json=[]), token="").fetch_code("o/r", "b", 1)
