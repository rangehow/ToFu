---
name: browser-extension-fullpage-screenshot-cdp
description: Browser-extension screenshot is fully BACKGROUND via CDP (full-page + viewport-CDP); only last-resort captureVisibleTab activates the tab. No tab-switch flicker.
enabled: true
tags: [browser-extension, screenshot, cdp, chrome-debugger, architecture]
created: 2026-05-04T13:04:20Z
updated: 2026-05-04T13:04:20Z
---

# Full-Page Screenshot via CDP (v4.2.0+)

`browser_screenshot` defaults to **full-page** capture using the Chrome DevTools
Protocol, not just the visible viewport.

## Extension side (`browser_extension/background.js`)

- `cmdScreenshotTab({tabId, format, fullPage=true})`.
- `_screenshotFullPageCDP(tabId, format, quality)`:
  1. `chrome.debugger.attach({tabId}, '1.3')`.
  2. `Page.enable`.
  3. `Page.getLayoutMetrics` → use `cssContentSize` (Chromium 90+), fall back
     to `contentSize`.
  4. `Page.captureScreenshot` with
     `{captureBeyondViewport: true, fromSurface: true, clip: {0,0,W,H,1}}`.
  5. Height clipped at `FULL_PAGE_MAX_HEIGHT_PX = 16000` (Chrome texture cap).
  6. `chrome.debugger.detach` in `finally`.
- `_screenshotViewportCDP(tabId, format, quality)` (v4.4.0+): same CDP attach,
  but `Page.captureScreenshot` with `captureBeyondViewport:false` — grabs the
  tab's CURRENT viewport **in the background, without activating/focusing it**.
- **Fallback chain (v4.4.0+):** full-page request →
  `_screenshotFullPageCDP` → (on fail) `_screenshotViewportCDP` → (on fail)
  `_screenshotViewport`. A `fullPage:false` request goes straight to
  `_screenshotViewportCDP` → (on fail) `_screenshotViewport`. Only the LAST
  resort `_screenshotViewport` (chrome.tabs.captureVisibleTab) activates the
  tab — that's the visible "navigation"/flicker we now avoid in the common case.
  Each fallthrough records `fallbackReason`.
- `COMMAND_TIMEOUT_OVERRIDES = {screenshot_tab: 55000}` because full-page
  captures can legitimately exceed the default 25 s cap.
- Requires `"debugger"` permission in `manifest.json` (already present).
- Chrome shows a yellow "extension is debugging this browser" banner while
  attached; it disappears as soon as `detach` runs.

## Server side

- `lib/tools/browser.py` — `browser_screenshot` has a `fullPage` bool param
  (default true).
- `lib/browser/handlers.py::_handle_screenshot`:
  - Passes `fullPage=false` to the extension only when the caller explicitly
    opts out (older extensions keep working).
  - Command timeout: 60 s when full-page, 15 s viewport.
  - Compression: max height **12000 px** for full-page screenshots, **3000 px**
    for viewport. Anything larger is resized with LANCZOS + JPEG q=70.
  - Returns metadata: `fullPage`, `width`, `height`, `contentHeight`,
    `truncatedHeight`, `fallbackReason`.
  - Logs a warning when the extension reports `fallbackReason`.
- `lib/browser/display.py` — label says "📸 Screenshot (full page)" or
  "(viewport)" based on `fn_args.fullPage`.

## Why not scroll-and-stitch

Simpler, single native call, no double-drawn fixed headers, no per-step
`captureVisibleTab` quota issues. The only downside is the debugger banner,
which is brief and far less disruptive than a page scrolling while the user
watches.

