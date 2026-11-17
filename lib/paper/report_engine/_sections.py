"""Required top-level sections of an explainer report and their repair splice.

Responsibility: derive the required section list from the report prompt
templates (``lib.paper.prompts._REPORT_PROMPT_EN`` / ``_REPORT_PROMPT_ZH``,
the single source of truth: the ``## `` headings between the two ``---``
separators), find which a finished report lacks, build the targeted repair
request, and splice the repaired sections back at their canonical positions.
Entry points: :func:`find_missing_sections`,
:func:`build_section_repair_request`, :func:`insert_repaired_sections`.
Pure text logic; the report worker owns the model round and publication.
Review/rebuttal documents have their own structure and never use this module.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

_HEADING_RE = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$')
_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
# Text after these marks is a qualifier, not the section's name
# ("Method — How It Works (write this section …)" is named "Method").
_STEM_CUT_RE = re.compile(r'\s*[(（:：—–]|\s+-\s+')
_EMOJI_MODIFIERS = {'\ufe0f', '\ufe0e', '\u200d'}


@dataclass(frozen=True)
class RequiredSection:
    """One required section; ``heading`` is the prompt heading minus its
    parenthetical instruction, in the report's language."""

    index: int
    heading: str
    emoji: str
    stems: tuple[str, ...]


def section_headings_from_prompt(template: str) -> list[str]:
    """Return the ``## `` heading texts of the report body specification."""
    lines = template.splitlines()
    separators = [i for i, line in enumerate(lines) if line.strip() == '---']
    if len(separators) < 2:
        raise ValueError('report prompt lacks the --- delimited section specification')
    headings = []
    in_fence = False
    for line in lines[separators[0] + 1:separators[1]]:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith('## '):
            headings.append(line[3:].strip())
    return headings


def _display_heading(prompt_heading: str) -> str:
    return re.sub(r'\s*[(（][^()（）]*[)）]\s*$', '', prompt_heading).strip()


def _split_heading(text: str) -> tuple[str, str]:
    """Return ``(emoji_key, normalized_stem)`` of one heading's text."""
    text = re.sub(r'[*_`]', '', text).strip()
    text = re.sub(r'^\d+[.)、]\s*', '', text)
    lead = []
    for position, char in enumerate(text):
        if char.isalnum() or char.isspace():
            text = text[position:]
            break
        lead.append(char)
    else:
        text = ''
    emoji = ''.join(
        c for c in lead
        if c not in _EMOJI_MODIFIERS and not unicodedata.category(c).startswith('P'))
    name = _STEM_CUT_RE.split(text.strip(), maxsplit=1)[0]
    stem = ' '.join(re.sub(r'[\W_]+', ' ', name.casefold()).split())
    return emoji, stem


@lru_cache(maxsize=None)
def _sections_by_language() -> dict[str, tuple[RequiredSection, ...]]:
    from lib.paper.prompts import _REPORT_PROMPT_EN, _REPORT_PROMPT_ZH

    templates = {'en': _REPORT_PROMPT_EN, 'zh': _REPORT_PROMPT_ZH}
    headings = {lang: section_headings_from_prompt(t) for lang, t in templates.items()}
    keys = {lang: [_split_heading(h) for h in hs] for lang, hs in headings.items()}
    if [e for e, _ in keys['en']] != [e for e, _ in keys['zh']]:
        raise ValueError('EN and ZH report prompts disagree on the section list')
    out = {}
    for lang, lang_headings in headings.items():
        out[lang] = tuple(
            RequiredSection(
                index=i, heading=_display_heading(heading), emoji=keys['en'][i][0],
                # A report may follow either language's names; accept both.
                stems=tuple(sorted({keys['en'][i][1], keys['zh'][i][1]} - {''})))
            for i, heading in enumerate(lang_headings))
    return out


def required_sections(lang: str) -> tuple[RequiredSection, ...]:
    return _sections_by_language()['zh' if lang == 'zh' else 'en']


def _match_section(heading_text: str, sections) -> int | None:
    emoji, stem = _split_heading(heading_text)
    if emoji:
        for section in sections:
            if section.emoji == emoji:
                return section.index
    if stem:
        for section in sections:
            if any(stem.startswith(s) for s in section.stems):
                return section.index
    return None


