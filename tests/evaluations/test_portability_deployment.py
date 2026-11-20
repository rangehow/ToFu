"""Unknown aliases are diagnostic only; trust must enter through a host port."""
import json
import pytest
from evaluations.portability import models, report, run
from evaluations.portability.scenario import Scenario, SCENARIO_MODULES, load_scenario

pytestmark = pytest.mark.unit


def scenario():
    def arm(task, client, scratch):
        return {'text': client.ask('fixture')}
    return Scenario('fixture', '1', 'score', ({'id': 'a'},), arm, arm, lambda *_: 1.0)


def response(*_):
    return {'model': 'subject', 'deployment_sha256': 'a'*64,
            'choices': [{'finish_reason': 'stop', 'message': {'content': 'fixture'}}], 'usage': {}}


def test_alias_and_payload_metadata_do_not_establish_deployment(monkeypatch, tmp_path):
    monkeypatch.setattr(models, '_request', response)
    s = scenario()
    path = run.run_cell(s, 'subject', 'baseline', tmp_path)
    run.run_cell(s, 'subject', 'baseline', tmp_path)
    run.run_cell(s, 'subject', 'tofu', tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 3 and not run.completed_keys(path, s)
    assert all(row['subject_deployment'] is None for row in rows)
    result = report.summarise(rows)['fixture']['models'][0]
    assert result['deployment_matched'] is False and result['paired']['gain'] is None


def test_trusted_host_identity_allows_only_matching_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(models, '_request', response)
    s = scenario()
    proof = lambda *_: 'a'*64
    path = run.run_cell(s, 'subject', 'baseline', tmp_path, identity_reader=proof)
    run.run_cell(s, 'subject', 'baseline', tmp_path, identity_reader=proof)
    assert len(path.read_text().splitlines()) == 1
    run.run_cell(s, 'subject', 'tofu', tmp_path, identity_reader=lambda *_: 'b'*64)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert (tmp_path / rows[0]['artifact_path']).parent != (tmp_path / rows[1]['artifact_path']).parent
    assert report.summarise(rows)['fixture']['models'][0]['paired']['gain'] is None


@pytest.mark.parametrize('invalid', [b'{\n', b'\xff\n', b'[]\n', b'no_lf', 'duplicate', 'invalid_usage'])
def test_attested_resume_uses_validated_prefix_without_rewriting(monkeypatch, tmp_path, invalid):
    monkeypatch.setattr(models, '_request', response)
    s = scenario()
    proof = lambda *_: 'a' * 64
    path = run.run_cell(s, 'subject', 'baseline', tmp_path, identity_reader=proof)
    run.run_cell(s, 'subject', 'tofu', tmp_path, identity_reader=proof)
    baseline, tofu = path.read_bytes().splitlines(keepends=True)
    if invalid == 'duplicate':
        invalid = b'{"score":0,' + tofu[1:]
    elif invalid == 'invalid_usage':
        invalid = json.dumps({**json.loads(tofu), 'usage': {'calls': -1}}).encode() + b'\n'
    elif invalid == b'no_lf':
        invalid = tofu.rstrip(b'\n')
    # For terminated corruption, even a subsequent fully attested row is not
    # evidence beyond the readable prefix. For a torn row, its bytes alone
    # would otherwise match the existing verified artifact.
    content = baseline + invalid + (tofu if invalid.endswith(b'\n') else b'')
    path.write_bytes(content)
    assert run.completed_keys(path, s, deployment_identities={'subject': proof()}) == {
        ('fixture', 'subject', 'baseline', 'a'),
    }
    assert path.read_bytes() == content


@pytest.mark.parametrize(('content', 'kind'), [
    (b'{\n', 'malformed_row'),
    (b'{', 'incomplete_final_row'),
])
def test_corrupt_journal_refuses_cell_before_model_or_write(monkeypatch, tmp_path, content, kind):
    path = tmp_path / 'results.jsonl'
    path.write_bytes(content)

    def unexpected_model(*args, **kwargs):
        pytest.fail('a corrupt journal must be rejected before opening a model client')

    monkeypatch.setattr(run, 'ModelClient', unexpected_model)
    with pytest.raises(ValueError, match=rf'line 1.*{kind}.*fresh.*--out'):
        run.run_cell(scenario(), 'subject', 'baseline', tmp_path)

    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_deployment_change_during_call_is_infrastructure(monkeypatch):
    monkeypatch.setattr(models, '_request', response)
    client = models.ModelClient('subject', identity_reader=lambda model, payload: ('a' if payload is None else 'b')*64)
    with pytest.raises(models.ModelIdentityError, match='Deployment changed'):
        client.ask('fixture')


def test_no_unshipped_builtin_is_advertised():
    for alias in SCENARIO_MODULES:
        assert isinstance(load_scenario(alias), Scenario)
    for absent in ('coding', 'creative_writing', 'ppt', 'resume', 'research_ideation', 'research_pilot', 'video'):
        assert absent not in SCENARIO_MODULES
        with pytest.raises(ValueError, match='unknown scenario'):
            load_scenario(absent)
