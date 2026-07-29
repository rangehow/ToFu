"""The pet's sun must not move when the pet turns, and its walk must read as a walk.

Three defects motivate this suite; each has a guard below.

1. LIGHT TELEPORTED ON TURN. The block is an isometric solid whose three faces
   and specular streak encode a sun in the upper-left, and the engine turned it
   around by mirroring the WHOLE sprite with scaleX(-1). Measured on the shipped
   art: the body band's luminance read left=117.0 / right=182.7 facing right and
   exactly the reverse facing left — the dark face and the highlight swapped
   sides on every turn. Meanwhile the cast shadow is derived from the REAL scene
   sun (tofu-pet.js `_applyLight` → `--pet-shadow-dx` from TofuScene.lightInfo)
   and did NOT flip, so the body and its own shadow claimed two different suns.
   tofu-scene.js states the invariant outright ("the light on the cat and the
   light in the field come from one sun"); the pet broke it half the time.

2. THE GAIT NEVER ALTERNATED. walk1 and walk3 were the SAME symmetric 'contact'
   pose, so no foot ever led — the legs scissored open and shut twice per cycle
   while the body rocked, which reads as shuffling in place or sliding BACKWARDS
   rather than stepping forward.

3. THE CYCLE RAN AT 6.7fps. 4 keyposes at 150ms is below the ~12fps floor where
   a cycle stops reading as motion and becomes a flicker between drawings.

The fix splits every frame into two coordinate spaces — [data-space="world"]
(the solid + its lighting, NEVER mirrored) and [data-space="char"] (feet, eyes,
mouth, blush — the only things that flip) — and the engine mirrors the second by
writing `--pet-face-flip`. The frames are inlined as live <svg> because a CSS
custom property cannot cross an <img> boundary, which is why the sprite could
not be lit by the scene at all before.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
GEN = REPO / "static" / "icons" / "_gen" / "tofu-pet" / "gen_tofu_pet.py"
FRAME_DIR = REPO / "static" / "icons" / "pet" / "tofu"
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
CSS = REPO / "static" / "styles.css"

# The engine's own frame list is the source of truth for which art must exist.
WALK_FRAMES = ["walk1", "walk2", "walk3", "walk4", "walk5", "walk6", "walk7", "walk8"]


def _frame(name: str) -> str:
    return (FRAME_DIR / f"tofu-{name}.svg").read_text(encoding="utf-8")


def _group(svg: str, space: str) -> str:
    """Concatenate every <g data-space="..."> body in the frame."""
    return "".join(
        re.findall(rf'<g data-space="{space}"[^>]*>(.*?)</g>', svg, re.S)
    )


def _js_code() -> str:
    """tofu-pet.js with comments stripped.

    Charter #24: a guard that scans source text must strip comments first, and
    must NOT carry a second hand-written stripper — this module's own comments
    quote the very idioms these guards forbid (e.g. `_img.src`) while explaining
    why they were removed, so a raw scan would fire on the explanation.
    """
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    return strip_comments(PET_JS.read_text(encoding="utf-8"), lang="js")


# ══════════════════════════════════════════════════════════════════════
#  1. THE LIGHT DOES NOT MOVE WHEN THE PET TURNS
# ══════════════════════════════════════════════════════════════════════

def test_world_space_group_holds_the_three_faces_and_the_specular():
    """The lit/shaded planes + the specular streak must live in WORLD space.

    These are the marks that encode where the sun is. If any of them lands in a
    character-space group it gets mirrored on every turn and the sun teleports.
    """
    svg = _frame("idle")
    world = _group(svg, "world")
    for gid, face in (("t", "top"), ("l", "front"), ("r", "right")):
        assert f'url(#{gid})' in world, (
            f"the {face} face (url(#{gid})) is not inside [data-space=\"world\"]. "
            "A face gradient encodes the sun direction; mirroring it makes the "
            "light jump across the body when the pet turns around."
        )
    assert "--pet-sheen" in world, (
        "the specular streak left the world group. It is the brightest single "
        "cue for where the sun is — mirrored, the highlight jumps corners."
    )


def test_world_space_group_is_never_mirrored():
    """No world-space group may consume the facing flip."""
    for name in ["idle", "walk1", "walk5", "sleeping", "celebrating"]:
        world = _group(_frame(name), "world")
        assert "--pet-face-flip" not in world, (
            f"tofu-{name}.svg mirrors its WORLD-space group. That is the exact "
            "regression this suite exists to prevent: the block's shading would "
            "flip with the pet while its cast shadow (driven by the real scene "
            "sun) would not, so the pet would contradict the diorama's one-sun "
            "invariant and read as a sticker."
        )


def test_character_space_holds_face_and_feet_and_is_mirrored():
    """Face + feet are character-space, and they DO flip."""
    svg = _frame("walk1")
    chars = re.findall(r'<g data-space="char"([^>]*)>(.*?)</g>', svg, re.S)
    assert chars, "no [data-space=\"char\"] group found in tofu-walk1.svg"
    for attrs, _body in chars:
        assert "--pet-face-flip" in attrs, (
            "a character-space group does not consume --pet-face-flip, so it "
            "would not turn with the pet."
        )
    joined = "".join(b for _a, b in chars)
    # The eyes are ink-filled rounded rects; the feet are stroked ellipses.
    assert "<rect" in joined, "the eyes are not in character space"
    assert "<ellipse" in joined, (
        "the FEET are not in character space. They carry the GAIT, and a stride "
        "has a leading foot — left in world space the pet keeps stepping to the "
        "right while travelling left, i.e. moonwalking."
    )


def test_mirroring_uses_css_transform_not_the_svg_attribute():
    """var() is a CSS feature; an SVG transform ATTRIBUTE would not resolve it.

    Also pins transform-box/transform-origin: CSS defaults transform-origin to
    50% 50%, which would silently add a half-viewBox offset on top of the
    translate sandwich and slide the face off the block.
    """
    svg = _frame("idle")
    m = re.search(r'<g data-space="char" style="([^"]+)"', svg)
    assert m, "character-space group does not carry a style= CSS transform"
    style = m.group(1)
    assert "transform-box:view-box" in style, (
        "transform-box:view-box missing — the CSS transform would resolve "
        "against the element's bounding box instead of the viewBox."
    )
    assert "transform-origin:0 0" in style, (
        "transform-origin is not pinned to 0 0; CSS defaults it to 50% 50%, "
        "which offsets the mirror by half the viewBox."
    )
    assert "scaleX(var(--pet-face-flip" in style, "the mirror is not var-driven"


def test_body_light_ordering_is_invariant_under_the_facing_flip():
    """PIXEL PROOF: flip the sprite's character space; the body's light must not move.

    Rasterizes the frame twice — once with --pet-face-flip:1, once with -1 —
    substituting the vars the way a browser resolves them (cairosvg supports
    neither var() nor the CSS transform property). Then measures which side of
    the block is darker in each render.

    Two-sided on purpose: the light must be IDENTICAL (the sun did not move) AND
    the two renders must DIFFER somewhere (the face actually turned). Asserting
    only the first would pass a sprite whose mirror silently does nothing.
    """
    cairosvg = pytest.importorskip("cairosvg", reason="pixel proof needs cairosvg")
    from PIL import Image

    def render(flip: int) -> Image.Image:
        svg = _frame("idle")
        # Resolve the character-space CSS transform into the SVG attribute form
        # the renderer understands (this is what the browser computes).
        svg = re.sub(
            r'<g data-space="char" style="[^"]*transform:translate\(([\d.]+)px,0\)'
            r'\s*scaleX\(var\(--pet-face-flip,\s*1\)\)\s*translate\(-([\d.]+)px,0\)"',
            lambda m: (
                f'<g data-space="char" transform="translate({m.group(1)},0) '
                f'scale({flip},1) translate(-{m.group(2)},0)"'
            ),
            svg,
        )
        # Resolve every remaining var(--x, fallback) to its fallback.
        svg = re.sub(r"var\(--[a-z-]+,\s*([^)]+)\)", r"\1", svg)
        png = cairosvg.svg2png(
            bytestring=svg.encode("utf-8"), output_width=200, output_height=200
        )
        return Image.open(io.BytesIO(png)).convert("RGB")

    def band_luminance(im: Image.Image, box: tuple[int, int, int, int]) -> float:
        px = list(im.crop(box).getdata())
        lit = [p for p in px if p != (255, 255, 255)]
        if not lit:
            return 0.0
        return sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in lit) / len(lit)

    # Sample the block's left and right thirds, above the feet.
    LEFT, RIGHT = (30, 70, 80, 140), (120, 70, 170, 140)
    a, b = render(1), render(-1)
    a_l, a_r = band_luminance(a, LEFT), band_luminance(a, RIGHT)
    b_l, b_r = band_luminance(b, LEFT), band_luminance(b, RIGHT)

    assert a_l > 0 and a_r > 0, "render produced no ink — the harness is broken"

    # Which side is darker must be the SAME in both renders.
    assert (a_l < a_r) == (b_l < b_r), (
        "THE SUN MOVED WHEN THE PET TURNED.\n"
        f"  facing right: left={a_l:.1f} right={a_r:.1f}\n"
        f"  facing left : left={b_l:.1f} right={b_r:.1f}\n"
        "The darker side swapped, which means a light-bearing mark (a face "
        "gradient or the specular) is inside a mirrored character-space group. "
        "Move it back into [data-space=\"world\"]."
    )
    # ...and the magnitudes must match closely, not merely keep their order.
    assert abs(a_l - b_l) < 2.0 and abs(a_r - b_r) < 2.0, (
        "the body's shading CHANGED under the flip "
        f"(left {a_l:.1f}->{b_l:.1f}, right {a_r:.1f}->{b_r:.1f}); "
        "world-space marks must render identically regardless of facing."
    )
    # Complement: the flip must actually do something, or this guard is vacuous.
    assert list(a.getdata()) != list(b.getdata()), (
        "the two renders are pixel-identical, so --pet-face-flip changes "
        "NOTHING — the pet would never appear to look where it walks. This "
        "guard's light-invariance assertion would then be vacuously true."
    )


# ══════════════════════════════════════════════════════════════════════
#  2. THE WALK CYCLE READS AS A WALK
# ══════════════════════════════════════════════════════════════════════

def _gait_offsets() -> dict[str, tuple[float, float]]:
    """Parse the generator's (near, far) foot-offset table."""
    src = GEN.read_text(encoding="utf-8")
    block = re.search(r"offsets = \{(.*?)\n    \}", src, re.S)
    assert block, "could not locate the foot-offset table in the generator"
    return {
        m.group(1): (float(m.group(2)), float(m.group(3)))
        for m in re.finditer(
            r"'([a-z_0-9]+)':\s*\(\s*(-?[\d.]+),\s*(-?[\d.]+)", block.group(1)
        )
    }


