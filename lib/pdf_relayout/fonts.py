"""Installed-font resolution and glyph-accurate advances for re-layout.

No renderer is involved: fontTools reads each face's ``cmap``/``hmtx``/``head``
tables, and the solver measures the same advance the later renderer will use.
The fallback order lives in :mod:`lib.pdf_relayout.typography`; this module
only resolves it against the host's font directories and reports families that
are absent instead of substituting silently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from fontTools.ttLib import TTCollection, TTFont

from .typography import FONT_DIRECTORIES, FONT_FALLBACK_ORDER

_FONT_SUFFIXES = frozenset({'.ttf', '.otf', '.ttc'})
_ACCEPTED_SUBFAMILIES = frozenset({'regular', 'book', 'roman', 'normal', 'bold'})
_CJK_RANGES = (
    (0x2E80, 0x2FFF),    # CJK radicals, Kangxi, symbols
    (0x3000, 0x303F),    # CJK punctuation
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xA960, 0xA97F),    # Hangul Jamo Extended-A
    (0xAC00, 0xD7AF),    # Hangul syllables
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFE30, 0xFE4F),    # CJK compatibility forms
    (0xFF00, 0xFFEF),    # Halfwidth/fullwidth forms
    (0x20000, 0x2FA1F),  # CJK Extension B+
)


class FontEnvironmentError(RuntimeError):
    """No installed face can serve a requested script."""


def is_cjk_char(ch: str) -> bool:
    codepoint = ord(ch)
    return any(low <= codepoint <= high for low, high in _CJK_RANGES)


@dataclass(frozen=True, slots=True)
class FontFace:
    family: str
    path: str
    index: int = 0
    weight: str = 'regular'

    def covers(self, ch: str) -> bool:
        return ord(ch) in _font_data(self.path, self.index).cmap

    def advance(self, ch: str, size_pt: float) -> float:
        data = _font_data(self.path, self.index)
        glyph = data.cmap.get(ord(ch))
        if glyph is None:
            width = data.notdef_advance
        else:
            width = data.advances.get(glyph, data.notdef_advance)
        return width / data.units_per_em * size_pt

    @property
    def ascent_ratio(self) -> float:
        return _font_data(self.path, self.index).ascent_ratio


@dataclass(frozen=True, slots=True)
class _FontData:
    units_per_em: int
    cmap: dict[int, str]
    advances: dict[str, int]
    notdef_advance: int
    ascent_ratio: float


@lru_cache(maxsize=64)
def _font_data(path: str, index: int) -> _FontData:
    font = TTFont(path, fontNumber=index, lazy=True)
    try:
        cmap = dict(font.getBestCmap() or {})
        units_per_em = int(font['head'].unitsPerEm) or 1000
        hmtx = font['hmtx'].metrics
        advances = {glyph: metrics[0] for glyph, metrics in hmtx.items()}
        notdef = advances.get('.notdef', units_per_em // 2)
        # PyMuPDF reports a text line's top as ``baseline - hhea.ascent``;
        # the solver's baseline = line_top + size * ascent_ratio must invert
        # exactly that relation.  OS/2 sTypoAscender is smaller (DejaVu: 1556
        # vs hhea 1901), so using it shifts every painted baseline upward.
        ascent = int(font['hhea'].ascent)
        if not ascent:
            os2 = font.get('OS/2')
            ascent = int(getattr(os2, 'sTypoAscender', 0) or 0) if os2 else 0
        ascent_ratio = max(0.7, min(1.4, ascent / units_per_em))
        return _FontData(
            units_per_em=units_per_em,
            cmap=cmap,
            advances=advances,
            notdef_advance=notdef,
            ascent_ratio=ascent_ratio,
        )
    finally:
        font.close()


def _face_names(font: TTFont) -> tuple[str, str] | None:
    try:
        name = font['name']
        family = name.getDebugName(16) or name.getDebugName(1) or ''
        subfamily = name.getDebugName(17) or name.getDebugName(2) or ''
    except Exception:
        return None
    return (family.strip(), subfamily.strip()) if family else None


def _classify_subfamily(subfamily: str) -> str | None:
    tokens = subfamily.casefold().replace('-', ' ').split()
    if not tokens:
        return None
    if any(token in {'italic', 'oblique', 'light', 'thin', 'condensed'} for token in tokens):
        return None
    if set(tokens) <= _ACCEPTED_SUBFAMILIES:
        if 'bold' in tokens:
            return 'bold'
        return 'regular'
    return None


def _iter_candidate_faces(path: str, suffix: str) -> Iterable[tuple[int, TTFont, bool]]:
    """Yield (index, font, owned) for every face in a file."""
    if suffix == '.ttc':
        collection = TTCollection(path, lazy=True)
        try:
            for index, font in enumerate(collection.fonts):
                yield index, font, False
        finally:
            collection.close()
        return
    font = TTFont(path, fontNumber=0, lazy=True)
    try:
        yield 0, font, True
    finally:
        font.close()


@lru_cache(maxsize=8)
def _build_index(directories: tuple[str, ...]) -> dict[str, dict[str, tuple[FontFace, ...]]]:
    index: dict[str, dict[str, list[FontFace]]] = {}
    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for current, _dirs, files in os.walk(root):
            for filename in files:
                suffix = Path(filename).suffix.casefold()
                if suffix not in _FONT_SUFFIXES:
                    continue
                path = str(Path(current) / filename)
                try:
                    for face_index, font, _owned in _iter_candidate_faces(path, suffix):
                        names = _face_names(font)
                        if names is None:
                            continue
                        family, subfamily = names
                        weight = _classify_subfamily(subfamily)
                        if weight is None:
                            continue
                        bucket = index.setdefault(family.casefold(), {})
                        bucket.setdefault(weight, []).append(
                            FontFace(family=family, path=path, index=face_index, weight=weight)
                        )
                except Exception:
                    continue
    return {
        family: {weight: tuple(faces) for weight, faces in weights.items()}
        for family, weights in index.items()
    }


def _directories_key(directories: Iterable[str] | None) -> tuple[str, ...]:
    raw = FONT_DIRECTORIES if directories is None else tuple(directories)
    return tuple(str(Path(item).expanduser()) for item in raw)


def missing_stack_families(directories: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return ``stack:family`` entries whose family is not installed."""
    index = _build_index(_directories_key(directories))
    missing = []
    for stack, families in FONT_FALLBACK_ORDER.items():
        for family in families:
            if not index.get(family.casefold()):
                missing.append(f'{stack}:{family}')
    return tuple(missing)


