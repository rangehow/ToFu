"""A burst above a key's per-minute limit queues in order instead of 429-holding.

Production 2026-11-13 04:54-05:51: conversation rounds on AppId …5409 sent
10-17 kimi-k3.1-pro requests a minute. In each episode about ten were
accepted, then the gateway answered ``每分钟请求次数超过限制`` and the shared
contention gate held the rest behind 0.3→15 s probes (up to 35.8 s; six
Watchdog quota episodes in one hour). A real loopback provider reproduces the
limit: per (key, model) it accepts ``LIMIT`` requests in any sliding window
and answers the FRIDAY body otherwise, without Retry-After or rate-limit
headers. Production time is scaled by ``SCALE`` (provider window and the
dispatcher's contention timing alike), so trunk and the fix are compared on
the same clock.

The specification: the provider window the limit names is the only time
constant; the limit itself is learned from the provider's refusal
(docs/NETWORK.md "Provider pacing"). A burst then completes with no failure,
at most one capacity probe refused per provider window, the provider-bound
completion time and the submission order.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import math
import re
import threading
import time

import pytest

from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

MODEL = 'kimi-k3.1-pro'
SCALE = 30.0                  # production seconds per test second
WINDOW_S = 60.0 / SCALE       # the gateway's per-minute window, scaled
LIMIT = 4                     # requests the provider accepts per window
LATENCY_S = 0.1               # provider time to first byte
SUBMIT_GAP_S = 0.02           # burst arrivals, in a defined order
SLACK_S = WINDOW_S / 2        # scheduling slack on a loaded host


class _MinuteLimitedProvider:
    """Sliding-window request limit per (key, model); refusals are free.

    ``hold_until`` (monotonic) makes it refuse everything before that instant
    with ``Retry-After`` instead.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.accepted = {}            # (key, model) -> [arrival instants]
        self.events = []              # (instant, tag, 'ok' | '429', key)
        self.hold_until = None

    def admit(self, key, model, tag):
        now = time.monotonic()
        with self.lock:
            if self.hold_until is not None and now < self.hold_until:
                self.events.append((now, tag, '429', key))
                return False
            window = [at for at in self.accepted.get((key, model), [])
                      if at > now - WINDOW_S]
            ok = len(window) < LIMIT
            if ok:
                window.append(now)
            self.accepted[(key, model)] = window
            self.events.append((now, tag, 'ok' if ok else '429', key))
            return ok

    def rejected(self, tags):
        with self.lock:
            return [tag for _at, tag, verdict, _key in self.events
                    if verdict == '429' and tag in tags]

    def acceptances(self, tags):
        with self.lock:
            return [(at, tag) for at, tag, verdict, _key in self.events
                    if verdict == 'ok' and tag in tags]

    def first_contact(self, tags):
        """``tags`` in the order each first reached the provider (accepted or refused)."""
        with self.lock:
            seen = []
            for _at, tag, _verdict, _key in self.events:
                if tag in tags and tag not in seen:
                    seen.append(tag)
            return seen

    def per_key(self, tags):
        """``{key: {'ok': n, '429': n}}`` for the given requests."""
        counts = {}
        with self.lock:
            for _at, tag, verdict, key in self.events:
                if tag in tags:
                    cell = counts.setdefault(key, {'ok': 0, '429': 0})
                    cell[verdict] += 1
        return counts


def _sse(content):
    frames = [
        {'choices': [{'index': 0, 'delta': {'content': content}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 3, 'completion_tokens': 1,
                   'total_tokens': 4}},
    ]
    return b''.join(b'data: ' + json.dumps(frame).encode() + b'\n\n'
                    for frame in frames) + b'data: [DONE]\n\n'


def _completion(content):
    return json.dumps({
        'id': 'loopback', 'object': 'chat.completion', 'model': MODEL,
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 1,
                  'total_tokens': 4},
    }).encode()


@contextmanager
def _provider():
    provider = _MinuteLimitedProvider()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _reply(self, status, content_type, payload, headers=()):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            body = json.loads(raw or b'{}')
            match = re.search(r'req-\d+', raw.decode('utf-8', 'replace'))
            tag = match.group(0) if match else '?'
            key = (self.headers.get('Authorization') or '')[-4:]
            model = body.get('model', '')
            if not provider.admit(key, model, tag):
                held = provider.hold_until is not None
                message = ('rate limited, retry later' if held else
                           f'App:**{key}在模型:{model}每分钟请求次数超过限制')
                payload = json.dumps({
                    'status': 429, 'message': message, 'data': None,
                    'ext': {'error': {'source': 'AIGC', 'service': 'aigc',
                                      'stage': 'validation'}},
                }, ensure_ascii=False).encode()
                retry_after = math.ceil(provider.hold_until - time.monotonic()) \
                    if held else None
                self._reply(429, 'application/json', payload,
                            [('Retry-After', str(retry_after))] if held else ())
                return
            time.sleep(LATENCY_S)
            if body.get('stream'):
                self._reply(200, 'text/event-stream', _sse('ok'))
            else:
                self._reply(200, 'application/json', _completion('ok'))

    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield provider, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


