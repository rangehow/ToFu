"""Pricing-tier (cheap) sync contract between templates and price tables.

The ``cheap`` capability is OWNED by the pricing authority
(``PRICING_TIERS`` + ``MODEL_PRICING`` / row-level ``pricing`` dicts),
not hand-picked per template. This suite pins that ownership three ways:

1. Every chat model WITH full pricing evidence must carry ``cheap`` iff the
   bracket derivation says so — both directions (no stale tags, no missed
   tags).
2. A chat model WITHOUT pricing evidence may keep a hand-authored ``cheap``,
   but only if it is recorded in ``UNPRICED_CHEAP_ALLOWLIST`` below — the
   explicit, reviewable list of labels awaiting a pricing row.
3. Non-chat models (image_gen / embedding / audio_chat / …) never carry
   managed tier tags, mirroring reevaluate_pricing_tags' strip behavior.

It also covers the two deepened behaviors of reevaluate_pricing_tags:
CNY-denominated row pricing is converted before bracket comparison, and
``strip_unpriced=False`` preserves authored tags at authoring seams.
"""

from __future__ import annotations

import pytest

from lib.llm_dispatch.config._pricing import (
    MANAGED_TIER_TAGS,
    PRICING_TIERS,
    _model_input_price,
    _model_output_price,
    _resolve_prices,
    _tier_matches,
    reevaluate_pricing_tags,
)
from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS
from lib.provider_template_recipes import load_provider_templates

pytestmark = pytest.mark.unit

