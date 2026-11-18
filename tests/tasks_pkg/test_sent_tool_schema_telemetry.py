"""A round's recorded tool-schema tokens are the schemas actually sent.

Admission measures before a route is chosen, so it counts the candidate
surface; a route's tool-name budget (docs/LLM_COST_OPTIMIZATION.md section 6)
then hides tools behind search_tools/execute_tools. The round record used to
keep the admission count, which overstated the Global Router's Claude schema
by about half and made the stage-2 audit (2026-11-18) read a 5-6k regression
where the wire grew ~1.6-1.8k. Admission itself stays the pre-route upper bound.
"""
import math

import pytest

from lib.llm._sse_core import prepare_request
from lib.tools.gateway import tool_schema_tokens
from tests.cache.test_cache_tool_name_budget_wire import _canonical_round
from tests.llm.test_tool_wire_name_budget import SANKUAI_ANTHROPIC
from tests.support.gateway_profiles import claude_tool_name_budget_gateway  # noqa: F401 (fixture)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit,
              pytest.mark.usefixtures('claude_tool_name_budget_gateway')]


def _sent_wire_projection():
    body = _canonical_round(['inspect the cache'])
    catalog = list(body['tools'])
    diagnostics = []
    body['_request_activity_sink'] = diagnostics.append
    prepare_request(body, base_url=SANKUAI_ANTHROPIC, api_key='test-key',
                    api_protocol='anthropic')
    wire = [entry for entry in diagnostics if entry.get('kind') == 'wire_projection'][-1]
    return catalog, wire


def _admitted_task(local_schema, *, ratio=2.2):
    serving = math.ceil(local_schema * ratio)
    return {'_lastPromptAdmission': {
                'round': 1, 'action': 'admitted', 'targetTokens': 777600,
                'messageTokens': 400000, 'toolSchemaTokens': serving,
                'totalTokens': 400000 + serving},
            '_lastPromptAdmissionLocalSchemaTokens': local_schema,
            '_contextTelemetryRounds': [{'round': 1, 'toolSchemaTokens': local_schema}]}


def test_tools_hidden_by_the_name_budget_are_not_counted():
    from lib.tasks_pkg.manager._stream import record_sent_tool_schema
    from lib.tasks_pkg.orchestrator._cache_round_accounting import bind_round_prompt_admission
    catalog, wire = _sent_wire_projection()
    admitted_local = tool_schema_tokens(catalog, model='kunlunbedrock.claude-opus-5.5-m17')
    assert wire['budgetDroppedNames'], 'the route must hide tools for this spec'
    assert 0 < wire['schemaTokens'] < admitted_local

    task = _admitted_task(admitted_local)
    record_sent_tool_schema(task, wire)
    api_round = {'round': 1}
    bind_round_prompt_admission(task, api_round, round_num=0)

    admission = api_round['promptAdmission']
    ratio = task['_lastPromptAdmission']['toolSchemaTokens'] / admitted_local
    assert admission['toolSchemaTokens'] == math.ceil(wire['schemaTokens'] * ratio)
    assert admission['totalTokens'] == admission['messageTokens'] + admission['toolSchemaTokens']
    # The pre-route measurement the ceiling was checked against stays visible.
    assert admission['admittedToolSchemaTokens'] == task['_lastPromptAdmission']['toolSchemaTokens']
    assert admission['admittedTotalTokens'] == task['_lastPromptAdmission']['totalTokens']
    assert task['_contextTelemetryRounds'][-1]['toolSchemaTokens'] == wire['schemaTokens']


def test_a_round_without_a_sent_projection_keeps_the_admission_record():
    from lib.tasks_pkg.orchestrator._cache_round_accounting import bind_round_prompt_admission
    task = _admitted_task(1000)
    api_round = {'round': 1}
    bind_round_prompt_admission(task, api_round, round_num=0)
    assert api_round['promptAdmission'] == task['_lastPromptAdmission']


def test_each_request_starts_without_an_earlier_requests_count():
    from lib.tasks_pkg.manager._stream import record_sent_tool_schema
    task = {}
    record_sent_tool_schema(task, {'kind': 'wire_projection', 'schemaTokens': 10})
    record_sent_tool_schema(task, None)
    assert '_sentToolSchemaLocalTokens' not in task
