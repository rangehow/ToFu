"""Pure operation latency metrics shared by collection and receipt validation."""
import statistics


def _percentile(sorted_samples, fraction):
    index = min(
        len(sorted_samples) - 1,
        max(0, int(round(fraction * (len(sorted_samples) - 1)))),
    )
    return sorted_samples[index]


def latency_metrics(samples_ms):
    ordered = sorted(samples_ms)
    total_s = sum(ordered) / 1e3
    return {
        'p50_ms': round(_percentile(ordered, 0.50), 6),
        'p95_ms': round(_percentile(ordered, 0.95), 6),
        'p99_ms': round(_percentile(ordered, 0.99), 6),
        'mean_ms': round(statistics.fmean(ordered), 6),
        'min_ms': round(ordered[0], 6),
        'max_ms': round(ordered[-1], 6),
        'ops_per_sec': round(len(ordered) / total_s, 1) if total_s else 0.0,
    }
