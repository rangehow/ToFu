"""Offline selection receipts bind actual inputs and completed selector output."""
import io
import json
import subprocess
from pathlib import Path

import pytest

from evaluations.paper_review_calibration import prepare_parquet, run
from evaluations.slides_bench import llm_bridge

pytestmark = pytest.mark.unit


@pytest.fixture
def selection_env(monkeypatch, tmp_path):
    script = tmp_path / 'prepare_parquet.py'
    script.write_text('synthetic selector v1')
    monkeypatch.setattr(run, '__file__', str(tmp_path / 'run.py'))
    corpus = {'id': 'a', 'title': 'A', 'decision': 'ICLR 2025 Poster',
              'accepted': 1, 'text': 'Synthetic body'}
    calls = []

    def download(url, target):
        if not target.exists():
            target.write_bytes(b'synthetic parquet ' + target.name.encode())
        return target

    def prepare(command, **kwargs):
        calls.append(command)
        opposite = {**corpus, 'id': 'b', 'title': 'B',
                    'decision': 'Rejected_Submission' if corpus['decision'] != 'Rejected_Submission'
                    else 'ICLR 2025 Poster',
                    'accepted': corpus['decision'] == 'Rejected_Submission'}
        Path(command[8]).write_text('\n'.join(map(json.dumps, [corpus, opposite])) + '\n')

    monkeypatch.setattr(run, '_download', download)
    monkeypatch.setattr(run.subprocess, 'run', prepare)
    return tmp_path / 'out', script, corpus, calls


def cached_paths(out):
    selected = next(out.glob('selection-1-*.jsonl'))
    return selected, selected.with_suffix('.receipt.json')


@pytest.mark.parametrize('exhausted', [False, True])
def test_selector_reaches_eligible_papers_beyond_old_prefix(monkeypatch, tmp_path, exhausted):
    accepted_ids = sorted((f'a{i}' for i in range(7)), key=prepare_parquet.key)
    rejected_ids = sorted((f'r{i}' for i in range(7)), key=prepare_parquet.key)
    venue = prepare_parquet.VENUE
    papers = [{'venue': venue, 'paper_openreview_id': identifier, 'title': identifier,
               'paper_decision': decision}
              for ids, decision in ((accepted_ids, 'ICLR 2025 Poster'),
                                    (rejected_ids, 'Rejected_Submission'))
              for identifier in reversed(ids)]
    shards = {'left': [], 'right': []}
    short_bodies = dict(zip(accepted_ids[:4], (None, '', ' \t\n\u00a0', 0)))
    if exhausted:
        short_bodies[rejected_ids[-1]] = None
    blank_paragraphs = {1: None, 4: '', 7: ' \t\n\u00a0'}
    for identifier in accepted_ids + rejected_ids:
        for idx in reversed(range(15)):
            section = 'Body'
            content = f'{identifier} paragraph {idx}'
            if identifier in short_bodies:
                if idx >= 9:
                    content = short_bodies[identifier]
            elif identifier in rejected_ids[:5]:
                if idx == 9:
                    section, content = ' Bibliography ', None
            elif idx == 13:
                section, content = 'References', None
            else:
                content = blank_paragraphs.get(idx, content)
            shards['left' if idx % 2 == 0 else 'right'].append({
                'venue': venue, 'paper_openreview_id': identifier, 'paragraph_idx': idx,
                'section': section, 'content': content})

    expected = accepted_ids[4:6] + rejected_ids[5:6 if exhausted else 7]
    fetched_content_ids = set()

    def read_rows(path, columns, *, filters=None):
        rows = papers if path == 'papers' else shards[path]
        for field, operator, value in filters or []:
            assert operator in ('=', 'in')
            rows = [row for row in rows
                    if (row[field] == value if operator == '=' else row[field] in value)]
        if 'content' in columns:
            fetched_content_ids.update(row['paper_openreview_id'] for row in rows)
        return [{field: row[field] for field in columns} for row in rows]

    monkeypatch.setattr(prepare_parquet, '_read_rows', read_rows)
    output = tmp_path / 'selection.jsonl'
    prepare_parquet.main('papers', output, 2, 'left', 'right')
    selected = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row['id'] for row in selected] == expected
    assert fetched_content_ids == set(expected) | set(short_bodies)
    for row in selected:
        assert row['text'].splitlines() == [
            f'[Body] {row["id"]} paragraph {idx}' for idx in range(13)
            if idx not in blank_paragraphs]
    if exhausted:
        with pytest.raises(ValueError, match='require exactly 2 papers per class'):
            run._selected_rows(output, 2)
    else:
        assert len(run._selected_rows(output, 2)) == 4


def test_completed_selection_is_reused(selection_env):
    out, script, corpus, calls = selection_env
    first = run.select_papers(out, 1)
    assert run.select_papers(out, 1) == first
    assert first[0]['accepted'] is True
    assert len(calls) == 1


