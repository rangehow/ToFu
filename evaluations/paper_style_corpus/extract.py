"""PDF → structured text and deterministic style features for one paper.

Responsibility: turn a corpus PDF into (a) a section-segmented text record
and (b) the measurable features the analysis compares across groups. Pure
function of the PDF bytes (PyMuPDF); no network, no model calls.

Segmentation: section headings are lines whose spans are bold or larger than
the body font and that look like a numbered heading ("3 Method", "4.2 ...")
or a known unnumbered heading (Abstract, References, Limitations, ...).
The body is the text before References/Bibliography; appendices follow it.
Section roles (introduction, related_work, method, experiments, conclusion,
limitations) come from the heading words of top-level sections.

Entry points: ``extract(pdf_path) -> dict``; ``features(record) -> dict``.
"""
from __future__ import annotations

import collections
import re
import statistics

import fitz  # PyMuPDF

from lib.longform.documents.style_metrics import (
    contribution_list, finding_verbs_per_k, lexical, sentences, words)

_NUMBERED = re.compile(r'^(?:(?:\d{1,2}|[IVX]{1,4}|[A-H])(?:\.\d{1,2}){0,3})\.?\s+([A-Z][^.]{1,90})$')
_UNNUMBERED = re.compile(
    r'^(abstract|introduction|related work|background|conclusions?|references|'
    r'bibliography|limitations?|acknowledge?ments?|ethics statement|broader impacts?|'
    r'impact statement|discussion|appendix|reproducibility statement)\b', re.I)
_STOP_BODY = re.compile(r'^(references|bibliography)$', re.I)
_CAPTION = re.compile(r'^(Figure|Fig\.|Table)\s*(\d+)\s*[:.|]', re.I)

ROLE_WORDS = (
    ('introduction', ('introduction',)),
    ('related_work', ('related work', 'background', 'prior work', 'previous work',
                      'literature', 'preliminar')),
    ('limitations', ('limitation',)),
    ('conclusion', ('conclusion', 'concluding', 'summary and', 'discussion')),
    ('experiments', ('experiment', 'evaluation', 'results', 'empirical', 'analysis',
                     'ablation', 'benchmark')),
    ('method', ('method', 'approach', 'model', 'framework', 'algorithm', 'our ',
                'proposed', 'architecture', 'formulation', 'setup', 'problem')),
)


_MISC = re.compile(r'acknowledg|ethic|broader|impact|reproducib|appendix|disclosure|funding|author contrib', re.I)


def _role(heading: str) -> str:
    text = heading.lower()
    if _MISC.search(text):
        return 'other'
    if 'theor' in text:
        return 'method'
    for role, role_words in ROLE_WORDS:
        if any(word in text for word in role_words):
            return role
    return 'other'


