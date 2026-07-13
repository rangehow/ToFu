"""Pet-frame processing pipeline + proof harness.

Turns the raw generated candidate frames (RGB, baked opaque checker bg) into
clean transparent, size-normalized sprites that share ONE bounding box so the
pet does not jitter between poses, and renders proofs at the actual on-screen
size (30px box, ~60px at 2x DPR).

Two normalization fixes vs the first cut:
  1. SIZE is normalized on the BODY's pixel AREA (largest connected component),
     not raw content height. A curled/lying cat is short-and-wide; scaling it to
     a fixed HEIGHT balloons it. Matching body AREA keeps a napping cat the same
     "size of animal" as a standing one. Floating accents (z z z / sparkles /
     '?' / '!') are separate components → excluded from the metric.
  2. HORIZONTAL anchor is the BODY CENTROID x, not the whole-frame bbox center.
     The tail + reaching legs push a walk frame's bbox off-center from idle;
     anchoring on the torso mass keeps idle<->walk from sliding sideways.
  Vertical anchor stays the body's bottom (feet on one baseline).

Standalone debug script — not imported by the app.
"""
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.ndimage import binary_erosion

CAND = 'static/icons/pet/_candidates/'

# Canonical pose -> candidate filename.
# 2026-07-10: switched to the refined PIXEL-ART chibi calico set (px-*),
# edit-chained off the owner-approved base cand-pixel-idle.png. Replaces the
# earlier soft-vector cand-chibi-* set (owner: "pet designs too simple").
POSES = {
    'idle': 'cand-pixel-idle.png', 'happy': 'px-happy.png',
    'sleepy': 'px-sleepy.png', 'sleeping': 'px-sleeping.png',
    'thinking': 'px-thinking.png', 'surprised': 'px-surprised.png',
    'sad': 'px-sad.png', 'celebrating': 'px-celebrating.png',
    'alert': 'px-alert.png',
    'walk1': 'px-walk1.png', 'walk2': 'px-walk2.png',
    'walk3': 'px-walk3.png', 'walk4': 'px-walk4.png',
    'groom1': 'px-groom1.png', 'groom2': 'px-groom2.png',
    'groom3': 'px-groom3.png',
    'scratch1': 'px-scratch1.png', 'scratch2': 'px-scratch2.png',
}
SHEET_ORDER = ['idle', 'happy', 'sleepy', 'sleeping', 'thinking', 'surprised',
               'sad', 'celebrating', 'alert', 'walk1', 'walk2', 'walk3',
               'walk4', 'groom1', 'groom2', 'groom3', 'scratch1', 'scratch2']

# The wander FSM (tofu-pet.js) treats the sprite's NATIVE orientation as
# FACING RIGHT: `W.x += W.dir * speed` with dir=+1 moving right shows the
# un-mirrored frame, and `_face(-1)` applies scaleX(-1) to face left. The
# side-profile walk candidates were generated facing LEFT, so they must be
# horizontally flipped to face right — otherwise the cat moonwalks (moving
# right shows a left-facing cat; moving left mirrors it to face right). Only
# the profile walk frames have a handedness; the front-facing poses are
# roughly symmetric and are left alone.
FLIP_TO_FACE_RIGHT = {'walk1', 'walk2', 'walk3', 'walk4'}


def cut_alpha(path, chroma_thresh=22):
    """Remove the achromatic checker/white background via border-connected fill.

    The cat is fully enclosed by a chromatic (dark-brown) outline, so a fill
    that only crosses LOW-CHROMA pixels reaches the whole background but stops
    at the outline, never eating the cream interior.
    """
    rgb = np.asarray(Image.open(path).convert('RGB')).astype(np.int16)
    chroma = rgb.max(2) - rgb.min(2)
    bglike = chroma < chroma_thresh
    lbl, _ = ndimage.label(bglike)
    border = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1])
    border.discard(0)
    bg = np.isin(lbl, list(border))
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    inner = binary_erosion(alpha > 0, iterations=1)
    soft = alpha.copy()
    soft[(alpha > 0) & (~inner)] = 160  # 1px feather
    return Image.fromarray(np.dstack([rgb.astype(np.uint8), soft]), 'RGBA')


def body_stats(im):
    """Return (body_area, centroid_x, body_bottom_y, full_bbox) for the LARGEST
    connected alpha component (the cat body), ignoring floating accents."""
    a = np.asarray(im)[..., 3] > 24
    lbl, n = ndimage.label(a)
    if n == 0:
        return 0, im.size[0] / 2, im.size[1], (0, 0, *im.size)
    sizes = ndimage.sum(np.ones_like(lbl), lbl, range(1, n + 1))
    body = (np.argmax(sizes) + 1)
    mask = lbl == body
    ys, xs = np.where(mask)
    cx = xs.mean()
    bottom = ys.max()
    fa = np.asarray(im)[..., 3] > 24
    fys, fxs = np.where(fa)
    fbbox = (fxs.min(), fys.min(), fxs.max() + 1, fys.max() + 1)
    return len(xs), cx, bottom, fbbox


