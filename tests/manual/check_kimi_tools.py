"""Live Kimi tool round trip with the reviewer's assistant-message handling."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.reviewer import OpenAIReviewer
from src.tools import READ_FILE_TOOL, execute_tool


def main():
    reviewer = OpenAIReviewer(provider="kimi", repository_root=ROOT)
    messages = [
        {"role": "system", "content": 'Read the requested repository file with read_file. You need its contents to answer. After receiving the tool output, return only JSON {"ok":true} if it succeeded.'},
        {"role": "user", "content": "Read README.md around line 1 with context_lines 2; then confirm with JSON."},
    ]
    print(f"Checking {reviewer.model} tool continuation", flush=True)
    response = reviewer.client.chat.completions.create(
        model=reviewer.model, messages=messages, reasoning_effort=reviewer.reasoning_effort,
        max_completion_tokens=2048, tools=[reviewer._chat_tool(READ_FILE_TOOL)],
    )
    message = response.choices[0].message
    assert message.tool_calls, "Model did not emit a tool call"
    reviewer._append_assistant(messages, message)
    for call in message.tool_calls:
        output = execute_tool(call.function.name, call.function.arguments, reviewer.repository_root)
        assert json.loads(output).get("ok"), output
        messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    response = reviewer.client.chat.completions.create(
        model=reviewer.model, messages=messages, reasoning_effort=reviewer.reasoning_effort,
        max_completion_tokens=2048, response_format={"type": "json_object"},
    )
    assert json.loads(response.choices[0].message.content)["ok"] is True
    print("PASS: Kimi strict read_file and reasoning-preserving continuation", flush=True)


if __name__ == "__main__":
    main()