def _clean(text: str) -> str:
    text = re.sub(r'-\n(?=[a-z])', '', text)
    text = re.sub(r'\s*\n\s*', ' ', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def extract(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    sizes = collections.Counter()
    pages = []
    for page in doc:
        data = page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)
        pages.append(data)
        for block in data['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    sizes[round(span['size'], 1)] += len(span['text'])
    body_size = sizes.most_common(1)[0][0] if sizes else 10.0
    lines = []   # (page, kind, text)
    for page_no, data in enumerate(pages):
        for block in data['blocks']:
            block_lines = []
            for line in block.get('lines', []):
                text = ''.join(span['text'] for span in line['spans']).strip()
                if not text:
                    continue
                spans = [s for s in line['spans'] if s['text'].strip()]
                bold = all((s['flags'] & 16) or 'bold' in s['font'].lower()
                           or 'medi' in s['font'].lower() for s in spans)
                big = max(s['size'] for s in spans) >= body_size + 0.8
                block_lines.append((text, bold, big))
            if not block_lines:
                continue
            first, bold, big = block_lines[0]
            consumed = 1
            # LaTeX headings often extract as two lines: "3.2" then "Title".
            if (re.fullmatch(r'(?:\d{1,2}|[A-H])(?:\.\d{1,2}){0,3}\.?', first)
                    and len(block_lines) > 1 and (block_lines[1][1] or block_lines[1][2])):
                first = f'{first} {block_lines[1][0]}'
                consumed = 2
            heading = None
            if (bold or big) and len(first) < 100:
                if _NUMBERED.match(first) or _UNNUMBERED.match(first):
                    heading = first
            if heading:
                lines.append((page_no, 'heading', heading))
                rest = '\n'.join(t for t, _b, _g in block_lines[consumed:])
                if rest:
                    lines.append((page_no, 'text', rest))
            else:
                lines.append((page_no, 'text', '\n'.join(t for t, _b, _g in block_lines)))
    # Title: largest-font line on page 1.
    title = ''
    best = 0
    if pages:
        for block in pages[0]['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    if span['size'] > best and len(span['text'].strip()) > 3:
                        best = span['size']
                        title = span['text'].strip()
    sections = []   # {heading, role, level, page, text}
    current = {'heading': '_front', 'role': 'front', 'level': 0, 'page': 0, 'parts': []}
    captions = []
    in_body = True
    for page_no, kind, text in lines:
        if kind == 'heading':
            sections.append(current)
            if _STOP_BODY.match(text.strip()):
                in_body = False
            number = re.match(r'^([\dIVXA-H]+(?:\.\d+)*)\.?\s', text)
            level = number.group(1).count('.') + 1 if number else 1
            current = {'heading': text, 'role': _role(text) if in_body else 'back',
                       'level': level, 'page': page_no, 'parts': [], 'body': in_body}
            if text.lower().startswith('abstract'):
                current['role'] = 'abstract'
            continue
        cap = _CAPTION.match(text.strip())
        if cap:
            captions.append({'kind': 'table' if cap.group(1).lower() == 'table' else 'figure',
                             'num': int(cap.group(2)), 'page': page_no, 'body': in_body,
                             'text': _clean(text)[:600]})
            continue
        current['parts'].append(text)
    sections.append(current)
    # Unrecognised top-level body sections before the experiments are the
    # method (papers name their method section after the method itself).
    seen_experiments = False
    for sec in sections:
        if sec['role'] == 'experiments' and sec['level'] == 1:
            seen_experiments = True
        if (sec['role'] == 'other' and sec['level'] == 1 and sec.get('body', True)
                and not seen_experiments and not _MISC.search(sec['heading'])):
            sec['role'] = 'method'
    # Top-level roles: a subsection inherits its parent's role.
    parent_role = 'front'
    for sec in sections:
        sec['text'] = _clean('\n'.join(sec.pop('parts')))
        sec.setdefault('body', True)
        if sec['role'] in ('abstract', 'front', 'back'):
            parent_role = sec['role'] if sec['role'] != 'abstract' else 'abstract'
            continue
        if sec['level'] == 1:
            parent_role = sec['role']
        elif parent_role not in ('front', 'abstract', 'back'):
            sec['role'] = parent_role
    abstract = next((s['text'] for s in sections if s['role'] == 'abstract'), '')
    if not abstract:   # inline "Abstract—..." or "Abstract. ..." styles
        front = ' '.join(s['text'] for s in sections[:2])
        match = re.search(r'\bAbstract[\s.:—-]+(.{200,2500}?)(?=\b1\.?\s+Introduction|$)', front)
        abstract = match.group(1) if match else ''
    page1_text = pages[0] and ' '.join(
        ''.join(s['text'] for s in line['spans'])
        for block in pages[0]['blocks'] for line in block.get('lines', [])) or ''
    page1 = doc[0] if len(doc) else None
    page1_images = 0
    page1_drawing_area = 0.0
    if page1 is not None:
        area = page1.rect.width * page1.rect.height
        for info in page1.get_image_info():
            bbox = fitz.Rect(info['bbox'])
            if bbox.width * bbox.height > 0.02 * area:
                page1_images += 1
        drawings = page1.get_drawings()
        union = fitz.Rect()
        for drawing in drawings:
            if drawing['rect'].width * drawing['rect'].height < 0.5 * area:
                union |= drawing['rect']
        page1_drawing_area = (union.width * union.height / area) if len(drawings) > 20 else 0.0
    return {'title_pdf': title, 'pages': len(doc), 'body_font': body_size,
            'abstract': _clean(abstract), 'sections': sections, 'captions': captions,
            'page1_has_fig1_caption': bool(re.search(r'\b(Figure|Fig\.)\s*1\s*[:.|]', page1_text)),
            'page1_images': page1_images, 'page1_drawing_area': round(page1_drawing_area, 3)}


# ── features ─────────────────────────────────────────────────────────────

COLON_NAME = re.compile(r'^\s*[A-Z][\w\-+.\d]*(\s[\w\-+.\d]+){0,2}\s*:\s+')


def title_features(title: str) -> dict:
    t = title.strip()
    tokens = words(t)
    first = tokens[0].lower() if tokens else ''
    return {
        'title_words': len(tokens),
        'title_colon': int(':' in t),
        'title_name_colon': int(bool(COLON_NAME.match(t))),
        'title_question': int(t.endswith('?')),
        'title_we_you': int(bool(re.search(r'\b(you|your|we|our)\b', t, re.I))),
        'title_claim_verb': int(bool(re.search(
            r'\b(is|are|can|do|does|make|makes|yields?|enables?|improves?|beats?|'
            r'reveals?|matters?|helps?|elicits?|learn|learns)\b', t, re.I))),
        'title_towards': int(first in ('towards', 'toward', 'on', 'a', 'an')),
        'title_acronym': int(bool(re.search(r'\b[A-Z][A-Za-z]*[A-Z][A-Za-z\d-]*\b', t))),
    }


def features(record: dict, title: str) -> dict:
    sections = record['sections']
    body = [s for s in sections if s.get('body', True) and s['role'] not in ('front', 'back', 'abstract')]
    by_role = collections.defaultdict(str)
    for sec in body:
        by_role[sec['role']] += ' ' + sec['text']
    body_text = ' '.join(by_role.values())
    n_body = len(words(body_text))
    intro = by_role['introduction']
    abstract = record['abstract']
    feats = {'pages': record['pages'], 'body_words': n_body,
             'n_sections_top': sum(1 for s in body if s['level'] == 1),
             'n_subsections': sum(1 for s in body if s['level'] > 1)}
    feats.update(title_features(title))
    for role in ('introduction', 'related_work', 'method', 'experiments', 'conclusion',
                 'limitations'):
        n = len(words(by_role[role]))
        feats[f'has_{role}'] = int(n > 30)
        feats[f'share_{role}'] = n / n_body if n_body else float('nan')
    feats['related_work_position'] = next(
        (round(i / max(1, len(body)), 3) for i, s in enumerate(body)
         if s['role'] == 'related_work' and s['level'] == 1), float('nan'))
    feats['related_work_after_method'] = (
        int(feats['related_work_position'] > 0.5)
        if feats['related_work_position'] == feats['related_work_position'] else float('nan'))
    feats['has_limitations_any'] = int(bool(re.search(r'\blimitations?\b', ' '.join(
        s['heading'] for s in sections), re.I)) or bool(re.search(
            r'\b(limitation|shortcoming)s?\b', by_role['conclusion'], re.I)))
    abs_lex = lexical(abstract)
    feats.update({f'abs_{k}': v for k, v in abs_lex.items()})
    feats['abs_sentences'] = len(sentences(abstract))
    feats['abs_has_number'] = int(bool(re.search(r'\d', abstract)))
    feats['abs_has_code_link'] = int(bool(re.search(r'github|https?://|code (is|and)', abstract, re.I)))
    intro_lex = lexical(intro)
    feats.update({f'intro_{k}': v for k, v in intro_lex.items()})
    feats['intro_contrib_list'] = contribution_list(intro)
    feats['intro_figure_ref'] = int(bool(re.search(r'\b(Figure|Fig\.)\s*1\b', intro)))
    feats['intro_first_person_findings'] = finding_verbs_per_k(intro)
    body_lex = lexical(body_text)
    feats.update({f'body_{k}': v for k, v in body_lex.items()})
    caps = [c for c in record['captions'] if c['body']]
    figs = {c['num'] for c in caps if c['kind'] == 'figure'}
    tabs = {c['num'] for c in caps if c['kind'] == 'table'}
    feats['n_figures_body'] = len(figs)
    feats['n_tables_body'] = len(tabs)
    feats['figures_per_page'] = len(figs) / record['pages'] if record['pages'] else float('nan')
    cap_words = [len(words(c['text'])) for c in caps]
    feats['caption_words_mean'] = statistics.mean(cap_words) if cap_words else float('nan')
    feats['caption_takeaway'] = (statistics.mean(
        int(len(sentences(c['text'])) >= 2) for c in caps) if caps else float('nan'))
    feats['teaser_page1'] = int(record['page1_has_fig1_caption'])
    feats['page1_visual'] = int(record['page1_has_fig1_caption'] or record['page1_images'] > 0
                                or record['page1_drawing_area'] > 0.08)
    return feats
