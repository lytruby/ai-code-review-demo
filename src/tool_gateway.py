"""Deterministic authorization for proposed repository tool calls.

The model supplies only a name and arguments. Source, stage, permissions and
budget belong to the workflow. Ordinary assistant text is never executable.
This gateway is an application policy boundary, not an OS sandbox.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from src.tools import ToolAccessError, execute_tool, validate_tool_request


@dataclass(frozen=True)
class ToolProposal:
    name: str
    arguments: str
    call_id: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    allowed: bool
    code: str
    output: str
    charged: bool = False


class ToolGateway:
    def __init__(self, repository_root: Path, max_calls: int):
        self.repository_root = repository_root.resolve()
        self.max_calls = max_calls
        self.calls = 0

    def execute(
        self, proposal: ToolProposal, *, source: str, stage: str,
        tools_allowed: bool = True,
    ) -> ToolOutcome:
        def deny(code: str, message: str, charged: bool = False) -> ToolOutcome:
            return ToolOutcome(False, code, json.dumps({"ok": False, "error": message}), charged)

        if source not in {"model", "workflow"}:
            return deny("invalid_source", "Only formal tool calls or workflow proposals are accepted")
        if not tools_allowed or stage not in {"verify", "acquire_context"}:
            return deny("stage_denied", "Tools are disabled in the current workflow phase")
        if source == "model" and (not isinstance(proposal.call_id, str) or not proposal.call_id):
            return deny("missing_call_id", "A formal model tool call must have a call id")
        if self.calls >= self.max_calls:
            return deny("budget_exhausted", "Tool call limit reached")
        # Invalid authorized-channel proposals also consume the finite budget.
        self.calls += 1
        try:
            if not isinstance(proposal.name, str):
                raise ValueError("Tool name must be a string")
            validate_tool_request(proposal.name, proposal.arguments, self.repository_root)
        except ToolAccessError as error:
            return deny("path_denied", str(error), True)
        except (OSError, ValueError, RuntimeError) as error:
            return deny("invalid_proposal", str(error), True)
        output = execute_tool(proposal.name, proposal.arguments, self.repository_root)
        return ToolOutcome(True, "executed", output, True)
