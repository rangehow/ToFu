"""Production quota watch over recorded request sequences (lib/llm_dispatch/quota_watch.py).

The example-corp AIGC gateway limits each AppId's requests and tokens per minute,
per model, and names that limit in its 429 body. The recorded sequence is the
2026-11-09 13:37-13:43 dispatch log: translation and fetch extraction hit hy4's
per-AppId minute limit on all four AppIds (and deepseek-v4-flash-yourprovider's on
0733) while conversation rounds on claude-opus-5.5 and kimi-k3.1-pro were
accepted. The watch must stay silent on that, and tell the owner's Watchdog
once per episode when conversation rounds on one AppId and model are rejected
by that limit and held by dispatcher backpressure. A full provider minute of
accepted conversation rounds without a minute-limit rejection closes it.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]

T0 = 1_794_200_000.0
OWNER = 7

# Recorded 2026-11-09 13:37:00-13:43:00: (second, caller, AppId tail, model, 429 kind).
AUXILIARY_REJECTIONS = (
    (30, 'Translate', '0733', 'hy4', 'rpm'), (31, 'Translate', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (34, 'Translate', '0733', 'hy4', 'rpm'), (35, 'Translate', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (73, 'Fetch:Extract', '5409', 'hy4', 'rpm'), (92, 'Fetch:Extract', '4861', 'hy4', 'rpm'),
    (94, 'Fetch:Extract', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (97, 'Fetch:Extract', '0733', 'kunlunbedrock.gpt-6.1-sol-m17', 'vendor_tpm'),
    (110, 'Translate', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'), (110, 'Translate', '4861', 'hy4', 'rpm'),
    (148, 'Fetch:Extract', '4861', 'hy4', 'rpm'), (150, 'Translate', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (150, 'Translate', '4861', 'hy4', 'rpm'), (164, 'Fetch:Extract', '5409', 'hy4', 'rpm'),
    (175, 'Fetch:Extract', '6672', 'hy4', 'rpm'), (184, 'Fetch:Extract', '5409', 'hy4', 'rpm'),
    (187, 'Fetch:Extract', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'), (190, 'Fetch:Extract', '4861', 'hy4', 'rpm'),
    (191, 'Fetch:Extract', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (215, 'Fetch:Extract', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'), (217, 'Fetch:Extract', '0733', 'hy4', 'rpm'),
    (230, 'Fetch:Extract', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'), (230, 'Fetch:Extract', '4861', 'hy4', 'rpm'),
    (263, 'Translate', '0733', 'hy4', 'rpm'), (310, 'Translate', '0733', 'deepseek-v4-flash-yourprovider', 'rpm'),
    (343, 'Translate', '0733', 'hy4', 'rpm'), (346, 'Translate', '0733', 'hy4', 'rpm'),
)
# Accepted conversation rounds in the same window, per (AppId tail, model).
PRODUCTION_ACCEPTED = {
    ('0733', 'claude-opus-5.5'): (1, 33, 55, 68, 71, 83, 87, 100, 104, 108, 109, 118, 120, 125, 131, 145, 154,
                                  161, 170, 196, 214, 231, 233, 246, 253, 256, 261, 274, 278, 297, 303, 304, 311,
                                  329, 339, 343, 356, 356),
    ('0733', 'kimi-k3.1-pro'): (6, 27, 57, 84, 85, 106, 125, 139, 238, 258, 277, 288),
    ('4861', 'claude-opus-5.5'): (29, 36, 37, 43, 62, 68, 69, 74, 85, 86, 95, 106, 108, 112, 112, 118, 123, 129,
                                  134, 138, 152, 160, 160, 174, 176, 182, 182, 184, 194, 203, 203, 204, 214, 219,
                                  225, 239, 241, 242, 247, 278, 279, 288, 303, 307, 316, 317, 327, 332, 333, 341,
                                  346, 349),
    ('6672', 'claude-opus-5.5'): (3, 3, 68, 86, 86, 104, 106, 118, 119, 133, 146, 170, 237, 241, 242, 245, 258,
                                  261, 264, 272, 273, 279, 280, 281, 282, 283, 289, 291, 293, 293, 293, 298, 298,
                                  300, 302, 304, 305, 306, 306, 310, 312, 321, 321, 322, 322, 328, 333, 334, 336,
                                  340, 341, 342, 344, 349, 349, 351, 351, 358, 358, 358),
    ('6672', 'kimi-k3.1-pro'): (2, 14, 36, 43, 56, 63, 65, 67, 82, 85, 86, 104, 105, 105, 117, 123, 124, 137,
                                139, 160, 169, 181, 212, 216, 218, 234, 236, 238, 249, 255, 256, 268, 269, 272,
                                287, 291, 304, 309, 321, 321, 338, 348, 354),
}
CREDENTIALS = {'0733': 'credential-1792419920936-26sj7f', '4861': 'cred_ec6300b9f58ffa14e3e6df16',
               '6672': 'credential-1788712374132-sts18d', '5409': 'cred_2009fa1132a88c55243ebcd3'}
CONTENDED = ('0733', 'claude-opus-5.5')


def _api_key(tail):
    return 'APPSECRET-9f3c2e71b4d8-' + tail


def _slot(tail, model):
    from lib.llm_dispatch.slot import Slot
    credential = CREDENTIALS[tail]
    return Slot(key_name=f'{credential}:deployment-{model}', api_key=_api_key(tail), model=model,
                capabilities={'text'}, provider_id='default', route_credential_id=credential)


def _error(kind, tail, model):
    """The RateLimitError the response classifier raises for each recorded 429 body."""
    from lib.llm.llm_errors import RateLimitError
    envelope = ('{"status":429,"message":"%s","data":null,"ext":{"error":{"source":"AIGC","service":"aigc",'
                '"stage":"validation"}}}')
    if kind == 'rpm':
        return RateLimitError('API HTTP 429: ' + envelope % f'App:**{tail}在模型:{model}每分钟请求次数超过限制',
                              status_code=429, is_shared_contention=True)
    if kind == 'tpm':
        return RateLimitError('API HTTP 429: ' + envelope % f'App:**{tail}在模型:{model}的tpm超过限制',
                              status_code=429)
    if kind == 'vendor_tpm':
        return RateLimitError('rate_limit_exceeded: Your requests to gpt-6.1-sol for gpt-6.1-sol in australiaeast '
                              'have exceeded token rate limit.', status_code=429)
    if kind == 'overloaded':
        return RateLimitError('API HTTP 429: The engine is currently overloaded, please try again later',
                              status_code=429)
    if kind == 'project':
        return RateLimitError(f'API HTTP 429: request reached project ({model}) TPM rate limit',
                              status_code=429, is_shared_contention=True)
    if kind == 'billing':
        return RateLimitError('API HTTP 429: insufficient_quota', status_code=429, is_quota=True)
    raise AssertionError(kind)


def _fail_loudly(message, *args, **kwargs):
    raise AssertionError('the quota watch failed: ' + (message % args if args else message))


@pytest.fixture
def env(monkeypatch):
    from lib.llm_dispatch import quota_watch
    clock = [T0]
    delivered = []
    watch = quota_watch.QuotaWatch(clock=lambda: clock[0], deliver=delivered.append)
    monkeypatch.setattr(quota_watch, '_WATCH', watch)
    # The hooks swallow and log their own failures; here a swallowed failure fails the spec.
    monkeypatch.setattr(quota_watch, 'logger', SimpleNamespace(debug=_fail_loudly, warning=_fail_loudly,
                                                               info=lambda *args, **kwargs: None))
    return SimpleNamespace(clock=clock, delivered=delivered, watch=watch, module=quota_watch)


def _feed(env, events):
    """Replay (second, caller, tail, model, outcome, queue wait ms, ttft ms) through the dispatcher hooks."""
    for second, caller, tail, model, outcome, wait_ms, ttft_ms in sorted(events, key=lambda event: event[0]):
        env.clock[0] = T0 + second
        scope = env.module.production_dispatch(OWNER) if caller == 'production' else nullcontext()
        with scope:
            if outcome == 'ok':
                env.module.observe_settlement(_slot(tail, model), ttft_ms=ttft_ms, queue_wait_ms=wait_ms)
            else:
                env.module.observe_rejection(_slot(tail, model), _error(outcome, tail, model),
                                             queue_wait_ms=wait_ms)


def _recorded_window(offset=0):
    events = [(offset + second, caller, tail, model, kind, 300.0, None)
              for second, caller, tail, model, kind in AUXILIARY_REJECTIONS]
    for (tail, model), seconds in PRODUCTION_ACCEPTED.items():
        events += [(offset + second, 'production', tail, model, 'ok', 0.0, 1800.0) for second in seconds]
    return events


def _contended(offset, start, end, *, wait_ms=2400.0):
    """Recorded window where rounds on (0733, claude-opus-5.5) in [start, end) are rejected, then held and accepted."""
    events = [event for event in _recorded_window(offset)
              if not (event[1] == 'production' and event[2:4] == CONTENDED and start <= event[0] - offset < end)]
    for second in PRODUCTION_ACCEPTED[CONTENDED]:
        if start <= second < end:
            events.append((offset + second - 0.5, 'production', *CONTENDED, 'rpm', 0.0, None))
            events.append((offset + second, 'production', *CONTENDED, 'ok', wait_ms, 2600.0))
    return events


def _public(transition):
    # The credential identity and reporter only key the producer's internal
    # alert record (lib/router/watchdog/quota.py); the signal never carries them.
    return {key: value for key, value in transition.items()
            if key not in ('delivery', 'credential', 'reporter')}


def _ms(second):
    return int((T0 + second) * 1000)


def test_minute_limit_names_only_the_gateway_per_app_limits():
    from lib.llm_dispatch.quota_watch import minute_limit
    assert minute_limit(str(_error('rpm', '4861', 'hy4'))) == 'rpm'
    assert minute_limit(str(_error('tpm', '0733', 'gpt-6-luna'))) == 'tpm'
    for kind in ('overloaded', 'vendor_tpm', 'project', 'billing'):
        assert minute_limit(str(_error(kind, '0733', 'kimi-k3'))) is None, kind
    assert minute_limit('') is None


def test_recorded_auxiliary_hy4_rejections_on_every_appid_never_alert(env):
    # The recorded window replayed three times back to back: 18 minutes of the
    # normal background, auxiliary requests held on every rejection.
    _feed(env, _recorded_window() + _recorded_window(360) + _recorded_window(720))
    assert env.delivered == []
    lanes = env.watch.snapshot()
    rejected = {(lane['keyTail'], lane['model']) for lane in lanes if lane['auxiliaryRejectedTotal']}
    assert {('0733', 'hy4'), ('4861', 'hy4'), ('5409', 'hy4'), ('6672', 'hy4')} <= rejected
    assert all(lane['productionRejectedTotal'] == 0 and lane['episode'] is None for lane in lanes)


def test_corroborated_production_rejections_alert_once_per_episode(env):
    _feed(env, [event for event in _contended(0, 100, 200) if event[0] < 250])
    [alert] = env.delivered
    public = _public(alert)
    assert public['kind'] == 'open' and public['owner'] == OWNER
    assert (public['providerId'], public['keyTail'], public['model']) == ('example-corp', *CONTENDED)
    # Corroborated at 100 s: the round rejected at 99.5 s was accepted after a 2.4 s dispatcher hold.
    assert public['openedAtMs'] == _ms(100)
    evidence = public['evidence']
    assert evidence['window'] == {'startMs': _ms(40), 'endMs': _ms(100)}
    assert evidence['production'] == {'accepted': 6, 'rejected': 1, 'held': 1, 'longestWaitMs': 2400.0,
                                      'ttftMedianMs': 1800.0, 'ttftMaxMs': 2600.0}
    assert evidence['auxiliary'] == {'accepted': 0, 'rejected': 0}
    assert evidence['limits'] == {'rpm': 1, 'tpm': 0}
    assert evidence['ownAnswered'] == 7 and evidence['cleanCapacity'] == 6
    others = {entry['keyTail']: entry for entry in evidence['otherAppIds']}
    assert others == {'4861': {'keyTail': '4861', 'accepted': 8, 'rejected': 0},
                      '6672': {'keyTail': '6672', 'accepted': 3, 'rejected': 0}}
    for fragment in ('…0733', 'claude-opus-5.5', 'example-corp', 'per-minute', 'held', '…4861'):
        assert fragment in public['detail']


def test_uncorroborated_or_non_minute_limit_rejections_do_not_alert(env):
    _feed(env, [
        # A round rejected by the minute limit and never held (the user stopped it).
        (10, 'production', *CONTENDED, 'rpm', 0.0, None), (15, 'production', *CONTENDED, 'ok', 0.0, 2000.0),
        # Upstream overload, a vendor token limit, a project-wide limit and a billing stop are not this
        # AppId's minute limit, even when the round was held.
        (100, 'production', *CONTENDED, 'overloaded', 0.0, None),
        (101, 'production', *CONTENDED, 'ok', 900.0, 2000.0),
        (102, 'production', *CONTENDED, 'vendor_tpm', 500.0, None),
        (103, 'production', *CONTENDED, 'project', 500.0, None),
        (104, 'production', *CONTENDED, 'billing', 500.0, None),
        # Auxiliary requests on the same AppId and model, rejected and held, are not production.
        (200, 'Translate', *CONTENDED, 'rpm', 3000.0, None), (201, 'Translate', *CONTENDED, 'ok', 3000.0, 2000.0),
    ])
    assert env.delivered == []


def test_a_tokens_per_minute_rejection_is_the_same_minute_limit(env):
    _feed(env, [(10, 'production', '0733', 'gpt-6-luna', 'tpm', 0.0, None),
                (12, 'production', '0733', 'gpt-6-luna', 'ok', 1500.0, 900.0)])
    [alert] = env.delivered
    assert alert['evidence']['limits'] == {'rpm': 0, 'tpm': 1}


def test_recovery_closes_the_episode_and_a_later_rise_is_a_new_episode(env):
    _feed(env, _contended(0, 100, 200) + _contended(360, 100, 160))
    assert [(transition['kind'], transition['openedAtMs']) for transition in env.delivered] == [
        ('open', _ms(100)), ('close', _ms(100)), ('open', _ms(460)), ('close', _ms(460))]
    close = _public(env.delivered[1])
    # The last rejection was at 195.5 s; the first accepted round a full minute later closes it.
    assert close['closedAtMs'] == _ms(256)
    assert close['evidence']['episode'] == {'productionRejected': 13, 'held': 13, 'longestWaitMs': 2400.0,
                                            'firstRejectedAtMs': _ms(99.5), 'lastRejectedAtMs': _ms(195.5),
                                            'horizonMs': _ms(255.5), 'productionAcceptedSince': 7,
                                            'auxiliaryAcceptedSince': 0}
    assert close['cause'] == 'recovered'
    assert 'Recovered' in close['resolution'] and '…0733' in close['resolution']
    # The open and close of one episode share one delivery record: the close reaches the signal the open made.
    assert env.delivered[0]['delivery'] is env.delivered[1]['delivery']
    assert env.delivered[2]['delivery'] is not env.delivered[0]['delivery']


def test_an_episode_reaches_each_owner_whose_rounds_were_rejected(env):
    _feed(env, [(10, 'production', *CONTENDED, 'rpm', 0.0, None), (11, 'production', *CONTENDED, 'ok', 2000.0, 900.0)])
    env.clock[0] = T0 + 20
    with env.module.production_dispatch(OWNER + 1):
        env.module.observe_rejection(_slot(*CONTENDED), _error('rpm', *CONTENDED), queue_wait_ms=0.0)
    with env.module.production_dispatch(OWNER):
        env.module.observe_rejection(_slot(*CONTENDED), _error('rpm', *CONTENDED), queue_wait_ms=0.0)
    assert [(transition['kind'], transition['owner']) for transition in env.delivered] == [
        ('open', OWNER), ('open', OWNER + 1)]
    env.clock[0] = T0 + 90
    with env.module.production_dispatch(OWNER):
        env.module.observe_settlement(_slot(*CONTENDED), ttft_ms=900.0, queue_wait_ms=0.0)
    assert sorted((transition['kind'], transition['owner']) for transition in env.delivered[2:]) == [
        ('close', OWNER), ('close', OWNER + 1)]


def test_alert_payload_carries_no_secrets(env):
    from lib.secret_envelope import secret_hint
    _feed(env, [event for event in _contended(0, 100, 200) if event[0] < 300])
    assert [transition['kind'] for transition in env.delivered] == ['open', 'close']
    rendered = json.dumps([_public(transition) for transition in env.delivered], ensure_ascii=False)
    assert '0733' in rendered
    for tail, credential in CREDENTIALS.items():
        for secret in (_api_key(tail), _api_key(tail)[:-4], secret_hint(_api_key(tail)), credential):
            assert secret not in rendered


def test_route_groups_and_the_configured_pool_share_one_lane_per_appid_and_model(env):
    """The quota is the credential's: per-request route groups and configured slots are one lane."""
    from lib.llm_dispatch.slot import Slot
    credential = CREDENTIALS['0733']

    def routed(group):
        # A conversation round's request-scoped candidate: the group pin is its provider_id.
        return Slot(key_name=f'ephemeral_{group}', api_key=_api_key('0733'), model='claude-opus-5.5',
                    capabilities={'text'}, provider_id=f'route-group-{group}', routing_provider_id='default',
                    routing_owner_user_id=OWNER, request_scoped=True, route_credential_id=credential)

    error = _error('rpm', *CONTENDED)
    env.clock[0] = T0 + 10
    with env.module.production_dispatch(OWNER):
        env.module.observe_rejection(routed('a'), error, queue_wait_ms=0.0)
        env.clock[0] = T0 + 12
        env.module.observe_settlement(routed('a'), ttft_ms=900.0, queue_wait_ms=2000.0)
    [opened] = env.delivered
    assert opened['providerId'] == 'example-corp' and opened['keyTail'] == '0733'
    # Auxiliary work on the configured slot of the same credential is the same quota: its rejection
    # 30 s later keeps the episode open past the next round of another request group.
    env.clock[0] = T0 + 40
    env.module.observe_rejection(_slot(*CONTENDED), error, queue_wait_ms=300.0)
    env.clock[0] = T0 + 80
    with env.module.production_dispatch(OWNER):
        env.module.observe_settlement(routed('b'), ttft_ms=900.0, queue_wait_ms=0.0)
    assert len(env.delivered) == 1
    env.clock[0] = T0 + 101
    with env.module.production_dispatch(OWNER):
        env.module.observe_settlement(routed('c'), ttft_ms=900.0, queue_wait_ms=0.0)
    assert [transition['kind'] for transition in env.delivered] == ['open', 'close']
    assert [(lane['keyTail'], lane['model']) for lane in env.watch.snapshot()] == [CONTENDED]


