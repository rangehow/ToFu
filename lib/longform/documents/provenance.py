"""Number and citation provenance: the document pipeline's no-invented-numbers gate.

Responsibility: for every number (percentages, decimals, counts, CI bounds,
p-values, table cells, figure labels) and every citation key in an output
text, decide whether it traces to a source text (the draft, the dossier and
the result files its facts cite, the bibliography). A value that rounds a
source value, or restates a fraction as a percentage (or back), is sourced
and recorded as such; anything else is unsourced and named with its location.

Layout markup is not content: comments, command options (``[width=…]``,
``[t]``), identifier arguments (labels, refs, cites, file names, package
names, environment names, column specs), the size arguments of box commands
and dimensions (``2pt``, ``0.9\\linewidth``) are blanked before numbers are
read. A digit inside a name (BF16, GPT-2, H100, ``\\nm{gap_7b}``) is part of
that name, not a value.

Entry points: ``SourcePool``, ``check``, ``check_texts``.
Dependencies: standard library only.
"""
from __future__ import annotations

import bisect
import re
from decimal import Decimal, InvalidOperation

__all__ = ['SourcePool', 'check', 'check_texts']

#: A leading-dot decimal (.05) only where a value can start; "row 6).1" is a
#: sentence end and a footnote mark.
_NUMBER = (r'\d{1,3}(?:(?:,|\{,\})\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?'
           r'|(?<![^\s(\[$=<>~{,;:])\.\d+')
#: A value as a reader reads it: not glued to a name (BF16, x_2, GPT-2) and
#: not the middle of a version string (1.2.3).
_VALUE_RE = re.compile(r'(?<![A-Za-z_\d])(?<!\d\.)(?<![A-Za-z]-)(' + _NUMBER + ')')
#: Every number a source states, names included (a source naming "BF16"
#: sources the 16 an output may write as "16-bit").
_SOURCE_RE = re.compile(r'(?<!\d)(?<!\d\.)(' + _NUMBER + ')')
_CITE_KEYS_RE = re.compile(r'\\(?:no)?cite[A-Za-z]*\*?(?:\s*\[[^\]]*\])*\s*\{([^}]*)\}')
_BIBITEM_RE = re.compile(r'\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}')
_COMMENT_RE = re.compile(r'(?<!\\)%.*$', re.MULTILINE)
_PERCENT_RE = re.compile(r'\s*(?:\\?%|percent\b)')
_COMMAND_RE = re.compile(r'\\([A-Za-z@]+)\*?')
_LINEBREAK_OPTION_RE = re.compile(r'\\\\\*?\s*\[[^\]]*\]')
_UNIT = r'(?:pt|mm|cm|em|ex|bp|sp|pc|dd|mu|in)(?![A-Za-z])'
_DIMENSION_RE = re.compile(
    r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:' + _UNIT + r'|\s*\\(?:line|text|column|paper)(?:width|height)'
    r'|\s*\\(?:hsize|vsize|baselineskip|tabcolsep|arraycolsep|fill)\b)')
_ARRAYSTRETCH_RE = re.compile(r'\\(?:renewcommand\s*\{?\s*\\arraystretch\s*\}?|def\s*\\arraystretch)'
                              r'\s*\{[^{}]*\}')
#: Commands whose every argument is an identifier, a file, a package or a size.
_IDENTIFIER_COMMANDS = frozenset((
    'nocite', 'ref', 'eqref', 'autoref', 'cref', 'Cref', 'vref', 'pageref', 'nameref',
    'cpageref', 'Cpageref', 'labelcref', 'label', 'includegraphics', 'url', 'input',
    'include', 'usepackage', 'RequirePackage', 'documentclass', 'bibliography',
    'bibliographystyle', 'bibitem', 'setlength', 'addtolength', 'setcounter',
    'addtocounter', 'vspace', 'hspace', 'vskip', 'hskip', 'cline', 'cmidrule',
    'definecolor', 'color', 'begin', 'end', 'newcolumntype', 'fontsize', 'linespread',
    'hypersetup', 'captionsetup', 'rule', 'graphicspath', 'pdfoutput'))
