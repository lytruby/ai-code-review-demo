import sys

from src.github_client import GitHubClient
from src.reviewer import OpenAIReviewer
from src.formatter import format_review


def main():
    github = GitHubClient()
    changes = github.get_changes()

    try:
        reviewer = OpenAIReviewer()
        result = reviewer.review(changes)
        review = format_review(result)
    except Exception as error:
        print(f"AI review failed: {error}", file=sys.stderr)
        github.post_comment(
            "## AI Code Review\n\nAI review failed. Please check the workflow logs."
        )
        return

    github.post_comment(review)


if __name__ == "__main__":
    main()