def normalize(canvas=256, idle_h_frac=0.82, baseline_pad=6):
    """Body-area-normalized, torso-centroid-x + baseline anchored frames."""
    raw = {p: cut_alpha(CAND + f) for p, f in POSES.items()}
    # Flip the left-facing profile walk frames to the FSM's native right-facing
    # orientation (see FLIP_TO_FACE_RIGHT). Done post-alpha-cut, pre-stats, so
    # the centroid/baseline anchor is measured on the final orientation.
    for p in FLIP_TO_FACE_RIGHT:
        if p in raw:
            raw[p] = raw[p].transpose(Image.FLIP_LEFT_RIGHT)
    stats = {p: body_stats(im) for p, im in raw.items()}

    # Reference size from idle: scale idle so its BODY height ~ idle_h_frac,
    # then use idle's resulting body AREA as the target area for every pose.
    ib_area, _, _, (ix0, iy0, ix1, iy1) = stats['idle']
    idle_body_h = iy1 - iy0
    idle_scale = (canvas * idle_h_frac) / idle_body_h
    ref_area = ib_area * idle_scale ** 2

    out = {}
    for p, im in raw.items():
        area, cx, bottom, _ = stats[p]
        scale = (ref_area / area) ** 0.5 if area else 1.0
        w, h = im.size
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        im2 = im.resize((nw, nh), Image.LANCZOS)
        cx2, bottom2 = cx * scale, bottom * scale
        cv = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
        x = round(canvas / 2 - cx2)                 # body centroid -> center
        y = round(canvas - baseline_pad - bottom2)  # body bottom -> baseline
        cv.alpha_composite(im2, (x, y))
        out[p] = cv
    return out, ref_area


def cell_img(im, cell, bg):
    c = Image.new('RGBA', (cell, cell), bg)
    c.alpha_composite(im.resize((cell, cell), Image.LANCZOS))
    return c


def sheet(fm, order, cell, bg, cols, pad=6, label=False):
    rows = (len(order) + cols - 1) // cols
    W = cols * (cell + pad) + pad
    H = rows * (cell + pad) + pad
    img = Image.new('RGBA', (W, H), bg)
    for i, k in enumerate(order):
        r, c = divmod(i, cols)
        img.alpha_composite(cell_img(fm[k], cell, bg),
                            (pad + c * (cell + pad), pad + r * (cell + pad)))
    return img


if __name__ == '__main__':
    barbg = (214, 224, 205, 255)
    fm, ref_area = normalize()

    # Full 18-frame sheet at 60px (true 2x-DPR on-screen size).
    sheet(fm, SHEET_ORDER, 60, barbg, cols=9).save(CAND + 'sheet_all18_60.png')
    # Bigger sheet for detail inspection.
    sheet(fm, SHEET_ORDER, 120, barbg, cols=6).save(CAND + 'sheet_all18_120.png')

    # Size-parity proof: sleeping (curled) vs idle (standing) MUST look like the
    # same size of animal now.
    sheet(fm, ['idle', 'sleeping', 'sleepy', 'groom2'], 60, barbg, cols=4).save(
        CAND + 'proof_sizeparity_60.png')
    # Jitter: idle vs walk1 vs walk3 — torso-centroid anchored, no sideways slide.
    sheet(fm, ['idle', 'walk1', 'walk3'], 60, barbg, cols=3).save(
        CAND + 'proof_jitter2_60.png')

    # Scratch cycle continuity (the re-rolled scratch2 vs scratch1).
    sheet(fm, ['scratch1', 'scratch2', 'scratch1', 'scratch2'], 96, barbg, cols=4).save(
        CAND + 'proof_scratch_96.png')

    for k, im in fm.items():
        im.save(CAND + 'norm-' + k + '.png')

    # ── EMIT final sprites into the on-disk oneko/ dir (in-place replace, so
    # tofu-pet.js _frameUrl is unchanged). Only runs with --emit. Each frame is
    # the normalized 256px RGBA sprite downscaled to _EMIT_PX; the shared
    # bottom-baseline + centroid anchor is preserved so the pet never jitters.
    import sys as _sys
    if '--emit' in _sys.argv:
        import os
        _EMIT_PX = 128
        outdir = 'static/icons/pet/oneko/'
        for pose, im in fm.items():
            im.resize((_EMIT_PX, _EMIT_PX), Image.LANCZOS).save(
                outdir + 'oneko-' + pose + '.png')
        # Name-resolution proof: exactly the frames the FSM requests must exist.
        fsm = (['idle', 'happy', 'sleepy', 'sleeping', 'thinking', 'surprised',
                'sad', 'celebrating', 'alert'] +
               ['walk1', 'walk2', 'walk3', 'walk4'] +
               ['groom1', 'groom2', 'groom3'] + ['scratch1', 'scratch2'])
        print('\nname-resolution check (FSM frame -> oneko-<frame>.png):')
        allok = True
        for f in fsm:
            p = outdir + 'oneko-' + f + '.png'
            ok = os.path.exists(p)
            allok = allok and ok
            im2 = Image.open(p)
            print(f'  {"OK " if ok else "MISS"} {f:12s} {im2.size} {im2.mode}')
        print('ALL 18 RESOLVE:', allok)

    # Report body-area consistency (should be ~1.0 across poses).
    print('ref_body_area(px^2 in 256 canvas) =', round(ref_area))
    print('pose            bodyArea/ref   centroidX(256)')
    for p in SHEET_ORDER:
        a, cx, bottom, _ = body_stats(fm[p])
        print(f'  {p:12s}  {a/ref_area:6.2f}        {cx:6.1f}')
