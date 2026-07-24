import pytest

from src.models import ReviewIssue, ReviewResult, review_result_from_dict


def test_review_result_basic():
    issue = ReviewIssue(
        file="test.py",
        severity="high",
        description="Possible division by zero",
        suggestion="Validate count before division",
    )
    result = ReviewResult(
        summary="One correctness issue found",
        issues=[issue],
    )

    empty_result = ReviewResult(summary="No issues found")

    assert empty_result.issues == []
    assert result.issues[0].severity == "high"


def test_review_results_do_not_share_issue_lists():
    result1 = ReviewResult(summary="First")
    result2 = ReviewResult(summary="Second")

    issue = ReviewIssue(
        file="test.py",
        severity="high",
        description="可能发生除零错误",
        suggestion="检查 count 是否为 0",
    )

    result1.issues.append(issue)

    assert len(result1.issues) == 1
    assert result2.issues == []
    assert result1.issues is not result2.issues


def test_review_result_from_valid_dict():
    result = review_result_from_dict(
        {
            "summary": "One issue found",
            "issues": [
                {
                    "file": "test.py",
                    "severity": "high",
                    "description": "Possible division by zero",
                    "suggestion": "Validate count before division",
                }
            ],
        }
    )

    assert result.summary == "One issue found"
    assert result.issues == [
        ReviewIssue(
            file="test.py",
            severity="high",
            description="Possible division by zero",
            suggestion="Validate count before division",
        )
    ]


def test_review_result_rejects_invalid_severity():
    with pytest.raises(ValueError, match="severity"):
        review_result_from_dict(
            {
                "summary": "One issue found",
                "issues": [
                    {
                        "file": "test.py",
                        "severity": "critical",
                        "description": "Possible division by zero",
                        "suggestion": "Validate count before division",
                    }
                ],
            }
        )


def test_review_result_rejects_missing_fields():
    with pytest.raises(ValueError, match="suggestion"):
        review_result_from_dict(
            {
                "summary": "One issue found",
                "issues": [
                    {
                        "file": "test.py",
                        "severity": "high",
                        "description": "Possible division by zero",
                    }
                ],
            }
        )
