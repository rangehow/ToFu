"""Validate the distilled style checklist and rules.

Usage:
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.validate \
      separation [--judge-model gpt-5.5] [--in-flight 8]
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.validate \
      ab --draft NAME=PATH.tex [...] [--writer-model M] [--judge-model M]

``separation`` (held-out split only; rules were derived from the train split):
two judges with the same model read the same anonymised paper view (abstract,
introduction, section headings, related-work opening, limitations and
conclusion; no title, authors, venue or award) and give a 1-10 score for
"exceptional (award-level / field-shaping) vs ordinary". The checklist judge
additionally receives the distilled checklist items (``style_rules.json``,
kind=checklist, with their text) and must mark each before scoring. AUC of
the score for positives (award + high-citation) against controls (accepted
and rejected), with a seeded bootstrap 95% CI over papers and the bootstrap
CI of the AUC difference. A deterministic lint-only score (share of metric
lint rules a paper passes) is reported beside them.

``ab``: for each draft, the abstract + introduction are revised twice by the
same writer model from the same draft: arm A with the pipeline's existing
paper rules only (``rules.RULES`` paper.* when importable, else their text
recorded in this file's fallback), arm B with those rules plus the style
rules and exemplars. The pipeline's one style-blind judge
(``pipeline._judge_pair``, a different model) compares A and B in both
presentation orders (``blind_pairwise``); B is preferred only if it wins both
orders. Every number and citation in a revision must trace to the draft (the
pipeline's provenance gate, ``lib.longform.documents.provenance``);
violations are reported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluations.paper_style_corpus.build_corpus import DEFAULT_ROOT, HERE  # noqa: E402

STYLE_RULES = ROOT / 'lib' / 'longform' / 'documents' / 'style_rules.json'
BOOTSTRAP = 2000
SEED = 20261118


def load_style_rules() -> list[dict]:
    return json.loads(STYLE_RULES.read_text())['rules']


# ── separation ───────────────────────────────────────────────────────────

def paper_view(record: dict) -> str:
    from evaluations.paper_style_corpus.annotate import _words
    headings = [s['heading'] for s in record['sections']
                if s.get('body', True) and s['role'] not in ('front', 'back')]
    by_role = {}
    for sec in record['sections']:
        if sec.get('body', True):
            by_role.setdefault(sec['role'], []).append(sec['text'])
    join = lambda role: ' '.join(by_role.get(role, []))  # noqa: E731
    return '\n\n'.join([
        '[SECTION HEADINGS]\n' + '\n'.join(headings),
        f'[ABSTRACT]\n{_words(record["abstract"], 400)}',
        f'[INTRODUCTION]\n{_words(join("introduction"), 1500)}',
        f'[RELATED WORK (opening)]\n{_words(join("related_work"), 400) or "(none)"}',
        f'[LIMITATIONS]\n{_words(join("limitations"), 250) or "(none)"}',
        f'[CONCLUSION]\n{_words(join("conclusion"), 350) or "(none)"}',
        f'[FIRST PAGE] teaser figure on page 1: '
        f'{"yes" if record.get("page1_has_fig1_caption") else "no"}'])


BASE_TASK = (
    'You are an experienced area chair at a top machine-learning venue. You '
    'read an anonymised excerpt of a paper (no title, authors or venue). '
    'Judge only the writing and presentation as evidence of quality: how '
    'likely is this paper to be an exceptional one (best-paper level or '
    'field-shaping, top 1-2%) rather than an ordinary submission? ')
SCORE_SPEC = ('Give "score" from 1 (clearly ordinary) to 10 (clearly '
              'exceptional). Do not try to identify the paper.')


def judge_messages(view: str, checklist: list[dict] | None) -> list[dict]:
    if checklist:
        items = '\n'.join(f'- {r["id"]}: {r["text"]}' for r in checklist)
        system = (BASE_TASK + 'First mark each checklist item pass or fail '
                  '(these properties distinguish exceptional papers from '
                  f'ordinary ones in a measured corpus):\n{items}\n' + SCORE_SPEC +
                  ' Reply with ONLY JSON: {"checklist":{"<id>":true},"score":0}')
    else:
        system = BASE_TASK + SCORE_SPEC + ' Reply with ONLY JSON: {"score":0}'
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': view}]


def auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC (ties count one half), by ranks."""
    if not pos or not neg:
        return float('nan')
    values = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(values):
        j = i
        while j < len(values) and values[j][0] == values[i][0]:
            j += 1
        mid = (i + j + 1) / 2
        rank_sum += mid * sum(flag for _v, flag in values[i:j])
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def stratified_auc(rows: list[dict], key: str) -> float:
    """AUC over positive/control pairs from the same venue-year only."""
    wins = pairs = 0.0
    for cluster in _clusters(rows).values():
        pos = [r[key] for r in cluster if r['positive']]
        neg = [r[key] for r in cluster if not r['positive']]
        if pos and neg:
            wins += auc(pos, neg) * len(pos) * len(neg)
            pairs += len(pos) * len(neg)
    return wins / pairs if pairs else float('nan')


def _clusters(rows: list[dict]) -> dict:
    out = {}
    for row in rows:
        out.setdefault(row['venue_year'], []).append(row)
    return out


def _pooled_auc(rows, key):
    return auc([r[key] for r in rows if r['positive']], [r[key] for r in rows if not r['positive']])


def _ci(values: list[float]) -> list[float]:
    values = sorted(v for v in values if v == v)
    return [round(values[int(0.025 * len(values))], 4),
            round(values[min(len(values) - 1, int(0.975 * len(values)))], 4)]


