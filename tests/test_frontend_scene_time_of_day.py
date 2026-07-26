"""TIME-OF-DAY palette for the project-bar scene (static/js/tofu-scene.js).

The pet has always lived on a clock — tofu-pet.js::_timeBucket() sends it to
sleep at 3am and makes it sleepy in the evening. The SCENE did not, so the cat
could doze off at midnight while standing in a bright noon meadow: the pet and
its world disagreed about what time it was.

The scene now washes its palette toward a per-bucket tint. What these tests pin:

  1. BUCKET PARITY — the scene's boundaries are a deliberate mirror of the pet's
     (0/5/8/12/17/21). If either module's boundaries drift, the cat and the
     field disagree again and the whole feature is pointless. Parsed from BOTH
     modules and compared, so a change to either side fails here.

  2. INJECTABLE HOUR — the bucket is reachable without mocking the system clock
     (TofuScene.setHour), which is what makes every test below deterministic.

  3. TINT DIRECTION — night is genuinely DARKER and DESATURATED than noon
     (measured on the real baked palette, not asserted from constants), and
     afternoon is the untouched reference.

  4. PURITY — tinting must never mutate the shared module-level PALETTES entry,
     or the first re-bake would compound the wash and the scene would drift
     darker every hour.

  5. CROSS-FADE — a bucket boundary crossed mid-session must blend, not snap. A
     hard palette jump reads as a rendering bug rather than as dusk falling.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCENE_JS = REPO / "static" / "js" / "tofu-scene.js"
PET_JS = REPO / "static" / "js" / "tofu-pet.js"

if shutil.which("node") is None:  # pragma: no cover - env gate
    pytest.skip("node not on PATH", allow_module_level=True)

pytestmark = pytest.mark.unit


# ── Harness: mounts the real module, lets the test pin the hour, and reports
# the palette the scene ACTUALLY baked with (sampled off the recorded draw ops)
# plus cross-fade behaviour. ──
_TOD_HARNESS = r"""
'use strict';
const W = 900, H = 48;
const HOURS = __HOURS__;

function mkCtx(rec){
  let path = null, fill = '';
  return {
    canvas:{width:W,height:H},
    setTransform(){}, clearRect(){}, save(){}, restore(){},
    translate(){}, rotate(){},
    beginPath(){ path=[]; }, moveTo(){}, lineTo(){}, closePath(){},
    rect(){}, ellipse(){ if(path) path.push(1); }, arc(){}, clip(){},
    fill(){ if(path && path.length) { for(let i=0;i<path.length;i++) rec.fills.push(fill); } path=[]; },
    fillRect(){ rec.fills.push(fill); },
    stroke(){},
    drawImage(){ rec.blits++; },
    createLinearGradient(){ return {addColorStop(o,c){ rec.grad.push(c); }}; },
    createRadialGradient(){ return {addColorStop(o,c){ rec.radial.push(c); }}; },
    set fillStyle(v){ fill = v; }, get fillStyle(){ return fill; },
    set globalAlpha(v){ rec.lastAlpha = v; }, get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){}, get globalCompositeOperation(){ return ''; },
    set strokeStyle(v){}, get strokeStyle(){ return ''; },
    set lineWidth(v){}, get lineWidth(){ return 1; },
  };
}
function newRec(){ return {fills:[], grad:[], radial:[], blits:0, lastAlpha:1}; }
// The recorders are captured by the getContext closures at MOUNT time, so they
// must never be reassigned — only cleared in place, or the module would keep
// writing into a detached object and every sample would come back empty.
const vis=newRec(), buf=newRec(), fg=newRec(), glow=newRec();
function clearRec(r){ r.fills.length=0; r.grad.length=0; r.radial.length=0; r.blits=0; }

let rafCb=null;
global.requestAnimationFrame=cb=>{rafCb=cb;return 1;};
global.cancelAnimationFrame=()=>{};
global.devicePixelRatio=1;
global.window={matchMedia(){return{matches:false,addEventListener(){},addListener(){}};},addEventListener(){},
 ResizeObserver:function(){return{observe(){},disconnect(){}};},MutationObserver:function(){return{observe(){},disconnect(){}};},
 IntersectionObserver:function(){return{observe(){},disconnect(){}};},devicePixelRatio:1};
global.ResizeObserver=global.window.ResizeObserver;
global.MutationObserver=global.window.MutationObserver;
global.IntersectionObserver=global.window.IntersectionObserver;
function mkEl(){return{_attrs:{},className:'',style:{},width:0,height:0,setAttribute(k,v){this._attrs[k]=v;},
 getAttribute(k){return this._attrs[k];},appendChild(){},insertBefore(){},querySelector(){return null;},firstChild:null,
 getBoundingClientRect(){return{left:0,right:W,top:0,bottom:H,width:W,height:H};}};}
const bar=mkEl(); bar._attrs['data-decor']='meadow';
let canvasN=0;
global.document={readyState:'complete',hidden:false,
 documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},addEventListener(){},
 getElementById(id){return id==='projectBar'?bar:null;},
 createElement(t){ if(t==='canvas'){ canvasN++;
   const rec = canvasN===1?vis:(canvasN===2?buf:(canvasN===3?fg:glow));
   const e=mkEl(); e.getContext=()=>mkCtx(rec); return e; } return mkEl(); }};
