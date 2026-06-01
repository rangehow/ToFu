---
name: pretraining-data-paper-conventions
description: How pretraining-data-cleaning papers (Nemotron-CC, Dolma) are typically structured
enabled: true
tags: [pretraining, data-curation, paper-writing, LLM]
created: 2026-05-06T10:17:49Z
updated: 2026-05-06T10:17:49Z
---

# Pretraining-Data-Cleaning Paper Conventions

Based on Nemotron-CC (NVIDIA, 2412.02595) and Dolma (AI2, 2402.00159). These are the template papers for this genre.

## Standard paper skeleton
1. **Abstract+Intro** — headline token count, comparison vs prior open corpora (FineWeb-Edu, DCLM, RedPajama, C4, Pile), quality-vs-quantity trade-off framing, numbered contributions (artifact + wins + ablation insights), teaser figure.
2. **Related Work** — position vs other *open* corpora; contrast closed models (PaLM 2, GPT-4, Claude, Llama) to motivate openness.
3. **Design goals / principles** — "consistency w/ prior recipes", "evidence-backed via ablations", "scale target", "openness"; state explicit deviations.
4. **Methods — by pipeline stage, per source**:
   - Acquisition (snapshots, dates, tools: CCNet, Pushshift, S2ORC)
   - Extraction (Justext vs Trafilatura; HTML→WET)
   - Language ID (FastText lid176 / pycld2, threshold ≥0.5)
   - Quality filter (Gopher rules + C4 NoPunc + model-based: FineWeb-Edu, DCLM fastText, ensemble)
   - Content filter (toxicity via Jigsaw-trained fastText; PII via regex mask-or-drop)
   - Dedup (URL → doc → paragraph; exact + fuzzy/MinHash/Bloom)
   - Synthetic data (NEW trend, Nemotron-CC): rephrase LQ, diversify HQ via prompts (Diverse QA, Distill, Extract Knowledge, Knowledge List, Wikipedia-style)
   - Decontamination (paragraph match against eval n-grams ≥13 tokens)
   - ALWAYS report % tokens removed per stage + absolute retained.
5. **Ablations — the scientific core**:
   - 1B–1.2B proxy model, ~150B tokens
   - Downstream: MMLU, HellaSwag, ARC-E/C, PIQA, WinoGrande, OpenBookQA, SciQ, BoolQ, SIQA, CSQA, RACE
   - Perplexity: Paloma suite (C4, Pile, mC4, M2D2, WikiText, Penn Treebank, ICE, Twitter-AAE, Gab, 4chan)
   - One ablation per design decision; training-curve plots + final-step tables.
6. **Flagship validation** — train 1B/7B/8B on full dataset; compare vs similarly-tokened public models (TinyLlama, Pythia, Llama 3.1).
7. **Limitations + Ethics** — English-only, decontamination caveat, copyright stance, PII policy, toxicity ideological, data-removal request form.
8. **Appendices (often > main body)** — Datasheet (Gebru template), exact regexes/thresholds, prompt templates, full per-task curves, tokenizer fertility, filter-correlation matrices, hyperparams/hardware.

## Key stylistic rules
- Report BOTH % removed AND absolute tokens retained.
- Show filters are orthogonal (low pairwise Pearson) to justify stacking.
- "Quality" in scare quotes or footnote that it's ideological (Dolma footnote 4).
- Release: dataset (HF Hub) + toolkit (GitHub Apache-2) + classifier checkpoints + reference blend.
- Cite exact CC snapshots used (e.g., "CC-MAIN-2013-20 through 2024-30").
- Tables for composition, figures for training curves.
- State token counts using Llama or GPT-NeoX tokenizer (be explicit which).

## Emerging trends to emulate
- Classifier **ensembling** (FineWeb-Edu + DCLM + custom) to raise HQ recall from ~10% to ~25%.
- **Synthetic rephrasing** stratified by quality tier (clean noise in LQ; diversify HQ).
- **Quality bucketing** (20 buckets → 5 tiers) via annealing continued-pretraining ablations.
- Removing heuristic filters from HQ split (only apply to LQ) to boost HQ token yield.
- Long-horizon training (15T tokens) matters: unique-token count > aggressive filtering.

