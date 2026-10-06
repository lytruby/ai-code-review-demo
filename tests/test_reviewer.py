import json
from types import SimpleNamespace

import pytest

from src.models import CandidateIssue, EvidenceRef, RequiredFact
import src.reviewer
from src.reviewer import MAX_CANDIDATES, MAX_CANDIDATES_PER_DISCOVERY_PASS, OpenAIReviewer, ReviewState


@pytest.fixture(autouse=True)
def single_discovery_sample(monkeypatch, request):
    # Most tests script one response per discovery pass; repeated sampling is
    # covered by its own tests.
    if "real_sample_default" not in request.keywords:
        monkeypatch.setattr(src.reviewer, "DISCOVERY_SAMPLES", 1)
    # Scripted responses are consumed in order, so discovery runs serially here.
    if "parallel_discovery" not in request.keywords:
        monkeypatch.setattr(src.reviewer, "DISCOVERY_WORKERS", 1)


class FakeCompletions:
    def __init__(
        self,
        responses,
        auto_empty_state_pass=True,
        auto_identity_deduplication=True,
    ):
        self.responses = responses
        self.requests = []
        self.auto_empty_state_pass = auto_empty_state_pass
        self.auto_identity_deduplication = auto_identity_deduplication

    def create(self, **request):
        self.requests.append(request)
        system_content = request["messages"][0]["content"]
        prompt_content = "\n".join(
            str(message.get("content") or "")
            for message in request["messages"]
            if isinstance(message, dict)
        )
        if (
            self.auto_empty_state_pass
            and "Discovery pass:" in prompt_content
            and "Discovery pass: correctness" not in prompt_content
        ):
            return response('{"candidates":[]}')
        if self.auto_identity_deduplication and "Stage: DEDUPLICATE" in system_content:
            candidates = json.loads(
                request["messages"][1]["content"].removeprefix("Candidates:\n")
            )
            groups = [
                {
                    "candidate_indices": [index],
                    "representative_index": index,
                    "priority": 3,
                    "reason": "Distinct candidate",
                }
                for index in range(len(candidates))
            ]
            return response(json.dumps({"groups": groups}))
        response_or_error = self.responses.pop(0)
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error


class FakeResponses:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        prompt_content = request["instructions"] + json.dumps(request["input"])
        if (
            "Discovery pass:" in prompt_content
            and "Discovery pass: correctness" not in prompt_content
        ):
            return responses_response('{"candidates":[]}')
        return self.responses.pop(0)


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


def responses_response(
    content,
    output=None,
    status="completed",
    usage=None,
    response_id="response-test",
):
    if output is None:
        output = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        ]
    return SimpleNamespace(
        id=response_id,
        output_text=content,
        output=output,
        status=status,
        incomplete_details=None,
        usage=usage,
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
                    '"supporting_evidence":[{"source":"diff",'
                    '"file":"example.py","side":"after",'
                    '"text":"return a / b"}],'
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
    assert len(completions.requests) == 7
    assert "Stage: DISCOVER" in completions.requests[0]["messages"][0]["content"]
    discover_prompt = completions.requests[0]["messages"][0]["content"]
    assert f"Return at most {MAX_CANDIDATES_PER_DISCOVERY_PASS} candidates" in discover_prompt
    assert "ordered by evidence strength" in discover_prompt
    assert "must declare the required facts" in discover_prompt
    assert "tools" not in completions.requests[0]
    assert "Stage: VERIFY" in completions.requests[4]["messages"][0]["content"]
    verify_prompt = completions.requests[4]["messages"][0]["content"]
    assert "smallest concrete defect" in verify_prompt
    assert "appears intentional" in verify_prompt
    assert "return revise and remove or narrow" in verify_prompt
    assert completions.requests[4]["tools"][0]["function"]["name"] == "read_file"
    assert completions.requests[4]["tools"][1]["function"]["name"] == "search_code"
    assert "Stage: FINALIZE" in completions.requests[6]["messages"][0]["content"]
    # JSON mode is only sent on turns without tools.
    assert completions.requests[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.requests[4]
    assert "response_format" not in completions.requests[5]
    assert completions.requests[6]["response_format"] == {"type": "json_object"}

    verify_after_tool = completions.requests[5]["messages"]
    assert verify_after_tool[-1]["role"] == "tool"
    assert "def divide" in verify_after_tool[-1]["content"]
    assert [event["stage"] for event in reviewer.last_trace if "stage" in event and event["type"] != "tool_gateway_decision"] == [
        "discover",
        "discover",
        "discover",
        "discover",
        "discover",
        "discover",
        "discover",
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
    assert reviewer.last_state.model_turns == 7
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

    assert len(completions.requests) == 6
    assert completions.requests[0]["max_completion_tokens"] == 4096
    for request in completions.requests[1:4]:
        assert request["max_completion_tokens"] == 4096
    for request in completions.requests[4:]:
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

    assert len(completions.requests) == 6
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


def test_kimi_assistant_continuation_preserves_reasoning_and_tools():
    from openai.types.chat import ChatCompletionMessage

    raw = {
        "role": "assistant", "content": None, "reasoning_content": "Need source evidence.",
        "tool_calls": [{"id": "call-read", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"x.py"}'}}],
    }
    message = ChatCompletionMessage.model_validate(raw)
    messages = []
    OpenAIReviewer._append_assistant(messages, message)
    assert messages == [raw]


def test_provider_selects_matching_api_key_and_endpoint(tmp_path, monkeypatch):
    created_clients = []

    class FakeOpenAI:
        def __init__(self, **options):
            created_clients.append(options)

    monkeypatch.setattr("src.reviewer.OpenAI", FakeOpenAI)
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://custom-moonshot.example/v1")
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)

    openai_reviewer = OpenAIReviewer(
        repository_root=tmp_path,
        provider="openai",
        model="gpt-5.6",
    )
    kimi_reviewer = OpenAIReviewer(
        repository_root=tmp_path,
        provider="kimi",
        model="kimi-k3",
    )

    assert openai_reviewer.provider == "openai"
    assert openai_reviewer.reasoning_effort == "medium"
    assert created_clients[0]["api_key"] == "openai-secret"
    assert "base_url" not in created_clients[0]
    assert created_clients[1]["api_key"] == "moonshot-secret"
    assert created_clients[1]["base_url"] == "https://custom-moonshot.example/v1"


def test_gpt_5_6_requests_use_medium_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    responses = FakeResponses(
        [
            responses_response('{"candidates":[]}'),
            responses_response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(responses=responses),
        repository_root=tmp_path,
        provider="openai",
        model="gpt-5.6",
    )

    reviewer.review([{"filename": "example.py", "patch": "+value = 1"}])

    assert len(responses.requests) == 5
    for request in responses.requests:
        assert request["reasoning"] == {"effort": "medium"}
        assert request["text"] == {"format": {"type": "json_object"}}
        assert "valid JSON" in request["input"][0]["content"]
        assert "messages" not in request
        assert "extra_body" not in request


def test_responses_continuation_uses_previous_id_and_only_new_tool_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "medium")
    responses = FakeResponses(
        [
            responses_response(
                "",
                output=[
                    {"type": "reasoning", "id": "reasoning-1", "summary": []},
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": (
                            '{"path":"example.py","line":null,'
                            '"context_lines":null}'
                        ),
                        "status": "completed",
                    },
                ],
                response_id="response-1",
            ),
            responses_response(
                '{"status":"complete"}',
                response_id="response-2",
            ),
        ]
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(responses=responses),
        repository_root=tmp_path,
        provider="openai",
        model="gpt-5.6",
    )
    state = ReviewState(stage="verify")
    messages = [
        {"role": "system", "content": "Return JSON"},
        {"role": "user", "content": "Read the file"},
    ]

    first = reviewer._request(messages, state, allow_tools=True)
    reviewer._append_assistant(messages, first)
    messages.append(
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'}
    )
    second = reviewer._request(messages, state, allow_tools=False)

    continuation = responses.requests[1]
    assert first.tool_calls[0].function.name == "read_file"
    assert second.content == '{"status":"complete"}'
    assert continuation["previous_response_id"] == "response-1"
    assert continuation["input"][1:] == [
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"ok":true}',
        }
    ]
    assert not any("status" in item for item in continuation["input"])


