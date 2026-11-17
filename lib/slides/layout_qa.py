"""Deterministic, browser-grounded text layout QA for PPTD decks.

Schema validation can prove that a text box has positive bounds; it cannot
prove that the selected font's glyphs fit those bounds or that two text boxes
do not paint over each other.  This pass asks Chromium for the real line
rectangles after webfonts settle, then reports text overflow and text/text
collisions.  It uses the same HTML renderer as previews, so the check sees the
actual font metrics rather than an approximate character-count heuristic.

Intentional display typography may opt out per element with
``allowOverlap: true``.  The escape hatch is deliberately local: it does not
disable overflow checks and it cannot hide collisions elsewhere on the page.
"""

from __future__ import annotations

import html
import math
import os
import re
import tempfile

from lib.log import get_logger
from lib.slides.pptd import Deck
from lib.slides.render_html import render_page_html

logger = get_logger(__name__)

__all__ = ['inspect_deck_layout', 'findings_text', 'relayout_repairs',
           'apply_relayout']

_MEASURE_JS = r"""
() => {
  const round = value => Math.round(value * 10) / 10;
  const rectJSON = rect => ({
    left: round(rect.left), top: round(rect.top),
    right: round(rect.right), bottom: round(rect.bottom),
    width: round(rect.width), height: round(rect.height)
  });
  const textRects = root => {
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of range.getClientRects()) {
        if (rect.width > 0.1 && rect.height > 0.1) out.push(rectJSON(rect));
      }
      range.detach();
    }
    return out;
  };
  const layer = [...document.querySelectorAll('.page > .el')];
  const texts = [...document.querySelectorAll('.el.text[data-element-id]')].map(el => {
    const style = getComputedStyle(el);
    const innerStyle = getComputedStyle(el.firstElementChild || el);
    return {
      id: el.dataset.elementId,
      z: layer.indexOf(el),
      allowOverlap: el.dataset.allowOverlap === 'true',
      fontSize: Number.parseFloat(innerStyle.fontSize || '0') || 0,
      lineHeight: Number.parseFloat(innerStyle.lineHeight || '') || 0,
      outer: rectJSON(el.getBoundingClientRect()),
      rects: textRects(el),
      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
               Number(style.opacity || 1) > 0.01
    };
  });
  const occluders = [...document.querySelectorAll('.el.image[data-element-id]')].map(el => {
    const style = getComputedStyle(el);
    return {
      id: el.dataset.elementId,
      type: 'image',
      z: layer.indexOf(el),
      outer: rectJSON(el.getBoundingClientRect()),
      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
               Number(style.opacity || 1) > 0.01
    };
  });
  return {texts, occluders};
}
"""


def _intersection(a: dict, b: dict, tolerance: float) -> tuple:
    width = min(a['right'], b['right']) - max(a['left'], b['left'])
    height = min(a['bottom'], b['bottom']) - max(a['top'], b['top'])
    if width <= tolerance or height <= tolerance:
        return 0.0, 0.0
    return width, height


def _union(rects: list) -> dict | None:
    if not rects:
        return None
    return {
        'left': min(r['left'] for r in rects),
        'top': min(r['top'] for r in rects),
        'right': max(r['right'] for r in rects),
        'bottom': max(r['bottom'] for r in rects),
    }


def _line_count(rects: list) -> int:
    tops = []
    for rect in sorted(rects, key=lambda r: r['top']):
        if not tops or abs(rect['top'] - tops[-1]) > 1.0:
            tops.append(rect['top'])
    return len(tops)


def _pptx_safety(record: dict) -> float:
    """Extra vertical room for Chrome→PowerPoint metric differences.

    Office and Chromium can choose different ascent/descent tables for the
    same CJK font. Multi-line boxes are the dangerous case: half an em is a
    deliberately conservative reserve that caught the real ZCOOL overflow
    where Chrome still reported 6–14 px of apparent clearance.
    """
    size = float(record.get('fontSize') or 0)
    if _line_count(record.get('rects') or []) >= 2:
        return max(6.0, min(24.0, size * 0.5))
    return max(2.0, min(6.0, size * 0.15))


