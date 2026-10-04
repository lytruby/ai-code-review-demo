"""Replay one historical context request and compare wire / SDK / reviewer.

Captures HTTP response body bytes before SDK parsing. Never saves auth headers.
No proposed tools are executed, and no benchmark review or judge is rerun.
"""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import openai
from src.reviewer import OpenAIReviewer, ReviewState

BASELINE = ROOT / 'evals/benchmark-runs/kimi/kimi-k3-gateway-sentry67876-v1/sentry-67876/result.json'


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def reconstruct_messages():
    trace = json.loads(BASELINE.read_text())['trace']
    events = iter(e for e in trace if e['type'] == 'model_response' and e['stage'] in {'discover', 'deduplicate'})
    reviewer = OpenAIReviewer(client=SimpleNamespace(), provider='kimi', repository_root=ROOT)
    def replay(messages, state, allow_tools):
        event = next(events)
        assert event['stage'] == state.stage and not event['tool_calls']
        return SimpleNamespace(content=event['content'], tool_calls=[])
    reviewer._request = replay
    changes = json.loads((ROOT / 'evals/benchmark-data/fixtures/sentry-67876/changes.json').read_text())
    diff = '\n\n'.join(f"File: {c['filename']}\nPatch:\n{c['patch']}" for c in changes)
    candidates = reviewer._discover(diff, ReviewState())
    candidate = candidates[0]
    fact = candidate.required_facts[1]
    search = next(json.loads(e['content']) for e in trace if e['type'] == 'tool_result' and e.get('candidate_index') == 0 and e.get('required_fact_index') == 1)
    context = {'required_fact': asdict(fact), 'search': search, 'read': None, 'resolved': False, 'tool_calls': 1}
    captured = {}
    class Captured(Exception):
        pass
    def intercept(messages, state, allow_tools):
        captured['messages'] = messages
        assert allow_tools
        raise Captured
    reviewer._request = intercept
    try:
        reviewer._recover_required_fact(candidate, fact, 0, 1, context, ReviewState(stage='acquire_context', tool_calls=3))
    except Captured:
        pass
    return captured['messages']


class CaptureCompletions:
    def __init__(self, real, output):
        self.real, self.output = real, output
        self.raw = None
        self.parsed = None

    def create(self, **request):
        write(self.output / 'request.json', request)
        raw_response = self.real.with_raw_response.create(**request)
        body = raw_response.http_response.content
        (self.output / 'response-body.json').write_bytes(body)
        self.raw = json.loads(body)
        write(self.output / 'http-metadata.json', {
            'status_code': raw_response.status_code,
            'request_id': raw_response.http_response.headers.get('x-request-id'),
            'sdk_version': openai.__version__,
        })
        self.parsed = raw_response.parse()
        write(self.output / 'sdk-parsed.json', self.parsed.model_dump(mode='json', exclude_unset=True))
        return self.parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    messages = reconstruct_messages()
    reviewer = OpenAIReviewer(provider='kimi', repository_root=ROOT)
    real_client = reviewer.client
    capture = CaptureCompletions(real_client.chat.completions, args.output)
    reviewer.client = SimpleNamespace(chat=SimpleNamespace(completions=capture))
    reviewer.max_transient_retries = 0
    state = ReviewState(stage='acquire_context', model_turns=5, tool_calls=3)
    try:
        message = reviewer._request(messages, state, allow_tools=True)
        write(args.output / 'reviewer-trace.json', state.trace)
        raw_message = capture.raw['choices'][0]['message']
        raw_calls = raw_message.get('tool_calls') or []
        sdk_calls = [c.model_dump(mode='json', exclude_unset=True) for c in message.tool_calls or []]
        projected = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in raw_calls]
        trace_event = state.trace[-1]
        comparison = {
            'historical_http_response_available': False,
            'request_context': 'Reconstructed first acquire_context call (historical turn 6); fresh API response',
            'sdk_version': openai.__version__,
            'wire_finish_reason': capture.raw['choices'][0].get('finish_reason'),
            'wire_message_keys': list(raw_message),
            'wire_tool_calls_count': len(raw_calls),
            'sdk_tool_calls_count': len(sdk_calls),
            'reviewer_tool_calls_count': len(trace_event['tool_calls']),
            'wire_sdk_tool_calls_equal': raw_calls == sdk_calls,
            'wire_reviewer_tool_calls_equal': projected == trace_event['tool_calls'],
            'wire_sdk_content_equal': raw_message.get('content') == message.content,
            'wire_reviewer_content_equal': raw_message.get('content') == trace_event['content'],
        }
        write(args.output / 'comparison.json', comparison)
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
    finally:
        real_client.close()


if __name__ == '__main__':
    main()
