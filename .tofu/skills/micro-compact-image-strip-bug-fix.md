---
name: micro-compact-image-strip-bug-fix
description: Bug fix: micro_compact ignored image_url blocks in tool results — images sat in context consuming 250K+ tokens each, triggering expensive force-compact LLM summarization
enabled: true
tags: [bug-fix, compaction, images, context-window, performance]
created: 2026-04-05T05:38:11Z
updated: 2026-04-05T05:38:11Z
---

# Micro-Compact Image Strip Bug Fix (2026-04-05)

## The Bug

`micro_compact()` in `lib/tasks_pkg/compaction.py` had two issues that caused images to persist in context indefinitely:

### Issue 1: Phase B early return blocked Phase C
When all tool results were within the hot tail (≤30), Phase B returned early with `return tokens_saved`, **preventing Phase C (image strip) from ever running**.

### Issue 2: Multimodal content size miscalculation  
When Phase B encountered a multimodal tool result (list with `image_url` + text blocks), it only measured `text_len` (sum of text parts). Since image tool results have tiny text descriptions (~60 chars, well below the 500 char `MICRO_COMPACT_THRESHOLD`), they were always `skipped_short` — even though the `image_url.url` base64 data consumed 250K-1M+ tokens each.

## Impact

In the `mnla7mxg2pskxv` conversation:
- 4 image tool results consumed 905K tokens (3.5MB base64)
- micro_compact did nothing (all 8 tools in hot tail, early return)
- force_compact triggered → expensive LLM summarization of everything
- The summary model couldn't even read the images (text-only) → information lost

## Fix

1. **Removed early return** in Phase B so Phase C always runs
2. **Phase B**: Added image detection — always compact cold multimodal results with images, regardless of text length
3. **Added Phase C**: Dedicated image strip with tight hot tail (`_IMAGE_HOT_TAIL = 2`) — runs independently of the text hot tail (30)

## Key Insight

Images are fundamentally different from text tool results:
- **Huge** (100KB-10MB base64 each vs <1KB text)
- **Non-searchable** (model has already "seen" them)
- **Can be re-loaded** (model can call `read_local_file` again)
- Should have a **much tighter hot tail** (2 vs 30 for text)

