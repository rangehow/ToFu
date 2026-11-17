"""Repair report-Markdown formatting-rule violations the renderers cannot recover.

Responsibility: a pure, deterministic, zero-LLM pass over an ordinary paper
report body (never Review/Rebuttal documents) that undoes the sloppy shapes
weaker models emit despite the prompt's "Formatting rules":

* a single outer ```markdown / ```md fence wrapping the whole report — or the
  orphaned closing fence left behind once the worker's preamble strip has
  already removed that opener;
* trailing closing remarks addressed to the reader ("Let me know if…",
  "希望这份报告对你有帮助");
* a backtick span that is unambiguously LaTeX math → ``$…$``.

Precision beats recall: fenced code and anything code-shaped stay verbatim, and
a backtick span is converted only when it carries a known LaTeX math command
(or ``_{`` / ``^{``) and nothing that indicates source code. The browser
renderer deliberately treats backticks as code (``frontend/src/markdown/
prepare-source.ts``); this is a one-shot repair of the stored report, not a
renderer heuristic.

Not handled here because another owner already does it: ``\\(…\\)`` /
``\\[…\\]`` delimiters (rendered by ``prepare-source.ts`` and
``lib/paper/report_markup.py``) and fake image targets such as
``![Figure 3](see main text)`` (downgraded to alt text by
``lib/paper/images/injection.py``).

Entry points: :func:`normalize_report_markdown`,
:func:`normalize_report_markdown_with_changes`. Dependencies: stdlib ``re``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReportMarkdownChange:
    kind: str
    count: int


_FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})(.*)$')
_OUTER_FENCE_INFO = frozenset({'markdown', 'md'})
_THEMATIC_BREAK_RE = re.compile(r'^ {0,3}([-*_])(?: *\1){2,} *$')
_STRUCTURAL_LINE_RE = re.compile(r'^\s*(?:#|\||>|[-*+]\s|\d+[.)]\s|!\[|<|\$\$)')
_LEADING_DECORATION_RE = re.compile(r'^[^\w\u4e00-\u9fff]+')
_CLOSING_REMARK_RES = (
    re.compile(r'(?:please )?let me know\b', re.IGNORECASE),
    re.compile(r'i hope (?:this|that|it|the above)\b.*\b(?:help|useful)', re.IGNORECASE),
    re.compile(r'hope (?:this|that|it) helps', re.IGNORECASE),
    re.compile(r'feel free to (?:ask|reach|let|contact)', re.IGNORECASE),
    re.compile(r'if you (?:have|need) any (?:further |other |more |additional )?'
               r'(?:questions|clarification|help)', re.IGNORECASE),
    re.compile(r"if you(?:'d| would) like (?:me )?to\b", re.IGNORECASE),
    re.compile(r'is there anything else', re.IGNORECASE),
    re.compile(r'希望[^。！!\n]*帮助'),
    re.compile(r'(?:如|若)果?(?:你|您)?还?(?:有|对)[^。\n]*(?:问题|疑问)'),
    re.compile(r'(?:如|若)果?(?:你|您)?(?:需要|想|希望)[^。\n]*(?:告诉我|随时|欢迎)'),
    re.compile(r'欢迎(?:随时|继续|进一步)?(?:提问|交流|讨论|告诉我)'),
)

_LATEX_MATH_COMMANDS = frozenset('''
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi
psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
frac dfrac tfrac binom sqrt text textbf textit textrm mathrm mathbf mathit
mathcal mathbb mathsf mathtt mathfrak boldsymbol bm operatorname
hat widehat tilde widetilde bar overline underline vec dot ddot
sum prod int oint lim max min argmax argmin arg exp log ln sin cos tanh det
cdot cdots ldots dots times div pm mp leq geq le ge neq ne approx sim simeq
equiv propto in notin subset subseteq supset supseteq cup cap setminus
infty partial nabla forall exists emptyset ell hbar prime
left right big Big bigl bigr Bigl Bigr top intercal mid vert Vert lVert rVert
langle rangle lfloor rfloor lceil rceil
to mapsto gets rightarrow leftarrow Rightarrow Leftarrow leftrightarrow
odot oplus otimes circ star ast quad qquad underbrace overbrace
'''.split())
_LATEX_COMMAND_RE = re.compile(r'\\([A-Za-z]+)')
_MATH_SCRIPT_RE = re.compile(r'[_^]\{')
# Shapes that only occur in source code, shell, regex or paths — any of them
# keeps the span as code even when it also contains a LaTeX-looking command.
_CODE_SIGNAL_RE = re.compile(
    r'''[$|"';#@`\n]|::|->|=>|==|!=|&&|\\\\|://|\[\^|\(\?|~/|\w:\\|\w\.[A-Za-z_]''')
_BACKTICK_RUN_RE = re.compile(r'`+')


def normalize_report_markdown(md: str) -> str:
    return normalize_report_markdown_with_changes(md)[0]


def normalize_report_markdown_with_changes(
        md: str) -> tuple[str, tuple[ReportMarkdownChange, ...]]:
    """Return the repaired report and one change record per repair kind."""
    changes: list[ReportMarkdownChange] = []
    lines = md.split('\n')

    if _unwrap_outer_markdown_fence(lines):
        changes.append(ReportMarkdownChange('unwrapped_outer_fence', 1))
    if _drop_orphan_trailing_fence(lines):
        changes.append(ReportMarkdownChange('removed_orphan_fence', 1))
    removed_remarks = _drop_trailing_closing_remarks(lines)
    if removed_remarks:
        changes.append(ReportMarkdownChange('removed_closing_remark', removed_remarks))
    tail_changed = bool(changes)

    in_fence, _ = _fence_map(lines)
    converted = 0
    for i, line in enumerate(lines):
        if not in_fence[i] and '`' in line:
            lines[i], n = _convert_math_backticks(line)
            converted += n
    if converted:
        changes.append(ReportMarkdownChange('backtick_math', converted))

    if not changes:
        return md, ()
    out = '\n'.join(lines)
    if tail_changed:
        out = out.rstrip('\n') + ('\n' if md.endswith('\n') else '')
    return out, tuple(changes)


def _fence_map(lines: list[str]) -> tuple[list[bool], int | None]:
    """Flag fence lines and their contents; also return an unclosed opener."""
    flags: list[bool] = []
    open_char = ''
    open_len = 0
    open_idx: int | None = None
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if open_idx is None:
            if m and not (m.group(1)[0] == '`' and '`' in m.group(2)):
                open_char, open_len, open_idx = m.group(1)[0], len(m.group(1)), i
                flags.append(True)
            else:
                flags.append(False)
            continue
        flags.append(True)
        if (m and m.group(1)[0] == open_char and len(m.group(1)) >= open_len
                and not m.group(2).strip()):
            open_idx = None
    return flags, open_idx


def _paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines + ['']:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(' '.join(current))
            current = []
    return paragraphs


def _is_closing_remark(paragraph: str) -> bool:
    if _STRUCTURAL_LINE_RE.match(paragraph):
        return False
    text = _LEADING_DECORATION_RE.sub('', paragraph)
    return any(rx.match(text) for rx in _CLOSING_REMARK_RES)


def _only_remarks(lines: list[str]) -> bool:
    return all(_is_closing_remark(p) for p in _paragraphs(lines))


def _unwrap_outer_markdown_fence(lines: list[str]) -> bool:
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return False
    opener = _FENCE_RE.match(lines[first])
    if not opener or opener.group(2).strip().lower() not in _OUTER_FENCE_INFO:
        return False
    char, length = opener.group(1)[0], len(opener.group(1))
    for j in range(len(lines) - 1, first, -1):
        closer = _FENCE_RE.match(lines[j])
        if (closer and closer.group(1)[0] == char and len(closer.group(1)) >= length
                and not closer.group(2).strip()):
            if not _only_remarks(lines[j + 1:]):
                return False
            del lines[j]
            del lines[first]
            return True
    return False


def _drop_orphan_trailing_fence(lines: list[str]) -> bool:
    """Remove a bare fence that opens an empty block at the end of the report.

    This is the closing half of an outer ```markdown wrapper whose opener sat
    before the first heading and was removed with the preamble.
    """
    _, open_idx = _fence_map(lines)
    if open_idx is None or _FENCE_RE.match(lines[open_idx]).group(2).strip():
        return False
    if not _only_remarks(lines[open_idx + 1:]):
        return False
    if not any(line.strip() for line in lines[:open_idx]):
        return False
    del lines[open_idx]
    return True


def _drop_trailing_closing_remarks(lines: list[str]) -> int:
    removed = 0
    while True:
        end = len(lines)
        while end and not lines[end - 1].strip():
            end -= 1
        start = end
        while start and lines[start - 1].strip():
            start -= 1
        if not any(line.strip() for line in lines[:start]):
            break
        in_fence, _ = _fence_map(lines)
        if any(in_fence[start:end]) or not _is_closing_remark(
                ' '.join(line.strip() for line in lines[start:end])):
            break
        del lines[start:]
        removed += 1
    if removed:
        end = len(lines)
        while end and not lines[end - 1].strip():
            end -= 1
        if (end >= 2 and _THEMATIC_BREAK_RE.match(lines[end - 1])
                and not lines[end - 2].strip()):
            del lines[end - 1:]
    return removed


def _is_unambiguous_math(content: str) -> bool:
    text = content.strip()
    if not text or _CODE_SIGNAL_RE.search(text):
        return False
    depth = 0
    for ch in text:
        depth += {'{': 1, '}': -1}.get(ch, 0)
        if depth < 0:
            return False
    if depth:
        return False
    commands = _LATEX_COMMAND_RE.findall(text)
    if any(cmd not in _LATEX_MATH_COMMANDS for cmd in commands):
        return False
    return bool(commands) or bool(_MATH_SCRIPT_RE.search(text))


def _convert_math_backticks(line: str) -> tuple[str, int]:
    out: list[str] = []
    pos = 0
    converted = 0
    while True:
        opener = _BACKTICK_RUN_RE.search(line, pos)
        if not opener:
            break
        closer = next((m for m in _BACKTICK_RUN_RE.finditer(line, opener.end())
                       if len(m.group()) == len(opener.group())), None)
        if closer is None:
            break
        content = line[opener.end():closer.start()]
        out.append(line[pos:opener.start()])
        if len(opener.group()) == 1 and _is_unambiguous_math(content):
            out.append(f'${content.strip()}$')
            converted += 1
        else:
            out.append(line[opener.start():closer.end()])
        pos = closer.end()
    out.append(line[pos:])
    return ''.join(out), converted
