"""The golden-document corpus is intact and its comparator applies exactly the
reviewed tolerance list (docs/GOLDEN_DOCUMENTS.md).

The unit cases are hermetic. ``test_python_implementation_matches_public_corpus``
reruns the Python implementation over every public case (PyMuPDF, python-pptx,
Chromium previews, registered fonts) and is marked ``slow``.
"""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from scripts import golden_documents as gd

pytestmark = pytest.mark.unit


def _rules():
    return gd.load_tolerances()['rules']


def _compare(expected, actual, tmp_path, tolerance=None):
    diff = gd.Diff()
    gd.compare_value(expected, actual, '', {
        'expected_dir': str(tmp_path / 'e'), 'actual_dir': str(tmp_path / 'a'),
        'tolerance': tolerance or {}, 'rules': _rules()}, diff)
    return diff.items


def test_manifest_is_valid_and_covers_every_worker_format():
    manifest, tolerances = gd.load_json(gd.MANIFEST), gd.load_tolerances()
    assert gd.validate_manifest(manifest, tolerances) == []
    kinds = {case['kind'] for case in manifest['cases']}
    assert {'pdf', 'docx', 'pptx', 'pptd'} <= kinds
    features = {f for case in manifest['cases'] for f in case['features']}
    assert {'tables', 'figures', 'cjk', 'embedded-fonts', 'real-world'} <= features
    for case in manifest['cases']:
        for op in case['operations']:
            assert len(op['outputSha256']) == 64, (case['id'], op['key'])


def test_every_public_case_is_frozen_with_matching_digests():
    manifest = gd.load_json(gd.MANIFEST)
    public = [case for case in manifest['cases'] if case['visibility'] == 'public']
    assert public
    for case in public:
        assert gd.availability(case, private_root='/nonexistent') is None, case['id']


def test_mirrored_tests_exist():
    for case in gd.load_json(gd.MANIFEST)['cases']:
        for ref in case['mirrors']:
            path, _, name = ref.partition('::')
            with open(os.path.join(gd.REPO, path), encoding='utf-8') as handle:
                source = handle.read()
            assert not name or f'def {name}(' in source, ref


def test_private_cases_commit_only_digests():
    for case in gd.load_json(gd.MANIFEST)['cases']:
        if case['visibility'] != 'private':
            continue
        assert not os.path.exists(os.path.join(gd.CORPUS, case['input']))
        assert not os.path.exists(os.path.join(gd.CORPUS, 'expected', case['id']))


def test_structure_text_and_numbers_compare_exactly(tmp_path):
    expected = {'text': 'a\nb', 'n': 1.0, 'items': [1, 2], 'k': True}
    assert _compare(expected, json.loads(json.dumps(expected)), tmp_path) == []
    diffs = _compare(expected, {'text': 'a b', 'n': 1.001, 'items': [1], 'extra': 0, 'k': 1},
                     tmp_path)
    joined = '\n'.join(diffs)
    for fragment in ('/text: text differs', '/n: 1.001', '/items: array length',
                     '/extra: unexpected', '/k: 1 != expected True'):
        assert fragment in joined
    assert _compare({'n': 0.3}, {'n': 0.1 + 0.2}, tmp_path) == []


def test_operation_number_rule_widens_only_its_path(tmp_path):
    tolerance = {'numbers': [{'path': '/pages/*/links/*', 'abs': 0.002}]}
    expected = {'pages': [{'links': [{'x': 0.5}], 'width': 10}]}
    assert _compare(expected, {'pages': [{'links': [{'x': 0.501}], 'width': 10}]},
                    tmp_path, tolerance) == []
    assert _compare(expected, {'pages': [{'links': [{'x': 0.51}], 'width': 10}]},
                    tmp_path, tolerance)
    assert _compare(expected, {'pages': [{'links': [{'x': 0.5}], 'width': 10.001}]},
                    tmp_path, tolerance)


def _png(path, pixels):
    from PIL import Image
    os.makedirs(path.parent, exist_ok=True)
    image = Image.new('RGB', (100, 100), (255, 255, 255))
    for xy, colour in pixels:
        image.putpixel(xy, colour)
    image.save(path)


