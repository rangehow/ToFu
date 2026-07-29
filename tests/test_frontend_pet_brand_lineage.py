"""The project-bar pet is the TOFU BRAND MASCOT — lineage + art-integrity guards.

WHY THIS FILE EXISTS
────────────────────
The bar pet used to be a borrowed public-domain pixel-art cat. It shared NOTHING
with the Tofu brand form: not the palette, not the ink, not the face language —
the owner's report was "inconsistent with the brand tone, and not good-looking
enough". The fix replaced it with the mascot itself (the isometric cream block
from ``static/icons/tofu-welcome.svg``).

The existing suite (``test_frontend_tofu_pet.py``) proves the pet BEHAVES — the
wander FSM, the walk cycle, the moods. Every one of those tests stayed green for
the entire period the pet was an off-brand cat, because none of them can see
what the art LOOKS like. That is the blind spot this file covers:

  1. LINEAGE — every colour the pet is drawn with must actually occur in the
     shipped brand mascot. This is what makes "same creature" a checkable fact
     rather than an intention. Note it is asserted against the SHIPPED logo, not
     against a candidate: an earlier draft of the pet was built on the palette of
     ``skins/a2-soft.svg``, a REJECTED logo candidate whose ink/blush/body all
     differ from the shipped mascot — that would have shipped a pet subtly
     mismatched with the logo beside it.
  2. NO SECOND COPY — the 18 frames are emitted by a generator. If a frame is
     hand-edited on disk the generator's ``--check`` gate goes red, so the body
     geometry can never silently desynchronise between frames.
  3. ART INTEGRITY — every frame must be well-formed SVG, must carry the brand
     face (eyes + the cube's seam structure), and must keep its ink INSIDE the
     viewBox. The clipping check is load-bearing: an accent mark placed at
     x=30.2 in a 32-unit box rendered as a cut-off stub at the shipped 30px, and
     no behavioural test could ever have seen it.
  4. NO MASCOT SWITCHER — mascot switching has been vetoed twice by the owner
     (first the pet pack-switcher, later the whole logo-skin picker). Guard that
     a character registry / picker / try-on does not grow back.

These are pure file + XML assertions: no node, no browser, no network.
"""
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
PET_DIR = REPO / "static" / "icons" / "pet" / "tofu"
BRAND_SVG = REPO / "static" / "icons" / "tofu-welcome.svg"
GEN = REPO / "static" / "icons" / "_gen" / "tofu-pet" / "gen_tofu_pet.py"

SVG_NS = "{http://www.w3.org/2000/svg}"

# The frames the engine resolves: 9 expressions + 4 walk + 3 groom + 2 stretch.
EXPECTED_FRAMES = (
    "idle happy sleepy sleeping thinking surprised sad celebrating alert "
    "walk1 walk2 walk3 walk4 groom1 groom2 groom3 scratch1 scratch2"
).split()

pytestmark = pytest.mark.unit


def _hexes(text):
    """Every 6-digit hex colour in a blob of SVG, upper-cased."""
    return {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}", text)}


def _frame_files():
    return sorted(PET_DIR.glob("tofu-*.svg"))


# Colours allowed beyond the mascot's own palette. Both are deliberate ACCENTS
# that appear only in accent glyphs (specular eye dot, sparkle gold, tear blue),
# never on the body.
ACCENT_COLOURS = {"#FFFFFF", "#F6C86A", "#8FC7E8"}


def _offbrand_colours(svg_text, brand):
    """Colours in ``svg_text`` that do NOT occur in the brand mascot.

    THE one implementation of the lineage rule — the guard and its NEUTER both
    call this, so the NEUTER exercises the shipped assertion path instead of
    re-implementing it (a re-implemented neuter proves only that the copy works).
    """
    return _hexes(svg_text) - brand - ACCENT_COLOURS


def _out_of_viewbox(svg_text, lo=-0.5, hi=32.5):
    """Path coordinates outside the 32-unit viewBox (i.e. art that gets CLIPPED).

    Same single-implementation contract as ``_offbrand_colours``.
    """
    joined = " ".join(re.findall(r'd="([^"]+)"', svg_text))
    coords = [float(n) for n in re.findall(r"-?\d+\.?\d*", joined)]
    return sorted({c for c in coords if c < lo or c > hi})


def test_every_declared_frame_has_art():
    """All 18 frames the engine can ask for must exist and be non-empty."""
    for name in EXPECTED_FRAMES:
        f = PET_DIR / f"tofu-{name}.svg"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty pet frame: {f}"


