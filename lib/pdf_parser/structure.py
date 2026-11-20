"""Pure-CPU structural model of a born-digital PDF.

Entry point: :func:`extract_pdf_structure`.  This is the extraction layer of
the PDF translation pipeline; it lives in the existing PDF-parsing domain
because it owns no translation policy.

The extractor uses PyMuPDF's low-level ``rawdict`` text API, which returns the
actual glyph runs with their PDF-point geometry, plus ``get_image_info`` for
raster figures and ``find_tables`` for ruled tables.  It deliberately performs
no OCR and calls no model: a page without a text layer is reported as
unsupported instead of being rendered and guessed.

Output contract (all coordinates are PDF points, origin top-left, matching
PyMuPDF's page space)::

    {
      'supported': bool,             # every page has a usable text layer
      'page_count': int,
      'unsupported_pages': [{'page_number': int, 'reason': str}],
      'pages': [{
        'page_number': int,
        'width': float, 'height': float,
        'supported': bool,
        'unsupported_reason': str | None,
        'column_count': int,
        'columns': [{'index': int, 'bbox': [x0, y0, x1, y1]}],
        'blocks': [{
          'index': int,              # reading order within the page
          'kind': 'title' | 'heading' | 'paragraph' | 'list'
                  | 'caption' | 'figure' | 'table' | 'header' | 'footer',
          'bbox': [x0, y0, x1, y1],
          'column': int | None,      # None = full-width (title, wide figure)
          'text': str,
          'lines': [{
            'bbox': [x0, y0, x1, y1],
            'text': str,
            'spans': [{
              'text': str,
              'bbox': [x0, y0, x1, y1],
              'font_family': str,
              'font_name': str,
              'font_size': float,
              'font_weight': 'normal' | 'bold',
              'font_style': 'normal' | 'italic',
              'font_flags': int,
            }],
          }],
          'rows': [[str]] | None,    # tables only
        }],
        'text': str,                 # all span text in reading order
      }],
    }
"""

from __future__ import annotations

import re
import statistics
from typing import Iterable, Sequence

try:
    import pymupdf
except ImportError as exc:  # pragma: no cover - environment guard
    raise RuntimeError(
        "lib.pdf_parser.structure requires the PyMuPDF package 'pymupdf'; "
        "it is declared in requirements.txt. Refusing to install it implicitly."
    ) from exc


_CAPTION_RE = re.compile(
    r'^\s*(?:figure|fig\.?|table|scheme|algorithm)\s*'
    r'(?:[A-Z]?\.?\d+|[IVXLC]+)\b',
    re.IGNORECASE,
)
_LIST_RE = re.compile(
    r'^\s*(?:[\u2022\u00b7\u2219\u25e6\u2023\u25aa\u25ab\-*]'
    r'|\(?\d{1,3}[.)]'
    r'|[a-zA-Z][.)])\s+'
)

# A block wider than this fraction of the content width is full-width rather
# than a column participant.  The value is a layout invariant used only for
# multi-column pages: a two-column body block cannot occupy 70% of the text
# measure, while a title/wide figure/table does.
_FULL_WIDTH_FRACTION = 0.70

# Zone fractions for running heads and folios.  They are page-geometry
# conventions, not tuning knobs: a block entirely above 7.5% / below 92.5% of
# the page and set no larger than body text is a running head/folio.
_HEADER_BAND = 0.075
_FOOTER_BAND = 0.925


def extract_pdf_structure(pdf_bytes: bytes | bytearray | memoryview) -> dict:
    """Extract the ordered structural model of a born-digital PDF.

    Raises:
        ValueError: the payload is not a readable PDF or is password protected.
        RuntimeError: PyMuPDF is unavailable (see module import guard).
    """
    try:
        doc = pymupdf.open(stream=bytes(pdf_bytes), filetype='pdf')
    except Exception as exc:
        raise ValueError(f'not a readable PDF: {exc}') from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError('password-protected PDFs are not supported')
    try:
        pages = [_extract_page(doc, index) for index in range(doc.page_count)]
    finally:
        doc.close()

    unsupported = [
        {
            'page_number': page['page_number'],
            'reason': page['unsupported_reason'],
        }
        for page in pages
        if not page['supported']
    ]
    return {
        'supported': bool(pages) and not unsupported,
        'page_count': len(pages),
        'unsupported_pages': unsupported,
        'pages': pages,
    }


