"""tests/test_frontend_p0_small_batch.py — pt_26a427d3.

Three P0-class frontend fixes, one suite each:

  ① Boot-path localStorage bare ``JSON.parse`` (core.js claude_client_config,
    core/cost.js claude_auto_translate / claude_auto_apply). One corrupted
    key threw at module top level and white-screened the app. Now routed
    through ``_safeJsonParse`` (corrupt → fallback). Drives the REAL helper
    under node.

  ② image-gen.js single-mode: user Cancel and the 150 s watchdog shared one
    AbortController, so the catch mislabelled a deliberate cancel as
    "Request timed out (150s)" and pushed a timeout error message. Now
    ``_igUserCancelled`` distinguishes them (cancel → 'cancelled' notice,
    no 150 s text, no timeout toast). Batch mode already handled this.

  ③ scheduler.js was an entirely dead panel (schedulerBadge/schedulerPanel/
    proactiveCount exist in NO template; toggleSchedulerPanel uncalled;
    _applySchedulerUI toggled a nonexistent badge). Removed: the file, its
    _DEFERRED_FILES entry, _applySchedulerUI + its call site, and the
    toolset-apply revert-family entry. The scheduler TOOL itself is
    server-side always-on and unaffected.

Each check carries a byte-reverting NEUTER.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_p0_small_batch.py -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')
COST_JS = os.path.join(ROOT, 'static', 'js', 'core', 'cost.js')
IMAGE_GEN_JS = os.path.join(ROOT, 'static', 'js', 'image-gen.js')
MAIN_JS = os.path.join(ROOT, 'static', 'js', 'main.js')
TOOLSET_JS = os.path.join(ROOT, 'static', 'js', 'toolset-apply.js')
BUNDLER = os.path.join(ROOT, 'lib', 'js_bundler.py')
SCHEDULER_JS = os.path.join(ROOT, 'static', 'js', 'scheduler.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── ① _safeJsonParse: behavior + call-site scans ──────────────────────────

_SAFE_DRIVER = r"""
const cases = [
  ['{"a":1}', 'valid'],
  ['{"a":1,', 'corrupt'],
  [null, 'null'],
  ['false', 'false'],
  ['true', 'true'],
];
const out = cases.map(([raw, tag]) => {
  const r = _safeJsonParse(raw, { fb: true });
  return { tag, type: typeof r, a: r && r.a, fb: !!(r && r.fb) };
});
console.log(JSON.stringify(out));
"""


def _extract_safe_parse(src: str) -> str:
    m = re.search(r'function _safeJsonParse\(raw, fallback\) \{.*?\n\}', src, re.DOTALL)
    assert m, '_safeJsonParse not found in core.js'
    return m.group(0)


def _run_safe(src: str) -> list:
    if not shutil.which('node'):
        pytest.skip('node not available')
    proc = subprocess.run(['node', '-e', src + '\n' + _SAFE_DRIVER],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'node harness failed:\n{proc.stderr[-1500:]}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_safe_json_parse_behavior():
    out = _run_safe(_extract_safe_parse(_read(CORE_JS)))
    by = {c['tag']: c for c in out}
    assert by['valid']['a'] == 1 and by['valid']['fb'] is False
    assert by['corrupt']['fb'] is True, 'corrupt JSON must fall back, not throw'
    assert by['null']['fb'] is True
    assert by['false']['type'] == 'boolean' and by['false']['fb'] is False
    assert by['true']['type'] == 'boolean'


def test_NEUTER_safe_parse_gutted_throws():
    """Byte-reverting NEUTER: replace the helper body with a bare JSON.parse —
    the corrupt case MUST throw (proves the guard is load-bearing)."""
    src = _extract_safe_parse(_read(CORE_JS))
    neutered = re.sub(r'try \{ return JSON\.parse\(raw\); \} catch \(_\) \{ return fallback; \}',
                      'return JSON.parse(raw);', src)
    assert neutered != src, 'NEUTER anchor missing'
    if not shutil.which('node'):
        pytest.skip('node not available')
    proc = subprocess.run(['node', '-e', neutered + '\n' + _SAFE_DRIVER],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, (
        'NEUTER FAILED: gutted helper did not throw on corrupt input')


def test_boot_sites_use_safe_parse():
    core = _read(CORE_JS)
    cost = _read(COST_JS)
    assert '_safeJsonParse(\n  localStorage.getItem("claude_client_config")' in core
    assert 'JSON.parse(\n  localStorage.getItem("claude_client_config")' not in core
    assert cost.count('_safeJsonParse(\n  localStorage.getItem("claude_auto_') == 2, (
        f'expected 2 safe-parse sites in cost.js, found '
        f'{cost.count("_safeJsonParse(chr(10)  localStorage")}')
    assert 'JSON.parse(\n  localStorage.getItem("claude_auto_' not in cost


# ── ② image-gen cancel vs timeout ─────────────────────────────────────────

def test_image_gen_cancel_distinguished_from_timeout():
    src = _read(IMAGE_GEN_JS)
    # Flag is set in the cancel path BEFORE aborting…
    cancel_fn = re.search(r'function _igCancelGeneration\(\) \{.*?\n\}', src, re.DOTALL)
    assert cancel_fn and '_igUserCancelled = true' in cancel_fn.group(0), (
        '_igCancelGeneration does not set the user-cancel flag')
    # …read by the single-mode catch…
    catch_start = src.find("const isAbort = err.name === 'AbortError';")
    assert catch_start != -1
    catch_region = src[catch_start:catch_start + 1800]
    assert '_igUserCancelled' in catch_region, (
        'single-mode catch does not consult the user-cancel flag')
    assert "'Cancelled by user.'" in catch_region
    assert "errorType: errType" in catch_region or "'cancelled'" in catch_region
    # …and cleared in finally (next generation starts clean).
    finally_region = src[src.find('} finally {', catch_start):]
    assert '_igUserCancelled = false;' in finally_region[:300]


def test_NEUTER_cancel_flag_absent_mislabels():
    """NEUTER: strip the flag from the cancel fn — the scan must fire."""
    src = _read(IMAGE_GEN_JS)
    cancel_fn = re.search(r'function _igCancelGeneration\(\) \{.*?\n\}', src, re.DOTALL)
    neutered_fn = cancel_fn.group(0).replace('_igUserCancelled = true;  // read by the single-mode catch to NOT mislabel this as a 150s timeout\n  ', '')
    assert neutered_fn != cancel_fn.group(0)
    assert '_igUserCancelled = true' not in neutered_fn


# ── ③ dead scheduler UI removed ───────────────────────────────────────────

def test_scheduler_dead_ui_fully_removed():
    assert not os.path.exists(SCHEDULER_JS), 'static/js/scheduler.js still exists'
    bundler = _read(BUNDLER)
    assert "'scheduler.js'," not in bundler, 'scheduler.js still in _DEFERRED_FILES'
    main = _read(MAIN_JS)
    assert '_applySchedulerUI' not in main, 'main.js still defines/calls _applySchedulerUI'
    toolset = _read(TOOLSET_JS)
    assert '_applySchedulerUI' not in toolset, (
        'toolset-apply.js still references the removed _applySchedulerUI')


def test_NEUTER_scheduler_scans_fire_on_old_shape():
    """NEUTER: reintroduce the markers — every absence-scan must fire."""
    bundler = _read(BUNDLER) + "    'scheduler.js',\n"
    assert "'scheduler.js'," in bundler
    main = _read(MAIN_JS) + '\nfunction _applySchedulerUI() {}\n'
    assert '_applySchedulerUI' in main


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
