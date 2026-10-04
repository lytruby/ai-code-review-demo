import sys
from types import SimpleNamespace

import pytest

from evals.run_suite import build_commands, run_suite


def test_build_commands_runs_review_then_score_for_each_case():
    commands = build_commands(
        ["sentry-93824", "grafana-79265"],
        provider="kimi",
        run_name="verified-claims-v1",
    )

    assert [command[2] for command in commands] == [
        "evals.run_eval",
        "evals.score",
        "evals.run_eval",
        "evals.score",
    ]
    assert all(command[0] == sys.executable for command in commands)
    assert commands[0][-6:] == [
        "--case-id",
        "sentry-93824",
        "--provider",
        "kimi",
        "--run-name",
        "verified-claims-v1",
    ]


def test_run_suite_continues_with_next_case_when_review_fails(monkeypatch):
    observed = []

    def fake_run(command, check):
        observed.append(command)
        case_id = command[command.index("--case-id") + 1]
        module = command[2]
        return SimpleNamespace(
            returncode=1 if case_id == "first" and module == "evals.run_eval" else 0
        )

    monkeypatch.setattr("evals.run_suite.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="first: review failed"):
        run_suite(["first", "second"], "kimi", "test-run")

    assert [(command[2], command[4]) for command in observed] == [
        ("evals.run_eval", "first"),
        ("evals.run_eval", "second"),
        ("evals.score", "second"),
    ]
