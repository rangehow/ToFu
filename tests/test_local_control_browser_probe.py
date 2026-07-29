"""tests/test_local_control_browser_probe.py — the extension-install guidance
must be driven by a real browser PROBE, not by ``remote_addr``.

WHAT THIS GUARDS
----------------
The "帮我打开扩展管理页" button was a dead button, and the three failures that
made it dead compounded into a single silent 404 (observed live: three
``POST /api/v1/browser/open-extensions → 404`` in ``logs/app.log``, each
logging "no Chrome-family browser found on this machine"):

  1. **Wrong machine.** ``subprocess.Popen`` opens a window on the SERVER.
     That only helps a user who is physically at the server. The gate for
     "is the user at the server" was ``_remote_is_loopback()`` — a pure IP
     test that, under the standard same-host reverse-proxy / tunnel
     deployment, reports ``127.0.0.1`` for *every public request* (the
     ProxyFix seam is not wired; see docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md
     §3.2b, which calls this out as a defect written up as a feature). So
     the gate said "you are local" to a user who was three network hops
     away, and the button rendered for them.
  2. **No browser to open.** A headless server has no Chrome, so even a
     genuinely-local user got a 404 from a button that had already promised
     to act.
  3. **Chrome-only probe.** Edge is Chromium-family and runs this very
     extension unchanged, but the probe never looked for it.

THE INVARIANT PINNED HERE
-------------------------
**A control that cannot achieve what it claims must not invite the click.**
``static/js/local-control.js`` states this rule in its own header and
enforces it for the two capability switches; this route's button was the one
place that broke it. So the fix is not a nicer failure message — the button
must NOT EXIST when the probe cannot find a browser to drive.

And the probe is the RIGHT criterion, strictly stronger than the IP test: if
this machine has no browser at all, then nobody is looking at this UI *from*
this machine, which also makes the server-side ``extensionPath`` useless.
Both therefore hang off the probe.

DISCIPLINE
----------
These are BEHAVIOUR guards. They assert what a caller observes — the JSON a
real request returns, the DOM a real render produces — never that some
private helper exists or that a line of source looks a certain way. The
frontend halves splice the renderer out of the SHIPPED file and run it under
jsdom, so the guard cannot drift into asserting a world production left
behind.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
LC_JS = ROOT / "static" / "js" / "local-control.js"
STYLES = ROOT / "static" / "styles.css"

_ROUTE_TOKEN = '_lc_probe_test_token__'


# ══════════════════════════════════════════════════════════
#  Backend: the probe is the single source of truth
# ══════════════════════════════════════════════════════════

def _get_status(flask_client, monkeypatch, client_addr, *, probe):
    """Drive one real GET /api/v1/browser/status with a chosen peer + probe.

    ``scope_base={'client': ...}`` sets the ASGI socket peer the way
    Hypercorn does, so the REAL loopback predicate runs against it — the
    mechanism tests/test_proxy_trust.py established. Quart's test client
    otherwise reports ``'<local>'``, which ``auth.py`` treats as loopback and
    which would make every assertion below pass for the wrong reason.
    """
    from routes.api_v1 import browser as browser_routes

    monkeypatch.setattr(browser_routes, '_detect_local_browser',
                        lambda: probe)
    monkeypatch.setenv('TUNNEL_TOKEN', _ROUTE_TOKEN)
    resp = flask_client.get(
        '/api/v1/browser/status',
        headers={'X-Tunnel-Token': _ROUTE_TOKEN},
        scope_base={'client': client_addr})
    return resp.status_code, (resp.get_json(silent=True) or {})


_CHROME = {'binary': '/fake/chrome', 'family': 'chrome',
           'name': 'Chrome', 'extensionsUrl': 'chrome://extensions'}
_EDGE = {'binary': '/fake/msedge', 'family': 'edge',
         'name': 'Edge', 'extensionsUrl': 'edge://extensions'}


def test_status_reports_no_local_browser_when_probe_finds_none(flask_client,
                                                              monkeypatch):
    """Headless server + loopback peer ⇒ the payload must say "no browser".

    This is the live-observed configuration. The frontend needs a POSITIVE
    signal to decide whether to render the button at all; without this field
    it can only find out by making the user click and collect a 404.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=None)
    assert status == 200, f"status endpoint broke: {status} {body}"
    assert 'localBrowser' in body, (
        "the payload carries no probe result, so the frontend cannot know "
        "whether opening a browser here is even possible — that is exactly "
        "how the dead button shipped")
    assert not body['localBrowser'], (
        f"probe found nothing, yet the payload advertises a browser: "
        f"{body['localBrowser']!r}")


