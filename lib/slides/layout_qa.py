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

import os
import tempfile

from lib.log import get_logger
from lib.slides.pptd import Deck
from lib.slides.render_html import render_page_html

logger = get_logger(__name__)

__all__ = ['inspect_deck_layout', 'findings_text', 'vertical_growth_repairs']

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


def _vertical_deficit(finding: dict, vertical: str = 'top') -> float:
    """Extra box height that removes a vertical overflow/clearance finding.

    A middle-anchored box grows on both sides, so it needs the PowerPoint
    reserve twice to keep that reserve below its last line.
    """
    reserve_shares = 2 if vertical == 'middle' else 1
    if finding.get('type') == 'pptx_text_overflow_risk':
        return reserve_shares * (float(finding['required'])
                                 - float(finding['clearance']))
    overflow = finding.get('overflow') or {}
    if set(overflow) - {'top', 'bottom'}:
        return 0.0
    return (float(overflow.get('top') or 0) + float(overflow.get('bottom') or 0)
            + reserve_shares * float(finding.get('required') or 0))


def vertical_growth_repairs(page_data: dict, findings: list, *,
                            page_height: int) -> dict:
    """Grow text boxes that only lack height; zero-LLM geometric repair.

    A box whose glyphs fit horizontally but spill a few pixels vertically
    (or lack PowerPoint's CJK metric reserve) needs a taller box, not a
    rewritten page. Each such top-level text element grows by exactly the
    measured deficit around its vertical anchor and stays inside the page.
    Returns ``{elementId: [x, y, w, h]}`` for the caller to apply and
    re-measure; horizontal overflow, collisions and component-expanded
    elements are left to the author repair.
    """
    anchors = {}
    for element in page_data.get('elements') or []:
        if isinstance(element, dict) and isinstance(element.get('content'), dict):
            align = element['content'].get('align')
            anchors[element.get('elementId')] = (
                align[1] if isinstance(align, list) and len(align) > 1 else 'top')
    deficits: dict = {}
    for finding in findings:
        if finding.get('type') not in ('text_overflow',
                                       'pptx_text_overflow_risk'):
            continue
        element_id = finding['elements'][0]
        deficit = _vertical_deficit(finding, anchors.get(element_id, 'top'))
        if deficit > 0:
            deficits[element_id] = max(deficits.get(element_id, 0.0), deficit)
    repairs = {}
    for element in page_data.get('elements') or []:
        if not isinstance(element, dict) or element.get('elementType') != 'text':
            continue
        element_id = element.get('elementId')
        bounds = element.get('bounds')
        if element_id not in deficits or not (
                isinstance(bounds, list) and len(bounds) == 4):
            continue
        x, y, width, height = (float(value) for value in bounds)
        grow = float(int(deficits[element_id]) + 1)
        content = element.get('content') if isinstance(
            element.get('content'), dict) else {}
        align = content.get('align') if isinstance(
            content.get('align'), list) else []
        vertical = align[1] if len(align) > 1 else 'top'
        if vertical == 'middle':
            y -= grow / 2
        elif vertical == 'bottom':
            y -= grow
        height += grow
        y = max(0.0, min(y, page_height - height))
        if height > page_height:
            continue
        repairs[element_id] = [x, round(y, 1), width, round(height, 1)]
    return repairs


def findings_text(findings: list) -> str:
    """Compact layout findings for an author repair prompt."""
    return '\n'.join(str(f.get('message') or f) for f in findings)
