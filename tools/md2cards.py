#!/usr/bin/env python3
"""
md2cards.py — Convert a Markdown article into beautifully styled card images
for posting on Xiaohongshu (小红书).

Usage:
    python tools/md2cards.py docs/article-cache-optimization-zh.md -o ./cards_output

Features:
    - 3:4 vertical ratio (1080×1440) — ideal for Xiaohongshu
    - Beautiful typography with Chinese font support
    - Smart section splitting — never cuts mid-paragraph
    - Syntax-highlighted code blocks
    - Styled tables, blockquotes, lists
    - Page numbering (e.g. "3/9")
    - Consistent branding across all cards
"""

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Card dimensions (pixels at 2x for retina)
# ---------------------------------------------------------------------------
CARD_W = 1080
CARD_H = 1440

# ---------------------------------------------------------------------------
# CSS Theme
# ---------------------------------------------------------------------------
CSS = """
/* Local fonts only — no external imports */

:root {
  --bg: #FFFDF7;
  --card-bg: #FFFFFF;
  --text: #1a1a2e;
  --text-secondary: #555770;
  --accent: #6C5CE7;
  --accent-light: #A29BFE;
  --accent-bg: #F0EEFF;
  --code-bg: #1E1E2E;
  --code-text: #CDD6F4;
  --border: #E8E6F0;
  --table-header-bg: #6C5CE7;
  --table-header-text: #FFFFFF;
  --table-alt-bg: #F8F7FF;
  --quote-bg: #FFF8E1;
  --quote-border: #FFB74D;
  --tag-bg: #E8F5E9;
  --tag-text: #2E7D32;
  --gold: #F59E0B;
  --silver: #94A3B8;
  --bronze: #D97706;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  width: ${CARD_W}px;
  min-height: ${CARD_H}px;
  background: var(--bg);
  font-family: 'FandolHei', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}

.card {
  width: ${CARD_W}px;
  min-height: ${CARD_H}px;
  background: var(--bg);
  padding: 48px 48px 72px;
  position: relative;
  display: flex;
  flex-direction: column;
}

/* --- Cover Card --- */
.cover-card {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  padding: 80px 60px;
  justify-content: center;
  align-items: center;
  text-align: center;
}

.cover-card .emoji-hero {
  font-size: 72px;
  margin-bottom: 32px;
}

.cover-card h1 {
  font-size: 52px;
  font-weight: 900;
  line-height: 1.3;
  margin-bottom: 28px;
  letter-spacing: -1px;
}

.cover-card .subtitle {
  font-size: 22px;
  opacity: 0.9;
  line-height: 1.6;
  max-width: 800px;
}

.cover-card .author-tag {
  margin-top: 48px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  background: rgba(255,255,255,0.2);
  border-radius: 24px;
  padding: 10px 24px;
  font-size: 18px;
  backdrop-filter: blur(10px);
}

/* --- Section Header --- */
.section-header {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 28px;
}

.section-num {
  background: var(--accent);
  color: white;
  font-size: 18px;
  font-weight: 700;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.section-title {
  font-size: 32px;
  font-weight: 900;
  color: var(--text);
  line-height: 1.3;
}

/* --- Subsection --- */
h3 {
  font-size: 20px;
  font-weight: 700;
  color: var(--accent);
  margin: 22px 0 10px;
  padding-left: 14px;
  border-left: 4px solid var(--accent);
}

/* --- Paragraph --- */
p {
  font-size: 17px;
  line-height: 1.75;
  color: var(--text);
  margin-bottom: 12px;
}

/* --- Strong / Em --- */
strong { color: var(--accent); font-weight: 700; }
em { font-style: italic; color: var(--text-secondary); }

/* --- Inline code --- */
code:not(pre code) {
  background: var(--accent-bg);
  color: var(--accent);
  padding: 2px 8px;
  border-radius: 6px;
  font-family: 'FandolFang', 'JetBrains Mono', monospace;
  font-size: 16px;
  font-weight: 500;
}

/* --- Code blocks --- */
pre {
  background: var(--code-bg);
  border-radius: 12px;
  padding: 16px 20px;
  margin: 12px 0 14px;
  overflow-x: auto;
  position: relative;
}

pre code {
  font-family: 'FandolFang', 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.55;
  color: var(--code-text);
  white-space: pre;
}

/* Syntax highlighting */
.code-comment { color: #6C7086; font-style: italic; }
.code-keyword { color: #CBA6F7; }
.code-string { color: #A6E3A1; }
.code-number { color: #FAB387; }
.code-symbol { color: #F38BA8; }
.code-type { color: #89DCEB; }

/* --- Tables --- */
table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  margin: 12px 0 14px;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 2px 12px rgba(108, 92, 231, 0.08);
  font-size: 16px;
}

thead th {
  background: var(--table-header-bg);
  color: var(--table-header-text);
  padding: 10px 12px;
  font-weight: 700;
  text-align: left;
  font-size: 14px;
  white-space: nowrap;
}

tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  line-height: 1.45;
  vertical-align: top;
  font-size: 15px;
}

tbody tr:nth-child(even) { background: var(--table-alt-bg); }
tbody tr:last-child td { border-bottom: none; }

/* --- Blockquote --- */
blockquote {
  background: var(--quote-bg);
  border-left: 5px solid var(--quote-border);
  border-radius: 0 12px 12px 0;
  padding: 14px 18px;
  margin: 12px 0 14px;
  font-size: 16px;
  line-height: 1.6;
  color: #5D4037;
}

blockquote p { margin-bottom: 6px; color: #5D4037; }

/* --- Lists --- */
ul, ol {
  margin: 8px 0 12px 24px;
  font-size: 17px;
  line-height: 1.75;
}

li { margin-bottom: 8px; }

/* --- Horizontal Rule --- */
hr {
  border: none;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-light), transparent);
  margin: 32px 0;
}

/* --- Special Blocks --- */
.key-insight {
  background: linear-gradient(135deg, #F0EEFF, #E8F5FF);
  border: 2px solid var(--accent-light);
  border-radius: 16px;
  padding: 22px 26px;
  margin: 20px 0;
}

.key-insight .insight-label {
  font-size: 14px;
  font-weight: 700;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 8px;
}

/* --- Results highlight --- */
.result-box {
  background: linear-gradient(135deg, #E8F5E9, #F1F8E9);
  border: 2px solid #66BB6A;
  border-radius: 16px;
  padding: 22px 26px;
  margin: 20px 0;
}

/* --- Page Footer --- */
.page-footer {
  position: absolute;
  bottom: 28px;
  left: 56px;
  right: 56px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 14px;
  color: #B0AEC6;
}

.page-footer .brand {
  display: flex;
  align-items: center;
  gap: 6px;
}

.page-footer .page-num {
  font-weight: 700;
  color: var(--accent-light);
  font-size: 15px;
}

/* --- Ranking medals --- */
.medal { font-size: 20px; }

/* --- Diagram-like code blocks --- */
.diagram {
  background: #F8F9FF;
  border: 2px solid var(--border);
  border-radius: 12px;
  padding: 16px 20px;
  margin: 12px 0 14px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.55;
  color: var(--text);
  white-space: pre;
  overflow-x: auto;
}

/* --- Summary card --- */
.summary-card {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  color: white;
  padding: 60px;
}

.summary-card h2 { color: var(--accent-light); }
.summary-card p { color: #CDD6F4; }
.summary-card strong { color: #A6E3A1; }

/* --- Spacer --- */
.spacer { height: 20px; }
.spacer-lg { height: 36px; }
""".replace('${CARD_W}', str(CARD_W)).replace('${CARD_H}', str(CARD_H))


