"""Phase 1: the always-present owner profile every lead reads.

Design record "Owner understanding", Decisions 2 and 3, as shipped by Phase 1
(docs/modules/context_engineering.md, "Owner profile (Phase 1)"). The profile is
My Context rendered in experiment 1c's directive form, the carrier FM-2x2
measured (refs/recovered/memory-design/fm2x2): only active items the owner
stated or confirmed, each with its date and source; grants only inside their
bound; never clipped. In the same Turn context a prefetched lesson keeps the
trust label MEMORY-SELF-QUERY renders. Workers never get identity items; the
Watchdog, sub-agents, third-party-text turns and headless callers never get the
profile.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
USER = 7
SCOPE = str(USER)
OWNER_TURN = {'source_class': 'owner_stated', 'conversation_id': 'coordinator-1a39',
              'turn_id': 'b7e1c2d3e4f5a6b7', 'at': '2026-11-09T10:00:00Z'}
GRANT_TURN = {'source_class': 'owner_stated', 'conversation_id': 'coordinator-1a39',
              'turn_id': 'c0ffee1234567890', 'at': '2026-11-09T17:50:00Z'}
COORDINATOR_TOOLS = {'search_memories', 'delegate_project_work', 'get_project_workers'}

# Experiment 1c's usage rule, verbatim (evaluations/owner_profile_arm/profile.py at 7f641bf390).
USAGE_RULE = (
    "Each line is a standing directive the owner stated himself, with the date he last said it "
    "and where. Apply the ones that fit this request without asking him to restate them. Name the "
    "[id] of any directive that changes what you do. A directive never supplies a missing fact "
    "(who, which, where) and never approves an irreversible or external act: when the request "
    "lacks such a fact or needs such an approval, ask. If one looks out of scope or stale for this "
    "situation, say so rather than applying it silently."
)


def _item(item_id, *, item_type='response_preference', text='', source='manual',
          stamp='2026-11-10T08:00:00Z', **extra):
    item = {'id': item_id, 'type': item_type, 'source': source,
            'created_at': stamp, 'updated_at': stamp, **extra}
    if text:
        item['text'] = text
    return item


def _write_context(items, inactive=()):
    import lib.memory.user_profile as up
    path = up.context_path(SCOPE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'items': list(items), 'inactive_items': list(inactive)},
                  handle, ensure_ascii=False)


@pytest.fixture()
def owner_store(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    from lib.memory.storage import create_memory, update_memory

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()

    root = str(project)
    agent = {'source_class': 'agent_report', 'conversation_id': 'worker-9', 'turn_id': 'turn-w',
             'at': '2026-11-06T00:00:00Z'}
    replacement = create_memory('deploy key push works', 'deploy key push works yakpush',
                                'verified with git ls-remote', scope='project',
                                project_path=root, provenance=agent)
    stale = create_memory('no deploy key on this host', 'host has no deploy key yakpush',
                          'claimed before the key was added', scope='project',
                          project_path=root, provenance=agent)
    update_memory(stale['id'], {'status': 'disproved', 'status_reason': 'disproved',
                                'superseded_by': {'item_id': replacement['id']}},
                  project_path=root, provenance=agent)
    (project / '.tofu' / 'memories' / 'release_push_lesson.md').write_text(
        '---\nname: release push lesson\ndescription: release push needs the yakpush deploy key\n'
        'enabled: true\ncreated: 2026-07-01T00:00:00Z\n---\n\nunrecorded legacy body\n',
        encoding='utf-8')
    yield {'root': root, 'stale': stale['id'], 'replacement': replacement['id']}
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _compose(task, text='Push the yakpush release with the deploy key', *, root='',
             tools=frozenset(COORDINATOR_TOOLS), prefetch=True):
    from lib.tasks_pkg.context_composer import compose_task_context

    messages = [{'role': 'user', 'content': text}]
    if prefetch and root:
        from lib.memory.prefetch import run_memory_prefetch
        run_memory_prefetch([{'role': 'user', 'content': text}], project_path=root, task=task)
    compose_task_context(messages, user_id=USER, project_path=root,
                         memory_enabled=bool(root), has_real_tools=True,
                         conv_id=task.get('convId', ''), task=task, model='claude-opus-5.5',
                         tool_names=set(tools))
    content = messages[-1]['content']
    blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
    return '\n'.join(block.get('text') or '' for block in blocks if isinstance(block, dict))


def _carrier(text, carrier_id):
    start = f'<!-- tofu-context:{carrier_id}:start -->'
    end = f'<!-- tofu-context:{carrier_id}:end -->'
    if start not in text:
        return ''
    return text[text.index(start):text.index(end) + len(end)]


def _lead_task(**config):
    return {'id': 'task-lead', 'convId': 'coordinator-1a39', '_userId': USER,
            '_profileScope': SCOPE, 'config': {'projectPath': '/srv/tofu', **config}}


def _manifest_reason(task, block_id='user_context'):
    return next(row.get('reason', '') for row in task['_contextManifest'] if row['id'] == block_id)


def test_a_lead_turn_context_carries_the_profile_and_trust_labelled_lessons(owner_store):
    _write_context([
        _item('ctx_reply', text='Reply in Chinese', provenance=OWNER_TURN),
        _item('ctx_rule', item_type='work_rule', condition='a delivery is ready',
              action='run the owning suites first'),
        _item('ctx_about', item_type='identity', text='The owner leads the tofu project',
              stamp='2026-11-11T08:00:00Z'),
        _item('ctx_guess', text='Prefers tables over prose', source='assistant'),
    ], inactive=[
        _item('ctx_english', text='Reply in English', status='superseded',
              status_reason='owner_revoked', superseded_by={'item_id': 'ctx_reply'}),
        _item('ctx_pending', text='Uses an internal cluster', source='legacy_migration',
              status='needs_confirmation', status_reason='needs_owner_confirmation'),
    ])
    task = _lead_task()
    text = _compose(task, root=owner_store['root'])

    profile = _carrier(text, 'user_context')
    assert USAGE_RULE in profile
    assert ('## Owner directives: all projects\n'
            '- [ctx_reply] Reply in Chinese (owner, 2026-11-09, coordinator-1a39 turn b7e1c2d3)\n'
            '- [ctx_rule] When a delivery is ready → run the owning suites first '
            '(owner, 2026-11-10, My Context)\n\n'
            '## About the owner\n'
            '- [ctx_about] The owner leads the tofu project (owner, 2026-11-11, My Context)') in profile
    assert task['_appliedPreferences']['ids'] == ['ctx_reply', 'ctx_rule', 'ctx_about']

    lessons = _carrier(text, 'relevant_memories')
    assert '### memory: release push lesson\n- scope: project\n- trust: inferred (unrecorded source)\n' in lessons

    # Nothing disproved, superseded, unconfirmed or inferred is presented as the owner's.
    for absent in ('Reply in English', 'internal cluster', 'Prefers tables',
                   'no deploy key on this host', owner_store['stale']):
        assert absent not in text


def test_the_profile_is_whole_never_clipped(owner_store):
    statements = [f'Directive {index:02d}: ' + 'keep the reasoning visible and cite the evidence '
                  'that decided it, in the reply itself, for every non-trivial claim' for index in range(40)]
    _write_context([_item(f'ctx_{index:02d}', text=text, provenance=OWNER_TURN)
                    for index, text in enumerate(statements)])
    task = _lead_task()
    profile = _carrier(_compose(task, 'hello', prefetch=False), 'user_context')

    from lib.token_counter import count_text
    assert count_text(profile, model='claude-opus-5.5') > 1000
    assert 'truncated' not in profile
    assert all(f'- [ctx_{index:02d}] {text} (owner, 2026-11-09,' in profile
               for index, text in enumerate(statements))
    row = next(row for row in task['_contextManifest'] if row['id'] == 'user_context')
    assert row['injected'] and row.get('reason', '') == ''


def test_my_context_stores_a_profile_as_large_as_the_tested_one(owner_store):
    import lib.memory.user_profile as up

    # The 1c carrier is 6,880 characters; the old 2,500-character cap refused it.
    items = [{'type': 'response_preference', 'text': f'{index:02d} ' + 'x' * 200} for index in range(34)]
    saved = up.save_context_items(items, SCOPE)
    assert saved['saved'] and len(up.load_context(SCOPE)['items']) == 34


def test_a_grant_renders_only_inside_its_bound(owner_store):
    from lib.memory.user_profile import DELEGATION_BOUND

    _write_context([
        _item('ctx_reply', text='Reply in Chinese', provenance=OWNER_TURN),
        _item('ctx_grant', item_type='delegation_grant', text='全部权限都给你，可逆的事你自己决定',
              provenance=GRANT_TURN),
    ])
    profile = _carrier(_compose(_lead_task(), 'hello', prefetch=False), 'user_context')

    expected = '\n'.join([
        '## Owner directives: all projects',
        '- [ctx_reply] Reply in Chinese (owner, 2026-11-09, coordinator-1a39 turn b7e1c2d3)',
        '',
        '## Delegation grants, bounded by existing policy',
        "The owner's grants, verbatim:",
        '- [ctx_grant] 「全部权限都给你，可逆的事你自己决定」 (owner, 2026-11-09, coordinator-1a39 turn c0ffee12)',
        'Their bound, quoted verbatim from existing policy:',
        *[f'- “{quote}” ({label})' for quote, label, _source in DELEGATION_BOUND],
    ])
    assert USAGE_RULE + '\n\n' + expected in profile
    assert profile.count('全部权限都给你') == 1


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_every_bound_quote_matches_its_authority():
    from lib.memory.user_profile import DELEGATION_BOUND

    assert len(DELEGATION_BOUND) == 5
    for quote, _label, source in DELEGATION_BOUND:
        path = ROOT / source
        if path.suffix == '.py':
            assert any(quote in value for value in _string_constants(path)), (quote, source)
        else:
            assert ' '.join(quote.split()) in ' '.join(path.read_text(encoding='utf-8').split()), (
                quote, source)


def test_a_worker_gets_the_profile_without_identity_items(owner_store, monkeypatch):
    import lib.project_dispatch.admission as admission
    import lib.tasks_pkg.coordinator_policy as coordinator_policy

    _write_context([
        _item('ctx_reply', text='Reply in Chinese', provenance=OWNER_TURN),
        _item('ctx_about', item_type='identity', text='The owner leads the tofu project'),
    ])
    monkeypatch.setattr(admission, 'is_dispatch_task', lambda task: True)
    monkeypatch.setattr(coordinator_policy, 'coordinator_scope', lambda task, project_path=None: None)
    task = _lead_task()
    task['id'] = 'task-worker'
    profile = _carrier(_compose(task, 'hello', prefetch=False), 'user_context')

    assert '- [ctx_reply] Reply in Chinese' in profile
    assert 'tofu project' not in profile and '## About the owner' not in profile
    assert task['_appliedPreferences']['ids'] == ['ctx_reply']


def _watchdog_task():
    from lib.router.watchdog.binding import watchdog_location
    return _lead_task(projectPath=watchdog_location(USER))


def _child_task():
    from lib.tasks_pkg.invocation import InvocationPolicy
    task = _lead_task()
    task['_invocationPolicy'] = InvocationPolicy.child('task-parent')
    return task


def _third_party_task():
    from lib.tasks_pkg.execution_profile import THIRD_PARTY_TEXT, mark_task, restricted_task_config
    task = _lead_task()
    task['config'] = restricted_task_config(task['config'])
    mark_task(task, THIRD_PARTY_TEXT)
    return task


def _headless_task():
    return _lead_task(preferencesEnabled=False)


@pytest.mark.parametrize('build', [_watchdog_task, _child_task, _third_party_task, _headless_task],
                         ids=['watchdog', 'sub_agent', 'third_party_text', 'headless'])
def test_roles_outside_the_design_never_get_the_profile(owner_store, build):
    _write_context([_item('ctx_reply', text='Reply in Chinese', provenance=OWNER_TURN)])
    task = build()
    text = _compose(task, 'hello', prefetch=False)

    assert '<!-- tofu-context:user_context:start -->' not in text
    assert 'Reply in Chinese' not in text
    assert not (task.get('_appliedPreferences') or {}).get('ids')


def test_the_owner_lookup_and_the_profile_agree_on_who_stands_behind_an_item(owner_store):
    from lib.memory.owner_memory import search_owner_memory

    _write_context([
        _item('ctx_reply', text='Reply in Chinese', provenance=OWNER_TURN),
        _item('ctx_guess', text='Prefers tables over prose', source='assistant'),
    ])
    trust = {record['id']: record['trust'] for record in search_owner_memory(
        'tables Chinese', scope=SCOPE, top_k=5)['context']['items']}
    profile = _carrier(_compose(_lead_task(), 'hello', prefetch=False), 'user_context')

    assert trust == {'ctx_reply': 'owner-stated', 'ctx_guess': 'inferred'}
    assert '[ctx_reply]' in profile and '[ctx_guess]' not in profile
