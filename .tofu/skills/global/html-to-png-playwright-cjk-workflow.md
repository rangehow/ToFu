---
name: html-to-png-playwright-cjk-workflow
description: Rendering HTML posters to PNG on headless Linux with Chinese + emoji support
enabled: true
tags: [playwright, html-to-png, chinese, emoji, fonts, headless]
created: 2026-04-20T03:44:08Z
updated: 2026-04-20T03:44:08Z
---

# HTML → PNG via Playwright + CJK/Emoji fonts

When user asks to convert an HTML design to PNG (posters, social cards, slide exports) on a headless Linux machine:

## Workflow

1. **Check tooling**: `python3 -c "from playwright.sync_api import sync_playwright"` — if Playwright + Chromium installed, use it (fastest, best CSS support).

2. **Check fonts** before rendering any CJK content:
   ```
   fc-list :lang=zh | head
   ```
   Headless Linux images usually have **zero CJK fonts** → all Chinese renders as tofu `□`.

3. **Install CJK + emoji fonts once** (user scope, no sudo needed):
   ```bash
   mkdir -p ~/.local/share/fonts
   # Noto Sans SC (Simplified Chinese, variable weight)
   curl -sSL -o ~/.local/share/fonts/NotoSansSC.ttf \
     "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
   # Noto Color Emoji
   curl -sSL -o ~/.local/share/fonts/NotoColorEmoji.ttf \
     "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf"
   fc-cache -f ~/.local/share/fonts/
   ```
   Warning: jsdelivr URL paths for noto-emoji change; prefer github raw. Verify with `file <font>` that it's TrueType data, not a 49-byte error page.

4. **CSS font stack** should explicitly list both:
   ```css
   font-family: "Noto Sans SC", "Noto Color Emoji", -apple-system, sans-serif;
   ```

5. **Render script** (Playwright):
   ```python
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       b = p.chromium.launch()
       ctx = b.new_context(viewport={"width":1080,"height":1440}, device_scale_factor=2)
       page = ctx.new_page()
       page.goto(html_path.as_uri(), wait_until="networkidle")
       page.evaluate("async () => { await document.fonts.ready; }")  # critical!
       page.query_selector(".poster").screenshot(path="out.png")  # element-scoped strips body padding
       b.close()
   ```
   Key bits:
   - `device_scale_factor=2` for retina-quality PNG (doubles pixel count)
   - `wait_until="networkidle"` + `document.fonts.ready` — without this, screenshots can capture mid-font-load
   - Element-scoped screenshot (`.screenshot(path=...)` on a locator) trims body padding cleanly

## Gotchas

- **Noto Color Emoji is a bitmap font** (CBDT table). Very large sizes (>72px) may look blurry or fail to render in some layouts. Wrap giant emoji in a `<span style="font-size:72px">` and inline-style it smaller than the surrounding heading.
- **`@font-face url("fonts/xxx.ttf")` alone is not reliable** on headless Chromium when running via `file://` — the safe path is system-install via `fc-cache`.
- **`file://` URLs** work but the file must be on a path accessible inside the headless Chromium sandbox (tmp/home/project are fine; weird FUSE mounts might not be).
- Default Chromium launch args are fine on most setups; add `["--no-sandbox"]` only if running as root in CI.

## Multi-root trap

In a multi-root workspace, when `apply_diff`/`write_file` complain "File not found" for an absolute path, it means the path is outside any registered workspace root. Use `rootname:relative/path` prefix, or fall back to `run_command` with `python3 -c "..."` for surgical edits.

