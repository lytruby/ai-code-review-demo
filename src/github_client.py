import json
import os
import urllib.request


class GitHubClient:
    def __init__(self):
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
            event = json.load(event_file)

        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.pr_number = event["pull_request"]["number"]
        self.headers = {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_changes(self):
        url = (
            f"https://api.github.com/repos/{self.repository}/pulls/"
            f"{self.pr_number}/files?per_page=100"
        )
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=self.headers)
        ) as response:
            files = json.load(response)

        return [
            {
                "filename": file["filename"],
                "patch": file.get("patch", "Patch unavailable for this file."),
            }
            for file in files
        ]

    def post_comment(self, body):
        url = (
            f"https://api.github.com/repos/{self.repository}/issues/"
            f"{self.pr_number}/comments"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps({"body": body}).encode(),
            headers={**self.headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            print(f"Comment posted (HTTP {response.status})")
