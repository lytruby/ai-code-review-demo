"""Controlled JSON-mode experiment using an immutable saved API request.

Never executes proposed tools or scores findings. Saves response bytes before
SDK parsing. Only response_format varies; API errors are recorded, not retried.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import openai
from src.reviewer import OpenAIReviewer


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def argument_errors(call, schemas):
    function = call.get('function')
    if not isinstance(function, dict) or function.get('name') not in schemas:
        return ['Unknown or missing function']
    try:
        arguments = json.loads(function['arguments'])
    except (KeyError, TypeError, ValueError):
        return ['Arguments are not a JSON string object']
    if not isinstance(arguments, dict):
        return ['Arguments must be an object']
    schema = schemas[function['name']]
    errors = []
    for name in schema.get('required', []):
        if name not in arguments:
            errors.append(f'Missing required parameter: {name}')
    for name, value in arguments.items():
        if name not in schema['properties']:
            errors.append(f'Unknown parameter: {name}')
            continue
        prop = schema['properties'][name]
        allowed = prop['type'] if isinstance(prop['type'], list) else [prop['type']]
        kind = 'null' if value is None else 'integer' if type(value) is int else 'string' if isinstance(value, str) else 'other'
        if kind not in allowed:
            errors.append(f'Wrong parameter type: {name}')
        elif kind == 'integer':
            if 'minimum' in prop and value < prop['minimum']:
                errors.append(f'Below minimum: {name}')
            if 'maximum' in prop and value > prop['maximum']:
                errors.append(f'Above maximum: {name}')
    # This measures the submitted schema only, not filesystem access or relevance.
    return errors


def classify(body, request):
    choice = body['choices'][0]
    message = choice['message']
    calls = message.get('tool_calls') or []
    schemas = {t['function']['name']: t['function']['parameters'] for t in request['tools']}
    checks = [argument_errors(call, schemas) for call in calls]
    return {
        'finish_reason': choice.get('finish_reason'),
        'native_tool_calls': len(calls),
        'native_tool_response': bool(calls),
        'pseudo_tool_response': not calls and OpenAIReviewer._has_text_tool_proposal(message.get('content') or ''),
        'valid_native_arguments': sum(not errors for errors in checks),
        'argument_errors': checks,
        'truncated': choice.get('finish_reason') == 'length',
    }


def aggregate(rows):
    groups = {}
    for group in ('json_on', 'json_off'):
        selected = [r for r in rows if r['group'] == group]
        successful = [r for r in selected if 'native_tool_calls' in r]
        calls = sum(r['native_tool_calls'] for r in successful)
        valid = sum(r['valid_native_arguments'] for r in successful)
        n = len(successful)
        groups[group] = {
            'attempts': len(selected), 'responses': n, 'errors': len(selected)-n,
            'native_tool_responses': sum(r['native_tool_response'] for r in successful),
            'pseudo_tool_responses': sum(r['pseudo_tool_response'] for r in successful),
            'native_tool_response_rate': sum(r['native_tool_response'] for r in successful)/n if n else None,
            'pseudo_tool_response_rate': sum(r['pseudo_tool_response'] for r in successful)/n if n else None,
            'native_calls': calls, 'valid_native_arguments': valid,
            'native_argument_valid_rate': valid/calls if calls else None,
            'truncated_responses': sum(r['truncated'] for r in successful),
        }
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    original = json.loads(args.request.read_text())
    assert original['response_format'] == {'type':'json_object'}
    args.output.mkdir(parents=True, exist_ok=False)
    common = deepcopy(original)
    common.pop('response_format')
    schedule = [group for index in range(args.repeats) for group in
                (('json_on','json_off') if index % 2 == 0 else ('json_off','json_on'))]
    write(args.output/'manifest.json', {
        'source_request': str(args.request.resolve()), 'source_sha256': digest(original),
        'common_request_sha256': digest(common), 'schedule': schedule,
        'sdk_version': openai.__version__, 'tools_executed': False,
        'argument_metric': 'Strict submitted JSON schema compliance among native calls; N/A when there are no native calls',
        'response_rate_denominator': 'Successful HTTP responses; transport errors separately reported',
    })
    reviewer = OpenAIReviewer(provider='kimi')
    client = reviewer.client.with_options(max_retries=0)
    rows = []
    try:
        for index, group in enumerate(schedule, 1):
            directory = args.output/f'{index:02d}-{group}'
            directory.mkdir()
            request = deepcopy(common)
            if group == 'json_on':
                request['response_format'] = {'type':'json_object'}
            assert digest({k:v for k,v in request.items() if k != 'response_format'}) == digest(common)
            write(directory/'request.json', request)
            row = {'index':index,'group':group}
            print(f'[{index}/{len(schedule)}] {group}', flush=True)
            started = time.monotonic()
            try:
                raw = client.chat.completions.with_raw_response.create(**request)
                body = raw.http_response.content
                (directory/'response-body.json').write_bytes(body)
                write(directory/'http-metadata.json', {'status_code':raw.status_code,'request_id':raw.http_response.headers.get('x-request-id')})
                decoded = json.loads(body)
                parsed = raw.parse()
                write(directory/'sdk-parsed.json', parsed.model_dump(mode='json',exclude_unset=True))
                row.update(classify(decoded, request))
                row['wire_sdk_content_equal'] = decoded['choices'][0]['message'].get('content') == parsed.choices[0].message.content
                row['wire_sdk_tool_calls_equal'] = (decoded['choices'][0]['message'].get('tool_calls') or []) == [c.model_dump(mode='json',exclude_unset=True) for c in parsed.choices[0].message.tool_calls or []]
            except Exception as error:
                row['error_type'] = type(error).__name__
                response = getattr(error,'response',None)
                if response is not None:
                    (directory/'error-response-body.txt').write_bytes(response.content)
                    row['http_status'] = response.status_code
            row['elapsed_seconds'] = round(time.monotonic()-started,2)
            rows.append(row)
            write(directory/'metrics.json',row)
            write(args.output/'summary.json',{'groups':aggregate(rows),'trials':rows})
            print(json.dumps(row,ensure_ascii=False),flush=True)
    finally:
        client.close()
        reviewer.client.close()


if __name__ == '__main__':
    main()
