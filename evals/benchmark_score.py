"""Cached LLM judgments with upstream matching and profile aggregation."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path

from openai import AsyncOpenAI

from evals.benchmark_data import digest, read_json, write_json
from evals.vendor import martian

SCORER_VERSION = "martian-e616e849-v2"


def candidate_texts(result: dict) -> list[str]:
    if result.get("status") != "complete" or not isinstance(result.get("issues"), list):
        raise ValueError("Only complete, structured review results can be scored")
    texts = []
    for issue in result["issues"]:
        if not isinstance(issue.get("description"), str) or not issue["description"].strip():
            raise ValueError("Review issue has no description")
        text = issue["description"]
        if issue.get("suggestion"):
            text += "\nSuggestion: " + issue["suggestion"]
        texts.append(text)
    return texts


class BenchmarkJudge:
    def __init__(self):
        self.model = os.environ.get("JUDGE_MODEL", "gpt-6.1-sol")
        self.reasoning_effort = os.environ.get("JUDGE_REASONING_EFFORT", "medium")
        self.max_completion_tokens = int(os.environ.get("JUDGE_MAX_COMPLETION_TOKENS", "4096"))
        if self.model.startswith("gpt-6") and self.reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("GPT-6 judge reasoning effort must be low, medium, high, xhigh, or max")
        if self.max_completion_tokens <= 0:
            raise ValueError("JUDGE_MAX_COMPLETION_TOKENS must be positive")
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                                 timeout=float(os.environ.get("JUDGE_TIMEOUT", "60")),
                                 max_retries=int(os.environ.get("JUDGE_MAX_RETRIES", "2")))
        self.semaphore = asyncio.Semaphore(int(os.environ.get("JUDGE_CONCURRENCY", "5")))

    async def call(self, system: str, prompt: str) -> dict:
        request = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.max_completion_tokens,
        }
        if self.model.startswith("gpt-6"):
            request["reasoning_effort"] = self.reasoning_effort
        else:
            # Preserve the historical sampling settings for the old judge.
            request["temperature"] = 0
        async with self.semaphore:
            response = await self.client.chat.completions.create(**request)
        if response.choices[0].finish_reason != "stop":
            raise ValueError(f"Judge did not finish: {response.choices[0].finish_reason}")
        import json
        return json.loads(response.choices[0].message.content or "")

    async def match_comment(self, golden: str, candidate: str) -> dict:
        result = await self.call(
            "You are a precise code review evaluator. Always respond with valid JSON.",
            martian.JUDGE_PROMPT.format(golden_comment=golden, candidate=candidate),
        )
        confidence = result.get("confidence")
        if type(result.get("match")) is not bool or type(confidence) not in {float, int} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid judge match/confidence")
        return result

    async def deduplicate(self, candidates: list[str]) -> list[list[int]]:
        import json
        result = await self.call(
            "You group duplicate code review comments. Always respond with valid JSON only.",
            martian.STRICT_PROMPT.format(candidates="\n".join(f"{i}. {text}" for i, text in enumerate(candidates))),
        )
        groups = martian._parse_groups_response(json.dumps(result), len(candidates))
        if groups is None or any(not group or any(type(i) is not int for i in group) for group in groups):
            raise ValueError("Invalid dedup partition; scoring is incomplete")
        return groups


async def score_case(result: dict, golden: dict, case_dir: Path, judge) -> dict:
    candidates = candidate_texts(result)
    identity = {"scorer": SCORER_VERSION, "judge_model": judge.model,
                "judge_reasoning_effort": getattr(judge, "reasoning_effort", None),
                "judge_max_completion_tokens": getattr(judge, "max_completion_tokens", None),
                "candidates": digest(candidates), "golden": digest(golden)}
    output = case_dir / "benchmark-evaluation.json"
    if output.exists():
        saved = read_json(output)
        if saved.get("identity") != identity:
            raise ValueError("Scoring inputs changed; use a new run name")
        return saved
    cache_path = case_dir / "judgments.json"
    cache = read_json(cache_path) if cache_path.exists() else {"identity": identity, "pairs": {}}
    if cache["identity"] != identity:
        raise ValueError("Judge/cache settings changed; use a new run name")
    if "groups" not in cache:
        cache["groups"] = await judge.deduplicate(candidates) if len(candidates) > 1 else [[i] for i in range(len(candidates))]
        write_json(cache_path, cache)

    class CachedJudge:
        async def match_comment(self, golden_text, candidate):
            key = digest([golden_text, candidate])
            if key not in cache["pairs"]:
                answer = await judge.match_comment(golden_text, candidate)
                if answer.get("error"):
                    raise ValueError(answer["error"])
                cache["pairs"][key] = {"golden": golden_text, "candidate": candidate, **answer}
                write_json(cache_path, cache)
            return cache["pairs"][key]

    evaluation = await martian.evaluate_review(CachedJudge(), golden["comments"], candidates, cache["groups"])
    if evaluation.get("errors_count", 0) or evaluation.get("skipped"):
        write_json(case_dir / "judge-errors.json", evaluation)
        raise ValueError(f"Judge incomplete: {evaluation.get('errors_count', 0)} failed comparisons; rerun to retry")
    evaluation["identity"] = identity
    evaluation["dedup_groups"] = cache["groups"]
    evaluation["profiles"] = profile_scores([evaluation])
    write_json(output, evaluation)
    return evaluation


def profile_scores(evaluations: list[dict]) -> dict:
    upstream = {str(i): {"agent": e} for i, e in enumerate(evaluations)}
    return {profile: martian.score_tools(upstream, {}, profile, 2.0).get("agent", {
        "tp": 0, "fp": 0, "fn": 0, "prs": 0,
        "precision": 0, "recall": 0, "f1": 0, "fbeta": 0,
    }) for profile in martian.PROFILE_CATEGORIES}