def _extract_page(doc, page_number: int) -> dict:
    page = doc[page_number]
    width, height = float(page.rect.width), float(page.rect.height)
    lines = _read_lines(page)

    table_blocks, table_line_ids = _read_tables(page, lines)
    body_lines = [
        line for index, line in enumerate(lines) if index not in table_line_ids
    ]
    body_median = _median_line_size(body_lines)

    header_ids, footer_ids = _running_line_ids(lines, height, body_median)
    image_blocks = _read_images(page, width, height, table_blocks)
    unsupported_reason = _scanned_reason(
        page, width, height, lines, image_blocks,
    )

    if unsupported_reason:
        return {
            'page_number': page_number,
            'width': width,
            'height': height,
            'supported': False,
            'unsupported_reason': unsupported_reason,
            'column_count': 0,
            'columns': [],
            'blocks': image_blocks,
            'text': '',
        }

    body_line_indexes = [
        index
        for index, line in enumerate(lines)
        if index not in header_ids
        and index not in footer_ids
        and index not in table_line_ids
    ]
    column_line_indexes = [
        index for index in body_line_indexes
        if lines[index]['size'] <= body_median * 1.4
    ]
    bands = _detect_column_bands(
        [lines[index] for index in column_line_indexes], width,
    )
    occupied = [
        band_index for band_index in range(len(bands))
        if any(
            _band_index(lines[index]['bbox'], bands) == band_index
            for index in column_line_indexes
        )
    ]
    bands = [bands[band_index] for band_index in occupied]
    content_left = min((lines[i]['bbox'][0] for i in column_line_indexes), default=0.0)
    content_right = max((lines[i]['bbox'][2] for i in column_line_indexes), default=width)
    content_width = max(content_right - content_left, 1.0)

    header_blocks = _group_running_lines(
        [lines[i] for i in sorted(header_ids, key=lambda i: lines[i]['bbox'][1])],
        'header', None,
    )
    footer_blocks = _group_running_lines(
        [lines[i] for i in sorted(footer_ids, key=lambda i: lines[i]['bbox'][1])],
        'footer', None,
    )

    text_blocks: list[dict] = []
    for band_index, (band_left, band_right) in enumerate(bands):
        band_lines = [
            lines[index] for index in body_line_indexes
            if _band_index(lines[index]['bbox'], bands) == band_index
        ]
        band_lines.sort(key=lambda line: (line['bbox'][1], line['bbox'][0]))
        text_blocks.extend(_group_text_lines(band_lines, band_index))
    if not bands:
        # No body lines at all should be impossible on a supported page, but
        # keep the invariant explicit rather than assigning a fake column.
        bands = []
        content_width = max(width, 1.0)

    for block in text_blocks + table_blocks:
        block['column'] = _column_for(
            block['bbox'], bands, content_width, len(bands),
        )
    for block in image_blocks:
        block['column'] = _column_for(
            block['bbox'], bands, content_width, len(bands),
        )

    all_blocks = header_blocks + text_blocks + table_blocks + image_blocks + footer_blocks
    _classify_kinds(all_blocks, page_number, body_median, height)
    ordered = _reading_order(all_blocks, len(bands))
    for index, block in enumerate(ordered):
        block['index'] = index
        block['reading_order'] = index

    columns = []
    for index, (band_left, band_right) in enumerate(bands):
        members = [b for b in ordered if b['column'] == index]
        if not members:
            continue
        columns.append({
            'index': index,
            'bbox': [
                min(b['bbox'][0] for b in members),
                min(b['bbox'][1] for b in members),
                max(b['bbox'][2] for b in members),
                max(b['bbox'][3] for b in members),
            ],
        })

    page_text = ''.join(
        span['text']
        for block in ordered
        for line in block['lines']
        for span in line['spans']
    )
    return {
        'page_number': page_number,
        'width': width,
        'height': height,
        'supported': True,
        'unsupported_reason': None,
        'column_count': len(columns),
        'columns': columns,
        'blocks': ordered,
        'text': page_text,
    }


