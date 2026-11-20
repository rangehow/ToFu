"""Executable spec for the end-to-end PDF translation path.

Every fixture is a synthetic born-digital page built locally by the render
pipeline's helpers; the translator is a deterministic fake. No real model,
network, user PDF or OCR path is involved.

The path under test is
``extract → translate (adapter) → solve → render → gate(translation)`` and its
contract is that the returned manifest covers every source block exactly once,
so translation mode cannot pass by simply ignoring the translated content.
"""
from __future__ import annotations

import json
import re

import pymupdf
import pytest

from lib.paper.builds.pdf_translation import (
    TranslationPipelineError,
    main,
    translate_pdf,
)
from tests.pdf_parser.test_pdf_render_pipeline import (
    DECLARED_FONTS,
    _single_column_source,
    _two_column_source,
)

pytestmark = pytest.mark.unit

_SOURCE_RE = re.compile(r'<source_text>\n(.*)\n</source_text>', re.DOTALL)


def _source_of(prompt: str) -> str:
    match = _SOURCE_RE.search(prompt)
    assert match is not None, prompt
    return match.group(1)


def _transform(function):
    return lambda prompt: function(_source_of(prompt))


def _raise_on(target: str):
    """Translate everything, but raise when the adapter sends ``target``."""
    def call(prompt: str) -> str:
        text = _source_of(prompt)
        if text == target:
            raise RuntimeError('injected translator failure')
        return text.upper()
    return call


def _rendered_text(pdf_bytes: bytes) -> str:
    document = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        return '\n'.join(page.get_text() for page in document)
    finally:
        document.close()


def _page_count(pdf_bytes: bytes) -> int:
    document = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        return document.page_count
    finally:
        document.close()


@pytest.mark.parametrize('builder,columns', [
    (_single_column_source, 1),
    (_two_column_source, 2),
], ids=['single_column', 'two_column'])
def test_translated_page_passes_translation_mode_gates(builder, columns):
    source = builder()
    result = translate_pdf(
        source, 'zh', translate=_transform(str.upper),
        declared_fonts=DECLARED_FONTS)

    assert result.pdf_bytes is not None
    assert result.gate['mode'] == 'translation'
    assert result.ok is True, result.failures
    assert result.page_reports and all(page['ok'] for page in result.page_reports)
    assert result.manifest['pages'][0]['blocks']
    manifest_check = result.page_reports[0]['checks']['translation_manifest']
    assert manifest_check['ok'] is True
    assert manifest_check['unmatched_source_blocks'] == []
    assert manifest_check['unused_manifest_blocks'] == []
    assert result.page_reports[0]['checks']['columns']['source'] == columns
    assert result.summary['translated'] >= 1
    assert _page_count(result.pdf_bytes) == 1


def test_overlong_translation_is_refused_and_source_text_survives():
    source = _single_column_source()
    result = translate_pdf(
        source, 'zh', translate=_transform(lambda text: (text + ' ') * 6),
        declared_fonts=DECLARED_FONTS)

    refused = [item for item in result.outcomes if item.status == 'refused']
    assert refused, result.outcomes
    assert result.summary['refused'] == len(refused)
    assert all(item.detail for item in refused)

    # The page still renders and the gate accepts the preserved source text.
    assert result.pdf_bytes is not None
    assert result.ok is True, result.failures
    refused_check = result.page_reports[0]['checks']['refused_text_present']
    assert refused_check['ok'] is True
    assert refused_check['blocks']
    assert 'The quick brown fox jumps over the lazy dog every morning.' \
        in _rendered_text(result.pdf_bytes)


