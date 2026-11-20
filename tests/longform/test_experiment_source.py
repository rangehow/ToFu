"""Offline specifications for audited, project-owned experiment ZIP sources."""
import hashlib
import io
import json
from pathlib import Path
import stat
from urllib.parse import unquote, urlsplit
import zipfile

import pytest

from tofu_environments.contracts import Actor
from tofu_environments.job_sources import extract_job_source
from lib.longform.documents.experiment_source import (
    create_experiment_source, verify_experiment_source,
)


pytestmark = pytest.mark.unit


@pytest.fixture
def inputs(tmp_path):
    code = b'raise RuntimeError("never execute sealed source")\n'
    return {
        'actor': Actor(owner_id=17, tenant_id='test', project_root=str(tmp_path)),
        'code': code, 'code_sha256': hashlib.sha256(code).hexdigest(),
        'plan': {'hypothesis': 'synthetic', 'arms': [{'name': 'method'}], 'seeds': [7]},
        'provider': 'offline', 'cell_name': 'method_7', 'arm': 'method',
        'seed': 7, 'attempt': 1,
    }


def _path(source):
    return Path(unquote(urlsplit(source['uri']).path))


def test_deterministic_exact_members_manifest_and_extraction(inputs, tmp_path):
    (tmp_path / 'secret.env').write_text('synthetic-secret-not-for-upload')
    (tmp_path / 'unrelated.py').write_text('not experiment code')
    source = create_experiment_source(**inputs)
    path = _path(source)
    payload = path.read_bytes()
    assert source == {'uri': path.as_uri(), 'sha256': hashlib.sha256(payload).hexdigest()}
    assert path == tmp_path / '.experiment-sources' / f'{source["sha256"]}.zip'
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    inode = path.stat().st_ino
    reordered = dict(inputs, plan=dict(reversed(list(inputs['plan'].items()))))
    assert create_experiment_source(**reordered) == source
    assert path.stat().st_ino == inode
    assert path.read_bytes() == payload
    assert list(path.parent.iterdir()) == [path]
    canonical_plan = json.dumps(inputs['plan'], sort_keys=True, ensure_ascii=False,
                                separators=(',', ':')).encode()
    expected_manifest = {
        'schema': 'tofu.experiment-source/v1', 'owner_scope': inputs['actor'].scope,
        'project_root': str(tmp_path), 'provider': 'offline',
        'cell': {'name': 'method_7', 'arm': 'method', 'seed': 7}, 'attempt': 1,
        'code_sha256': inputs['code_sha256'],
        'scientific_plan_sha256': hashlib.sha256(canonical_plan).hexdigest(),
    }
    assert verify_experiment_source(source, **inputs) == expected_manifest
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ['experiment.py', 'plan.json', 'manifest.json']
        assert archive.read('experiment.py') == inputs['code']
        assert archive.read('plan.json') == canonical_plan
        assert json.loads(archive.read('manifest.json')) == expected_manifest
    staging = tmp_path / 'staging'
    staging.mkdir()
    receipt = extract_job_source(inputs['actor'], source, staging,
                                 byte_limit=len(payload), file_limit=3)
    assert receipt['source_sha256'] == source['sha256']
    assert {item['path'] for item in receipt['files']} == {
        'experiment.py', 'plan.json', 'manifest.json'}
    assert (staging / 'experiment.py').read_bytes() == inputs['code']
    assert b'synthetic-secret-not-for-upload' not in payload
    # Remove the archive and reconstruct it: fresh creation remains byte-identical.
    path.unlink()
    assert create_experiment_source(**inputs) == source
    assert path.read_bytes() == payload


@pytest.mark.parametrize('field', [
    'plan', 'code', 'owner', 'tenant', 'project', 'provider',
    'cell_name', 'arm', 'seed', 'attempt',
])
def test_every_binding_change_refuses_old_source(inputs, tmp_path, field):
    source = create_experiment_source(**inputs)
    changed = dict(inputs)
    actor = inputs['actor']
    if field == 'owner':
        changed['actor'] = Actor(18, actor.tenant_id, actor.project_root)
    elif field == 'tenant':
        changed['actor'] = Actor(actor.owner_id, 'other', actor.project_root)
    elif field == 'project':
        other = tmp_path / 'other-project'
        other.mkdir()
        changed['actor'] = Actor(actor.owner_id, actor.tenant_id, str(other))
    elif field == 'plan':
        changed['plan'] = dict(inputs['plan'], hypothesis='changed')
    elif field == 'code':
        changed['code'] = inputs['code'] + b'# changed\n'
        changed['code_sha256'] = hashlib.sha256(changed['code']).hexdigest()
    elif field in ('seed', 'attempt'):
        changed[field] += 1
    else:
        changed[field] += '-changed'
    with pytest.raises(ValueError, match='binding'):
        verify_experiment_source(source, **changed)
    new_source = create_experiment_source(**changed)
    assert new_source['sha256'] != source['sha256']
    assert verify_experiment_source(new_source, **changed)


