"""Native fit QA projects a PPTX to what PowerPoint/WPS show on open."""

from __future__ import annotations

import zipfile

import pytest

from evaluations.slides_bench import native_fit

pytestmark = pytest.mark.unit


def _pptx(tmp_path, body_fit: bytes):
    path = tmp_path / 'deck.pptx'
    slide = (b'<p:sld xmlns:a="a" xmlns:p="p"><p:txBody><a:bodyPr>' + body_fit
             + b'</a:bodyPr></p:txBody></p:sld>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ppt/slides/slide1.xml', slide)
        archive.writestr('ppt/presentation.xml', b'<keep/>')
    return path


@pytest.mark.parametrize('body_fit', [
    b'<a:normAutofit/>', b'<a:normAutofit fontScale="62500" lnSpcReduction="20000"/>',
    b'<a:spAutoFit/>'])
def test_every_autofit_becomes_no_autofit_and_other_parts_are_kept(tmp_path, body_fit):
    source = _pptx(tmp_path, body_fit)
    assert native_fit.autofit_counts(str(source))['noAutofit'] == 0
    forced = tmp_path / 'forced.pptx'
    native_fit.force_no_autofit(str(source), str(forced))
    counts = native_fit.autofit_counts(str(forced))
    assert counts == {'bare_normAutofit': 0, 'normAutofit_with_fontScale': 0,
                      'spAutoFit': 0, 'noAutofit': 1}
    with zipfile.ZipFile(forced) as archive:
        assert archive.read('ppt/presentation.xml') == b'<keep/>'


def test_a_span_leaving_its_frame_is_reported_and_one_inside_is_not():
    frames = [('title', 100, 100, 400, 140), ('card', 0, 0, 1280, 720)]
    spans = [{'text': 'fits', 'bbox': (110, 105, 300, 145), 'origin': (110, 135)},
             {'text': 'runs past the bottom', 'bbox': (110, 130, 300, 160),
              'origin': (110, 152)},
             {'text': 'runs past the right edge', 'bbox': (110, 105, 420, 135),
              'origin': (110, 130)}]
    findings = native_fit.outside_spans(spans, frames[:1])
    # The em box of 'fits' pokes 5pt below the frame; its baseline is inside.
    assert [f['text'] for f in findings] == ['runs past the bottom',
                                             'runs past the right edge']
    assert findings[1]['frame'] == 'title'
    # A larger text frame that holds the lines absorbs them.
    assert native_fit.outside_spans(spans, frames) == []


def test_font_keys_match_subset_pdf_names_and_declared_typefaces():
    assert native_fit._face_key('BAAAAA+NotoSansSC-Regular') == \
        native_fit._face_key('Noto Sans SC') == 'notosanssc'
    assert native_fit._face_key('DAAAAA+DejaVuSans-Bold') != \
        native_fit._face_key('Liter')
