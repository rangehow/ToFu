---
name: chatui-streaming-perf-optimizations
description: Frontend performance optimizations for chatui streaming: rAF coalescing, incremental markdown rendering, zone caching, tool DOM truncation, CSS containment, fingerprint caching
enabled: true
tags: [javascript, performance, streaming, frontend, dom, optimization]
created: 2026-03-17T03:25:28Z
updated: 2026-03-17T03:25:28Z
---

# ChatUI Frontend Streaming Performance Optimizations

## Problem
Single conversation rounds with 1000+ tool calls and very long thinking/content cause severe lag because:
1. Every SSE token triggers a full DOM update + markdown re-render
2. `_syncSearchRoundsDOM` runs 5 `.filter()` passes per frame
3. `updateStreamingUI` does `querySelector` lookups every frame
4. All 1000+ tool rounds create DOM nodes even when panel is collapsed
5. `renderMarkdown()` re-parses entire content (can be 100KB+) on every token

## Optimizations Applied

### 1. `requestAnimationFrame` coalescing (core.js `twUpdate`)
Multiple SSE events between paint frames are merged — only the last buffer state is rendered per frame. Typically reduces updates from 50-100/sec to 60fps max.

### 2. Incremental content rendering (ui.js `updateStreamingUI`)
Content is split at paragraph boundaries (`\n\n`). The "frozen" prefix is rendered once and kept in DOM; only the "tail" portion is re-rendered on each token. Uses `contentZone._frozenLen` and a `.md-stream-tail` span.

### 3. DOM zone caching (ui.js `_getStreamZones`)
`_streamZoneCache` stores references to `#streaming-body`, search/think/content/status zones. Only refreshed when the body element changes. Eliminates 4-5 `querySelector` calls per frame.

### 4. Fingerprint-based skip (ui.js `_syncSearchRoundsDOM`)
A lightweight fingerprint (`length:roundNum+status+queryLen`) is computed from rounds and compared to `container._roundsFingerprint`. If unchanged, the entire function returns immediately.

### 5. Single-pass round classification
Instead of 5 separate `.filter()` calls for projRounds/browserRounds/etc, all rounds are classified in a single `for` loop.

### 6. Tool DOM truncation
- **During streaming**: only last 50 completed rounds + active ones are rendered as DOM nodes
- **Static render**: groups with >100 items show only last 50 with "⋯ N earlier tools hidden" placeholder

### 7. CSS containment
`contain: layout style` on `.ptool-panel-body` and `.md-content` tells the browser that changes inside these elements don't affect siblings, reducing layout recalculation scope.

### 8. Zone cache invalidation
`twStop()` resets `_streamZoneCache` so stale references from a previous stream don't cause issues.

