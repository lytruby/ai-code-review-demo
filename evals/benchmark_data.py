"""Import the complete, pinned offline dataset without exposing answers to review."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from evals.fetch_fixtures import fetch_fixture, write_fixture

REVISION = "e616e849755441da38f18bf3adba2c9583b03803"
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "benchmark-data"
SOURCES = {"sentry": "python", "grafana": "go", "cal_dot_com": "typescript",
           "discourse": "ruby", "keycloak": "java"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_json(url: str):
    headers = {"User-Agent": "ai-code-review-benchmark", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def prepare_catalog(data_dir: Path = DATA_DIR, fetch=get_json) -> dict:
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        return load_catalog(data_dir)
    known = {}
    for split in ("dev", "heldout"):
        for case in read_json(ROOT / "cases" / f"{split}.json"):
            known[case["pr_url"]] = (case["id"], split)
    cases = []
    for project, language in SOURCES.items():
        url = f"https://raw.githubusercontent.com/withmartian/code-review-benchmark/{REVISION}/offline/golden_comments/{project}.json"
        entries = fetch(url)
        if not isinstance(entries, list) or len(entries) != 10:
            raise ValueError(f"Expected ten cases in {project}")
        for entry in entries:
            pr_url = entry["url"]  # Copied benchmark PRs are authoritative, not original_url.
            match = re.fullmatch(r"https://github.com/([\w.-]+/[\w.-]+)/pull/(\d+)", pr_url)
            if not match:
                raise ValueError(f"Unsupported benchmark PR URL: {pr_url}")
            repository, number = match.groups()
            identity = known.get(pr_url) or known.get(entry.get("original_url"))
            case_id, split = identity or (f"{project}-{'benchmark-' if repository.startswith('ai-code-review-evaluation/') else ''}{number}", "benchmark")
            comments = entry["comments"]
            if not isinstance(comments, list) or any(
                not isinstance(c, dict) or not isinstance(c.get("comment"), str)
                or c.get("category") not in {"bug", "security", "concurrency", "data", "api", "perf", "test_gap", "doc_defect", "style", "speculative"}
                for c in comments
            ):
                raise ValueError(f"Invalid golden comments: {case_id}")
            golden = {"case_id": case_id, "source": url, "comments": comments}
            write_json(data_dir / "golden" / f"{case_id}.json", golden)
            cases.append({"id": case_id, "project": project, "repository": repository,
                          "pull_number": int(number), "pr_url": pr_url, "language": language,
                          "split": split, "golden_count": len(comments), "golden_sha256": digest(golden)})
    if len(cases) != 50 or len({c["id"] for c in cases}) != 50 or sum(c["golden_count"] for c in cases) != 173:
        raise ValueError("Pinned benchmark must contain 50 unique PRs and 173 golden comments")
    manifest = {"benchmark_revision": REVISION, "cases": cases}
    write_json(manifest_path, manifest)
    return manifest


def load_catalog(data_dir: Path) -> dict:
    manifest = read_json(data_dir / "manifest.json")
    if manifest["benchmark_revision"] != REVISION:
        raise ValueError("Benchmark revision changed; use a separate data directory")
    for case in manifest["cases"]:
        golden = read_json(data_dir / "golden" / f"{case['id']}.json")
        if digest(golden) != case["golden_sha256"]:
            raise ValueError(f"Golden data changed: {case['id']}")
    return manifest


def prepare_fixture(case: dict, data_dir: Path, fetch=get_json) -> Path:
    target = data_dir / "fixtures" / case["id"]
    # Reuse existing locked inputs for the five original cases when identity matches.
    for source in (target, ROOT / "fixtures" / case["id"]):
        if (source / "metadata.json").is_file() and (source / "changes.json").is_file():
            metadata = read_json(source / "metadata.json")
            if metadata.get("repository") != case["repository"] or metadata.get("pull_number") != case["pull_number"]:
                raise ValueError(f"Fixture identity mismatch: {source}")
            if source != target:
                write_fixture(data_dir / "fixtures", case["id"], metadata, read_json(source / "changes.json"))
            return target
    metadata, changes = fetch_fixture(case, fetch)
    # Do not accept a diff fetched while the PR was being updated.
    latest = fetch(f"https://api.github.com/repos/{case['repository']}/pulls/{case['pull_number']}")
    if latest["head"]["sha"] != metadata["head_sha"] or latest["base"]["sha"] != metadata["base_sha"]:
        raise ValueError(f"PR changed during fixture download: {case['id']}; retry")
    if latest.get("changed_files", len(changes)) != len(changes):
        raise ValueError(f"Incomplete GitHub file list: {case['id']}")
    if any(c["patch"] == "Patch unavailable for this file." for c in changes):
        raise ValueError(f"Missing patch in {case['id']}; cannot score incomplete input")
    write_fixture(data_dir / "fixtures", case["id"], metadata, changes)
    return target