def _foot_positions(offsets: dict[str, tuple[float, float]], pose: str
                    ) -> tuple[float, float]:
    """Return (near_x, far_x) as the sprite ACTUALLY draws them.

    `_feet` places each foot at ``cx + offset``. Reading the authored offsets
    alone cannot answer "who leads": a magnitude says how far from centre a foot
    sits, while the SIGN decides whether it is in front or behind. Every gait
    assertion below is written against this resolved position.
    """
    FRONT_L, FRONT_R = 6.0, 22.0
    cx = (FRONT_L + FRONT_R) / 2 + 1.0
    near_off, far_off = offsets[pose]
    return cx + near_off, cx + far_off


def _leader(offsets: dict[str, tuple[float, float]], pose: str) -> str:
    """Which foot is AHEAD (larger x) in this pose — 'near' or 'far'."""
    near_x, far_x = _foot_positions(offsets, pose)
    return "near" if near_x > far_x else "far"


def test_walk_contact_beats_alternate_the_leading_foot():
    """A stride needs a leading foot, and it must SWAP between contact beats.

    Measured on RENDERED position (cx + offset), never on authored magnitude.
    That distinction IS the test. An earlier version of this guard asserted
    ``abs(near) > abs(far)`` and passed on a cycle where the far foot led all 8
    frames by ~12 units: with near always negative and far always positive, the
    feet only slid as a pair and the legs never scissored past each other —
    precisely the "shuffling, not stepping" the owner reported, waved through by
    a green guard. Magnitude is not position.
    """
    offsets = _gait_offsets()
    for pose in ("contact_a", "contact_b"):
        assert pose in offsets, (
            f"walk pose '{pose}' is gone. The contact beats must exist as a "
            "near-leads / far-leads PAIR or no foot ever leads."
        )
    lead_a, lead_b = _leader(offsets, "contact_a"), _leader(offsets, "contact_b")
    na, fa = _foot_positions(offsets, "contact_a")
    nb, fb = _foot_positions(offsets, "contact_b")
    assert lead_a != lead_b, (
        "THE LEADING FOOT NEVER SWAPS between the two contact beats.\n"
        f"  contact_a: near_x={na:.1f} far_x={fa:.1f} -> {lead_a} leads\n"
        f"  contact_b: near_x={nb:.1f} far_x={fb:.1f} -> {lead_b} leads\n"
        "Both beats put the same foot in front, so the feet slide as a pair and "
        "the pet reads as shuffling in place. The offsets' SIGNS must swap "
        "between beats, not merely their magnitudes."
    )


