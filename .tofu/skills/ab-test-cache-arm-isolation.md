---
name: ab-test-cache-arm-isolation
description: MANDATORY: A/B test arms must use unique system prompts to prevent cross-arm cache sharing — without this, results show 40-50% fake savings for second arm
enabled: true
tags: [testing, cache, a/b-test, methodology]
created: 2026-04-06T08:30:53Z
updated: 2026-04-06T08:30:53Z
---

# A/B Test Cache Arm Isolation (Learned 2026-04-06)

## The Bug

When A/B testing cache strategies, if both arms use the same system prompt + tools prefix:
1. Arm A (runs first) primes the server-side cache
2. Arm B (runs second) starts with a warm cache from Arm A
3. Arm B shows 40-50% lower cost → **false positive**

## The Fix

Add a unique, non-semantic suffix to each arm's system prompt:

```python
arm_seed = f'\n\n<!-- arm={label} seed={time.time():.0f} -->'
messages = [
    {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
    ...
]
```

This changes the prefix bytes → different cache key → each arm starts cold.

## Also Important: Minimum Token Threshold

Opus/Haiku require **4096 tokens** minimum cacheable segment. Sonnet requires 1024.
If your test system prompt + tools is below this, cache never activates (cw=0, cr=0).

Claude tokenizer is ~1.44x tiktoken (cl100k_base). So for Opus:
- Need ≥4096 Claude tokens ≈ 2800 tiktoken tokens in the first cacheable segment

## Example False Positive (Before Fix)

```
ARM A (RANDOM, cold):  cr=43,636  cw=21,454  cost=$0.4413
ARM B (SORTED, warm):  cr=50,597  cw=0       cost=$0.2228  ← 49% "savings" (fake!)
```

After adding arm seeds:
```
ARM A (RANDOM, cold):  cr=39,471  cw=21,454  cost=$0.6217
ARM B (SORTED, cold):  cr=39,471  cw=21,600  cost=$0.5990  ← 3.7% (noise, not real)
```