def _line_boxes(record: dict) -> list:
    """The record's line boxes.

    Range rects are font content areas, which exceed the line box when the
    authored line height is below the font's ascent+descent. Line boxes are
    what must fit their frame and stay apart (and what PowerPoint lays out),
    so the half-leading deficit is neither overflow nor a collision.
    """
    rects = record.get('rects') or []
    line_height = float(record.get('lineHeight') or 0)
    if not line_height:
        return rects
    out = []
    for rect in rects:
        deficit = max(0.0, (rect['height'] - line_height) / 2)
        out.append({**rect, 'top': rect['top'] + deficit,
                    'bottom': rect['bottom'] - deficit,
                    'height': rect['height'] - 2 * deficit})
    return out


def _measured(record: dict) -> dict:
    """Page-space geometry a repair needs to fit or move this text exactly.

    ``lines`` is the union of line boxes, ``contentBottom`` the lowest font
    content edge and ``reserve`` the PowerPoint metric reserve; a box fits
    when it spans ``lines.top`` .. ``contentBottom + reserve``.
    """
    lines = _union(record.get('rects') or [])
    content = _union(record.get('content_rects') or []) or lines
    return {'outer': {k: record['outer'][k]
                      for k in ('left', 'top', 'right', 'bottom')},
            'lines': lines, 'contentBottom': content['bottom'],
            'reserve': round(_pptx_safety(record), 1)}


def _page_findings(records: list, page_index: int, *, tolerance: float,
                   occluders: list | None = None) -> list:
    findings = []
    active = [{**r, 'content_rects': r.get('rects') or [],
               'rects': _line_boxes(r)}
              for r in records if r.get('visible')]
    for record in active:
        outer = record['outer']
        rects = record.get('rects') or []
        if not rects:
            continue
        actual = _union(rects)
        left = actual['left']
        top = actual['top']
        right = actual['right']
        bottom = actual['bottom']
        overflow = {
            'left': max(0.0, outer['left'] - left),
            'top': max(0.0, outer['top'] - top),
            'right': max(0.0, right - outer['right']),
            'bottom': max(0.0, bottom - outer['bottom']),
        }
        overflow = {k: round(v, 1) for k, v in overflow.items()
                    if v > tolerance}
        if overflow:
            findings.append({
                'type': 'text_overflow',
                'page': page_index + 1,
                'elements': [record['id']],
                'overflow': overflow,
                'required': round(_pptx_safety(record), 1),
                'measured': {record['id']: _measured(record)},
                'message': (f'page {page_index + 1}: text "{record["id"]}" '
                            f'overflows its bounds ({overflow})'),
            })
        elif _line_count(rects) >= 2:
            # The PowerPoint reserve stays measured from the content area.
            clearance = outer['bottom'] - _union(
                record['content_rects'])['bottom']
            needed = _pptx_safety(record)
            if clearance < needed:
                findings.append({
                    'type': 'pptx_text_overflow_risk',
                    'page': page_index + 1,
                    'elements': [record['id']],
                    'clearance': round(clearance, 1),
                    'required': round(needed, 1),
                    'measured': {record['id']: _measured(record)},
                    'message': (
                        f'page {page_index + 1}: multiline text '
                        f'"{record["id"]}" has only {clearance:.1f}px bottom '
                        f'clearance; reserve at least {needed:.1f}px for '
                        'PowerPoint font metrics'),
                })

    for i, first in enumerate(active):
        if first.get('allowOverlap'):
            continue
        for second in active[i + 1:]:
            if second.get('allowOverlap'):
                continue
            best = (0.0, 0.0)
            for a in first.get('rects') or []:
                for b in second.get('rects') or []:
                    hit = _intersection(a, b, tolerance)
                    if hit[0] * hit[1] > best[0] * best[1]:
                        best = hit
            if best[0] <= 0 or best[1] <= 0:
                continue
            # Ignore microscopic antialiasing/italic-overhang contacts while
            # retaining any overlap large enough to obscure a real glyph.
            if best[0] * best[1] < 8.0:
                continue
            overlap = {'width': round(best[0], 1),
                       'height': round(best[1], 1)}
            findings.append({
                'type': 'text_collision',
                'page': page_index + 1,
                'elements': [first['id'], second['id']],
                'overlap': overlap,
                'measured': {first['id']: _measured(first),
                             second['id']: _measured(second)},
                'message': (f'page {page_index + 1}: text "{first["id"]}" '
                            f'collides with "{second["id"]}" ({overlap})'),
            })

    # A later image paints above an earlier text box. Inflate only the text's
    # lower edge by the PowerPoint metric reserve: a Chrome-safe 10px gap can
    # disappear when Office selects a taller CJK ascent/descent. Images below
    # text in DOM order are safe because the text paints on top.
    for record in active:
        actual = _union(record.get('rects') or [])
        if actual is None:
            continue
        safe = dict(actual)
        safe['bottom'] += _pptx_safety(record)
        for occluder in occluders or []:
            if (not occluder.get('visible')
                    or int(occluder.get('z', -1)) <= int(record.get('z', -1))):
                continue
            width, height = _intersection(safe, occluder['outer'], tolerance)
            if width <= 0 or height <= 0 or width * height < 8.0:
                continue
            overlap = {'width': round(width, 1), 'height': round(height, 1)}
            findings.append({
                'type': 'text_image_occlusion_risk',
                'page': page_index + 1,
                'elements': [record['id'], occluder['id']],
                'overlap': overlap,
                'message': (
                    f'page {page_index + 1}: later image '
                    f'"{occluder["id"]}" can cover PowerPoint overflow from '
                    f'text "{record["id"]}" ({overlap})'),
            })
    return findings