# ── glyph runs ────────────────────────────────────────────────────────────


def _read_lines(page) -> list[dict]:
    raw = page.get_text('rawdict')
    lines: list[dict] = []
    for raw_block in raw.get('blocks', []):
        if raw_block.get('type', 0) != 0:
            continue
        for raw_line in raw_block.get('lines', []):
            spans = []
            for raw_span in raw_line.get('spans', []):
                text = ''.join(char.get('c', '') for char in raw_span.get('chars', []))
                if not text:
                    continue
                font_name = raw_span.get('font', '') or ''
                flags = int(raw_span.get('flags', 0) or 0)
                spans.append({
                    'text': text,
                    'bbox': [float(v) for v in raw_span['bbox']],
                    'font_family': _font_family(font_name),
                    'font_name': font_name,
                    'font_size': round(float(raw_span.get('size', 0.0)), 3),
                    'font_weight': _font_weight(font_name, flags),
                    'font_style': _font_style(font_name, flags),
                    'font_flags': flags,
                })
            if not spans:
                continue
            bbox = _union_bbox(span['bbox'] for span in spans)
            lines.append({
                'bbox': bbox,
                'text': ''.join(span['text'] for span in spans),
                'spans': spans,
                'size': max(span['font_size'] for span in spans),
                'family': max(
                    (span['font_family'] for span in spans),
                    key=lambda name: sum(
                        len(s['text']) for s in spans if s['font_family'] == name
                    ),
                ),
                'bold': any(span['font_weight'] == 'bold' for span in spans),
                'italic': any(span['font_style'] == 'italic' for span in spans),
            })
    return lines


def _font_family(font_name: str) -> str:
    name = re.sub(r'^[A-Z]{6}\+', '', font_name or '')
    for marker in ('-', ','):
        if marker in name:
            name = name.split(marker, 1)[0]
    return name or 'unknown'


def _font_weight(font_name: str, flags: int) -> str:
    lowered = (font_name or '').lower()
    if flags & 16 or any(token in lowered for token in (
        'bold', 'black', 'heavy', 'semibold', 'demibold',
    )):
        return 'bold'
    return 'normal'


def _font_style(font_name: str, flags: int) -> str:
    lowered = (font_name or '').lower()
    if flags & 2 or 'italic' in lowered or 'oblique' in lowered:
        return 'italic'
    return 'normal'


def _median_line_size(lines: Sequence[dict]) -> float:
    sizes = [line['size'] for line in lines if line['size'] > 0]
    return float(statistics.median(sizes)) if sizes else 12.0


def _union_bbox(boxes: Iterable[Sequence[float]]) -> list[float]:
    boxes = list(boxes)
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


# ── running heads / folios ────────────────────────────────────────────────


def _running_line_ids(lines: Sequence[dict], height: float,
                      body_median: float) -> tuple[set[int], set[int]]:
    header: set[int] = set()
    footer: set[int] = set()
    for index, line in enumerate(lines):
        size = line['size']
        if size > body_median * 1.1:
            continue
        if line['bbox'][3] <= height * _HEADER_BAND:
            header.add(index)
        elif line['bbox'][1] >= height * _FOOTER_BAND:
            footer.add(index)
    return header, footer


def _group_running_lines(lines: Sequence[dict], kind: str,
                         column: int | None) -> list[dict]:
    if not lines:
        return []
    blocks = []
    current = [lines[0]]
    for line in lines[1:]:
        previous = current[-1]
        gap = line['bbox'][1] - previous['bbox'][3]
        if gap <= max(4.0, 0.6 * max(previous['size'], line['size'])):
            current.append(line)
        else:
            blocks.append(_text_block(current, kind, column))
            current = [line]
    blocks.append(_text_block(current, kind, column))
    return blocks


