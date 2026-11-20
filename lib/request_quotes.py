"""Read request-local quotes from the storage usage projection, without repricing."""

from math import fsum

from lib.cost import (
    _AGGREGATE_KEYS, _AGGREGATE_TOKEN_KEYS, _ANTHROPIC_RESIDUAL_KEYS, _USAGE_KEY_ALIASES, _USAGE_NESTED_ALIASES,
    _cache_write_1h_tokens, compute_api_rounds_cost, normalize_usage, split_input_tokens, usage_cache_convention,
)


_POPULATION_KEYS = ('inputTokens', 'outputTokens', 'totalInputTokens',
                    'cacheReadTokens', 'cacheWriteTokens')
_COMPONENT_KEYS = ('inputCostCny', 'outputCostCny', 'cacheReadCostCny', 'cacheWriteCostCny')


def _has_numbers(value, keys):
    return isinstance(value, dict) and all(
        type(value.get(key)) in (int, float) and 0 <= value[key] < float('inf')
        for key in keys)


def receipt_identity(row):
    receipt = row.get('usageReceiptId')
    if isinstance(receipt, str) and receipt:
        return ('receipt', receipt)
    task_id = row.get('taskId')
    origin = row.get('sourceRound', row.get('round'))
    if isinstance(task_id, str) and task_id and type(origin) is int and origin > 0:
        return ('native', task_id, origin)
    return None


def usage_population(usage, *, sparse=False):
    """Project known counters without borrowing a price or another request's tier."""
    if not isinstance(usage, dict) or not usage:
        return None
    def known(channel):
        values = [usage.get(k) for k in _USAGE_KEY_ALIASES[channel]]
        values.extend(usage[parent].get(key) for parent, key in _USAGE_NESTED_ALIASES.get(channel, ())
                      if isinstance(usage.get(parent), dict))
        return any(type(v) in (int, float) and 0 <= v < float('inf') for v in values)

    counts = normalize_usage(usage)
    cold, total = split_input_tokens(usage)
    population = {}
    if known('input'):
        if not sparse:
            population.update(inputTokens=cold, totalInputTokens=total)
        elif usage_cache_convention(usage) == 'anthropic':
            # Residual-style input is itself observed, not inferred from cache.
            population['inputTokens'] = cold
        else:
            # Inclusive prompt count says nothing about the cold subset.
            population['totalInputTokens'] = total
    if known('output') or known('thinking'):
        population['outputTokens'] = counts['output'] or counts['thinking']
    if known('cache_read'):
        population['cacheReadTokens'] = counts['cache_read']
    if known('cache_write'):
        population['cacheWriteTokens'] = counts['cache_write']
        if not sparse or 'cache_creation' in usage or counts['cache_write'] == 0:
            population['cacheWrite1hTokens'] = _cache_write_1h_tokens(usage, counts['cache_write'])
    return population or None


def merge_usage_observations(target, usage, conflicts, path=()):
    """Join public numeric wire observations, preserving zero and wire keys."""
    for key, value in (usage or {}).items():
        if path + (key,) in conflicts or key.startswith('_'):
            continue
        if isinstance(value, dict):
            if isinstance(target.setdefault(key, {}), dict):
                merge_usage_observations(target[key], value, conflicts, path + (key,))
        elif type(value) in (int, float) and 0 <= value < float('inf'):
            if key not in target or target[key] == value:
                target[key] = value
            else:
                target.pop(key)
                conflicts.add(path + (key,))


def _join_wire_population(wire, quoted):
    population = usage_population(wire, sparse=True) or {}
    # A sparse wire spelling can be ambiguous. A frozen split disambiguates
    # only when the observed input matches one of its two witnessed counts.
    if 'totalInputTokens' in quoted and 'inputTokens' in quoted:
        total, cold = quoted['totalInputTokens'], quoted['inputTokens']
        prompt = wire.get('prompt_tokens')
        native = wire.get('input_tokens')
        if 'totalInputTokens' in population and prompt == cold and total != cold:
            population.pop('totalInputTokens')
        if ('inputTokens' in population and native == total and total != cold
                and not any(k in wire for k in ('cache_read_input_tokens', 'cache_creation_input_tokens'))):
            population.pop('inputTokens')
    return population


