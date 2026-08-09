import json
from types import SimpleNamespace

import pytest

from src.models import CandidateIssue, EvidenceRef
from src.reviewer import OpenAIReviewer


class FakeCompletions:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        response_or_error = self.responses.pop(0)
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error


def response(content=None, tool_calls=None, finish_reason="stop", usage=None):
    return SimpleNamespace(
        usage=usage,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
            )
        ]
    )


def test_reviewer_runs_discover_verify_and_finalize_with_tool(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    candidate = (
        '{"file":"example.py","severity":"high",'
        '"claim":"Possible division by zero",'
        '"evidence":[{"side":"after","text":"return a / b"}],'
        '"required_facts":[]}'
    )
    verified_issue = (
        '{"file":"example.py","severity":"high",'
        '"description":"Possible division by zero",'
        '"suggestion":"Validate b before division"}'
    )
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="read_file",
            arguments='{"path": "example.py"}',
        ),
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(tool_calls=[tool_call]),
                response(
                    '{"decisions":[{"candidate_index":0,"verdict":"keep",'
                    f'"basis":"diff","reason":"Confirmed from the file",'
                f'"issue":{verified_issue}}}]}}'
            ),
            response('{"status":"complete","summary":"One issue found"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [{"filename": "example.py", "patch": "+    return a / b"}]
    )

    assert result.summary == "One issue found"
    assert result.issues[0].file == "example.py"
    assert len(completions.requests) == 4
    assert "Stage: DISCOVER" in completions.requests[0]["messages"][0]["content"]
    discover_prompt = completions.requests[0]["messages"][0]["content"]
    assert "Return at most 5 candidates" in discover_prompt
    assert "ordered by evidence strength" in discover_prompt
    assert "must declare the required facts" in discover_prompt
    assert "tools" not in completions.requests[0]
    assert "Stage: VERIFY" in completions.requests[1]["messages"][0]["content"]
    assert completions.requests[1]["tools"][0]["function"]["name"] == "read_file"
    assert completions.requests[1]["tools"][1]["function"]["name"] == "search_code"
    assert "Stage: FINALIZE" in completions.requests[3]["messages"][0]["content"]

    verify_after_tool = completions.requests[2]["messages"]
    assert verify_after_tool[-1]["role"] == "tool"
    assert "def divide" in verify_after_tool[-1]["content"]
    assert [event["stage"] for event in reviewer.last_trace if "stage" in event] == [
        "discover",
        "discover",
        "verify",
        "verify",
        "verify",
        "verify",
        "verify",
        "finalize",
        "finalize",
    ]
    assert reviewer.last_state is not None
    assert reviewer.last_state.stage == "complete"
    assert reviewer.last_state.model_turns == 4
    assert reviewer.last_state.tool_calls == 1


def test_kimi_requests_use_fast_baseline_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_THINKING", "disabled")
    monkeypatch.setenv("LLM_MAX_COMPLETION_TOKENS", "2048")
    monkeypatch.setenv("LLM_DISCOVER_MAX_COMPLETION_TOKENS", "4096")
    completions = FakeCompletions(
        [
            response(
                '{"candidates":[]}',
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=10,
                    total_tokens=110,
                ),
            ),
            response('{"decisions":[]}'),
            response(
                '{"status":"complete","summary":"No significant issues found"}'
            ),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client,
        repository_root=tmp_path,
        model="kimi-k2.6",
    )

    reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert len(completions.requests) == 3
    assert completions.requests[0]["max_completion_tokens"] == 4096
    for request in completions.requests[1:]:
        assert request["max_completion_tokens"] == 2048
    for request in completions.requests:
        assert request["extra_body"] == {"thinking": {"type": "disabled"}}
        assert request["response_format"] == {"type": "json_object"}
    first_trace = reviewer.last_trace[0]
    assert first_trace["finish_reason"] == "stop"
    assert first_trace["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
    }