@pytest.mark.parametrize('changed', ['papers.parquet', 'paragraphs-0.parquet',
                                    'paragraphs-1.parquet', 'selector', 'selection', 'receipt'])
def test_changed_selection_dependencies_are_not_reused(selection_env, changed):
    out, script, corpus, calls = selection_env
    run.select_papers(out, 1)
    if changed == 'selector':
        script.write_text('synthetic selector v2')
    elif changed == 'selection':
        cached_paths(out)[0].write_text(json.dumps({**corpus, 'text': 'tampered'}))
    elif changed == 'receipt':
        cached_paths(out)[1].unlink()
    else:
        (out / 'data' / changed).write_bytes(b'changed actual parquet bytes')
    assert run.select_papers(out, 1)[0]['text'] == corpus['text']
    assert len(calls) == 2


def test_digestless_selection_is_not_reused(selection_env):
    out, script, corpus, calls = selection_env
    out.mkdir()
    (out / 'selection-1.jsonl').write_text(json.dumps({**corpus, 'text': 'stale'}))
    assert run.select_papers(out, 1)[0]['text'] == corpus['text']
    assert len(calls) == 1
    assert json.loads((out / 'selection-1.jsonl').read_text())['text'] == 'stale'


def test_failed_selector_cannot_certify_partial_output(selection_env, monkeypatch):
    out, script, corpus, calls = selection_env
    def fail(command, **kwargs):
        Path(command[8]).write_text(json.dumps(corpus))
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(run.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        run.select_papers(out, 1)
    assert not list(out.glob('selection-1-*.receipt.json'))


@pytest.mark.parametrize('decision,accepted', [
    ('ICLR 2025 Poster', False), ('Rejected_Submission', True),
    ('ICLR 2025 Oral', '0'), ('Rejected_Submission', 'false'),
    ('accept', True), ('Unknown decision', False), ('ICLR 2025 Poster', 2),
])
def test_selected_corpus_refuses_unsupported_or_contradictory_labels(selection_env, decision, accepted):
    out, script, corpus, calls = selection_env
    corpus.update(decision=decision, accepted=accepted)
    with pytest.raises(ValueError, match='decision|label'):
        run.select_papers(out, 1)
    assert not list(out.glob('selection-1-*.receipt.json'))


@pytest.mark.parametrize('decision,accepted', [('ICLR 2025 Oral', 1),
    ('ICLR 2025 Spotlight', True), ('ICLR 2025 Poster', 1), ('Rejected_Submission', 0)])
def test_exact_supported_decisions_determine_boolean_labels(selection_env, decision, accepted):
    out, script, corpus, calls = selection_env
    corpus.update(decision=decision, accepted=accepted)
    selected = run.select_papers(out, 1)
    assert selected[0]['accepted'] is (decision != 'Rejected_Submission')


@pytest.mark.parametrize('finish_reason', [None, '', 'unknown', 'tool_calls',
                                          'content_filter', 'length', 'stop', 'end_turn'])
def test_completion_boundary_retains_actual_evidence(monkeypatch, finish_reason):
    paper = {'id': 'a', 'title': 'A', 'decision': 'ICLR 2025 Poster',
             'accepted': True, 'text': 'Synthetic body',
             'selection_receipt': {'per_class': 1, 'status': 'completed',
                                   'selector_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
                                   'parquet_sha256': {name: 'c' * 64 for name in run._FILES}}}
    doc_type, checklist, messages = run.review_request(paper)
    content = json.dumps({'overall': 8, 'scores': {name: 8 for name, _ in doc_type.rubric},
                         'checklist': [{'rule': rule.id, 'pass': True} for rule in checklist]})
    seen = []
    def request_completion(actual_messages, **kwargs):
        seen.append(actual_messages)
        return {'model': 'fixture', 'choices': [{'finish_reason': finish_reason,
                                                 'message': {'content': content}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 34}}
    monkeypatch.setattr(llm_bridge, 'request_completion', request_completion)
    row = run.judge(paper, model='fixture', endpoint='http://127.0.0.1:9')
    if finish_reason not in ('stop', 'end_turn'):
        assert row['overall'] is row['completion'] is None
        assert row['errors'] and 'finish_reason' in row['errors'][0]
    else:
        assert row['overall'] == 8
        assert row['errors'] == []
        assert row['reported_model'] == row['requested_model'] == 'fixture'
        assert row['completion'] == content
        assert row['usage'] == {'prompt_tokens': 12, 'completion_tokens': 34}
    assert row['finish_reason'] == finish_reason
    assert row['request_messages'] == seen[0] == messages


@pytest.mark.parametrize('field', ['per_class', 'parquet_sha256', 'selector_sha256',
                                  'selection_sha256', 'status'])
@pytest.mark.parametrize('change', ['missing', 'stale'])
def test_incomplete_or_stale_receipt_cannot_authorize_reuse(selection_env, field, change):
    out, script, corpus, calls = selection_env
    run.select_papers(out, 1)
    path = cached_paths(out)[1]
    receipt = json.loads(path.read_text())
    if change == 'missing':
        receipt.pop(field)
    else:
        receipt[field] = 'stale'
    path.write_text(json.dumps(receipt))
    run.select_papers(out, 1)
    assert len(calls) == 2


def test_malformed_receipt_cannot_authorize_reuse(selection_env):
    out, script, corpus, calls = selection_env
    run.select_papers(out, 1)
    cached_paths(out)[1].write_text('{broken')
    run.select_papers(out, 1)
    assert len(calls) == 2


def test_matching_receipt_does_not_bypass_corpus_label_validation(selection_env):
    import hashlib
    out, script, corpus, calls = selection_env
    run.select_papers(out, 1)
    selection = cached_paths(out)[0]
    selection.write_text(json.dumps({**corpus, 'accepted': False}))
    path = cached_paths(out)[1]
    receipt = json.loads(path.read_text())
    receipt['selection_sha256'] = hashlib.sha256(selection.read_bytes()).hexdigest()
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='label'):
        run.select_papers(out, 1)
    assert len(calls) == 1


def test_length_row_is_never_reusable_even_without_recorded_errors():
    paper = {'id': 'a', 'title': 'A', 'decision': 'ICLR 2025 Poster',
             'accepted': True, 'text': 'Synthetic body',
             'selection_receipt': {'per_class': 1, 'status': 'completed',
                                   'selector_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
                                   'parquet_sha256': {name: 'c' * 64 for name in run._FILES}}}
    provenance = run.request_provenance(paper, 'fixture', run.review_request(paper)[2],
                                        endpoint='offline', effective_route='synthetic-route')
    row = {**paper, **provenance, 'reported_model': 'fixture', 'overall': 8,
           'errors': [], 'finish_reason': 'length'}
    assert run.reusable_rows([json.dumps(row)], run.current_prompt_digest(), papers=[paper],
                             model='fixture', endpoint='offline',
                             effective_route='synthetic-route') == {}


@pytest.mark.parametrize('change', [None, [], 'missing-id', 'empty-id', 'duplicate-id',
                                   'title', 'text', 'decision', 'accepted'])
def test_invalid_selection_shape_is_never_certified(selection_env, monkeypatch, change):
    out, script, corpus, calls = selection_env
    rows = [dict(corpus), {**corpus, 'id': 'b', 'title': 'B',
                          'decision': 'Rejected_Submission', 'accepted': False}]
    if change is None or change == []:
        rows[0] = change
    elif change == 'missing-id':
        rows[0].pop('id')
    elif change == 'empty-id':
        rows[0]['id'] = ' '
    elif change == 'duplicate-id':
        rows[1]['id'] = rows[0]['id']
    else:
        rows[0][change] = [] if change in ('decision', 'accepted') else ' '
    monkeypatch.setattr(run.subprocess, 'run', lambda command, **kwargs:
                        Path(command[8]).write_text('\n'.join(map(json.dumps, rows))))
    with pytest.raises(ValueError):
        run.select_papers(out, 1)
    assert not list(out.glob('selection-1-*.receipt.json'))


@pytest.mark.parametrize('count', [0, 1, 2])
def test_underfilled_selection_is_never_completed(selection_env, monkeypatch, count):
    out, script, corpus, calls = selection_env
    rows = [{**corpus, 'id': str(i)} for i in range(count)]
    monkeypatch.setattr(run.subprocess, 'run', lambda command, **kwargs:
                        Path(command[8]).write_text('\n'.join(map(json.dumps, rows))))
    with pytest.raises(ValueError, match='[Ii]nsufficient|[Uu]nderfill'):
        run.select_papers(out, 1)
    assert not list(out.glob('selection-1-*.receipt.json'))


def test_selection_rows_bind_the_completed_receipt_without_changing_raw_data(selection_env):
    out, script, corpus, calls = selection_env
    rows = run.select_papers(out, 1)
    selection, receipt_path = cached_paths(out)
    receipt = json.loads(receipt_path.read_text())
    assert all(row['selection_receipt'] == receipt for row in rows)
    assert all('selection_receipt' not in json.loads(line)
               for line in selection.read_text().splitlines())


@pytest.mark.parametrize('per_class', [0, -1, True])
def test_invalid_selection_count_refuses_before_transport(selection_env, per_class):
    out, script, corpus, calls = selection_env
    with pytest.raises(ValueError):
        run.select_papers(out, per_class)
    assert not calls


@pytest.mark.parametrize('choice_count,choice_fields,message_fields,usage_finish,accepted', [
    (0, {}, {}, 'stop', False),
    (2, {'finish_reason': 'stop'}, {}, 'stop', False),
    (1, {}, {}, 'stop', False),
    (1, {'finish_reason': 'length'}, {}, 'stop', False),
    (1, {'finish_reason': 'stop'}, {'tool_calls': [{'type': 'function',
                                                  'function': {'name': 'fixture'}}]}, 'stop', False),
    (1, {'finish_reason': 'stop'}, {'function_call': {'name': 'fixture'}}, 'stop', False),
    (1, {'finish_reason': 'stop'}, {'tool_calls': [], 'function_call': None}, 'length', True),
    (1, {'finish_reason': 'end_turn'}, {}, None, True),
])
def test_summary_validates_actual_compatible_choice(selection_env, monkeypatch, choice_count,
                                                    choice_fields, message_fields, usage_finish,
                                                    accepted):
    out, script, corpus, calls = selection_env
    endpoint = 'http://127.0.0.1:9/v1/chat/completions'
    doc_type, checklist, _ = run.review_request(corpus)
    content = json.dumps({'overall': 8, 'scores': {name: 8 for name, _ in doc_type.rubric},
                         'checklist': [{'rule': rule.id, 'pass': True} for rule in checklist]})
    payload = {'model': 'fixture', 'usage': {'finish_reason': usage_finish}, 'choices': [
        {**choice_fields, 'message': {'content': content, **message_fields}}] * choice_count}

    def urlopen(request, **kwargs):
        assert request.full_url == endpoint
        body = json.loads(request.data)
        assert body['model'] == 'fixture'
        assert body['tools'] == [] and body['stream'] is False
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(llm_bridge.urllib.request, 'urlopen', urlopen)
    assert run.main(['--out', str(out), '--per-class', '1', '--judge-model', 'fixture',
                     '--endpoint', endpoint]) == 0
    summary = json.loads((out / 'summary-fixture.json').read_text())
    assert summary['judged'] == (2 if accepted else 0)
    assert summary['selection_receipt'] == json.loads(cached_paths(out)[1].read_text())
    raw = [json.loads(line) for line in (out / 'rows-fixture.jsonl').read_text().splitlines()]
    assert all(row['selection_receipt'] == summary['selection_receipt'] for row in raw)
    actual_finish = choice_fields.get('finish_reason') if choice_count == 1 else None
    assert all(row['finish_reason'] == actual_finish for row in raw)
    assert all(row['completion'] == (content if accepted else None) for row in raw)
    assert all(('completion_validation' in row) == accepted for row in raw)


def test_shared_benchmark_chat_keeps_projected_result(monkeypatch):
    payload = {'model': 'fixture', 'choices': [{'finish_reason': 'length',
                                               'message': {'content': 'Partial draft'}}],
               'usage': {'completion_tokens': 12}}
    monkeypatch.setattr(llm_bridge.urllib.request, 'urlopen', lambda *args, **kwargs:
                        io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(llm_bridge, 'ENDPOINT', 'http://127.0.0.1:9/v1/chat/completions')
    assert llm_bridge.chat([{'role': 'user', 'content': 'Draft a slide'}], model='fixture') == (
        'Partial draft', {'model': 'fixture', 'finish_reason': 'length', 'completion_tokens': 12})


@pytest.mark.parametrize('detached', [None, {'status': 'completed'}, {'status': 'incomplete'},
                                      'legacy_projection'])
def test_detached_scores_do_not_enter_current_summary(selection_env, monkeypatch, detached):
    out, script, corpus, calls = selection_env

    def judge(paper, **kwargs):
        row = {**paper, 'reported_model': 'fixture', 'finish_reason': 'stop',
               'overall': 8, 'errors': []}
        if detached != 'legacy_projection':
            row.update(selection_receipt=detached, completion_validation='compatible_text_choice_v1')
        return row

    monkeypatch.setattr(run, 'judge', judge)
    run.main(['--out', str(out), '--per-class', '1', '--judge-model', 'fixture',
              '--endpoint', 'http://127.0.0.1:9'])
    summary = json.loads((out / 'summary-fixture.json').read_text())
    assert summary['judged'] == 0
    assert summary['status'] == 'insufficient_evidence'
    assert summary['threshold'] is summary['heldout_auc'] is None


def test_matching_receipt_cannot_certify_cached_underfill(selection_env):
    out, script, corpus, calls = selection_env
    run.select_papers(out, 1)
    selection, receipt_path = cached_paths(out)
    selection.write_text(json.dumps(corpus) + '\n')
    receipt = json.loads(receipt_path.read_text())
    receipt['selection_sha256'] = run._file_digest(selection)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='Insufficient'):
        run.select_papers(out, 1)
    assert len(calls) == 1
