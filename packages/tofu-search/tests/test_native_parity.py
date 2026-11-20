"""Native kernels are bit-identical to their Python oracles.

``tofu_search._native`` (Rust, ``rust/``) replaces the CPU-bound text kernels
when installed. Every replaced public name keeps its Python body in the owning
module's ``PYTHON_KERNELS``; this suite runs both over a corpus and seeded
random text designed to reach Unicode class, case-mapping, normalization,
notation, chunking and scoring edges. Skipped when the extension is absent (a
pure build or a source checkout); the native evidence run installs it.
"""

from __future__ import annotations

import contextlib
import random
from pathlib import Path

import pytest

from tofu_search import _native_kernels, lexical, passages
from tofu_search.search import dedup, rerank

native = _native_kernels.native
pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(native is None, reason='native extension not built')]

_FIXTURES = Path(__file__).resolve().parent / 'fixtures'

_ALPHABET = (
    'abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789 '
    '_-+#()*/^!.,:;"\'?[]{}<>@$%&=~`|\\ \t\n\n\r\x0b\x0c\x1c\xa0\u2003\u3000'
    'ÀÉÎõüßẞſİıΣσςΟΔΩωθΘ ﬁﬂ①²³½ⅫＡＢＣ１２３ｶﾞ'
    '中文搜索引擎测试日本語のテキスト・カタカナ한국어텍스트゠'
    'हिन्दी ा ि ् русский текст العربية ‍ \u200b\u0301e\u0301'
    '\U0001F600\U00020000\U0002A700'
)

_PHRASES = [
    'O(n log n)', 'o( n + 1 )', 'Θ(n^2)', 'ω (1)', 'O(', 'C++ and C#', 'c#_x', 'f#a',
    'counter++', 'x++y', 'site:docs.rs', '-site:x.com', '"exact phrase"', 'intitle:"a b"',
    '+"plus quoted"', 'foo:"bar baz"', '(OR)', 'AND', 'NOT', 'ſite:x', 'İnurl:y',
    'LANGUAGE:en', '"unterminated', 'ΟΔΟΣ', 'Straße', 'rust 1.80 release',
    '--- Page Links ---', 'https://example.com/a?utm_source=x', 'GPT-4o', 'RFC 9110',
]


def _random_text(rng: random.Random, length: int) -> str:
    parts = []
    while sum(map(len, parts)) < length:
        if rng.random() < 0.3:
            parts.append(rng.choice(_PHRASES))
        else:
            parts.append(''.join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 12))))
        parts.append(rng.choice([' ', ' ', '\n', '\n\n', '\n \n\n', '. ']))
    return ''.join(parts)


def _corpus() -> list[str]:
    rng = random.Random(20261118)
    texts = list(_PHRASES) + ['', ' ', '\n\n', 'a', '中', '中文', '中文字']
    texts += [_random_text(rng, rng.choice([20, 80, 400, 3000])) for _ in range(250)]
    for path in sorted(_FIXTURES.rglob('*')):
        if path.is_file() and path.suffix in {'.html', '.txt', '.md', '.json'}:
            texts.append(path.read_text(encoding='utf-8', errors='replace')[:60_000])
    return texts


CORPUS = _corpus()
QUERIES = ['O(n log n) sort', 'C++ templates', 'rust site:docs.rs "async trait"', '中文 搜索',
           'Σ ΟΔΟΣ', 'straße', '', 'the', 'GPT-4o release -rumor', 'intitle:x 1.80',
           'lorem ipsum', 'example', 'テキスト', 'текст', 'counter++ c#']


@contextlib.contextmanager
def _python_oracle():
    """Run the oracles with every kernel they call also pure Python."""
    with pytest.MonkeyPatch.context() as patch:
        for name, function in lexical.PYTHON_KERNELS.items():
            patch.setattr(lexical, name, function)
        patch.setattr(passages, 'tokenize', lexical.PYTHON_KERNELS['tokenize'])
        patch.setattr(passages, 'query_subject', lexical.PYTHON_KERNELS['query_subject'])
        patch.setattr(rerank, '_tokenize', lexical.PYTHON_KERNELS['tokenize'])
        yield