def test_gpt_responses_request_sends_native_function_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "medium")
    responses = FakeResponses(
        [
            responses_response(
                "",
                output=[
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "search_code",
                        "arguments": '{"query":"value","path":null}',
                    }
                ],
            )
        ]
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(responses=responses),
        repository_root=tmp_path,
        provider="openai",
        model="gpt-5.6",
    )
    state = ReviewState(stage="verify")

    message = reviewer._request(
        [
            {"role": "system", "content": "Return JSON"},
            {"role": "user", "content": "Verify candidate"},
        ],
        state,
        allow_tools=True,
    )

    assert responses.requests[0]["tools"][0]["name"] == "read_file"
    assert responses.requests[0]["tools"][1]["name"] == "search_code"
    assert responses.requests[0]["tools"][0]["strict"] is True
    assert message.tool_calls[0].function.name == "search_code"


def test_reviewer_retries_transient_api_error_without_using_model_turn(
    tmp_path, monkeypatch, capsys
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
    assert reviewer.last_state.model_turns == 6
    assert reviewer.last_state.api_attempts == 7
    request_errors = [
        event
        for event in reviewer.last_trace
        if event["type"] == "model_request_error"
    ]
    assert len(request_errors) == 1
    assert request_errors[0]["stage"] == "discover"
    assert request_errors[0]["attempt"] == 1
    assert request_errors[0]["will_retry"] is True
    assert request_errors[0]["error_type"] == "TemporaryAPIError"
    assert request_errors[0]["error"] == "temporary gateway failure"
    assert request_errors[0]["elapsed_seconds"] >= 0
    stderr = capsys.readouterr().err
    assert "model request stage=discover turn=1 attempt=1/3" in stderr
    assert "model request failed stage=discover" in stderr
    assert "model response stage=discover turn=1 attempt=2" in stderr


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
    assert len(completions.requests) == 7
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
    assert len(completions.requests) == 6
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


def test_verify_rejects_repository_evidence_that_was_not_read():
    candidate = CandidateIssue(
        file="example.py",
        severity="medium",
        claim="Lookup may return nil",
        evidence=[EvidenceRef(side="after", text="record.value")],
    )
    decision = (
        '{"decisions":[{"candidate_index":0,"verdict":"revise",'
        '"basis":"repository","reason":"A different lifecycle creates rows",'
        '"supporting_evidence":[{"source":"repository",'
        '"file":"models/record.py","text":"records are created on visit"}],'
        '"issue":{"file":"example.py","severity":"medium",'
        '"description":"Lookup may return nil before a visit",'
        '"suggestion":"Handle the missing record"}}]}'
    )

    with pytest.raises(ValueError, match="successful read_file result"):
        OpenAIReviewer._parse_decisions(
            decision,
            [candidate],
            repository_context_available=True,
            expected_basis="repository",
            code_changes="File: example.py\nPatch:\n+record.value",
            repository_sources=[
                {
                    "ok": True,
                    "path": "controllers/example.py",
                    "content": "authentication is required\n",
                }
            ],
            require_supporting_evidence=True,
        )


def test_verify_accepts_exact_repository_supporting_evidence():
    candidate = CandidateIssue(
        file="example.py",
        severity="medium",
        claim="Lookup may return nil",
        evidence=[EvidenceRef(side="after", text="record.value")],
    )
    decision = (
        '{"decisions":[{"candidate_index":0,"verdict":"revise",'
        '"basis":"repository","reason":"Rows are only created on visit",'
        '"supporting_evidence":[{"source":"repository",'
        '"file":"models/record.py","text":"create_record_on_visit(user)"}],'
        '"issue":{"file":"example.py","severity":"medium",'
        '"description":"Lookup may return nil before a visit",'
        '"suggestion":"Handle the missing record"}}]}'
    )

    verified, decisions = OpenAIReviewer._parse_decisions(
        decision,
        [candidate],
        repository_context_available=True,
        expected_basis="repository",
        code_changes="File: example.py\nPatch:\n+record.value",
        repository_sources=[
            {
                "ok": True,
                "path": "models/record.py",
                "content": "def visit(user):\n    create_record_on_visit(user)\n",
            }
        ],
        require_supporting_evidence=True,
    )

    assert len(verified) == 1
    assert decisions[0]["verdict"] == "revise"


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
    retry_messages = completions.requests[5]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "Invalid VERIFY output" in retry_messages[-1]["content"]
    assert not any(
        message["role"] == "assistant" and not message.get("content")
        for message in retry_messages
        if "tool_calls" not in message
    )


def test_reviewer_reserves_tool_free_turn_to_finalize_verify(tmp_path):
    candidate = (
        '{"file":"example.py","severity":"low",'
        '"claim":"Changed behavior","evidence":'
        '[{"side":"after","text":"value = changed()"}],'
        '"required_facts":[]}'
    )
    valid_decision = (
        '{"decisions":[{"candidate_index":0,"verdict":"keep",'
        '"basis":"diff","reason":"The changed call has a concrete impact",'
        '"supporting_evidence":[{"source":"diff",'
        '"file":"example.py","side":"after",'
        '"text":"value = changed()"}],'
        '"issue":{"file":"example.py","severity":"low",'
        '"description":"Changed behavior",'
        '"suggestion":"Handle the changed behavior"}}]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response("{}"),
            response("{}"),
            response("{}"),
            response("{}"),
            response(valid_decision),
            response('{"status":"complete","summary":"One issue found"}'),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    result = reviewer.review(
        [{"filename": "example.py", "patch": "+value = changed()"}]
    )

    assert len(result.issues) == 1
    finalization_request = completions.requests[-2]
    assert "tools" not in finalization_request
    assert "Verification tool and exploration budget is exhausted" in (
        finalization_request["messages"][-1]["content"]
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
                '"supporting_evidence":[{"source":"diff",'
                '"file":"second.py","side":"after","text":"second()"}],'
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
    assert len(completions.requests) == 8
    assert '"claim": "First claim"' in completions.requests[5]["messages"][1]["content"]
    assert '"claim": "Second claim"' in completions.requests[6]["messages"][1]["content"]
    # Shared changes come before candidate text so the cached prefix is reused.
    first_verify = completions.requests[5]["messages"]
    second_verify = completions.requests[6]["messages"]
    assert first_verify[0] == second_verify[0]
    prefix = first_verify[1]["content"].split("Candidate:")[0]
    assert "+first()" in prefix and "+second()" in prefix
    assert second_verify[1]["content"].startswith(prefix)
    discover_requests = completions.requests[:4]
    discover_prefix = discover_requests[0]["messages"][1]["content"].split("Discovery pass:")[0]
    assert "+first()" in discover_prefix
    for request in discover_requests:
        assert request["messages"][0] == discover_requests[0]["messages"][0]
        assert request["messages"][1]["content"].startswith(discover_prefix)
    decisions = [
        event
        for event in reviewer.last_trace
        if event["type"] == "candidate_result"
    ]
    assert [decision["candidate_index"] for decision in decisions] == [0, 1]
    assert [decision["basis"] for decision in decisions] == ["diff", "diff"]


def test_discover_merges_independent_correctness_and_state_passes(tmp_path):
    correctness = (
        '{"file":"example.py","severity":"high","claim":"Null dereference",'
        '"evidence":[{"file":"example.py","side":"after",'
        '"text":"record.value"}],"required_facts":[]}'
    )
    state = (
        '{"file":"example.py","severity":"high","claim":"Concurrent lost update",'
        '"evidence":[{"file":"example.py","side":"after",'
        '"text":"record.value"}],"required_facts":[]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{correctness}]}}'),
            response(f'{{"candidates":[{state}]}}'),
            response('{"candidates":[]}'),
            response('{"candidates":[]}'),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"rejected",'
                '"basis":"diff","reason":"Not supported","issue":null}]}'
            ),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"rejected",'
                '"basis":"diff","reason":"Not supported","issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ],
        auto_empty_state_pass=False,
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path,
        model="test",
    )

    reviewer.review([{"filename": "example.py", "patch": "+record.value"}])

    assert [candidate.claim for candidate in reviewer.last_state.candidates] == [
        "Null dereference",
        "Concurrent lost update",
    ]
    assert "Discovery pass: correctness" in completions.requests[0]["messages"][1]["content"]
    assert "Discovery pass: state_and_concurrency" in completions.requests[1]["messages"][1]["content"]
    assert "Discovery pass: behavioral_consistency" in completions.requests[2]["messages"][1]["content"]
    assert "Discovery pass: tests_quality" in completions.requests[3]["messages"][1]["content"]
    discover_result = next(
        event
        for event in reviewer.last_trace
        if event["type"] == "stage_result" and event["stage"] == "discover"
    )
    assert discover_result["pass_candidate_counts"] == {
        "correctness": 1,
        "state_and_concurrency": 1,
        "behavioral_consistency": 0,
        "tests_quality": 0,
    }


def test_semantic_deduplication_merges_evidence_and_required_facts(tmp_path):
    candidates = [
        CandidateIssue(
            file="worker.py",
            severity="high",
            claim="Concurrent requests lose an update",
            evidence=[EvidenceRef(file="worker.py", side="after", text="save()")],
            required_facts=[
                RequiredFact(
                    question="Is this transactional?",
                    source="repository",
                    path="worker.py",
                )
            ],
        ),
        CandidateIssue(
            file="worker.py",
            severity="medium",
            claim="The check-then-write path races",
            evidence=[EvidenceRef(file="worker.py", side="after", text="check()")],
            required_facts=[],
        ),
    ]
    completions = FakeCompletions(
        [
            response(
                '{"groups":[{"candidate_indices":[0,1],'
                '"representative_index":0,"priority":5,'
                '"reason":"Same lost-update race"}]}'
            )
        ],
        auto_identity_deduplication=False,
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path,
        model="test",
    )

    deduplicated = reviewer._deduplicate_candidates(
        candidates, ReviewState(stage="deduplicate")
    )

    assert len(deduplicated) == 1
    assert deduplicated[0].claim == "Concurrent requests lose an update"
    assert [reference.text for reference in deduplicated[0].evidence] == [
        "save()",
        "check()",
    ]
    assert deduplicated[0].required_facts[0].question == "Is this transactional?"


def test_semantic_deduplication_requires_complete_partition():
    output = (
        '{"groups":[{"candidate_indices":[0],"representative_index":0,'
        '"priority":3,"reason":"First only"}]}'
    )

    with pytest.raises(ValueError, match="include every candidate"):
        OpenAIReviewer._parse_deduplication_groups(output, candidate_count=2)


def test_semantic_deduplication_ranks_groups_before_truncation(tmp_path):
    candidates = [
        CandidateIssue(
            file="example.py",
            severity="low",
            claim="Conditional concern",
            evidence=[EvidenceRef(file="example.py", side="after", text="run()")],
        ),
        CandidateIssue(
            file="example.py",
            severity="medium",
            claim="Concrete failure",
            evidence=[EvidenceRef(file="example.py", side="after", text="run()")],
        ),
    ]
    completions = FakeCompletions(
        [
            response(
                '{"groups":['
                '{"candidate_indices":[0],"representative_index":0,'
                '"priority":1,"reason":"Conditional"},'
                '{"candidate_indices":[1],"representative_index":1,'
                '"priority":5,"reason":"Concrete"}]}'
            )
        ],
        auto_identity_deduplication=False,
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path,
        model="test",
    )

    ranked = reviewer._deduplicate_candidates(
        candidates, ReviewState(stage="deduplicate")
    )

    assert [candidate.claim for candidate in ranked] == [
        "Concrete failure",
        "Conditional concern",
    ]


def test_semantic_deduplication_requires_priority():
    output = (
        '{"groups":[{"candidate_indices":[0],"representative_index":0,'
        '"reason":"Missing priority"}]}'
    )

    with pytest.raises(ValueError, match="priority"):
        OpenAIReviewer._parse_deduplication_groups(output, candidate_count=1)


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


def test_unresolved_fact_recovers_with_model_directed_read(tmp_path):
    source = tmp_path / "controller.rb"
    source.write_text("before_filter :ensure_logged_in\n", encoding="utf-8")
    candidate = (
        '{"file":"controller.rb","severity":"high","claim":"Missing auth",'
        '"evidence":[{"side":"after","text":"def unsubscribe"}],'
        '"required_facts":[{"question":"Is authentication required?",'
        '"source":"repository","query":"authenticate_user"}]}'
    )
    recovery_read = SimpleNamespace(
        id="read-recovery",
        function=SimpleNamespace(
            name="read_file",
            arguments='{"path":"controller.rb"}',
        ),
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response(tool_calls=[recovery_read]),
            response(
                '{"decisions":[{"candidate_index":0,"verdict":"rejected",'
                '"basis":"repository","reason":"Authentication is present",'
                '"issue":null}]}'
            ),
            response('{"status":"complete","summary":"No issues"}'),
        ]
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path,
        model="test",
    )

    result = reviewer.review(
        [{"filename": "controller.rb", "patch": "+def unsubscribe"}]
    )

    assert result.issues == []
    tool_results = [
        event for event in reviewer.last_trace if event["type"] == "tool_result"
    ]
    assert [event["name"] for event in tool_results] == [
        "search_code",
        "read_file",
    ]
    assert tool_results[1]["origin"] == "model"
    decision = next(
        event for event in reviewer.last_trace if event["type"] == "candidate_result"
    )
    assert decision["verdict"] == "rejected"


