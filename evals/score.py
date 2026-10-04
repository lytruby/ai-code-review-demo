from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
from typing import Protocol

from dotenv import load_dotenv
from openai import AsyncOpenAI


EVALS_DIR = Path(__file__).parent
RUNS_DIR = EVALS_DIR / "runs"
GOLDEN_DIR = EVALS_DIR / "golden" / "dev"

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

Return a brief explanation, a boolean match, and confidence from 0.0 to 1.0.
"""

MATCH_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "match_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "match": {"type": "boolean"},
                "confidence": {"type": "number"},
            },
            "required": ["reasoning", "match", "confidence"],
            "additionalProperties": False,
        },
    },
}


class Judge(Protocol):
    async def match(self, golden_comment: str, candidate: str) -> dict: ...


class OpenAIJudge:
    def __init__(self) -> None:
        load_dotenv()
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")

        self.model = os.environ.get("JUDGE_MODEL", "gpt-5.2")
        self.client = AsyncOpenAI(
            api_key=api_key,
            timeout=float(os.environ.get("JUDGE_TIMEOUT", "30")),
            max_retries=int(os.environ.get("JUDGE_MAX_RETRIES", "2")),
        )
        self.semaphore = asyncio.Semaphore(
            int(os.environ.get("JUDGE_CONCURRENCY", "10"))
        )

    async def match(self, golden_comment: str, candidate: str) -> dict:
        prompt = JUDGE_PROMPT.format(
            golden_comment=golden_comment,
            candidate=candidate,
        )
        async with self.semaphore:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise code review evaluator. "
                            "Always follow the response schema."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=MATCH_SCHEMA,
            )
        content = response.choices[0].message.content or ""
        return json.loads(content)


def _select_one_to_one_matches(
    matches: list[dict],
    golden_count: int,
) -> list[dict]:
    """Maximize match count first, then total judge confidence."""
    edges_by_golden: list[list[dict]] = [[] for _ in range(golden_count)]
    for match in matches:
        edges_by_golden[match["golden_index"]].append(match)
    for edges in edges_by_golden:
        edges.sort(key=lambda edge: (-edge["confidence"], edge["candidate_index"]))

    cache: dict[tuple[int, int], tuple[int, float, tuple[dict, ...]]] = {}

    def solve(
        golden_index: int,
        used_candidates: int,
    ) -> tuple[int, float, tuple[dict, ...]]:
        key = (golden_index, used_candidates)
        if key in cache:
            return cache[key]
        if golden_index == golden_count:
            return 0, 0.0, ()

        best = solve(golden_index + 1, used_candidates)
        for edge in edges_by_golden[golden_index]:
            candidate_bit = 1 << edge["candidate_index"]
            if used_candidates & candidate_bit:
                continue
            count, confidence, selected = solve(
                golden_index + 1,
                used_candidates | candidate_bit,
            )
            option = (
                count + 1,
                confidence + edge["confidence"],
                (edge, *selected),
            )
            if option[:2] > best[:2]:
                best = option

        cache[key] = best
        return best

    return list(solve(0, 0)[2])


def load_inputs(
    case_id: str,
    provider: str,
    run_name: str,
    runs_dir: Path = RUNS_DIR,
    golden_dir: Path = GOLDEN_DIR,
) -> tuple[list[dict], list[dict]]:
    result_path = runs_dir / provider.lower() / run_name / case_id / "result.json"
    golden_path = golden_dir / f"{case_id}.json"

    if not result_path.is_file():
        raise FileNotFoundError(f"Review result not found: {result_path}")
    if not golden_path.is_file():
        raise FileNotFoundError(f"Golden comments not found: {golden_path}")

    result = json.loads(result_path.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    candidates = result.get("issues")
    golden_comments = golden.get("comments")

    if not isinstance(candidates, list):
        raise ValueError("Review result issues must be a list")
    if not isinstance(golden_comments, list):
        raise ValueError("Golden comments must be a list")
    return candidates, golden_comments


async def evaluate(
    judge: Judge,
    candidates: list[dict],
    golden_comments: list[dict],
) -> dict:
    tasks = []
    task_metadata = []

    for golden_index, golden in enumerate(golden_comments):
        for candidate_index, candidate in enumerate(candidates):
            tasks.append(judge.match(golden["comment"], candidate["description"]))
            task_metadata.append((golden_index, candidate_index))

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    pairwise_judgments = []
    possible_matches = []
    errors = []

    for metadata, result in zip(task_metadata, raw_results, strict=True):
        golden_index, candidate_index = metadata
        if isinstance(result, Exception):
            errors.append(
                {
                    "golden_index": golden_index,
                    "candidate_index": candidate_index,
                    "error": str(result),
                }
            )
            continue

        judgment = {
            "golden_index": golden_index,
            "candidate_index": candidate_index,
            "match": result.get("match") is True,
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.0)))),
            "reasoning": result.get("reasoning", ""),
        }
        pairwise_judgments.append(judgment)
        if judgment["match"]:
            possible_matches.append(judgment)

    selected_matches = _select_one_to_one_matches(
        possible_matches,
        golden_count=len(golden_comments),
    )
    selected_by_golden = {
        match["golden_index"]: match for match in selected_matches
    }
    selected_candidate_indices = {
        match["candidate_index"] for match in selected_matches
    }

    true_positives = []
    false_negatives = []
    for golden_index, golden in enumerate(golden_comments):
        match = selected_by_golden.get(golden_index)
        if match is None:
            false_negatives.append(
                {
                    "golden_index": golden_index,
                    "golden_comment": golden["comment"],
                    "severity": golden.get("severity"),
                }
            )
            continue

        candidate_index = match["candidate_index"]
        true_positives.append(
            {
                **match,
                "golden_comment": golden["comment"],
                "severity": golden.get("severity"),
                "matched_candidate": candidates[candidate_index]["description"],
            }
        )

    false_positives = []
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index in selected_candidate_indices:
            continue
        duplicate_edges = [
            edge
            for edge in possible_matches
            if edge["candidate_index"] == candidate_index
            and edge["golden_index"] in selected_by_golden
        ]
        false_positive = {
            "candidate_index": candidate_index,
            "candidate": candidate["description"],
            "reason": "unmatched",
        }
        if duplicate_edges:
            competing_edge = max(
                duplicate_edges,
                key=lambda edge: (edge["confidence"], -edge["golden_index"]),
            )
            selected = selected_by_golden[competing_edge["golden_index"]]
            false_positive.update(
                {
                    "reason": "duplicate_match",
                    "competing_golden_index": competing_edge["golden_index"],
                    "selected_candidate_index": selected["candidate_index"],
                    "match_confidence": competing_edge["confidence"],
                }
            )
        false_positives.append(false_positive)

    tp = len(true_positives)
    fp = len(false_positives)
    fn = len(false_negatives)
    if tp + fp != len(candidates):
        raise RuntimeError("Scorer invariant failed: tp + fp != total_candidates")
    if tp + fn != len(golden_comments):
        raise RuntimeError("Scorer invariant failed: tp + fn != total_golden")
    precision = tp / len(candidates) if candidates else 0.0
    recall = tp / len(golden_comments) if golden_comments else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )

    return {
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "pairwise_judgments": pairwise_judgments,
        "errors": errors,
        "total_candidates": len(candidates),
        "total_golden": len(golden_comments),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "errors_count": len(errors),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Score one agent review with an OpenAI LLM judge."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.provider):
        parser.error("Provider may contain only letters, numbers, '-' and '_'")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_name):
        parser.error("Run name may contain only letters, numbers, '-' and '_'")

    provider = args.provider.lower()
    case_dir = RUNS_DIR / provider / args.run_name / args.case_id
    output_path = case_dir / "evaluation.json"
    if output_path.exists() and not args.force:
        parser.error(f"Evaluation already exists: {output_path}; use --force")

    candidates, golden_comments = load_inputs(
        args.case_id,
        provider,
        args.run_name,
    )
    judge = OpenAIJudge()
    comparisons = len(candidates) * len(golden_comments)
    print(
        f"judge {args.case_id}: {len(candidates)} candidates × "
        f"{len(golden_comments)} golden = {comparisons} comparisons"
    )
    evaluation = await evaluate(judge, candidates, golden_comments)
    evaluation["case_id"] = args.case_id
    evaluation["provider"] = provider
    evaluation["run_name"] = args.run_name
    evaluation["judge_model"] = judge.model

    case_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(evaluation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"precision={evaluation['precision']:.1%} "
        f"recall={evaluation['recall']:.1%} "
        f"f1={evaluation['f1']:.1%}"
    )
    print(f"saved {output_path}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
