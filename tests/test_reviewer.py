import json
from types import SimpleNamespace

import pytest

from src.models import CandidateIssue, EvidenceRef, RequiredFact
from src.reviewer import OpenAIReviewer, ReviewState


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
        if (
            self.auto_empty_state_pass
            and "Discovery pass:" in system_content
            and "Discovery pass: correctness" not in system_content
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
    assert "Return at most 3 candidates" in discover_prompt
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

    verify_after_tool = completions.requests[5]["messages"]
    assert verify_after_tool[-1]["role"] == "tool"
    assert "def divide" in verify_after_tool[-1]["content"]
    assert [event["stage"] for event in reviewer.last_trace if "stage" in event] == [
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
    assert reviewer.last_state.model_turns == 6
    assert reviewer.last_state.api_attempts == 7
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
    assert "Discovery pass: correctness" in completions.requests[0]["messages"][0]["content"]
    assert "Discovery pass: state_and_concurrency" in completions.requests[1]["messages"][0]["content"]
    assert "Discovery pass: behavioral_consistency" in completions.requests[2]["messages"][0]["content"]
    assert "Discovery pass: tests_quality" in completions.requests[3]["messages"][0]["content"]
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
