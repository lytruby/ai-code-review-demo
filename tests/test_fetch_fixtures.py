import json

import pytest

from evals.fetch_fixtures import fetch_fixture, load_cases, write_fixture


def test_load_cases(tmp_path):
    case_file = tmp_path / "cases.json"
    case_file.write_text(
        json.dumps(
            [
                {
                    "id": "example-1",
                    "repository": "example/repo",
                    "pull_number": 1,
                    "pr_url": "https://github.com/example/repo/pull/1",
                    "language": "python",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert load_cases([case_file])[0]["id"] == "example-1"


def test_load_cases_rejects_duplicate_ids(tmp_path):
    case = {
        "id": "duplicate",
        "repository": "example/repo",
        "pull_number": 1,
        "pr_url": "https://github.com/example/repo/pull/1",
        "language": "python",
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps([case]), encoding="utf-8")
    second.write_text(json.dumps([case]), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate case id"):
        load_cases([first, second])


def test_fetch_fixture_builds_locked_input():
    case = {
        "id": "example-1",
        "repository": "example/repo",
        "pull_number": 1,
        "pr_url": "https://github.com/example/repo/pull/1",
        "language": "python",
    }

    def fake_fetch(url):
        if "/files?" in url:
            return [{"filename": "src/example.py", "patch": "@@ -1 +1 @@"}]
        return {"base": {"sha": "base123"}, "head": {"sha": "head456"}}

    metadata, changes = fetch_fixture(case, fake_fetch)

    assert metadata["base_sha"] == "base123"
    assert metadata["head_sha"] == "head456"
    assert changes == [
        {"filename": "src/example.py", "patch": "@@ -1 +1 @@"}
    ]


def test_write_fixture(tmp_path):
    write_fixture(
        tmp_path,
        "example-1",
        {"base_sha": "base123", "head_sha": "head456"},
        [{"filename": "example.py", "patch": "+print('hello')"}],
    )

    fixture = tmp_path / "example-1"
    metadata = json.loads((fixture / "metadata.json").read_text(encoding="utf-8"))
    changes = json.loads((fixture / "changes.json").read_text(encoding="utf-8"))

    assert metadata["head_sha"] == "head456"
    assert changes[0]["filename"] == "example.py"


def test_empty_git_blob_is_not_confused_with_missing_binary_patch():
    case = {"id": "example", "repository": "example/repo", "pull_number": 1,
            "pr_url": "https://github.com/example/repo/pull/1", "language": "python"}

    def fetch(url):
        if "/files?" in url:
            return [
                {"filename": "__init__.py", "status": "added", "changes": 0,
                 "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"},
                {"filename": "image.png", "status": "added", "changes": 0, "sha": "not-empty"},
            ]
        return {"base": {"sha": "base"}, "head": {"sha": "head"}}

    _, changes = fetch_fixture(case, fetch)
    assert changes[0]["patch"] == "Empty file added (no text content)."
    assert changes[1]["patch"] == "Patch unavailable for this file."


def test_content_preserving_rename_keeps_both_paths():
    case = {"id": "rename", "repository": "example/repo", "pull_number": 1,
            "pr_url": "https://github.com/example/repo/pull/1", "language": "java"}

    def fetch(url):
        if "/files?" in url:
            return [
                {"filename": "new.properties", "previous_filename": "old.properties",
                 "status": "renamed", "changes": 0, "additions": 0, "deletions": 0},
                {"filename": "changed.properties", "previous_filename": "before.properties",
                 "status": "renamed", "changes": 1, "additions": 1, "deletions": 0},
            ]
        return {"base": {"sha": "base"}, "head": {"sha": "head"}}

    _, changes = fetch_fixture(case, fetch)
    assert changes[0]["patch"] == "similarity index 100%\nrename from old.properties\nrename to new.properties"
    assert changes[1]["patch"] == "Patch unavailable for this file."