#: Commands whose first N arguments are sizes or identifiers; the rest is content.
_LEADING_ARGUMENTS = {'multicolumn': 2, 'multirow': 2, 'resizebox': 2, 'scalebox': 1,
                      'raisebox': 1, 'rotatebox': 1, 'textcolor': 1, 'colorbox': 1,
                      'cellcolor': 1, 'rowcolor': 1, 'href': 1, 'parbox': 1,
                      'adjustbox': 1, 'makebox': 0, 'framebox': 0}


def _group_end(text: str, start: int, opening: str, closing: str) -> int:
    """Index after the group opening at ``start`` (balanced), or -1."""
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == '\\':
            i += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in ' \t':
        i += 1
    return i


def content_view(text: str, *, comments: bool = True) -> str:
    """``text`` with LaTeX layout and identifiers blanked; offsets preserved.

    ``comments=False`` reads ``%`` as a percent sign (plain text that may
    still carry LaTeX commands a writer added)."""
    if comments:
        text = _COMMENT_RE.sub(lambda m: ' ' * len(m.group(0)), text)
    spans = [match.span() for pattern in (_LINEBREAK_OPTION_RE, _ARRAYSTRETCH_RE, _DIMENSION_RE)
             for match in pattern.finditer(text)]
    for match in _COMMAND_RE.finditer(text):
        name = match.group(1)
        if name.startswith('cite') or name in ('nocite',):
            kind, keep_after = 'all', None
        elif name in _IDENTIFIER_COMMANDS:
            kind, keep_after = 'all', None
        elif name in _LEADING_ARGUMENTS:
            kind, keep_after = 'leading', _LEADING_ARGUMENTS[name]
        else:
            kind, keep_after = 'options', 0
        i, braces = match.end(), 0
        while True:
            i = _skip_space(text, i)
            if i < len(text) and text[i] == '[':
                end = _group_end(text, i, '[', ']')
                if end < 0:
                    break
                spans.append((i, end))
                i = end
            elif i < len(text) and text[i] == '{' and (
                    kind == 'all' or (kind == 'leading' and braces < keep_after)):
                end = _group_end(text, i, '{', '}')
                if end < 0:
                    break
                spans.append((i, end))
                i, braces = end, braces + 1
            else:
                break
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != '\n':
                chars[i] = ' '
    return ''.join(chars)


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace('{,}', '').replace(',', ''))
    except InvalidOperation:
        return None


def _places(token: str) -> int:
    return len(token.split('.', 1)[1]) if '.' in token else 0


def _citation_keys(text: str) -> list[tuple[str, int]]:
    """``(key, offset)`` of every ``\\cite*`` and ``\\bibitem`` key."""
    found = []
    for pattern in (_CITE_KEYS_RE, _BIBITEM_RE):
        for match in pattern.finditer(text):
            found += [(key.strip(), match.start()) for key in match.group(1).split(',')
                      if key.strip()]
    return found


