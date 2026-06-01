---
name: ci-tests-must-use-hardcoded-model-names
description: CI tests must use hardcoded model names, not env-dependent lib.QWEN_MODEL/LLM_MODEL which are empty in CI (no server config)
enabled: true
tags: [testing, ci, bug-pattern]
created: 2026-04-03T02:11:29Z
updated: 2026-04-03T02:11:29Z
---

# CI Tests: Hardcoded Model Names Required

## Problem
Tests that import `from lib import QWEN_MODEL, DOUBAO_MODEL, LLM_MODEL` fail in CI because:
- CI has no `data/config/server_config.json`
- All model vars resolve to empty string `''`
- Model family detectors (`is_qwen('')`) return False
- `_clamp_max_tokens` doesn't clamp → assertions fail
- `build_body` doesn't add thinking params → assertions fail

## Rule
**Always use hardcoded model names in test assertions**, never env-dependent variables:

```python
# ❌ BAD — empty string in CI
from lib import QWEN_MODEL
body = build_body(QWEN_MODEL, msgs, max_tokens=200000, stream=False)

# ✅ GOOD — deterministic
body = build_body('qwen-plus', msgs, max_tokens=200000, stream=False)
```

## Common hardcoded models for tests
- Qwen family: `'qwen-plus'`, `'qwen-turbo'`, `'qwq-plus'`
- Claude: `'claude-sonnet-4-20250514'`
- Doubao: `'doubao-seed-1-6'`
- Gemini: `'gemini-2.0-flash'`

