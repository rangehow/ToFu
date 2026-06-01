#!/usr/bin/env python3
"""Convert logo.png (kawaii pixel tofu cube) to SVG at multiple sizes.

Analyzes the PNG via OpenCV contour detection and color sampling,
then emits hand-tuned SVG paths matching the original pixel art.

Usage:
    python3 scripts/convert_logo_to_svg.py

Outputs:
    static/icons/tofu-pixel.svg   — 512×512 full logo (for apple-touch-icon, og:image)
    static/icons/tofu-favicon.svg — 32×32  favicon (for <link rel="icon">)
"""

import os
import sys

import cv2
import numpy as np
from PIL import Image

# ── Paths ──────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PNG = os.path.join(PROJECT_ROOT, 'static', 'icons', 'logo.png')
OUT_512 = os.path.join(PROJECT_ROOT, 'static', 'icons', 'tofu-pixel.svg')
OUT_32 = os.path.join(PROJECT_ROOT, 'static', 'icons', 'tofu-favicon.svg')


def analyze_png(path: str) -> dict:
    """Extract key geometry and colors from the logo PNG."""
    img = Image.open(path).convert('RGBA')
    arr = np.array(img)
    gray = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # ── 1. Find cube outline vertices via contour approximation ──
    _, outline_bin = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(outline_bin, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    main = max(contours, key=cv2.contourArea)
    eps = 0.02 * cv2.arcLength(main, True)
    approx = cv2.approxPolyDP(main, eps, True)
    verts = [(int(pt[0][0]), int(pt[0][1])) for pt in approx]
    # Expected: 6 vertices → T, L, BL, B, BR, R (clockwise from top)
    print(f"[analyze] {len(verts)} outline vertices: {verts}")

    # ── 2. Compute inner center C (intersection of T→B and L→BR) ──
    # Vertices (from OpenCV, 1024-space):
    #   T=(491,148), L=(205,309), BL=(196,676), B=(551,871), BR=(829,713), R=(838,335)
    T, L, BL, B, BR, R = verts[:6]

    # Parametric intersection of line L→BR and line T→B
    # L + t*(BR-L) = T + s*(B-T)
    dx1, dy1 = BR[0] - L[0], BR[1] - L[1]
    dx2, dy2 = B[0] - T[0], B[1] - T[1]
    det = dx1 * (-dy2) - (-dx2) * dy1
    if abs(det) > 1e-6:
        t = ((T[0] - L[0]) * (-dy2) - (-dx2) * (T[1] - L[1])) / det
    else:
        t = 0.5
    C = (int(L[0] + t * dx1), int(L[1] + t * dy1))
    print(f"[analyze] Center vertex C: {C}")

    # ── 3. Sample face colors ──
    def sample(y, x):
        r, g, b = arr[y, x, :3]
        return f"#{r:02x}{g:02x}{b:02x}"

    colors = {
        'top_light': sample((T[1] + C[1]) // 2, (T[0] + L[0]) // 2),
        'top_dark': sample((L[1] + C[1]) // 2, (L[0] + C[0]) // 2),
        'front_light': sample((L[1] + BL[1]) // 2, (L[0] + BL[0]) // 2 + 40),
        'front_dark': sample((BL[1] + B[1]) // 2, (BL[0] + B[0]) // 2),
        'right_light': sample((C[1] + R[1]) // 2, (C[0] + R[0]) // 2),
        'right_dark': sample((B[1] + BR[1]) // 2, (B[0] + BR[0]) // 2),
    }
    print(f"[analyze] Colors: {colors}")

    # ── 4. Detect eye bounding boxes ──
    # Left eye: ~x∈[250,334], y∈[465,535]
    # Right eye: ~x∈[418,501], y∈[540,640]
    def find_dark_blob(y_min, y_max, x_min, x_max, thresh=60):
        region = gray[y_min:y_max, x_min:x_max]
        dark = (region < thresh).astype(np.uint8) * 255
        cs, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            return None
        biggest = max(cs, key=cv2.contourArea)
        bx, by, bw, bh = cv2.boundingRect(biggest)
        return (x_min + bx, y_min + by, bw, bh)

    left_eye = find_dark_blob(440, 560, 220, 360)
    right_eye = find_dark_blob(510, 670, 390, 530)
    print(f"[analyze] Left eye bbox: {left_eye}")
    print(f"[analyze] Right eye bbox: {right_eye}")

    # ── 5. Detect blush marks ──
    r_ch, g_ch, b_ch = arr[:, :, 0].astype(int), arr[:, :, 1].astype(int), arr[:, :, 2].astype(int)
    interior = np.zeros(gray.shape, dtype=np.uint8)
    cv2.drawContours(interior, [main], -1, 255, -1)
    blush = ((r_ch - g_ch) > 30) & ((r_ch - b_ch) > 20) & (r_ch > 180) & (interior > 0)
    blush_cs, _ = cv2.findContours(blush.astype(np.uint8) * 255,
                                    cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blush_sorted = sorted(blush_cs, key=cv2.contourArea, reverse=True)
    blushes = []
    for bc in blush_sorted[:2]:
        bx, by, bw, bh = cv2.boundingRect(bc)
        cx, cy = bx + bw // 2, by + bh // 2
        blushes.append((cx, cy, bw, bh, sample(cy, cx)))
    print(f"[analyze] Blush marks: {blushes}")

    # ── 6. Measure outline thickness ──
    outline_w = 0
    for x in range(L[0] - 20, L[0] + 40):
        if gray[L[1] + 50, x] < 80:
            if outline_w == 0:
                outline_w = 1
            else:
                outline_w += 1
        elif outline_w > 0:
            break
    print(f"[analyze] Outline thickness: ~{outline_w}px")

    return {
        'size': (w, h),
        'vertices': {'T': T, 'L': L, 'BL': BL, 'B': B, 'BR': BR, 'R': R, 'C': C},
        'colors': colors,
        'left_eye': left_eye,
        'right_eye': right_eye,
        'blushes': blushes,
        'outline_width': outline_w,
    }


def gen_svg_512(info: dict) -> str:
    """Generate the 512×512 SVG from analysis data."""
    v = info['vertices']
    s = 512 / info['size'][0]  # scale factor (1024 → 512)
    c = info['colors']

    def sc(pt):
        """Scale a point from 1024-space to 512-space."""
        return (round(pt[0] * s, 1), round(pt[1] * s, 1))

    T, L, BL, B, BR, R, C = [sc(v[k]) for k in ('T', 'L', 'BL', 'B', 'BR', 'R', 'C')]

    # Eye geometry (scaled)
    le = info['left_eye']
    re = info['right_eye']
    if le:
        le = (round(le[0]*s, 1), round(le[1]*s, 1), round(le[2]*s, 1), round(le[3]*s, 1))
    if re:
        re = (round(re[0]*s, 1), round(re[1]*s, 1), round(re[2]*s, 1), round(re[3]*s, 1))

    # Blush geometry (scaled)
    bl = []
    for cx, cy, bw, bh, col in info['blushes']:
        bl.append((round(cx*s, 1), round(cy*s, 1), round(bw*s*0.5, 1), round(bh*s*0.5, 1), col))

    # Outline stroke width
    ow = round(info['outline_width'] * s, 1)

    # Smile: approximate curve between/below the two eyes
    if le and re:
        smile_x1 = le[0] + le[2] * 0.3
        smile_x2 = re[0] + re[2] * 0.7
        smile_y = le[1] + le[3] + 15 * s
        smile_cy = smile_y + 14 * s
        smile_mid_x = (smile_x1 + smile_x2) / 2
    else:
        smile_x1, smile_x2, smile_y, smile_cy, smile_mid_x = 170, 250, 340, 355, 210

    # Highlight on top face: diagonal white stripe
    hl_x1 = round((T[0] + L[0]) / 2 - 20, 1)
    hl_y1 = round((T[1] + L[1]) / 2 - 8, 1)
    hl_x2 = hl_x1 + 55
    hl_y2 = hl_y1 - 18
    hl_x3 = hl_x2 + 18
    hl_y3 = hl_y2 + 8
    hl_x4 = hl_x1 + 18
    hl_y4 = hl_y1 + 8

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" fill="none">
  <!--
    Tofu (豆腐) — kawaii pixel-art isometric cube logo
    Auto-generated from logo.png by scripts/convert_logo_to_svg.py
  -->
  <defs>
    <linearGradient id="face-top" x1="0" y1="0" x2="0.6" y2="1">
      <stop offset="0%" stop-color="{c['top_light']}"/>
      <stop offset="100%" stop-color="{c['top_dark']}"/>
    </linearGradient>
    <linearGradient id="face-front" x1="0" y1="0" x2="0.3" y2="1">
      <stop offset="0%" stop-color="{c['front_light']}"/>
      <stop offset="100%" stop-color="{c['front_dark']}"/>
    </linearGradient>
    <linearGradient id="face-right" x1="0" y1="0" x2="1" y2="0.8">
      <stop offset="0%" stop-color="{c['right_light']}"/>
      <stop offset="100%" stop-color="{c['right_dark']}"/>
    </linearGradient>
  </defs>

  <!-- ══════ Cube faces (fills) ══════ -->
  <path d="M{T[0]} {T[1]} L{L[0]} {L[1]} L{C[0]} {C[1]} L{R[0]} {R[1]} Z"
        fill="url(#face-top)"/>
  <path d="M{L[0]} {L[1]} L{BL[0]} {BL[1]} L{B[0]} {B[1]} L{C[0]} {C[1]} Z"
        fill="url(#face-front)"/>
  <path d="M{C[0]} {C[1]} L{B[0]} {B[1]} L{BR[0]} {BR[1]} L{R[0]} {R[1]} Z"
        fill="url(#face-right)"/>

  <!-- ══════ Thick pixel-art outline ══════ -->
  <path d="M{T[0]} {T[1]} L{L[0]} {L[1]} L{BL[0]} {BL[1]} L{B[0]} {B[1]} L{BR[0]} {BR[1]} L{R[0]} {R[1]} Z"
        stroke="#1a1520" stroke-width="{ow}" stroke-linejoin="round" fill="none"/>
  <!-- Inner edges from C -->
  <line x1="{C[0]}" y1="{C[1]}" x2="{T[0]}" y2="{T[1]}"
        stroke="#1a1520" stroke-width="{round(ow*0.5,1)}" opacity="0.12"/>
  <line x1="{C[0]}" y1="{C[1]}" x2="{B[0]}" y2="{B[1]}"
        stroke="#1a1520" stroke-width="{round(ow*0.5,1)}" opacity="0.12"/>
  <line x1="{C[0]}" y1="{C[1]}" x2="{R[0]}" y2="{R[1]}"
        stroke="#1a1520" stroke-width="{round(ow*0.5,1)}" opacity="0.12"/>

  <!-- ══════ Top face highlight ══════ -->
  <path d="M{hl_x1} {hl_y1} L{hl_x2} {hl_y2} L{hl_x3} {hl_y3} L{hl_x4} {hl_y4} Z"
        fill="white" opacity="0.45"/>'''

    # ── Eyes ──
    if le:
        # Slight roundrect for pixel-art feel
        svg += f'''

  <!-- Left eye -->
  <rect x="{le[0]}" y="{le[1]}" width="{le[2]}" height="{le[3]}" rx="2" fill="#1a1520"/>
  <rect x="{round(le[0]+le[2]*0.55,1)}" y="{round(le[1]+le[3]*0.08,1)}"
        width="{round(le[2]*0.35,1)}" height="{round(le[3]*0.35,1)}" rx="1.5" fill="white" opacity="0.92"/>
  <rect x="{round(le[0]+le[2]*0.1,1)}" y="{round(le[1]+le[3]*0.55,1)}"
        width="{round(le[2]*0.22,1)}" height="{round(le[3]*0.22,1)}" rx="1" fill="white" opacity="0.5"/>'''

    if re:
        svg += f'''

  <!-- Right eye -->
  <rect x="{re[0]}" y="{re[1]}" width="{re[2]}" height="{re[3]}" rx="2" fill="#1a1520"/>
  <rect x="{round(re[0]+re[2]*0.55,1)}" y="{round(re[1]+re[3]*0.08,1)}"
        width="{round(re[2]*0.35,1)}" height="{round(re[3]*0.35,1)}" rx="1.5" fill="white" opacity="0.92"/>
  <rect x="{round(re[0]+re[2]*0.1,1)}" y="{round(re[1]+re[3]*0.55,1)}"
        width="{round(re[2]*0.22,1)}" height="{round(re[3]*0.22,1)}" rx="1" fill="white" opacity="0.5"/>'''

    # ── Smile ──
    svg += f'''

  <!-- Smile -->
  <path d="M{round(smile_x1,1)} {round(smile_y,1)} Q{round(smile_mid_x,1)} {round(smile_cy,1)} {round(smile_x2,1)} {round(smile_y,1)}"
        stroke="#1a1520" stroke-width="{round(ow*0.4,1)}" fill="none"
        stroke-linecap="round" opacity="0.5"/>'''

    # ── Blush ──
    for i, (bx, by, brx, bry, bcol) in enumerate(bl):
        side = "Left" if i == 0 else "Right"
        svg += f'''

  <!-- {side} cheek blush -->
  <ellipse cx="{bx}" cy="{by}" rx="{brx}" ry="{bry}" fill="{bcol}" opacity="0.5"/>'''

    svg += '\n</svg>\n'
    return svg


def gen_svg_32(info: dict) -> str:
    """Generate a compact 32×32 favicon SVG."""
    v = info['vertices']
    s = 32 / info['size'][0]
    c = info['colors']

    def sc(pt):
        return (round(pt[0] * s, 2), round(pt[1] * s, 2))

    T, L, BL, B, BR, R, C = [sc(v[k]) for k in ('T', 'L', 'BL', 'B', 'BR', 'R', 'C')]

    le = info['left_eye']
    re = info['right_eye']

    # At 32px, we simplify: just small squares for eyes, simple curve for smile
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="t" x1="0" y1="0" x2=".6" y2="1">
      <stop offset="0%" stop-color="{c['top_light']}"/>
      <stop offset="100%" stop-color="{c['top_dark']}"/>
    </linearGradient>
    <linearGradient id="f" x1="0" y1="0" x2=".3" y2="1">
      <stop offset="0%" stop-color="{c['front_light']}"/>
      <stop offset="100%" stop-color="{c['front_dark']}"/>
    </linearGradient>
    <linearGradient id="r" x1="0" y1="0" x2="1" y2=".8">
      <stop offset="0%" stop-color="{c['right_light']}"/>
      <stop offset="100%" stop-color="{c['right_dark']}"/>
    </linearGradient>
  </defs>
  <path d="M{T[0]} {T[1]} L{L[0]} {L[1]} L{C[0]} {C[1]} L{R[0]} {R[1]}Z" fill="url(#t)"/>
  <path d="M{L[0]} {L[1]} L{BL[0]} {BL[1]} L{B[0]} {B[1]} L{C[0]} {C[1]}Z" fill="url(#f)"/>
  <path d="M{C[0]} {C[1]} L{B[0]} {B[1]} L{BR[0]} {BR[1]} L{R[0]} {R[1]}Z" fill="url(#r)"/>
  <path d="M{T[0]} {T[1]} L{L[0]} {L[1]} L{BL[0]} {BL[1]} L{B[0]} {B[1]} L{BR[0]} {BR[1]} L{R[0]} {R[1]}Z"
        stroke="#1a1520" stroke-width=".7" stroke-linejoin="round" fill="none"/>'''

    # Simplified eyes at 32px scale
    if le:
        ex, ey = round(le[0]*s, 1), round(le[1]*s, 1)
        ew, eh = round(le[2]*s, 1), round(le[3]*s, 1)
        svg += f'''
  <rect x="{ex}" y="{ey}" width="{ew}" height="{eh}" rx=".3" fill="#1a1520"/>
  <rect x="{round(ex+ew*0.55,1)}" y="{round(ey+eh*0.1,1)}" width="{round(ew*0.35,1)}" height="{round(eh*0.35,1)}" rx=".2" fill="white" opacity=".9"/>'''

    if re:
        ex, ey = round(re[0]*s, 1), round(re[1]*s, 1)
        ew, eh = round(re[2]*s, 1), round(re[3]*s, 1)
        svg += f'''
  <rect x="{ex}" y="{ey}" width="{ew}" height="{eh}" rx=".3" fill="#1a1520"/>
  <rect x="{round(ex+ew*0.55,1)}" y="{round(ey+eh*0.1,1)}" width="{round(ew*0.35,1)}" height="{round(eh*0.35,1)}" rx=".2" fill="white" opacity=".9"/>'''

    # Smile
    if le and re:
        sx1 = round((le[0] + le[2]*0.3)*s, 1)
        sx2 = round((re[0] + re[2]*0.7)*s, 1)
        sy = round((le[1] + le[3])*s + 0.8, 1)
        scy = round(sy + 0.9, 1)
        smx = round((sx1+sx2)/2, 1)
        svg += f'''
  <path d="M{sx1} {sy} Q{smx} {scy} {sx2} {sy}" stroke="#1a1520" stroke-width=".5" fill="none" stroke-linecap="round" opacity=".5"/>'''

    # Blush (simplified ellipses)
    for cx, cy, bw, bh, bcol in info['blushes']:
        svg += f'''
  <ellipse cx="{round(cx*s,1)}" cy="{round(cy*s,1)}" rx="{round(bw*s*0.4,1)}" ry="{round(bh*s*0.4,1)}" fill="{bcol}" opacity=".5"/>'''

    svg += '\n</svg>\n'
    return svg


def main():
    if not os.path.isfile(SRC_PNG):
        print(f"ERROR: Source PNG not found: {SRC_PNG}", file=sys.stderr)
        sys.exit(1)

    print(f"[convert] Analyzing {SRC_PNG} ...")
    info = analyze_png(SRC_PNG)

    print(f"\n[convert] Generating 512×512 SVG → {OUT_512}")
    svg_512 = gen_svg_512(info)
    with open(OUT_512, 'w') as f:
        f.write(svg_512)
    print(f"  Written {len(svg_512)} bytes")

    print(f"\n[convert] Generating 32×32 SVG → {OUT_32}")
    svg_32 = gen_svg_32(info)
    with open(OUT_32, 'w') as f:
        f.write(svg_32)
    print(f"  Written {len(svg_32)} bytes")

    # Also output the URL-encoded version for inline <link> tags
    import urllib.parse
    encoded_32 = urllib.parse.quote(svg_32, safe='')
    print("\n[convert] Inline data URI for favicon (for <link rel=\"icon\">):")
    print(f"  data:image/svg+xml,{encoded_32[:120]}...")

    print("\n[convert] Done! To use as project logo:")
    print("  1. Update FAVICON_SVG in routes/common.py")
    print("  2. Update <link rel='icon'> in index.html")
    print("  3. Update <link rel='apple-touch-icon'> in index.html")


if __name__ == '__main__':
    main()
