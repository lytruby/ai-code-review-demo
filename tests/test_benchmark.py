import asyncio
import json

import pytest

from evals.benchmark import report, run_benchmark, select_cases
from evals.benchmark_data import REVISION, digest, load_catalog, prepare_catalog, write_json
from evals.benchmark_score import candidate_texts, profile_scores, score_case
from evals.vendor import martian


class FakeJudge:
    model = "test-judge"

    def __init__(self):
        self.calls = []
        self.fail = set()

    async def deduplicate(self, candidates):
        return [[i] for i in range(len(candidates))]

    async def match_comment(self, golden, candidate):
        self.calls.append((golden, candidate))
        if (golden, candidate) in self.fail:
            raise TimeoutError("temporary judge failure")
        return {"match": golden in candidate, "confidence": 0.9, "reasoning": "test"}


def result(*descriptions):
    return {"status": "complete", "summary": "review", "issues": [
        {"description": d, "suggestion": "", "file": "x.py"} for d in descriptions
    ]}


def catalog(tmp_path, count=2):
    data = tmp_path / "data"
    cases = []
    for i in range(count):
        name = f"case-{i}"
        golden = {"case_id": name, "comments": [{"comment": "bug", "category": "bug", "severity": "High"}]}
        write_json(data / "golden" / f"{name}.json", golden)
        cases.append({"id": name, "project": "demo", "split": "benchmark", "golden_sha256": digest(golden)})
    write_json(data / "manifest.json", {"benchmark_revision": REVISION, "cases": cases})
    return data, cases


def test_score_cache_resumes_only_failed_comparisons(tmp_path):
    judge = FakeJudge()
    judge.fail = {("bug", "noise")}
    golden = {"comments": [{"comment": "bug", "category": "bug"}]}
    with pytest.raises(ValueError, match="incomplete"):
        asyncio.run(score_case(result("bug", "noise"), golden, tmp_path, judge))
    assert not (tmp_path / "benchmark-evaluation.json").exists()
    judge.fail.clear()
    score = asyncio.run(score_case(result("bug", "noise"), golden, tmp_path, judge))
    assert judge.calls.count(("bug", "bug")) == 1
    assert judge.calls.count(("bug", "noise")) == 2
    assert score["profiles"]["core"]["precision"] == 0.5
    asyncio.run(score_case(result("bug", "noise"), golden, tmp_path, judge))
    assert len(judge.calls) == 3
    judge.model = "different"
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(score_case(result("bug", "noise"), golden, tmp_path, judge))


def test_profiles_exclude_style_matches_without_false_positive_penalty(tmp_path):
    golden = {"comments": [{"comment": "bug", "category": "bug"}, {"comment": "style", "category": "style"}]}
    score = asyncio.run(score_case(result("bug", "style", "noise"), golden, tmp_path, FakeJudge()))
    core = score["profiles"]["core"]
    assert (core["tp"], core["fp"], core["fn"]) == (1, 1, 0)
    assert core["precision"] == 0.5
    assert score["profiles"]["all"]["precision"] == pytest.approx(2 / 3)


def test_upstream_matching_and_dedup_are_not_one_to_one(tmp_path):
    class Duplicates(FakeJudge):
        async def deduplicate(self, candidates):
            return [[0, 1], [2]]

        async def match_comment(self, golden, candidate):
            return {"match": candidate == "broad", "confidence": 0.9}

    golden = {"comments": [{"comment": "a", "category": "bug"}, {"comment": "b", "category": "bug"}]}
    score = asyncio.run(score_case(result("broad", "duplicate", "noise"), golden, tmp_path, Duplicates()))
    assert score["tp"] == 2
    assert score["fp"] == 1
    assert score["profiles"]["core"]["precision"] == pytest.approx(2 / 3)


def test_empty_review_is_valid_zero_recall(tmp_path):
    judge = FakeJudge()
    score = asyncio.run(score_case(result(), {"comments": [{"comment": "bug", "category": "bug"}]}, tmp_path, judge))
    assert score["fn"] == 1
    assert score["profiles"]["core"]["recall"] == 0
    assert judge.calls == []


