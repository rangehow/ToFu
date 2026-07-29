#!/usr/bin/env python3
"""gen_tofu_pet.py — emit the 18 project-bar pet frames as brand-native SVG.

WHY THIS EXISTS (the drift argument)
────────────────────────────────────
The pet needs 18 frames (9 expressions + 4 walk + 3 groom + 2 scratch). Hand
writing 18 SVGs means the body geometry and the brand palette are copied 18
times, and the first palette tweak silently desynchronises them — the exact
"same declaration written twice, drift is invisible in the code" failure the
project has been bitten by before. So the BODY is defined ONCE here and every
frame is that body under a different pose spec.

BRAND SOURCE OF TRUTH
─────────────────────
The character is the Tofu mascot itself, not a generic cube. The cream/ink/blush
values below are the shipped brand mascot's own palette (static/icons/
tofu-welcome.svg — #FBF0D6 body, #E0C6A1 shaded face, #1F1C25 ink, #FAADA6
blush), so the pet and the logo are provably the same creature: a three-face
isometric cream block, rounded-rect eyes with a white specular dot, an ω mouth.

WHY THIS BLOCK HAS NO LIMBS (a measured decision, not laziness)
──────────────────────────────────────────────────────────────
Arm nubs were built and then REMOVED: rendered at the shipped 30px, a raised
nub reads as a MOUSE EAR and a lowered one as a panel glued to the side face —
both of which break the one thing that makes this the Tofu logo (its clean
cube silhouette). Expression is therefore carried by the face, by squash &
stretch, by tilt, and by accent marks. Feet stay, because they are tucked under
the body and read as feet.

WHY A BLOCK CAN ACT AT ALL (the objection this answers)
──────────────────────────────────────────────────────
An earlier tofu-block pet was dropped in favour of the kitten because a rigid
cube can't perform a walk cycle. The fix is not to re-draw the cube — it is to
animate the property a cube actually has: tofu is SOFT. A block that squashes on
the down-beat and stretches on the up-beat reads as alive at 30px, where
articulated limbs would only turn to mud.

USAGE
─────
    python3 static/icons/_gen/tofu-pet/gen_tofu_pet.py            # write frames
    python3 static/icons/_gen/tofu-pet/gen_tofu_pet.py --check    # verify in sync

Output: static/icons/pet/tofu/tofu-<frame>.svg (a PRODUCT asset dir).
This generator lives under _gen/ (the workbench); it is never served.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
OUT_DIR = REPO / 'static' / 'icons' / 'pet' / 'tofu'

# ── Brand palette. EVERY value below is measured present in the shipped mascot
# static/icons/tofu-welcome.svg. That file is a VTracer trace (45 distinct
# fills, ~20 near-blacks), so there is no clean "3 faces" tuple to copy — these
# are the representatives of each family, chosen by VISUAL MASS (summed path
# weight) rather than by eye:
#   #1F1C25 w=5299 → the outline (NOT #14121C w=865, one of the minor blacks)
#   #E0C6A1 w=5239 → the shaded face   ·  #FBF0D6 w=2255 → the lit body
#   #FEFDFA w=1040 → the top highlight ·  #E9D0AE/#F6E3CB → the mid family
# Re-derived from the SHIPPED logo (not the rejected a2-soft candidate) so the
# pet and the mascot beside it cannot drift apart. ──
TOP_A, TOP_B = '#FEFDFA', '#FBF0D6'      # top face: the lit plane
LEFT_A, LEFT_B = '#F6E3CB', '#E9D0AE'    # front face: the face-bearing plane
RIGHT_A, RIGHT_B = '#DFC4A3', '#E0C6A1'  # right face: the shaded plane
INK = '#1F1C25'                          # outline + eyes (dominant by mass)
BLUSH = '#FAADA6'
SHEEN = '#FFFFFF'

# ── Geometry (32×32 viewBox; feet land on y=29 so the CSS contact shadow at
# bottom:-1px sits exactly under them). The block is drawn FACING RIGHT; the
# engine mirrors it with scaleX(-1) when walking left. ──
FRONT_L, FRONT_R = 6.0, 22.0     # front face x-span
FRONT_T, FRONT_B = 11.0, 26.0    # front face y-span
DEPTH_X, DEPTH_Y = 5.0, 3.4      # isometric offset to the back-top-right
STROKE = 1.5
FOOT_Y = 29.0


def _g(gid, x1, y1, x2, y2, a, b):
    return (f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/>'
            f'</linearGradient>')


def _eyes(kind, cx_l, cx_r, cy):
    """The face's eyes. Rounded-rect + specular dot is the logo's own eye."""
    w, h, r = 3.1, 4.2, 1.55
    out = []
    if kind in ('open', 'wide', 'up'):
        hh = h * (1.22 if kind == 'wide' else 1.0)
        dy = -0.5 if kind == 'up' else 0.0
        for cx in (cx_l, cx_r):
            out.append(f'<rect x="{cx - w/2:.2f}" y="{cy - hh/2 + dy:.2f}" '
                       f'width="{w:.2f}" height="{hh:.2f}" rx="{r:.2f}" fill="{INK}"/>')
            out.append(f'<circle cx="{cx + 0.55:.2f}" cy="{cy - hh/2 + 1.15 + dy:.2f}" '
                       f'r="0.72" fill="{SHEEN}"/>')
    elif kind == 'happy':          # ^ ^ — upward arcs
        for cx in (cx_l, cx_r):
            out.append(f'<path d="M{cx - 1.8:.2f} {cy + 0.9:.2f} Q{cx:.2f} {cy - 1.9:.2f} '
                       f'{cx + 1.8:.2f} {cy + 0.9:.2f}" fill="none" stroke="{INK}" '
                       f'stroke-width="1.45" stroke-linecap="round"/>')
    elif kind in ('closed', 'sleepy'):   # ‿ ‿ — downward arcs (lids)
        for cx in (cx_l, cx_r):
            out.append(f'<path d="M{cx - 1.8:.2f} {cy - 0.6:.2f} Q{cx:.2f} {cy + 1.7:.2f} '
                       f'{cx + 1.8:.2f} {cy - 0.6:.2f}" fill="none" stroke="{INK}" '
                       f'stroke-width="1.45" stroke-linecap="round"/>')
    elif kind == 'sad':
        # Brow and lid MUST stay visibly apart: at the shipped 30px a brow only
        # 0.3u above the lid merged with it into one dark smudge and the frame
        # read as neutral-with-a-tear (measured). Smaller lid + a clear 1.6u gap,
        # inner end (toward the face centre) HIGHER — that angle is what makes it
        # sad rather than angry.
        for cx, sgn in ((cx_l, 1), (cx_r, -1)):
            out.append(f'<rect x="{cx - w/2:.2f}" y="{cy - 0.9:.2f}" width="{w:.2f}" '
                       f'height="{h * 0.62:.2f}" rx="{r:.2f}" fill="{INK}"/>')
            out.append(f'<path d="M{cx - 1.9 * sgn:.2f} {cy - 2.5:.2f} '
                       f'L{cx + 1.9 * sgn:.2f} {cy - 4.1:.2f}" stroke="{INK}" '
                       f'stroke-width="1.25" stroke-linecap="round"/>')
    return ''.join(out)


def _mouth(kind, cx, cy):
    if kind == 'omega':        # the logo's ω
        return (f'<path d="M{cx - 2.6:.2f} {cy:.2f} Q{cx - 1.3:.2f} {cy + 2.0:.2f} '
                f'{cx:.2f} {cy + 0.15:.2f} Q{cx + 1.3:.2f} {cy + 2.0:.2f} '
                f'{cx + 2.6:.2f} {cy:.2f}" fill="none" stroke="{INK}" '
                f'stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>')
    if kind == 'smile':
        return (f'<path d="M{cx - 2.0:.2f} {cy - 0.2:.2f} Q{cx:.2f} {cy + 1.8:.2f} '
                f'{cx + 2.0:.2f} {cy - 0.2:.2f}" fill="none" stroke="{INK}" '
                f'stroke-width="1.15" stroke-linecap="round"/>')
    if kind == 'open':
        return (f'<ellipse cx="{cx:.2f}" cy="{cy + 0.5:.2f}" rx="1.5" ry="1.85" fill="{INK}"/>')
    if kind == 'o':
        return (f'<ellipse cx="{cx:.2f}" cy="{cy + 0.4:.2f}" rx="1.05" ry="1.3" fill="{INK}"/>')
    if kind == 'frown':
        return (f'<path d="M{cx - 1.9:.2f} {cy + 1.2:.2f} Q{cx:.2f} {cy - 0.7:.2f} '
                f'{cx + 1.9:.2f} {cy + 1.2:.2f}" fill="none" stroke="{INK}" '
                f'stroke-width="1.15" stroke-linecap="round"/>')
    return (f'<path d="M{cx - 1.5:.2f} {cy + 0.4:.2f} L{cx + 1.5:.2f} {cy + 0.4:.2f}" '
            f'stroke="{INK}" stroke-width="1.15" stroke-linecap="round"/>')


def _body_path():
    """The three-face isometric block silhouette, corners softened.

    One path for the outline + three for the fills, so the perimeter stroke
    rounds the whole silhouette exactly like the brand cube does.
    """
    l, r, t, b = FRONT_L, FRONT_R, FRONT_T, FRONT_B
    dx, dy = DEPTH_X, DEPTH_Y
    front = f'M{l} {t} L{r} {t} L{r} {b} L{l} {b} Z'
    top = f'M{l} {t} L{l + dx} {t - dy} L{r + dx} {t - dy} L{r} {t} Z'
    right = f'M{r} {t} L{r + dx} {t - dy} L{r + dx} {b - dy} L{r} {b} Z'
    # Perimeter: front-left-bottom → around the back-top → down the right side.
    peri = (f'M{l} {b} L{l} {t} L{l + dx} {t - dy} L{r + dx} {t - dy} '
            f'L{r + dx} {b - dy} L{r} {b} Z')
    seams = f'M{l} {t} L{r} {t} L{r} {b} M{r} {t} L{r + dx} {t - dy}'
    return front, top, right, peri, seams


def _feet(pose, cy=FOOT_Y):
    """Two nub feet. `pose` shifts them into the walk-cycle keyposes.

    Drawn BEFORE the body fills and centred so their top third is OVERLAPPED by
    the block's bottom edge. A fully-exposed stroked ellipse reads as a hollow
    ring / wheel at 30px (measured); tucking it under the body turns the same
    shape into a foot emerging from the block.
    """
    offsets = {
        'stand':   (-4.0, 4.2, 0.0, 0.0),
        'contact': (-6.6, 6.4, 0.5, -0.4),
        'passing': (-1.6, 2.4, -0.3, 0.3),
        'up':      (-5.4, 5.2, -0.7, -0.7),
        'sit':     (-4.2, 4.4, 0.9, 0.9),
        'tip':     (-3.4, 3.6, 0.5, 0.5),
    }
    lx, rx, ly, ry = offsets.get(pose, offsets['stand'])
    cx = (FRONT_L + FRONT_R) / 2 + 1.0
    top = cy - 2.0   # ellipse centre; body bottom (FRONT_B) hides everything above it
    out = []
    for ox, oy in ((lx, ly), (rx, ry)):
        out.append(f'<ellipse cx="{cx + ox:.2f}" cy="{top + oy:.2f}" rx="2.9" ry="2.15" '
                   f'fill="{LEFT_B}" stroke="{INK}" stroke-width="{STROKE}" '
                   f'stroke-linejoin="round"/>')
    return ''.join(out)


def _extra(kind):
    """Per-pose accent marks (Zzz, sparkles, ? and !) — the art carries them.

    Every coordinate stays within x∈[1.5,30.5] y∈[1.5,30.5]: the earlier set ran
    to x=30.2 and clipped against the viewBox edge at the shipped 30px, showing
    as a cut-off stub rather than a mark.
    """
    if kind == 'zzz':
        return (f'<g fill="none" stroke="{INK}" stroke-width="1.1" stroke-linecap="round" '
                f'stroke-linejoin="round" opacity="0.85">'
                f'<path d="M23.4 9.4 L26.4 9.4 L23.4 12.3 L26.4 12.3"/>'
                f'<path d="M26.8 5.0 L29.1 5.0 L26.8 7.2 L29.1 7.2"/></g>')
    if kind == 'sparkle':
        pts = ((25.2, 7.2, 2.1), (28.6, 11.0, 1.5), (22.6, 3.8, 1.4))
        return ''.join(
            f'<path d="M{x} {y - s} L{x + s * 0.34:.2f} {y - s * 0.34:.2f} L{x + s} {y} '
            f'L{x + s * 0.34:.2f} {y + s * 0.34:.2f} L{x} {y + s} '
            f'L{x - s * 0.34:.2f} {y + s * 0.34:.2f} L{x - s} {y} '
            f'L{x - s * 0.34:.2f} {y - s * 0.34:.2f} Z" fill="#F6C86A"/>'
            for x, y, s in pts)
    if kind == 'think':
        return (f'<g fill="{INK}" opacity="0.8"><circle cx="24.9" cy="9.2" r="1.15"/>'
                f'<circle cx="27.3" cy="6.4" r="0.85"/>'
                f'<circle cx="29.0" cy="4.3" r="0.6"/></g>')
    if kind == 'alert':
        return (f'<g fill="{INK}"><rect x="24.9" y="4.4" width="1.9" height="4.6" rx="0.95"/>'
                f'<circle cx="25.85" cy="10.5" r="1.05"/></g>')
    if kind == 'shine':
        return (f'<path d="M24.6 8.0 L25.4 10.0 L27.4 10.8 L25.4 11.6 L24.6 13.6 '
                f'L23.8 11.6 L21.8 10.8 L23.8 10.0 Z" fill="{SHEEN}" opacity="0.92" '
                f'stroke="{INK}" stroke-width="0.5" stroke-linejoin="round"/>')
    if kind == 'tear':
        return (f'<path d="M8.4 20.4 Q7.0 23.2 8.4 24.1 Q9.8 23.2 8.4 20.4 Z" '
                f'fill="#8FC7E8" stroke="{INK}" stroke-width="0.55" stroke-linejoin="round"/>')
    return ''


def frame(name, *, squash=1.0, lean=0.0, lift=0.0, tilt=0.0, eyes='open', mouth='omega',
          feet='stand', blush=True, extra=''):
    """Render ONE frame. Every pose is this body under different parameters."""
    front, top, right, peri, seams = _body_path()
    cx = (FRONT_L + FRONT_R) / 2
    eye_y = FRONT_T + 6.2
    # Squash & stretch (and tilt) about the FOOT LINE — a soft block's own way
    # of moving, and the reason this character can act without limbs.
    stretch = 1.0 / squash if squash else 1.0
    body_tf = (f'translate({cx + lean:.2f} {FOOT_Y + lift:.2f}) '
               f'rotate({tilt:.2f}) '
               f'scale({stretch:.4f} {squash:.4f}) translate({-cx:.2f} {-FOOT_Y:.2f})')
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">',
        '<defs>',
        _g('t', 0, 0, 0.6, 1, TOP_A, TOP_B),
        _g('l', 0, 0, 0.3, 1, LEFT_A, LEFT_B),
        _g('r', 0, 0, 1, 0.7, RIGHT_A, RIGHT_B),
        '</defs>',
        f'<g transform="{body_tf}">',
        _feet(feet),
        f'<path d="{top}" fill="url(#t)"/>',
        f'<path d="{right}" fill="url(#r)"/>',
        f'<path d="{front}" fill="url(#l)"/>',
        # top-face sheen, quiet — the same cue the brand cube carries
        f'<path d="M{FRONT_L + 2.2} {FRONT_T - 0.9} L{FRONT_L + 6.4} {FRONT_T - 3.1}" '
        f'stroke="{SHEEN}" stroke-width="1.5" stroke-linecap="round" opacity="0.5"/>',
        f'<path d="{peri}" fill="none" stroke="{INK}" stroke-width="{STROKE}" '
        f'stroke-linejoin="round" stroke-linecap="round"/>',
        f'<path d="{seams}" fill="none" stroke="{INK}" stroke-width="{STROKE * 0.62:.2f}" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="0.75"/>',
    ]
    if blush:
        for bx in (FRONT_L + 2.6, FRONT_R - 2.6):
            parts.append(f'<ellipse cx="{bx:.2f}" cy="{eye_y + 3.6:.2f}" rx="1.9" ry="1.15" '
                         f'fill="{BLUSH}" opacity="0.62"/>')
    parts.append(_eyes(eyes, cx - 3.4, cx + 3.4, eye_y))
    parts.append(_mouth(mouth, cx, eye_y + 3.9))
    parts.append('</g>')
    parts.append(_extra(extra))
    parts.append('</svg>')
    return ''.join(parts) + '\n'


# ── The 18 frames the engine resolves (9 expressions + 4 walk + 3 groom + 2
# scratch). Names MUST match tofu-pet.js EXPRESSIONS / *_FRAMES. ──
FRAMES = {
    # resting expressions
    'idle':        dict(eyes='open', mouth='omega'),
    'happy':       dict(eyes='happy', mouth='smile', squash=0.97, lift=-0.6),
    'sleepy':      dict(eyes='sleepy', mouth='flat', squash=1.05, blush=True),
    'sleeping':    dict(eyes='closed', mouth='flat', squash=1.12, feet='sit', extra='zzz'),
    'thinking':    dict(eyes='up', mouth='flat', extra='think'),
    'surprised':   dict(eyes='wide', mouth='open', squash=0.9, lift=-1.2),
    'sad':         dict(eyes='sad', mouth='frown', squash=1.06, blush=False, extra='tear'),
    'celebrating': dict(eyes='happy', mouth='open', squash=0.92, lift=-2.0,
                        feet='tip', extra='sparkle'),
    'alert':       dict(eyes='wide', mouth='o', squash=0.96, lift=-0.5, extra='alert'),
    # Walk cycle: contact → down(squash) → passing → up(stretch). The amplitude
    # is deliberately large: at 30px a subtle 4% squash is invisible, so the
    # keyposes are pushed until the down-beat and up-beat differ at a glance.
    'walk1': dict(feet='contact', lean=0.9, tilt=2.5, squash=0.99,
                  eyes='open', mouth='smile'),
    'walk2': dict(feet='passing', squash=1.13, lift=0.8,
                  eyes='open', mouth='smile'),
    'walk3': dict(feet='contact', lean=-0.9, tilt=-2.5, squash=0.99,
                  eyes='open', mouth='smile'),
    'walk4': dict(feet='up', squash=0.88, lift=-2.0,
                  eyes='open', mouth='smile'),
    # Groom: with no limbs to lick a paw, the "tidy myself up" beat is a WOBBLE
    # — rock left, rock right, settle upright with a gleam. Tilt carries it;
    # squash alone was invisible between consecutive frames (measured).
    'groom1': dict(feet='sit', squash=1.06, tilt=-6.0, eyes='closed', mouth='smile'),
    'groom2': dict(feet='sit', squash=1.06, tilt=6.0, eyes='closed', mouth='omega'),
    'groom3': dict(feet='sit', squash=1.02, eyes='happy', mouth='smile', extra='shine'),
    # Scratch: a soft block's "stretch" is a whole-body stretch — the pose that
    # justifies the character. Tall+thin on the reach, wide+low on the settle.
    'scratch1': dict(feet='tip', squash=0.78, lift=-2.4, eyes='closed', mouth='o'),
    'scratch2': dict(feet='sit', squash=1.18, eyes='happy', mouth='smile'),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='verify on-disk frames match this generator (CI gate)')
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    drift = []
    for name, spec in FRAMES.items():
        svg = frame(name, **spec)
        path = OUT_DIR / f'tofu-{name}.svg'
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != svg:
                drift.append(path.name)
        else:
            path.write_text(svg, encoding='utf-8')

    if args.check:
        if drift:
            print(f'DRIFT: {len(drift)} frame(s) differ from the generator: '
                  f'{", ".join(sorted(drift))}', file=sys.stderr)
            print('Re-run: python3 static/icons/_gen/tofu-pet/gen_tofu_pet.py',
                  file=sys.stderr)
            return 1
        print(f'OK: all {len(FRAMES)} frames match the generator.')
        return 0

    print(f'Wrote {len(FRAMES)} frames → {OUT_DIR.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
