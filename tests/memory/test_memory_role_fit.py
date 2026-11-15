"""Memory context fits the role that receives it.

Design record "Owner understanding", Decisions 4 and 8: memory guidance names
only the memory tools the request holds and states when to consult the
owner-memory lookup; a prefetched lesson is never shown through a tool the
role lacks and always carries its trust class; selection reads the owner's own
words (a Worker also its brief), never envelope scaffolding or Router evidence.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MEMORY_TOOLS = {'search_memories', 'create_memory', 'update_memory', 'delete_memory',
                'merge_memories'}


def _guidance(tool_names, *, catalog=None):
    from lib.tasks_pkg.context_composer._models import ComposeRequest
    from lib.tasks_pkg.context_composer._providers import _memory_guidance

    task = {} if catalog is None else {'_executable_tool_catalog': [
        {'type': 'function', 'function': {'name': name}} for name in catalog]}
    return _memory_guidance(ComposeRequest(
        project_path='/project', project_enabled=True, memory_enabled=True,
        has_real_tools=True, tool_names=set(tool_names), task=task))


def test_guidance_names_only_the_memory_tools_a_role_holds():
    coordination = _guidance({'search_memories', 'delegate_project_work'})
    assert 'search_memories(mode=owner)' in coordination
    for case in ('before asking the owner about a preference',
                 'before an irreversible or external act', 'when asked why'):
        assert case in coordination.lower()
    assert 'Save only' not in coordination and 'CRUD' not in coordination

    no_memory_tools = _guidance({'read_files', 'run_command'})
    assert 'search_memories' not in no_memory_tools and 'Save only' not in no_memory_tools

    worker = _guidance(MEMORY_TOOLS | {'read_files'})
    assert 'search_memories(mode=owner)' in worker and 'verified reusable lessons' in worker
    assert len(worker) < 650


def test_a_deferred_lookup_counts_as_held():
    deferred = _guidance({'search_tools', 'execute_tools'}, catalog={'search_memories'})
    assert 'search_memories(mode=owner)' in deferred


def _lesson(memory_id, body, **provenance):
    return {'id': memory_id, 'name': f'{memory_id} lesson', 'description': f'{memory_id} rule',
            'body': body, 'scope': 'project', 'filepath': f'/p/.tofu/memories/{memory_id}.md',
            'status': 'active', 'provenance': provenance}


def test_a_prefetched_lesson_is_opened_only_through_a_tool_the_role_holds():
    from lib.memory.prefetch._inject import _render_relevant_memories_block

    small = _lesson('small', 'short body', source_class='agent_report', at='2026-11-10T08:00:00Z')
    coordination = _render_relevant_memories_block([small], openers={'search_memories'})
    assert '- id: small' in coordination
    assert 'read_files' not in coordination and '- path:' not in coordination

    worker = _render_relevant_memories_block([small], openers={'search_memories', 'read_files'})
    assert '- path: /p/.tofu/memories/small.md' in worker

    huge = _lesson('huge', 'x' * 50_000)
    assert 'search_memories(memory_id="huge")' in _render_relevant_memories_block(
        [huge], openers={'search_memories'})
    assert 'read with read_files' in _render_relevant_memories_block(
        [huge], openers={'read_files'})
    assert _render_relevant_memories_block([huge], openers=set()) == ''
    assert '### memory: huge' not in _render_relevant_memories_block(
        [small, huge], openers=set())


def test_every_prefetched_lesson_shows_its_trust_class():
    from lib.memory.prefetch._inject import _render_relevant_memories_block

    block = _render_relevant_memories_block([
        _lesson('agent', 'a', source_class='agent_report', at='2026-11-10T08:00:00Z'),
        _lesson('legacy', 'b'),
        _lesson('owner', 'c', source_class='owner_stated', at='2026-11-11T08:00:00Z'),
    ], openers={'search_memories'})
    assert '- trust: inferred (agent report, 2026-11-10)' in block
    assert '- trust: inferred (unrecorded source)' in block
    assert '- trust: owner-stated (owner stated, 2026-11-11)' in block


def _authored(text):
    return [{'role': 'user', 'content': text}]


def test_a_forward_selects_by_the_owner_words_not_the_router_evidence():
    from lib.memory.prefetch._query import _extract_current_user_request
    from lib.router.forwarding import ROUTER_EVIDENCE_HEADER

    forwarded = ('看一下备份为什么这么慢\n\n' + ROUTER_EVIDENCE_HEADER + '\nThe owner request above '
                 'applies standing instruction ri-1 (owner Turn t-1 of Global Router conversation '
                 'coordinator-1). Router note (evidence): Coordinator worker project turn message.')
    assert _extract_current_user_request(_authored(forwarded)) == '看一下备份为什么这么慢'


@pytest.mark.parametrize('steered', [False, True])
def test_a_worker_selects_by_its_brief_not_the_envelope(monkeypatch, steered):
    """The envelope comes from the real producer, so a change to its format fails here.

    The input carries the brief and a source line linking the owner's request
    (an owner message steered into a nonhuman Coordinator reply is named by its
    steer); neither the owner's words nor the Router's evidence is repeated.
    """
    from types import SimpleNamespace

    from lib.memory.prefetch._query import _extract_current_user_request
    from lib.project_dispatch.delegation import worker_input
    from lib.router.forwarding import ROUTER_EVIDENCE_HEADER

    source = {'conversationId': 'coordinator-1', 'turnId': 'turn-1', 'projectionRevision': 3}
    words = ('Finish the unfinished work\n\n' + ROUTER_EVIDENCE_HEADER
             + '\nRouter note (evidence): principal changed')
    if steered:
        source['steerId'] = 'steer-1'
        monkeypatch.setattr('lib.turn_initiation.steered_input_turn', lambda reply, steer_id: {
            'projection': {'content': 'Finish the unfinished work'}})
    turn = {'actor': 'assistant' if steered else 'human', 'turnId': 'turn-1',
            'projectionRevision': 3, 'projection': {'content': words}}
    repository = SimpleNamespace(owner=7, turn_get=lambda conversation, turn_id, user_id: turn)
    envelope = worker_input(repository, {
        'authorizationSource': source,
        'brief': 'Carry the lane with cherry-pick -x and run integration_checkpoint.'})
    assert ('message steer-1' in envelope) is steered
    query = _extract_current_user_request(_authored(envelope))
    assert 'Finish the unfinished work' not in envelope and 'Router note' not in envelope
    assert query == 'Carry the lane with cherry-pick -x and run integration_checkpoint.'


def test_a_follow_up_selects_by_its_instruction():
    from lib.memory.prefetch._query import _extract_current_user_request
    from lib.project_dispatch.delegation import follow_up_input

    assert _extract_current_user_request(_authored(follow_up_input('Rerun the router suite.'))) == (
        'Rerun the router suite.')
