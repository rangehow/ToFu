"""Render QA for staged documents: assemble, compile, and gate the PDF.

Responsibility: build a self-contained LaTeX directory from the template kit,
figures, bibliography and drafted sections; compile it with the paper
toolchain owner (``lib.paper.latex_layout.compile_project``); and report every
render defect the gates refuse: TeX errors, overfull boxes, undefined
references/citations, body pages over the limit (a ``\\label`` at the end of
the page-limited body, read back from the ``.aux``), template drift from the
fetched kit, and the paper quality owner's source/figure/font/DPI rows
(``lib.paper.checks``).

Entry points: ``assemble``, ``compile_and_check``, ``pdf_text``.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

__all__ = ['assemble', 'compile_and_check', 'pdf_text', 'BODY_END_LABEL']

BODY_END_LABEL = 'tofu-pipeline-body-end'
_OVERFULL_RE = re.compile(r'^Overfull \\[hv]box \(([\d.]+)pt too \w+\)(.*)$',
                          re.MULTILINE)
_UNDEFINED_RE = re.compile(
    r"LaTeX Warning: (?:Reference|Citation) `([^']+)' on page \d+ undefined")
_NATBIB_UNDEFINED_RE = re.compile(
    r"Package natbib Warning: Citation `([^']+)' on page \d+ undefined")
_BODY_END_RE = re.compile(
    r'\\newlabel\{' + re.escape(BODY_END_LABEL) + r'\}\{\{[^}]*\}\{(\d+)\}')


def figure_block(figure: dict) -> str:
    width = '\\linewidth'
    return ('\\begin{figure}[t]\n\\centering\n'
            f'\\includegraphics[width={width}]{{{figure["file"]}}}\n'
            f'\\caption{{{figure["caption"]}}}\n'
            f'\\label{{{figure["label"]}}}\n\\end{{figure}}\n')


def assemble(build: Path, *, doc_type, brief: dict, kit: dict, kit_dir: Path,
             paper_dir: Path, sections: dict, outline: dict, figures: list,
             bib_text: str) -> Path:
    """Write ``build/main.tex`` and its inputs; return the build directory."""
    from lib.longform.documents.venues import frame_main_tex
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    for leaf in kit.get('files') or {}:
        shutil.copy2(kit_dir / leaf, build / leaf)
    for figure in figures:
        target = build / figure['file']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paper_dir / figure['file'], target)
    (build / 'references.bib').write_text(bib_text, encoding='utf-8')
    by_label = {figure['label']: figure for figure in figures}
    titles = {row['id']: row['title'] for row in outline['sections']}
    parts = []
    for section_id, _default in doc_type.sections:
        if section_id == 'abstract':
            continue
        text = sections[section_id]
        floats = ''.join(figure_block(by_label[label]) for label in
                         next(row for row in outline['sections']
                              if row['id'] == section_id).get('figures') or [])
        parts.append(f'\\section{{{titles[section_id]}}}\n'
                     f'\\label{{sec:{section_id}}}\n{floats}{text}\n')
        if section_id == doc_type.body_end_after:
            parts.append(f'\\label{{{BODY_END_LABEL}}}\n')
    body = '\n'.join(parts)
    if doc_type.needs_venue:
        tex = frame_main_tex(brief['venue'], kit, title=brief['title'],
                             authors=brief.get('authors') or 'Anonymous',
                             abstract=sections['abstract'], body=body, bibfile='references')
    else:
        template = (paper_dir / brief['main_template']).read_text(encoding='utf-8')
        tex = template.replace('%%BODY%%', body)
    (build / 'main.tex').write_text(tex, encoding='utf-8')
    return build


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pdf_text(pdf: Path) -> str:
    tool = shutil.which('pdftotext')
    if tool:
        proc = subprocess.run([tool, '-enc', 'UTF-8', '-layout', str(pdf), '-'],
                              capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return proc.stdout
    from pypdf import PdfReader
    return '\n'.join(page.extract_text() or '' for page in PdfReader(str(pdf)).pages)


def pdf_pages(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


_USEPACKAGE_RE = re.compile(r'\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}')


def forbidden_package_uses(build: Path, main: str, forbidden: tuple[str, ...]) -> list[str]:
    """Venue-forbidden packages the document's own sources load."""
    from lib.longform.documents.existing_draft import expand
    text = expand(build, main)
    used = {name.strip() for group in _USEPACKAGE_RE.findall(text) for name in group.split(',')}
    return sorted(used & set(forbidden))


