"""Frontend test — the brand-mascot skin mechanism (core/brand_logo.js).

Two invariants the owner asked for, both driven against the REAL shipped
module under jsdom (load-and-eval, matching the project's other frontend
tests):

1. DEFAULT IS THE ORIGINAL. With nothing stored — and with a stored id that
   is unknown/removed — the resolved URL MUST be the shipped
   ``tofu-welcome.svg``. A candidate can never become the default by being
   added to the registry.
2. A MISSING CANDIDATE FALLS BACK, NEVER BLANKS. If a skin's file 404s, the
   <img> onerror path (`onBrandLogoError`) MUST swap in the default URL, so
   a deleted candidate can't leave an empty logo.

Plus the cache-bust invariant that motivated the module: every resolved URL
carries the ``?v=`` token (icons ship with max-age=86400, so a bare path made
a logo change invisible for 24h — that is exactly how a rollback once looked
like it "didn't happen").

Neuters prove each assertion is load-bearing.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
BRAND_LOGO = ROOT / "static" / "js" / "core" / "brand_logo.js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run(
            [_node(), "-e", "require('jsdom')"],
            cwd=ROOT, capture_output=True, check=True,
        )
        return True
    except Exception:
        return False


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><head>
      <link rel="icon" href="/static/icons/tofu-welcome.svg">
    </head><body>
      <img id="side" data-brand-logo="1" src="/static/icons/tofu-welcome.svg">
      <img id="wel"  data-brand-logo="1" src="/static/icons/tofu-welcome.svg">
    </body>`, {{ url: 'http://localhost:15000/' }});
    global.window = dom.window;
    global.document = dom.window.document;
    global.localStorage = dom.window.localStorage;
    global.BASE_PATH = '';

    // ---- BEGIN real shipped module ----
    {source}
    // ---- END real shipped module ----

    const out = {{}};

    // 1. Nothing stored → default skin, default URL.
    out.freshSkin = window.getLogoSkin();
    out.freshUrl = window.logoUrl();

    // 2. Unknown / removed skin id → resolves back to the default.
    localStorage.setItem('tofu_logo_skin', 'a-skin-that-was-deleted');
    out.unknownSkin = window.getLogoSkin();
    out.unknownUrl = window.logoUrl();

    // 3. A real candidate can be worn, and it repoints every marked img + favicon.
    localStorage.removeItem('tofu_logo_skin');
    const skins = window.listLogoSkins();
    out.skinIds = skins.map(s => s.id);
    out.firstIsDefault = skins[0].id === 'default';
    const candidate = skins.find(s => s.id !== 'default');
    window.setLogoSkin(candidate.id);
    out.wornSkin = window.getLogoSkin();
    out.sideSrc = document.getElementById('side').getAttribute('src');
    out.welSrc = document.getElementById('wel').getAttribute('src');
    out.favHref = document.querySelector('link[rel="icon"]').getAttribute('href');

    // 4. Persistence: the choice survives a "reload" (fresh read of storage).
    out.persisted = window.getLogoSkin() === candidate.id;

    // 5. Missing candidate file → onerror falls back to the default URL.
    const broken = document.getElementById('side');
    window.onBrandLogoError(broken);
    out.afterErrorSrc = broken.getAttribute('src');
    out.defaultUrl = window.defaultLogoUrl();
    // Guard against an infinite error loop: a second call is a no-op.
    window.onBrandLogoError(broken);
    out.afterSecondErrorSrc = broken.getAttribute('src');

    // 6. Back to default explicitly.
    window.setLogoSkin('default');
    out.backToDefaultUrl = window.logoUrl();

    console.log(JSON.stringify(out));
""")


def _run(source: str) -> dict:
    script = HARNESS.format(source=source)
    proc = subprocess.run(
        [_node(), "-e", script], cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _source() -> str:
    return BRAND_LOGO.read_text(encoding="utf-8")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_default_skin_is_the_shipped_original():
    out = _run(_source())
    assert out["freshSkin"] == "default", "no stored choice must resolve to the original"
    assert "tofu-welcome.svg" in out["freshUrl"], "default URL must be the shipped mascot"
    assert out["firstIsDefault"] is True, "registry order: the original must be first"
    # An unknown id (candidate removed from the registry) must NOT blank out.
    assert out["unknownSkin"] == "default", "unknown skin id must resolve to default"
    assert "tofu-welcome.svg" in out["unknownUrl"], "unknown skin must serve the original"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_every_resolved_url_is_cache_busted():
    """The reason this module exists: icons ship with max-age=86400."""
    out = _run(_source())
    for key in ("freshUrl", "unknownUrl", "defaultUrl", "backToDefaultUrl"):
        assert "?v=" in out[key], f"{key} must carry the cache-bust token"
    assert "?v=" in out["sideSrc"], "applied skin URL must be cache-busted too"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_wearing_a_candidate_repoints_every_surface_and_persists():
    out = _run(_source())
    assert len(out["skinIds"]) >= 2, "at least one candidate must be registered"
    assert out["wornSkin"] != "default", "the candidate must actually be worn"
    for key in ("sideSrc", "welSrc", "favHref"):
        assert "candidate-a2-soft" in out[key], (
            f"{key} must repoint to the worn candidate (sidebar + welcome + favicon "
            "all switch together)"
        )
    assert out["persisted"] is True, "the choice must survive a reload"
    assert "tofu-welcome.svg" in out["backToDefaultUrl"], "switching back must restore the original"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_missing_candidate_file_falls_back_to_default_not_blank():
    out = _run(_source())
    assert out["afterErrorSrc"] == out["defaultUrl"], (
        "a candidate asset that fails to load must fall back to the shipped default"
    )
    assert out["afterSecondErrorSrc"] == out["defaultUrl"], (
        "the fallback must be idempotent (no error loop)"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_unknown_skin_not_validated_serves_a_dead_path():
    """Neuter: drop the registry lookup in getLogoSkin → a removed skin id
    resolves to a path that no longer exists (blank logo)."""
    src = _source().replace(
        "return _skinById(id) ? id : DEFAULT_SKIN;",
        "return id || DEFAULT_SKIN;",
    )
    out = _run(src)
    assert out["unknownSkin"] != "default", (
        "NEUTER must leak: without the registry lookup an unknown/removed skin id "
        "is accepted verbatim instead of falling back to the original"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_onerror_fallback_removed_leaves_broken_image():
    """Neuter: make the error handler a no-op → a missing candidate file
    leaves the broken src in place (blank logo, the exact failure the
    fallback prevents)."""
    src = _source().replace(
        "img.setAttribute('src', defaultLogoUrl());",
        "/* neutered: no fallback */",
    )
    out = _run(src)
    assert out["afterErrorSrc"] != out["defaultUrl"], (
        "NEUTER must leak: without the onerror fallback a missing candidate file "
        "leaves the logo blank instead of restoring the original"
    )
