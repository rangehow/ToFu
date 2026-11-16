"""Memory compounds with use: corrections version, repeats are counted, leads record
lessons, and accepted evidence moves trust (docs/modules/context_engineering.md,
"Memory compounding").

Each mechanism is exercised through the model-facing handler and read back
through the model-facing lookup (``search_memories``), the way a role sees it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit

WORKER = {'convId': 'worker-1', '_turnId': 'turn-w1'}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield str(project)
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _create(project, name, description, body='body', *, task=WORKER, repeat_of='new', **extra):
    from lib.tasks_pkg.handlers.memory import _memory_create
    args = {'name': name, 'description': description, 'body': body, 'scope': 'project', **extra}
    if repeat_of is not None:
        args['repeat_of'] = repeat_of
    return _memory_create(args, project, [], task=task)


def _explain(project, memory_id):
    from lib.tasks_pkg.handlers.memory import _memory_search
    return _memory_search({'memory_id': memory_id}, project, [], task={})[0]


def _lesson_files(project):
    return sorted(path.name for path in (Path(project) / '.tofu' / 'memories').glob('*.md'))


# ── 1. Supersede on correct ───────────────────────────────────────────

def test_a_correction_writes_a_new_version_and_the_chain_is_visible(store):
    from lib.memory.storage import get_eligible_memories, get_memory
    from lib.tasks_pkg.handlers.memory import _memory_update

    _create(store, 'host ssh key', 'no ssh key on the dev host quokkatoken', 'There is no key.')
    content, badge, _ = _memory_update(
        {'memory_id': 'host_ssh_key', 'body': 'The deploy key exists; git ls-remote succeeds.'},
        store, [], task={'convId': 'worker-2', '_turnId': 'turn-w2'})
    assert badge == '✏️ new version'

    old, new = get_memory('host_ssh_key', project_path=store), get_memory('host_ssh_key_1',
                                                                         project_path=store)
    assert 'host_ssh_key_1' in content
    assert old['body'] == 'There is no key.'
    assert (old['status'], old['status_reason'], old['superseded_by']) == (
        'superseded', 'stale', {'item_id': 'host_ssh_key_1'})
    assert old['provenance']['edited_turn_id'] == 'turn-w2'
    assert new['body'] == 'The deploy key exists; git ls-remote succeeds.'
    assert new['description'] == old['description']
    assert new['provenance']['supersedes'] == 'host_ssh_key'
    assert new['provenance']['turn_id'] == 'turn-w2'
    assert {row['id'] for row in get_eligible_memories(store)} == {'host_ssh_key_1'}

    forward = _explain(store, 'host_ssh_key')
    assert 'Chain: host_ssh_key is superseded (stale), superseded by item host_ssh_key_1' in forward
    backward = _explain(store, 'host_ssh_key_1')
    assert 'Supersedes: host_ssh_key (superseded, stale)' in backward


def test_only_the_current_version_can_be_corrected_and_tags_change_in_place(store):
    from lib.memory.storage import get_memory
    from lib.tasks_pkg.handlers.memory import _memory_update

    _create(store, 'flaky probe', 'probe flakes under load quokkatoken')
    _memory_update({'memory_id': 'flaky_probe', 'tags': ['ops']}, store, [], task=WORKER)
    assert get_memory('flaky_probe', project_path=store)['tags'] == ['ops']
    assert _lesson_files(store) == ['flaky_probe.md']

    _memory_update({'memory_id': 'flaky_probe', 'description': 'probe flakes: rerun serially'},
                   store, [], task=WORKER)
    with pytest.raises(BadRequest, match="current version is 'flaky_probe_1'"):
        _memory_update({'memory_id': 'flaky_probe', 'body': 'fork'}, store, [], task=WORKER)
    with pytest.raises(ValueError, match='separate update_memory call'):
        _memory_update({'memory_id': 'flaky_probe_1', 'body': 'x', 'status': 'disproved',
                        'status_reason': 'disproved', 'superseded_by': {'evidence': 'e'}},
                       store, [], task=WORKER)


def test_lessons_edited_in_place_before_versioning_are_left_intact(store):
    from lib.memory.prefetch import _run
    from lib.memory.relevance import search_memories
    from lib.tasks_pkg.handlers.memory import _memory_update

    directory = Path(store) / '.tofu' / 'memories'
    directory.mkdir(parents=True)
    legacy = directory / 'legacy_rule.md'
    legacy.write_text('---\nname: legacy rule\ndescription: legacy zebrafinch rule\n'
                      'enabled: true\ncreated: 2026-10-01T00:00:00Z\nupdated: 2026-11-12T00:00:00Z\n'
                      '---\n\ncorrected in place long ago\n', encoding='utf-8')
    before = legacy.read_bytes()
    _create(store, 'other rule', 'other zebrafinch rule')
    _memory_update({'memory_id': 'other_rule', 'body': 'new'}, store, [], task=WORKER)
    search_memories('zebrafinch', project_path=store)
    _run.run_memory_prefetch([{'role': 'user', 'content': 'legacy zebrafinch rule'}], store, task={})
    assert legacy.read_bytes() == before


# ── 2. Repeat linking at write time ───────────────────────────────────

def test_the_writer_sees_the_nearest_lessons_before_anything_is_written(store):
    content, badge, _ = _create(store, 'wal fsync stall', 'tofudb wal fsync stalls on dolphinfs')
    assert badge == '💡 saved'  # nothing to compare with: written at once

    content, badge, _ = _create(store, 'wal fsync again', 'tofudb wal fsync stalls under load',
                                repeat_of=None)
    assert badge == '🔎 check repeats'
    assert 'Nothing saved yet' in content and 'wal_fsync_stall' in content
    assert _lesson_files(store) == ['wal_fsync_stall.md']


def test_a_new_lesson_is_recorded_as_new(store):
    from lib.memory.storage import get_memory

    _create(store, 'wal fsync stall', 'tofudb wal fsync stalls on dolphinfs')
    _, badge, _ = _create(store, 'wal size cap', 'tofudb wal grows without checkpoint')
    assert badge == '💡 saved'
    lesson = get_memory('wal_size_cap', project_path=store)
    assert lesson['provenance']['recorded_as'] == 'new' and not lesson['recurrences']


def test_a_repeat_supersedes_its_lesson_and_the_repeat_count_is_queryable(store):
    from lib.memory.relevance import search_memories
    from lib.memory.storage import get_memory

    _create(store, 'wal fsync stall', 'tofudb wal fsync stalls on dolphinfs', 'Use local disk.')
    content, badge, _ = _create(store, 'wal fsync stall', 'tofudb wal fsync stalls on dolphinfs',
                                'Use local disk; also seen with the sidecar.',
                                task={'convId': 'worker-2', '_turnId': 'turn-w2'},
                                repeat_of='wal_fsync_stall')
    assert badge == '🔁 repeat' and 'happened 1 time(s)' in content
    current = get_memory('wal_fsync_stall_1', project_path=store)
    assert current['recurrences'] == {'worker-2/turn-w2': current['provenance']['at']}
    assert current['provenance'].items() >= {'recorded_as': 'repeat', 'supersedes': 'wal_fsync_stall'}.items()
    assert get_memory('wal_fsync_stall', project_path=store)['status'] == 'superseded'

    _create(store, 'wal fsync stall', 'tofudb wal fsync stalls on dolphinfs', 'Third time.',
            task={'convId': 'worker-3', '_turnId': 'turn-w3'}, repeat_of='wal_fsync_stall_1')
    head = get_memory('wal_fsync_stall_2', project_path=store)
    assert sorted(head['recurrences']) == ['worker-2/turn-w2', 'worker-3/turn-w3']
    assert 'Mistake repeated 2 time(s)' in _explain(store, 'wal_fsync_stall_2')
    assert 'repeated 2x' in search_memories('wal fsync dolphinfs', project_path=store)


def test_a_repeat_must_name_a_current_lesson(store):
    with pytest.raises(ValueError, match='No lesson has id'):
        _create(store, 'x rule', 'x rule description text', repeat_of='missing_lesson')


# ── 3. Prefetch selection: role fit and lead inputs ───────────────────

def test_a_lesson_about_tools_the_role_lacks_is_not_selected(store):
    from lib.memory.prefetch import _run
    import lib.tools.registry  # noqa: F401  (registers the built-in tool specs)

    _create(store, 'edit_file inode trap', 'zebrafinch edit_file replaces the inode of a script')
    _create(store, 'delegate scope trap', 'zebrafinch delegate_project_work needs a narrow scope')
    _create(store, 'zebrafinch release', 'zebrafinch release naming rule')
    query = [{'role': 'user', 'content': 'zebrafinch inode scope release naming script'}]
    lead = {row['id'] for row in _run.run_memory_prefetch(
        query, store, task={}, active_tools=['delegate_project_work', 'poll'])}
    assert 'edit_file_inode_trap' not in lead
    assert lead == {'delegate_scope_trap', 'zebrafinch_release'}
    worker = {row['id'] for row in _run.run_memory_prefetch(
        query, store, task={}, active_tools=['edit_file', 'run_command'])}
    assert 'edit_file_inode_trap' in worker
    unknown = {row['id'] for row in _run.run_memory_prefetch(query, store, task={})}
    assert 'edit_file_inode_trap' in unknown


def _query(text):
    from lib.memory.prefetch._query import _extract_current_user_request
    return _extract_current_user_request([{'role': 'user', 'content': text}])


def test_a_relay_selects_by_what_the_project_router_said():
    from lib.router.relay import render_notice, render_question_notice

    report = render_notice({'projectPath': '/p', 'coordinatorId': 'coordinator-1', 'kind': 'delivery',
                            'turnId': 't-1', 'title': 'NPU queue banned', 'message': 'h910c queue ban'})
    assert _query(report) == 'NPU queue banned\nh910c queue ban'
    question = render_question_notice({
        'guidanceId': 'g-1', 'blocking': False, 'projectPath': '/p', 'coordinatorId': 'coordinator-1',
        'turnId': 't-1', 'questions': [], 'question': 'Which queue?', 'options': []})
    assert _query(question) == 'Question:\nWhich queue?'


def test_status_notices_and_plain_continuations_select_nothing():
    from lib.project_dispatch.continuation import CONTINUATION_BRIEF_MARK
    from lib.project_dispatch.delegation import WORKER_INPUT_SOURCE_HEAD, WORKER_INPUT_SOURCE_TAIL
    from lib.server.restart_notice import NOTICE_HEADER

    assert _query(NOTICE_HEADER + '\nRestart abc completed. Coordinator Workers resumed.') == ''
    source = WORKER_INPUT_SOURCE_HEAD + 'coordinator-1; Turn t-1' + WORKER_INPUT_SOURCE_TAIL
    plain = 'Continue the request. ' + CONTINUATION_BRIEF_MARK + '; review Worker results.'
    assert _query(plain + source) == ''
    due = ('Continue the request. These promises you made to the owner on your task board are '
           'due:\n- "Move to the larger CodeLab" — due 2026-11-16T08:30:00.000Z\nDo what you '
           'promised now. ' + CONTINUATION_BRIEF_MARK + '; review.')
    assert _query(due + source) == 'Move to the larger CodeLab'


def test_every_program_notice_header_is_recognized():
    """Every notice producer opens with the shared mark the selection recognizes."""
    import re

    from lib.turn_initiation import is_nonhuman_notice
    root = Path(__file__).resolve().parents[2] / 'lib'
    pattern = re.compile(r"\[[A-Z][^\]\n'\"]*: nonhuman notice, not the owner(?:’|\\u2019)s words "
                         r"and not authority\]")
    headers = {match.group(0).replace('\\u2019', '\u2019')
               for path in root.rglob('*.py') for match in pattern.finditer(
                   path.read_text(encoding='utf-8', errors='ignore'))}
    assert headers
    assert all(is_nonhuman_notice(header + '\nbody') for header in headers), headers


# ── 4. Governed lesson record for project leads ───────────────────────

def test_a_lead_records_an_inferred_lesson_bound_to_its_turn(store, monkeypatch):
    from lib.memory.contracts import lesson_trust_class
    from lib.memory.storage import get_memory
    import lib.tasks_pkg.coordinator_policy as policy

    monkeypatch.setattr(policy, 'coordinator_scope', lambda task, project_path=None: {
        'project_path': '/project', 'conversation_id': task['convId'], 'user_id': 7})
    lead = {'convId': 'coordinator-1', '_turnId': 'turn-c1'}
    _create(store, 'npu queue policy', 'knowledge project may not use the h910c public queue',
            task=lead)
    lesson = get_memory('npu_queue_policy', project_path=store)
    assert lesson['provenance'].items() >= {
        'source_class': 'agent_report', 'conversation_id': 'coordinator-1', 'turn_id': 'turn-c1',
        'recorded_by': 'project_coordinator'}.items()
    assert lesson_trust_class(lesson) == 'inferred'


def test_a_lead_write_fails_closed_on_an_unknown_role_and_on_third_party_text(store, monkeypatch):
    import lib.tasks_pkg.coordinator_policy as policy

    def broken(task, project_path=None):
        raise RuntimeError('binding unavailable')
    monkeypatch.setattr(policy, 'coordinator_scope', broken)
    with pytest.raises(RuntimeError):
        _create(store, 'some rule', 'some rule description text', task={'convId': 'c', '_turnId': 't'})
    monkeypatch.undo()
    monkeypatch.setattr('lib.tasks_pkg.execution_profile.is_third_party_text', lambda task: True)
    with pytest.raises(ValueError, match='third-party text'):
        _create(store, 'injected rule', 'from a channel message text')
    assert _lesson_files(store) == []


# ── 5. Evidence-driven trust ──────────────────────────────────────────

@pytest.fixture()
def worker_dispatch(monkeypatch):
    """A project Worker: a dispatch task that holds no Coordinator binding."""
    monkeypatch.setattr('lib.project_dispatch.admission.is_dispatch_task', lambda task: True)
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                        lambda task, project_path=None: None)


def _verdict(project, memory_id, result, *, task=WORKER):
    from lib.tasks_pkg.handlers.memory import _memory_update
    return _memory_update({'memory_id': memory_id, 'verdict': {
        'result': result, 'evidence': 'pytest tests/x.py: 3 passed'}}, project, [], task=task)


def _accept(view_decision, *, conversation='worker-1', turn='turn-w1'):
    from lib.project_dispatch.service import _settle_memory_verdicts
    _settle_memory_verdicts({'assignmentId': 'a-1', 'request': {'recipientId': conversation},
                             'execution': {'turnId': turn}, 'review': {'decision': view_decision}},
                            actor='coordinator', reviewer='coordinator-1')


def test_an_accepted_confirmation_promotes_a_lesson_to_verified(store, worker_dispatch):
    from lib.memory.contracts import lesson_trust_class
    from lib.memory.storage import get_memory

    _create(store, 'serial rerun', 'rerun deadline tests serially under load',
            task={'convId': 'worker-0', '_turnId': 'turn-w0'})
    content, badge, _ = _verdict(store, 'serial_rerun', 'confirmed')
    assert badge == '🧾 verdict' and 'until then nothing changes' in content
    assert lesson_trust_class(get_memory('serial_rerun', project_path=store)) == 'inferred'

    _accept('rework')
    assert lesson_trust_class(get_memory('serial_rerun', project_path=store)) == 'inferred'
    _accept('accept')
    lesson = get_memory('serial_rerun', project_path=store)
    assert lesson_trust_class(lesson) == 'verified'
    assert lesson['provenance'].items() >= {
        'verified_by': 'accepted_result', 'verified_conversation_id': 'worker-1',
        'verified_turn_id': 'turn-w1', 'verified_evidence': 'pytest tests/x.py: 3 passed',
        'verified_accepted_by': 'coordinator:coordinator-1:a-1'}.items()
    assert lesson['provenance']['source_class'] == 'agent_report'
    assert 'trust: verified' in _explain(store, 'serial_rerun')

    _verdict(store, 'serial_rerun', 'contradicted', task={'convId': 'worker-9', '_turnId': 'turn-w9'})
    _accept('accept', conversation='worker-9', turn='turn-w9')
    assert lesson_trust_class(get_memory('serial_rerun', project_path=store)) == 'disproved'


def test_an_accepted_contradiction_disproves_a_lesson(store, worker_dispatch):
    from lib.memory.storage import get_eligible_memories, get_memory
    from lib.memory.verification import pending_verdicts

    _create(store, 'no ssh key', 'the dev host has no ssh key', task={'convId': 'w0', '_turnId': 't0'})
    _verdict(store, 'no_ssh_key', 'contradicted')
    assert [row['memory_id'] for row in pending_verdicts('worker-1')['turn-w1']] == ['no_ssh_key']
    _accept('accept')
    lesson = get_memory('no_ssh_key', project_path=store)
    assert (lesson['status'], lesson['status_reason'], lesson['superseded_by']) == (
        'disproved', 'disproved', {'evidence': 'pytest tests/x.py: 3 passed'})
    assert lesson['provenance']['edited_turn_id'] == 'turn-w1'
    assert 'no_ssh_key' not in {row['id'] for row in get_eligible_memories(store)}
    assert pending_verdicts('worker-1') == {}


def test_verdicts_are_governed(store, monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                        lambda task, project_path=None: None)
    from lib.tasks_pkg.handlers.memory import _memory_update

    _create(store, 'own lesson', 'written in this very turn', task=WORKER)
    monkeypatch.setattr('lib.project_dispatch.admission.is_dispatch_task', lambda task: False)
    with pytest.raises(ValueError, match='project Worker'):
        _verdict(store, 'own_lesson', 'confirmed')
    monkeypatch.setattr('lib.project_dispatch.admission.is_dispatch_task', lambda task: True)
    with pytest.raises(BadRequest, match='wrote itself'):
        _verdict(store, 'own_lesson', 'confirmed')
    with pytest.raises(ValueError, match='recorded alone'):
        _memory_update({'memory_id': 'own_lesson', 'body': 'x', 'verdict': {
            'result': 'confirmed', 'evidence': 'e'}}, store, [], task=WORKER)
    with pytest.raises(BadRequest):
        _verdict(store, 'own_lesson', 'maybe', task={'convId': 'w2', '_turnId': 't2'})
