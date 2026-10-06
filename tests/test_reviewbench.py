import subprocess

from evals.reviewbench import build_changes, findings_file, pr_key
from src.models import ReviewIssue, ReviewResult


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_pr_key_matches_reviewbench():
    pr = {"repo": "https://github.com/AA-Factory/aafactory-prototype", "pr_number": 17, "head": "a1978cb7" + "0" * 32}
    assert pr_key(pr) == "AA-Factory_aafactory-prototype_17-a1978cb7"


def test_build_changes_uses_the_merge_base_and_skips_deleted_files(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "gone.py").write_text("y = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "gone.py").unlink()
    git(tmp_path, "commit", "-qam", "feature")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "other.py").write_text("z = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base moves on")
    base = git(tmp_path, "rev-parse", "HEAD")

    changes = build_changes(tmp_path, base, head)

    assert [c["filename"] for c in changes] == ["a.py"]
    assert changes[0]["patch"].startswith("@@ -1 +1 @@")
    assert "+x = 2" in changes[0]["patch"]


def test_findings_file_uses_head_lines_and_falls_back_to_line_one():
    pr = {"repo": "https://github.com/o/r", "pr_number": 1, "base": "b" * 40, "head": "h" * 40, "title": "t"}
    result = ReviewResult(summary="", issues=[
        ReviewIssue("a.py", "high", "Bug", "Fix it", start_line=3, end_line=5),
        ReviewIssue("b.py", "low", "Nit", ""),
    ])
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "cached_prompt_tokens": 4}

    output = findings_file(pr, result, usage, 1.5)

    assert output["pr"] == {"repo": "https://github.com/o/r", "pr_number": 1, "base": "b" * 40, "head": "h" * 40}
    assert output["findings"][0] == {"producer": "ai-code-review-demo", "file": "a.py", "start_line": 3,
                                     "end_line": 5, "message": "Bug\n\nSuggestion: Fix it"}
    assert (output["findings"][1]["start_line"], output["findings"][1]["end_line"]) == (1, 1)
    assert output["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 4, "time_in_ms": 1500}
