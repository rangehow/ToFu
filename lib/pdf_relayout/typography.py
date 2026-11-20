"""Typographic limits and curated font fallback order for PDF re-layout.

The solver owns *geometry*, not rendering.  These limits are the documented
contract shared with the later renderer:

* A translated body block may shrink its font to
  :data:`MIN_READABLE_FONT_SIZE_PT` (7 pt) but never below it.  Seven point is
  the smallest size that remains legible in print and at 100% zoom without
  hinting tricks; smaller type is a refusal, not a silent quality loss.
* Leading is reduced before font size, down to a floor of
  :data:`MIN_LEADING_RATIO` times the font size (1.08).  Tighter leading makes
  ascenders and descenders collide, so the floor is a correctness bound.
* Blocks keep :data:`BLOCK_GAP_PT` clear of the next block or fixed element.
  A caption keeps its original separation from its figure, but never less than
  :data:`CAPTION_MIN_GAP_PT`.

Fonts are resolved from installed families in a fixed order.  Measurement uses
the actual glyph advances from each face (fontTools), so changing the stack
changes the plan consistently.  Every family named here is expected to exist
on a supported host; :func:`lib.pdf_relayout.fonts.missing_stack_families`
reports gaps without guessing.
"""

from __future__ import annotations

MIN_READABLE_FONT_SIZE_PT = 7.0
MIN_LEADING_RATIO = 1.08
BLOCK_GAP_PT = 4.0
CAPTION_MIN_GAP_PT = 2.0

#: Ordered fallback per script/style.  Latin text prefers the sans/serif/mono
#: family matching the source style and falls through to DejaVu and finally
#: Noto Sans SC (which also carries Latin).  CJK text starts at Noto Sans SC
#: because no other installed family has Han coverage.
FONT_FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "sans": ("DejaVu Sans", "Lato", "Noto Sans SC"),
    "serif": ("DejaVu Serif", "Lato", "Noto Sans SC"),
    "mono": ("DejaVu Sans Mono", "Noto Sans SC"),
    "cjk": ("Noto Sans SC", "DejaVu Sans"),
}

#: Standard locations searched for the families above.  ``~/.fonts`` is
#: included because a user-scoped Noto Sans SC install is common on Linux.
FONT_DIRECTORIES: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
)

FIXED_BLOCK_KINDS = frozenset({"figure", "table"})
FLOWABLE_BLOCK_KINDS = frozenset({"text", "heading", "caption", "list", "formula"})

#: Kinds whose translation is optional; fixed geometry carries no text.
TEXT_BEARING_KINDS = FLOWABLE_BLOCK_KINDS


def style_for_family(family: str) -> str:
    """Map a source font family name to a curated style stack key."""
    name = (family or "").casefold()
    if any(token in name for token in ("mono", "courier", "consol", "code")):
        return "mono"
    if "sans" not in name and any(
        token in name for token in ("serif", "times", "roman", "georgia", "garamond")
    ):
        return "serif"
    return "sans"


def weight_for_font_weight(weight: str) -> str:
    """Collapse a source weight descriptor onto the two installed weights."""
    name = (weight or "").casefold()
    if any(token in name for token in ("bold", "black", "heavy", "semibold", "demi")):
        return "bold"
    return "regular"