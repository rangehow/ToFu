"""End-to-end born-digital PDF translation path.

Entry points:
  * :func:`translate_pdf` — extract → translate (block adapter) → solve →
    render → fidelity gate in translation mode.
  * :func:`main` — thin CLI over the same path with deterministic fake
    translators.  Exit codes match the fidelity CLI: 0 when every page passes,
    1 when any page fails a gate, 2 for unusable input.

The translation callable is injected and has the
:class:`lib.pdf_translate.adapter.ModelCall` shape: ``callable(prompt) -> str``.
It is passed straight to the block adapter, so this module owns no provider
transport, network, key handling or retry policy.  Tests and the CLI use a
deterministic fake translator; no production model path is invoked here.

Source geometry belongs to :mod:`lib.pdf_parser.layout` and
:mod:`lib.pdf_relayout`; the gate belongs to
:mod:`lib.paper.builds.pdf_fidelity`.  This module composes them and adds no
layout tolerance of its own.

The manifest passed to the gate is built from the extraction blocks, one entry
per source block: figures/tables are ``fixed``, blocks the layout bridge keeps
verbatim are ``preserved``, accepted translations are ``translated``, and every
block whose translation failed or was refused is ``refused`` because the
renderer keeps its source text.  A manifest that failed to cover a source block
would make the gate's ``translation_manifest`` check fail rather than pass
vacuously.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import pymupdf

from lib.paper.builds.pdf_fidelity import run_gate
from lib.pdf_parser.layout import page_layout_from_structure
from lib.pdf_parser.render import render_page_plan
from lib.pdf_parser.structure import extract_pdf_structure
from lib.pdf_relayout import PagePlan, solve_page
from lib.pdf_translate import translate_page
from lib.pdf_translate.model import BlockTranslation, PageTranslation

__all__ = [
    'BlockOutcome',
    'TranslationPipelineError',
    'TranslationResult',
    'main',
    'translate_pdf',
]

GATE_MODE = 'translation'

#: Every family the solver's curated stack can emit, so a correctly rendered
#: page never fails the gate merely because the caller omitted a declaration.
DEFAULT_DECLARED_FONTS = {
    'latin': ['DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono', 'Lato'],
    'cjk': ['Noto Sans SC', 'DejaVu Sans'],
}

_FIXED_KINDS = frozenset({'figure', 'table'})
_SOURCE_TEXT_RE = re.compile(r'<source_text>\n(.*)\n</source_text>', re.DOTALL)


class TranslationPipelineError(ValueError):
    """The source document cannot enter the translation path (e.g. no text layer)."""


@dataclass(frozen=True, slots=True)
class BlockOutcome:
    """Final disposition of one extraction block in the produced document."""

    page: int
    block_id: str
    kind: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Structured outcome of one document translation run.

    ``pdf_bytes`` is the merged rendered document, or ``None`` when no page
    could be rendered.  ``page_reports`` is the gate's per-page report list;
    each entry carries ``ok`` and its own ``failures``.  ``failures`` is the
    flat gate failure list, each naming ``page`` and ``check``.
    """

    pdf_bytes: bytes | None
    gate: dict
    page_reports: tuple[dict, ...]
    manifest: dict
    outcomes: tuple[BlockOutcome, ...]
    summary: dict

    @property
    def ok(self) -> bool:
        return bool(self.gate.get('ok'))

    @property
    def failures(self) -> tuple[dict, ...]:
        return tuple(self.gate.get('failures') or ())


