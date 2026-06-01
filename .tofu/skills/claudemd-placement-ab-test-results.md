---
name: claudemd-placement-ab-test-results
description: A/B test result: CLAUDE.md in user message (Claude Code style) beats system message (ChatUI current) — 18% cost savings, 49% vs 42% cache hit rate
enabled: true
tags: [cache, ab-test, claude-code, system-prompt, optimization]
created: 2026-04-08T07:09:24Z
updated: 2026-04-08T07:09:24Z
---

# CLAUDE.md Placement A/B Test Results (2026-04-08)

## Test: System message (ChatUI) vs User message (Claude Code style)

**Arm A (ChatUI current)**: ALL context (CLAUDE.md + static + guidance) in `messages[0]` role=system
**Arm B (Claude Code style)**: Static instructions only in system msg; CLAUDE.md in prepended user msg with `<system-reminder>` tags

## Results (aws.claude-opus-4.6, 8 rounds + Task 2)

| Metric | SYSTEM_MSG (A) | USER_MSG (B) | Delta |
|--------|---------------|-------------|-------|
| Uncached prompt tokens | 4,076 | 13 | **-99.7%** ✅ |
| Cache reads | 38,350 | 41,504 | **+8.2%** ✅ |
| Cache writes | 20,811 | 18,540 | **-10.9%** ✅ |
| TOTAL COST | $0.6165 | $0.5050 | **-18.1%** ✅ |
| Cache savings % | 41.6% | 49.3% | **+18.4%** ✅ |
| Avg TTFT | 5.1s | 4.7s | **-7.9%** ✅ |
| Avg round time | 6.2s | 5.2s | **-15.9%** ✅ |

## Key observation: R1 cache behavior
- Arm A R1: MISS (pt=4,066, cr=0, cw=0) — entire system msg uncached
- Arm B R1: WRITE (pt=3, cr=0, cw=8,242) — cache write happens immediately
- Arm B gets cache writes from R1, Arm A doesn't until R2

## Why Arm B wins
1. **Smaller system message** → BP1-BP2 cover a shorter, ultra-stable prefix
2. **CLAUDE.md in user message** → automatically cached by BP4 (tail breakpoint) along with conversation history
3. **R1 cache write** happens immediately because the smaller system prefix meets minimum cache segment requirements sooner
4. **Inter-task cache**: B gets 98% hit vs A gets 80% (smaller system prefix survives better across tasks)

## Architecture (Claude Code)
```
messages[0] = {role: system, content: static_base + guidance}  ← small, ultra-stable, BP1
messages[1] = {role: user, content: <system-reminder>CLAUDE.md...</system-reminder>}  ← BP in tail
messages[2+] = actual conversation
```

## Projected savings for 54-round conversation
- SYSTEM_MSG: ~$3.70
- USER_MSG: ~$3.03
- **Saves ~$0.67 per conversation**

## Test file
`debug/test_system_placement_ab.py`

