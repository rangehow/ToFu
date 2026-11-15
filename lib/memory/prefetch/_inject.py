"""Render selected memory evidence for Context Composer."""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.prefetch._config import PREFETCH_MAX_BYTES, PREFETCH_MAX_TOKENS

logger = get_logger(__name__)


_RELEVANT_MEMORIES_TAG = '<relevant_memories>'


def _trust_line(memory: dict) -> str:
    """``- trust: <class> (<source>, <date>)`` from the lesson's stored provenance."""
    from lib.memory.contracts import memory_trust_class
    provenance = memory.get('provenance') or {}
    source = str(provenance.get('source_class') or '')
    trust = memory_trust_class(memory.get('status'), source,
                               provenance.get('edited_source_class'))
    detail = source.replace('_', ' ') if source else 'unrecorded source'
    at = str(provenance.get('at') or '')[:10]
    return f'- trust: {trust} ({detail}{", " + at if at else ""})\n'


def _render_relevant_memories_block(selected_memories: list[dict], *,
                                    openers: frozenset[str] | set[str]) -> str:
    """Render evidence within both the token and defensive byte budgets.

    ``openers`` are the tools this request holds that can open a lesson
    (``search_memories`` by id, ``read_files`` by path). A body that does not
    fit becomes a pointer only through one of them; with neither, that memory
    is left out rather than shown as a title the role can never read.
    """
    header = (
        'The following memories were pre-selected as likely relevant to '
        "what you're doing in this turn. Read them BEFORE taking action — "
        'they may warn you about traps you previously hit or remind you of '
        'project conventions you previously established. If a memory turns '
        'out not to apply, just ignore it. Each trust line says who stands '
        'behind it: owner-stated or verified by the owner, or inferred (an '
        "agent's report; check it before relying on it)."
    )
    chunks: list[str] = []
    total = len(header) + len(_RELEVANT_MEMORIES_TAG) * 2 + 200

    def _tokens(text: str) -> int:
        try:
            from lib.token_counter import count_text
            return int(count_text(text))
        except Exception as exc:
            logger.debug('[MemoryPrefetch] token counter fallback: %s', exc)
            return (len(text) + 3) // 4
    from lib.memory.secret_scan import redact_secret_shapes
    by_id = 'search_memories' in openers and 'read_files' not in openers
    for m in selected_memories:
        name = redact_secret_shapes(m.get('name', ''))
        desc = redact_secret_shapes(m.get('description', ''))
        body = redact_secret_shapes((m.get('body') or '').strip())
        scope = m.get('scope', 'project')
        fp = m.get('filepath', '')
        locator = (f'- id: {m.get("id", "")}\n' if by_id else f'- path: {fp}\n')
        chunk = (
            f'### memory: {name}\n'
            f'- scope: {scope}\n'
            f'{_trust_line(m)}'
            f'- description: {desc}\n'
            f'{locator}\n'
            f'{body}'
        )
        projected = total + len(chunk)
        if (projected > PREFETCH_MAX_BYTES
                or _tokens('\n\n'.join([header, *chunks, chunk]))
                > PREFETCH_MAX_TOKENS):
            # Budget exhausted — truncate remaining bodies to titles + descs
            if 'read_files' in openers:
                pointer = f'- path: {fp}  (body omitted — read with read_files if needed)'
            elif 'search_memories' in openers and m.get('id'):
                pointer = (f'- id: {m["id"]}  (body omitted — open with '
                           f'search_memories(memory_id="{m["id"]}"))')
            else:
                continue
            chunk_short = f'### memory: {name}\n{_trust_line(m)}- description: {desc}\n{pointer}'
            if (total + len(chunk_short) > PREFETCH_MAX_BYTES
                    or _tokens('\n\n'.join([header, *chunks, chunk_short]))
                    > PREFETCH_MAX_TOKENS):
                break
            chunks.append(chunk_short)
            total += len(chunk_short)
            continue
        chunks.append(chunk)
        total += len(chunk)

    if not chunks:
        return ''
    body = '\n\n'.join(chunks)
    return (
        f'{_RELEVANT_MEMORIES_TAG}\n'
        f'{header}\n\n{body}\n'
        f'</relevant_memories>'
    )


__all__ = ['_RELEVANT_MEMORIES_TAG', '_render_relevant_memories_block']
