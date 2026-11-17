"""Native fit QA: what PowerPoint and WPS show when they open a deck.

PowerPoint and WPS draw ``normAutofit`` text at its stored ``fontScale`` until
the text is edited; LibreOffice refits it on open and so hides overflow. This
check writes a copy with every text body set to ``noAutofit`` (the open-time
view at authored size), renders it with the benchmark's LibreOffice
(``render.py``) and reports every text span whose glyph box leaves the frame
that holds it. A span drawn in a font the deck never names (LibreOffice
substituted it, e.g. DejaVu Sans for an unavailable Latin face) has other
metrics than PowerPoint will use, so it is reported apart and does not decide
the verdict.

Usage: python -m evaluations.slides_bench.native_fit <deck.pptx> <out_dir>
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile

_EMU_PER_PT = 12700
#: LibreOffice positions text in twips (1/20 pt); its PDF coordinates are
#: accurate to that unit, so a line that starts on a frame edge reads 0.04 pt
#: off it.
_LIBREOFFICE_UNIT_PT = 1 / 20
_BODY_FIT = re.compile(rb'<a:(?:normAutofit|spAutoFit)\b[^>]*?(?:/>|>.*?</a:(?:normAutofit|spAutoFit)>)',
                       re.S)


def autofit_counts(pptx: str) -> dict:
    counts = {'bare_normAutofit': 0, 'normAutofit_with_fontScale': 0,
              'spAutoFit': 0, 'noAutofit': 0}
    with zipfile.ZipFile(pptx) as archive:
        for name in archive.namelist():
            if not re.match(r'ppt/slides/slide\d+\.xml$', name):
                continue
            xml = archive.read(name).decode('utf-8')
            counts['bare_normAutofit'] += len(re.findall(r'<a:normAutofit\s*/>', xml))
            counts['normAutofit_with_fontScale'] += len(
                re.findall(r'<a:normAutofit[^>]*fontScale', xml))
            counts['spAutoFit'] += len(re.findall(r'<a:spAutoFit', xml))
            counts['noAutofit'] += len(re.findall(r'<a:noAutofit', xml))
    return counts


def force_no_autofit(pptx: str, out_path: str) -> None:
    with zipfile.ZipFile(pptx) as source, \
            zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if re.match(r'ppt/slides/slide\d+\.xml$', item.filename):
                data = _BODY_FIT.sub(b'<a:noAutofit/>', data)
            target.writestr(item, data)


def text_frames(pptx: str) -> list[list[tuple]]:
    """Per slide: (name, left, top, right, bottom) in points for every frame
    that draws text (text bodies, tables, charts)."""
    from pptx import Presentation
    slides = []
    for slide in Presentation(pptx).slides:
        frames = []
        for shape in slide.shapes:
            drawn = (getattr(shape, 'has_table', False) or getattr(shape, 'has_chart', False)
                     or (shape.has_text_frame and shape.text_frame.text.strip()))
            if drawn:
                left, top = shape.left / _EMU_PER_PT, shape.top / _EMU_PER_PT
                frames.append((shape.name, left, top, left + shape.width / _EMU_PER_PT,
                               top + shape.height / _EMU_PER_PT))
        slides.append(frames)
    return slides


def declared_typefaces(pptx: str) -> set[str]:
    with zipfile.ZipFile(pptx) as archive:
        return {_face_key(face) for name in archive.namelist()
                if re.match(r'ppt/(slides/slide|charts/chart)\d+\.xml$', name)
                for face in re.findall(r'typeface="([^"]+)"',
                                       archive.read(name).decode('utf-8'))}


def _face_key(name: str) -> str:
    """``ABCDEF+NotoSansSC-Bold`` and ``Noto Sans SC`` both key as
    ``notosanssc``."""
    name = name.split('+', 1)[-1]
    return re.sub(r'[^a-z0-9]', '', name.split('-', 1)[0].lower())


def outside_spans(spans: list[dict], frames: list[tuple]) -> list[dict]:
    """Spans that leave the frame holding their baseline.

    A PDF span's box is the font's em box (ascent + descent), which pokes past
    a tight text box with no ink there (big digits have none below the
    baseline), so a line counts as inside when its baseline lies within the
    frame's height and its advance within the frame's width: an overflowing
    PowerPoint text box shows as lines whose baseline falls below the box.
    """
    findings = []
    for span in spans:
        x0, y0, x1, y1 = span['bbox']
        baseline = span.get('origin', (x0, y1))[1]
        cx = (x0 + x1) / 2
        unit = _LIBREOFFICE_UNIT_PT
        holders = [f for f in frames
                   if f[1] <= cx <= f[3] and f[2] - unit <= baseline <= f[4] + unit]
        inside = [f for f in holders if f[1] - unit <= x0 and x1 <= f[3] + unit]
        if not inside:
            frame = min(holders, key=lambda f: (f[3] - f[1]) * (f[4] - f[2]),
                        default=None)
            findings.append({
                'text': span['text'][:60], 'bbox_pt': [round(v, 1) for v in span['bbox']],
                'baseline_pt': round(baseline, 1),
                'frame': frame[0] if frame else None,
                'frame_pt': [round(v, 1) for v in frame[1:]] if frame else None,
            })
    return findings


def check(pptx: str, out_dir: str) -> dict:
    import fitz

    from evaluations.slides_bench.render import render
    os.makedirs(out_dir, exist_ok=True)
    forced = os.path.join(out_dir, 'no-autofit.pptx')
    force_no_autofit(pptx, forced)
    render(forced, out_dir)
    document = fitz.open(os.path.join(out_dir, 'deck.pdf'))
    declared = declared_typefaces(forced)
    pages = []
    substituted_fonts: set = set()
    for index, (page, frames) in enumerate(zip(document, text_frames(forced)), 1):
        spans = [span for block in page.get_text('dict')['blocks']
                 for line in block.get('lines', []) for span in line['spans']
                 if span['text'].strip()]
        faithful = [span for span in spans if _face_key(span['font']) in declared]
        substituted = [span for span in spans if span not in faithful]
        substituted_fonts |= {span['font'].split('+', 1)[-1] for span in substituted}
        pages.append({'slide': index, 'spans': len(spans),
                      'outside': outside_spans(faithful, frames),
                      'substituted_outside': outside_spans(substituted, frames)})
    report = {'pptx': pptx, 'autofit': autofit_counts(pptx), 'pages': pages,
              'spans_outside': sum(len(p['outside']) for p in pages),
              'substituted_fonts': sorted(substituted_fonts),
              'substituted_spans_outside': sum(
                  len(p['substituted_outside']) for p in pages)}
    report['ok'] = (report['spans_outside'] == 0
                    and report['autofit']['bare_normAutofit'] == 0)
    with open(os.path.join(out_dir, 'native_fit.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


if __name__ == '__main__':
    result = check(sys.argv[1], sys.argv[2])
    print(json.dumps({k: result[k] for k in (
        'ok', 'autofit', 'spans_outside', 'substituted_fonts',
        'substituted_spans_outside')}))
