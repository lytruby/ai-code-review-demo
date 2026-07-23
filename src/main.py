import sys

from github_client import GitHubClient
from reviewer import OpenAIReviewer


def main():
    github = GitHubClient()
    changes = github.get_changes()

    try:
        reviewer = OpenAIReviewer()
        review = reviewer.review(changes)
    except Exception as error:
        print(f"AI review failed: {error}", file=sys.stderr)
        github.post_comment(
            "## AI Code Review\n\nAI review failed. Please check the workflow logs."
        )
        return

    github.post_comment(review)


if __name__ == "__main__":
    main()
