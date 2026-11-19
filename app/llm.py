"""LLM-backed refactoring. The model is only ever asked for *source text*; nothing it
returns is executed - it is syntax-checked, then reviewed by a human before commit."""

from __future__ import annotations

import ast
import re
from typing import Protocol

from app.config import ConfigurationError

SYSTEM_PROMPT = (
    "You are a senior Python engineer performing a code-review refactor. "
    "Fix every reported lint/style problem while preserving the program's behaviour "
    "exactly: do not change public names, signatures, return values or side effects, "
    "and do not add dependencies. Follow PEP 8 / black formatting. "
    "Respond with the COMPLETE updated file in a single ```python code block and nothing else."
)
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


class RefactorError(RuntimeError):
    pass


class Refactorer(Protocol):
    def propose(
        self,
        path: str,
        code: str,
        lint_report: str,
        feedback: str = "",
        previous_draft: str | None = None,
    ) -> str: ...


def extract_code(text: str) -> str:
    match = _FENCE.search(text)
    code = match.group(1) if match else text
    return code.strip("\n") + "\n"


def build_user_prompt(path, code, lint_report, feedback, previous_draft) -> str:
    parts = [f"File: {path}", f"Original source:\n```python\n{code}\n```"]
    if lint_report:
        parts.append(f"Linter findings (flake8 / black):\n{lint_report}")
    if previous_draft and feedback:
        parts.append(f"Your previous draft (rejected):\n```python\n{previous_draft}\n```")
    if feedback:
        parts.append(f"Human reviewer feedback to address:\n{feedback}")
    return "\n\n".join(parts)


class LLMRefactorer:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", max_attempts: int = 2):
        self._api_key, self._model, self._max_attempts = api_key, model, max_attempts
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            if not self._api_key:
                raise ConfigurationError("OPENAI_API_KEY is not set")
            from langchain_openai import ChatOpenAI

            self._llm = ChatOpenAI(model=self._model, api_key=self._api_key, temperature=0)
        return self._llm

    def propose(self, path, code, lint_report, feedback="", previous_draft=None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = build_user_prompt(path, code, lint_report, feedback, previous_draft)
        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
        last_error = ""
        for _ in range(self._max_attempts):
            reply = self._get_llm().invoke(messages)
            candidate = extract_code(str(reply.content))
            try:
                ast.parse(candidate)
                return candidate
            except SyntaxError as exc:
                last_error = f"{exc.msg} (line {exc.lineno})"
                messages += [
                    reply,
                    HumanMessage(f"Syntax error: {last_error}. Return the corrected full file."),
                ]
        raise RefactorError(f"LLM produced invalid Python for {path}: {last_error}")