class FontLibrary:
    """Per-solve resolver with a per-character face cache."""

    def __init__(self, directories: Iterable[str] | None = None) -> None:
        self._index = _build_index(_directories_key(directories))
        self._faces: dict[tuple[str, str], FontFace | None] = {}
        self._char_cache: dict[tuple[str, str, str], FontFace] = {}
        self.missing_glyphs: set[str] = set()

    def _pick(self, family: str, weight: str) -> FontFace | None:
        key = (family.casefold(), weight)
        if key in self._faces:
            return self._faces[key]
        weights = self._index.get(family.casefold(), {})
        face = None
        for candidate_weight in ((weight, 'regular') if weight == 'bold' else ('regular', 'bold')):
            faces = weights.get(candidate_weight)
            if faces:
                face = faces[0]
                break
        self._faces[key] = face
        return face

    def face_for_char(self, ch: str, style: str, weight: str) -> FontFace:
        cache_key = (ch, style, weight)
        cached = self._char_cache.get(cache_key)
        if cached is not None:
            return cached
        stack_key = 'cjk' if is_cjk_char(ch) else style
        families = FONT_FALLBACK_ORDER[stack_key]
        first: FontFace | None = None
        for family in families:
            face = self._pick(family, weight)
            if face is None:
                continue
            if first is None:
                first = face
            if face.covers(ch):
                self._char_cache[cache_key] = face
                return face
        if first is None:
            raise FontEnvironmentError(
                f'no installed font serves {ch!r} ({stack_key}); checked {families!r}'
            )
        self.missing_glyphs.add(ch)
        self._char_cache[cache_key] = first
        return first