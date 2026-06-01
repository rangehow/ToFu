---
name: pymupdf-thread-safety-core-dump
description: Root cause of core dump in reading mode report generation: concurrent pymupdf access from fetch ThreadPool workers parsing PDF URLs
enabled: true
tags: [bug, pymupdf, thread-safety, crash, paper, report]
created: 2026-05-22T06:41:18Z
updated: 2026-05-22T06:41:18Z
---

# PyMuPDF Thread-Safety Core Dump in Reading Mode Reports

## Root Cause
PyMuPDF (MuPDF C library) is **explicitly not thread-safe**. The official docs state:
> "PyMuPDF does not support running on multiple threads - doing so may cause
> incorrect behaviour or even crash Python itself."

Report generation triggers concurrent PDF access because:
1. `_execute_report_tool('web_search')` runs parallel searches via `run_batch_concurrent`
2. Each search hits academic paper URLs (arXiv PDFs, etc.)
3. `fetch_page_content()` calls `extract_pdf_text()` → `pymupdf.open()` in ThreadPoolExecutor workers
4. Multiple PDF fetches run concurrently on different threads → native crash (no Python traceback)

Normal chat searches rarely hit multiple PDFs simultaneously, which is why the crash
is specific to report generation on academic papers.

## Fix Applied
Added `PYMUPDF_LOCK = threading.Lock()` in `lib/pdf_parser/_common.py` and wrapped ALL
`pymupdf.open()` / page operations behind `with PYMUPDF_LOCK:` in:
- `lib/pdf_parser/text.py` (Strategy 1 pymupdf4llm + Strategy 2 raw get_text)
- `lib/pdf_parser/core.py` (parse_pdf image extraction)
- `lib/pdf_parser/images.py` (render_pdf_pages)
- `routes/paper.py` (_extract_paper_figures)

Also reduced `routes/paper.py` PaperSearch concurrency from 5→2 and changed
`lib/search/orchestrator.py` `fetch_pool.shutdown(wait=False)` → `shutdown(wait=True, cancel_futures=True)`
to prevent thread accumulation.

## Diagnostic Signature
- No Python exception in logs before crash
- Last log entry is fetch/search thread activity, then 30-40s gap, then server restart
- Only happens during report generation on academic papers (many PDF URLs)
- Does NOT happen during normal chat web searches (rare PDF hits)

