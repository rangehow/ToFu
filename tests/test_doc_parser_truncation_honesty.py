"""Truncation honesty for ``lib.doc_parser`` spreadsheet extraction.

WHY THIS EXISTS
---------------
``_extract_xlsx`` has THREE independent cuts that can drop content, and none
of them used to tell the caller how much was dropped:

  * ``_XLSX_MAX_ROWS``      — kept 1,000 of 5,000 rows (80% loss) while the
    warning only said ``truncated at 1000 rows``: a numerator with no scale.
  * ``_XLSX_MAX_EMPTY_RUN`` — broke out of the scan entirely and emitted
    **no warning at all**, so a sheet shaped "summary / 60 blank rows /
    detail" lost the whole detail block leaving no trace in the output.
  * ``_XLSX_MAX_COLS``      — same missing-denominator shape as rows.

The measured char budget is NOT the cause of any of this: production passes
``MAX_TEXT_CHARS = 50 * 1024 * 1024`` (and the upload route passes 0 =
unlimited), so a 175 KB / 5,000-row book never comes near it. These are
ROW/COLUMN-level caps, independent of the char budget.

DISCIPLINE (charter)
--------------------
* These tests assert the RESULT (does the warning carry a denominator? does
  the caller learn content is missing?) — never a constant's value. Retuning
  ``_XLSX_MAX_ROWS`` must NOT make them falsely red; deleting a warning must.
* The corpus is a REAL OOXML container built by openpyxl and saved through a
  real ``.xlsx`` write path — not a hand-written template string. Cell values
  are synthetic/anonymised on purpose: the property under test is row-count
  and truncation behaviour, which does not depend on cell semantics, and no
  production document may enter the repo.
* ``test_scan_surface_report`` prints what the corpus actually exercises
  BEFORE any assertion, so a fixture that silently fails to cross a threshold
  cannot leave the suite green while testing nothing.
"""

import io

import pytest

openpyxl = pytest.importorskip('openpyxl')

pytestmark = pytest.mark.unit

from lib.doc_parser._office import (  # noqa: E402
    _XLSX_MAX_COLS,
    _XLSX_MAX_EMPTY_RUN,
    _XLSX_MAX_ROWS,
    _extract_xlsx,
)

# The real production budget for both callers (lib/file_reader/_router.py).
# Passed so the tests exercise the SAME char headroom production has; the row
# and column caps must fire regardless of it.
PROD_CHAR_LIMIT = 50 * 1024 * 1024


def _real_xlsx(fill) -> bytes:
    """Build a genuine .xlsx (real OOXML zip container) and return its bytes."""
    wb = openpyxl.Workbook()
    fill(wb.active)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_over_row_cap(ws):
    """5,000 distinct data rows — comfortably past the 1,000-row cap."""
    ws.append(['id', 'name', 'dept', 'amount'])
    for i in range(1, 5000):
        ws.append([i, f'employee_{i}', f'dept_{i % 37}', 1000 + i])


def _sheet_with_blank_gap(ws):
    """Summary block, a blank gap longer than the cap, then a detail block."""
    ws.append(['section', 'value'])
    for i in range(100):
        ws.append([f'summary_{i}', i])
    for _ in range(_XLSX_MAX_EMPTY_RUN + 10):
        ws.append([None, None])
    for i in range(200):
        ws.append([f'detail_{i}', i * 7])


def _sheet_over_col_cap(ws):
    ws.append([f'c{i}' for i in range(_XLSX_MAX_COLS + 50)])
    for r in range(5):
        ws.append([f'v{r}_{c}' for c in range(_XLSX_MAX_COLS + 50)])


def _sheet_small(ws):
    ws.append(['a', 'b'])
    ws.append([1, 2])


def _digits(text: str) -> list[int]:
    """All integers in a string, with thousands separators folded away."""
    import re
    return [int(m.replace(',', '')) for m in re.findall(r'\d[\d,]*', text)]


