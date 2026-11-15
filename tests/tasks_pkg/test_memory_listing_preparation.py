"""An accepted attempt loads its resident memory listing at acceptance.

Startup memory ranks the corpus that the process's resident memory listing
holds for the attempt's roots. A store directory the process holds no snapshot
of is loaded by its first read, so an accepted ordinary attempt whose
configuration opens the memory-prefetch gate reads its roots at acceptance,
next to its input translation, and ``startup_memory`` finds them loaded.
Attempts that cannot prefetch never load; a preparation that cannot start or
fails never fails acceptance. The listing is process-owned: the attempt holds
nothing it must release.
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_QUERY = 'fix ParserState in lib/parser/state.py'


def _run_now(fn, *args):
    future = Future()
    try:
        future.set_result(fn(*args))
    except BaseException as error:  # noqa: BLE001 - the Future carries it
        future.set_exception(error)
    return future


def _messages(text=_QUERY):
    return [{'role': 'user', 'content': text}]


def _write_memory(directory: Path, memory_id: str, description: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: {description}\n'
        'tags: [parser]\n'
        '---\n\n'
        f'body of {memory_id}\n',
        encoding='utf-8',
    )
    return path


@pytest.fixture()
def project(tmp_path, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    import lib.memory.storage._resident_listing as resident

    data_dir = tmp_path / 'data'
    root = tmp_path / 'project'
    (root / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_store_migrated', True)
    storage_dirs._migrated_roots.add(str(root))
    monkeypatch.setattr(crud, 'resident_listing', resident.ResidentMemoryListing())
    monkeypatch.setattr(resident, '_submit_refresh', _run_now)
    memory_files._metadata_cache.clear()
    yield root
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.discard(str(root))


@pytest.fixture()
def submissions(monkeypatch):
    """Run acceptance preparations synchronously and record every one."""
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )

    submitted = []

    def submit(fn, *args):
        submitted.append((fn, args))
        return _run_now(fn, *args)

    monkeypatch.setattr(context_provider_executor, 'submit', submit)
    return submitted


def _stub_acceptance(monkeypatch, order):
    """Real attempt acceptance over stubbed storage, registry and executor."""
    import lib.conversation_sync.task_start as task_start

    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(
        'lib.tasks_pkg.coordinator_policy.pin_coordinator_request_policy',
        lambda conversation_id, config, owner_user_id: config)
    monkeypatch.setattr(
        'lib.turn_lifecycle.build_api_messages',
        lambda *args, **kwargs: order.append(('context',)) or _messages())
    task = {'id': 'task-early', 'convId': 'conv-early', 'config': {},
            'status': 'pending'}
    monkeypatch.setattr(task_start, 'create_task', lambda *args, **kwargs: task)
    monkeypatch.setattr('lib.tasks_pkg.manager.discard_task',
                        lambda task_id, conv_id=None: None)
    return task_start, task


def _config(**fields):
    return {'model': 'm', '_turnOwnerUserId': 1, '_turnId': 'turn-e',
            '_attemptId': 'attempt-e', **fields}


def _accept(monkeypatch, config, order):
    """Accept an ordinary attempt through to its worker submission."""
    task_start, task = _stub_acceptance(monkeypatch, order)
    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda cfg: None)
    spawned = []
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', spawned.append)

    started = task_start.start_conversation_attempt_executor(
        'conv-early', _config(**config))

    assert started == ('task-early', None)
    assert spawned == [task]
    return task


# ── Acceptance ────────────────────────────────────────────────────────

def test_accepted_attempt_loads_its_listing_before_model_context(
        monkeypatch, submissions):
    order = []
    monkeypatch.setattr(
        'lib.memory.storage.resident_eligible_memories',
        lambda project_path, **kwargs: order.append(
            ('list', project_path, kwargs)) or [])

    task = _accept(monkeypatch, {
        'projectPath': '/proj', 'projectPaths': ['/proj', '/extra', '']}, order)

    assert order == [
        ('list', '/proj', {'extra_paths': ['/extra'], 'details': None}),
        ('context',),
    ]
    assert len(submissions) == 1
    # The listing is process-owned: the task carries nothing to release.
    assert not [key for key in task if 'emory' in key and 'isting' in key]


@pytest.mark.parametrize('config', [
    {'memoryEnabled': False},
    {'profile': 'minimal'},
    {'toolHistory': [{
        'toolCalls': [{'id': 'call-1', 'name': 'read_files', 'arguments': '{}'}],
        'toolResults': [{'tool_call_id': 'call-1', 'content': 'x'}]}]},
    {'flowBuiltin': 'review'},
    {'autopilot': True},
], ids=['memory-off', 'profile-memory-off', 'continue-tool-history', 'flow',
        'goal-mode'])
def test_attempts_that_cannot_prefetch_never_load(monkeypatch, submissions,
                                                  config):
    from lib.tasks_pkg.memory_listing import prepare_memory_listing

    monkeypatch.setattr(
        'lib.memory.storage.resident_eligible_memories',
        lambda *args, **kwargs: pytest.fail('a non-prefetching attempt loaded'))

    prepare_memory_listing({'model': 'm', 'projectPath': '/proj', **config})
    assert submissions == []


def test_disabled_prefetch_never_loads(monkeypatch, submissions):
    from lib.tasks_pkg.memory_listing import prepare_memory_listing

    monkeypatch.setattr('lib.memory.prefetch.PREFETCH_ENABLED', False)
    prepare_memory_listing({'projectPath': '/proj'})
    assert submissions == []


def test_a_preparation_that_cannot_start_never_fails_acceptance(monkeypatch):
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )

    def broken_submit(fn, *args):
        raise RuntimeError('submission refused')

    monkeypatch.setattr(context_provider_executor, 'submit', broken_submit)
    _accept(monkeypatch, {'projectPath': '/proj'}, [])


def test_a_failed_preparation_never_fails_acceptance(monkeypatch, submissions):
    def broken(*args, **kwargs):
        raise OSError('memory store unavailable')

    monkeypatch.setattr('lib.memory.storage.resident_eligible_memories', broken)
    order = []
    _accept(monkeypatch, {'projectPath': '/proj'}, order)

    assert len(submissions) == 1
    assert order == [('context',)]


def test_memory_disabled_acceptance_never_loads(monkeypatch, submissions):
    order = []
    monkeypatch.setattr(
        'lib.memory.storage.resident_eligible_memories',
        lambda *args, **kwargs: pytest.fail('a memory-disabled attempt loaded'))

    _accept(monkeypatch, {'projectPath': '/proj', 'memoryEnabled': False}, order)

    assert order == [('context',)]
    assert submissions == []


@pytest.mark.parametrize('config', [
    {},
    {'projectPath': '/proj'},
    {'projectPath': '/proj', 'projectPaths': ['/proj', '/b', '/b', '', '/c']},
    {'projectPaths': ['/a', '/b']},
    {'projectPath': '/proj', 'chatMode': 'chat'},
    {'projectPath': '/proj', 'profile': 'coding'},
])
def test_acceptance_loads_exactly_the_roots_startup_memory_ranks(
        monkeypatch, config):
    import lib.memory.prefetch as prefetch
    import lib.tasks_pkg.orchestrator._memory_prefetch as seam
    from lib.agent_core.profiles import apply_profile
    from lib.tasks_pkg.model_config import _resolve_model_config

    effective = apply_profile(config)
    mcfg = _resolve_model_config(effective, 'roots-parity')
    ranked = []
    monkeypatch.setattr(prefetch, 'run_memory_prefetch',
                        lambda messages, **kwargs: ranked.append(
                            (kwargs['project_path'], tuple(kwargs['extra_paths']))))
    seam.maybe_run_memory_prefetch(
        task={'id': 'roots'}, cfg=effective, messages=_messages(),
        tool_list=[], project_path=mcfg['project_path'],
        project_enabled=mcfg['project_enabled'],
        memory_enabled=mcfg['memory_enabled'], has_real_tools=True,
        injected_tool_calls=0)

    roots = seam.accepted_memory_prefetch_roots(config)
    assert roots is not None
    assert ranked == [(roots[0], tuple(roots[1]))]


def test_project_dispatch_worker_ranks_the_roots_its_acceptance_loaded(
        monkeypatch, submissions):
    """Worker-entry checkout binding never moves a worker's memory roots.

    Admission names the canonical project; the worker later runs in a task
    checkout, but memories belong to the canonical project, so acceptance
    loads exactly the listing ``startup_memory`` reads.
    """
    import lib.tasks_pkg.orchestrator._memory_prefetch as seam
    from lib.agent_core.profiles import apply_profile
    from lib.tasks_pkg.memory_listing import prepare_memory_listing
    from lib.tasks_pkg.model_config import _resolve_model_config

    listed = []

    def resident(project_path, **kwargs):
        listed.append((project_path, tuple(kwargs['extra_paths'])))
        return [{'id': 'parser-state', 'name': 'ParserState rule',
                 'description': 'ParserState rule in lib/parser/state.py',
                 'tags': [], 'enabled': True, 'eligible': True,
                 'is_package': False, 'scope': 'project'}]

    monkeypatch.setattr('lib.memory.storage.resident_eligible_memories', resident)
    monkeypatch.setattr('lib.memory.storage.load_eligible_memories',
                        lambda ids, *args, **kwargs: [])
    monkeypatch.setattr(seam, 'append_event', lambda task, event: None)
    accepted = {'model': 'm',
                '_projectDispatchAdmission': {'assignmentId': 'assignment-1'},
                'canonicalProjectPath': '/proj', 'projectPath': '/proj',
                'projectPaths': []}

    prepare_memory_listing(accepted)

    assert len(submissions) == 1
    bound = apply_profile({**accepted, 'projectPath': '/worktrees/worker-1',
                           'canonicalProjectPaths': []})
    mcfg = _resolve_model_config(bound, 'dispatch-worker')
    task = {'id': 'dispatch-worker'}
    seam.maybe_run_memory_prefetch(
        task=task, cfg=bound, messages=_messages(), tool_list=[],
        project_path=mcfg['project_path'],
        project_enabled=mcfg['project_enabled'],
        memory_enabled=mcfg['memory_enabled'], has_real_tools=True,
        injected_tool_calls=0)

    assert mcfg['project_path'] == '/worktrees/worker-1'
    assert listed == [('/proj', ()), ('/proj', ())]
    assert task['_memoryPrefetch']['phase'] == 'done'


# ── The startup read ─────────────────────────────────────────────────

def _prefetch(project_root, *, extra_paths=None):
    from lib.memory.prefetch import run_memory_prefetch

    events = []
    task = {'id': 'early-consumer', 'convId': 'conv-early'}
    selected = run_memory_prefetch(
        _messages(), str(project_root), task=task, emit_event=events.append,
        extra_paths=extra_paths)
    return selected, task, events


def test_startup_reads_the_listing_acceptance_loaded_without_walking(
        project, monkeypatch, submissions):
    import lib.memory.storage._resident_listing as resident
    from lib.tasks_pkg.memory_listing import prepare_memory_listing

    memories = project / '.tofu' / 'memories'
    _write_memory(memories, 'parser-state-rollback',
                  'ParserState rollback rule in lib/parser/state.py')
    _write_memory(memories, 'css-palette', 'CSS palette and typography')
    monkeypatch.setattr(resident, '_submit_refresh', lambda *args: None)

    prepare_memory_listing({'projectPath': str(project)})
    monkeypatch.setattr(resident, '_walk', lambda *args: pytest.fail(
        'startup walked a directory acceptance had loaded'))
    selected, task, _events = _prefetch(project)

    assert len(submissions) == 1
    assert [row['id'] for row in selected] == ['parser-state-rollback']
    assert task['_memoryPrefetch']['phase'] == 'done'


def test_memory_written_before_acceptance_is_listed_by_startup(
        project, submissions):
    from lib.memory.storage import create_memory
    from lib.tasks_pkg.memory_listing import prepare_memory_listing

    memories = project / '.tofu' / 'memories'
    _write_memory(memories, 'existing-note', 'an older css note')
    prepare_memory_listing({'projectPath': str(project)})

    written = create_memory(
        name='ParserState Before Acceptance',
        description='ParserState rule in lib/parser/state.py, written before '
                    'the next attempt was accepted',
        body='durable body', scope='project', project_path=str(project))
    prepare_memory_listing({'projectPath': str(project)})
    selected, _task, _events = _prefetch(project)

    assert [row['id'] for row in selected] == [written['id']]


def test_failed_listing_fails_the_prefetch(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('memory store unavailable')

    monkeypatch.setattr('lib.memory.storage.resident_eligible_memories', broken)
    selected, task, _events = _prefetch('/proj')

    assert selected == []
    assert task['_memoryPrefetch'] == {
        'phase': 'failed', 'reason': 'load_error:memory store unavailable'}
