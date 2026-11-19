import pytest

from app.llm import LLMRefactorer, RefactorError, extract_code


def test_extract_code_strips_markdown_fences():
    assert extract_code("Sure!\n```python\nx = 1\n```\nbye") == "x = 1\n"
    assert extract_code("y = 2") == "y = 2\n"


class StubLLM:
    def __init__(self, replies):
        self.replies, self.n = replies, 0

    def invoke(self, messages):
        self.n += 1
        return type("R", (), {"content": self.replies.pop(0)})()


def refactorer_with(replies):
    r = LLMRefactorer("k")
    r._llm = StubLLM(replies)
    return r


def test_retries_once_on_invalid_python_then_succeeds():
    r = refactorer_with(["```python\ndef f(:\n```", "```python\ndef f():\n    pass\n```"])
    assert r.propose("a.py", "def f(): pass", "E1") == "def f():\n    pass\n"
    assert r._llm.n == 2


def test_gives_up_after_max_attempts():
    r = refactorer_with(["def f(:", "def g(:"])
    with pytest.raises(RefactorError):
        r.propose("a.py", "x", "")
