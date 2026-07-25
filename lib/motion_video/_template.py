"""lib/motion_video/_template.py — Zero-LLM scene composition generator.

The headless engine's composition fallback: a clean kinetic-type card per
scene (headline slide-in → hold → gentle settle) built on the same contract
as ``guide/skeleton.html``. Output is deterministic and passes
:func:`lib.motion_video._gates.check_composition_html` by construction
(HTML-escaped text, one paused GSAP timeline, no banned patterns).

The chat-agent path authors creative compositions itself; this template is
the floor that lets a fully-automatic SRT → narrated-video run succeed.
"""

from __future__ import annotations

import html as _html

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['render_scene_html']

#: (max_chars, font_px) — conservative steps so CJK text fits 1080px width.
_FONT_STEPS = ((18, 120), (36, 96), (64, 76), (110, 60), (10**9, 46))


def _font_size(text: str) -> int:
    n = len(text)
    for cap, px in _FONT_STEPS:
        if n <= cap:
            return px
    return 46


_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <title>{scene_id}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
      body {{ font-family: Inter, "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif; }}
      #root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; }}
      .bgfill {{ position: absolute; inset: 0; background: {background}; }}
      .clip {{ position: absolute; inset: 0; display: grid; place-items: center; }}
      .headline {{
        font-size: {font_px}px; font-weight: 800; color: #fff; letter-spacing: -1px;
        max-width: {max_width}px; text-align: center; line-height: 1.4;
        text-shadow: 0 4px 32px rgba(0,0,0,.45);
      }}
      .tag {{
        position: absolute; top: 48px; left: 56px;
        font-size: 30px; font-weight: 600; color: rgba(255,255,255,.55);
        letter-spacing: 4px;
      }}
    </style>
  </head>
  <body>
    <div
      id="root"
      data-composition-id="main"
      data-start="0"
      data-duration="{duration}"
      data-width="{width}"
      data-height="{height}"
    >
      <div class="bgfill"></div>
      <section id="card-1" class="clip" data-start="0" data-duration="{duration}" data-track-index="1">
        <div class="tag" id="scenetag">{tag}</div>
        <h1 class="headline" id="headline">{headline}</h1>
      </section>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      tl.from('#headline', {{ y: 56, opacity: 0, duration: 0.7, ease: 'power3.out' }}, 0.15);
      tl.from('#scenetag', {{ opacity: 0, duration: 0.5, ease: 'power2.out' }}, 0.1);
      window.__timelines['main'] = tl;
    </script>
  </body>
</html>
"""

_GRADIENTS = (
    'linear-gradient(160deg, #0b0f14 0%, #14243a 60%, #1d3a5f 100%)',
    'linear-gradient(160deg, #140b12 0%, #2a1430 60%, #3f1d4d 100%)',
    'linear-gradient(160deg, #0b1410 0%, #14301f 60%, #1d4d33 100%)',
    'linear-gradient(160deg, #14120b 0%, #302714 60%, #4d3d1d 100%)',
)


def render_scene_html(scene: dict, *, width: int = 1080, height: int = 1440,
                      duration: float | None = None,
                      scene_index: int = 1, total_scenes: int = 1) -> str:
    """Render one scene dict (id/start/end/text) into a composition HTML."""
    text = str(scene.get('text') or '').strip()
    headline = _html.escape(text) if text else '…'
    dur = duration if duration and duration > 0 else (
        float(scene.get('end') or 0) - float(scene.get('start') or 0))
    dur = max(0.5, round(float(dur), 3))
    scene_id = str(scene.get('id') or f'scene-{scene_index:03d}')
    return _TEMPLATE.format(
        width=width, height=height, duration=dur,
        scene_id=_html.escape(scene_id),
        headline=headline,
        tag=_html.escape(f'{scene_index:02d} / {total_scenes:02d}'),
        font_px=_font_size(text),
        max_width=int(width * 0.84),
        background=_GRADIENTS[(scene_index - 1) % len(_GRADIENTS)],
    )
