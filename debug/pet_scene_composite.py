"""Render the ACTUAL composited project-bar (scene canvas + the real pet sprite
at its DOM position + the CSS contact shadow) to a PNG, so we can SEE what reads
as "pet floats on top / fake interaction" instead of tuning blind.

Faithful pipeline:
  1. tests/_scene_pixeldiff.js records the real tofu-scene.js draw stream
     (baked buffer + per-frame overlay) for a given scene + pet foot x.
  2. cairo replays it to an RGB image at SCALE× (same rasterizer the pixel-diff
     acceptance test uses).
  3. PIL composites the real pet PNG at the DOM geometry (.tofu-pet: 30px box,
     bottom:1px, left = footx-16) + the .tofu-pet::after radial contact shadow.

Usage: python3 debug/pet_scene_composite.py <decor> <pose> <footx> <out.png>
"""
import json
import math
import subprocess
import sys
from pathlib import Path

import cairocffi
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "_scene_pixeldiff.js"
PET_DIR = REPO / "static" / "icons" / "pet" / "oneko"

W, H = 360, 48
SCALE = 8            # upscale so we can see fine structure


def _hex(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0)


def _apply_grad(g):
    if g["kind"] == "linear":
        pat = cairocffi.LinearGradient(g["x0"], g["y0"], g["x1"], g["y1"])
    else:
        pat = cairocffi.RadialGradient(g["x0"], g["y0"], g["r0"], g["x1"], g["y1"], g["r1"])
    for off, col in g["stops"]:
        if col.startswith("rgb"):
            nums = col[col.index("(") + 1:col.index(")")].split(",")
            r, gg, b = (float(nums[0]) / 255, float(nums[1]) / 255, float(nums[2]) / 255)
            a = float(nums[3]) if len(nums) > 3 else 1.0
            pat.add_color_stop_rgba(off, r, gg, b, a)
        else:
            r, gg, b = _hex(col)
            pat.add_color_stop_rgba(off, r, gg, b, 1.0)
    return pat


def _replay(ops_list):
    surf = cairocffi.ImageSurface(cairocffi.FORMAT_ARGB32, W * SCALE, H * SCALE)
    ctx = cairocffi.Context(surf)
    ctx.scale(SCALE, SCALE)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    for ops in ops_list:
        for o in ops:
            comp = o.get("comp", "source-over")
            ctx.set_operator(cairocffi.OPERATOR_ADD if comp == "lighter" else cairocffi.OPERATOR_OVER)
            if o["t"] == "rect":
                if o.get("grad"):
                    ctx.set_source(_apply_grad(o["grad"]))
                else:
                    r, g, b = _hex(o["color"])
                    ctx.set_source_rgba(r, g, b, o.get("alpha", 1.0))
                ctx.rectangle(o["x"], o["y"], o["w"], o["h"])
                ctx.fill()
            elif o["t"] == "dab":
                r, g, b = _hex(o["color"])
                ctx.save()
                ctx.translate(o["x"], o["y"])
                ctx.rotate(o["ang"])
                ctx.scale(max(o["rx"], 1e-3), max(o["ry"], 1e-3))
                ctx.arc(0, 0, 1.0, 0, 2 * math.pi)
                ctx.restore()
                ctx.set_source_rgba(r, g, b, min(1.0, o.get("alpha", 1.0)))
                ctx.fill()
    surf.flush()
    return surf


def _replay_transparent(ops_list):
    """Like _replay but onto a TRANSPARENT surface (no white base paint) — for
    the foreground overlay that must composite over the scene keeping its alpha."""
    surf = cairocffi.ImageSurface(cairocffi.FORMAT_ARGB32, W * SCALE, H * SCALE)
    ctx = cairocffi.Context(surf)
    ctx.scale(SCALE, SCALE)
    for ops in ops_list:
        for o in ops:
            if o["t"] != "dab":
                continue
            r, g, b = _hex(o["color"])
            ctx.set_operator(cairocffi.OPERATOR_OVER)
            ctx.save()
            ctx.translate(o["x"], o["y"])
            ctx.rotate(o["ang"])
            ctx.scale(max(o["rx"], 1e-3), max(o["ry"], 1e-3))
            ctx.arc(0, 0, 1.0, 0, 2 * math.pi)
            ctx.restore()
            ctx.set_source_rgba(r, g, b, min(1.0, o.get("alpha", 1.0)))
            ctx.fill()
    surf.flush()
    return surf


def _cairo_to_pil_rgba(surf):
    data = bytes(surf.get_data())
    stride = surf.get_stride()
    img = Image.new("RGBA", (W * SCALE, H * SCALE), (0, 0, 0, 0))
    px = img.load()
    for y in range(H * SCALE):
        for x in range(W * SCALE):
            i = y * stride + x * 4
            b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
            px[x, y] = (r, g, b, a)
    return img


def _cairo_to_pil(surf):
    data = bytes(surf.get_data())
    stride = surf.get_stride()
    img = Image.new("RGB", (W * SCALE, H * SCALE))
    px = img.load()
    for y in range(H * SCALE):
        for x in range(W * SCALE):
            i = y * stride + x * 4
            b, g, r = data[i], data[i + 1], data[i + 2]
            px[x, y] = (r, g, b)
    return img


def main():
    decor = sys.argv[1] if len(sys.argv) > 1 else "meadow"
    pose = sys.argv[2] if len(sys.argv) > 2 else "walk1"
    footx = float(sys.argv[3]) if len(sys.argv) > 3 else 180.0
    out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/composite.png"

    stream = subprocess.run(
        ["node", str(HARNESS), str(W), str(H), str(footx), "1600", decor],
        capture_output=True, text=True, cwd=str(REPO), timeout=30)
    assert stream.returncode == 0, stream.stderr
    data = json.loads(stream.stdout.strip().splitlines()[-1])
    scene = _cairo_to_pil(_replay([data["buffer"], data["frame"]])).convert("RGBA")
    fg_stream = data.get("fg") or []

    # DOM geometry: .tofu-pet is 30x30, bottom:1px, left = footx-16
    petbox = 30
    petleft = (footx - 16) * SCALE
    pettop = (H - 1 - petbox) * SCALE
    petpx = petbox * SCALE

    # contact shadow (.tofu-pet::after): 22x5 ellipse, centered left:50%, bottom:-1
    sh = Image.new("RGBA", (W * SCALE, H * SCALE), (0, 0, 0, 0))
    from PIL import ImageDraw
    d = ImageDraw.Draw(sh)
    shw, shh = 22 * SCALE, 5 * SCALE
    shcx = petleft + petpx / 2
    shcy = (H - 1) * SCALE
    d.ellipse([shcx - shw / 2, shcy - shh / 2, shcx + shw / 2, shcy + shh / 2],
              fill=(64, 52, 34, 90))
    sh = sh.filter(__import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur(SCALE))
    scene = Image.alpha_composite(scene, sh)

    petf = PET_DIR / f"oneko-{pose}.png"
    pet = Image.open(petf).convert("RGBA").resize((petpx, petpx), Image.LANCZOS)
    scene.alpha_composite(pet, (int(petleft), int(pettop)))

    # REAL foreground occlusion plane (the shipped tofu-scene.js _paintForeground
    # stream), composited IN FRONT of the pet — exactly the z2 canvas the browser
    # paints above the DOM pet at z1.
    if fg_stream:
        fg_img = _cairo_to_pil_rgba(_replay_transparent([fg_stream]))
        scene.alpha_composite(fg_img, (0, 0))

    scene.convert("RGB").save(out)
    print("wrote", out, scene.size)


if __name__ == "__main__":
    main()