# ---------------------------------------------------------------------------
# Markdown → HTML conversion (lightweight, no external deps)
# ---------------------------------------------------------------------------

def _highlight_code(code: str, lang: str) -> str:
    """Very basic syntax highlighting via regex.

    Uses a two-phase approach: first collect spans with positions,
    then apply non-overlapping spans from left to right.
    For JSON or unknown langs, just escape HTML.
    """
    import html as html_mod
    code = html_mod.escape(code)

    # Skip highlighting for JSON or unknown langs
    if lang in ('json', 'jsonc', ''):
        return code

    # Collect all highlight spans as (start, end, css_class) tuples
    spans = []

    # Comments (← arrows, //, #)
    for m in re.finditer(r'(←.*?)$', code, re.MULTILINE):
        spans.append((m.start(), m.end(), 'code-comment'))
    for m in re.finditer(r'(//.*?)$', code, re.MULTILINE):
        spans.append((m.start(), m.end(), 'code-comment'))
    for m in re.finditer(r'(?:^|\s)(#\s.*?)$', code, re.MULTILINE):
        spans.append((m.start(1), m.end(1), 'code-comment'))

    # Strings
    for m in re.finditer(r'&quot;[^&]*?&quot;', code):
        spans.append((m.start(), m.end(), 'code-string'))
    for m in re.finditer(r"&#x27;[^&]*?&#x27;", code):
        spans.append((m.start(), m.end(), 'code-string'))

    # Keywords
    kw_pattern = r'\b(def|class|return|import|from|if|else|elif|for|while|try|except|raise|with|as|in|not|and|or|const|let|var|function|async|await|true|false|null|None|True|False)\b'
    for m in re.finditer(kw_pattern, code):
        spans.append((m.start(), m.end(), 'code-keyword'))

    # Numbers
    for m in re.finditer(r'\b(\d[\d,\.]*[KMk]?)\b', code):
        spans.append((m.start(), m.end(), 'code-number'))

    if not spans:
        return code

    # Sort by start position; resolve overlaps (first match wins)
    spans.sort(key=lambda x: (x[0], -x[1]))
    non_overlapping = []
    end_pos = 0
    for s, e, cls in spans:
        if s >= end_pos:
            non_overlapping.append((s, e, cls))
            end_pos = e

    # Build result string with non-overlapping spans
    parts = []
    prev = 0
    for s, e, cls in non_overlapping:
        parts.append(code[prev:s])
        parts.append(f'<span class="{cls}">{code[s:e]}</span>')
        prev = e
    parts.append(code[prev:])

    return ''.join(parts)