def translate_pdf(
    source_pdf: bytes | bytearray | memoryview,
    target_language: str,
    *,
    translate: Callable[[str], str],
    declared_fonts: Mapping[str, Sequence[str]] | None = None,
    font_dirs: Sequence[str] | None = None,
) -> TranslationResult:
    """Translate a born-digital PDF and gate the result in translation mode.

    ``translate(prompt)`` is called once per text-bearing block by
    :func:`lib.pdf_translate.adapter.translate_page`.  A call that raises or
    returns an empty/non-string result fails only its block; the block keeps
    its source text in the output and is reported as ``failed``.

    Raises:
        TranslationPipelineError: the payload is unreadable, password
            protected, or contains a page without a text layer.  There is no
            OCR fallback.
        TypeError: ``translate`` is not callable.
        ValueError: ``target_language`` is empty.
    """
    if not callable(translate):
        raise TypeError('translate must be callable')
    if not isinstance(target_language, str) or not target_language.strip():
        raise ValueError('target_language must be a non-empty string')
    payload = _coerce_pdf_bytes(source_pdf)

    structure = extract_pdf_structure(payload)
    if not structure.get('supported'):
        pages = ', '.join(
            f"{item['page_number'] + 1} ({item['reason']})"
            for item in structure.get('unsupported_pages') or ()
        )
        raise TranslationPipelineError(
            f'source has pages without a usable text layer: {pages}'
        )

    manifest_pages: list[dict] = []
    outcomes: list[BlockOutcome] = []
    rendered_pages: list[bytes] = []
    for page in structure.get('pages') or ():
        adapted = page_layout_from_structure(page)
        translation = translate_page(
            page,
            target_language=target_language,
            model_call=translate,
        )
        translations = dict(translation.translations)
        plan = solve_page(adapted.layout, translations, font_dirs=font_dirs)
        render = render_page_plan(payload, plan)
        rendered_pages.append(render.pdf_bytes)
        page_blocks, page_outcomes = _page_evidence(page, adapted, translation, plan)
        manifest_pages.append({'page': page['page_number'] + 1,
                               'blocks': page_blocks})
        outcomes.extend(page_outcomes)

    rendered_pdf = _merge_pages(rendered_pages) if rendered_pages else None
    manifest = {'pages': manifest_pages}
    if rendered_pdf is None:
        raise TranslationPipelineError('source PDF has no pages to translate')

    gate = run_gate(
        payload,
        rendered_pdf,
        mode=GATE_MODE,
        declared_fonts=(dict(DEFAULT_DECLARED_FONTS) if declared_fonts is None
                        else dict(declared_fonts)),
        translation=manifest,
    )
    return TranslationResult(
        pdf_bytes=rendered_pdf,
        gate=gate,
        page_reports=tuple(gate.get('pages') or ()),
        manifest=manifest,
        outcomes=tuple(outcomes),
        summary=_summary(outcomes, len(manifest_pages)),
    )


# ── per-page evidence ─────────────────────────────────────────────────────


def _page_evidence(page: Mapping[str, Any], adapted, translation: PageTranslation,
                   plan: PagePlan) -> tuple[list[dict], list[BlockOutcome]]:
    page_number = int(page['page_number'])
    preserved = {item.block_id for item in adapted.preserved}
    results = {item.block_id: item for item in translation.blocks}
    manifest_blocks: list[dict] = []
    outcomes: list[BlockOutcome] = []
    for block in page.get('blocks') or ():
        index = block.get('index')
        if index is None:
            continue
        block_id = f'p{page_number}-b{index}'
        kind = str(block.get('kind') or '')
        text = block.get('text') or ''
        if kind not in _FIXED_KINDS and not text.strip():
            continue  # the source page has no block here for the gate to cover
        outcome = _classify(block_id, kind, block_id in preserved,
                            results.get(block_id), plan)
        outcomes.append(BlockOutcome(
            page=page_number + 1,
            block_id=block_id,
            kind=kind,
            status=outcome[0],
            detail=outcome[1],
        ))
        status = _manifest_status(kind, outcome[0])
        if status is None:
            continue
        manifest_blocks.append({
            'source_bbox': [float(value) for value in block['bbox']],
            'status': status,
            'source_text': text,
        })
    return manifest_blocks, outcomes


def _classify(block_id: str, kind: str, preserved: bool,
              result: BlockTranslation | None,
              plan: PagePlan) -> tuple[str, str | None]:
    if kind in _FIXED_KINDS:
        return 'skipped', 'fixed geometry'
    if preserved:
        return 'preserved', 'kept verbatim by the layout bridge'
    if result is None:
        return 'skipped', 'no translation result'
    if result.status == 'skipped_no_text':
        return 'skipped', 'no translatable text'
    if result.status == 'failed':
        if result.refusal is not None:
            return 'refused', result.refusal.message
        return 'failed', result.error or 'translation call failed'
    if plan.placement(block_id) is not None:
        return 'translated', None
    refusal = next((item for item in plan.refusals
                    if item.block_id == block_id), None)
    return 'refused', refusal.message if refusal is not None else 'not placed'


