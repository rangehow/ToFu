"""Fold today's Turn quotes and reprice the observed token mix at catalog rates.

Consumed by the existing calendar scan. Neither the fold nor its model
estimates read transcripts, write storage, or dispatch a model.
"""

from lib.cost import compute_cost
from lib.pricing import MODEL_PRICING, QWEN_PRICING_CNY, get_pricing_data, normalize_pricing

_BUCKETS = {
    'input': ('inputTokens', 'inputCostCny'),
    'output': ('outputTokens', 'outputCostCny'),
    'cache_read': ('cacheReadTokens', 'cacheReadCostCny'),
    'cache_write': ('cacheWriteTokens', 'cacheWriteCostCny'),
}


class DayTokenCosts:
    """Accumulate disjoint token counts even when their recorded price is zero."""

    def __init__(self):
        self.buckets = {key: {'tokens': 0, 'cost_cny': 0.0} for key in _BUCKETS}
        self.cache_write_1h = 0
        self.partial = False
        self.used_fallback_rates = False

    def add(self, quote):
        if not quote:
            return 0.0, 0
        return self.add_population(quote, quote)

    def add_population(self, population, quote=None):
        """Known token counts contribute even when their money is unknown."""
        amount_cny, token_count = 0.0, 0
        for key, (tokens, cost) in _BUCKETS.items():
            count = population.get(tokens, 0) if population is not None else 0
            self.buckets[key]['tokens'] += count
            token_count += count
            if quote is not None:
                self.buckets[key]['cost_cny'] += quote[cost]
                amount_cny += quote[cost]
        if population is not None:
            self.cache_write_1h += population.get('cacheWrite1hTokens', 0)
        if quote is not None:
            self.used_fallback_rates |= quote['pricingSource'] in (
                'default_estimate', 'qwen_default_estimate')
        return amount_cny, token_count

    def finish(self, date):
        buckets = {key: {**bucket, 'cost_cny': round(bucket['cost_cny'], 6)}
                   for key, bucket in self.buckets.items()}
        return {
            'date': date.isoformat(),
            'buckets': buckets,
            'total_cny': round(sum(bucket['cost_cny'] for bucket in self.buckets.values()), 6),
            'models': self._model_estimates(),
            'partial': self.partial,
            'used_fallback_rates': self.used_fallback_rates,
        }

    def _model_estimates(self):
        counts = {key: bucket['tokens'] for key, bucket in self.buckets.items()}
        if not any(counts.values()):
            return []
        # Residual input is already disjoint from read and write. Preserve that
        # spelling when handing the aggregate back to the shared pricing math.
        usage = {
            'input_tokens': counts['input'], 'output_tokens': counts['output'],
            'cache_read_input_tokens': counts['cache_read'],
            'cache_creation_input_tokens': counts['cache_write'],
            'cache_creation': {'ephemeral_1h_input_tokens': self.cache_write_1h},
        }
        pricing_data = get_pricing_data()
        estimates = []
        for model_id in sorted((MODEL_PRICING.keys() | QWEN_PRICING_CNY.keys()) - {'_default'}):
            rate = normalize_pricing(model_id, MODEL_PRICING.get(model_id))
            # Use the standard (first) tier and no peak adjustments. Applying
            # a context threshold to a whole day's token count would overprice
            # it as one giant prompt. The UI makes the standard-rate assumption explicit.
            tiers = rate.get('contextTiers')
            if tiers:
                rate.update(tiers[0])
            if 'input' not in rate or 'output' not in rate:
                continue
            quote = compute_cost(usage, model_id, pricing_override=rate, pricing_data=pricing_data,
                                 unrounded_cny_components=True)
            cost_cny = round(sum(quote[cost] for _tokens, cost in _BUCKETS.values()), 6)
            estimates.append({'id': model_id, 'name': rate.get('name') or model_id,
                              'cost_cny': cost_cny})
        return estimates
