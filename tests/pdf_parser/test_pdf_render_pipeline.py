"""End-to-end specification for extract → reflow → render → fidelity gate.

Fixtures are synthetic born-digital pages generated here with PyMuPDF; no
real user PDF, model, network or OCR is involved.

The gate (`lib.paper.builds.pdf_fidelity`) is a *reproduction* gate: its
`text_presence` check requires the source wording to survive.  An identity
translation therefore passes every check.  A genuinely longer translation
changes wording by definition, so that run asserts the checks the gate can
still make for accepted blocks — page/column geometry, exact figure/table
boxes, reserved regions and the declared font stack — plus the plan-level
baselines and text, instead of pretending `text_presence` can pass.
"""

from __future__ import annotations

import json

import pymupdf
import pytest

from lib.paper.builds.pdf_fidelity import run_gate, write_report
from lib.pdf_parser.layout import page_layout_from_structure
from lib.pdf_parser.render import (
    RenderFontError,
    render_page_plan,
)
from lib.pdf_parser.structure import extract_pdf_structure
from lib.pdf_relayout import solve_page
from lib.pdf_relayout.fonts import FontLibrary

pytestmark = pytest.mark.unit

DECLARED_FONTS = {'latin': ['DejaVu Sans'], 'cjk': ['Noto Sans SC']}

#: Checks that must stay green for a longer translation: none of them depends
#: on the source wording, only on geometry, fixed content and the font stack.
STRUCTURAL_CHECKS = {
    'page_size', 'columns', 'figure_table_bbox_exact', 'reserved_regions',
    'font_family',
}

_FONTS = FontLibrary()


def _latin_face():
    return _FONTS.face_for_char('M', 'sans', 'regular')


def _cjk_face():
    return _FONTS.face_for_char('中', 'cjk', 'regular')


def _write_lines(page, lines, x, y, size, leading, face=None):
    face = face or _latin_face()
    font = pymupdf.Font(fontfile=face.path) if hasattr(face, 'path') else face
    writer = pymupdf.TextWriter(page.rect)
    for line in lines:
        writer.append((x, y), line, font=font, fontsize=size)
        y += leading
    writer.write_text(page)


def _figure(page, rect):
    page.draw_rect(pymupdf.Rect(*rect), color=(0, 0, 0), width=0.8)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10))
    pixmap.set_rect(pixmap.irect, (30, 90, 160))
    page.insert_image(pymupdf.Rect(*rect), pixmap=pixmap)


def _single_column_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Synthetic Single Column Document'], 72, 80, 16, 20)
    _write_lines(page, [
        'The quick brown fox jumps over the lazy dog every morning.',
        'A second line follows with ordinary words and punctuation.',
        'A third line keeps the paragraph block connected and stable.',
        'A fourth line adds enough width for a useful column measure.',
        'A fifth line remains comfortably inside the text measure.',
        'A sixth line closes the source paragraph in this fixture.',
    ], 72, 140, 10, 12)
    _figure(page, (72, 240, 300, 360))
    _write_lines(page, ['Figure 1. A synthetic vector and raster figure.'],
                 72, 378, 9, 11)
    payload = document.tobytes()
    document.close()
    return payload


def _two_column_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['Two Column Synthetic Layout'], 50, 70, 15, 18)
    _write_lines(page, [
        'Left column alpha line one stays short.',
        'Left column beta line two stays short.',
        'Left column gamma line three stays short.',
        'Left column delta line four stays short.',
        'Left column epsilon line five stays short.',
    ], 50, 120, 10, 12)
    _write_lines(page, [
        'Right column alpha line one is short.',
        'Right column beta line two is short.',
        'Right column gamma line three is short.',
        'Right column delta line four is short.',
    ], 312, 120, 10, 12)
    _figure(page, (312, 210, 500, 330))
    _write_lines(page, ['Figure 2. Right column figure.'], 312, 348, 9, 11)
    payload = document.tobytes()
    document.close()
    return payload