def test_walk_cycle_shows_both_feet_leading_across_the_stride():
    """Over the full cycle, BOTH feet must take a turn in front.

    The per-beat guard compares two poses; this asserts the property the eye
    actually judges — across every frame the ticker plays, the set of "who
    leads" contains both answers. A cycle that never shows the near foot in
    front is a shuffle however the poses are named.
    """
    js = _js_code()
    m = re.search(r"var WALK_FRAMES = \[(.*?)\];", js, re.S)
    assert m, "WALK_FRAMES not found in tofu-pet.js"
    frames = re.findall(r"'(walk\d+)'", m.group(1))
    src = GEN.read_text(encoding="utf-8")
    offsets = _gait_offsets()

    leaders, detail = set(), []
    for name in frames:
        fm = re.search(rf"'{name}': dict\(feet='([a-z_0-9]+)'", src)
        assert fm, f"{name} has no feet= pose in the generator's FRAMES table"
        pose = fm.group(1)
        near_x, far_x = _foot_positions(offsets, pose)
        who = _leader(offsets, pose)
        leaders.add(who)
        detail.append(f"  {name:7} ({pose:10}) near_x={near_x:5.1f} "
                      f"far_x={far_x:5.1f} -> {who} leads")

    assert leaders == {"near", "far"}, (
        "the walk cycle never alternates which foot is in front — only "
        f"{sorted(leaders)} ever leads across all {len(frames)} frames:\n"
        + "\n".join(detail)
        + "\nA stride must show each foot out front once per cycle."
    )