# ── tables and figures ────────────────────────────────────────────────────


def _read_tables(page, lines: Sequence[dict]) -> tuple[list[dict], set[int]]:
    blocks: list[dict] = []
    table_lines: set[int] = set()
    try:
        finder = page.find_tables()
    except Exception:
        return blocks, table_lines
    for table in finder.tables:
        bbox = [float(v) for v in table.bbox]
        members = []
        for index, line in enumerate(lines):
            cx = (line['bbox'][0] + line['bbox'][2]) / 2
            cy = (line['bbox'][1] + line['bbox'][3]) / 2
            if (bbox[0] - 1 <= cx <= bbox[2] + 1
                    and bbox[1] - 1 <= cy <= bbox[3] + 1):
                members.append((index, line))
        if not members:
            continue
        for index, _line in members:
            table_lines.add(index)
        ordered = [line for _index, line in _row_major(members)]
        rows = _table_cell_rows(table, lines)
        blocks.append({
            'bbox': bbox,
            'kind': 'table',
            'column': None,
            'text': '\n'.join(line['text'] for line in ordered),
            'lines': [_line_payload(line) for line in ordered],
            'rows': rows,
        })
    return blocks, table_lines


def _table_cell_rows(table, lines: Sequence[dict]) -> list[list[str | None]]:
    """Cell text from the drawn glyph runs, not ``Table.extract()``.

    ``Table.extract()`` reads through MuPDF's process-global quad-correction
    state.  Importing ``pymupdf4llm`` at its lazy markdown call site flips
    that state and reorders characters inside a cell -- a drawn ``91.2``
    comes back as ``912\n.`` -- so the same PDF yields different rows
    depending on import order.  Assigning the already-extracted ``rawdict``
    lines to the detected cell rectangles keeps cell text tied to what is
    actually drawn, and keeps ``rows`` consistent with the block's ``text``.
    """
    rows: list[list[str | None]] = []
    for row in table.rows:
        cells: list[str | None] = []
        for cell in row.cells:
            if cell is None:
                cells.append(None)
                continue
            x0, y0, x1, y1 = (float(v) for v in cell)
            members = []
            for index, line in enumerate(lines):
                cx = (line['bbox'][0] + line['bbox'][2]) / 2
                cy = (line['bbox'][1] + line['bbox'][3]) / 2
                if x0 <= cx < x1 and y0 <= cy < y1:
                    members.append((index, line))
            ordered = [line for _index, line in _row_major(members)]
            cells.append('\n'.join(line['text'] for line in ordered))
        rows.append(cells)
    return rows


def _row_major(members: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
    """Sort table lines row-major, clustering slightly uneven baselines."""
    if not members:
        return []
    ordered = sorted(members, key=lambda item: (item[1]['bbox'][1], item[1]['bbox'][0]))
    rows: list[list[tuple[int, dict]]] = [[ordered[0]]]
    for item in ordered[1:]:
        if abs(item[1]['bbox'][1] - rows[-1][0][1]['bbox'][1]) <= 3.0:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda m: m[1]['bbox'][0])]


def _read_images(page, width: float, height: float,
                 table_blocks: Sequence[dict]) -> list[dict]:
    page_area = max(width * height, 1.0)
    seen: set[tuple[int, ...]] = set()
    figures: list[dict] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return figures
    for info in infos:
        bbox = [float(v) for v in info.get('bbox', (0, 0, 0, 0))]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        key = tuple(round(v, 1) for v in bbox)
        if key in seen:
            continue
        seen.add(key)
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area < page_area * 0.01:
            continue
        if any(_intersects(bbox, table['bbox']) for table in table_blocks):
            continue
        figures.append({
            'bbox': bbox,
            'kind': 'figure',
            'column': None,
            'text': '',
            'lines': [],
            'rows': None,
        })
    return figures