def test_partial_run_resume_and_configuration_guard(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    data, cases = catalog(tmp_path)
    source, run = tmp_path / "source", tmp_path / "run"
    write_json(source / "case-0" / "result.json", result("bug"))
    judge = FakeJudge()
    summary = asyncio.run(run_benchmark(data, run, cases, "kimi", source, judge_factory=lambda: judge))
    assert summary["scored_cases"] == 1
    assert summary["complete"] is False
    assert summary["cases"][1]["status"] == "review_failed"
    assert summary["profiles"]["core"]["recall"] == 1  # Explicitly subset-only.
    write_json(source / "case-1" / "result.json", result())
    summary = asyncio.run(run_benchmark(data, run, cases, "kimi", source, judge_factory=lambda: judge))
    assert summary["complete"] is True
    assert summary["profiles"]["core"]["recall"] == 0.5
    assert len(judge.calls) == 1
    monkeypatch.setenv("JUDGE_MODEL", "changed")
    with pytest.raises(ValueError, match="configuration"):
        asyncio.run(run_benchmark(data, run, cases, "kimi", source, judge_factory=lambda: judge))


def test_catalog_checksum_and_selection(tmp_path):
    data, cases = catalog(tmp_path)
    manifest = load_catalog(data)
    assert select_cases(manifest, ["case-1"])[0]["id"] == "case-1"
    with pytest.raises(ValueError):
        select_cases(manifest, ["missing"])
    write_json(data / "golden" / "case-0.json", {"comments": []})
    with pytest.raises(ValueError, match="changed"):
        load_catalog(data)


def test_catalog_uses_official_copied_url_and_preserves_dev_split(tmp_path):
    counts = iter([4] * 23 + [3] * 27)  # 173 comments.

    def fetch(url):
        project = url.rsplit("/", 1)[-1].removesuffix(".json")
        entries = []
        for i in range(10):
            pr_url = f"https://github.com/demo/{project}/pull/{i + 1}"
            if project == "sentry" and i == 0:
                pr_url = "https://github.com/getsentry/sentry/pull/93824"
            entry = {"url": pr_url, "comments": [{"comment": f"bug {j}", "category": "bug"} for j in range(next(counts))]}
            if project == "discourse" and i == 0:
                entry["url"] = "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/1"
                entry["original_url"] = "https://github.com/discourse/discourse/commit/abc"
            entries.append(entry)
        return entries

    manifest = prepare_catalog(tmp_path, fetch)
    assert len(manifest["cases"]) == 50
    assert sum(c["golden_count"] for c in manifest["cases"]) == 173
    assert next(c for c in manifest["cases"] if c["id"] == "sentry-93824")["split"] == "dev"
    copied = next(c for c in manifest["cases"] if c["id"] == "discourse-benchmark-1")
    assert copied["repository"] == "ai-code-review-evaluation/discourse-graphite"


def test_summary_detects_edited_result(tmp_path):
    data, cases = catalog(tmp_path, 1)
    source, run = tmp_path / "source", tmp_path / "run"
    write_json(source / "case-0" / "result.json", result("bug"))
    asyncio.run(run_benchmark(data, run, cases, "kimi", source, judge_factory=FakeJudge))
    write_json(run / "case-0" / "result.json", result("changed"))
    with pytest.raises(ValueError, match="changed"):
        report(data, run)


def test_fixture_refuses_pr_update_during_download(tmp_path):
    from evals.benchmark_data import prepare_fixture
    case = {"id": "snapshot-test", "repository": "demo/repo", "pull_number": 1,
            "pr_url": "https://github.com/demo/repo/pull/1", "language": "python"}
    calls = 0

    def fetch(url):
        nonlocal calls
        if "/files?" in url:
            return [{"filename": "x.py", "patch": "@@ -1 +1 @@\n-a\n+b"}]
        calls += 1
        return {"base": {"sha": "base"}, "head": {"sha": "first" if calls == 1 else "second"}}

    with pytest.raises(ValueError, match="changed during"):
        prepare_fixture(case, tmp_path, fetch)
    assert not (tmp_path / "fixtures" / case["id"] / "metadata.json").exists()


def test_gpt6_judge_omits_temperature_and_rejects_truncated_output(monkeypatch):
    from types import SimpleNamespace
    from evals.benchmark_score import BenchmarkJudge

    requests = []
    finish = "stop"

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content='{"match":true,"confidence":0.9}')
        )])

    monkeypatch.setattr("evals.benchmark_score.AsyncOpenAI", lambda **_: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setenv("JUDGE_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "medium")
    judge = BenchmarkJudge()
    asyncio.run(judge.match_comment("bug", "bug"))
    assert requests[0]["reasoning_effort"] == "medium"
    assert "temperature" not in requests[0]
    finish = "length"
    with pytest.raises(ValueError, match="did not finish"):
        asyncio.run(judge.match_comment("bug", "bug"))


def test_reasoning_change_invalidates_judge_cache(tmp_path):
    judge = FakeJudge()
    judge.reasoning_effort = "low"
    golden = {"comments": [{"comment": "bug", "category": "bug"}]}
    asyncio.run(score_case(result("bug"), golden, tmp_path, judge))
    judge.reasoning_effort = "medium"
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(score_case(result("bug"), golden, tmp_path, judge))


@pytest.mark.parametrize("failure_kind", ["verification_turn_limit", "tool_protocol_error"])
def test_candidate_failure_is_visible_without_removing_golden_from_score(tmp_path, failure_kind):
    data, cases = catalog(tmp_path, 1)
    source, run = tmp_path / "source", tmp_path / "run"
    review = result()
    review["trace"] = [{
        "type": "candidate_result", "stage": "verify", "candidate_index": 0,
        "verdict": "inconclusive", "failure_kind": failure_kind,
        "last_validation_error": "Invalid VERIFY output",
    }]
    write_json(source / "case-0" / "result.json", review)
    summary = asyncio.run(run_benchmark(data, run, cases, "kimi", source, judge_factory=FakeJudge))
    assert summary["selection_complete"] is True
    assert summary["cases_with_verification_failures"] == 1
    assert summary["verification_verdicts"] == {"inconclusive": 1}
    assert summary["profiles"]["core"]["fn"] == 1
    assert summary["profiles"]["core"]["recall"] == 0
    assert "Verification failure for candidate 0" in (run / "report.md").read_text()


def test_runtime_uncertainty_and_summary_are_not_scored_as_findings():
    review = result('a formal finding')
    expected = candidate_texts(review)
    review['summary'] = 'Context insufficient: tool_protocol_error; not a no-issues conclusion'
    review['trace'] = [{'type':'candidate_result','candidate_index':0,'verdict':'inconclusive','failure_kind':'tool_protocol_error','reason':'Missing context'}]
    assert candidate_texts(review) == expected
