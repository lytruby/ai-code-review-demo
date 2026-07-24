from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["low", "medium", "high"]


@dataclass
class ReviewIssue:
    file: str
    severity: Severity
    description: str
    suggestion: str


@dataclass
class ReviewResult:
    summary: str
    issues: list[ReviewIssue] = field(default_factory=list)


def review_result_from_dict(data: dict) -> ReviewResult:
    if not isinstance(data, dict):
        raise ValueError("Review result must be an object")

    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ValueError("Review summary must be a string")

    raw_issues = data.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("Review issues must be a list")

    issues: list[ReviewIssue] = []

    for raw_issue in raw_issues:
        if not isinstance(raw_issue, dict):
            raise ValueError("Each review issue must be an object")

        file = raw_issue.get("file")
        severity = raw_issue.get("severity")
        description = raw_issue.get("description")
        suggestion = raw_issue.get("suggestion")

        if not isinstance(file, str):
            raise ValueError("Issue file must be a string")
        if not isinstance(severity, str) or severity not in {"low", "medium", "high"}:
            raise ValueError("Issue severity must be low, medium, or high")
        if not isinstance(description, str):
            raise ValueError("Issue description must be a string")
        if not isinstance(suggestion, str):
            raise ValueError("Issue suggestion must be a string")

        issues.append(
            ReviewIssue(
                file=file,
                severity=severity,
                description=description,
                suggestion=suggestion,
            )
        )

    return ReviewResult(summary=summary, issues=issues)