def test_lane_state_is_bounded_by_the_provider_minute(env):
    lane = ('4861', 'claude-opus-5.5')
    _feed(env, [(second, 'production', *lane, 'ok', 0.0, 1000.0) for second in range(0, 600, 2)])
    [state] = [entry for entry in env.watch.snapshot() if (entry['keyTail'], entry['model']) == lane]
    assert state['windowEvents'] == 30 and state['cleanCapacity'] == 30


def test_dispatch_hooks_feed_the_watch_and_never_change_the_dispatch(env, monkeypatch):
    """A conversation round through the real stream dispatch: rejected, held, then accepted."""
    from lib.llm_dispatch import api
    from tests.llm_dispatch.test_shared_contention_backoff import _dispatcher
    from tests.providers.test_vendor_transient_dispatch import _FakeDispatcher
    slot = _slot(*CONTENDED)
    disp = _FakeDispatcher([slot] * 6)
    real = _dispatcher([slot])
    disp.note_shared_contention = real.note_shared_contention
    disp.reserve_shared_contention_probe = real.reserve_shared_contention_probe
    disp.note_shared_success = real.note_shared_success
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
    monkeypatch.setattr('lib.key_stats.record_rate_limit', lambda *a, **k: False)
    real_sleep = time.sleep

    def _sleep(seconds):
        env.clock[0] += seconds
        real_sleep(0.002)  # the dispatcher meters its hold on the monotonic clock

    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', _sleep)
    calls = []

    def _fake_stream(body, **kwargs):
        calls.append(body)
        if len(calls) == 1:
            raise _error('rpm', *CONTENDED)
        return 'ok', 'stop', {}

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
    with env.module.production_dispatch(OWNER):
        msg, finish, usage = api.dispatch_stream([{'role': 'user', 'content': 'hi'}], log_prefix='[Task t][R1]')
    assert (msg, finish) == ('ok', 'stop') and len(calls) == 2
    assert usage['_dispatch']['queue_wait_ms'] > 0
    [alert] = env.delivered
    assert (alert['kind'], alert['owner'], alert['keyTail']) == ('open', OWNER, '0733')
    assert alert['evidence']['production']['held'] == 1 and alert['evidence']['production']['rejected'] == 1

    # An auxiliary call outside the conversation-round scope is counted, never alerted on.
    calls.clear()
    msg, _finish, _usage = api.dispatch_stream([{'role': 'user', 'content': 'hi'}], log_prefix='[Translate]')
    assert msg == 'ok' and len(env.delivered) == 1
    [lane] = [entry for entry in env.watch.snapshot() if (entry['keyTail'], entry['model']) == CONTENDED]
    assert (lane['productionRejectedTotal'], lane['auxiliaryRejectedTotal']) == (1, 1)

    # A failing watch is logged and never reaches the dispatch outcome.
    def _broken(*args, **kwargs):
        raise RuntimeError('watch failure')

    logged = []
    monkeypatch.setattr(env.watch, 'observe', _broken)
    monkeypatch.setattr(env.module, 'logger', SimpleNamespace(
        debug=lambda message, *args, **kwargs: logged.append(message), warning=_fail_loudly,
        info=lambda *args, **kwargs: None))
    calls.clear()
    with env.module.production_dispatch(OWNER):
        msg, _finish, _usage = api.dispatch_stream([{'role': 'user', 'content': 'hi'}], log_prefix='[Task t][R2]')
    assert msg == 'ok' and len(calls) == 2
    assert logged == ['[QuotaWatch] rejection not observed', '[QuotaWatch] settlement not observed']


