---
name: dispatch-stream-vs-chat-return-shape
description: dispatch_stream returns assistant DICT; dispatch_chat/smart_chat return content STRING — unwrap msg.get('content') when migrating sync→stream
enabled: true
tags: [llm, translate, bug-pattern, dispatch]
created: 2026-05-09T05:36:36Z
updated: 2026-05-09T05:36:36Z
---

# dispatch_stream vs dispatch_chat return-shape mismatch

## The trap

`lib/llm_dispatch/api.py` exposes two non-equivalent return shapes:

| Function | Returns |
|---|---|
| `dispatch_chat(...)` / `smart_chat(...)` | `(content_str: str, usage_dict: dict)` |
| `dispatch_stream(...)` | `(msg_dict: dict, finish_reason: str, usage_dict: dict)` |

`msg_dict` is the assistant message constructed at
`lib/llm_client.py:2513` — `{'role': 'assistant', 'content': '...', ...}`.

When migrating a call site from non-streaming to streaming (e.g. to
get a live preview via `progress_cb`), it's easy to write
`c = _stream_msg or ''` — but `_stream_msg` is a **dict**, not a string.
Downstream regex / string ops then crash with
`TypeError: expected string or bytes-like object, got 'dict'`.

## Real example (fixed 2026-05-09)

`routes/translate.py` `_translate_one_chunk()` had a streaming branch
that did `c = _stream_msg or ''`, then later `re.sub(r'</?translate>', '', c)`
blew up. Fix:

```python
_stream_msg, _finish, _usage = dispatch_stream(...)
if isinstance(_stream_msg, dict):
    c = _stream_msg.get('content', '') or ''
else:
    c = _stream_msg or ''
```

## Rule

Whenever you swap `smart_chat`/`dispatch_chat` → `dispatch_stream`
(or write a new caller of `dispatch_stream`), unwrap the assistant
message dict before any string/regex op:

```python
content = msg_dict.get('content', '') if isinstance(msg_dict, dict) else (msg_dict or '')
```

Also note `msg_dict` may carry `tool_calls`, `reasoning_content` —
don't accidentally lose those when extracting content.

