"""RENDER-BUDGET INVARIANTS for the project-bar scene (static/js/tofu-scene.js).

The scene is an always-on animation living behind an ordinary UI control, so its
per-frame cost is a FEATURE REQUIREMENT, not a nice-to-have: if it regresses, it
regresses on every user, on every frame, forever, in the background of whatever
they were actually trying to do.

Three properties keep it cheap, and each is easy to silently undo with an
innocent-looking edit. So each is pinned here with a biting NEUTER:

  1. BATCHED DABS — dabs are queued by (colour, alpha bucket) and flushed as one
     path + one fill per bucket, with the rotation carried in the ellipse() args.
     The naive alternative (save/globalAlpha/fillStyle/translate/rotate/
     beginPath/ellipse/fill/restore per dab) is ~9 canvas calls and one
     rasterizer flush EACH. Guard: fills must be a small fraction of dabs.

  2. WIDTH PLATEAU — live populations are re-resolved every frame, so seeding
     them by AREA made the per-frame cost grow without bound as the window
     widened. They are capped (survivors widen to hold the painted area).
     Guard: per-frame canvas calls at 2400px must not materially exceed 900px.

  3. FRAME PACING + OFF-SCREEN PARK — the scene is slow ambient weather, so it
     paints on a ~30fps cadence rather than every vsync, and stops entirely
     while the bar is scrolled out of view.

Plus the DECKLE edge (the torn-paper silhouette), which is an art requirement
the owner asked for explicitly ("the border could even be irregular") and which
must not decay back into a plain rectangle.

Everything runs the REAL shipped module under node against a counting mock 2d
context — no re-implementation.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCENE_JS = REPO / "static" / "js" / "tofu-scene.js"

if shutil.which("node") is None:  # pragma: no cover - env gate
    pytest.skip("node not on PATH", allow_module_level=True)

pytestmark = pytest.mark.unit


# ── Harness: a mock 2d context that TALLIES every canvas call, so we can measure
# the real per-frame API cost of the shipped module at a chosen bar width. It
# also records rAF scheduling + the deckle path, so pacing / parking / the torn
# outline are observable from the same run. ──
_PERF_HARNESS = r"""
'use strict';
const W = __W__, H = 48;
const DECOR = "__DECOR__";
const OFFSCREEN = __OFFSCREEN__;

function mkCtx(rec){
  const bump = k => () => { rec[k] = (rec[k]||0)+1; rec.total++; };
  let path = null;
  return {
    canvas:{width:W,height:H},
    setTransform:bump('setTransform'), clearRect:bump('clearRect'),
    save:bump('save'), restore:bump('restore'),
    translate:bump('translate'), rotate:bump('rotate'),
    beginPath(){ rec.beginPath=(rec.beginPath||0)+1; rec.total++; path=[]; },
    moveTo(x,y){ rec.moveTo=(rec.moveTo||0)+1; rec.total++; if(path)path.push([x,y]); },
    lineTo(x,y){ rec.lineTo=(rec.lineTo||0)+1; rec.total++; if(path)path.push([x,y]); },
    closePath(){ rec.total++; },
    rect(x,y,w,h){ rec.rect=(rec.rect||0)+1; rec.total++; if(path)path.push(['RECT',x,y,w,h]); },
    ellipse(){ rec.ellipse=(rec.ellipse||0)+1; rec.total++; },
    arc(){ rec.total++; },
    fill(rule){
      rec.fill=(rec.fill||0)+1; rec.total++;
      if (rule === 'evenodd' && path) {
        rec.deckle = path.filter(p => p[0] !== 'RECT');
        rec.deckleHadRect = path.some(p => p[0] === 'RECT');
        rec.deckleCuts = (rec.deckleCuts||0)+1;
      }
    },
    fillRect:bump('fillRect'), stroke:bump('stroke'), drawImage:bump('drawImage'),
    createLinearGradient(){ rec.total++; return {addColorStop(){}}; },
    createRadialGradient(){ rec.total++; return {addColorStop(){}}; },
    set fillStyle(v){ rec.fillStyle=(rec.fillStyle||0)+1; rec.total++; },
    get fillStyle(){ return ''; },
    set globalAlpha(v){ rec.globalAlpha=(rec.globalAlpha||0)+1; rec.total++; },
    get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){ rec.gco=(rec.gco||0)+1; rec.total++; },
    get globalCompositeOperation(){ return ''; },
    set strokeStyle(v){}, get strokeStyle(){ return ''; },
    set lineWidth(v){}, get lineWidth(){ return 1; },
  };
}
const vis={total:0}, buf={total:0}, fg={total:0};

