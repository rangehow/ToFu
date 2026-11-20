"""The Phase 0 test classification manifest builds, complete, from the work tree.

docs/RUST_REWRITE_EVIDENCE.md "Phased plan": before Phase 1 every test is
classified black-box or white-box; each "Behaviours that are easy to lose" row
names the tests that pin it or states its gap. The manifest is derived on
demand, never committed, so it cannot go stale ("Progress"). Generator and
rules: ``scripts/gen_test_classification.py``.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location(
        'gen_test_classification', ROOT / 'scripts/gen_test_classification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generator():
    return _generator()


@pytest.fixture(scope='module')
def generated(generator):
    # Building validates the parity mapping and raises on any unmapped row,
    # missing file or missing test function.
    return generator.build_manifest(ROOT)


def test_no_committed_manifest_can_go_stale():
    assert not (ROOT / 'tests/parity/test_classification_v1.json').exists()


def test_cli_writes_the_manifest_it_builds(generator, generated, tmp_path):
    target = tmp_path / 'manifest.json'
    assert generator.main(['--output', str(target)]) == 0
    assert target.read_text('utf-8') == generator.render(generated)


def test_every_easy_to_lose_row_is_mapped_once(generator, generated):
    rows = generator.parse_parity_rows((ROOT / generator.EVIDENCE_DOC).read_text('utf-8'))
    mapped = Counter((case['area'], case['behaviour']) for case in generated['parityCases'])
    assert set(mapped) == {(row['area'], row['behaviour']) for row in rows}
    assert set(mapped.values()) == {1}
    for case in generated['parityCases']:
        assert case['tests'] or case.get('gap'), case['behaviour']


def test_a_doc_row_without_a_mapping_fails_generation(generator):
    rows = [{'area': 'Wire', 'behaviour': 'A row added to the doc', 'source': ''}]
    with pytest.raises(generator.GenerationError, match='doc row without mapping'):
        generator.build_parity_cases(ROOT, rows, {'cases': []}, set())


def test_referenced_parity_tests_exist(generated):
    for case in generated['parityCases']:
        for reference in case['tests']:
            path = ROOT / reference['path']
            assert path.is_dir() if reference['path'].endswith('/') else path.is_file(), reference
            for name in reference.get('functions', ()):
                assert name.split('::')[-1] in path.read_text('utf-8'), (reference['path'], name)


def test_every_test_file_is_classified_exactly_once(generator, generated):
    test_files = sorted(p for p in generator._git_ls_files(ROOT, 'tests')
                        if generator.is_python_test(p) or generator.is_js_test(p))
    classified = [entry['path'] for entry in generated['files']]
    assert classified == test_files
    assert len(set(classified)) == len(classified)
    for entry in generated['files']:
        assert entry['kind'] in ('black-box', 'white-box')
        assert (entry['kind'] == 'black-box') == bool(entry['surfaces'])
        assert set(entry['surfaces']) <= set(generator.SURFACES)
    totals = generated['totals']
    assert totals['files'] == len(classified)
    assert totals['tests'] == sum(totals['byKind'][kind]['tests'] for kind in ('black-box', 'white-box'))


def test_rules_classify_by_explicit_evidence(generator):
    def classify(source, fixtures=frozenset(generator.HTTP_BASE_FIXTURES)):
        return generator.classify_python('tests/meta/test_test_classification_manifest.py', source.encode(), set(fixtures))

    assert classify('def test_a(flask_client):\n    flask_client.get("/")\n')['surfaces'] == ['http']
    stream = classify('def test_a(live_server):\n    assert "text/event-stream"\n')
    assert stream['surfaces'] == ['http', 'sse']
    server = classify('import subprocess\nARGV = ["python3", "server.py"]\n'
                      'def test_a():\n    subprocess.Popen(ARGV)\n')
    assert server['signals'] == ['process:launch:Popen:server.py']
    # A literal elsewhere in a module that runs Git is not a server launch.
    scan = classify('import subprocess\nNAMES = ["server.py"]\n'
                    'def test_a():\n    subprocess.run(["git", "ls-files"])\n')
    assert scan['kind'] == 'white-box'
    wire = classify('from lib.storage.protocol import recv_frame\ndef test_a():\n    pass\n')
    assert wire['signals'] == ['storage-wire:import:lib.storage.protocol']
    assert classify('class TestA:\n    def test_a(self):\n        async def test_b():\n'
                    '            pass\n')['testCount'] == 2
