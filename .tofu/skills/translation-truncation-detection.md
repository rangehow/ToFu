---
name: translation-truncation-detection
description: Cheap models silently truncate translations — finish_reason propagation + retry rotation that actually changes models (exclude_models + slot penalty)
enabled: true
tags: [translation, bug-fix, truncation, cheap-model]
created: 2026-04-07T08:30:24Z
updated: 2026-05-26T00:56:51Z
---

# Translation Truncation Detection

## Problem
Cheap translation models (e.g. `LongCat-MoE3B-Chat-Meituan`, `MiniMax-M2.5`)
sometimes produce truncated translations — the output ends mid-word/sentence
without completing. This was invisible because:

1. `chat()` in `lib/llm_client.py` discarded `finish_reason` from the API response
2. `_translate_one_chunk()` in `routes/translate.py` had no truncation detection

## Symptoms
- `translatedContent` ends mid-word (e.g. `- *Per`)
- Translation ratio (output/input chars) is suspiciously low (~10–20%)
- `_translateDone: True` even though translation is clearly incomplete
- Logs show `output too short (XXX/YYYY = N%) model=...` with the SAME model
  on every retry (the original "rotation" was nominal only)

## Fix history

### 2026-04-07 — initial detection
1. **`lib/llm_client.py`**: `chat()` injects `finish_reason` into `usage`.
2. **`routes/translate.py`**: `_translate_one_chunk()` checks
   `finish_reason == 'length'` OR `len(out) < 0.20 * len(chunk)` (ratio
   threshold lowered from 30% → 20% to avoid false positives on EN→ZH).
3. On truncation: retry up to N times via `smart_chat`, then accept partial.

### 2026-05-26 — actual model rotation (fixes the silent re-pick)
The 2026-04-07 fix logged "retrying with different model" but did NOT
change models — `dispatch_stream` / `smart_chat` were called without
`exclude_models`, so the slot picker kept choosing the same low-score
cheap model (MiniMax-M2.5 on the sankuai gateway). Result: 5 retries on
the SAME model, all returning ~13% length, partial accepted.

Fixed in this commit:
1. **`Slot.record_truncation(error='')`** in `lib/llm_dispatch/slot.py`:
   bumps `consecutive_errors` + cooldown after 3 truncations. Soft-failure
   path — doesn't touch quota / key-stats.
2. **`LLMDispatcher.record_truncation(key_name, model, error='')`** in
   `lib/llm_dispatch/dispatcher.py`: looks up the slot and forwards.
3. **`exclude_models` parameter** plumbed through `dispatch_chat`,
   `dispatch_stream`, `smart_chat` (`lib/llm_dispatch/api.py`). Caller's
   set is preserved across the periodic 60s exclusion-reset during 429
   cycling (it goes back into `exclude` after `.clear()`).
4. **`routes/translate.py::_translate_one_chunk`**: maintains
   `_excluded_models: set[str]`. On empty / truncated output:
   - reads `(key, model)` from `usage['_dispatch']`
   - calls `dispatcher.record_truncation(key, model, error=reason)`
     (penalizes the slot for FUTURE translate calls too)
   - adds the model to `_excluded_models` for THIS chunk's retries
   Both dispatch calls now pass `exclude_models=_excluded_models or None`.

## Key Insight
The ratio heuristic (20%) catches MOST truncations because Chinese is
40-60% of English char length so anything <20% is suspicious. But
`finish_reason='length'` is the only fully reliable signal — the
heuristic is a fallback for models that don't return proper finish_reason.

The model that wins the cheap-tier `score()` race is whichever has the
lowest latency EMA × inflight × error penalty. A 200 OK with truncated
body counted as success and even LOWERED its latency EMA — making the
broken model MORE attractive on the next pick. `record_truncation()`
fixes this by bumping consecutive_errors so score() deprioritizes the
slot for ~5–300s.

## Verification
Watch `logs/app.log` for:
- `Excluding model and retrying` (new wording — confirms exclusion path)
- `Slot KEY:MODEL cooled down Ns after N consecutive truncations`
- Retries should now show DIFFERENT `model=` values across attempts.

