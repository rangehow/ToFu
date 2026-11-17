"""tests/server/test_platform_download.py — the desktop download must name
the file for the user's platform, and the Settings desktop card must render it.

WHAT THIS GUARDS
----------------
The download link once made the user guess: ``_desktop_download_url``
returned only ``…/releases/latest`` — a page carrying five assets (two DMGs,
an .exe, a .tar.gz, SHA256SUMS). The asset names must be derived from the ONE
list in ``scripts/release_assets.py``; a hand-typed copy in a route would
drift the moment a platform is added.

THE macOS TRAP
--------------
Apple Silicon and Intel Macs are **indistinguishable from the UA string** —
Chrome and Safari both report ``Intel Mac OS X`` on an M-series machine.
Architecture is only knowable from ``Sec-CH-UA-Arch``, a high-entropy client
hint that Chromium sends only after an ``Accept-CH`` opt-in and that Safari
never sends. So when the arch is unknown the server MUST offer both DMGs
rather than pick one: guessing wrong hands the user a download that cannot
open, which is strictly worse than asking them to choose.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent.parent
ASSET_SCRIPT = ROOT / "scripts" / "release_assets.py"
CLIENT_DOWNLOADS_TS = ROOT / "frontend" / "src" / "features" / "settings" / "client-downloads.ts"

from tests._runtime_sections import native_module_path



def _node() -> str:
    exe = shutil.which("node")
    require_node_modules('jsdom', 'esbuild')
    return exe


# ══════════════════════════════════════════════════════════════════
#  1. The download must name the file for THIS machine
# ══════════════════════════════════════════════════════════════════

def _asset_mod():
    spec = importlib.util.spec_from_file_location(
        "_tofu_release_assets_dl", ASSET_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _platform_mod():
    from routes.api_v1 import desktop as mod
    return mod


def test_the_route_derives_asset_names_from_the_shared_list():
    """No sixth copy of the filenames.

    ``scripts/release_assets.py`` is the single source of truth the two release
    gates already share. A route that hand-typed ``Tofu-Setup-*-win64.exe``
    would keep working today and silently miss the next platform added to that
    list — and, unlike the gates, nothing would ever go red.
    """
    src = (ROOT / "routes" / "api_v1" / "desktop.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    mod = _asset_mod()
    leaked = [pat for *_rest, pat in mod.REQUIRED_PLATFORM_ASSETS if pat in code]
    assert not leaked, (
        f"asset globs hardcoded in the route: {leaked}. Import them from "
        f"scripts/release_assets.py instead — that list is what the release "
        f"gates already agree on."
    )
    assert "release_assets" in code, (
        "the route must consult scripts/release_assets.py for the asset names")


def test_the_shared_list_carries_a_machine_readable_platform_key():
    """A human label like 'macOS arm64 DMG' cannot be matched against a UA.

    Parsing the label (or the glob) with a regex would put a second, implicit
    copy of the platform mapping in the route. The list itself must say which
    OS and architecture each asset is for.
    """
    mod = _asset_mod()
    seen = set()
    for entry in mod.PLATFORM_ASSETS:
        # The arity is pinned because consumers unpack these rows POSITIONALLY
        # (routes/api_v1/desktop.py, scripts/release_assets.py and the helpers
        # in this file), so widening a row silently breaks them — the route's
        # table load is wrapped in `except Exception` and degrades to the
        # releases page, i.e. the symptom is "direct download links quietly
        # disappeared", not a traceback. min_bytes joined the row when the
        # release gate learned to reject a correctly-named but hollow
        # installer; see tests/release/test_release_asset_size_floor.py.
        assert len(entry) == 5, (
            f"expected (os, arch, label, glob, min_bytes) entries, got {entry!r}")
        os_key, arch, _label, _glob, _min_bytes = entry
        assert os_key in {"windows", "macos", "linux"}, os_key
        seen.add((os_key, arch))
    assert ("macos", "arm64") in seen and ("macos", "x86_64") in seen, seen
    assert ("windows", "x86_64") in seen and ("linux", "x86_64") in seen, seen
    # The release gates consume the 2-tuple shape. It must be DERIVED from the
    # list above, never maintained beside it — two hand-kept lists is the exact
    # drift scripts/release_assets.py exists to prevent.
    assert mod.REQUIRED_PLATFORM_ASSETS == tuple(
        (label, glob) for _o, _a, label, glob, _min in
        mod.PLATFORM_ASSETS) + (
                *tuple((f'Python wheels {platform}/{machine}{suffix}',
                        f'python-wheels-*-cpython-312-{platform}-{machine}.tar.gz{suffix}')
                       for platform, machine in mod.PYTHON_BUNDLE_TARGETS
                       for suffix in ('', '.sha256')),
                *tuple((f'Prebuilt TofuDB {target}{suffix}', f'tofu-db-*-{target}.tar.gz{suffix}')
                       for target in ('x86_64-unknown-linux-musl', 'aarch64-unknown-linux-musl',
                                      'x86_64-apple-darwin', 'aarch64-apple-darwin')
                       for suffix in ('', '.sha256')),
                ('prebuilt frontend graph', 'frontend-dist-*.tar.gz'),
                ('prebuilt frontend SHA-256', 'frontend-dist-*.tar.gz.sha256'),
            ), (
        "REQUIRED_PLATFORM_ASSETS is not derived from "
        "PLATFORM_ASSETS")


# A realistic published-asset list, DERIVED from the shared globs rather than
# hand-typed — a hand-typed copy here would be the very drift the single-source
# rule exists to prevent, and would keep passing after a platform was added.
def _published():
    """The release's assets in the shape the API returns them.

    Each record carries its OWN pinned-tag URL next to the name. Deriving both
    from one version string is deliberate: that IS the one-snapshot property
    the production parser has to preserve, so a fixture allowing them to differ
    would not exercise the real contract.
    """
    mod = _asset_mod()
    out = []
    for _o, _a, _l, pat, _min in mod.PLATFORM_ASSETS:
        name = pat.replace('*', _FIXTURE_VER)
        out.append({
            'name': name,
            'url': ('https://github.com/rangehow/ToFu/releases/download/'
                    f'v{_FIXTURE_VER}/{name}'),
        })
    return out


_FIXTURE_VER = "0.15.2"


@pytest.mark.parametrize("ua,expect_os", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", "windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605", "macos"),
    ("Mozilla/5.0 (X11; Linux x86_64) Chrome/120", "linux"),
])
def test_the_users_platform_is_detected_from_the_request(ua, expect_os):
    """The user must not be handed a five-asset page to choose from."""
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="", published=_published())
    assert picks, f"no asset matched for {ua!r}"
    assert all(p["os"] == expect_os for p in picks), picks


def test_an_unknown_arch_on_macos_offers_BOTH_dmgs():
    """THE TRAP. Apple Silicon reports ``Intel Mac OS X`` in its UA.

    Both Chrome and Safari do this on an M-series machine, so the UA can never
    tell the two DMGs apart. Picking one would hand roughly half of Mac users
    a download that will not open — strictly worse than showing two clearly
    labelled choices.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
        arch_hint="", published=_published())
    arches = sorted(p["arch"] for p in picks)
    assert arches == ["arm64", "x86_64"], (
        f"macOS with an unknown architecture must offer BOTH DMGs, got "
        f"{arches}. The UA string says 'Intel' on Apple Silicon too, so a "
        f"single pick is a coin flip that breaks the download."
    )