def test_unresolved_fact_becomes_inconclusive_after_context_turn_budget(tmp_path):
    candidate = (
        '{"file":"controller.rb","severity":"high","claim":"Missing auth",'
        '"evidence":[{"side":"after","text":"def unsubscribe"}],'
        '"required_facts":[{"question":"Is authentication required?",'
        '"source":"repository","query":"authenticate_user"}]}'
    )
    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate}]}}'),
            response('{"status":"still_searching"}'),
            response('{"status":"still_searching"}'),
            response('{"status":"still_searching"}'),
            response('{"status":"complete","summary":"No verified issues"}'),
        ]
    )
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path,
        model="test",
    )

    result = reviewer.review(
        [{"filename": "controller.rb", "patch": "+def unsubscribe"}]
    )

    assert result.issues == []
    decision = next(
        event for event in reviewer.last_trace if event["type"] == "candidate_result"
    )
    assert decision["verdict"] == "inconclusive"
    assert decision["unresolved_fact_indices"] == [0]
    assert decision["stage"] == "acquire_context"
    assert all(
        "Stage: VERIFY" not in request["messages"][0]["content"]
        for request in completions.requests
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

    with pytest.raises(ValueError, match="evidence file diff"):
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

    with pytest.raises(ValueError, match="evidence file diff"):
        OpenAIReviewer._parse_candidates(output, changes)


def test_discover_accepts_cross_file_evidence():
    output = (
        '{"candidates":[{"file":"config/routes.rb","severity":"high",'
        '"claim":"A GET route mutates state","evidence":['
        '{"file":"config/routes.rb","side":"after",'
        '"text":"get unsubscribe"},'
        '{"file":"app/controllers/topics_controller.rb","side":"after",'
        '"text":"record.save!"}],"required_facts":[]}]} '
    )
    changes = (
        "File: config/routes.rb\nPatch:\n+get unsubscribe\n\n"
        "File: app/controllers/topics_controller.rb\nPatch:\n+record.save!"
    )

    candidates = OpenAIReviewer._parse_candidates(output, changes)

    assert [reference.file for reference in candidates[0].evidence] == [
        "config/routes.rb",
        "app/controllers/topics_controller.rb",
    ]


def test_discover_requires_candidate_file_in_cross_file_evidence():
    output = (
        '{"candidates":[{"file":"config/routes.rb","severity":"high",'
        '"claim":"A GET route mutates state","evidence":['
        '{"file":"app/controllers/topics_controller.rb","side":"after",'
        '"text":"record.save!"}],"required_facts":[]}]} '
    )
    changes = (
        "File: config/routes.rb\nPatch:\n+get unsubscribe\n\n"
        "File: app/controllers/topics_controller.rb\nPatch:\n+record.save!"
    )

    with pytest.raises(ValueError, match="Candidate file must appear"):
        OpenAIReviewer._parse_candidates(output, changes)


def verification_candidate(path="example.py", repository=False):
    return CandidateIssue(
        file=path, severity="medium", claim="Changed behavior",
        evidence=[EvidenceRef(side="after", text="changed()")],
        required_facts=[RequiredFact(question="Check definition", source="repository", path=path)] if repository else [],
    )


def verification_decision(path="example.py", basis="diff", verdict="keep"):
    return json.dumps({"decisions": [{
        "candidate_index": 0, "verdict": verdict, "basis": basis,
        "reason": "Checked the available evidence",
        "supporting_evidence": [{"source": basis, "file": path, "side": "after", "text": "changed()"}],
        "issue": {"file": path, "severity": "medium", "description": "Changed behavior", "suggestion": "Fix behavior"}
        if verdict in {"keep", "revise"} else None,
    }]})


def verification_reviewer(tmp_path, responses):
    completions = FakeCompletions(responses)
    reviewer = OpenAIReviewer(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        repository_root=tmp_path, model="test",
    )
    return reviewer, completions


def test_repository_finalization_accepts_inconclusive_without_switching_basis(tmp_path):
    reviewer, completions = verification_reviewer(tmp_path, [
        *[response("{}") for _ in range(4)],
        response(verification_decision(basis="repository", verdict="inconclusive")),
    ])
    state = ReviewState(stage="verify")
    issues, decision = reviewer._verify_candidate(
        "File: example.py\nPatch:\n+changed()", verification_candidate(repository=True),
        0, state, repository_context=[], initial_successful_tool_calls=1,
    )
    assert issues == []
    assert decision["verdict"] == "inconclusive"
    assert "failure_kind" not in decision
    final_request = completions.requests[-1]
    assert "tools" not in final_request
    assert "Keep basis=repository" in final_request["messages"][-1]["content"]
    assert "use basis=diff" not in final_request["messages"][-1]["content"]
    with pytest.raises(ValueError, match="basis must be repository"):
        reviewer._parse_decisions(
            verification_decision(basis="diff"), [verification_candidate(repository=True)],
            expected_basis="repository", repository_context_available=True,
        )
    invalid = json.loads(verification_decision())
    invalid["decisions"][0]["verdict"] = "inconclusive"
    with pytest.raises(ValueError, match="null issue"):
        reviewer._parse_decisions(json.dumps(invalid), [verification_candidate()])


def test_textual_tool_arguments_require_a_formal_call_before_execution(tmp_path):
    (tmp_path / "example.py").write_text("changed()\n")
    call = SimpleNamespace(id="formal-read", function=SimpleNamespace(
        name="read_file", arguments='{"path":"example.py"}',
    ))
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"path":"example.py","line":1,"context_lines":0}'),
        response(tool_calls=[call]),
        response(verification_decision(basis="repository")),
    ])
    state = ReviewState(stage="verify")
    issues, decision = reviewer._verify_candidate(
        "File: example.py\nPatch:\n+changed()", verification_candidate(repository=True),
        3, state, repository_context=[], initial_successful_tool_calls=0,
    )
    assert len(issues) == 1
    assert decision["candidate_index"] == 3
    assert state.tool_calls == 1
    assert "Nothing was executed" in completions.requests[1]["messages"][-1]["content"]
    results = [e for e in state.trace if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "formal-read"
    decisions = [e for e in state.trace if e["type"] == "tool_gateway_decision"]
    assert len(decisions) == 1 and decisions[0]["allowed"] is True
    assert len(completions.requests) == 3


@pytest.mark.parametrize("output", [
    '{"query":"symbol","line":1}', '{"path":"example.py","command":"rm"}',
    '{"path":42}', '{"path":"example.py","line":true}',
    '{"decisions":[],"path":"example.py"}', 'not JSON',
])
def test_textual_tool_detector_rejects_ambiguous_or_invalid_requests(output):
    assert OpenAIReviewer._text_tool_name(output) is None


@pytest.mark.parametrize("final_response", [
    response('{"path":"example.py"}'),
    response(tool_calls=[SimpleNamespace(id="late-tool", function=SimpleNamespace(name="read_file", arguments='{"path":"example.py"}'))]),
])
def test_finalization_never_executes_tools_even_if_model_requests_them(tmp_path, final_response):
    reviewer, _ = verification_reviewer(tmp_path, [*[response("{}") for _ in range(4)], final_response, final_response])
    state = ReviewState(stage="verify")
    issues, decision = reviewer._verify_candidate(
        "File: example.py\nPatch:\n+changed()", verification_candidate(),
        0, state, repository_context=[], initial_successful_tool_calls=0,
    )
    assert issues == []
    assert state.tool_calls == 0
    assert decision["failure_kind"] == ("verification_turn_limit" if final_response.choices[0].message.tool_calls else "tool_protocol_error")


def test_repeated_textual_tool_arguments_are_inconclusive_without_any_execution(tmp_path):
    (tmp_path / "example.py").write_text("changed()\n")
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"path":"example.py"}'), response('{"query":"changed"}'),
    ])
    state = ReviewState(stage="verify")
    issues, decision = reviewer._verify_candidate("", verification_candidate(), 0, state, [], 0)
    assert issues == []
    assert decision["verdict"] == "inconclusive"
    assert decision["failure_kind"] == "tool_protocol_error"
    assert state.tool_calls == 0
    assert not any(e["type"] in {"tool_result", "tool_gateway_decision"} for e in state.trace)
    assert len(completions.requests) == 2