def test_status_names_the_detected_browser(flask_client, monkeypatch):
    """A detected browser is reported BY NAME, so the UI can stop saying
    "Chrome" when it is really driving Edge."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_EDGE)
    assert status == 200
    lb = body.get('localBrowser') or {}
    assert lb.get('name') == 'Edge', (
        f"expected the probe's browser name to reach the client, got {lb!r}")
    assert 'chrome' not in json.dumps(lb).lower() or lb.get('family') == 'edge', (
        f"Edge must not be reported as Chrome: {lb!r}")


def test_status_never_leaks_the_binary_path(flask_client, monkeypatch):
    """The absolute path of a local executable is server filesystem detail
    the browser has no use for. Name and family are what the UI renders."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_CHROME)
    assert status == 200
    assert '/fake/chrome' not in json.dumps(body), (
        "the probe's binary path reached the client; expose only what the "
        "UI renders (name/family/extensionsUrl)")


def test_extension_path_is_withheld_when_no_browser_exists(flask_client,
                                                           monkeypatch):
    """No browser on this machine ⇒ nobody is viewing this UI from it ⇒ the
    server-side unpacked-extension path is useless and must be withheld.

    This is the ROOT fix for "wrong machine". The old gate was the IP test
    alone, which a same-host reverse proxy makes vacuously true for public
    traffic; the probe is a fact about the machine that a proxy cannot forge.
    Withholding the path is what makes the UI fall through to the
    download-the-ZIP instruction, which is the only actionable path for a
    user who is genuinely elsewhere.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=None)
    assert status == 200
    assert not body.get('extensionPath'), (
        f"a headless server still advertised its own on-disk extension "
        f"folder ({body.get('extensionPath')!r}) as if the user's browser "
        f"could load it")


def test_extension_path_still_served_to_a_real_local_user(flask_client,
                                                          monkeypatch):
    """The complement: loopback peer AND a real browser ⇒ the path IS served.

    Without this, "withhold when no browser" could be satisfied by
    withholding always, killing the same-machine install path entirely.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_CHROME)
    assert status == 200
    assert body.get('extensionPath'), (
        "a genuinely local user with a real browser lost the on-disk "
        "install path")


