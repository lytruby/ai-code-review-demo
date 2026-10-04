"""Verbatim scoring functions from withmartian/code-review-benchmark.

Revision: e616e849755441da38f18bf3adba2c9583b03803
See LICENSE and README.md in this directory. Transport/CLI code is omitted.
"""


import asyncio
import json
from typing import Any

LLMJudge = Any
BATCH_SIZE = 20


JUDGE_PROMPT = """You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}}"""


async def process_batch(tasks: list, batch_size: int = BATCH_SIZE) -> list:
    results = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        if i + batch_size < len(tasks):
            await asyncio.sleep(0.5)
    return results


def _build_sibling_map(
    candidates: list[str],
    groups: list[list[int]] | None,
) -> dict[str, set[str]]:
    """
    Build a mapping from each candidate text to its duplicate siblings.
    If no groups are provided (no dedup), every candidate maps to an empty set.
    """
    if not groups:
        return {}
    sibling_map: dict[str, set[str]] = {}
    for group in groups:
        group_texts = {candidates[i] for i in group if i < len(candidates)}
        for i in group:
            if i < len(candidates):
                sibling_map[candidates[i]] = group_texts - {candidates[i]}
    return sibling_map


async def evaluate_review(
    judge: LLMJudge,
    golden_comments: list[dict],
    candidates: list[str],
    dedup_groups: list[list[int]] | None = None,
) -> dict:
    """Evaluate candidates against golden comments. Returns precision and recall metrics."""

    if not golden_comments:
        return {
            "skipped": True,
            "reason": "No golden comments",
        }

    if not candidates:
        return {
            "skipped": False,
            "true_positives": [],
            "false_positives": [],
            "false_negatives": [
                {"golden_comment": gc["comment"], "severity": gc.get("severity"), "category": gc.get("category")}
                for gc in golden_comments
            ],
            "errors": [],
            "total_candidates": 0,
            "total_golden": len(golden_comments),
            "tp": 0,
            "fp": 0,
            "fn": len(golden_comments),
            "errors_count": 0,
            "precision": 0.0,
            "recall": 0.0,
        }

    # Create matching tasks: each golden comment vs each candidate
    tasks = []
    task_meta = []

    for gc in golden_comments:
        for candidate in candidates:
            tasks.append(judge.match_comment(gc["comment"], candidate))
            task_meta.append(
                {
                    "golden": gc["comment"],
                    "golden_severity": gc.get("severity"),
                    "candidate": candidate,
                }
            )

    # Process all comparisons
    results = await process_batch(tasks)

    # Build match matrix
    # Initialize all golden comments as unmatched
    golden_matched = {
        gc["comment"]: {
            "severity": gc.get("severity"),
            "category": gc.get("category"),
            "matched": False,
            "best_confidence": 0.0,
            "matched_candidate": None,
        }
        for gc in golden_comments
    }
    candidate_matched = dict.fromkeys(candidates, False)
    # Pre-build sibling lookup so matched candidates propagate to duplicates
    sibling_map = _build_sibling_map(candidates, dedup_groups)
    errors = []

    for i, result in enumerate(results):
        meta = task_meta[i]
        golden = meta["golden"]
        candidate = meta["candidate"]

        if isinstance(result, Exception):
            errors.append({"golden": golden, "candidate": candidate, "error": str(result)})
            continue
        if result.get("error"):
            errors.append({"golden": golden, "candidate": candidate, "error": result["error"]})
            continue

        if result.get("match") and result.get("confidence", 0) > golden_matched[golden]["best_confidence"]:
            golden_matched[golden]["matched"] = True
            golden_matched[golden]["best_confidence"] = result["confidence"]
            golden_matched[golden]["matched_candidate"] = candidate
            golden_matched[golden]["reasoning"] = result.get("reasoning")
            candidate_matched[candidate] = True
            # Propagate to duplicate siblings so they aren't counted as FPs
            for sibling in sibling_map.get(candidate, set()):
                candidate_matched[sibling] = True

    # Calculate metrics
    true_positives = []
    false_negatives = []

    for golden, info in golden_matched.items():
        if info["matched"]:
            true_positives.append(
                {
                    "golden_comment": golden,
                    "severity": info["severity"],
                    "category": info["category"],
                    "matched_candidate": info["matched_candidate"],
                    "confidence": info["best_confidence"],
                    "reasoning": info.get("reasoning"),
                }
            )
        else:
            false_negatives.append(
                {
                    "golden_comment": golden,
                    "severity": info["severity"],
                    "category": info["category"],
                }
            )

    # False positives: candidates that didn't match any golden
    false_positives = [{"candidate": c} for c, matched in candidate_matched.items() if not matched]

    total_candidates = len(candidates)
    total_golden = len(golden_comments)
    tp_count = len(true_positives)

    precision = tp_count / total_candidates if total_candidates > 0 else 0.0
    recall = tp_count / total_golden if total_golden > 0 else 0.0

    return {
        "skipped": False,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "errors": errors,
        "total_candidates": total_candidates,
        "total_golden": total_golden,
        "tp": tp_count,
        "fp": len(false_positives),
        "fn": len(false_negatives),
        "errors_count": len(errors),
        "precision": precision,
        "recall": recall,
    }


