"""The office landing is applied before any application code: index.html's pre-paint script.

An address without a route opens #/world and stamps html[data-landing="world"] (the classic chrome stays
hidden until the application names a workspace); a stored classic choice or an address that already names
a route is left alone; an unavailable store never breaks the page. The key is the one the world feature
writes (frontend/src/application-shell/landing.ts).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _landing_script() -> str:
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    idx = html.index("'tofu_landing_workspace'")
    start = html.rindex('(function(){try{', 0, idx)
    end = html.index('})();', idx) + len('})();')
    return html[start:end]


def test_prepaint_landing_uses_the_world_feature_key():
    landing = (ROOT / 'frontend/src/application-shell/landing.ts').read_text(encoding='utf-8')
    key = re.search(r"LANDING_STORAGE_KEY = '([^']+)'", landing).group(1)
    assert f"'{key}'" in _landing_script()


def test_prepaint_landing_opens_the_office_unless_classic_was_chosen():
    require_node_modules()
    harness = r"""
const { JSDOM } = require(require('path').join(process.argv[1], 'node_modules', 'jsdom'));
const SCRIPT = process.argv[2];
function run(url, stored, broken = false) {
  const dom = new JSDOM('<!DOCTYPE html><html><head></head><body></body></html>', { url, runScripts: 'outside-only' });
  if (stored !== null) dom.window.localStorage.setItem('tofu_landing_workspace', stored);
  if (broken) Object.defineProperty(dom.window, 'localStorage', { get() { throw new Error('denied'); } });
  dom.window.eval(SCRIPT);
  return { hash: dom.window.location.hash, landing: dom.window.document.documentElement.getAttribute('data-landing') };
}
const out = {
  fresh: run('http://localhost/', null),
  worldChoice: run('http://localhost/?x=1', 'world'),
  classicChoice: run('http://localhost/', 'chat'),
  explicitRoute: run('http://localhost/#/chat', null),
  deepLink: run('http://localhost/#/paper/arxiv-1', null),
  storeUnavailable: run('http://localhost/', null, true),
};
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(['node', '-e', harness, str(ROOT), _landing_script()], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out['fresh'] == {'hash': '#/world', 'landing': 'world'}
    assert out['worldChoice'] == {'hash': '#/world', 'landing': 'world'}
    assert out['classicChoice'] == {'hash': '', 'landing': None}
    assert out['explicitRoute'] == {'hash': '#/chat', 'landing': None}
    assert out['deepLink'] == {'hash': '#/paper/arxiv-1', 'landing': None}
    assert out['storeUnavailable'] == {'hash': '', 'landing': None}