def inspect_deck_layout(deck: Deck, *, timeout_ms: int = 20000,
                        tolerance: float = 1.5, page_indexes=None,
                        abort_check=None) -> dict:
    """Inspect every page with one Chromium session.

    Returns ``{'ran', 'ok', 'pages', 'findings'}``. Browser unavailability is
    represented as ``ran: False`` rather than raising, preserving the slide
    pipeline's degradable behaviour while making the skipped gate explicit.
    """
    from lib.slides.render_png import _render_contract, _settle_render_page
    _, timeout_ms = _render_contract(deck, 1, timeout_ms)
    indexes = tuple(range(len(deck.pages)) if page_indexes is None else page_indexes)
    if len(indexes) > 64 or any(not isinstance(i, int) or not 0 <= i < len(deck.pages)
                               for i in indexes):
        raise ValueError('layout QA page selection exceeds the finite limit')

    def check_abort():
        if abort_check is not None and abort_check():
            raise InterruptedError('slide layout QA aborted')

    check_abort()
    try:
        from playwright.sync_api import sync_playwright
        try:
            import chromium_env
            chromium_env.ensure_chromium_env(os.environ)
        except Exception as exc:
            logger.debug('[Slides:layout-qa] chromium shim unavailable: %s',
                         exc)
    except Exception as exc:
        logger.debug('[Slides:layout-qa] Playwright unavailable: %s', exc)
        return {'ran': False, 'ok': False, 'reason': str(exc), 'pages': [],
                'findings': []}

    page_results = []
    all_findings = []
    try:
        with tempfile.TemporaryDirectory(prefix='tofu-slide-layout-') as tmpdir:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    browser_page = browser.new_page(
                        viewport={'width': deck.width, 'height': deck.height})
                    for index in indexes:
                        check_abort()
                        slide_page = deck.pages[index]
                        html_path = os.path.join(tmpdir, f'{index + 1:02d}.html')
                        html = render_page_html(deck, slide_page,
                                                page_index=index)
                        with open(html_path, 'w', encoding='utf-8') as fh:
                            fh.write(html)
                        browser_page.goto('file://' + html_path,
                                          wait_until='load',
                                          timeout=timeout_ms)
                        browser_page.evaluate(
                            "limit => Promise.race([document.fonts.ready.then(() => true), "
                            "new Promise((_, reject) => setTimeout(() => "
                            "reject(new Error('slide fonts readiness timed out')), limit))])",
                            timeout_ms)
                        _settle_render_page(browser_page, timeout_ms)
                        measurement = browser_page.evaluate(_MEASURE_JS)
                        records = measurement.get('texts') or []
                        findings = _page_findings(
                            records, index, tolerance=tolerance,
                            occluders=measurement.get('occluders') or [])
                        page_results.append({
                            'index': index,
                            'ok': not findings,
                            'findings': findings,
                            'elementsMeasured': len(records),
                        })
                        all_findings.extend(findings)
                finally:
                    browser.close()
    except InterruptedError:
        raise
    except Exception as exc:
        logger.warning('[Slides:layout-qa] skipped after browser failure: %s',
                       exc)
        return {'ran': False, 'ok': False, 'reason': str(exc), 'pages': [],
                'findings': []}
    logger.info('[Slides:layout-qa] %d page(s), %d finding(s)',
                len(page_results), len(all_findings))
    return {'ran': True, 'ok': not all_findings, 'pages': page_results,
            'findings': all_findings}


