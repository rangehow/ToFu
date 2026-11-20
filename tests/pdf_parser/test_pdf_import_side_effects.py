"""Import-time MuPDF state must not leak out of the PDF domain.

``pymupdf4llm``'s helper modules call
``pymupdf.TOOLS.unset_quad_corrections(True)`` while their bodies execute.
That is a process-global MuPDF switch: after it flips, every later
``page.get_text`` / ``find_tables`` in the process returns glyph geometry with
ascender/descender corrections disabled. Importing ``lib.pdf_parser._common``
used to import pymupdf4llm eagerly, so a consumer that touched the PDF domain
first corrupted table cell text in unrelated PDF paths.

These tests pin the mechanism: domain import is inert for both starting
states, the lazy import restores the caller's state, and the glyph-based table
reader still returns exact cell text in the import-order scenario.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]


def _run(code: str, **env_overrides) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop('TOFU_ENABLE_PYMUPDF_LAYOUT', None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        check=False,
    )


@pytest.mark.parametrize('initial', [False, True])
def test_importing_the_pdf_domain_leaves_quad_corrections_unchanged(initial):
    proc = _run(
        "import sys, pymupdf\n"
        f"pymupdf.TOOLS.unset_quad_corrections({initial!r})\n"
        "before = pymupdf.TOOLS.unset_quad_corrections()\n"
        "import lib.pdf_parser._common\n"
        "import lib.pdf_parser.text\n"
        "import lib.pdf_parser.core\n"
        "import lib.pdf_parser.structure\n"
        "after = pymupdf.TOOLS.unset_quad_corrections()\n"
        "print(before == after, 'pymupdf4llm' in sys.modules, after)\n"
    )
    assert proc.returncode == 0, proc.stderr
    # The flag is preserved AND the optional markdown package stays unloaded.
    assert proc.stdout.strip() == f'True False {initial}'


_TABLE_CELL_SCENARIO = r'''
import sys
import lib.pdf_parser._common            # domain import first: must stay inert
import lib.pdf_parser.structure as structure
assert 'pymupdf4llm' not in sys.modules
import pymupdf
flag_before = pymupdf.TOOLS.unset_quad_corrections()
doc = pymupdf.open()
page = doc.new_page(width=612, height=792)
xs = [72, 200, 360, 500]
ys = [120, 150, 180, 210]
for y in ys:
    page.draw_line((xs[0], y), (xs[-1], y), color=(0, 0, 0), width=1)
for x in xs:
    page.draw_line((x, ys[0]), (x, ys[-1]), color=(0, 0, 0), width=1)
rows = [
    ['Model', 'Accuracy', 'Latency'],
    ['Baseline', '91.2', '45 ms'],
    ['Ours', '94.8', '39 ms'],
]
for r, row in enumerate(rows):
    for c, text in enumerate(row):
        page.insert_text((xs[c] + 4, ys[r] + 18), text, fontsize=10,
                         fontname='helv')
payload = doc.tobytes()
doc.close()
result = structure.extract_pdf_structure(payload)
tables = [b for b in result['pages'][0]['blocks'] if b['kind'] == 'table']
print('ROWS|' + repr(tables[0]['rows']))
print('FLAG|%s' % (flag_before == pymupdf.TOOLS.unset_quad_corrections()))
'''


def test_table_cell_text_is_exact_after_the_pdf_domain_is_imported_first():
    proc = _run(_TABLE_CELL_SCENARIO)
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines()
             if line.startswith(('ROWS|', 'FLAG|'))]
    assert len(lines) == 2, proc.stdout
    assert lines[0] == 'ROWS|' + repr([
        ['Model', 'Accuracy', 'Latency'],
        ['Baseline', '91.2', '45 ms'],
        ['Ours', '94.8', '39 ms'],
    ])
    assert lines[1] == 'FLAG|True'


@pytest.mark.parametrize('initial', [False, True])
def test_lazy_pymupdf4llm_path_works_and_restores_the_flag(initial):
    proc = _run(
        "import sys, pymupdf\n"
        f"pymupdf.TOOLS.unset_quad_corrections({initial!r})\n"
        "import lib.pdf_parser._common as common\n"
        "assert 'pymupdf4llm' not in sys.modules\n"
        "before = pymupdf.TOOLS.unset_quad_corrections()\n"
        "import lib.pdf_parser.text as text_module\n"
        "doc = pymupdf.open()\n"
        "page = doc.new_page(width=612, height=792)\n"
        "page.insert_text((72, 72), 'Hello lazy extraction.', fontsize=12,\n"
        "                 fontname='helv')\n"
        "xs = [72, 220, 400]\n"
        "ys = [140, 170, 200]\n"
        "for y in ys:\n"
        "    page.draw_line((xs[0], y), (xs[-1], y), color=(0, 0, 0), width=1)\n"
        "for x in xs:\n"
        "    page.draw_line((x, ys[0]), (x, ys[-1]), color=(0, 0, 0), width=1)\n"
        "for r, row in enumerate([['Name', 'Qty'], ['Widget', '3']]):\n"
        "    for c, value in enumerate(row):\n"
        "        page.insert_text((xs[c] + 4, ys[r] + 18), value, fontsize=10,\n"
        "                         fontname='helv')\n"
        "payload = doc.tobytes()\n"
        "doc.close()\n"
        "extracted, extractor = text_module.extract_pdf_text_with_meta(\n"
        "    payload, mode='rich')\n"
        "after = pymupdf.TOOLS.unset_quad_corrections()\n"
        "print('STATE|' + ' '.join([extractor, str(before == after),\n"
        "                          str('pymupdf4llm' in sys.modules)]))\n"
        "print('TEXT|' + ' '.join([str('Hello lazy extraction.' in extracted),\n"
        "                          str('Widget' in extracted),\n"
        "                          str('Qty' in extracted)]))\n"
    )
    assert proc.returncode == 0, proc.stderr
    lines = {line.split('|', 1)[0]: line.split('|', 1)[1]
             for line in proc.stdout.splitlines()
             if line.startswith(('STATE|', 'TEXT|'))}
    extractor, restored, loaded = lines['STATE'].split()
    assert extractor in ('pymupdf4llm', 'pymupdf4llm-partial')
    assert restored == 'True'
    assert loaded == 'True'
    assert lines['TEXT'].split() == ['True', 'True', 'True']