@pytest.fixture
def scaled_dispatch(monkeypatch, request):
    """The model's AppId lanes on a real dispatcher and transport, production
    time / SCALE. Parametrize indirectly with the AppIds (default ``5409``)."""
    import lib.desktop.egress as egress
    from lib.llm import _transport
    from lib.llm_dispatch import api, dispatcher as dispatcher_module, quota_watch
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot import Slot

    monkeypatch.setattr(quota_watch, 'WINDOW_S', WINDOW_S)
    monkeypatch.setattr(dispatcher_module, 'WINDOW_S', WINDOW_S, raising=False)
    for name in ('_CONTENTION_DECAY_GRACE_S', '_CONTENTION_RESET_GRACE_S',
                 '_CONTENTION_BASE_RETRY_S', '_CONTENTION_PROBE_SPACING_S',
                 '_CONTENTION_MAX_PROBE_SPACING_S',
                 '_CONTENTION_MAX_WAIT_SLICE_S'):
        monkeypatch.setattr(LLMDispatcher, name,
                            getattr(LLMDispatcher, name) / SCALE)
    monkeypatch.setattr(egress, 'route_request', lambda url, **kw: 'direct')
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
    monkeypatch.setattr('lib.key_stats.record_rate_limit',
                        lambda *a, **k: False)
    _transport.reset_pools_for_test()

    with _provider() as (provider, base_url):
        slots = [Slot(key_name=f'cred-{app}', api_key=f'sk-test-app-{app}',
                      model=model, capabilities={'text'}, provider_id='gw',
                      base_url=base_url, protocol='openai')
                 for app in getattr(request, 'param', ('5409',))
                 for model in (MODEL, 'warm-up')]
        disp = LLMDispatcher()
        disp.slots = slots
        disp.initialize = lambda: None
        disp._contention_strikes = {}
        disp._logical_index = {}
        disp._direct_models = {MODEL, 'warm-up'}
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        # A cold first dispatch spends a few hundred milliseconds importing
        # and connecting, which would let a whole burst arrive at once. Warm
        # every transport on another model: the provider counts each
        # (key, model) apart.
        for transport in ('sync_stream', 'async_stream', 'chat'):
            _dispatch(transport, 'warm', 'warm-up')
        yield provider
    _transport.reset_pools_for_test()


def _dispatch(transport, tag, model):
    from lib.llm_dispatch import api

    messages = [{'role': 'user', 'content': tag}]
    options = dict(prefer_model=model, strict_model=True, max_tokens=8,
                   log_prefix=f'[{tag}]')
    if transport == 'chat':
        content, _usage = api.dispatch_chat(messages, **options)
        return content
    if transport == 'async_stream':
        result = asyncio.run(api.async_dispatch_stream(messages, **options))
    else:
        result = api.dispatch_stream(messages, **options)
    return result.message.get('content')


def _burst(first, count, transport='sync_stream'):
    """Submit ``count`` requests in order, ``SUBMIT_GAP_S`` apart."""
    tags = [f'req-{index:02d}' for index in range(first, first + count)]
    submitted, outcomes, workers = {}, {}, []

    def run(tag):
        try:
            outcomes[tag] = _dispatch(transport, tag, MODEL)
        except BaseException as error:  # reported by the assertions
            outcomes[tag] = error

    started = time.monotonic()
    for tag in tags:
        submitted[tag] = time.monotonic()
        worker = threading.Thread(target=run, args=(tag,), daemon=True)
        worker.start()
        workers.append(worker)
        time.sleep(SUBMIT_GAP_S)
    deadline = started + math.ceil(count / LIMIT) * WINDOW_S * 4 + 10
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    assert not any(worker.is_alive() for worker in workers), 'a burst request hung'
    return tags, submitted, outcomes, started, time.monotonic()


