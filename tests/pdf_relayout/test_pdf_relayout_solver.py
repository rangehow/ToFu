"""Geometry-only specifications for the length-aware PDF re-layout solver."""

from __future__ import annotations

import os

import pytest

from lib.pdf_relayout import (
    Block,
    Column,
    FontSpec,
    PageLayout,
    Rect,
    solve_page,
)
from lib.pdf_relayout.fonts import missing_stack_families

pytestmark = pytest.mark.unit

PAGE = Rect(0, 0, 612, 792)
SENTENCE = 'the quick brown fox jumps over the lazy dog. '


def _single_column_page(block: Block, *extra: Block) -> PageLayout:
    return PageLayout(
        'page-1',
        PAGE,
        (Column(0, Rect(72, 72, 540, 720)),),
        (block, *extra),
    )


def _two_column_page(*blocks: Block) -> PageLayout:
    return PageLayout(
        'page-1',
        PAGE,
        (
            Column(0, Rect(50, 50, 300, 740)),
            Column(1, Rect(312, 50, 562, 740)),
        ),
        blocks,
    )


def _assert_no_figure_overlap_and_columns_kept(plan, layout: PageLayout) -> None:
    fixed = [p for p in plan.placements if p.kind in {'figure', 'table'}]
    for placement in plan.placements:
        if placement.kind not in {'figure', 'table'}:
            for obstacle in fixed:
                assert not placement.bbox.intersects(obstacle.bbox), (
                    f'{placement.block_id} overlaps {obstacle.block_id}'
                )
    for placement in plan.placements:
        assert placement.column == next(
            block.column for block in layout.blocks if block.block_id == placement.block_id
        )


def test_single_column_growth_extends_without_changing_font():
    source = SENTENCE * 10
    translated = source + SENTENCE * 3
    block = Block(
        'body',
        'text',
        Rect(72, 100, 540, 160),  # six source lines at 10/12
        0,
        FontSpec('DejaVu Sans', 10, 12),
    )
    figure = Block('figure-1', 'figure', Rect(72, 400, 540, 620), 0)
    layout = _single_column_page(block, figure)

    plan = solve_page(layout, {'body': translated})

    assert plan.accepted, plan.refusals
    placement = plan.placement('body')
    assert placement is not None
    assert placement.font_size_pt == 10
    assert placement.leading_pt == 12
    assert placement.bbox.height > block.bbox.height
    assert placement.bbox.y0 == block.bbox.y0
    assert placement.bbox.x0 == block.bbox.x0
    assert placement.bbox.x1 == block.bbox.x1
    assert placement.bbox.y1 < figure.bbox.y0
    assert len(translated) == pytest.approx(len(source) * 1.3, rel=0.01)
    assert all(os.path.exists(run.font_path) for line in placement.lines for run in line.runs)
    _assert_no_figure_overlap_and_columns_kept(plan, layout)


def test_two_columns_never_bleed_into_each_other():
    left_text = SENTENCE * 40
    right_text = SENTENCE * 4
    left = Block('left-body', 'text', Rect(50, 100, 300, 160), 0, FontSpec('DejaVu Sans', 10, 12))
    right = Block('right-body', 'text', Rect(312, 120, 562, 180), 1, FontSpec('DejaVu Sans', 10, 12))
    layout = _two_column_page(left, right)

    plan = solve_page(layout, {'left-body': left_text, 'right-body': right_text})

    assert plan.accepted, plan.refusals
    left_plan = plan.placement('left-body')
    right_plan = plan.placement('right-body')
    assert left_plan is not None and right_plan is not None
    assert left_plan.bbox.x1 <= 300
    assert right_plan.bbox.x0 >= 312
    assert not left_plan.bbox.intersects(right_plan.bbox)
    assert left_plan.bbox.y1 > left.bbox.y1  # long translation used its own column's space
    assert left_plan.column == 0 and right_plan.column == 1
    _assert_no_figure_overlap_and_columns_kept(plan, layout)


def test_refuses_rather_than_overlap_a_figure():
    block = Block(
        'body',
        'text',
        Rect(72, 100, 540, 120),
        0,
        FontSpec('DejaVu Sans', 10, 12),
    )
    figure = Block('figure-1', 'figure', Rect(72, 124, 540, 400), 0)
    layout = _single_column_page(block, figure)

    plan = solve_page(layout, {'body': SENTENCE * 40})

    assert not plan.accepted
    assert plan.placement('body') is None
    refusal = plan.refusals[0]
    assert refusal.block_id == 'body'
    assert refusal.code == 'does_not_fit'
    assert refusal.minimum_font_size_pt == 7.0
    assert '7.0pt minimum' in refusal.message
    assert 'figure' in refusal.limiting_element
    assert plan.placement('figure-1').bbox == figure.bbox
    _assert_no_figure_overlap_and_columns_kept(plan, layout)


def test_caption_refuses_instead_of_detaching_from_its_table():
    caption = Block(
        'caption',
        'caption',
        Rect(72, 176, 540, 188),
        0,
        FontSpec('DejaVu Sans', 9, 11),
        attached_to='table-1',
    )
    table = Block('table-1', 'table', Rect(72, 200, 540, 500), 0)
    layout = _single_column_page(caption, table)

    plan = solve_page(layout, {'caption': SENTENCE * 10})

    assert not plan.accepted
    refusal = plan.refusals[0]
    assert refusal.block_id == 'caption'
    assert refusal.code == 'does_not_fit'
    assert 'table' in refusal.limiting_element
    assert plan.placement('table-1').bbox == table.bbox
    _assert_no_figure_overlap_and_columns_kept(plan, layout)


def test_cjk_uses_curated_installed_fallback_stack():
    assert missing_stack_families() == ()
    block = Block(
        'mixed',
        'text',
        Rect(72, 80, 540, 120),
        0,
        FontSpec('Noto Sans SC', 10, 13),
    )
    layout = _single_column_page(block)
    text = (
        '这是一段用于测试排版的中文文本，包含标点符号。'
        'Mixed Latin PDF translation 保持布局不变。\n'
    ) * 4

    plan = solve_page(layout, {'mixed': text})

    assert plan.accepted, plan.refusals
    assert plan.missing_glyphs == ()
    placement = plan.placement('mixed')
    assert placement is not None
    assert len(placement.lines) > 1
    runs = [run for line in placement.lines for run in line.runs]
    cjk_runs = [run for run in runs if any('\u4e00' <= ch <= '\u9fff' for ch in run.text)]
    latin_runs = [run for run in runs if 'Mixed' in run.text]
    assert cjk_runs and all(run.font_family == 'Noto Sans SC' for run in cjk_runs)
    assert latin_runs and all(run.font_family == 'DejaVu Sans' for run in latin_runs)
    assert all(os.path.exists(run.font_path) for run in runs)
    column = layout.columns[0]
    assert all(line.bbox.x1 <= column.bbox.x1 for line in placement.lines)
    assert placement.bbox.x1 <= column.bbox.x1
    _assert_no_figure_overlap_and_columns_kept(plan, layout)