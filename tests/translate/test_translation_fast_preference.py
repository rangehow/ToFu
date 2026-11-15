"""Every translation dispatch prefers Models Artificial Analysis measures as fast.

On 2026-11-11 translation picks rotated through the whole cheap tier on 429s
and landed on slow reasoning models (kimi-k3.1-pro: median 13.6 s per call,
deepseek-v4-pro on one route: 42 s) while fast flash models were available.
The preference never narrows eligibility; the dispatcher owns its fallback.
"""
import pytest

import lib.llm_dispatch as dispatch_facade
from lib.translate.engine._engine import _dispatch_translation_candidate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["stream", "strict_chat", "smart_chat"])
def test_every_translation_dispatch_prefers_fast_models(monkeypatch, mode):
    seen = {}

    def fake_chat(*_args, **kwargs):
        seen.update(kwargs)
        return "ok", {}

    def fake_stream(*_args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(dispatch_facade, "dispatch_stream", fake_stream)
    monkeypatch.setattr(dispatch_facade, "dispatch_chat", fake_chat)
    monkeypatch.setattr(dispatch_facade, "smart_chat", fake_chat)

    call = dict(
        chunk_label="", max_tokens=64, timeout=5, max_retries=0,
        excluded_models=set(), progress_cb=None,
        deadline_exceeded=lambda: False,
        remaining_deadline_seconds=lambda: 5.0,
        stream=mode == "stream", strict_model=mode == "strict_chat",
        prefer_model="m" if mode == "strict_chat" else None,
    )
    try:
        _dispatch_translation_candidate([{"role": "user", "content": "x"}], **call)
    except RuntimeError:
        pass
    assert seen.get("prefer_fast") is True
