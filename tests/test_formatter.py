from src.formatter import format_review
from src.models import ReviewIssue, ReviewResult


def test_format_review_with_issues():
    result = ReviewResult(
        summary="One correctness issue found",
        issues=[
            ReviewIssue(
                file="test.py",
                severity="high",
                description="Possible division by zero",
                suggestion="Validate count before division",
            )
        ],
    )

    expected = """## AI Code Review

One correctness issue found

### HIGH — test.py

Possible division by zero

**Suggestion:** Validate count before division"""

    assert format_review(result) == expected


def test_format_review_without_issues():
    result = ReviewResult(summary="No issues found")

    expected = """## AI Code Review

No issues found

✅ No potential issues were identified."""

    assert format_review(result) == expected