def test_unicode_tables_match_this_interpreter():
    import unicodedata
    assert native.UNIDATA_VERSION == unicodedata.unidata_version


@pytest.mark.parametrize('name', ['tokenize', 'matching_text', 'query_subject', 'subject_terms'])
def test_lexical_kernels(name):
    oracle = lexical.PYTHON_KERNELS[name]
    kernel = getattr(native, name)
    assert getattr(lexical, name) is kernel
    with _python_oracle():
        for text in CORPUS + QUERIES:
            assert kernel(text) == oracle(text), text[:200]
    assert kernel(None) == oracle(None)


def test_lexical_random_unicode_codepoints():
    rng = random.Random(7)
    with _python_oracle():
        for _ in range(400):
            text = ''.join(chr(rng.choice([rng.randint(0x20, 0x2FF), rng.randint(0x300, 0xFFFF),
                                           rng.randint(0x10000, 0x10FFFF)]))
                           for _ in range(rng.randint(1, 40)))
            text = text.encode('utf-8', 'replace').decode('utf-8', 'replace')
            if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
                continue
            for name in ('tokenize', 'matching_text', 'query_subject'):
                assert getattr(native, name)(text) == lexical.PYTHON_KERNELS[name](text), text


@pytest.mark.parametrize('max_chars', [0, 1, 10, 25, 120, 400, 1_500, 7_999])
def test_select_relevant_passages(max_chars):
    oracle = passages.PYTHON_KERNELS['select_relevant_passages']
    assert passages.select_relevant_passages is native.select_relevant_passages
    rng = random.Random(max_chars)
    with _python_oracle():
        for text in CORPUS:
            query = rng.choice(QUERIES)
            options = {'chunk_chars': rng.choice([1, 80, 200, 1_200]),
                       'include_lead': rng.random() < 0.4,
                       'fallback_edges': rng.random() < 0.4}
            expected = oracle(text, query, max_chars, **options)
            assert native.select_relevant_passages(text, query, max_chars, **options) == expected, (
                text[:120], query, options)


def test_dedup_shingles_and_decisions():
    with _python_oracle():
        for text in CORPUS:
            assert native.text_shingles(text) == dedup.PYTHON_KERNELS['_text_to_shingles'](text)
            assert native.full_text_shingles(text) == dedup.PYTHON_KERNELS['_full_text_shingles'](text)
        rng = random.Random(3)
        for threshold in (0.0, 0.2, 0.45, 0.78, 1.0):
            texts = [rng.choice(CORPUS)[:rng.randint(0, 500)] for _ in range(60)]
            texts += texts[:20]
            for name in ('_group_indices', '_full_content_duplicates'):
                assert getattr(dedup, name)(texts, threshold) == dedup.PYTHON_KERNELS[name](
                    texts, threshold)
        # Long bodies take the head/tail window; edited mirrors straddle the
        # threshold so both decisions are exercised.
        bodies = [_random_text(rng, 40_000) for _ in range(4)]
        long_texts = []
        for body in bodies:
            long_texts += [body, body[:20_000] + 'edited ' * 300 + body[20_000:],
                           body[: len(body) // 2] + _random_text(rng, 20_000)]
        for text in long_texts:
            assert native.full_text_shingles(text) == dedup.PYTHON_KERNELS[
                '_full_text_shingles'](text)
        for threshold in (0.3, 0.78, 0.95):
            expected = dedup.PYTHON_KERNELS['_full_content_duplicates'](long_texts, threshold)
            assert native.full_content_duplicates(long_texts, threshold) == expected
        assert any(d is not None for d in native.full_content_duplicates(long_texts, 0.3))


def test_bm25_scores_are_bit_identical():
    rng = random.Random(11)
    with _python_oracle():
        for query in QUERIES:
            terms = list(set(lexical.PYTHON_KERNELS['tokenize'](
                lexical.PYTHON_KERNELS['query_subject'](query))))
            docs = [rng.choice(CORPUS)[:rng.randint(0, 4_000)] for _ in range(rng.randint(0, 30))]
            assert rerank._bm25_scores(terms, docs) == rerank.PYTHON_KERNELS['_bm25_scores'](
                terms, docs)
