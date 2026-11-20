"""Engine registration and fail-closed response cardinality on the adopted harness."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

import pytest

from tests.rust_parity.corpus import write_jsonl
from tests.rust_parity.runner import run_corpus, rust_core_command

pytestmark = pytest.mark.unit


def test_engine_boundary_delegates_to_the_real_python_reference(monkeypatch):
    from tests.rust_parity import python_impl
    from tests.rust_parity_engine import reference
    expected = [{'name': 'outcome', 'b64': base64.b64encode(b'owned').decode()}]
    seen = []
    monkeypatch.setattr(reference, 'engine_session', lambda value: seen.append(value) or expected)
    assert python_impl.BOUNDARIES['engine_session']({'model': 'fixture'}) == expected
    assert seen == [{'model': 'fixture'}]


def test_engine_fixture_packaging_uses_the_original_cases(tmp_path):
    from tests.rust_parity.corpus import read_jsonl
    from tests.rust_parity_engine.shared_harness import HERE, write_engine_corpus
    count = write_engine_corpus(tmp_path)
    source = [json.loads(line) for line in (HERE / 'cases.jsonl').read_text().splitlines() if line]
    packed = list(read_jsonl(tmp_path / 'engine_session.jsonl.gz'))
    assert count == len(source) == len(packed)
    assert [row['id'] for row in packed] == [row['name'] for row in source]
    assert [row['input'] for row in packed] == [row['input'] for row in source]


@pytest.mark.parametrize('extra', [False, True])
def test_reference_response_count_must_match_the_corpus(tmp_path, monkeypatch, extra):
    import tests.rust_parity.runner as runner
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    write_jsonl(corpus / 'unknown.jsonl.gz', [{'id': 'one', 'input': {}, 'session': 's'}])
    script = tmp_path / 'reference.py'
    script.write_text('import json,sys\n'
                      'for line in sys.stdin:\n'
                      ' r=json.loads(line)\n'
                      + (' print(json.dumps({"id":r["id"],"parts":[]})); print(json.dumps({"id":"extra","parts":[]}))\n'
                         if extra else ' pass\n'))
    monkeypatch.setattr(runner, 'python_command', lambda: [sys.executable, str(script)])
    report = run_corpus(corpus, ['python'], boundaries=['unknown'], check_recorded=False)
    assert not report['summary']['green']
    assert report['boundaries']['unknown']['referenceErrors']


def test_automatic_local_cargo_builds_are_not_an_implicit_parity_side_effect():
    with pytest.raises(SystemExit, match='remote_cargo'):
        rust_core_command()
