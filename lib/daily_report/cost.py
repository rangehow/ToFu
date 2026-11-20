"""Server-side cost calculation for the daily-report calendar.

Strategy: past days come from the durable daily-cost cache; current day
reuses a storage-validated rolling projection; the calendar wraps both in a
30-second in-process TTL cache with one in-flight computation per owner month
(``calendar_overview``). Scans read the storage authority's per-Turn usage
projection (``conversation.turn_usage``), never transcripts.
"""

import datetime as _dt
import threading
import time
import uuid

from lib.cost import compute_cost, normalize_usage
from lib.identity import require_user_id
from lib.log import get_logger
from lib.utils import safe_json

from .conversations import _safe_int_ts
from lib.request_quotes import RequestEvidence, component_amount, has_complete_price, receipt_identity, usage_population

logger = get_logger(__name__)


# ── Calendar endpoint TTL cache (avoids 5s+ repeated full-table scans) ──
# Owned by calendar_overview: it publishes month overviews here, and report
# saves and cost invalidation clear them through calendar_overview.invalidate.
from lib.ttl_cache import TTLCache

# (owner_user_id, year, month) → {'days': dict, 'conv_days': set, 'cost_days': dict, ...}
_CALENDAR_CACHE_TTL = 30  # seconds
_CALENDAR_CACHE_SIZE = 256  # Existing calendar retention budget.
_calendar_cache = TTLCache(
    ttl=_CALENDAR_CACHE_TTL,
    max_size=_CALENDAR_CACHE_SIZE,
    name='daily_report_calendar',
)


# Legacy preset → model_id migration table (mirrors core.js _LEGACY_PRESET_TO_MODEL)
_LEGACY_PRESET_TO_MODEL = {
    'qwen': 'qwen3.5-plus', 'low': 'qwen3.5-plus',
    'gemini': 'gemini-3-flash-preview', 'gemini_flash': 'gemini-3-flash-preview',
    'minimax': 'MiniMax-M2.7', 'doubao': 'Doubao-Seed-2.0-pro',
    'opus': 'aws.claude-opus-4.7',
    'medium': 'aws.claude-opus-4.7', 'high': 'aws.claude-opus-4.7',
    'xhigh': 'aws.claude-opus-4.7', 'max': 'aws.claude-opus-4.7',
}


def _qwen_cny(tokens, tok_type, model_id=''):
    """Qwen tiered CNY pricing — mirrors core.js _qwenCny().

    Args:
        tokens: Token count.
        tok_type: 'input' or 'output'.
        model_id: Model identifier for per-model tier lookup.

    Returns:
        Cost in CNY.
    """
    from lib import QWEN_PRICING_CNY
    # Per-model tiers: lookup model, fallback to '_default'
    model_tiers = QWEN_PRICING_CNY.get(model_id) or QWEN_PRICING_CNY.get('_default', {})
    tiers = model_tiers.get(tok_type, [])
    for max_tokens, price_per_1m in tiers:
        if tokens <= max_tokens:
            return tokens * price_per_1m / 1e6
    # Beyond last tier — use last tier's price
    if tiers:
        return tokens * tiers[-1][1] / 1e6
    return 0.0


def _calc_msg_cost_cny(usage, model_or_preset='', provider_id='', at=None):
    """Return the legacy calendar amount for a single message."""
    result = _calc_msg_cost(usage, model_or_preset, provider_id, at)
    return float((result or {}).get('costCny') or 0.0)


def _calc_msg_cost(usage, model_or_preset='', provider_id='', at=None, *,
                   unrounded_cny_components=False):
    """Calculate the component quote for a single message's usage dict.

    This is a faithful Python port of the frontend ``calcCostCny()``
    in ``core.js``, using the same pricing logic.

    Args:
        usage: Token usage dict (prompt_tokens, completion_tokens, etc.).
        model_or_preset: Model ID or legacy preset key.
        provider_id: Optional provider that served the call. When set, a
            provider-scoped override in ``PROVIDER_PRICING`` (registered
            from the provider template's per-model ``pricing`` field) is
            preferred over the global ``MODEL_PRICING`` table.
        at: UTC epoch seconds of the MESSAGE — peak schedules are evaluated
            at the message's own time so a historical rescan never bills a
            past day at today's peak status. Defaults to now.

    Returns:
        A ``compute_cost`` quote, or None if no tokens.
    """
    model_id = _LEGACY_PRESET_TO_MODEL.get(
        model_or_preset or '', model_or_preset or '')
    return compute_cost(usage, model_id=model_id,
                        provider_id=provider_id or None, at=at,
                        unrounded_cny_components=unrounded_cny_components)