def test_audited_code_hash_required_before_publication(inputs, tmp_path):
    inputs['code'] += b'changed'
    with pytest.raises(ValueError, match='audited'):
        create_experiment_source(**inputs)
    assert not (tmp_path / '.experiment-sources').exists()


@pytest.mark.parametrize('change', ['hash', 'uri', 'outside', 'traversal', 'extra'])
def test_source_reference_must_be_exact(inputs, change, tmp_path):
    source = create_experiment_source(**inputs)
    if change == 'hash':
        source['sha256'] = '0' * 64
    elif change == 'uri':
        source['uri'] = 'artifact:invented'
    elif change == 'outside':
        source['uri'] = (tmp_path.parent / 'outside.zip').as_uri()
    elif change == 'traversal':
        source['uri'] = source['uri'].replace('/.experiment-sources/', '/x/../.experiment-sources/')
    else:
        source['unexpected'] = True
    with pytest.raises(ValueError, match='binding'):
        verify_experiment_source(source, **inputs)


@pytest.mark.parametrize('member', ['experiment.py', 'plan.json', 'manifest.json', 'secret.env'])
def test_resealed_tampered_zip_is_not_accepted(inputs, member):
    source = create_experiment_source(**inputs)
    path = _path(source)
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    contents[member] = b'changed'
    replacement = io.BytesIO()
    with zipfile.ZipFile(replacement, 'w') as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    path.chmod(0o600)
    path.write_bytes(replacement.getvalue())
    changed_source = dict(source, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError):
        verify_experiment_source(changed_source, **inputs)
    with pytest.raises(ValueError):
        verify_experiment_source(source, **inputs)
    with pytest.raises(ValueError):
        create_experiment_source(**inputs)
    assert path.read_bytes() == replacement.getvalue()
    assert list(path.parent.iterdir()) == [path]


def test_same_length_corruption_detected(inputs):
    source = create_experiment_source(**inputs)
    path = _path(source)
    payload = path.read_bytes().replace(b'never execute', b'NEVER execute')
    assert len(payload) == path.stat().st_size
    path.chmod(0o600)
    path.write_bytes(payload)
    with pytest.raises(ValueError, match='changed'):
        verify_experiment_source(source, **inputs)
    with pytest.raises(ValueError, match='changed'):
        create_experiment_source(**inputs)


@pytest.mark.parametrize('location', ['project', 'ancestor', 'directory', 'archive'])
def test_symlinks_rejected_for_create_and_verify(inputs, tmp_path, location):
    source = create_experiment_source(**inputs)
    path = _path(source)
    actor = inputs['actor']
    if location == 'archive':
        moved = tmp_path / 'original.zip'
        path.rename(moved)
        path.symlink_to(moved)
    elif location == 'directory':
        moved = tmp_path / 'original-directory'
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
    else:
        alias = tmp_path / 'alias'
        alias.symlink_to(tmp_path, target_is_directory=True)
        project = alias
        if location == 'ancestor':
            (tmp_path / 'nested').mkdir()
            project = alias / 'nested'
        inputs['actor'] = Actor(actor.owner_id, actor.tenant_id, str(project))
        # Reference syntax can match, but a symlink-containing root still fails.
        source['uri'] = (project / '.experiment-sources' / path.name).as_uri()
    with pytest.raises((OSError, ValueError)):
        create_experiment_source(**inputs)
    with pytest.raises((OSError, ValueError)):
        verify_experiment_source(source, **inputs)


@pytest.mark.parametrize('root', ['', '.', '/tmp/../elsewhere'])
def test_explicit_absolute_project_without_traversal_required(inputs, root):
    inputs['actor'] = Actor(17, 'test', root)
    with pytest.raises(ValueError):
        create_experiment_source(**inputs)


@pytest.mark.parametrize('plan', [{'bad': float('nan')}, {1: 'coerced'}, {'bad': (1, 2)}])
def test_non_json_plan_rejected(inputs, plan, tmp_path):
    inputs['plan'] = plan
    with pytest.raises(ValueError):
        create_experiment_source(**inputs)
    assert not (tmp_path / '.experiment-sources').exists()


def test_project_replaced_with_symlink_after_publication(inputs, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    inputs['actor'] = Actor(17, 'test', str(project))
    source = create_experiment_source(**inputs)
    moved = tmp_path / 'moved-project'
    project.rename(moved)
    project.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        verify_experiment_source(source, **inputs)
    with pytest.raises(OSError):
        create_experiment_source(**inputs)


def test_concurrent_creation_is_atomic_and_idempotent(inputs):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create_experiment_source(**inputs), range(8)))
    assert all(source == results[0] for source in results)
    assert verify_experiment_source(results[0], **inputs)
    path = _path(results[0])
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('field,value', [('seed', True), ('attempt', 0),
                                        ('attempt', True), ('cell_name', '')])
def test_invalid_binding_inputs_rejected_before_publishing(inputs, tmp_path, field, value):
    inputs[field] = value
    with pytest.raises(ValueError):
        create_experiment_source(**inputs)
    assert not (tmp_path / '.experiment-sources').exists()
