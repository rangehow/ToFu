"""Fixture-checkable PDF fidelity gate for born-digital documents.

Entry points:
  * ``run_gate(source, rendered, declared_fonts=..., mode=...)`` returns a
    machine-readable report (schema ``tofu.pdf-fidelity/v1``).
  * ``main(argv)`` is the CLI used by CI and workers; it returns 1 when any
    check fails and 0 when every check passes.

The gate has two explicit modes:

``reproduction`` (default, unchanged)
    The rendered PDF must reproduce the source wording and geometry:

      page_count           document-level page total
      page_size            width/height of each page
      columns              number of body-text columns
      reading_order        canonical block order (column-major)
      block_bbox_iou       every corresponding block's bbox IoU
      figure_table_bbox_exact  figure and table boxes must not drift at all
      text_presence        no source text block may be dropped
      font_family          used families and source->output mapping agree with
                           the declared Latin/CJK stack
      reserved_regions     no block intrudes into a figure/table region

``translation``
    The page keeps its *format* while text is translated, so source wording
    cannot survive by definition. The checks become:

      page_count / page_size / columns   unchanged document geometry
      reading_order        blocks keep their column-major order
      figure_table_bbox_exact  figures/tables/ruled elements keep their box
      non_translated_bbox  headers, folios, math-only and skipped text keep
                           their original box within the reproduction tolerance
      translated_block_placement  a translated block stays inside its original
                           column band and intrudes into no figure/table or
                           another block's region
      target_presence      every source block still produces a non-empty
                           target block (nothing is dropped)
      refused_text_present a refused block still shows its source text
      translation_manifest the supplied block manifest covers every source
                           block exactly once (inference when omitted)
      font_family          output families map to the declared stack

All tolerances below are part of the report, so a failure is reproducible
from the report alone. They are deliberately small: a moved block, a dropped
word, a different font or a resized page must fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:  # PyMuPDF >= 1.24.3, with the legacy module name as a fallback.
    import pymupdf
except ImportError:  # pragma: no cover - the test extra always installs PyMuPDF
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        pymupdf = None  # type: ignore[assignment]

REPORT_SCHEMA = 'tofu.pdf-fidelity/v1'

MODES = ('reproduction', 'translation')

#: Tolerances are reported verbatim; changing one changes the gate's contract.
TOLERANCES = {
    'block_iou': 0.85,
    'figure_table_iou': 0.999,
    'page_size_pt': 0.01,
    'overlap_ratio': 0.02,
    'reserved_overlap_pt2': 1.0,
    'column_gutter_min_pt': 8.0,
    'column_gutter_page_fraction': 0.012,
}

CHECK_NAMES = (
    'page_count', 'page_size', 'columns', 'reading_order', 'block_bbox_iou',
    'figure_table_bbox_exact', 'text_presence', 'font_family',
    'reserved_regions',
)

TRANSLATION_CHECK_NAMES = (
    'page_count', 'page_size', 'columns', 'reading_order',
    'figure_table_bbox_exact', 'non_translated_bbox',
    'translated_block_placement', 'target_presence', 'refused_text_present',
    'translation_manifest', 'font_family',
)

#: Extra tolerances used only in translation mode, so reproduction keeps its
#: original tolerance contract.  A column band is the extracted x-range; the
#: renderer allows a glyph's advance to reach one page-size tolerance beyond
#: the measured ink extent.
TRANSLATION_TOLERANCES = {
    'column_band_pt': 1.0,
}

_TRANSLATION_STATUSES = ('translated', 'preserved', 'refused', 'fixed')

_CJK_RE = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]')
_SPACE_RE = re.compile(r'\s+')
_SUBSET_PREFIX_RE = re.compile(r'^[A-Z]{6}\+')
_STYLE_SUFFIX_RE = re.compile(
    r'[-_, ](bold|italic|oblique|light|medium|semibold|demibold|black|regular|roman|book|thin|'
    r'extrabold|extralight)$', re.IGNORECASE)


def _open_pdf(value):
    if pymupdf is None:
        raise RuntimeError('PyMuPDF is required for the PDF fidelity gate')
    if isinstance(value, (bytes, bytearray)):
        return pymupdf.open(stream=bytes(value), filetype='pdf')
    return pymupdf.open(value)


def _sha256(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(value).hexdigest()
    digest = hashlib.sha256()
    with open(value, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _norm_text(text: str) -> str:
    return _SPACE_RE.sub(' ', (text or '').replace('\u00ad', '')).strip()


def _family(font: str) -> str:
    """Family name with subset prefix and weight/slant style removed."""
    family = _SUBSET_PREFIX_RE.sub('', font or '').split(',')[0].strip()
    while _STYLE_SUFFIX_RE.search(family):
        family = _STYLE_SUFFIX_RE.sub('', family).strip()
    return family


def _family_key(font: str) -> str:
    """Comparison key tolerant of PostScript spelling vs a human family name."""
    return re.sub(r'[\s_-]+', '', _family(font)).lower()


def _iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def _intersection_area(a, b) -> float:
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return iw * ih


def _area(bbox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _table_rects(page):
    try:
        tables = page.find_tables(strategy='lines').tables
    except Exception:
        return []
    rows = []
    for table in tables:
        text = _norm_text(' '.join(' '.join(cell or '' for cell in row)
                                   for row in table.extract()))
        rows.append((tuple(table.bbox), text))
    return rows


def _page_blocks(page) -> list[dict]:
    """Typed content blocks for one page.

    Text blocks are split by ``get_text('dict')``; table cell text is folded
    into its table block so a table is one content block with one bbox.
    """
    tables = _table_rects(page)
    blocks: list[dict] = []
    for raw in page.get_text('dict').get('blocks', []):
        bbox = tuple(float(x) for x in raw['bbox'])
        if raw.get('type') == 1:
            blocks.append({'role': 'figure', 'bbox': bbox, 'text': '',
                           'fonts': {}, 'lines': []})
            continue
        text = ''
        fonts: dict[str, set] = {'latin': set(), 'cjk': set()}
        lines = []
        for line in raw.get('lines', []):
            for span in line.get('spans', []):
                span_text = span.get('text', '')
                text += span_text
                if span_text.strip():
                    script = 'cjk' if _CJK_RE.search(span_text) else 'latin'
                    fonts[script].add(_family(span.get('font', '')))
            if line.get('spans'):
                lines.append({'bbox': tuple(float(x) for x in line['bbox']),
                              'size': max(float(s.get('size') or 0)
                                          for s in line['spans'])})
        text = text.strip()
        if not text:
            continue
        if any(_area(bbox) > 0 and
               _intersection_area(bbox, rect) >= 0.5 * _area(bbox)
               for rect, _ in tables):
            continue  # table cell text belongs to the table block
        blocks.append({'role': 'text', 'bbox': bbox, 'text': text,
                       'fonts': fonts, 'lines': lines})
    for rect, text in tables:
        blocks.append({'role': 'table', 'bbox': rect, 'text': text,
                       'fonts': {}, 'lines': []})
    return sorted(blocks, key=lambda block: (block['bbox'][1], block['bbox'][0]))


def _gutter(page_width: float, tolerances: dict) -> float:
    return max(tolerances['column_gutter_min_pt'],
               page_width * tolerances['column_gutter_page_fraction'])


def _column_bands(blocks: list[dict], page_width: float,
                  tolerances: dict) -> list[tuple[float, float]]:
    """Occupied body-text x bands; a wide empty gutter separates columns."""
    lines = [line for block in blocks if block['role'] == 'text'
             for line in block['lines']]
    if not lines:
        return [(0.0, page_width)]
    sizes = sorted(line['size'] for line in lines if line['size'] > 0)
    median = sizes[len(sizes) // 2] if sizes else 0.0
    intervals = []
    for line in lines:
        x0, _, x1, _ = line['bbox']
        width = x1 - x0
        # A spanning title/heading is not body-column evidence.
        if width < 2.0 or width > page_width * 0.60:
            continue
        if median and line['size'] > median * 1.15:
            continue
        intervals.append((x0, x1))
    if not intervals:
        return [(0.0, page_width)]
    intervals.sort()
    gap = _gutter(page_width, tolerances)
    bands = [list(intervals[0])]
    for x0, x1 in intervals[1:]:
        if x0 - bands[-1][1] <= gap:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return [(float(x0), float(x1)) for x0, x1 in bands]


def _band_index(x0: float, bands: list[tuple[float, float]]) -> int:
    best, best_distance = 0, float('inf')
    for index, (start, end) in enumerate(bands):
        distance = 0.0 if start <= x0 <= end else min(abs(x0 - start), abs(x0 - end))
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def _signature(block: dict) -> str:
    text = _norm_text(block['text'])
    return f"{block['role']}:{text}" if text else 'figure'


def _ordered_signatures(blocks: list[dict], bands) -> list[str]:
    ordered = sorted(blocks, key=lambda b: (_band_index(b['bbox'][0], bands),
                                            b['bbox'][1], b['bbox'][0]))
    return [_signature(block) for block in ordered]


def _match_blocks(source: list[dict], rendered: list[dict]) -> list[dict]:
    """Greedy one-to-one position match within the same role."""
    unused = set(range(len(rendered)))
    pairs = []
    for s_index, s_block in enumerate(source):
        candidates = [r_index for r_index in unused
                      if rendered[r_index]['role'] == s_block['role']]
        if not candidates:
            continue
        r_index = max(candidates, key=lambda i: _iou(s_block['bbox'],
                                                      rendered[i]['bbox']))
        score = _iou(s_block['bbox'], rendered[r_index]['bbox'])
        if score <= 0.05:
            continue
        unused.remove(r_index)
        pairs.append({'source': s_index, 'rendered': r_index, 'iou': round(score, 6)})
    return pairs


def _counter_filter(sequence: list[str], limit: dict[str, int]) -> list[str]:
    remaining = dict(limit)
    kept = []
    for key in sequence:
        if remaining.get(key, 0) > 0:
            kept.append(key)
            remaining[key] -= 1
    return kept


def _check_page_size(source_page, rendered_page, tolerances) -> dict:
    sw, sh = source_page.rect.width, source_page.rect.height
    rw, rh = rendered_page.rect.width, rendered_page.rect.height
    ok = abs(sw - rw) <= tolerances['page_size_pt'] and abs(sh - rh) <= tolerances['page_size_pt']
    return {'ok': ok, 'source': {'width': round(sw, 3), 'height': round(sh, 3)},
            'rendered': {'width': round(rw, 3), 'height': round(rh, 3)}}


def _check_columns(source, rendered, page_width, tolerances) -> dict:
    source_bands = _column_bands(source, page_width, tolerances)
    rendered_bands = _column_bands(rendered, page_width, tolerances)
    return {'ok': len(source_bands) == len(rendered_bands),
            'source': len(source_bands), 'rendered': len(rendered_bands),
            'source_bands': [list(map(lambda v: round(v, 2), band)) for band in source_bands],
            'rendered_bands': [list(map(lambda v: round(v, 2), band))
                               for band in rendered_bands]}


def _check_reading_order(source, rendered, page_width, tolerances) -> dict:
    bands = _column_bands(source, page_width, tolerances)
    source_order = _ordered_signatures(source, bands)
    rendered_order = _ordered_signatures(rendered, bands)
    source_counts, rendered_counts = {}, {}
    for key in source_order:
        source_counts[key] = source_counts.get(key, 0) + 1
    for key in rendered_order:
        rendered_counts[key] = rendered_counts.get(key, 0) + 1
    common = {key: min(source_counts.get(key, 0), rendered_counts.get(key, 0))
              for key in set(source_counts) | set(rendered_counts)}
    expected = _counter_filter(source_order, common)
    actual = _counter_filter(rendered_order, common)
    return {'ok': expected == actual, 'source': expected, 'rendered': actual}


def _check_bbox_iou(source, rendered, pairs, tolerances) -> dict:
    rows = []
    for pair in pairs:
        iou = pair['iou']
        rows.append({'source': pair['source'], 'rendered': pair['rendered'],
                     'role': source[pair['source']]['role'], 'iou': iou,
                     'ok': iou >= tolerances['block_iou']})
    matched = {row['source'] for row in rows}
    missing = [index for index in range(len(source)) if index not in matched]
    return {'ok': all(row['ok'] for row in rows), 'blocks': rows,
            'unmatched_source_blocks': missing,
            'note': 'unmatched blocks are owned by text_presence, not IoU'}


def _check_figure_table_exact(source, rendered, pairs, tolerances) -> dict:
    by_source = {pair['source']: pair for pair in pairs}
    rows = []
    for index, block in enumerate(source):
        if block['role'] not in ('figure', 'table'):
            continue
        pair = by_source.get(index)
        iou = pair['iou'] if pair else 0.0
        rows.append({'source': index, 'role': block['role'], 'iou': iou,
                     'ok': bool(pair) and iou >= tolerances['figure_table_iou']})
    return {'ok': all(row['ok'] for row in rows), 'blocks': rows}


def _check_text_presence(source, rendered) -> dict:
    available = {}
    for index, block in enumerate(rendered):
        text = _norm_text(block['text'])
        if text:
            available.setdefault(text, []).append(index)
    used = set()
    missing = []
    for index, block in enumerate(source):
        text = _norm_text(block['text'])
        if not text:
            continue
        match = next((candidate for candidate in available.get(text, ())
                      if candidate not in used), None)
        if match is None:
            missing.append({'source': index, 'role': block['role'],
                            'text': text[:120]})
        else:
            used.add(match)
    return {'ok': not missing, 'missing': missing}


def _block_script_families(block: dict) -> dict[str, set]:
    return {script: set(block['fonts'].get(script, ())) for script in ('latin', 'cjk')}


def _check_font_family(source, rendered, pairs, declared) -> dict:
    used = {'latin': set(), 'cjk': set()}
    for block in rendered:
        if block['role'] != 'text':
            continue
        for script, families in _block_script_families(block).items():
            used[script].update(families)
    undeclared = []
    if declared:
        for script in ('latin', 'cjk'):
            allowed = set(declared.get(script, ()))
            allowed_keys = {_family_key(family) for family in allowed}
            for family in sorted(used[script]):
                if _family_key(family) not in allowed_keys:
                    undeclared.append({'script': script, 'family': family,
                                       'allowed': sorted(allowed)})
    mapping: dict[str, dict[str, list[str]]] = {'latin': {}, 'cjk': {}}
    by_source = {pair['source']: pair for pair in pairs}
    for index, block in enumerate(source):
        pair = by_source.get(index)
        if block['role'] != 'text' or pair is None:
            continue
        rendered_block = rendered[pair['rendered']]
        for script in ('latin', 'cjk'):
            source_families = _block_script_families(block)[script]
            target_families = _block_script_families(rendered_block)[script]
            for family in source_families:
                bucket = mapping[script].setdefault(family, [])
                for target in target_families:
                    if target not in bucket:
                        bucket.append(target)
    inconsistent = [{'script': script, 'source_family': family, 'rendered_families': targets,
                     'declared': declared}
                    for script, families in mapping.items()
                    for family, targets in families.items()
                    if len(targets) > 1]
    return {'ok': not undeclared and not inconsistent,
            'declared': declared,
            'used': {script: sorted(values) for script, values in used.items()},
            'undeclared': undeclared,
            'mapping': {script: {family: sorted(targets) for family, targets in families.items()}
                        for script, families in mapping.items()},
            'inconsistent': inconsistent}


def _check_reserved_regions(rendered, tolerances) -> dict:
    overlaps = []
    for first in range(len(rendered)):
        for second in range(first + 1, len(rendered)):
            a, b = rendered[first], rendered[second]
            area = _intersection_area(a['bbox'], b['bbox'])
            if area <= 0:
                continue
            reserved = a['role'] in ('figure', 'table') or b['role'] in ('figure', 'table')
            if reserved:
                bad = area > tolerances['reserved_overlap_pt2']
            else:
                smaller = min(_area(a['bbox']), _area(b['bbox']))
                bad = smaller > 0 and area > tolerances['overlap_ratio'] * smaller
            if bad:
                overlaps.append({'first': first, 'second': second,
                                 'roles': [a['role'], b['role']],
                                 'area': round(area, 3)})
    return {'ok': not overlaps, 'overlaps': overlaps}


# ── translation mode ──────────────────────────────────────────────────────


def _manifest_pages(translation):
    """Normalise a translation manifest to ``{page_number: [blocks]}``.

    The manifest is optional: without one the gate infers which source blocks
    were translated from text presence in the rendered page.  A supplied
    manifest is authoritative about *status* (translated/preserved/refused/
    fixed), never about source geometry, which still comes from the PDF.
    """
    if translation is None:
        return None
    raw_pages = translation.get('pages') if isinstance(translation, dict) else translation
    if not isinstance(raw_pages, (list, tuple)):
        raise ValueError('translation manifest must be a list of pages')
    pages = {}
    for index, page in enumerate(raw_pages):
        if not isinstance(page, dict):
            raise ValueError(f'translation manifest page {index} is not an object')
        number = int(page.get('page', index + 1))
        blocks = page.get('blocks') or ()
        if not isinstance(blocks, (list, tuple)):
            raise ValueError(f'translation manifest page {number} blocks must be a list')
        pages[number] = list(blocks)
    return pages


def _entry_bbox(entry: dict):
    bbox = entry.get('source_bbox') or entry.get('bbox')
    if not bbox or len(bbox) != 4:
        raise ValueError(f'translation manifest block has no source bbox: {entry!r}')
    return tuple(float(value) for value in bbox)


def _entry_status(entry: dict) -> str:
    status = entry.get('status')
    if status not in _TRANSLATION_STATUSES:
        raise ValueError(f'translation manifest status is invalid: {entry!r}')
    return status


def _match_manifest(source: list[dict], entries: list[dict], tolerances) -> dict:
    """Map every source block to at most one manifest entry.

    Text entries match on normalized source text when supplied, otherwise on
    bbox IoU.  Fixed entries (figure/table) must declare ``status='fixed'`` and
    match by role and bbox.  This is what stops an empty or partial manifest
    from turning translation mode into a vacuous pass.
    """
    unused = set(range(len(entries)))
    mapping: dict[int, int] = {}
    for index, block in enumerate(source):
        best = None
        for candidate in sorted(unused):
            entry = entries[candidate]
            status = _entry_status(entry)
            if block['role'] in ('figure', 'table'):
                if status != 'fixed':
                    continue
            elif status == 'fixed':
                continue
            iou = _iou(block['bbox'], _entry_bbox(entry))
            declared_text = entry.get('source_text')
            text_match = (
                block['role'] == 'text' and declared_text is not None
                and _norm_text(declared_text) == _norm_text(block['text'])
            )
            if not text_match and iou < tolerances['block_iou']:
                continue
            score = (2.0 if text_match else 0.0) + iou
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is not None:
            mapping[index] = best[1]
            unused.discard(best[1])
    return {'mapping': mapping, 'unused': sorted(unused)}


def _translation_statuses(source: list[dict], rendered: list[dict],
                          entries, tolerances):
    """Per-source-block status plus the manifest-coverage detail."""
    if entries is None:
        rendered_text = {_norm_text(block['text']) for block in rendered
                         if block['role'] == 'text'}
        statuses = []
        for block in source:
            if block['role'] in ('figure', 'table'):
                statuses.append('fixed')
            elif _norm_text(block['text']) in rendered_text:
                statuses.append('preserved')
            else:
                statuses.append('translated')
        return statuses, {'ok': True, 'declared': False,
                          'source_blocks': len(source),
                          'note': 'no manifest; status inferred from source '
                                  'text presence in the rendered page'}, \
            [None] * len(source)

    matched = _match_manifest(source, entries, tolerances)
    rendered_text = {_norm_text(block['text']) for block in rendered
                     if block['role'] == 'text'}
    statuses = []
    inferred = []
    aligned = []
    for index, block in enumerate(source):
        candidate = matched['mapping'].get(index)
        aligned.append(entries[candidate] if candidate is not None else None)
        if candidate is not None:
            statuses.append(_entry_status(entries[candidate]))
            continue
        # An uncovered block is a manifest failure, but inferring its status
        # keeps every other check independent instead of cascading.
        inferred.append(index)
        if block['role'] in ('figure', 'table'):
            statuses.append('fixed')
        elif _norm_text(block['text']) in rendered_text:
            statuses.append('preserved')
        else:
            statuses.append('translated')
    unmatched = [{'source': index, 'role': source[index]['role'],
                  'text': _norm_text(source[index]['text'])[:120]}
                 for index in inferred]
    unused = [{'entry': index, 'status': _entry_status(entries[index])}
              for index in matched['unused']]
    return statuses, {'ok': not unmatched and not unused, 'declared': True,
                      'source_blocks': len(source),
                      'manifest_blocks': len(entries),
                      'unmatched_source_blocks': unmatched,
                      'unused_manifest_blocks': unused}, aligned


def _match_translation_targets(source: list[dict], rendered: list[dict],
                               statuses, source_bands, rendered_bands):
    """Map source blocks to rendered blocks without requiring source wording.

    Preserved/refused blocks are found by their unchanged text; fixed content
    by role and position.  Translated blocks are the remainder and pair in
    column-major order, which is exactly the order the gate then verifies.
    """
    used: set[int] = set()
    pairs: dict[int, int] = {}
    for index, block in enumerate(source):
        if statuses[index] not in ('preserved', 'refused'):
            continue
        text = _norm_text(block['text'])
        candidates = [candidate for candidate, item in enumerate(rendered)
                      if candidate not in used and item['role'] == 'text'
                      and _norm_text(item['text']) == text]
        if candidates:
            chosen = max(candidates, key=lambda candidate: _iou(
                block['bbox'], rendered[candidate]['bbox']))
            used.add(chosen)
            pairs[index] = chosen

    for index, block in enumerate(source):
        if statuses[index] != 'fixed':
            continue
        candidates = [candidate for candidate, item in enumerate(rendered)
                      if candidate not in used and item['role'] == block['role']]
        if candidates:
            chosen = max(candidates, key=lambda candidate: _iou(
                block['bbox'], rendered[candidate]['bbox']))
            used.add(chosen)
            pairs[index] = chosen

    remaining_source = [index for index, status in enumerate(statuses)
                        if status == 'translated' and index not in pairs]
    remaining_rendered = [index for index, item in enumerate(rendered)
                          if index not in used and item['role'] == 'text']
    source_order = sorted(remaining_source, key=lambda index: (
        _band_index(source[index]['bbox'][0], source_bands),
        source[index]['bbox'][1], source[index]['bbox'][0]))
    rendered_order = sorted(remaining_rendered, key=lambda index: (
        _band_index(rendered[index]['bbox'][0], rendered_bands),
        rendered[index]['bbox'][1], rendered[index]['bbox'][0]))
    for source_index, rendered_index in zip(source_order, rendered_order):
        pairs[source_index] = rendered_index
    return pairs


def _allowed_bands(bands, tolerance: float) -> list[tuple[float, float]]:
    """Column bands widened to their shared gutter midpoints.

    ``_column_bands`` measures *occupied ink*, which is narrower than the
    column measure available for a longer translation.  Two adjacent bands
    share the empty gutter; either may use up to the midpoint.  The outer
    edges keep a page-size tolerance for glyph advance.
    """
    allowed = []
    for index, (start, end) in enumerate(bands):
        left = (bands[index - 1][1] + start) / 2.0 if index > 0 else start - tolerance
        right = ((end + bands[index + 1][0]) / 2.0
                 if index + 1 < len(bands) else end + tolerance)
        allowed.append((left, right))
    return allowed


def _translation_bands(source, aligned_entries, source_bands, tolerance):
    """The original column x-band for every source block."""
    allowed = _allowed_bands(source_bands, tolerance)
    bands = []
    for index, block in enumerate(source):
        entry = aligned_entries[index]
        declared = entry.get('column_band') if isinstance(entry, dict) else None
        if declared and len(declared) == 2:
            bands.append((float(declared[0]), float(declared[1])))
        else:
            bands.append(allowed[_band_index(block['bbox'][0], source_bands)])
    return bands


def _check_target_presence(source, rendered, pairs) -> dict:
    missing = []
    for index, block in enumerate(source):
        candidate = pairs.get(index)
        if candidate is None:
            missing.append({'source': index, 'role': block['role'],
                            'text': _norm_text(block['text'])[:120]})
        elif block['role'] == 'text' and not _norm_text(rendered[candidate]['text']):
            missing.append({'source': index, 'role': block['role'],
                            'text': _norm_text(block['text'])[:120],
                            'reason': 'empty target text'})
    return {'ok': not missing, 'missing': missing}


def _check_non_translated_bbox(source, rendered, pairs, statuses, tolerances) -> dict:
    rows = []
    for index, block in enumerate(source):
        if statuses[index] != 'preserved':
            continue
        candidate = pairs.get(index)
        iou = _iou(block['bbox'], rendered[candidate]['bbox']) \
            if candidate is not None else 0.0
        rows.append({'source': index, 'rendered': candidate,
                     'role': block['role'], 'iou': round(iou, 6),
                     'ok': candidate is not None
                     and iou >= tolerances['block_iou']})
    return {'ok': all(row['ok'] for row in rows), 'blocks': rows}


def _check_translated_placement(source, rendered, pairs, statuses, bands,
                                tolerances, page_height) -> dict:
    rows = []
    tolerance = tolerances['column_band_pt']
    for index, block in enumerate(source):
        if statuses[index] != 'translated':
            continue
        candidate = pairs.get(index)
        if candidate is None:
            # A missing target is owned by target_presence, not this check.
            rows.append({'source': index, 'rendered': None, 'ok': True,
                         'note': 'missing target reported by target_presence'})
            continue
        target = rendered[candidate]['bbox']
        band = bands[index]
        inside = (target[0] >= band[0] - tolerance
                  and target[2] <= band[1] + tolerance
                  and target[1] >= -tolerance
                  and target[3] <= page_height + tolerance)
        overlaps = []
        for other_index, other in enumerate(rendered):
            if other_index == candidate:
                continue
            area = _intersection_area(target, other['bbox'])
            if area <= 0:
                continue
            if other['role'] in ('figure', 'table'):
                bad = area > tolerances['reserved_overlap_pt2']
            else:
                smaller = min(_area(target), _area(other['bbox']))
                bad = smaller > 0 and area > tolerances['overlap_ratio'] * smaller
            if bad:
                overlaps.append({'rendered': other_index,
                                 'role': other['role'],
                                 'area': round(area, 3)})
        rows.append({'source': index, 'rendered': candidate,
                     'column_band': [round(band[0], 2), round(band[1], 2)],
                     'target_bbox': [round(value, 2) for value in target],
                     'inside_column': inside, 'overlaps': overlaps,
                     'ok': inside and not overlaps})
    return {'ok': all(row['ok'] for row in rows), 'blocks': rows}


def _check_refused_text(source, rendered, pairs, statuses) -> dict:
    rows = []
    for index, block in enumerate(source):
        if statuses[index] != 'refused':
            continue
        candidate = pairs.get(index)
        expected = _norm_text(block['text'])
        actual = _norm_text(rendered[candidate]['text']) \
            if candidate is not None else ''
        rows.append({'source': index, 'rendered': candidate,
                     'text': expected[:120], 'ok': actual == expected})
    return {'ok': all(row['ok'] for row in rows), 'blocks': rows}


def _check_translation_reading_order(source, rendered, pairs, statuses, bands,
                                     source_bands, rendered_bands, tolerances) -> dict:
    tolerance = tolerances['column_band_pt']
    included = []
    excluded = []
    for index, block in enumerate(source):
        candidate = pairs.get(index)
        if candidate is None:
            excluded.append(index)
            continue
        target = rendered[candidate]['bbox']
        source_band = _band_index(block['bbox'][0], source_bands)
        target_band = _band_index(target[0], rendered_bands)
        moved = source_band != target_band
        if block['role'] == 'text' and statuses[index] == 'translated':
            moved = moved or not (target[0] >= bands[index][0] - tolerance
                                  and target[2] <= bands[index][1] + tolerance)
        if moved:
            # A block outside its own column is owned by that check, not by
            # reading order; excluding it keeps the failure attributable.
            excluded.append(index)
        else:
            included.append(index)
    expected = sorted(included, key=lambda index: (
        _band_index(source[index]['bbox'][0], source_bands),
        source[index]['bbox'][1], source[index]['bbox'][0]))
    actual = sorted(included, key=lambda index: (
        _band_index(rendered[pairs[index]]['bbox'][0], rendered_bands),
        rendered[pairs[index]]['bbox'][1], rendered[pairs[index]]['bbox'][0]))
    return {'ok': expected == actual,
            'source': expected, 'rendered': actual,
            'excluded': excluded}


def _translation_page_report(page_no, source_page, rendered_page, tolerances,
                              declared_fonts, entries):
    source = _page_blocks(source_page)
    rendered = _page_blocks(rendered_page)
    page_width = source_page.rect.width
    source_bands = _column_bands(source, page_width, tolerances)
    rendered_bands = _column_bands(rendered, page_width, tolerances)
    statuses, manifest, aligned_entries = _translation_statuses(
        source, rendered, entries, tolerances)
    pairs = _match_translation_targets(source, rendered, statuses,
                                       source_bands, rendered_bands)
    bands = _translation_bands(source, aligned_entries, source_bands,
                               tolerances['column_band_pt'])
    checks = {
        'page_size': _check_page_size(source_page, rendered_page, tolerances),
        'columns': _check_columns(source, rendered, page_width, tolerances),
        'reading_order': _check_translation_reading_order(
            source, rendered, pairs, statuses, bands, source_bands,
            rendered_bands, tolerances),
        'figure_table_bbox_exact': _check_figure_table_exact(
            source, rendered, [{'source': index, 'rendered': candidate,
                                'iou': _iou(source[index]['bbox'],
                                            rendered[candidate]['bbox'])}
                               for index, candidate in pairs.items()], tolerances),
        'non_translated_bbox': _check_non_translated_bbox(
            source, rendered, pairs, statuses, tolerances),
        'translated_block_placement': _check_translated_placement(
            source, rendered, pairs, statuses, bands, tolerances,
            source_page.rect.height),
        'target_presence': _check_target_presence(source, rendered, pairs),
        'refused_text_present': _check_refused_text(
            source, rendered, pairs, statuses),
        'translation_manifest': manifest,
        'font_family': _check_font_family(
            source, rendered,
            [{'source': index, 'rendered': candidate,
              'iou': _iou(source[index]['bbox'], rendered[candidate]['bbox'])}
             for index, candidate in pairs.items()], declared_fonts),
    }
    failures = [{'page': page_no, 'check': name,
                 'detail': _failure_detail(name, detail)}
                for name, detail in checks.items() if not detail['ok']]
    return {'page': page_no, 'ok': not failures, 'checks': checks,
            'failures': failures}, failures


def _page_report(page_no, source_page, rendered_page, tolerances, declared_fonts):
    source = _page_blocks(source_page)
    rendered = _page_blocks(rendered_page)
    page_width = source_page.rect.width
    pairs = _match_blocks(source, rendered)
    checks = {
        'page_size': _check_page_size(source_page, rendered_page, tolerances),
        'columns': _check_columns(source, rendered, page_width, tolerances),
        'reading_order': _check_reading_order(source, rendered, page_width, tolerances),
        'block_bbox_iou': _check_bbox_iou(source, rendered, pairs, tolerances),
        'figure_table_bbox_exact': _check_figure_table_exact(source, rendered, pairs,
                                                             tolerances),
        'text_presence': _check_text_presence(source, rendered),
        'font_family': _check_font_family(source, rendered, pairs, declared_fonts),
        'reserved_regions': _check_reserved_regions(rendered, tolerances),
    }
    failures = [{'page': page_no, 'check': name,
                 'detail': _failure_detail(name, detail)}
                for name, detail in checks.items() if not detail['ok']]
    return {'page': page_no, 'ok': not failures, 'checks': checks,
            'failures': failures}, failures


def _failure_detail(name, detail) -> str:
    if name == 'page_size':
        return f"source {detail['source']} != rendered {detail['rendered']}"
    if name == 'columns':
        return f"source {detail['source']} column(s) != rendered {detail['rendered']}"
    if name == 'reading_order':
        return f"source order {detail['source']} != rendered order {detail['rendered']}"
    if name == 'block_bbox_iou':
        bad = [row for row in detail['blocks'] if not row['ok']]
        return f"{len(bad)} block(s) below IoU tolerance: {bad}"
    if name == 'figure_table_bbox_exact':
        bad = [row for row in detail['blocks'] if not row['ok']]
        return f"{len(bad)} figure/table block(s) moved: {bad}"
    if name == 'text_presence':
        return f"missing source text: {detail['missing']}"
    if name == 'font_family':
        return f"undeclared={detail['undeclared']} inconsistent={detail['inconsistent']}"
    if name == 'reserved_regions':
        return f"overlaps: {detail['overlaps']}"
    if name == 'non_translated_bbox':
        bad = [row for row in detail['blocks'] if not row['ok']]
        return f"{len(bad)} non-translated block(s) moved: {bad}"
    if name == 'translated_block_placement':
        bad = [row for row in detail['blocks'] if not row['ok']]
        return f"{len(bad)} translated block(s) outside their column or overlapping: {bad}"
    if name == 'target_presence':
        return f"source block(s) without a non-empty target: {detail['missing']}"
    if name == 'refused_text_present':
        bad = [row for row in detail['blocks'] if not row['ok']]
        return f"refused block(s) lost their source text: {bad}"
    if name == 'translation_manifest':
        return (f"unmatched source={detail.get('unmatched_source_blocks')} "
                f"unused manifest={detail.get('unused_manifest_blocks')}")
    return json.dumps(detail, ensure_ascii=False)


def run_gate(source, rendered, *, mode='reproduction', declared_fonts=None,
             tolerances=None, translation=None) -> dict:
    """Compare ``source`` with ``rendered`` and return the fidelity report.

    ``mode`` selects the contract.  ``reproduction`` (the default) demands
    the source wording and block boxes; ``translation`` keeps format
    preservation measurable after the wording changed.  ``declared_fonts`` is
    a mapping such as ``{"latin": ["Helvetica"], "cjk": ["Heiti"]}``.  When
    omitted, the font check verifies source->rendered mapping consistency
    only.

    ``translation`` is an optional manifest for translation mode::

        {"pages": [{"page": 1, "blocks": [
            {"source_bbox": [x0, y0, x1, y1],
             "status": "translated" | "preserved" | "refused" | "fixed",
             "source_text": "...", "target_text": "...",
             "column_band": [x0, x1]}, ...]}]}

    Without it, translated blocks are inferred from the absence of their
    source text in the rendered page; a supplied manifest must cover every
    source block exactly once.
    """
    if mode not in MODES:
        raise ValueError(f"unknown fidelity mode {mode!r}; expected one of {MODES}")
    effective = dict(TOLERANCES)
    if mode == 'translation':
        effective.update(TRANSLATION_TOLERANCES)
    if tolerances:
        effective.update(tolerances)
    manifest = _manifest_pages(translation)
    source_doc = _open_pdf(source)
    rendered_doc = _open_pdf(rendered)
    try:
        source_pages = list(source_doc)
        rendered_pages = list(rendered_doc)
        page_count_ok = len(source_pages) == len(rendered_pages)
        failures = []
        if not page_count_ok:
            failures.append({'page': None, 'check': 'page_count',
                             'detail': f'source {len(source_pages)} != '
                                       f'rendered {len(rendered_pages)}'})
        pages = []
        for index in range(min(len(source_pages), len(rendered_pages))):
            if mode == 'translation':
                report, page_failures = _translation_page_report(
                    index + 1, source_pages[index], rendered_pages[index],
                    effective, declared_fonts,
                    None if manifest is None else manifest.get(index + 1, []))
            else:
                report, page_failures = _page_report(
                    index + 1, source_pages[index], rendered_pages[index],
                    effective, declared_fonts)
            pages.append(report)
            failures.extend(page_failures)
        report = {
            'schema': REPORT_SCHEMA,
            'mode': mode,
            'ok': not failures,
            'tolerances': effective,
            'source': {'pages': len(source_pages), 'sha256': _sha256(source)},
            'rendered': {'pages': len(rendered_pages), 'sha256': _sha256(rendered)},
            'checks': {'page_count': {'ok': page_count_ok,
                                      'source': len(source_pages),
                                      'rendered': len(rendered_pages)}},
            'pages': pages,
            'failures': failures,
        }
        return report
    finally:
        source_doc.close()
        rendered_doc.close()


def write_report(report: dict, path) -> None:
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding='utf-8')


def main(argv=None) -> int:
    """CLI entry point: 0 pass, 1 fidelity failure, 2 unusable invocation."""
    parser = argparse.ArgumentParser(
        prog='python -m lib.paper.builds.pdf_fidelity',
        description='Compare a born-digital PDF with our rendered reproduction.')
    parser.add_argument('--source', required=True, help='original born-digital PDF')
    parser.add_argument('--rendered', required=True, help='our rendered PDF')
    parser.add_argument('--fonts', help='JSON declared stack, e.g. '
                                        '\'{"latin":["Helvetica"],"cjk":["Heiti"]}\'')
    parser.add_argument('--mode', choices=MODES, default='reproduction',
                        help='reproduction (default) or translation')
    parser.add_argument('--translation',
                        help='JSON translation manifest (translation mode only)')
    parser.add_argument('--report', help='write the JSON report to this path')
    parser.add_argument('--quiet', action='store_true', help='do not print the report')
    args = parser.parse_args(argv)
    try:
        declared = json.loads(args.fonts) if args.fonts else None
        translation = (json.loads(Path(args.translation).read_text(encoding='utf-8'))
                       if args.translation else None)
        report = run_gate(args.source, args.rendered, mode=args.mode,
                          declared_fonts=declared, translation=translation)
    except Exception as exc:  # unusable input, never a fidelity verdict
        print(f'pdf fidelity gate error: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    if args.report:
        write_report(report, args.report)
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())