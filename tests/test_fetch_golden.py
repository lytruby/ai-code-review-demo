import json

import pytest

from evals.fetch_golden import download_golden, find_golden_entry, load_dev_cases


def test_load_dev_cases_requires_golden_file(tmp_path):
    path = tmp_path / "dev.json"
    path.write_text(
        json.dumps([{"id": "example", "pr_url": "https://example.com/pr/1"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="golden_file"):
        load_dev_cases(path)


def test_find_golden_entry_matches_original_url():
    entry = {
        "url": "https://example.com/copied/pr/1",
        "original_url": "https://example.com/original/pr/1",
        "comments": [],
    }

    assert (
        find_golden_entry([entry], "https://example.com/original/pr/1") is entry
    )


def test_download_golden_writes_only_selected_case(tmp_path):
    cases = [
        {
            "id": "example-1",
            "pr_url": "https://github.com/example/repo/pull/1",
            "golden_file": "example.json",
        }
    ]

    def fake_fetch(url):
        assert url.endswith("/golden_comments/example.json")
        return [
            {
                "url": cases[0]["pr_url"],
                "comments": [{"comment": "A real bug", "severity": "High"}],
            },
            {
                "url": "https://github.com/example/repo/pull/2",
                "comments": [{"comment": "Hidden case", "severity": "High"}],
            },
        ]

    paths = download_golden(cases, "main", tmp_path, get_json=fake_fetch)

    output = json.loads(paths[0].read_text(encoding="utf-8"))
    assert output["case_id"] == "example-1"
    assert output["comments"] == [
        {"comment": "A real bug", "severity": "High"}
    ]
