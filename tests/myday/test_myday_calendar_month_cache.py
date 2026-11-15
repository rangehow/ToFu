"""My Day month cache: a busy month still paints the calendar instantly.

The retained panel persists each month overview through the typed, bounded
report repository (128 KiB per month) and paints it on the next open before
the network answers. The calendar paints only each day's cost and
conversation count; the server's per-conversation cost details grow with
every conversation (125,280 bytes by 2026-11-09 for one owner's November) and
must not push the month past the bound, where the write is skipped and the
instant paint is silently lost.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_sections_dir

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CACHE_OWNER = ROOT / 'frontend/src/features/myday/report-cache.ts'

_HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));  // typed bounded cache + repository
global.window = global;
const els = {};
const element = () => {
  const el = { _html: '', textContent: '', style: {}, children: [],
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    querySelectorAll: () => [], querySelector: () => null, appendChild() {}, replaceChildren() {},
    addEventListener() {}, setAttribute() {}, removeAttribute() {} };
  Object.defineProperty(el, 'innerHTML', { get() { return el._html; }, set(v) { el._html = v; } });
  return el;
};
global.document = {
  getElementById: (id) => (els[id] = els[id] || element()),
  querySelectorAll: () => [], createElement: () => element(), addEventListener() {},
  readyState: 'complete',
};
global.t = (key) => key;
const months = new Map();
const storage = {
  read: async (store, key) => (store === 'months' ? months.get(key)?.value ?? null : null),
  write: async (store, record) => { if (store === 'months') months.set(record.key, record); },
};
global.MyDayReportRepository = createMyDayReportRepository({
  cache: createMyDayPersistentCache({ storage, now: () => Date.now() }), ownerId: () => 7,
});
// A busy month: 120 titled conversations a day, each with its cost details.
const busy = {};
for (let day = 1; day <= 28; day += 1) {
  const conversations = {};
  for (let index = 0; index < 120; index += 1) {
    conversations[`conv-${day}-${index}`] = { name: `对话标题 ${day}-${index}`, cost: 0.01, tokens: 1234 };
  }
  busy[day] = { cost: day + 0.5, conversations };
}
let calls = 0;
global.Api = { daily: {
  calendar: () => {
    calls += 1;  // the first open answers; after the reload the server is slow
    return calls === 1 ? Promise.resolve({ days: {}, conv_days: { 3: 4 }, cost_days: busy })
      : new Promise(() => {});
  },
} };
const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
(async () => {
  const now = new Date();
  eval(fs.readFileSync(process.argv[3], 'utf8'));  // the panel as first opened
  await _mydayFetchMonthOverview(now.getFullYear(), now.getMonth());
  await tick(20);
  const stored = [...months.values()].map((record) => record.value);
  els.mydayCalGrid._html = '';
  eval(fs.readFileSync(process.argv[3], 'utf8'));  // a reload: fresh panel state
  _mydayFetchMonthOverview(now.getFullYear(), now.getMonth());
  await tick(50);
  console.log(JSON.stringify({
    calls,
    cachedMonths: stored.length,
    storedBytes: stored.length ? Buffer.byteLength(JSON.stringify(stored[0])) : null,
    limit: MYDAY_CACHE_RESOURCE_BUDGET.maximumMonthBytes,
    paintedCost: els.mydayCalGrid._html.includes('¥3.50'),
    paintedConversations: /mcal-dot unknown/.test(els.mydayCalGrid._html),
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def test_a_busy_month_still_paints_the_calendar_from_the_cache(tmp_path):
    from tests._jsdom import require_node_modules
    require_node_modules()
    harness = tmp_path / 'month_cache_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    cache_bundle = native_module_path('.native/myday-report-cache.js', CACHE_OWNER)
    proc = subprocess.run(
        ['node', str(harness), cache_bundle, os.path.join(runtime_sections_dir(), 'myday.js')],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result['calls'] == 2, result
    assert result['cachedMonths'] == 1, f'the busy month was not cached: {result}'
    assert result['storedBytes'] <= result['limit'], result
    assert result['paintedCost'] and result['paintedConversations'], result
