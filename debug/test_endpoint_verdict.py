"""Unit tests for the three-way endpoint verdict parser.

Covers:
  1. Explicit [VERDICT: STOP]
  2. Explicit [VERDICT: CONTINUE_WORKER]
  3. Explicit [VERDICT: CONTINUE_PLANNER] with a mandatory PLAN_DEFECT tag
  4. Legacy bare [VERDICT: CONTINUE] (must map to 'worker')
  5. No tag at all (must default to 'worker')
  6. Double tag (last wins)
  7. Defense-in-depth: STOP with ❌ → downgrade to 'worker' (2026-04-26
     rewrite — previously 'planner', but residual ❌ is a worker problem,
     not a plan problem)
  8. Defense-in-depth: STOP with "still NOT met" → 'worker'
  9. CONTINUE_PLANNER without [PLAN_DEFECT: ...] → downgrade to 'worker'
 10. CONTINUE_PLANNER with worker-rationalization defect ("worker didn't…")
     → downgrade to 'worker'
 11. Kill switch: CHATUI_ENDPOINT_REPLAN=0 downgrades planner→worker

Run: python debug/test_endpoint_verdict.py
Exits 0 on success, raises on failure.
"""

import os
import sys

# Ensure project root is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _reload_endpoint_review():
    """Re-import endpoint_review so env changes to CHATUI_ENDPOINT_REPLAN take effect."""
    import importlib

    import lib.tasks_pkg.endpoint_review as mod
    importlib.reload(mod)
    return mod


def _test_replan_enabled():
    """Helper: run the parser tests with replan enabled."""
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'
    mod = _reload_endpoint_review()
    _parse_verdict = mod._parse_verdict

    # 1. STOP
    fb, ph, defect = _parse_verdict("All items ✅\n[VERDICT: STOP]")
    assert ph == 'stop', f"STOP: expected 'stop', got {ph!r}"
    assert fb == "All items ✅", f"STOP: feedback mismatch: {fb!r}"
    assert defect is None

    # 2. CONTINUE_WORKER
    fb, ph, defect = _parse_verdict("Needs iter 2.\n[VERDICT: CONTINUE_WORKER]")
    assert ph == 'worker', f"CONT_WORKER: expected 'worker', got {ph!r}"
    assert defect is None

    # 3. CONTINUE_PLANNER with a valid PLAN_DEFECT tag
    fb, ph, defect = _parse_verdict(
        "Plan is wrong.\n"
        "[PLAN_DEFECT: checklist item 4 assumes trading enabled but TRADING_ENABLED=0]\n"
        "[VERDICT: CONTINUE_PLANNER]"
    )
    assert ph == 'planner', f"CONT_PLANNER w/ defect: expected 'planner', got {ph!r}"
    assert defect and 'trading' in defect.lower(), (
        f"Defect reason not captured: {defect!r}"
    )
    assert 'PLAN_DEFECT' not in fb, f"Defect tag not stripped from feedback: {fb!r}"

    # 3b. CONTINUE_PLANNER WITHOUT a PLAN_DEFECT tag → downgraded to worker.
    fb, ph, defect = _parse_verdict(
        "Plan is wrong — user changed scope mid-turn.\n[VERDICT: CONTINUE_PLANNER]"
    )
    assert ph == 'worker', (
        f"CONT_PLANNER w/o defect should downgrade: expected 'worker', "
        f"got {ph!r}"
    )
    assert defect is None

    # 3c. CONTINUE_PLANNER with worker-rationalization defect → downgraded.
    fb, ph, defect = _parse_verdict(
        "[PLAN_DEFECT: worker didn't finish item 3]\n[VERDICT: CONTINUE_PLANNER]"
    )
    assert ph == 'worker', (
        f"CONT_PLANNER w/ worker-rationalization defect should downgrade: "
        f"got {ph!r}"
    )
    assert defect and "worker didn't" in defect.lower(), defect

    # 4. Legacy bare CONTINUE
    fb, ph, defect = _parse_verdict("Needs work. [VERDICT: CONTINUE]")
    assert ph == 'worker', f"Legacy CONT: expected 'worker', got {ph!r}"

    # 5. No tag
    fb, ph, defect = _parse_verdict("Some feedback without a verdict tag.")
    assert ph == 'worker', f"No-tag: expected 'worker', got {ph!r}"

    # 6. Double tag — last wins
    fb, ph, defect = _parse_verdict(
        "First draft said [VERDICT: CONTINUE_WORKER] but on reflection "
        "[VERDICT: STOP]"
    )
    assert ph == 'stop', f"Double-tag: expected 'stop', got {ph!r}"

    # 7. Defense-in-depth: STOP with ❌ → downgrade to WORKER (not planner).
    #    Residual ❌ is a worker-execution problem, not a plan-structural one.
    fb, ph, defect = _parse_verdict(
        "- ❌ Item 1: failing\n- ❌ Item 2: also failing\n[VERDICT: STOP]"
    )
    assert ph == 'worker', (
        f"Override STOP→worker (❌): expected 'worker', got {ph!r}"
    )

    # 8. Defense-in-depth: STOP with "still NOT met" → worker
    fb, ph, defect = _parse_verdict(
        "Acceptance criterion 1 is still NOT met.\n[VERDICT: STOP]"
    )
    assert ph == 'worker', (
        f"Override STOP→worker (phrase): expected 'worker', got {ph!r}"
    )

    # STOP without any unresolved markers should remain STOP
    fb, ph, defect = _parse_verdict("Everything passes ✅ ✅ ✅.\n[VERDICT: STOP]")
    assert ph == 'stop', f"Clean STOP: expected 'stop', got {ph!r}"

    # Verdict tag correctly stripped from feedback
    fb, _ph, _d = _parse_verdict("Test body.\n### Verdict\n[VERDICT: STOP]")
    assert '[VERDICT' not in fb, f"Tag not stripped: {fb!r}"
    assert '### Verdict' not in fb, f"Header not stripped: {fb!r}"

    print('[test_endpoint_verdict] replan-enabled: all checks passed ✅')


def _test_replan_disabled():
    """When CHATUI_ENDPOINT_REPLAN=0: planner → worker is downgraded."""
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '0'
    mod = _reload_endpoint_review()
    _parse_verdict = mod._parse_verdict

    # planner → worker downgrade (even with a valid defect tag).
    fb, ph, defect = _parse_verdict(
        "[PLAN_DEFECT: plan requires forbidden library]\n[VERDICT: CONTINUE_PLANNER]"
    )
    assert ph == 'worker', (
        f"Kill-switch planner→worker: expected 'worker', got {ph!r}"
    )

    # Restore default for any follow-up tests
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'
    _reload_endpoint_review()

    print('[test_endpoint_verdict] replan-disabled: all 1 check passed ✅')


def main():
    _test_replan_enabled()
    _test_replan_disabled()
    print('\n[test_endpoint_verdict] ALL TESTS PASSED')


if __name__ == '__main__':
    main()
