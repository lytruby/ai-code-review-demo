"""Map an issue's evidence excerpts to line numbers in the head version of a file."""

import re
from pathlib import Path

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def patch_after_lines(patch: str) -> list[tuple[int, str]]:
    """Return (head line number, text) for every context and added line of a patch."""
    lines = []
    number = None
    for raw in (patch or "").splitlines():
        header = HUNK_HEADER.match(raw)
        if header:
            number = int(header.group(1))
            continue
        if number is None or raw.startswith(("---", "+++", "\\")):
            continue
        if raw.startswith("-"):
            continue
        lines.append((number, raw[1:]))
        number += 1
    return lines


def first_added_line(patch: str) -> int | None:
    number = None
    for raw in (patch or "").splitlines():
        header = HUNK_HEADER.match(raw)
        if header:
            number = int(header.group(1))
            continue
        if number is None or raw.startswith(("---", "+++", "\\")):
            continue
        if raw.startswith("+"):
            return number
        if not raw.startswith("-"):
            number += 1
    return None


def locate_text(text: str, numbered_lines: list[tuple[int, str]]) -> tuple[int, int] | None:
    """Find text as consecutive lines, ignoring indentation and blank lines."""
    wanted = [line.strip() for line in text.splitlines() if line.strip()]
    source = [(number, line.strip()) for number, line in numbered_lines if line.strip()]
    if not wanted or len(wanted) > len(source):
        return None
    for start in range(len(source) - len(wanted) + 1):
        window = source[start:start + len(wanted)]
        # The first and last excerpt lines may be partial lines.
        if all(a == b for a, (_, b) in zip(wanted[1:-1], window[1:-1])) and (
            wanted[0] in window[0][1] and wanted[-1] in window[-1][1]
        ):
            return window[0][0], window[-1][0]
    return None


def _file_lines(repository_root: Path | None, path: str) -> list[tuple[int, str]]:
    if repository_root is None:
        return []
    try:
        content = (Path(repository_root) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(enumerate(content.splitlines(), start=1))


def locate_issue(
    file: str,
    excerpts: list[str],
    patch: str,
    repository_root: Path | None,
) -> tuple[int, int] | None:
    """Return the head line range of the first excerpt found, else the first added line."""
    after = patch_after_lines(patch)
    head = None
    for text in excerpts:
        found = locate_text(text, after)
        if found is None:
            if head is None:
                head = _file_lines(repository_root, file)
            found = locate_text(text, head)
        if found is not None:
            return found
    line = first_added_line(patch)
    return (line, line) if line is not None else None