def _table_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Table Bearing Synthetic Page'], 72, 80, 16, 20)
    _write_lines(page, [
        'The body paragraph sits above the ruled table in this fixture.',
        'It has two ordinary lines of source language text.',
    ], 72, 130, 10, 12)
    x0, y0, x1, y1 = 72, 180, 380, 320
    for index in range(4):
        page.draw_line((x0, y0 + index * (y1 - y0) / 3),
                       (x1, y0 + index * (y1 - y0) / 3),
                       color=(0, 0, 0), width=0.8)
        page.draw_line((x0 + index * (x1 - x0) / 3, y0),
                       (x0 + index * (x1 - x0) / 3, y1),
                       color=(0, 0, 0), width=0.8)
    for row in range(3):
        for column in range(3):
            page.insert_text(
                (x0 + column * (x1 - x0) / 3 + 8,
                 y0 + row * (y1 - y0) / 3 + 18),
                f'Cell {row}{column}', fontsize=9, fontname='helv')
    _write_lines(page, ['Table 1. A ruled synthetic table.'], 72, 338, 9, 11)
    payload = document.tobytes()
    document.close()
    return payload


def _refusal_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, [
        'This short source paragraph sits immediately above a figure.',
        'Only a tiny band of free space remains before the figure.',
    ], 72, 110, 10, 12)
    _figure(page, (72, 132, 400, 360))
    payload = document.tobytes()
    document.close()
    return payload


def _text_block(adapted, *, longest: bool = True):
    flowable = [block for block in adapted.layout.blocks if not block.is_fixed]
    assert flowable
    if longest:
        return max(flowable, key=lambda block: len(block.source_text))
    return flowable[0]


def _grow(text: str, ratio: float = 1.30) -> str:
    """Return ``text`` repeated to exactly ``ratio`` of its length."""
    base = ' '.join(text.split())
    target = max(len(base) + 1, int(round(len(base) * ratio)))
    grown = base
    while len(grown) < target:
        grown += ' ' + base
    grown = grown[:target]
    if ' ' in grown:
        grown = grown[:grown.rfind(' ')]
    assert len(grown) >= target - max(len(base), 1)
    return grown


def _pipeline(source: bytes, translations, tmp_path):
    structure = extract_pdf_structure(source)
    assert structure['supported']
    adapted = page_layout_from_structure(structure['pages'][0])
    plan = solve_page(adapted.layout, translations)
    rendered_path = tmp_path / 'rendered.pdf'
    report = render_page_plan(source, plan, rendered_path)
    assert rendered_path.read_bytes() == report.pdf_bytes
    gate = run_gate(source, report.pdf_bytes, declared_fonts=DECLARED_FONTS)
    report_path = tmp_path / 'pdf-fidelity-report.json'
    write_report(gate, report_path)
    assert json.loads(report_path.read_text(encoding='utf-8'))['schema'] == gate['schema']
    return structure, adapted, plan, report, gate, report_path


def _identity(adapted) -> dict[str, str]:
    return {
        block.block_id: block.source_text
        for block in adapted.layout.blocks if not block.is_fixed
    }


def _structural_failures(gate) -> list[dict]:
    return [failure for failure in gate['failures']
            if failure['check'] in STRUCTURAL_CHECKS]


def _rendered_lines(report) -> list[str]:
    document = pymupdf.open(stream=report.pdf_bytes, filetype='pdf')
    try:
        lines = []
        for block in document[0].get_text('dict')['blocks']:
            if block.get('type') == 1:
                continue
            for line in block.get('lines', []):
                text = ''.join(span['text'] for span in line['spans'])
                if text.strip():
                    lines.append(text)
        return lines
    finally:
        document.close()


def _rendered_line_baselines(report) -> list[tuple[str, float]]:
    document = pymupdf.open(stream=report.pdf_bytes, filetype='pdf')
    try:
        rows = []
        for block in document[0].get_text('rawdict')['blocks']:
            if block.get('type') == 1:
                continue
            for line in block.get('lines', []):
                chars = [char for span in line.get('spans', [])
                         for char in span.get('chars', [])]
                text = ''.join(char.get('c', '') for char in chars)
                if not text.strip() or not chars:
                    continue
                rows.append((text, float(chars[0]['origin'][1])))
        return rows
    finally:
        document.close()


def _assert_exact_baselines(plan, report) -> None:
    rendered = _rendered_line_baselines(report)
    cursor = 0
    for placement in plan.placements:
        for line in placement.lines:
            found = next((index for index in range(cursor, len(rendered))
                          if rendered[index][0] == line.text), None)
            assert found is not None, (
                f'plan line not found in rendered text: {line.text!r}')
            assert abs(rendered[found][1] - line.baseline_y) <= 0.5, (
                line.text, rendered[found][1], line.baseline_y)
            cursor = found + 1


