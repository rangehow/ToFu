"""Office documents publish as OOXML packages, not as bare filename extensions.

The public history gate admits a ZIP's members by repository path class. A
PPTX/DOCX/XLSX is a ZIP whose part set is declared by ``[Content_Types].xml``
and ``_rels/.rels``; its members are document parts. ``ppt/printerSettings/
printerSettings1.bin`` is printer settings, not model weights, and the
container decides that — a ZIP that is not an Office package keeps the
path-class refusal for a ``.bin``/``.safetensors`` member.
"""
from __future__ import annotations

import io
import os
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._history as history  # noqa: E402
import export_pkg._publish as publish  # noqa: E402

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
OFFICE_FIXTURES = [
    'tests/fixtures/golden_documents/expected/synthetic-pptd-feature-tour/'
    'slides.export_pptx/deck.pptx',
    'tests/fixtures/golden_documents/inputs/synthetic/pptx_200_slides.pptx',
    'tests/fixtures/golden_documents/inputs/synthetic/pptx_import_fidelity.pptx',
]
REFUSAL = 'Duplicate, private or encrypted public history archive member'

_PACKAGE_PARTS = {
    '[Content_Types].xml':
        b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
        b'package/2006/content-types"><Default Extension="bin" '
        b'ContentType="application/vnd.openxmlformats-officedocument.'
        b'presentationml.printerSettings"/></Types>',
    '_rels/.rels':
        b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        b'package/2006/relationships"></Relationships>',
}


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # duplicate-name warnings are the point
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, content in members:
                archive.writestr(name, content)
    return stream.getvalue()


def _office_zip(members: list[tuple[str, bytes]]) -> bytes:
    return _zip(list(_PACKAGE_PARTS.items()) + members)


def _mark_encrypted(data: bytes, name: str) -> bytes:
    """Set the ZIP general-purpose bit 0 on ``name``'s central-directory entry.

    ``zipfile`` resets flag_bits when writing, so encrypt the archive metadata
    afterwards; the gate must refuse the entry before it ever reads the member.
    """
    raw = bytearray(data)
    marker, encoded, start = b'PK\x01\x02', name.encode(), 0
    while True:
        at = raw.index(marker, start)
        length = int.from_bytes(raw[at + 28:at + 30], 'little')
        if bytes(raw[at + 46:at + 46 + length]) == encoded:
            raw[at + 8] |= 1
            return bytes(raw)
        start = at + 4


@pytest.mark.parametrize('rel', OFFICE_FIXTURES)
def test_an_office_fixture_is_checked_as_a_package_part_set(rel):
    data = (REPO / rel).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = {entry.orig_filename for entry in archive.infolist()
                 if not entry.orig_filename.endswith('/')}
    assert {'[Content_Types].xml', '_rels/.rels'} <= names
    assert 'ppt/printerSettings/printerSettings1.bin' in names
    history._check_blob(data, rel, history._HistoryBudget())


@pytest.mark.parametrize('member', ['model.bin', 'weights/model.safetensors'])
def test_a_zip_outside_an_office_package_still_refuses_a_weight_member(member):
    packed = _zip([('[Content_Types].xml', b'not a package'), (member, b'weights')])
    with pytest.raises(publish.ExportPublishSafetyError, match=REFUSAL):
        history._check_blob(packed, 'vendor/package.zip', history._HistoryBudget())


def test_an_office_package_still_refuses_duplicate_parts():
    packed = _office_zip([('ppt/printerSettings/printerSettings1.bin', b'a'),
                          ('ppt/printerSettings/printerSettings1.bin', b'b')])
    with pytest.raises(publish.ExportPublishSafetyError, match=REFUSAL):
        history._check_blob(packed, 'tests/fixtures/deck.pptx', history._HistoryBudget())


def test_an_office_package_still_refuses_encrypted_parts():
    packed = _office_zip([('ppt/printerSettings/printerSettings1.bin', b'settings')])
    packed = _mark_encrypted(packed, 'ppt/printerSettings/printerSettings1.bin')
    with pytest.raises(publish.ExportPublishSafetyError, match=REFUSAL):
        history._check_blob(packed, 'tests/fixtures/deck.pptx', history._HistoryBudget())


def test_an_office_package_part_is_still_secret_scanned():
    packed = _office_zip([('ppt/printerSettings/printerSettings1.bin',
                           b'/mnt/your-fs')])
    with pytest.raises(publish.ExportPublishSafetyError):
        history._check_blob(packed, 'tests/fixtures/deck.pptx', history._HistoryBudget())


def test_top_level_path_classification_is_unchanged():
    assert history._public_path('model.bin') is False
    assert history._public_path('weights/model.safetensors') is False
    assert history._public_path(OFFICE_FIXTURES[0]) is True