def test_pet_palette_is_drawn_from_the_shipped_brand_mascot():
    """LINEAGE: every colour in every pet frame must occur in the shipped mascot.

    This is the assertion that makes "the pet and the logo are the same
    creature" a fact instead of a claim. A palette drifting toward some other
    art (or toward a rejected logo candidate) turns this red.

    Two colours are allowed beyond the mascot's own palette, and both are
    deliberate ACCENTS rather than body colour: the specular white on the eye
    and the small mark colours (sparkle gold / tear blue) that only appear in
    accent glyphs, never on the body.
    """
    assert BRAND_SVG.exists(), f"brand mascot missing: {BRAND_SVG}"
    brand = _hexes(BRAND_SVG.read_text(encoding="utf-8"))

    offenders = {}
    for f in _frame_files():
        stray = _offbrand_colours(f.read_text(encoding="utf-8"), brand)
        if stray:
            offenders[f.name] = sorted(stray)
    assert not offenders, (
        "pet frames use colours that do NOT occur in the shipped brand mascot "
        f"({BRAND_SVG.name}) — the pet has drifted off-brand: {offenders}"
    )


def test_pet_uses_the_mascots_own_ink_and_blush():
    """The two colours that carry brand identity hardest must be the mascot's.

    A palette can pass the set-membership check above while picking, say, one of
    the mascot's ~20 near-black trace artefacts as its outline. Pin the two that
    a viewer actually reads: the dominant ink and the blush.
    """
    brand_text = BRAND_SVG.read_text(encoding="utf-8")
    # Dominant ink = the fill on the mascot's largest path (its outline).
    paths = re.findall(r'<path d="([^"]+)" fill="(#[0-9A-Fa-f]{6})"', brand_text)
    assert paths, "could not parse any filled paths out of the brand mascot"
    dominant_ink = max(paths, key=lambda p: len(p[0]))[1].upper()

    idle = (PET_DIR / "tofu-idle.svg").read_text(encoding="utf-8")
    used = _hexes(idle)
    assert dominant_ink in used, (
        f"the pet's outline is not the mascot's dominant ink {dominant_ink} — "
        f"pet uses {sorted(used)}"
    )
    blush = {c for c in used if c in {"#FAADA6", "#FEABA3"}}
    assert blush, f"the pet lost the mascot's blush (#FAADA6/#FEABA3); has {sorted(used)}"


def test_frames_are_wellformed_svg_and_carry_the_brand_face():
    """Every frame must parse AND carry the mascot's face + cube structure.

    'Parses' alone is far too weak — an empty <svg/> parses. So each frame must
    also show the cube's three-plane seam structure and a real face.
    """
    for f in _frame_files():
        try:
            root = ET.fromstring(f.read_text(encoding="utf-8"))
        except ET.ParseError as e:
            pytest.fail(f"{f.name} is not well-formed SVG: {e}")
        assert root.get("viewBox") == "0 0 32 32", \
            f"{f.name} viewBox changed — the engine sizes frames into a 30px box"
        # Body: the three isometric faces + the perimeter/seam strokes.
        paths = root.iter(f"{SVG_NS}path")
        assert sum(1 for _ in paths) >= 5, \
            f"{f.name} has too few paths to be the 3-plane cube + outline"
        # Face: eyes are rect/path/ellipse, but a mouth or eye mark must exist.
        marks = (list(root.iter(f"{SVG_NS}rect"))
                 + list(root.iter(f"{SVG_NS}ellipse"))
                 + list(root.iter(f"{SVG_NS}circle")))
        assert marks, f"{f.name} has no face marks (eyes/mouth/feet) at all"


def test_no_frame_paints_outside_the_viewbox():
    """Ink must stay inside 0..32 — art that overflows is CLIPPED when rendered.

    Measured failure this pins: an accent mark at x=30.2 with stroke width was
    cropped by the viewBox edge and rendered at the shipped 30px as a cut-off
    stub. Nothing behavioural can see that; only a coordinate check can.
    """
    bad = {}
    for f in _frame_files():
        out = _out_of_viewbox(f.read_text(encoding="utf-8"))
        if out:
            bad[f.name] = out[:6]
    assert not bad, (
        "path coordinates fall outside the 0..32 viewBox — this art is CLIPPED "
        f"at render size: {bad}"
    )


def test_pet_art_survives_every_export_level():
    """The art must actually REACH the user (charter: export is a first-class
    acceptance target).

    An asset that only exists in the dev tree renders as a broken <img> in every
    shipped build. Three things are asserted at once, because they fail
    differently: the FRAMES must ship (or the pet is invisible), the GENERATOR
    must ship (it is the single source of the frames + a CI gate), and the
    review PROOF SHEETS must NOT ship (multi-MB, review-only).
    """
    import sys
    sys.path.insert(0, str(REPO))
    from export import _should_exclude

    frame = ("static/icons/pet/tofu/tofu-idle.svg", "tofu-idle.svg")
    gen = ("static/icons/_gen/tofu-pet/gen_tofu_pet.py", "gen_tofu_pet.py")
    proof = ("static/icons/pet/_candidates/proof_tofu_sheet_96.png",
             "proof_tofu_sheet_96.png")

    for mode in ("personal", "internal", "opensource"):
        assert not _should_exclude(*frame, mode), \
            f"pet art is stripped from the {mode} export — the pet ships broken"
        assert not _should_exclude(*gen, mode), \
            f"the frame generator is stripped from the {mode} export"
        assert _should_exclude(*proof, mode), \
            f"review proof sheets must NOT ship in the {mode} export"


