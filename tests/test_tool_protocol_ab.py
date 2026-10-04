from copy import deepcopy
import json

from evals.tool_protocol_ab import aggregate, argument_errors, classify
from src.reviewer import OpenAIReviewer
from src.tools import READ_FILE_TOOL


def test_argument_metric_uses_full_submitted_schema_without_executing_tools():
    schemas = {'read_file':READ_FILE_TOOL['parameters']}
    call = {'function':{'name':'read_file','arguments':json.dumps({'path':'does-not-exist.py','line':None,'context_lines':None})}}
    assert argument_errors(call, schemas) == []
    missing = deepcopy(call)
    missing['function']['arguments'] = '{"path":"source.py"}'
    assert len(argument_errors(missing, schemas)) == 2
    invalid = deepcopy(call)
    invalid['function']['arguments'] = '{"path":"source.py","line":true,"context_lines":1000}'
    assert len(argument_errors(invalid, schemas)) == 2


def test_pseudo_call_is_not_in_native_argument_denominator():
    request = {'tools':[OpenAIReviewer._chat_tool(READ_FILE_TOOL)]}
    metrics = classify({'choices':[{'finish_reason':'stop','message':{'content':'{"tool_calls":1}'}}]}, request)
    groups = aggregate([{'group':'json_on',**metrics}, {'group':'json_off','error_type':'Timeout'}])
    assert groups['json_on']['pseudo_tool_response_rate'] == 1
    assert groups['json_on']['native_argument_valid_rate'] is None
    assert groups['json_off']['errors'] == 1
    assert groups['json_off']['native_tool_response_rate'] is None
