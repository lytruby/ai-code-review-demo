from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
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


def load_inputs(
    case_id: str,
    provider: str,
    runs_dir: Path = RUNS_DIR,
    golden_dir: Path = GOLDEN_DIR,
) -> tuple[list[dict], list[dict]]:
    result_path = runs_dir / provider.lower() / case_id / "result.json"
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
    golden_matches: list[dict | None] = [None] * len(golden_comments)
    candidate_matched = [False] * len(candidates)
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

        if not result.get("match"):
            continue

        confidence = float(result.get("confidence", 0.0))
        current = golden_matches[golden_index]
        if current is None or confidence > current["confidence"]:
            golden_matches[golden_index] = {
                "golden_index": golden_index,
                "candidate_index": candidate_index,
                "confidence": confidence,
                "reasoning": result.get("reasoning", ""),
            }
        candidate_matched[candidate_index] = True

    true_positives = []
    false_negatives = []
    for golden_index, golden in enumerate(golden_comments):
        match = golden_matches[golden_index]
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

    false_positives = [
        {
            "candidate_index": index,
            "candidate": candidate["description"],
        }
        for index, candidate in enumerate(candidates)
        if not candidate_matched[index]
    ]

    tp = len(true_positives)
    fp = len(false_positives)
    fn = len(false_negatives)
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
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    provider = args.provider.lower()
    case_dir = RUNS_DIR / provider / args.case_id
    output_path = case_dir / "evaluation.json"
    if output_path.exists() and not args.force:
        parser.error(f"Evaluation already exists: {output_path}; use --force")

    candidates, golden_comments = load_inputs(args.case_id, provider)
    judge = OpenAIJudge()
    comparisons = len(candidates) * len(golden_comments)
    print(
        f"judge {args.case_id}: {len(candidates)} candidates × "
        f"{len(golden_comments)} golden = {comparisons} comparisons"
    )
    evaluation = await evaluate(judge, candidates, golden_comments)
    evaluation["case_id"] = args.case_id
    evaluation["provider"] = provider
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
