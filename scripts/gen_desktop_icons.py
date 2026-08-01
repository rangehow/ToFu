#!/usr/bin/env python3
"""Generate platform icons (.ico, .icns) from the source logo.png.

Usage:
    python scripts/gen_desktop_icons.py

Outputs:
    static/icons/tofu.ico   — Windows multi-size icon (16–256px)
    static/icons/tofu.icns  — macOS icon (16–512px @1x/@2x)
    static/icons/installer/wizard-large.bmp — Inno Setup left panel (164×314)
    static/icons/installer/wizard-small.bmp — Inno Setup inner page (55×58)
"""

import os
import sys

# Windows CI consoles default to cp1252, which can't encode the ✓/× glyphs
# in our status prints — that raises UnicodeEncodeError and fails the build.
# Force UTF-8 on the streams (Python 3.7+) so output is portable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

try:
    from PIL import Image, ImageChops, ImageDraw
except ImportError:
    print('ERROR: Pillow is required. Install with: pip install Pillow')
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS_DIR = os.path.join(ROOT, 'static', 'icons')
SOURCE_PNG = os.path.join(ICONS_DIR, 'logo.png')

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]

ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def gen_ico(src: Image.Image, out_path: str):
    """Generate a multi-size .ico file."""
    src.save(out_path, format='ICO', sizes=ICO_SIZES)
    print(f'  ✓ {out_path} ({os.path.getsize(out_path) // 1024} KB)')


def gen_icns(src: Image.Image, out_path: str):
    """Generate an .icns file via Pillow (supports basic icns writing)."""
    sizes = [sz for sz in ICNS_SIZES if sz <= src.width]
    imgs = [src.resize((sz, sz), Image.LANCZOS) for sz in sizes]
    imgs[0].save(out_path, format='ICNS', append_images=imgs[1:])
    print(f'  ✓ {out_path} ({os.path.getsize(out_path) // 1024} KB)')


# Inno Setup `modern` wizard geometry — these dimensions are part of the
# Inno contract, not a design choice: WizardImageFile is the left panel of
# the welcome/finish pages, WizardSmallImageFile the top-right of inner pages.
WIZARD_LARGE_SIZE = (164, 314)
WIZARD_SMALL_SIZE = (55, 58)

# Brand accent ramp (static/styles.css --accent / --accent-hover, dark theme).
_ACCENT_TOP = (0x7c, 0x66, 0xd4)
_ACCENT_BOTTOM = (0x55, 0x3c, 0xa8)


def _cut_out_background(src: Image.Image) -> Image.Image:
    """Return an RGBA copy with the EXTERIOR near-white background removed.

    logo.png is RGBA but fully opaque (alpha 255 everywhere) over a uniform
    ~254-white canvas. A global white threshold would also punch holes in the
    cube's interior specular highlight and leave antialiased fringes; a
    flood-fill seeded from the four corners removes ONLY the connected
    exterior region — the closed black outline keeps the interior untouched.
    thresh=24 sits far from both the cream face (~41 away) and any outline
    blend pixel, so the fill cannot leak across the border.
    """
    im = src.convert('RGB')
    sentinel = (255, 0, 255)
    for corner in ((0, 0), (im.width - 1, 0),
                   (0, im.height - 1), (im.width - 1, im.height - 1)):
        ImageDraw.floodfill(im, corner, sentinel, thresh=24)
    # Alpha from the sentinel WITHOUT getdata (deprecated in Pillow 12):
    # difference-against-sentinel is exactly (0,0,0) on sentinel pixels.
    diff = ImageChops.difference(im, Image.new('RGB', im.size, sentinel))
    alpha = diff.convert('L').point(lambda v: 0 if v == 0 else 255)
    out = im.convert('RGBA')
    out.putalpha(alpha)
    return out


def gen_wizard_images(src: Image.Image, out_dir: str):
    """Generate the Inno Setup wizard bitmaps from the source logo.

    wizard-large.bmp: brand accent gradient with the logo on a soft light
    disc — the installer is the FIRST screen a Windows user ever sees of the
    product, and the default Inno blank panel was the only unbranded surface
    left in the install flow. wizard-small.bmp: logo on white (Inno inner
    pages are white). Both are RGB BMPs, the only format ISCC accepts here.
    """
    os.makedirs(out_dir, exist_ok=True)

    # ── Large panel: vertical accent gradient + disc + centered logo ──
    w, h = WIZARD_LARGE_SIZE
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(_ACCENT_TOP[i] + (_ACCENT_BOTTOM[i] - _ACCENT_TOP[i]) * t)
                    for i in range(3)) + (255,)
        for x in range(w):
            px[x, y] = row

    logo_size = 96
    logo_cx, logo_cy = w // 2, 78 + logo_size // 2
    disc = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse(
        [logo_cx - 62, logo_cy - 62, logo_cx + 62, logo_cy + 62],
        fill=(255, 255, 255, 46))
    img = Image.alpha_composite(img, disc)
    logo = _cut_out_background(src).resize((logo_size, logo_size),
                                           Image.LANCZOS)
    img.paste(logo, (logo_cx - logo_size // 2, logo_cy - logo_size // 2), logo)

    large_path = os.path.join(out_dir, 'wizard-large.bmp')
    img.convert('RGB').save(large_path, format='BMP')
    print(f'  ✓ {large_path} ({os.path.getsize(large_path) // 1024} KB)')

    # ── Small tile: logo on white (inner pages) ──
    sw, sh = WIZARD_SMALL_SIZE
    small = Image.new('RGBA', (sw, sh), (255, 255, 255, 255))
    s_logo = _cut_out_background(src).resize((44, 44), Image.LANCZOS)
    small.paste(s_logo, ((sw - 44) // 2, (sh - 44) // 2), s_logo)
    small_path = os.path.join(out_dir, 'wizard-small.bmp')
    small.convert('RGB').save(small_path, format='BMP')
    print(f'  ✓ {small_path} ({os.path.getsize(small_path) // 1024} KB)')


def main():
    if not os.path.isfile(SOURCE_PNG):
        print(f'ERROR: Source logo not found: {SOURCE_PNG}')
        sys.exit(1)

    src = Image.open(SOURCE_PNG).convert('RGBA')
    print(f'Source: {SOURCE_PNG} ({src.width}×{src.height})')

    ico_path = os.path.join(ICONS_DIR, 'tofu.ico')
    gen_ico(src, ico_path)

    icns_path = os.path.join(ICONS_DIR, 'tofu.icns')
    try:
        gen_icns(src, icns_path)
    except Exception as e:
        print(f'  ! ICNS generation failed ({e}) — run on macOS or use iconutil')

    gen_wizard_images(src, os.path.join(ICONS_DIR, 'installer'))


if __name__ == '__main__':
    main()
