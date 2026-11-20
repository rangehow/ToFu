"""Record ``extract_html_text`` outputs for the HTML corpus fixtures.

The golden file pins the extraction output of the implementation that was
current when the corpus was captured (trafilatura + BeautifulSoup path), with
the library versions that produced it. ``tests/test_native_html_corpus.py``
compares the Python reference and the native kernel against it when the same
library versions are installed, and against each other always.

Usage: python3 record_html_corpus_golden.py [--check]
"""

import hashlib
import json
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE))
CORPUS = PACKAGE / 'tests' / 'fixtures' / 'html_corpus'
GOLDEN = CORPUS / 'golden.json'
# Unlimited and the fetch path's ordinary preview budget.
MAX_CHARS = (0, 4000)


def versions():
    import bs4
    import lxml
    import trafilatura
    return {'python': sys.version.split()[0], 'trafilatura': trafilatura.__version__,
            'beautifulsoup4': bs4.__version__, 'lxml': lxml.__version__}


def extract_all():
    from tofu_search.fetch.html_extract import extract_html_text
    manifest = json.loads((CORPUS / 'manifest.json').read_text())
    cases = {}
    for page in manifest['pages']:
        html = (CORPUS / f"{page['name']}.html").read_text()
        for limit in MAX_CHARS:
            scope = {}
            text = extract_html_text(html, limit, page['url'], scope=scope)
            document = scope.pop('document_text', None)
            cases[f"{page['name']}:{limit}"] = {
                'text': text,
                'scope': scope,
                'document_text_sha256': (hashlib.sha256(document.encode()).hexdigest()
                                         if document is not None else None),
            }
    return {'versions': versions(), 'cases': cases}


def main():
    golden = extract_all()
    data = (json.dumps(golden, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            + '\n').encode()
    if '--check' in sys.argv:
        recorded = json.loads(GOLDEN.read_text())
        if recorded != golden:
            sys.stderr.write('golden outputs differ from the current implementation\n')
            return 1
        return 0
    GOLDEN.write_bytes(data)
    sys.stdout.write(f"{len(golden['cases'])} cases, {len(data)} bytes, {golden['versions']}\n")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
