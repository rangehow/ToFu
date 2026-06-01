---
name: longform-no-artificial-max-tokens-cap
description: Long-form generation (paper report, analyses) must pass max_tokens=128000 not default 4096
enabled: true
tags: [paper-mode, max-tokens, truncation, convention, claude-md]
created: 2026-04-18T08:03:20Z
updated: 2026-04-18T08:03:20Z
---

# No Artificial max_tokens Cap on Long-Form Generation

## Rule (now in CLAUDE.md §13)

For long-form generation (paper reports, deep analyses, full translations, multi-section writeups), **pass `max_tokens=128000`** to `dispatch_stream`/`dispatch_chat`/`_stream_llm_sse`. Never rely on the default `4096`.

`_clamp_max_tokens()` in `lib/model_info.py` automatically reduces the value to each model's native API limit (GPT=32k, Claude=128k, Qwen per-model 16-64k, Doubao=16k, GLM=131k, etc.). So passing 128000 means "use as much as the model allows, no artificial cap."

## Why

- User report: Reading Mode paper reports felt incomplete — last sections (Research Landscape, Technical Reference, Reproducibility Checklist) were silently truncated.
- Root cause: `routes/paper.py` → `_run_report_task()` called `dispatch_stream(...)` without a `max_tokens` arg, getting the function's default of **4096** (~3k words) — not nearly enough for a 9-section report.

## Fix applied (2026-04-18)

`routes/paper.py`:
- `_run_report_task` → added `max_tokens=128000` to the `dispatch_stream` call.
- `_stream_llm_sse` → default changed from `max_tokens=4096` → `max_tokens=128000` (for paper Q&A / translate).

## Paper report required sections (from `_REPORT_PROMPT_EN/_ZH` in routes/paper.py lines 63-300)

1. ⚡ TL;DR
2. 📋 Paper Card
3. 🎯 Problem & Motivation
4. 💡 Method — How It Works (Core Insight, Architecture, Novel vs Borrowed, Design Choices, Training)
5. 📊 Experimental Analysis (Main Results table, Setup, Deep Dive, Ablations, What's Missing)
6. ✅ Strengths (5-7 bullets)
7. ⚠️ Weaknesses & Limitations (5-7 bullets)
8. 🗺️ Research Landscape & Impact (Positioning, Intellectual Lineage, Impact, Future Directions)
9. 📝 Technical Reference (Glossary, Key Equations, Reproducibility Checklist)

## Approval note

Per CLAUDE.md §10.1, `max_tokens` is normally hyperparameter-gated. But §13.4 clarifies: **raising** the cap to eliminate truncation in long-form paths is a correctness fix, not tuning, and doesn't need separate approval. Lowering it back does.

