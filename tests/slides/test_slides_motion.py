"""tests/slides/test_slides_motion.py — PPTD semantic motion → OOXML.

Executable specification for ``lib/slides/motion.py``: the page ``motion``
vocabulary (reveal / emphasize / continues) validates zero-LLM, and the
exporter writes it as PowerPoint built-in effects in CT_Slide order:
build-by-paragraph fade entrances, a Pulse emphasis, fade transitions and a
Morph transition (fade fallback) whose shared objects carry ``!!`` names.
The LibreOffice round trip runs when ``soffice`` is on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.slides.pptd import parse_deck, validate_deck  # noqa: E402

pytestmark = pytest.mark.unit

_P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_P159 = '{http://schemas.microsoft.com/office/powerpoint/2015/09/main}'

_POINTS = '''background: {type: solid, color: "#FFFFFF"}
motion:
  reveal: [points]
elements:
  - elementId: title
    elementType: text
    bounds: [72, 60, 1136, 80]
    content: {text: "Why latency matters"}
  - elementId: points
    elementType: text
    bounds: [72, 180, 1136, 400]
    content:
      text: "<ul><li>Users abandon after 3 s</li><li>Each 100 ms costs 1%</li><li>So we cut p95 first</li></ul>"
'''

_RESULT = '''background: {type: solid, color: "#FFFFFF"}
motion:
  reveal: [chartbox]
  emphasize: kpi
components:
  - componentId: kpi
    componentType: metric
    bounds: [72, 160, 520, 400]
    value: "-42%"
    label: "p95 latency"
elements:
  - elementId: title
    elementType: text
    bounds: [72, 60, 1136, 80]
    content: {text: "The result"}
  - elementId: chartbox
    elementType: shape
    bounds: [680, 160, 520, 400]
    shapeName: roundRect
    fill: {type: solid, color: "#16283C"}
'''

_CONTINUED = '''background: {type: solid, color: "#FFFFFF"}
motion:
  continues: true
elements:
  - elementId: title
    elementType: text
    bounds: [72, 60, 1136, 80]
    content: {text: "The result, in context"}
  - elementId: chartbox
    elementType: shape
    bounds: [72, 200, 300, 220]
    shapeName: roundRect
    fill: {type: solid, color: "#16283C"}
'''


def _deck(tmp_path, pages):
    import yaml
    root = tmp_path / 'deck'
    (root / 'pages').mkdir(parents=True)
    names = []
    for index, text in enumerate(pages, 1):
        name = f'pages/{index:02d}.page'
        (root / name).write_text(text, encoding='utf-8')
        names.append(name)
    theme = {'colors': {'accent': '#C0652B'},
             'textStyles': {'title': {'fontSize': 40}, 'body': {'fontSize': 22},
                            'bignum': {'fontSize': 72}, 'caption': {'fontSize': 12}}}
    (root / 'deck.pptd').write_text(yaml.safe_dump(
        {'version': 'v2', 'title': 'motion', 'size': [1280, 720],
         'theme': theme, 'pages': names}), encoding='utf-8')
    return parse_deck(str(root / 'deck.pptd'))


def _export(tmp_path, pages):
    pytest.importorskip('pptx')
    from lib.slides.export_pptx import export_pptx
    out = str(tmp_path / 'out.pptx')
    summary = export_pptx(_deck(tmp_path, pages), out, embed_fonts=False)
    return out, summary


def _slide(out, number):
    from lxml import etree
    with zipfile.ZipFile(out) as archive:
        return etree.fromstring(archive.read(f'ppt/slides/slide{number}.xml'))


def _shape_ids(root):
    return {node.get('name'): node.get('id') for node in root.iter(_P + 'cNvPr')}


def _click_groups(root):
    seq = root.find(f'{_P}timing/{_P}tnLst/{_P}par/{_P}cTn/{_P}childTnLst/{_P}seq')
    assert seq is not None
    assert seq.find(_P + 'cTn').get('nodeType') == 'mainSeq'
    return seq.findall(f'{_P}cTn/{_P}childTnLst/{_P}par')


class TestMotionValidation:
    def test_unknown_key_and_target_are_findings(self, tmp_path):
        page = _POINTS.replace('reveal: [points]',
                               'reveal: [nope]\n  sparkle: true')
        findings = validate_deck(_deck(tmp_path, [page]))
        assert any("unknown key 'sparkle'" in f for f in findings)
        assert any("no element or component 'nope'" in f for f in findings)

    def test_first_page_continues_is_a_noop(self, tmp_path):
        out, summary = _export(tmp_path, [_CONTINUED])
        assert (summary['fadeTransitions'], summary['morphTransitions']) == (1, 0)
        assert '!!title' not in _shape_ids(_slide(out, 1))

    def test_author_page_gate_sees_motion(self, tmp_path):
        from lib.slides.author import _validate_page_text
        deck = _deck(tmp_path, [_POINTS])
        findings = _validate_page_text(deck, 'pages/01.page', _POINTS.replace(
            'reveal: [points]', 'reveal: [missing]'))
        assert any("no element or component 'missing'" in f for f in findings)

    def test_double_reveal_and_bad_types(self, tmp_path):
        page = _POINTS.replace('reveal: [points]',
                               'reveal: [points, points]\n  emphasize: [x]\n'
                               '  continues: "yes"')
        findings = validate_deck(_deck(tmp_path, [page]))
        assert any('revealed twice' in f for f in findings)
        assert any('emphasize: must be one' in f for f in findings)
        assert any('continues: must be boolean' in f for f in findings)

    def test_component_targets_resolve(self, tmp_path):
        assert validate_deck(_deck(tmp_path, [_POINTS, _RESULT, _CONTINUED])) == []


class TestMotionOoxml:
    def test_build_by_paragraph_reveal(self, tmp_path):
        out, summary = _export(tmp_path, [_POINTS])
        root = _slide(out, 1)
        children = [child.tag for child in root]
        assert children.index(_P + 'transition') < children.index(_P + 'timing')
        spid = _shape_ids(root)['points']
        clicks = _click_groups(root)
        assert len(clicks) == 3                       # one click per bullet
        for paragraph, click in enumerate(clicks):
            assert click.find(f'{_P}cTn/{_P}stCondLst/{_P}cond').get('delay') == 'indefinite'
            effect = click.find(f'.//{_P}cTn[@presetClass]')
            assert (effect.get('presetClass'), effect.get('presetID'),
                    effect.get('nodeType')) == ('entr', '10', 'clickEffect')
            ranges = {(t.getparent().get('spid'), r.get('st'), r.get('end'))
                      for t in click.iter(_P + 'txEl') for r in t.iter(_P + 'pRg')}
            assert ranges == {(spid, str(paragraph), str(paragraph))}
            assert click.find(f'.//{_P}animEffect').get('filter') == 'fade'
            assert click.find(f'.//{_P}set//{_P}attrName').text == 'style.visibility'
        builds = root.findall(f'{_P}timing/{_P}bldLst/{_P}bldP')
        assert [(b.get('spid'), b.get('build')) for b in builds] == [(spid, 'p')]
        ids = [node.get('id') for node in root.iter(_P + 'cTn')]
        assert len(ids) == len(set(ids))
        assert summary['buildSteps'] == 3 and summary['animatedSlides'] == 1
        assert summary['packageValidation']['animationSteps'] == 3

    def test_component_reveal_then_key_number_pulse(self, tmp_path):
        out, _summary = _export(tmp_path, [_RESULT])
        root = _slide(out, 1)
        names = _shape_ids(root)
        clicks = _click_groups(root)
        assert len(clicks) == 2
        entrance = clicks[0].find(f'.//{_P}cTn[@presetClass]')
        assert entrance.get('presetClass') == 'entr'
        assert {t.get('spid') for t in clicks[0].iter(_P + 'spTgt')} == {names['chartbox']}
        pulse = clicks[1].find(f'.//{_P}cTn[@presetClass]')
        assert (pulse.get('presetClass'), pulse.get('presetID')) == ('emph', '26')
        assert {t.get('spid') for t in clicks[1].iter(_P + 'spTgt')} == {names['kpi--value']}
        scale = clicks[1].find(f'.//{_P}animScale')
        assert scale.find(f'{_P}cBhvr/{_P}cTn').get('autoRev') == '1'

    def test_default_fade_and_morph_continuity(self, tmp_path):
        out, summary = _export(tmp_path, [_POINTS, _RESULT, _CONTINUED])
        assert (summary['fadeTransitions'], summary['morphTransitions']) == (2, 1)
        first = _slide(out, 1)
        assert first.find(f'{_P}transition/{_P}fade') is not None
        previous, continued = _slide(out, 2), _slide(out, 3)
        alternate = continued.find(_MC + 'AlternateContent')
        assert continued.find(_P + 'transition') is None
        choice = alternate.find(_MC + 'Choice')
        assert choice.get('Requires') == 'p159'
        assert choice.find(f'{_P}transition/{_P159}morph').get('option') == 'byObject'
        assert alternate.find(f'{_MC}Fallback/{_P}transition/{_P}fade') is not None
        assert {'!!title', '!!chartbox'} <= set(_shape_ids(continued))
        assert {'!!title', '!!chartbox'} <= set(_shape_ids(previous))
        assert 'kpi--value' in _shape_ids(previous)  # unshared objects keep ids
        assert continued.find(_P + 'timing') is None

    def test_page_without_motion_has_no_timing(self, tmp_path):
        out, summary = _export(tmp_path, [_POINTS.replace(
            'motion:\n  reveal: [points]\n', '')])
        root = _slide(out, 1)
        assert root.find(_P + 'timing') is None
        assert summary['buildSteps'] == 0 and summary['fadeTransitions'] == 1

    def test_package_qa_rejects_dangling_animation_target(self, tmp_path):
        from lib.slides.pptx_qa import inspect_pptx_package
        out, _summary = _export(tmp_path, [_POINTS])
        broken = str(tmp_path / 'broken.pptx')
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(broken, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == 'ppt/slides/slide1.xml':
                    data = data.replace(b'<p:bldP spid="3"', b'<p:bldP spid="99"')
                dst.writestr(info, data)
        verdict = inspect_pptx_package(broken, expected_pages=1)
        assert not verdict['ok']
        assert 'build list names missing shape 99' in verdict['findings'][0]


@pytest.mark.slow
@pytest.mark.timeout(0)
@pytest.mark.skipif(shutil.which('soffice') is None,
                    reason='LibreOffice (soffice) is not on PATH')
def test_libreoffice_imports_motion(tmp_path):
    out, _summary = _export(tmp_path, [_POINTS, _RESULT, _CONTINUED])
    converted = tmp_path / 'odp'
    subprocess.run(
        [shutil.which('soffice'), '--headless', '--norestore',
         f'-env:UserInstallation=file://{tmp_path / "lo"}',
         '--convert-to', 'odp', '--outdir', str(converted), out],
        check=True, capture_output=True)
    with zipfile.ZipFile(converted / 'out.odp') as archive:
        content = archive.read('content.xml').decode()
    assert content.count('<draw:page ') == 3
    assert content.count('presentation:preset-id="ooo-entrance-fade-in"') == 4
    # LibreOffice names PowerPoint's emphasis preset 26 (Pulse) flash-bulb.
    assert 'presentation:preset-id="ooo-emphasis-flash-bulb"' in content
    assert content.count('presentation:node-type="on-click"') == 5
    assert 'smil:type="fade"' in content
