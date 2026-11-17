"""Deterministic, browser-grounded text layout QA for PPTD decks.

Schema validation can prove that a text box has positive bounds; it cannot
prove that the selected font's glyphs fit those bounds or that two text boxes
do not paint over each other.  This pass asks Chromium for the real line
rectangles after webfonts settle, then reports text overflow, text/text
collisions, text running out of the card it sits in, and (advisory, never
gating) bad line breaks: widows and words split across lines.  It uses the
same HTML renderer as previews, so the check sees the
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
           'line_break_repairs', 'apply_relayout']

_MEASURE_JS = r"""
(headings = []) => {
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
  // Glyph ink of each text line: a range rect is the font's content area
  // (ascent..descent), which holds empty space above and below the glyphs;
  // a rule drawn there touches no glyph. Canvas measures the run's real
  // ink extent about the baseline.
  const canvas = document.createElement('canvas').getContext('2d');
  const inkRects = root => {
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const s = getComputedStyle(node.parentElement);
      canvas.font = `${s.fontStyle} ${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
      const m = canvas.measureText(node.nodeValue.trim());
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of range.getClientRects()) {
        if (rect.width <= 0.1 || rect.height <= 0.1) continue;
        const baseline = rect.top + m.fontBoundingBoxAscent;
        out.push(rectJSON({left: rect.left, right: rect.right,
          top: baseline - m.actualBoundingBoxAscent,
          bottom: baseline + m.actualBoundingBoxDescent,
          width: rect.width,
          height: m.actualBoundingBoxAscent + m.actualBoundingBoxDescent}));
      }
      range.detach();
    }
    return out;
  };
  const words = new Intl.Segmenter(undefined, {granularity: 'word'});
  // Line-break defects per paragraph: a widow (the last line holds a single
  // word) or a word split across two lines. Words come from the browser's
  // own segmenter (dictionary-based for CJK), lines from glyph positions: a
  // glyph starts a new line below the current line's middle.
  const blockLines = block => {
    const lines = [];
    const lineOf = [];
    let text = '';
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      for (let i = 0; i < node.data.length; i++) {
        const range = document.createRange();
        range.setStart(node, i); range.setEnd(node, i + 1);
        const rect = range.getClientRects()[0];
        range.detach();
        text += node.data[i];
        if (!rect) { lineOf.push(lines.length - 1); continue; }
        const line = lines[lines.length - 1];
        if (!line || rect.top > (line.top + line.bottom) / 2) {
          lines.push({top: rect.top, bottom: rect.bottom, text: node.data[i]});
        } else {
          line.text += node.data[i];
          line.bottom = Math.max(line.bottom, rect.bottom);
        }
        lineOf.push(lines.length - 1);
      }
    }
    return {lines, lineOf, text};
  };
  const lineBreaks = root => {
    const out = {widow: false, split: false, lines: 0};
    const blocks = [...root.querySelectorAll('p, li')];
    for (const block of (blocks.length ? blocks : [root])) {
      const {lines, lineOf, text} = blockLines(block);
      out.lines += lines.length;
      if (lines.length < 2) continue;
      const last = [...words.segment(lines[lines.length - 1].text)]
        .filter(s => s.isWordLike);
      if (last.length === 1) out.widow = true;
      for (const seg of words.segment(text)) {
        if (seg.isWordLike &&
            lineOf[seg.index] !== lineOf[seg.index + seg.segment.length - 1]) {
          out.split = true;
        }
      }
    }
    return out;
  };
  // A wrapped CJK heading breaks where a reader pauses: after clause
  // punctuation (not the enumeration comma 、 inside a list) or before an opening bracket, never inside a word the
  // browser's dictionary does not know (外|采) nor between a number and its
  // measure word (5 | 处). Only if no such boundary can hold the line count
  // does it fall back to boundaries between two dictionary words of two or
  // more characters. Among the feasible breaks it takes the most balanced
  // set (smallest widest line) and returns it as the count of non-space
  // characters before each break; null when the browser's own wrapping is
  // already that plan or no plan keeps the line count.
  const CJK = /[\u2E80-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]/;
  const AFTER = new Set([...'，。；：！？,;:!?）)」』”’】》·｜|—…']);
  const BEFORE = new Set([...'（(「『“‘【《']);
  const HAN = /\p{Script=Han}/u;
  const LATIN = /[A-Za-z]/;
  const phrasePlan = inner => {
    const blocks = [...inner.querySelectorAll('p, li')];
    if (blocks.length > 1 || inner.querySelector('br')) return null;
    const block = blocks[0] || inner;
    const {lines, lineOf, text} = blockLines(block);
    if (lines.length < 2 || !CJK.test(text)) return null;
    const isSpace = c => /\s/.test(c);
    const prevSolid = i => { let j = i - 1; while (j >= 0 && isSpace(text[j])) j--; return j; };
    const phrase = [];
    for (let i = 1; i < text.length; i++) {
      const c = text[i];
      const j = prevSolid(i);
      if (isSpace(c) || j < 0) continue;
      const p = text[j];
      if ((AFTER.has(p) && !AFTER.has(c)) || (BEFORE.has(c) && !BEFORE.has(p)) ||
          (j < i - 1 && LATIN.test(p) && LATIN.test(c))) phrase.push(i);
    }
    const known = [];
    const segs = [...words.segment(text)].filter(s => s.isWordLike);
    const single = s => s.segment.length === 1 && HAN.test(s.segment);
    for (let k = 1; k < segs.length; k++) {
      const a = segs[k - 1], b = segs[k];
      if (text.slice(a.index + a.segment.length, b.index).trim()) continue;
      if (single(a) || single(b)) continue;
      if (/\d$/.test(a.segment) && /^\d/.test(b.segment)) continue;
      known.push(b.index);
    }
    const clone = block.cloneNode(true);
    clone.style.cssText += ';position:absolute;visibility:hidden;' +
      'white-space:nowrap;width:auto;max-width:none;left:0;top:0';
    block.parentElement.appendChild(clone);
    const nodes = [];
    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
    let node, offset = 0;
    while ((node = walker.nextNode())) {
      nodes.push({node, start: offset}); offset += node.data.length;
    }
    const at = i => {
      let k = nodes.length - 1;
      while (k > 0 && nodes[k].start > i) k--;
      return [nodes[k].node, Math.min(i - nodes[k].start, nodes[k].node.data.length)];
    };
    const widths = new Map();
    const width = (a, b) => {
      const key = a + ':' + b;
      if (!widths.has(key)) {
        let s = a, e = b;
        while (s < e && isSpace(text[s])) s++;
        while (e > s && isSpace(text[e - 1])) e--;
        const range = document.createRange();
        range.setStart(...at(s)); range.setEnd(...at(e));
        widths.set(key, range.getBoundingClientRect().width);
        range.detach();
      }
      return widths.get(key);
    };
    const avail = block.getBoundingClientRect().width + 0.5;
    const solve = cands => {
      const points = [0, ...cands, text.length];
      // best[n][k]: smallest widest line of n lines ending at points[k].
      let best = points.map((_, k) => {
        const w = k ? width(0, points[k]) : Infinity;
        return w <= avail ? {cost: w, breaks: []} : null;
      });
      for (let n = 2; n <= lines.length; n++) {
        best = points.map((end, k) => {
          let pick = null;
          for (let j = 1; j < k; j++) {
            if (!best[j]) continue;
            const w = width(points[j], end);
            if (w > avail) continue;
            const cost = Math.max(best[j].cost, w);
            if (!pick || cost < pick.cost) {
              pick = {cost, breaks: [...best[j].breaks, points[j]]};
            }
          }
          return pick;
        });
      }
      return best[points.length - 1];
    };
    let plan = solve(phrase);
    if (!plan) plan = solve([...new Set([...phrase, ...known])].sort((a, b) => a - b));
    clone.remove();
    if (!plan) return null;
    const current = [];
    for (let i = 1; i < text.length; i++) {
      if (!isSpace(text[i]) && lineOf[i] !== lineOf[prevSolid(i)]) current.push(i);
    }
    if (current.join() === plan.breaks.join()) return null;
    return plan.breaks.map(i => [...text.slice(0, i)].filter(c => !isSpace(c)).length);
  };
  // The narrowest text width keeping the line count: the browser's own
  // text-wrap: balance, measured, then restored.
  const balancedWidth = inner => {
    const before = inner.style.textWrap;
    inner.style.textWrap = 'balance';
    const rects = textRects(inner);
    inner.style.textWrap = before;
    if (!rects.length) return 0;
    return Math.max(...rects.map(r => r.right)) - Math.min(...rects.map(r => r.left));
  };
  // The widest text width, pixel by pixel down to the balanced width, at
  // which the line count holds and no word is split and no line widowed;
  // 0 when none in that range does.
  const cleanWidth = (inner, base, floor) => {
    const before = inner.style.width;
    let found = 0;
    for (let w = Math.floor(inner.getBoundingClientRect().width) - 1;
         w >= Math.ceil(floor); w--) {
      inner.style.width = w + 'px';
      const breaks = lineBreaks(inner);
      if (breaks.lines === base.lines && !breaks.widow && !breaks.split) {
        found = w;
        break;
      }
    }
    inner.style.width = before;
    return found;
  };
  const layer = [...document.querySelectorAll('.page > .el')];
  const texts = [...document.querySelectorAll('.el.text[data-element-id]')].map(el => {
    const style = getComputedStyle(el);
    const inner = el.firstElementChild || el;
    const innerStyle = getComputedStyle(inner);
    const breaks = lineBreaks(inner);
    const phrase = headings.includes(el.dataset.elementId) ? phrasePlan(inner) : null;
    const flawed = breaks.widow || breaks.split;
    const balanced = flawed ? balancedWidth(inner) : 0;
    return {
      phraseBreaks: phrase,
      widow: breaks.widow,
      split: breaks.split,
      painted: innerStyle.backgroundColor !== 'rgba(0, 0, 0, 0)' ||
               innerStyle.backgroundImage !== 'none',
      innerWidth: round(inner.getBoundingClientRect().width),
      balancedWidth: round(balanced),
      cleanWidth: flawed ? cleanWidth(inner, breaks, balanced) : 0,
      id: el.dataset.elementId,
      z: layer.indexOf(el),
      allowOverlap: el.dataset.allowOverlap === 'true',
      fontSize: Number.parseFloat(innerStyle.fontSize || '0') || 0,
      lineHeight: Number.parseFloat(innerStyle.lineHeight || '') || 0,
      outer: rectJSON(el.getBoundingClientRect()),
      rects: textRects(el),
      ink: inkRects(el),
      opacity: Number(style.opacity || 1),
      color: innerStyle.color,
      colors: (() => {
        const seen = new Map();
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
          if (!node.nodeValue || !node.nodeValue.trim()) continue;
          const s = getComputedStyle(node.parentElement);
          const size = Number.parseFloat(s.fontSize || '0') || 0;
          const bold = (Number.parseInt(s.fontWeight, 10) || 400) >= 700;
          const key = s.color + '|' + size + '|' + bold;
          if (!seen.has(key)) seen.set(key, {color: s.color, size, bold});
        }
        return [...seen.values()];
      })(),
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
  const boxes = selector => [...document.querySelectorAll(selector)].map(el => {
    const style = getComputedStyle(el);
    return {
      id: el.dataset.elementId,
      z: layer.indexOf(el),
      outer: rectJSON(el.getBoundingClientRect()),
      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
               Number(style.opacity || 1) > 0.01
    };
  });
  // Painted outlines of drawn shapes and lines, sampled in page pixels
  // along the SVG path itself (one sample per rendered pixel of length):
  // strokes, and the edges of solid fills, are what can cut through glyphs.
  const graphics = [...document.querySelectorAll(
      '.el.shape[data-element-id], .el.line[data-element-id]')].map(el => {
    const style = getComputedStyle(el);
    const path = el.querySelector('svg > path');
    const stroke = path ? getComputedStyle(path) : null;
    const stroked = !!stroke && stroke.stroke !== 'none' &&
      Number.parseFloat(stroke.strokeWidth || '0') > 0 &&
      Number(stroke.strokeOpacity || 1) > 0.01;
    const filled = !!stroke && stroke.fill !== 'none' &&
      Number(stroke.fillOpacity || 1) > 0.01;
    const points = [];
    if (path && (stroked || filled) && path.getTotalLength) {
      const matrix = path.getScreenCTM();
      const total = path.getTotalLength();
      const scale = matrix ? Math.max(Math.hypot(matrix.a, matrix.b),
                                      Math.hypot(matrix.c, matrix.d)) : 1;
      const steps = Math.max(1, Math.ceil(total * scale));
      for (let i = 0; i <= steps && matrix; i++) {
        const p = path.getPointAtLength(total * i / steps);
        points.push([round(matrix.a * p.x + matrix.c * p.y + matrix.e),
                     round(matrix.b * p.x + matrix.d * p.y + matrix.f)]);
      }
    }
    return {
      id: el.dataset.elementId,
      kind: el.classList.contains('line') ? 'line' : 'shape',
      z: layer.indexOf(el),
      stroked, filled,
      strokeWidth: stroked ? Number.parseFloat(stroke.strokeWidth) : 0,
      fill: filled ? stroke.fill : '',
      arrows: path ? [path.hasAttribute('marker-start'),
                      path.hasAttribute('marker-end')] : [false, false],
      opaque: Number(style.opacity || 1) >= 1 && filled &&
              Number(stroke.fillOpacity || 1) >= 1,
      outer: rectJSON(el.getBoundingClientRect()),
      points,
      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
               Number(style.opacity || 1) > 0.01
    };
  });
  // Table cells paint their own fill: text colour against it, per cell.
  const cells = [...document.querySelectorAll('.el.table[data-element-id]')]
    .flatMap(table => [...table.querySelectorAll('td, th')]
      .filter(cell => cell.textContent.trim())
      .map(cell => {
        const s = getComputedStyle(cell);
        return {table: table.dataset.elementId, color: s.color,
                background: s.backgroundColor,
                size: Number.parseFloat(s.fontSize || '0') || 0,
                bold: (Number.parseInt(s.fontWeight, 10) || 400) >= 700,
                text: cell.textContent.trim().slice(0, 40)};
      }));
  // A table's rows grow with their text; the grid can run past its box.
  const tables = [...document.querySelectorAll('.el.table[data-element-id]')]
    .map(el => {
      const grid = el.querySelector('table');
      return {id: el.dataset.elementId,
              outer: rectJSON(el.getBoundingClientRect()),
              grid: grid ? rectJSON(grid.getBoundingClientRect()) : null};
    });
  const pageStyle = getComputedStyle(document.querySelector('.page'));
  return {texts, occluders, graphics, cells, tables,
          pageBackground: pageStyle.backgroundImage === 'none'
            ? pageStyle.backgroundColor : '',
          shapes: boxes('.el.shape[data-element-id]'),
          charts: boxes('.el.chart[data-element-id]'),
          targets: boxes('.el.table[data-element-id], .el.icon[data-element-id]')};
}
"""


#: WCAG 2.x success criterion 1.4.3 (contrast, minimum): 4.5:1 for text,
#: 3:1 for large text, which is at least 18pt (24px) or 14pt (18.67px) bold.
_WCAG_TEXT_CONTRAST = 4.5
_WCAG_LARGE_TEXT_CONTRAST = 3.0
_WCAG_LARGE_TEXT_PX = 24.0
_WCAG_LARGE_BOLD_TEXT_PX = 18.67

_RGB_RE = re.compile(r'rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)'
                     r'(?:[,\s/]+([\d.]+%?))?\s*\)')


def _rgba(value: str) -> tuple | None:
    match = _RGB_RE.fullmatch(str(value or '').strip())
    if not match:
        return None
    alpha = match.group(4)
    if alpha is None:
        a = 1.0
    elif alpha.endswith('%'):
        a = float(alpha[:-1]) / 100
    else:
        a = float(alpha)
    return (float(match.group(1)), float(match.group(2)),
            float(match.group(3)), a)


def _luminance(rgb) -> float:
    def channel(value):
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(foreground: tuple, background: tuple, alpha: float) -> float:
    blended = tuple(alpha * f + (1 - alpha) * b
                    for f, b in zip(foreground[:3], background[:3]))
    high, low = sorted((_luminance(blended), _luminance(background)),
                       reverse=True)
    return (high + 0.05) / (low + 0.05)


def _text_background(record: dict, graphics: list, occluders: list,
                     page_background: str) -> tuple | None:
    """The solid colour behind ``record``'s lines, or ``None`` when an
    image, gradient or translucent layer makes it unknown."""
    lines = _union(record.get('rects') or [])
    z = int(record.get('z', -1))
    for image in occluders or []:
        if (image.get('visible') and int(image.get('z', -1)) < z
                and _intersection(lines, image['outer'], 0)[0] > 0):
            return None
    below = [g for g in graphics or []
             if g.get('visible') and g.get('filled')
             and int(g.get('z', -1)) < z
             and g['outer']['left'] <= lines['left']
             and g['outer']['right'] >= lines['right']
             and g['outer']['top'] <= lines['top']
             and g['outer']['bottom'] >= lines['bottom']]
    if below:
        top = max(below, key=lambda g: int(g.get('z', -1)))
        fill = _rgba(top.get('fill'))
        return fill if top.get('opaque') and fill and fill[3] >= 1 else None
    page = _rgba(page_background)
    return page if page and page[3] >= 1 else None


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


def _container(record: dict, shapes: list) -> dict | None:
    """The innermost shape painted below ``record`` where its first line starts.

    A card or panel the text was placed in: it spans the first line's width
    and holds its top. An accent bar beside a heading does not span the
    heading's first line, so it is never a container.
    """
    rects = sorted(record.get('rects') or [], key=lambda r: (r['top'], r['left']))
    if not rects:
        return None
    first = [r for r in rects if r['top'] < rects[0]['bottom']]
    line = _union(first)
    best = None
    for shape in shapes:
        box = shape['outer']
        if (not shape.get('visible')
                or int(shape.get('z', -1)) >= int(record.get('z', -1))
                or line['left'] < box['left'] or line['right'] > box['right']
                or not box['top'] <= line['top'] < box['bottom']):
            continue
        area = (box['right'] - box['left']) * (box['bottom'] - box['top'])
        if best is None or area < best[0]:
            best = (area, shape)
    return best[1] if best else None


def _rect_distance(point, box: dict) -> float:
    dx = max(box['left'] - point[0], 0.0, point[0] - box['right'])
    dy = max(box['top'] - point[1], 0.0, point[1] - box['bottom'])
    return math.hypot(dx, dy)


def _ray_entry(tip, source, line_id, boxes) -> tuple[float, str] | None:
    """How far beyond ``tip`` the ray from ``source`` through it first
    enters another element's box (0 when a box holds the tip), and that
    element's id; ``None`` when it reaches none."""
    length = math.hypot(tip[0] - source[0], tip[1] - source[1])
    if not length:
        return None
    dx, dy = (tip[0] - source[0]) / length, (tip[1] - source[1]) / length
    best = None
    for box_id, box in boxes:
        if box_id == line_id or _rect_distance(source, box) == 0:
            continue
        # Slab entry of the ray tip + t * (dx, dy), t > 0.
        low, high = 0.0, math.inf
        for origin, step, lo, hi in ((tip[0], dx, box['left'], box['right']),
                                     (tip[1], dy, box['top'], box['bottom'])):
            if step == 0:
                if not lo <= origin <= hi:
                    low, high = 1.0, 0.0
                continue
            a, b = (lo - origin) / step, (hi - origin) / step
            low, high = max(low, min(a, b)), min(high, max(a, b))
        if low <= high and high > 0 and (best is None or low < best[0]):
            best = (low, box_id)
    if best is None:
        return None
    return max(best[0], 0.0), best[1]


def _arrow_findings(page_index: int, graphics: list, boxes: list) -> list:
    """An arrowhead must point at something: along its own direction it
    reaches another element (text lines, shape, image, chart, table) within
    one arrowhead length of its tip."""
    from lib.slides.render_html import ARROWHEAD_LENGTH
    findings = []
    for graphic in graphics:
        points = graphic.get('points') or []
        if (graphic.get('kind') != 'line' or not graphic.get('visible')
                or len(points) < 2):
            continue
        reach = ARROWHEAD_LENGTH * float(graphic.get('strokeWidth') or 1)
        for end, has_arrow in enumerate(graphic.get('arrows') or []):
            if not has_arrow:
                continue
            tip = points[-1] if end else points[0]
            source = points[0] if end else points[-1]
            # Pointing at something: the tip lies in another element's box,
            # or the line's own direction reaches one within an arrowhead.
            # A panel holding the whole arrow is its background, not a target.
            entry = _ray_entry(tip, source, graphic['id'], boxes)
            if entry is not None and entry[0] <= reach:
                continue
            finding = {
                'type': 'arrow_to_nothing', 'page': page_index + 1,
                'elements': [graphic['id']], 'end': end,
                'tip': list(tip),
                'message': (f'page {page_index + 1}: the arrow '
                            f'"{graphic["id"]}" ends at x={tip[0]:.0f}, '
                            f'y={tip[1]:.0f}, pointing at no element'),
            }
            if entry is not None:
                # It points at an element but stops short of what is drawn
                # there (a text's measured lines, not its box bounds).
                gap, target = entry
                length = math.hypot(tip[0] - source[0], tip[1] - source[1])
                finding.update({
                    'target': target, 'gap': round(gap),
                    'reach_point': [tip[0] + (tip[0] - source[0]) / length * gap,
                                    tip[1] + (tip[1] - source[1]) / length * gap],
                    'message': (f'page {page_index + 1}: the arrow '
                                f'"{graphic["id"]}" ends at x={tip[0]:.0f}, '
                                f'y={tip[1]:.0f} and stops {gap:.0f}px short '
                                f'of "{target}"'),
                })
            findings.append(finding)
    return findings


def _page_findings(records: list, page_index: int, *, tolerance: float,
                   occluders: list | None = None,
                   shapes: list | None = None,
                   graphics: list | None = None,
                   charts: list | None = None,
                   page_background: str = '',
                   targets: list | None = None) -> list:
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

    # Text placed in a card must stay in it: a box that fits its own text can
    # still run past the card it sits in (or be moved out of it by a repair).
    for record in active:
        container = _container(record, shapes or [])
        if container is None:
            continue
        actual = _union(record['rects'])
        box = container['outer']
        outside = {
            'left': max(0.0, box['left'] - actual['left']),
            'top': max(0.0, box['top'] - actual['top']),
            'right': max(0.0, actual['right'] - box['right']),
            'bottom': max(0.0, actual['bottom'] - box['bottom']),
        }
        outside = {k: round(v, 1) for k, v in outside.items() if v > tolerance}
        if outside:
            findings.append({
                'type': 'text_outside_container',
                'page': page_index + 1,
                'elements': [record['id'], container['id']],
                'outside': outside,
                'container': dict(box),
                'measured': {record['id']: _measured(record)},
                'message': (f'page {page_index + 1}: text "{record["id"]}" '
                            f'runs outside its container "{container["id"]}" '
                            f'({outside})'),
            })

    # A drawn outline running through a glyph line: a stroke (rule, arrow,
    # ring, card border), or the edge of a fill painted over the text or
    # opaque beneath it, which flips the text's background mid-line. The
    # card a text sits in is judged by the containment check above.
    for record in active:
        if record.get('allowOverlap'):
            continue
        container = _container(record, shapes or [])
        boxes = [{'left': r['left'] + tolerance, 'top': r['top'] + tolerance,
                  'right': r['right'] - tolerance,
                  'bottom': r['bottom'] - tolerance}
                 for r in record.get('ink') or []]
        for graphic in graphics or []:
            if (not graphic.get('visible')
                    or (container is not None
                        and graphic['id'] == container['id'])):
                continue
            above = int(graphic.get('z', -1)) > int(record.get('z', -1))
            if not (graphic.get('stroked') or (graphic.get('filled') and (
                    above or graphic.get('opaque')))):
                continue
            hits = [(x, y) for x, y in graphic.get('points') or []
                    if any(b['left'] < x < b['right']
                           and b['top'] < y < b['bottom'] for b in boxes)]
            if not hits:
                continue
            span = {'left': min(x for x, _ in hits),
                    'top': min(y for _, y in hits),
                    'right': max(x for x, _ in hits),
                    'bottom': max(y for _, y in hits)}
            findings.append({
                'type': 'stroke_through_text',
                'page': page_index + 1,
                'elements': [record['id'], graphic['id']],
                'crossing': {k: round(v, 1) for k, v in span.items()},
                'strokeWidth': graphic.get('strokeWidth') or 0,
                'measured': {record['id']: _measured(record)},
                'message': (
                    f'page {page_index + 1}: the {graphic.get("kind")} '
                    f'"{graphic["id"]}" draws through text "{record["id"]}"'
                    f' at x={span["left"]:.0f}..{span["right"]:.0f}, '
                    f'y={span["top"]:.0f}..{span["bottom"]:.0f}'),
            })

    # Text below the WCAG minimum contrast against the solid colour it sits
    # on, judged for the box's own colour at each size it is set in. Inline
    # emphasis colours are theme accents, owned by the theme's palette.
    for record in active:
        background = _text_background(record, graphics or [],
                                      occluders or [], page_background)
        if background is None:
            continue
        worst = None
        for run in record.get('colors') or []:
            colour = _rgba(run.get('color'))
            if colour is None or run.get('color') != record.get('color'):
                continue
            size = float(run.get('size') or 0)
            large = size >= _WCAG_LARGE_TEXT_PX or (
                run.get('bold') and size >= _WCAG_LARGE_BOLD_TEXT_PX)
            needed = _WCAG_LARGE_TEXT_CONTRAST if large \
                else _WCAG_TEXT_CONTRAST
            ratio = _contrast(colour, background,
                              colour[3] * float(record.get('opacity', 1)))
            if ratio < needed and (worst is None or ratio < worst[0]):
                worst = (ratio, needed, run.get('color'))
        if worst is None:
            continue
        findings.append({
            'type': 'text_contrast',
            'page': page_index + 1,
            'elements': [record['id']],
            'contrast': round(worst[0], 2), 'required': worst[1],
            'message': (f'page {page_index + 1}: text "{record["id"]}" '
                        f'colour {worst[2]} has contrast {worst[0]:.2f}:1 '
                        f'against its background (WCAG minimum '
                        f'{worst[1]}:1)'),
        })

    boxes = ([(r['id'], _union(r['rects'])) for r in active if r['rects']]
             + [(g['id'], g['outer']) for g in graphics or []
                if g.get('visible') and g.get('kind') == 'shape']
             + [(o['id'], o['outer']) for o in (occluders or []) + (charts or [])
                + (targets or []) if o.get('visible')])
    findings += _arrow_findings(page_index, graphics or [], boxes)

    # A native chart's plot area is laid out by each renderer (this
    # preview, PowerPoint, LibreOffice) on its own, so a shape or line drawn
    # over a chart cannot register with its data points in all of them.
    for chart in charts or []:
        if not chart.get('visible'):
            continue
        box = chart['outer']
        for graphic in graphics or []:
            if (not graphic.get('visible')
                    or int(graphic.get('z', -1)) <= int(chart.get('z', -1))
                    or not (graphic.get('stroked') or graphic.get('filled'))):
                continue
            if not any(box['left'] + tolerance < x < box['right'] - tolerance
                       and box['top'] + tolerance < y < box['bottom'] - tolerance
                       for x, y in graphic.get('points') or []):
                continue
            findings.append({
                'type': 'graphic_over_chart',
                'page': page_index + 1,
                'elements': [graphic['id'], chart['id']],
                'message': (
                    f'page {page_index + 1}: the {graphic.get("kind")} '
                    f'"{graphic["id"]}" is drawn over chart '
                    f'"{chart["id"]}"'),
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
    all_breaks = []
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
                        measurement = browser_page.evaluate(
                            _MEASURE_JS, _heading_ids(slide_page))
                        records = measurement.get('texts') or []
                        findings = _page_findings(
                            records, index, tolerance=tolerance,
                            occluders=measurement.get('occluders') or [],
                            shapes=measurement.get('shapes') or [],
                            graphics=measurement.get('graphics') or [],
                            charts=measurement.get('charts') or [],
                            page_background=measurement.get(
                                'pageBackground') or '',
                            targets=measurement.get('targets') or [])
                        findings += _sketched_plots(slide_page.raw, index)
                        findings += _cell_contrast(
                            measurement.get('cells') or [], index)
                        findings += _table_overflow(
                            measurement.get('tables') or [], index,
                            tolerance)
                        findings += _text_over_table(
                            records, measurement.get('tables') or [], index,
                            tolerance)
                        breaks = _page_line_breaks(records, index)
                        page_results.append({
                            'index': index,
                            'ok': not findings,
                            'findings': findings,
                            'line_breaks': breaks,
                            'elementsMeasured': len(records),
                        })
                        all_findings.extend(findings)
                        all_breaks.extend(breaks)
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
            'findings': all_findings, 'line_breaks': all_breaks}


def _contrast_shortfall(colour: str, background: str, size: float,
                        bold: bool) -> tuple | None:
    """``(ratio, required)`` when ``colour`` on ``background`` is below the
    WCAG minimum for that text size, else ``None``."""
    fg, bg = _rgba(colour), _rgba(background)
    if fg is None or bg is None or bg[3] < 1:
        return None
    large = size >= _WCAG_LARGE_TEXT_PX or (bold and size >= _WCAG_LARGE_BOLD_TEXT_PX)
    needed = _WCAG_LARGE_TEXT_CONTRAST if large else _WCAG_TEXT_CONTRAST
    ratio = _contrast(fg, bg, fg[3])
    return (ratio, needed) if ratio < needed else None


def _cell_contrast(cells: list, page_index: int) -> list:
    """Table cell text below WCAG contrast on the cell's own fill, one
    finding per table naming its worst cell."""
    worst: dict = {}
    for cell in cells:
        short = _contrast_shortfall(cell.get('color'), cell.get('background'),
                                    float(cell.get('size') or 0),
                                    bool(cell.get('bold')))
        if short and (cell['table'] not in worst
                      or short[0] < worst[cell['table']][0][0]):
            worst[cell['table']] = (short, cell)
    return [{
        'type': 'text_contrast', 'page': page_index + 1,
        'elements': [table_id], 'contrast': round(short[0], 2),
        'required': short[1],
        'message': (f'page {page_index + 1}: table "{table_id}" cell '
                    f'"{cell.get("text")}" colour {cell.get("color")} on '
                    f'{cell.get("background")} has contrast {short[0]:.2f}:1 '
                    f'(WCAG minimum {short[1]}:1)'),
    } for table_id, (short, cell) in worst.items()]


def _table_overflow(tables: list, page_index: int, tolerance: float) -> list:
    """A table whose rendered grid runs past its box paints its rows over
    whatever sits below or beside it (text collision checks see only text
    boxes, not table rows)."""
    findings = []
    for table in tables:
        grid, outer = table.get('grid'), table.get('outer')
        if not grid or not outer:
            continue
        below = grid['bottom'] - outer['bottom']
        beside = grid['right'] - outer['right']
        if below <= tolerance and beside <= tolerance:
            continue
        height = grid['bottom'] - outer['top']
        width = grid['right'] - outer['left']
        findings.append({
            'type': 'table_overflow', 'page': page_index + 1,
            'elements': [table['id']],
            'message': (
                f'page {page_index + 1}: table "{table["id"]}" renders '
                f'{width:.0f}x{height:.0f}px but its box is '
                f'{outer["right"] - outer["left"]:.0f}x'
                f'{outer["bottom"] - outer["top"]:.0f}px, so its rows run '
                'over the elements around it. Give the box that size only '
                'where that space is empty; otherwise drop rows or columns '
                'the claim does not need, or shorten header and cell text. '
                'Keep the numbers the page claims.'),
        })
    return findings


def _text_over_table(records: list, tables: list, page_index: int,
                     tolerance: float) -> list:
    """Text lines painted across a table's rendered grid (a caption laid
    over its first row). Text collision checks compare text boxes only."""
    findings = []
    for record in records:
        if not record.get('visible') or record.get('allowOverlap'):
            continue
        lines = _union(record.get('rects') or [])
        if lines is None:
            continue
        for table in tables:
            grid = table.get('grid')
            if not grid:
                continue
            width, height = _intersection(lines, grid, tolerance)
            if width <= 0 or height <= 0:
                continue
            findings.append({
                'type': 'text_table_collision', 'page': page_index + 1,
                'elements': [record['id'], table['id']],
                'message': (
                    f'page {page_index + 1}: text "{record["id"]}" is painted '
                    f'over table "{table["id"]}" ({width:.0f}x{height:.0f}px '
                    f'of its rows): table y={grid["top"]:.0f}..'
                    f'{grid["bottom"]:.0f}, text y={lines["top"]:.0f}..'
                    f'{lines["bottom"]:.0f}. Move the text or the table '
                    'clear of each other into empty space, or shorten the '
                    'table; keep the numbers the page claims.'),
            })
    return findings


def _sketched_plots(page_data, page_index: int) -> list:
    """Lines that sketch a data shape: three or more points with a slanted
    segment and no arrowhead. A hand-placed curve has no values, so its
    peaks and markers land wherever the drawing (and its smoothing) puts
    them; a plotted quantity is a chart built from evidence numbers."""
    from lib.slides.components import expand_page_components
    try:
        elements = expand_page_components(page_data) \
            if isinstance(page_data, dict) else []
    except ValueError:
        return []
    findings = []
    for element in elements:
        if (not isinstance(element, dict)
                or element.get('elementType') != 'line'
                or any(element.get('arrow') or [])):
            continue
        points = []
        for token in str(element.get('points') or '').split():
            try:
                x, y = token.split(',')
                points.append((float(x), float(y)))
            except ValueError:
                continue
        if len(points) < 3 or not any(
                a[0] != b[0] and a[1] != b[1]
                for a, b in zip(points, points[1:])):
            continue
        element_id = str(element.get('elementId') or '')
        findings.append({
            'type': 'sketched_plot', 'page': page_index + 1,
            'elements': [element_id],
            'message': (f'page {page_index + 1}: line "{element_id}" '
                        f'sketches a curve through {len(points)} points'),
        })
    return findings


def _page_line_breaks(records: list, page_index: int) -> list:
    """Texts with a widow (a paragraph's last line holds one word) or a word
    split across two lines.

    Advisory, never a gate finding: the layout stage repairs the ones
    geometry can (``line_break_repairs``) and the visual review judges the
    rest.
    """
    out = []
    for record in records:
        phrase = record.get('phraseBreaks') or []
        if not record.get('visible') or not (record.get('widow')
                                             or record.get('split') or phrase):
            continue
        kinds = [kind for kind, flag in (('a single-word last line',
                                          record.get('widow')),
                                         ('a word split across lines',
                                          record.get('split')),
                                         ('heading lines not broken at their '
                                          'most balanced phrase boundaries',
                                          phrase)) if flag]
        out.append({
            'phraseBreaks': [int(count) for count in phrase],
            'type': 'text_line_break', 'page': page_index + 1,
            'elements': [record['id']],
            'widow': bool(record.get('widow')),
            'split': bool(record.get('split')),
            'painted': bool(record.get('painted')),
            'innerWidth': float(record.get('innerWidth') or 0),
            'balancedWidth': float(record.get('balancedWidth') or 0),
            'cleanWidth': float(record.get('cleanWidth') or 0),
            'message': (f'page {page_index + 1}: text "{record["id"]}" has '
                        + ' and '.join(kinds))})
    return out


def line_break_repairs(page_data: dict, breaks: list) -> dict:
    """Re-break each badly broken text so its lines break cleanly.

    A heading with a measured phrase plan (``phraseBreaks``) gets explicit
    ``<br/>`` breaks there: both renderings then break at the same reader
    pauses, whatever each one's font metrics. Other texts are narrowed.

    The target text width is the widest one at which the line count holds
    with no widow and no split word (measured pixel by pixel down to the
    balanced width); failing that, a widow alone takes the browser's
    ``text-wrap: balance`` width, which pulls words onto the lone last line.
    The box keeps its alignment edge (left, centre or right). A painted text
    background or justified text would visibly change, so those are left to
    the author. Same return shape as ``relayout_repairs``.
    """
    texts = _text_elements(page_data)
    repairs = {}
    for flaw in breaks:
        element_id = flaw['elements'][0]
        element = texts.get(element_id)
        if element is None:
            continue
        content = element['content']
        if flaw.get('phraseBreaks'):
            repairs[element_id] = {'text': _with_line_breaks(
                str(content.get('text') or ''), flaw['phraseBreaks'])}
            continue
        align = content.get('align')
        halign = align[0] if isinstance(align, list) and align else 'left'
        if halign not in ('left', 'center', 'right') or flaw.get('painted'):
            continue
        target = flaw.get('cleanWidth') or (
            flaw['balancedWidth'] if flaw.get('widow') else 0)
        if not target:
            continue
        x, y, width, height = (float(v) for v in element['bounds'])
        narrowed = math.ceil(width - flaw['innerWidth'] + target)
        if not 0 < narrowed < width:
            continue
        if halign == 'center':
            x += (width - narrowed) / 2
        elif halign == 'right':
            x += width - narrowed
        repairs[element_id] = {'bounds': [round(v, 1) for v in (x, y, narrowed, height)],
                               'valign': _valign(element)}
    return repairs


_SOURCE_TOKEN_RE = re.compile(r'<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|\w+);|.', re.S)


def _with_line_breaks(text: str, counts: list) -> str:
    """``text`` with ``<br/>`` right after each of ``counts`` non-space
    visible characters (tags are invisible, an entity is one character).
    Whitespace after a break is dropped, so the new line starts flush in
    both renderings."""
    targets = {int(count) for count in counts if int(count) > 0}
    out: list = []
    seen = 0
    at_break = False
    for token in _SOURCE_TOKEN_RE.findall(text):
        visible = not token.startswith('<')
        space = visible and html.unescape(token).isspace()
        if at_break and space:
            continue
        out.append(token)
        if visible and not space:
            seen += 1
            at_break = seen in targets
            if at_break:
                out.append('<br/>')
    return ''.join(out)


def _heading_ids(page) -> list:
    """Text elements set in the deck's title style."""
    return [str(element.get('elementId')) for element in page.elements
            if isinstance(element, dict) and element.get('elementType') == 'text'
            and isinstance(element.get('content'), dict)
            and element['content'].get('style') == '$title']


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
    out = {eid: {'bounds': [round(v, 1) for v in boxes[eid]],
                 'valign': valigns[eid]} for eid in sorted(changed)}
    out.update(_rule_repairs(page_data, findings, page_height=page_height))
    # A box-level colour below contrast takes the theme's ink, or its page
    # colour for text on a dark fill; the caller measures both variants.
    for finding in findings:
        if (finding.get('type') != 'text_contrast'
                or finding['elements'][0] not in texts):
            continue
        out.setdefault(finding['elements'][0], {})['color'] = (
                '$ink' if move == 'lower' else '$bg')
    return out


def _rule_repairs(page_data: dict, findings: list, *, page_height: int) -> dict:
    """Move a straight rule off a text's glyphs; the text keeps its place.

    A horizontal rule goes to the nearer edge of the text's line boxes
    (above its first line or below its last). A vertical rule that passes
    through the text goes to its nearer side; one that ends inside it (a
    tick, a leader) is shifted along itself until it ends at the text.
    """
    lines = {element.get('elementId'): element
             for element in page_data.get('elements') or []
             if isinstance(element, dict)
             and element.get('elementType') == 'line'
             and isinstance(element.get('bounds'), list)
             and len(element['bounds']) == 4}
    out: dict = {}
    for finding in findings:
        if finding.get('type') != 'stroke_through_text':
            continue
        text_id, line_id = finding['elements']
        crossing = finding.get('crossing') or {}
        measured = (finding.get('measured') or {}).get(text_id)
        if line_id not in lines or line_id in out or not measured:
            continue
        x, y, w, h = (float(v) for v in lines[line_id]['bounds'])
        half = float(finding.get('strokeWidth') or 0) / 2
        box = measured['lines']
        dx = dy = 0.0
        if crossing.get('top') == crossing.get('bottom'):
            rule = float(crossing['top'])
            dy = (box['top'] - half - rule if rule - box['top']
                  <= box['bottom'] - rule else box['bottom'] + half - rule)
        elif crossing.get('left') == crossing.get('right'):
            rule = float(crossing['left'])
            if y + h <= box['bottom'] and crossing['top'] > box['top']:
                dy = box['top'] - half - crossing['bottom']
            elif y >= box['top'] and crossing['bottom'] < box['bottom']:
                dy = box['bottom'] + half - crossing['top']
            else:
                dx = (box['left'] - half - rule if rule - box['left']
                      <= box['right'] - rule
                      else box['right'] + half - rule)
        else:
            continue
        new_x = math.floor(x + dx) if dx < 0 else math.ceil(x + dx)
        new_y = math.floor(y + dy) if dy < 0 else math.ceil(y + dy)
        if (new_y < 0 or new_y + h > page_height or new_x < 0
                or (new_x, new_y) == (x, y)):
            continue
        out[line_id] = {'bounds': [new_x, new_y, round(w, 1), round(h, 1)]}
    return out


def apply_relayout(page_data: dict, repairs: dict) -> None:
    """Write ``relayout_repairs`` output into the page's elements."""
    for element in page_data.get('elements') or []:
        if not isinstance(element, dict):
            continue
        repair = repairs.get(element.get('elementId'))
        if not repair:
            continue
        if 'color' in repair:
            element['content']['color'] = repair['color']
        if 'text' in repair:
            element['content']['text'] = repair['text']
        if 'bounds' not in repair:
            continue
        element['bounds'] = repair['bounds']
        if 'valign' in repair and _valign(element) != repair['valign']:
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
        elif kind == 'text_outside_container' and measured:
            element_id, container_id = finding['elements']
            m = measured[element_id]
            box = finding['container']
            available = box['bottom'] - m['lines']['top']
            needed = m['lines']['bottom'] - m['lines']['top']
            chars = _visible_chars(texts.get(element_id))
            cut = ''
            if chars and 0 < available < needed:
                cut = (f', or cut this text to about '
                       f'{int(chars * available / needed)} characters '
                       f'(now {chars})')
            lines.append(
                f'{message}. The text spans x={m["lines"]["left"]:.0f}..'
                f'{m["lines"]["right"]:.0f}, y={m["lines"]["top"]:.0f}..'
                f'{m["lines"]["bottom"]:.0f}; "{container_id}" spans '
                f'x={box["left"]:.0f}..{box["right"]:.0f}, '
                f'y={box["top"]:.0f}..{box["bottom"]:.0f}: grow the container '
                f'or move the text inside it, only into empty space{cut}; '
                'keep its claim and numbers.')
        elif kind == 'stroke_through_text':
            element_id, graphic_id = finding['elements']
            lines.append(
                f'{message}. Move or resize "{graphic_id}" so its outline '
                f'clears the text, or move "{element_id}" clear of it, only '
                'into empty space; keep their claims and numbers.')
        elif kind == 'text_contrast':
            lines.append(
                f'{message}. Use a theme text colour that reaches '
                f'{finding["required"]}:1 on that background ($ink on light, '
                '$bg on dark), or move the text onto a background it '
                'contrasts with.')
        elif kind == 'arrow_to_nothing' and finding.get('target'):
            x, y = finding['reach_point']
            lines.append(
                f'{message}, whose drawn content starts at x={x:.0f}, '
                f'y={y:.0f} along it: end the arrow there (or move '
                f'"{finding["target"]}" up to its tip), keeping its direction.')
        elif kind == 'arrow_to_nothing':
            lines.append(
                f'{message}. End it on the element it points to; if it is an '
                'axis, a timeline or a decoration, drop its arrowhead.')
        elif kind == 'sketched_plot':
            lines.append(
                f'{message}. A hand-drawn curve encodes no values: its '
                'peaks and any markers on it do not match the data. Replace '
                'it with a chart element built from the evidence numbers, or '
                'state the shape in words; delete markers placed on it.')
        elif kind == 'graphic_over_chart':
            graphic_id, chart_id = finding['elements']
            lines.append(
                f'{message}. PowerPoint and LibreOffice lay out a native '
                'chart\'s plot area themselves, so a hand-placed marker, '
                'leader line or reference line never lands on its data. '
                f'Delete "{graphic_id}" and say it with the chart itself (a '
                'series for a reference value, data labels) or with a text '
                f'outside "{chart_id}"\'s bounds.')
        else:
            lines.append(message)
    return '\n'.join(lines)
