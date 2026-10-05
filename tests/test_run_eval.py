import json

import pytest

from evals.run_eval import (
    checkout_pr,
    load_fixture,
    prepare_cached_repository,
    run_case,
    save_failure,
    save_result,
)
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


def test_run_case_passes_explicit_provider_to_reviewer(tmp_path):
    fixture = make_fixture(tmp_path)
    observed = {}

    class FakeReviewer:
        def __init__(self, repository_root, provider):
            observed["provider"] = provider
            self.last_trace = []

        def review(self, changes):
            return ReviewResult(summary="No issues")

    result_path = run_case(
        fixture,
        tmp_path / "runs",
        reviewer_factory=FakeReviewer,
        checkout=lambda metadata, destination: None,
        provider="openai",
    )

    assert result_path.is_file()
    assert observed["provider"] == "openai"


def test_run_case_discover_only_skips_checkout(tmp_path):
    fixture = make_fixture(tmp_path)
    observed = {}

    def fail_checkout(metadata, destination):
        raise AssertionError("discover-only must not check out the repository")

    class FakeReviewer:
        def __init__(self, repository_root, candidates_per_pass):
            observed["candidates_per_pass"] = candidates_per_pass
            self.last_trace = []

        def review(self, changes):
            raise AssertionError("discover-only must not run the full review")

        def discover_only(self, changes):
            return ReviewResult(summary="Discovery only: 0 unverified candidate(s).")

    result_path = run_case(
        fixture,
        tmp_path / "runs",
        reviewer_factory=FakeReviewer,
        checkout=fail_checkout,
        repository_cache=tmp_path / "cache",
        reviewer_settings={"candidates_per_pass": 3},
        discover_only=True,
    )

    assert result_path.is_file()
    assert observed["candidates_per_pass"] == 3


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


def test_prepare_cached_repository_reuses_locked_checkout(tmp_path):
    metadata = {
        "repository": "example/repo",
        "head_sha": "abc123",
    }
    calls = []

    def fake_checkout(received_metadata, destination):
        calls.append(received_metadata)
        (destination / "example.py").write_text("value = 1\n", encoding="utf-8")

    first = prepare_cached_repository(metadata, tmp_path / "cache", fake_checkout)
    second = prepare_cached_repository(metadata, tmp_path / "cache", fake_checkout)

    assert first == second
    assert (second / "example.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (second / ".benchmark-head-sha").read_text().strip() == "abc123"
    assert len(calls) == 1


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


def test_failed_run_can_be_retried_in_same_directory(tmp_path):
    run_dir = tmp_path / "runs" / "test-run"

    first_error = save_failure(run_dir, "example-1", TimeoutError("first"))
    second_error = save_failure(run_dir, "example-1", TimeoutError("second"))
    result_path = save_result(
        run_dir,
        "example-1",
        ReviewResult(summary="Recovered"),
    )

    assert first_error.name == "error.json"
    assert second_error.name == "error-2.json"
    assert result_path.name == "result.json"
    assert json.loads(result_path.read_text(encoding="utf-8"))["summary"] == "Recovered"

    with pytest.raises(FileExistsError, match="Result already exists"):
        save_result(run_dir, "example-1", ReviewResult(summary="Do not overwrite"))
