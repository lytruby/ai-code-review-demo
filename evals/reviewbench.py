"""Run the reviewer on ReviewBench PRs and score the findings with its judge.

ReviewBench (https://github.com/review-bench/ReviewBench) is cloned into
evals/reviewbench (not committed). Repositories come from its mirrors at
github.com/review-bench/<owner>_<repo>. Results go to
evals/benchmark-runs/reviewbench/<run-name>/.

    uv run python -m evals.reviewbench review --run-name rb-v1 --jobs 4
    uv run python -m evals.reviewbench judge --run-name rb-v1
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from dotenv import load_dotenv

from evals.benchmark import review_usage
from src.reviewer import OpenAIReviewer

EVALS_DIR = Path(__file__).parent
BENCH_DIR = EVALS_DIR / "reviewbench"
REPOSITORIES_DIR = EVALS_DIR / "repositories" / "reviewbench"
RUNS_DIR = EVALS_DIR / "benchmark-runs" / "reviewbench"
AGENT = "ai-code-review-demo"
MIRROR_ORG = "review-bench"


def pr_key(pr: dict) -> str:
    repo = pr["repo"].removeprefix("https://github.com/").replace("/", "_", 1)
    return f"{repo}_{pr['pr_number']}-{pr['head'][:8]}"


def load_prs(split: str) -> list[dict]:
    path = BENCH_DIR / "corpus" / "test" / "test.json" if split == "test" else BENCH_DIR / "corpus" / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"Clone ReviewBench first: git clone --depth 1 https://github.com/review-bench/ReviewBench.git {BENCH_DIR}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data["prs"]


def _git(arguments: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def checkout(pr: dict) -> Path:
    """Return a cached head checkout with enough history to find the merge base."""
    owner, repo = pr["repo"].removeprefix("https://github.com/").split("/", 1)
    for sha in (pr["base"], pr["head"]):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"Invalid commit: {sha}")
    target = REPOSITORIES_DIR / f"{owner}__{repo}" / pr["head"]
    if (target / ".git").is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        work = Path(temp) / "repo"
        # Blobless clone, as the ReviewBench judge does: full history, file contents on demand.
        _git(["clone", "-q", "--filter=blob:none", "--no-checkout",
              f"https://github.com/{MIRROR_ORG}/{owner}_{repo}.git", str(work)], target.parent)
        for sha in (pr["base"], pr["head"]):
            if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=work, capture_output=True).returncode:
                _git(["fetch", "-q", "origin", sha], work)
        _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", pr["head"]], work)
        work.replace(target)
    return target


def build_changes(repository: Path, base: str, head: str) -> list[dict]:
    """Split the PR diff (base...head, as GitHub and ReviewBench show it) into {filename, patch} changes."""
    diff = _git(["diff", "--no-ext-diff", "--no-textconv", "--no-color", f"{base}...{head}"], repository)
    changes = []
    for section in re.split(r"(?m)^(?=diff --git )", diff):
        if not section.startswith("diff --git "):
            continue
        header, _, body = section.partition("\n@@")
        new_path = re.search(r"(?m)^\+\+\+ (?:b/(.*)|/dev/null)$", header)
        if not body or new_path is None or new_path.group(1) is None:
            continue  # Binary, mode-only or deleted file.
        changes.append({"filename": new_path.group(1), "patch": "@@" + body.rstrip("\n")})
    return changes


def findings_file(pr: dict, result, usage: dict, seconds: float) -> dict:
    findings = []
    for issue in result.issues:
        line = issue.start_line or 1
        message = issue.description
        if issue.suggestion:
            message += f"\n\nSuggestion: {issue.suggestion}"
        findings.append({"producer": AGENT, "file": issue.file, "start_line": line,
                         "end_line": max(issue.end_line or line, line), "message": message})
    return {
        "pr": {key: pr[key] for key in ("repo", "pr_number", "base", "head")},
        "agent": AGENT,
        "findings": findings,
        "usage": {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                  "cached_tokens": usage["cached_prompt_tokens"], "time_in_ms": int(seconds * 1000)},
    }


def review_pr(pr: dict, run_dir: Path, provider: str, settings: dict) -> str:
    key = pr_key(pr)
    candidate_path = run_dir / "candidates" / f"{key}.json"
    if candidate_path.exists():
        return f"{key}: done"
    repository = checkout(pr)
    changes = build_changes(repository, pr["base"], pr["head"])
    reviewer = OpenAIReviewer(repository_root=repository, provider=provider, **settings)
    started = time.monotonic()
    case_dir = run_dir / "cases" / key
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = reviewer.review(changes)
    except Exception as error:
        (case_dir / "error.json").write_text(json.dumps(
            {"error_type": type(error).__name__, "error": str(error), "trace": reviewer.last_trace},
            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raise
    seconds = time.monotonic() - started
    responses = [e for e in reviewer.last_trace if e.get("type") == "model_response"]
    usage = review_usage(responses)
    (case_dir / "result.json").write_text(json.dumps(
        {**asdict(result), "usage": usage, "seconds": round(seconds, 1), "trace": reviewer.last_trace},
        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(findings_file(pr, result, usage, seconds), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{key}: {len(result.issues)} findings, {usage['uncached_prompt_tokens']} uncached / {usage['cached_prompt_tokens']} cached / {usage['completion_tokens']} output tokens, {seconds:.0f}s"


def review(args, settings: dict) -> None:
    prs = load_prs(args.split)
    if args.pr:
        prs = [pr for pr in prs if pr_key(pr) in args.pr]
    if args.max_lines:
        prs = [pr for pr in prs if pr["lines_added"] + pr["lines_removed"] < args.max_lines]
    if args.limit:
        prs = prs[:args.limit]
    run_dir = RUNS_DIR / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    config = {"provider": args.provider, "split": args.split, "settings": settings,
              "model": os.environ.get("KIMI_MODEL" if args.provider == "kimi" else "OPENAI_MODEL")}
    config_path = run_dir / "run.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8"))["settings"] != settings:
        raise SystemExit("Settings changed; use a new --run-name")
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def one(pr):
        try:
            return review_pr(pr, run_dir, args.provider, settings)
        except Exception as error:
            return f"{pr_key(pr)}: FAILED {type(error).__name__}: {str(error)[:300]}"

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for index, line in enumerate(pool.map(one, prs), 1):
            print(f"[{index}/{len(prs)}] {line}", flush=True)
    totals = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "findings": 0, "prs": 0}
    for path in (run_dir / "candidates").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        totals["prs"] += 1
        totals["findings"] += len(data["findings"])
        for name in ("prompt_tokens", "cached_tokens", "completion_tokens"):
            totals[name] += data["usage"][name]
    print(f"Reviewed {totals['prs']} PRs, {totals['findings']} findings. Review tokens: "
          f"{totals['prompt_tokens'] - totals['cached_tokens']} uncached prompt, "
          f"{totals['cached_tokens']} cached prompt, {totals['completion_tokens']} completion.")


def judge(args) -> None:
    run_dir = (RUNS_DIR / args.run_name).resolve()
    if not shutil.which("npm"):
        raise SystemExit("npm is required for the ReviewBench judge")
    if not (BENCH_DIR / "node_modules").is_dir():
        subprocess.run(["npm", "ci"], cwd=BENCH_DIR, check=True)
    output = run_dir / f"judge-{args.judge_model}.json"
    command = ["npm", "run", "judge", "--", "--candidate", str(run_dir / "candidates"),
               "--provider", args.judge_provider, "--model", args.judge_model,
               "--output", str(output), "--concurrency", str(args.jobs),
               "--repo-dir", str(REPOSITORIES_DIR.resolve() / "judge")]
    if args.limit:
        command += ["--limit", str(args.limit)]
    with (run_dir / f"judge-{args.judge_model}.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=BENCH_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
        if process.wait():
            raise SystemExit(process.returncode)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("review", "judge"))
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--provider", choices=("kimi", "openai"), default="kimi")
    parser.add_argument("--split", choices=("test", "all"), default="test")
    parser.add_argument("--pr", nargs="+", help="Only these PR keys, e.g. owner_repo_12-abcdef01")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--max-lines", type=int, help="Only PRs with fewer added+removed lines than this")
    parser.add_argument("--report-rule", help="e.g. certainty=4")
    parser.add_argument("--verify-policy", choices=("strict", "refute"))
    parser.add_argument("--judge-provider", default="openai")
    # The judge's model registry has no gpt-6.1-sol; gpt-5.5 is its newest OpenAI model.
    parser.add_argument("--judge-model", default="gpt-5.5")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_name):
        parser.error("Invalid run name")
    settings = {}
    if args.report_rule:
        settings["report_rule"] = {name.strip(): int(value) for name, value in
                                   (item.split("=") for item in args.report_rule.split(","))}
    if args.verify_policy:
        settings["verify_policy"] = args.verify_policy
    if args.command == "review":
        review(args, settings)
    else:
        judge(args)


if __name__ == "__main__":
    main()