def test_scan_surface_report(capsys):
    """Print what the corpus actually exercises before trusting any assertion.

    Charter: "这个守卫现在到底扫到了哪些东西?把清单打出来" — a fixture that
    fails to cross a threshold would otherwise leave every downstream test
    vacuously green.
    """
    rows = _extract_xlsx(_real_xlsx(_sheet_over_row_cap), PROD_CHAR_LIMIT)
    gap = _extract_xlsx(_real_xlsx(_sheet_with_blank_gap), PROD_CHAR_LIMIT)
    cols = _extract_xlsx(_real_xlsx(_sheet_over_col_cap), PROD_CHAR_LIMIT)
    clean = _extract_xlsx(_real_xlsx(_sheet_small), PROD_CHAR_LIMIT)

    kept = rows['text'].count('\n| ')
    print('\n--- scan surface ---')
    print(f'row-cap corpus : wrote 5000 rows, kept ~{kept}, '
          f'warnings={len(rows["warnings"])}')
    print(f'blank-gap corpus: wrote 100+{_XLSX_MAX_EMPTY_RUN + 10}blank+200, '
          f'detail_0 present={"detail_0" in gap["text"]}, '
          f'warnings={len(gap["warnings"])}')
    print(f'col-cap corpus : wrote {_XLSX_MAX_COLS + 50} cols, '
          f'warnings={len(cols["warnings"])}')
    print(f'clean corpus   : warnings={len(clean["warnings"])}')
    print(f'char limit passed: {PROD_CHAR_LIMIT:,} '
          f'(row-cap textLength={rows["textLength"]:,} — nowhere near it)')

    # The corpus MUST genuinely trip each cut, else the rest tests nothing.
    assert kept < 5000, 'row-cap corpus did not trip the row cap'
    assert 'detail_0' not in gap['text'], 'blank-gap corpus did not trip the break'
    assert cols['warnings'], 'col-cap corpus did not trip the column cap'
    assert not clean['warnings'], 'clean corpus should trip nothing'
    # And the char budget must NOT be the thing that fired.
    assert rows['textLength'] < PROD_CHAR_LIMIT


def test_row_cap_warning_reports_the_denominator():
    res = _extract_xlsx(_real_xlsx(_sheet_over_row_cap), PROD_CHAR_LIMIT)
    assert res['warnings'], 'row truncation must warn'
    warn = ' '.join(res['warnings'])
    kept = res['text'].count('\n| ')

    # The caller must be able to see BOTH sides of the fraction, so it can
    # tell "I got 20%" from "I got 99%". Asserting the numbers appear — not
    # that any constant equals a literal — keeps this true after retuning.
    nums = _digits(warn)
    assert kept - 1 in nums or kept in nums, (
        f'warning must state how many rows were KEPT; got {warn!r}')
    assert 5000 in nums, (
        f'warning must state the sheet total (the denominator); got {warn!r}')
    assert 'NOT read' in warn or 'not read' in warn


def test_blank_gap_break_is_not_silent():
    """The empty-run break dropped 200 rows with zero warnings — never again."""
    res = _extract_xlsx(_real_xlsx(_sheet_with_blank_gap), PROD_CHAR_LIMIT)

    assert 'detail_0' not in res['text'], 'fixture precondition: detail is dropped'
    assert res['warnings'], (
        'content after a long blank run was dropped with NO warning — the '
        'caller cannot know anything is missing')
    warn = ' '.join(res['warnings'])
    assert 'blank' in warn.lower(), f'warning must name the cause; got {warn!r}'
    nums = _digits(warn)
    assert any(n > 0 for n in nums), (
        f'warning must quantify what was skipped; got {warn!r}')


def test_column_cap_warning_reports_the_denominator():
    res = _extract_xlsx(_real_xlsx(_sheet_over_col_cap), PROD_CHAR_LIMIT)
    warn = ' '.join(res['warnings'])
    nums = _digits(warn)
    assert _XLSX_MAX_COLS in nums, f'must state columns kept; got {warn!r}'
    assert _XLSX_MAX_COLS + 50 in nums, (
        f'must state the real column count (denominator); got {warn!r}')


def test_an_untruncated_sheet_claims_nothing():
    """The complement: no cut fired, so no warning may be invented.

    Without this, an implementation that always warns would satisfy every
    assertion above while making the signal worthless.
    """
    res = _extract_xlsx(_real_xlsx(_sheet_small), PROD_CHAR_LIMIT)
    assert res['warnings'] == []
    assert res['textLength'] > 0
