"""Calendar costs come from per-Turn usage projections, never transcripts.

2026-11-08 P0: every calendar cache miss hydrated the whole transcript of
every candidate conversation (``conversation.list ids=... include_messages``)
only to read each message's ``usage``; 249 concurrent scans held hydrated
transcripts and the API host grew from 0.87 to 22.2 GB. The cost path reads
the storage authority's per-Turn usage projection (``conversation.turn_usage``)
instead: transcript content never crosses the RPC, the scan's memory does not
grow with transcript size, and the values equal the full-hydration
computation, which this file keeps as an executable oracle.
"""

from __future__ import annotations

import datetime as dt
import json
import tracemalloc

import pytest

from lib.daily_report import cost
from lib.daily_report.conversations import _safe_int_ts
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41
MONTH = dt.date(2026, 8, 1)
_USAGE_FIELDS = ('model', 'preset', 'effort', 'provider_id', 'providerId')


def _at(day: int, hour: int = 12, *, month: dt.date = MONTH) -> int:
    moment = dt.datetime.combine(month.replace(day=day), dt.time(hour))
    return int(moment.timestamp() * 1000)


def _range_ms(first: dt.date, end: dt.date) -> tuple[int, int]:
    return (int(dt.datetime.combine(first, dt.time.min).timestamp() * 1000),
            int(dt.datetime.combine(end, dt.time.min).timestamp() * 1000))


