"""Local full-benchmark runner: no GitHub comments or leaderboard submission."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import time

from dotenv import load_dotenv

from evals.benchmark_data import DATA_DIR, ROOT, digest, load_catalog, prepare_catalog, prepare_fixture, read_json, write_json
from evals.benchmark_score import BenchmarkJudge, SCORER_VERSION, candidate_texts, profile_scores, score_case
from evals.run_eval import REPOSITORIES_DIR, run_case

RUNS_DIR = ROOT / "benchmark-runs"


def select_cases(manifest: dict, ids: list[str] | None = None, limit: int | None = None) -> list[dict]:
    known = {c["id"] for c in manifest["cases"]}
    if ids and (set(ids) - known or len(set(ids)) != len(ids)):
        raise ValueError("Unknown or repeated case id")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    cases = [c for c in manifest["cases"] if not ids or c["id"] in ids]
    return cases[:limit] if limit else cases


def configuration(provider: str, source_run: Path | None) -> dict:
    # Record only non-secret settings. Never serialize the environment wholesale.
    names = ("LLM_MODEL", "OPENAI_MODEL", "KIMI_MODEL", "LLM_REASONING_EFFORT",
             "KIMI_THINKING", "LLM_MAX_COMPLETION_TOKENS", "LLM_DISCOVER_MAX_COMPLETION_TOKENS",
             "LLM_TIMEOUT", "LLM_MAX_RETRIES", "LLM_TRANSIENT_RETRIES", "LLM_RETRY_BACKOFF_SECONDS",
             "JUDGE_MODEL", "JUDGE_REASONING_EFFORT", "JUDGE_MAX_COMPLETION_TOKENS",
             "JUDGE_TIMEOUT", "JUDGE_MAX_RETRIES", "JUDGE_CONCURRENCY")
    code = {}
    for directory in (ROOT.parent / "src", ROOT):
        for path in sorted(directory.glob("*.py")):
            code[str(path.relative_to(ROOT.parent))] = hashlib.sha256(path.read_bytes()).hexdigest()
    code["vendor"] = hashlib.sha256((ROOT / "vendor" / "martian.py").read_bytes()).hexdigest()
    defaults = {"LLM_MODEL": "", "OPENAI_MODEL": "gpt-5.6", "KIMI_MODEL": "kimi-k3",
                "KIMI_THINKING": "disabled", "LLM_MAX_COMPLETION_TOKENS": "2048",
                "LLM_DISCOVER_MAX_COMPLETION_TOKENS": "4096", "LLM_TIMEOUT": "120",
                "LLM_MAX_RETRIES": "0", "LLM_TRANSIENT_RETRIES": "2", "LLM_RETRY_BACKOFF_SECONDS": "1",
                "JUDGE_MODEL": "gpt-6.1-sol", "JUDGE_REASONING_EFFORT": "medium", "JUDGE_MAX_COMPLETION_TOKENS": "4096",
                "JUDGE_TIMEOUT": "60", "JUDGE_MAX_RETRIES": "2", "JUDGE_CONCURRENCY": "5"}
    model = os.environ.get("LLM_MODEL") or os.environ.get("KIMI_MODEL" if provider == "kimi" else "OPENAI_MODEL", defaults["KIMI_MODEL" if provider == "kimi" else "OPENAI_MODEL"])
    defaults["LLM_REASONING_EFFORT"] = "medium" if model.startswith("gpt-5.6") else "low"
    return {"provider": provider, "review_model": model if source_run is None else "historical-see-source-run",
            "settings": {n: os.environ.get(n, defaults[n]) for n in names}, "code_sha256": digest(code),
            "source_run": str(source_run.resolve()) if source_run else None,
            "endpoint_config_sha256": digest({k: os.environ.get(k) for k in ("OPENAI_BASE_URL", "KIMI_BASE_URL", "LLM_BASE_URL")})}


@contextmanager
def run_lock(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This benchmark run is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def report(data_dir: Path, run_dir: Path) -> dict:
    catalog = load_catalog(data_dir)
    run_manifest = read_json(run_dir / "run.json")
    selected = set(run_manifest["case_ids"])
    rows, evaluations, groups = [], [], {}
    stage_counts = Counter()
    for case in catalog["cases"]:
        case_dir = run_dir / case["id"]
        row = {"case_id": case["id"], "project": case["project"], "split": case["split"],
               "status": "pending" if case["id"] in selected else "not_selected"}
        path = case_dir / "benchmark-evaluation.json"
        if case["id"] in selected and path.exists():
            evaluation = read_json(path)
            result = read_json(case_dir / "result.json")
            if evaluation["identity"]["candidates"] != digest(candidate_texts(result)) or evaluation["identity"]["golden"] != digest(read_json(data_dir / "golden" / f"{case['id']}.json")):
                raise ValueError(f"Report inputs changed: {case['id']}")
            if evaluation.get("errors_count", 0):
                raise ValueError(f"Incomplete judge artifact: {case['id']}")
            evaluations.append(evaluation)
            groups.setdefault(case["project"], []).append(evaluation)
            row.update(status="scored", profiles=evaluation["profiles"],
                       false_negatives=evaluation["false_negatives"], false_positives=evaluation["false_positives"],
                       true_positives=evaluation["true_positives"])
            for event in result.get("trace", []):
                if event.get("type") == "candidate_result":
                    stage_counts[event.get("verdict", "unknown")] += 1
            row["verification_failures"] = [
                e for e in result.get("trace", [])
                if e.get("type") == "candidate_result" and e.get("failure_kind")
            ]
            responses = [e for e in result.get("trace", []) if e.get("type") == "model_response"]
            row["review_model_calls"] = len(responses)
            row["review_total_tokens"] = sum((e.get("usage") or {}).get("total_tokens", 0) or 0 for e in responses)
        elif case["id"] in selected and (case_dir / "status.json").exists():
            row.update(read_json(case_dir / "status.json"))
        rows.append(row)
    summary = {"benchmark_revision": catalog["benchmark_revision"], "scorer": SCORER_VERSION,
               "total_cases": len(rows), "selected_cases": len(selected), "scored_cases": len(evaluations),
               "complete": len(evaluations) == len(rows), "selection_complete": len(evaluations) == len(selected),
               "score_scope": "full_benchmark" if len(evaluations) == len(rows) else "completed_subset_only",
               "profiles": profile_scores(evaluations), "by_project": {k: profile_scores(v) for k, v in groups.items()},
               "verification_verdicts": dict(stage_counts),
               "cases_with_verification_failures": sum(bool(r.get("verification_failures")) for r in rows),
               "cases": rows,
               "configuration": run_manifest["configuration"]}
    write_json(run_dir / "summary.json", summary)
    lines = ["# Local benchmark evaluation", "", f"Scored **{len(evaluations)}/{len(rows)}** PRs; selected {len(selected)}.",
             "Full benchmark complete." if summary["complete"] else "**Partial results: these scores describe completed cases only.**", "",
             "Pinned upstream scoring; structured issue input and local OpenAI judge. Not an official leaderboard run.", "",
             "| Profile | TP | FP | FN | Precision | Recall | F1 | F2 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in summary["profiles"].items():
        lines.append(f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%} | {m['fbeta']:.1%} |")
    lines += ["", "## Cases (Core profile)", "", "| Case | Status | Precision | Recall | F1 |", "|---|---|---:|---:|---:|"]
    for row in rows:
        m = row.get("profiles", {}).get("core")
        metrics = f"{m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%}" if m else "— | — | —"
        lines.append(f"| {row['case_id']} | {row['status']} | {metrics} |")
    lines += ["", "## Diagnostics", "", "Unmatched comments are benchmark FPs, not proof of an incorrect review. Manually audit them before tuning. Stage counts do not establish why a particular golden was missed."]
    lines += ["", f"Scored cases with candidate verification failures: **{summary['cases_with_verification_failures']}**. "
              "These candidates produced no issue; all golden comments remain in the scoring denominator."]
    for row in rows:
        if row.get("error"):
            lines += ["", f"### {row['case_id']}", "", row["error"]]
        elif row["status"] == "scored":
            lines += ["", f"### {row['case_id']}"]
            for failure in row.get("verification_failures", []):
                lines += ["", f"Verification failure for candidate {failure['candidate_index']}: "
                          f"{failure['failure_kind']}; {failure.get('last_validation_error')}"]
            lines += ["", "Missed golden issues:"]
            lines += [f"- [{x.get('category')}/{x.get('severity')}] {x['golden_comment']}" for x in row["false_negatives"]] or ["- None"]
            lines += ["", "Unmatched agent comments:"]
            lines += [f"- {x['candidate']}" for x in row["false_positives"]] or ["- None"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


async def run_benchmark(data_dir: Path, run_dir: Path, cases: list[dict], provider: str,
                        source_run: Path | None = None, judge_factory=BenchmarkJudge,
                        review_runner=run_case, fixture_preparer=prepare_fixture) -> dict:
    catalog = load_catalog(data_dir)
    identity = {"dataset_sha256": digest(catalog), "scorer": SCORER_VERSION,
                "case_ids": [c["id"] for c in cases], "configuration": configuration(provider, source_run)}
    with run_lock(run_dir):
        manifest_path = run_dir / "run.json"
        if manifest_path.exists():
            previous = read_json(manifest_path)
            if any(previous[k] != v for k, v in identity.items()):
                raise ValueError("Run configuration/code/dataset changed; use a new run name")
        else:
            write_json(manifest_path, {**identity, "created_at": datetime.now(timezone.utc).isoformat()})
        judge = None
        try:
            for index, case in enumerate(cases, 1):
                case_dir = run_dir / case["id"]
                started = time.monotonic()
                stage = "review"
                print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
                try:
                    result_path = case_dir / "result.json"
                    if source_run:
                        source = source_run / case["id"] / "result.json"
                        source_result = read_json(source)
                        candidate_texts(source_result)
                        if result_path.exists() and digest(read_json(result_path)) != digest(source_result):
                            raise ValueError("Imported result changed; choose a new run name")
                        if not result_path.exists():
                            write_json(result_path, source_result)
                    else:
                        fixture = fixture_preparer(case, data_dir)
                        inputs = {"metadata": read_json(fixture / "metadata.json"), "changes_sha256": digest(read_json(fixture / "changes.json"))}
                        lock_path = case_dir / "input.json"
                        if lock_path.exists() and read_json(lock_path) != inputs:
                            raise ValueError("Locked fixture changed; choose a new run name")
                        write_json(lock_path, inputs)
                        if not result_path.exists():
                            review_runner(fixture, run_dir, repository_cache=REPOSITORIES_DIR, provider=provider)
                    stage = "judge"
                    result = read_json(result_path)
                    golden = read_json(data_dir / "golden" / f"{case['id']}.json")
                    if judge is None:
                        judge = judge_factory()
                    print(f"  scoring {len(result['issues'])} issues × {len(golden['comments'])} golden (cached comparisons reused)", flush=True)
                    await score_case(result, golden, case_dir, judge)
                    write_json(case_dir / "status.json", {"status": "scored", "last_attempt_seconds": round(time.monotonic() - started, 2)})
                except Exception as error:
                    write_json(case_dir / "status.json", {"status": f"{stage}_failed", "error": str(error), "last_attempt_seconds": round(time.monotonic() - started, 2)})
                    print(f"  {stage} failed: {error}", flush=True)
                report(data_dir, run_dir)
        finally:
            if judge is not None and hasattr(judge, "client"):
                await judge.client.close()
            summary = report(data_dir, run_dir)
        return summary


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report"))
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--runs", type=Path, default=RUNS_DIR)
    parser.add_argument("--provider", choices=("kimi", "openai"), default="kimi")
    parser.add_argument("--run-name", default="baseline-v1")
    parser.add_argument("--case-id", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fixtures", action="store_true", help="Also fetch selected PR inputs during prepare (no LLM calls)")
    parser.add_argument("--source-run", type=Path, help="Score existing results without rerunning review; historical configuration is not inferred")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_name):
        parser.error("Invalid run name")
    try:
        if args.command == "prepare":
            manifest = prepare_catalog(args.data)
            cases = select_cases(manifest, args.case_id, args.limit)
            failures = []
            if args.fixtures:
                for case in cases:
                    try:
                        prepare_fixture(case, args.data)
                        print(f"ready {case['id']}", flush=True)
                    except Exception as error:
                        failures.append({"case_id": case["id"], "error": str(error)})
                write_json(args.data / "fixture-errors.json", failures)
            print(f"Dataset ready: {len(manifest['cases'])} PRs, 173 golden comments. Fixture failures: {len(failures)}")
            if failures:
                raise ValueError("Some fixtures failed; rerun prepare to resume")
            return
        run_dir = args.runs / args.provider / args.run_name
        if args.command == "report":
            summary = report(args.data, run_dir)
        else:
            manifest = load_catalog(args.data)
            cases = select_cases(manifest, args.case_id, args.limit)
            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("OPENAI_API_KEY is required for the judge")
            if not args.source_run and not os.environ.get("MOONSHOT_API_KEY" if args.provider == "kimi" else "OPENAI_API_KEY"):
                raise ValueError("Review provider API key is missing")
            summary = asyncio.run(run_benchmark(args.data, run_dir, cases, args.provider, args.source_run))
        print(f"Scored {summary['scored_cases']}/{summary['total_cases']} PRs; report: {run_dir / 'report.md'}")
        if not summary["selection_complete"]:
            raise SystemExit(1)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