@pytest.mark.parametrize("hint,expect", [
    ('"arm"', "arm64"),
    ('"x86"', "x86_64"),
])
def test_a_client_hint_narrows_macos_to_one_dmg(hint, expect):
    """``Sec-CH-UA-Arch`` is the ONLY honest source of the architecture.

    When Chromium does supply it, the ambiguity is gone and the user should
    get exactly one file — otherwise the hint buys nothing.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120",
        arch_hint=hint, published=_published())
    assert [p["arch"] for p in picks] == [expect], picks


def test_an_unrecognised_platform_falls_back_to_the_releases_page():
    """A BSD / mobile / bot UA must not be guessed at.

    Returning no direct link is honest; the releases page still works. Guessing
    would send a phone user a Windows installer.
    """
    mod = _platform_mod()
    assert mod._match_platform_assets("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)",
                                      arch_hint="", published=_published()) == []
    assert mod._match_platform_assets("", arch_hint="", published=_published()) == []


def test_direct_urls_point_at_a_real_asset_not_the_listing_page():
    """The whole point: a URL that downloads a file, not one that shows five.

    ``filename`` must be the RESOLVED asset name, never the glob it was matched
    with. This assertion exists because a NEUTER that fed the glob straight
    into the URL slipped past an earlier version of this test: the glob also
    ends in ``.exe``, so an extension check alone cannot tell
    ``Tofu-Setup-0.15.2-win64.exe`` from ``Tofu-Setup-<star>-win64.exe`` — and
    the second one is a guaranteed 404, because GitHub's
    ``/releases/latest/download/<name>`` resolves the RELEASE but not the
    FILENAME.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", arch_hint="", published=_published())
    assert picks, (
        'no asset matched a Windows visitor against an injected, COMPLETE '
        'release asset list — that is a broken matcher, not an unreachable '
        'network. (This was a pytest.skip while the test still probed GitHub; '
        'leaving it would let the whole assertion vacuum out on any host '
        'without egress.)')
    assert len(picks) == 1, picks
    url = picks[0]["url"]
    name = picks[0]["filename"]
    assert "/releases/" in url and url.endswith(".exe"), url
    assert not url.endswith("/releases/latest"), (
        "this is the listing page again — the user still has to choose")
    for meta in "*?[":
        assert meta not in name, (
            f"filename {name!r} still carries the glob metacharacter {meta!r} — "
            f"it is the PATTERN, not a resolved asset name, so the download URL "
            f"is a guaranteed 404.")
        assert meta not in url, f"url {url!r} carries glob metacharacter {meta!r}"


