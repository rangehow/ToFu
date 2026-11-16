#!/usr/bin/env python3
"""Provider-boundary observer work at the provider-ingress isolation boundary.

The first content/thinking delta requests the boundary's observer work (the
segment mirror the Turn projection copies, and presence), but that work must
not run on the callback stack that drains the upstream model stream.
``stream_llm_response`` therefore coalesces all in-flight requests and runs it
once, as soon as that provider dispatch has returned. The boundary writes no
task-result row (``test_conversation_attempt_sampled_checkpoints.py``).
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _make_task():
    import threading
    return {
        'id': 'ckpt-test-1234',
        'convId': 'cv-ckpt',
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'aborted': False,
        'status': 'running',
        '_userId': 1,
        'created_at': time.time(),
        'content_lock': threading.Lock(),
        'events': [],
        'events_lock': threading.Lock(),
    }


def test_first_delta_requests_a_mirror_refresh_settled_at_provider_boundary(
        monkeypatch):
    import lib.tasks_pkg.manager as m
    import lib.tasks_pkg.manager._stream as st

    calls = []
    # ★ Package-split binding: ``stream_llm_response`` lives in the ``_stream``
    #   submodule, which imports ``refresh_segment_mirror`` (from ._sync) and
    #   ``append_event`` (from ._events) into ITS OWN namespace at import and
    #   calls them directly. Patching the ``manager`` facade attribute rebinds
    #   only the facade's re-export, NOT ``_stream``'s binding, so the stub
    #   never installs (the pre-split monolith made these patchable on the
    #   facade; the split moved the true binding site). Patch ``_stream``.
    #   ``dispatch_stream`` is also owned by ``_stream`` as an imported
    #   dependency, so patch the same concrete binding.
    monkeypatch.setattr(st, 'refresh_segment_mirror', lambda task: calls.append(task['content']))
    # Don't actually persist events to DB during the test.
    monkeypatch.setattr(st, 'append_event', lambda task, ev: task['events'].append(ev))

    def _fake_dispatch_stream(body, *, on_content=None, on_thinking=None, **kwargs):
        # The first delta samples the boundary work immediately, but it is
        # isolated until this provider function has returned.
        on_content('hello ')
        assert calls == []
        on_content('world')
        assert calls == []
        # dispatch_stream returns (message_dict, finish_reason, usage).
        return {'content': 'hello world', 'tool_calls': []}, 'stop', {'completion_tokens': 2}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    msg, finish, usage = m.stream_llm_response(task, {'model': 'test-model'})

    assert task['content'] == 'hello world'
    assert calls == ['hello world'], 'the mirror refresh did not settle at the provider boundary'


def test_rapid_deltas_coalesce_into_one_boundary_refresh(monkeypatch):
    """Rapid deltas coalesce into one post-provider mirror refresh."""
    import lib.tasks_pkg.manager as m
    import lib.tasks_pkg.manager._stream as st

    calls = []
    # See the first test for why these patch _stream (real binding site),
    # including its dispatcher dependency.
    monkeypatch.setattr(st, 'refresh_segment_mirror', lambda task: calls.append(task['content']))
    monkeypatch.setattr(st, 'append_event', lambda task, ev: task['events'].append(ev))

    def _fake_dispatch_stream(body, *, on_content=None, on_thinking=None, **kwargs):
        for _ in range(10):
            on_content('x')
            assert calls == []
        return {'content': 'x' * 10, 'tool_calls': []}, 'stop', {}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    m.stream_llm_response(task, {'model': 'test-model'})

    # Exactly one refresh after provider return; no callback ran it.
    assert len(calls) == 1, f'expected 1 coalesced refresh, got {len(calls)}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
