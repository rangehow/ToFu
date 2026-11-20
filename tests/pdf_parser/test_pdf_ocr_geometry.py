"""CPU OCR geometry fixture for the scanned-page path.

Generates a synthetic raster line locally with PyMuPDF, OCRs it through the
installed Tesseract subprocess, and asserts the recognized text and non-empty
pixel geometry. Scanned-path accuracy is claimed only for this fixture.
"""
import pytest

pytestmark = pytest.mark.unit

pymupdf = pytest.importorskip('pymupdf')

from lib.pdf_parser import ocr_geometry  # noqa: E402

FIXTURE_TEXT = 'The quick brown fox jumps over 42 lazy dogs.'
FIXTURE_DPI = 200


def _raster_fixture():
    document = pymupdf.open()
    page = document.new_page(width=600, height=140)
    page.insert_text((40, 90), FIXTURE_TEXT, fontsize=24)
    pixmap = page.get_pixmap(dpi=FIXTURE_DPI)
    document.close()
    return pixmap.tobytes('png'), pixmap.width, pixmap.height


def test_recognized_text_and_geometry_match_the_raster_fixture():
    inventory = ocr_geometry.engine_inventory()
    if not inventory['available']:
        pytest.skip(f'no CPU OCR engine on this host: {inventory}')

    image, width, height = _raster_fixture()
    blocks = ocr_geometry.recognize_text_blocks(image, language='eng')

    assert blocks, 'OCR returned no line blocks for a readable fixture'
    recognized = ' '.join(block.text for block in blocks).split()
    assert recognized == FIXTURE_TEXT.split()

    for block in blocks:
        x0, y0, x1, y1 = block.bbox
        assert x1 > x0 and y1 > y0, f'empty geometry: {block.bbox}'
        assert 0 <= x0 < x1 <= width, f'x outside image: {block.bbox}'
        assert 0 <= y0 < y1 <= height, f'y outside image: {block.bbox}'
        assert 0.0 <= block.confidence <= 100.0
        assert block.text.strip()


def test_missing_engine_is_an_explicit_blocked_report(monkeypatch):
    monkeypatch.setattr(ocr_geometry.shutil, 'which', lambda _name: None)

    inventory = ocr_geometry.engine_inventory()
    assert inventory['available'] is False
    assert inventory['binary'] is None

    with pytest.raises(ocr_geometry.OcrEngineUnavailable) as error:
        ocr_geometry.recognize_text_blocks(b'not-read-because-engine-missing')
    assert 'tesseract' in str(error.value)
    assert 'tessdata' in str(error.value)