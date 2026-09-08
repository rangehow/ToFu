"""Durable research records survive registry loss without claiming live execution."""
from copy import deepcopy
import json

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    from lib.research import records

    class Store:
        def __init__(self):
            self.rows = {}
            self.writes = 0
            self.fail = False

        def command(self, operation, payload, command_id):
            assert operation == 'record.put'
            if self.fail:
                raise RuntimeError('write refused')
            self.writes += 1
            self.rows[payload['namespace'], payload['key']] = deepcopy(payload['value'])
            return {'version': self.writes}

        def query(self, operation, payload):
            if operation == 'record.get':
                value = self.rows.get((payload['namespace'], payload['key']))
                return {'value': deepcopy(value)} if value else None
            assert operation == 'record.list'
            return [{'value': deepcopy(value)} for (namespace, key), value in
                    sorted(self.rows.items()) if namespace == payload['namespace']
                    and key.startswith(payload['prefix'])][:payload['limit']]

    value = Store()
    monkeypatch.setattr(records, '_storage', lambda **kwargs: value)
    return value


def task(tmp_path):
    from lib.research import runtime
    value = runtime._new_research_task(runtime._research_task_id(),
        direction='Synthetic direction', workdir=str(tmp_path), lang='en', user_id=12)
    value['_research_record_enabled'] = True
    return value


def test_registry_loss_restores_paper_metadata_and_explicit_checkpoint_resume(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine
    value = task(tmp_path)
    checkpoint = {'version': 1, 'stages': {'harvest': {'done': True, 'artifact': {'folder_id': 'owned'}}}}
    (tmp_path / 'pipeline_state.json').write_text(json.dumps(checkpoint))
    records.persist_record(value, event={'type': 'stage_done'})
    runtime._append_research_event(value, {'type': 'paper_done', 'stage': 'harvest',
        'result': {'arxivId': '2401.00001', 'title': 'Captured before stage completion',
                   'paperId': 'harvest_2401.00001', 'status': 'parsed'}})
    assert records.load_record(value['task_id'], user_id=13) is None
    assert records.list_records(user_id=13) == []
    assert runtime._research_runtime.remove_owned(value['task_id'], user_id=12)
    reopened = records.load_record(value['task_id'], user_id=12)
    assert reopened['status'] == 'interrupted'
    assert reopened['resumable'] is True
    assert reopened['papers'][0]['title'] == 'Captured before stage completion'
    assert 'checkpoint' not in reopened
    from pathlib import Path
    import jsonschema
    schema_path = Path(__file__).parents[1] / 'contracts' / 'research_record_v1.schema.json'
    schema = json.loads(schema_path.read_text())
    # Resolve the sibling progress schema without network access.
    schema['properties']['research_progress']['anyOf'][1] = json.loads(
        schema_path.with_name('research_progress_v1.schema.json').read_text())
    jsonschema.validate(reopened, schema)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path / 'restart'))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    result = records.resume_record(value['task_id'], user_id=12)
    assert result['deduped'] is False
    assert len(spawned) == 1
    restored = tmp_path / 'restart' / 'jobs' / value['task_id'] / 'pipeline_state.json'
    assert json.loads(restored.read_text()) == checkpoint


def test_write_failure_stops_worker_instead_of_clean_success(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine, recipe
    value = task(tmp_path)
    records.persist_record(value)

    def build(*args, **kwargs):
        store.fail = True
        kwargs['emit']({'type': 'paper_done', 'result': {'arxivId': '2401.00001'}})
        return {'accepted': [], 'rejected': []}

    monkeypatch.setattr(recipe, 'build_research_from_direction', build)
    engine.run_research_task(value)
    assert value['status'] == 'error'
    assert not any(e.get('type') == 'final' for e in value['events'])
    assert value['_research_persistence_error']


def test_token_traffic_has_no_durable_write_and_papers_are_bounded(tmp_path, store):
    from lib.research import records, runtime
    value = task(tmp_path)
    records.persist_record(value)
    writes = store.writes
    for _ in range(1000):
        runtime._append_research_event(value, {'type': 'text_delta', 'text': 'x'})
    assert store.writes == writes
    for index in range(200):
        records.persist_record(value, event={'type': 'paper_done', 'result': {
            'arxivId': str(index), 'title': 'x' * 1000}})
    assert len(value['_durable_research_record']['papers']) == 200
    assert len(value['_durable_research_record']['papers'][0]['title']) == 500
    assert len(store.rows) == 2  # replace-in-place snapshot + compact index
    with pytest.raises(ValueError, match='ceiling'):
        records.persist_record(value, event={'type': 'paper_done', 'result': {'arxivId': 'overflow'}})


def test_run_result_is_frozen_independently_of_same_direction(tmp_path, store):
    from lib.research import records
    first = task(tmp_path)
    second = task(tmp_path)
    result = {'survey_md': 'first run evidence', 'accepted': []}
    records.persist_record(first, status='done', result=result)
    result['survey_md'] = 'mutated caller'
    records.persist_record(second, status='done', result={'survey_md': 'second run'})
    assert records.load_record(first['task_id'], user_id=12)['result']['survey_md'] == 'first run evidence'
    assert records.load_record(second['task_id'], user_id=12)['result']['survey_md'] == 'second run'


def test_start_refusal_does_not_spawn(tmp_path, monkeypatch, store):
    from lib.research import engine, runtime
    store.fail = True
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    monkeypatch.setattr(runtime._research_runtime, 'spawn',
                        lambda *args: pytest.fail('unpersisted task spawned'))
    with pytest.raises(RuntimeError, match='write refused'):
        engine.produce_research('cannot persist', user_id=12)
