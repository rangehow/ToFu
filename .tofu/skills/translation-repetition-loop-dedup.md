---
name: translation-repetition-loop-dedup
description: Bug fix: translation repetition loop dedup — 3-phase detection (inline/single-line/multi-line block) with consecutive-only matching to avoid false positives on tables/code
enabled: true
tags: [python, translation, bug-fix, cheap-model, repetition-loop, dedup]
created: 2026-04-07T04:57:18Z
updated: 2026-04-07T05:11:01Z
---

# Translation Repetition Loop Detection & Truncation

## Root Cause
Cheap translation models enter degenerate repetition loops on structured/technical content. This is a **model behavior issue**, NOT caused by the chunked parallel translation mechanism. Evidence:
- Single-chunk translations (< 12000 chars) showed 109x line repetition
- Chunked translations' inline loops occurred within a single chunk's output
- Cross-chunk duplication was not observed

## Three Types of Repetition Loops

### 1. Inline (no-newline) loops
Model produces a 100-500 char block repeated 100+ times with **no \n separators**. Example: 261-char block repeated 113 times = 29757-char single line. Detected by `_dedup_inline_loop()` using sliding window sampling.

### 2. Single-line consecutive loops
Same line repeated 6+ times IN A ROW with \n separators. Threshold=6 consecutive to avoid false positives from table separators (`|---|---|`) or code lines (`@abstractmethod`) that legitimately appear 3-4 times in different parts of the document.

### 3. Multi-line block loops (ABCDABCD pattern)
A block of 2-8 lines repeated 4+ times consecutively. Example: 4 PPM-related bullet points repeated 30 times. Detected by Phase 3 block pattern matching.

## Key Design Decision: CONSECUTIVE only
The old approach counted total occurrences of a line across the entire text (max_repeats=3). This caused **massive false positive damage** — 47 translations were incorrectly truncated because table separators and code patterns appeared 4+ times in different sections. The fix requires **consecutive** repetition to trigger.

## Temperature Change
Translation temperature changed from 0.3 → 1 per user request. No repetition_penalty added.

## Cleanup
- 29 damaged translations cleared from DB (will re-translate on demand)
- Remaining translations verified clean with new algorithm

