---
name: llm-string-coercion-traps
description: Two recurring bug patterns: LLM emits stringified ints in tool args, and Anthropic returns content as a list of blocks where callers expect str
enabled: true
tags: [llm, tool-args, type-coercion, translate, read_files]
created: 2026-05-11T08:28:52Z
updated: 2026-05-11T08:28:52Z
---

# LLM string-coercion traps (2026-05-11)

Two distinct shape mismatches between what the model emits / API returns
and what our code expects. Both observed live in `logs/error.log`.

## 1. Stringified integers in tool args

`read_files` with `{"start_line": "70", "end_line": "76"}` (strings) crashed
`_merge_same_file_ranges()` at `lib/project_mod/read_tools.py:310` with
`TypeError: can only concatenate str (not "int") to str` when computing
`sl <= prev_e + GAP_THRESHOLD`.

**Fix**: coerce all numeric tool-arg fields at the entry of
`tool_read_files()` — `int(v)` with `(ValueError, TypeError)` → `None`
fallback. Anywhere we walk JSON-parsed tool args, never trust types
beyond what the JSON schema declared. The model will produce ANY shape
(see also `compaction-tolerates-malformed-tool-call-args` memory for
bare-string `reads` specs).

## 2. Anthropic-style list-of-blocks where callers expect str

`smart_chat` / `dispatch_chat` / `chat()` return `(content, usage)`
where `content` may be a **list of structured blocks** like
`[{"type": "text", "text": "..."}, {"type": "thinking", ...}]` for
Anthropic responses, or a dict in some edge cases. In `routes/translate.py:615`
this hit `re.sub(r'</?translate>', '', c)` → `TypeError: expected string or
bytes-like object, got 'dict'`.

**Fix**: at the boundary where downstream code does `re.sub` / `.strip()`
on `c`, normalize first:
```python
if isinstance(c, list):
    c = ''.join(b.get('text', '') for b in c
                if isinstance(b, dict) and b.get('type') == 'text')
elif isinstance(c, dict):
    c = c.get('text', '') or c.get('content', '') or ''
if not isinstance(c, str):
    c = str(c) if c is not None else ''
```

## Rule of thumb

Any text-returning LLM call site must defensively coerce before regex /
string ops. Don't trust the type contract — providers vary and the
unwrap path in `dispatch_stream` / `chat()` doesn't always flatten
multimodal blocks.

