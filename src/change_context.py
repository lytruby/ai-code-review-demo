"""Repository context for discovery: callers of changed functions and the
definitions of functions the change calls."""

import re
from pathlib import Path

from src.tools import search_code

MAX_SYMBOLS = 10
MAX_CALLED_SYMBOLS = 8
MAX_SITES_PER_SYMBOL = 3
# Names with more matches than this are too generic to show useful callers.
MAX_MATCHES_PER_SYMBOL = 40
SITE_CONTEXT_LINES = 2
DEFINITION_CONTEXT_LINES = 6
MAX_CONTEXT_CHARS = 12_000

DEFINITION_PATTERNS = (
    r"\bdef\s+(?:self\.)?(\w+)",
    r"\bfunction\s*\*?\s*(\w+)",
    r"\bfunc\s+(?:\([^)]*\)\s*)?(\w+)",
    r"\bclass\s+(\w+)",
    r"\b(\w+)\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|\w+\s*=>)",
    r"^\s*(?:(?:public|private|protected|static|final|abstract|async|override|synchronized|export|default)\s+)+"
    r"(?:[\w<>\[\],.?]+\s+)?(\w+)\s*\(",
    r"^\s*(?:async\s+)?(\w+)\s*\([^()]*\)\s*(?::\s*[\w<>\[\], .|?]+)?\s*\{",
)
CALL_PATTERN = re.compile(r"(?<![\w.$])(?:[\w$]+\.)*([A-Za-z_]\w*)\s*\(")
STOP_WORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "constructor",
    "super", "this", "self", "print", "require", "import", "assert", "expect",
    "describe", "it", "test", "len", "str", "int", "list", "dict", "set", "get",
    "map", "filter", "join", "push", "then", "string", "number", "object",
    "array", "promise", "error", "init", "main", "render", "setup", "new",
    "typeof", "await", "async", "sizeof", "format", "append", "split", "keys",
    "values", "items", "range", "isinstance", "useState", "useEffect", "Error",
    "String", "Number", "Object", "Array", "Promise", "Date", "JSON", "console",
    "toString", "length", "update", "create", "delete", "find", "where", "select",
}


def _defined_names(line: str) -> list[str]:
    names = []
    for pattern in DEFINITION_PATTERNS:
        for match in re.finditer(pattern, line):
            names.append(match.group(1))
    return names


def _useful(name: str) -> bool:
    return len(name) >= 4 and name not in STOP_WORDS and not name.isupper()


def _changed_symbols(changes) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (defined, called): functions the change edits, then functions it calls."""
    defined, called = [], []
    seen_defined, seen_called = set(), set()
    def add_defined(name, filename):
        if _useful(name) and name not in seen_defined:
            seen_defined.add(name)
            defined.append((name, filename))

    for change in changes:
        filename = change["filename"]
        enclosing = []
        for raw in (change.get("patch") or "").splitlines():
            if raw.startswith("@@"):
                # Git puts the enclosing function after the hunk range.
                enclosing = _defined_names(raw.split("@@", 2)[-1])
                continue
            if raw.startswith(("+++", "---")):
                continue
            line = raw[1:]
            names = _defined_names(line)
            if raw[:1] not in "+-":
                # An unchanged definition line encloses the changes below it.
                enclosing = names or enclosing
                continue
            for name in enclosing + names:
                add_defined(name, filename)
            if raw.startswith("+"):
                for match in CALL_PATTERN.finditer(line):
                    name = match.group(1)
                    if _useful(name) and name not in seen_called:
                        seen_called.add(name)
                        called.append(name)
    called = [name for name in called if name not in seen_defined]
    return defined[:MAX_SYMBOLS], called[:MAX_CALLED_SYMBOLS]


def _matches(name: str, repository_root: Path) -> list[dict] | None:
    try:
        # Fixed-string search also hits longer names, so fetch more and filter.
        result = search_code(name, repository_root, max_matches=4 * MAX_MATCHES_PER_SYMBOL)
    except (OSError, ValueError, RuntimeError):
        return None
    if result["truncated"]:
        return None
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    matches = [m for m in result["matches"] if word.search(m["content"])]
    return None if len(matches) > MAX_MATCHES_PER_SYMBOL else matches


def _excerpt(repository_root: Path, path: str, line: int, before: int, after: int) -> str:
    try:
        lines = (repository_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    first, last = max(1, line - before), min(len(lines), line + after)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(first, last + 1))


def _normalize(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def build_change_context(changes, repository_root: Path | None) -> str:
    """Return a context block for the discovery prompt, or "" when there is none."""
    if repository_root is None or not Path(repository_root).is_dir():
        return ""
    root = Path(repository_root)
    defined, called = _changed_symbols(changes)
    sections = []

    for name, filename in defined:
        matches = _matches(name, root)
        if not matches:
            continue
        sites = [m for m in matches if name not in _defined_names(m["content"])]
        # Callers elsewhere say more about the contract than the changed file.
        sites.sort(key=lambda m: _normalize(m["path"]) == filename)
        excerpts = [
            f"{_normalize(m['path'])}:{m['line']}\n"
            + _excerpt(root, m["path"], m["line"], SITE_CONTEXT_LINES, SITE_CONTEXT_LINES)
            for m in sites[:MAX_SITES_PER_SYMBOL]
        ]
        if excerpts:
            sections.append(
                f"Callers of {name} (changed in {filename}), {len(sites)} use(s):\n"
                + "\n".join(excerpts)
            )

    for name in called:
        matches = _matches(name, root)
        if not matches:
            continue
        definitions = [m for m in matches if name in _defined_names(m["content"])]
        if not definitions or len(definitions) > 2:
            continue
        excerpts = [
            f"{_normalize(m['path'])}:{m['line']}\n"
            + _excerpt(root, m["path"], m["line"], 0, DEFINITION_CONTEXT_LINES)
            for m in definitions
        ]
        sections.append(f"Definition of {name}, called by the change:\n" + "\n".join(excerpts))

    text = ""
    for section in sections:
        if len(text) + len(section) > MAX_CONTEXT_CHARS:
            break
        text += section + "\n\n"
    return text.strip()
