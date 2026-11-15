"""Lay out generated Rust constant items exactly as ``cargo fmt`` does.

Entry: rustfmt_const_items(text). Generators project contracts into Rust source
that ``cargo fmt --check`` (scripts/check_static_gates.py) and each generator's
exact-text ``--check`` both gate, so a generator must emit rustfmt's layout
itself. This pass rewrites single-line items and the two-line ``NAME: T =`` /
``    value;`` form; every other line is left untouched. Standard library only.

Modelled rustfmt defaults (no rustfmt.toml in packages/tofu-db): max_width 100,
array_width 60 (use_small_heuristics = "Default"), short array element width
threshold 10.
"""
from __future__ import annotations

import re

MAX_WIDTH = 100
ARRAY_WIDTH = 60
SHORT_ARRAY_ELEMENT_WIDTH = 10
INDENT = '    '

_HEAD = re.compile(r'^(?P<head>(?:pub(?:\(crate\))? )?const [A-Z0-9_]+: [^=]+ =)(?: (?P<value>.+;))?$')
_ITEM = r'"(?:[^"\\]|\\.)*"|-?[A-Za-z0-9_.]+'
_ARRAY = re.compile(rf'^(?P<ref>&?)\[(?P<items>(?:{_ITEM})(?:, (?:{_ITEM}))*)?\]$')
_SCALAR = re.compile(rf'^(?:{_ITEM})$')


def _layout(head: str, value: str) -> list[str] | None:
    one_line = f'{head} {value};'
    array = _ARRAY.match(value)
    if array is None:
        if not _SCALAR.match(value):
            return None
        items = [value]
    else:
        items = re.findall(_ITEM, array['items'] or '')
    horizontal = array is None or len(items) <= 1 or len(value) - len(array['ref']) <= ARRAY_WIDTH
    if horizontal:
        if len(one_line) <= MAX_WIDTH:
            return [one_line]
        if len(INDENT) + len(value) + 1 <= MAX_WIDTH:
            return [head, f'{INDENT}{value};']
        return [one_line]
    if all(len(item) <= SHORT_ARRAY_ELEMENT_WIDTH for item in items):
        raise ValueError(f'{head} needs rustfmt\'s mixed array layout, which this pass does not model')
    return [f'{head} {array["ref"]}[', *(f'{INDENT}{item},' for item in items), '];']


def rustfmt_const_items(text: str) -> str:
    lines = text.split('\n')
    output: list[str] = []
    index = 0
    while index < len(lines):
        match = _HEAD.match(lines[index])
        consumed, value = 1, None
        if match:
            value = match['value']
            if (value is None and index + 1 < len(lines)
                    and lines[index + 1].startswith(INDENT)
                    and lines[index + 1].endswith(';')):
                value, consumed = lines[index + 1][len(INDENT):], 2
        laid_out = _layout(match['head'], value[:-1]) if match and value else None
        if laid_out is None:
            output.append(lines[index])
            index += 1
        else:
            output.extend(laid_out)
            index += consumed
    return '\n'.join(output)