# ══════════════════════════════════════════════════════════════════
#  2. The frontend must actually CONSUME the platform links
# ══════════════════════════════════════════════════════════════════
#
# A backend that resolves the right installer is worth nothing if no rendered
# element links to it. This is the "my Python guards were all green while the
# fix was unreachable in the product" failure mode: the only way to rule it out
# is a guard that drives the SHIPPED renderer end-to-end.

SETTINGS_CORE_JS = Path(native_module_path(
    '.native/settings-core-panel.js',
    ROOT / 'frontend/src/features/settings/core-panel.ts'))
GENERAL_HTML = ROOT / 'static' / 'settings_panels' / 'general.html'


def _run_settings_desktop_card(payload_js: str, language: str | None = None,
                               uad_js: str = 'null') -> dict:
    """Drive the Settings → About&Update full-desktop card end-to-end.

    The full Windows/macOS installers are a download, not a control, and
    live in this card — so the direct-link guards drive
    the shipped `_stgDownloadLinks` renderer exactly as it runs in product.
    `uad_js` stubs `navigator.userAgentData` (absent by default, as in
    Safari/jsdom) so the arch-recommendation branch is drivable.
    """
    require_node_modules('jsdom')
    locale = ("require(" + json.dumps(str(
        ROOT / 'frontend/src/i18n/locales' / f'{language}.json')) + ")"
        if language else '{}')
    src = textwrap.dedent("""
        const {{ JSDOM }} = require('jsdom');
        const dom = new JSDOM('<!DOCTYPE html><body>' +
          '<div class="settings-group" id="settingsDesktopCard" style="display:none">' +
          '<div id="settingsDesktopRows"></div></div></body>');
        global.document = dom.window.document;
        global.window = dom.window;
        global.navigator = dom.window.navigator;
        const uad = {uad};
        if (uad) Object.defineProperty(global.navigator, 'userAgentData',
          {{ value: uad, configurable: true }});
        const locale = {locale};
        global.t = (k) => locale[k] || k;
        window.t = global.t;
        global.escapeHtml = (s) => String(s == null ? '' : s)
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;');
        const payload = {payload};
        // featureRegistry is a Proxy that falls through to window[name]; the
        // jsdom window is distinct from Node's global, so seed Api on window.
        window.Api = {{ desktop: {{ status: () => Promise.resolve(payload) }} }};

        {shipped}

        // The typed owner self-bridges onto featureRegistry (copied onto
        // globalThis by the native-module adapter). Invoke the shipped
        // renderer through that bridge exactly as production does.
        featureRegistry._renderSettingsDesktopCard();
        setTimeout(() => {{
          const card = document.getElementById('settingsDesktopCard');
          const box = document.getElementById('settingsDesktopRows');
          const links = Array.from(box.querySelectorAll('a')).map(a => ({{
            href: a.getAttribute('href'), text: a.textContent, cls: a.className }}));
          console.log(JSON.stringify({{ html: box.innerHTML, text: box.textContent,
            links, shown: card.style.display !== 'none' }}));
        }}, 10);
    """).format(shipped=SETTINGS_CORE_JS.read_text(encoding="utf-8"),
                payload=payload_js, locale=locale, uad=uad_js)
    # The compiled typed owner is far larger than the retired classic section,
    # so inline `node -e` now exceeds ARG_MAX. Write the script to a temp file.
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=ROOT / 'tests', delete=False, encoding='utf-8'
    ) as fh:
        fh.write(src)
        script_path = fh.name
    try:
        proc = subprocess.run([_node(), script_path], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(script_path)
    assert proc.returncode == 0, (
        f"settings harness exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_settings_desktop_card_renders_a_direct_link_per_installer():
    """THE REACHABILITY GUARD — full installers now live in Settings.

    Without this, every backend test above can pass while the settings card
    still shows only the five-asset releases page — the fix would be correct
    and invisible.
    """
    out = _run_settings_desktop_card(
        "{download_url:'https://example.invalid/releases/latest',"
        " downloads:[{os:'windows',arch:'x86_64',label:'Windows installer',"
        " filename:'Tofu-Setup-0.15.2-win64.exe',"
        " url:'https://example.invalid/releases/latest/download/Tofu-Setup-0.15.2-win64.exe'}]}")
    assert out["shown"], "the desktop card stayed hidden despite a valid payload"
    direct = [l for l in out["links"] if "stg-mobile-row" in (l["cls"] or "")]
    assert direct, (
        f"no direct-download link rendered; the card still only offers the "
        f"releases page. links={out['links']!r}")
    assert direct[0]["href"].endswith("Tofu-Setup-0.15.2-win64.exe"), direct


@pytest.mark.parametrize("older,refreshing,phrase", [
    (False, False, None),
    (True, True, "The server is fetching the newer one"),
    (True, False, "has not fetched the newer one yet"),
])
def test_the_settings_desktop_card_names_versions_against_the_server(
        older, refreshing, phrase):
    """The row shows the installer version,
    and one older than the server says so instead of passing as current."""
    version = "0.18.0" if older else "0.19.2"
    out = _run_settings_desktop_card(json.dumps({
        "download_url": "https://example.invalid/releases/latest",
        "server_version": "0.19.2", "installers_refreshing": refreshing,
        "downloads": [{
            "os": "windows", "arch": "x86_64", "label": "Windows installer",
            "filename": f"Tofu-Setup-{version}-win64.exe",
            "version": version, "older_than_server": older, "size": 136507710,
            "url": f"https://example.invalid/d/Tofu-Setup-{version}-win64.exe"}],
    }), language="en")
    assert f"v{version} · 130 MB" in out["text"], out["text"]
    if phrase is None:
        assert "stg-desktop-older" not in out["html"], out["html"]
    else:
        assert "stg-desktop-older" in out["html"], out["html"]
        assert phrase in out["text"] and "v0.19.2" in out["text"], out["text"]


def test_both_macos_dmgs_render_cleanly_when_arch_is_unknown():
    """The ambiguous case: two labelled rows, NO wall of text, NO fake badge.

    When the browser cannot say which chip this Mac has (Safari, jsdom — no
    `userAgentData`), each row's label already names its arch, so the card
    must stay quiet: the old explanatory paragraph was noise, and a
    recommendation badge without evidence would be a guess dressed up as a
    fact.
    """
    out = _run_settings_desktop_card(
        "{download_url:'https://example.invalid/releases/latest',"
        " downloads:[{os:'macos',arch:'arm64',label:'macOS arm64 DMG',"
        " filename:'Tofu-0.15.2-macos-arm64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-arm64.dmg'},"
        " {os:'macos',arch:'x86_64',label:'macOS x86_64 DMG',"
        " filename:'Tofu-0.15.2-macos-x86_64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-x86_64.dmg'}]}",
        'zh')
    direct = [l for l in out["links"] if "stg-mobile-row" in (l["cls"] or "")]
    assert len(direct) == 2, f"expected both DMGs, got {direct!r}"
    assert any("arm64" in (l["text"] or "") for l in direct), direct
    assert any("x86_64" in (l["text"] or "") for l in direct), direct
    assert "About This Mac" not in out["html"] and "关于本机" not in out["html"], (
        f"the explanatory paragraph is back — the row labels already carry "
        f"the arch, the paragraph is pure noise. html={out['html']!r}")
    assert "stg-desktop-rec" not in out["html"], (
        f"a recommendation badge rendered with NO arch evidence — that is a "
        f"guess presented as a fact. html={out['html']!r}")


@pytest.mark.parametrize("language,word", [('zh', '推荐'), ('en', 'Recommended')])
def test_the_detected_arch_gets_a_subtle_recommendation(language, word):
    """When the browser CAN say which chip this is, mark that row — subtly.

    `getHighEntropyValues` is the only honest arch source on macOS; when it
    answers, the matching row gets a quiet badge and the other row stays
    plain (the choice remains visible — the user may be fetching an
    installer for a DIFFERENT machine).
    """
    out = _run_settings_desktop_card(
        "{download_url:'https://example.invalid/releases/latest',"
        " downloads:[{os:'macos',arch:'arm64',label:'macOS arm64 DMG',"
        " filename:'Tofu-0.15.2-macos-arm64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-arm64.dmg'},"
        " {os:'macos',arch:'x86_64',label:'macOS x86_64 DMG',"
        " filename:'Tofu-0.15.2-macos-x86_64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-x86_64.dmg'}]}",
        language,
        uad_js="{ getHighEntropyValues: () => Promise.resolve({architecture:'arm'}) }")
    direct = [l for l in out["links"] if "stg-mobile-row" in (l["cls"] or "")]
    assert len(direct) == 2, f"expected both DMGs, got {direct!r}"
    arm = next(l for l in direct if "arm64" in (l["text"] or ""))
    intel = next(l for l in direct if "x86_64" in (l["text"] or ""))
    assert word in arm["text"], (
        f"the arm64 row lost its recommendation badge ({word!r}): {arm!r}")
    assert word not in intel["text"], (
        f"the badge leaked onto the WRONG row: {intel!r}")
    assert out["html"].count("stg-desktop-rec") == 1, (
        f"expected exactly ONE recommendation badge: {out['html']!r}")


def test_the_releases_page_link_survives_as_a_fallback():
    """An unrecognised platform (empty `downloads`) must still get somewhere.

    This is the path for BSD, mobile, a release missing an asset, or an
    unreachable GitHub API — the direct links are an upgrade, never a
    replacement for the page that always works.
    """
    out = _run_settings_desktop_card(
        "{download_url:'https://example.invalid/releases/latest', downloads:[]}")
    hrefs = [l["href"] for l in out["links"]]
    assert "https://example.invalid/releases/latest" in hrefs, (
        f"the releases-page fallback disappeared; a visitor whose platform we "
        f"cannot identify now has NO way to download at all. links={hrefs!r}")


def test_the_settings_desktop_card_renders_the_linux_archive_too():
    """The Linux archive is a full-desktop asset as well — a link the
    Windows/macOS rows get and the Linux row lacks is a drift only one user
    population ever sees. One renderer serves every platform row."""
    out = _run_settings_desktop_card(
        "{download_url:'https://example.invalid/releases/latest',"
        " downloads:[{os:'linux',arch:'x86_64',label:'Linux archive',"
        " filename:'Tofu-0.15.2-linux-x86_64.tar.gz',"
        " url:'https://example.invalid/d/Tofu-0.15.2-linux-x86_64.tar.gz'}]}")
    direct = [l for l in out["links"] if "stg-mobile-row" in (l["cls"] or "")]
    assert direct, f"settings card renders no Linux direct link: {out['links']!r}"


def test_the_settings_desktop_card_stays_hidden_without_a_payload():
    """No status answer (or one without any URL) must never leave a dead
    card on the Settings surface."""
    out = _run_settings_desktop_card("null")
    assert not out["shown"], "the card rendered despite a null status payload"
    out2 = _run_settings_desktop_card("{downloads:[]}")
    assert not out2["shown"], "the card rendered with no URL and no downloads"


def test_the_client_resolves_its_own_architecture():
    """macOS cannot be narrowed any other way.

    `Sec-CH-UA-Arch` only arrives AFTER a server has answered once with an
    `Accept-CH` opt-in, so the first paint — the one carrying the download
    button — would be arch-blind. The client must ask
    `navigator.userAgentData.getHighEntropyValues` itself and pass the answer.
    """
    src = CLIENT_DOWNLOADS_TS.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith(("*", "/*", "//")))
    assert "getHighEntropyValues" in code, (
        "the Settings desktop card never asks the browser for its architecture")
    assert "desktop.status(" in code, (
        "the resolved architecture is never threaded into the status probe")
    api = (ROOT / 'frontend/src/api/endpoint-registry/desktop.ts').read_text(encoding='utf-8')
    assert re.search(r"status:\s*\(arch", api), (
        "Api.desktop.status does not accept the client-resolved architecture")


def test_every_new_download_string_is_translated():
    """A raw i18n key in the download row is a visible defect."""
    src = CLIENT_DOWNLOADS_TS.read_text(encoding="utf-8")
    keys = set(re.findall(r"stgDlT\('(settings\.[A-Za-z]+)'", src))
    assert keys, "the download row uses no translated strings at all"
    catalogs = [json.loads((ROOT / 'frontend' / 'src' / 'i18n' / 'locales'
                            / f'{language}.json').read_text(encoding='utf-8'))
                for language in ('zh', 'en')]
    for key in sorted(keys):
        assert all(key in catalog for catalog in catalogs), (
            f"{key!r} is not defined in every locale chunk")


# ══════════════════════════════════════════════════════════════════
#  3. The URL and the FILENAME must come from ONE snapshot
# ══════════════════════════════════════════════════════════════════
#
# THE DEFECT. The URL was assembled as
# ``/releases/latest/download/<cached filename>`` — two halves with DIFFERENT
# lifetimes glued together:
#
#   * the filename is cached here for 900 s;
#   * ``latest`` is resolved by GitHub at click time.
#
# They agree at the instant the cache is filled and diverge the moment a new
# release publishes: ``latest`` moves to the new tag, whose asset set does not
# contain the old filename. Measured against the real repo:
#
#   latest/download/Tofu-Setup-0.14.2-win64.exe   -> 200  (in current latest)
#   latest/download/Tofu-Setup-0.15.2-win64.exe   -> 404  (not in latest)
#   latest/download/Tofu-Setup-0.13.0-win64.exe   -> 404  (older name)
#   releases/download/v0.14.2/Tofu-Setup-...exe   -> 200  (tag pinned)
#
# The window is open RIGHT NOW: VERSION says 0.15.2 while the newest release is
# v0.14.2, so the next publish must pass through it — handing 404s to everyone
# who clicks in the following 15 minutes, i.e. exactly when a fresh release
# draws the most downloads.
#
# Shortening the TTL cannot fix this (any window > 0 has the same failure), so
# the invariant is structural: THE URL MUST BE PINNED TO THE SAME RELEASE THE
# FILENAME CAME FROM. GitHub already hands us that URL — every asset in the
# payload carries ``browser_download_url`` of the form
# ``/releases/download/<tag>/<name>`` — so the fix is to stop discarding it.

_VER_RE = re.compile(r"\d+\.\d+\.\d+")


def _versions_in(text: str) -> list[str]:
    return _VER_RE.findall(text or "")


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
])
def test_the_download_url_is_pinned_to_the_filenames_own_release(ua):
    """THE REGRESSION. A ``/releases/latest/download/`` URL is a time bomb.

    ``latest`` is resolved when the user CLICKS, but the filename was resolved
    when the server last probed. Pinning the URL to the release the filename
    actually belongs to is the only construction where the two cannot drift.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="",
                                      published=_published())
    assert picks, f"no asset matched for {ua!r}"
    for p in picks:
        url = p["url"]
        assert "/releases/latest/download/" not in url, (
            f"url {url!r} resolves 'latest' at CLICK time while its filename "
            f"was resolved at PROBE time — the moment a release publishes "
            f"inside the cache window, this 404s. Pin the URL to the release "
            f"the filename came from (the API's browser_download_url)."
        )
        assert "/releases/download/" in url, (
            f"url {url!r} is not a pinned-tag asset URL")


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
])
def test_the_url_version_equals_the_filename_version(ua):
    """The sharper form of the same invariant, and the one that survives.

    Asserting merely "not /latest/" would be satisfied by any pinned URL, even
    one pinned to the WRONG tag. What actually has to hold is that the release
    the URL points at is the release the file belongs to — so the version in
    the tag segment must equal the version in the filename.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="",
                                       published=_published())
    assert picks, f"no asset matched for {ua!r}"
    for p in picks:
        fname, url = p["filename"], p["url"]
        fv = _versions_in(fname)
        assert fv, f"filename {fname!r} carries no version to compare"
        # The tag segment is what sits between /releases/download/ and the
        # trailing filename.
        m = re.search(r"/releases/download/([^/]+)/", url)
        assert m, f"url {url!r} has no pinned tag segment"
        tag_v = _versions_in(m.group(1))
        assert tag_v, f"tag {m.group(1)!r} in {url!r} carries no version"
        assert tag_v[0] == fv[0], (
            f"the URL points at release {tag_v[0]} while the file it names is "
            f"from {fv[0]} ({fname!r} vs {url!r}). A file is only downloadable "
            f"from the release it was uploaded to, so these must be equal."
        )
        assert url.endswith(fname), (
            f"url {url!r} does not end with its own filename {fname!r}")


