"""Experimental cache plan from one supplied launch probe, not new probing.

Application concurrency and operator overrides remain explicit inputs. This
plan does not itself register or enforce a process-wide cache reservation pool.
"""
from dataclasses import dataclass

MIB = 1024 * 1024


@dataclass(frozen=True)
class CacheBudgetPlan:
    total_bytes: int
    per_scope_bytes: int
    maximum_live_scopes: int
    source: str


def derive_cache_budget(snapshot, *, maximum_live_scopes, override_per_scope_bytes=None):
    if type(maximum_live_scopes) is not int or maximum_live_scopes < 1:
        raise ValueError('explicit positive live-scope limit required')
    if override_per_scope_bytes is not None:
        if type(override_per_scope_bytes) is not int or override_per_scope_bytes < 1:
            raise ValueError('cache override must be a positive integer')
        return CacheBudgetPlan(override_per_scope_bytes * maximum_live_scopes,
                               override_per_scope_bytes, maximum_live_scopes, 'operator_override')
    capacity = snapshot.effective_memory_capacity_mb
    headroom = snapshot.effective_memory_available_mb
    if capacity is None or headroom is None:
        total = MIB
        source = 'probe_failure_lean_fallback'
    elif capacity <= 0 or headroom <= 0:
        total = 0
        source = 'no_observed_memory_headroom'
    else:
        # Bound reconstructible cache growth relative to both physical/cgroup
        # capacity and current headroom. No fixed high-end-host ceiling.
        total = min(capacity * MIB // 64, headroom * MIB // 16)
        source = 'shared_launch_probe'
    per_scope = total // maximum_live_scopes
    return CacheBudgetPlan(per_scope * maximum_live_scopes, per_scope,
                           maximum_live_scopes, source)
