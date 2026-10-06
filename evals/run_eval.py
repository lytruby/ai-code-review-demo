from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable

from src.reviewer import OpenAIReviewer


EVALS_DIR = Path(__file__).parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
RUNS_DIR = EVALS_DIR / "runs"
REPOSITORIES_DIR = EVALS_DIR / "repositories"
GIT_FETCH_RETRIES = 2


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
    for retry_index in range(GIT_FETCH_RETRIES + 1):
        try:
            _run_git(
                ["fetch", "--depth=1", "origin", expected_head],
                destination,
            )
            break
        except RuntimeError:
            if retry_index >= GIT_FETCH_RETRIES:
                raise
            time.sleep(2**retry_index)

    actual_head = _run_git(["rev-parse", "FETCH_HEAD"], destination)
    if actual_head != expected_head:
        raise ValueError(
            f"Fixture head SHA mismatch: expected {expected_head}, got {actual_head}"
        )

    _run_git(["checkout", "--detach", actual_head], destination)


def prepare_cached_repository(
    metadata: dict,
    cache_root: Path,
    checkout: Callable[[dict, Path], None] = checkout_pr,
) -> Path:
    repository_key = metadata["repository"].replace("/", "--")
    expected_head = metadata["head_sha"]
    target = cache_root / repository_key / expected_head
    marker = target / ".benchmark-head-sha"

    if target.exists():
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected_head:
            return target
        raise ValueError(f"Invalid cached repository: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"{expected_head[:12]}-",
        dir=target.parent,
    ) as temp_dir:
        temporary_checkout = Path(temp_dir)
        checkout(metadata, temporary_checkout)
        (temporary_checkout / ".benchmark-head-sha").write_text(
            expected_head + "\n",
            encoding="utf-8",
        )
        temporary_checkout.replace(target)

    return target


def save_result(
    run_dir: Path,
    case_id: str,
    result,
    trace: list[dict] | None = None,
) -> Path:
    case_dir = run_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    result_path = case_dir / "result.json"
    if result_path.exists():
        raise FileExistsError(f"Result already exists: {result_path}")
    output = asdict(result)
    output["trace"] = trace or []
    result_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result_path


def save_failure(
    run_dir: Path,
    case_id: str,
    error: Exception,
    trace: list[dict] | None = None,
) -> Path:
    case_dir = run_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    error_path = case_dir / "error.json"
    error_index = 2
    while error_path.exists():
        error_path = case_dir / f"error-{error_index}.json"
        error_index += 1
    error_path.write_text(
        json.dumps(
            {
                "error_type": type(error).__name__,
                "error": str(error),
                "trace": trace or [],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return error_path


def run_case(
    fixture_dir: Path,
    run_dir: Path,
    reviewer_factory: Callable[..., OpenAIReviewer] = OpenAIReviewer,
    checkout: Callable[[dict, Path], None] = checkout_pr,
    repository_cache: Path | None = None,
    provider: str | None = None,
    reviewer_settings: dict | None = None,
    discover_only: bool = False,
) -> Path:
    metadata, changes = load_fixture(fixture_dir)
    case_id = metadata["id"]

    def review_repository(repository_root: Path):
        reviewer_options = {"repository_root": repository_root, **(reviewer_settings or {})}
        if provider is not None:
            reviewer_options["provider"] = provider
        reviewer = reviewer_factory(**reviewer_options)
        try:
            if discover_only:
                result = reviewer.discover_only(changes)
            else:
                result = reviewer.review(changes)
        except Exception as error:
            error_path = save_failure(
                run_dir,
                case_id,
                error,
                trace=getattr(reviewer, "last_trace", []),
            )
            raise RuntimeError(
                f"{error}. Failure trace saved to {error_path}"
            ) from error
        return result, getattr(reviewer, "last_trace", [])

    # Unit discovery and caller context read the repository, so they need the checkout.
    settings = reviewer_settings or {}
    needs_checkout = (
        not discover_only
        or settings.get("discovery_mode") == "units"
        or settings.get("discovery_context", "none") != "none"
    )
    if repository_cache is not None and needs_checkout:
        repository_root = prepare_cached_repository(
            metadata,
            repository_cache,
            checkout=checkout,
        )
        result, trace = review_repository(repository_root)
    else:
        with tempfile.TemporaryDirectory(prefix=f"code-review-{case_id}-") as temp_dir:
            repository_root = Path(temp_dir)
            # Discovery reads only the diff, so it needs no checkout.
            if needs_checkout:
                checkout(metadata, repository_root)
            result, trace = review_repository(repository_root)

    return save_result(
        run_dir,
        case_id,
        result,
        trace=trace,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the code review agent against one locked eval fixture."
    )
    parser.add_argument("--case-id", required=True, help="Fixture id to run.")
    parser.add_argument(
        "--provider",
        required=True,
        choices=("kimi", "openai"),
        help="Model provider name used as the output directory, for example kimi.",
    )
    parser.add_argument(
        "--run-name",
        required=True,
        help="Agent version or experiment name, for example baseline or sop-v1.",
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--runs", type=Path, default=RUNS_DIR)
    parser.add_argument(
        "--repository-cache",
        type=Path,
        default=REPOSITORIES_DIR,
        help="Persistent cache for repositories checked out at fixture head SHAs.",
    )
    args = parser.parse_args()

    fixture_dir = args.fixtures / args.case_id
    if not fixture_dir.is_dir():
        parser.error(f"Fixture not found: {fixture_dir}")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.provider):
        parser.error("Provider may contain only letters, numbers, '-' and '_'")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_name):
        parser.error("Run name may contain only letters, numbers, '-' and '_'")

    run_dir = args.runs / args.provider.lower() / args.run_name
    print(f"run {args.case_id} ({args.provider.lower()}/{args.run_name})")
    result_path = run_case(
        fixture_dir,
        run_dir,
        repository_cache=args.repository_cache,
        provider=args.provider.lower(),
    )
    print(f"saved {result_path}")


if __name__ == "__main__":
    main()
