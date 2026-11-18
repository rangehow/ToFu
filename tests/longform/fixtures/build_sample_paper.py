"""Materialize the synthetic sample paper for the document pipeline.

Usage: python3 tests/longform/fixtures/build_sample_paper.py <target_dir>

Copies ``sample_paper/`` to ``<target_dir>`` and compiles its vector figure
(pgfplots) and its previous, weaker draft (``previous/previous.pdf``, the
blind A/B baseline) with the paper toolchain owner. Needs latexmk/pdflatex
with pgfplots on PATH.
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
    shutil.copytree(SOURCE, target, dirs_exist_ok=True)
    figure = compile_project(target / 'figures')
    if figure.get('status') != 'success':
        raise RuntimeError(f'figure build failed: {figure}')
    previous = compile_project(target / 'previous')
    if not previous.get('pdf'):
        raise RuntimeError(f'previous draft build failed: {previous}')
    shutil.copy2(previous['pdf'], target / 'previous' / 'previous.pdf')
    return target


if __name__ == '__main__':
    print(build(Path(sys.argv[1])))
