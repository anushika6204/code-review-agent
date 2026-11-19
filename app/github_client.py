"""Thin GitHub REST client (httpx) for reading PR files and committing changes."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import ConfigurationError

MAX_FILE_BYTES = 200_000


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FileSnapshot:
    content: str
    sha: str  # blob sha; required by the contents API to update the file


@dataclass(frozen=True)
class CommitResult:
    sha: str
    url: str


class GitHubGateway(Protocol):
    """What the graph needs from GitHub. Implemented by the real and the fake client."""

    def fetch_code(
        self, repo: str, branch: str, pr_number: int | None, max_files: int = 20
    ) -> dict[str, FileSnapshot]: ...

    def commit_file(
        self, repo: str, path: str, content: str, branch: str, message: str, sha: str
    ) -> CommitResult: ...


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ):
        self._token = token
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    # -- plumbing -------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        if not self._token:
            raise ConfigurationError("GITHUB_TOKEN is not set")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-review-agent",
        }
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise GitHubError(
                f"GitHub {method} {path} -> {resp.status_code}: {detail}", resp.status_code
            )
        return resp.json()

    # -- reads ----------------------------------------------------------------
    def list_pr_files(self, repo: str, pr_number: int) -> list[dict]:
        files: list[dict] = []
        for page in range(1, 4):  # GitHub caps PR file listings at 3000 (30 pages of 100)
            batch = self._request(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            files.extend(batch)  # type: ignore[arg-type]
            if len(batch) < 100:
                break
        return files

    def list_branch_python_files(self, repo: str, branch: str) -> list[str]:
        tree = self._request(
            "GET",
            f"/repos/{repo}/git/trees/{quote(branch, safe='')}",
            params={"recursive": 1},
        )
        return [
            e["path"]
            for e in tree["tree"]  # type: ignore[index]
            if e["type"] == "blob" and e["path"].endswith(".py")
        ]

    def get_file(self, repo: str, path: str, ref: str) -> FileSnapshot | None:
        data = self._request("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref})
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        if data.get("size", 0) > MAX_FILE_BYTES:
            return None
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return FileSnapshot(content=text, sha=data["sha"])

    def fetch_code(
        self, repo: str, branch: str, pr_number: int | None, max_files: int = 20
    ) -> dict[str, FileSnapshot]:
        """Python files touched by the PR (or, with no PR number, all .py files on the branch)."""
        if pr_number:
            paths = [
                f["filename"]
                for f in self.list_pr_files(repo, pr_number)
                if f["filename"].endswith(".py") and f["status"] != "removed"
            ]
        else:
            paths = self.list_branch_python_files(repo, branch)
        snapshots: dict[str, FileSnapshot] = {}
        for path in paths[:max_files]:
            snap = self.get_file(repo, path, branch)
            if snap is not None:
                snapshots[path] = snap
        return snapshots

    # -- writes ---------------------------------------------------------------
    def commit_file(
        self, repo: str, path: str, content: str, branch: str, message: str, sha: str
    ) -> CommitResult:
        data = self._request(
            "PUT",
            f"/repos/{repo}/contents/{quote(path)}",
            json={
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "sha": sha,
                "branch": branch,
            },
        )
        commit = data["commit"]  # type: ignore[index]
        return CommitResult(sha=commit["sha"], url=commit.get("html_url", ""))