def test_translator_failure_marks_one_block_failed_and_page_still_renders():
    source = _single_column_source()
    # The paragraph directly above the figure is the second text call.
    target = ('The quick brown fox jumps over the lazy dog every morning.\n'
              'A second line follows with ordinary words and punctuation.\n'
              'A third line keeps the paragraph block connected and stable.\n'
              'A fourth line adds enough width for a useful column measure.\n'
              'A fifth line remains comfortably inside the text measure.\n'
              'A sixth line closes the source paragraph in this fixture.')

    result = translate_pdf(
        source, 'zh', translate=_raise_on(target),
        declared_fonts=DECLARED_FONTS)

    failed = [item for item in result.outcomes if item.status == 'failed']
    assert len(failed) == 1, result.outcomes
    assert failed[0].kind == 'paragraph'
    assert 'injected translator failure' in (failed[0].detail or '')
    assert result.summary['failed'] == 1
    assert result.summary['translated'] >= 1

    assert result.pdf_bytes is not None
    assert _page_count(result.pdf_bytes) == 1
    assert result.ok is True, result.failures
    assert 'The quick brown fox jumps over the lazy dog every morning.' \
        in _rendered_text(result.pdf_bytes)


def test_manifest_covers_every_source_block_exactly_once():
    source = _two_column_source()
    result = translate_pdf(
        source, 'zh', translate=_transform(str.upper),
        declared_fonts=DECLARED_FONTS)

    blocks = result.manifest['pages'][0]['blocks']
    assert [entry['status'] for entry in blocks].count('fixed') == 1
    assert all(entry['status'] in ('translated', 'preserved', 'refused', 'fixed')
               for entry in blocks)
    assert len(blocks) == len({tuple(entry['source_bbox']) for entry in blocks})
    check = result.page_reports[0]['checks']['translation_manifest']
    assert check['ok'] is True
    assert check['source_blocks'] == check['manifest_blocks'] == len(blocks)


def test_multi_page_document_is_merged_and_gated_per_page():
    first = pymupdf.open(stream=_single_column_source(), filetype='pdf')
    second = pymupdf.open(stream=_two_column_source(), filetype='pdf')
    try:
        first.insert_pdf(second)
        source = first.tobytes()
    finally:
        first.close()
        second.close()

    result = translate_pdf(source, 'zh', translate=_transform(str.upper),
                           declared_fonts=DECLARED_FONTS)

    assert _page_count(result.pdf_bytes) == 2
    assert [page['page'] for page in result.page_reports] == [1, 2]
    assert all(page['ok'] for page in result.page_reports)
    assert [page['page'] for page in result.manifest['pages']] == [1, 2]
    assert [page['page'] for page in result.summary['pages']] == [1, 2]
    assert result.ok is True, result.failures


def test_unreadable_and_textless_inputs_are_typed_errors():
    with pytest.raises(ValueError):
        translate_pdf(b'%PDF-1.4\nnot a document\n', 'zh',
                      translate=_transform(str.upper))

    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0))
    payload = document.tobytes()
    document.close()
    with pytest.raises(TranslationPipelineError):
        translate_pdf(payload, 'zh', translate=_transform(str.upper))


def test_cli_exits_zero_when_all_pages_pass_and_nonzero_on_gate_failure(tmp_path):
    source = tmp_path / 'source.pdf'
    source.write_bytes(_single_column_source())

    output = tmp_path / 'translated.pdf'
    report = tmp_path / 'report.json'
    manifest = tmp_path / 'manifest.json'
    args = ['--source', str(source), '--output', str(output),
            '--target-language', 'zh', '--translator', 'upper',
            '--report', str(report), '--manifest', str(manifest), '--quiet']

    assert main(args) == 0
    assert output.exists() and manifest.exists()
    assert json.loads(report.read_text(encoding='utf-8'))['ok'] is True

    # An overlong translation is a refusal, not a gate failure.
    assert main(args[:6] + ['--translator', 'overlong'] + args[8:]) == 0

    # A rendered family the caller declares away fails font_family on page 1.
    bad_report = tmp_path / 'bad-report.json'
    bad_fonts = '{"latin": ["Helvetica"], "cjk": ["Helvetica"]}'
    assert main(['--source', str(source), '--output', str(tmp_path / 'bad.pdf'),
                 '--target-language', 'zh', '--translator', 'upper',
                 '--fonts', bad_fonts, '--report', str(bad_report),
                 '--quiet']) == 1
    failures = json.loads(bad_report.read_text(encoding='utf-8'))['failures']
    assert [(item['page'], item['check']) for item in failures] == [
        (1, 'font_family')]