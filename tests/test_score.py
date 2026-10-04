import asyncio
import json

from evals.score import evaluate, load_inputs


class FakeJudge:
    async def match(self, golden_comment, candidate):
        matched = golden_comment == "Real bug" and candidate == "Found real bug"
        return {
            "reasoning": "Same issue" if matched else "Different issue",
            "match": matched,
            "confidence": 0.9,
        }


class MatrixJudge:
    def __init__(self, matches):
        self.matches = matches

    async def match(self, golden_comment, candidate):
        confidence = self.matches.get((golden_comment, candidate))
        return {
            "reasoning": "Configured matrix edge",
            "match": confidence is not None,
            "confidence": confidence or 0.0,
        }


def test_load_inputs(tmp_path):
    case_dir = tmp_path / "runs" / "kimi" / "sop-v1" / "example-1"
    case_dir.mkdir(parents=True)
    (case_dir / "result.json").write_text(
        json.dumps({"issues": [{"description": "Found real bug"}]}),
        encoding="utf-8",
    )
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "example-1.json").write_text(
        json.dumps(
            {"comments": [{"comment": "Real bug", "severity": "High"}]}
        ),
        encoding="utf-8",
    )

    candidates, golden = load_inputs(
        "example-1",
        "KIMI",
        "sop-v1",
        runs_dir=tmp_path / "runs",
        golden_dir=golden_dir,
    )

    assert candidates[0]["description"] == "Found real bug"
    assert golden[0]["severity"] == "High"


def test_evaluate_calculates_official_style_metrics():
    candidates = [
        {"description": "Found real bug"},
        {"description": "Unrelated concern"},
    ]
    golden = [
        {"comment": "Real bug", "severity": "High"},
        {"comment": "Missed bug", "severity": "Medium"},
    ]

    result = asyncio.run(evaluate(FakeJudge(), candidates, golden))

    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["fn"] == 1
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["f1"] == 0.5
    assert result["true_positives"][0]["candidate_index"] == 0
    assert len(result["pairwise_judgments"]) == 4
    assert result["tp"] + result["fp"] == result["total_candidates"]
    assert result["tp"] + result["fn"] == result["total_golden"]


def test_duplicate_candidate_is_counted_as_false_positive():
    candidates = [
        {"description": "Primary report"},
        {"description": "Duplicate report"},
    ]
    golden = [{"comment": "One bug", "severity": "High"}]
    judge = MatrixJudge(
        {
            ("One bug", "Primary report"): 0.95,
            ("One bug", "Duplicate report"): 0.80,
        }
    )

    result = asyncio.run(evaluate(judge, candidates, golden))

    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["true_positives"][0]["candidate_index"] == 0
    assert result["false_positives"] == [
        {
            "candidate_index": 1,
            "candidate": "Duplicate report",
            "reason": "duplicate_match",
            "competing_golden_index": 0,
            "selected_candidate_index": 0,
            "match_confidence": 0.8,
        }
    ]


def test_matching_maximizes_cardinality_before_confidence():
    candidates = [
        {"description": "Broad report"},
        {"description": "First-only report"},
    ]
    golden = [
        {"comment": "First bug", "severity": "High"},
        {"comment": "Second bug", "severity": "Medium"},
    ]
    judge = MatrixJudge(
        {
            ("First bug", "Broad report"): 0.99,
            ("Second bug", "Broad report"): 0.90,
            ("First bug", "First-only report"): 0.80,
        }
    )

    result = asyncio.run(evaluate(judge, candidates, golden))

    assert result["tp"] == 2
    assert result["fp"] == 0
    assert result["fn"] == 0
    selected = {
        match["golden_index"]: match["candidate_index"]
        for match in result["true_positives"]
    }
    assert selected == {0: 1, 1: 0}
