"""A storage engine never deploys or starts on a store whose on-disk features it cannot read.

R3/R4 rollback safety (Coordinator 2026-11-13, DB-4's answers): R3's hash-shard
overflow (the DIRCAP fix) writes loose overflow blocks under
``<block-store root>/blocks-overflow`` once a shard passes about 1 M entries,
and engines older than R3 read them as missing. The store's marker directories
say which features it uses; an engine names the features it reads in
``tofu-db --build-info`` (``store_features``; a build without the key reads
none). Every door refuses an engine that misses a used feature, with a typed,
owner-visible, audited error and nothing changed: engine publication
(``scripts/deploy_tofudb_engine.py``, shared by restart preparation, certified
deployments and operator deployments), restart preparation that keeps the
pinned engine (``storage_recover.sh`` PATH A: the restart deploys the existing
bytes) and every storage start. The features a release knows come from
contracts/storage_v2.json ``nativeStoreFeatures``; a release that declares none
checks nothing, so the guard never ships ahead of an engine that reports its
features, and ``check-engine`` is the release check for that engine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex

import pytest

from serverctl_pkg import native_engine as engines
from serverctl_pkg import store_features

pytestmark = pytest.mark.unit

FEATURES = {'loose-overflow-v1': 'blocks-overflow'}
BLOCK_STORE_ROOTS = ('tofudb-authority', 'projections/tofudb-search', 'backups/tofudb/closed')


@pytest.fixture
def audit_records(tmp_path, monkeypatch):
    import lib.log
    monkeypatch.setattr(lib.log, 'LOG_DIR', str(tmp_path / 'audit-logs'))
    monkeypatch.setattr(lib.log, 'AUDIT_LOG_FILE', str(tmp_path / 'audit-logs' / 'audit.log'))

    def records(event):
        path = tmp_path / 'audit-logs' / 'audit.log'
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row.get('event') == event]
    return records


@pytest.fixture
def declared(monkeypatch):
    """This release declares loose-overflow-v1 (DB-4's contract entry)."""
    monkeypatch.setattr(store_features, 'declared', lambda: dict(FEATURES))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / 'data' / 'tofudb-authority').mkdir(parents=True)
    return root


def engine(path: Path, features=None, source='a' * 64) -> Path:
    """A stand-in engine answering --build-info; FEATURES None means a build without the key."""
    info = {'format': 'tofu-db-build.v1', 'source_sha256': source, 'version': 'test',
            'embedded_allocator': 'system'}
    if features is not None:
        info['store_features'] = list(features)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nif [ "$1" = "--build-info" ]; then\n  printf \'%s\\n\' '
                    f'{shlex.quote(json.dumps(info))}\n  exit 0\nfi\nexit 3\n')
    path.chmod(0o755)
    return path


def overflow(project: Path, store: str = 'tofudb-authority') -> Path:
    marker = project / 'data' / store / 'blocks-overflow'
    (marker / '01').mkdir(parents=True)
    return marker


def test_a_store_without_markers_admits_any_engine(project, declared, tmp_path):
    store_features.require_readable(engine(tmp_path / 'old' / 'tofu-db'), project / 'data', door='storage start')


def test_an_engine_that_reads_the_feature_opens_an_overflowed_store(project, declared, tmp_path):
    overflow(project)
    store_features.require_readable(engine(tmp_path / 'r3' / 'tofu-db', ['loose-overflow-v1']),
                                    project / 'data', door='storage start')


@pytest.mark.parametrize('store', BLOCK_STORE_ROOTS)
@pytest.mark.parametrize('features', [None, []], ids=['no-store-features-key', 'empty-list'])
def test_an_engine_that_cannot_read_an_overflowed_store_is_refused_and_audited(
        project, declared, audit_records, tmp_path, store, features):
    marker = overflow(project, store)
    old = engine(tmp_path / 'old' / 'tofu-db', features, source='b' * 64)

    with pytest.raises(store_features.EngineStoreFeaturesRefusedError) as refused:
        store_features.require_readable(old, project / 'data', door='storage start')

    assert isinstance(refused.value, engines.EngineAdmissionRefusedError)
    message = str(refused.value)
    assert 'loose-overflow-v1' in message and str(marker) in message and str(old) in message
    assert 'bbbbbbbbbbbb' in message and 'storage start' in message
    assert refused.value.decision['missing'] == ['loose-overflow-v1']
    [record] = audit_records('storage_engine.store_features_refused')
    assert record['missing'] == ['loose-overflow-v1'] and record['door'] == 'storage start'
    assert record['binary'] == str(old) and record['markers'] == [str(marker)]


def test_nothing_is_checked_until_the_release_declares_its_store_features(project, monkeypatch, tmp_path):
    """The guard never ships ahead of an engine that reports store_features (Coordinator 2026-11-13)."""
    monkeypatch.setattr(store_features, 'declared', lambda: {})
    overflow(project)
    store_features.require_readable(engine(tmp_path / 'r3-without-key' / 'tofu-db'), project / 'data',
                                    door='storage start')


def test_the_declared_features_are_the_storage_contracts(monkeypatch):
    from lib.storage import generated_v2_semantics
    monkeypatch.setattr(generated_v2_semantics, 'NATIVE_STORE_FEATURES', dict(FEATURES), raising=False)
    assert store_features.declared() == FEATURES


