from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable
import urllib.request


EVALS_DIR = Path(__file__).parent
DEV_CASES = EVALS_DIR / "cases" / "dev.json"
GOLDEN_DIR = EVALS_DIR / "golden" / "dev"
RAW_ROOT = (
    "https://raw.githubusercontent.com/withmartian/"
    "code-review-benchmark/{ref}/offline/golden_comments/{filename}"
)


def fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-code-review-eval"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def load_dev_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError(f"Development cases must be a list: {path}")

    for case in cases:
        if not isinstance(case, dict) or not all(
            key in case for key in ("id", "pr_url", "golden_file")
        ):
            raise ValueError(
                "Each development case needs id, pr_url, and golden_file"
            )
    return cases


def find_golden_entry(entries: object, pr_url: str) -> dict:
    if not isinstance(entries, list):
        raise ValueError("Official golden file must contain a list")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if pr_url in {entry.get("url"), entry.get("original_url")}:
            return entry

    raise ValueError(f"No golden comments found for {pr_url}")


def download_golden(
    cases: list[dict],
    ref: str,
    output_dir: Path,
    get_json: Callable[[str], object] = fetch_json,
    force: bool = False,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cached_files = {}
    written = []

    for case in cases:
        output_path = output_dir / f"{case['id']}.json"
        if output_path.exists() and not force:
            print(f"skip {case['id']}: golden file already exists")
            continue

        filename = case["golden_file"]
        if filename not in cached_files:
            url = RAW_ROOT.format(ref=ref, filename=filename)
            cached_files[filename] = (url, get_json(url))

        source_url, entries = cached_files[filename]
        entry = find_golden_entry(entries, case["pr_url"])
        comments = entry.get("comments")
        if not isinstance(comments, list):
            raise ValueError(f"Golden comments must be a list for {case['id']}")

        output = {
            "case_id": case["id"],
            "source": source_url,
            "comments": comments,
        }
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(output_path)
        print(f"saved {case['id']}: {len(comments)} golden comments")

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download golden comments for development cases only."
    )
    parser.add_argument("--cases", type=Path, default=DEV_CASES)
    parser.add_argument("--output", type=Path, default=GOLDEN_DIR)
    parser.add_argument("--ref", default="main", help="Benchmark git ref.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    cases = load_dev_cases(args.cases)
    download_golden(cases, args.ref, args.output, force=args.force)


if __name__ == "__main__":
    main()
