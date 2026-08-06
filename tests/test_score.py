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


def test_load_inputs(tmp_path):
    case_dir = tmp_path / "runs" / "kimi" / "example-1"
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
