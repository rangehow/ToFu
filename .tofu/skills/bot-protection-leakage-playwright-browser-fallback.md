---
name: bot-protection-leakage-playwright-browser-fallback
description: Bug fix: bot-protection pages (Cloudflare/DDoS-Guard) leak through Playwright and browser extension fallbacks because they skip _is_bot_extracted_text check; also missing Chinese patterns
enabled: true
tags: [python, fetch, bot-protection, cloudflare, bug-fix]
created: 2026-04-06T10:57:49Z
updated: 2026-04-06T10:57:49Z
---

# Bot-Protection Page Leakage Through Fallback Paths

## Bug
Bot-protection / Cloudflare challenge pages appeared in web_search tool results as `Full Page Content` 
with ~100-130 chars of garbage text like "正在进行安全验证" or "Checking your browser".

## Root Cause (Two Issues)

### 1. Fallback paths skip bot-content checks
When `_is_bot_protection(html)` detects a challenge page in HTML, it calls `try_playwright_fallback()`.
When `_is_bot_extracted_text()` detects bot text post-extraction, it also tries Playwright.

**But neither Playwright fallback nor browser extension fallback checked their own results for bot content!**

Headless Playwright is also detected by Cloudflare, so it returns the same challenge page text.
That text (> 50 chars) was cached and returned as valid content.

### 2. Missing Chinese bot-protection patterns
`_BOT_TEXT_PATTERNS` and `_is_bot_protection` indicators only had English patterns.
Chinese-localized Cloudflare pages (e.g. "正在进行安全验证") were not detected.

## Fix
1. Added `_is_bot_extracted_text()` check in `try_playwright_fallback()` (lib/fetch/http.py)
2. Added `_is_bot_extracted_text()` check in `try_browser_fetch()` (lib/fetch/http.py)
3. Added Chinese patterns to both `_BOT_TEXT_PATTERNS` and `_is_bot_protection` indicators (lib/fetch/utils.py)

## Files Modified
- `lib/fetch/utils.py` — Chinese patterns in `_BOT_TEXT_PATTERNS` and `_is_bot_protection`
- `lib/fetch/http.py` — Bot-content guards in `try_playwright_fallback()` and `try_browser_fetch()`

