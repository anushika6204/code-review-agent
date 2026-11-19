"""Custom LangChain tool: static lint analysis of untrusted Python in a sandbox.

Isolation measures (the code is *never imported or executed*, only analysed):
  * source is written to a throw-away temp directory that is deleted afterwards
  * linters run as child processes with a scrubbed environment (no secrets, no
    inherited PYTHONPATH, HOME pointed at the temp dir)
  * wall-clock timeout plus POSIX rlimits on CPU time and written file size
  * input and output size caps
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

SNIPPET_NAME = "snippet.py"
LINT_CLEAN = "OK: no lint issues found."
MAX_SOURCE_BYTES = 200_000
MAX_OUTPUT_CHARS = 4_000
TIMEOUT_SECONDS = 30

_FLAKE8 = [
    sys.executable,
    "-m",
    "flake8",
    "--max-line-length=88",
    "--extend-ignore=E203,W503",
    SNIPPET_NAME,
]
_BLACK = [sys.executable, "-m", "black", "--check", "--diff", "--quiet", SNIPPET_NAME]


def _limit_resources() -> None:  # pragma: no cover - runs in the child process
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (5_000_000, 5_000_000))


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        preexec_fn=_limit_resources if os.name == "posix" else None,
    )


def _check_tool_available(proc: subprocess.CompletedProcess[str], name: str) -> None:
    if "No module named" in proc.stderr:
        raise RuntimeError(f"Linter '{name}' is not installed in this environment")


@tool
def run_linter_tool(code_string: str) -> str:
    """Writes python code to a temporary file and runs flake8 + black --check on it.

    Returns "OK: no lint issues found." when clean, otherwise a report with the
    stdout/stderr of each failing checker. Syntax errors surface as flake8 E999.
    """
    if len(code_string.encode("utf-8")) > MAX_SOURCE_BYTES:
        return f"ERROR: source exceeds {MAX_SOURCE_BYTES} bytes; refusing to analyse."

    sections: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lint-sandbox-") as tmp:
        workdir = Path(tmp)
        (workdir / SNIPPET_NAME).write_text(code_string, encoding="utf-8")
        for label, cmd in (("flake8", _FLAKE8), ("black --check", _BLACK)):
            try:
                proc = _run(cmd, workdir)
            except subprocess.TimeoutExpired:
                sections.append(f"[{label}]\nERROR: timed out after {TIMEOUT_SECONDS}s")
                continue
            _check_tool_available(proc, label)
            if proc.returncode != 0:
                body = (proc.stdout + proc.stderr).strip()
                sections.append(f"[{label}]\n{body}")

    if not sections:
        return LINT_CLEAN
    report = "\n".join(sections)
    if len(report) > MAX_OUTPUT_CHARS:
        report = report[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return report