# An untitled conversation is named by its first message in the per-day cost
# breakdown.
_TITLE_FALLBACK_CHARACTERS = 30
_COST_CANDIDATE_LIMIT = 10_000  # Existing calendar/storage list bound.


def _usage_rows_from_projections(projections):
    """Yield billing rows from per-Turn usage projections, one at a time."""
    for projection in projections:
        metadata = projection.metadata
        settings = metadata.get('settings') or {}
        conv_model = (settings.get('model') or settings.get('preset')
                      or settings.get('effort') or '')
        for turn in projection.turns:
            yield {
                'id': metadata['id'],
                'title': metadata.get('title') or '',
                'created_at': metadata.get('created_at') or 0,
                'updated_at': metadata.get('updated_at') or 0,
                'conv_model': conv_model,
                'first_content': projection.title_fallback,
                'msg_index': turn['index'],
                'total_msgs': projection.message_count,
                'usage': turn['usage'],
                **({'api_rounds': turn['apiRounds']} if 'apiRounds' in turn else {}),
                'msg_timestamp': turn.get('timestamp', 0),
                'msg_model': (turn.get('model') or turn.get('preset')
                              or turn.get('effort') or ''),
                'msg_provider': (turn.get('provider_id')
                                 or turn.get('providerId') or ''),
            }






def _population_total(population):
    input_count = population.get('totalInputTokens')
    if input_count is None:
        input_count = sum(population.get(key, 0) for key in ('inputTokens', 'cacheReadTokens', 'cacheWriteTokens'))
    return input_count + population.get('outputTokens', 0)


def _residual_lower_bounds(rows, evidence):
    """Overlapping unknown identities establish one conservative lower bound."""
    parents = {}
    candidates = []
    anonymous = (0, 0)

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for index, (*_, row, usage) in enumerate(rows):
        rounds = row.get('api_rounds')
        if rounds is None:
            continue
        _, covered, _, unknown = evidence.take(rounds, exclude_anonymous_usage=True)
        if not unknown:
            continue
        named = [key for key in unknown if key is not None]
        anonymous_observed = max((
            _population_total(usage_population(r.get('usage'), sparse=True) or {})
            for r in rounds if isinstance(r, dict) and receipt_identity(r) is None
            and not has_complete_price(r.get('cost'))), default=0) if None in unknown else 0
        aggregate = usage_population(usage)
        residual = (_population_total(aggregate) - sum(map(_population_total, covered))
                    if aggregate else 0)
        if named and not anonymous_observed:
            first = root(named[0])
            for key in named[1:]:
                parents[root(key)] = first
            if residual > 0:
                candidates.append((first, index, residual))
        else:
            witness = max(residual, anonymous_observed)
            if witness > anonymous[1]:
                anonymous = (index, witness)
    best = {}
    for key, index, bound in candidates:
        group = root(key)
        if bound > best.get(group, (0, 0))[1]:
            best[group] = (index, bound)
    # Anonymous evidence may overlap the independently named population.
    # Only its excess can raise the conservative coverage estimate.
    result = dict(best.values())
    index, witness = anonymous
    excess = witness - sum(result.values())
    if excess > 0:
        result[index] = result.get(index, 0) + excess
    return result


