---
name: xuecheng-mcp-code-block-language-normalization
description: xuecheng-mcp normalizes fenced code-block languages (json/python/mermaid → JSON/Python/Mermaid) at parse time
enabled: true
tags: [xuecheng-mcp, converter, convention]
created: 2026-05-08T10:40:49Z
updated: 2026-05-08T10:40:49Z
---

# xuecheng-mcp: Code-block Language Normalization

## Why
Citadel's code_block validator is **case-sensitive** and only accepts
canonical language names (`Python`, `JSON`, `Mermaid`, `C++`, …). LLMs
near-universally emit lowercase fenced blocks (```python, ```json, ```mermaid),
which used to fail `create_doc` / `update_doc_by_md` with:

    code_block.attrs.language "python" 不在支持的语言列表中

## Fix (2026-05)
`lib._converter_common.normalize_code_language(raw)` maps common
aliases → canonical, falls back to `Plain Text` for unknown tokens
(so the doc still creates, just without highlighting).

Wired into both code-block parse paths in `_converter_parser.py`:
- Standard fenced ``` block (regex widened to `[\w+#.\-]*` so `c++` / `c#` are captured)
- `:::code_block{language=...}` macro

The strict validator still rejects bad languages — normalization is
**parser-only**. Direct JSON callers (e.g. anyone constructing
ProseMirror JSON without going through `citadel_md_to_json`) keep the
original safety net (`test_validator_rejects_bad_code_lang`).

## Files
- `src/xuecheng_mcp/_converter_common.py` — `_CODE_LANGUAGE_ALIAS` table + `normalize_code_language()`
- `src/xuecheng_mcp/_converter_parser.py` — uses normalizer at both code_block sites; widened fence regex
- `tests/test_converter.py` — `test_code_block_language_normalized` (16 cases) + `test_code_block_macro_language_normalized`

## Adding a new alias
Append to `_CODE_LANGUAGE_ALIAS` in `_converter_common.py`. Key must be
lowercase. Value must already be in `SUPPORTED_CODE_LANGUAGES` in
`_converter_validator.py` — or the normalizer will produce a value the
validator then rejects, defeating the point.

