from __future__ import annotations

import argparse
import subprocess
import sys


def build_commands(
    case_ids: list[str],
    provider: str,
    run_name: str,
) -> list[list[str]]:
    commands = []
    for case_id in case_ids:
        common_arguments = [
            "--case-id",
            case_id,
            "--provider",
            provider,
            "--run-name",
            run_name,
        ]
        commands.append(
            [sys.executable, "-m", "evals.run_eval", *common_arguments]
        )
        commands.append(
            [sys.executable, "-m", "evals.score", *common_arguments]
        )
    return commands


def run_suite(case_ids: list[str], provider: str, run_name: str) -> None:
    failures = []
    commands = build_commands(case_ids, provider, run_name)
    for command_index in range(0, len(commands), 2):
        run_command = commands[command_index]
        score_command = commands[command_index + 1]
        case_id = run_command[run_command.index("--case-id") + 1]

        run_result = subprocess.run(run_command, check=False)
        if run_result.returncode != 0:
            failures.append(f"{case_id}: review failed")
            continue

        score_result = subprocess.run(score_command, check=False)
        if score_result.returncode != 0:
            failures.append(f"{case_id}: scoring failed")

    if failures:
        raise RuntimeError("Suite completed with failures: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run and score one or more locked code review eval cases."
    )
    parser.add_argument(
        "--case-id",
        dest="case_ids",
        nargs="+",
        required=True,
        help="One or more fixture ids.",
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()

    run_suite(args.case_ids, args.provider, args.run_name)


if __name__ == "__main__":
    main()