def _manifest_status(kind: str, outcome: str) -> str | None:
    if kind in _FIXED_KINDS:
        return 'fixed'
    if outcome == 'translated':
        return 'translated'
    if outcome == 'preserved':
        return 'preserved'
    if outcome in ('refused', 'failed'):
        # The renderer keeps a failed block's source text, so it is refused
        # text for the gate's refused_text_present check.
        return 'refused'
    return None  # skipped text-bearing blocks have no gate block to cover


def _summary(outcomes: Sequence[BlockOutcome], page_count: int) -> dict:
    totals = Counter(outcome.status for outcome in outcomes)
    pages = []
    for page in range(1, page_count + 1):
        counts = Counter(item.status for item in outcomes if item.page == page)
        pages.append({'page': page, **_counts(counts)})
    return {'pages': pages, **_counts(totals)}


def _counts(counter: Counter) -> dict:
    return {
        'translated': counter.get('translated', 0),
        'refused': counter.get('refused', 0),
        'skipped': counter.get('skipped', 0),
        'failed': counter.get('failed', 0),
        'preserved': counter.get('preserved', 0),
    }


def _merge_pages(pages: Sequence[bytes]) -> bytes:
    merged = pymupdf.open()
    try:
        for payload in pages:
            page = pymupdf.open(stream=payload, filetype='pdf')
            try:
                merged.insert_pdf(page)
            finally:
                page.close()
        return merged.tobytes(garbage=3, deflate=True)
    finally:
        merged.close()


def _coerce_pdf_bytes(source_pdf) -> bytes:
    if not isinstance(source_pdf, (bytes, bytearray, memoryview)):
        raise TypeError('source_pdf must be PDF bytes')
    return bytes(source_pdf)


# ── CLI fake translators ─────────────────────────────────────────────────


def _prompt_source(prompt: str) -> str:
    match = _SOURCE_TEXT_RE.search(prompt)
    if match is None:
        raise ValueError('prompt does not carry a <source_text> block')
    return match.group(1).replace('&lt;/source_text&gt;', '</source_text>')


def _fake_translator(name: str) -> Callable[[str], str]:
    """Deterministic stand-ins for a model; used by the CLI acceptance runs."""
    if name == 'identity':
        return _prompt_source
    if name == 'upper':
        return lambda prompt: _prompt_source(prompt).upper()
    if name == 'overlong':
        return lambda prompt: (_prompt_source(prompt) + ' ') * 6
    if name == 'raise-first':
        state = {'raised': False}

        def raise_once(prompt: str) -> str:
            if not state['raised']:
                state['raised'] = True
                raise RuntimeError('injected translator failure')
            return _prompt_source(prompt).upper()
        return raise_once
    raise ValueError(f'unknown fake translator {name!r}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m lib.paper.builds.pdf_translation',
        description='Translate a born-digital PDF with a deterministic fake '
                    'translator and gate the output in translation mode.')
    parser.add_argument('--source', required=True, help='input PDF')
    parser.add_argument('--output', required=True, help='rendered output PDF')
    parser.add_argument('--target-language', required=True)
    parser.add_argument('--translator', default='upper',
                        choices=('identity', 'upper', 'overlong', 'raise-first'))
    parser.add_argument('--fonts', help='JSON declared stack for the gate')
    parser.add_argument('--report', help='write the gate report JSON here')
    parser.add_argument('--manifest', help='write the built manifest JSON here')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    try:
        source_pdf = open(args.source, 'rb').read()
        declared = json.loads(args.fonts) if args.fonts else None
        result = translate_pdf(
            source_pdf,
            args.target_language,
            translate=_fake_translator(args.translator),
            declared_fonts=declared,
        )
        with open(args.output, 'wb') as stream:
            stream.write(result.pdf_bytes or b'')
        if args.manifest:
            with open(args.manifest, 'w', encoding='utf-8') as stream:
                json.dump(result.manifest, stream, ensure_ascii=False, indent=2)
        if args.report:
            with open(args.report, 'w', encoding='utf-8') as stream:
                json.dump(result.gate, stream, ensure_ascii=False, indent=2)
    except Exception as exc:  # unusable input, never a gate verdict
        print(f'pdf translation error: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps({'ok': result.ok,
                          'summary': result.summary,
                          'failures': list(result.failures)},
                         ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())