def test_raster_tolerance_accepts_antialiasing_noise_and_rejects_moved_content(tmp_path):
    blob = {'$blob': 'p.png', 'kind': 'raster'}
    block = [((x, y), (0, 0, 0)) for x in range(10, 30) for y in range(10, 30)]
    _png(tmp_path / 'e' / 'p.png', block)
    noisy = [(xy, (40, 40, 40) if xy[0] == 10 else c) for xy, c in block]
    _png(tmp_path / 'a' / 'p.png', noisy)
    assert _compare(blob, dict(blob), tmp_path) == []
    _png(tmp_path / 'a' / 'p.png', [((x + 40, y), c) for (x, y), c in block])
    assert 'raster mean' in ' '.join(_compare(blob, dict(blob), tmp_path))
    from PIL import Image
    Image.new('RGB', (100, 101), (255, 255, 255)).save(tmp_path / 'a' / 'p.png')
    assert 'raster size' in ' '.join(_compare(blob, dict(blob), tmp_path))


def _package(path, parts):
    os.makedirs(path.parent, exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


CORE = ('<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/">'
        '<dcterms:created>{}</dcterms:created></cp:coreProperties>')


def test_package_compares_canonical_xml_with_masked_timestamps(tmp_path):
    blob = {'$blob': 'deck.pptx', 'kind': 'ooxml'}
    _package(tmp_path / 'e' / 'deck.pptx', {
        'docProps/core.xml': CORE.format('2026-01-01T00:00:00Z'),
        'ppt/slides/slide1.xml': '<s a="1" b="2"><t>Hi</t></s>',
        'ppt/media/image1.png': b'\x89PNG-1'})
    _package(tmp_path / 'a' / 'deck.pptx', {
        'ppt/media/image1.png': b'\x89PNG-1',
        'ppt/slides/slide1.xml': '<s b="2" a="1">\n  <t>Hi</t>\n</s>',
        'docProps/core.xml': CORE.format('2027-05-05T10:00:00Z')})
    assert _compare(blob, dict(blob), tmp_path) == []
    _package(tmp_path / 'a' / 'deck.pptx', {
        'docProps/core.xml': CORE.format('2026-01-01T00:00:00Z'),
        'ppt/slides/slide1.xml': '<s a="1" b="2"><t>Ho</t></s>',
        'ppt/media/image1.png': b'\x89PNG-2',
        'ppt/extra.xml': '<x/>'})
    joined = '\n'.join(_compare(blob, dict(blob), tmp_path))
    assert 'slide1.xml: canonical XML differs' in joined
    assert 'image1.png: binary part bytes differ' in joined
    assert 'ppt/extra.xml: unexpected part' in joined


def test_normalization_replaces_output_root_and_font_urls(tmp_path):
    out = str(tmp_path / 'out')
    result = {'html': f"<img src='file://{out}/deck/media/a.png'> "
                      "src: url('file:///home/x/fonts/w400.woff2') format('woff2')"}
    normalized = gd.normalize_result(result, out, str(tmp_path / 'in.pptx'),
                                     {'normalize': ['font-face-url']})
    assert normalized['html'] == ("<img src='file://{out}/deck/media/a.png'> "
                                  "src: url('{font-file}') format('woff2')")
    assert gd.normalize_result({'a': [{'sizeKB': 3, 'page': 1}]}, out, out,
                               {'ignore': ['/a/*/sizeKB']}) == {'a': [{'page': 1}]}


def test_external_implementation_protocol_runs_a_command(tmp_path):
    script = tmp_path / 'impl.py'
    script.write_text('import json, os, sys\nop, inp, out, params = sys.argv[1:]\n'
                      'json.dump({"op": op, "params": json.loads(params)}, '
                      'open(os.path.join(out, "result.json"), "w"))\n')
    out = tmp_path / 'out'
    gd.run_operation(f'python3 {script} {{op}} {{input}} {{out}} {{params}}',
                     {'op': 'doc.extract', 'params': {'max_chars': 5}}, 'x.docx', str(out))
    assert json.loads((out / 'result.json').read_text()) == {
        'op': 'doc.extract', 'params': {'max_chars': 5}}


@pytest.mark.slow
@pytest.mark.timeout(1800)
def test_python_implementation_matches_public_corpus(tmp_path):
    report = tmp_path / 'report.json'
    code = gd.main(['check', '--private-root', str(tmp_path / 'no-private'),
                    '--out', str(tmp_path / 'out'), '--report', str(report)])
    data = json.loads(report.read_text())
    failures = [(c['id'], o['key'], o.get('diffs')) for c in data['cases']
                for o in c['operations'] if o['status'] != 'pass']
    assert code == 0 and not failures, failures
