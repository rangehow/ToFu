"""Executable spec for the born-digital PDF reproduction fidelity gate.

Each negative control changes one property of the rendered PDF and must fail
exactly the check that owns that property; a gate that cannot fail is useless.
All fixtures are synthetic and generated locally by ``tests.support``.
"""
from __future__ import annotations

import json

import pytest

from lib.paper.builds.pdf_fidelity import (CHECK_NAMES, REPORT_SCHEMA,
                                           TRANSLATION_CHECK_NAMES,
                                           run_gate, main)
from lib.pdf_parser.layout import page_layout_from_structure
from lib.pdf_parser.render import render_page_plan
from lib.pdf_parser.structure import extract_pdf_structure
from lib.pdf_relayout import solve_page
from tests.pdf_parser.test_pdf_render_pipeline import (
    DECLARED_FONTS as RENDER_FONTS, _grow, _identity, _refusal_source,
    _single_column_source, _text_block, _two_column_source)
from tests.support.pdf_fidelity_pairs import FONT_STACK, build, build_pair
from tests.support.pdf_fidelity_translation import (
    FONT_STACK as TRANSLATION_FONT_STACK, build_pair as build_translation_pair)

pytestmark = pytest.mark.unit

#: Translation-mode control -> the one check that owns that property.
TRANSLATION_CONTROLS = (
    ('translated_column', 'translated_block_placement'),
    ('figure_shift', 'figure_table_bbox_exact'),
    ('drop_block', 'target_presence'),
    ('non_translated_shift', 'non_translated_bbox'),
)

# variant -> (owning check, pages that carry the failure)
NEGATIVE_CONTROLS = (
    ('page_count', 'page_count', {None}),
    ('page_size', 'page_size', {1, 2}),
    ('columns', 'columns', {1}),
    ('reading_order', 'reading_order', {1}),
    ('bbox_shift', 'block_bbox_iou', {1}),
    ('figure_table_shift', 'figure_table_bbox_exact', {1}),
    ('drop_text', 'text_presence', {1}),
    ('font', 'font_family', {1}),
    ('cjk_font', 'font_family', {1}),
    ('overlap', 'reserved_regions', {1}),
)


@pytest.fixture
def pair(tmp_path):
    source = tmp_path / 'source.pdf'
    rendered = tmp_path / 'rendered.pdf'
    build_pair(source, rendered)
    return source, rendered


@pytest.fixture
def rendered_variant(tmp_path, pair):
    source, _ = pair

    def make(variant):
        target = tmp_path / f'{variant}.pdf'
        build(target, variant)
        return source, target
    return make


def test_identical_reproduction_passes_every_check_on_every_page(pair, tmp_path):
    source, rendered = pair
    report = run_gate(source, rendered, declared_fonts=FONT_STACK)
    assert report['schema'] == REPORT_SCHEMA
    assert report['ok'] is True, report['failures']
    assert report['checks']['page_count']['ok'] is True
    assert [page['page'] for page in report['pages']] == [1, 2]
    for page in report['pages']:
        assert page['ok'] is True
        assert set(page['checks']) == set(CHECK_NAMES) - {'page_count'}
        assert all(check['ok'] for check in page['checks'].values())
        assert page['failures'] == []
    target = tmp_path / 'report.json'
    target.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    assert json.loads(target.read_text(encoding='utf-8'))['ok'] is True


@pytest.mark.parametrize('variant,owning_check,page_numbers',
                         NEGATIVE_CONTROLS, ids=[row[0] for row in NEGATIVE_CONTROLS])
def test_negative_control_fails_only_its_check(rendered_variant, variant,
                                               owning_check, page_numbers):
    source, rendered = rendered_variant(variant)
    report = run_gate(source, rendered, declared_fonts=FONT_STACK)
    assert report['ok'] is False
    assert {failure['check'] for failure in report['failures']} == {owning_check}, \
        report['failures']
    assert {failure['page'] for failure in report['failures']} == page_numbers
    for failure in report['failures']:
        assert failure['detail']


