---
name: multilingual-coding-benchmarks
description: Catalog of multilingual (natural-language) coding benchmarks, with notes on what "multilingual" means in each
enabled: true
tags: [benchmarks, llm-eval, multilingual, code]
created: 2026-05-12T10:53:50Z
updated: 2026-05-12T10:53:50Z
---

# Multilingual coding benchmarks — natural language axis

When users ask about "multilingual coding benchmarks" always clarify: programming-language multilingual vs. natural-language multilingual. They are completely different.

## Programming-language multilingual (repo-level, SWE-Bench style)
- SWE-bench Multilingual (official) — 9 prog languages
- Multi-SWE-bench (ByteDance, 1632 instances, 7 langs)
- SWE-PolyBench (Amazon, ~2K, 4 langs)
- M2RC-Eval (completion across many prog langs)
- AACR-Bench (Alibaba, code review, 10 prog langs)

## Natural-language multilingual

### Function-level
- HumanEval-XL — 80 problems × 23 NL × 12 PL (most cited)
- MBXP, MCoNaLa, ODEX, CRUXEVAL-X, CodeMixBench

### Competitive-programming
- ProBench (Codeforces/AtCoder/Nowcoder; native EN/ZH/JA)
- xCodeEval (25M samples)

### Repo/agent-level (the closest to "SWE-Bench but multilingual NL")
- **MAPS** (Fujitsu-FRE + Cohere, arXiv 2505.15935) — KEY ONE.
  - HF: Fujitsu-FRE/MAPS and Fujitsu-FRE/MAPS_Verified
  - Translates GAIA, SWE-Bench, MATH, ASB into 11 languages
    (DE, ES, PT-BR, JA, RU, ZH, IT, AR, HE, KO, HI) + EN
  - 805 unique tasks × 12 langs = 9,660 instances
  - Hybrid translation: Google NMT + Cohere Command-A + 25% human verification
  - Caveat: SWE-Bench part shows only ~1.3% multilingual degradation
    because inputs are mostly code; the language signal is in GAIA
    (12-16% drop) and ASB (security).
  - Compatible with existing SWE-bench harness (only problem_statement
    swapped; repo/tests/code stay English).

### Bilingual EN/ZH specifically
- CodeArena (Alibaba+CAS, NAACL 2025) — 397 tasks, human-pref eval
- CodeApex (SJTU) — bilingual, function-level

## Bottom line
For testing a SWE-bench-style harness across natural languages, MAPS-SWE-Bench
is the only off-the-shelf option. For actually measuring model multilingual
capability, pair it with MAPS-GAIA where the language signal is strong.

