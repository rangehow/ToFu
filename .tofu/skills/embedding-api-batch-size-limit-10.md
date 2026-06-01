---
name: embedding-api-batch-size-limit-10
description: Bug fix: text-embedding-v4/3-small/3-large API has max batch_size=10 (not 32), returns HTTP 400 InvalidParameter on larger batches — embed_texts() default updated from 32 to 10
enabled: true
tags: [python, embedding, api, batch-size, bug-fix, text-embedding-v4]
created: 2026-03-27T15:32:03Z
updated: 2026-03-27T15:32:03Z
---

# Embedding API Batch Size Limit = 10

## Problem
The embedding API endpoint (`/embeddings`) enforces a max batch size of **10 texts per request**.
Sending more than 10 texts in the `input` array returns HTTP 400:

```
{"error":{"message":"<400> InternalError.Algo.InvalidParameter: Value error, batch size is invalid, it should not be larger than 10.: input.contents","type":"InvalidParameter"}}
```

## Fix
`lib/embeddings.py` → `embed_texts()` default `batch_size` changed from 32 to 10.
The function already handles batching internally, so >10 texts still work — they're just split into multiple API calls.

## Affected models
- `text-embedding-v4` (1024d, ~320ms, ~105 RPM/key)
- `text-embedding-3-small` (1536d, ~3000ms, ~68 RPM/key) 
- `text-embedding-3-large` (3072d, ~5000ms, ~32 RPM/key)

All three share the same batch size limit of 10.

## Usage pattern for search reranking
Typical search reranking embeds 1 query + 15-20 candidates = 16-21 texts.
With batch_size=10, this becomes 2-3 API calls (~1.5s total for text-embedding-v4).