# Chat models whose authored ``cheap`` tag has NO pricing evidence anywhere
# (no row-level pricing dict, no MODEL_PRICING row). Each entry is a deliberate
# hand-authored label; add the model to MODEL_PRICING and remove it here
# instead of growing this list.
UNPRICED_CHEAP_ALLOWLIST = frozenset(
    {
        ("ai21", "jamba-1.7-large"),
        ("ai21", "jamba-1.7-mini"),
        ("baidu", "ernie-4.5-300b-a47b"),
        ("baidu", "ernie-5.0-thinking-preview"),
        ("cerebras", "gpt-oss-120b"),
        ("cerebras", "llama-4-scout-17b-16e-instruct"),
        ("cerebras", "qwen-3.5-397b-a17b-instruct"),
        ("cohere", "command-a"),
        ("cohere", "command-a-plus"),
        ("cohere", "north-mini-code"),
        ("deepinfra", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("deepinfra", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("deepinfra", "moonshotai/Kimi-K2.6"),
        ("deepinfra", "zai-org/GLM-5.3"),
        ("doubao", "doubao-seed-2-0-lite-260215"),
        ("doubao", "doubao-seed-2-0-mini-260215"),
        ("fireworks", "accounts/fireworks/models/deepseek-v3.2-exp"),
        ("fireworks", "accounts/fireworks/models/glm-5.3"),
        ("fireworks", "accounts/fireworks/models/gpt-oss-120b"),
        ("fireworks", "accounts/fireworks/models/kimi-k2.6"),
        ("groq", "meta-llama/llama-4-maverick-17b-128e-instruct"),
        ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
        ("groq", "openai/gpt-oss-120b"),
        ("groq", "openai/gpt-oss-20b"),
        ("huggingface", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("huggingface", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("huggingface", "openai/gpt-oss-120b"),
        ("huggingface", "zai-org/GLM-5.3"),
        ("modelscope", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("modelscope", "XiaomiMiMo/MiMo-V2.5-Pro"),
        ("modelscope", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("modelscope", "zai-org/GLM-5.3"),
        ("nebius", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("nebius", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("nebius", "moonshotai/Kimi-K2.6"),
        ("nebius", "zai-org/GLM-5.3"),
        ("novita", "deepseek/deepseek-v3.2-exp"),
        ("novita", "moonshotai/kimi-k2.6"),
        ("novita", "qwen/qwen3.5-397b-a17b-instruct"),
        ("novita", "zai-org/glm-5.3"),
        ("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
        ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
        ("nvidia", "nvidia/nemotron-3-super-120b-a12b"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
        ("openrouter", "google/gemini-3.1-pro-preview"),
        ("openrouter", "google/gemini-3.7-flash"),
        ("openrouter", "google/gemini-3.8-flash"),
        ("perplexity", "sonar"),
        ("perplexity", "sonar-reasoning"),
        ("perplexity", "sonar-reasoning-pro"),
        ("sambanova", "DeepSeek-V3.2-Exp"),
        ("sambanova", "Llama-4-Maverick-17B-128E-Instruct"),
        ("sambanova", "gpt-oss-120b"),
        ("siliconflow", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("siliconflow", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("siliconflow", "moonshotai/Kimi-K2.6"),
        ("siliconflow", "zai-org/GLM-5.3"),
        ("stepfun", "step-3.5-flash"),
        ("stepfun", "step-3.7-flash"),
        ("together", "Qwen/Qwen3.5-397B-A17B-Instruct"),
        ("together", "deepseek-ai/DeepSeek-V3.2-Exp"),
        ("together", "moonshotai/Kimi-K2.6"),
        ("together", "zai-org/GLM-5.3"),
        ("upstage", "solar-mini"),
        ("upstage", "solar-pro-3"),
        ("upstage", "solar-pro4"),
    }
)

_CHEAP_TIER = next(t for t in PRICING_TIERS if t[0] == "cheap")


def _chat_recipes():
    for template in load_provider_templates():
        for row in template.get("offering_recipes") or []:
            caps = set(row.get("capabilities") or [])
            if caps & DISPATCHER_NON_CHAT_CAPS:
                continue
            yield template["key"], row


def test_priced_chat_models_authored_cheap_matches_derivation():
    mismatches = []
    for key, row in _chat_recipes():
        inp, out = _resolve_prices(
            row["model_id"], _model_input_price(row), _model_output_price(row)
        )
        if inp is None or out is None:
            continue
        derived = _tier_matches(_CHEAP_TIER, inp, out, None)
        authored = "cheap" in set(row.get("capabilities") or [])
        if authored != derived:
            mismatches.append(
                (
                    key,
                    row["model_id"],
                    f"authored={authored}",
                    f"derived={derived}",
                    inp,
                    out,
                )
            )
    assert not mismatches, (
        "authored cheap disagrees with the pricing bracket — fix the "
        "template row or the pricing table, never the test:\n"
        + "\n".join(str(m) for m in mismatches)
    )


def test_unpriced_cheap_tags_are_allowlisted():
    found = set()
    for key, row in _chat_recipes():
        caps = set(row.get("capabilities") or [])
        if "cheap" not in caps:
            continue
        inp, out = _resolve_prices(
            row["model_id"], _model_input_price(row), _model_output_price(row)
        )
        if inp is None or out is None:
            found.add((key, row["model_id"]))
    assert found == UNPRICED_CHEAP_ALLOWLIST, (
        f"new unpriced cheap tags: {sorted(found - UNPRICED_CHEAP_ALLOWLIST)}; "
        f"allowlist entries now priced (remove them): "
        f"{sorted(UNPRICED_CHEAP_ALLOWLIST - found)}"
    )


def test_non_chat_models_never_carry_managed_tier_tags():
    offenders = []
    for template in load_provider_templates():
        for row in template.get("offering_recipes") or []:
            caps = set(row.get("capabilities") or [])
            if caps & DISPATCHER_NON_CHAT_CAPS and caps & MANAGED_TIER_TAGS:
                offenders.append((template["key"], row["model_id"]))
    assert not offenders, (
        "non-chat models must not carry pricing-tier tags "
        "(reevaluate_pricing_tags strips them at runtime): "
        + ", ".join(f"{k}/{m}" for k, m in offenders)
    )


def test_cny_row_pricing_is_converted_before_bracket_comparison():
    # ¥20/¥100 per 1M ≈ $2.76/$13.81 @ 7.24 → inside the cheap bracket.
    models = [
        {
            "model_id": "cny-cheap",
            "capabilities": ["text"],
            "pricing": {"input": 20.0, "output": 100.0, "currency": "CNY"},
        }
    ]
    reevaluate_pricing_tags(models)
    assert "cheap" in models[0]["capabilities"]

    # ¥30/¥200 per 1M ≈ $4.14/$27.62 → OUTSIDE the bracket on both axes;
    # a raw (unconverted) comparison would have flipped this to cheap.
    models = [
        {
            "model_id": "cny-not-cheap",
            "capabilities": ["text", "cheap"],
            "pricing": {"input": 30.0, "output": 200.0, "currency": "CNY"},
        }
    ]
    reevaluate_pricing_tags(models)
    assert "cheap" not in models[0]["capabilities"]


def test_strip_unpriced_false_preserves_authored_tags():
    models = [{"model_id": "no-pricing-anywhere", "capabilities": ["text", "cheap"]}]
    reevaluate_pricing_tags(models, strip_unpriced=False)
    assert "cheap" in models[0]["capabilities"]

    models = [{"model_id": "no-pricing-anywhere", "capabilities": ["text", "cheap"]}]
    reevaluate_pricing_tags(models)  # default: strip what we cannot verify
    assert "cheap" not in models[0]["capabilities"]


def test_template_onboarding_bundle_keeps_unpriced_authored_tag():
    """The compile seam re-evaluates tags but must not strip hand-authored
    labels from models the pricing tables do not cover yet (allowlist)."""
    from lib.provider_template_recipes import compile_provider_template_bundle

    bundle = compile_provider_template_bundle(
        "doubao", selected_model_ids=["doubao-seed-2-0-lite-260215"]
    )
    assert "cheap" in bundle["offerings"][0]["capabilities"]
    assert "cheap" in bundle["models"][0]["capabilities"]