def _in_window_order(tags, accept_order, per_window):
    """Each request reaches the provider no later than any request of a later
    provider window. Requests granted together race on the wire, so order
    inside one window is not a property of pacing."""
    windows = [tags.index(tag) // per_window for tag in accept_order]
    return windows == sorted(windows)


def _report(provider, tags, submitted, outcomes, started=None, ended=None):
    accepted = provider.acceptances(tags)
    holds = {tag: at - submitted[tag] for at, tag in accepted}
    return {'failed': {tag: repr(outcome) for tag, outcome in outcomes.items()
                       if outcome != 'ok'},
            '429s': len(provider.rejected(tags)),
            'per_key': provider.per_key(tags),
            'accept_order': [tag for _at, tag in accepted],
            'holds_s': {tag: round(hold, 3) for tag, hold in holds.items()},
            'elapsed_s': (round(ended - started, 3)
                          if started is not None else None)}


def test_a_burst_above_the_learned_minute_limit_queues_in_order(scaled_dispatch):
    provider = scaled_dispatch

    # First contact teaches the limit: the provider's refusal is its only
    # source, so only requests written before that refusal returned are
    # refused; the refused one keeps its turn.
    tags, submitted, outcomes, _start, _end = _burst(0, LIMIT + 2)
    first = _report(provider, tags, submitted, outcomes)

    time.sleep(WINDOW_S)   # the provider window is empty again

    # Three windows' demand: no failure, at most one capacity probe refused
    # per provider window, the provider-bound completion time and the
    # submission order (no request is overtaken or starves).
    count = 3 * LIMIT
    windows = math.ceil(count / LIMIT)
    tags, submitted, outcomes, started, ended = _burst(100, count)
    burst = _report(provider, tags, submitted, outcomes, started, ended)

    report = {'first_contact': first, 'burst': burst}
    assert not first['failed'] and not burst['failed'], report
    assert first['429s'] <= 2, report
    assert burst['429s'] <= windows, report
    assert sorted(burst['accept_order']) == sorted(tags), report
    assert _in_window_order(tags, burst['accept_order'], LIMIT), report
    bound = (windows - 1) * WINDOW_S + count * SUBMIT_GAP_S + LATENCY_S + SLACK_S
    assert burst['elapsed_s'] <= bound, (bound, report)
    for index, tag in enumerate(tags):
        assert burst['holds_s'][tag] <= (index // LIMIT) * WINDOW_S + SLACK_S, report


@pytest.mark.parametrize('transport', ['async_stream', 'chat'])
def test_every_dispatch_transport_paces_after_the_first_refusal(
        scaled_dispatch, transport):
    provider = scaled_dispatch
    tags, submitted, outcomes, started, ended = _burst(0, LIMIT + 2, transport)
    report = _report(provider, tags, submitted, outcomes, started, ended)
    assert not report['failed'], report
    assert report['429s'] <= 2, report
    assert sorted(report['accept_order']) == sorted(tags), report
    # Until the first refusal returns nothing is learned, so nothing is held:
    # the burst reaches the provider in wire order, not submission order (a
    # fresh event loop per async call delays some first bytes past the 20 ms
    # gaps on a loaded host). Pacing keeps the order requests reached it in:
    # a refused request is accepted before any request of a later window.
    report['first_contact'] = provider.first_contact(tags)
    assert _in_window_order(report['first_contact'], report['accept_order'], LIMIT), report
    assert report['elapsed_s'] <= WINDOW_S + LATENCY_S + SLACK_S, report


def test_retry_after_holds_the_lane_for_the_named_time(scaled_dispatch):
    provider = scaled_dispatch
    provider.hold_until = time.monotonic() + 1.0
    tags, submitted, outcomes, started, ended = _burst(0, 3)
    report = _report(provider, tags, submitted, outcomes, started, ended)
    assert not report['failed'], report
    # Only requests already written when the first refusal returned are
    # refused; nothing is sent again before the provider's Retry-After, and
    # the whole queue goes when it ends (granted together, in any order).
    refused_at = [at for at, tag, verdict, _key in provider.events
                  if verdict == '429' and tag in tags]
    assert max(refused_at) - min(refused_at) <= SUBMIT_GAP_S * len(tags), report
    assert sorted(report['accept_order']) == tags, report
    holds = report['holds_s'].values()
    assert min(holds) >= 1.0 - SUBMIT_GAP_S * len(tags), report
    assert max(holds) <= 1.0 + SLACK_S, report


@pytest.mark.parametrize('scaled_dispatch', [('5409', '0733')], indirect=True)
def test_a_burst_spreads_over_the_models_keys_within_each_limit(scaled_dispatch):
    """The owner's keys for one Model share a burst; each stays in its limit.

    Every key learns its own limit from its own refusal; automatic work then
    takes the key whose window has room, so two keys finish twice the demand
    in the same provider-bound time, with at most one capacity probe refused
    per key and window.
    """
    provider = scaled_dispatch
    keys = 2
    tags, submitted, outcomes, _start, _end = _burst(0, keys * LIMIT + 2)
    first = _report(provider, tags, submitted, outcomes)

    time.sleep(WINDOW_S)

    count = 3 * keys * LIMIT
    windows = math.ceil(count / (keys * LIMIT))
    tags, submitted, outcomes, started, ended = _burst(100, count)
    burst = _report(provider, tags, submitted, outcomes, started, ended)

    report = {'first_contact': first, 'burst': burst}
    assert not first['failed'] and not burst['failed'], report
    assert all(cell['429'] <= 2 for cell in first['per_key'].values()), report
    assert all(cell['429'] <= windows for cell in burst['per_key'].values()), report
    assert sorted(burst['per_key']) == ['0733', '5409'], report
    assert all(cell['ok'] == count // keys for cell in burst['per_key'].values()), report
    bound = (windows - 1) * WINDOW_S + count * SUBMIT_GAP_S + LATENCY_S + SLACK_S
    assert burst['elapsed_s'] <= bound, (bound, report)