def test_conversation_rounds_dispatch_inside_the_production_scope(monkeypatch):
    """stream_llm_response runs its provider dispatch as a conversation round of the task's owner."""
    from lib.llm_dispatch import api, quota_watch
    import lib.tasks_pkg.manager._stream as manager_stream
    seen = []

    def _fake_dispatch(body, **kwargs):
        seen.append((quota_watch.production_owner(), kwargs.get('owner_user_id')))
        return {'role': 'assistant', 'content': 'ok'}, 'stop', {'prompt_tokens': 1}

    # The provider dispatch behind the Task path's seam, so the seam itself runs.
    monkeypatch.setattr(api, 'dispatch_stream', _fake_dispatch)
    task = {'id': 'task-quota-watch', 'convId': 'conv-quota-watch', 'content': '', '_userId': OWNER,
            'thinking': '', 'config': {}, 'events': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}
    body = {'model': 'claude-opus-5.5', 'messages': [{'role': 'user', 'content': 'go'}]}
    message, _finish, _usage = manager_stream.stream_llm_response(task, body, tag='R1')
    assert message['content'] == 'ok'
    assert seen == [(OWNER, OWNER)]
    assert quota_watch.production_owner() is None


def test_each_process_reports_as_itself_and_a_forked_shard_takes_its_own_identity():
    """Execution shards fork from one image; a reporter identity made before the fork is not theirs."""
    import os
    from lib.llm_dispatch.quota_watch import QuotaWatch
    watch = QuotaWatch()
    first = watch.reporter
    assert watch.reporter == first
    watch._reporter_pid = os.getpid() + 1   # what a forked child sees
    assert watch.reporter not in (first, '')


