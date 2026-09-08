"""lib/search_extract.py — fetch_url ``extract`` mode (Gemini web_fetch
inspiration): answer one question about an already-fetched page with a single
bounded model pass, so a huge page does not have to enter the conversation.

Fail-soft by contract: the caller owns the raw page and falls back to it —
this module raises, the handler degrades, the fetched content is never lost.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

#: Extraction is a lookup on fixed evidence, not creative — deterministic.
_EXTRACT_TEMPERATURE = 0.0
#: One focused answer; a page QA never needs more.
_EXTRACT_MAX_TOKENS = 1500
#: Bound the page excerpt so a pathological page cannot flood the call. The
#: fetch pipeline already filters boilerplate; this is the second fence.
_MAX_PAGE_EXCERPT_CHARS = 48_000


def dispatch_stream(*args, **kwargs):
    """Facade → real dispatcher. Patched by tests as ``search_extract.dispatch_stream``."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def _messages(question: str, url: str, page_excerpt: str) -> list[dict[str, str]]:
    system = (
        'You answer one question about one fetched web page. Ground every '
        'claim in the provided page content — never invent facts the page '
        'does not contain. If the page does not answer the question, say so '
        'plainly in one sentence, then give the closest relevant fact the '
        'page DOES contain. Answer in the same language as the question. '
        'Be concise: a direct answer first, then at most a few supporting '
        'details with short quotes when useful.')
    payload = (
        f'Page URL: {url}\n\n'
        f'--- PAGE CONTENT (possibly truncated) ---\n{page_excerpt}\n'
        f'--- END PAGE CONTENT ---\n\n'
        f'Question: {question}')
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': payload},
    ]


def extract_answer(question: str, url: str, page_content: str) -> str:
    """Answer ``question`` against ``page_content`` in one bounded model pass.

    Returns the answer text. Raises on any dispatch failure — the caller
    falls back to the raw page content.
    """
    clean_question = str(question or '').strip()[:2_000]
    if not clean_question:
        raise ValueError('extract question is required')
    excerpt = str(page_content or '')[:_MAX_PAGE_EXCERPT_CHARS]
    if not excerpt:
        raise ValueError('no page content to extract from')

    from lib.llm.stream_result import require_verified_provider_stream_result

    buf = {'content': ''}

    def _on_content(chunk):
        buf['content'] += chunk

    stream_result = require_verified_provider_stream_result(
        dispatch_stream(
            _messages(clean_question, str(url or ''), excerpt),
            on_content=_on_content,
            capability='text',
            max_tokens=_EXTRACT_MAX_TOKENS,
            temperature=_EXTRACT_TEMPERATURE,
            thinking_enabled=False, max_retries=2,
            log_prefix='[Fetch:Extract]'),
        context='fetch_url extract')
    message = stream_result.message
    answer = buf['content'] or (
        str(message.get('content') or '')
        if hasattr(message, 'get') else '')
    answer = answer.strip()
    if not answer:
        raise ValueError('extract pass returned an empty answer')
    return answer


__all__ = ['extract_answer']