def cluster_bootstrap(rows: list[dict], keys: tuple[str, str], rng: random.Random) -> dict:
    """Venue-year cluster bootstrap of pooled and within-venue-year AUCs.

    Positives and their matched controls share a venue-year, so resampling
    whole venue-years keeps the matching and the dependence between papers of
    one venue-year. Both arms are scored on every resample (paired).
    """
    clusters = list(_clusters(rows).values())
    stats = {f'{kind}_{k}': [] for kind in ('pooled', 'stratified') for k in keys}
    stats['pooled_diff'], stats['stratified_diff'] = [], []
    for _ in range(BOOTSTRAP):
        sample = [r for c in (rng.choice(clusters) for _ in clusters) for r in c]
        for kind, fn in (('pooled', _pooled_auc), ('stratified', stratified_auc)):
            a, b = fn(sample, keys[0]), fn(sample, keys[1])
            stats[f'{kind}_{keys[0]}'].append(a)
            stats[f'{kind}_{keys[1]}'].append(b)
            stats[f'{kind}_diff'].append(b - a)
    out = {name: _ci(values) for name, values in stats.items()}
    out['pooled_diff_p_le_0'] = round(sum(d <= 0 for d in stats['pooled_diff']) / BOOTSTRAP, 4)
    out['stratified_diff_p_le_0'] = round(sum(d <= 0 for d in stats['stratified_diff']) / BOOTSTRAP, 4)
    return out


def paired_permutation_p(rows: list[dict], keys: tuple[str, str], rng: random.Random) -> float:
    """Two-sided p of the pooled AUC difference under swapping arms within papers."""
    observed = abs(_pooled_auc(rows, keys[1]) - _pooled_auc(rows, keys[0]))
    hits = 0
    for _ in range(BOOTSTRAP):
        swapped = []
        for r in rows:
            a, b = (r[keys[0]], r[keys[1]]) if rng.random() < 0.5 else (r[keys[1]], r[keys[0]])
            swapped.append({'positive': r['positive'], 'a': a, 'b': b})
        hits += abs(_pooled_auc(swapped, 'b') - _pooled_auc(swapped, 'a')) >= observed - 1e-12
    return round((hits + 1) / (BOOTSTRAP + 1), 4)


def lint_pass_share(record: dict, rules: list[dict]) -> float:
    from lib.longform.documents import style_metrics
    metric_rules = [r for r in rules if r.get('metric')]
    if not metric_rules:
        return float('nan')
    sections = section_texts_from_record(record)
    passed = sum(1 for r in metric_rules
                 if style_metrics.check(r['metric'], sections.get(r['metric']['section'], ''),
                                        plain=True) is None)
    return passed / len(metric_rules)


def section_texts_from_record(record: dict) -> dict:
    out = {'abstract': record['abstract']}
    for sec in record['sections']:
        if sec.get('body', True) and sec['role'] not in ('front', 'back', 'abstract', 'other'):
            out[sec['role']] = (out.get(sec['role'], '') + ' ' + sec['text']).strip()
    return out


def separation_papers(root: Path, split: str) -> list[dict]:
    corpus = json.loads((root / 'corpus.json').read_text())
    feats = {json.loads(l)['id']: json.loads(l) for l in (root / 'features.jsonl').read_text().splitlines()}
    return [r for r in corpus if r['split'] == split and not r.get('status')
            and r['stratum'] in ('main', 'datasets_benchmarks')
            and r['id'] in feats and not feats[r['id']].get('error')
            and feats[r['id']].get('body_words', 0) >= 1500
            and feats[r['id']].get('abs_n_words', 0) >= 40]