def test_workflow_reads_pass_through_gateway_and_do_not_expose_sensitive_files(tmp_path):
    (tmp_path / ".env").write_text("SYNTHETIC_SECRET_ONLY=example\n")
    reviewer, _ = verification_reviewer(tmp_path, [])
    state = ReviewState(stage="acquire_context")
    output, succeeded = reviewer._execute_workflow_tool(
        "read_file", '{"path":".env"}', 0, 0, state,
    )
    assert not succeeded
    assert "SYNTHETIC_SECRET_ONLY" not in output
    decision = next(e for e in state.trace if e["type"] == "tool_gateway_decision")
    assert decision["origin"] == "workflow"
    assert decision["allowed"] is False
    assert decision["code"] == "path_denied"


def test_candidate_turn_exhaustion_preserves_prior_issues_and_continues_review(tmp_path):
    from dataclasses import asdict
    candidates = [verification_candidate(p) for p in ("first.py", "failed.py", "last.py")]
    reviewer, _ = verification_reviewer(tmp_path, [
        response(json.dumps({"candidates": [asdict(c) for c in candidates]})),
        response(verification_decision("first.py")),
        *[response('{"decisions":[]}') for _ in range(6)],
        response(verification_decision("last.py")),
        response('{"status":"complete","summary":"Two verified issues"}'),
    ])
    result = reviewer.review([{"filename": c.file, "patch": "+changed()"} for c in candidates])
    assert result.status == "complete"
    assert [i.file for i in result.issues] == ["first.py", "last.py"]
    decisions = [e for e in reviewer.last_trace if e["type"] == "candidate_result"]
    assert [d["verdict"] for d in decisions] == ["keep", "inconclusive", "keep"]
    assert decisions[1]["failure_kind"] == "verification_turn_limit"
    assert "one decision per candidate" in decisions[1]["last_validation_error"]


