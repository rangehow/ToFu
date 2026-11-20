"""Benchmark the native kernels against the pure-Python package on its own workloads.

Each arm runs in a fresh interpreter: ``python`` blocks ``tofu_search._native``
(the package exactly as the pure wheel ships it), ``native`` requires it, and
``previous`` loads it but restores the Python URL, authority, citation and HTML
assembly layers (the native kernel set before those layers were ported). The
workload is the post-fetch CPU stage of ``search()``: snippet grouping,
full-content dedup, BM25 rerank (whose documents are query passages of each
body) and relevance annotation, plus the kernels in isolation. Bodies are the
repository's tracked Markdown documents read as fetched pages. The extraction
workloads read the captured real-page corpus (``tests/fixtures/html_corpus``)
through ``extract_html_text``; trafilatura's own article pass stays Python in
every arm. No network, model or browser is involved, so these are local CPU
costs, not search quality.

Usage: python3 bench_native.py [--repeat N] [--json OUT]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = PACKAGE_DIR.parents[1]
QUERIES = [
    'storage authority restart recovery',
    'resource control hardware never limits work',
    'tofu-search fetch budget deadline',
    'TofuDB compaction checkpoint',
    'browser extension proxy route',
    '会话 存储 重启',
]
POOL_SIZE = 20


def _corpus() -> list[str]:
    listed = subprocess.run(['git', 'ls-files', 'docs/*.md'], cwd=REPO_DIR, check=True,
                            capture_output=True, text=True).stdout.split()
    # Typical extracted pages are tens of KB; take the documents nearest 40 KB.
    paths = sorted(listed, key=lambda p: (abs((REPO_DIR / p).stat().st_size - 40_000), p))
    return [(REPO_DIR / p).read_text(encoding='utf-8') for p in paths[:POOL_SIZE]]


def _html_pages() -> list[tuple[str, str]]:
    corpus = PACKAGE_DIR / 'tests' / 'fixtures' / 'html_corpus'
    manifest = json.loads((corpus / 'manifest.json').read_text())
    return [(page['url'], (corpus / f"{page['name']}.html").read_text())
            for page in manifest['pages']]


def _bibliography(bodies: list[str]) -> tuple[str, str]:
    """A deterministic 200-entry .bib and the matching freeform reference list."""
    words = [w for w in re.findall(r'[A-Za-z]{4,}', ' '.join(bodies)) if w.isascii()]
    entries, refs = [], []
    for i in range(200):
        title = ' '.join(words[(i * 7 + k) % len(words)] for k in range(8)).title()
        authors = ' and '.join(f'{words[(i + k) % len(words)].title()}, A.' for k in range(3))
        year = 1990 + i % 35
        arxiv = f'{2100 + i % 99:04d}.{10000 + i:05d}'
        if i % 3 == 0:
            ident = f'doi = {{10.{1000 + i}/bench.{i}}}'
        elif i % 3 == 1:
            ident = f'eprint = {{{arxiv}}}, archivePrefix = {{arXiv}}'
        else:
            ident = f'url = {{https://example.org/papers/{i}}}'
        entries.append(f'@article{{key{i},\n  title = {{{title}}},\n  author = {{{authors}}},\n'
                       f'  journal = {{Bench Journal}},\n  year = {{{year}}},\n  {ident}\n}}')
        cited = f'doi:10.{1000 + i}/bench.{i}' if i % 2 else f'arXiv:{arxiv}'
        refs.append(f'[{i + 1}] {authors.replace(" and ", ", ")}. \u201c{title},\u201d '
                    f'Bench Journal, {year}. {cited}')
    return '\n\n'.join(entries), '\n'.join(refs)


def _pool(bodies: list[str]) -> list[dict]:
    rows = []
    for index, body in enumerate(bodies):
        title = body.split('\n', 1)[0].lstrip('# ')
        rows.append({'url': f'https://example{index % 7}.org/doc/{index}', 'title': title,
                     'snippet': body[200:420], 'source': 'bench', 'rrf_score': 1 / (60 + index),
                     'full_content': body})
    return rows


def _timed(function, repeat: int) -> dict:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        function()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {'median_ms': round(statistics.median(samples), 3),
            'p90_ms': round(samples[min(len(samples) - 1, int(len(samples) * 0.9))], 3),
            'samples': repeat}


def _threads(function, count: int) -> None:
    workers = [threading.Thread(target=function) for _ in range(count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _threads_each(function, chunks) -> None:
    workers = [threading.Thread(target=function, args=(chunk,)) for chunk in chunks]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _restore_python_layers() -> None:
    """The ``previous`` arm: native text kernels, Python layers ported later."""
    from tofu_search.fetch import html_extract
    from tofu_search.search import authority, candidates, dedup, rerank
    from tofu_search.verify import parse
    for module in (authority, parse, html_extract):
        for name, function in module.PYTHON_KERNELS.items():
            setattr(module, name, function)
    dedup.canonical_url_key = dedup.PYTHON_KERNELS['canonical_url_key']
    for name in ('brand_tokens', 'classify_authority', 'host_brand_labels'):
        setattr(rerank, name, authority.PYTHON_KERNELS[name])
    candidates.canonical_url_key = dedup.PYTHON_KERNELS['canonical_url_key']


def _arm(mode: str, repeat: int) -> dict:
    if mode == 'python':
        sys.modules['tofu_search._native'] = None
    sys.path.insert(0, str(PACKAGE_DIR))
    import tofu_search  # noqa: F401
    from tofu_search import _native_kernels
    if mode == 'previous':
        _restore_python_layers()
    from tofu_search.fetch import html_extract
    from tofu_search.fetch.html_extract import extract_html_text
    from tofu_search.lexical import tokenize
    from tofu_search.passages import select_relevant_passages
    from tofu_search.search import authority, dedup
    from tofu_search.search.dedup import dedup_by_full_content, group_by_content
    from tofu_search.search.quality import annotate_relevance
    from tofu_search.search.rerank import rerank_by_bm25
    from tofu_search.verify import parse

    assert (_native_kernels.native is None) == (mode == 'python'), mode
    bodies = _corpus()
    pages = _html_pages()
    links = sorted({m.group(1) for _, html in pages
                    for m in re.finditer(r'href="([^"]{1,300})"', html)})
    bibtex, references = _bibliography(bodies)
    assembly_args = []
    assemble = html_extract._assemble_document

    def spy(*args):
        assembly_args.append(args)
        return assemble(*args)

    html_extract._assemble_document = spy
    for url, html in pages:
        extract_html_text(html, 0, url, scope={})
    html_extract._assemble_document = assemble
    heavy = max(3, repeat // 3)
    rss_ready = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def post_fetch(query):
        rows = [dict(row) for row in _pool(bodies)]
        groups = group_by_content(rows)
        kept = dedup_by_full_content([group[0] for group in groups])
        ranked = rerank_by_bm25(query, kept, 10)
        for row in ranked:
            annotate_relevance(row, query)
        return ranked

    def post_fetch_with_extraction(index, query):
        # One search: 16 fetched pages extracted, keyed, ranked and labelled.
        chosen = [pages[(index * 16 + k) % len(pages)] for k in range(16)]
        rows, seen = [], set()
        for k, (url, html) in enumerate(chosen):
            text = extract_html_text(html, 0, url, scope={}) or ''
            key = dedup.canonical_url_key(url)
            if key in seen:
                continue
            seen.add(key)
            rows.append({'url': url, 'title': text.split('\n', 1)[0][:120],
                         'snippet': text[200:420], 'source': 'bench',
                         'rrf_score': 1 / (60 + k), 'full_content': text})
        groups = group_by_content(rows)
        kept = dedup_by_full_content([group[0] for group in groups])
        ranked = rerank_by_bm25(query, kept, 10)
        for row in ranked:
            annotate_relevance(row, query)
            row['label'] = authority.authority_label(row['url'], query)
        return ranked

    def extract_pages(chunk):
        for url, html in chunk:
            extract_html_text(html, 0, url, scope={})

    results = {
        'canonical_url_key_corpus_links': _timed(
            lambda: [dedup.canonical_url_key(u) for u in links], repeat),
        'classify_authority_corpus_links': _timed(
            lambda: [authority.classify_authority(u, QUERIES[1]) for u in links], repeat),
        'parse_bibtex_200_entries': _timed(lambda: parse.parse_bibtex(bibtex), repeat),
        'parse_references_200': _timed(lambda: parse.parse_references(references), repeat),
        'extract_citations_docs_pool': _timed(
            lambda: [parse.extract_citations_from_text(b) for b in bodies], repeat),
        'html_assembly_64_pages': _timed(
            lambda: [html_extract._assemble_document(*a) for a in assembly_args], heavy),
        'extract_html_text_64_pages': _timed(
            lambda: [extract_html_text(h, 0, u, scope={}) for u, h in pages], heavy),
        'post_fetch_with_extraction_6_queries': _timed(
            lambda: [post_fetch_with_extraction(i, q) for i, q in enumerate(QUERIES)], heavy),
        # Eight caller threads extracting 8 pages each: the trafilatura pass
        # stays serialized by its lxml lock in every arm.
        'extract_8_threads_wall': _timed(lambda: _threads_each(
            extract_pages, [pages[k * 8:(k + 1) * 8] for k in range(8)]), heavy),
        'tokenize_pool': _timed(lambda: [tokenize(b) for b in bodies], repeat),
        'passages_pool_8k': _timed(lambda: [select_relevant_passages(b, QUERIES[0], 8_000)
                                            for b in bodies], repeat),
        'group_by_content': _timed(lambda: group_by_content(_pool(bodies)), repeat),
        'dedup_by_full_content': _timed(lambda: dedup_by_full_content(_pool(bodies)), repeat),
        'rerank_by_bm25': _timed(lambda: rerank_by_bm25(QUERIES[1], _pool(bodies), 10), repeat),
        'post_fetch_stage_all_queries': _timed(lambda: [post_fetch(q) for q in QUERIES], repeat),
        # Eight concurrent rerank calls on caller threads: the native kernels
        # release the GIL, the Python ones cannot.
        'rerank_8_threads_wall': _timed(lambda: _threads(
            lambda: rerank_by_bm25(QUERIES[1], _pool(bodies), 10), 8), repeat),
    }
    output = [[row['url'] for row in post_fetch(q)] for q in QUERIES]
    extracted = hashlib.sha256('\x00'.join(
        extract_html_text(h, 0, u, scope={}) or '' for u, h in pages).encode()).hexdigest()
    searched = [[(row['url'], row['label']) for row in post_fetch_with_extraction(i, q)]
                for i, q in enumerate(QUERIES)]
    return {
        'mode': mode,
        'native': repr(_native_kernels.native),
        'pool_chars': sum(map(len, bodies)),
        'html_pages': len(pages),
        'html_chars': sum(len(h) for _, h in pages),
        'links': len(links),
        'timings': results,
        'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        'workload_rss_growth_mib': round(
            (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_ready) / 1024, 1),
        'ranked_urls': output,
        'extracted_sha256': extracted,
        'searched': searched,
        'citations': [[c.to_dict() for c in parse.parse_bibtex(bibtex)],
                      [c.to_dict() for c in parse.parse_references(references)]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat', type=int, default=15)
    parser.add_argument('--json')
    parser.add_argument('--arm', choices=['python', 'previous', 'native'])
    args = parser.parse_args()
    if args.arm:
        json.dump(_arm(args.arm, args.repeat), sys.stdout)
        return 0
    arms = {}
    load_before = os.getloadavg()[0]
    for mode in ('python', 'previous', 'native'):
        env = dict(os.environ, PYTHONHASHSEED='0')
        completed = subprocess.run(
            [sys.executable, __file__, '--arm', mode, '--repeat', str(args.repeat)],
            check=True, capture_output=True, text=True, env=env)
        arms[mode] = json.loads(completed.stdout)
    py, prev, nat = arms['python'], arms['previous'], arms['native']
    same = all(py[key] == prev[key] == nat[key]
               for key in ('ranked_urls', 'extracted_sha256', 'searched', 'citations'))
    for arm in arms.values():
        del arm['citations']
    report = {
        'arms': arms,
        'identical_outputs': same,
        'load_avg_1m': [round(load_before, 1), round(os.getloadavg()[0], 1)],
        'speedup_median': {name: round(py['timings'][name]['median_ms']
                                       / nat['timings'][name]['median_ms'], 2)
                           for name in py['timings']},
        'speedup_vs_previous_native': {name: round(prev['timings'][name]['median_ms']
                                                   / nat['timings'][name]['median_ms'], 2)
                                       for name in py['timings']},
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    sys.stdout.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