def test_font_check_needs_a_declared_stack_but_still_detects_inconsistency(
        rendered_variant):
    source, rendered = rendered_variant('font')
    report = run_gate(source, rendered)
    assert report['ok'] is False
    assert {failure['check'] for failure in report['failures']} == {'font_family'}
    detail = report['pages'][0]['checks']['font_family']
    assert detail['declared'] is None
    assert detail['inconsistent'] and detail['inconsistent'][0]['source_family'] == 'Helvetica'


def test_cli_returns_zero_on_pass_and_one_on_failure(pair, rendered_variant, tmp_path):
    source, rendered = pair
    report_path = tmp_path / 'cli-pass.json'
    assert main(['--source', str(source), '--rendered', str(rendered),
                 '--fonts', json.dumps(FONT_STACK), '--report', str(report_path),
                 '--quiet']) == 0
    assert json.loads(report_path.read_text(encoding='utf-8'))['ok'] is True

    bad_source, bad = rendered_variant('bbox_shift')
    report_path = tmp_path / 'cli-fail.json'
    assert main(['--source', str(bad_source), '--rendered', str(bad),
                 '--fonts', json.dumps(FONT_STACK), '--report', str(report_path),
                 '--quiet']) == 1
    failures = json.loads(report_path.read_text(encoding='utf-8'))['failures']
    assert [(failure['page'], failure['check']) for failure in failures] == [
        (1, 'block_bbox_iou')]


def test_reproduction_mode_stays_the_default_and_is_not_weakened(pair):
    source, rendered = pair
    report = run_gate(source, rendered, declared_fonts=FONT_STACK)
    assert report['mode'] == 'reproduction'
    assert report['ok'] is True, report['failures']
    # Translation-only tolerances never enter a reproduction report.
    assert 'column_band_pt' not in report['tolerances']
    with pytest.raises(ValueError):
        run_gate(source, rendered, mode='loose')


def test_unreadable_input_is_an_error_not_a_fidelity_verdict(tmp_path, pair):
    source, _ = pair
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'%PDF-1.4\nnot a document\n')
    assert main(['--source', str(source), '--rendered', str(broken), '--quiet']) == 2


# ── translation mode ──────────────────────────────────────────────────────


def _render(source: bytes, *, grow: bool):
    structure = extract_pdf_structure(source)
    adapted = page_layout_from_structure(structure['pages'][0])
    translations = _identity(adapted)
    if grow:
        target = _text_block(adapted)
        translations[target.block_id] = _grow(target.source_text)
    plan = solve_page(adapted.layout, translations)
    return render_page_plan(source, plan)


def _source_manifest(source: bytes, *, refused_text=None) -> dict:
    """A complete manifest for a one-page fixture built from its structure."""
    structure = extract_pdf_structure(source)
    blocks = []
    for block in structure['pages'][0]['blocks']:
        status = 'fixed' if block['kind'] in ('figure', 'table') else 'preserved'
        if refused_text is not None and block.get('text') == refused_text:
            status = 'refused'
        blocks.append({
            'source_bbox': [float(value) for value in block['bbox']],
            'status': status,
            'source_text': block.get('text') or '',
        })
    return {'pages': [{'page': 1, 'blocks': blocks}]}


@pytest.mark.parametrize('builder', [_single_column_source, _two_column_source],
                         ids=['single_column', 'two_column'])
def test_translation_mode_passes_identity_renderer_fixture(builder):
    source = builder()
    report = run_gate(source, _render(source, grow=False).pdf_bytes,
                      mode='translation', declared_fonts=RENDER_FONTS)
    assert report['mode'] == 'translation'
    assert report['ok'] is True, report['failures']
    for page in report['pages']:
        assert set(page['checks']) == set(TRANSLATION_CHECK_NAMES) - {'page_count'}
        assert all(check['ok'] for check in page['checks'].values())


@pytest.mark.parametrize('builder', [_single_column_source, _two_column_source],
                         ids=['single_column', 'two_column'])