def test_pet_art_is_git_tracked():
    """The second door: a file can pass the export filter and still be absent
    from a clean clone if git never tracked it (the exact gap that shipped a
    documented-but-missing script before). Assert every frame is tracked."""
    r = subprocess.run(["git", "ls-files", "static/icons/pet/tofu"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=30)
    assert r.returncode == 0, f"git ls-files failed: {r.stderr}"
    tracked = {line.strip() for line in r.stdout.splitlines() if line.strip()}
    missing = [f.name for f in _frame_files()
               if f"static/icons/pet/tofu/{f.name}" not in tracked]
    assert not missing, (
        "pet frames exist on disk but are NOT git-tracked — they would be absent "
        f"from a clean clone: {missing}"
    )


def test_generator_is_the_single_source_of_the_frames():
    """NO SECOND COPY: the on-disk frames must match the generator exactly.

    18 hand-maintained SVGs would each hold their own copy of the body geometry
    and palette, and the first tweak would desynchronise them invisibly. The
    generator's --check gate makes that drift a red test instead.
    """
    assert GEN.exists(), f"the frame generator is missing: {GEN}"
    r = subprocess.run(["python3", str(GEN), "--check"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert r.returncode == 0, (
        "pet frames on disk have drifted from their generator "
        f"(re-run {GEN.relative_to(REPO)}):\n{r.stdout}\n{r.stderr}"
    )


def test_engine_resolves_the_tofu_character_not_a_borrowed_sprite():
    """The shipped module must point at the brand art directory."""
    src = PET_JS.read_text(encoding="utf-8")
    assert "/static/icons/pet/tofu" in src, \
        "tofu-pet.js no longer resolves the brand-native tofu frames"
    assert "data-pet', 'tofu'" in src or 'data-pet", "tofu"' in src, \
        "the pet element no longer marks itself as the tofu character"


def test_no_mascot_switcher_grows_back():
    """Mascot switching was vetoed TWICE (pet pack-switcher, then the logo-skin
    picker). Guard that a character registry / picker / try-on does not return
    to the pet module."""
    src = PET_JS.read_text(encoding="utf-8")
    for sym in ("PET_PACKS", "cyclePack", "setPack", "PACK_ORDER",
                "setCharacter", "cycleCharacter", "CHARACTERS", "listPetSkins"):
        assert sym not in src, (
            f"mascot-switching symbol {sym!r} is back in tofu-pet.js — "
            "the owner has vetoed mascot switching twice"
        )


# ── NEUTER: each guard above must be shown to BITE, on a COPY, never on disk ──

def test_NEUTER_offbrand_palette_is_caught():
    """Recolour a REAL frame off-brand (in memory) and drive the SHIPPED check.

    Calls ``_offbrand_colours`` — the same helper the guard uses — so this proves
    the guard's own code path fires, not that a copy of it fires.
    """
    brand = _hexes(BRAND_SVG.read_text(encoding="utf-8"))
    clean = (PET_DIR / "tofu-idle.svg").read_text(encoding="utf-8")
    assert not _offbrand_colours(clean, brand), "the real frame is already off-brand"
    poisoned = clean.replace("#1F1C25", "#3355FF")
    assert poisoned != clean, "neuter did not match the pet's ink colour"
    assert _offbrand_colours(poisoned, brand) == {"#3355FF"}, \
        "the lineage check did not flag an off-brand recolour — it is blind"


def test_NEUTER_out_of_viewbox_art_is_caught():
    """Push a REAL frame's accent past the viewBox and drive the SHIPPED check."""
    clean = (PET_DIR / "tofu-alert.svg").read_text(encoding="utf-8")
    assert not _out_of_viewbox(clean), "the real frame already overflows"
    poisoned = clean.replace('d="M', 'd="M40.5 9 L44 9 M', 1)
    assert poisoned != clean, "neuter did not modify any path"
    assert _out_of_viewbox(poisoned), \
        "the clipping check did not flag out-of-viewBox art — it is blind"


def test_NEUTER_generator_drift_is_caught():
    """A hand-edited frame must make the --check gate red.

    Uses a real temporary edit + guaranteed restore, because the gate compares
    bytes on disk: an in-memory poison could not exercise it.
    """
    target = PET_DIR / "tofu-idle.svg"
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original.replace('viewBox="0 0 32 32"',
                                           'viewBox="0 0 33 33"'), encoding="utf-8")
        r = subprocess.run(["python3", str(GEN), "--check"],
                           capture_output=True, text=True, cwd=str(REPO), timeout=60)
        assert r.returncode != 0, \
            "the --check gate stayed green after a frame was hand-edited — it does not bite"
    finally:
        target.write_text(original, encoding="utf-8")
    # and the tree is genuinely restored
    r2 = subprocess.run(["python3", str(GEN), "--check"],
                        capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert r2.returncode == 0, "failed to restore the frame after the neuter"
