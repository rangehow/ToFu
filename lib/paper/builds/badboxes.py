"""Overfull-box records from a TeX compile log: the shared bad-box parser.

Responsibility: read every ``Overfull \\hbox``/``\\vbox`` record of a TeX log
with its amount, context and line range, and attribute it to the source file
open when TeX wrote it. TeX logs a box only when it exceeds the document's own
tolerance (``\\hfuzz``/``\\vfuzz``, 0.1pt in the LaTeX and plain TeX kernels),
so every record is an overflow by the document's own TeX contract; this module
applies no further threshold. ``Underfull`` records are not overflows and are
ignored.

File attribution is best effort: it follows the log's ``(file`` ... ``)``
open-file stack, and TeX reports a paragraph's line range when the paragraph
ends, so a range can start in an ``\\input`` file that has already closed
(``lines 1--7`` reported under ``main.tex``). Box-content lines after a record
are skipped because document text in them can hold unbalanced parentheses.

Entry point: ``overfull_boxes(log_text)``. Dependencies: standard library only
(the portable paper compiler imports it).
"""
from __future__ import annotations

import re

__all__ = ['overfull_boxes']

_RECORD = re.compile(
    r'^Overfull \\([hv])box \(([\d.]+)pt too (?:wide|high)\) '
    r'(?:in (paragraph|alignment) at lines (\d+)--(\d+)'
    r'|(detected) at line (\d+)'
    r'|(has occurred while \\output is active))')
_FILE_TOKEN = re.compile(r'\(|\)')
_OPENED = re.compile(r'"?([^\s()"]+)')


def _track_files(line: str, stack: list[str]) -> None:
    for match in _FILE_TOKEN.finditer(line):
        if match.group() == ')':
            if stack:
                stack.pop()
            continue
        opened = _OPENED.match(line, match.end())
        stack.append(opened.group(1) if opened else '')


def _current_tex(stack: list[str]) -> str:
    return next((name for name in reversed(stack)
                 if name.endswith(('.tex', '.ltx'))), '')


def overfull_boxes(log_text: str) -> list[dict]:
    """``[{box, amount_pt, where, lines, file, record}]`` in log order.

    ``where`` is ``paragraph``, ``alignment`` (a tabular/array row),
    ``detected`` (an explicit box) or ``output`` (the page builder);
    ``lines`` is TeX's ``(first, last)`` input-line range, ``None`` for
    ``output``; ``file`` is the innermost open ``.tex`` file or ``''``.
    """
    boxes: list[dict] = []
    stack: list[str] = []
    in_box_text = False
    for line in log_text.splitlines():
        if in_box_text:
            in_box_text = bool(line.strip())
            continue
        match = _RECORD.match(line)
        if not match:
            _track_files(line, stack)
            continue
        if match.group(3):
            where, lines = match.group(3), (int(match.group(4)), int(match.group(5)))
        elif match.group(6):
            where, lines = 'detected', (int(match.group(7)),) * 2
        else:
            where, lines = 'output', None
        boxes.append({'box': f'{match.group(1)}box', 'amount_pt': float(match.group(2)),
                      'where': where, 'lines': lines, 'file': _current_tex(stack),
                      'record': match.group(0)})
        in_box_text = True
    return boxes