def _usage(tokens: int) -> dict:
    return {'prompt_tokens': tokens, 'completion_tokens': tokens // 10}


def _conversation(conversation_id, messages, *, title=None, created_at=None,
                  updated_at=None, settings=None, archive=False):
    return {
        'id': conversation_id,
        'title': f'Title {conversation_id}' if title is None else title,
        'created_at': _at(1, 1) if created_at is None else created_at,
        'updated_at': _at(28) if updated_at is None else updated_at,
        'settings': {'model': 'conversation-model', 'other': 'x' * 64}
        if settings is None else settings,
        'messages': messages,
        # A frozen pre-Turn archive keeps raw message timestamps; Turn-native
        # messages carry their Turn time (projection timestamp, else
        # creation time) exactly as both authorities derive them.
        'archive': archive,
    }


def _known_month() -> dict[str, dict]:
    before = MONTH - dt.timedelta(days=2)
    conversations = [
        _conversation('alpha', [
            {'role': 'user', 'content': 'question', 'timestamp': _at(2, 9)},
            {'role': 'assistant', 'content': 'answer', 'timestamp': _at(2, 10),
             'usage': _usage(100), 'model': 'model-x',
             'providerId': 'provider-y'},
            {'role': 'assistant', 'content': 'conversation model',
             'timestamp': _at(3), 'usage': _usage(200)},
            {'role': 'assistant', 'content': 'empty usage',
             'timestamp': _at(3), 'usage': {}},
            {'role': 'assistant', 'content': 'not a mapping',
             'timestamp': _at(3), 'usage': 'garbage'},
            {'role': 'assistant', 'content': 'before the range',
             'timestamp': int(dt.datetime.combine(
                 before, dt.time(12)).timestamp() * 1000),
             'usage': _usage(300)},
            {'role': 'assistant', 'content': 'preset only',
             'timestamp': _at(5), 'usage': _usage(400), 'preset': 'opus',
             'provider_id': 'snake-provider'},
            {'role': 'assistant', 'content': 'effort only',
             'timestamp': _at(5, 13), 'usage': _usage(500), 'effort': 'high'},
            {'role': 'assistant', 'content': 'after the range',
             'timestamp': _at(25), 'usage': _usage(600)},
        ]),
        _conversation('untitled', [
            {'role': 'user', 'timestamp': _at(7),
             'content': 'a first message that is longer than thirty chars'},
            {'role': 'assistant', 'content': 'reply', 'timestamp': _at(7, 13),
             'usage': _usage(700), 'model': 'model-z'},
        ], title='', settings={}),
        _conversation('archive', [
            {'role': 'user', 'content': 'imported question', 'timestamp': 0},
            # Untimestamped usage is placed by its transcript position.
            {'role': 'assistant', 'content': 'interpolated', 'usage': _usage(800)},
            {'role': 'assistant', 'content': 'explicit zero', 'timestamp': 0,
             'usage': _usage(900)},
            {'role': 'assistant', 'content': 'out of range',
             'timestamp': _at(26), 'usage': _usage(1000)},
            {'role': 'assistant', 'content': 'in range', 'timestamp': _at(9),
             'usage': _usage(1100)},
        ], created_at=_at(4), updated_at=_at(14),
            settings={'preset': 'qwen'}, archive=True),
        _conversation('created-after-range', [
            {'role': 'assistant', 'content': 'x', 'timestamp': _at(10),
             'usage': _usage(1200)},
        ], created_at=_at(22)),
        _conversation('stale', [
            {'role': 'assistant', 'content': 'x', 'timestamp': _at(10),
             'usage': _usage(1300)},
        ], updated_at=int(dt.datetime.combine(
            before, dt.time(12)).timestamp() * 1000)),
    ]
    return {conversation['id']: conversation for conversation in conversations}


def _tolerant_timestamp(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


class _Authority:
    """Storage client double for one owner, answering like the wire.

    Every response is JSON round-tripped as the real transport decodes it,
    so a caller that asks for transcripts really allocates them.
    """

    def __init__(self, conversations, *, refused=()):
        self.conversations = conversations
        self.refused = set(refused)
        self.calls = []

    def query(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        assert payload['user_id'] == OWNER_USER_ID
        if payload.get('conv_id') in self.refused:
            raise StorageError('database_resource_exhausted',
                               'database_resource_exhausted')
        if operation == 'conversation.list':
            answer = self._list(payload)
        elif operation == 'conversation.turn_usage':
            answer = self._turn_usage(payload)
        else:
            raise AssertionError(f'unexpected storage operation {operation}')
        return json.loads(json.dumps(answer))

    def _metadata(self, conversation, settings_keys):
        settings = dict(conversation['settings'])
        if settings_keys is not None:
            settings = {key: settings[key] for key in settings_keys
                        if key in settings}
        return {
            'id': conversation['id'], 'user_id': OWNER_USER_ID,
            'title': conversation['title'],
            'created_at': conversation['created_at'],
            'updated_at': conversation['updated_at'],
            'settings': settings,
            'msg_count': len(conversation['messages']), 'rev': conversation.get('rev', 1),
        }

    def _list(self, payload):
        if payload.get('ids') is not None:
            selected = [self.conversations[conversation_id]
                        for conversation_id in payload['ids']]
        else:
            selected = [
                conversation for conversation in self.conversations.values()
                if conversation['updated_at'] >= payload.get(
                    'updated_at_gte', conversation['updated_at'])
                and conversation['created_at'] < payload.get(
                    'created_at_lt', conversation['created_at'] + 1)
            ]
            selected.sort(key=lambda item: (item['updated_at'], item['id']),
                          reverse=True)
            selected = selected[:payload['limit']]
        return [{
            'metadata': self._metadata(conversation,
                                       payload.get('settings_keys')),
            'messages': (list(conversation['messages'])
                         if payload.get('include_messages') else []),
        } for conversation in selected]

    def _turn_usage(self, payload):
        conversation = self.conversations.get(payload['conv_id'])
        if conversation is None:
            return None
        start, end = payload['timestamp_gte'], payload['timestamp_lt']
        turns = []
        for index, message in enumerate(conversation['messages']):
            usage = message.get('usage')
            if not isinstance(usage, dict) or not usage:
                continue
            row = {'index': index, 'usage': usage}
            if conversation['archive']:
                raw = message.get('timestamp')
                if (isinstance(raw, int) and not isinstance(raw, bool)
                        and raw and not start <= raw < end):
                    continue
                if 'timestamp' in message:
                    row['timestamp'] = raw
            else:
                turn_time = _tolerant_timestamp(message.get('timestamp'))
                if turn_time and not start <= turn_time < end:
                    continue
                row['timestamp'] = turn_time
            row.update({key: message[key] for key in (*_USAGE_FIELDS, 'apiRounds')
                        if key in message})
            turns.append(row)
        title_fallback = ''
        characters = payload.get('title_fallback_characters', 0)
        if (characters and not conversation['title']
                and conversation['messages']):
            content = conversation['messages'][0].get('content')
            title_fallback = (content[:characters]
                              if isinstance(content, str) else '')
        return {
            'metadata': {
                key: value for key, value in self._metadata(
                    conversation, ['model', 'preset', 'effort']).items()
                if key in ('id', 'title', 'created_at', 'updated_at',
                           'settings')
            },
            'message_count': len(conversation['messages']),
            'title_fallback': title_fallback,
            'turns': turns,
        }


def _full_hydration_costs(conversations, ms_start, ms_end, year, month,
                          price):
    """The pre-fix computation: hydrate every candidate, read every usage."""
    days = {}
    for conversation in conversations.values():
        if not (conversation['updated_at'] >= ms_start
                and conversation['created_at'] < ms_end):
            continue
        settings = conversation['settings']
        conv_model = (settings.get('model') or settings.get('preset')
                      or settings.get('effort') or '')
        messages = conversation['messages']
        first = messages[0].get('content', '') if messages else ''
        title = (conversation['title']
                 or (first[:30] if isinstance(first, str) else '')
                 or 'Untitled')
        conv_start = _safe_int_ts(conversation['created_at']
                                  or conversation['updated_at'])
        conv_end = _safe_int_ts(conversation['updated_at']
                                or conversation['created_at'])
        for index, message in enumerate(messages):
            usage = message.get('usage')
            if not isinstance(usage, dict) or not usage:
                continue
            ts = _safe_int_ts(message.get('timestamp', 0))
            if not ts:
                if (conv_start and conv_end and conv_start != conv_end
                        and len(messages) > 1):
                    ts = conv_start + int((conv_end - conv_start) * index
                                          / (len(messages) - 1))
                else:
                    ts = conv_start
            if not ts or ts < ms_start or ts >= ms_end:
                continue
            day = dt.datetime.fromtimestamp(ts / 1000)
            if (day.year, day.month) != (year, month):
                continue
            model = (message.get('model') or message.get('preset')
                     or message.get('effort') or conv_model)
            provider = message.get('provider_id') or message.get('providerId') or ''
            amount = price(usage, model, provider, at=ts / 1000)
            if amount <= 0:
                continue
            entry = days.setdefault(day.day, {'cost': 0.0, 'conversations': {}})
            entry['cost'] += amount
            item = entry['conversations'].setdefault(
                conversation['id'], {'name': title, 'cost': 0.0, 'tokens': 0})
            item['cost'] += amount
            item['tokens'] += (usage['prompt_tokens']
                               + usage['completion_tokens'])
    for entry in days.values():
        entry['cost'] = round(entry['cost'], 4)
        for item in entry['conversations'].values():
            item['cost'] = round(item['cost'], 4)
    return days


def _price(usage, model, provider, *, at):
    """Deterministic price that depends on every attribution input."""
    return (usage['prompt_tokens'] / 100 + len(model) / 1000
            + (0.5 if provider else 0.0) + (at % 7) / 10_000)


@pytest.fixture
def priced(monkeypatch):
    calls = []

    def price(usage, model='', provider='', at=None, *, unrounded_cny_components=False):
        calls.append((usage['prompt_tokens'], model, provider, at))
        from lib.cost import compute_cost
        amount = _price(usage, model, provider, at=at)
        quote = {**compute_cost(usage), 'costCny': amount}
        if unrounded_cny_components:
            quote.update(inputCostCny=amount, outputCostCny=0)
        return quote

    monkeypatch.setattr(cost, '_calc_msg_cost', price)
    return calls


def _use(monkeypatch, authority):
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: authority)
    return authority


def _hydrating_calls(authority):
    return [(operation, payload) for operation, payload in authority.calls
            if operation == 'conversation.get'
            or payload.get('include_messages')
            or payload.get('derive_messages')]


def test_scan_reads_turn_usage_projection_and_never_hydrates(
        monkeypatch, priced):
    authority = _use(monkeypatch, _Authority(_known_month()))
    start, end = _range_ms(MONTH, MONTH.replace(day=20))

    cost._scan_costs_in_range(start, end, 2026, 8, owner_user_id=OWNER_USER_ID)

    assert _hydrating_calls(authority) == []
    projected = sorted(payload['conv_id'] for operation, payload
                       in authority.calls
                       if operation == 'conversation.turn_usage')
    assert projected == ['alpha', 'archive', 'untitled']
    assert all(payload['timestamp_gte'] == start
               and payload['timestamp_lt'] == end
               for operation, payload in authority.calls
               if operation == 'conversation.turn_usage')


def test_projected_costs_equal_the_full_hydration_computation(
        monkeypatch, priced):
    conversations = _known_month()
    _use(monkeypatch, _Authority(conversations))
    start, end = _range_ms(MONTH, MONTH.replace(day=20))
    oracle_calls = []

    def oracle_price(usage, model, provider, *, at):
        oracle_calls.append((usage['prompt_tokens'], model, provider, at))
        return _price(usage, model, provider, at=at)

    expected = _full_hydration_costs(
        conversations, start, end, 2026, 8, oracle_price)

    got = cost._scan_costs_in_range(
        start, end, 2026, 8, owner_user_id=OWNER_USER_ID)

    assert got == {day: {**data, "partial": True} for day, data in expected.items()}
    assert sorted(priced) == sorted(oracle_calls)
    # The fixture exercises Turn time, interpolation and title fallback.
    assert set(got) == {2, 3, 5, 7, 9}
    assert got[7]['conversations']['untitled']['name'] == (
        'a first message that is longer')
    assert {day for day, data in got.items()
            if 'archive' in data['conversations']} == {7, 9}


def _large_month(content_bytes: int) -> dict[str, dict]:
    conversations = {}
    for number in range(4):
        conversation_id = f'large-{number}'
        messages = []
        for turn in range(3):
            messages.append({'role': 'user', 'timestamp': _at(3 + turn),
                             'content': 'q' * content_bytes})
            messages.append({
                'role': 'assistant', 'timestamp': _at(3 + turn, 13),
                'content': 'a' * content_bytes,
                'toolRounds': [{'calls': [{'name': 'read_file'}],
                                'result': 'r' * content_bytes}],
                'usage': _usage(10 + turn), 'model': 'model-x',
            })
        conversations[conversation_id] = _conversation(
            conversation_id, messages)
    return conversations


def _scan_peak_bytes(monkeypatch, conversations) -> int:
    _use(monkeypatch, _Authority(conversations))
    start, end = _range_ms(MONTH, MONTH.replace(day=20))
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        cost._scan_costs_in_range(
            start, end, 2026, 8, owner_user_id=OWNER_USER_ID)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_scan_memory_does_not_grow_with_transcript_size(monkeypatch, priced):
    small = _scan_peak_bytes(monkeypatch, _large_month(1024))
    large = _scan_peak_bytes(monkeypatch, _large_month(1024 * 1024))

    # 4 conversations x 3 rounds x 3 bodies of 1 MiB is 36 MiB of transcript;
    # the projection scan must allocate the same as for 1 KiB bodies.
    assert large - small < 256 * 1024, (small, large)
    assert large < 2 * 1024 * 1024, (small, large)


def test_unreadable_usage_fails_the_month_and_persists_nothing(
        monkeypatch, priced):
    month = (dt.date.today().replace(day=1)
             - dt.timedelta(days=40)).replace(day=1)
    conversations = {
        name: _conversation(name, [
            {'role': 'assistant', 'content': 'x', 'usage': _usage(10),
             'timestamp': _at(10, month=month)},
        ], created_at=_at(2, month=month), updated_at=_at(27, month=month))
        for name in ('readable', 'refused')
    }
    _use(monkeypatch, _Authority(conversations, refused={'refused'}))
    persisted = []
    monkeypatch.setattr(cost, '_load_cached_day_costs',
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cost, '_persist_day_cost',
        lambda date_str, *_args, **_kwargs: persisted.append(date_str))

    with pytest.raises(StorageError) as caught:
        cost._get_monthly_costs(
            month.year, month.month, owner_user_id=OWNER_USER_ID)

    assert caught.value.message == 'database_resource_exhausted'
    assert persisted == []


def test_monthly_costs_backfill_and_today_use_the_projection(
        monkeypatch, priced):
    today = dt.date.today()
    first = today.replace(day=1)
    conversations = {
        'live': _conversation('live', [
            {'role': 'assistant', 'content': 'today', 'usage': _usage(10),
             'timestamp': int(dt.datetime.combine(
                 today, dt.time(0, 30)).timestamp() * 1000)},
            {'role': 'assistant', 'content': 'month start', 'usage': _usage(20),
             'timestamp': int(dt.datetime.combine(
                 first, dt.time(0, 30)).timestamp() * 1000)},
        ], created_at=int(dt.datetime.combine(
            first, dt.time.min).timestamp() * 1000),
            updated_at=int(dt.datetime.combine(
                today, dt.time(0, 30)).timestamp() * 1000)),
    }
    authority = _use(monkeypatch, _Authority(conversations))
    persisted = {}
    monkeypatch.setattr(cost, '_load_cached_day_costs',
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cost, '_persist_day_cost',
        lambda date_str, day_data, **_kwargs: persisted.update(
            {date_str: day_data}))

    got = cost._get_monthly_costs(
        today.year, today.month, owner_user_id=OWNER_USER_ID)

    assert _hydrating_calls(authority) == []
    assert today.day in got
    if today.day > 1:
        assert got[today.day]['cost'] == pytest.approx(
            _price(_usage(10), 'conversation-model', '',
                   at=conversations['live']['messages'][0]['timestamp'] / 1000),
            abs=1e-6)
        assert got[1]['cost'] == pytest.approx(
            _price(_usage(20), 'conversation-model', '',
                   at=conversations['live']['messages'][1]['timestamp'] / 1000),
            abs=1e-4)
        # A legacy estimate still renders; only complete history gets pinned.
        assert first.isoformat() not in persisted
    assert today.isoformat() not in persisted


def _usage_reads(authority):
    return [data['conv_id'] for op, data in authority.calls if op == 'conversation.turn_usage']


def test_live_day_reuses_unchanged_projections_and_fold(monkeypatch, priced):
    from lib.pricing._revision import bump_pricing_revision

    day = MONTH.replace(day=7)
    stamp = _at(7)
    rows = {
        f'conv-{i}': _conversation(f'conv-{i}', [
            {'role': 'assistant', 'timestamp': stamp, 'usage': _usage(i + 1)},
        ], updated_at=stamp)
        for i in range(256)
    }
    authority = _use(monkeypatch, _Authority(rows))
    def read():
        return cost._get_live_day_costs(day, owner_user_id=OWNER_USER_ID)

    cold = read()
    assert len(_usage_reads(authority)) == len(priced) == 256
    authority.calls.clear()
    priced.clear()
    assert read() == cold
    assert [op for op, _ in authority.calls] == ['conversation.list']
    assert not priced  # Even the joined price fold is unchanged.

    # Same timestamp and message count: only the authoritative rev changed.
    rows['conv-1']['messages'][0]['usage'] = _usage(91)
    rows['conv-1']['rev'] = 2
    authority.calls.clear()
    changed = read()
    assert _usage_reads(authority) == ['conv-1']
    assert changed['cost'] > cold['cost']
    assert changed == cost._LiveDayCosts().read(day, owner_user_id=OWNER_USER_ID)

    # Provider/model pricing changes invalidate the total, not the projected rows.
    authority.calls.clear()
    priced.clear()
    bump_pricing_revision()
    assert read() == changed
    assert not _usage_reads(authority) and len(priced) == 256

    # Header-only edits can retain the same transcript revision.
    rows['conv-1']['settings']['model'] = 'model-x'
    rows['conv-1']['title'] = 'Renamed'
    authority.calls.clear()
    renamed = read()
    assert _usage_reads(authority) == ['conv-1']
    assert renamed['conversations']['conv-1']['name'] == 'Renamed'
    assert renamed['cost'] != changed['cost']

    authority.calls.clear()
    assert cost._get_live_day_costs(day + dt.timedelta(days=1), owner_user_id=OWNER_USER_ID)[
        'token_costs']['buckets']['input']['tokens'] == 0
    assert not _usage_reads(authority)


def test_live_day_retracts_deleted_clone_and_reassigns_shared_receipt(monkeypatch):
    from lib.cost import compute_cost

    day = MONTH.replace(day=7)
    stamp = _at(7)
    usage = _usage(1000)
    quote = compute_cost(usage, pricing_override={'input': 10, 'output': 0, 'currency': 'CNY'})
    rows = {key: _conversation(key, [{
        'role': 'assistant', 'timestamp': when, 'usage': usage,
        'apiRounds': [{'usageReceiptId': 'shared', 'cost': quote}],
    }], updated_at=when) for key, when in [('original', stamp), ('clone', stamp + 1)]}
    authority = _use(monkeypatch, _Authority(rows))
    original = cost._get_live_day_costs(day, owner_user_id=OWNER_USER_ID)
    assert set(original['conversations']) == {'original'}
    assert original['cost'] == pytest.approx(0.01)
    rows.pop('original')
    authority.calls.clear()
    after = cost._get_live_day_costs(day, owner_user_id=OWNER_USER_ID)
    assert set(after['conversations']) == {'clone'}
    assert after['token_costs'] == original['token_costs']
    assert not _usage_reads(authority)
    assert after == cost._LiveDayCosts().read(day, owner_user_id=OWNER_USER_ID)


def test_unstable_projection_read_cannot_be_reused(monkeypatch, priced):
    day = MONTH.replace(day=7)
    stamp = _at(7)
    conv = _conversation('racing', [{'role': 'assistant', 'timestamp': stamp, 'usage': _usage(10)}],
                         updated_at=stamp)
    authority = _use(monkeypatch, _Authority({'racing': conv}))
    read = authority._turn_usage

    def read_and_edit(payload):
        projection = json.loads(json.dumps(read(payload)))
        conv['messages'][0]['usage'] = _usage(999)
        conv['rev'] = 2
        return projection

    monkeypatch.setattr(authority, '_turn_usage', read_and_edit)
    old = cost._get_live_day_costs(day, owner_user_id=OWNER_USER_ID)
    monkeypatch.setattr(authority, '_turn_usage', read)
    authority.calls.clear()
    current = cost._get_live_day_costs(day, owner_user_id=OWNER_USER_ID)
    assert _usage_reads(authority) == ['racing']
    assert current['cost'] > old['cost']
