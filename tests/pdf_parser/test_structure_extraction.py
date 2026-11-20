"""Executable specification for the pure-CPU PDF structure extractor.

Every fixture is generated here with PyMuPDF itself.  No real user PDFs, no
network, no OCR and no model are involved.
"""

from __future__ import annotations

import pymupdf
import pytest

from lib.pdf_parser.structure import extract_pdf_structure

pytestmark = pytest.mark.unit


# ── fixture generation ────────────────────────────────────────────────────


def _single_column_pdf() -> tuple[bytes, str]:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    pieces = {
        'header': 'Journal of Synthetic Fixtures 2026',
        'title': 'Structural Extraction of Born-Digital PDFs',
        'heading': '1. Introduction',
        'p1': 'This paper studies a pure-CPU extractor.',
        'p2': 'Every glyph run is preserved exactly.',
        'list': '- First list item\n- Second list item',
        'footer': '1',
    }
    page.insert_text((72, 30), pieces['header'], fontsize=9, fontname='helv')
    page.insert_text((72, 90), pieces['title'], fontsize=24, fontname='hebo')
    page.insert_text((72, 140), pieces['heading'], fontsize=16, fontname='hebo')
    page.insert_text((72, 175), pieces['p1'], fontsize=12, fontname='helv')
    page.insert_text((72, 195), pieces['p2'], fontsize=12, fontname='helv')
    x = 72.0
    for text, font in (
        ('Normal text ', 'helv'),
        ('bold span ', 'hebo'),
        ('italic tail', 'heit'),
    ):
        page.insert_text((x, 225), text, fontsize=12, fontname=font)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=12)
    page.insert_text((72, 255), '- First list item', fontsize=12, fontname='helv')
    page.insert_text((72, 275), '- Second list item', fontsize=12, fontname='helv')
    page.insert_text((300, 760), pieces['footer'], fontsize=9, fontname='helv')
    payload = doc.tobytes()
    doc.close()
    expected = ''.join([
        pieces['header'],
        pieces['title'],
        pieces['heading'],
        pieces['p1'],
        pieces['p2'],
        'Normal text bold span italic tail',
        '- First list item',
        '- Second list item',
        pieces['footer'],
    ])
    return payload, expected


def _two_column_pdf() -> tuple[bytes, str]:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    header = 'Two Column Fixture'
    title = 'A Two-Column Structural Extraction Layout'
    left = [
        'Left column first line.',
        'Left column second line.',
        'Left column third line.',
        'Left column fourth line.',
    ]
    right = [
        'Right column first line.',
        'Right column second line.',
        'Right column third line.',
        'Right column fourth line.',
    ]
    page.insert_text((72, 30), header, fontsize=9, fontname='helv')
    page.insert_text((54, 80), title, fontsize=22, fontname='hebo')
    # Interleave insertion order so a raw glyph order would be wrong.
    for index in range(4):
        page.insert_text((54, 140 + 20 * index), left[index], fontsize=12, fontname='helv')
        page.insert_text((330, 140 + 20 * index), right[index], fontsize=12, fontname='helv')
    page.insert_text((300, 760), '2', fontsize=9, fontname='helv')
    payload = doc.tobytes()
    doc.close()
    expected = header + title + ''.join(left) + ''.join(right) + '2'
    return payload, expected


def _figure_caption_pdf() -> tuple[bytes, str]:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    intro = 'Before the figure.'
    caption = 'Figure 1: Synthetic figure caption.'
    page.insert_text((72, 80), intro, fontsize=12, fontname='helv')
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
    pixmap.set_rect(pixmap.irect, (40, 90, 160))
    page.insert_image((72, 120, 372, 320), stream=pixmap.tobytes('png'))
    page.insert_text((72, 340), caption, fontsize=10, fontname='heit')
    payload = doc.tobytes()
    doc.close()
    return payload, intro + caption


def _table_pdf() -> tuple[bytes, list[list[str]]]:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    intro = 'The results are summarized below.'
    after = 'The extractor preserves table cells.'
    x_positions = [72, 200, 360, 500]
    y_positions = [120, 150, 180, 210]
    for y in y_positions:
        page.draw_line((x_positions[0], y), (x_positions[-1], y), color=(0, 0, 0), width=1)
    for x in x_positions:
        page.draw_line((x, y_positions[0]), (x, y_positions[-1]), color=(0, 0, 0), width=1)
    rows = [
        ['Model', 'Accuracy', 'Latency'],
        ['Baseline', '91.2', '45 ms'],
        ['Ours', '94.8', '39 ms'],
    ]
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            page.insert_text(
                (x_positions[column_index] + 4, y_positions[row_index] + 18),
                text, fontsize=10, fontname='helv',
            )
    page.insert_text((72, 80), intro, fontsize=12, fontname='helv')
    page.insert_text((72, 240), after, fontsize=12, fontname='helv')
    payload = doc.tobytes()
    doc.close()
    return payload, rows


def _scanned_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
    pixmap.set_rect(pixmap.irect, (40, 90, 160))
    page.insert_image((36, 36, 576, 756), stream=pixmap.tobytes('png'))
    payload = doc.tobytes()
    doc.close()
    return payload


# ── assertions ────────────────────────────────────────────────────────────


def _assert_bbox(actual: list[float], expected: list[float], tolerance: float = 1.0) -> None:
    assert actual == pytest.approx(expected, abs=tolerance)


def _assert_text_complete(result: dict, expected: str) -> None:
    actual = ''.join(page['text'] for page in result['pages'])
    assert actual == expected


def _block(result: dict, kind: str) -> dict:
    matches = [block for block in result['pages'][0]['blocks'] if block['kind'] == kind]
    assert len(matches) == 1, f'expected exactly one {kind} block, got {len(matches)}'
    return matches[0]


