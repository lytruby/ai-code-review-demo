import json

import pytest

from evals.run_eval import checkout_pr, load_fixture, run_case
from src.models import ReviewIssue, ReviewResult


def make_fixture(root):
    fixture = root / "example-1"
    fixture.mkdir()
    (fixture / "metadata.json").write_text(
        json.dumps(
            {
                "id": "example-1",
                "repository": "example/repo",
                "pull_number": 1,
                "head_sha": "head123",
            }
        ),
        encoding="utf-8",
    )
    (fixture / "changes.json").write_text(
        json.dumps([{"filename": "example.py", "patch": "+return value"}]),
        encoding="utf-8",
    )
    return fixture


def test_load_fixture(tmp_path):
    fixture = make_fixture(tmp_path)

    metadata, changes = load_fixture(fixture)

    assert metadata["head_sha"] == "head123"
    assert changes[0]["filename"] == "example.py"


def test_run_case_uses_checked_out_repository(tmp_path):
    fixture = make_fixture(tmp_path)
    run_dir = tmp_path / "runs" / "test-run"
    observed = {}

    def fake_checkout(metadata, destination):
        observed["checkout_metadata"] = metadata
        (destination / "example.py").write_text("return value\n", encoding="utf-8")

    class FakeReviewer:
        def __init__(self, repository_root):
            observed["repository_root"] = repository_root
            self.last_trace = [
                {"type": "model_response", "content": "done", "tool_calls": []}
            ]

        def review(self, changes):
            observed["changes"] = changes
            assert (observed["repository_root"] / "example.py").is_file()
            return ReviewResult(
                summary="One issue",
                issues=[
                    ReviewIssue(
                        file="example.py",
                        severity="medium",
                        description="Example issue",
                        suggestion="Fix it",
                    )
                ],
            )

    result_path = run_case(
        fixture,
        run_dir,
        reviewer_factory=FakeReviewer,
        checkout=fake_checkout,
    )

    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert observed["checkout_metadata"]["head_sha"] == "head123"
    assert observed["changes"][0]["patch"] == "+return value"
    assert saved["issues"][0]["severity"] == "medium"
    assert saved["trace"][0]["type"] == "model_response"


def test_checkout_pr_rejects_changed_head(tmp_path, monkeypatch):
    outputs = iter(["", "", "", "different-head"])

    def fake_run_git(arguments, cwd):
        return next(outputs)

    monkeypatch.setattr("evals.run_eval._run_git", fake_run_git)

    with pytest.raises(ValueError, match="head SHA mismatch"):
        checkout_pr(
            {
                "repository": "example/repo",
                "pull_number": 1,
                "head_sha": "expected-head",
            },
            tmp_path,
        )


def test_run_case_saves_trace_when_review_fails(tmp_path):
    fixture = make_fixture(tmp_path)
    run_dir = tmp_path / "runs" / "test-run"

    def fake_checkout(metadata, destination):
        pass

    class FailingReviewer:
        def __init__(self, repository_root):
            self.last_trace = [
                {
                    "type": "model_response",
                    "content": '{"status":"needs_context"}',
                    "tool_calls": [],
                }
            ]

        def review(self, changes):
            raise ValueError("workflow turn limit")

    with pytest.raises(RuntimeError, match="Failure trace saved"):
        run_case(
            fixture,
            run_dir,
            reviewer_factory=FailingReviewer,
            checkout=fake_checkout,
        )

    error_path = run_dir / "example-1" / "error.json"
    saved = json.loads(error_path.read_text(encoding="utf-8"))
    assert saved["error_type"] == "ValueError"
    assert saved["error"] == "workflow turn limit"
    assert saved["trace"][0]["type"] == "model_response"
