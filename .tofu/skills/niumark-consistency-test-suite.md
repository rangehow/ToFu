---
name: niumark-consistency-test-suite
description: NiuMark v5: domain-agnostic PDF converter (13,177 PDFs 100% robustness, 100.0% fixable clean rate), font-metric driven classification
enabled: true
tags: [niumark, testing, pdf, consistency]
created: 2026-04-10T10:53:48Z
updated: 2026-04-11T03:30:23Z
---

# NiuMark v5 — Domain-Agnostic PDF Converter

## Location
`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/niumark/`

## Architecture (v5 redesign)

### Core Principle
**Font-metric driven, domain-agnostic.** No hardcoded keywords like "Abstract", "Introduction", "Figure 1".
Classification uses only: relative font size, weight/style, position, geometry.

### Key Components
- `FontAnalyzer` — document-wide font analysis: body_size, size_levels, math_fonts
- `HeadingDetector` — heading hierarchy from font size tiers (title → h1 → h2 → h3)
- `LayoutAnalyzer` — margins, column detection, centering, indentation
- `VisualRegionDetector` — equations (math-font ratio), figures/tables (multilingual caption regex)
- `_classify()` — header/footer → title/heading → meta → list → quote → code → paragraph
- `_sanitize_nm()` — 11-step post-processing for formatting artifacts

### Tag Changes (v4 → v5)
- `@authors` → `@meta` (domain-agnostic: authors, dates, org, version, etc.)
- Added: `@subtitle`, `@quote`, `@code`, `@sidebar`, `@watermark`, `@h3`
- Caption regex: multilingual (English/German/Spanish/Chinese/Russian/Japanese)
- `_classify()` never checks for "Abstract", "Introduction" etc. — pure font metrics

## Test Commands
```bash
# Full robustness test (all PDFs, ~64 min)
python test_all_pdfs.py --workers 24 --timeout 300

# Consistency on existing outputs (seconds)
python test_consistency.py --nm-dir data/test_output --limit 0

# Fresh conversion + consistency (uses forkserver pool)
python test_consistency.py --fresh --limit 500
```

## Current Results (2026-04-11, v5)
- **13,177 PDFs**: 100.0% robustness, 0 errors, 0 timeouts
- **Fixable clean rate**: 100.0% (13,177/13,177)
- **Title detection**: 98.9%
- **Performance**: Mean 5.18s, Median 2.87s, P95 17.2s

## Issue Categories
- **Fixable (all 0)**: empty_bold, unbalanced_bold, mismatched_cols, duplicate_title, empty_paragraph, bold_in_header, huge_header, huge_meta
- **Unfixable (source PDF)**: unicode_replacement (20%), no_title (1.1%), too_short (1 file)

## Key Design Decisions
1. `HeadingDetector` uses font size tiers relative to body_size — works for any document
2. `@meta` replaces `@authors` — metadata is metadata, not always author names
3. Caption detection uses multilingual regex, not English-only "Figure N"
4. Header/footer detection is purely positional (top 8%, bottom 5%) + size
5. MuPDF stderr suppressed via C-level fd redirect (`_suppress_stderr`)
6. ProcessPoolExecutor not fork-safe with MuPDF; use forkserver or sequential for testing