def _text_elements(page_data: dict) -> dict:
    """Top-level text elements by id (component-expanded ones are absent)."""
    return {element.get('elementId'): element
            for element in page_data.get('elements') or []
            if isinstance(element, dict)
            and element.get('elementType') == 'text'
            and isinstance(element.get('content'), dict)
            and isinstance(element.get('bounds'), list)
            and len(element['bounds']) == 4}


def _valign(element: dict) -> str:
    align = element['content'].get('align')
    return align[1] if isinstance(align, list) and len(align) > 1 else 'top'


def _is_vertical(finding: dict) -> bool:
    if finding.get('type') == 'pptx_text_overflow_risk':
        return True
    return (finding.get('type') == 'text_overflow'
            and not set(finding.get('overflow') or {}) - {'top', 'bottom'})


def _fitted_box(element: dict, measured: dict) -> tuple:
    """Smallest change to ``element``'s box that holds its text in place.

    The text needs ``lines.top`` .. ``contentBottom + reserve``. A top
    anchor keeps its top and extends down; a middle anchor extends both
    sides around its centre; a bottom anchor glues the text to the box's
    lower edge, so no height adds clearance below it: the box re-anchors at
    the top of the text's current first line, leaving the text where it is.
    Returns ``((y, height), valign)``.
    """
    _x, y, _w, height = (float(v) for v in element['bounds'])
    need_top = float(measured['lines']['top'])
    need_bottom = float(measured['contentBottom']) + float(measured['reserve'])
    valign = _valign(element)
    if valign == 'middle':
        centre = y + height / 2
        half = max(height / 2, centre - need_top, need_bottom - centre)
        top, bottom = centre - half, centre + half
    elif valign == 'bottom':
        top, bottom, valign = need_top, need_bottom, 'top'
    else:
        top, bottom = min(y, need_top), max(y + height, need_bottom)
    top, bottom = math.floor(top), math.ceil(bottom)
    return (top, bottom - top), valign


def relayout_repairs(page_data: dict, findings: list, *, page_height: int,
                     move: str = 'lower') -> dict:
    """Zero-LLM re-layout of one page's measured text findings.

    - A vertical overflow or missing PowerPoint reserve gets the exact box
      its measured text needs (``_fitted_box``).
    - Two colliding stacked texts are separated by moving one box clear of
      the other's last line plus its reserve: the ``lower`` one down or the
      ``upper`` one up, per ``move``; the caller measures both variants.

    Horizontal overflow (an unbreakable run wider than its box) has no
    geometric answer that keeps the composition, so it goes to the author.
    Returns ``{elementId: {'bounds': [x, y, w, h], 'valign': str}}``; a
    change that would leave the page is not proposed.
    """
    texts = _text_elements(page_data)
    boxes = {eid: [float(v) for v in element['bounds']]
             for eid, element in texts.items()}
    valigns = {eid: _valign(element) for eid, element in texts.items()}
    changed: set = set()
    for finding in findings:
        if not _is_vertical(finding):
            continue
        element_id = finding['elements'][0]
        measured = (finding.get('measured') or {}).get(element_id)
        if element_id not in texts or not measured:
            continue
        (y, height), valign = _fitted_box(texts[element_id], measured)
        if y < 0 or y + height > page_height:
            continue
        boxes[element_id][1], boxes[element_id][3] = y, height
        valigns[element_id] = valign
        changed.add(element_id)
    for finding in findings:
        if finding.get('type') != 'text_collision':
            continue
        measured = finding.get('measured') or {}
        first, second = finding['elements']
        if first not in measured or second not in measured:
            continue
        upper, lower = sorted(
            (first, second), key=lambda eid: measured[eid]['lines']['top'])
        shift = math.ceil(measured[upper]['lines']['bottom']
                          + measured[upper]['reserve']
                          - measured[lower]['lines']['top'])
        target = lower if move == 'lower' else upper
        if target not in texts or shift <= 0:
            continue
        y = boxes[target][1] + (shift if target == lower else -shift)
        if y < 0 or y + boxes[target][3] > page_height:
            continue
        boxes[target][1] = y
        changed.add(target)
    return {eid: {'bounds': [round(v, 1) for v in boxes[eid]],
                  'valign': valigns[eid]} for eid in sorted(changed)}


