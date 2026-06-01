---
name: api_ok-status-keyword-collision
description: api_ok must not declare keyword-only status param — collides with body field
enabled: true
tags: [bug, api_response, convention]
created: 2026-05-30T11:30:39Z
updated: 2026-05-30T11:30:39Z
---

# `api_ok` body fields vs HTTP status

`lib/api_response.py::api_ok` returns HTTP **200** unconditionally. It
takes only `data` and `**extras` — there is intentionally NO
keyword-only HTTP-status parameter, because callers commonly want to
emit `status` as a body field:

```python
return api_ok(taskId=task_id, status='aborting')   # 200 + {ok, taskId, status: 'aborting'}
return api_ok(taskId=tid, status='deleted')        # 200 + body status field
```

A previous version declared `def api_ok(data=None, *, status: int = 200,
**extras)`. That swallowed `status='aborting'` as the HTTP code → Quart
crashed in `make_response` with
`ValueError: invalid literal for int() with base 10: 'aborting'`.

Rules:
- Use `api_created` for 201, `api_error(..., status=N)` for 4xx/5xx.
- Never re-introduce a keyword-only HTTP-status parameter on `api_ok`
  without renaming the slot (e.g. `_http_status`) so it can't collide
  with body field names.
- Regression covered by `test_api_ok_status_is_body_field` in
  `tests/test_api_response.py`.