def _md_table_to_html(lines: list[str]) -> str:
    """Convert markdown table lines to HTML table."""
    if len(lines) < 2:
        return ''

    headers = [c.strip() for c in lines[0].strip('|').split('|')]

    # Skip separator line (line with ---)
    data_start = 1
    if data_start < len(lines) and re.match(r'^\|[\s\-:|]+\|$', lines[data_start].strip()):
        data_start = 2

    html = '<table><thead><tr>'
    for h in headers:
        h = _inline_md(h)
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'

    for line in lines[data_start:]:
        cells = [c.strip() for c in line.strip('|').split('|')]
        html += '<tr>'
        for c in cells:
            c = _inline_md(c)
            html += f'<td>{c}</td>'
        html += '</tr>'

    html += '</tbody></table>'
    return html


def _inline_md(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links)."""
    import html as html_mod
    # Inline code first (protect from other transforms)
    parts = re.split(r'(`[^`]+`)', text)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`'):
            code_text = html_mod.escape(part[1:-1])
            result.append(f'<code>{code_text}</code>')
        else:
            # Bold
            part = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', part)
            # Italic
            part = re.sub(r'\*(.+?)\*', r'<em>\1</em>', part)
            # Links
            part = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', part)
            result.append(part)
    return ''.join(result)


def md_to_html_sections(md_text: str) -> list[dict]:
    """
    Parse markdown into logical card sections.
    Returns list of {'title': str, 'num': str, 'html': str, 'type': str}.
    """
    lines = md_text.split('\n')
    sections = []
    current_section = None
    i = 0

    def flush():
        nonlocal current_section
        if current_section and current_section.get('html', '').strip():
            sections.append(current_section)
        current_section = None

    while i < len(lines):
        line = lines[i]

        # Skip cover image line
        if line.strip().startswith('![') and '封面' in line:
            i += 1
            continue

        # H1 — Title (cover card)
        if line.startswith('# ') and not line.startswith('## '):
            flush()
            title_text = line[2:].strip()
            current_section = {
                'type': 'cover',
                'title': title_text,
                'num': '',
                'html': ''
            }
            i += 1
            # Collect the abstract blockquote
            abstract_html = ''
            while i < len(lines):
                if lines[i].strip().startswith('> '):
                    text = lines[i].strip()[2:]
                    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # strip bold for cover
                    abstract_html += text + ' '
                    i += 1
                elif lines[i].strip() == '' or lines[i].strip() == '---':
                    i += 1
                else:
                    break
            current_section['abstract'] = abstract_html.strip()
            continue

        # H2 — Major section
        if line.startswith('## '):
            flush()
            title = line[3:].strip()
            # Extract section number (e.g., "一", "二", ...)
            m = re.match(r'([\u4e00-\u9fff\d]+)[、.．](.+)', title)
            if m:
                num = m.group(1)
                title_text = m.group(2).strip()
            else:
                num = ''
                title_text = title
            current_section = {
                'type': 'section',
                'title': title_text,
                'num': num,
                'html': ''
            }
            i += 1
            continue

        # If no section yet, skip
        if current_section is None:
            i += 1
            continue

        # H3/H4 — Subsection
        if re.match(r'^#{3,4}\s', line):
            # Strip leading #'s to get title text
            stripped = line.lstrip('#').strip()
            current_section['html'] += f'<h3>{_inline_md(stripped)}</h3>\n'
            i += 1
            continue

        # Code block
        if line.strip().startswith('```'):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code = '\n'.join(code_lines)

            # Detect if it's a diagram (contains box-drawing chars or arrows)
            is_diagram = any(c in code for c in '┌┐└┘│├┤─═╔╗╚╝║')
            if is_diagram:
                import html as html_mod
                current_section['html'] += f'<div class="diagram">{html_mod.escape(code)}</div>\n'
            else:
                highlighted = _highlight_code(code, lang)
                current_section['html'] += f'<pre><code>{highlighted}</code></pre>\n'
            continue

        # Table
        if '|' in line and i + 1 < len(lines) and '---' in lines[i + 1]:
            table_lines = []
            while i < len(lines) and '|' in lines[i]:
                table_lines.append(lines[i])
                i += 1
            current_section['html'] += _md_table_to_html(table_lines) + '\n'
            continue

        # Blockquote
        if line.strip().startswith('> '):
            quote_lines = []
            while i < len(lines) and (lines[i].strip().startswith('> ') or lines[i].strip().startswith('>')):
                text = re.sub(r'^>\s?', '', lines[i].strip())
                quote_lines.append(text)
                i += 1
            quote_html = '<br>'.join(_inline_md(l) for l in quote_lines)
            current_section['html'] += f'<blockquote><p>{quote_html}</p></blockquote>\n'
            continue

        # Unordered list
        if re.match(r'^[-*]\s', line.strip()):
            items = []
            while i < len(lines) and re.match(r'^[-*]\s', lines[i].strip()):
                items.append(lines[i].strip()[2:])
                i += 1
            current_section['html'] += '<ul>'
            for item in items:
                current_section['html'] += f'<li>{_inline_md(item)}</li>'
            current_section['html'] += '</ul>\n'
            continue

        # Ordered list
        if re.match(r'^\d+\.\s', line.strip()):
            items = []
            while i < len(lines) and re.match(r'^\*?\*?\d+', lines[i].strip()):
                text = re.sub(r'^\*?\*?\d+[\.\)]\s*\*?\*?\s*', '', lines[i].strip())
                items.append(text)
                i += 1
            current_section['html'] += '<ol>'
            for item in items:
                current_section['html'] += f'<li>{_inline_md(item)}</li>'
            current_section['html'] += '</ol>\n'
            continue

        # HR
        if line.strip() == '---':
            current_section['html'] += '<hr>\n'
            i += 1
            continue

        # Empty line
        if line.strip() == '':
            i += 1
            continue

        # Paragraph
        para_lines = []
        while i < len(lines) and lines[i].strip() and not lines[i].startswith('#') \
                and not lines[i].strip().startswith('```') and not lines[i].strip().startswith('|') \
                and not lines[i].strip().startswith('> ') and not lines[i].strip() == '---' \
                and not re.match(r'^[-*]\s', lines[i].strip()) \
                and not re.match(r'^\d+\.\s', lines[i].strip()):
            para_lines.append(lines[i].strip())
            i += 1
        if para_lines:
            text = ' '.join(para_lines)
            current_section['html'] += f'<p>{_inline_md(text)}</p>\n'
        else:
            # Safety: if no handler matched this line, skip it to avoid infinite loop
            i += 1

    flush()
    return sections


def build_card_html(section: dict, page_num: int, total_pages: int) -> str:
    """Build the full HTML for a single card."""

    if section['type'] == 'cover':
        inner = f'''
        <div class="cover-card card">
            <div class="emoji-hero" style="font-size:64px;font-weight:900;letter-spacing:4px;opacity:0.9;">$ → ¢</div>
            <h1>{section["title"]}</h1>
            <div class="subtitle">{section.get("abstract", "")}</div>
            <div class="author-tag">Tofu · AI 缓存优化实战</div>
            <div class="page-footer" style="color: rgba(255,255,255,0.5);">
                <span class="brand">豆腐 Tofu</span>
                <span class="page-num">{page_num}/{total_pages}</span>
            </div>
        </div>
        '''
    else:
        # Regular section card
        header = ''
        if section['num']:
            header = f'''
            <div class="section-header">
                <div class="section-num">{section["num"]}</div>
                <div class="section-title">{section["title"]}</div>
            </div>
            '''
        else:
            header = f'<div class="section-header"><div class="section-title">{section["title"]}</div></div>'

        inner = f'''
        <div class="card">
            {header}
            <div class="content">
                {section["html"]}
            </div>
            <div class="page-footer">
                <span class="brand">豆腐 Tofu</span>
                <span class="page-num">{page_num}/{total_pages}</span>
            </div>
        </div>
        '''

    return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width={CARD_W}">
<style>{CSS}</style>
</head><body>
{inner}
</body></html>'''


# ---------------------------------------------------------------------------
# Smart section merging — combine small sections to fill cards
# ---------------------------------------------------------------------------

def merge_small_sections(sections: list[dict]) -> list[dict]:
    """
    Merge consecutive non-cover sections that are too short to fill a card.
    Also split sections that are too long into multiple cards.
    """
    merged = []
    for sec in sections:
        if sec['type'] == 'cover':
            merged.append(sec)
            continue

        # Estimate content height by character count
        # Rough heuristic: ~800 chars of HTML content fits one card comfortably
        html_len = len(sec['html'])

        if html_len > 2800 and merged and merged[-1]['type'] != 'cover':
            # Section is very long — keep as-is, it may need splitting later
            merged.append(sec)
        elif html_len < 800 and merged and merged[-1]['type'] not in ('cover',) and len(merged[-1].get('html', '')) < 2400:
            # Too short — merge with previous (only if previous isn't already large)
            prev = merged[-1]
            prev['html'] += f'<hr>\n<div class="section-header"><div class="section-num">{sec["num"]}</div><div class="section-title">{sec["title"]}</div></div>\n'
            prev['html'] += sec['html']
            prev['title'] = prev['title']  # keep original title
        else:
            merged.append(sec)

    return merged


# ---------------------------------------------------------------------------
# Split oversized sections into multiple cards
# ---------------------------------------------------------------------------

def split_oversized(sections: list[dict], max_chars: int = 2400) -> list[dict]:
    """Split sections whose HTML content is too long for one card."""
    result = []
    for sec in sections:
        if sec['type'] == 'cover' or len(sec['html']) <= max_chars:
            result.append(sec)
            continue

        # Split at natural boundaries: <h3>, <hr>, <table>, <pre>, <blockquote>
        # Strategy: greedily accumulate blocks until we exceed max_chars
        blocks = re.split(r'(?=<(?:h3|hr|table|pre|blockquote|div class="diagram"|ul|ol))', sec['html'])

        current_html = ''
        part_num = 0
        for block in blocks:
            if len(current_html) + len(block) > max_chars and current_html.strip():
                # Emit current card
                part_num += 1
                result.append({
                    'type': 'section',
                    'title': sec['title'] + (f'（续）' if part_num > 1 else ''),
                    'num': sec['num'],
                    'html': current_html
                })
                current_html = block
            else:
                current_html += block

        if current_html.strip():
            part_num += 1
            result.append({
                'type': 'section',
                'title': sec['title'] + (f'（续）' if part_num > 1 else ''),
                'num': sec['num'],
                'html': current_html
            })

    return result


# ---------------------------------------------------------------------------
# Render cards to images using Playwright
# ---------------------------------------------------------------------------

def _measure_height(page, section: dict, idx: int, total: int) -> int:
    """Render a section in Playwright and return its pixel height."""
    html = build_card_html(section, idx + 1, total)
    page.set_content(html, wait_until='domcontentloaded')
    page.wait_for_timeout(150)
    return page.evaluate('document.body.scrollHeight')


def _adaptive_split(sections: list[dict], page, max_height: int = CARD_H) -> list[dict]:
    """
    Two-pass split: measure each section's rendered height. If it exceeds
    max_height, binary-search for the right HTML split point.
    """
    result = []
    total = len(sections)  # approximate for measurement
    for idx, sec in enumerate(sections):
        if sec['type'] == 'cover':
            result.append(sec)
            continue

        h = _measure_height(page, sec, idx, total)
        if h <= max_height + 20:
            result.append(sec)
            continue

        # Need to split this section. Split at block boundaries.
        blocks = re.split(r'(?=<(?:h3|hr|table|pre|blockquote|div class="diagram"|ul|ol|p))', sec['html'])
        blocks = [b for b in blocks if b.strip()]

        current_html = ''
        part_num = 0
        for block in blocks:
            candidate = current_html + block
            test_sec = {**sec, 'html': candidate}
            test_h = _measure_height(page, test_sec, idx, total)

            if test_h > max_height and current_html.strip():
                # Emit current card
                part_num += 1
                result.append({
                    'type': 'section',
                    'title': sec['title'] + ('（续）' if part_num > 1 else ''),
                    'num': sec['num'],
                    'html': current_html
                })
                current_html = block
            else:
                current_html = candidate

        if current_html.strip():
            part_num += 1
            result.append({
                'type': 'section',
                'title': sec['title'] + ('（续）' if part_num > 1 else ''),
                'num': sec['num'],
                'html': current_html
            })

        print(f"  Split '{sec['title'][:20]}' ({h}px) → {part_num} cards")

    # Post-pass: merge trailing short cards with previous
    while len(result) >= 2:
        last = result[-1]
        prev = result[-2]
        if last['type'] == 'cover' or prev['type'] == 'cover':
            break
        # Try merging and see if it fits
        merged_html = prev['html'] + '<hr>\n' + last['html']
        test_sec = {**prev, 'html': merged_html}
        test_h = _measure_height(page, test_sec, len(result) - 2, len(result))
        if test_h <= max_height:
            prev['html'] = merged_html
            result.pop()
            print(f"  Merged trailing card → {len(result)} total")
        else:
            break

    return result


def render_cards(sections: list[dict], output_dir: str):
    """Render each section as a card image using Playwright.

    Uses adaptive splitting: measures rendered height and re-splits
    sections that exceed the target card height.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(output_dir, exist_ok=True)

    print(f"Pass 1: Measuring heights & splitting oversized sections...")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': CARD_W, 'height': CARD_H})

        # Pass 1: adaptive split based on real rendered heights
        sections = _adaptive_split(sections, page)
        total = len(sections)

        print(f"\nPass 2: Rendering {total} final cards...")

        for i, section in enumerate(sections):
            page_num = i + 1
            html = build_card_html(section, page_num, total)

            # Write HTML for debugging
            html_path = os.path.join(output_dir, f'card_{page_num:02d}.html')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html)

            page.set_content(html, wait_until='domcontentloaded')
            page.wait_for_timeout(150)

            # Get actual content height
            body_height = page.evaluate('document.body.scrollHeight')
            actual_height = max(CARD_H, body_height)

            # Resize viewport to capture full content
            page.set_viewport_size({'width': CARD_W, 'height': actual_height})
            page.wait_for_timeout(100)

            # Screenshot
            img_path = os.path.join(output_dir, f'card_{page_num:02d}.png')
            page.screenshot(path=img_path, full_page=True)

            # Reset viewport for next card
            page.set_viewport_size({'width': CARD_W, 'height': CARD_H})

            title_preview = section.get('title', '')[:30]
            overflow = '  ⚠️ OVER' if actual_height > CARD_H + 20 else ''
            print(f"  [{page_num}/{total}] {title_preview}  ({actual_height}px){overflow}")

        browser.close()

    print(f"\n✅ Done! {total} cards saved to {output_dir}/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Convert Markdown to Xiaohongshu card images')
    parser.add_argument('input', help='Path to markdown file')
    parser.add_argument('-o', '--output', default='./cards_output', help='Output directory')
    parser.add_argument('--debug-html', action='store_true', help='Only generate HTML, skip screenshots')
    args = parser.parse_args()

    # Read markdown
    md_path = Path(args.input)
    if not md_path.exists():
        print(f"Error: {md_path} not found")
        sys.exit(1)

    md_text = md_path.read_text(encoding='utf-8')

    # Parse into sections
    sections = md_to_html_sections(md_text)
    print(f"Parsed {len(sections)} raw sections")

    # Merge small sections
    sections = merge_small_sections(sections)
    print(f"After merging small sections: {len(sections)}")

    # NOTE: We skip char-count-based splitting here.
    # The adaptive Playwright-based split in render_cards() handles this
    # accurately based on real rendered pixel heights.
    # sections = split_oversized(sections)
    print(f"Sections before render: {len(sections)}")

    # Post-process: merge trailing short sections with previous
    merged_any = True
    while merged_any and len(sections) >= 2:
        merged_any = False
        last = sections[-1]
        prev = sections[-2]
        if last['type'] != 'cover' and prev['type'] != 'cover' and len(last['html']) < 1200 and len(prev['html']) + len(last['html']) < 3200:
            prev['html'] += f'<hr>\n{last["html"]}'
            sections = sections[:-1]
            merged_any = True
            print(f"Merged short trailing section → {len(sections)} cards")

    if args.debug_html:
        os.makedirs(args.output, exist_ok=True)
        total = len(sections)
        for i, sec in enumerate(sections):
            html = build_card_html(sec, i + 1, total)
            path = os.path.join(args.output, f'card_{i+1:02d}.html')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"  [{i+1}/{total}] {sec.get('title', '')[:30]}")
        print(f"\n✅ HTML files saved to {args.output}/ (use --debug-html to skip rendering)")
    else:
        render_cards(sections, args.output)


if __name__ == '__main__':
    main()
