"""Fixed slides-benchmark corpus: identity, private data root, inputs per arm."""

from __future__ import annotations

import hashlib
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_CHECKOUT = os.environ.get(
    'TOFU_PROJECT_PATH',
    os.path.dirname(os.path.dirname(_HERE)))
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)


def data_root() -> str:
    return os.environ.get('SLIDES_BENCH_DATA') or os.path.join(
        _SHARED_CHECKOUT, 'data', 'ppt-bench')


def manifest() -> dict:
    """Committed public entries plus the private manifest under the data root."""
    with open(os.path.join(_HERE, 'corpus.json'), encoding='utf-8') as fh:
        committed = json.load(fh)
    private_path = os.path.join(data_root(), committed['private_manifest'])
    if os.path.exists(private_path):
        with open(private_path, encoding='utf-8') as fh:
            private_docs = json.load(fh)['documents']
    else:
        private_docs = []
    return {**committed, 'documents': private_docs + committed['documents']}


def document_ids() -> list[str]:
    return [doc['id'] for doc in manifest()['documents']]


def load_document(doc_id: str) -> dict:
    """Return the pinned source text plus each arm's identical input file.

    Every arm reads the same ``text``. Tofu's ``produce_slides`` contract
    bounds ``material`` at MAX_SLIDE_MATERIAL_CHARS, so a caller must cut a
    longer document; ``material_for_tofu`` is that caller-side cut.
    """
    from lib.slides.contracts import MAX_SLIDE_MATERIAL_CHARS
    doc = next(d for d in manifest()['documents'] if d['id'] == doc_id)
    path = os.path.join(data_root(), doc['path'])
    with open(path, 'rb') as fh:
        raw = fh.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != doc['sha256']:
        raise ValueError(f'{doc_id}: source digest {digest} != pinned')
    text = _HTML_COMMENT_RE.sub('', raw.decode('utf-8')).strip()
    return {**doc, 'text': text,
            'material_for_tofu': text[:MAX_SLIDE_MATERIAL_CHARS]}


def write_input(doc_id: str, out_path: str) -> str:
    """Materialise the shared input file every competitor arm consumes."""
    doc = load_document(doc_id)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(f'# {doc["title"]}\n\n{doc["text"]}\n')
    return out_path
