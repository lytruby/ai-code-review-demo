from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable

from src.reviewer import OpenAIReviewer


EVALS_DIR = Path(__file__).parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
RUNS_DIR = EVALS_DIR / "runs"


def load_fixture(fixture_dir: Path) -> tuple[dict, list[dict]]:
    metadata_path = fixture_dir / "metadata.json"
    changes_path = fixture_dir / "changes.json"

    if not metadata_path.is_file() or not changes_path.is_file():
        raise FileNotFoundError(f"Incomplete fixture: {fixture_dir}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    changes = json.loads(changes_path.read_text(encoding="utf-8"))

    if not isinstance(metadata, dict):
        raise ValueError(f"Fixture metadata must be an object: {metadata_path}")
    if not isinstance(changes, list):
        raise ValueError(f"Fixture changes must be a list: {changes_path}")

    return metadata, changes


def _run_git(arguments: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def checkout_pr(metadata: dict, destination: Path) -> None:
    repository = metadata["repository"]
    pull_number = metadata["pull_number"]
    expected_head = metadata["head_sha"]

    _run_git(["init"], destination)
    _run_git(
        ["remote", "add", "origin", f"https://github.com/{repository}.git"],
        destination,
    )
    _run_git(
        ["fetch", "--depth=1", "origin", f"pull/{pull_number}/head"],
        destination,
    )

    actual_head = _run_git(["rev-parse", "FETCH_HEAD"], destination)
    if actual_head != expected_head:
        raise ValueError(
            f"Fixture head SHA mismatch: expected {expected_head}, got {actual_head}"
        )

    _run_git(["checkout", "--detach", actual_head], destination)


def save_result(run_dir: Path, case_id: str, result) -> Path:
    case_dir = run_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    result_path = case_dir / "result.json"
    result_path.write_text(
        json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result_path


def run_case(
    fixture_dir: Path,
    run_dir: Path,
    reviewer_factory: Callable[..., OpenAIReviewer] = OpenAIReviewer,
    checkout: Callable[[dict, Path], None] = checkout_pr,
) -> Path:
    metadata, changes = load_fixture(fixture_dir)
    case_id = metadata["id"]

    with tempfile.TemporaryDirectory(prefix=f"code-review-{case_id}-") as temp_dir:
        repository_root = Path(temp_dir)
        checkout(metadata, repository_root)
        reviewer = reviewer_factory(repository_root=repository_root)
        result = reviewer.review(changes)

    return save_result(run_dir, case_id, result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the code review agent against one locked eval fixture."
    )
    parser.add_argument("--case-id", required=True, help="Fixture id to run.")
    parser.add_argument(
        "--provider",
        required=True,
        help="Model provider name used as the output directory, for example kimi.",
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--runs", type=Path, default=RUNS_DIR)
    args = parser.parse_args()

    fixture_dir = args.fixtures / args.case_id
    if not fixture_dir.is_dir():
        parser.error(f"Fixture not found: {fixture_dir}")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.provider):
        parser.error("Provider may contain only letters, numbers, '-' and '_'")

    run_dir = args.runs / args.provider.lower()
    print(f"run {args.case_id}")
    result_path = run_case(fixture_dir, run_dir)
    print(f"saved {result_path}")


if __name__ == "__main__":
    main()
