"""lib/motion_video/_quality.py — per-scene quality telemetry + the asset floor.

Two things live here, and they share a module because they answer the same
question from opposite ends: *is this frame actually rich, or merely valid?*

**1. Telemetry.** Until now ``job.json`` recorded only ``gate_failed_scenes``
and ``authored_scenes`` — a boolean-ish verdict with no numbers behind it. So
the only way to answer "did this film get better?" was to re-measure every
scene by hand in a browser (measured: the owner did exactly that, and it is
what surfaced that a 32% dead-band alarm was 100% attributable to ONE 2-node
fallback card rather than to any layout defect). A verdict you cannot diff
across runs cannot show progress, so every number the verdict is derived from
is now persisted per scene.

**2. The asset floor.** ``verify_asset_refs`` only checks that a reference
RESOLVES; nothing has ever required a reference to EXIST. Combined with the
author prompt's "REAL IMAGERY IS AVAILABLE — use it when the beat calls for
it" (purely permissive), the measured water-line on this host was **one
background image per scene, and some scenes with none at all**. An optional
capability that nothing asks for is an unused capability.

**What counts as a graphic, and why inline SVG counts.** The floor asks for a
real GRAPHIC, not specifically a generated bitmap. A hand-authored inline SVG
gauge with a drawn arc and a needle (measured: scene-005 of the film that
prompted this work, 15 paint nodes, 89.5% span) is richer than a stock
background PNG dropped behind text. Counting only files would push authors
away from the better artefact, so both are counted — and reported separately,
because "6 files / 0 inline" and "0 files / 6 inline" are different films and
the telemetry must not blur them.

Fonts are deliberately NOT graphics. Every scene is handed a CJK sans face by
the author loop, so counting fonts would make the floor pass for free — it
would report success on precisely the all-text card deck it exists to reject.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['GRAPHIC_EXTENSIONS', 'count_scene_graphics', 'scene_telemetry',
           'asset_floor_findings', 'film_quality_summary',
           'is_text_only_exempt']

#: Extensions that count as a real GRAPHIC for the floor. A strict subset of
#: :data:`lib.motion_video._assets.ALLOWED_EXTENSIONS` — the font formats are
#: excluded on purpose (see the module docstring: every scene gets a font, so
#: counting fonts would make the floor self-satisfying).
GRAPHIC_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.svg',
    '.mp4', '.webm',
})

#: An inline ``<svg>`` element together with its body, so we can ask whether it
#: actually DRAWS anything rather than merely existing.
_SVG_BLOCK_RE = re.compile(r'<svg\b[^>]*>(.*?)</svg\s*>', re.IGNORECASE | re.S)

#: Elements that put ink on the frame. An ``<svg>`` holding only ``<defs>`` or
#: a lone ``<title>`` paints nothing, and must not be credited as a graphic.
_SVG_PAINT_RE = re.compile(
    r'<\s*(path|circle|rect|line|polyline|polygon|ellipse|image|use|text)\b',
    re.IGNORECASE)

#: ``@font-face`` declarations — reported so a serif regression (the host's
#: only CJK face) is visible in telemetry instead of only in the pixels.
_FONT_FACE_RE = re.compile(r'@font-face\s*\{', re.IGNORECASE)

#: The reserved ``visual`` marker for the end card. It is a credits card by
#: construction, so the floor must not demand imagery of it.
_SOURCES_MARKER = 'sources'


def is_text_only_exempt(scene: dict | None) -> tuple[bool, str]:
    """``(exempt, reason)`` — may this scene ship with no graphic at all?

    Two exemptions, both narrow and both declared in the DATA rather than
    inferred from the composition (an inferred exemption is indistinguishable
    from the failure it excuses):

      * the reserved ``visual == 'sources'`` end card, which is a credits
        frame by construction;
      * an explicit ``text_only_reason`` on the scene — the owner's
        "hold/transition frames are allowed, but the reason must be stated"
        rule. A scene that simply forgot its imagery has no such field, so it
        still fails the floor.
    """
    if not isinstance(scene, dict):
        return False, ''
    if str(scene.get('visual') or '').strip().lower() == _SOURCES_MARKER:
        return True, 'sources end card'
    reason = str(scene.get('text_only_reason') or '').strip()
    if reason:
        return True, reason
    return False, ''


def count_scene_graphics(html: str, scene_dir: str) -> dict:
    """Count the real graphics a composition carries.

    Returns ``{'asset_files', 'inline_svg', 'font_faces', 'graphics'}`` where
    ``graphics = asset_files + inline_svg`` — the number the floor judges.

    A referenced file is counted only when it EXISTS on disk: a reference to a
    file the author invented renders as a blank rectangle, and crediting it
    would let the floor pass on a frame that is visibly broken. Existence
    follows symlinks on purpose — a correctly materialised asset is normally a
    symlink into the shared library.
    """
    from lib.motion_video._assets import extract_local_refs

    asset_files = 0
    seen: set[str] = set()
    for ref in extract_local_refs(html or ''):
        clean = ref.split('#', 1)[0].split('?', 1)[0].strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        if os.path.splitext(clean)[1].lower() not in GRAPHIC_EXTENSIONS:
            continue  # a font, or something we do not count as ink
        if os.path.isabs(clean):
            continue  # rejected elsewhere; never credited here
        if os.path.isfile(os.path.join(scene_dir or '', clean)):
            asset_files += 1

    inline_svg = 0
    for body in _SVG_BLOCK_RE.findall(html or ''):
        if _SVG_PAINT_RE.search(body or ''):
            inline_svg += 1

    return {
        'asset_files': asset_files,
        'inline_svg': inline_svg,
        'font_faces': len(_FONT_FACE_RE.findall(html or '')),
        'graphics': asset_files + inline_svg,
    }


def asset_floor_findings(scene: dict, html: str, scene_dir: str, *,
                         mode: str = 'authored') -> list[str]:
    """Findings when an AUTHORED scene ships with no real graphic.

    Scope is deliberate on both axes:

    * **Authored scenes only.** A template fallback carries no graphic by
      construction and is ALREADY reported on the quality axis as a degrade;
      failing it here too would report one defect twice and bury the signal
      that matters (the fallback itself).
    * **Never a hard reject.** Rejecting the composition would degrade the
      scene to the template — a card with *zero* graphics — so the floor would
      make the very metric it measures strictly worse. This is the same trap
      the fill gate documents: the finding travels to the author's repair loop
      and to the film's quality verdict, not to a destructive rollback.
    """
    if mode != 'authored':
        return []
    exempt, _reason = is_text_only_exempt(scene)
    if exempt:
        return []
    counts = count_scene_graphics(html, scene_dir)
    if counts['graphics'] > 0:
        return []
    return [
        'this scene ships NO real graphic — no image/video asset and no '
        'inline SVG that draws anything. A frame of text on a gradient is the '
        'zero-LLM fallback, not an authored composition. Either call '
        'generate_asset for the imagery this beat needs (illustration, '
        'diagram, background texture) or build a real supporting graphic '
        'inline (chart, gauge, drawn icon). If this beat genuinely must be '
        'text-only (a hold or a transition), that is allowed — but it has to '
        'be declared with a text_only_reason on the scene rather than left as '
        'an accident.']


def scene_telemetry(scene: dict, html: str, scene_dir: str, *,
                    mode: str, fill: dict | None,
                    rounds: int = 0, tokens: int = 0,
                    gate_findings: list | None = None) -> dict:
    """The per-scene quality record persisted into ``job.json``.

    ``fill`` is a :func:`lib.motion_video._fill.measure_fill` result the caller
    ALREADY took (or ``None`` when it could not be taken). It is passed in
    rather than measured here so one film boots one browser per scene, not
    two, and so the telemetry and the gate verdict can never disagree about
    the same composition.

    ``None`` fill fields are written as ``None``, never as 0: a measurement
    that did not happen is UNKNOWN, and a 0 would read as "the frame is empty"
    — the worst possible score — in every future diff.
    """
    counts = count_scene_graphics(html, scene_dir)
    exempt, exempt_reason = is_text_only_exempt(scene)
    rec = {
        'scene_id': scene.get('id'),
        'mode': mode,
        'span': round(fill['span'], 4) if fill else None,
        'bottom_dead': round(fill['bottom_dead'], 4) if fill else None,
        'top_dead': round(fill['top_dead'], 4) if fill else None,
        'paint_nodes': fill['nodes'] if fill else None,
        'asset_files': counts['asset_files'],
        'inline_svg': counts['inline_svg'],
        'graphics': counts['graphics'],
        'font_faces': counts['font_faces'],
        'html_bytes': len(html or ''),
        'author_rounds': int(rounds or 0),
        'author_tokens': int(tokens or 0),
        'gate_findings': len(gate_findings or []),
    }
    if exempt:
        rec['text_only_exempt'] = exempt_reason
    return rec


def film_quality_summary(records: list) -> dict:
    """Film-level roll-up of the per-scene records.

    Deliberately reports COUNTS and MEANS over the scenes that were actually
    measured, and states how many were not (``measured``). A mean silently
    computed over a subset is how "the film improved" gets claimed from two
    scenes out of eight.
    """
    if not records:
        return {'scenes': 0, 'measured': 0}
    spans = [r['span'] for r in records if r.get('span') is not None]
    deads = [r['bottom_dead'] for r in records
             if r.get('bottom_dead') is not None]
    graphics = [int(r.get('graphics') or 0) for r in records]
    return {
        'scenes': len(records),
        'measured': len(spans),
        'authored': sum(1 for r in records if r.get('mode') == 'authored'),
        'mean_span': round(sum(spans) / len(spans), 4) if spans else None,
        'min_span': round(min(spans), 4) if spans else None,
        'mean_bottom_dead': round(sum(deads) / len(deads), 4) if deads else None,
        'max_bottom_dead': round(max(deads), 4) if deads else None,
        'total_graphics': sum(graphics),
        'scenes_without_graphics': sum(1 for g in graphics if g == 0),
        'mean_graphics': round(sum(graphics) / len(graphics), 2),
        'scenes_with_font_face': sum(1 for r in records
                                     if int(r.get('font_faces') or 0) > 0),
    }
