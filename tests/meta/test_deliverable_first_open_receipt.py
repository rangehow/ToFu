"""Receipt-only checks; no Chromium launch or historical sample replay."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('first_open_receipt', ROOT / 'benchmarks/deliverable_first_open.py')
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def test_historical_aggregate_matches_retained_tables_and_medians():
    note = (ROOT / 'benchmarks/deliverable_first_open.md').read_text()
    history = json.loads(DRIVER.HISTORY.read_text())
    component_table = note.split('```text\n', 1)[1].split('```', 1)[0]
    expected = []
    for line in component_table.splitlines():
        repeat, size, arm, *values = line.split()
        kind, order = arm.split('/')
        expected.append({'repeat': int(repeat), 'size': size, 'kind': kind, 'order': order,
                         'componentsMs': dict(zip(history['componentColumns'], map(float, values)))})
    assert history['samples'] == expected
    assert len(expected) == 18
    assert len({(row['repeat'], row['size'], row['kind'], row['order']) for row in expected}) == 18
    assert max(row['componentsMs']['clickToVisibleFrame'] for row in expected) == 117.7
    medians = DRIVER.summarize_history()['timingMediansMs']
    for row in medians:
        size_bytes = history['fixtureSizesReported'][row['size']]['sourceBytes']
        pattern = rf'^\| {size_bytes:,} \| {row["kind"]}/{row["order"]} \| (.+) \|$'
        values = re.search(pattern, note, re.MULTILINE).group(1).split(' | ')
        assert [row[key] for key in history['componentColumns']] == list(map(float, values))
    assert history['provenance']['cannotInferFromGitHead'] is True
    assert history['provenance']['historicalCertification'] == 'incomplete'
    assert '/tmp/' not in json.dumps(history) and 'workspaces/' not in json.dumps(history)


def test_historical_request_clocks_are_separate_overlapping_intervals():
    history = json.loads(DRIVER.HISTORY.read_text())
    note = (ROOT / 'benchmarks/deliverable_first_open.md').read_text()
    request_table = note.split('### All document request clocks', 1)[1].split('## Pinpointed', 1)[0]
    rows = []
    for line in request_table.splitlines():
        if not re.match(r'^\| [012] (small|large) ', line):
            continue
        identity, meta, versions, raw, decoded, shell = line.strip('| ').split(' | ')
        repeat, size, order = identity.split()
        rows.append([int(repeat), size, order, *map(float, meta.split('/')),
                     *map(float, versions.split('/')), *map(float, raw.split('/')),
                     float(decoded), float(shell)])
    assert rows == history['requestClocksMs']
    assert len(rows) == 12
    for values in rows:
        row = dict(zip(history['requestClockColumns'], values))
        assert row['rawStart'] >= row['metaDecoded']
        assert row['versionsStart'] >= row['metaDecoded']
        assert max(row['versionsStart'], row['rawStart']) < min(row['versionsRelease'], row['rawRelease'])
        if row['order'] == 'body-first':
            sample = next(sample for sample in history['samples'] if all(
                sample[key] == row[key] for key in ('repeat', 'size', 'order')))
            assert sample['componentsMs']['clickToVisibleFrame'] < row['versionsRelease']
    assert DRIVER.summarize_history()['countsReported'] == history['countsReported']


@pytest.fixture
def receipt_sources(tmp_path, monkeypatch):
    fixture = tmp_path / 'fixture.ts'
    owner = tmp_path / 'artifact.js'
    fixture.write_text("import '#tofu-test-root/frontend/src/styles/application/00-design-tokens.css';\n"
                       '// RETAINED_ARTIFACT_OWNER\nexport const value = 7;\n')
    owner.write_text('const isolatedOwner = 3;\n')
    monkeypatch.setattr(DRIVER, 'FIXTURE', fixture)
    monkeypatch.setattr(DRIVER, 'ARTIFACTS', owner)
    return fixture, owner


def test_build_receipt_records_originals_entry_and_emitted_bytes(tmp_path, receipt_sources):
    from tests._jsdom import require_node_modules
    require_node_modules('vite')
    fixture, owner = receipt_sources
    output = tmp_path / 'build'
    bundle, styles, receipt = DRIVER.build(output)
    assert receipt['format'] == 'tofu.first-open-build/v2'
    originals = {'fixture.ts': fixture, 'artifact-owner.js': owner,
                 'driver.py': DRIVER.DRIVER, 'bundler.mjs': DRIVER.BUNDLER}
    for name, path in originals.items():
        value = path.read_bytes()
        assert (output / 'originals' / name).read_bytes() == value
        assert receipt['originals'][name] == {'sha256': hashlib.sha256(value).hexdigest(), 'sizeBytes': len(value)}
    generated = fixture.read_text().replace('// RETAINED_ARTIFACT_OWNER', owner.read_text()).encode()
    assert (output / 'entry.ts').read_bytes() == generated
    assert receipt['generatedEntry']['sha256'] == hashlib.sha256(generated).hexdigest()
    assert receipt['bundle']['sha256'] == hashlib.sha256(bundle.encode()).hexdigest()
    assert receipt['combinedStyles']['sha256'] == hashlib.sha256(styles.encode()).hexdigest()
    assert receipt['bundlerComplete'] is True
    assert any(row['id'] == 'generated-entry' and row['sha256'] == receipt['generatedEntry']['sha256']
               for row in receipt['inputs'])
    assert any(row['id'].endswith('.css') for row in receipt['outputs'])
    for row in receipt['outputs']:
        assert row['sha256'] == hashlib.sha256((output / row['id']).read_bytes()).hexdigest()
    assert json.loads((output / 'inputs.json').read_text()) == receipt
    assert str(tmp_path) not in json.dumps(receipt)
    assert str(ROOT) not in json.dumps(receipt)


@pytest.mark.parametrize('caller_cwd', ['repository', 'outside'])
def test_relative_output_cli_publishes_path_free_generated_entry(tmp_path, monkeypatch, receipt_sources, caller_cwd):
    from tests._jsdom import require_node_modules
    require_node_modules('vite')
    caller = ROOT if caller_cwd == 'repository' else tmp_path
    destination = tmp_path / 'relative-evidence'
    relative_output = os.path.relpath(destination, caller)
    monkeypatch.chdir(caller)
    monkeypatch.setattr(sys, 'argv', ['first-open', '--output', relative_output])

    class BeforeChromium(Exception):
        pass

    def stop_before_chromium():
        raise BeforeChromium

    monkeypatch.setattr(DRIVER, 'sync_playwright', stop_before_chromium)
    with pytest.raises(BeforeChromium):
        DRIVER.main()
    published = tmp_path / 'relative-evidence' / 'build-receipt'
    receipt = json.loads((published / 'inputs.json').read_text())
    assert receipt['format'] == 'tofu.first-open-build/v2'
    entry = (published / 'entry.ts').read_bytes()
    digest = hashlib.sha256(entry).hexdigest()
    assert receipt['generatedEntry']['sha256'] == digest
    assert any(row['id'] == 'generated-entry' and row['sha256'] == digest for row in receipt['inputs'])
    assert str(tmp_path) not in json.dumps(receipt)
    assert str(ROOT) not in json.dumps(receipt)
    assert not list(published.parent.glob('.first-open-*'))


@pytest.mark.parametrize('existing_receipt', [False, True])
def test_failed_build_never_publishes_raw_manifest(tmp_path, monkeypatch, receipt_sources, existing_receipt):
    destination = tmp_path / 'evidence' / 'build-receipt'
    prior = {}
    if existing_receipt:
        from tests._jsdom import require_node_modules
        require_node_modules('vite')
        DRIVER.build(destination)
        prior = {path.relative_to(destination): path.read_bytes() for path in destination.rglob('*') if path.is_file()}
    intermediate = []

    def emit_raw_without_css(command, **kwargs):
        manifest = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('--inputs-manifest=')))
        manifest.write_text(json.dumps({'inputs': [str(tmp_path / 'private-original.ts')], 'outputs': []}))
        intermediate.append(manifest)
        assert not manifest.is_relative_to(destination)
        if existing_receipt:
            assert (destination / 'inputs.json').read_bytes() == prior[Path('inputs.json')]
        else:
            assert not destination.exists()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(DRIVER.subprocess, 'run', emit_raw_without_css)
    with pytest.raises(AssertionError, match='Bundled CSS is required'):
        DRIVER.build(destination)
    assert intermediate and all(not path.exists() for path in intermediate)
    if existing_receipt:
        assert {path.relative_to(destination): path.read_bytes() for path in destination.rglob('*') if path.is_file()} == prior
    else:
        assert not destination.exists()
    assert not list(destination.parent.glob('.first-open-*'))
