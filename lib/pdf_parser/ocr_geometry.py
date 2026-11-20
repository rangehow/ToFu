"""Narrow CPU OCR geometry probe for the scanned-page path.

Responsibility
--------------
Expose the OCR capability that already exists on this host without adding a
second translation path: Tesseract runs as a CPU-only subprocess and returns
line blocks with pixel geometry. The subprocess boundary keeps Tesseract's
GIL-holding native pass out of the server process, matching the owned-child
rule in ``lib/knowledge/ocr.py``.

No model API, network access or bundled model is used. The scanned-page
translation path is not wired to this probe; callers use it only to check that
the engine can produce text plus geometry for a raster page.

Entry points
------------
``engine_inventory()`` reports the exact binary, version and language data.
``recognize_text_blocks(image_bytes)`` returns :class:`TextBlock` values.
``recognize_text(image_bytes)`` returns their joined text.

Dependencies
------------
The ``tesseract`` executable and its ``TESSDATA_PREFIX`` language data.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_LANGUAGE = 'eng'
_LANGUAGE_PATTERN = re.compile(r'^[A-Za-z0-9_+]+$')

__all__ = [
    'DEFAULT_LANGUAGE',
    'OcrEngineUnavailable',
    'OcrRecognitionError',
    'TextBlock',
    'engine_inventory',
    'recognize_text',
    'recognize_text_blocks',
]


class OcrEngineUnavailable(RuntimeError):
    """No CPU OCR executable is installed or it could not start."""


class OcrRecognitionError(RuntimeError):
    """The OCR executable ran but did not return a usable result."""


@dataclass(frozen=True, slots=True)
class TextBlock:
    """One recognized line with its source-image pixel geometry."""

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float
    page_number: int
    block_number: int
    line_number: int


def _binary() -> str:
    binary = shutil.which('tesseract')
    if not binary:
        raise OcrEngineUnavailable(
            'tesseract binary not found on PATH; install the conda-forge '
            '`tesseract` package (5.x) with its `tessdata` language data')
    return binary


def _run(binary: str, arguments: list[str], *,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [binary, *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise OcrEngineUnavailable(
            f'tesseract could not start: {error}') from error


def engine_inventory() -> dict:
    """Report the installed OCR binary and language data, or availability."""
    binary = shutil.which('tesseract')
    if not binary:
        return {'available': False, 'binary': None, 'version': '',
                'leptonica': '', 'languages': [], 'tessdataPrefix': ''}
    version_result = _run(binary, ['--version'])
    version_text = (version_result.stdout or b'').decode('utf-8', 'replace')
    first_line = version_text.splitlines()[0].strip() if version_text else ''
    version = first_line.removeprefix('tesseract ').strip()
    leptonica = ''
    for line in version_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('leptonica'):
            leptonica = stripped
            break
    language_result = _run(binary, ['--list-langs'])
    language_text = (language_result.stdout or b'').decode('utf-8', 'replace')
    languages = [
        line.strip() for line in language_text.splitlines()[1:]
        if line.strip() and line.strip() != '*'
    ]
    return {
        'available': version_result.returncode == 0,
        'binary': binary,
        'version': version,
        'leptonica': leptonica,
        'languages': languages,
        'tessdataPrefix': os.environ.get('TESSDATA_PREFIX', ''),
    }


def _parse_tsv(encoded: bytes) -> list[TextBlock]:
    text = encoded.decode('utf-8', 'replace')
    grouped: dict[tuple[int, int, int, int], list[list[str]]] = {}
    order: list[tuple[int, int, int, int]] = []
    for row in text.splitlines():
        columns = row.split('\t')
        if len(columns) < 12 or columns[0] != '5':
            continue
        word = '\t'.join(columns[11:]).strip()
        if not word:
            continue
        key = (int(columns[1]), int(columns[2]),
               int(columns[3]), int(columns[4]))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(columns)
    blocks: list[TextBlock] = []
    for key in order:
        rows = grouped[key]
        left = min(int(row[6]) for row in rows)
        top = min(int(row[7]) for row in rows)
        right = max(int(row[6]) + int(row[8]) for row in rows)
        bottom = max(int(row[7]) + int(row[9]) for row in rows)
        confidences = []
        for row in rows:
            try:
                confidence = float(row[10])
            except ValueError:
                continue
            if confidence >= 0:
                confidences.append(confidence)
        blocks.append(TextBlock(
            text=' '.join('\t'.join(row[11:]).strip() for row in rows),
            bbox=(float(left), float(top), float(right), float(bottom)),
            confidence=(
                sum(confidences) / len(confidences) if confidences else 0.0),
            page_number=key[0],
            block_number=key[1],
            line_number=key[3],
        ))
    return blocks


def recognize_text_blocks(
    image_bytes: bytes, *, language: str = DEFAULT_LANGUAGE,
) -> list[TextBlock]:
    """OCR one raster image and return line blocks with pixel geometry.

    Coordinates are pixels in the source image. An image with no recognized
    text returns an empty list; an engine failure raises explicitly.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError('image_bytes must be non-empty encoded image bytes')
    if not isinstance(language, str) or not _LANGUAGE_PATTERN.match(language):
        raise ValueError('language must be a Tesseract language code')
    binary = _binary()
    result = _run(binary, ['stdin', 'stdout', '-l', language, 'tsv'],
                  input_bytes=bytes(image_bytes))
    if result.returncode != 0:
        detail = (result.stderr or b'').decode('utf-8', 'replace').strip()
        raise OcrRecognitionError(
            f'tesseract exited {result.returncode}: {detail[:2_000]}')
    return _parse_tsv(result.stdout or b'')


def recognize_text(image_bytes: bytes, *,
                   language: str = DEFAULT_LANGUAGE) -> str:
    """Return recognized line text joined by newlines."""
    return '\n'.join(
        block.text for block in recognize_text_blocks(
            image_bytes, language=language))