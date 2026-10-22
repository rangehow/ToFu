"""Scale-up/down, lean failure, zero headroom and explicit override checks."""
import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from budget import derive_cache_budget
from runtime_guards import probe_system_resources, resource_budget_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    snapshot = probe_system_resources()
    manifest = resource_budget_manifest(snapshot=snapshot)
    configured = os.environ.get('TOFU_MAX_INFLIGHT_TASKS', '')
    try:
        scopes = int(configured)
    except ValueError:
        scopes = 0
    if scopes < 1:
        scopes = manifest['defaults']['TOFU_MAX_INFLIGHT_TASKS']
    actual = derive_cache_budget(snapshot, maximum_live_scopes=scopes)
    small = replace(snapshot, effective_memory_capacity_mb=1024, effective_memory_available_mb=512)
    large = replace(snapshot, effective_memory_capacity_mb=16384, effective_memory_available_mb=8192)
    low = derive_cache_budget(small, maximum_live_scopes=8)
    high = derive_cache_budget(large, maximum_live_scopes=8)
    assert high.total_bytes == low.total_bytes * 16
    assert high.per_scope_bytes == low.per_scope_bytes * 16
    assert derive_cache_budget(large, maximum_live_scopes=16).per_scope_bytes == high.per_scope_bytes // 2
    missing = replace(snapshot, effective_memory_capacity_mb=None, effective_memory_available_mb=None)
    assert derive_cache_budget(missing, maximum_live_scopes=8).source == 'probe_failure_lean_fallback'
    exhausted = replace(snapshot, effective_memory_available_mb=0)
    assert derive_cache_budget(exhausted, maximum_live_scopes=8).per_scope_bytes == 0
    override = derive_cache_budget(exhausted, maximum_live_scopes=8, override_per_scope_bytes=123456)
    assert override.per_scope_bytes == 123456 and override.total_bytes == 8 * 123456
    result = {'scope': 'proposed cache plan; process-wide quota/task registration and performance unverified',
              'probe': snapshot.as_dict(), 'maximum_live_scopes': scopes,
              'actual_plan': asdict(actual), 'small_plan': asdict(low), 'large_plan': asdict(high),
              'verdict': 'budget_scaling_checks_pass; no_production_admission'}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('shared launch probe, scale-up/down, concurrent scope division, lean failure, zero-headroom and override checks passed')


if __name__ == '__main__':
    main()
