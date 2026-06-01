---
name: playwright-text-extraction-pattern
description: Use locator.text_content() not inner_text() for scraping — avoids actionability timeout
enabled: true
tags: [playwright, web-scraping, timeout, best-practice]
created: 2026-04-16T07:25:09Z
updated: 2026-04-16T07:25:09Z
---

# Playwright Text Extraction: inner_text vs text_content vs evaluate

## The Problem
`page.inner_text('body')` performs **actionability checks** — waits for element to be visible, stable, and not mid-navigation. On pages that never finish JS (redirect stubs, heavy SPAs), this hangs until default 30s timeout.

## The Fix — Use `text_content()` for Scraping

| Method | Actionability wait? | Hidden text? | Use case |
|---|---|---|---|
| `page.inner_text(sel)` / `locator.inner_text()` | ✅ YES — can hang | ❌ Skips hidden | UI testing |
| `locator.text_content()` | ❌ NO — returns immediately | ✅ Includes all | **Web scraping** |
| `page.evaluate('document.body?.innerText')` | ❌ NO — raw JS | ❌ Skips hidden | Fallback |

**Modern pattern for scraping:**
```python
# Primary: locator-based, no actionability wait
body_text = page.locator('body').text_content(timeout=remaining_ms) or ''

# Fallback: raw JS evaluate
body_text = page.evaluate('document.body?.innerText || ""')
```

`text_content()` maps to DOM's `Node.textContent` — returns all text including hidden elements, no layout computation needed. `inner_text()` maps to `HTMLElement.innerText` — requires layout computation and actionability.

## Key Insight
For web scraping, we don't care about actionability (we're not clicking anything). We just want the text. `text_content()` is the correct Playwright API for this.

