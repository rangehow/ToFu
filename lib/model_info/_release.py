"""Structured, static release-date knowledge for model ids.

This registry describes vendor knowledge only: when the creator first
published the trained model.  It is projected onto the model-routing v2
authority at read time (``public_projection``) and is never persisted into
the owner aggregate — the same boundary ``_context.py`` keeps for context
windows.  Unknown models deliberately return ``None`` instead of guessing
from generation numbers; the UI hides the fact for them.

Granularity contract: ``YYYY-MM-DD`` where a vendor day is evidenced,
``YYYY-MM`` where only the release month is.  Needles are matched against
the lowercase id with ``.`` folded to ``-`` so wire respellings
(``aws.claude-opus-4.8``, ``doubao-seed-2-0-pro-260215``) hit the same rule
as the canonical id.  Order matters: a more specific id must precede any
prefix of it (``gemini-3.5-flash-lite`` before ``gemini-3.5-flash``).
"""

from __future__ import annotations

_RULES: tuple[tuple[str, str], ...] = (
    # ── Anthropic ────────────────────────────────────────────────
    # Opus 5.5 GA 2026-09-22 (platform.claude.com/docs/en/models/opus-5-5/overview).
    ('claude-opus-5-5', '2026-09-22'),
    # Sonnet 5.5 GA 2026-09-28, Haiku 5.5 GA 2026-10-07 (anthropic.com news index).
    ('claude-sonnet-5-5', '2026-09-28'),
    ('claude-haiku-5-5', '2026-10-07'),
    ('claude-opus-5', '2026-08'),
    # Fable 5.2 released 2026-11-09 (platform.claude.com release notes); the
    # needle must precede the claude-fable-5 prefix it extends.
    ('fable-5-2', '2026-11-09'),
    ('fable-5-1', '2026-09-01'),
    ('claude-fable-5', '2026-08'),
    ('claude-opus-4-8', '2026-08'),
    ('claude-opus-4-7', '2026-07'),
    ('claude-opus-4-6', '2026-05'),
    ('claude-sonnet-4-6', '2026-04'),
    ('claude-haiku-4-5', '2026-02'),
    # ── OpenAI ───────────────────────────────────────────────────
    # https://developers.openai.com/api/docs/changelog (2026-11-17).
    ('gpt-6-2-astra', '2026-11-17'),
    ('gpt-6-2-sol', '2026-11-17'),
    # https://learn.chatgpt.com/docs/changelog (checked 2026-09-30).
    ('gpt-6-1-sol', '2026-09-29'),
    # Same changelog + deploymentsafety.openai.com index (checked 2026-10-28).
    ('gpt-6-1-terra', '2026-10-26'),
    # GPT-6 Sol/Luna GA 2026-09-22 (developers.openai.com model pages +
    # openai.com/index/introducing-gpt-6-sol-and-luna). Order: astra last so
    # the bare 'gpt-6' substring never steals a dated sibling's rule.
    ('gpt-6-sol', '2026-09-22'),
    ('gpt-6-luna', '2026-09-22'),
    ('gpt-6-astra', '2026-09-03'),
    ('gpt-5-6-sol', '2026-08'),
    ('gpt-5-6-terra', '2026-08'),
    ('gpt-5-6-luna', '2026-08'),
    ('gpt-5-6', '2026-08'),
    # API release 2026-04-24 (official API changelog); the dated snapshot is
    # gpt-5.5-2026-04-23, not the release announcement date.
    ('gpt-5-5', '2026-04-24'),
    ('gpt-5-4-mini', '2026-05'),
    ('gpt-5-4', '2026-05'),
    ('gpt-5-3-codex-spark', '2026-03'),
    ('gpt-4-1', '2025-04'),  # vendor launch 2025-04-14, covers -mini/-nano
    # GPT-Image-2.5 (Sunburst/Flare) — default snapshots dated 2026-09-08 per
    # developers.openai.com/api/docs/models/gpt-image-2.5-sunburst (2026-09-20).
    ('gpt-image-2-5', '2026-09-08'),
    ('gpt-image-2', '2026-04'),
    ('gpt-image-1-5', '2025-12'),
    # Media ids (developers.openai.com changelog + model pages, checked
    # 2026-11-12). gpt-image-1-mini launched at DevDay 2025-10-06. The mini
    # audio aliases point to their 2025-12-15 snapshots; the 2025-03-20
    # snapshots keep their own date, so they precede the aliases.
    ('gpt-image-1-mini', '2025-10-06'),
    ('gpt-4o-mini-transcribe-2025-03-20', '2025-03-20'),
    ('gpt-4o-mini-transcribe', '2025-12-15'),
    ('gpt-4o-mini-tts-2025-03-20', '2025-03-20'),
    ('gpt-4o-mini-tts', '2025-12-15'),
    ('gpt-4o-transcribe', '2025-03-20'),
    ('text-embedding-3', '2024-01'),  # vendor launch 2025-01-25 → 2024-01-25
    # ── Google ───────────────────────────────────────────────────
    # Gemini 4 Argon GA flagship (ai.google.dev/gemini-api/docs/models /
    # models/gemini-4-argon: stable id, latest update October 2026).
    ('gemini-4-argon', '2026-10'),
    # Gemini 4 Carbon GA on the Gemini API 2026-11-10 (official changelog;
    # models/gemini-4-carbon: stable id).
    ('gemini-4-carbon', '2026-11-10'),
    # Gemini 4 Hydrogen GA on the Gemini API 2026-11-12 (official changelog;
    # models/gemini-4-hydrogen: stable id, release date).
    ('gemini-4-hydrogen', '2026-11-12'),
    # TTS GA 2026-09-22 (ai.google.dev/gemini-api/docs/changelog). Both ids
    # contain 'gemini-3-8-flash', so they precede the chat model's rule.
    ('gemini-3-8-flash-lite-tts', '2026-09-22'),
    ('gemini-3-8-flash-tts', '2026-09-22'),
    ('gemini-3-8-flash', '2026-09'),
    ('gemini-3-7-flash', '2026-08'),
    ('gemini-3-6-flash', '2026-06'),
    ('gemini-3-5-flash-lite', '2026-05'),
    ('gemini-3-5-flash', '2026-05'),
    ('gemini-3-1-flash-lite-preview', '2026-02'),
    ('gemini-3-1-flash-image-preview', '2026-03'),
    ('gemini-3-1-pro-preview', '2026-02'),
    ('gemini-3-pro-image-preview', '2026-01'),
    ('gemini-3-flash-preview', '2025-12'),
    ('gemini-2-5-flash-image', '2025-08'),
    # Media ids (ai.google.dev/gemini-api/docs/changelog, checked 2026-11-12).
    # GA ids follow their -preview needles, which contain them. Nano Banana
    # 2 Lite is month precision: the changelog says 2026-06-30, the Agent
    # Platform model page 2026-06-23.
    ('gemini-3-1-flash-image', '2026-05-28'),
    ('gemini-3-pro-image', '2026-05-28'),
    ('gemini-3-1-flash-lite-image', '2026-06'),
    ('gemini-nano-banana-2-1', '2026-10-06'),
    ('gemini-omni-1-2-flash', '2026-11-05'),
    ('gemini-omni-1-1-flash', '2026-08-27'),
    ('lyria-3-clip-preview', '2026-03-25'),
    ('lyria-3-pro-preview', '2026-03-25'),
    ('lyria-3-5', '2026-09-03'),
    # Agent Platform model page: 'Release date' per id (checked 2026-11-12).
    ('veo-3-1-lite-generate-001', '2026-04-02'),
    ('veo-3-1-fast-generate-001', '2025-11-17'),
    ('veo-3-1-generate-001', '2025-11-17'),
    ('gemini-2-5-pro', '2025-06'),
    ('gemini-2-5-flash', '2025-06'),
    # ── DeepSeek ─────────────────────────────────────────────────
    # V4.1 Flash 1017 is a dated checkpoint of the rolling Flash alias.
    ('deepseek-v4-1-flash-1017', '2026-10-17'),
    # V4.1 Flash GA (2026-09-10); canonical id. The legacy v4-flash ids
    # route to it; V4 Pro remains a separate, available model.
    ('deepseek-v4-1-flash', '2026-09-10'),
    ('deepseek-flash', '2026-09-10'),
    ('deepseek-v4-pro-0813', '2026-08-12'),
    ('deepseek-v4-pro', '2026-05'),
    ('deepseek-v4-flash', '2026-05'),
    ('deepseek-v3-2', '2025-09'),
    # ── Zhipu GLM ────────────────────────────────────────────────
    # glm-5.3 anchored by its vendor model card dated 2026-08-14
    # (lib/model_info/_family.py, _capabilities.py).
    ('glm-5-3-flash', '2026-08'),
    ('glm-5-3', '2026-08'),
    ('glm-5-2', '2026-06'),
    ('glm-5-1', '2026-04'),
    ('glm-5v-turbo', '2026-05'),
    # ── Moonshot Kimi ────────────────────────────────────────────
    # kimi-k3 was live on the sankuai gateway by 2026-07-17
    # (lib/model_info/_max_output.py).
    ('kimi-k2-7-code', '2026-06'),
    # kimi-k3.1-pro (+ -highspeed) launched 2026-10-23 US time (10-24 Beijing):
    # Kimi blog "available today" on the API; OpenRouter dated slug
    # moonshotai/kimi-k3.1-pro-20261023.
    ('kimi-k3-1', '2026-10-23'),
    ('kimi-k3', '2026-07'),
    ('kimi-k2-6', '2026-03'),
    ('kimi-k2-thinking', '2025-11'),
    # ── Alibaba Qwen ─────────────────────────────────────────────
    # qwen3.8-omni-flash GA 2026-09-18 (qwen.ai/blog + qwencloud model page).
    ('qwen3-8-omni-flash', '2026-09-18'),
    # qwen3.8-27b on Model Studio (Beijing + Singapore) 2026-08-17 per the
    # official newly-released-models notes; open weights 2026-08-14.
    ('qwen3-8-27b', '2026-08-17'),
    ('qwen3-8-max', '2026-08'),
    ('qwen3-8-flash', '2026-08'),
    ('qwen3-7-plus', '2026-05'),
    ('qwen3-5-plus', '2026-03'),
    ('qwen3-max', '2025-09'),
    ('qwen-plus', '2025-07'),
    ('qwen-flash', '2025-08'),
    ('text-embedding-v4', '2025-10'),
    # ── MiniMax ──────────────────────────────────────────────────
    # Xiaomi MiMo V2.6 series GA 2026-09-22 (mimo.mi.com models page; weights
    # open-sourced 2026-09-21). Covers -pro / -flash / -pro-ultraspeed.
    ('mimo-v2-6', '2026-09-22'),
    # M3.1-Flash-Preview: live on platform.minimax.io by 2026-10-10 (Anthropic-compat
    # docs + agent.minimax.cn M Plan promo); M Plan / MiniMax Code only at launch.
    ('minimax-m3-1', '2026-10'),
    ('minimax-m3', '2026-06'),
    ('minimax-m2-7', '2026-03'),
    ('minimax-m2-5', '2025-12'),
    # ── ByteDance Doubao ─────────────────────────────────────────
    # 260215 in the seed-2.0 wire ids is the vendor launch stamp.
    ('doubao-seed-asr-2-0', '2026-02'),
    # 260628 in the seed-2.1 pro wire id is the vendor snapshot stamp.
    ('doubao-seed-2-1', '2026-06'),
    ('doubao-seed-2-0', '2026-02'),
    # ── Tencent Hunyuan ──────────────────────────────────────────
    ('hy4-preview', '2026-07'),
    # Hy4 (Tencent announcement 2026-11-04; live on TokenHub that day). The
    # needle is a substring of hy4-preview, so it must follow the preview's.
    ('hy4', '2026-11-04'),
    ('hy3-preview', '2026-04'),
    # ── Meituan LongCat ──────────────────────────────────────────
    # 2601/2603 in the flash ids are the vendor iteration stamps.
    # LongCat API change log: 2.5-Preview 2026-09-25, 2.5 GA 2026-11-02;
    # the preview needle must precede its substring.
    ('longcat-2-5-preview', '2026-09-25'),
    ('longcat-2-5', '2026-11-02'),
    ('longcat-2-0', '2026-06'),
    ('longcat-flash-omni-2603', '2026-03'),
    ('longcat-flash-thinking-2601', '2026-01'),
    # ── Meta ─────────────────────────────────────────────────────
    # muse-spark-1.3 was listed on the Meta Model API catalogue
    # 2026-09-02 (standard + contributor SKUs same day).
    ('muse-spark-1-3', '2026-09-02'),
    # Muse Ember released 2026-11-09 on Muse Code and the Meta Model API as
    # muse-ember-1.0 (research.meta.ai blog index + launch post).
    ('muse-ember-1-0', '2026-11-09'),
    # ── StepFun ──────────────────────────────────────────────────────
    # Step 5 Preview opened to all API users 2026-09-20 (stepfun.com news).
    ('step-5-preview', '2026-09-20'),
    # ── Mistral ────────────────────────────────────────────────────
    # Mistral Large 4 Public Preview 2026-10-06 (docs model page, v26.10).
    ('mistral-large-4-0', '2026-10-06'),
    # ── Thinking Machines ────────────────────────────────────────
    # Inkling 2 Small released 2026-11-05 (thinkingmachines.ai model card
    # 'Date of release'; news post). Open weights followed on 2026-11-12.
    ('inkling-2-small', '2026-11-05'),
    # ── xAI ──────────────────────────────────────────────────────
    # grok-4.7 (GA 2026-09-21, current flagship): docs.x.ai release notes.
    ('grok-4-7', '2026-09-21'),
    ('grok-4-6', '2026-08'),
    # xAI March release notes list the multi-agent variant separately;
    # its unversioned alias still lacks confirmed context and pricing.
    ('grok-4-20-multi-agent', '2026-03'),
    ('grok-4-20', '2026-06'),
    ('grok-4-1-mini', '2026-04'),
)


def _fold(model: str) -> str:
    return (model or '').strip().lower().replace('.', '-')


def release_date(model: str) -> str | None:
    """Return the vendor release date (``YYYY-MM[-DD]``) for *model*.

    Matching is a substring probe over the folded id so provider wire
    respellings (region prefixes, namespaces, snapshot stamps) resolve to
    the trained model's date.  ``None`` means no evidenced date.
    """
    folded = _fold(model)
    if not folded:
        return None
    for needle, date in _RULES:
        if needle in folded:
            return date
    return None


__all__ = ['release_date']