def _quote_row(row, usage, ts, token_costs, evidence, residuals):
    rounds = row.get('api_rounds')
    if rounds is not None:
        receipts, _, missing, _ = evidence.take(rounds, exclude_anonymous_usage=True)
        unallocated = residuals
        amount, tokens = 0.0, 0
        for population, quote in receipts:
            if population is not None:
                tokens += _population_total(population)
            if token_costs is not None:
                token_costs.add_population(population, quote)
            if quote is not None:
                amount += component_amount(quote)
        if token_costs is not None:
            token_costs.partial |= missing or bool(unallocated)
        return amount, tokens + unallocated, missing, unallocated

    # Compatibility estimate only: no reconstructible request ledger.
    model = row.get('msg_model') or row.get('conv_model') or ''
    provider = row.get('msg_provider') or ''
    if token_costs is not None:
        return (*token_costs.add(_calc_msg_cost(usage, model, provider, at=ts / 1000,
                                               unrounded_cny_components=True)), False, 0)
    quote = _calc_msg_cost(usage, model, provider, at=ts / 1000)
    counts = normalize_usage(usage)
    return float((quote or {}).get('costCny') or 0.0), counts['input'] + counts['output'], bool(quote), 0


def _scan_costs_in_range(
        ms_start, ms_end, year=None, month=None, *, owner_user_id,
        token_costs=None, unsettled_days=None):
    """Scan conversation snapshots and build per-day costs for a range.

    Args:
        ms_start: Inclusive lower bound (epoch-ms).
        ms_end:   Exclusive upper bound (epoch-ms).
        year, month: Optional extra filter — only keep days whose date falls
            in this year/month (when aggregating a full month).  If None,
            all days in the range are kept.
        token_costs: Optional collector for the live one-day token breakdown.

    Returns:
        dict mapping day-of-month (int) → {'cost': float,
            'conversations': {conv_id: {'name', 'cost', 'tokens'}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost scan')
    from lib.conversations.repository import scan_conversation_turn_usage
    # Only per-Turn usage rows cross the storage boundary, so memory does not
    # grow with transcripts. A read failure fails the scan: never persist a
    # silently incomplete month as zeros.
    candidate_limit = _COST_CANDIDATE_LIMIT
    candidate_count, projections = scan_conversation_turn_usage(
        user_id=owner_id,
        updated_at_gte=ms_start,
        created_at_lt=ms_end,
        timestamp_gte=ms_start,
        timestamp_lt=ms_end,
        limit=candidate_limit,
        title_fallback_characters=_TITLE_FALLBACK_CHARACTERS,
    )
    if token_costs is not None:
        token_costs.partial = candidate_count >= candidate_limit
    if unsettled_days is not None and candidate_count >= candidate_limit:
        unsettled_days.update(range(1, 32))
    return _fold_costs(projections, ms_start, ms_end, year, month,
                       token_costs=token_costs, unsettled_days=unsettled_days)


def _fold_costs(projections, ms_start, ms_end, year, month, *,
                token_costs=None, unsettled_days=None):
    rows = _usage_rows_from_projections(projections)
    evidence = RequestEvidence()
    receipt_rows = []
    days = {}   # day_num → {cost, conversations}

    for r in rows:
        # psycopg decodes JSONB to dict; SQLite's json_extract returns text.
        # Accept both without feeding an already-decoded dict back through
        # json.loads (which logs a false "corrupt JSON" warning).
        usage_raw = r['usage']
        usage = (usage_raw if isinstance(usage_raw, dict) else
                 safe_json(usage_raw, default={}, label='cost-usage'))
        if not isinstance(usage, dict):
            usage = {}
        if not usage and r.get('api_rounds') is None:
            continue

        conv_start = _safe_int_ts(r['created_at'] or r['updated_at'] or 0)
        conv_end = _safe_int_ts(r['updated_at'] or r['created_at'] or 0)
        total_msgs = int(r.get('total_msgs') or 0)
        msg_index = int(r.get('msg_index') or 0)
        conv_title = r['title'] or r.get('first_content') or ''
        conv_title = conv_title or 'Untitled'
        conv_id = r['id']

        ts = _safe_int_ts(r.get('msg_timestamp', 0))
        if not ts:
            if (conv_start and conv_end and conv_start != conv_end
                    and total_msgs > 1):
                ts = conv_start + int(
                    (conv_end - conv_start) * msg_index / (total_msgs - 1))
            else:
                ts = conv_start
        if not ts or ts < ms_start or ts >= ms_end:
            continue

        d = _dt.datetime.fromtimestamp(ts / 1000)
        if year is not None and month is not None:
            if d.year != year or d.month != month:
                continue
        day_num = d.day

        if r.get('api_rounds') is not None:
            evidence.observe(r['api_rounds'])
        receipt_rows.append((ts, conv_id, msg_index, day_num, conv_title, r, usage))

    # Stable attribution when copied history appears in several conversations.
    ordered = sorted(receipt_rows, key=lambda item: (item[0], item[1], item[2]))
    incomplete_days = set()
    residuals = _residual_lower_bounds(ordered, evidence)
    evidence.begin_fold()
    for index, (ts, conv_id, _, day_num, conv_title, r, usage) in enumerate(ordered):
        cost_cny, tokens, incomplete, unallocated = _quote_row(
            r, usage, ts, token_costs, evidence, residuals.get(index, 0))
        if incomplete:
            incomplete_days.add(day_num)
            if unsettled_days is not None:
                unsettled_days.add(day_num)
        if token_costs is not None and not tokens and not cost_cny:
            continue
        if token_costs is None and cost_cny <= 0 and not (incomplete and tokens):
            continue

        if day_num not in days:
            days[day_num] = {'cost': 0.0, 'conversations': {}}
        days[day_num]['cost'] += cost_cny

        if conv_id not in days[day_num]['conversations']:
            days[day_num]['conversations'][conv_id] = {
                'name': conv_title,
                'cost': 0.0,
                'tokens': 0,
            }
        entry = days[day_num]['conversations'][conv_id]
        entry['cost'] += cost_cny
        entry['tokens'] += tokens
        if unallocated:
            entry['unallocated_tokens'] = entry.get('unallocated_tokens', 0) + unallocated

    for day_num in incomplete_days & days.keys():
        days[day_num]['partial'] = True
    precision = 6 if token_costs is not None else 4
    for day_data in days.values():
        day_data['cost'] = round(day_data['cost'], precision)
        for conv_entry in day_data['conversations'].values():
            conv_entry['cost'] = round(conv_entry['cost'], precision)

    return days


class _LiveDayCosts:
    """One reconstructible projection and price fold per owner, replaced on rollover."""

    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = None
        self.revision = None
        self.result = None

    def read(self, day, *, owner_user_id):
        from lib.conversations.usage_snapshot import reconcile_day_usage
        from lib.pricing import pricing_revision
        from .token_costs import DayTokenCosts

        start = _dt.datetime.combine(day, _dt.time.min)
        end = start + _dt.timedelta(days=1)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        with self.lock:
            snapshot = reconcile_day_usage(
                owner_user_id=owner_user_id, start_ms=start_ms, end_ms=end_ms,
                limit=_COST_CANDIDATE_LIMIT, title_characters=_TITLE_FALLBACK_CHARACTERS,
                previous=self.snapshot)
            revision = pricing_revision()
            if snapshot is self.snapshot and revision == self.revision:
                return self.result
            token_costs = DayTokenCosts()
            token_costs.partial = snapshot.candidate_count >= _COST_CANDIDATE_LIMIT
            day_data = _fold_costs(snapshot.projections, start_ms, end_ms, day.year, day.month,
                                   token_costs=token_costs).get(day.day, {'conversations': {}})
            summary = token_costs.finish(day)
            result = {**day_data, 'cost': summary['total_cny'], 'token_costs': summary}
            if summary['partial']:
                result['partial'] = True
            self.snapshot, self.revision, self.result = snapshot, revision, result
            return result


# Invalidation drops the holder, not just its value. Work already inside an
# invalidated holder cannot republish into the next reader's holder.
_live_day_cache = TTLCache(ttl=0, max_size=_CALENDAR_CACHE_SIZE, name='daily_report_live_usage')


def _get_live_day_costs(day, *, owner_user_id):
    return _live_day_cache.get_or_compute(owner_user_id, _LiveDayCosts).read(
        day, owner_user_id=owner_user_id)


def _load_cached_day_costs(year, month, *, owner_user_id):
    """Load persisted per-day costs for a given month from daily_cost_cache.

    Returns:
        dict mapping day-of-month (int) → {'cost': float, 'conversations': {...}}
        for days that have cached entries.  Days without entries are absent.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cached costs')
    try:
        from lib.storage import get_storage_client
        rows = get_storage_client().query('daily_cost.month', {
            'user_id': owner_id, 'year': year, 'month': month,
        })
    except Exception as e:
        logger.warning('[DailyReport] Load cached costs %d-%02d failed: %s',
                       year, month, e)
        return {}

    out = {}
    for r in rows:
        date_str = r['date']
        try:
            day_num = int(date_str.split('-')[2])
        except (ValueError, IndexError, AttributeError) as e:
            logger.debug('[DailyReport] Skipping invalid cached date %r: %s',
                         date_str, e)
            continue
        cost_val = float(r['cost'])
        convs = r.get('conversations') or {}
        if not isinstance(convs, dict):
            convs = {}
        out[day_num] = {'cost': round(cost_val, 4), 'conversations': convs}
    return out


