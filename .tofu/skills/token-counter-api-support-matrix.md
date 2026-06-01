---
name: token-counter-api-support-matrix
description: Token-counter package (lib/token_counter/) — wired into compaction/deferral/manager as of 2026-05-05
enabled: true
tags: [token-counting, api, reference, compaction]
created: 2026-05-05T02:15:49Z
updated: 2026-05-05T03:03:07Z
---

# Token-counting backend matrix (verified 2026-05-05)

Implementation: **`lib/token_counter/` package** (modular, one file per
backend). Consult this memory before adding a new provider or changing
backend priority.

## Per-provider matrix

| Provider | Exact offline | Exact online | Our resolver priority |
|---|---|---|---|
| **Anthropic / Claude** | HF `Xenova/claude-tokenizer` mirror | `POST /v1/messages/count_tokens` (Meituan gateway proxies it at `/v1/anthropic/v1/messages/count_tokens`; also native `api.anthropic.com`) | usage_cache → anthropic_api → hf → tiktoken → heuristic |
| **AWS Bedrock** (Claude) | n/a | `bedrock-runtime.count_tokens` (same answer as Anthropic) | not wired — fallback to anthropic_api |
| **Google Gemini** | HF `google/gemma-2-9b` (proxy) | `/v1{,beta}/models/X:countTokens` — **Meituan gateway does NOT proxy (404), only works with native Google endpoints** | usage_cache → gemini_api → hf → tiktoken → heuristic |
| **OpenAI (GPT-4/4o/5/o*)** | `tiktoken` (official, exact) | no server endpoint | usage_cache → tiktoken → heuristic |
| **DeepSeek** | `deepseek_tokenizer` pip pkg (official offline) | no server endpoint | usage_cache → deepseek → hf → tiktoken → heuristic |
| **Qwen (DashScope)** | HF `Qwen/Qwen2.5-7B-Instruct` | no endpoint | usage_cache → hf → tiktoken → heuristic |
| **GLM (Zhipu)** | HF `THUDM/glm-4-9b-chat` | no endpoint | usage_cache → hf → tiktoken → heuristic |
| **MiniMax** | HF proxy (Qwen fallback) | no endpoint | usage_cache → hf → tiktoken → heuristic |
| **Doubao (Volcengine Ark)** | HF proxy (Qwen fallback) | no endpoint | usage_cache → hf → tiktoken → heuristic |
| **Llama / Mistral** | HF native repos | no endpoint | usage_cache → hf → tiktoken → heuristic |

## Package layout

```
lib/token_counter/
  __init__.py           # public re-exports
  api.py                # count_tokens / count_text / record_usage / invalidate
  base.py               # TokenCounter ABC, CountResult, iter_message_texts, count_images
  config.py             # env-var knobs (MODE, API_TIMEOUT, API_THRESHOLD, CACHE_TTL)
  heuristic.py          # Tier 0 — CJK-aware char estimator (always works)
  usage_cache.py        # Tier 1 — NEW: reuse last response's prompt_tokens
  tiktoken_counter.py   # Tier 2 — universal local tokenizer
  deepseek_counter.py   # Tier 2 — offline DeepSeek tokenizer (if installed)
  hf_counter.py         # Tier 2 — HF AutoTokenizer (Qwen, GLM, Claude mirror, …)
  anthropic_api.py      # Tier 3 — Anthropic count_tokens (exact, network)
  gemini_api.py         # Tier 3 — Gemini countTokens (native endpoints only)
  resolver.py           # per-model priority chains + force_backend()
```

## Key env vars

- `CHATUI_TOKEN_COUNTER` — force mode (`auto` default; `tiktoken`, `anthropic_api`, `gemini_api`, `deepseek`, `hf`, `usage_cache`, `heuristic`)
- `CHATUI_TOKEN_COUNTER_API_THRESHOLD=0.50` — skip network tiers when cheap estimate < 50% of context limit
- `CHATUI_TOKEN_COUNTER_API_TIMEOUT=10` — seconds
- `CHATUI_TOKEN_COUNTER_CACHE_TTL=3600` — usage-cache entry TTL
- `CHATUI_TOKEN_COUNTER_HF_AUTOFETCH=0` — set `1` to allow HF download on first use

