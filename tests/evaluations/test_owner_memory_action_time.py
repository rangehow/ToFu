"""The action-time candidate (measured, not shipped): a lesson reaches the model after the tool round its
trigger matches, once (``evaluations.owner_memory.action_time``). Synthetic lessons only.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

WORKER = {'convId': 'worker-1', '_turnId': 'turn-w1'}
TOOLS = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
         for name in ('run_command', 'read_files', 'search_memories')]


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
    _create(str(project), 'zorblat scratch reads', 'read zorblat scratch output with cat, not read_files',
            'read_files on a zorblat scratch file just written by run_command returns "File not found"')
    _create(str(project), 'quux palette', 'quux palette tokens live in one stylesheet',
            'restyling quux buttons in the studio stylesheet')
    yield str(project)
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _create(project, name, description, trigger):
    from lib.tasks_pkg.handlers.memory import _memory_create
    return _memory_create({'name': name, 'description': description, 'trigger': trigger,
                           'body': 'body', 'scope': 'project', 'repeat_of': 'new'},
                          project, [], task=WORKER)


def _round(call_id, name, arguments, result):
    return [{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': call_id, 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments)}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': result}]


def _task(project):
    return {'id': 't1', 'convId': 'c1', '_userId': 1, 'config': {},
            '_memoryCorpusRoots': [project, []]}


def _deliver(task, messages):
    from evaluations.owner_memory.action_time import deliver_round_lessons
    deliver_round_lessons(task, messages, model='claude-opus-4', round_tools=TOOLS)


def _carriers(messages):
    return [block['text'] for message in messages if message.get('role') == 'user'
            for block in (message['content'] if isinstance(message['content'], list)
                          else [{'text': message['content']}])
            if '<lesson_check>' in (block.get('text') or '')]


def test_selection_reads_actions_and_errors_and_skips_shown_lessons(store):
    from evaluations.owner_memory.action_time import select_for_tool_round

    evidence = 'read_files {"path": "/tmp/zorblat/out.txt"}\nError: File not found: /tmp/zorblat/out.txt'
    assert [r['id'] for r in select_for_tool_round(evidence, store, shown_ids=set())] == [
        'zorblat_scratch_reads']
    assert select_for_tool_round(evidence, store, shown_ids={'zorblat_scratch_reads'}) == []
    assert select_for_tool_round('', store, shown_ids=set()) == []


def test_round_evidence_keeps_error_text_and_drops_successful_output():
    from evaluations.owner_memory.action_time import last_tool_round as _last_tool_round, round_lesson_evidence

    messages = [{'role': 'user', 'content': 'go'},
                *_round('a', 'run_command', {'command': 'ls'}, 'zorblat palette quux\n[exit code: 0]'),
                *_round('b', 'run_command', {'command': 'cat x'}, 'cat: x: No such file\n[exit code: 1]')]
    calls, results = _last_tool_round(messages)
    assert [c['id'] for c in calls] == ['b']
    evidence = round_lesson_evidence(calls, results)
    assert 'cat x' in evidence and 'No such file' in evidence
    calls, results = _last_tool_round(messages[:3])
    assert 'zorblat palette' not in round_lesson_evidence(calls, results)


def test_a_matching_round_appends_one_check_after_its_results_and_never_repeats_it(store):
    task = _task(store)
    messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'summarize the run'},
                *_round('a', 'run_command', {'command': 'python gen.py > /tmp/zorblat/out.txt'},
                        '[exit code: 0]'),
                *_round('b', 'read_files', {'path': '/tmp/zorblat/out.txt'},
                        'Error: File not found: /tmp/zorblat/out.txt')]
    history = json.dumps(messages, sort_keys=True)
    _deliver(task, messages)
    [check] = _carriers(messages)
    assert 'When read_files on a zorblat scratch file' in check
    assert 'read zorblat scratch output with cat' in check
    assert 'trust: inferred' in check and 'path: ' in check
    assert 'quux' not in check
    # Earlier messages are untouched; the check follows the round's results.
    assert json.dumps(messages[:len(json.loads(history))], sort_keys=True) == history
    _deliver(task, messages)  # same round prepared again
    assert len(_carriers(messages)) == 1

    messages += _round('c', 'read_files', {'path': '/tmp/zorblat/out2.txt'},
                       'Error: File not found: /tmp/zorblat/out2.txt')
    _deliver(task, messages)  # the lesson is already in front of the model
    assert len(_carriers(messages)) == 1


def test_lessons_shown_at_turn_start_are_not_sent_again(store):
    task = _task(store)
    messages = [{'role': 'user', 'content': '<relevant_memories>\n### memory: zorblat scratch reads\n'
                                            f'- path: {store}/.tofu/memories/zorblat_scratch_reads.md\n'
                                            '</relevant_memories>\nsummarize the run'},
                *_round('b', 'read_files', {'path': '/tmp/zorblat/out.txt'}, 'Error: File not found')]
    _deliver(task, messages)
    assert _carriers(messages) == []


def test_no_delivery_without_an_admitted_prefetch_or_a_way_to_open_lessons(store):
    messages = [{'role': 'user', 'content': 'go'},
                *_round('b', 'read_files', {'path': '/tmp/zorblat/out.txt'}, 'Error: File not found')]
    task = _task(store)
    del task['_memoryCorpusRoots']
    _deliver(task, messages)
    assert _carriers(messages) == []

    from evaluations.owner_memory.action_time import deliver_round_lessons
    deliver_round_lessons(_task(store), messages, model='claude-opus-4', round_tools=TOOLS[:1])
    assert _carriers(messages) == []


def test_an_open_tool_batch_gets_nothing(store):
    messages = [{'role': 'user', 'content': 'go'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'x', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{}'}}]}]
    _deliver(_task(store), messages)
    assert _carriers(messages) == []
