import json
from pathlib import Path


MAX_FILE_CHARS = 10_000

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
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "strict": True,
}


def execute_tool(
    tool_name: str,
    arguments: str,
    repository_root: Path,
) -> str:
    if tool_name != "read_file":
        return json.dumps({"ok": False, "error": f"Unknown tool: {tool_name}"})

    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "Tool arguments must be valid JSON"})

    if not isinstance(parsed_arguments, dict):
        return json.dumps({"ok": False, "error": "Tool arguments must be an object"})
    if set(parsed_arguments) != {"path"}:
        return json.dumps(
            {"ok": False, "error": "read_file accepts only the path argument"}
        )

    try:
        content = read_file(
            path=parsed_arguments["path"],
            repository_root=repository_root,
        )
    except (FileNotFoundError, ValueError) as error:
        return json.dumps({"ok": False, "error": str(error)})

    return json.dumps({"ok": True, "content": content})


def read_file(
    path: str,
    repository_root: Path,
    max_chars: int = MAX_FILE_CHARS,
) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("File path must be a non-empty string")

    relative_path = Path(path)
    if relative_path.is_absolute():
        raise ValueError("File path must be relative to the repository")

    root = repository_root.resolve()
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("File path must stay inside the repository") from error

    if not target.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not target.is_file():
        raise ValueError(f"Path is not a file: {path}")

    try:
        with target.open(encoding="utf-8") as file:
            content = file.read(max_chars + 1)
    except UnicodeDecodeError as error:
        raise ValueError(f"File is not UTF-8 text: {path}") from error

    if len(content) > max_chars:
        raise ValueError(f"File exceeds the {max_chars}-character limit: {path}")

    return content
