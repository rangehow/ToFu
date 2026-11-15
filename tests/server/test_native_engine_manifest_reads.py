"""Source identity reparses a manifest only when its bytes change, in one
isolated child per operation, with no stale cache."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit


@pytest.fixture
def source_tree(tmp_path):
    for name in ('tofu-db', 'dependency', 'leaf'):
        crate = tmp_path / 'packages' / name
        (crate / 'src').mkdir(parents=True)
        (crate / 'src/lib.rs').write_text('pub fn fixture() {}\n')
        (crate / 'Cargo.toml').write_text(
            f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n')
    root = tmp_path / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text() + '''
[dependencies]
dependency = { path = "../dependency" }
[dev-dependencies]
leaf = { path = "../leaf" }
[target.'cfg(unix)'.build-dependencies]
dependency = { path = "../dependency" }
[package.metadata.tofu]
external-inputs = ["contracts/fixture.json"]
''')
    dependency = tmp_path / 'packages/dependency/Cargo.toml'
    dependency.write_text(dependency.read_text() + '\n[dependencies]\nleaf = { path = "../leaf" }\n')
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/fixture.json').write_text('{"fixture":true}\n')
    return tmp_path


@pytest.fixture(autouse=True)
def no_reusable_parses(monkeypatch):
    """Each spec starts with an empty content map, so the children it expects run."""
    monkeypatch.setattr(engines, '_parsed_manifests', {})


@pytest.fixture
def composed_budget():
    """Run real parser children under a whole preparation budget.

    These specs are about parse semantics, not the standalone liveness bound; a
    fixed bound would make their outcome depend on interpreter start-up speed.
    The per-test hang guard owns their wall-clock limit.
    """
    with engines._preparation_budget(engines.PREPARATION_TOTAL_TIMEOUT_SECONDS):
        yield


def observed_children(monkeypatch):
    """Manifests parsed by each isolated parser child, in launch order."""
    original = engines.subprocess.run
    children = []

    def run(command, **options):
        children.append([])
        completed = original(command, **options)
        children[-1].extend(Path(json.loads(line)[0]) for line in completed.stdout.splitlines())
        return completed

    monkeypatch.setattr(engines.subprocess, 'run', run)
    return children


@pytest.mark.usefixtures('composed_budget')
@pytest.mark.parametrize('module', ['sitecustomize', 'json'])
@pytest.mark.parametrize('entry', ['pythonpath', 'working_directory'])
def test_manifest_child_does_not_execute_ambient_python_code(
        source_tree, tmp_path, monkeypatch, module, entry):
    ambient = tmp_path / 'ambient-python'
    ambient.mkdir()
    marker = tmp_path / 'ambient-code-executed'
    (ambient / (module + '.py')).write_text(
        f"open({str(marker)!r}, 'w').write('ambient startup code ran')\n"
        "raise RuntimeError('ambient code must not own native source identity')\n")
    if entry == 'pythonpath':
        monkeypatch.setenv('PYTHONPATH', str(ambient))
    else:
        monkeypatch.chdir(ambient)
    manifest = source_tree / 'packages/tofu-db/Cargo.toml'

    result = engines._read_manifest(manifest)

    assert result['package'] == 'tofu-db'
    assert result['external_inputs'] == ['contracts/fixture.json']
    assert not marker.exists()


@pytest.mark.usefixtures('composed_budget')
def test_only_changed_manifest_bytes_reach_the_parser(source_tree, monkeypatch):
    children = observed_children(monkeypatch)
    first = engines.source_fingerprint(source_tree)
    expected = {path.resolve() for path in (source_tree / 'packages').glob('*/Cargo.toml')}
    assert len(children) == 1
    assert Counter(children[0]) == Counter({path: 1 for path in expected})
    children.clear()
    assert engines.source_fingerprint(source_tree) == first
    assert children == []
    leaf = (source_tree / 'packages/leaf/Cargo.toml').resolve()
    leaf.write_text(leaf.read_text().replace('0.0.0', '0.0.1'))
    edited = engines.source_fingerprint(source_tree)
    assert edited != first and children == [[leaf]]
    assert engines.source_fingerprint(source_tree) == edited and children == [[leaf]]


@pytest.mark.usefixtures('composed_budget')
def test_same_size_edit_with_restored_timestamps_reaches_the_parser(source_tree, monkeypatch):
    leaf = (source_tree / 'packages/leaf/Cargo.toml').resolve()
    assert engines._read_manifest(leaf)['package'] == 'leaf'
    before = leaf.stat()
    leaf.write_text(leaf.read_text().replace('"leaf"', '"lea7"'))
    os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (leaf.stat().st_size, leaf.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    children = observed_children(monkeypatch)
    assert engines._read_manifest(leaf)['package'] == 'lea7'
    assert children == [[leaf]]


@pytest.mark.usefixtures('composed_budget')
def test_reused_parse_shares_no_objects_between_operations(source_tree, monkeypatch):
    root = source_tree / 'packages/tofu-db/Cargo.toml'
    engines._read_manifest(root)['dependencies'].append('../mutated-by-a-caller')
    children = observed_children(monkeypatch)
    assert engines._read_manifest(root)['dependencies'] == ['../dependency', '../leaf', '../dependency']
    assert children == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX FIFO')
def test_fifo_manifest_is_left_to_the_bounded_parser(tmp_path, monkeypatch):
    fifo = tmp_path / 'Cargo.toml'
    os.mkfifo(fifo)
    # No writer ever opens the FIFO, so only the parser's own bound can end the read.
    monkeypatch.setattr(engines, 'MANIFEST_PARSE_TIMEOUT_SECONDS', 0.5)
    with pytest.raises(engines.EnginePreparationError, match='isolated parser timed out after 0.5s'):
        engines._read_manifest(fifo)


@pytest.mark.usefixtures('composed_budget')
def test_standalone_layout_parses_each_manifest_once(source_tree, monkeypatch):
    children = observed_children(monkeypatch)
    layout = engines.source_layout(source_tree)
    assert len(layout) == 3
    assert len(children) == 1
    assert Counter(children[0]) == Counter({location / 'Cargo.toml': 1 for location, _, _, _ in layout})
    root = source_tree / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text().replace('contracts/fixture.json', 'contracts/new.json'))
    (source_tree / 'contracts/new.json').write_text('{}')
    assert engines.external_source_inputs(source_tree) == [('contracts/new.json', source_tree / 'contracts/new.json')]


@pytest.mark.usefixtures('composed_budget')
def test_failed_parse_does_not_poison_next_fingerprint(source_tree):
    first = engines.source_fingerprint(source_tree)
    leaf = source_tree / 'packages/leaf/Cargo.toml'
    original = leaf.read_text()
    leaf.write_text('invalid TOML')
    with pytest.raises(engines.EnginePreparationError, match='source manifest'):
        engines.source_fingerprint(source_tree)
    leaf.write_text(original)
    assert engines.source_fingerprint(source_tree) == first


@pytest.mark.usefixtures('composed_budget')
def test_dependency_parse_failure_names_that_manifest_without_another_child(source_tree, monkeypatch):
    dependency = (source_tree / 'packages/dependency/Cargo.toml').resolve()
    dependency.write_text('invalid TOML')
    children = observed_children(monkeypatch)
    with pytest.raises(engines.EnginePreparationError,
                       match=re.escape(f'{dependency}: isolated parser exited with code 1.')) as caught:
        engines.source_fingerprint(source_tree)
    assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)
    assert len(children) == 1


def test_parser_stopped_mid_closure_reports_the_manifest_it_was_parsing(source_tree, monkeypatch):
    root = (source_tree / 'packages/tofu-db/Cargo.toml').resolve()
    dependency = (source_tree / 'packages/dependency/Cargo.toml').resolve()
    parsed_root = json.dumps([str(root), root.read_bytes().decode('latin-1'),
                              {'package': 'tofu-db', 'dependencies': ['../dependency']}])
    # POSIX timeouts carry partial output as bytes, ending mid-line here.
    failure = subprocess.TimeoutExpired(
        'manifest fixture', 10, output=f'{parsed_root}\n["{dependency}'.encode())
    commands = []

    def stop_after_root(command, **options):
        commands.append(command)
        raise failure

    monkeypatch.setattr(engines.subprocess, 'run', stop_after_root)
    with pytest.raises(engines.EnginePreparationError,
                       match=re.escape(f'{dependency}: isolated parser timed out after 10s.')) as caught:
        engines.source_fingerprint(source_tree)
    assert caught.value.__cause__ is failure
    assert len(commands) == 1


@pytest.mark.parametrize('failure', [
    subprocess.TimeoutExpired('manifest fixture', 10),
    subprocess.CalledProcessError(1, 'manifest fixture'),
])
def test_parse_subprocess_keeps_timeout_failure_and_join_boundary(source_tree, monkeypatch, failure):
    commands = []

    def fail(command, **options):
        commands.append((command, options))
        raise failure

    monkeypatch.setattr(engines.subprocess, 'run', fail)
    with pytest.raises(engines.EnginePreparationError) as caught:
        engines.source_fingerprint(source_tree)
    assert caught.value.__cause__ is failure
    if isinstance(failure, subprocess.TimeoutExpired):
        assert 'parser timed out after 10s' in str(caught.value)
    else:
        assert 'parser exited with code 1' in str(caught.value)
    assert len(commands) == 1
    command, options = commands[0]
    assert '-c' in command and Path(command[-1]).name == 'Cargo.toml'
    assert options['timeout'] == engines.MANIFEST_PARSE_TIMEOUT_SECONDS
    assert options['check'] and options['capture_output']


@pytest.mark.usefixtures('composed_budget')
def test_changed_external_declaration_and_new_dependency_are_not_cached(source_tree):
    before = engines.source_fingerprint(source_tree)
    root = source_tree / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text().replace('contracts/fixture.json', '../escape.json'))
    with pytest.raises(engines.EnginePreparationError, match='escapes'):
        engines.source_fingerprint(source_tree)
    root.write_text(root.read_text().replace('../escape.json', 'contracts/fixture.json'))
    assert engines.source_fingerprint(source_tree) == before
    root.write_text(root.read_text() + '\n[build-dependencies]\nmissing = { path = "../missing" }\n')
    with pytest.raises(engines.EnginePreparationError, match='no manifest'):
        engines.source_fingerprint(source_tree)


def test_manifest_parse_uses_remaining_preparation_budget(source_tree, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(engines.time, 'monotonic', lambda: 100.0)
    calls = []

    def parse(command, **options):
        calls.append(options['timeout'])
        return SimpleNamespace(stdout=json.dumps([command[-1], '', {'package': 'fixture'}]) + '\n')

    monkeypatch.setattr(engines.subprocess, 'run', parse)
    token = engines._deadline.set(123.0)
    try:
        assert engines._read_manifest(source_tree / 'packages/tofu-db/Cargo.toml')['package'] == 'fixture'
    finally:
        engines._deadline.reset(token)
    assert calls == [23.0]


def test_expired_preparation_does_not_launch_manifest_parser(source_tree, monkeypatch):
    monkeypatch.setattr(engines.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(engines.subprocess, 'run', lambda *a, **k: pytest.fail('expired preparation launched a child'))
    token = engines._deadline.set(100.0)
    try:
        with pytest.raises(engines.EnginePreparationError, match='preparation timed out'):
            engines._read_manifest(source_tree / 'packages/tofu-db/Cargo.toml')
    finally:
        engines._deadline.reset(token)
