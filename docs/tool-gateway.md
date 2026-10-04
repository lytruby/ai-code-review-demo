# Repository tool gateway

```text
Model formal tool_call ─┐
                       ├─> ToolProposal ─> ToolGateway ─> read_file / search_code
Workflow context read ─┘                       │                   │
                                   allow / deny audit     untrusted tool result
```

The model provides a tool name, JSON arguments and a call id. The workflow
supplies the source, stage and whether tools are currently allowed; these are
not fields the model may set. One gateway instance per review owns the shared
call budget across model and workflow requests. Invalid proposals arriving on
an authorized channel consume the budget too.

The gateway denies unknown sources, missing model call ids, forbidden stages,
exhausted budgets, unregistered tools, invalid argument schemas and disallowed
paths before calling an executor. Ordinary text is never converted to an
executable proposal. Both evidence acquisition and verification recognize JSON
tool-call imitations (including tool_calls envelopes and bare arguments). They
record a protocol anomaly, explicitly state that no tool ran and no new context
was obtained, and allow one correction within the remaining phase budget.
If that retry produces neither native tool calls nor a valid termination, the
candidate's evidence collection stops with tool_protocol_error and inconclusive.
Acquisition can terminate with {"status":"inconclusive","reason":"..."};
verification uses the existing decision schema and evidence checks. The summary
explicitly indicates insufficient context rather than a no-issues conclusion.
Successful execution does not imply that retrieved content
is trusted or that the candidate is correct.

The only registered capabilities are repository reads and fixed-text searches.
There is no shell, write or network capability. Search uses an argument vector
with a fixed `rg` command, not a model-supplied command string. `rg` configuration
is disabled and it does not follow symlinks. Native and fallback searches use
the same sensitive-path policy; the fallback validates every file before reading.

The shared policy is in `src/tools.py`: `.env*`, Git metadata, common credential
locations and private-key suffixes are blocked, as are symlinks (including those
pointing inside the checkout). `.env.example` is deliberately blocked too.
Read windows, search limits and the review's existing tool budget remain in force.
Each gateway decision records source, stage, tool, candidate, allow/deny and a
reason code in the trace. Results from denied calls contain no file contents.

This is an application-level boundary for a stable review checkout. It is not
an OS sandbox, does not defend against another local process changing files
between validation and access, and cannot identify secrets embedded in arbitrary
source or redact the initial PR diff. Keep credentials out of review checkouts.
Running untrusted code or adding write/network tools would require separate
capability policies and stronger isolation.

Benchmark scoring, judge configuration and golden denominators are unchanged.
Candidate failures stay visible; valid issues from other candidates survive.
Use a new run name for live comparisons because the reviewer code has changed.