def test_candidate_recovery_does_not_swallow_api_errors(tmp_path):
    reviewer, _ = verification_reviewer(tmp_path, [RuntimeError("Provider unavailable")])
    state = ReviewState(stage="verify")
    with pytest.raises(RuntimeError, match="Provider unavailable"):
        reviewer._verify_candidate("", verification_candidate(), 0, state, [], 0)
    assert not any(e["type"] == "candidate_result" for e in state.trace)


@pytest.mark.parametrize('structured', [True, False])
def test_kimi_wire_response_preserves_tool_calls_through_real_sdk_and_reviewer(tmp_path, structured):
    import httpx
    from openai import OpenAI
    wire_message = {
        'role': 'assistant',
        'content': None if structured else '{"tool_calls":[{"name":"read_file","arguments":{"path":"example.py"}}]}',
        'reasoning_content': 'Synthetic provider metadata',
    }
    wire_calls = [{
        'id': 'call-wire-test', 'type': 'function',
        'function': {'name': 'read_file', 'arguments': '{"path":"example.py","line":1,"context_lines":0}'},
    }] if structured else []
    if structured:
        wire_message['tool_calls'] = wire_calls
    wire = {
        'id': 'chatcmpl-wire-test', 'object': 'chat.completion', 'created': 1,
        'model': 'kimi-k3', 'choices': [{
            'index': 0, 'message': wire_message,
            'finish_reason': 'tool_calls' if structured else 'stop',
        }],
    }
    def transport(request):
        sent = json.loads(request.content)
        assert sent['tools'][0]['function']['name'] == 'read_file'
        return httpx.Response(200, json=wire)
    with OpenAI(api_key='synthetic-test-key', base_url='https://provider.invalid/v1',
                http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        reviewer = OpenAIReviewer(client=client, provider='kimi', model='kimi-k3', repository_root=tmp_path)
        state = ReviewState(stage='verify')
        message = reviewer._request([{'role':'system', 'content':'Use tools or JSON.'}], state, allow_tools=True)
        assert len(message.tool_calls or []) == len(wire_calls)
        assert state.trace[-1]['tool_calls'] == [
            {'id':c['id'], 'name':c['function']['name'], 'arguments':c['function']['arguments']} for c in wire_calls
        ]
        assert message.content == wire_message['content'] == state.trace[-1]['content']
        assert message.reasoning_content == wire_message['reasoning_content']


@pytest.mark.parametrize('content', [
    '{"tool_calls":[{"name":"search_code","arguments":{"query":"sender"}}]}',
    '{"tool_calls":[{"function":{"name":"read_file","arguments":"{}"}}]}',
    '{"tool_calls":[{"query":"sender","path":"source.py"}]}',
    '{"tool_calls":1}',
    '{"name":"functions.search_code","arguments":{"query":"sender"}}',
    '{"function_call":{"name":"read_file","arguments":"{}"}}',
])
def test_text_tool_envelopes_are_protocol_errors_not_executable(content):
    assert OpenAIReviewer._has_text_tool_proposal(content)


@pytest.mark.parametrize('content', [
    '{"decisions":[{"reason":"tool_calls is just a word in evidence"}]}',
    '{"status":"inconclusive","reason":"Missing context"}',
    '{"source":"diff","text":"example()"}',
    'Call read_file please',
])
def test_normal_review_text_is_not_misclassified_as_tool_proposal(content):
    assert not OpenAIReviewer._has_text_tool_proposal(content)


def unresolved_protocol_fact():
    fact = RequiredFact(question='Find definition', source='repository', query='not_found_symbol')
    context = {'required_fact': {}, 'search': {'ok':True, 'matches':[]}, 'read':None, 'resolved':False, 'tool_calls':0}
    return fact, context


@pytest.mark.parametrize('retry_content', ['{"tool_calls":1}', '{}', 'not JSON'])
def test_context_protocol_retry_failure_stops_without_executing_text(tmp_path, retry_content):
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"tool_calls":[{"name":"read_file","arguments":{"path":"example.py"}}]}'),
        response(retry_content),
    ])
    state = ReviewState(stage='acquire_context')
    fact, context = unresolved_protocol_fact()
    count = reviewer._recover_required_fact(verification_candidate(), fact, 0, 0, context, state)
    assert count == 0 and state.tool_calls == 0
    assert not context['resolved'] and context['failure_kind'] == 'tool_protocol_error'
    assert len(completions.requests) == 2
    feedback = completions.requests[1]['messages'][-1]['content']
    assert 'Nothing was executed' in feedback and 'No new context was obtained' in feedback
    assert not any(e['type'] in {'tool_result', 'tool_gateway_decision'} for e in state.trace)
    assert [e['retry_allowed'] for e in state.trace if e['type']=='tool_protocol_error'] == [True, False]