def _scan_headings(text: str, sections):
    """Yield ``(line_start, line_end, level, section_index_or_None)``."""
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line.rstrip('\r\n'))
        if match:
            yield start, offset, len(match.group(1)), _match_section(match.group(2), sections)


def _present_headings(text: str, sections) -> dict[int, tuple[int, int, int]]:
    present = {}
    for start, end, level, index in _scan_headings(text, sections):
        if index is not None and index not in present:
            present[index] = (start, end, level)
    return present


def find_missing_sections(report: str, lang: str) -> list[RequiredSection]:
    """Return the required sections ``report`` lacks, in canonical order."""
    sections = required_sections(lang)
    present = _present_headings(report or '', sections)
    return [s for s in sections if s.index not in present]


def build_section_repair_request(missing, lang: str) -> str:
    listing = '\n'.join(f'## {s.heading}' for s in missing)
    if lang == 'zh':
        return (
            '你上面的报告缺少以下必需章节：\n\n'
            f'{listing}\n\n'
            '现在**只**撰写这些缺失的章节，按上面列出的顺序，每节以上面给出的标题原样开头，'
            '内容完整，满足原始要求中该节的全部深度要求。语言与格式规则（KaTeX 公式、'
            '图片嵌入、提示框）与原报告保持一致。**不要**重复或改写已经存在的任何章节，'
            '第一个标题之前和最后一节之后不要输出任何文字。不要调用工具，'
            '使用本对话中已经获得的资料。'
        )
    return (
        'Your report above is missing these required top-level sections:\n\n'
        f'{listing}\n\n'
        'Write ONLY these missing sections now, in the order listed, each starting '
        'with exactly the heading shown and complete to the depth the original '
        'instructions require for it. Use the same language and formatting rules '
        '(KaTeX math, figure embedding, callouts) as the report above. Do NOT repeat '
        'or rewrite any section that already exists, and write nothing before the '
        'first heading or after the last section. Do not call tools; use the '
        'research already gathered in this conversation.'
    )


def _extract_sections(repair_text: str, wanted: set[int], sections) -> dict[int, str]:
    headings = [h for h in _scan_headings(repair_text, sections) if h[3] is not None]
    bodies = {}
    for position, (_start, end, _level, index) in enumerate(headings):
        if index not in wanted or index in bodies:
            continue
        stop = headings[position + 1][0] if position + 1 < len(headings) else len(repair_text)
        body = repair_text[end:stop].strip('\n').rstrip()
        if body.strip():
            bodies[index] = body
    return bodies


def _section_end(report: str, heading_end: int, level: int) -> int:
    for start, _end, other_level, _index in _scan_headings(report[heading_end:], ()):
        if other_level <= level:
            return heading_end + start
    return len(report)


def insert_repaired_sections(report: str, repair_text: str, missing, lang: str):
    """Splice repaired sections into ``report`` at their canonical positions.

    Each section returned for a ``missing`` heading goes right after the
    nearest preceding present required section (or before the first present
    one). Anything else in ``repair_text`` — preamble, repeated existing
    sections — is dropped. Returns ``(new_report, repaired_sections)``.
    """
    sections = required_sections(lang)
    present = _present_headings(report, sections)
    if not present:
        return report, []
    bodies = _extract_sections(repair_text or '', {s.index for s in missing}, sections)
    repaired = [s for s in missing if s.index in bodies]
    if not repaired:
        return report, []
    first_start, _, first_level = min(present.values())
    insertions: dict[int, list[str]] = {}
    for section in repaired:
        anchors = [i for i in present if i < section.index]
        if anchors:
            _start, end, level = present[max(anchors)]
            offset = _section_end(report, end, level)
        else:
            offset, level = first_start, first_level
        insertions.setdefault(offset, []).append(
            f"{'#' * level} {section.heading}\n\n{bodies[section.index]}")
    for offset in sorted(insertions, reverse=True):
        block = '\n\n'.join(insertions[offset])
        head, tail = report[:offset].rstrip('\n'), report[offset:].lstrip('\n')
        report = (f'{head}\n\n' if head else '') + f'{block}\n' + (f'\n{tail}' if tail else '')
    return report, repaired


__all__ = [
    'RequiredSection',
    'build_section_repair_request',
    'find_missing_sections',
    'insert_repaired_sections',
    'required_sections',
    'section_headings_from_prompt',
]
