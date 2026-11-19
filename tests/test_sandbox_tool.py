from app.tools.sandbox import LINT_CLEAN, run_linter_tool


def lint(code: str) -> str:
    return run_linter_tool.invoke({"code_string": code})


def test_identifies_syntax_error_in_bad_code():
    report = lint("def broken(:\n    pass\n")
    assert "E999" in report and "SyntaxError" in report
    assert "[flake8]" in report


def test_clean_code_passes():
    assert lint("def add(a, b):\n    return a + b\n") == LINT_CLEAN


def test_style_and_unused_import_are_reported():
    report = lint("import os, sys\ndef f(a):\n    x=1\n    return a\n")
    assert "F401" in report and "E401" in report
    assert "[black --check]" in report and "would" not in LINT_CLEAN


def test_is_a_langchain_tool_with_description():
    assert run_linter_tool.name == "run_linter_tool"
    assert "flake8" in run_linter_tool.description


def test_does_not_execute_submitted_code(tmp_path):
    marker = tmp_path / "pwned"
    lint(f"open({str(marker)!r}, 'w').write('x')\n")
    assert not marker.exists()


def test_oversized_source_is_rejected():
    assert lint("x = 1\n" * 100_000).startswith("ERROR")
