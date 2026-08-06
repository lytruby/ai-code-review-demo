from types import SimpleNamespace

from src.reviewer import OpenAIReviewer


class FakeCompletions:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_reviewer_uses_read_file_tool(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")

    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="read_file",
            arguments='{"path": "example.py"}',
        ),
    )
    first_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tool_call])
            )
        ]
    )
    final_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    content=(
                        '{"status":"complete","summary":"One issue found","issues":[{'
                        '"file":"example.py","severity":"high",'
                        '"description":"Possible division by zero",'
                        '"suggestion":"Validate b before division"}]}'
                    ),
                )
            )
        ]
    )
    fake_completions = FakeCompletions([first_response, final_response])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions)
    )
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [{"filename": "example.py", "patch": "+    return a / b"}]
    )

    assert result.summary == "One issue found"
    assert result.issues[0].file == "example.py"
    assert len(fake_completions.requests) == 2

    first_input = fake_completions.requests[0]["messages"]
    assert first_input[0]["role"] == "system"
    assert "Follow this review procedure" in first_input[0]["content"]
    assert "Use read_file when additional context" in first_input[0]["content"]
    assert first_input[1]["role"] == "user"
    assert "+    return a / b" in first_input[1]["content"]

    second_input = fake_completions.requests[1]["messages"]
    tool_output = second_input[-1]
    assert tool_output["role"] == "tool"
    assert tool_output["tool_call_id"] == "call-1"
    assert "def divide" in tool_output["content"]

    assert [event["type"] for event in reviewer.last_trace] == [
        "model_response",
        "tool_result",
        "model_response",
    ]
    assert reviewer.last_trace[0]["tool_calls"][0]["name"] == "read_file"
    assert "def divide" in reviewer.last_trace[1]["content"]


def test_kimi_request_uses_fast_baseline_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_THINKING", "disabled")
    monkeypatch.setenv("LLM_MAX_COMPLETION_TOKENS", "2048")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    content='{"status":"complete","summary":"No issues","issues":[]}',
                )
            )
        ]
    )
    completions = FakeCompletions([response])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client,
        repository_root=tmp_path,
        model="kimi-k2.6",
    )

    reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    request = completions.requests[0]
    assert request["max_completion_tokens"] == 2048
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["response_format"] == {"type": "json_object"}


def test_reviewer_retries_non_final_json(tmp_path):
    incomplete_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    content=(
                        '{"status":"needs_context","summary":'
                        '"I still need to inspect the implementation","issues":[]}'
                    ),
                )
            )
        ]
    )
    complete_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    content=(
                        '{"status":"complete","summary":'
                        '"No significant issues found","issues":[]}'
                    ),
                )
            )
        ]
    )
    completions = FakeCompletions([incomplete_response, complete_response])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert result.status == "complete"
    assert len(completions.requests) == 2
    retry_messages = completions.requests[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "Invalid final review" in retry_messages[-1]["content"]
    assert "call read_file directly" in retry_messages[-1]["content"]
    assert [event["type"] for event in reviewer.last_trace] == [
        "model_response",
        "workflow_feedback",
        "model_response",
    ]