def test_swing_foot_gathers_under_the_body_on_the_passing_beat():
    """On a passing beat the swinging foot travels THROUGH the planted one.

    The passing pose is what makes a stride read as one leg overtaking the
    other rather than two feet easing toward each other: the feet must be
    CLOSER together at passing than at the contact beat it follows.
    """
    offsets = _gait_offsets()
    for contact, passing in (("contact_a", "passing_a"), ("contact_b", "passing_b")):
        cn, cf = _foot_positions(offsets, contact)
        pn, pf = _foot_positions(offsets, passing)
        assert abs(pn - pf) < abs(cn - cf), (
            f"{passing} is not a passing beat: the feet are {abs(pn - pf):.1f} "
            f"apart vs {abs(cn - cf):.1f} at {contact}. The swing foot must "
            "gather under the body, not stay splayed."
        )


def test_walk_cycle_frames_all_exist_and_are_distinct():
    """Every frame the engine plays must exist, and no two may be identical."""
    js = _js_code()
    m = re.search(r"var WALK_FRAMES = \[(.*?)\];", js, re.S)
    assert m, "WALK_FRAMES not found in tofu-pet.js"
    listed = re.findall(r"'(walk\d+)'", m.group(1))
    assert listed == WALK_FRAMES, (
        f"engine walk list {listed} != expected {WALK_FRAMES}"
    )
    bodies = {}
    for name in listed:
        p = FRAME_DIR / f"tofu-{name}.svg"
        assert p.exists(), f"engine plays {name} but tofu-{name}.svg is missing"
        bodies[name] = p.read_text(encoding="utf-8")
    for i, a in enumerate(listed):
        for b in listed[i + 1:]:
            assert bodies[a] != bodies[b], (
                f"tofu-{a}.svg and tofu-{b}.svg are byte-identical — a cycle "
                "with duplicate keyposes drops real frames and stutters."
            )


