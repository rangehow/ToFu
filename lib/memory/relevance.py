"""lib/memory/relevance.py — BM25-based memory relevance scoring.

Lightweight BM25 scorer that ranks memories by relevance to a query string.
No external dependencies — uses only stdlib math.

Used to reduce the number of memories injected per turn from 100+ to ~30,
cutting context consumption while preserving discoverability.
"""

import math
import re
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['filter_relevant_memories']

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

DEFAULT_TOP_K = 30
BM25_K1 = 1.5
BM25_B = 0.75

# Common English stop words — excluded from both query and document tokens
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'who', 'new', 'get', 'set', 'one', 'two', 'any',
})

# Regex: split on whitespace + common punctuation
_TOKENIZE_RE = re.compile(r'[^a-z0-9_]+')


# ═══════════════════════════════════════════════════════
#  Tokenizer
# ═══════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens, removing stop words.

    Splits on whitespace and punctuation. Also splits snake_case and
    kebab-case identifiers into sub-tokens (e.g. 'flask_migration' →
    ['flask', 'migration']).
    """
    lowered = text.lower()
    # Replace hyphens and underscores with spaces for sub-token splitting
    lowered = lowered.replace('-', ' ').replace('_', ' ')
    tokens = _TOKENIZE_RE.split(lowered)
    return [t for t in tokens if t and t not in _STOP_WORDS and len(t) > 1]


# ═══════════════════════════════════════════════════════
#  BM25 Scorer
# ═══════════════════════════════════════════════════════

def _build_memory_doc(mem: dict[str, Any]) -> list[str]:
    """Build a token list from a memory's metadata (name + description + tags)."""
    parts = [
        mem.get('name', ''),
        mem.get('description', ''),
    ]
    tags = mem.get('tags', [])
    if isinstance(tags, list):
        parts.extend(tags)
    return _tokenize(' '.join(parts))


def filter_relevant_memories(
    memories: list[dict[str, Any]],
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Filter memories by BM25 relevance to query, returning top-K.

    Args:
        memories: List of memory dicts (with 'name', 'description', 'tags').
        query: User message text to match against.
        top_k: Maximum number of memories to return.

    Returns:
        List of memory dicts, sorted by relevance (most relevant first).
        If len(memories) <= top_k, returns all memories unchanged (no filtering).
        If query is empty/None, returns all memories unchanged.
    """
    if not query or not memories:
        return memories

    n = len(memories)
    if n <= top_k:
        return memories

    query_tokens = _tokenize(query)
    if not query_tokens:
        return memories

    # Build document token lists for all memories
    docs = [_build_memory_doc(s) for s in memories]
    doc_lens = [len(d) for d in docs]
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    # Compute document frequency (DF) for each query term
    query_terms = set(query_tokens)
    df: dict[str, int] = {}
    for term in query_terms:
        count = sum(1 for doc in docs if term in doc)
        df[term] = count

    # Compute BM25 score for each memory
    scores = []
    for i, (mem, doc, dl) in enumerate(zip(memories, docs, doc_lens)):
        score = 0.0
        # Term frequency map for this document
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in query_terms:
                tf_map[t] = tf_map.get(t, 0) + 1

        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            d = df.get(term, 0)
            # IDF: log((N - df + 0.5) / (df + 0.5) + 1)
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            # BM25 term score
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl)
            score += idf * numerator / denominator

        scores.append((score, i))

    # Sort by score descending, then by original index for stability
    scores.sort(key=lambda x: (-x[0], x[1]))

    # Return top_k memories
    result = [memories[idx] for _, idx in scores[:top_k]]
    n_filtered = n - len(result)
    if n_filtered > 0:
        logger.debug('[MemoryBM25] Filtered %d→%d memories for query (%.60s)',
                     n, len(result), query)
    return result
