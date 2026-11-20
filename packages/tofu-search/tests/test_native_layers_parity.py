"""Native URL, authority, citation and HTML-extraction kernels match Python.

Each replaced name keeps its Python body in the owning module's
``PYTHON_KERNELS``. This suite runs both over hand-picked edge cases, every
link of the captured real-page corpus (``fixtures/html_corpus``) and seeded
random inputs built to reach parser recovery paths: unterminated tags, quotes,
comments and references, IDNA/IPv6/port validation, BibTeX quirks and
``re.IGNORECASE`` folds. Skipped when the extension is absent; the corpus
golden test (``test_html_extraction_corpus.py``) covers the Python path alone.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tofu_search import _native_kernels
from tofu_search.fetch import html_extract
from tofu_search.search import authority, dedup
from tofu_search.verify import parse

native = _native_kernels.native
pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(native is None, reason='native extension not built')]

CORPUS_DIR = Path(__file__).resolve().parent / 'fixtures' / 'html_corpus'


def corpus_pages():
    manifest = json.loads((CORPUS_DIR / 'manifest.json').read_text())
    for page in manifest['pages']:
        html = (CORPUS_DIR / f"{page['name']}.html").read_text()
        yield page, html


# ── generators (also used by the large offline randomized run) ─────────────────

_SCHEMES = ['http', 'https', 'HTTP', 'Https', 'ftp', 'mailto', '', 'javascript', 'git+ssh', '1x']
_HOSTS = [
    'example.com', 'EXAMPLE.com.', 'a..b.com', 'xn--bcher-kva.de', 'bücher.de', '[::1]',
    '[::ffff:1.2.3.4]', '[1.2.3.4]', '[v1.x]', '[vZ.x]', '[bad', 'bad]', '1.2.3.4', '01.2.3.4',
    '999.1.1.1', 'user:pw@h.com', 'u@[::1]:8443', 'h.com:8080', 'h.com:80', 'h.com:443',
    'h.com:0', 'h.com:99999', 'h.com:٣', 'h.com:', '\u212a.com', 'İ.com', '\u2100.com',
    'docs.foo.io', 'top10vpn.com', 'reviews.co.uk', 'aws.amazon.com', 'www.github.com',
    'nvd.nist.gov', 'a' * 64 + '.com', 'b' * 63 + '.org', 'x.' * 130 + 'com', '-bad-.com',
    'under_score.com', 'dev.to', 'learn.microsoft.com', 'cheap-flights.co.jp', 'h%41.com',
    '[fe80::1%25eth0]', '[::1%]', '[1:2:3:4:5:6:7:8]', '[1::2::3]', '[12345::]', 'Ünïcode.ΕΛ',
]
_PATHS = ['', '/', '/a/b/', '/%7euser/%2f%zz/', '/a;b=c/d;e', '/./x/../y', '//double',
          '/中文/路径', '/p%E4%B8%AD', '/trailing///', '/a b', '/\t/x\n/y']
_QUERIES = ['', '?', '?a=1&b=2', '?utm_source=x&Utm_Medium=y&fbclid=z&q=keep', '?b&&a=',
            '?x=%2B+y&x=1', '?=v', '?k=%FF%FE', '?a=1#frag', '?q=中文&Q=Ä', '?gclid=1&_hsenc=2']
_FRAGMENTS = ['', '#', '#top', '#a?b', '#中']
_WRAPPERS = ['', ' ', '\x00\x1f ', '\t', '  \u3000', '\n']


def random_url(rng: random.Random) -> str:
    scheme = rng.choice(_SCHEMES)
    head = f'{scheme}:' if scheme else ''
    slashes = rng.choice(['//', '//', '/', '', '///'])
    url = (head + slashes + rng.choice(_HOSTS) + rng.choice(_PATHS) + rng.choice(_QUERIES)
           + rng.choice(_FRAGMENTS))
    if rng.random() < 0.2:
        cut = rng.randint(0, len(url))
        url = url[:cut] + rng.choice('[]@:%#?/\t \u2100') + url[cut:]
    return rng.choice(_WRAPPERS) + url + rng.choice(_WRAPPERS)


def corpus_urls() -> list[str]:
    urls = set()
    for page, html in corpus_pages():
        urls.add(page['url'])
        urls.update(m.group(1) for m in re.finditer(r'href="([^"]{1,300})"', html))
    return sorted(urls)


_BIB_TYPES = ['article', 'Misc', 'INPROCEEDINGS', 'comment', 'string', 'preamble', 'book', 'x_y']
_BIB_FIELDS = ['title', 'author', 'editor', 'year', 'date', 'doi', 'eprint', 'archivePrefix',
               'url', 'note', 'journal', 'howpublished', 'pages', 'Title', 'bad name', '-x']
_BIB_VALUES = [
    'Attention Is All You Need', '{Nested {Braces} here}', 'A. Vaswani and N. Shazeer and  X',
    'Smith, John AND Doe, Jane', 'Li\u3000and\u3000Wang', '2017', '2019-05-01', 'May 2020',
    '10.1145/3292500.3330701).', 'doi:10.1000/XYZ_1;', 'https://doi.org/10.48550/arXiv.1706.03762',
    'arXiv', 'arxiv', '1706.03762', 'hep-th/9901001v2', 'math.AG/0509025', 'ARXIV: 2301.12345v3',
    'arxiv:hep-ph/0101001', 'Proc. \\& Co', 'a\\{b', '١٢٣٤.٥٦٧٨', 'DOİ: 10.1234/ıştanbul',
    'https://arxiv.org/abs/2301.00001', 'Über Straße', '', '  ', 'x,y',
]


def random_bibtex(rng: random.Random) -> str:
    out = []
    for _ in range(rng.randint(1, 6)):
        if rng.random() < 0.2:
            out.append(rng.choice(['junk @ text ', '@{', '@x {', 'email@host.com ', '%c\n']))
        fields = []
        for _ in range(rng.randint(0, 7)):
            value = rng.choice(_BIB_VALUES)
            form = rng.random()
            if form < 0.6:
                rendered = '{' + value + '}'
            elif form < 0.75:
                rendered = '"' + value + '"'
            elif form < 0.9:
                rendered = value.replace(',', '')
            else:
                rendered = '{' + value
            equals = rng.choice(['=', ' = ', '\t=\n'])
            fields.append(f'{rng.choice(_BIB_FIELDS)}{equals}{rendered}')
        body = rng.choice(['key1', 'Vaswani2017', '', ' spaced ']) + ','
        body += rng.choice([',', ',\n', ', ']).join(fields)
        closer = rng.choice(['}', '}', '}', '', '}}'])
        gap = rng.choice(['', ' ', '\n'])
        out.append(f'@{rng.choice(_BIB_TYPES)}{gap}{{{body}{closer}\n')
    return ''.join(out)


_REF_PIECES = [
    '[1] ', '[12]\t', '\n[3]  ', '1. ', '\n  2. ', '\n٣. ', '10.', '\n\n', '\n \n', '\n',
    'A. Author, “A Sufficiently Long Title,” ', '"Short" ', '"Another Quoted Title Here" ',
    'Proc. 2019. ', 'in 1999, ', 'doi:10.1000/xyz123. ', 'arXiv:1706.03762v5 ', 'hep-th/9901001 ',
    'see https://doi.org/10.1145/1.2 ', 'J. Doe et al. ', '20201 ', '١٩٩٩ ', 'x ',
]


def random_references(rng: random.Random) -> str:
    return ''.join(rng.choice(_REF_PIECES) for _ in range(rng.randint(1, 25)))


_ID_PIECES = [
    '10.1145/3292500.3330701', '10.1145/3292500.3330701.', 'doi: 10.1234/ABC)', 'DOI:10.1/x',
    '10.12/no', 'arXiv:1706.03762', 'ARXIV 2301.123456', 'arxiv: hep-th/9901001v2', 'arxiv:',
    '1706.03762', 'math.AG/0509025', 'ARX\u0130V:2101.00001', 'arxıv:2101.00002', 'doı:10.5555/q',
    '١٢٣٤.١٢٣٤٥', 'v2', ' ', '\n', '. ', 'text', '中文', 'Straße', '(', ')',
]


def random_identifier_text(rng: random.Random) -> str:
    return ''.join(rng.choice(_ID_PIECES) for _ in range(rng.randint(1, 30)))


_TAGS = ['p', 'div', 'main', 'article', 'section', 'nav', 'header', 'footer', 'aside', 'form',
         'span', 'a', 'button', 'table', 'tr', 'td', 'th', 'pre', 'code', 'noscript', 'template',
         'svg', 'iframe', 'xmp', 'title', 'textarea', 'body', 'html', 'head', 'h1', 'h2', 'ul',
         'li', 'dl', 'dt', 'dd', 'ruby', 'rt', 'rp', 'Div', 'TD', 'x-widget', 'select', 'input']
_VOIDS = ['br', 'img', 'hr', 'input', 'meta', 'BR', 'wbr']
_ATTRS = [
    'class="site-nav x"', "class='content main'", 'class="pt-[calc(10rem+var(--banner-height))]"',
    'class=coo\u212aie-consent', 'class=""', 'class="  sidebar  "', 'id=content', 'id="Main-x"',
    'id=root', 'id="__next"', 'role=main', 'role="Main"', 'hidden', 'aria-hidden=" TRUE "',
    'style="color:red; DISPLAY : none"', 'style=display:block', 'href="/docs/getting-started"',
    'href="https://other.org/a"', "href='#top'", 'href="javascript:void(0)"', 'href="mailto:x"',
    'href="//[bad"', 'href="../up/two-words_here"', 'href="?q=1"', 'href=" spaced "', 'href=',
    'href="x?y=&amp;z"', 'data-reactroot', 'ng-app=""', 'ng-version="1"', 'a="unterminated',
    "b='also", '=odd', 'c = "spaced"', 'class="a" class="nav"', 'x=y/', 'title="&lt;&gt;&notit;"',
]
_TEXTS = [
    'Hello world', 'Getting started guide', 'Lorem ipsum dolor sit amet, consectetur adipiscing.',
    'Loading...', '正在加载', 'Please enable JavaScript to continue', '请启用 JavaScript',
    '&amp; &ampx &#65 &#x41; &#128; &#0; &#xD800; &#1114112; &nbsp &notit; &unknown; &',
    '&#', '&#;', '&#x;', 'a < b', '< p', '</>', '</ p>', '</3>', '<!-- c -->', '<!--->', '<!-->',
    '<!---- x --!>', '<!DOCTYPE html>', '<!doctype HTML PUBLIC "x">', '<![CDATA[cdata <b>]]>',
    '<![if gte IE 9]>', '<![endif]>', '<?xml version="1.0"?>', '<!bogus>', '<a"b>', '<p =x>',
    '\u3000full\u00a0width ', '   ', '\n\n', '\r\n', 'x|y', '```code```', 'Home', 'Top', '→',
    'İstanbul ΟΔΟΣ Straße 中文搜索 日本語', 'Copy: npm install tofu', 'cell value',
]


def random_html(rng: random.Random) -> str:
    out = []
    for _ in range(rng.randint(5, 220)):
        roll = rng.random()
        if roll < 0.32:
            attrs = ' '.join(rng.choice(_ATTRS) for _ in range(rng.randint(0, 3)))
            closer = rng.choice(['>', '>', '>', '/>', ' >', '', '\n>'])
            out.append(f'<{rng.choice(_TAGS)}{" " if attrs else ""}{attrs}{closer}')
        elif roll < 0.55:
            out.append(f'</{rng.choice(_TAGS)}{rng.choice([">", ">", " >", ""])}')
        elif roll < 0.62:
            out.append(f'<{rng.choice(_VOIDS)}{rng.choice([">", "/>"])}')
        elif roll < 0.64:
            out.append(rng.choice(['<script>if (a<b && c) {}</script>', '<style>p{}</style>',
                                   '<script>unterminated', '<plaintext>rest <b>raw',
                                   '<title>T &amp; t</title>', '<textarea> a &lt; </textarea>']))
        else:
            out.append(rng.choice(_TEXTS))
        out.append(rng.choice(['', '', ' ', '\n']))
    html = ''.join(out)
    if rng.random() < 0.25:
        html = html[:rng.randint(0, len(html))]
    return html


def mutate_page(rng: random.Random, pages: list[str]) -> str:
    """A real page cut and spliced at arbitrary offsets (mid-tag, mid-quote)."""
    html = rng.choice(pages)
    for _ in range(rng.randint(1, 4)):
        other = rng.choice(pages)
        at, start = rng.randint(0, len(html)), rng.randint(0, len(other))
        html = html[:at] + other[start:start + rng.randint(0, 4000)] + html[at + rng.randint(0, 2000):]
    return html[:rng.randint(len(html) // 2, len(html))]


_BASES = ['https://example.com/x/y.html', 'http://h.com', '', 'https://[bad', 'ftp://f/a/',
          'https://docs.example.org/en/latest/']
_MAIN_TEXTS = [None, '', 'short', 'Hello world, this is a fairly long paragraph of content text.',
               '| cell value |', 'Getting started guide\n\nCopy: npm install tofu']


def random_extraction_args(rng: random.Random):
    return (random_html(rng), rng.choice(_MAIN_TEXTS), rng.choice(_BASES),
            rng.choice([None, 0, 40, 4000, -10]), rng.random() < 0.5)


# ── oracle context ──────────────────────────────────────────────────────────

@contextlib.contextmanager
def python_oracle():
    """Run the oracles with every kernel they call also pure Python."""
    with pytest.MonkeyPatch.context() as patch:
        for module in (authority, dedup, parse, html_extract):
            for name, function in module.PYTHON_KERNELS.items():
                patch.setattr(module, name, function)
        yield


def _rows(citations):
    return [c.to_dict() for c in citations]


# ── tests ────────────────────────────────────────────────────────────────────

URLS = corpus_urls()


def test_public_names_bind_native_wrappers():
    for module, names in ((dedup, ['canonical_url_key']),
                          (authority, ['classify_authority', 'host_brand_labels', 'brand_tokens']),
                          (parse, ['parse_bibtex', 'parse_references', 'extract_identifiers',
                                   'extract_citations_from_text'])):
        for name in names:
            assert getattr(module, name) is not module.PYTHON_KERNELS[name]
            assert getattr(module, name).__wrapped__ is module.PYTHON_KERNELS[name]
    assert html_extract._assemble_document is not html_extract.PYTHON_KERNELS['_assemble_document']
    assert len(URLS) > 3000


def test_canonical_url_key():
    rng = random.Random(1)
    inputs = URLS + [random_url(rng) for _ in range(4000)] + ['', None, '   ', 'x\ud800y']
    oracle = dedup.PYTHON_KERNELS['canonical_url_key']
    for url in inputs:
        assert dedup.canonical_url_key(url) == oracle(url), repr(url)


def test_authority_classifier():
    rng = random.Random(2)
    inputs = URLS + [random_url(rng) for _ in range(4000)]
    queries = ['', 'amazon s3 pricing', 'cloudflare workers', 'Python re module', 'İstanbul',
               'GitHub actions', 'top10 vpn', 'docs', 'x\ud800']
    deferred = 0
    for url in inputs:
        query = rng.choice(queries)
        with python_oracle():
            expected = (authority.classify_authority(url, query),
                        authority.host_brand_labels(url), authority.brand_tokens(query))
        if '\ud800' not in url + query and native.classify_authority(url, query) is None:
            deferred += 1
            host = urlsplit(url).hostname or ''
            assert not host.removesuffix('.').isascii(), url
        assert (authority.classify_authority(url, query), authority.host_brand_labels(url),
                authority.brand_tokens(query)) == expected, (url, query)
        assert authority.authority_label(url, query) == authority._LABEL[expected[0]]
    # Only non-ASCII hosts (Python's IDNA 2003 codec) keep the oracle.
    assert 0 < deferred < len(inputs) // 2


def test_citation_parsing():
    rng = random.Random(3)
    samples = {
        'parse_bibtex': [random_bibtex(rng) for _ in range(2500)] + ['', '@comment{x}'],
        'parse_references': [random_references(rng) for _ in range(2500)] + ['', ' \n '],
        'extract_citations_from_text': [random_identifier_text(rng) for _ in range(2500)],
    }
    for name, texts in samples.items():
        for text in texts:
            with python_oracle():
                expected = _rows(getattr(parse, name)(text))
            assert _rows(getattr(parse, name)(text)) == expected, (name, text)
    for text in samples['extract_citations_from_text'] + ['', 'x\ud800 10.1234/abcd']:
        with python_oracle():
            expected = parse.extract_identifiers(text)
        assert parse.extract_identifiers(text) == expected, text
    assert parse.extract_identifiers(None) == ('', '')


def _corpus_extraction_args():
    """``_assemble_document`` arguments exactly as the extractor builds them."""
    calls = []

    def spy(*args):
        calls.append(args)
        return html_extract.PYTHON_KERNELS['_assemble_document'](*args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(html_extract, '_assemble_document', spy)
        for page, html in corpus_pages():
            for limit in (0, 4000):
                html_extract.extract_html_text(html, limit, page['url'], scope={})
    return calls


def test_html_extraction_real_pages():
    calls = _corpus_extraction_args()
    # Every page but the GitHub code blob (returned before assembly), twice.
    assert len(calls) >= 2 * 60
    oracle = html_extract.PYTHON_KERNELS['_assemble_document']
    for html, main_text, url, max_chars, want_rendering in calls:
        variants = [(main_text, max_chars, want_rendering), (None, max_chars, True),
                    (main_text, -25, False), (main_text or 'x', 600, True)]
        for text, limit, rendering in variants:
            expected = oracle(html, text, url, limit, rendering)
            got = native.extract_document(html, text, url, limit, rendering)
            assert got == expected, (url, limit, rendering, text is None)


def test_html_extraction_random_documents():
    rng = random.Random(4)
    oracle = html_extract.PYTHON_KERNELS['_assemble_document']
    for _ in range(1500):
        args = random_extraction_args(rng)
        assert native.extract_document(*args) == oracle(*args), args
    pages = [html for _, html in corpus_pages()]
    for _ in range(40):
        args = (mutate_page(rng, pages), rng.choice(_MAIN_TEXTS), rng.choice(_BASES),
                rng.choice([None, 0, 4000]), rng.random() < 0.5)
        assert native.extract_document(*args) == oracle(*args), args[1:]
    # Surrogates and non-str documents keep the oracle path.
    assert html_extract._assemble_document('<p>x\ud800</p>' * 9, None, '', 0, False) == oracle(
        '<p>x\ud800</p>' * 9, None, '', 0, False)