def compile_and_check(build: Path, *, engine: str, page_limit: int,
                      kit: dict, main: str = 'main.tex',
                      forbidden_packages: tuple[str, ...] = ()) -> dict:
    """Compile and return the QA report; ``failures`` empty means clean."""
    from lib.paper.checks import run_pdf_file_checks, run_source_checks
    from lib.paper.latex_layout import compile_project
    stem = Path(main).with_suffix('')
    result = compile_project(build, engine=engine, main=main)
    failures: list[str] = []
    report = {'compile_status': result.get('status'), 'engine': engine,
              'errors': result.get('errors') or []}
    # latexmk also exits nonzero for warnings it cannot resolve (undefined
    # citations); a PDF without TeX errors is checked below instead.
    if not result.get('pdf') or result.get('errors'):
        failures.append(f'compile {result.get("status")}: '
                        + '; '.join(result.get('errors') or [])[:2000])
        report['failures'] = failures
        return report
    log = Path(result['log']).read_text(encoding='utf-8', errors='replace')
    overfull = [f'{amount}pt{where}'.strip() for amount, where in
                _OVERFULL_RE.findall(log)]
    undefined = sorted(set(_UNDEFINED_RE.findall(log))
                       | set(_NATBIB_UNDEFINED_RE.findall(log)))
    blg = build / stem.with_suffix('.blg')
    if blg.is_file():
        bib_errors = re.findall(r'^(.*)---line \d+ of file', blg.read_text(
            encoding='utf-8', errors='replace'), re.MULTILINE)
        if bib_errors:
            failures.append(f'BibTeX errors: {bib_errors}')
    if overfull:
        failures.append(f'{len(overfull)} overfull box(es): {overfull}')
    if undefined:
        failures.append(f'undefined references/citations: {undefined}')
    aux_path = build / stem.with_suffix('.aux')
    aux = aux_path.read_text(encoding='utf-8', errors='replace') if aux_path.is_file() else ''
    match = _BODY_END_RE.search(aux)
    pdf = Path(result['pdf'])
    pages = pdf_pages(pdf)
    body_pages = int(match.group(1)) if match else pages
    if body_pages > page_limit:
        failures.append(f'body ends on page {body_pages}, over the '
                        f'{page_limit}-page limit')
    drift = sorted(leaf for leaf, digest in (kit.get('files') or {}).items()
                   if not (build / leaf).is_file() or _sha(build / leaf) != digest)
    if drift:
        failures.append(f'template files differ from the fetched official kit: {drift}')
    banned = forbidden_package_uses(build, main, forbidden_packages)
    if banned:
        failures.append(f'the venue forbids packages {banned}')
    from lib.longform.documents.existing_draft import reachable_tex
    compiled = reachable_tex(build, main)
    # The tree may carry files the document never inputs (a vendored template
    # sample, old drafts); only the compiled sources are this paper's defects.
    source_rows = [row for row in run_source_checks(build, main)
                   if not row.get('file') or not row['file'].endswith(('.tex', '.ltx'))
                   or row['file'] in compiled]
    pdf_rows = run_pdf_file_checks(pdf)
    for row in source_rows + pdf_rows:
        if row['severity'] in ('error', 'warning'):
            failures.append(f'{row["code"]}: {row["message"]}'
                            + (f' ({row["file"]}:{row["line"]})' if row.get('file') else ''))
    report.update({
        'pdf': str(pdf), 'pdf_sha256': _sha(pdf), 'pages': pages,
        'body_pages': body_pages, 'page_limit': page_limit,
        'overfull': overfull, 'undefined': undefined,
        'quality_rows': source_rows + pdf_rows, 'failures': failures,
    })
    return report