# ── tests ─────────────────────────────────────────────────────────────────


def test_single_column_blocks_fonts_and_reading_order():
    payload, expected_text = _single_column_pdf()
    result = extract_pdf_structure(payload)
    page = result['pages'][0]

    assert result['supported'] is True
    assert page['column_count'] == 1
    assert [block['kind'] for block in page['blocks']] == [
        'header', 'title', 'heading', 'paragraph', 'paragraph', 'list', 'footer',
    ]
    assert [block['index'] for block in page['blocks']] == list(range(7))

    header = _block(result, 'header')
    title = _block(result, 'title')
    heading = _block(result, 'heading')
    footer = _block(result, 'footer')

    assert header['bbox'][0] == pytest.approx(72.0, abs=0.5)
    assert header['bbox'][3] < 40.0
    assert title['bbox'][0] == pytest.approx(72.0, abs=0.5)
    assert title['bbox'][1] == pytest.approx(64.3, abs=1.0)
    assert heading['bbox'][1] == pytest.approx(122.9, abs=1.0)
    assert footer['bbox'][1] > 740.0

    title_span = title['lines'][0]['spans'][0]
    assert title_span['font_family'] == 'Helvetica'
    assert title_span['font_size'] == 24.0
    assert title_span['font_weight'] == 'bold'
    assert title_span['font_style'] == 'normal'

    mixed = [block for block in page['blocks'] if block['text'] == 'Normal text bold span italic tail']
    assert len(mixed) == 1
    spans = mixed[0]['lines'][0]['spans']
    assert [span['text'] for span in spans] == ['Normal text ', 'bold span ', 'italic tail']
    assert spans[0]['font_weight'] == 'normal'
    assert spans[1]['font_weight'] == 'bold'
    assert spans[2]['font_style'] == 'italic'

    list_block = _block(result, 'list')
    assert list_block['text'] == '- First list item\n- Second list item'

    _assert_text_complete(result, expected_text)


def test_two_column_layout_columns_and_reading_order():
    payload, expected_text = _two_column_pdf()
    result = extract_pdf_structure(payload)
    page = result['pages'][0]

    assert page['column_count'] == 2
    assert [column['index'] for column in page['columns']] == [0, 1]

    title = _block(result, 'title')
    assert title['column'] is None

    left_blocks = [
        block for block in page['blocks']
        if block['column'] == 0 and block['kind'] == 'paragraph'
    ]
    right_blocks = [
        block for block in page['blocks']
        if block['column'] == 1 and block['kind'] == 'paragraph'
    ]
    assert len(left_blocks) == 1
    assert len(right_blocks) == 1

    left = left_blocks[0]
    right = right_blocks[0]
    assert left['text'].startswith('Left column first line.')
    assert right['text'].startswith('Right column first line.')
    assert left['bbox'][0] == pytest.approx(54.0, abs=1.0)
    assert right['bbox'][0] == pytest.approx(330.0, abs=1.0)

    # Reading order must be title, complete left column, complete right column.
    assert title['index'] < left['index'] < right['index']
    assert page['text'] == (
        'Two Column Fixture'
        + 'A Two-Column Structural Extraction Layout'
        + 'Left column first line.Left column second line.'
        + 'Left column third line.Left column fourth line.'
        + 'Right column first line.Right column second line.'
        + 'Right column third line.Right column fourth line.'
        + '2'
    )

    _assert_text_complete(result, expected_text)


def test_figure_and_caption_geometry_and_order():
    payload, expected_text = _figure_caption_pdf()
    result = extract_pdf_structure(payload)
    page = result['pages'][0]

    assert page['column_count'] == 1
    figure = _block(result, 'figure')
    caption = _block(result, 'caption')

    _assert_bbox(figure['bbox'], [72.0, 120.0, 372.0, 320.0])
    assert figure['text'] == ''
    assert figure['lines'] == []

    cap_span = caption['lines'][0]['spans'][0]
    assert cap_span['font_family'] == 'Helvetica'
    assert cap_span['font_size'] == 10.0
    assert cap_span['font_style'] == 'italic'
    assert caption['bbox'][0] == pytest.approx(72.0, abs=0.5)
    assert caption['bbox'][1] == pytest.approx(329.3, abs=1.0)

    assert figure['index'] < caption['index']
    _assert_text_complete(result, expected_text)


def test_table_block_extracts_cells_and_geometry():
    payload, rows = _table_pdf()
    result = extract_pdf_structure(payload)
    page = result['pages'][0]

    assert page['column_count'] == 1
    assert [block['kind'] for block in page['blocks']] == ['paragraph', 'table', 'paragraph']

    table = _block(result, 'table')
    _assert_bbox(table['bbox'], [72.0, 120.0, 500.0, 210.0])
    assert table['rows'] == rows
    assert table['text'] == (
        'Model\nAccuracy\nLatency\nBaseline\n91.2\n45 ms\nOurs\n94.8\n39 ms'
    )
    assert all(
        span['font_size'] == 10.0
        for line in table['lines']
        for span in line['spans']
    )

    _assert_text_complete(
        result,
        'The results are summarized below.'
        + 'ModelAccuracyLatencyBaseline91.245 msOurs94.839 ms'
        + 'The extractor preserves table cells.',
    )


def test_scanned_page_is_reported_unsupported_not_silently_empty():
    result = extract_pdf_structure(_scanned_pdf())
    page = result['pages'][0]

    assert result['supported'] is False
    assert result['unsupported_pages'] == [{
        'page_number': 0,
        'reason': 'scanned_page_unsupported',
    }]
    assert page['supported'] is False
    assert page['unsupported_reason'] == 'scanned_page_unsupported'
    assert page['text'] == ''
    assert [block['kind'] for block in page['blocks']] == ['figure']