def _persist_day_cost(date_str, day_data, *, owner_user_id):
    """Write a single day's cost aggregate to daily_cost_cache.

    Args:
        date_str: 'YYYY-MM-DD'.
        day_data: {'cost': float, 'conversations': {conv_id: {...}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost persistence')
    try:
        from lib.storage import get_storage_client
        get_storage_client(write=True).command('daily_cost.upsert', {
            'user_id': owner_id,
            'date': date_str,
            'cost': float(day_data.get('cost', 0.0)),
            'conversations': day_data.get('conversations', {}),
            'computed_at': int(time.time() * 1000),
        }, f'daily-cost-upsert:{uuid.uuid4().hex}')
    except Exception as e:
        logger.warning('[DailyReport] Persist day cost %s failed: %s',
                       date_str, e)


def invalidate_day_cost_cache(date_str=None, *, owner_user_id):
    """Invalidate persisted per-day cost cache entries.

    Args:
        date_str: If given, remove only that day ('YYYY-MM-DD').
                  If None, clear all entries (e.g. on bulk delete).
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost invalidation')
    try:
        from lib.storage import get_storage_client
        payload = {'user_id': owner_id}
        if date_str is not None:
            payload['date'] = date_str
        get_storage_client(write=True).command(
            'daily_cost.delete', payload,
            f'daily-cost-delete:{uuid.uuid4().hex}')
        if date_str:
            logger.debug('[DailyReport] Invalidated day-cost cache for %s', date_str)
        else:
            logger.info('[DailyReport] Invalidated ALL day-cost cache entries')
        if date_str is None or date_str == _dt.date.today().isoformat():
            _live_day_cache.invalidate(owner_id)
        # Also drop the in-process calendar overview (cached or in flight)
        # so the next request picks up the change.
        from .calendar_overview import invalidate as invalidate_calendar
        invalidate_calendar(owner_id)
    except Exception as e:
        logger.warning('[DailyReport] invalidate_day_cost_cache(%s) failed: %s',
                       date_str, e)


