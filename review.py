import json
import os
import urllib.request


with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
    event = json.load(event_file)

repository = os.environ["GITHUB_REPOSITORY"]
pr_number = event["pull_request"]["number"]
url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"

request = urllib.request.Request(
    url,
    data=json.dumps({"body": "AI Code Review triggered successfully"}).encode(),
    headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    },
    method="POST",
)

with urllib.request.urlopen(request) as response:
    print(f"Comment posted (HTTP {response.status})")
