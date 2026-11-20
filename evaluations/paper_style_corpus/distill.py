"""Distil supported findings into candidate style rules and per-type skeletons.

Usage:
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.distill \
      [--root DATA_DIR] [--annotation-model gpt-5.6-terra]

Reads ``findings.json`` (``analyze.py``) and the private records/annotations,
and writes ``candidates.json`` (committed): one candidate per *supported*
finding in the ``top_vs_accepted`` contrast that a writing rule can act on,
in the pipeline's data-rule format (``lib/longform/documents/rules.py``,
``style_rules.json``), plus per-paper-type abstract / introduction /
contribution skeletons. A candidate becomes a shipped rule only after the
per-rule blind A/B (``validate.py rules``) shows it helps on held-out drafts;
``validate.py rules --write`` writes ``style_rules.json``.

Metric bounds follow one convention, defined here once: a lint bound sits at
the top-group quartile on the unfavourable side of the finding (25th
percentile of award/high-citation papers for a "more is better" finding, 75th
for "less is better"), so a rule never fails more than a quarter of the papers
that justify it. A candidate whose bound would be vacuous (no ordinary paper
fails it) is dropped as unenforceable.

Exemplars are short sentences (<= 40 words) from training-split top papers;
the source paper id travels with each quote in ``candidates.json``.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluations.paper_style_corpus.build_corpus import DEFAULT_ROOT, HERE  # noqa: E402
from lib.longform.documents import style_metrics as sm  # noqa: E402

ORIGIN = ('paper-style corpus analysis (award/high-citation vs matched controls, '
          'evaluations/paper_style_corpus), 2026-11-18')
TYPES = ('method', 'analysis_mechanism', 'benchmark_dataset', 'empirical_study', 'systems')
MAX_QUOTE_WORDS = 40

# Candidate rule templates keyed by the finding (feature) that justifies them.
# ``metric``: (style_metrics name, pipeline section id, direction) for lint;
# ``checklist``: True for reviewer items; ``testable``: whether the effect is
# visible in an abstract + introduction draft (the A/B unit).
TEMPLATES = {
    # 28% of top papers have no number in the abstract, so a hard lint would
    # fail more than a quarter of the papers that justify it: checklist only.
    'abs_has_number': {
        'id': 'paper.style.abstract-number', 'kind': 'checklist',
        'text': 'When the main result is quantitative, the abstract states it '
                'as a concrete number taken from the evidence.',
        'quote_from': ('abstract', lambda s: bool(re.search(r'\d', s)))},
    'abs_nominal_per_k': {
        'id': 'paper.style.abstract-verbs', 'kind': 'lint',
        'metric': ('nominal_per_k', 'abstract', 'less'),
        'text': 'Write the abstract with verbs, not nominalisations: "we measure '
                'how X changes" rather than "a measurement of the change of X".',
        'quote_from': ('abstract', lambda s: len(re.findall(r'\b(we|our)\b', s, re.I)) > 0)},
    'intro_nominal_per_k': {
        'id': 'paper.style.intro-verbs', 'kind': 'lint',
        'metric': ('nominal_per_k', 'introduction', 'less'),
        'text': 'Write the introduction with agents and verbs ("we show", "the '
                'model forgets"), not chains of nominalisations.',
        'quote_from': ('introduction', lambda s: bool(re.search(r'\bwe (show|find|demonstrate)', s, re.I)))},
    'intro_first_person_findings': {
        'id': 'paper.style.intro-findings-first-person', 'kind': 'checklist',
        'text': 'The introduction states the main findings explicitly in first '
                'person ("we show/find/demonstrate that ...").',
        'quote_from': ('introduction', lambda s: bool(re.search(
            r'\bwe (show|find|demonstrate|prove|observe|reveal|establish|identify)\b', s, re.I)))},
    'intro_number_per_k': {
        'id': 'paper.style.intro-quantified', 'kind': 'checklist',
        'text': 'The introduction quantifies the main result (the same headline '
                'number as the abstract), not only "significant improvements".',
        'quote_from': ('introduction', lambda s: bool(re.search(r'\d+(\.\d+)?\s*(%|x|×|points?)', s)))},
    'intro_contrib_list': {
        'id': 'paper.style.contributions-prose', 'kind': 'checklist',
        'direction_required': 'less',
        'text': 'Do not pad the introduction with a generic bulleted '
                'contribution list; state the contributions in prose, and use a '
                'list only for genuinely distinct contributions.'},
    'has_limitations': {
        'id': 'paper.style.limitations-section', 'kind': 'checklist', 'testable': False,
        'text': 'A dedicated Limitations section names specific limitations of '
                'this work (settings not tested, assumptions), not generic caveats.'},
    'related_work_after_method': {
        'id': 'paper.style.related-work-late', 'kind': 'checklist', 'testable': False,
        'text': 'Related work comes after the method and results unless the '
                'method cannot be understood without it.'},
    'caption_takeaway': {
        'id': 'paper.style.caption-takeaway', 'kind': 'checklist', 'testable': False,
        'text': 'Every figure and table caption states its takeaway in a full '
                'sentence, not only what is plotted.'},
    'page1_visual': {
        'id': 'paper.style.teaser-figure', 'kind': 'checklist', 'testable': False,
        'text': 'Page 1 carries a teaser figure that shows the main idea or '
                'result.'},
    'teaser_page1': {
        'id': 'paper.style.teaser-figure', 'kind': 'checklist', 'testable': False,
        'text': 'Page 1 carries a teaser figure that shows the main idea or '
                'result.'},
    # Annotation-derived (blind model annotation, annotate.py).
    'ann_insight_stated': {
        'id': 'paper.style.one-sentence-insight', 'kind': 'checklist',
        'text': 'The introduction states the core insight in one sentence a '
                'reader could repeat.', 'quote_key': 'insight_quote'},
    'ann_abs_has_gap': {
        'id': 'paper.style.abstract-gap', 'kind': 'checklist',
        'text': 'The abstract names the specific gap in current knowledge or '
                'methods before the approach.',
        'quote_move': 'gap'},
    'ann_abs_has_implication': {
        'id': 'paper.style.abstract-implication', 'kind': 'checklist',
        'text': 'The abstract ends with what the result implies for the field, '
                'not only what was done.',
        'quote_move': 'implication'},
    'ann_intro_first_specific': {
        'id': 'paper.style.intro-specific-opening', 'kind': 'checklist',
        'text': 'The introduction opens with the specific problem or finding, '
                'not a broad statement about the field\'s importance.'},
    'ann_intro_quantified': {
        'id': 'paper.style.intro-quantified', 'kind': 'checklist',
        'text': 'The introduction quantifies the main result (the same headline '
                'number as the abstract), not only "significant improvements".'},
    'ann_rw_contrast': {
        'id': 'paper.style.related-work-contrast', 'kind': 'checklist', 'testable': False,
        'text': 'Related work is organised by theme and each theme ends with how '
                'this work differs.'},
    'ann_rw_states_difference': {
        'id': 'paper.style.related-work-contrast', 'kind': 'checklist', 'testable': False,
        'text': 'Related work is organised by theme and each theme ends with how '
                'this work differs.'},
    'ann_lim_dedicated_specific': {
        'id': 'paper.style.limitations-section', 'kind': 'checklist', 'testable': False,
        'text': 'A dedicated Limitations section names specific limitations of '
                'this work (settings not tested, assumptions), not generic caveats.',
        'quote_key': 'limitations_quote'},
    'ann_claims_scoped': {
        'id': 'paper.style.scoped-claims', 'kind': 'checklist',
        'text': 'Main claims are stated firmly and scoped to the settings tested '
                '("on the three benchmarks we study, X ..."), never universal.'},
    'ann_contrib_bulleted': {
        'id': 'paper.style.contributions-prose', 'kind': 'checklist',
        'direction_required': 'less',
        'text': 'Do not pad the introduction with a generic bulleted '
                'contribution list; state the contributions in prose, and use a '
                'list only for genuinely distinct contributions.'},
}


def quantile(values: list[float], q: float) -> float:
    values = sorted(values)
    position = q * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def short(sentence: str) -> str | None:
    sentence = ' '.join(sentence.split())
    if 8 <= len(sentence.split()) <= MAX_QUOTE_WORDS and not re.search(r'\[\d|\(\w+ et al', sentence):
        return sentence
    return None


def section_text(record: dict, role: str) -> str:
    if role == 'abstract':
        return record['abstract']
    return ' '.join(s['text'] for s in record['sections']
                    if s.get('body', True) and s['role'] == role)


def evidence_string(row: dict) -> str:
    t, te = row['train'], row.get('test') or {}
    return (f"findings.json top_vs_accepted/{row['feature']}: top {t['mean_a']:.3g} vs "
            f"accepted {t['mean_b']:.3g}, paired diff {t['mean_diff']:+.3g} "
            f"[95% CI {t['ci95'][0]:+.3g}, {t['ci95'][1]:+.3g}], dz {t['dz']:+.2f}, "
            f"q {t['q']:.3f}, n={t['n']} train pairs; held-out diff {te.get('mean_diff', 0):+.3g} "
            f"n={te.get('n', 0)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--annotation-model', default='gpt-5.6-terra')
    args = parser.parse_args(argv)
    root = Path(args.root)
    findings = json.loads((HERE / 'findings.json').read_text())
    corpus = {r['id']: r for r in json.loads((root / 'corpus.json').read_text())}
    feats = {}
    for line in (root / 'features.jsonl').read_text().splitlines():
        row = json.loads(line)
        feats[row['id']] = row
    from evaluations.paper_style_corpus.annotate import PROMPT_DIGEST
    annotations = {}
    for path in (root / 'annotations').glob(f'*.{PROMPT_DIGEST}.{args.annotation_model}.json'):
        data = json.loads(path.read_text())
        annotations[data['id']] = data
    analyzed = set(findings.get('analyzed_ids') or feats)
    top_train = [i for i, r in corpus.items() if r['group'] in ('award', 'highcite')
                 and r['split'] == 'train' and i in analyzed and r['stratum'] == 'main']
    ctrl_train = [i for i, r in corpus.items() if r['group'] == 'control_accepted'
                  and r['split'] == 'train' and i in analyzed]

    def record(pid):
        path = root / 'records' / f'{pid}.json'
        return json.loads(path.read_text()) if path.is_file() else None

    # The award contrast is matched by venue-year; the high-citation contrast
    # is not (its controls are same-year ICLR/NeurIPS papers), so a rule also
    # needs the same direction among award papers alone.
    award_rows = {r['feature']: r for r in findings['contrasts']['award_vs_accepted']}
    candidates = {}
    for row in findings['contrasts']['top_vs_accepted']:
        template = TEMPLATES.get(row['feature'])
        if not template or not row.get('supported'):
            continue
        award = (award_rows.get(row['feature']) or {}).get('train')
        if not award or award['mean_diff'] * row['train']['mean_diff'] <= 0:
            continue
        direction = 'more' if row['train']['mean_diff'] > 0 else 'less'
        if template.get('direction_required', template.get('metric', (0, 0, direction))[2]) != direction:
            continue
        rule_id = template['id']
        entry = candidates.setdefault(rule_id, {
            'id': rule_id, 'kind': template['kind'], 'text': template['text'],
            'origin': ORIGIN, 'evidence': [], 'exemplars': [], 'exemplar_sources': [],
            'testable_on_abstract_intro': template.get('testable', True)})
        entry['evidence'].append(evidence_string(row))
        if template['kind'] == 'lint':
            name, section, _ = template['metric']
            values = []
            for pid in top_train:
                rec = record(pid)
                if rec:
                    value = sm.METRICS[name](section_text(rec, section))
                    if value == value:
                        values.append(value)
            bound = quantile(values, 0.25 if direction == 'more' else 0.75)
            control_fail = sum(1 for pid in ctrl_train if (rec := record(pid)) and (
                (v := sm.METRICS[name](section_text(rec, section))) == v) and (
                v < bound if direction == 'more' else v > bound))
            if control_fail == 0:
                candidates.pop(rule_id)
                continue
            entry['metric'] = {'name': name, 'section': section,
                               'op': '>=' if direction == 'more' else '<=',
                               'value': round(bound, 2),
                               'why': (f'{"25th" if direction == "more" else "75th"} '
                                       f'percentile of award/high-citation papers, '
                                       f'n={len(values)}')}
        # Exemplars.
        quotes = []
        if 'quote_from' in template:
            role, keep = template['quote_from']
            for pid in sorted(top_train):
                rec = record(pid)
                if not rec:
                    continue
                for sentence in sm.sentences(section_text(rec, role)):
                    q = short(sentence)
                    if q and keep(q):
                        quotes.append((pid, q))
                        break
        elif 'quote_key' in template:
            for pid in sorted(top_train):
                q = short((annotations.get(pid) or {}).get(template['quote_key']) or '')
                if q:
                    quotes.append((pid, q))
        elif 'quote_move' in template:
            for pid in sorted(top_train):
                for label, sentence in (annotations.get(pid) or {}).get('abstract_sentence_roles') or []:
                    q = short(sentence) if label == template['quote_move'] else None
                    if q:
                        quotes.append((pid, q))
                        break
        # Prefer permissively licensed sources, then a deterministic spread.
        quotes.sort(key=lambda pq: ('creativecommons' not in corpus[pq[0]].get('license', '')
                                    and 'ACL Anthology' not in corpus[pq[0]].get('license', ''),
                                    pq[0]))
        for pid, quote in quotes[:3]:
            if quote not in entry['exemplars']:
                entry['exemplars'].append(quote)
                entry['exemplar_sources'].append(f"{pid} {corpus[pid]['title'][:80]}")
    skeletons = build_skeletons(corpus, annotations, top_train, ctrl_train)
    out = {'generated': findings['generated'], 'bound_convention': (
        'lint bound = top-group 25th percentile (more-is-better) or 75th percentile '
        '(less-is-better), training split'), 'candidates': list(candidates.values()),
        'skeletons': skeletons}
    (HERE / 'candidates.json').write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for c in candidates.values():
        print(c['id'], c['kind'], c.get('metric'), len(c['exemplars']), 'testable' if c['testable_on_abstract_intro'] else 'structural')
    for t, s in skeletons.items():
        print(t, s['n_top'], s.get('abstract_moves'), s.get('intro_roles'), s.get('status', ''))
    return 0


def _modal_sequence(sequences: list[list[str]], canonical: tuple[str, ...]) -> list[dict]:
    """Moves present in at least half the sequences, in median position order."""
    n = len(sequences)
    stats = []
    for move in canonical:
        positions = [seq.index(move) / max(1, len(seq) - 1) for seq in sequences if move in seq]
        if positions:
            stats.append({'move': move, 'share': round(len(positions) / n, 2),
                          'median_position': round(statistics.median(positions), 2)})
    return sorted((s for s in stats if s['share'] >= 0.5), key=lambda s: s['median_position'])


def build_skeletons(corpus, annotations, top_train, ctrl_train) -> dict:
    from evaluations.paper_style_corpus.annotate import INTRO_ROLES, MOVES
    out = {}
    for paper_type in TYPES:
        top = [annotations[p] for p in top_train if (annotations.get(p) or {}).get('paper_type') == paper_type]
        ctrl = [annotations[p] for p in ctrl_train if (annotations.get(p) or {}).get('paper_type') == paper_type]
        if len(top) < 8:
            out[paper_type] = {'n_top': len(top), 'n_control': len(ctrl),
                               'status': 'insufficient top papers of this type (< 8)'}
            continue
        abstract = _modal_sequence([a.get('abstract_moves') or [] for a in top], MOVES)
        intro = _modal_sequence([a.get('intro_roles') or [] for a in top], INTRO_ROLES)
        ctrl_abstract = {s['move']: s['share'] for s in _modal_sequence_all(
            [a.get('abstract_moves') or [] for a in ctrl], MOVES)}
        ctrl_intro = {s['move']: s['share'] for s in _modal_sequence_all(
            [a.get('intro_roles') or [] for a in ctrl], INTRO_ROLES)}
        for s in abstract:
            s['control_share'] = ctrl_abstract.get(s['move'], 0.0)
        for s in intro:
            s['control_share'] = ctrl_intro.get(s['move'], 0.0)
        forms = collections.Counter(a.get('contribution_form') for a in top)
        ctrl_forms = collections.Counter(a.get('contribution_form') for a in ctrl)
        counts = [a['contribution_count'] for a in top if isinstance(a.get('contribution_count'), int) and a['contribution_count'] > 0]
        exemplars = {}
        for s in abstract:
            for p in sorted(top_train):
                a = annotations.get(p) or {}
                if a.get('paper_type') != paper_type:
                    continue
                hit = next((short(sent) for label, sent in a.get('abstract_sentence_roles') or []
                            if label == s['move'] and short(sent)
                            and (s['move'] != 'result' or re.search(r'\d', sent))), None)
                if hit:
                    exemplars[s['move']] = {'quote': hit, 'source': f"{p} {corpus[p]['title'][:80]}"}
                    break
        contrib_examples = []
        for p in sorted(top_train):
            a = annotations.get(p) or {}
            if a.get('paper_type') == paper_type and a.get('contribution_items'):
                item = short(a['contribution_items'][0])
                if item:
                    contrib_examples.append({'quote': item, 'source': f"{p} {corpus[p]['title'][:80]}"})
            if len(contrib_examples) == 2:
                break
        out[paper_type] = {
            'n_top': len(top), 'n_control': len(ctrl),
            'abstract_moves': [s['move'] for s in abstract], 'abstract_move_stats': abstract,
            'intro_roles': [s['move'] for s in intro], 'intro_role_stats': intro,
            'contribution_form_top': dict(forms), 'contribution_form_control': dict(ctrl_forms),
            'contribution_count_median': statistics.median(counts) if counts else 0,
            'abstract_exemplars': exemplars, 'contribution_exemplars': contrib_examples,
        }
    return out


TYPE_DESCRIPTIONS = {
    'method': 'proposes a new method, model or algorithm',
    'analysis_mechanism': 'explains how or why something works (mechanism, theory-backed analysis)',
    'benchmark_dataset': 'contributes a benchmark, dataset or evaluation resource',
    'empirical_study': 'a systematic empirical study of existing methods or models',
}


def style_outlines(skeletons: dict) -> dict:
    """Per-type abstract/introduction outlines in the registry's data format.

    Only types with enough training-split top papers (``build_skeletons``)
    become outlines; the move order is the top papers' modal order.
    """
    out = {}
    for paper_type, s in skeletons.items():
        if s.get('status') or paper_type not in TYPE_DESCRIPTIONS:
            continue
        forms = s['contribution_form_top']
        listed = forms.get('bulleted_list', 0) / max(1, sum(forms.values()))
        ctrl = s['contribution_form_control']
        ctrl_listed = ctrl.get('bulleted_list', 0) / max(1, sum(ctrl.values()))
        stats = lambda rows: ', '.join(f"{r['move']} {r['share']:.0%} (ctrl {r['control_share']:.0%})" for r in rows)  # noqa: E731
        out[paper_type] = {
            'applies_to': TYPE_DESCRIPTIONS[paper_type],
            'abstract': s['abstract_moves'],
            'introduction': s['intro_roles'],
            'contributions': ('state the contributions in prose or one numbered sentence; '
                              'a bulleted list only for genuinely distinct contributions'),
            'exemplars': {move: row['quote'] for move, row in s['abstract_exemplars'].items()},
            'evidence': (f"candidates.json skeletons/{paper_type}: n top={s['n_top']}, "
                         f"control={s['n_control']} (training split); abstract moves "
                         f"{stats(s['abstract_move_stats'])}; intro roles "
                         f"{stats(s['intro_role_stats'])}; bulleted contribution list "
                         f"{listed:.0%} of top vs {ctrl_listed:.0%} of controls"),
        }
    return out


def _modal_sequence_all(sequences, canonical):
    n = max(1, len(sequences))
    return [{'move': m, 'share': round(sum(m in s for s in sequences) / n, 2)} for m in canonical]


if __name__ == '__main__':
    sys.exit(main())