def _cost_days_for_messages(messages, conv_start=0, conv_end=0):
    """Return the set of ``'YYYY-MM-DD'`` days a message list contributes cost to.

    Only messages carrying a ``usage`` dict affect the per-day aggregate, so
    those are the only days whose cache is stale after an edit/delete. The
    timestamp resolution mirrors :func:`_scan_costs_in_range` (message
    timestamp, else conversation-span fallback) so the invalidated days line
    up with the persisted aggregates.

    Args:
        messages: List of message dicts (or anything non-list → empty set).
        conv_start: Conversation created_at (epoch-ms) fallback for
            timestamp-less messages.
        conv_end: Conversation updated_at (epoch-ms) fallback.

    Returns:
        set[str] of day strings.
    """
    from .conversations import _safe_int_ts

    if not isinstance(messages, list):
        return set()
    cs = _safe_int_ts(conv_start or conv_end or 0)
    days: set = set()
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get('usage'):
            continue
        ts = _safe_int_ts(msg.get('timestamp', 0))
        if not ts:
            ts = cs
        if not ts:
            continue
        d = _dt.datetime.fromtimestamp(ts / 1000)
        days.add(f'{d.year:04d}-{d.month:02d}-{d.day:02d}')
    return days


def _persisted_cost_dates(date_strs, *, owner_user_id):
    """Return the subset of ``date_strs`` that already have a persisted row.

    A ``daily_cost_cache`` row for a past day is a *settled snapshot* — the
    day is over, its messages are immutable, so its aggregate never needs to
    change again. This lookup lets :func:`invalidate_cost_cache_for_messages`
    recognise such days and refuse to drop them.

    Args:
        date_strs: Iterable of ``'YYYY-MM-DD'`` day strings.

    Returns:
        set[str] of the day strings that have a persisted cache row.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report persisted costs')
    dates = [d for d in date_strs if d]
    if not dates:
        return set()
    try:
        from lib.storage import get_storage_client
        result = get_storage_client().query('daily_cost.persisted_dates', {
            'user_id': owner_id, 'dates': dates,
        })
    except Exception as e:
        logger.warning('[DailyReport] Persisted-date lookup failed: %s', e)
        return set()
    return set(result['dates'])


def _should_pin_day(date_str, today_str, persisted_dates):
    """Whether a day is a settled, immutable snapshot that must NOT be dropped.

    A day is pinned iff it is strictly BEFORE today AND already persisted in
    ``daily_cost_cache``. Once a day is over its cost aggregate is final, so a
    later edit/delete of a *cross-midnight* conversation (whose messages span
    that past day) must leave the past day's cache untouched — otherwise a
    single such edit forces a full live rescan of that day on the next
    calendar open, permanently defeating the persistent cost cache.

    This predicate is load-bearing: neutering it to ``return False`` makes
    every touched day invalidate again (the old behaviour) and turns the
    regression guard red.
    """
    return bool(date_str) and date_str < today_str and date_str in persisted_dates


def invalidate_cost_cache_for_messages(
        messages, conv_start=0, conv_end=0, *, owner_user_id):
    """Scoped cost-cache invalidation for a delete/edit of specific messages.

    Removes ONLY the persisted per-day entries the given messages touch,
    instead of nuking the whole table. This is what conversation/message
    deletes must call: wiping all days forces the next calendar open to
    live-rescan the entire month (~10s), so a single delete would otherwise
    permanently defeat the persistent cost cache.

    Settled-day pinning: a touched day that is strictly BEFORE today AND
    already persisted is treated as an immutable snapshot and is NOT
    invalidated (see :func:`_should_pin_day`). This is what makes historical
    balances stay stable + instant: a cross-midnight edit today can only ever
    drop *today's* (unsettled) entry, never yesterday's. An explicit recompute
    (a direct :func:`invalidate_day_cost_cache` call or a forced report
    regeneration) is unaffected — it bypasses this path entirely.

    Args:
        messages: The messages being removed (or the whole conversation's
            messages when a conversation is deleted).
        conv_start: Conversation created_at (epoch-ms) — timestamp fallback.
        conv_end: Conversation updated_at (epoch-ms) — timestamp fallback.

    Returns:
        set[str] of the day strings that were actually invalidated (pinned
        settled days are excluded).
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report scoped cost invalidation')
    day_strs = _cost_days_for_messages(messages, conv_start, conv_end)
    if not day_strs:
        return set()

    today_str = _dt.date.today().isoformat()
    # Only past days can possibly be pinned; look those up in one query.
    past_days = {d for d in day_strs if d < today_str}
    persisted = (
        _persisted_cost_dates(past_days, owner_user_id=owner_id)
        if past_days else set()
    )
    pinned = {d for d in day_strs if _should_pin_day(d, today_str, persisted)}

    to_invalidate = day_strs - pinned
    for date_str in to_invalidate:
        invalidate_day_cost_cache(date_str, owner_user_id=owner_id)
    if pinned:
        logger.debug('[DailyReport] Pinned %d settled day(s) (not invalidated): %s',
                     len(pinned), sorted(pinned))
    if to_invalidate:
        logger.debug('[DailyReport] Scoped cost invalidation for %d day(s): %s',
                     len(to_invalidate), sorted(to_invalidate))
    return to_invalidate


