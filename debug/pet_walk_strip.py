"""Render a multi-frame STRIP of the pet WALKING left→right through the
foreground occlusion band, so we can SEE the near blades part as the cat passes
and spring back behind it (the "interaction in motion" proof the owner asked
for). Reuses the faithful cairo raster from pet_scene_composite.py.

Pipeline:
  1. tests/_scene_walkstrip.js drives the REAL tofu-scene.js with a MOVING pet
     (footx X0→X1 over ~90 frames) and emits buffer + N keyframe {x, frame, fg}.
  2. For each keyframe: cairo-raster bg (buffer+frame) → composite the real pet
     PNG at its DOM x + CSS shadow → composite the fg occlusion plane on top.
  3. Stack the keyframes vertically into one strip PNG.

Usage: python3 debug/pet_walk_strip.py <decor> <pose> <x0> <x1> <nkeys> <out.png>
"""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import pet_scene_composite as C  # reuse _replay / _replay_transparent / raster

REPO = Path(__file__).resolve().parent.parent
STRIP_HARNESS = REPO / "tests" / "_scene_walkstrip.js"
PET_DIR = REPO / "static" / "icons" / "pet" / "oneko"
SCALE = C.SCALE
W, H = C.W, C.H


def _compose_frame(buffer, frame, fg, footx, pose):
    scene = C._cairo_to_pil(C._replay([buffer, frame])).convert("RGBA")
    petbox = 30
    petleft = (footx - 16) * SCALE
    pettop = (H - 1 - petbox) * SCALE
    petpx = petbox * SCALE

    # CSS contact shadow (.tofu-pet::after)
    sh = Image.new("RGBA", scene.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    shw, shh = 22 * SCALE, 5 * SCALE
    shcx = petleft + petpx / 2
    shcy = (H - 1) * SCALE
    d.ellipse([shcx - shw / 2, shcy - shh / 2, shcx + shw / 2, shcy + shh / 2],
              fill=(64, 52, 34, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(SCALE))
    scene = Image.alpha_composite(scene, sh)

    petfile = PET_DIR / f"oneko-{pose}.png"
    if not petfile.exists():
        petfile = PET_DIR / "oneko-walk1.png"
    pet = Image.open(petfile).convert("RGBA").resize((petpx, petpx), Image.LANCZOS)
    scene.alpha_composite(pet, (int(petleft), int(pettop)))

    if fg:
        fg_img = C._cairo_to_pil_rgba(C._replay_transparent([fg]))
        scene.alpha_composite(fg_img, (0, 0))
    return scene.convert("RGB")


def main():
    decor = sys.argv[1] if len(sys.argv) > 1 else "meadow"
    pose = sys.argv[2] if len(sys.argv) > 2 else "walk1"
    x0 = sys.argv[3] if len(sys.argv) > 3 else "120"
    x1 = sys.argv[4] if len(sys.argv) > 4 else "210"
    nkeys = sys.argv[5] if len(sys.argv) > 5 else "4"
    out = sys.argv[6] if len(sys.argv) > 6 else "/tmp/walk_strip.png"

    r = subprocess.run(["node", str(STRIP_HARNESS), str(W), str(H), decor, x0, x1, nkeys],
                       capture_output=True, text=True, cwd=str(REPO), timeout=40)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    buffer = data["buffer"]
    frames = data["frames"]

    # cycle the 4 walk poses across keyframes so the legs also animate,
    # starting from the requested pose
    poses = ["walk1", "walk2", "walk3", "walk4"]
    start = poses.index(pose) if pose in poses else 0
    tiles = []
    for i, fr in enumerate(frames):
        p = poses[(start + i) % len(poses)]
        tiles.append(_compose_frame(buffer, fr["frame"], fr["fg"], fr["x"], p))

    gap = 6 * SCALE
    strip = Image.new("RGB", (W * SCALE, H * SCALE * len(tiles) + gap * (len(tiles) - 1)),
                      (255, 255, 255))
    y = 0
    for t in tiles:
        strip.paste(t, (0, y))
        y += H * SCALE + gap
    strip.save(out)
    print("wrote", out, strip.size, "keyframes:", [round(f["x"]) for f in frames])


if __name__ == "__main__":
    main()