def test_walk_cycle_clears_the_twelve_fps_floor():
    """8 keyposes at 75ms = 13.3fps over the same 600ms stride.

    Below roughly 12fps a cycle stops reading as motion and starts reading as a
    flicker between drawings — the "animations aren't smooth" half of the
    report. The stride DURATION must stay ~600ms so the pet's travel speed and
    the CSS bob (tofuPetWalk .3s, two bounces per cycle) stay in sync.
    """
    js = _js_code()
    m = re.search(r"var WALK_FRAME_MS = (\d+)", js)
    assert m, "WALK_FRAME_MS not found"
    per = int(m.group(1))
    fps = 1000.0 / per
    assert fps >= 12.0, (
        f"walk cycle runs at {fps:.1f}fps ({per}ms/frame), below the ~12fps "
        "floor where frame-by-frame motion turns into a visible flicker."
    )
    stride = per * len(WALK_FRAMES)
    assert 520 <= stride <= 700, (
        f"stride is {stride}ms; it must stay ~600ms to remain in sync with the "
        "CSS travel bob (tofuPetWalk .3s = two bounces per cycle)."
    )


def test_entering_a_walk_asserts_facing():
    """A walk must set its facing, not inherit whatever the last state left.

    'gaze' deliberately flips facing every GAZE_TURN_MS while standing still, so
    a walk entered straight out of a glance could set off with the body facing
    one way and travel going the other.
    """
    js = _js_code()
    m = re.search(r"if \(state === 'walk'\) \{(.*?)\} else if", js, re.S)
    assert m, "could not isolate the _enter('walk') branch"
    assert "_face(" in m.group(1), (
        "entering 'walk' no longer asserts facing, so a walk can start with the "
        "body facing away from its direction of travel (the pet appears to walk "
        "backwards). This is reachable in one hop: the 'gaze' state flips facing "
        "on a timer, then _pickNext() can enter 'walk'."
    )


# ══════════════════════════════════════════════════════════════════════
#  3. THE SPRITE IS REACHABLE BY THE SCENE'S LIGHT
# ══════════════════════════════════════════════════════════════════════

def test_frames_are_inlined_not_pointed_at_with_img():
    """A custom property cannot cross an <img> boundary.

    Behind <img> the frame is an isolated document, so every var() collapses to
    its fallback and the pet is welded to a frozen sun. The engine must build a
    live <svg> in the DOM instead.

    Scans CODE ONLY (charter #24): the module's comments legitimately quote the
    old `_img.src` idiom while explaining why it was removed, and a raw substring
    scan would fire on the explanation rather than on real code.
    """
    js = _js_code()
    assert "_img.src" not in js, (
        "the engine still assigns _img.src. Inside an <img> the sprite cannot "
        "see --pet-front-a/--pet-face-flip, so neither the live lighting nor "
        "the character-space mirror can work."
    )
    assert "querySelector('svg')" in js, (
        "frames are no longer parsed into a live <svg> element"
    )
    assert "_frameNode" in js, "the parsed-frame cache is gone"


