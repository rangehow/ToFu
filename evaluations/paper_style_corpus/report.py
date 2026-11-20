"""Render the committed evidence (findings, candidates, validation) as Markdown.

Usage:
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.report

Rewrites the block between ``<!-- evidence:begin -->`` and
``<!-- evidence:end -->`` in ``docs/PAPER_STYLE_EVIDENCE.md`` from
``findings.json``, ``candidates.json``, ``validation_rule_ab.json``,
``validation_separation.json``, ``validation_drafts_selection.json`` and
``validation_drafts.json``, so the document's
numbers always come from the committed data. Standard library only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC = HERE.parents[1] / 'docs' / 'PAPER_STYLE_EVIDENCE.md'
BEGIN, END = '<!-- evidence:begin -->', '<!-- evidence:end -->'

LABELS = {
    'abs_has_number': 'abstract states a number (share)',
    'abs_number_per_k': 'numbers per 1k abstract words',
    'abs_nominal_per_k': 'nominalisations per 1k abstract words',
    'abs_we_per_k': 'first person per 1k abstract words',
    'abs_hedge_per_k': 'hedges per 1k abstract words',
    'abs_sent_len_mean': 'abstract words per sentence',
    'abs_n_words': 'abstract words',
    'abs_question_per_k': 'questions per 1k abstract words',
    'intro_nominal_per_k': 'nominalisations per 1k intro words',
    'intro_first_person_findings': '"we show/find/..." per 1k intro words',
    'intro_number_per_k': 'numbers per 1k intro words',
    'intro_contrib_list': 'intro has a contribution list (regex)',
    'intro_hedge_per_k': 'hedges per 1k intro words',
    'intro_we_per_k': 'first person per 1k intro words',
    'intro_sent_len_mean': 'intro words per sentence',
    'body_hedge_per_k': 'hedges per 1k body words',
    'body_nominal_per_k': 'nominalisations per 1k body words',
    'body_passive_per_sent': 'passives per body sentence',
    'body_promo_per_k': 'promotional words per 1k body words',
    'body_sent_len_mean': 'body words per sentence',
    'title_words': 'title words', 'title_colon': 'title has a colon',
    'title_name_colon': 'title is "Name: description"', 'title_question': 'title is a question',
    'title_claim_verb': 'title states a claim (verb)',
    'teaser_page1': 'Figure 1 on page 1', 'page1_visual': 'any visual on page 1',
    'caption_words_mean': 'caption words', 'caption_takeaway': 'captions with >= 2 sentences (share)',
    'n_figures_body': 'figures in body', 'n_tables_body': 'tables in body',
    'has_limitations': 'dedicated Limitations section', 'share_limitations': 'Limitations share of body',
    'related_work_after_method': 'related work placed after the method',
    'related_work_position': 'related-work position (0 = start of body)',
    'share_introduction': 'introduction share of body', 'share_experiments': 'experiments share of body',
    'share_method': 'method share of body', 'share_related_work': 'related-work share of body',
    'pages': 'pages (incl. appendix)', 'body_words': 'body words', 'n_sections_top': 'top-level sections',
    'ann_abs_has_gap': 'abstract states a gap (annotated)',
    'ann_abs_has_problem': 'abstract states a problem (annotated)',
    'ann_abs_has_implication': 'abstract states an implication (annotated)',
    'ann_abs_monotone_order': 'abstract moves in canonical order (annotated)',
    'ann_insight_stated': 'intro states a one-sentence insight (annotated)',
    'ann_intro_quantified': 'intro quantifies the main result (annotated)',
    'ann_intro_first_broad': 'intro opens broadly (annotated)',
    'ann_contrib_bulleted': 'bulleted contribution list (annotated)',
    'ann_contrib_any_list': 'any contribution list (annotated)',
    'ann_contrib_count': 'number of listed contributions (annotated)',
    'ann_claim_strength': 'claim strength 1-5 (annotated)',
    'ann_claims_scoped': 'claims scoped to tested settings (annotated)',
    'ann_rw_contrast': 'related work thematic with contrast (annotated)',
    'ann_lim_dedicated_specific': 'specific dedicated limitations (annotated)',
    'ann_problem_understanding': 'framed as an understanding gap (annotated)',
    'ann_problem_capability': 'framed as a capability gap (annotated)',
}
# Rows shown for every analysis dimension the acceptance criteria name, whether
# or not they reached significance (absence of an effect is a finding too).
DIMENSIONS = [
    ('Title patterns', ['title_words', 'title_colon', 'title_name_colon', 'title_question', 'title_claim_verb']),
    ('Abstract move structure', ['ann_abs_has_problem', 'ann_abs_has_gap', 'ann_abs_has_implication',
                                 'ann_abs_monotone_order', 'abs_has_number', 'abs_number_per_k', 'abs_n_words']),
    ('Introduction roles and contributions', ['ann_insight_stated', 'ann_intro_quantified', 'ann_intro_first_broad',
                                              'intro_first_person_findings', 'ann_contrib_any_list',
                                              'ann_contrib_bulleted', 'ann_contrib_count']),
    ('Claim strength and hedging', ['ann_claim_strength', 'ann_claims_scoped', 'abs_hedge_per_k',
                                    'intro_hedge_per_k', 'body_hedge_per_k', 'body_promo_per_k']),
    ('Sentence length, voice, nominalisation', ['abs_sent_len_mean', 'body_sent_len_mean', 'abs_we_per_k',
                                                'intro_we_per_k', 'body_passive_per_sent', 'abs_nominal_per_k',
                                                'intro_nominal_per_k', 'body_nominal_per_k']),
    ('Teaser figure and figure/table design', ['teaser_page1', 'page1_visual', 'n_figures_body', 'n_tables_body',
                                               'caption_words_mean', 'caption_takeaway']),
    ('Related-work positioning', ['related_work_after_method', 'related_work_position', 'ann_rw_contrast',
                                  'share_related_work']),
    ('Section structure and length ratios', ['n_sections_top', 'share_introduction', 'share_method',
                                             'share_experiments', 'body_words', 'pages']),
    ('Limitations framing', ['has_limitations', 'share_limitations', 'ann_lim_dedicated_specific']),
    ('Problem framing', ['ann_problem_understanding', 'ann_problem_capability']),
]


def fmt(x: float) -> str:
    return f'{x:.3g}' if abs(x) < 1000 else f'{x:.0f}'


def row_md(contrasts: dict, feature: str) -> str:
    cells = [LABELS.get(feature, feature)]
    for name in ('top_vs_accepted', 'award_vs_accepted', 'accepted_vs_rejected'):
        row = next((r for r in contrasts[name] if r['feature'] == feature), None)
        t = (row or {}).get('train')
        if not t:
            cells.append('n/a')
            continue
        te = row.get('test') or {}
        mark = ' **✓**' if row.get('supported') else ''
        cells.append(f"{fmt(t['mean_a'])} vs {fmt(t['mean_b'])}; Δ {t['mean_diff']:+.3g} "
                     f"[{t['ci95'][0]:+.3g}, {t['ci95'][1]:+.3g}], dz {t['dz']:+.2f}, q {t['q']:.2f}, "
                     f"n={t['n']}; test Δ {te.get('mean_diff', 0):+.3g} (n={te.get('n', 0)}){mark}")
    return '| ' + ' | '.join(cells) + ' |'


def separation_md(separation: dict | None) -> list[str]:
    if not separation or 'runs' not in separation:
        return ['- Separation: not run.']
    out = ['Separation (`validate.py separation`): AUC of the judge score for award/high-citation '
           'papers vs controls, checklist-free vs with checklist. CIs: 2000-draw bootstrap that '
           'resamples whole venue-years (keeps the matching); "within venue-year" counts only '
           'positive/control pairs from the same venue-year; permutation p swaps the two arms within papers.', '',
           '| split / judge / checklist | repeats | n pos / ctrl | AUC free | AUC checklist | Δ [95% CI] | perm. p | within venue-year free → checklist, Δ CI |',
           '|---|---|---|---|---|---|---|---|']
    for key, run in separation['runs'].items():
        for name in ('vs_all_controls', 'vs_accepted_controls'):
            e = run[name]
            b = e['bootstrap_by_venue_year']
            out.append(f"| {key} ({name.replace('_', ' ')}) | {run['repeats']} | {e['n_pos']} / {e['n_neg']} | "
                       f"{e['auc_free']:.3f} {b['pooled_free']} | {e['auc_checklist']:.3f} {b['pooled_checklist']} | "
                       f"{e['auc_diff']:+.3f} {b['pooled_diff']} | {e['paired_permutation_p']} | "
                       f"{e['stratified_auc_free']:.3f} → {e['stratified_auc_checklist']:.3f}, {b['stratified_diff']} |")
    return out


def drafts_md(drafts: dict | None, title: str) -> list[str]:
    if not drafts:
        return [f'- {title}: not run.']
    out = [f"{title} (`validate.py drafts`; writer `{drafts['writer_model']}`, judge `{drafts['judge_model']}` "
           f"via {drafts['judge']}; {len(drafts['units'])} units: "
           f"{', '.join(u['id'] for u in drafts['units'][:2])} + {len(drafts['units']) - 2} held-out "
           f"accepted-ordinary papers not used for rule selection). Each unit: candidate preferred in both "
           f"orders / split / baseline preferred in both orders. Units whose revision added a number not in "
           f"the draft, per arm: {drafts['invented_numbers']}.", '',
           '| comparison / judge | candidate | split | baseline | sign-test p | mean score [95% CI] | rubric margin [95% CI] |',
           '|---|---|---|---|---|---|---|']
    for key, s in drafts['comparisons'].items():
        if s:
            out.append(f"| {key} | {s['candidate_preferred']} | {s['no_preference']} | {s['baseline_preferred']} | "
                       f"{s['sign_test_p']} | {s['mean_score']:+.2f} {s['mean_score_ci95']} | "
                       f"{s['mean_rubric_margin']:+.1f} {s['rubric_margin_ci95']} |")
    if 'harmful' in drafts:
        out += ['', f"Harm criterion: {drafts['harm_criterion']}. Removed: {', '.join(drafts['harmful']) or 'none'}."]
    return out


def main() -> int:
    findings = json.loads((HERE / 'findings.json').read_text())
    candidates = json.loads((HERE / 'candidates.json').read_text())
    load = lambda name: json.loads((HERE / name).read_text()) if (HERE / name).is_file() else None  # noqa: E731
    rule_ab, separation = load('validation_rule_ab.json'), load('validation_separation.json')
    harmful = set((load('validation_drafts_selection.json') or {}).get('harmful') or ())
    c = findings['contrasts']
    out = [f"Generated {findings['generated']} from `evaluations/paper_style_corpus/` "
           f"(annotation model `{findings['annotation_model']}`). Papers analysed per group: "
           f"{findings['n_analyzed_by_group']}.", '',
           'Cells: mean of first group vs second group; paired mean difference Δ with seeded '
           'bootstrap 95% CI; standardized paired effect dz; Benjamini–Hochberg q; n training '
           'pairs; held-out Δ. **✓** = supported (train CI excludes 0, q < 0.10, same sign on '
           'held-out). Columns: award+high-citation vs accepted-ordinary (same venue-year), '
           'award only vs accepted-ordinary, accepted-ordinary vs rejected.', '']
    for title, feats in DIMENSIONS:
        out += [f'### {title}', '', '| measure | top vs accepted | award vs accepted | accepted vs rejected |',
                '|---|---|---|---|']
        out += [row_md(c, f) for f in feats if any(r['feature'] == f for r in c['top_vs_accepted'])]
        out.append('')
    out += ['### Candidate rules and exemplars', '']
    for cand in candidates['candidates']:
        verdict = ((rule_ab or {}).get('rules') or {}).get(cand['id'])
        status = ('structural (not A/B-testable on abstract+introduction); shipped on corpus evidence'
                  if not cand.get('testable_on_abstract_intro') else
                  'A/B pending' if not verdict else
                  f"A/B: rule preferred on {verdict['rule_preferred_both_orders']}, baseline on "
                  f"{verdict['baseline_preferred_both_orders']} of {verdict['n_units']} held-out units → "
                  f"{'**shipped**' if verdict['keep'] else '**not shipped**'}")
        if cand['id'] in harmful:
            status += ' → **removed** (draft ablation: removal preferred, see Validation)'
        out += [f"- **`{cand['id']}`** ({cand['kind']}{', metric ' + json.dumps({k: v for k, v in cand['metric'].items() if k != 'why'}) if cand.get('metric') else ''}): "
                f"{cand['text']} — {status}"]
        out += [f'  - evidence: {e}' for e in cand['evidence']]
        out += [f'  - exemplar: “{q}” ({s})' for q, s in zip(cand['exemplars'], cand['exemplar_sources'])]
    out += ['', '### Per-type skeletons (training-split award/high-citation papers)', '',
            ('Shipped as writer outlines: no (draft ablation: removal preferred).' if 'outlines' in harmful
             else 'Shipped as writer outlines (`style_rules.json` "outlines").'), '']
    for paper_type, s in candidates['skeletons'].items():
        if 'status' in s:
            out.append(f"- **{paper_type}**: {s['status']} (n={s['n_top']})")
            continue
        moves = ' → '.join(f"{m['move']} ({m['share']:.0%} vs ctrl {m['control_share']:.0%})"
                           for m in s['abstract_move_stats'])
        roles = ' → '.join(f"{m['move']} ({m['share']:.0%} vs ctrl {m['control_share']:.0%})"
                           for m in s['intro_role_stats'])
        out += [f"- **{paper_type}** (n top={s['n_top']}, control={s['n_control']}): abstract {moves}; "
                f"intro {roles}; contribution form top {s['contribution_form_top']} vs control "
                f"{s['contribution_form_control']}"]
        out += [f"  - {move}: “{e['quote']}” ({e['source']})" for move, e in s['abstract_exemplars'].items()]
    out += ['', '### Validation', '']
    out += separation_md(separation) + [''] + drafts_md(load('validation_drafts_selection.json'),
                                                        'Draft A/B and per-rule ablation (all 9 rules + outlines)')
    out += [''] + drafts_md(load('validation_drafts.json'), 'Draft A/B of the shipped configuration')
    text = DOC.read_text()
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    DOC.write_text(text[:start] + '\n' + '\n'.join(out) + '\n' + text[end:])
    return 0


if __name__ == '__main__':
    sys.exit(main())