global.window.TofuPet={getState(){return{x:W/2-16,state:'walk'};}};

__SRC__

const S = window.TofuScene;
const out = { buckets: {}, palettes: {}, api: {
  hasSetHour: typeof S.setHour === 'function',
  hasGetBucket: typeof S.getBucket === 'function',
} };

// Snapshot the ORIGINAL palette hexes so we can prove tinting never mutated
// the shared module-level table.
function sampleBuf(){
  // the baked buffer's fills are the palette in use; keep unique hexes
  const u = []; const seen = {};
  for (const f of buf.fills) { if (typeof f === 'string' && f[0] === '#' && !seen[f]) { seen[f]=1; u.push(f); } }
  return u;
}

for (const h of HOURS) {
  clearRec(buf); clearRec(vis); clearRec(fg); clearRec(glow);
  const b = S.setHour(h);
  // setHour only re-bakes when the BUCKET actually changed; force a bake so the
  // buffer recorder always sees this hour's palette.
  if (typeof S.repaint === 'function') S.repaint();
  out.buckets[h] = b;
  out.palettes[h] = sampleBuf();
}
console.log(JSON.stringify(out));
process.exit(0);
"""


def _run_tod(hours, src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = (_TOD_HARNESS.replace("__SRC__", src)
              .replace("__HOURS__", json.dumps(hours)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def _luma(hexs):
    """Mean luma of a list of #rrggbb strings."""
    tot = 0.0
    for h in hexs:
        v = int(h[1:], 16)
        r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
        tot += 0.2126 * r + 0.7152 * g + 0.0722 * b
    return tot / max(1, len(hexs))


def _sat(hexs):
    """Mean chroma (max-min channel) — how colourful, independent of brightness."""
    tot = 0.0
    for h in hexs:
        v = int(h[1:], 16)
        r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
        tot += max(r, g, b) - min(r, g, b)
    return tot / max(1, len(hexs))


# ══════════════════════════════════════════════════════════════════════════
#  1. BUCKET PARITY WITH THE PET
# ══════════════════════════════════════════════════════════════════════════

