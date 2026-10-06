import json

import pytest

from src.tool_gateway import ToolGateway, ToolProposal


@pytest.mark.parametrize("source,stage,allowed,call_id,code", [
    ("text", "verify", True, None, "invalid_source"),
    ("model", "verify", True, None, "missing_call_id"),
    ("model", "discover", False, "call", "stage_denied"),
    ("model", "deduplicate", True, "call", "stage_denied"),
    ("workflow", "finalize", True, None, "stage_denied"),
    ("model", "verify", False, "call", "stage_denied"),
])
def test_denied_channel_or_phase_never_reaches_executor(tmp_path, monkeypatch, source, stage, allowed, call_id, code):
    def must_not_execute(*args):
        pytest.fail("Denied proposal reached executor")
    monkeypatch.setattr("src.tool_gateway.execute_tool", must_not_execute)
    gateway = ToolGateway(tmp_path, max_calls=3)
    outcome = gateway.execute(ToolProposal("read_file", '{"path":"source.py"}', call_id),
                              source=source, stage=stage, tools_allowed=allowed)
    assert not outcome.allowed
    assert outcome.code == code
    assert gateway.calls == 0


@pytest.mark.parametrize("name,arguments", [
    ("shell", '{"command":"cat source.py"}'),
    ("read_file", '{"path":"source.py","allowed":true}'),
    ("read_file", '{"path":"source.py","line":true}'),
    ("read_file", '{"path":"source.py","context_lines":500}'),
    ("search_code", '{"query":42}'),
    ("search_code", '{"query":"valid","source":"workflow"}'),
    ("read_file", '[]'),
])
def test_invalid_proposal_cannot_change_permissions_or_reach_executor(tmp_path, monkeypatch, name, arguments):
    (tmp_path / "source.py").write_text("ordinary source\n")
    def must_not_execute(*args):
        pytest.fail("Invalid proposal reached executor")
    monkeypatch.setattr("src.tool_gateway.execute_tool", must_not_execute)
    gateway = ToolGateway(tmp_path, max_calls=3)
    outcome = gateway.execute(ToolProposal(name, arguments, "call"), source="model", stage="verify")
    assert not outcome.allowed
    assert outcome.code == "invalid_proposal"
    assert gateway.calls == 1


@pytest.mark.parametrize("source", ["model", "workflow"])
def test_sensitive_path_denied_before_execution_for_all_sources(tmp_path, monkeypatch, source):
    (tmp_path / ".env").write_text("SYNTHETIC_ONLY=example\n")
    def must_not_execute(*args):
        pytest.fail("Sensitive path reached executor")
    monkeypatch.setattr("src.tool_gateway.execute_tool", must_not_execute)
    gateway = ToolGateway(tmp_path, max_calls=3)
    outcome = gateway.execute(ToolProposal("read_file", '{"path":".env"}', "call"),
                              source=source, stage="acquire_context")
    assert not outcome.allowed and outcome.code == "path_denied"
    assert "SYNTHETIC_ONLY" not in outcome.output


def test_shared_budget_across_workflow_and_model_calls(tmp_path):
    (tmp_path / "source.py").write_text("ordinary source\n")
    gateway = ToolGateway(tmp_path, max_calls=2)
    proposal = ToolProposal("read_file", '{"path":"source.py"}', "call")
    for source in ("workflow", "model"):
        outcome = gateway.execute(proposal, source=source, stage="acquire_context")
        assert outcome.allowed and json.loads(outcome.output)["ok"]
    outcome = gateway.execute(proposal, source="model", stage="verify")
    assert outcome.code == "budget_exhausted"
    assert not outcome.allowed
    assert gateway.calls == 2