let rafCb = null, rafRequests = 0;
global.requestAnimationFrame = function(cb){ rafCb = cb; rafRequests++; return rafRequests; };
global.cancelAnimationFrame = function(){};
global.devicePixelRatio = 2;

// Capture the IntersectionObserver the module installs so we can drive it.
let ioCb = null;
global.window = {
  matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; },
  addEventListener(){},
  ResizeObserver: function(){ return {observe(){}, disconnect(){}}; },
  MutationObserver: function(){ return {observe(){}, disconnect(){}}; },
  IntersectionObserver: function(cb){ ioCb = cb; return {observe(){}, disconnect(){}}; },
  devicePixelRatio: 2,
};
global.ResizeObserver = global.window.ResizeObserver;
global.MutationObserver = global.window.MutationObserver;
global.IntersectionObserver = global.window.IntersectionObserver;

function mkEl(){
  return {_attrs:{}, className:'', style:{}, width:0, height:0,
    setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
    appendChild(){}, insertBefore(){}, querySelector(){return null;}, firstChild:null,
    getBoundingClientRect(){return {left:0,right:W,top:0,bottom:H,width:W,height:H};}};
}
const bar = mkEl(); bar._attrs['data-decor'] = DECOR;
let canvasN = 0;
global.document = {
  readyState:'complete', hidden:false,
  documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},
  addEventListener(){},
  getElementById(id){ return id==='projectBar' ? bar : null; },
  createElement(t){
    if (t==='canvas'){ canvasN++; const rec = canvasN===1?vis:(canvasN===2?buf:fg);
      const e = mkEl(); e.getContext = () => mkCtx(rec); return e; }
    return mkEl();
  },
};
global.window.TofuPet = { getState(){ return {x: W/2 - 16, state:'walk'}; } };

__SRC__

function reset(r){ for (const k of Object.keys(r)) delete r[k]; r.total = 0; }

let t = 100;
if (rafCb) { const cb = rafCb; rafCb = null; cb(t); }     // t0 frame

if (OFFSCREEN && ioCb) {
  ioCb([{isIntersecting:false}]);                          // bar scrolls away
  const before = rafRequests;
  // Pump whatever tick is still in flight; the loop must NOT re-arm itself.
  if (rafCb) { const cb = rafCb; rafCb = null; cb(t += 40); }
  console.log(JSON.stringify({offscreenRearmed: rafRequests > before, parked: rafCb === null}));
  process.exit(0);
}

