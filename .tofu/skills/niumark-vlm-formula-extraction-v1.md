---
name: niumark-vlm-formula-extraction-v1
description: VLM-based formula extraction module (vlm_formula.py) integrated into NiuMark translation pipeline
enabled: true
tags: [niumark, vlm, formula, extraction, pipeline, sglang]
created: 2026-04-17T07:03:01Z
updated: 2026-04-17T07:03:01Z
---

# NiuMark VLM Formula Extraction

## Architecture
- `niumark/vlm_formula.py` (438 lines) — VLM-based math extraction
- Integrated into `translate_engine.py` as "Step 0a+" between PDF→NiuMark and logical reorder
- Uses 4 Qwen3.5-fp8 endpoints on sglang (same nodes as text LLM)
- Must re-stamp `shadow.json` after modifying `.nm` file (artifact_integrity)

## How it works
1. MuPDF extraction (Step 0a) gives bboxes, fonts, layout → `.nm` with `$latex$` and `[m:xxx]`
2. VLM enhancement (Step 0a+) sends page images to VLM, gets back text+LaTeX
3. Alignment: match VLM paragraphs to `.nm` elements by element ID `{#p3_e28}` and text similarity
4. Replace broken `$latex$` and `[m:xxx]` markers with VLM's correct LaTeX

## Key design decisions
- Uses `chat_template_kwargs: {"enable_thinking": False}` for sglang Qwen3.5
- Element IDs from `.nm` text (`{#p3_e28}`) used to find page number from shadow
- Text similarity via `_subsequence_ratio()` to match paragraphs
- Positional alignment: i-th math slot → i-th VLM math expression

## BabelDOC comparison (they do NOT use VLM)
- BabelDOC: formulas are opaque PDF glyph boxes, never converted to LaTeX
- Placeholders `{v1}` during translation, original PDF ops repositioned after
- Our advantage: LLM sees math context → better translations

## Test results
- PhyPlan (8pp): 22 math replaced, 33.6s VLM, 73.3s total
- MDLM (49pp): 67 math replaced, 112.5s VLM, 430.9s total
- Remaining issues: LaTeX→Typst conversion for `\langle` (`angle.l`) patterns

## Files modified
- `/mnt/.../niumark/niumark/vlm_formula.py` — new module
- `/mnt/.../niumark/niumark/translate_engine.py` — Step 0a+ integration + artifact re-stamp

