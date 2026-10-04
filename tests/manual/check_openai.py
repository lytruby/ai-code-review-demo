import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.reviewer import OpenAIReviewer, ReviewState


def main() -> None:
    reviewer = OpenAIReviewer(
        provider="openai",
        repository_root=REPOSITORY_ROOT,
    )
    state = ReviewState(stage="verify")
    messages = [
        {
            "role": "system",
            "content": (
                "Test the function-tool protocol. First call read_file exactly "
                "once. After receiving its result, return valid JSON with "
                '{"status":"complete"}.'
            ),
        },
        {
            "role": "user",
            "content": (
                "Call read_file for README.md with line=null and "
                "context_lines=null."
            ),
        },
    ]

    first = reviewer._request(messages, state, allow_tools=True)
    reviewer._append_assistant(messages, first)
    tool_calls = first.tool_calls or []
    if len(tool_calls) != 1 or tool_calls[0].function.name != "read_file":
        raise RuntimeError("OpenAI did not return the expected read_file tool call")

    successful_calls = reviewer._execute_tool_calls(
        messages,
        tool_calls,
        state,
        candidate_index=0,
    )
    if successful_calls != 1:
        raise RuntimeError("read_file tool execution failed")

    second = reviewer._request(messages, state, allow_tools=False)
    output = json.loads(second.content or "")
    if output.get("status") != "complete":
        raise RuntimeError("OpenAI did not complete after the tool result")

    print(f"model: {reviewer.model}")
    print(f"reasoning_effort: {reviewer.reasoning_effort}")
    print("tool_round_trip: OK")
    print("PASS: OpenAI Responses connection is working")


if __name__ == "__main__":
    main()
