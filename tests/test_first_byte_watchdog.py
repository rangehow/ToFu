"""Tests for the first-byte (TTFT) watchdog + waiting heartbeat.

WHY
---
2026-07-27 incident (conv ms2gb19gfdco20, task 53c65134): the upstream
gateway accepted the request and then sent ZERO bytes for the full 300s
read timeout. During that window the client had no signal at all — no
error, no retry, no phase event — so the frontend could only show the
static "Sent to {model}, waiting…" phase for 5+ minutes even though the
slot pool already knew the line was erroring (4 consecutive errors →
cooldown warnings from OTHER tasks' threads).

The fix has two halves, both pinned here:

  1. FirstByteWatchdog (lib/llm/_transport.py): bounds the time from
     request-send to first SSE byte (TOFU_LLM_TTFT_TIMEOUT, default 180s)
     and kills the attempt by closing the response — the transport
     translates the kill into ``FirstByteTimeoutError`` which escapes the
     same-key retry loop straight to the dispatch layer, where it is a
     normal upstream soft error (record_error → pair exclusion → slot
     rotation, the exact path a read timeout already takes). While
     waiting it fires heartbeat beats (TOFU_LLM_FIRST_BYTE_HEARTBEAT_S,
     default 20s) via ``on_first_byte_wait``.
  2. Waiting heartbeat (lib/tasks_pkg/manager/_stream.py::_on_waiting):
     the dispatch layer threads the beats up with the current slot's
     context (cooldown_reason / last_error_msg / consecutive_errors) and
     the manager emits a transient ``retrying`` PHASE event carrying
     ``stream.phase.waitingFirstByte[Reason]`` detailKeys (+ typed
     reasonKey) so the HUD localizes AND refreshes per beat (the
     retrying branch's phaseKey includes ``attempt``).

Covers: the watchdog unit (kill/beat/notify/cancel), both transports
(sync requests + async httpx), both dispatch loops (failover + honest
retry reason), the manager heartbeat emission, and the i18n mapping.

Run:  pytest tests/test_first_byte_watchdog.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═════════════════════════════════════════════════════════════════════
#  A. FirstByteWatchdog unit — kill / beat / notify / cancel
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFirstByteWatchdogUnit:
    def test_kill_fires_at_timeout_and_trips(self):
        from lib.llm._transport import FirstByteWatchdog
        killed = []
        wd = FirstByteWatchdog(timeout=0.15, heartbeat_interval=0,
                               on_beat=None, on_kill=lambda: killed.append(1))
        wd.start()
        time.sleep(0.45)
        assert killed == [1]
        assert wd.tripped is True

    def test_beats_fire_at_interval_before_kill(self):
        from lib.llm._transport import FirstByteWatchdog
        beats = []
        wd = FirstByteWatchdog(timeout=0.5, heartbeat_interval=0.08,
                               on_beat=lambda e: beats.append(e), on_kill=None)
        wd.start()
        time.sleep(0.30)
        wd.cancel()
        # ~3 beats in 0.30s at 0.08s cadence (allow scheduler slack).
        assert 2 <= len(beats) <= 5
        assert beats == sorted(beats)  # elapsed monotonic

    def test_notify_first_byte_stops_kill_and_beats(self):
        from lib.llm._transport import FirstByteWatchdog
        killed, beats = [], []
        wd = FirstByteWatchdog(timeout=0.12, heartbeat_interval=0.05,
                               on_beat=lambda e: beats.append(e),
                               on_kill=lambda: killed.append(1))
        wd.start()
        wd.notify_first_byte()
        time.sleep(0.30)
        assert killed == []
        assert beats == []
        assert wd.tripped is False

    def test_cancel_stops_everything(self):
        from lib.llm._transport import FirstByteWatchdog
        killed, beats = [], []
        wd = FirstByteWatchdog(timeout=0.10, heartbeat_interval=0.04,
                               on_beat=lambda e: beats.append(e),
                               on_kill=lambda: killed.append(1))
        wd.start()
        wd.cancel()
        time.sleep(0.25)
        assert killed == []
        assert beats == []

    def test_beat_exception_does_not_kill_watchdog(self):
        """A raising on_beat must be swallowed (logging discipline §2.2 —
        a HUD callback bug must never kill the request watchdog)."""
        from lib.llm._transport import FirstByteWatchdog

        def _bad_beat(_e):
            raise RuntimeError('boom')

        killed = []
        wd = FirstByteWatchdog(timeout=0.15, heartbeat_interval=0.04,
                               on_beat=_bad_beat, on_kill=lambda: killed.append(1))
        wd.start()
        time.sleep(0.35)
        assert killed == [1]  # kill still fired after beat exceptions


# ═════════════════════════════════════════════════════════════════════
#  B. Sync transport (lib/llm/stream.py) — kill → FirstByteTimeoutError
# ═════════════════════════════════════════════════════════════════════

class _BlockingResp:
    """A 200 response whose body iterator blocks until close() — the wedged
    upstream shape from the incident (headers sent, zero body bytes)."""

    def __init__(self):
        self.headers = {}
        self.status_code = 200
        self.encoding = None
        self._closed = threading.Event()

    def iter_lines(self, decode_unicode=False):
        self._closed.wait(timeout=15)
        raise ValueError('I/O operation on closed file')
        yield  # pragma: no cover - unreachable, keeps this a generator

    def close(self):
        self._closed.set()


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *a, **k):
        return self._resp


@pytest.mark.unit
class TestSyncTransportWatchdog:
    def _call_once(self, monkeypatch, resp, on_first_byte_wait=None):
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(resp))
        monkeypatch.setattr('lib.llm._transport.TTFT_TIMEOUT', 0.20)
        monkeypatch.setattr('lib.llm._transport.FIRST_BYTE_HEARTBEAT_S', 0.05)
        from lib.llm.stream import _stream_chat_once
        return _stream_chat_once(
            {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
            api_key='sk-x', base_url='http://fake.local/v1',
            on_first_byte_wait=on_first_byte_wait, log_prefix='[t]')

    def test_wedged_attempt_killed_into_first_byte_timeout(self, monkeypatch):
        from lib.llm_errors import FirstByteTimeoutError
        beats = []
        t0 = time.monotonic()
        with pytest.raises(FirstByteTimeoutError):
            self._call_once(monkeypatch, _BlockingResp(),
                            on_first_byte_wait=beats.append)
        elapsed = time.monotonic() - t0
        # Killed near the 0.20s watchdog, NOT the 300s read timeout.
        assert elapsed < 5.0
        # Heartbeat beats fired while waiting (0.05s cadence → ~3-4).
        assert len(beats) >= 2
        assert beats == sorted(beats)

    def test_fast_stream_not_killed_no_error(self, monkeypatch):
        class _FastResp:
            headers = {}
            status_code = 200
            encoding = None

            def iter_lines(self, decode_unicode=False):
                yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
                yield 'data: [DONE]'

            def close(self):
                pass

        beats = []
        msg, finish, usage = self._call_once(
            monkeypatch, _FastResp(), on_first_byte_wait=beats.append)
        assert 'hi' in (msg.get('content') or '')
        assert beats == []  # first byte arrived before the first beat

    def test_watchdog_disabled_by_zero_timeout(self, monkeypatch):
        """TOFU_LLM_TTFT_TIMEOUT=0 must fully disable the kill (opt-out
        hatch) — the blocking resp then surfaces its OWN error, never a
        FirstByteTimeoutError."""
        from lib.llm_errors import FirstByteTimeoutError
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(_BlockingResp()))
        monkeypatch.setattr('lib.llm._transport.TTFT_TIMEOUT', 0.0)
        monkeypatch.setattr('lib.llm._transport.FIRST_BYTE_HEARTBEAT_S', 0.0)
        # Speed the fake's own raise up: pre-close it so iter_lines raises
        # immediately instead of after its 15s guard.
        resp = _BlockingResp()
        resp.close()
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(resp))
        from lib.llm.stream import _stream_chat_once
        with pytest.raises(Exception) as ei:
            _stream_chat_once(
                {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
        assert not isinstance(ei.value, FirstByteTimeoutError)


# ═════════════════════════════════════════════════════════════════════
#  C. Async transport (lib/llm/astream.py) — kill → FirstByteTimeoutError
# ═════════════════════════════════════════════════════════════════════

class _AsyncBlockingResp:
    def __init__(self):
        self.headers = {}
        self.status_code = 200
        self._closed = asyncio.Event()

    async def aclose(self):
        self._closed.set()

    async def aiter_lines(self):
        # 15s guard: without the watchdog (NEUTER proof) the test must FAIL,
        # not hang the suite — mirrors the sync fake's guard.
        await asyncio.wait_for(self._closed.wait(), timeout=15)
        raise RuntimeError('stream closed')
        yield  # pragma: no cover


class _AsyncStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeAsyncClient:
    def __init__(self, resp):
        self._resp = resp
        self.is_closed = False

    def stream(self, *a, **k):
        return _AsyncStreamCM(self._resp)


@pytest.mark.unit
class TestAsyncTransportWatchdog:
    def test_wedged_attempt_killed_into_first_byte_timeout(self, monkeypatch):
        from lib.llm_errors import FirstByteTimeoutError

        async def _main():
            resp = _AsyncBlockingResp()
            monkeypatch.setattr('lib.llm.astream.get_async_client',
                                lambda _proxy: _FakeAsyncClient(resp))
            monkeypatch.setattr('lib.llm._transport.TTFT_TIMEOUT', 0.20)
            monkeypatch.setattr('lib.llm._transport.FIRST_BYTE_HEARTBEAT_S', 0.05)
            beats = []
            from lib.llm.astream import _async_stream_chat_once
            _t0 = time.monotonic()
            with pytest.raises(FirstByteTimeoutError):
                await _async_stream_chat_once(
                    {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                    api_key='sk-x', base_url='http://fake.local/v1',
                    on_first_byte_wait=beats.append, log_prefix='[t]')
            _elapsed = time.monotonic() - _t0
            # The KILL (resp.aclose) must be what ended the wait — without
            # it (NEUTER proof) the fake's 15s guard is what raises and the
            # flag-only translation still yields FirstByteTimeoutError but
            # ~15s late. Pin the kill cadence, not just the exception type.
            assert _elapsed < 5.0
            assert len(beats) >= 2

        asyncio.new_event_loop().run_until_complete(_main())

    def test_fast_stream_not_killed(self, monkeypatch):
        class _AsyncFastResp:
            headers = {}
            status_code = 200

            async def aclose(self):
                pass

            async def aiter_lines(self):
                yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
                yield 'data: [DONE]'

        async def _main():
            monkeypatch.setattr(
                'lib.llm.astream.get_async_client',
                lambda _proxy: _FakeAsyncClient(_AsyncFastResp()))
            monkeypatch.setattr('lib.llm._transport.TTFT_TIMEOUT', 0.20)
            monkeypatch.setattr('lib.llm._transport.FIRST_BYTE_HEARTBEAT_S', 0.05)
            beats = []
            from lib.llm.astream import _async_stream_chat_once
            msg, finish, usage = await _async_stream_chat_once(
                {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                api_key='sk-x', base_url='http://fake.local/v1',
                on_first_byte_wait=beats.append, log_prefix='[t]')
            assert 'hi' in (msg.get('content') or '')
            assert beats == []

        asyncio.new_event_loop().run_until_complete(_main())


# ═════════════════════════════════════════════════════════════════════
#  D. Dispatch integration — FirstByteTimeoutError is a NORMAL upstream
#     soft error: record_error + pair exclusion + rotation + honest
#     on_retry reason, on BOTH loops.
# ═════════════════════════════════════════════════════════════════════

def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _FakeDispatcher:
    def __init__(self, slots):
        self._slots = list(slots)
        self.slots = []

    def pick_and_reserve(self, **kwargs):
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.mark.unit
class TestDispatchFirstByteTimeout:
    def test_sync_dispatch_rotates_and_reports_reason(self, monkeypatch):
        from lib.llm_errors import FirstByteTimeoutError
        from lib.llm_dispatch import api

        wedged = _make_slot(key='wedged')
        live = _make_slot(key='live')
        disp = _FakeDispatcher([wedged, live])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}
        captured_wait = []

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                # The beat callback must be threaded through with slot ctx.
                cb = kwargs.get('on_first_byte_wait')
                if cb:
                    cb(20.0)
                raise FirstByteTimeoutError('first byte timeout (180s)')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries, waits = [], []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw),
            on_waiting=lambda **kw: waits.append(kw))

        assert msg == 'ok'
        assert calls['n'] == 2
        # Normal upstream-soft-error bookkeeping: consecutive error recorded
        # (feeds the cooldown ladder), pair excluded, second slot succeeded.
        assert wedged.consecutive_errors >= 1
        assert live.last_success_time > 0
        assert any(r.get('reason') == 'First byte timeout' for r in retries)
        # The heartbeat reached the caller WITH the slot context.
        assert waits and waits[0]['elapsed'] == 20.0
        assert waits[0]['slot'] is wedged

    def test_async_dispatch_rotates_and_reports_reason(self, monkeypatch):
        from lib.llm_errors import FirstByteTimeoutError
        from lib.llm_dispatch import api

        wedged = _make_slot(key='wedged')
        live = _make_slot(key='live')
        disp = _FakeDispatcher([wedged, live])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        async def _fake_astream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                cb = kwargs.get('on_first_byte_wait')
                if cb:
                    cb(20.0)
                raise FirstByteTimeoutError('first byte timeout (180s)')
            return 'ok', 'stop', {}

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        retries, waits = [], []

        async def _main():
            return await api.async_dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
                on_retry=lambda **kw: retries.append(kw),
                on_waiting=lambda **kw: waits.append(kw))

        msg, finish, usage = asyncio.new_event_loop().run_until_complete(_main())
        assert msg == 'ok'
        assert calls['n'] == 2
        assert wedged.consecutive_errors >= 1
        assert live.last_success_time > 0
        assert any(r.get('reason') == 'First byte timeout' for r in retries)
        assert waits and waits[0]['slot'] is wedged


# ═════════════════════════════════════════════════════════════════════
#  E. Manager heartbeat emission (lib/tasks_pkg/manager/_stream.py) —
#     on_waiting → transient retrying PHASE with waitingFirstByte keys.
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestManagerWaitingHeartbeat:
    def _drive(self, slot):
        """Drive the REAL stream_llm_response with a scripted dispatch that
        captures on_waiting; fire one beat; return the PHASE events."""
        import threading as _thr
        from types import SimpleNamespace

        import lib.tasks_pkg.manager as _mgr

        task = {'id': 'task-hb', 'convId': 'hb-conv',
                'content': '', 'thinking': '', 'config': {}, 'events': [],
                'toolRounds': [], 'content_lock': _thr.Lock(),
                'events_lock': _thr.Lock()}

        captured = {}

        def _fake_dispatch(body, **kwargs):
            captured['on_waiting'] = kwargs.get('on_waiting')
            return ({'role': 'assistant', 'content': 'ok',
                     'reasoning_content': ''}, 'stop', {})

        _orig = _mgr.dispatch_stream
        _mgr.dispatch_stream = _fake_dispatch
        try:
            _mgr.stream_llm_response(
                task, {'model': 'kimi-k3',
                       'messages': [{'role': 'user', 'content': 'go'}]},
                tag='R1')
        finally:
            _mgr.dispatch_stream = _orig
        assert captured['on_waiting'], 'on_waiting not wired into dispatch'
        captured['on_waiting'](elapsed=42.0, slot=slot)
        return [e for e in task['events']
                if e.get('type') == 'phase' and e.get('phase') == 'retrying'
                and 'waitingFirstByte' in (e.get('detailKey') or '')]

    def test_heartbeat_with_typed_slot_cause(self):
        from types import SimpleNamespace
        slot = SimpleNamespace(
            model='kimi-k3', key_name='sankuai_key_1',
            cooldown_reason='upstream', cooldown_until=time.time() + 20,
            last_error_msg='', consecutive_errors=4)
        evs = self._drive(slot)
        assert evs, 'no waitingFirstByte heartbeat phase emitted'
        ev = evs[-1]
        assert ev['detailKey'] == 'stream.phase.waitingFirstByteReason'
        assert ev['detailArgs']['elapsed'] == 42
        assert ev['detailArgs']['model'] == 'kimi-k3'
        assert ev['detailArgs']['reasonKey'] == 'stream.retryReason.upstreamError'
        # attempt drives the frontend phaseKey refresh — must beat-count.
        assert ev['attempt'] >= 2
        # Legacy English detail preserved for headless clients.
        assert '42' in ev['detail'] and 'kimi-k3' in ev['detail']

    def test_heartbeat_without_slot_cause(self):
        from types import SimpleNamespace
        slot = SimpleNamespace(
            model='kimi-k3', key_name='sankuai_key_0',
            cooldown_reason='', cooldown_until=0.0,
            last_error_msg='', consecutive_errors=0)
        evs = self._drive(slot)
        assert evs, 'no waitingFirstByte heartbeat phase emitted'
        ev = evs[-1]
        assert ev['detailKey'] == 'stream.phase.waitingFirstByte'
        assert ev['detailArgs']['elapsed'] == 42
        assert 'reasonKey' not in ev['detailArgs']

    def test_heartbeat_raw_error_text_fallback(self):
        """A slot with a raw last_error_msg (no typed cooldown cause) shows
        the truncated raw reason — no reasonKey."""
        from types import SimpleNamespace
        slot = SimpleNamespace(
            model='kimi-k3', key_name='sankuai_key_0',
            cooldown_reason='', cooldown_until=0.0,
            last_error_msg='API HTTP 400: 请求失败,请稍后再尝试',
            consecutive_errors=2)
        evs = self._drive(slot)
        assert evs
        ev = evs[-1]
        assert ev['detailKey'] == 'stream.phase.waitingFirstByteReason'
        assert 'reasonKey' not in ev['detailArgs']
        assert '请稍后' in ev['detailArgs']['reason']


# ═════════════════════════════════════════════════════════════════════
#  F. i18n registration — mapping + shipped keys
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFirstByteI18n:
    def test_reason_token_mapped(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        assert RETRY_REASON_KEYS.get('First byte timeout') == \
            'stream.retryReason.firstByteTimeout'

    def test_i18n_keys_shipped_zh_and_en(self):
        with open(os.path.join(ROOT, 'static', 'js', 'i18n.js'),
                  encoding='utf-8') as f:
            src = f.read()
        for key in ('stream.phase.waitingFirstByte',
                    'stream.phase.waitingFirstByteReason',
                    'stream.retryReason.firstByteTimeout'):
            assert f"'{key}'" in src, f'{key} missing from i18n.js'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