def _get_monthly_costs(year, month, *, owner_user_id):
    """Return per-day cost breakdown for a month, using persistent cache.

    Strategy:
      - For past days (date < today): read from daily_cost_cache.  On miss,
        compute that day and persist (messages on past days are immutable).
      - For today: reconcile the rolling projection against current owner
        metadata. Do not persist it as a settled day until backfill.
      - Future days: skipped entirely.

    Args:
        year: Calendar year (int).
        month: Calendar month 1-12 (int).

    Returns:
        dict mapping day-of-month (int) → {'cost': float,
            'conversations': {conv_id: {'name': str, 'cost': float, 'tokens': int}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report monthly costs')
    t0 = time.monotonic()
    today = _dt.date.today()

    # Determine which past days need an on-demand compute+persist pass.
    if month < 12:
        next_month_start = _dt.date(year, month + 1, 1)
    else:
        next_month_start = _dt.date(year + 1, 1, 1)
    month_start = _dt.date(year, month, 1)

    # Past-day range for this month (days strictly before today):
    if month_start >= today:
        past_end = month_start  # no past days in this month
    elif next_month_start <= today:
        past_end = next_month_start  # whole month is past
    else:
        past_end = today  # part of month is past

    # 1) Load already-persisted day rows.  This returns ALL cached rows
    #    including zero-cost days — we need those to know they've been
    #    scanned already (so we don't rescan them), but we filter them out
    #    of the final response below to match legacy behavior.
    cached_days = _load_cached_day_costs(
        year, month, owner_user_id=owner_id)
    cached_hits = len(cached_days)
    days = {}  # response payload — only non-zero days

    # 2) Back-fill any past day that's missing from the cache by scanning
    #    just those days' range and persisting the result (zeros included,
    #    so they're not scanned again next time).
    missing_past_days = []
    if past_end > month_start:
        d = month_start
        while d < past_end:
            if d.day not in cached_days:
                missing_past_days.append(d)
            d += _dt.timedelta(days=1)

    if missing_past_days:
        # Scan the tight range covering only the missing past days.
        # In the common case (modal opened on a settled month with zero cache)
        # this is still just one scan of the month — but once filled, it's
        # free forever.
        range_start = missing_past_days[0]
        range_end = missing_past_days[-1] + _dt.timedelta(days=1)
        ms_range_start = int(_dt.datetime.combine(range_start, _dt.time.min).timestamp() * 1000)
        ms_range_end = int(_dt.datetime.combine(range_end, _dt.time.min).timestamp() * 1000)
        unsettled_days = set()
        scanned = _scan_costs_in_range(
            ms_range_start, ms_range_end, year, month,
            owner_user_id=owner_id, unsettled_days=unsettled_days)

        for d_obj in missing_past_days:
            day_num = d_obj.day
            day_data = scanned.get(day_num, {'cost': 0.0, 'conversations': {}})
            date_str = f'{year:04d}-{month:02d}-{day_num:02d}'
            # Pin only complete observations; unknown cost is not a settled zero.
            if day_num not in unsettled_days:
                _persist_day_cost(date_str, day_data, owner_user_id=owner_id)
            else:
                day_data = {**day_data, 'partial': True}
            cached_days[day_num] = day_data

    # Copy non-zero cached/backfilled days into the response.
    for day_num, day_data in cached_days.items():
        if day_data.get('cost', 0) > 0 or day_data.get('partial'):
            days[day_num] = day_data

    # 3) Reconcile today's live entry, including a successfully empty day.
    today_cost = None
    if year == today.year and month == today.month:
        days[today.day] = _get_live_day_costs(today, owner_user_id=owner_id)
        today_cost = days[today.day]['cost'] or None

    elapsed = time.monotonic() - t0
    total_cost = sum(d['cost'] for d in days.values())
    logger.info('[DailyReport] Monthly costs %d-%02d: %d days with costs, '
                '¥%.2f total (%d cache hits, %d live-computed past days, '
                'today=%s) in %.2fs',
                year, month, len(days), total_cost, cached_hits,
                len(missing_past_days),
                f'¥{today_cost:.2f}' if today_cost is not None else 'n/a',
                elapsed)
    return days