// ── steady-state: measure ONE painted frame's canvas cost ──
reset(vis); reset(fg);
let painted = 0, ticks = 0;
// Advance in 16ms (60Hz) steps and count how many of them actually PAINT.
for (let i = 0; i < 12; i++) {
  t += 16; ticks++;
  const before = vis.total;
  if (!rafCb) break;
  const cb = rafCb; rafCb = null; cb(t);
  if (vis.total > before) {
    painted++;
    if (painted === 1) { /* keep the first painted frame's tallies below */ }
  }
}
console.log(JSON.stringify({
  bg: vis, fg: fg, buf: buf,
  perFrame: vis.total + fg.total,
  ticks: ticks, painted: painted,
}));
process.exit(0);
"""


def _run_perf(width=900, decor="meadow", offscreen=False, src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = (_PERF_HARNESS
              .replace("__SRC__", src)
              .replace("__W__", str(width))
              .replace("__DECOR__", decor)
              .replace("__OFFSCREEN__", "true" if offscreen else "false"))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


# ══════════════════════════════════════════════════════════════════════════
#  1. BATCHED DABS
# ══════════════════════════════════════════════════════════════════════════

def test_dabs_are_batched_into_few_fills():
    """The dominant per-frame cost is one rasterizer flush per fill(). Dabs are
    bucketed by (colour, alpha) and flushed as ONE path per bucket, so a scene
    painting hundreds of dabs must issue only DOZENS of fills. A regression to
    fill-per-dab shows up here as a ~1:1 ratio."""
    r = _run_perf()
    for plane in ("bg", "fg"):
        dabs = r[plane].get("ellipse", 0)
        fills = r[plane].get("fill", 0)
        assert dabs > 100, f"{plane}: too few dabs to be a meaningful check ({dabs})"
        assert fills * 4 <= dabs, (
            f"{plane}: dabs are not batched — {fills} fills for {dabs} dabs. "
            f"Each fill is a rasterizer flush; batching must collapse them.")


def test_no_per_dab_transform_churn():
    """Rotation rides in the ellipse() args, so the per-dab
    save/translate/rotate/restore quartet must be GONE. A handful of
    save/restore pairs remain for the composite-op scopes (sun glow, critter) —
    but they must not scale with the dab count."""
    r = _run_perf()
    dabs = r["bg"].get("ellipse", 0) + r["fg"].get("ellipse", 0)
    churn = sum(r[p].get(k, 0) for p in ("bg", "fg")
                for k in ("translate", "rotate", "save", "restore"))
    assert dabs > 100
    assert churn < dabs / 10, (
        f"per-dab transform churn is back: {churn} transform calls for {dabs} dabs "
        f"(expected a fixed handful of composite scopes, not one set per dab)")


def test_NEUTER_unbatched_dabs_is_caught():
    """NEUTER: flush the queue after EVERY enqueued dab (i.e. one fill per dab,
    the pre-optimization behaviour) → the batching ratio assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("    arr.push(x, y, len, wid, ang);",
                       "    arr.push(x, y, len, wid, ang);\n    flushDabs();  /* NEUTER: unbatched */", 1)
    assert neut != src, "neuter did not match the dab enqueue"
    r = _run_perf(src=neut)
    dabs = r["bg"].get("ellipse", 0)
    fills = r["bg"].get("fill", 0)
    assert fills * 4 > dabs, (
        f"neutered (unbatched) build still looked batched: {fills} fills / {dabs} dabs "
        f"— the batching guard would not bite")


# ══════════════════════════════════════════════════════════════════════════
#  2. WIDTH PLATEAU — per-frame cost must not scale with the bar's width
# ══════════════════════════════════════════════════════════════════════════

def test_per_frame_cost_plateaus_with_bar_width():
    """LIVE populations are re-resolved every frame, so seeding them purely by
    AREA made a wide window pay proportionally more forever. They are capped, so
    the per-frame call count must FLATTEN: a 2400px bar may not cost materially
    more than a 900px one (it renders the same 48px-tall painting)."""
    narrow = _run_perf(width=900)["perFrame"]
    wide = _run_perf(width=2400)["perFrame"]
    assert narrow > 0 and wide > 0
    assert wide <= narrow * 1.25, (
        f"per-frame cost still scales with width: 900px={narrow} calls vs "
        f"2400px={wide} calls. The live-population caps are not holding.")


def test_narrow_bar_is_not_thinned_by_the_caps():
    """The caps must sit ABOVE a normal bar's natural population, so a typical
    project bar is painted at full density and the optimization is invisible.
    A 360px bar must still paint a rich field."""
    r = _run_perf(width=360)
    assert r["bg"].get("ellipse", 0) >= 150, \
        f"a normal-width bar lost density: only {r['bg'].get('ellipse')} bg dabs"
    assert r["fg"].get("ellipse", 0) >= 100, \
        f"the near plane thinned out: only {r['fg'].get('ellipse')} fg dabs"


def test_NEUTER_uncapped_live_population_is_caught():
    """NEUTER: remove the cap (let _budget always return the wanted count) → the
    per-frame cost resumes scaling with width and the plateau assertion fails."""
    src = SCENE_JS.read_text()
    neut = src.replace(
        "    if (wanted <= cap) return { n: wanted, scale: 1 };",
        "    return { n: wanted, scale: 1 };  /* NEUTER: uncapped */\n"
        "    if (wanted <= cap) return { n: wanted, scale: 1 };", 1)
    assert neut != src, "neuter did not match the _budget cap"
    narrow = _run_perf(width=900, src=neut)["perFrame"]
    wide = _run_perf(width=2400, src=neut)["perFrame"]
    assert wide > narrow * 1.25, (
        f"neutered (uncapped) build still plateaued (900px={narrow}, 2400px={wide}) "
        f"— the plateau guard would not bite")


