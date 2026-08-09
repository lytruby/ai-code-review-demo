import json
import os
from pathlib import Path
import subprocess


MAX_FILE_CHARS = 10_000
DEFAULT_CONTEXT_LINES = 50
MAX_CONTEXT_LINES = 100
MAX_SEARCH_QUERY_CHARS = 200
MAX_SEARCH_MATCHES = 20

READ_FILE_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": (
        "Read a UTF-8 text file from the current repository when the patch "
        "does not provide enough context for the code review."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file, relative to the repository root.",
            },
            "line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional center line for reading a window.",
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_CONTEXT_LINES,
                "description": (
                    "Lines to read before and after line. Defaults to 50 and "
                    "requires line."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "strict": True,
}

SEARCH_CODE_TOOL = {
    "type": "function",
    "name": "search_code",
    "description": (
        "Search repository text to locate symbol definitions, references, and "
        "call sites before reading the relevant file. Uses exact text search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Exact text to search for in repository files.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional repository-relative file or directory to search."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}


class FileWindowRequired(ValueError):
    def __init__(self, path: str, total_lines: int):
        super().__init__(
            f"File exceeds the {MAX_FILE_CHARS}-character limit; "
            f"specify line and context_lines: {path}"
        )
        self.path = path
        self.total_lines = total_lines


def execute_tool(
    tool_name: str,
    arguments: str,
    repository_root: Path,
) -> str:
    if tool_name not in {"read_file", "search_code"}:
        return json.dumps({"ok": False, "error": f"Unknown tool: {tool_name}"})

    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "Tool arguments must be valid JSON"})

    if not isinstance(parsed_arguments, dict):
        return json.dumps({"ok": False, "error": "Tool arguments must be an object"})
    try:
        if tool_name == "read_file":
            allowed = {"path", "line", "context_lines"}
            if not set(parsed_arguments) <= allowed or "path" not in parsed_arguments:
                raise ValueError(
                    "read_file requires path and accepts optional line and context_lines"
                )
            result = read_file(
                path=parsed_arguments["path"],
                line=parsed_arguments.get("line"),
                context_lines=parsed_arguments.get("context_lines"),
                repository_root=repository_root,
            )
        else:
            allowed = {"query", "path"}
            if not set(parsed_arguments) <= allowed or "query" not in parsed_arguments:
                raise ValueError(
                    "search_code requires query and accepts an optional path"
                )
            result = search_code(
                query=parsed_arguments["query"],
                path=parsed_arguments.get("path"),
                repository_root=repository_root,
            )
    except FileWindowRequired as error:
        return json.dumps(
            {
                "ok": False,
                "error": str(error),
                "path": error.path,
                "total_lines": error.total_lines,
                "hint": "Call read_file again with line and context_lines.",
            }
        )
    except (FileNotFoundError, ValueError) as error:
        return json.dumps({"ok": False, "error": str(error)})

    return json.dumps({"ok": True, **result})


def read_file(
    path: str,
    repository_root: Path,
    line: int | None = None,
    context_lines: int | None = None,
    max_chars: int = MAX_FILE_CHARS,
) -> dict:
    target = _resolve_repository_path(path, repository_root)

    if context_lines is not None and line is None:
        raise ValueError("context_lines requires line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        raise ValueError("line must be a positive integer")
    if context_lines is None:
        context_lines = DEFAULT_CONTEXT_LINES
    if (
        not isinstance(context_lines, int)
        or isinstance(context_lines, bool)
        or not 0 <= context_lines <= MAX_CONTEXT_LINES
    ):
        raise ValueError(
            f"context_lines must be an integer between 0 and {MAX_CONTEXT_LINES}"
        )

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"File is not UTF-8 text: {path}") from error

    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    if line is None:
        if len(content) > max_chars:
            raise FileWindowRequired(path, total_lines)
        return {
            "path": path,
            "start_line": 1 if total_lines else 0,
            "end_line": total_lines,
            "total_lines": total_lines,
            "content": content,
            "truncated": False,
        }

    if line > total_lines:
        raise ValueError(f"line {line} exceeds file length {total_lines}: {path}")
    start_line = max(1, line - context_lines)
    end_line = min(total_lines, line + context_lines)
    window = "".join(lines[start_line - 1 : end_line])
    if len(window) > max_chars:
        raise ValueError(
            f"Requested window exceeds the {max_chars}-character limit; "
            "use fewer context_lines"
        )
    return {
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "content": window,
        "truncated": start_line > 1 or end_line < total_lines,
    }


def search_code(
    query: str,
    repository_root: Path,
    path: str | None = None,
    max_matches: int = MAX_SEARCH_MATCHES,
) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query must be a non-empty string")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"Search query exceeds the {MAX_SEARCH_QUERY_CHARS}-character limit"
        )
    if "\n" in query or "\r" in query:
        raise ValueError("Search query must be a single line")
    if not isinstance(max_matches, int) or isinstance(max_matches, bool) or max_matches < 1:
        raise ValueError("max_matches must be a positive integer")

    root = repository_root.resolve()
    search_target = root
    if path is not None:
        search_target = _resolve_repository_path(path, repository_root, require_file=False)
    relative_target = search_target.relative_to(root)
    target_argument = "." if not relative_target.parts else str(relative_target)

    command = [
        "rg",
        "--json",
        "--fixed-strings",
        "--color",
        "never",
        "--",
        query,
        target_argument,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return _search_code_without_rg(
            query=query,
            search_target=search_target,
            repository_root=root,
            path=path,
            max_matches=max_matches,
        )

    matches = []
    truncated = False
    assert process.stdout is not None
    for output_line in process.stdout:
        event = json.loads(output_line)
        if event.get("type") != "match":
            continue
        data = event["data"]
        if len(matches) >= max_matches:
            truncated = True
            process.terminate()
            break
        matches.append(
            {
                "path": data["path"]["text"],
                "line": data["line_number"],
                "content": data["lines"]["text"].rstrip("\r\n"),
            }
        )
    process.wait()

    return {
        "query": query,
        "path": path,
        "matches": matches,
        "truncated": truncated,
    }


def _search_code_without_rg(
    query: str,
    search_target: Path,
    repository_root: Path,
    path: str | None,
    max_matches: int,
) -> dict:
    """Provide the workflow with a portable fixed-text search fallback."""
    if search_target.is_file():
        files = [search_target]
    else:
        files = []
        for directory, directory_names, file_names in os.walk(search_target):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in {".git", ".venv", "node_modules"}
            )
            files.extend(Path(directory) / name for name in sorted(file_names))

    matches = []
    truncated = False
    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line_content in enumerate(content.splitlines(), start=1):
            if query not in line_content:
                continue
            if len(matches) >= max_matches:
                truncated = True
                break
            matches.append(
                {
                    "path": str(file_path.relative_to(repository_root)),
                    "line": line_number,
                    "content": line_content,
                }
            )
        if truncated:
            break

    return {
        "query": query,
        "path": path,
        "matches": matches,
        "truncated": truncated,
    }


def _resolve_repository_path(
    path: str,
    repository_root: Path,
    require_file: bool = True,
) -> Path:
    if not isinstance(path, str) or not path:
        raise ValueError("Path must be a non-empty string")
    relative_path = Path(path)
    if relative_path.is_absolute():
        raise ValueError("Path must be relative to the repository")

    root = repository_root.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("Path must stay inside the repository") from error
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if require_file and not target.is_file():
        raise ValueError(f"Path is not a file: {path}")
    return target