def test_translation_mode_passes_thirty_percent_longer_renderer_fixture(builder):
    source = builder()
    rendered = _render(source, grow=True)
    report = run_gate(source, rendered.pdf_bytes, mode='translation',
                      declared_fonts=RENDER_FONTS)
    assert report['mode'] == 'translation'
    assert report['ok'] is True, report['failures']

    # The same output is correctly rejected by the reproduction gate: this is
    # exactly the gap translation mode exists to close.
    reproduction = run_gate(source, rendered.pdf_bytes,
                            declared_fonts=RENDER_FONTS)
    assert reproduction['ok'] is False
    assert 'text_presence' in {failure['check']
                              for failure in reproduction['failures']}


@pytest.mark.parametrize('variant,owning_check', TRANSLATION_CONTROLS,
                         ids=[row[0] for row in TRANSLATION_CONTROLS])
def test_translation_negative_control_fails_only_its_check(
        tmp_path, variant, owning_check):
    source = tmp_path / 'source.pdf'
    rendered = tmp_path / f'{variant}.pdf'
    build_translation_pair(source, rendered, variant)
    report = run_gate(source, rendered, mode='translation',
                      declared_fonts=TRANSLATION_FONT_STACK)
    assert report['ok'] is False
    assert {failure['check'] for failure in report['failures']} == {owning_check}, \
        report['failures']
    assert {failure['page'] for failure in report['failures']} == {1}


def test_translation_manifest_must_cover_every_source_block(tmp_path):
    source = _single_column_source()
    rendered = _render(source, grow=False).pdf_bytes
    empty = {'pages': [{'page': 1, 'blocks': []}]}
    report = run_gate(source, rendered, mode='translation',
                      declared_fonts=RENDER_FONTS, translation=empty)
    assert report['ok'] is False
    assert {failure['check'] for failure in report['failures']} == {
        'translation_manifest'}
    assert report['pages'][0]['checks']['translation_manifest'][
        'unmatched_source_blocks']


def test_complete_manifest_passes_and_refused_text_stays_visible(tmp_path):
    source = _refusal_source()
    structure = extract_pdf_structure(source)
    text_block = next(block for block in structure['pages'][0]['blocks']
                      if block['kind'] == 'paragraph')
    rendered = _render(source, grow=False).pdf_bytes
    manifest = _source_manifest(source, refused_text=text_block['text'])
    report = run_gate(source, rendered, mode='translation',
                      declared_fonts=RENDER_FONTS, translation=manifest)
    assert report['ok'] is True, report['failures']
    refused = report['pages'][0]['checks']['refused_text_present']
    assert refused['ok'] is True and len(refused['blocks']) == 1
    assert report['pages'][0]['checks']['translation_manifest']['ok'] is True


def test_translation_cli_names_the_mode_and_keeps_the_exit_codes(tmp_path):
    source = tmp_path / 'cli-source.pdf'
    rendered = tmp_path / 'cli-rendered.pdf'
    build_translation_pair(source, rendered)
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps({
        'pages': [{'page': 1, 'blocks': [
            {'source_bbox': [0, 0, 1, 1], 'status': 'not-a-status'},
        ]}],
    }), encoding='utf-8')

    report_path = tmp_path / 'translation-pass.json'
    assert main(['--source', str(source), '--rendered', str(rendered),
                 '--mode', 'translation',
                 '--fonts', json.dumps(TRANSLATION_FONT_STACK),
                 '--report', str(report_path), '--quiet']) == 0
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['mode'] == 'translation' and report['ok'] is True

    bad = tmp_path / 'translation-bad.pdf'
    build_translation_pair(source, bad, 'figure_shift')
    assert main(['--source', str(source), '--rendered', str(bad),
                 '--mode', 'translation',
                 '--fonts', json.dumps(TRANSLATION_FONT_STACK),
                 '--quiet']) == 1

    assert main(['--source', str(source), '--rendered', str(rendered),
                 '--mode', 'translation', '--translation', str(manifest_path),
                 '--quiet']) == 2