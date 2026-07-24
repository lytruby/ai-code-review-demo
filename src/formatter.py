from src.models import ReviewResult


def format_review(result: ReviewResult) -> str:
    sections = ["## AI Code Review", result.summary]

    if not result.issues:
        sections.append("✅ No potential issues were identified.")
        return "\n\n".join(sections)

    for issue in result.issues:
        sections.append(
            f"### {issue.severity.upper()} — {issue.file}\n\n"
            f"{issue.description}\n\n"
            f"**Suggestion:** {issue.suggestion}"
        )

    return "\n\n".join(sections)
