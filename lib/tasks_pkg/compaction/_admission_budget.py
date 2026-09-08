"""Shared rendered-prompt ceiling for proactive recovery and final dispatch."""

from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold


def resolve_admission_budget(task, *, round_num=0):
    """Return hard ceiling and target, including first-dispatch host headroom."""
    effective, window_threshold, working_set = _compaction_trigger_threshold(task)
    configured = effective if working_set > 0 else window_threshold
    ceiling = max(1, min(window_threshold, configured))
    if round_num == 0:
        ceiling = min(ceiling, 256_000)
        return ceiling, max(1, ceiling * 15 // 16)
    return ceiling, ceiling
