import json

import pytest

from src.tools import READ_FILE_TOOL, execute_tool, read_file


def test_read_file_tool_schema():
    assert READ_FILE_TOOL["type"] == "function"
    assert READ_FILE_TOOL["name"] == "read_file"
    assert READ_FILE_TOOL["strict"] is True
    assert READ_FILE_TOOL["parameters"]["required"] == ["path"]
    assert READ_FILE_TOOL["parameters"]["additionalProperties"] is False


def test_execute_tool_reads_file(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")

    output = execute_tool("read_file", '{"path": "example.py"}', tmp_path)

    assert json.loads(output) == {"ok": True, "content": "print('hello')\n"}


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
        "error": "File path must stay inside the repository",
    }


def test_read_file_returns_repository_file(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")

    assert read_file("example.py", repository) == "print('hello')\n"


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
