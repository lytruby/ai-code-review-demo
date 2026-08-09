import json

import pytest

from src.tools import (
    READ_FILE_TOOL,
    SEARCH_CODE_TOOL,
    execute_tool,
    read_file,
    search_code,
)


def test_read_file_tool_schema():
    assert READ_FILE_TOOL["type"] == "function"
    assert READ_FILE_TOOL["name"] == "read_file"
    assert READ_FILE_TOOL["strict"] is True
    assert READ_FILE_TOOL["parameters"]["required"] == ["path"]
    assert READ_FILE_TOOL["parameters"]["additionalProperties"] is False
    assert "line" in READ_FILE_TOOL["parameters"]["properties"]
    assert "context_lines" in READ_FILE_TOOL["parameters"]["properties"]


def test_search_code_tool_schema():
    assert SEARCH_CODE_TOOL["name"] == "search_code"
    assert SEARCH_CODE_TOOL["parameters"]["required"] == ["query"]
    assert SEARCH_CODE_TOOL["parameters"]["additionalProperties"] is False


def test_execute_tool_reads_file(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")

    output = execute_tool("read_file", '{"path": "example.py"}', tmp_path)

    assert json.loads(output) == {
        "ok": True,
        "path": "example.py",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 1,
        "content": "print('hello')\n",
        "truncated": False,
    }


def test_execute_tool_rejects_invalid_arguments(tmp_path):
    output = execute_tool("read_file", "not json", tmp_path)

    assert json.loads(output) == {
        "ok": False,
        "error": "Tool arguments must be valid JSON",
    }


def test_execute_tool_rejects_unknown_tool(tmp_path):
    output = execute_tool("delete_file", '{"path": "example.py"}', tmp_path)

    assert json.loads(output) == {"ok": False, "error": "Unknown tool: delete_file"}


def test_execute_tool_returns_safe_file_error(tmp_path):
    output = execute_tool("read_file", '{"path": "../secret.txt"}', tmp_path)

    assert json.loads(output) == {
        "ok": False,
        "error": "Path must stay inside the repository",
    }


def test_read_file_returns_repository_file(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")

    assert read_file("example.py", repository) == {
        "path": "example.py",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 1,
        "content": "print('hello')\n",
        "truncated": False,
    }


def test_read_file_rejects_path_outside_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="inside the repository"):
        read_file("../secret.txt", repository)


def test_read_file_rejects_large_file(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "large.py"
    source.write_text("x" * 11, encoding="utf-8")

    with pytest.raises(ValueError, match="character limit"):
        read_file("large.py", repository, max_chars=10)


def test_execute_tool_large_file_returns_window_hint(tmp_path):
    source = tmp_path / "large.py"
    source.write_text("line\n" * 3000, encoding="utf-8")

    output = json.loads(execute_tool("read_file", '{"path":"large.py"}', tmp_path))

    assert output["ok"] is False
    assert output["total_lines"] == 3000
    assert output["hint"] == "Call read_file again with line and context_lines."


def test_read_file_returns_window_around_line(tmp_path):
    source = tmp_path / "large.py"
    source.write_text("".join(f"line {number}\n" for number in range(1, 301)))

    result = read_file("large.py", tmp_path, line=150, context_lines=2)

    assert result["start_line"] == 148
    assert result["end_line"] == 152
    assert result["total_lines"] == 300
    assert result["content"] == (
        "line 148\nline 149\nline 150\nline 151\nline 152\n"
    )
    assert result["truncated"] is True


def test_read_file_context_lines_requires_line(tmp_path):
    (tmp_path / "example.py").write_text("value = 1\n")

    with pytest.raises(ValueError, match="requires line"):
        read_file("example.py", tmp_path, context_lines=10)


def test_search_code_returns_bounded_repository_matches(tmp_path):
    (tmp_path / "first.py").write_text("class SpansBuffer:\n    pass\n")
    (tmp_path / "second.py").write_text("buffer = SpansBuffer()\n")

    result = search_code("SpansBuffer", tmp_path, max_matches=1)

    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"].endswith(("first.py", "second.py"))
    assert result["matches"][0]["line"] == 1
    assert "SpansBuffer" in result["matches"][0]["content"]
    assert result["truncated"] is True


def test_search_code_falls_back_when_ripgrep_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "first.py").write_text(
        "class SpansBuffer:\n    pass\n", encoding="utf-8"
    )

    def missing_ripgrep(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("src.tools.subprocess.Popen", missing_ripgrep)

    result = search_code("SpansBuffer", tmp_path)

    assert result == {
        "query": "SpansBuffer",
        "path": None,
        "matches": [
            {
                "path": "first.py",
                "line": 1,
                "content": "class SpansBuffer:",
            }
        ],
        "truncated": False,
    }


def test_execute_search_code_rejects_path_outside_repository(tmp_path):
    output = execute_tool(
        "search_code",
        '{"query":"secret","path":"../outside"}',
        tmp_path,
    )

    assert json.loads(output) == {
        "ok": False,
        "error": "Path must stay inside the repository",
    }