def separation(args) -> int:
    """Checklist vs checklist-free judge on one split, ``--repeats`` scores per arm.

    Repeat 0 is the temperature-0 judgment (its cache file predates repeats);
    repeats 1.. sample at temperature 1 and the paper's arm score is the mean,
    which removes judge noise from the paired comparison.
    """
    from evaluations.slides_bench import llm_bridge
    root = Path(args.root)
    rules = load_style_rules()
    checklist = [r for r in rules if r['kind'] == 'checklist']
    papers = separation_papers(root, args.split)
    out_dir = root / 'validation'
    out_dir.mkdir(exist_ok=True)
    digests = {arm: hashlib.sha256(json.dumps(judge_messages('', items)).encode()).hexdigest()[:12]
               for arm, items in (('free', None), ('checklist', checklist))}

    def score(row, arm, rep):
        suffix = '' if rep == 0 else f'.r{rep}'
        target = out_dir / f'{row["id"]}.{arm}.{digests[arm]}.{args.judge_model}{suffix}.json'
        if target.is_file():
            return json.loads(target.read_text())
        record = json.loads((root / 'records' / f'{row["id"]}.json').read_text())
        messages = judge_messages(paper_view(record), checklist if arm == 'checklist' else None)
        for _ in range(3):
            try:
                content, _usage = llm_bridge.chat(messages, model=args.judge_model, max_tokens=8000,
                                                  temperature=0 if rep == 0 else 1)
                data = json.loads(re.search(r'\{.*\}', content or '', re.S).group(0))
                if isinstance(data.get('score'), (int, float)):
                    target.write_text(json.dumps(data))
                    return data
            except Exception as exc:  # a malformed reply or a transient route failure
                print('judge retry', row['id'], arm, rep, exc, flush=True)
        return {'score': None}
    jobs = [(row, arm, rep) for rep in range(args.repeats) for row in papers
            for arm in ('free', 'checklist')]
    with ThreadPoolExecutor(args.in_flight) as pool:
        results = list(pool.map(lambda job: score(*job), jobs))
    by = {}
    for (row, arm, rep), res in zip(jobs, results):
        entry = by.setdefault(row['id'], {
            'id': row['id'], 'group': row['group'], 'venue_year': f'{row["venue"]} {row["year"]}',
            'positive': row['group'] in ('award', 'highcite'), 'free_reps': {}, 'checklist_reps': {}})
        if res.get('score') is not None:
            entry[f'{arm}_reps'][rep] = float(res['score'])
    rows = []
    for entry in by.values():
        if len(entry['free_reps']) == len(entry['checklist_reps']) == args.repeats:
            entry['free'] = statistics.mean(entry['free_reps'].values())
            entry['checklist'] = statistics.mean(entry['checklist_reps'].values())
            entry['free_r0'], entry['checklist_r0'] = entry['free_reps'][0], entry['checklist_reps'][0]
            rows.append(entry)
    for row in rows:
        record = json.loads((root / 'records' / f'{row["id"]}.json').read_text())
        row['lint'] = lint_pass_share(record, rules)
    rng = random.Random(SEED)
    summary = {'judge_model': args.judge_model, 'prompt_digests': digests, 'split': args.split,
               'checklist': [r['id'] for r in checklist],
               'repeats': args.repeats, 'n_papers_selected': len(papers), 'n': len(rows),
               'n_positive': sum(r['positive'] for r in rows),
               'n_venue_years': len(_clusters(rows))}
    for name, subset in (('vs_all_controls', rows),
                         ('vs_accepted_controls', [r for r in rows if r['positive'] or r['group'] == 'control_accepted'])):
        boot = cluster_bootstrap(subset, ('free', 'checklist'), rng)
        entry = {'n_pos': sum(r['positive'] for r in subset), 'n_neg': sum(not r['positive'] for r in subset),
                 'auc_free': round(_pooled_auc(subset, 'free'), 4),
                 'auc_checklist': round(_pooled_auc(subset, 'checklist'), 4),
                 'auc_diff': round(_pooled_auc(subset, 'checklist') - _pooled_auc(subset, 'free'), 4),
                 'auc_free_r0': round(_pooled_auc(subset, 'free_r0'), 4),
                 'auc_checklist_r0': round(_pooled_auc(subset, 'checklist_r0'), 4),
                 'stratified_auc_free': round(stratified_auc(subset, 'free'), 4),
                 'stratified_auc_checklist': round(stratified_auc(subset, 'checklist'), 4),
                 'bootstrap_by_venue_year': boot,
                 'paired_permutation_p': paired_permutation_p(subset, ('free', 'checklist'), rng),
                 'auc_lint_only': (round(_pooled_auc(linted, 'lint'), 4) if (linted := [
                     r for r in subset if r['lint'] == r['lint']]) else None)}
        summary[name] = entry
    path = HERE / 'validation_separation.json'
    stored = json.loads(path.read_text()) if path.is_file() else {}
    stored = stored if 'runs' in stored else {'runs': {}}
    stored['runs'][f'{args.split}/{args.judge_model}/checklist-{len(checklist)}'] = summary
    path.write_text(json.dumps(stored, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


# ── shared revision helpers (the historical per-rule selection uses these) ──

_PIPELINE_PAPER_RULES_FALLBACK = (
    'State contributions and results confidently: no hedging or verbal fences.',
    'The introduction states the problem, the gap and the contribution in its first paragraph, and every section advances one takeaway.',
    'The novelty is stated precisely against named closest work, not as generic improvement.',
    'Every claim in the text points to its experiment, figure or table.',
    'No verbal fences or hedging outside the Limitations section.',
)


def pipeline_paper_rules() -> list[str]:
    try:
        from lib.longform.documents.rules import RULES
    except ImportError:
        return list(_PIPELINE_PAPER_RULES_FALLBACK)
    return [r.text for r in RULES if r.id.startswith('paper.') and not r.id.startswith('paper.style.')
            and r.kind in ('lint', 'checklist')]


def tex_unit(path: Path) -> str:
    text = path.read_text(encoding='utf-8')
    start = text.find('\\begin{abstract}')
    intro = re.search(r'\\section\{Introduction\}', text)
    after = re.search(r'\\section\{', text[intro.end():]) if intro else None
    end = intro.end() + after.start() if after else len(text)
    title = re.search(r'\\title\{(.+?)\}\s*\n', text, re.S)
    head = f'\\title{{{title.group(1)}}}\n' if title else ''
    return head + text[start:end]


def unsourced_items(source: str, revised: str, latex: bool) -> list[str]:
    """Numbers and citations in ``revised`` that ``source`` does not state, by
    the pipeline's provenance gate (``lib.longform.documents.provenance``;
    rounding a stated value is allowed)."""
    from lib.longform.documents.provenance import check_texts
    return [f'{row["kind"]} {row["value"]}' for row in
            check_texts({'revision': revised}, {'source': source}, latex=latex)['unsourced']]


def revise_messages(unit: str, rules: list[str], style: list[dict] | None) -> list[dict]:
    lines = '\n'.join(f'- {r}' for r in rules)
    extra = ''
    if style:
        extra = '\nAlso apply these evidence-based style rules (measured on award-winning and highly cited papers vs matched controls):\n'
        for r in style:
            extra += f'- {r["text"]}\n'
            for ex in (r.get('exemplars') or [])[:1]:
                extra += f'    exemplar: "{ex}"\n'
    system = ('You revise the title, abstract and introduction of a machine-learning paper '
              '(LaTeX). Keep every fact, number, macro (e.g. \\pn{...}), citation key, label and '
              'reference exactly; never add numbers or claims that are not in the draft. Keep '
              'LaTeX valid. Return ONLY the revised LaTeX of the same unit.\nRules:\n' + lines + extra)
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': unit}]


# ── per-rule blind A/B on held-out units ───────────────────────────────────────

RULE_AB_UNITS = 16   # held-out accepted-ordinary papers used as drafts


def held_out_units(root: Path, n: int) -> list[tuple[str, str]]:
    """(id, abstract + introduction) of test-split accepted-ordinary papers."""
    corpus = json.loads((root / 'corpus.json').read_text())
    rows = sorted((r for r in corpus if r['split'] == 'test' and r['group'] == 'control_accepted'
                   and not r.get('status')), key=lambda r: hashlib.sha256(r['id'].encode()).hexdigest())
    units = []
    for row in rows:
        path = root / 'records' / f'{row["id"]}.json'
        if not path.is_file():
            continue
        record = json.loads(path.read_text())
        intro = ' '.join(s['text'] for s in record['sections']
                         if s.get('body', True) and s['role'] == 'introduction')
        if 40 <= len(record['abstract'].split()) <= 400 and 300 <= len(intro.split()) <= 1600:
            units.append((row['id'], f'Abstract\n{record["abstract"]}\n\nIntroduction\n{intro}'))
        if len(units) == n:
            break
    return units


def _revise(unit: str, rules: list[str], style: list[dict] | None, model: str, cache: Path) -> str:
    from evaluations.slides_bench import llm_bridge
    messages = revise_messages(unit, rules, style)
    messages[0]['content'] = messages[0]['content'].replace('(LaTeX)', '(plain text)').replace(
        'Keep LaTeX valid. ', '')
    key = hashlib.sha256(json.dumps([messages, model]).encode()).hexdigest()[:20]
    target = cache / f'rev-{key}.txt'
    if target.is_file():
        return target.read_text()
    content, _ = llm_bridge.chat(messages, model=model, max_tokens=6000, temperature=0)
    content = re.sub(r'^```\w*\n|\n```$', '', content.strip())
    target.write_text(content)
    return content


def _judge_both_orders(a: str, b: str, uid: str, model: str, cache: Path) -> int:
    """+1 if B wins both orders, -1 if A wins both, 0 otherwise, by the
    pipeline's one style-blind judge (``pipeline._judge_pair``)."""
    from lib.production.critic import blind_pairwise
    judge = pipeline_judges([uid], model, cache)[uid][PIPELINE_JUDGE]
    return _score(blind_pairwise(judge, {'text': b}, {'text': a}, seed=f'rule-ab:{uid}')['verdict'])


def rules_ab(args) -> int:
    from evaluations.slides_bench import llm_bridge
    llm_bridge.install(args.judge_model)
    root = Path(args.root)
    cache = root / 'rule_ab'
    cache.mkdir(exist_ok=True)
    candidates = json.loads((HERE / 'candidates.json').read_text())['candidates']
    testable = [c for c in candidates if c.get('testable_on_abstract_intro')]
    base = pipeline_paper_rules()
    units = held_out_units(root, RULE_AB_UNITS)

    def baseline(unit):
        return _revise(unit[1], base, None, args.writer_model, cache)
    with ThreadPoolExecutor(args.in_flight) as pool:
        baselines = list(pool.map(baseline, units))
    results = {}
    for cand in testable:
        def one(i):
            unit = units[i]
            revised = _revise(unit[1], base, [cand], args.writer_model, cache)
            invented = unsourced_items(unit[1], revised, latex=False)
            return _judge_both_orders(baselines[i], revised, unit[0], args.judge_model, cache), invented
        with ThreadPoolExecutor(args.in_flight) as pool:
            outcomes = list(pool.map(one, range(len(units))))
        b = sum(1 for o, _ in outcomes if o > 0)
        a = sum(1 for o, _ in outcomes if o < 0)
        results[cand['id']] = {'n_units': len(units), 'rule_preferred_both_orders': b,
                               'baseline_preferred_both_orders': a, 'split': len(units) - a - b,
                               'units_with_invented_numbers': sum(1 for _, inv in outcomes if inv),
                               'keep': b > a}
        print(cand['id'], results[cand['id']], flush=True)
    out = {'writer_model': args.writer_model, 'judge_model': args.judge_model,
           'units': [u[0] for u in units], 'criterion': (
               'a testable candidate ships only if the revision with it is preferred in both '
               'presentation orders on more held-out units than the baseline revision'),
           'rules': results}
    (HERE / 'validation_rule_ab.json').write_text(json.dumps(out, indent=1))
    if args.write:
        write_style_rules(candidates, results)
    return 0


def write_style_rules(candidates: list[dict], results: dict) -> None:
    shipped = []
    for cand in candidates:
        verdict = results.get(cand['id'])
        if cand.get('testable_on_abstract_intro') and not (verdict and verdict['keep']):
            continue
        row = {k: cand[k] for k in ('id', 'kind', 'text', 'origin', 'exemplars') if cand.get(k)}
        row['evidence'] = ' ; '.join(cand['evidence']) + (
            f" ; rule A/B: preferred {verdict['rule_preferred_both_orders']} vs "
            f"{verdict['baseline_preferred_both_orders']} of {verdict['n_units']} held-out units"
            if verdict else ' ; structural rule (not testable on abstract+introduction A/B)')
        if cand.get('metric'):
            row['metric'] = cand['metric']
        shipped.append(row)
    write_style_file(shipped)


def write_style_file(rules: list[dict], outlines: bool = True) -> None:
    """``style_rules.json``: the shipped rules plus the per-type outlines."""
    from evaluations.paper_style_corpus.distill import style_outlines
    skeletons = json.loads((HERE / 'candidates.json').read_text())['skeletons']
    STYLE_RULES.write_text(json.dumps({'source': 'evaluations/paper_style_corpus (docs/PAPER_STYLE_EVIDENCE.md)',
                                       'rules': rules, 'outlines': style_outlines(skeletons) if outlines else {}},
                                      indent=1, ensure_ascii=False) + '\n')


# ── blind A/B on drafts with the pipeline's judge, plus per-rule ablation ──

DRAFT_RULE_SELECTION_UNITS = 16   # the held-out units ``rules`` selected rules on; never reused


def registry_rules() -> tuple[list, list]:
    """(base paper rules, style rules) as the pipeline's writer reads them."""
    from lib.longform.documents.rules import rules_for
    writable = [r for r in rules_for('paper') if r.kind in ('lint', 'checklist')]
    return ([r for r in writable if not r.id.startswith('paper.style.')],
            [r for r in writable if r.id.startswith('paper.style.')])


def registry_revise_messages(unit: str, rules: list, outline: str, latex: bool) -> list[dict]:
    fmt = 'LaTeX' if latex else 'plain text'
    system = (f'You revise the title (if present), abstract and introduction of a machine-learning '
              f'paper ({fmt}). Keep every fact, number, macro (e.g. \\pn{{...}}), citation key, label '
              'and reference exactly; never add numbers or claims that are not in the draft.'
              + (' Keep LaTeX valid.' if latex else '')
              + f' Return ONLY the revised {fmt} of the same unit.\n\nRules (all mandatory):\n'
              + '\n'.join(f'- [{r.id}] {r.text}' for r in rules)
              + (f'\n\n{outline}' if outline else ''))
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': unit}]


def _cached_revision(unit: str, rules: list, outline: str, latex: bool, model: str, cache: Path) -> str:
    from evaluations.slides_bench import llm_bridge
    messages = registry_revise_messages(unit, rules, outline, latex)
    target = cache / f'rev-{hashlib.sha256(json.dumps([messages, model]).encode()).hexdigest()[:20]}.txt'
    if target.is_file():
        return target.read_text()
    content, _ = llm_bridge.chat(messages, model=model, max_tokens=8000, temperature=0)
    content = re.sub(r'^```\w*\n|\n```$', '', (content or '').strip())
    if not content:
        raise RuntimeError('writer returned no content')
    target.write_text(content)
    return content


#: The pipeline judge is style-blind since 2026-11-18 (no registry rule in its
#: prompt). Earlier runs cached 'production' (every registry rule) and
#: 'style_blind' (every rule but ``paper.style.*``) judgements under the same
#: pair keys, so this name keeps them from being replayed as this judge.
PIPELINE_JUDGE = 'rule_free'


def pipeline_judges(unit_ids: list[str], model: str, cache: Path) -> dict:
    """Per unit: the pipeline's blind-A/B judge (``pipeline._judge_pair``) as it
    runs: style-blind, told only the task, the venue and the two versions."""
    from lib.longform.documents import pipeline as pl
    from lib.longform.documents.types import document_type
    judges = {}
    for uid in unit_ids:
        ctx = {'doc_type': document_type('paper'), 'brief': {'judge_model': model},
               'workdir': str(cache / 'judge-work' / hashlib.sha256(uid.encode()).hexdigest()[:12]),
               'artifacts': {}}
        judges[uid] = {PIPELINE_JUDGE: _cached_judge(pl._judge_pair(ctx, 'style-ab'),
                                                     PIPELINE_JUDGE, model, cache)}
    return judges


def _cached_judge(judge, name: str, model: str, cache: Path):
    def run(pair):
        target = cache / f'pj-{hashlib.sha256(json.dumps([name, model, pair]).encode()).hexdigest()[:20]}.json'
        if target.is_file():
            return tuple(json.loads(target.read_text()))
        last = None
        for _ in range(3):
            try:
                result = judge(pair)
                target.write_text(json.dumps(list(result)))
                return result
            except (ValueError, RuntimeError) as exc:  # malformed reply or route failure
                last = exc
        raise last
    return run


def _score(verdict: str) -> int:
    return {'candidate': 1, 'baseline': -1}.get(verdict, 0)


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided binomial sign test (ties dropped)."""
    from math import comb
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(0, min(wins, losses) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def comparison_summary(outcomes: list[dict], rng: random.Random) -> dict:
    scores = [_score(o['verdict']) for o in outcomes]
    margins = [o['totals']['candidate'] - o['totals']['baseline'] for o in outcomes]
    boot = lambda xs: _ci([statistics.mean(rng.choice(xs) for _ in xs) for _ in range(BOOTSTRAP)])  # noqa: E731
    wins, losses = scores.count(1), scores.count(-1)
    return {'n': len(scores), 'candidate_preferred': wins, 'no_preference': scores.count(0),
            'baseline_preferred': losses, 'sign_test_p': sign_test_p(wins, losses),
            'mean_score': round(statistics.mean(scores), 4), 'mean_score_ci95': boot(scores),
            'mean_rubric_margin': round(statistics.mean(margins), 3), 'rubric_margin_ci95': boot(margins)}


def drafts(args) -> int:
    """Revise each unit with the base rules (A) and with base + style rules +
    outlines (B); judge B vs A with the pipeline's judge in both presentation
    orders (``blind_pairwise``). Then ablate each style rule and the outlines
    (B without it vs full B, same judge): a rule whose removal is
    preferred on more units than its presence is harmful and, with
    ``--write``, is removed from ``style_rules.json``."""
    from evaluations.slides_bench import llm_bridge
    from lib.longform.documents.rules import outline_guidance
    from lib.production.critic import blind_pairwise
    llm_bridge.install(args.judge_model)
    root = Path(args.root)
    cache = root / 'drafts_ab'
    cache.mkdir(exist_ok=True)
    units = [(name, tex_unit(Path(path)), True, path)
             for name, path in (spec.split('=', 1) for spec in args.draft)]
    held = held_out_units(root, DRAFT_RULE_SELECTION_UNITS + args.units)[DRAFT_RULE_SELECTION_UNITS:]
    units += [(pid, text, False, f'held-out accepted paper {pid}') for pid, text in held]
    base, style = registry_rules()
    outline = outline_guidance('paper')
    arms = {'A': (base, ''), 'B': (base + style, outline)}
    if args.ablate:
        arms.update({f'B-{r.id}': (base + [s for s in style if s.id != r.id], outline) for r in style})
        if outline:
            arms['B-outlines'] = (base + style, '')
    jobs = [(u, arm) for u in units for arm in arms]

    def write(job):
        (uid, text, latex, _src), arm = job
        return _cached_revision(text, arms[arm][0], arms[arm][1], latex, args.writer_model, cache)
    with ThreadPoolExecutor(args.in_flight) as pool:
        revisions = dict(zip([(u[0], arm) for u, arm in jobs], pool.map(write, jobs)))
    judges = pipeline_judges([u[0] for u in units], args.judge_model, cache)
    comparisons = [('B_vs_A', PIPELINE_JUDGE, 'B', 'A')]
    comparisons += [(f'ablate:{arm[2:]}', PIPELINE_JUDGE, arm, 'B') for arm in arms if arm.startswith('B-')]
    cjobs = [(c, u) for c in comparisons for u in units]

    def judge(cjob):
        (name, judge_name, cand, baseline), (uid, _text, _latex, _src) = cjob
        return blind_pairwise(judges[uid][judge_name], {'text': revisions[(uid, cand)]},
                              {'text': revisions[(uid, baseline)]}, seed=f'{name}:{uid}')
    with ThreadPoolExecutor(args.in_flight) as pool:
        results = list(pool.map(judge, cjobs))
    rng = random.Random(SEED)
    by = {}
    for ((name, judge_name, _c, _b), u), res in zip(cjobs, results):
        by.setdefault(f'{name}/{judge_name}', []).append({'unit': u[0], **res})
    out = {'writer_model': args.writer_model, 'judge_model': args.judge_model,
           'judge': 'lib.longform.documents.pipeline._judge_pair + lib.production.critic.blind_pairwise '
                    '(both presentation orders, rubric totals decide each order)',
           'style_rules': [r.id for r in style], 'outlines': bool(outline),
           'units': [{'id': u[0], 'source': u[3]} for u in units],
           'rule_selection_units_excluded': DRAFT_RULE_SELECTION_UNITS,
           'invented_numbers': {arm: sum(1 for u in units if unsourced_items(u[1], revisions[(u[0], arm)], u[2]))
                                for arm in arms},
           'comparisons': {}, 'per_unit': {}}
    for key, rows in by.items():
        out['comparisons'][key] = comparison_summary(rows, rng)
        out['comparisons'][key + '/drafts_only'] = comparison_summary(
            [r for r in rows if r['unit'] in {u[0] for u in units if u[2]}], rng) if args.draft else None
        out['per_unit'][key] = {r['unit']: {'verdict': r['verdict'], 'position_bias': r['position_bias'],
                                            'totals': r['totals']} for r in rows}
    harmful = sorted(key.split('/')[0][len('ablate:'):] for key, s in out['comparisons'].items()
                     if key.startswith('ablate:') and not key.endswith('drafts_only')
                     and s['candidate_preferred'] > s['baseline_preferred'])
    out['harm_criterion'] = ('a style rule (or the outlines) is harmful when B without it is preferred '
                             'in both orders on more units than full B (style-blind judge)')
    out['harmful'] = harmful
    (HERE / ('validation_drafts_selection.json' if args.ablate else 'validation_drafts.json')).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != 'per_unit'}, indent=1))
    if args.write and harmful:
        kept = [r for r in load_style_rules() if r['id'] not in harmful]
        write_style_file(kept, outlines='outlines' not in harmful)
    return 0


# ── pre-registered held-out A/B of the shipped checklist (PREREG_HELDOUT_AB.md) ──

HELDOUT_PREREG = HERE / 'prereg_heldout_ab.json'
HELDOUT_RESULT = HERE / 'validation_heldout_ab.json'
HELDOUT_JUDGES = ('gpt-5.5', 'deepseek-v4-pro')   # primary, second family
#: Own drafts that were not among the 26 selection units (bf16_mechanism and
#: parallel_paper3 were); one file per paper, earlier copies of a paper excluded.
HELDOUT_OWN_DRAFTS = {
    'knowledge_recovery': 'knowledge/paper/main.tex',
    'knowledge_allocation': 'knowledge/paper/allocation/main.tex',
    'knowledge_repair': 'knowledge/paper/repair/main.tex',
    'card_iclr2027': 'card/CARD_iclr2027/main.tex',
    'parallel_parallelism': 'parallel/paper/main.tex',
    'parallel_c4l': 'parallel/papers/main_c4l/main.tex',
    'parallel_paperA': 'parallel/papers/paperA/main.tex',
    'parallel_paperB': 'parallel/papers/paperB/main.tex',
}
HELDOUT_OWN_ROOT = Path('/mnt/your-fs')
HELDOUT_ACCEPTED = 42   # accepted-ordinary papers after the 16 + 24 already used


def draft_unit(path: Path) -> str:
    """Title + abstract + first body section of a LaTeX draft, ``\\input`` inlined.

    Like ``tex_unit``, but a draft whose introduction lives in an input file or
    carries another heading still yields its abstract and opening section."""
    def inline(match):
        name = match.group(1)
        target = path.parent / (name if name.endswith('.tex') else name + '.tex')
        return target.read_text(encoding='utf-8') if target.is_file() else match.group(0)
    text = re.sub(r'\\input\{([^}]+)\}', inline, path.read_text(encoding='utf-8'))
    start = text.find('\\begin{abstract}')
    first = re.search(r'\\section\*?\{', text[start:])
    second = re.search(r'\\section\*?\{', text[start + first.end():]) if first else None
    end = start + first.end() + second.start() if second else len(text)
    title = re.search(r'\\title\{(.+?)\}\s*\n', text, re.S)
    return (f'\\title{{{title.group(1)}}}\n' if title else '') + text[start:end]


def unsourced(source: str, revision: str) -> dict:
    """Provenance gate: numbers and citation keys a revision states that its source lacks."""
    from lib.longform.documents.rules import numbers_in, strip_latex_arguments
    keys = lambda t: {k.strip() for group in re.findall(r'\\cite\w*\*?(?:\[[^\]]*\])*\{([^}]*)\}', t)  # noqa: E731
                      for k in group.split(',')}
    return {'numbers': sorted(numbers_in(strip_latex_arguments(revision)) - numbers_in(source)),
            'citations': sorted(keys(revision) - keys(source))}


def heldout_freeze(args) -> int:
    """Snapshot the units (copies) and write the registered unit list with hashes."""
    root = Path(args.root)
    cache = root / 'heldout_ab'
    (cache / 'units').mkdir(parents=True, exist_ok=True)
    used = json.loads((HERE / 'validation_drafts.json').read_text())['units']
    used_ids = {u['id'] for u in used} | {pid for pid in json.loads(
        (HERE / 'validation_rule_ab.json').read_text())['units']}
    used_sources = {u['source'] for u in used}
    units = []
    for name, rel in HELDOUT_OWN_DRAFTS.items():
        path = HELDOUT_OWN_ROOT / rel
        assert str(path) not in used_sources, path
        units.append({'id': name, 'kind': 'own_draft', 'latex': True, 'source': str(path),
                      'text': draft_unit(path)})
    first = DRAFT_RULE_SELECTION_UNITS + 24
    for pid, text in held_out_units(root, first + HELDOUT_ACCEPTED)[first:]:
        units.append({'id': pid, 'kind': 'heldout_accepted', 'latex': False,
                      'source': f'held-out accepted paper {pid}', 'text': text})
    assert len(units) == len(HELDOUT_OWN_DRAFTS) + HELDOUT_ACCEPTED
    assert not used_ids & {u['id'] for u in units}
    for unit in units:
        (cache / 'units' / f'{unit["id"]}.txt').write_text(unit['text'])
    HELDOUT_PREREG.write_text(json.dumps({
        'units': [{'id': u['id'], 'kind': u['kind'], 'latex': u['latex'], 'source': u['source'],
                   'words': len(u['text'].split()),
                   'sha256': hashlib.sha256(u['text'].encode()).hexdigest()} for u in units],
        'style_items': [r.id for r in registry_rules()[1]],
        'excluded_ids': sorted(used_ids)}, indent=1) + '\n')
    print(len(units), 'units frozen')
    return 0


def heldout(args) -> int:
    """Run the registered A/B: A = base paper rules, B = base + the shipped
    ``paper.style.*`` checklist; both judges in both orders; provenance gate."""
    from evaluations.slides_bench import llm_bridge
    from lib.longform.documents.rules import outline_guidance
    from lib.production.critic import blind_pairwise
    llm_bridge.install(HELDOUT_JUDGES[0])
    root = Path(args.root)
    cache = root / 'heldout_ab'
    prereg = json.loads(HELDOUT_PREREG.read_text())
    units = []
    for row in prereg['units']:
        text = (cache / 'units' / f'{row["id"]}.txt').read_text()
        assert hashlib.sha256(text.encode()).hexdigest() == row['sha256'], row['id']
        units.append((row['id'], text, row['latex']))
    base, style = registry_rules()
    assert [r.id for r in style] == prereg['style_items'] and not outline_guidance('paper')
    arms = {'A': base, 'B': base + style}
    jobs = [(u, arm) for u in units for arm in arms]

    def write(job):
        (uid, text, latex), arm = job
        return _cached_revision(text, arms[arm], '', latex, args.writer_model, cache)
    with ThreadPoolExecutor(args.in_flight) as pool:
        revisions = dict(zip([(u[0], arm) for u, arm in jobs], pool.map(write, jobs)))
    judges = {model: pipeline_judges([u[0] for u in units], model, cache) for model in HELDOUT_JUDGES}
    cjobs = [(model, u) for model in HELDOUT_JUDGES for u in units]

    def judge(cjob):
        model, (uid, _text, _latex) = cjob
        return blind_pairwise(judges[model][uid][PIPELINE_JUDGE], {'text': revisions[(uid, 'B')]},
                              {'text': revisions[(uid, 'A')]}, seed=f'heldout:{uid}')
    with ThreadPoolExecutor(args.in_flight) as pool:
        results = list(pool.map(judge, cjobs))
    rng = random.Random(SEED)
    out = {'prereg': 'evaluations/paper_style_corpus/PREREG_HELDOUT_AB.md',
           'writer_model': args.writer_model, 'judges': list(HELDOUT_JUDGES),
           'judge': 'lib.longform.documents.pipeline._judge_pair (rule-free) + '
                    'lib.production.critic.blind_pairwise (both presentation orders)',
           'style_items': [r.id for r in style], 'n_units': len(units),
           'provenance': {}, 'comparisons': {}, 'per_unit': {}}
    for arm in arms:
        gate = {u[0]: unsourced(u[1], revisions[(u[0], arm)]) for u in units}
        out['provenance'][arm] = {
            'units_with_unsourced_numbers': sum(1 for g in gate.values() if g['numbers']),
            'unsourced_numbers': sum(len(g['numbers']) for g in gate.values()),
            'units_with_unsourced_citations': sum(1 for g in gate.values() if g['citations']),
            'per_unit': {uid: g for uid, g in gate.items() if g['numbers'] or g['citations']}}
    for model in HELDOUT_JUDGES:
        rows = [{'unit': u[0], **res} for (m, u), res in zip(cjobs, results) if m == model]
        out['comparisons'][model] = comparison_summary(rows, rng)
        for kind in ('own_draft', 'heldout_accepted'):
            ids = {r['id'] for r in prereg['units'] if r['kind'] == kind}
            out['comparisons'][f'{model}/{kind}'] = comparison_summary(
                [r for r in rows if r['unit'] in ids], rng)
        out['per_unit'][model] = {r['unit']: {'verdict': r['verdict'], 'position_bias': r['position_bias'],
                                              'totals': r['totals']} for r in rows}
    primary, second = (out['comparisons'][m] for m in HELDOUT_JUDGES)
    out['outcome'] = {
        'primary_significant': primary['candidate_preferred'] > primary['baseline_preferred']
                               and primary['sign_test_p'] < 0.05,
        'second_judge_same_direction': second['candidate_preferred'] > second['baseline_preferred'],
        'zero_unsourced_numbers_in_B': out['provenance']['B']['unsourced_numbers'] == 0}
    out['outcome']['pass'] = all(out['outcome'].values())
    HELDOUT_RESULT.write_text(json.dumps(out, indent=1) + '\n')
    print(json.dumps({k: v for k, v in out.items() if k != 'per_unit'}, indent=1))
    return 0


def _committed_units(path: Path) -> list[str]:
    """``tex_unit`` of the file now and of every committed version of it."""
    import subprocess
    import tempfile
    units = [tex_unit(path)]
    log = subprocess.run(['git', '-C', str(path.parent), 'log', '--format=%H', '--', path.name],
                         capture_output=True, text=True)
    for commit in log.stdout.split() if log.returncode == 0 else []:
        shown = subprocess.run(['git', '-C', str(path.parent), 'show', f'{commit}:./{path.name}'],
                               capture_output=True, text=True)
        if shown.returncode == 0:
            with tempfile.NamedTemporaryFile('w', suffix='.tex') as handle:
                handle.write(shown.stdout)
                handle.flush()
                units.append(tex_unit(Path(handle.name)))
    return list(dict.fromkeys(units))


def provenance_audit(args) -> int:
    """Run the pipeline's provenance gate over every revision the studies
    produced (``drafts_ab``, ``rule_ab`` and ``ab`` caches), read-only. Each
    cached revision is matched to the unit it revises by word overlap; the
    shipped configuration's arms A/B of the 26 draft units are named exactly.
    Writes ``provenance_audit.json``."""
    from lib.longform.documents.provenance import check_texts
    root = Path(args.root)
    recorded = json.loads((HERE / 'validation_drafts.json').read_text())
    # A draft may have changed since it was revised: every committed version
    # of its file is a candidate source, and each revision is matched to the
    # closest one.
    drafts_units = [(u['id'], text, True) for u in recorded['units'] if u['source'].endswith('.tex')
                    for text in _committed_units(Path(u['source']))]
    drafts_units += [(pid, text, False) for pid, text in
                     held_out_units(root, DRAFT_RULE_SELECTION_UNITS + args.units)[DRAFT_RULE_SELECTION_UNITS:]]
    assert list(dict.fromkeys(u[0] for u in drafts_units)) == [u['id'] for u in recorded['units']]
    rule_units = [(pid, text, False) for pid, text in held_out_units(root, RULE_AB_UNITS)]
    base, style = registry_rules()
    named = {}
    for arm, rules in (('A', base), ('B', base + style)):
        for uid, text, latex in drafts_units:
            messages = registry_revise_messages(text, rules, '', latex)
            key = hashlib.sha256(json.dumps([messages, recorded['writer_model']]).encode()).hexdigest()[:20]
            named[f'rev-{key}.txt'] = f'shipped config arm {arm}'

    def words(text):
        return set(re.findall(r'[a-z]{4,}', text.lower()))

    def closest(text, units):
        own = words(text)
        return max(units, key=lambda u: len(own & words(u[1])) / (len(own | words(u[1])) or 1))
    rows = []
    for cache, units in (('drafts_ab', drafts_units), ('rule_ab', rule_units)):
        for path in sorted((root / cache).glob('rev-*.txt')):
            text = path.read_text()
            uid, source, latex = closest(text, units)
            rows.append((f'{cache}/{path.name}', named.get(path.name, ''), uid, source, latex, text))
    for path in sorted((root / 'ab').glob('*.tex')):
        uid = path.name.split('.')[0]
        text = path.read_text()
        source = closest(text, [u for u in drafts_units if u[0] == uid])[1]
        rows.append((f'ab/{path.name}', 'ab mode', uid, source, True, text))
    results = []
    for name, label, uid, source, latex, text in rows:
        report = check_texts({name: text}, {uid: source}, latex=latex)
        results.append({'revision': name, 'label': label, 'unit': uid, 'latex': latex,
                        'checked': report['checked'], 'unsourced': report['unsourced'],
                        'rounded': report['rounded']})
    out = {'gate': 'lib.longform.documents.provenance.check_texts (source = the unit revised)',
           'revisions': len(results),
           'with_unsourced': sum(1 for r in results if r['unsourced']), 'results': results}
    (HERE / 'provenance_audit.json').write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps({k: v for k, v in out.items() if k != 'results'}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('separation', 'drafts', 'rules', 'provenance-audit',
                                         'heldout-freeze', 'heldout'))
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--judge-model', default='gpt-5.5')
    parser.add_argument('--writer-model', default='gpt-5.6-terra')
    parser.add_argument('--in-flight', type=int, default=8)
    parser.add_argument('--draft', action='append', default=[])
    parser.add_argument('--split', choices=('test', 'train'), default='test')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--units', type=int, default=24)
    parser.add_argument('--ablate', action='store_true',
                        help='drafts: also ablate each style rule and the outlines '
                             '(writes validation_drafts_selection.json)')
    args = parser.parse_args(argv)
    return {'separation': separation, 'drafts': drafts, 'rules': rules_ab,
            'provenance-audit': provenance_audit,
            'heldout-freeze': heldout_freeze, 'heldout': heldout}[args.mode](args)


if __name__ == '__main__':
    sys.exit(main())
