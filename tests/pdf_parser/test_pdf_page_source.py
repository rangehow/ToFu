"""Scanned/born-digital page boundary for PDF translation.

Executable specification for ``lib.pdf_parser.page_source``: classification
uses only the extractor's text layer, scanned pages route to a separate
``scanned_unsupported`` status, and no page may become an empty translation.
"""
import pytest

pytestmark = pytest.mark.unit

pymupdf = pytest.importorskip('pymupdf')

BODY = (
    'Born-digital pages carry a real text layer. Translation reads that layer '
    'and preserves its layout. This paragraph is long enough to clear the '
    'documented minimum of fifty non-whitespace characters.')


def _text_pdf(text=BODY, *, pages=1):
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 500), text, fontsize=11)
    encoded = document.tobytes()
    document.close()
    return encoded


def _raster_pdf(text='Scanned page with no text layer.'):
    source = pymupdf.open()
    source_page = source.new_page(width=595, height=842)
    source_page.insert_text((60, 120), text, fontsize=24)
    pixmap = source_page.get_pixmap(dpi=150)
    source.close()

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pixmap)
    encoded = document.tobytes()
    document.close()
    return encoded


def _mixed_pdf():
    document = pymupdf.open()
    text_page = document.new_page(width=595, height=842)
    text_page.insert_textbox(pymupdf.Rect(50, 50, 545, 500), BODY, fontsize=11)

    source = pymupdf.open()
    source_page = source.new_page(width=595, height=842)
    source_page.insert_text((60, 120), 'Scanned page.', fontsize=24)
    pixmap = source_page.get_pixmap(dpi=150)
    source.close()
    image_page = document.new_page(width=595, height=842)
    image_page.insert_image(image_page.rect, pixmap=pixmap)
    encoded = document.tobytes()
    document.close()
    return encoded


def test_classification_uses_only_extractable_text():
    from lib.pdf_parser.page_source import (
        BORN_DIGITAL, MIN_PAGE_TEXT_CHARS, SCANNED, classify_page_text,
    )

    assert classify_page_text(0, '').kind == SCANNED
    assert classify_page_text(0, '   \n\t ').kind == SCANNED
    assert classify_page_text(
        0, 'x' * (MIN_PAGE_TEXT_CHARS - 1)).kind == SCANNED
    born = classify_page_text(0, 'x' * MIN_PAGE_TEXT_CHARS)
    assert born.kind == BORN_DIGITAL
    assert born.text_chars == MIN_PAGE_TEXT_CHARS
    assert born.text_blocks == 1


def test_extract_page_sources_reads_the_text_layer_without_ocr():
    from lib.pdf_parser.page_source import (
        BORN_DIGITAL, SCANNED, extract_page_sources,
    )

    sources = extract_page_sources(_mixed_pdf())
    assert [source.kind for source in sources] == [BORN_DIGITAL, SCANNED]
    assert sources[0].page_index == 0
    assert sources[0].text_chars >= 50
    assert sources[0].text_blocks >= 1
    assert sources[1].page_index == 1
    assert sources[1].text_chars == 0
    assert 'no extractable text layer' in sources[1].reason


def test_scanned_page_routes_to_scanned_unsupported():
    from lib.pdf_parser.page_source import (
        SCANNED_UNSUPPORTED, extract_page_sources, route_pages,
    )

    routing = route_pages(extract_page_sources(_mixed_pdf()))
    report = routing.report()
    assert routing.born_digital_pages == (0,)
    assert routing.scanned_pages == (1,)
    assert report['status'] == SCANNED_UNSUPPORTED
    assert report['scannedStatus'] == SCANNED_UNSUPPORTED
    assert [page['status'] for page in report['pages']] == [
        'ready', SCANNED_UNSUPPORTED]
    assert report['pages'][1]['kind'] == 'scanned'


def test_structure_extraction_feeds_the_same_routing():
    from lib.pdf_parser.page_source import (
        SCANNED_UNSUPPORTED, classify_structure,
    )
    from lib.pdf_parser.structure import extract_pdf_structure

    routing = classify_structure(extract_pdf_structure(_mixed_pdf()))
    assert routing.born_digital_pages == (0,)
    assert routing.scanned_pages == (1,)
    assert routing.report()['scannedStatus'] == SCANNED_UNSUPPORTED
    assert routing.report()['pages'][1]['reason']


def test_all_born_digital_document_is_ready():
    from lib.pdf_parser.page_source import extract_page_sources, route_pages

    report = route_pages(extract_page_sources(_text_pdf())).report()
    assert report['status'] == 'ready'
    assert report['bornDigitalPages'] == [0]
    assert report['scannedPages'] == []
    assert report['scannedStatus'] is None


def test_translation_boundary_refuses_scanned_and_empty_pages():
    from lib.pdf_parser.page_source import (
        ScannedPageUnsupported, classify_page_text,
        require_translation_text, EMPTY_SOURCE, PageTranslationRefused,
    )

    scanned = classify_page_text(2, '')
    with pytest.raises(ScannedPageUnsupported) as error:
        require_translation_text(
            scanned, 'translated text must never be fabricated here')
    assert error.value.status == 'scanned_unsupported'
    assert error.value.page_index == 2

    born = classify_page_text(0, BODY, text_blocks=1)
    assert require_translation_text(born, BODY) == BODY
    with pytest.raises(PageTranslationRefused) as empty:
        require_translation_text(born, '   ')
    assert empty.value.status == EMPTY_SOURCE