def has_complete_price(quote):
    return (_has_numbers(quote, _COMPONENT_KEYS)
            and isinstance(quote.get('pricingSource'), str))


def _evidence(row):
    if not isinstance(row, dict):
        return None, None, True
    quote = row.get('cost')
    population = ({k: quote[k] for k in _POPULATION_KEYS} | {
        'cacheWrite1hTokens': quote.get('cacheWrite1hTokens', 0)}
        if _has_numbers(quote, _POPULATION_KEYS) else usage_population(row.get('usage'), sparse=True))
    price = ({k: quote[k] for k in (*(k for k in _AGGREGATE_KEYS if k not in _AGGREGATE_TOKEN_KEYS
                              and not k.startswith('cacheSavings')), 'pricingSource') if k in quote}
             if has_complete_price(quote) else None)
    return population, price, bool(row.get('pricingEvidence'))


def _derive_population(observed, conflicts=()):
    """Apply the disjoint-input identity only after joining observations."""
    population = {k: v for k, v in observed.items() if k not in conflicts}
    keys = ('inputTokens', 'cacheReadTokens', 'cacheWriteTokens')
    if 'totalInputTokens' not in population and 'totalInputTokens' not in conflicts:
        if all(k in population for k in keys):
            population['totalInputTokens'] = sum(population[k] for k in keys)
    if 'inputTokens' not in population and 'inputTokens' not in conflicts:
        if all(k in population for k in ('totalInputTokens', *keys[1:])):
            residual = population['totalInputTokens'] - sum(population[k] for k in keys[1:])
            if residual >= 0:
                population['inputTokens'] = residual
    return population or None


class RequestEvidence:
    """Join receipt copies before aggregation; unknown is not a conflicting value."""

    def __init__(self):
        self._records = {}
        self._seen = set()

    def begin_fold(self):
        self._seen.clear()

    def observe(self, rounds):
        for row in rounds:
            identity = receipt_identity(row) if isinstance(row, dict) else None
            if identity is None:
                continue
            population, price, uncertain = _evidence(row)
            record = self._records.setdefault(identity, [{}, set(), [], False, {}, set(), {}, {}, set()])
            raw = row.get('usage')
            if isinstance(raw, dict):
                for key in ('prompt_tokens', 'input_tokens'):
                    value = raw.get(key)
                    if type(value) in (int, float) and 0 <= value < float('inf'):
                        claims = record[6].setdefault(key, set())
                        if len(claims) < 3:
                            claims.add(value)
                # Keep convention-bearing observations separate from aliases
                # whose meaning can only be inferred from the joined receipt.
                if any(raw.get(key) is not None for key in _ANTHROPIC_RESIDUAL_KEYS):
                    explicit = usage_population(raw, sparse=True) or {}
                    cold = explicit.get('inputTokens')
                    if cold is not None:
                        merge_usage_observations(record[7], {'inputTokens': cold}, record[8])
                merge_usage_observations(record[4], raw, record[5])
            quoted = row.get('cost')
            if not _has_numbers(quoted, _POPULATION_KEYS):
                population = None
            for key, value in (population or {}).items():
                if key in record[0] and record[0][key] != value:
                    record[1].add(key)
                record[0][key] = value
            if price is not None and price not in record[2] and len(record[2]) < 2:
                record[2].append(price)
            record[3] |= uncertain


    def take(self, rounds, *, exclude_anonymous_usage=False):
        allocated = []
        covered = []
        partial = False
        unknown_ids = []
        visited = set()
        for row in rounds:
            identity = receipt_identity(row) if isinstance(row, dict) else None
            if identity is not None:
                if identity in visited:
                    continue
                visited.add(identity)
                quoted, conflicts, prices, uncertain, wire, wire_conflicts, claims, explicit, explicit_conflicts = self._records[identity]
                if {'inputTokens', 'totalInputTokens'} <= quoted.keys():
                    witnessed = {quoted['inputTokens'], quoted['totalInputTokens']}
                    wire_conflicts = {path for path in wire_conflicts
                                      if not (len(path) == 1 and path[0] in claims
                                              and claims[path[0]] <= witnessed)}
                observed = _join_wire_population(wire, quoted)
                if any(path in wire_conflicts for path in (('prompt_tokens',), ('input_tokens',))):
                    # With the input claim removed, cache-only wire data must
                    # not be interpreted as another input count.
                    observed.pop('inputTokens', None)
                    observed.pop('totalInputTokens', None)
                conflicts = conflicts | {k for k in observed.keys() & quoted.keys()
                                         if observed[k] != quoted[k]}
                uncertain |= bool(conflicts)
                # A raw input claim can dispute a frozen split, but cannot
                # erase its witnessed counts. Conflicting frozen splits still
                # withhold the disputed fields (record[1]).
                conflicts = conflicts - ({'inputTokens', 'totalInputTokens'} - self._records[identity][1])
                population = _derive_population({**observed, **quoted}, conflicts)
                uncertain |= (bool(wire_conflicts or explicit_conflicts)
                              or any(key in quoted and quoted[key] != value
                                     for key, value in explicit.items()))
                price = prices[0] if len(prices) == 1 else None
                uncertain |= bool(conflicts)
                fresh = identity not in self._seen
                self._seen.add(identity)
            else:
                population, price, uncertain = _evidence(row)
                population = _derive_population(population or {})
                fresh = True
            partial |= (population is None or not all(k in population for k in _POPULATION_KEYS)
                        or price is None or uncertain or identity is None
                        or identity[0] != 'receipt')
            if exclude_anonymous_usage and identity is None and price is None:
                # Keep the observations for the Turn coverage witness, not
                # as independently additive request populations.
                unknown_ids.append(None)
                continue
            if population is not None:
                covered.append(population)
            if population is None or not all(k in population for k in ('totalInputTokens', 'outputTokens')):
                unknown_ids.append(identity)
            if fresh:
                allocated.append((population, price))
        return allocated, covered, partial, unknown_ids


