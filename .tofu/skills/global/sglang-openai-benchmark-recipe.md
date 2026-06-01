---
name: sglang-openai-benchmark-recipe
description: How to benchmark an SGLang OpenAI endpoint when sglang.bench_serving is broken locally
enabled: true
tags: [sglang, benchmark, throughput, openai-api]
created: 2026-04-22T02:28:16Z
updated: 2026-04-22T02:28:16Z
---

# Benchmarking an SGLang (OpenAI-compatible) endpoint from this environment

## When `python -m sglang.bench_serving` fails
In this sandbox the local Python env has a broken editable sympy install
that poisons `sys.path`, so `transformers` (and thus `sglang.bench_serving`)
fails with `ImportError: cannot import name 'Mapping' from 'collections'`
even when running `env PYTHONPATH= python ...`. Don't fight it — roll a
small custom async benchmark.

## Pattern that works
`/tmp/k26_bench/bench.py` — aiohttp async client, streams
`/v1/chat/completions`, uses `stream_options: {include_usage: true}` to
pull prompt/completion/reasoning token counts from the final chunk's
usage block. Measures TTFT from first non-empty delta, TPOT from
`(e2e - ttft) / (completion_tokens - 1)`.

Key payload keys for SGLang-served reasoning models (Kimi K2.x, GLM-4.6, etc.):
```json
{
  "model": "...",
  "stream": true,
  "stream_options": {"include_usage": true},
  "chat_template_kwargs": {"thinking": false}  // or true; also accepts enable_thinking for some
}
```
For Kimi-K2.x specifically the key is `thinking` (not `enable_thinking`).
In thinking mode the content is returned in `reasoning_content`
(and `usage.reasoning_tokens`) until the model closes the think block.

## Recommended reference workload (per SGLang cookbook)
`random` dataset, `input=1000`, `output=1000`, concurrency 100-128,
num-prompts = 2-4 × concurrency. For thinking models bump `max_tokens`
high (≥2000 minimum, 32k for real reasoning eval) or almost every request
truncates at max_tokens.

## Kimi-K2.6 reference numbers on internal deployment (128 concurrency, ~1000 in / 1000 out)
- Non-thinking: ~5.6k total tok/s, ~2.8k output tok/s, TPOT ~41 ms
- Thinking:     ~4.6k total tok/s, ~3.1k output tok/s (incl. reasoning), TPOT ~40 ms

