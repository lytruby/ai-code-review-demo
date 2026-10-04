"""Small live judge checks; not part of the offline unit suite."""
import asyncio
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.benchmark_score import BenchmarkJudge


async def main():
    load_dotenv(ROOT / ".env")
    judge = BenchmarkJudge()
    print(f"Judge: {judge.model}; reasoning={judge.reasoning_effort}", flush=True)
    try:
        same, different, groups = await asyncio.gather(
            judge.match_comment("Dividing by zero raises an exception when count is zero.",
                                "The code divides by count without checking for zero, so count=0 crashes."),
            judge.match_comment("Dividing by zero raises an exception when count is zero.",
                                "The log message has a spelling mistake."),
            judge.deduplicate(["Division by count crashes when count is zero.",
                               "A zero count causes division by zero.",
                               "The log message contains a typo."]),
        )
        assert same["match"] is True, same
        assert different["match"] is False, different
        assert {frozenset(g) for g in groups} == {frozenset({0, 1}), frozenset({2})}, groups
        print("PASS: matching, nonmatching, and semantic deduplication", flush=True)
    finally:
        await judge.client.close()


if __name__ == "__main__":
    asyncio.run(main())
