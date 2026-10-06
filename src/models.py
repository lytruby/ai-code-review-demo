from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["low", "medium", "high"]
EvidenceSide = Literal["before", "after"]
FactSource = Literal["repository"]
WorkflowStatus = Literal["complete"]


@dataclass
class ReviewIssue:
    file: str
    severity: Severity
    description: str
    suggestion: str
    # Head-version line range, filled in by the workflow after verification.
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class EvidenceRef:
    side: EvidenceSide
    text: str
    file: str | None = None


@dataclass
class RequiredFact:
    question: str
    source: FactSource
    path: str | None = None
    query: str | None = None


@dataclass
class CandidateIssue:
    file: str
    severity: Severity
    claim: str
    evidence: list[EvidenceRef]
    required_facts: list[RequiredFact] = field(default_factory=list)


@dataclass
class ReviewResult:
    summary: str
    issues: list[ReviewIssue] = field(default_factory=list)
    status: WorkflowStatus = "complete"


def review_result_from_dict(data: dict) -> ReviewResult:
    if not isinstance(data, dict):
        raise ValueError("Review result must be an object")

    status = data.get("status")
    if status != "complete":
        raise ValueError("Review status must be complete")

    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ValueError("Review summary must be a string")

    raw_issues = data.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("Review issues must be a list")

    issues: list[ReviewIssue] = []

    for raw_issue in raw_issues:
        issues.append(review_issue_from_dict(raw_issue))

    return ReviewResult(summary=summary, issues=issues, status=status)


def review_issue_from_dict(data: dict) -> ReviewIssue:
    if not isinstance(data, dict):
        raise ValueError("Each review issue must be an object")

    file = data.get("file")
    severity = data.get("severity")
    description = data.get("description")
    suggestion = data.get("suggestion")

    if not isinstance(file, str):
        raise ValueError("Issue file must be a string")
    if not isinstance(severity, str) or severity not in {"low", "medium", "high"}:
        raise ValueError("Issue severity must be low, medium, or high")
    if not isinstance(description, str):
        raise ValueError("Issue description must be a string")
    if not isinstance(suggestion, str):
        raise ValueError("Issue suggestion must be a string")

    return ReviewIssue(
        file=file,
        severity=severity,
        description=description,
        suggestion=suggestion,
    )