def test_context_protocol_retry_can_produce_a_native_gateway_call(tmp_path):
    (tmp_path/'example.py').write_text('changed()\n')
    native = SimpleNamespace(id='native-after-correction', function=SimpleNamespace(name='read_file', arguments='{"path":"example.py"}'))
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"tool_calls":1}'), response(tool_calls=[native]),
    ])
    state = ReviewState(stage='acquire_context')
    fact, context = unresolved_protocol_fact()
    count = reviewer._recover_required_fact(verification_candidate(), fact, 0, 0, context, state)
    assert count == state.tool_calls == 1
    assert context['resolved'] and 'failure_kind' not in context
    decisions = [e for e in state.trace if e['type']=='tool_gateway_decision']
    assert len(decisions) == 1 and decisions[0]['allowed']
    assert decisions[0]['tool_call_id'] == 'native-after-correction'


def test_context_protocol_retry_can_stop_as_inconclusive(tmp_path):
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"tool_calls":1}'),
        response('{"status":"inconclusive","reason":"Definition unavailable"}'),
    ])
    state = ReviewState(stage='acquire_context')
    fact, context = unresolved_protocol_fact()
    reviewer._recover_required_fact(verification_candidate(), fact, 0, 0, context, state)
    assert not context['resolved'] and state.tool_calls == 0
    assert context['stop_reason'] == 'Definition unavailable'
    assert len(completions.requests) == 2