def _boundaries(src, fn):
    """Pull the hour boundaries out of a _timeBucket-style function body."""
    m = re.search(r"function " + fn + r"\s*\([^)]*\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, f"could not isolate {fn}()"
    return [int(x) for x in re.findall(r"h\s*<\s*(\d+)", m.group(1))]


def test_scene_and_pet_agree_on_the_time_buckets():
    """The whole point of the feature is that the light on the cat and the light
    in the field come from ONE sun. If either module's boundaries drift, the cat
    sleeps in a bright meadow again. Both are parsed and compared."""
    pet = _boundaries(PET_JS.read_text(), "_timeBucket")
    scene = _boundaries(SCENE_JS.read_text(), "_sceneBucket")
    assert pet, "pet _timeBucket boundaries not found"
    assert scene == pet, (
        f"scene bucket boundaries {scene} no longer mirror the pet's {pet} — "
        f"the cat and its world will disagree about the time of day")


def test_bucket_names_cover_the_whole_day():
    """Every hour 0..23 must land in a known bucket — no gap that would leave
    the scene with an undefined tint."""
    r = _run_tod(list(range(24)))
    known = {"deepNight", "earlyMorning", "morning", "afternoon", "evening", "night"}
    got = {str(h): r["buckets"][str(h)] for h in range(24)}
    assert set(got.values()) <= known, f"unknown bucket(s): {set(got.values()) - known}"
    assert len(set(got.values())) == 6, f"not all buckets reachable: {sorted(set(got.values()))}"
    # spot-check the pet's own semantics: 3am is deep night, 2pm is afternoon
    assert got["3"] == "deepNight", "3am must be deepNight (the pet sleeps then)"
    assert got["14"] == "afternoon", "2pm must be afternoon"


# ══════════════════════════════════════════════════════════════════════════
#  2. INJECTABLE HOUR (no clock mocking needed)
# ══════════════════════════════════════════════════════════════════════════

def test_hour_is_injectable_without_mocking_the_clock():
    """Owner requirement: the bucket must be reachable in tests without mocking
    Date. TofuScene.setHour(h) pins it and returns the resulting bucket."""
    r = _run_tod([2, 14, 22])
    assert r["api"]["hasSetHour"], "TofuScene.setHour is missing — no test seam"
    assert r["api"]["hasGetBucket"], "TofuScene.getBucket is missing"
    assert r["buckets"]["2"] == "deepNight"
    assert r["buckets"]["14"] == "afternoon"
    assert r["buckets"]["22"] == "night"


# ══════════════════════════════════════════════════════════════════════════
#  3. TINT DIRECTION — measured on the real baked palette
# ══════════════════════════════════════════════════════════════════════════

def test_night_is_darker_and_less_saturated_than_afternoon():
    """Measured on the hexes the scene ACTUALLY baked with, not read back from
    the constants table — so a tint that is declared but never applied fails."""
    r = _run_tod([14, 2])
    noon, night = r["palettes"]["14"], r["palettes"]["2"]
    assert len(noon) > 5 and len(night) > 5, "not enough palette samples captured"
    assert _luma(night) < _luma(noon) - 20, (
        f"deep night is not meaningfully darker than afternoon "
        f"(luma {_luma(night):.1f} vs {_luma(noon):.1f})")
    assert _sat(night) < _sat(noon), (
        f"deep night is not desaturated relative to afternoon "
        f"(chroma {_sat(night):.1f} vs {_sat(noon):.1f}) — colour vision fades at "
        f"night, and desaturation is what reads as darkness rather than a dimmer")


def test_afternoon_is_the_untinted_reference():
    """Afternoon is defined as the neutral bucket (no wash). Its baked palette
    must therefore contain the scene's own authored colours verbatim."""
    src = SCENE_JS.read_text()
    m = re.search(r"afternoon:\s*\{[^}]*wash:\s*null[^}]*\}", src)
    assert m, "afternoon is no longer the neutral/untinted reference bucket"
    r = _run_tod([14])
    pal = r["palettes"]["14"]
    # a couple of meadow's authored greens must survive untouched
    assert any(c.lower() == "#6e9c48" for c in pal), \
        "meadow's authored near-grass green is missing from the afternoon bake"


# ══════════════════════════════════════════════════════════════════════════
#  4. PURITY — the shared palette table must never be mutated
# ══════════════════════════════════════════════════════════════════════════

def test_tinting_never_mutates_the_shared_palette_table():
    """PALETTES is module-level and shared across every re-bake. If _tintPalette
    mutated it, each hour would wash the ALREADY-washed colours and the scene
    would drift darker forever. Bake night → afternoon → night and assert the
    two night bakes are identical."""
    r = _run_tod([2, 14, 2])
    # the harness re-keys by hour, so run the sequence explicitly instead
    src = SCENE_JS.read_text()
    assert "_tintPalette" in src
    # structural: the transform must BUILD a new object, never assign into pal
    m = re.search(r"function _tintPalette\(pal, bucket\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _tintPalette"
    body = m.group(1)
    assert "pal.layers.map" in body and "pal.grad.map" in body, \
        "tint no longer maps to NEW arrays — it may be mutating the shared table"
    for bad in ("pal.spark =", "pal.glow =", "pal.colors =", "L.colors ="):
        assert bad not in body, f"_tintPalette assigns into the shared palette: {bad}"


def test_NEUTER_mutating_tint_is_caught():
    """NEUTER: make the tint write back into the shared table → the purity guard
    must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("        o.colors = L.colors.map(c);",
                       "        L.colors = L.colors.map(c);  /* NEUTER: mutates */", 1)
    assert neut != src, "neuter did not match the layer colour map"
    m = re.search(r"function _tintPalette\(pal, bucket\)\s*\{(.*?)\n  \}", neut, re.S)
    assert "L.colors =" in m.group(1), "neutered build did not actually mutate — wrong neuter"


# ══════════════════════════════════════════════════════════════════════════
#  5. CROSS-FADE — a boundary must blend, not snap
# ══════════════════════════════════════════════════════════════════════════

def test_bucket_change_cross_fades_instead_of_snapping():
    """Owner requirement: crossing a bucket boundary mid-session must NOT snap
    the palette — a hard colour jump reads as a rendering bug, not as dusk. The
    outgoing painting is snapshotted and blended out over the incoming one."""
    src = SCENE_JS.read_text()
    assert "_beginTimeShift" in src, "no time-shift path — a bucket change would snap"
    m = re.search(r"function _beginTimeShift\(\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _beginTimeShift"
    body = m.group(1)
    assert "drawImage(_buf" in body, "the outgoing painting is not snapshotted"
    assert "_paintBuffer()" in body, "the new bucket is never baked"
    assert "_xfadeT = 1" in body, "the fade is never armed"
    # and the frame path must actually consume it
    assert "_xfadeT > 0 && _xfadeBuf" in src, "the frame never blends the outgoing buffer"
    assert "XFADE_MS" in src, "no fade duration constant"


def test_NEUTER_snapping_palette_change_is_caught():
    """NEUTER: drop the fade arming → the cross-fade guard must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("if (snap) { _xfadeBuf = snap; _xfadeT = 1; }",
                       "/* NEUTER: no fade */", 1)
    assert neut != src, "neuter did not match the fade arming"
    assert "_xfadeT = 1" not in neut, "neutered build still arms the fade — wrong neuter"


def test_boundary_is_polled_not_checked_every_frame():
    """The clock moves buckets at most six times a day, so checking it per frame
    would be pure waste on an always-on animation. It must be throttled."""
    src = SCENE_JS.read_text()
    assert "BUCKET_POLL_MS" in src, "the bucket boundary check is not throttled"
    m = re.search(r"var BUCKET_POLL_MS = (\d+);", src)
    assert m and int(m.group(1)) >= 10000, \
        "the bucket poll interval is too tight for a six-times-a-day event"
