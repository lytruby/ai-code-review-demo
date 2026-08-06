from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Callable


CASES_DIR = Path(__file__).parent / "cases"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_CASE_FILES = [CASES_DIR / "dev.json", CASES_DIR / "heldout.json"]


def github_json(url: str, token: str | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-code-review-eval",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def load_cases(case_files: list[Path]) -> list[dict]:
    cases = []
    seen_ids = set()

    for case_file in case_files:
        with case_file.open(encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError(f"Case file must contain a list: {case_file}")

        for case in data:
            if not isinstance(case, dict):
                raise ValueError(f"Each case must be an object: {case_file}")

            required = {"id", "repository", "pull_number", "pr_url", "language"}
            missing = required - set(case)
            if missing:
                raise ValueError(
                    f"Case in {case_file} is missing: {', '.join(sorted(missing))}"
                )
            if case["id"] in seen_ids:
                raise ValueError(f"Duplicate case id: {case['id']}")

            seen_ids.add(case["id"])
            cases.append(case)

    return cases


def fetch_fixture(
    case: dict,
    fetch_json: Callable[[str], object],
) -> tuple[dict, list[dict]]:
    repository = case["repository"]
    pull_number = case["pull_number"]
    api_root = f"https://api.github.com/repos/{repository}/pulls/{pull_number}"

    pull = fetch_json(api_root)
    if not isinstance(pull, dict):
        raise ValueError(f"Unexpected PR response for {case['id']}")

    changes = []
    page = 1
    while True:
        files = fetch_json(f"{api_root}/files?per_page=100&page={page}")
        if not isinstance(files, list):
            raise ValueError(f"Unexpected files response for {case['id']}")

        changes.extend(
            {
                "filename": item["filename"],
                "patch": item.get("patch", "Patch unavailable for this file."),
            }
            for item in files
        )

        if len(files) < 100:
            break
        page += 1

    metadata = {
        "id": case["id"],
        "repository": repository,
        "pull_number": pull_number,
        "pr_url": case["pr_url"],
        "language": case["language"],
        "base_sha": pull["base"]["sha"],
        "head_sha": pull["head"]["sha"],
    }
    return metadata, changes


def write_fixture(
    output_dir: Path,
    case_id: str,
    metadata: dict,
    changes: list[dict],
) -> None:
    fixture_dir = output_dir / case_id
    fixture_dir.mkdir(parents=True, exist_ok=True)

    for filename, data in (
        ("metadata.json", metadata),
        ("changes.json", changes),
    ):
        path = fixture_dir / filename
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and lock GitHub PR inputs for local evaluation."
    )
    parser.add_argument(
        "--case-file",
        action="append",
        type=Path,
        dest="case_files",
        help="JSON case file. May be supplied more than once.",
    )
    parser.add_argument("--case-id", help="Download only one case id.")
    parser.add_argument("--output", type=Path, default=FIXTURES_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing fixture instead of skipping it.",
    )
    args = parser.parse_args()

    case_files = args.case_files or DEFAULT_CASE_FILES
    cases = load_cases(case_files)
    if args.case_id:
        cases = [case for case in cases if case["id"] == args.case_id]
        if not cases:
            parser.error(f"Unknown case id: {args.case_id}")

    token = os.environ.get("GITHUB_TOKEN")
    fetch_json = lambda url: github_json(url, token=token)

    for case in cases:
        fixture_dir = args.output / case["id"]
        if fixture_dir.exists() and not args.force:
            print(f"skip {case['id']}: fixture already exists")
            continue

        print(f"fetch {case['id']}")
        metadata, changes = fetch_fixture(case, fetch_json)
        write_fixture(args.output, case["id"], metadata, changes)
        print(f"saved {case['id']}: {len(changes)} changed files")


if __name__ == "__main__":
    main()