def test_kimi_k3_requests_use_low_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    completions = FakeCompletions(
        [
            response('{"candidates":[]}'),
            response('{"decisions":[]}'),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client,
        repository_root=tmp_path,
        model="kimi-k3",
    )

    reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert len(completions.requests) == 3
    for request in completions.requests:
        assert request["reasoning_effort"] == "low"
        assert "extra_body" not in request


def test_kimi_k3_rejects_invalid_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "disabled")
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))

    with pytest.raises(ValueError, match="low, high, or max"):
        OpenAIReviewer(
            client=client,
            repository_root=tmp_path,
            model="kimi-k3",
        )


def test_reviewer_retries_transient_api_error_without_using_model_turn(
    tmp_path, monkeypatch
):
    class TemporaryAPIError(Exception):
        pass

    monkeypatch.setattr("src.reviewer.TRANSIENT_API_ERRORS", (TemporaryAPIError,))
    monkeypatch.setenv("LLM_TRANSIENT_RETRIES", "2")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "0")
    completions = FakeCompletions(
        [
            TemporaryAPIError("temporary gateway failure"),
            response('{"candidates":[]}'),
            response('{"decisions":[]}'),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert result.summary == "No issues"
    assert reviewer.last_state.model_turns == 3
    assert reviewer.last_state.api_attempts == 4
    request_errors = [
        event
        for event in reviewer.last_trace
        if event["type"] == "model_request_error"
    ]
    assert request_errors == [
        {
            "type": "model_request_error",
            "stage": "discover",
            "attempt": 1,
            "will_retry": True,
            "error_type": "TemporaryAPIError",
            "status_code": None,
            "error": "temporary gateway failure",
        }
    ]


def test_reviewer_retries_invalid_discover_output(tmp_path):
    completions = FakeCompletions(
        [
            response('{"status":"complete"}'),
            response('{"candidates":[]}'),
            response('{"decisions":[]}'),
            response(
                '{"status":"complete","summary":"No significant issues found"}'
            ),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert result.status == "complete"
    assert len(completions.requests) == 4
    retry_messages = completions.requests[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "Invalid DISCOVER output" in retry_messages[-1]["content"]
    feedback = [event for event in reviewer.last_trace if event["type"] == "workflow_feedback"]
    assert feedback[0]["stage"] == "discover"


def test_reviewer_keeps_valid_candidates_when_one_candidate_is_rejected(tmp_path):
    candidates = (
        '[{"file":"example.py","severity":"medium","claim":"Valid claim",'
        '"evidence":[{"side":"after","text":"value = 1"}],'
        '"required_facts":[]},'
        '{"file":"example.py","severity":"low","claim":"Invented claim",'
        '"evidence":[{"side":"after","text":"missing_call()"}],'
        '"required_facts":[]}]'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":{candidates}}}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"diff","reason":"Not a defect","issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert result.issues == []
    assert len(completions.requests) == 3
    rejection_event = next(
        event
        for event in reviewer.last_trace
        if event["type"] == "candidate_rejections"
    )
    assert rejection_event["rejections"][0]["candidate_index"] == 1
    discover_result = next(
        event
        for event in reviewer.last_trace
        if event["type"] == "stage_result" and event["stage"] == "discover"
    )
    assert discover_result["candidate_count"] == 1
    assert discover_result["rejected_candidate_count"] == 1


def test_verify_drop_decision_does_not_become_final_issue():
    candidate = CandidateIssue(
        file="example.py",
        severity="low",
        claim="Speculative concern",
        evidence=[EvidenceRef(side="after", text="value = 1")],
    )

    verified, decisions = OpenAIReviewer._parse_decisions(
        '{"decisions":[{"candidate_index":0,"verdict":"drop",'
        '"basis":"diff","reason":"Only a future maintenance concern",'
        '"issue":null}]}',
        [candidate],
    )

    assert verified == []
    assert decisions[0]["verdict"] == "drop"


def test_verify_requires_one_decision_per_candidate():
    candidate = CandidateIssue(
        file="example.py",
        severity="medium",
        claim="Possible bug",
        evidence=[EvidenceRef(side="after", text="value = 1")],
    )

    with pytest.raises(ValueError, match="one decision per candidate"):
        OpenAIReviewer._parse_decisions('{"decisions":[]}', [candidate])


def test_verify_repository_basis_requires_successful_candidate_tool_call():
    candidate = CandidateIssue(
        file="example.py",
        severity="medium",
        claim="Definition may contradict the call",
        evidence=[EvidenceRef(side="after", text="call()")],
    )
    decision = (
        '{"decisions":[{"candidate_index":0,"verdict":"drop",'
        '"basis":"repository","reason":"Definition checked","issue":null}]}'
    )

    with pytest.raises(ValueError, match="requires a successful tool call"):
        OpenAIReviewer._parse_decisions(decision, [candidate])

    verified, decisions = OpenAIReviewer._parse_decisions(
        decision,
        [candidate],
        repository_context_available=True,
    )

    assert verified == []
    assert decisions[0]["basis"] == "repository"


def test_reviewer_retries_empty_verify_response_without_replaying_it(tmp_path):
    candidate = (
        '{"file":"example.py","severity":"low",'
        '"claim":"Speculative concern","evidence":'
        '[{"side":"after","text":"value = 1"}],"required_facts":[]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(content=""),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"diff","reason":"Unsupported speculation",'
                '"issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues found"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert result.issues == []
    retry_messages = completions.requests[2]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "Invalid VERIFY output" in retry_messages[-1]["content"]
    assert not any(
        message["role"] == "assistant" and not message.get("content")
        for message in retry_messages
        if "tool_calls" not in message
    )


def test_reviewer_verifies_candidates_in_separate_model_calls(tmp_path):
    candidates = (
        '[{"file":"first.py","severity":"low","claim":"First claim",'
        '"evidence":[{"side":"after","text":"first()"}],'
        '"required_facts":[]},'
        '{"file":"second.py","severity":"medium",'
        '"claim":"Second claim","evidence":'
        '[{"side":"after","text":"second()"}],"required_facts":[]}]'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":{candidates}}}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"diff","reason":"Unsupported","issue":null}]}'
            ),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"keep",'
                '"basis":"diff","reason":"Supported",'
                '"issue":{"file":"second.py",'
                '"severity":"medium","description":"Second issue",'
                '"suggestion":"Fix it"}}]}'
            ),
            response('{"status":"complete","summary":"One issue found"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [
            {"filename": "first.py", "patch": "+first()"},
            {"filename": "second.py", "patch": "+second()"},
        ]
    )

    assert [issue.file for issue in result.issues] == ["second.py"]
    assert len(completions.requests) == 4
    assert '"claim": "First claim"' in completions.requests[1]["messages"][1]["content"]
    assert '"claim": "Second claim"' in completions.requests[2]["messages"][1]["content"]
    decisions = [
        event
        for event in reviewer.last_trace
        if event["type"] == "candidate_result"
    ]
    assert [decision["candidate_index"] for decision in decisions] == [0, 1]
    assert [decision["basis"] for decision in decisions] == ["diff", "diff"]


def test_workflow_acquires_required_repository_facts_before_verify(tmp_path):
    source = tmp_path / "buffer.py"
    source.write_text("class SpansBuffer:\n    pass\n", encoding="utf-8")
    candidate = (
        '{"file":"caller.py","severity":"medium","claim":"Buffer concern",'
        '"evidence":[{"side":"after","text":"SpansBuffer()"}],'
        '"required_facts":[{"question":"How is SpansBuffer implemented?",'
        '"source":"repository","query":"class SpansBuffer"}]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"repository",'
                '"reason":"Definition disproves the concern","issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [{"filename": "caller.py", "patch": "+SpansBuffer()"}]
    )

    assert result.issues == []
    assert reviewer.last_state.tool_calls == 2
    tool_results = [
        event for event in reviewer.last_trace if event["type"] == "tool_result"
    ]
    assert [event["name"] for event in tool_results] == ["search_code", "read_file"]
    assert [event["stage"] for event in tool_results] == [
        "acquire_context",
        "acquire_context",
    ]
    assert [event["candidate_index"] for event in tool_results] == [0, 0]
    assert "buffer.py" in tool_results[0]["content"]
    assert "class SpansBuffer" in tool_results[1]["content"]
    candidate_result = next(
        event
        for event in reviewer.last_trace
        if event["type"] == "candidate_result"
    )
    assert candidate_result["basis"] == "repository"
    assert candidate_result["successful_tool_calls"] == 2


def test_workflow_reads_exact_required_fact_path_without_searching(tmp_path):
    template = tmp_path / "app" / "views" / "topics" / "show.html.erb"
    template.parent.mkdir(parents=True)
    template.write_text("<%= @topic_view.title %>\n", encoding="utf-8")
    candidate = (
        '{"file":"controller.rb","severity":"medium",'
        '"claim":"The template may require missing state",'
        '"evidence":[{"side":"after","text":"render :show"}],'
        '"required_facts":[{"question":"What state does the template use?",'
        '"source":"repository",'
        '"path":"app/views/topics/show.html.erb"}]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"repository","reason":"Template context is available",'
                '"issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [{"filename": "controller.rb", "patch": "+render :show"}]
    )

    assert result.issues == []
    tool_results = [
        event for event in reviewer.last_trace if event["type"] == "tool_result"
    ]
    assert [event["name"] for event in tool_results] == ["read_file"]
    read_result = json.loads(tool_results[0]["content"])
    assert read_result["path"] == "app/views/topics/show.html.erb"
    assert read_result["content"] == "<%= @topic_view.title %>\n"
    candidate_result = next(
        event
        for event in reviewer.last_trace
        if event["type"] == "candidate_result"
    )
    assert candidate_result["successful_tool_calls"] == 1


def test_workflow_searches_within_required_fact_path(tmp_path):
    source = tmp_path / "app" / "controllers" / "topics_controller.rb"
    source.parent.mkdir(parents=True)
    source.write_text(
        "before_filter :ensure_logged_in\n\ndef unsubscribe\nend\n",
        encoding="utf-8",
    )
    candidate = (
        '{"file":"app/controllers/topics_controller.rb","severity":"high",'
        '"claim":"unsubscribe may be unauthenticated",'
        '"evidence":[{"side":"after","text":"def unsubscribe\\nend"}],'
        '"required_facts":[{"question":"Does the controller require login?",'
        '"source":"repository","path":"app/controllers/topics_controller.rb",'
        '"query":"ensure_logged_in"}]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"drop",'
                '"basis":"repository","reason":"Authentication is present",'
                '"issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [
            {
                "filename": "app/controllers/topics_controller.rb",
                "patch": "+def unsubscribe\n+end",
            }
        ]
    )

    assert result.issues == []
    tool_results = [
        event for event in reviewer.last_trace if event["type"] == "tool_result"
    ]
    assert [event["name"] for event in tool_results] == ["search_code", "read_file"]
    search_result = json.loads(tool_results[0]["content"])
    assert search_result["path"] == "app/controllers/topics_controller.rb"
    assert search_result["matches"][0]["content"] == (
        "before_filter :ensure_logged_in"
    )


def test_discover_rejects_required_fact_without_path_or_query():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Missing validation","evidence":'
        '[{"side":"after","text":"value = 1"}],'
        '"required_facts":[{"question":"How is this used?",'
        '"source":"repository"}]}]}'
    )

    with pytest.raises(ValueError, match="path, query, or both"):
        OpenAIReviewer._parse_candidates(
            output, "File: example.py\nPatch:\n+value = 1"
        )


def test_discover_rejects_evidence_not_present_in_diff():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Missing validation","evidence":'
        '[{"side":"after","text":"if value is None"}],'
        '"required_facts":[]}]} '
    )

    with pytest.raises(ValueError, match="declared file diff"):
        OpenAIReviewer._parse_candidates(output, "File: example.py\nPatch:\n+value = 1")


def test_discover_accepts_source_evidence_without_diff_marker_or_indentation():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Division may fail","evidence":'
        '[{"side":"after","text":"return a / b"}],'
        '"required_facts":[]}]} '
    )
    changes = "File: example.py\nPatch:\n@@ -1,2 +1,2 @@\n+    return a / b"

    candidates = OpenAIReviewer._parse_candidates(output, changes)

    assert candidates[0].evidence == [
        EvidenceRef(side="after", text="return a / b")
    ]


def test_discover_matches_consecutive_lines_in_post_change_file():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Changed behavior","evidence":'
        '[{"side":"after","text":"start()\\nfinish()"}],'
        '"required_facts":[]}]} '
    )
    changes = (
        "File: example.py\nPatch:\n@@ -1,2 +1,2 @@\n"
        "-old_start()\n+start()\n finish()"
    )

    candidates = OpenAIReviewer._parse_candidates(output, changes)

    assert candidates[0].evidence == [
        EvidenceRef(side="after", text="start()\nfinish()")
    ]