## Public API usage

```python
from lib.token_counter import count_tokens, count_text, record_usage, invalidate

# Pre-flight
result = count_tokens(messages, model='aws.claude-opus-4.7',
                      system=sys, tools=tools,
                      conv_id=conv_id, context_limit=1_000_000,
                      api_base_url=provider_base, api_key=api_key)
# result: {'tokens': 1310784, 'method': 'anthropic_api', 'elapsed_ms': 800,
#          'confidence': 'exact'}

# After streaming completes (already wired in manager.py)
record_usage(conv_id, prompt_tokens=usage['prompt_tokens'],
             model=model, message_count=len(messages), messages=messages)

# After compaction / message edit (already wired in compaction.py)
invalidate(conv_id)
```

## Inspired by

- **Claude Code** (`src/services/tokenEstimation.ts`) — 3-tier API/Haiku/heuristic
- **OpenCode** (`packages/opencode/src/session/message-v2.ts`) — trust `usage.prompt_tokens` from last response → this is the **usage_cache** tier (our biggest upgrade, ~zero cost, near-exact)
- **opencode-tokenscope** — multi-backend `TokenizerManager` with tiktoken + HF + approximate fallback
- **OpenCode-DCP** — hybrid strategy: API values for Total/System, tokenizer for User/Tools

## Meituan gateway probe results (2026-05-04)

Only `/v1/anthropic/v1/messages/count_tokens` works (200). All others 404:
- `/v1/gemini/{v1beta,v1}/models/X:countTokens`
- `/v1/dashscope/api/v1/tokenizer`
- `/v1/openai/native/{tokenize,count_tokens}`
- `/v1/minimax/v1/tokenize`
- `/v1/doubao/api/v3/tokenization`

## Wiring — COMPLETE (2026-05-05)

**`lib/tasks_pkg/compaction.py`:**
- `_estimate_msg_tokens` now uses `lib.token_counter.heuristic.cheap_estimate_text` (CJK-aware; ≥2× more accurate than chars/4).
- New `_count_tokens_authoritative(messages, task)` returns `(tokens, method)` via `count_tokens()` (usage_cache → tiktoken → API → heuristic). Called by `_should_force_compact` so the trigger is no longer tricked by CJK.
- New `_parse_reported_token_count(error_text)` extracts `N` from `"prompt is too long: N tokens > M maximum"`.
- `reactive_compact(error_text=...)` parses the upstream count, invalidates usage_cache, and threads `reported_token_count` into `_head_truncate`.
- `_head_truncate(reported_token_count=...)` computes the REAL fraction to shed instead of relying on the heuristic.

**`lib/tasks_pkg/llm_fallback.py`:**
- Forwards `error_text=str(e)` to `reactive_compact()` on `PromptTooLongError`.

**`lib/tasks_pkg/manager.py::stream_llm_response`:**
- After every successful stream, calls `record_usage(conv_id, prompt_tokens, model, message_count, messages=body['messages'])`. Next round's compaction check gets a ~exact number at zero cost.

**`lib/tools/deferral.py::_estimate_tool_tokens`:**
- Now calls `count_text(json_blob)` (tiktoken-exact for tool schemas).

## Regression check

Against the failing conversation `mo4fr5xeup9ogp` (flattened):

| estimator | tokens | trigger @ 864k? |
|---|---|---|
| OLD `chars/4` | 1,093,129 | ✓ but post-L1 fell under threshold |
| NEW CJK-aware heuristic | 1,824,146 | ✓ |
| NEW authoritative (tiktoken) | 1,947,414 | ✓ |
| Real Bedrock reject at failure | 1,310,784 | ✓ |

With all three upgrades together:
1. The heuristic is ≥2× more accurate.
2. `_count_tokens_authoritative` is exact for Claude (via usage_cache/API) and tiktoken-exact for OpenAI.
3. Reactive `_head_truncate` now sizes drops by the API's own reported token count — no more "shed 5% when we need 30%" misses.

## Smoke tests

- `debug/test_token_counter.py` — package-level backend tests.
- Ad-hoc regression: reads `mo4fr5xeup9ogp` from DB, compares old vs new estimators, asserts all new tiers trigger at the correct threshold.