def _deploy_arguments(project: Path, candidate: Path, **overrides) -> argparse.Namespace:
    return argparse.Namespace(**{
        'binary': str(candidate), 'dest': str(project / 'packages/tofu-db/target/certified/tofu-db'),
        'evidence': None, 'note': 'test', 'env_file': str(project / '.env'), 'no_env_sync': False,
        'no_smoke': True, 'dry_run': False, **overrides})


def test_engine_publication_refuses_an_older_engine_before_anything_is_written(
        project, declared, audit_records, tmp_path):
    """Restart preparation, certified deployments and storage_recover.sh PATH C all publish here."""
    from scripts import deploy_tofudb_engine
    overflow(project)
    (project / '.env').write_text('TOFUDB_BIN=packages/tofu-db/target/certified/tofu-db\n')
    old = engine(tmp_path / 'candidate' / 'tofu-db')

    with pytest.raises(store_features.EngineStoreFeaturesRefusedError):
        deploy_tofudb_engine.deploy(_deploy_arguments(project, old))

    assert not (project / 'packages/tofu-db/target/certified').exists()
    assert (project / '.env').read_text() == 'TOFUDB_BIN=packages/tofu-db/target/certified/tofu-db\n'
    [record] = audit_records('storage_engine.store_features_refused')
    assert record['door'] == 'engine publication'


def test_engine_publication_proceeds_for_an_engine_that_reads_the_store(project, declared, tmp_path):
    from scripts import deploy_tofudb_engine
    overflow(project)
    plan = deploy_tofudb_engine.deploy(_deploy_arguments(
        project, engine(tmp_path / 'candidate' / 'tofu-db', ['loose-overflow-v1']), dry_run=True))
    assert plan['dest'].endswith('packages/tofu-db/target/certified/tofu-db')


def test_the_deploy_command_reports_the_refusal_without_a_traceback(project, declared, tmp_path, capsys):
    from scripts import deploy_tofudb_engine
    overflow(project)
    old = engine(tmp_path / 'candidate' / 'tofu-db')
    assert deploy_tofudb_engine.main(['--binary', str(old), '--dest', str(project / 'engine/tofu-db'),
                                      '--env-file', str(project / '.env'), '--no-smoke']) == 1
    assert 'loose-overflow-v1' in capsys.readouterr().err


def test_restart_preparation_refuses_to_keep_a_pinned_engine_that_cannot_read_the_store(
        project, declared, monkeypatch, tmp_path):
    """storage_recover.sh PATH A: the pinned engine matches the checkout, so the restart deploys it as is."""
    dest = engine(project / 'packages/tofu-db/target/certified/tofu-db')
    overflow(project)
    for name in ('_verify_generation', '_verify_pin', '_require_source_generation',
                 '_require_storage_configuration', '_remaining_preparation_seconds', '_report_stage',
                 '_report_current_engine'):
        monkeypatch.setattr(engines, name, lambda *args, **kwargs: None, raising=False)

    with pytest.raises(store_features.EngineStoreFeaturesRefusedError) as refused:
        engines._prepare_generation(project, {}, {}, dest, 'tofu-db', 'a' * 64)

    assert 'restart preparation' in str(refused.value)
    engine(dest, ['loose-overflow-v1'])
    assert engines._prepare_generation(project, {}, {}, dest, 'tofu-db', 'a' * 64) is False


def test_every_storage_start_refuses_an_engine_that_cannot_read_the_store(project, declared, monkeypatch, tmp_path):
    """Crash respawns, lease recovery and a hand-pinned TOFUDB_BIN all start the engine through here."""
    from lib.storage import supervisor
    monkeypatch.setattr(supervisor, '_tofudb_checkout_root', lambda: project)
    overflow(project)

    with pytest.raises(store_features.EngineStoreFeaturesRefusedError):
        supervisor._advise_tofudb_engine_staleness(str(engine(tmp_path / 'old' / 'tofu-db')))

    supervisor._advise_tofudb_engine_staleness(str(engine(tmp_path / 'r3' / 'tofu-db', ['loose-overflow-v1'])))


@pytest.mark.parametrize('features,code', [(None, 1), ([], 1), (['loose-overflow-v1'], 0)],
                         ids=['no-key', 'missing-feature', 'complete'])
def test_the_release_check_refuses_an_engine_that_does_not_report_the_declared_features(
        declared, tmp_path, capsys, features, code):
    """GATE-R3 runs this on the release's certified engine before publishing the preflight."""
    candidate = engine(tmp_path / 'release' / 'tofu-db', features)
    assert store_features.main(['check-engine', str(candidate)]) == code
    output = capsys.readouterr()
    assert ('loose-overflow-v1' in output.err) == (code == 1)


def test_the_release_check_needs_the_store_features_key_even_before_features_are_declared(
        monkeypatch, tmp_path):
    monkeypatch.setattr(store_features, 'declared', lambda: {})
    assert store_features.main(['check-engine', str(engine(tmp_path / 'release' / 'tofu-db'))]) == 1
    assert store_features.main(['check-engine', str(engine(tmp_path / 'next' / 'tofu-db', []))]) == 0