def test_discover_accepts_before_and_after_transition_evidence():
    output = (
        '{"candidates":[{"file":"example.py","severity":"high",'
        '"claim":"The call became blocking","evidence":['
        '{"side":"before","text":"go run()"},'
        '{"side":"after","text":"run()"}],"required_facts":[]}]} '
    )
    changes = "File: example.py\nPatch:\n@@ -1 +1 @@\n-go run()\n+run()"

    candidates = OpenAIReviewer._parse_candidates(output, changes)

    assert candidates[0].evidence == [
        EvidenceRef(side="before", text="go run()"),
        EvidenceRef(side="after", text="run()"),
    ]


def test_discover_uses_hunk_header_function_context_as_source():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Initialization changed","evidence":['
        '{"side":"after","text":"def __init__(\\nself.value = value"}],'
        '"required_facts":[]}]} '
    )
    changes = (
        "File: example.py\nPatch:\n@@ -10,2 +10,2 @@ def __init__(\n"
        "-    self.value = old_value\n+    self.value = value"
    )

    candidates = OpenAIReviewer._parse_candidates(output, changes)

    assert candidates[0].evidence[0].text == "def __init__(\nself.value = value"


def test_discover_rejects_evidence_on_wrong_side():
    output = (
        '{"candidates":[{"file":"example.py","severity":"high",'
        '"claim":"The call became blocking","evidence":['
        '{"side":"after","text":"go run()"}],"required_facts":[]}]} '
    )
    changes = "File: example.py\nPatch:\n@@ -1 +1 @@\n-go run()\n+run()"

    with pytest.raises(ValueError, match="consecutive after source lines"):
        OpenAIReviewer._parse_candidates(output, changes)


def test_discover_rejects_ellipsis_between_non_contiguous_evidence():
    output = (
        '{"candidates":[{"file":"example.py","severity":"medium",'
        '"claim":"Two calls interact","evidence":['
        '{"side":"after","text":"first()\\n...\\nlast()"}],'
        '"required_facts":[]}]} '
    )
    changes = "File: example.py\nPatch:\n+first()\n+middle()\n+last()"

    with pytest.raises(ValueError, match="consecutive after source lines"):
        OpenAIReviewer._parse_candidates(output, changes)


def test_discover_rejects_evidence_copied_from_another_file():
    output = (
        '{"candidates":[{"file":"first.py","severity":"medium",'
        '"claim":"Missing validation","evidence":'
        '[{"side":"after","text":"dangerous_call()"}],'
        '"required_facts":[]}]} '
    )
    changes = (
        "File: first.py\nPatch:\n+safe_call()\n\n"
        "File: second.py\nPatch:\n+dangerous_call()"
    )

    with pytest.raises(ValueError, match="declared file diff"):
        OpenAIReviewer._parse_candidates(output, changes)