# ══════════════════════════════════════════════════════════════════════════
#  3. FRAME PACING + OFF-SCREEN PARK
# ══════════════════════════════════════════════════════════════════════════

def test_loop_paces_below_display_refresh():
    """The scene is slow ambient weather; painting it every vsync spent half the
    work on frames the eye cannot separate. Driven at 60Hz (16ms ticks), it must
    PAINT on only a fraction of them."""
    r = _run_perf()
    assert r["ticks"] >= 10, f"harness did not deliver enough ticks: {r['ticks']}"
    assert r["painted"] < r["ticks"], (
        f"the loop painted on every one of {r['ticks']} 60Hz ticks — frame pacing "
        f"is not in effect")
    assert r["painted"] >= 2, (
        f"the loop barely painted ({r['painted']}/{r['ticks']}) — pacing must slow "
        f"the scene, not freeze it")


def test_NEUTER_unpaced_loop_is_caught():
    """NEUTER: paint on every tick (pace interval 0) → the pacing assertion must
    fail, proving it is the pacing gate being measured and not tick starvation."""
    src = SCENE_JS.read_text()
    neut = src.replace("var SCENE_FRAME_MS = 1000 / SCENE_FPS - 1;",
                       "var SCENE_FRAME_MS = 0;  /* NEUTER: unpaced */", 1)
    assert neut != src, "neuter did not match the pacing constant"
    r = _run_perf(src=neut)
    assert r["painted"] == r["ticks"], (
        f"neutered (unpaced) build still skipped frames ({r['painted']}/{r['ticks']}) "
        f"— the pacing guard would not bite")


def test_loop_parks_when_the_bar_scrolls_out_of_view():
    """An off-screen painting is invisible work. When the IntersectionObserver
    reports the bar has left the viewport the rAF chain must stop entirely — not
    merely skip painting, which would still wake per vsync."""
    r = _run_perf(offscreen=True)
    assert r["offscreenRearmed"] is False, \
        "the loop re-armed rAF after the bar went off-screen — it never parks"


def test_NEUTER_no_offscreen_park_is_caught():
    """NEUTER: ignore the off-screen flag in the loop gate → the loop keeps
    re-arming while invisible and the park assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("    if (_offscreen) return;                       "
                       "// bar not on screen → nothing to paint",
                       "    /* NEUTER: no offscreen park */", 1)
    assert neut != src, "neuter did not match the offscreen gate in _loop"
    neut2 = neut.replace(
        "    if (_offscreen) { if (_raf) { cancelAnimationFrame(_raf); _raf = 0; } return; }",
        "    /* NEUTER: no offscreen park */", 1)
    assert neut2 != neut, "neuter did not match the offscreen gate in _ensureLoop"
    r = _run_perf(offscreen=True, src=neut2)
    assert r["offscreenRearmed"] is True, \
        "neutered build still parked off-screen — the park guard would not bite"


# ══════════════════════════════════════════════════════════════════════════
#  4. THE DECKLE EDGE — an irregular, torn silhouette, not a rectangle
# ══════════════════════════════════════════════════════════════════════════

def test_painting_is_cut_to_an_irregular_torn_outline():
    """OWNER ASK — 'the border could even be irregular'. The painting is cut to a
    DECKLE edge (the feathered rim of handmade paper): an even-odd cut of
    [full rect] minus [wandering outline]. Assert the cut happens, that the
    outline is a real polygon, and — the part that matters — that its inward
    bite VARIES, because a constant bite is just a smaller rectangle."""
    r = _run_perf()
    bg = r["bg"]
    assert bg.get("deckleCuts", 0) >= 1, \
        "the painting is never cut — the frame is a plain rectangle again"
    assert bg.get("deckleHadRect") is True, \
        "the even-odd cut must start from a full-canvas rect to erase the OUTSIDE"
    pts = bg.get("deckle") or []
    assert len(pts) >= 16, f"deckle outline is too coarse to read as torn: {len(pts)} points"
    W, H = 900, 48
    bites = [min(x, y, W - x, H - y) for x, y in pts]
    variation = max(bites) - min(bites)
    assert variation > 1.0, (
        f"the outline's inward bite is nearly constant ({variation:.2f}px of "
        f"variation) — that is a rounded rectangle, not a torn edge")
    assert min(bites) >= 0, "the outline escaped the canvas box"


def test_deckle_is_cut_on_every_live_layer():
    """Both live canvases must be torn by the SAME edge. The baked buffer is cut
    at bake time, but the per-frame layers (sun wash, flow, wake) paint across
    the whole canvas and would refill the torn margin; the near plane would
    likewise square off the bottom corners."""
    r = _run_perf()
    assert r["bg"].get("deckleCuts", 0) >= 1, "the composited frame is not re-torn"
    assert r["fg"].get("deckleCuts", 0) >= 1, "the foreground plane is not torn"


def test_NEUTER_untorn_rectangle_is_caught():
    """NEUTER: zero the bite amplitude → the outline collapses onto a uniform
    inset (a rectangle), and the irregularity assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("var DECKLE_BITE = 3.4;", "var DECKLE_BITE = 0;  /* NEUTER */", 1)
    assert neut != src, "neuter did not match the deckle bite constant"
    r = _run_perf(src=neut)
    pts = r["bg"].get("deckle") or []
    assert pts, "neutered build cut nothing at all — wrong neuter"
    W, H = 900, 48
    bites = [min(x, y, W - x, H - y) for x, y in pts]
    assert (max(bites) - min(bites)) <= 1.0, \
        "neutered build still produced an irregular outline — the guard would not bite"


