"""Route benchmark model calls through the running Tofu server's model routing.

A benchmark process cannot attach to the production storage authority (it is
owned by the running server and refuses foreign engine builds), so it cannot
build its own dispatcher slots. Every arm instead calls the server's
OpenAI-compatible endpoint, which applies Tofu's model-routing v2, provider
keys and accounting. ``tools: []`` selects one direct provider completion
(no task-backed agent loop), per ``routes/compat_openai.py``.

``install()`` replaces ``lib.llm_dispatch.api.dispatch_chat`` for an in-process
run of Tofu's slide recipe; ``chat()`` serves the judge and auditor.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get(
    'SLIDES_BENCH_ENDPOINT', 'http://127.0.0.1:15000/v1/chat/completions')
DEFAULT_MODEL = os.environ.get('SLIDES_BENCH_MODEL', 'gpt-5.5')
_REQUEST_DEADLINE_S = 900
_TRANSIENT_ATTEMPTS = 4


def chat(messages, *, model: str | None = None, max_tokens: int = 4096,
         temperature: float = 0.2) -> tuple[str, dict]:
    body = json.dumps({
        'model': model or DEFAULT_MODEL, 'messages': messages,
        'max_tokens': int(max_tokens), 'temperature': float(temperature),
        'tools': [], 'stream': False,
    }).encode('utf-8')
    last = None
    for attempt in range(_TRANSIENT_ATTEMPTS):
        request = urllib.request.Request(
            ENDPOINT, data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(
                    request, timeout=_REQUEST_DEADLINE_S) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:400]
            last = RuntimeError(f'HTTP {exc.code}: {detail}')
            if exc.code < 500 and exc.code != 429:
                raise last from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        else:
            choice = (payload.get('choices') or [{}])[0]
            content = (choice.get('message') or {}).get('content') or ''
            usage = dict(payload.get('usage') or {})
            usage['model'] = payload.get('model')
            return content, usage
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'model endpoint failed: {last}')


def _dispatch_chat(messages, *, max_tokens=4096, temperature=0,
                   prefer_model=None, **_ignored):
    return chat(messages, model=prefer_model or None,
                max_tokens=max_tokens, temperature=temperature)


def install(model: str | None = None) -> None:
    """Point Tofu's in-process dispatch seam at the server endpoint."""
    global DEFAULT_MODEL
    if model:
        DEFAULT_MODEL = model
    import lib.llm_dispatch.api as api
    api.dispatch_chat = _dispatch_chat
