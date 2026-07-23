import json
import os
import urllib.request


with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
    event = json.load(event_file)

repository = os.environ["GITHUB_REPOSITORY"]
pr_number = event["pull_request"]["number"]
headers = {
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

files_url = (
    f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files?per_page=100"
)
with urllib.request.urlopen(
    urllib.request.Request(files_url, headers=headers)
) as response:
    changed_files = json.load(response)

file_list = "\n".join(f"- {file['filename']}" for file in changed_files)
patches = "\n\n".join(
    f"### {file['filename']}\n```diff\n"
    f"{file.get('patch', 'Patch unavailable for this file.')}\n```"
    for file in changed_files
)
comment = (
    f"AI Code Review triggered successfully\n\n"
    f"Changed files:\n{file_list}\n\n"
    f"Patches:\n\n{patches}"
)

comments_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
comment_request = urllib.request.Request(
    comments_url,
    data=json.dumps({"body": comment}).encode(),
    headers={**headers, "Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(comment_request) as response:
    print(f"Comment posted (HTTP {response.status})")
