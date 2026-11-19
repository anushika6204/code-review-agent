"""Offline fakes used by the tests and by MOCK_MODE=true (no GitHub / OpenAI needed)."""

from __future__ import annotations

import black

from app.github_client import CommitResult, FileSnapshot, GitHubError

SAMPLE_FILES = {
    "src/utils.py": (
        "import os, sys\n"
        "def squares(nums):\n"
        "    result=[]\n"
        "    for n in nums:\n"
        "        result.append(n*n)\n"
        "    return result\n"
    ),
}


class FakeGitHubClient:
    def __init__(self, files: dict[str, str] | None = None, fail_fetch: str | None = None):
        self.files = dict(files if files is not None else SAMPLE_FILES)
        self.fail_fetch = fail_fetch
        self.commits: list[dict] = []

    def fetch_code(self, repo, branch, pr_number, max_files=20):
        if self.fail_fetch:
            raise GitHubError(self.fail_fetch, 404)
        return {
            p: FileSnapshot(c, sha=f"blob{i}")
            for i, (p, c) in enumerate(list(self.files.items())[:max_files])
        }

    def commit_file(self, repo, path, content, branch, message, sha):
        n = len(self.commits) + 1
        self.files[path] = content
        self.commits.append(
            {
                "repo": repo,
                "path": path,
                "branch": branch,
                "message": message,
                "sha": sha,
                "content": content,
            }
        )
        return CommitResult(
            sha=f"fakecommit{n:04d}", url=f"https://github.com/{repo}/commit/fakecommit{n:04d}"
        )


class FakeRefactorer:
    """Deterministic stand-in for the LLM: black-formats and records reviewer feedback."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def propose(self, path, code, lint_report, feedback="", previous_draft=None):
        self.calls.append(
            {"path": path, "feedback": feedback, "had_previous": bool(previous_draft)}
        )
        out = black.format_str(code, mode=black.Mode())
        if feedback:
            out += f"# reviewer feedback applied: {feedback}\n"
        return out