# ── How an episode closes when nothing routes to its lane (docs/modules/llm_io.md) ──
# Live 2026-11-18: 15 quota alarms on …5409 / …4861 stayed open for hours because
# an episode closed only at an accepted conversation round, and nothing routed to
# those lanes any more (the Watchdog steps off them; 5409 was out of credit).


def _scheduled_watch(env, monkeypatch):
    """The env watch with a recording scheduler in place of the real timers."""
    timers = []
    watch = env.module.QuotaWatch(clock=lambda: env.clock[0], deliver=env.delivered.append,
                                  schedule=lambda delay, callback: timers.append((env.clock[0] + delay, callback)))
    monkeypatch.setattr(env.module, '_WATCH', watch)
    env.watch = watch
    return timers


def _fire_due(env, timers):
    for at, callback in [timer for timer in timers if timer[0] <= env.clock[0]]:
        timers.remove((at, callback))
        callback()


def _rejected_and_held(env, second, *, retry_after_s=None):
    env.clock[0] = T0 + second
    error = _error('rpm', *CONTENDED)
    if retry_after_s is not None:
        error.retry_after_s = retry_after_s
    with env.module.production_dispatch(OWNER):
        env.module.observe_rejection(_slot(*CONTENDED), error, queue_wait_ms=0.0)
        env.clock[0] = T0 + second + 1
        env.module.observe_rejection(_slot(*CONTENDED), error, queue_wait_ms=1500.0)


