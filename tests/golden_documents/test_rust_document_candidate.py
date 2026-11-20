"""Synthetic checker contracts, not execution or parity evidence for Rust."""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest

from scripts import golden_documents as gd
from tests.golden_documents import check_rust_document_candidate as candidate

pytestmark = pytest.mark.unit


@pytest.fixture
def actual(tmp_path):
    expected = (Path(gd.CORPUS) / 'expected' / 'synthetic-pptx-import-fidelity'
                / 'slides.import_pptx')
    return Path(shutil.copytree(expected, tmp_path / 'candidate'))


def test_exact_fixture_match_is_comparison_only(actual, capsys):
    assert candidate.check_candidate(actual) == []
    assert candidate.main(['--candidate-dir', str(actual)]) == 0
    output = capsys.readouterr().out
    assert 'comparison only' in output
    assert 'Rust producer/source unverified' in output


def test_documented_float_rounding_passes_but_moved_content_fails(actual):
    path = actual / 'result.json'
    result = json.loads(path.read_text())
    bounds = result['deck']['pages/01.page']['elements'][0]['bounds']
    original = bounds[1]
    bounds[1] = math.nextafter(original, math.inf)
    assert 0 < bounds[1] - original < 1e-9
    path.write_text(json.dumps(result))
    assert candidate.check_candidate(actual) == []
    bounds[1] = original + 0.01
    path.write_text(json.dumps(result))
    assert any('/bounds/1:' in diff for diff in candidate.check_candidate(actual))


@pytest.mark.parametrize('change', ['text', 'missing-field', 'extra-field', 'array', 'type', 'bool'])
def test_semantic_mismatches_never_pass(actual, change):
    path = actual / 'result.json'
    result = json.loads(path.read_text())
    page = result['deck']['pages/01.page']
    if change == 'text':
        page['elements'][0]['content']['text'] += ' '
    elif change == 'missing-field':
        del page['background']
    elif change == 'extra-field':
        page['unexpected'] = True
    elif change == 'array':
        page['elements'].pop()
    elif change == 'type':
        page['elements'][0]['bounds'][0] = '36.0'
    else:
        page['elements'][0]['bounds'][0] = True
    path.write_text(json.dumps(result))
    assert candidate.check_candidate(actual)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '1e309'])
def test_nonfinite_numbers_are_rejected(actual, value, capsys):
    path = actual / 'result.json'
    path.write_text(path.read_text().replace('21.6', value, 1))
    assert candidate.main(['--candidate-dir', str(actual)]) == 2
    assert 'non-finite' in capsys.readouterr().err


@pytest.mark.parametrize('missing', ['directory', 'result'])
def test_missing_candidate_is_rejected(tmp_path, missing, capsys):
    path = tmp_path / 'absent'
    if missing == 'result':
        path.mkdir()
    assert candidate.main(['--candidate-dir', str(path)]) == 2
    assert 'REJECTED' in capsys.readouterr().err


@pytest.mark.parametrize('change', ['missing', 'different', 'escape'])
def test_candidate_media_cannot_be_missing_changed_or_reference_expected(actual, change):
    media = actual / 'deck/media/slide03_03.png'
    if change == 'missing':
        media.unlink()
        assert any('blob file' in diff for diff in candidate.check_candidate(actual))
    elif change == 'different':
        media.write_bytes(b'changed synthetic media')
        assert any('bytes differ' in diff for diff in candidate.check_candidate(actual))
    else:
        path = actual / 'result.json'
        result = json.loads(path.read_text())
        result['deck']['media/slide03_03.png']['$blob'] = str(
            Path(gd.CORPUS) / 'expected' / 'synthetic-pptx-import-fidelity'
            / 'slides.import_pptx/deck/media/slide03_03.png')
        path.write_text(json.dumps(result))
        with pytest.raises(ValueError, match='candidate directory'):
            candidate.check_candidate(actual)


def test_reference_directory_and_result_symlink_are_rejected(actual):
    expected = (Path(gd.CORPUS) / 'expected' / 'synthetic-pptx-import-fidelity'
                / 'slides.import_pptx')
    with pytest.raises(ValueError, match='separate from corpus'):
        candidate.check_candidate(expected)
    result = actual / 'result.json'
    result.unlink()
    result.symlink_to(expected / 'result.json')
    with pytest.raises(ValueError, match='locally contained'):
        candidate.check_candidate(actual)


def test_extra_blob_metadata_is_rejected(actual):
    path = actual / 'result.json'
    result = json.loads(path.read_text())
    result['deck']['media/slide03_03.png']['unexpected'] = 'not a tolerance'
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match='blob fields'):
        candidate.check_candidate(actual)


def test_ancestor_candidate_cannot_alias_a_nested_reference_tree(actual, tmp_path, monkeypatch):
    corpus = tmp_path / 'corpus'
    expected = corpus / 'expected/synthetic-pptx-import-fidelity/slides.import_pptx'
    shutil.copytree(actual, expected)
    relative_input = Path('inputs/synthetic/pptx_import_fidelity.pptx')
    input_path = corpus / relative_input
    input_path.parent.mkdir(parents=True)
    shutil.copyfile(Path(gd.CORPUS) / relative_input, input_path)
    monkeypatch.setattr(gd, 'CORPUS', str(corpus))
    (tmp_path / 'result.json').symlink_to(expected / 'result.json')
    (tmp_path / 'deck').symlink_to(expected / 'deck', target_is_directory=True)
    with pytest.raises(ValueError, match='separate from corpus'):
        candidate.check_candidate(tmp_path)


@pytest.mark.parametrize('payload', ['{', 'deep-malformed', 'deep-complete'])
def test_malformed_or_deep_json_is_rejected(actual, payload, capsys):
    if payload.startswith('deep-'):
        depth = sys.getrecursionlimit() + 1
        payload = '[' * depth + ('0' + ']' * depth if payload == 'deep-complete' else '')
    (actual / 'result.json').write_text(payload)
    assert candidate.main(['--candidate-dir', str(actual)]) == 2
    assert 'REJECTED' in capsys.readouterr().err
