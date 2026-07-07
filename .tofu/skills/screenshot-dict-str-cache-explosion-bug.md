---
name: screenshot-dict-str-cache-explosion-bug
description: Bug CLASS: opaque blob (base64/binary) enters text message stream → 1M-token overflow. 4-layer defense: read_files type-routing after path-resolve, clamp_tool_result_text hard ceiling, reactive in-place truncate, property test.
enabled: true
tags: [bug-fix, screenshot, cache, base64, token-explosion, context-window, streaming-executor, dedup]
created: 2026-04-13T05:06:32Z
updated: 2026-06-17T10:39:09Z
---

# Binary/Base64 Blob → Text-Stream Overflow Bug CLASS

This is ONE bug class with many ingress seams, not separate bugs:
**an opaque large blob (base64 image / decoded binary) enters the text
message stream and tokenises to ~1M tokens → fatal HTTP 400.** Fix the
CLASS, not the seam of the week.

## Known historical occurrences (all same shape)
- 2026-04-05 micro_compact — image_url not stripped
- 2026-04-13 dedup/prefetch cache — `__screenshot__` dict str()-ified (843K base64 as text)
- 2026-05-04 reactive_compact — strip gated on bytes not tokens
- 2026-06-16 conv **mqgfkmxy** — relative-path PNG decoded as text (THE big one)

## conv mqgfkmxy root cause (the missed case)
`read_files` had TWO routes split on PATH SHAPE, not file type:
- absolute path → `_read_absolute_file` → `read_local_file` → image/PDF/Office detection → `__screenshot__` dict ✓
- **relative path → `_read_project_file` → `open(target, errors='replace')` → NO binary detection** ✗

Agent read `static/icons/tofu-*.png` by RELATIVE path. Four sub-512KB PNGs
(under the `MAX_FILE_SIZE` guard) decoded to ~1.7M chars of U+FFFD → ~1.36M
tokens → HTTP 400. Bigger icons (>512KB) were saved by the size guard.
Ratio tell: 1.7M chars ÷ 1.36M tok ≈ 1.26 char/tok = binary-as-text, NOT
prose (~4) and NOT properly-counted images (~800 tok flat).

## The 4-layer fix (2026-06-17)
**L1 root — `lib/project_mod/read_tools.py::_read_project_file`**: classify
file type AFTER `_safe_path` resolution, identical for rel & abs. Image/PDF/
Office ext → `read_local_file()` (→ `__screenshot__`). Then a content-based
binary sniff (>30% non-printable in first 8KB, mirrors `file_reader._read_text`)
→ `[Binary file: …]` stub. `tool_read_files` relative branch now collects the
returned `__screenshot__` dict into `image_results`/`__batch_images__` like the
abs branch. `.svg`/minified JS are real text → pass the sniff, read normally.

**L2 catch-all — `compaction/_budget.py::clamp_tool_result_text`** (NEW): hard
ceiling `_SINGLE_RESULT_HARD_CEILING_CHARS=800_000` on ANY single tool-result
text, **including budget-exempt read_files** (Layer 0 skips it; this does NOT).
Wired at the text-commit boundary in `tool_dispatch.py` right before
`messages.append(...)`. Non-str (image dicts) pass through. This makes the
class unrepresentable: any future leak gets clamped to degraded-but-alive.

**L4 remediate — `compaction/_reactive.py::_truncate_largest_message`** (NEW):
Phase 0.5 shrinks the single largest text message IN PLACE. Whole-message
dropping (`_head_truncate`) cannot fix a single fat tail message — this can.

**Property test — `tests/test_binary_blob_text_stream_guard.py`**: for EVERY
registered tool, an oversized blob result is either a `__screenshot__` dict or
≤ ceiling. Catches a future 6th ingress point that per-incident tests miss.

## Key Pattern / Guardrails
- File-type dispatch MUST be independent of path shape (abs vs rel) — key on
  the resolved file, never on `_is_absolute_path`.
- Any `str(content)` on a tool result MUST check `__screenshot__` first.
- No single tool result text may exceed the hard ceiling — backstop everything
  at the commit boundary, with NO per-tool exemption.
- Images cost ~85–1500 tok (resolution-based), NOT base64 byte size — on a
  vision model an uploaded image is cheap; a million tokens means a blob
  leaked as TEXT, never "the images are too big."

