"""Executable spec: malformed/corrupted TeX length specs and real overfull boxes
are reported by the paper gates, never passed silently.

Origin: a number formatter rewrote ``p{0.05\\textwidth}`` into
``p{5\\textwidth}``; the compile log carried a 2,400 pt ``Overfull \\hbox`` yet
the paper build diagnostics, the source lint and the template gate all passed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lib.paper.builds.badboxes import overfull_boxes
from lib.paper.builds.compiler import overfull_diagnostics
from lib.paper.checks import run_source_checks, run_tex_checks

pytestmark = pytest.mark.unit

_PDFLATEX = shutil.which('pdflatex')
needs_tex = pytest.mark.skipif(not _PDFLATEX, reason='pdflatex not on PATH')

# Shape of a real pdfTeX log (TeX Live 2026) for the corrupted column spec:
# the bad paragraph started inside tables/tab.tex and ended in main.tex.
REAL_LOG = r"""This is pdfTeX, Version 3.141592653-2.6-1.40.28 (TeX Live 2026)
(./main.tex
LaTeX2e <2025-11-01>
(./tables/tab.tex
LaTeX Font Info:    External font `cmex10' loaded for size
(Font)              <5> on input line 1.
)
Overfull \hbox (1528.20021pt too wide) in paragraph at lines 1--7
[][]
 []


Overfull \hbox (293.13057pt too wide) in paragraph at lines 4--9
[]\OT1/cmr/m/n/10 Some text (an unbalanced paren in the box text
 []

Underfull \hbox (badness 10000) in paragraph at lines 11--12
 []

Overfull \vbox (12.0pt too high) has occurred while \output is active []

Overfull \hbox (3.5pt too wide) detected at line 20
[]
(./main.aux) )
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return tmp_path


def _length_rows(tmp_path, body: str):
    root = _tree(tmp_path, {'main.tex': '\\documentclass{article}\n\\begin{document}\n'
                            + body + '\n\\end{document}\n'})
    return [row for row in run_source_checks(root, 'main.tex')
            if row['code'].startswith('length_')]


# ── overfull boxes from the TeX log ──────────────────────────────────────

def test_overfull_records_are_parsed_with_amount_lines_and_file():
    boxes = overfull_boxes(REAL_LOG)
    assert [(b['box'], b['amount_pt'], b['where']) for b in boxes] == [
        ('hbox', 1528.20021, 'paragraph'), ('hbox', 293.13057, 'paragraph'),
        ('vbox', 12.0, 'output'), ('hbox', 3.5, 'detected')]
    assert boxes[0]['lines'] == (1, 7) and boxes[0]['file'] == './main.tex'
    assert boxes[3]['lines'] == (20, 20)
    # Box text after a record is skipped, so its stray paren cannot corrupt
    # the open-file stack used for attribution.
    assert boxes[3]['file'] == './main.tex'
    assert overfull_boxes('Underfull \\hbox (badness 10000) in paragraph at lines 1--2\n') == []


def test_build_diagnostics_report_every_overfull_box_as_a_warning(tmp_path):
    (tmp_path / 'main.tex').write_text('x')
    rows = overfull_diagnostics(REAL_LOG, source_root=tmp_path, base_dir=tmp_path)
    assert [row['code'] for row in rows] == ['overfull_hbox', 'overfull_hbox',
                                             'overfull_vbox', 'overfull_hbox']
    assert {row['severity'] for row in rows} == {'warning'}
    assert rows[0]['file'] == 'main.tex' and rows[0]['line'] == 1
    assert '1528.20021pt too wide' in rows[0]['message']
    assert overfull_diagnostics('no boxes here\n', source_root=tmp_path, base_dir=tmp_path) == []


# ── length specs in the source ───────────────────────────────────────────

def test_corrupted_line_relative_width_is_flagged(tmp_path):
    rows = _length_rows(tmp_path, '\\begin{tabular}{p{5\\textwidth}>{\\raggedright\\arraybackslash}'
                                  'p{0.36\\textwidth}}a&b\\end{tabular}')
    assert [(row['code'], row['severity']) for row in rows] == [('length_exceeds_line', 'warning')]
    assert 'p{5\\textwidth}' in rows[0]['message'] and rows[0]['line'] == 3


@pytest.mark.parametrize('body', [
    '\\begin{tabular}{p{0.05\\textwidth}p{.5\\linewidth}m{1,5cm}b{3em}}x\\end{tabular}',
    '\\begin{tabular}{@{}p{0.24\\textwidth}|p{\\columnwidth}@{}}x\\end{tabular}',
    '\\begin{tabularx}{\\textwidth}{lX}x\\end{tabularx}',
    '\\begin{tabular*}{0.98\\textwidth}{l}x\\end{tabular*}',
    '\\begin{tabular}{p{0.3\\textwidth-2\\tabcolsep}p{\\dimexpr 0.5\\linewidth\\relax}}x\\end{tabular}',
    '\\begin{minipage}[t]{0.48\\linewidth}x\\end{minipage}\\parbox{12 pt}{y}',
    '\\includegraphics[width=0.9\\linewidth,height=3cm]{fig}',
    '\\begin{tabular}{p{1\\textwidth}}x\\end{tabular}',
    '\\begin{tabular}{p{1truein}}x\\end{tabular}',
])
def test_valid_and_unjudgeable_lengths_pass(tmp_path, body):
    assert _length_rows(tmp_path, body) == []


@pytest.mark.parametrize('spec', ['p{5}', 'p{0..05\\textwidth}', 'p{2ptt}', 'p{pt}'])
def test_malformed_lengths_are_errors(tmp_path, spec):
    rows = _length_rows(tmp_path, '\\begin{tabular}{' + spec + 'l}x\\end{tabular}')
    assert [(row['code'], row['severity']) for row in rows] == [('length_malformed', 'error')]


def test_scaled_float_downgrades_wide_spec_to_info(tmp_path):
    rows = _length_rows(tmp_path, '\\begin{table}\\resizebox{\\linewidth}{!}{'
                                  '\\begin{tabular}{p{1.4\\linewidth}}x\\end{tabular}}'
                                  '\\caption{T.}\\label{t}\\end{table} Table~\\ref{t}.')
    assert [(row['code'], row['severity']) for row in rows] == [('length_exceeds_line', 'info')]


def test_includegraphics_and_minipage_widths_are_checked(tmp_path):
    rows = _length_rows(tmp_path, '\\includegraphics[width=1.5\\linewidth]{fig}'
                                  '\\begin{minipage}{12\\columnwidth}x\\end{minipage}')
    assert sorted(row['code'] for row in rows) == ['length_exceeds_line'] * 2


# ── run_tex_checks: the built-paper gate (source + log) ──────────────────

def test_run_tex_checks_combines_source_and_log_rows(tmp_path):
    root = _tree(tmp_path, {
        'main.tex': '\\documentclass{article}\n\\begin{document}\n\\input{tables/tab}\n\\end{document}\n',
        'tables/tab.tex': '\\begin{tabular}{p{5\\textwidth}}a\\end{tabular}\n',
        'build/main.log': REAL_LOG})
    rows = run_tex_checks(root, 'main.tex', log=root / 'build/main.log')
    codes = [row['code'] for row in rows]
    assert codes.count('overfull_hbox') == 3 and 'overfull_vbox' in codes
    assert any(row['code'] == 'length_exceeds_line' and row['file'] == 'tables/tab.tex'
               for row in rows)
    (root / 'build/failed.log').write_text(
        "(./main.tex\n! LaTeX Error: File `icml2027.sty' not found.\n\n"
        '!  ==> Fatal error occurred, no output PDF file produced!\n', encoding='utf-8')
    failed = run_tex_checks(root, 'main.tex', log=root / 'build/failed.log')
    assert any(row['severity'] == 'error' and 'icml2027.sty' in row['message'] for row in failed)
    missing = run_tex_checks(root, 'main.tex', log=root / 'build/absent.log')
    assert any(row['code'] == 'tex_log_unavailable' and row['severity'] == 'error'
               for row in missing)


def _compile(root: Path) -> str:
    subprocess.run([_PDFLATEX, '-interaction=nonstopmode', '-file-line-error', 'main.tex'],
                   cwd=root, capture_output=True, timeout=120, check=False)
    return (root / 'main.log').read_text(encoding='utf-8', errors='replace')


@needs_tex
def test_real_compile_of_corrupted_column_reports_overfull(tmp_path):
    root = _tree(tmp_path, {
        'main.tex': '\\documentclass{article}\n\\usepackage{array}\n\\begin{document}\n'
                    '\\begin{table}[t]\\input{tables/tab}\\end{table}\nText.\n\\end{document}\n',
        'tables/tab.tex': '\\begin{tabular}{p{5\\textwidth}p{0.36\\textwidth}}\na & b \\\\\n'
                          '\\end{tabular}\n'})
    _compile(root)
    rows = run_tex_checks(root, 'main.tex', log=root / 'main.log')
    overfull = [row for row in rows if row['code'] == 'overfull_hbox']
    assert overfull and float(overfull[0]['message'].split('(')[1].split('pt')[0]) > 1000
    assert any(row['code'] == 'length_exceeds_line' for row in rows)


@needs_tex
def test_real_compile_of_wide_valid_layouts_is_clean(tmp_path):
    root = _tree(tmp_path, {'main.tex': (
        '\\documentclass[twocolumn]{article}\n\\usepackage{array,tabularx,graphicx}\n'
        '\\begin{document}\n'
        '\\begin{table*}[t]\\centering\\begin{tabular}{p{0.05\\textwidth}p{0.40\\textwidth}'
        'p{0.40\\textwidth}}\na & b & c \\\\\n\\end{tabular}\\caption{Wide.}\\label{w}\\end{table*}\n'
        '\\begin{table}[t]\\begin{tabularx}{\\columnwidth}{lX}a & b \\\\\\end{tabularx}'
        '\\caption{Column.}\\label{c}\\end{table}\n'
        '\\begin{table}[t]\\resizebox{\\columnwidth}{!}{\\begin{tabular}{p{1.5\\columnwidth}}'
        'scaled \\\\\\end{tabular}}\\caption{Scaled.}\\label{s}\\end{table}\n'
        'Tables~\\ref{w}, \\ref{c} and~\\ref{s}.\n\\end{document}\n')})
    _compile(root)
    rows = run_tex_checks(root, 'main.tex', log=root / 'main.log')
    assert [row for row in rows if row['severity'] in ('error', 'warning')] == [], rows