def test_deckle_degrades_when_the_context_cannot_cut():
    """The cut needs ctx.rect + fill('evenodd'). A context without them (an old
    engine, or a reduced mock) must simply keep the plain rounded silhouette
    rather than throwing — the scene degrades, it never breaks."""
    src = SCENE_JS.read_text()
    assert "if (!_deckle.length || !ctx.rect) return;" in src, (
        "_cutDeckle lost its capability guard — a context without rect() would "
        "throw on every frame")


# ══════════════════════════════════════════════════════════════════════════
#  5. THE PET's per-frame cost (static/js/tofu-pet.js)
# ══════════════════════════════════════════════════════════════════════════

PET_JS = REPO / "static" / "js" / "tofu-pet.js"


def test_pet_does_not_write_a_dead_parallax_property_every_frame():
    """The pet's _place() runs on every rAF tick. It used to write
    `--bar-scene-x` on .project-bar unconditionally — but the ONLY rule reading
    that property is the SVG ground (::after) `background-position`, and that
    ::after is set to display:none whenever the canvas is live (the normal case,
    gated on data-scene-canvas="on").

    So the write was invalidating the bar's custom-property inheritance every
    frame to scroll the background of a box that is not rendered. It must be
    gated on the SVG ground actually being the thing on screen. The property is
    still maintained for the no-JS / no-canvas fallback, which is its only real
    reader."""
    src = PET_JS.read_text()
    m = re.search(r"function _place\(\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _place()"
    body = m.group(1)
    assert "--bar-scene-x" in body, "the parallax property write vanished entirely"
    assert "data-scene-canvas" in body, (
        "_place() writes --bar-scene-x unconditionally again — that is a per-frame "
        "style invalidation feeding a display:none rule whenever the canvas is live")
    # and the CSS half must still be the thing that makes it dead
    css = (REPO / "static" / "styles.css").read_text()
    assert 'data-scene-canvas="on"]::after{display:none}' in css.replace(" ", ""), (
        "the canvas-wins gate that makes the SVG ground (and thus --bar-scene-x) "
        "dead is gone — re-check whether the _place() gate is still correct")


def test_pet_light_writes_are_deduped():
    """_applyLight() runs every rAF tick (deliberately — it must keep working
    during a drag). It must only touch the DOM when a ROUNDED light value
    actually changed, else it writes four custom properties per frame forever."""
    src = PET_JS.read_text()
    m = re.search(r"function _applyLight\(\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _applyLight()"
    body = m.group(1)
    assert "_lastLightK" in body and "if (k === _lastLightK) return;" in body, (
        "_applyLight lost its change-detection guard — it now writes 4 custom "
        "properties on every animation frame")