def component_amount(quote):
    return fsum(quote[key] for key in _COMPONENT_KEYS)


def has_receipt_evidence(rounds):
    return any(isinstance(row, dict) and (receipt_identity(row) is not None or 'cost' in row)
               for row in rounds)


def fold_stored_quotes(rounds, *, usage=None, model='', provider=None):
    """One stored-quote display fold for live and durable output."""
    evidence = RequestEvidence()
    evidence.observe(rounds)
    allocated, _, partial, unknown = evidence.take(rounds)
    rows = [{'cost': {**price, **(population or {})}, 'model': model,
             'provider_id': provider} for population, price in allocated if price is not None]
    result = compute_api_rounds_cost(rows, use_frozen_costs=True)
    if result:
        for key in _POPULATION_KEYS:
            measured = [population[key] for population, _ in allocated if population and key in population]
            if measured:
                result[key] = sum(measured)
        for key, value in (usage_population(usage) or {}).items():
            if unknown and key in ('totalInputTokens', 'outputTokens') and value > result.get(key, 0):
                result[key] = value
                partial = True
        snapshot = result['pricingSnapshot']
        for name, route_key, aliases, fallback in (
                ('models', 'model', ('model',), model),
                ('providers', 'provider', ('provider_id', 'providerId'), provider)):
            routes = set()
            for row in rounds:
                if not isinstance(row, dict) or not isinstance(row.get('cost'), dict):
                    continue
                stored = row['cost'].get('pricingSnapshot') or {}
                value = stored.get(route_key) if isinstance(stored, dict) else None
                value = value or next((row[k] for k in aliases if row.get(k)), fallback)
                if isinstance(value, str) and value:
                    routes.add(value)
            snapshot[name] = sorted(routes)
        if partial and any(isinstance(row, dict) and (receipt_identity(row) is not None or 'pricingEvidence' in row)
                           for row in rounds):
            snapshot['partial'] = True
    return result