def test_protocol_failure_stops_later_facts_and_cannot_be_summarized_as_no_issues(tmp_path):
    from dataclasses import asdict
    (tmp_path/'later.py').write_text('changed()\n')
    candidate = verification_candidate()
    candidate.required_facts = [
        RequiredFact(question='Missing definition', source='repository', query='not_found_symbol'),
        RequiredFact(question='Later context', source='repository', path='later.py'),
    ]
    reviewer, completions = verification_reviewer(tmp_path, [
        response(json.dumps({'candidates':[asdict(candidate)]})),
        response('{"tool_calls":[{"name":"read_file","arguments":{"path":"later.py"}}]}'),
        response('{"tool_calls":1}'),
        response('{"status":"complete","summary":"No issues found"}'),
    ])
    result = reviewer.review([{'filename':'example.py','patch':'+changed()'}])
    assert result.issues == []
    assert 'tool_protocol_error' in result.summary and 'Context insufficient' in result.summary
    assert 'No issues found' not in result.summary
    decision = next(e for e in reviewer.last_trace if e['type']=='candidate_result')
    assert decision['verdict'] == 'inconclusive' and decision['failure_kind'] == 'tool_protocol_error'
    assert decision['unresolved_fact_indices'] == [0,1]
    results = [e for e in reviewer.last_trace if e['type']=='tool_result']
    assert len(results)==1 and results[0]['name']=='search_code'


def test_verify_protocol_retry_failure_stops_even_if_second_response_is_not_tool_shaped(tmp_path):
    reviewer, completions = verification_reviewer(tmp_path, [response('{"tool_calls":1}'), response('{}')])
    state = ReviewState(stage='verify')
    issues, decision = reviewer._verify_candidate('', verification_candidate(), 0, state, [], 0)
    assert issues == [] and decision['failure_kind'] == 'tool_protocol_error'
    assert len(completions.requests)==2 and state.tool_calls==0


def test_verify_native_calls_take_precedence_over_textual_imitation(tmp_path):
    (tmp_path/'example.py').write_text('changed()\n')
    native = SimpleNamespace(id='native-valid', function=SimpleNamespace(name='read_file',arguments='{"path":"example.py"}'))
    reviewer, completions = verification_reviewer(tmp_path, [
        response('{"tool_calls":1}', tool_calls=[native]),
        response(verification_decision(basis='repository')),
    ])
    state = ReviewState(stage='verify')
    issues, decision = reviewer._verify_candidate('File: example.py\nPatch:\n+changed()', verification_candidate(repository=True), 0, state, [], 0)
    assert len(issues)==1 and state.tool_calls==1
    assert not any(e['type']=='tool_protocol_error' for e in state.trace)


@pytest.mark.parametrize(
    "output",
    ['```json\n{"status": "complete"}\n```', '```\n{"status": "complete"}\n```'],
)
def test_tool_turn_output_accepts_fenced_json(output):
    assert OpenAIReviewer._parse_json_object(output) == {"status": "complete"}


def test_tool_turn_output_accepts_prose_before_fenced_json():
    output = 'The claim holds.\n\n```json\n{"status": "complete"}\n```'
    assert OpenAIReviewer._parse_json_object(output) == {"status": "complete"}


def test_verify_finalizes_immediately_when_tool_budget_is_spent(tmp_path):
    reviewer, completions = verification_reviewer(tmp_path, [
        response(verification_decision(verdict="inconclusive")),
    ])
    state = ReviewState(stage="verify", tool_calls=reviewer.max_tool_calls)
    issues, decision = reviewer._verify_candidate(
        "File: example.py\nPatch:\n+changed()", verification_candidate(), 0, state, [], 0,
    )
    assert issues == []
    assert decision["verdict"] == "inconclusive"
    assert len(completions.requests) == 1
    request = completions.requests[0]
    assert "tools" not in request
    assert "budget is exhausted" in request["messages"][-1]["content"]


def test_discover_only_returns_unverified_candidates_with_configured_limits(tmp_path):
    candidate = (
        '{"file":"example.py","severity":"high",'
        '"claim":"Possible division by zero",'
        '"evidence":[{"side":"after","text":"return a / b"}],'
        '"required_facts":[]}'
    )
    completions = FakeCompletions([response(f'{{"candidates":[{candidate}]}}')])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client,
        repository_root=tmp_path,
        model="test",
        candidates_per_pass=3,
        max_candidates=8,
    )

    result = reviewer.discover_only(
        [{"filename": "example.py", "patch": "+    return a / b"}]
    )

    assert result.status == "complete"
    assert [issue.description for issue in result.issues] == ["Possible division by zero"]
    assert "Return at most 3 candidates" in completions.requests[0]["messages"][0]["content"]
    assert not any("Stage: VERIFY" in r["messages"][0]["content"] for r in completions.requests)


def test_reviewer_rejects_limits_above_supported_maximum(tmp_path):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))
    with pytest.raises(ValueError):
        OpenAIReviewer(
            client=client,
            repository_root=tmp_path,
            model="test",
            candidates_per_pass=MAX_CANDIDATES_PER_DISCOVERY_PASS + 1,
        )


def test_reviewer_runs_only_selected_discovery_passes(tmp_path):
    completions = FakeCompletions([response('{"candidates":[]}')], auto_empty_state_pass=False)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client, repository_root=tmp_path, model="test", discovery_passes=["correctness"]
    )

    result = reviewer.discover_only([{"filename": "example.py", "patch": "+x = 1"}])

    assert result.issues == []
    assert len(completions.requests) == 1
    assert "Discovery pass: correctness" in completions.requests[0]["messages"][1]["content"]
    with pytest.raises(ValueError):
        OpenAIReviewer(client=client, repository_root=tmp_path, model="test", discovery_passes=["missing"])


