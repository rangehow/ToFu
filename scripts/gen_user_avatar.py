#!/usr/bin/env python3
"""Generate a beautiful user avatar icon (onigiri/rice ball) via image generation.

Produces a high-quality kawaii onigiri PNG in static/icons/, then converts
it to SVG using the png_to_svg pipeline.

Usage:
    python3 scripts/gen_user_avatar.py
    python3 scripts/gen_user_avatar.py --no-svg       # PNG only, skip SVG conversion
    python3 scripts/gen_user_avatar.py --attempts 3   # limit retries
"""

import argparse
import base64
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.log import get_logger
from lib.image_gen import generate_image

logger = get_logger(__name__)

_ICONS_DIR = os.path.join(_PROJECT_ROOT, 'static', 'icons')
_LOGO_PATH = os.path.join(_ICONS_DIR, 'logo.png')
_OUT_PNG = os.path.join(_ICONS_DIR, 'onigiri-new.png')
_OUT_SVG = os.path.join(_ICONS_DIR, 'onigiri-new.svg')

_PROMPT = (
    'A kawaii isometric 3D onigiri (Japanese rice ball) character on a clean white background. '
    'The onigiri has a soft triangular shape with gently rounded corners, '
    'cream-white rice surface with subtle shading, wrapped in a dark green nori (seaweed) '
    'band around its lower third. '
    'It has cute rectangular pixel-art style eyes with white highlight catchlights, '
    'rosy pink blush marks on both cheeks, and a gentle happy smile. '
    'The onigiri is rendered in the same isometric 3D perspective as the reference tofu cube — '
    'slightly tilted to show depth, with soft shadows and highlights that give it volume. '
    'A few tiny rice grain highlights sparkle on the surface. '
    'The style matches the reference image exactly — same cream/beige color palette, '
    'same kawaii face proportions, same dark outline weight, same soft shading style. '
    'Simple, clean composition with no background clutter. '
    'High quality digital illustration, soft flat shading, mascot icon style, '
    'suitable for a small 28×28px avatar display.'
)


def _load_reference() -> list:
    """Load logo.png as style reference."""
    if not os.path.exists(_LOGO_PATH):
        logger.warning('[GenAvatar] logo.png not found at %s', _LOGO_PATH)
        return []
    with open(_LOGO_PATH, 'rb') as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode('ascii')
    logger.info('[GenAvatar] Loaded logo.png as reference: %d bytes', len(raw))
    return [{'image_b64': b64, 'mime_type': 'image/png'}]


def generate_avatar(source_images: list, max_attempts: int = 5) -> bool:
    """Generate the user avatar PNG.

    Args:
        source_images: Style reference images.
        max_attempts: Max generation attempts.

    Returns:
        True if avatar was generated successfully.
    """
    for attempt in range(1, max_attempts + 1):
        logger.info('[GenAvatar] Generating user avatar (attempt %d/%d)…',
                     attempt, max_attempts)

        try:
            result = generate_image(
                prompt=_PROMPT,
                aspect_ratio='1:1',
                resolution='1K',
                source_images=source_images if source_images else None,
                timeout=180,
                max_retries=5,
            )
        except Exception as e:
            logger.error('[GenAvatar] generate_image() raised: %s', e, exc_info=True)
            if attempt < max_attempts:
                time.sleep(5)
                continue
            return False

        if not result.get('ok'):
            logger.warning('[GenAvatar] Generation failed: %s', result.get('error', 'unknown'))
            if attempt < max_attempts:
                time.sleep(3)
                continue
            return False

        image_b64 = result.get('image_b64', '')
        if not image_b64:
            logger.warning('[GenAvatar] No image data returned')
            continue

        try:
            img_bytes = base64.b64decode(image_b64)
            with open(_OUT_PNG, 'wb') as f:
                f.write(img_bytes)
            logger.info('[GenAvatar] ✅ Saved PNG → %s (%d bytes)', _OUT_PNG, len(img_bytes))
            return True
        except Exception as e:
            logger.error('[GenAvatar] Failed to save PNG: %s', e, exc_info=True)
            return False

    return False


def convert_to_svg() -> bool:
    """Convert the generated PNG to SVG."""
    try:
        from scripts.png_to_svg import convert_png_to_svg
        ok = convert_png_to_svg(_OUT_PNG, _OUT_SVG, trace_size=512, remove_bg=True)
        if ok:
            logger.info('[GenAvatar] ✅ SVG created: %s', _OUT_SVG)
        return ok
    except ImportError:
        logger.warning('[GenAvatar] png_to_svg not importable, trying subprocess…')
        import subprocess
        ret = subprocess.run(
            [sys.executable, os.path.join(_PROJECT_ROOT, 'scripts', 'png_to_svg.py'), 'onigiri-new.png'],
            cwd=_PROJECT_ROOT,
            capture_output=True, text=True,
        )
        if ret.returncode == 0:
            logger.info('[GenAvatar] ✅ SVG created via subprocess')
            return True
        logger.error('[GenAvatar] SVG conversion failed: %s', ret.stderr[:500])
        return False


def main():
    parser = argparse.ArgumentParser(description='Generate kawaii onigiri user avatar')
    parser.add_argument('--no-svg', action='store_true', help='Skip SVG conversion')
    parser.add_argument('--no-reference', action='store_true', help='Skip logo.png reference')
    parser.add_argument('--attempts', type=int, default=5, help='Max generation attempts')
    args = parser.parse_args()

    source_images = [] if args.no_reference else _load_reference()

    ok = generate_avatar(source_images, max_attempts=args.attempts)
    if not ok:
        print('❌ Failed to generate avatar PNG')
        sys.exit(1)

    print(f'✅ PNG saved: {_OUT_PNG} ({os.path.getsize(_OUT_PNG):,} bytes)')

    if not args.no_svg:
        svg_ok = convert_to_svg()
        if svg_ok and os.path.exists(_OUT_SVG):
            print(f'✅ SVG saved: {_OUT_SVG} ({os.path.getsize(_OUT_SVG):,} bytes)')
        else:
            print('⚠️  SVG conversion failed — PNG is still available')

    print('\nTo use the new avatar, rename onigiri-new.svg → onigiri.svg')
    print('Or run: cp static/icons/onigiri-new.svg static/icons/onigiri.svg')


if __name__ == '__main__':
    main()