def _intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _scanned_reason(page, width: float, height: float, lines: Sequence[dict],
                    figures: Sequence[dict]) -> str | None:
    if lines:
        return None
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    page_area = max(width * height, 1.0)
    image_area = sum(
        max(0.0, (float(i['bbox'][2]) - float(i['bbox'][0])))
        * max(0.0, (float(i['bbox'][3]) - float(i['bbox'][1])))
        for i in infos
        if i.get('bbox')
    )
    if infos and image_area >= page_area * 0.25:
        return 'scanned_page_unsupported'
    return 'no_text_layer_unsupported'


# ── columns ───────────────────────────────────────────────────────────────


def _detect_column_bands(lines: Sequence[dict], page_width: float) -> list[tuple[float, float]]:
    """Find vertical gutters that no body line crosses.

    Returns column x-ranges.  A gutter must be interior to the text measure and
    wide enough to be a real layout gutter rather than inter-word spacing.
    """
    if not lines:
        return [(0.0, page_width)]
    left = min(line['bbox'][0] for line in lines)
    right = max(line['bbox'][2] for line in lines)
    if right - left < page_width * 0.15:
        return [(left, right)]

    intervals = [
        (line['bbox'][0] - 1.0, line['bbox'][2] + 1.0) for line in lines
    ]
    scan_left = int(left)
    scan_right = int(right) + 1
    uncovered: list[tuple[int, int]] = []
    run_start: int | None = None
    for x in range(scan_left, scan_right + 1):
        covered = any(start <= x <= end for start, end in intervals)
        if not covered and run_start is None:
            run_start = x
        elif covered and run_start is not None:
            uncovered.append((run_start, x - 1))
            run_start = None
    if run_start is not None:
        uncovered.append((run_start, scan_right))

    minimum_gutter = max(10.0, page_width * 0.015)
    gutters: list[float] = []
    for start, end in uncovered:
        if end - start + 1 < minimum_gutter:
            continue
        midpoint = (start + end) / 2
        if not (page_width * 0.20 <= midpoint <= page_width * 0.80):
            continue
        gutters.append(midpoint)
    if not gutters:
        return [(left, right)]

    boundaries = [left]
    for gutter in gutters:
        if gutter - boundaries[-1] >= page_width * 0.05:
            boundaries.append(gutter)
    boundaries.append(right)
    bands = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]
    return bands


def _band_index(bbox: Sequence[float],
                bands: Sequence[tuple[float, float]]) -> int | None:
    center = (bbox[0] + bbox[2]) / 2
    for index, (left, right) in enumerate(bands):
        if left <= center <= right:
            return index
    if not bands:
        return None
    return min(
        range(len(bands)),
        key=lambda i: abs(center - (bands[i][0] + bands[i][1]) / 2),
    )


def _column_for(bbox: Sequence[float], bands: Sequence[tuple[float, float]],
                content_width: float, column_count: int) -> int | None:
    if column_count <= 1:
        return 0 if column_count else None
    width = bbox[2] - bbox[0]
    if width >= content_width * _FULL_WIDTH_FRACTION:
        return None
    for index, (left, right) in enumerate(bands):
        if bbox[0] < right and bbox[2] > left:
            # A block that straddles a gutter is full-width, not a column item.
            center = (bbox[0] + bbox[2]) / 2
            if left <= center <= right:
                return index
    return _band_index(bbox, bands)


# ── block construction and classification ─────────────────────────────────


def _group_text_lines(lines: Sequence[dict], column: int) -> list[dict]:
    if not lines:
        return []
    blocks: list[dict] = []
    current = [lines[0]]
    for line in lines[1:]:
        if _same_block(current[-1], line):
            current.append(line)
        else:
            blocks.append(_text_block(current, 'paragraph', column))
            current = [line]
    blocks.append(_text_block(current, 'paragraph', column))
    return blocks