def test_an_avoided_lane_lapses_at_the_provider_window_without_any_probe(env, monkeypatch):
    """Nothing routes to the lane after its rejections: the episode closes as lapsed, and nothing was sent."""
    timers = _scheduled_watch(env, monkeypatch)
    _rejected_and_held(env, 10)
    [opened] = env.delivered
    assert opened['kind'] == 'open'
    # The timer is set to the provider's own horizon: one window after the last rejection.
    assert [at for at, _ in timers] == [T0 + 11 + 60]
    env.clock[0] = T0 + 70
    _fire_due(env, timers)
    assert len(env.delivered) == 1 and env.watch.open_appids() == {('example-corp', CREDENTIALS['0733'])}
    env.clock[0] = T0 + 71
    _fire_due(env, timers)
    [_, closed] = env.delivered
    assert closed['kind'] == 'close' and closed['cause'] == 'lapsed'
    assert closed['delivery'] is opened['delivery'] and closed['closedAtMs'] == _ms(71)
    assert closed['resolution'].startswith('Lapsed') and 'no probe' in closed['resolution']
    assert closed['evidence']['episode']['productionAcceptedSince'] == 0
    assert env.watch.open_appids() == set()
    # The watch observed only the two rejections: it sent nothing to the lane to close the episode.
    [lane] = env.watch.snapshot()
    assert lane['episode'] is None and lane['productionRejectedTotal'] == 2


