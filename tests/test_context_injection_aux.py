"""Auxiliary producers retain evidence and separate engine policy from user input."""
from types import SimpleNamespace

import pytest

from lib.swarm.agent import SubAgent
from lib.swarm.types import SubTaskSpec
from lib.tasks_pkg.context_composer.injections import append_injection
from lib.tasks_pkg.message_builder._prefetch import inject_prefetched_urls
from lib.tasks_pkg.conv_message_builder._load import _append_note_as_tail_user

pytestmark = pytest.mark.unit


def test_url_prefetch_retains_exact_payload_and_receipt():
    messages = [{'role': 'user', 'content': 'read this URL'}]
    task = {'toolRounds': [1]}
    assert inject_prefetched_urls(messages, [('https://a/x.pdf', '[Page 1] full evidence')], task) == 1
    assert messages[0] == {'role': 'user', 'content': 'read this URL'}
    assert messages[-1]['_isUrlPrefetch'] is True
    assert messages[-1]['content'].endswith('[Page 1] full evidence')
    assert task['_contextManifest'][-1]['id'] == 'url_prefetch'


def test_lifecycle_note_preserves_text_and_meta_classification():
    messages = [{'role': 'user', 'content': 'continue'}]
    assert _append_note_as_tail_user(messages, '  outcome unknown\n')
    assert messages[-1]['content'] == '  outcome unknown\n'
    assert messages[-1]['_isMeta'] and messages[-1]['_isLifecycleCaution']


def test_worker_policy_and_artifact_hint_do_not_rewrite_objective():
    store = SimpleNamespace(list_keys=lambda: ['x' * 1000] * 500)
    agent = SubAgent(
        SubTaskSpec(id='aux', role='general', objective='EXACT objective', context='EXACT context'),
        parent_task={'id': 'parent', 'config': {}}, all_tools=[], model='test-model',
        system_prompt_base='User-authored system', artifact_store=store,
        thinking_enabled=False)
    assert agent.messages[0] == {'role': 'system', 'content': 'User-authored system'}
    assert agent.messages[1]['role'] == 'system'
    assert 'Tool authority for this run' in agent.messages[1]['content']
    assert agent.messages[2] == {'role': 'user', 'content': '## Your Task\nEXACT objective\n\n## Context\nEXACT context'}
    assert all(m['_isMeta'] for m in agent.messages[3:])
    assert {row['id'] for row in agent.context_task['_contextManifest']} == {'worker_policy', 'worker_artifacts'}
    assert '_contextManifest' not in agent.parent_task
    assert len(agent.messages[-1]['content']) < 2200
    assert 'list_artifacts' in agent.messages[-1]['content']


def test_runtime_identity_survives_checkpoint_copy():
    messages = []
    append_injection(messages, 'worker_policy', 'scoped tools')
    assert dict(messages[0])['_contextInjectionId'] == 'worker_policy'


def test_dependency_renderer_preserves_explicit_user_context():
    from lib.swarm.scheduler import StreamingScheduler
    from lib.swarm.types import SubAgentResult
    dep = SubTaskSpec(id='dep', role='general', objective='verify')
    spec = SubTaskSpec(id='next', role='general', objective='report', context='explicit user context', depends_on=['dep'])
    scheduler = SimpleNamespace(_completed={'dep': (dep, SubAgentResult(final_answer='confirmed evidence'))})
    evidence = StreamingScheduler._inject_deps_locked(scheduler, spec)
    assert spec.context == 'explicit user context'
    assert 'confirmed evidence' in evidence