def _same_block(previous: dict, line: dict) -> bool:
    gap = line['bbox'][1] - previous['bbox'][3]
    if gap < -2.0:
        return False
    if gap > max(6.0, 0.8 * max(previous['size'], line['size'])):
        return False
    if abs(previous['size'] - line['size']) > 0.5:
        return False
    if previous['family'] != line['family']:
        return False
    return True


def _text_block(lines: Sequence[dict], kind: str,
                column: int | None) -> dict:
    return {
        'bbox': _union_bbox(line['bbox'] for line in lines),
        'kind': kind,
        'column': column,
        'text': '\n'.join(line['text'] for line in lines),
        'lines': [_line_payload(line) for line in lines],
        'rows': None,
    }


def _line_payload(line: dict) -> dict:
    return {
        'bbox': line['bbox'],
        'text': line['text'],
        'spans': [
            {
                'text': span['text'],
                'bbox': span['bbox'],
                'font_family': span['font_family'],
                'font_name': span['font_name'],
                'font_size': span['font_size'],
                'font_weight': span['font_weight'],
                'font_style': span['font_style'],
                'font_flags': span['font_flags'],
            }
            for span in line['spans']
        ],
    }


def _classify_kinds(blocks: list[dict], page_number: int,
                    body_median: float, page_height: float) -> None:
    text_blocks = [b for b in blocks if b['lines']]
    title: dict | None = None
    if page_number == 0:
        candidates = [
            block for block in text_blocks
            if block['kind'] not in ('header', 'footer')
            and _max_size(block) >= max(14.0, body_median * 1.4)
            and block['bbox'][1] < page_height * 0.4
        ]
        if candidates:
            title = max(candidates, key=lambda b: (_max_size(b), -b['bbox'][1]))

    for block in text_blocks:
        if block['kind'] in ('header', 'footer', 'table', 'figure'):
            continue
        text = block['text'].strip()
        first_line = block['lines'][0]['text'].strip()
        if _CAPTION_RE.match(text):
            block['kind'] = 'caption'
        elif block is title:
            block['kind'] = 'title'
        elif _max_size(block) >= body_median * 1.12 and len(text) <= 200:
            block['kind'] = 'heading'
        elif _LIST_RE.match(first_line):
            block['kind'] = 'list'
        else:
            block['kind'] = 'paragraph'


def _max_size(block: dict) -> float:
    return max(
        (span['font_size'] for line in block['lines'] for span in line['spans']),
        default=0.0,
    )


# ── reading order ─────────────────────────────────────────────────────────


def _reading_order(blocks: Sequence[dict], column_count: int) -> list[dict]:
    headers = sorted(
        (b for b in blocks if b['kind'] == 'header'),
        key=lambda b: (b['bbox'][1], b['bbox'][0]),
    )
    footers = sorted(
        (b for b in blocks if b['kind'] == 'footer'),
        key=lambda b: (b['bbox'][1], b['bbox'][0]),
    )
    body = [
        b for b in blocks
        if b['kind'] not in ('header', 'footer')
    ]
    if column_count <= 1:
        ordered_body = sorted(body, key=lambda b: (b['bbox'][1], b['bbox'][0]))
        return headers + ordered_body + footers

    full_width = sorted(
        (b for b in body if b['column'] is None),
        key=lambda b: (b['bbox'][1], b['bbox'][0]),
    )
    columns: dict[int, list[dict]] = {index: [] for index in range(column_count)}
    for block in body:
        if block['column'] is not None:
            columns.setdefault(block['column'], []).append(block)
    for members in columns.values():
        members.sort(key=lambda b: (b['bbox'][1], b['bbox'][0]))

    result: list[dict] = []
    emitted: set[int] = set()

    def emit(block: dict) -> None:
        key = id(block)
        if key not in emitted:
            result.append(block)
            emitted.add(key)

    for separator in full_width:
        for index in range(column_count):
            for block in columns.get(index, []):
                if block['bbox'][1] < separator['bbox'][1] - 1.0:
                    emit(block)
        emit(separator)
    for index in range(column_count):
        for block in columns.get(index, []):
            emit(block)
    return headers + result + footers