STRICT_PROMPT = """You are identifying duplicate code review comments.

Below is a numbered list of issues extracted from an AI tool's code review.
Some tools post the same issue in both a summary comment and an inline comment,
creating near-identical duplicates. Your job is to find those duplicates.

Two candidates are duplicates ONLY IF:
- They describe the same problem AND
- A single code change would fix both (i.e., they would be one bug report)

Two candidates are NOT duplicates if:
- They describe the same TYPE of bug but in different files, functions, or
  classes (e.g., "negative slicing in OptimizedCursorPaginator" vs "negative
  slicing in BasePaginator" are separate issues — fixing one does not fix
  the other)
- They describe related but distinct problems (e.g., "returns wrong type" vs
  "caller crashes because of wrong type" are separate issues)

When in doubt, keep candidates separate — it is better to leave a duplicate
ungrouped than to incorrectly merge two distinct issues.

Candidates:
{candidates}

Return ONLY a JSON object where each group is a list of 0-based indices.
Singletons (no duplicate) must still appear as single-element groups.

Example for 4 candidates where 0 and 2 are duplicates:
{{"groups": [[0, 2], [1], [3]]}}

Your response:"""


def _parse_groups_response(content: str, n_candidates: int) -> list[list[int]] | None:
    """
    Parse and validate the LLM grouping response.
    Returns None if the response is invalid.
    """
    # Strip markdown code fences if present
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else content
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if "groups" not in data or not isinstance(data["groups"], list):
        return None

    groups = data["groups"]

    # Validate: every index 0..n-1 must appear exactly once
    seen: set[int] = set()
    for group in groups:
        if not isinstance(group, list):
            return None
        for idx in group:
            if not isinstance(idx, int):
                return None
            if idx < 0 or idx >= n_candidates:
                return None
            if idx in seen:
                return None
            seen.add(idx)

    if seen != set(range(n_candidates)):
        return None

    return groups


PROFILE_CATEGORIES: dict[str, frozenset[str]] = {
    "strict": frozenset({"bug", "security", "concurrency", "data", "api"}),
    "core": frozenset({"bug", "security", "concurrency", "data", "api", "perf", "test_gap", "doc_defect"}),
    "all": frozenset({
        "bug", "security", "concurrency", "data", "api",
        "perf", "test_gap", "doc_defect", "style", "speculative",
    }),
}


_HIDDEN_TOOLS: frozenset[str] = frozenset({
    "qodo", "greptile", "linearb", "bito", "sentry", "vercel", "kodus",
    "cubic-dev", "cubic-v3", "greptile-v4", "mergemonkey", "qodo-v22",
    "qodo-v2-2", "qodo-extended-summary", "qodo-extended", "entelligence",
    "mesa", "codeant", "propel", "propel-v2", "gemini", "gitar", "deepsource",
})


_HIDDEN_PREFIXES: tuple[str, ...] = ("mra-",)


def _is_hidden(tool: str) -> bool:
    return tool in _HIDDEN_TOOLS or any(tool.startswith(p) for p in _HIDDEN_PREFIXES)


def _get_category(entry: dict, golden_categories: dict[str, str]) -> str:
    return entry.get("category") or golden_categories.get(entry.get("golden_comment", ""), "")


def _fbeta(precision: float, recall: float, beta: float) -> float:
    beta2 = beta * beta
    denom = beta2 * precision + recall
    return (1 + beta2) * precision * recall / denom if denom > 0 else 0.0


def score_tools(
    evaluations: dict,
    golden_categories: dict[str, str],
    profile_name: str,
    beta: float,
) -> dict[str, dict]:
    """Compute per-tool aggregate metrics for a given profile and beta."""
    profile_cats = PROFILE_CATEGORIES[profile_name]
    tool_totals: dict[str, dict[str, int]] = {}

    for _pr_url, pr_evals in evaluations.items():
        for tool, tool_eval in pr_evals.items():
            if _is_hidden(tool) or tool_eval.get("skipped"):
                continue

            if tool not in tool_totals:
                tool_totals[tool] = {"tp": 0, "fp": 0, "fn": 0, "prs": 0}

            tps = tool_eval.get("true_positives", [])
            fns = tool_eval.get("false_negatives", [])
            fp = tool_eval.get("fp", 0)

            tp_in = sum(1 for t in tps if _get_category(t, golden_categories) in profile_cats)
            fn_in = sum(1 for f in fns if _get_category(f, golden_categories) in profile_cats)

            tool_totals[tool]["tp"] += tp_in
            tool_totals[tool]["fp"] += fp
            tool_totals[tool]["fn"] += fn_in
            tool_totals[tool]["prs"] += 1

    results: dict[str, dict] = {}
    for tool, t in tool_totals.items():
        precision = t["tp"] / (t["tp"] + t["fp"]) if (t["tp"] + t["fp"]) > 0 else 0
        recall = t["tp"] / (t["tp"] + t["fn"]) if (t["tp"] + t["fn"]) > 0 else 0
        f1 = _fbeta(precision, recall, 1.0)
        fb = _fbeta(precision, recall, beta)

        results[tool] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "fbeta": fb,
            "tp": t["tp"],
            "fp": t["fp"],
            "fn": t["fn"],
            "prs": t["prs"],
        }

    return results