def apply_relayout(page_data: dict, repairs: dict) -> None:
    """Write ``relayout_repairs`` output into the page's elements."""
    for element_id, element in _text_elements(page_data).items():
        repair = repairs.get(element_id)
        if not repair:
            continue
        element['bounds'] = repair['bounds']
        if _valign(element) != repair['valign']:
            align = element['content'].get('align')
            horizontal = (align[0] if isinstance(align, list) and align
                          else 'left')
            element['content']['align'] = [horizontal, repair['valign']]


_TAG_RE = re.compile(r'<[^>]+>')


def _visible_chars(element: dict | None) -> int:
    if not element:
        return 0
    text = html.unescape(_TAG_RE.sub('', str(element['content'].get('text') or '')))
    return len(''.join(text.split()))


def findings_text(findings: list, page_data: dict | None = None) -> str:
    """Layout findings as measured, actionable instructions for the author.

    Each line states what is wrong in pixels and the two legal fixes: move
    or resize into empty space, or cut that element's copy to the length
    the measurement says fits (shrinking type below the legibility floor is
    rejected elsewhere, so it is never offered).
    """
    texts = _text_elements(page_data or {})
    lines = []
    for finding in findings:
        message = str(finding.get('message') or finding)
        measured = finding.get('measured') or {}
        kind = finding.get('type')
        if _is_vertical(finding) and measured:
            element_id = finding['elements'][0]
            m = measured[element_id]
            available = m['outer']['bottom'] - m['outer']['top']
            needed = (m['contentBottom'] + m['reserve']) - m['lines']['top']
            chars = _visible_chars(texts.get(element_id))
            cut = ''
            if chars and needed > available:
                cut = (f', or cut this text to about '
                       f'{int(chars * available / needed)} characters '
                       f'(now {chars})')
            lines.append(
                f'{message}. The box is {available:.0f}px tall and the text '
                f'needs {needed:.0f}px including the PowerPoint reserve: make '
                f'the box {needed:.0f}px tall only if that space is empty'
                f'{cut}; keep its claim and numbers.')
        elif kind == 'text_overflow' and measured:
            element_id = finding['elements'][0]
            m = measured[element_id]
            box = m['outer']['right'] - m['outer']['left']
            run = m['lines']['right'] - m['lines']['left']
            lines.append(
                f'{message}. Its widest line, a run that cannot wrap (a path, '
                f'identifier, long word or no-wrap label), is {run:.0f}px; '
                f'the box is {box:.0f}px wide: widen the box to at least '
                f'{run:.0f}px only into empty space, otherwise replace that '
                'run with a shorter wording; keep its claim and numbers.')
        elif kind == 'text_collision' and measured:
            upper, lower = sorted(
                finding['elements'], key=lambda e: measured[e]['lines']['top'])
            above, below = measured[upper], measured[lower]
            available = below['lines']['top'] - above['reserve'] \
                - above['lines']['top']
            needed = above['contentBottom'] - above['lines']['top']
            chars = _visible_chars(texts.get(upper))
            cut = ''
            if chars and 0 < available < needed:
                cut = (f', or cut "{upper}" to about '
                       f'{int(chars * available / needed)} characters '
                       f'(now {chars}) so it ends above "{lower}"')
            lines.append(
                f'{message}. "{upper}" text ends at y='
                f'{above["lines"]["bottom"]:.0f} but "{lower}" text '
                f'starts at y={below["lines"]["top"]:.0f}: move one box '
                f'clear into empty space{cut}; keep their claims and '
                'numbers.')
        else:
            lines.append(message)
    return '\n'.join(lines)