def test_an_avoided_lane_lapses_on_a_read_of_open_appids_without_a_timer(env):
    """The Watchdog's avoidance read reaches the horizon even where no timer runs."""
    _rejected_and_held(env, 10)
    env.clock[0] = T0 + 200
    assert env.watch.open_appids() == set()
    assert [(t['kind'], t.get('cause')) for t in env.delivered] == [('open', None), ('close', 'lapsed')]


def test_the_hold_the_provider_named_extends_the_horizon(env, monkeypatch):
    timers = _scheduled_watch(env, monkeypatch)
    _rejected_and_held(env, 10, retry_after_s=180.0)
    assert [at for at, _ in timers] == [T0 + 11 + 180]
    env.clock[0] = T0 + 120
    assert env.watch.open_appids() == {('example-corp', CREDENTIALS['0733'])}
    env.clock[0] = T0 + 191
    _fire_due(env, timers)
    assert [t['kind'] for t in env.delivered] == ['open', 'close']


def test_auxiliary_traffic_alone_lapses_the_episode_and_says_so(env):
    _rejected_and_held(env, 10)
    for second in (30, 50, 80):
        env.clock[0] = T0 + second
        env.module.observe_settlement(_slot(*CONTENDED), ttft_ms=900.0, queue_wait_ms=0.0)
    closed = env.delivered[-1]
    assert closed['kind'] == 'close' and closed['cause'] == 'lapsed' and closed['closedAtMs'] == _ms(80)
    assert closed['evidence']['episode']['auxiliaryAcceptedSince'] == 3
    assert 'auxiliary requests accepted on it meanwhile: 3' in closed['resolution']


