"""HTML extraction on captured real pages keeps its recorded output.

``fixtures/html_corpus`` holds 64 pages captured through the shared network
authority (``scripts/capture_search_html_corpus.py``) across news, reference,
documentation, code hosting and forum sites in 15+ languages, plus
``golden.json``: ``extract_html_text`` output (text and rendering scope) of
the implementation current at capture. The Python extraction path is checked
always; the native kernel path additionally when the extension is installed.
trafilatura/BeautifulSoup/lxml upgrades legitimately change output, so the
comparison runs only with the library versions that recorded it (re-record
with ``rust/scripts/record_html_corpus_golden.py``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import bs4
import lxml
import pytest
import trafilatura

from tofu_search import _native_kernels
from tofu_search.fetch import html_extract

pytestmark = pytest.mark.unit

CORPUS_DIR = Path(__file__).resolve().parent / 'fixtures' / 'html_corpus'
GOLDEN = json.loads((CORPUS_DIR / 'golden.json').read_text())
MANIFEST = json.loads((CORPUS_DIR / 'manifest.json').read_text())
_VERSIONS = {'python': sys.version.split()[0], 'trafilatura': trafilatura.__version__,
             'beautifulsoup4': bs4.__version__, 'lxml': lxml.__version__}
_SAME_LIBRARIES = all(GOLDEN['versions'][key] == value for key, value in _VERSIONS.items()
                      if key != 'python')

_PATHS = ['python'] + (['native'] if _native_kernels.native is not None else [])


def test_corpus_breadth():
    pages = MANIFEST['pages']
    hosts = {page['url'].split('/')[2] for page in pages}
    assert len(pages) >= 50 and len(hosts) >= 50
    assert all(page['fixture_chars'] <= 160_000 for page in pages)
    assert len(GOLDEN['cases']) == 2 * len(pages)


@pytest.mark.skipif(not _SAME_LIBRARIES, reason='golden recorded with other library versions')
@pytest.mark.parametrize('path', _PATHS)
def test_extract_html_text_matches_golden(path, monkeypatch):
    if path == 'python':
        monkeypatch.setattr(html_extract, '_assemble_document',
                            html_extract.PYTHON_KERNELS['_assemble_document'])
    for page in MANIFEST['pages']:
        html = (CORPUS_DIR / f"{page['name']}.html").read_text()
        for limit in (0, 4000):
            scope = {}
            text = html_extract.extract_html_text(html, limit, page['url'], scope=scope)
            document = scope.pop('document_text', None)
            expected = GOLDEN['cases'][f"{page['name']}:{limit}"]
            assert text == expected['text'], (page['url'], limit)
            assert scope == expected['scope'], (page['url'], limit)
            digest = hashlib.sha256(document.encode()).hexdigest() if document is not None else None
            assert digest == expected['document_text_sha256'], (page['url'], limit)
