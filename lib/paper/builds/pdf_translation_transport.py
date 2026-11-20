"""Transport binding for the full-document PDF translation pipeline.

``lib.paper.builds.pdf_translation.translate_pdf`` deliberately owns no
provider transport: it takes an injected ``translate(prompt) -> str`` callable.
This module supplies that callable from Tofu's configured model dispatch and
reports the absence of a usable route explicitly, so the HTTP entry can say
"not configured" instead of inventing a translator.

Entry points: :func:`resolve_pdf_translator`,
:func:`pdf_translation_configured`. Dependencies: ``lib.llm_dispatch`` (lazy;
importing this module does not read routing configuration or touch a provider).
"""

from __future__ import annotations

from collections.abc import Callable

Translator = Callable[[str], str]


def _dispatcher():
    from lib.llm_dispatch.factory import get_dispatcher

    return get_dispatcher()


def pdf_translation_configured() -> bool:
    """Whether at least one durable text route can serve a translation call.

    Cooldown, rate-limit and in-flight state is ignored: a temporarily busy
    provider is configured, not missing.
    """
    return bool(_dispatcher().has_capable_slots('text'))


def resolve_pdf_translator(
    *,
    owner_user_id: int | None = None,
) -> Translator | None:
    """Return the product model call path, or ``None`` when none is configured.

    Each call dispatches one prompt through the shared pool. Provider, key,
    retry and credential policy stay in ``lib.llm_dispatch``; this module adds
    no second transport and performs no network work at resolution time.
    """
    if not pdf_translation_configured():
        return None

    def translate(prompt: str) -> str:
        from lib.llm_dispatch import smart_chat

        content, _usage = smart_chat(
            [{'role': 'user', 'content': prompt}],
            capability='text',
            temperature=0,
            owner_user_id=owner_user_id,
            log_prefix='[Paper:PDFTranslate]',
        )
        return content if isinstance(content, str) else ''

    return translate


__all__ = ['Translator', 'pdf_translation_configured', 'resolve_pdf_translator']