def test_discovery_samples_repeat_passes_and_merge_candidates(tmp_path):
    def candidate(claim):
        return (
            f'{{"file":"example.py","severity":"medium","claim":"{claim}",'
            '"evidence":[{"file":"example.py","side":"after","text":"return a / b"}],'
            '"required_facts":[]}'
        )

    completions = FakeCompletions(
        [
            response(f'{{"candidates":[{candidate("Division by zero")}]}}'),
            response(f'{{"candidates":[{candidate("Integer division truncates")}]}}'),
            response(
                '{"groups":['
                '{"candidate_indices":[0],"representative_index":0,"priority":5,"reason":"a"},'
                '{"candidate_indices":[1],"representative_index":1,"priority":4,"reason":"b"}]}'
            ),
        ],
        auto_empty_state_pass=False,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client,
        repository_root=tmp_path,
        model="test",
        discovery_passes=["correctness"],
        discovery_samples=2,
    )

    result = reviewer.discover_only([{"filename": "example.py", "patch": "+    return a / b"}])

    assert [issue.description for issue in result.issues] == [
        "Division by zero",
        "Integer division truncates",
    ]
    # Both samples send the same prompt so the second reuses the cached prefix.
    assert completions.requests[0]["messages"] == completions.requests[1]["messages"]
    assert reviewer.max_candidates == 2 * MAX_CANDIDATES
    stage = next(e for e in reviewer.last_trace if e.get("type") == "stage_result" and e["stage"] == "discover")
    assert stage["pass_candidate_counts"] == {"correctness": 1, "correctness#2": 1}


def test_discovery_samples_are_bounded(tmp_path):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))
    with pytest.raises(ValueError):
        OpenAIReviewer(client=client, repository_root=tmp_path, model="test", discovery_samples=0)
    with pytest.raises(ValueError):
        OpenAIReviewer(
            client=client, repository_root=tmp_path, model="test",
            discovery_samples=2, max_candidates=2 * MAX_CANDIDATES + 1,
        )


@pytest.mark.real_sample_default
def test_default_review_samples_discovery_twice_with_scaled_budgets(tmp_path):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")

    assert reviewer.discovery_samples == 2
    assert reviewer.max_candidates == 2 * MAX_CANDIDATES
    assert reviewer.max_tool_calls == 5 * reviewer.max_candidates


@pytest.mark.parallel_discovery
def test_discovery_warms_cache_then_runs_remaining_passes_concurrently(tmp_path):
    import threading

    later_passes = threading.Barrier(3, timeout=5)
    events = []
    lock = threading.Lock()

    class PassAwareCompletions:
        requests = []

        def create(self, **request):
            content = request["messages"][1]["content"]
            if "Candidates:" in content:
                return response('{"groups":[]}')
            pass_name = content.split("Discovery pass: ", 1)[1].split("\n", 1)[0]
            with lock:
                first = not events
                events.append(("start", pass_name))
            if not first:
                # Three later passes must be in flight together for this to return.
                later_passes.wait()
            with lock:
                events.append(("end", pass_name))
            claim = f"Defect found by {pass_name}"
            return response(
                '{"candidates":[{"file":"example.py","severity":"medium",'
                f'"claim":"{claim}",'
                '"evidence":[{"file":"example.py","side":"after","text":"x = 1"}],'
                '"required_facts":[]}]}'
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=PassAwareCompletions()))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test")
    reviewer.discovery_samples = 1
    reviewer.discovery_passes = reviewer.discovery_passes[:4]
    state = ReviewState()

    reviewer._discover("File: example.py\nPatch:\n+x = 1", state)

    # The first pass finishes before any other starts, so its prefix is cached.
    assert events[:2] == [("start", "correctness"), ("end", "correctness")]
    stage = next(e for e in state.trace if e.get("type") == "stage_result" and e["stage"] == "discover")
    assert list(stage["pass_candidate_counts"]) == [name for name, _ in reviewer.discovery_passes]


def test_invalid_final_decision_gets_one_correction_turn(tmp_path):
    reviewer, completions = verification_reviewer(tmp_path, [
        *[response("{}") for _ in range(4)],
        response(verification_decision().replace('"file": "example.py", "severity"', '"file": "other.py", "severity"')),
        response(verification_decision()),
    ])
    state = ReviewState(stage="verify")
    issues, decision = reviewer._verify_candidate(
        "File: example.py\nPatch:\n+changed()", verification_candidate(),
        0, state, repository_context=[], initial_successful_tool_calls=0,
    )
    assert decision["verdict"] == "keep"
    assert len(issues) == 1
    assert "mention other files in the description" in completions.requests[-1]["messages"][-1]["content"]


def test_unit_discovery_reviews_each_unit_with_surrounding_code(tmp_path):
    (tmp_path / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 81)))
    changes = [
        {"filename": "a.py", "patch": "@@ -10,1 +10,1 @@\n-old\n+line10\n@@ -60,1 +60,1 @@\n-old\n+line60"},
        {"filename": "b.py", "patch": "@@ -0,0 +1,1 @@\n+x = 1"},
    ]
    completions = FakeCompletions(
        [response('{"candidates":[]}') for _ in range(3)], auto_empty_state_pass=False
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = OpenAIReviewer(
        client=client, repository_root=tmp_path, model="test", discovery_mode="units"
    )

    units = reviewer._discovery_units(changes)
    assert len(units) == 3
    assert "Code before the hunk (lines 1-9)" in units[0]
    assert "Code after the hunk (lines 11-26)" in units[0]
    assert "10: line10" not in units[0]  # the changed line is only in the hunk
    assert "Code before" not in units[2]  # b.py is not in the checkout

    reviewer.discover_only(changes)
    contents = [r["messages"][1]["content"] for r in completions.requests]
    assert len(contents) == 3
    assert all("Review only this unit" in c for c in contents)
    # Every unit request shares the diff and the checklist as its cached prefix.
    prefixes = {c.split("Discovery pass:")[0] for c in contents}
    assert len(prefixes) == 1 and "Check every changed line" in prefixes.pop()


def test_discovery_units_are_capped(tmp_path):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))
    reviewer = OpenAIReviewer(client=client, repository_root=tmp_path, model="test", discovery_mode="units")
    changes = [{"filename": f"f{i}.py", "patch": f"@@ -0,0 +1,{i + 1} @@\n" + "+x\n" * (i + 1)} for i in range(30)]
    units = reviewer._discovery_units(changes)
    assert 1 <= len(units) <= 8
    assert sum(u.count("File: ") for u in units) == 30
    with pytest.raises(ValueError):
        OpenAIReviewer(client=client, repository_root=tmp_path, model="test", discovery_mode="files")