def _assert_embedded_fonts(report) -> None:
    document = pymupdf.open(stream=report.pdf_bytes, filetype='pdf')
    try:
        fonts = document[0].get_fonts()
        assert fonts
        for xref, extension, *_rest in fonts:
            assert xref > 0 and extension != 'n/a', fonts
    finally:
        document.close()


def _assert_plan_geometry(plan) -> None:
    columns = {column.column_id: column for column in plan.columns}
    fixed = [placement for placement in plan.placements
             if placement.preserved_geometry]
    for placement in plan.placements:
        if placement.preserved_geometry:
            continue
        column = columns[placement.column]
        assert column.bbox.contains(placement.bbox, tolerance=1e-3)
        for line in placement.lines:
            assert line.bbox.x0 >= column.bbox.x0 - 1e-6
            assert line.bbox.x1 <= column.bbox.x1 + 1e-6
        for obstacle in fixed:
            assert not placement.bbox.intersects(obstacle.bbox)


# ── identity ──────────────────────────────────────────────────────────────


def test_identity_single_column_passes_the_whole_fidelity_gate(tmp_path):
    source = _single_column_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    _, _, plan, report, gate, report_path = _pipeline(
        source, _identity(adapted), tmp_path)

    assert plan.accepted, plan.refusals
    assert report.accepted
    assert report.refusals == ()
    assert gate['ok'] is True, gate['failures']
    assert gate['pages'][0]['checks']['columns']['source'] == 1
    assert gate['pages'][0]['checks']['figure_table_bbox_exact']['ok'] is True
    assert json.loads(report_path.read_text())['ok'] is True
    _assert_exact_baselines(plan, report)
    _assert_embedded_fonts(report)


def test_identity_two_column_page_keeps_both_columns(tmp_path):
    source = _two_column_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    _, _, plan, report, gate, report_path = _pipeline(
        source, _identity(adapted), tmp_path)

    assert len(plan.columns) == 2
    assert plan.accepted, plan.refusals
    assert gate['ok'] is True, gate['failures']
    assert gate['pages'][0]['checks']['columns']['source'] == 2
    _assert_plan_geometry(plan)
    assert report_path.exists()


# ── 30% longer translation ────────────────────────────────────────────────


def test_longer_single_column_renders_every_accepted_block(tmp_path):
    source = _single_column_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    target = _text_block(adapted)
    translations = _identity(adapted)
    translations[target.block_id] = _grow(target.source_text)
    assert len(translations[target.block_id]) >= len(target.source_text) * 1.25

    plan = solve_page(adapted.layout, translations)
    report = render_page_plan(source, plan, tmp_path / 'rendered.pdf')
    gate = run_gate(source, report.pdf_bytes, declared_fonts=DECLARED_FONTS)
    report_path = tmp_path / 'pdf-fidelity-report.json'
    write_report(gate, report_path)

    assert plan.accepted, plan.refusals
    placement = plan.placement(target.block_id)
    assert placement is not None
    assert len(placement.lines) > 1
    _assert_plan_geometry(plan)
    assert _structural_failures(gate) == [], gate['failures']

    # The only wording-driven failures are the translated block itself.
    failing_checks = {failure['check'] for failure in gate['failures']}
    assert failing_checks <= {'block_bbox_iou', 'text_presence', 'reading_order'}
    missing = gate['pages'][0]['checks']['text_presence']['missing']
    assert len(missing) == 1
    assert missing[0]['text'] == ''.join(target.source_text.split('\n'))[:120]
    _assert_exact_baselines(plan, report)
    _assert_embedded_fonts(report)
    rendered = _rendered_lines(report)
    for line in placement.lines:
        assert line.text in rendered
    assert report_path.exists()


def test_identity_table_rulings_and_cells_are_copied_unchanged(tmp_path):
    source = _table_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    assert any(block.kind == 'table' for block in adapted.layout.blocks)
    _, _, plan, report, gate, report_path = _pipeline(
        source, _identity(adapted), tmp_path)

    assert plan.accepted, plan.refusals
    assert gate['ok'] is True, gate['failures']
    assert gate['pages'][0]['checks']['figure_table_bbox_exact']['ok'] is True
    rendered_text = '\n'.join(_rendered_lines(report))
    assert 'Cell 11' in rendered_text
    assert report_path.exists()