def test_the_asset_url_is_never_reassembled_from_latest():
    """Structural: no executable code pairs a name with ``latest``.

    Rebuilding a URL from ``latest`` is what allowed a stale name and a live
    release to be paired in the first place. The probe resolves ONE tag and
    builds every URL from it (see
    ``test_checksum_rows_pin_the_release_they_were_read_from``).

    Docstrings AND comments are stripped before scanning. The module's own
    prose NAMES the rejected construction (``/releases/latest/download/``) in
    order to explain why it is wrong, so a raw substring scan would flag the
    very documentation that prevents the regression — and could be silenced by
    deleting that explanation, which is exactly backwards.
    """
    import ast

    # The URL assembly moved with the 2026-07 extraction
    # (pt_a859c11e75d142d1) from routes/api_v1/desktop.py to
    # lib/desktop_dist/platforms.py — scan its real home, not the re-export.
    src = (ROOT / "lib" / "desktop_dist" / "platforms.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    # Collect every docstring's exact text so it can be excised.
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.append(d)
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    for d in docs:
        code = code.replace(d, "")
    assert "releases/latest/download" not in code, (
        "the 'latest/download' construction is still present in EXECUTABLE "
        "code; it pairs a probe-time filename with a click-time release")


def test_checksum_rows_pin_the_release_they_were_read_from():
    """Every URL names the tag whose ``SHA256SUMS`` listed the file — never
    ``latest``, which is resolved at click time while the name was resolved
    at probe time. Verified by driving the real parser."""
    mod = _platform_mod()
    rows = mod._assets_from_checksums(
        "ab" * 32 + "  Tofu-Setup-9.9.9-win64.exe\n", "owner/repo", "v9.9.9")
    assert [r["name"] for r in rows] == ["Tofu-Setup-9.9.9-win64.exe"]
    url = rows[0]["url"]
    assert url == ("https://github.com/owner/repo/releases/download/"
                   "v9.9.9/Tofu-Setup-9.9.9-win64.exe"), url
    assert "latest" not in url


def test_checksums_without_a_release_tag_yield_nothing():
    """Refuse to invent a URL. Without the tag the manifest came from there
    is no honest address, and the caller falls back to the releases page —
    a guessed URL would 404 silently while looking authoritative."""
    mod = _platform_mod()
    rows = mod._assets_from_checksums(
        "ab" * 32 + "  Tofu-Setup-9.9.9-win64.exe\n", "owner/repo", "")
    assert rows == [], rows