def test_frame_swap_does_not_reparse_markup_every_frame():
    """Frame changes must toggle visibility, not rebuild the subtree.

    Writing innerHTML per frame would reconstruct the sprite's DOM 13x/second,
    which is exactly the per-frame churn that shows up as stutter.
    """
    js = _js_code()
    m = re.search(r"function _paintFrame\(name\) \{(.*?)\n  \}", js, re.S)
    assert m, "_paintFrame not found"
    body = m.group(1)
    assert "innerHTML" not in body, (
        "_paintFrame writes innerHTML, so every walk frame reparses ~2KB of "
        "markup and rebuilds the sprite's DOM — visible as stutter."
    )
    assert "display" in body, "_paintFrame no longer toggles visibility"


def test_live_lighting_drives_the_side_faces_from_the_same_sun_as_the_shadow():
    """Body shading and cast shadow must be derived from ONE number."""
    js = _js_code()
    m = re.search(r"function _applyLight\(\) \{(.*?)\n  \}", js, re.S)
    assert m, "_applyLight not found"
    body = m.group(1)
    assert "--pet-shadow-dx" in body, "the cast shadow is no longer sun-driven"
    for var in ("--pet-front-a", "--pet-front-b", "--pet-right-a", "--pet-right-b"):
        assert var in body, (
            f"{var} is not written by _applyLight, so the block's own faces are "
            "frozen while its cast shadow tracks the real sun — the two would "
            "again claim different suns."
        )
    assert "rel" in body, "the shadow/shading no longer share the sun offset"


def test_relighting_stays_inside_the_brand_cream_family():
    """Re-lighting may only ever produce the logo's own cream tones or a blend.

    The pet must not be lit into a colour the mascot does not contain.
    """
    js = _js_code()
    lit = re.search(r"var FACE_LIT = \['(#[0-9A-Fa-f]{6})', '(#[0-9A-Fa-f]{6})'\]", js)
    shade = re.search(r"var FACE_SHADE = \['(#[0-9A-Fa-f]{6})', '(#[0-9A-Fa-f]{6})'\]", js)
    assert lit and shade, "the face-colour families are gone from the engine"
    gen = GEN.read_text(encoding="utf-8")
    for hexv in lit.groups() + shade.groups():
        assert hexv.upper() in gen.upper(), (
            f"{hexv} is not one of the generator's authored brand tones; the "
            "pet would be lit off-palette."
        )


def test_authored_fallbacks_keep_a_scene_less_pet_unchanged():
    """Every var() must carry the authored brand hex as its fallback.

    A non-tofu theme, a scene-less pet, or any renderer that ignores custom
    properties must still get exactly the palette the generator authored.
    """
    svg = _frame("idle")
    for var in ("--pet-top-a", "--pet-front-a", "--pet-right-a", "--pet-sheen"):
        m = re.search(rf"var\({re.escape(var)},\s*([^)]+)\)", svg)
        assert m, f"{var} has no fallback value"
        assert m.group(1).strip(), f"{var}'s fallback is empty"


def test_generated_frames_match_the_generator():
    """The art is generated; on-disk drift means someone hand-edited a frame."""
    r = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert r.returncode == 0, (
        "on-disk frames drifted from gen_tofu_pet.py — hand-editing a frame "
        f"desynchronises the body geometry and palette.\n{r.stdout}\n{r.stderr}"
    )


def test_css_declares_the_flip_default_and_no_wrapper_mirror():
    """--pet-face-flip must default to 1, and no wrapper may mirror the sprite."""
    css = CSS.read_text(encoding="utf-8")
    assert "--pet-face-flip:1" in css, (
        "--pet-face-flip has no default, so a pet that has not turned yet would "
        "resolve the var to nothing."
    )
    assert ".tofu-pet-facing" not in css, (
        "the .tofu-pet-facing wrapper is back. Mirroring a wrapper mirrors the "
        "block's lighting with it — the defect this suite guards."
    )
    m = re.search(r"\.tofu-pet \.tofu-pet-img\{([^}]*)\}", css)
    assert m, "could not isolate the .tofu-pet-img rule"
    assert "scaleX" not in m.group(1), (
        "the frame layer mirrors the whole sprite again (scaleX on "
        ".tofu-pet-img), which drags the block's shading around with it."
    )