def test_remote_peer_gets_no_extension_path_even_with_a_browser(flask_client,
                                                                monkeypatch):
    """Both conditions are required. A remote peer must not be handed a
    server-side path even when the server does happen to have a browser."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('203.0.113.7', 5555), probe=_CHROME)
    assert status == 200
    assert not body.get('extensionPath'), (
        "a remote peer was handed the server's own folder path")


# ══════════════════════════════════════════════════════════
#  Backend: Edge is a first-class target, with ITS url
# ══════════════════════════════════════════════════════════

def test_probe_finds_edge_when_only_edge_is_installed(monkeypatch):
    """Edge is Chromium-family and runs this extension unchanged, so a
    machine with only Edge must not report "no browser".

    Driven through the real probe with a fake PATH resolver, so the platform
    branch actually executes rather than being asserted about.
    """
    import sys as _sys

    from routes.api_v1 import browser as browser_routes

    if _sys.platform not in ('linux', 'linux2'):
        pytest.skip('POSIX name-resolution branch')

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'edge' in n else None)
    got = browser_routes._detect_local_browser()
    assert got, ("only Edge installed and the probe reported nothing — Edge "
                 "runs this extension unchanged")
    assert got['family'] == 'edge', f"misidentified Edge: {got!r}"


def test_edge_gets_its_own_extensions_url(monkeypatch):
    """``chrome://extensions`` is not Edge's extensions page. Handing a
    browser another vendor's internal URL is the same class of bug as the
    dead button: an action that looks taken and lands nowhere useful."""
    import sys as _sys

    from routes.api_v1 import browser as browser_routes

    if _sys.platform not in ('linux', 'linux2'):
        pytest.skip('POSIX name-resolution branch')

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'edge' in n else None)
    edge = browser_routes._detect_local_browser()
    assert edge['extensionsUrl'].startswith('edge://'), (
        f"Edge was given {edge['extensionsUrl']!r}")

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'chrom' in n else None)
    chrome = browser_routes._detect_local_browser()
    assert chrome['extensionsUrl'].startswith('chrome://'), (
        f"Chrome was given {chrome['extensionsUrl']!r}")


def test_open_extensions_launches_the_probes_own_url(flask_client,
                                                     monkeypatch):
    """The route must spawn the URL the PROBE chose — not a hardcoded one.

    Two hardcoded copies of "which page is the extensions page" is how the
    Edge case silently regresses to chrome://.
    """
    import subprocess as _sp

    from routes.api_v1 import browser as browser_routes

    calls = []
    monkeypatch.setattr(_sp, 'Popen',
                        lambda argv, **kw: calls.append(argv) or object())
    monkeypatch.setattr(browser_routes, '_detect_local_browser',
                        lambda: _EDGE)
    monkeypatch.setenv('TUNNEL_TOKEN', _ROUTE_TOKEN)
    resp = flask_client.post(
        '/api/v1/browser/open-extensions',
        headers={'X-Tunnel-Token': _ROUTE_TOKEN},
        scope_base={'client': ('127.0.0.1', 5555)})
    assert resp.status_code == 200, (
        f"Edge launch failed: {resp.status_code} {resp.get_json(silent=True)}")
    assert len(calls) == 1, f"expected one spawn, got {calls}"
    assert calls[0][0] == '/fake/msedge', f"wrong binary: {calls[0]!r}"
    assert 'edge://extensions' in calls[0], (
        f"the route ignored the probe's URL and launched {calls[0]!r}")


# ══════════════════════════════════════════════════════════
#  Frontend: no probe ⇒ no button (jsdom, shipped source)
# ══════════════════════════════════════════════════════════

def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


_SETUP_HTML = (
    '<div id="lcBrowserStatus"><span class="browser-status-dot"></span>'
    '<span class="lc-status-text"></span></div>'
    '<div id="lcBrowserAbout"></div>'
    '<div id="lcBrowserSetup"></div>'
    '<button id="lcBrowserSwitch"><span class="lc-switch-knob"></span></button>'
    '<div id="localControlBadge"></div><div id="localControlToggle"></div>'
)

_HARNESS = textwrap.dedent(r"""
(async () => {
  const { JSDOM } = require('jsdom');
  const fs = require('fs');
  const dom = new JSDOM('<!doctype html><body>' + %(html)s + '</body>');
  global.window = dom.window; global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.browserEnabled = false; global.desktopEnabled = false;
  global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  global.t = (k) => k;                 // fall back to the inline defaults
  global._safeClipboardWrite = () => Promise.resolve();
  global.Api = { browser: { openExtensions: () => Promise.resolve(null) } };

  // Splice the SHIPPED renderer in, so the guard tracks production.
  const src = fs.readFileSync(%(lc)s, 'utf8');
  eval(src.replace(/\bif \(typeof window !== 'undefined'\) \{[\s\S]*$/, ''));

  const out = {};
  for (const [name, payload] of Object.entries(%(cases)s)) {
    document.getElementById('lcBrowserSetup').innerHTML = '';
    _lcRenderBrowser(payload);
    const el = document.getElementById('lcBrowserSetup');
    out[name] = {
      hasOpenBtn: !!el.querySelector('#lcExtOpenBtn'),
      hasDownloadBtn: !!el.querySelector('#lcExtDownloadBtn'),
      hasPath: el.textContent.includes('/srv/tofu/browser_extension'),
      text: el.textContent.trim(),
      state: _lcBrowserSetupState(payload),
    };
  }
  console.log(JSON.stringify(out));
})();
""")


def _render_cases(cases: dict) -> dict:
    if not _has_jsdom():
        pytest.skip("jsdom not installed")
    script = _HARNESS % {
        'html': json.dumps(_SETUP_HTML),
        'lc': json.dumps(str(LC_JS)),
        'cases': json.dumps(cases),
    }
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, (
        f"harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_LOCAL = {'connected': False,
          'extensionPath': '/srv/tofu/browser_extension',
          'localBrowser': {'family': 'edge', 'name': 'Edge',
                           'extensionsUrl': 'edge://extensions'}}
_NO_BROWSER = {'connected': False,
               'extensionPath': '/srv/tofu/browser_extension',
               'localBrowser': None}
_REMOTE = {'connected': False, 'localBrowser': None}


def test_button_absent_when_no_browser_can_be_driven():
    """THE headline invariant: probe found nothing ⇒ the button does not exist.

    A rendered-but-doomed button is what produced the live 404s. Note the
    payload here deliberately still carries ``extensionPath`` — the guard
    must fail if the frontend keys the button off the path alone, which is
    precisely what it used to do.
    """
    out = _render_cases({'no_browser': _NO_BROWSER})['no_browser']
    assert not out['hasOpenBtn'], (
        "the open-extensions button rendered on a machine with no browser to "
        "open — clicking it can only ever produce a 404. A control that "
        "cannot achieve what it claims must not invite the click.")


def test_a_real_local_user_still_gets_the_button():
    """The complement — otherwise "never render it" would pass test 1 while
    deleting the feature."""
    out = _render_cases({'local': _LOCAL})['local']
    assert out['hasOpenBtn'], (
        "a local user with a real browser lost the one-click path")
    assert out['hasPath'], "the folder path to paste went missing"


def test_no_browser_falls_through_to_an_actionable_instruction():
    """Removing the button must leave something the user CAN do, not a void.

    Whatever the surface offers here, it has to be actionable from where the
    user actually is: the ZIP download.
    """
    out = _render_cases({'no_browser': _NO_BROWSER})['no_browser']
    assert out['text'], (
        "removing the dead button left an EMPTY panel — the user is now told "
        "nothing at all, which is worse than a wrong instruction")
    assert out['hasDownloadBtn'], (
        f"expected the download-the-ZIP path to take over, got: "
        f"{out['text'][:200]!r}")


def test_the_instruction_names_the_detected_browser():
    """With Edge detected, the guidance must not order the user into Chrome."""
    out = _render_cases({'local': _LOCAL})['local']
    assert 'Edge' in out['text'], (
        f"Edge was detected but the instruction never names it: "
        f"{out['text'][:300]!r}")


# ══════════════════════════════════════════════════════════
#  CSS: .lc-substep must carry its own typography
# ══════════════════════════════════════════════════════════

def _decl(selector: str) -> dict:
    """Effective declarations for ``selector``, merged across ALL rules.

    A single-rule lookup would be asserting the SHAPE of the stylesheet
    ("this property must appear in this literal rule"), which is exactly the
    kind of implementation-coupled guard that goes red when someone factors
    two rules into a grouped selector without changing a single rendered
    pixel. What matters is the value that actually lands, so this merges every
    rule whose selector list contains ``selector`` in document order.

    All the selectors involved here are single classes (specificity 0,1,0), so
    document order alone decides the winner — no specificity model needed.
    """
    css = STYLES.read_text(encoding='utf-8')
    # Strip comments FIRST. Without this, the selector capture below swallows
    # any preceding /* ... */ block, so ".lc-step" ends up glued to the comment
    # text and no longer compares equal to a bare selector part.
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    out: dict[str, str] = {}
    hits = 0
    for sel_list, body in re.findall(r'([^{}]+)\{([^}]*)\}', css):
        parts = [s.strip() for s in sel_list.split(',')]
        if selector not in parts:
            continue
        hits += 1
        for decl in body.split(';'):
            if ':' in decl:
                k, v = decl.split(':', 1)
                out[k.strip()] = v.strip()
    assert hits, f"selector {selector} not found in styles.css"
    return out


def test_lc_substep_carries_its_own_typography():
    """``.lc-substep`` declared ONLY ``margin``, so a bare-text node inside it
    inherited the modal's default size and rendered visibly larger than the
    ``.lc-step`` lines beside it.

    It looked fine everywhere else purely by accident: the other two
    ``.lc-substep`` uses wrap an ``<a class="lc-dl-link">``, and *that* class
    sets its own font-size — a child was silently covering for the parent.
    """
    sub = _decl('.lc-substep')
    missing = [p for p in ('font-size', 'color', 'line-height')
               if p not in sub]
    assert not missing, (
        f".lc-substep is missing {missing} — a bare text node inside it "
        f"renders at the modal's default size instead of matching the "
        f".lc-step lines beside it. Declared: {sub}")


def test_substep_and_step_share_one_font_size():
    """Both are secondary instruction text in the same panel; two different
    literals here is the drift that let this diverge unnoticed."""
    assert _decl('.lc-substep')['font-size'] == _decl('.lc-step')['font-size'], (
        "sibling instruction lines disagree on font-size")


def test_the_dl_link_form_is_not_regressed():
    """The two ``.lc-dl-link`` uses must still look right after the parent
    gains a size — this is the form that was accidentally already correct,
    so it is the one a fix could quietly break."""
    assert 'font-size' in _decl('.lc-dl-link'), (
        ".lc-dl-link lost its own font-size; it is nested in .lc-substep and "
        "both forms must render correctly")