class SourcePool:
    """Every number and citation key the sources state, indexed for lookup."""

    def __init__(self, sources: dict[str, str], bib_keys=()):
        values, percents = {}, set()
        for name, text in sources.items():
            for match in _SOURCE_RE.finditer(text or ''):
                value = _decimal(match.group(1))
                if value is None:
                    continue
                values.setdefault(value, (match.group(1), name))
                if _PERCENT_RE.match(text, match.end()):
                    percents.add(value)
        self._origin = values
        self._sorted = sorted(values)
        self._percents = sorted(percents)
        self.citations = set(bib_keys)
        for text in sources.values():
            self.citations |= {key for key, _ in _citation_keys(text or '')}

    def trace(self, token: str, *, percent: bool = False) -> dict | None:
        """How ``token`` traces to a source: exact, rounded or rescaled; else None.

        A source fraction sources an output percentage (``percent``: the
        output writes a % sign) and a source percentage an output fraction;
        no other rescaling."""
        value = _decimal(token)
        if value is None:
            return None
        if value in self._origin:
            return {'how': 'exact', 'source_value': self._origin[value][0],
                    'source': self._origin[value][1]}
        half = Decimal(5).scaleb(-(_places(token) + 1))
        searches = [('rounded', Decimal(1), self._sorted),
                    ('rescaled fraction', Decimal('0.01'), self._percents)]
        if percent:
            searches.insert(1, ('rescaled percent', Decimal(100), self._sorted))
        for how, scale, pool in searches:
            low, high = (value - half) / scale, (value + half) / scale
            candidates = pool[bisect.bisect_left(pool, low):bisect.bisect_right(pool, high)]
            if candidates:
                source = min(candidates, key=lambda s: abs(s * scale - value))
                return {'how': how, 'source_value': self._origin[source][0],
                        'source': self._origin[source][1]}
        return None


def _location(text: str, offset: int) -> tuple[int, str]:
    """Line number and the sentence (within that line) holding ``offset``."""
    line = text.count('\n', 0, offset) + 1
    start = text.rfind('\n', 0, offset) + 1
    end = text.find('\n', offset)
    end = end if end >= 0 else len(text)
    before = [m.end() for m in re.finditer(r'[.!?]\s+(?=[A-Z\\])', text[start:offset])]
    after = re.search(r'[.!?](?=\s|$)', text[offset:end])
    return line, text[start + (before[-1] if before else 0):
                      offset + after.end() if after else end].strip()


def check(outputs: dict[str, str], pool: SourcePool, *, latex: bool = True) -> dict:
    """Trace every number and citation in ``outputs`` (``{name: text}``).

    Returns ``{'unsourced': [...], 'rounded': [...], 'checked': {...}}``; each
    row names ``kind`` (number|citation), ``value``, ``location``
    (``name:line``) and the line as ``context``. ``rounded`` lists the
    numbers that trace by rounding or percent/fraction rescaling, with the
    source value they come from. ``latex=False`` (plain text) reads ``%`` as
    a percent sign, not a comment; LaTeX commands are read as markup either
    way.
    """
    unsourced, rounded = [], []
    numbers = citations = 0
    for name, text in outputs.items():
        text = text or ''
        view = content_view(text, comments=latex)
        for match in _VALUE_RE.finditer(view):
            numbers += 1
            token = match.group(1)
            line, context = _location(text, match.start(1))
            row = {'kind': 'number', 'value': token, 'location': f'{name}:{line}',
                   'context': context}
            trace = pool.trace(token, percent=bool(_PERCENT_RE.match(view, match.end(1))))
            if trace is None:
                unsourced.append(row)
            elif trace['how'] != 'exact':
                rounded.append({**row, **trace})
        for key, offset in _citation_keys(text):
            citations += 1
            if key not in pool.citations:
                line, context = _location(text, offset)
                unsourced.append({'kind': 'citation', 'value': key,
                                  'location': f'{name}:{line}', 'context': context})
    return {'unsourced': unsourced, 'rounded': rounded,
            'checked': {'numbers': numbers, 'citations': citations}}


def check_texts(outputs: dict[str, str], sources: dict[str, str], *, bib_keys=(),
                latex: bool = True) -> dict:
    """``check`` against a pool built from ``sources`` and ``bib_keys``."""
    return check(outputs, SourcePool(sources, bib_keys), latex=latex)


def describe(row: dict) -> str:
    """One unsourced item, as a gate error names it."""
    return (f'deliver.provenance: unsourced {row["kind"]} {row["value"]!r} at '
            f'{row["location"]}: {row["context"]}')