def test_longer_two_column_translation_does_not_bleed_or_move_fixed_content(tmp_path):
    source = _two_column_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    target = _text_block(adapted)
    translations = _identity(adapted)
    translations[target.block_id] = _grow(target.source_text)

    plan = solve_page(adapted.layout, translations)
    report = render_page_plan(source, plan, tmp_path / 'rendered.pdf')
    gate = run_gate(source, report.pdf_bytes, declared_fonts=DECLARED_FONTS)
    report_path = tmp_path / 'pdf-fidelity-report.json'
    write_report(gate, report_path)

    assert plan.accepted, plan.refusals
    _assert_plan_geometry(plan)
    assert len(plan.columns) == 2
    assert _structural_failures(gate) == [], gate['failures']
    assert gate['pages'][0]['checks']['figure_table_bbox_exact']['ok'] is True
    assert gate['pages'][0]['checks']['columns']['ok'] is True
    left, right = sorted(plan.columns, key=lambda column: column.bbox.x0)
    placement = plan.placement(target.block_id)
    assert placement.column == target.column
    assert placement.bbox.x1 <= plan.columns[placement.column].bbox.x1
    assert placement.bbox.x1 <= right.bbox.x0  # never crosses the gutter
    for line in placement.lines:
        assert line.bbox.x1 <= plan.columns[placement.column].bbox.x1 + 1e-6
    assert report_path.exists()


# ── refusals ──────────────────────────────────────────────────────────────


def test_refusal_is_surfaced_and_source_text_is_not_dropped(tmp_path):
    source = _refusal_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    target = _text_block(adapted)
    translations = _identity(adapted)
    translations[target.block_id] = _grow(target.source_text, 6.0)

    plan = solve_page(adapted.layout, translations)
    assert not plan.accepted
    assert plan.placement(target.block_id) is None
    refusal = next(item for item in plan.refusals
                   if item.block_id == target.block_id)
    assert refusal.code == 'does_not_fit'

    report = render_page_plan(source, plan, tmp_path / 'rendered.pdf')
    assert report.accepted is False
    assert report.refusals == plan.refusals
    preserved_text = '\n'.join(item.text for item in report.preserved_source_text)
    assert 'tiny band of free space' in preserved_text
    rendered_text = '\n'.join(_rendered_lines(report))
    assert 'tiny band of free space' in rendered_text
    document = pymupdf.open(stream=report.pdf_bytes, filetype='pdf')
    try:
        assert document.page_count == 1
    finally:
        document.close()


def test_full_width_flowable_on_two_columns_is_preserved_and_reported(tmp_path):
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Full Width Title Spanning Both Columns'],
                 50, 70, 15, 18)
    _write_lines(page, ['Left column body line one.', 'Left column body line two.'],
                 50, 120, 10, 12)
    _write_lines(page, ['Right column body line one.', 'Right column body line two.'],
                 312, 120, 10, 12)
    source = document.tobytes()
    document.close()

    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    assert [item.reason for item in adapted.preserved] == [
        'full-width flowable on a multi-column page'
    ]
    plan = solve_page(adapted.layout, _identity(adapted))
    report = render_page_plan(source, plan)
    rendered_text = '\n'.join(_rendered_lines(report))
    assert 'Full Width Title' in rendered_text


# ── fonts ─────────────────────────────────────────────────────────────────


def test_cjk_translation_embeds_the_curated_fallback_faces(tmp_path):
    source = _single_column_source()
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    target = _text_block(adapted)
    translations = _identity(adapted)
    translations[target.block_id] = (
        '这是一段用于测试排版的中文译文，保持栏目宽度不变。'
        '中文更长，需要按字断行。Mixed Latin text stays readable. '
    ) * 2

    plan = solve_page(adapted.layout, translations)
    report = render_page_plan(source, plan, tmp_path / 'rendered.pdf')
    gate = run_gate(source, report.pdf_bytes, declared_fonts=DECLARED_FONTS)

    assert plan.accepted, plan.refusals
    families = {family for _path, family in report.fonts}
    assert 'DejaVu Sans' in families
    assert 'Noto Sans SC' in families
    rendered_text = '\n'.join(_rendered_lines(report))
    assert '中文译文' in rendered_text
    assert gate['pages'][0]['checks']['font_family']['ok'] is True


def test_missing_face_is_a_typed_error_not_a_substitution():
    with pytest.raises(RenderFontError):
        from lib.pdf_parser.render import _font_for
        _font_for('/nonexistent/font.ttf', 'DejaVu Sans')