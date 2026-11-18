"""Materialize the synthetic sample paper for the document pipeline.

Usage: python3 tests/longform/fixtures/build_sample_paper.py <target_dir> [--draft]

Default: copies ``sample_paper/`` to ``<target_dir>`` and compiles its vector
figure (pgfplots) and its previous, weaker draft (``previous/previous.pdf``,
the blind A/B baseline) — the "write from the dossier" mode.

``--draft``: lays out a paper directory that already holds a LaTeX draft
(``sample_paper/draft/``: ``main.tex``, an outdated ``acl.sty``, its brief),
with the compiled figure, results and bibliography beside it — the
"revise an existing draft" mode. Needs latexmk/pdflatex with pgfplots.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = Path(__file__).resolve().parent / 'sample_paper'


def build(target: Path) -> Path:
    from lib.paper.latex_layout import compile_project
    target = Path(target)
    shutil.copytree(SOURCE, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('draft'))
    figure = compile_project(target / 'figures')
    if figure.get('status') != 'success':
        raise RuntimeError(f'figure build failed: {figure}')
    previous = compile_project(target / 'previous')
    if not previous.get('pdf'):
        raise RuntimeError(f'previous draft build failed: {previous}')
    shutil.copy2(previous['pdf'], target / 'previous' / 'previous.pdf')
    return target


def build_draft(target: Path) -> Path:
    from lib.paper.latex_layout import compile_project
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for name in ('figures', 'results'):
        shutil.copytree(SOURCE / name, target / name, dirs_exist_ok=True)
    shutil.copy2(SOURCE / 'references.bib', target / 'references.bib')
    shutil.copytree(SOURCE / 'draft', target, dirs_exist_ok=True)
    figure = compile_project(target / 'figures')
    if figure.get('status') != 'success':
        raise RuntimeError(f'figure build failed: {figure}')
    for leftover in (target / 'figures').iterdir():
        if leftover.suffix in ('.aux', '.log', '.fls', '.fdb_latexmk') or \
                leftover.name.endswith('.synctex.gz'):
            leftover.unlink()
    return target


if __name__ == '__main__':
    print(build_draft(Path(sys.argv[1])) if '--draft' in sys.argv[2:]
          else build(Path(sys.argv[1])))