def test_a_recovered_lane_closes_at_its_horizon_as_recovered(env, monkeypatch):
    timers = _scheduled_watch(env, monkeypatch)
    _rejected_and_held(env, 10)
    env.clock[0] = T0 + 30
    with env.module.production_dispatch(OWNER):
        env.module.observe_settlement(_slot(*CONTENDED), ttft_ms=900.0, queue_wait_ms=0.0)
    assert len(env.delivered) == 1
    # No further round: the timer at the horizon closes it on the round already accepted.
    env.clock[0] = T0 + 71
    _fire_due(env, timers)
    closed = env.delivered[-1]
    assert closed['cause'] == 'recovered' and closed['resolution'].startswith('Recovered')
    assert closed['evidence']['episode']['productionAcceptedSince'] == 1


def test_a_genuinely_ongoing_quota_stays_open(env, monkeypatch):
    """Rejections inside every provider window keep the episode, and its single alert, open."""
    timers = _scheduled_watch(env, monkeypatch)
    for second in range(10, 900, 45):
        _rejected_and_held(env, second)
        with env.module.production_dispatch(OWNER):
            env.clock[0] = T0 + second + 20
            env.module.observe_settlement(_slot(*CONTENDED), ttft_ms=900.0, queue_wait_ms=4000.0)
        _fire_due(env, timers)
        assert env.watch.open_appids() == {('example-corp', CREDENTIALS['0733'])}
    assert [t['kind'] for t in env.delivered] == ['open']
    # One pending timer at a time, never one per rejection.
    assert len(timers) == 1
