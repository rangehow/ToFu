"""Executable specification of the staged document pipeline.

Stage gates, rule application, the shared critic and one full run over the
synthetic sample paper with scripted model replies (no network, no real
model). The full run compiles with the real TeX toolchain and is skipped when
latexmk/pgfplots are not installed.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.longform.documents import pipeline as pl
from lib.longform.documents.rules import lint_text, reported_numbers, rules_for
from lib.longform.documents.types import document_type
from lib.production.critic import blind_pairwise, critic_prompt, parse_critic_choice
from lib.production.stages import StageFailed

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _template_seams(monkeypatch):
    """The venue freshness check and the conformance gate reach the venue's
    pages and its official kit; their own suite is test_venue_conformance."""
    monkeypatch.setattr(pl, '_check_freshness', lambda edition: {
        'edition': edition, 'conference': edition, 'verdict': 'current'})
    def conformance(ctx, build, main):
        pdf = Path(build) / f'{Path(main).stem}.pdf'
        return {'edition': 'arr-rolling', 'stage': 'submission', 'passed': True,
                'submission_ready': True, 'draft_label': '', 'rows': [], 'failures': [],
                'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest() if pdf.is_file() else ''}
    monkeypatch.setattr(pl, '_conformance', conformance)

FIXTURES = Path(__file__).parent / 'fixtures'


# ── rules ───────────────────────────────────────────────────────────────────

def test_paper_lint_refuses_fences_outside_limitations():
    rules = rules_for('paper')
    text = 'We do not claim generality; this is merely a first step.'
    findings = lint_text(rules, 'introduction', text)
    assert any("'we do not claim'" in item for item in findings)
    assert any("'merely'" in item for item in findings)
    assert any("'a first step'" in item for item in findings)
    assert lint_text(rules, 'limitations', text) == []
    assert lint_text(rules, 'introduction', 'Our method is better.') == []


def test_paper_lint_allows_calibrated_hedges_and_refuses_overclaims():
    # Owner 2026-11-18: claim less, phrase carefully, but no fence sentences.
    rules = rules_for('paper')
    calibrated = ('The gap suggests that rounding, not capacity, may explain the '
                  'loss; it is consistent with the update-size analysis.')
    assert lint_text(rules, 'introduction', calibrated) == []
    over = lint_text(rules, 'experiments', 'Our method always outperforms baselines in all cases.')
    assert any('paper.no-overclaim' in item for item in over)
    assert any('paper.no-overclaim' in item for item in lint_text(
        rules, 'limitations', 'This universally holds.'))


def test_resume_lint_carries_owner_corrections():
    rules = rules_for('resume')
    findings = lint_text(rules, 'projects',
                         'A white-box harness for researchers, built by directing AI agents.')
    assert any('for researchers' in item for item in findings)
    assert any('by directing' in item for item in findings)
    assert lint_text(rules_for('paper'), 'projects', 'built for researchers') == []


def test_reported_numbers_ignore_identifiers_and_dimensions():
    text = (r'Loss drops to 3.124 (\citep{a2019}), a 0.39\% gap; see '
            r'\includegraphics[width=0.9\linewidth]{f1.5.pdf} and \ref{tab:2.1}.')
    assert reported_numbers(text) == {'3.124', '0.39'}


def test_every_owner_correction_is_a_registered_rule():
    paper = {rule.id for rule in rules_for('paper')}
    assert {'paper.official-template', 'paper.no-fences', 'paper.no-overclaim',
            'paper.calibrated-claims', 'paper.figure-qa',
            'paper.compile-clean', 'paper.independent-review', 'paper.blind-ab',
            'paper.solid-experiments', 'paper.novelty'} <= paper
    resume = {rule.id for rule in rules_for('resume')}
    assert {'resume.complete-coverage', 'resume.accepted-first-author',
            'resume.lead-distinctive', 'resume.insight-not-detail',
            'resume.confident-voice', 'resume.org-timeline'} <= resume
    assert all(rule.origin for rule in rules_for('paper') + rules_for('resume'))


def test_data_rules_plug_into_lint_and_checklist(tmp_path, monkeypatch):
    from lib.longform.documents import rules as rules_mod
    path = tmp_path / 'style_rules.json'
    path.write_text(json.dumps({'rules': [
        {'id': 'paper.style.no-utilize', 'kind': 'lint', 'text': 'Say use.',
         'origin': 'corpus analysis', 'phrases': ['Utilize']},
        {'id': 'paper.style.claim-first', 'kind': 'checklist', 'text': 'Lead with the claim.',
         'origin': 'corpus analysis', 'exemplars': ['We show that X.']}]}))
    monkeypatch.setattr(rules_mod, 'DATA_RULES_PATH', path)
    rules = rules_for('paper')
    assert any('paper.style.no-utilize' in f
               for f in lint_text(rules, 'method', 'We utilize X.'))
    assert any(r.id == 'paper.style.claim-first' and r.kind == 'checklist'
               and 'We show that X.' in r.text for r in rules)
    assert all(not r.id.startswith('paper.style') for r in rules_for('resume'))
    path.write_text(json.dumps({'rules': [{'id': 'x', 'kind': 'gate', 'text': 't',
                                           'origin': 'o'}]}))
    with pytest.raises(ValueError):
        rules_for('paper')


# ── shared critic ───────────────────────────────────────────────────────────

def test_critic_prompt_keeps_the_slides_contract_bytes():
    digests = [{'title': 'a'}, {'title': 'b'}]
    expected = (
        'Judge X. Output ONLY JSON: '
        '{"winner":1,"reason":"specific concise reason","scores":['
        '{"content":0,"evidence":0},{"content":0,"evidence":0}]}. '
        'Scores are integers 0..10. Candidates:\n'
        + json.dumps(digests, ensure_ascii=False, separators=(',', ':')))
    assert critic_prompt('Judge X.', ('content', 'evidence'), digests) == expected
    long = [{'text': 'x' * 40000}]
    assert len(critic_prompt('J.', ('a',), long)) < 29000
    assert 'x' * 40000 in critic_prompt('J.', ('a',), long, digest_chars=None)


def test_parse_critic_choice_validates_winner():
    assert parse_critic_choice('{"winner":2,"reason":" r ","scores":[]}', 2,
                               label='t', default_reason='d') == (1, 'r', [])
    with pytest.raises(ValueError):
        parse_critic_choice('{"winner":3}', 2, label='t', default_reason='d')


def test_document_judge_follows_its_rubric_scores_over_a_contradicting_winner(
        tmp_path, monkeypatch):
    ctx = _ctx(_sample(tmp_path), tmp_path)
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(
        {'winner': 1, 'reason': 'Version 2 is far stronger',
         'scores': [{'novelty': 1, 'clarity': 1}, {'novelty': 6, 'clarity': 8}]}))
    winner, _reason, _scores = pl._judge_pair(ctx, 'blind-ab')([{'a': 1}, {'b': 2}])
    assert winner == 1


def test_blind_pairwise_needs_a_win_in_both_orders():
    first_always = lambda pair: (0, 'first', [{}, {}])
    assert blind_pairwise(first_always, {'v': 'new'}, {'v': 'old'},
                          seed='s')['verdict'] == 'no preference'
    prefers_new = lambda pair: (0 if pair[0]['v'] == 'new' else 1, 'new', [{}, {}])
    result = blind_pairwise(prefers_new, {'v': 'new'}, {'v': 'old'}, seed='s')
    assert result['preferred'] and {r['candidate_position'] for r in result['rounds']} == {1, 2}
    prefers_old = lambda pair: (0 if pair[0]['v'] == 'old' else 1, 'old', [{}, {}])
    assert not blind_pairwise(prefers_old, {'v': 'new'}, {'v': 'old'}, seed='s')['preferred']


# ── stage gates ─────────────────────────────────────────────────────────────

def _sample(tmp_path: Path) -> Path:
    target = tmp_path / 'paper'
    shutil.copytree(FIXTURES / 'sample_paper', target)
    (target / 'figures' / 'loss_curve.pdf').write_bytes(b'%PDF-1.4 stub')
    (target / 'previous' / 'previous.pdf').write_bytes(b'%PDF-1.4 stub')
    return target


def _ctx(paper: Path, tmp_path: Path) -> dict:
    brief = json.loads((paper / pl.BRIEF_FILE).read_text())
    return {'paper_dir': str(paper), 'workdir': str(tmp_path / 'work'),
            'brief': brief, 'doc_type': document_type(brief['type']),
            'run_started': 100.0, 'artifacts': {}}


def _kit(fetched_at=200.0, commit='a' * 40):
    return {'upstream_commit': commit, 'archive_sha256': 'b' * 64 if commit else '',
            'source_url': 'https://example.invalid/kit.zip', 'files': {'x.sty': 'c'},
            'fetched_at': fetched_at, 'edition': 'arr-rolling', 'submission_ready': True,
            'freshness': {'verdict': 'current'}}


def test_intake_gate_refuses_shared_judge_and_stale_template(tmp_path):
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    good = {'brief': ctx['brief'], 'kit': _kit(), 'run_started': 100.0}
    assert pl._gate_intake(ctx, good) == []
    same = dict(ctx['brief'], judge_model=ctx['brief']['writer_model'])
    assert any('different model' in e for e in
               pl._gate_intake(ctx, {**good, 'brief': same}))
    stale = pl._gate_intake(ctx, {**good, 'kit': _kit(fetched_at=50.0)})
    assert any('not fetched in this run' in e for e in stale)
    unproven = pl._gate_intake(ctx, {**good, 'kit': _kit(commit='')})
    assert any('official archive URL or its hash' in e for e in unproven)


def test_dossier_gate_refuses_numbers_absent_from_source(tmp_path):
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    assert pl._gate_dossier(ctx, pl._run_dossier(ctx)) == []
    dossier = json.loads((paper / 'dossier.json').read_text())
    dossier['facts'][3]['text'] += ' Peak loss 9.999.'
    (paper / 'dossier.json').write_text(json.dumps(dossier))
    errors = pl._gate_dossier(ctx, pl._run_dossier(ctx))
    assert any("['9.999']" in e for e in errors)


def _with_dossier(ctx):
    ctx['artifacts']['dossier'] = pl._run_dossier(ctx)
    return ctx


def test_evidence_gate_turns_gaps_into_experiment_requests(tmp_path, monkeypatch):
    ctx = _with_dossier(_ctx(_sample(tmp_path), tmp_path))
    reply = [{'claim_id': cid, 'strength': 'strong', 'rationale': 'direct'}
             for cid in ('mechanism', 'c_selective', 'c_match', 'c_recover', 'c_memory')]
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(reply))
    art = pl._run_evidence(ctx)
    assert pl._gate_evidence(ctx, art) == []
    scale = next(row for row in art['rows'] if row['claim_id'] == 'c_scale')
    assert scale['strength'] == 'none'
    request = next(req for req in art['requests'] if req['claim_id'] == 'c_scale')
    assert request['to'] == ctx['brief']['project_lead'] and request['needed_experiment']
    assert (Path(ctx['workdir']) / 'artifacts' / 'experiment_requests.json').is_file()
    art['requests'] = []
    assert any('without experiment requests' in e for e in pl._gate_evidence(ctx, art))


def _with_evidence(ctx, monkeypatch):
    reply = [{'claim_id': cid, 'strength': 'strong', 'rationale': 'direct'}
             for cid in ('mechanism', 'c_selective', 'c_match', 'c_recover', 'c_memory')]
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(reply))
    ctx['artifacts']['evidence'] = pl._run_evidence(ctx)
    return ctx


def test_novelty_gate_fails_when_the_delta_is_unclear(tmp_path, monkeypatch):
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    record = {'title': 'Close 2025 Work', 'year': 2025, 'arxiv_id': '2501.00001',
              'authors': ['A. Author'], 'abstract': 'stochastic rounding'}
    monkeypatch.setattr(pl, '_academic_search', lambda q: (
        [record, {'title': 'Old', 'year': 2019}],
        [{'source': 'arxiv', 'status': 'available'}]))
    clear = [{'cid': 'c1', 'what_they_do': 'SR everywhere', 'what_we_add': 'only the update',
              'evidence': ['c_match'], 'delta_clear': True}]
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(clear))
    art = pl._run_novelty(ctx)
    assert [c['title'] for c in art['candidates']] == ['Close 2025 Work']
    assert pl._gate_novelty(ctx, art) == [] and '@article{relatedc1' in art['bib']
    vague = [dict(clear[0], delta_clear=False, evidence=['c_scale'])]
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(vague))
    errors = pl._gate_novelty(ctx, pl._run_novelty(ctx))
    assert any('unclear' in e for e in errors)
    assert any('cites no supported contribution' in e for e in errors)


def _outline(figure_section='experiments'):
    rows = []
    for sid, title in document_type('paper').sections:
        rows.append({'id': sid, 'title': title, 'takeaway': f'takeaway {sid}',
                     'facts': [], 'figures': ['fig:loss'] if sid == figure_section else []})
    rows[1]['facts'] = ['mechanism', 'c_selective', 'c_match', 'c_recover', 'c_memory']
    return {'sections': rows}


def test_outline_gate_enforces_coverage_and_confirmed_facts(tmp_path, monkeypatch):
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    assert pl._gate_outline(ctx, _outline()) == []
    dropped = _outline()
    dropped['sections'][1]['facts'] = ['c_match']
    assert any('left out' in e for e in pl._gate_outline(ctx, dropped))
    unsupported = _outline()
    unsupported['sections'][2]['facts'] = ['c_scale']
    assert any('unsupported claims' in e for e in pl._gate_outline(ctx, unsupported))
    missing_figure = _outline(figure_section='none')
    assert any('placed exactly once' in e for e in pl._gate_outline(ctx, missing_figure))


def test_outline_and_draft_prompts_carry_the_registry_paper_outlines(tmp_path, monkeypatch):
    # The shipped data may hold no outlines (the draft ablation removed them,
    # docs/PAPER_STYLE_EVIDENCE.md); the wiring is specified on a data file.
    from lib.longform.documents import rules as rules_mod
    data = json.loads(rules_mod.DATA_RULES_PATH.read_text())
    data['outlines'] = {'method': {
        'applies_to': 'proposes a new method', 'abstract': ['context', 'problem', 'method', 'result'],
        'introduction': ['context', 'method', 'contributions'], 'contributions': 'in prose',
        'exemplars': {'result': 'We obtain relative improvements of up to 40%.'}, 'evidence': 'e'}}
    data_file = tmp_path / 'style_rules.json'
    data_file.write_text(json.dumps(data))
    monkeypatch.setattr(rules_mod, 'DATA_RULES_PATH', data_file)
    guidance = rules_mod.outline_guidance('paper')
    assert rules_mod.outline_guidance('resume') == ''
    assert 'method (proposes a new method): abstract context -> problem -> method -> result' in guidance
    assert 'We obtain relative improvements of up to 40%.' in guidance
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    prompts = []

    def chat(_ctx, _role, messages, label):
        prompts.append((label, messages[0]['content']))
        if label == 'outline':
            return json.dumps(_outline())
        return '<latex>Body.</latex>'
    monkeypatch.setattr(pl, '_chat', chat)
    monkeypatch.setattr(pl, '_packages', lambda _ctx: 'amsmath')
    ctx['artifacts']['outline'] = pl._run_outline(ctx)
    pl._run_draft(ctx)
    labels = [label for label, _prompt in prompts]
    assert 'outline' in labels and any(label.startswith('draft:') for label in labels)
    assert all(guidance in prompt for _label, prompt in prompts)


def test_draft_gate_refuses_invented_numbers_hedges_and_unknown_keys(tmp_path, monkeypatch):
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    ctx['artifacts']['outline'] = _outline()
    errors = pl._section_check(ctx, 'introduction',
                               r'We believe loss is 2.718~\citep{nobody2020}.')
    assert any('no-invented-numbers' in e for e in errors)
    assert any('paper.no-fences' in e for e in errors)
    assert any('unknown keys' in e for e in errors)
    assert pl._section_check(ctx, 'introduction',
                             r'Loss reaches 3.124~\citep{kalamkar2019study}.') == []
    leaks = pl._section_check(ctx, 'experiments',
                              'Table 2 (source: dossier fact c_memory) shows memory.')
    assert any('no-pipeline-leaks' in e and 'c_memory' in e and 'dossier' in e
               for e in leaks)


def test_resume_gates_apply_owner_corrections(tmp_path):
    ctx = {'paper_dir': str(tmp_path), 'workdir': str(tmp_path / 'w'),
           'brief': {'type': 'resume'}, 'doc_type': document_type('resume'),
           'artifacts': {}}
    facts = [
        {'id': 'p1', 'kind': 'publication', 'venue_status': 'accepted',
         'author_role': 'first', 'status': 'sourced', 'source': {'url': 'u'}, 'text': 'A'},
        {'id': 'p2', 'kind': 'publication', 'venue_status': 'preprint',
         'author_role': 'first', 'status': 'sourced', 'source': {'url': 'u'}, 'text': 'B'},
        {'id': 'n', 'kind': 'stat', 'counts': 'first_author_accepted', 'value': 2,
         'status': 'sourced', 'source': {'url': 'u'}, 'text': '2 first-author papers'},
        {'id': 'r1', 'kind': 'role', 'include': True, 'status': 'sourced',
         'source': {'url': 'u'}, 'text': 'Team A'},
        {'id': 'r2', 'kind': 'role', 'include': True, 'status': 'confirmed',
         'source': {'owner': 'owner, 2026-11-18'}, 'text': 'Team B'},
    ]
    dossier = {'facts': facts, 'figures': [], 'number_gaps': {}, 'bib_keys': [],
               'awaiting_owner': []}
    errors = pl._gate_dossier(ctx, dossier)
    assert any('only 1 are accepted' in e for e in errors)
    ctx['artifacts']['dossier'] = dossier
    outline = {'sections': [{'id': sid, 'title': t, 'takeaway': f't {sid}',
                             'facts': ['r1'] if sid == 'experience' else [], 'figures': []}
                            for sid, t in document_type('resume').sections]}
    errors = pl._gate_outline(ctx, outline)
    assert any("complete-coverage: the outline drops ['r2']" in e for e in errors)
    assert any('org-timeline' in e and 'r1' in e for e in errors)


def test_render_repairs_while_badness_falls_and_keeps_the_best(tmp_path, monkeypatch):
    from lib.longform.documents import render as render_mod
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    ctx['artifacts']['outline'] = _outline()
    ctx['artifacts']['intake'] = {'kit': {'files': {}, 'page_limit': 8, 'family': 'acl', 'edition': 'arr-rolling'},
                                  'owner_directives': {'items': []}, 'draft': None}
    ctx['artifacts']['draft'] = {'sections': {}}
    ctx['artifacts']['revise'] = {'sections': {sid: f'v0 {sid}' for sid, _t in
                                               document_type('paper').sections}}
    reports = iter([3, 1, 2])
    compiled = []

    def fake_compile(build, **_k):  # noqa: ARG001
        n = next(reports, None)
        if n is None:  # the re-render of the kept best version
            n = compiled[1]
        compiled.append(n)
        return {'compile_status': 'success', 'pdf': 'x.pdf', 'pdf_sha256': str(n),
                'body_pages': 4, 'page_limit': 4, 'overfull': ['1pt'] * n,
                'undefined': [], 'failures': [f'{n} overfull box(es)']}
    monkeypatch.setattr(render_mod, 'compile_and_check', fake_compile)
    monkeypatch.setattr(render_mod, 'assemble', lambda build, **k: build.mkdir(
        parents=True, exist_ok=True) or (build / 'main.tex').write_text('x'))
    repairs = []
    monkeypatch.setattr(pl, '_chat', lambda c, role, messages, label: (
        repairs.append(label), f'<latex id="method">v{len(repairs)} method</latex>')[1])
    checked = tmp_path / 'checked.pdf'
    checked.write_bytes(b'%PDF-1.4 checked build')
    digest = hashlib.sha256(checked.read_bytes()).hexdigest()
    monkeypatch.setattr(pl, '_conformance', lambda *args: {
        'passed': True, 'failures': [], 'pdf': str(checked), 'pdf_sha256': digest,
        'pages': 4, 'body_pages': 4})
    art = pl._run_render(ctx)
    assert art['pdf'] == str(checked) and art['pdf_sha256'] == digest
    ctx['artifacts']['render'] = art
    assert pl._gate_deliver(ctx, {'pdf': str(checked), 'provenance': {'unsourced': []}}) == []
    assert repairs == ['render-repair', 'render-repair']
    assert compiled == [3, 1, 2, 1]
    assert art['sections']['method'] == 'v1 method' and art['overfull'] == ['1pt']
    assert pl._gate_render(ctx, art) == ['1 overfull box(es)']


@pytest.mark.parametrize('portfolio_policy', [None, 'owner-ccf-a'])
def test_delivery_refuses_pdf_not_inspected_by_conformance(tmp_path, portfolio_policy):
    delivered = tmp_path / 'delivered.pdf'
    delivered.write_bytes(b'%PDF delivered render')
    inspected = tmp_path / 'inspected.pdf'
    inspected.write_bytes(b'%PDF different conformance build')
    ctx = {'portfolio_policy': portfolio_policy, 'artifacts': {'render': {
        'pdf_sha256': hashlib.sha256(delivered.read_bytes()).hexdigest(),
        'conformance': {'passed': True,
                        'pdf_sha256': hashlib.sha256(inspected.read_bytes()).hexdigest()}}}}
    errors = pl._gate_deliver(ctx, {'pdf': str(delivered),
                                   'provenance': {'unsourced': []}})
    assert errors == ['delivered PDF differs from the template-conformance PDF']
    render = ctx['artifacts']['render']
    render['conformance']['pdf_sha256'] = render['pdf_sha256']
    artifact = {'pdf': str(delivered), 'provenance': {'unsourced': []}}
    assert pl._gate_deliver(ctx, artifact) == []
    delivered.write_bytes(b'%PDF changed after render and conformance')
    assert pl._gate_deliver(ctx, artifact) == [
        'delivered PDF differs from the render-QA PDF']


# ── full run ────────────────────────────────────────────────────────────────

def _tex_ready() -> bool:
    if not shutil.which('latexmk'):
        return False
    probe = subprocess.run(['kpsewhich', 'pgfplots.sty', 'natbib.sty'],
                           capture_output=True, text=True, check=False)
    return probe.stdout.count('.sty') == 2


_SECTION_TEXT = {
    'abstract': r'Stochastically rounding only the BF16 weight update matches FP32 '
                r'master weights: validation loss 3.124 versus 3.112.',
    'introduction': r'Pure BF16 training stalls because 61.5\% of updates become stale '
                    r'(Section~\ref{sec:method}). Rounding the update stochastically '
                    r'removes the stall~\citep{kalamkar2019study,relatedc1}.',
    'related_work': r'Mixed precision keeps FP32 master weights~\citep{micikevicius2018mixed}.',
    'method': r'We stochastically round the AdamW update~\citep{loshchilov2019decoupled}.',
    'experiments': r'Figure~\ref{fig:loss} shows loss 3.124 against 3.112; memory drops '
                   r'from 12 to 8 bytes per parameter, a 33.3\% saving.',
    'conclusion': r'Rounding the update is enough for BF16 training.',
    'limitations': r'We test GPT-2 small only; scale is future work.',
}


def _fake_chat(ctx, role, messages, label):
    content = messages[-1]['content']
    if label == 'evidence':
        return json.dumps([{'claim_id': c, 'strength': 'strong', 'rationale': 'direct'}
                           for c in ('mechanism', 'c_selective', 'c_match', 'c_recover', 'c_memory')])
    if label == 'novelty':
        return json.dumps([{'cid': 'c1', 'what_they_do': 'SR everywhere',
                            'what_we_add': 'only the update', 'evidence': ['c_match'],
                            'delta_clear': True}])
    if label == 'outline':
        return json.dumps(_outline())
    if label.startswith('draft:'):
        sid = label.split(':', 1)[1]
        return f'<latex>{_SECTION_TEXT[sid]}</latex>'
    if label == 'critique':
        return json.dumps({
            'scores': {name: 7 for name, _m in document_type('paper').rubric},
            'overall': 6, 'checklist': [{'rule': r.id, 'pass': True, 'quote': 'q'}
                                        for r in rules_for('paper') if r.kind == 'checklist'],
            'findings': [{'section': 'conclusion', 'issue': 'thin', 'fix': 'state the saving'}]})
    if label.startswith('revise:'):
        return r'<latex>Rounding the update is enough: memory falls by 33.3\%.</latex>'
    if label in ('revise-ab', 'blind-ab'):
        start = content.index('Candidates:\n') + len('Candidates:\n')
        pair = json.loads(content[start:])
        newer = 0 if ('33.3' in json.dumps(pair[0]) and 'Possibly' not in json.dumps(pair[0])
                      and ('falls by' in json.dumps(pair[0]) or label == 'blind-ab')) else 1
        scores = [{'novelty': 5}, {'novelty': 5}]
        scores[newer] = {'novelty': 8}
        return json.dumps({'winner': newer + 1, 'reason': 'stronger', 'scores': scores})
    raise AssertionError(f'unexpected model call {label}')


def _fake_acl_kit(edition, destination):
    """A stand-in for the fetched ACL kit (the real fetch is network)."""
    destination.mkdir(parents=True, exist_ok=True)
    sty = ('\\ProvidesPackage{acl}\n\\DeclareOption*{}\n\\ProcessOptions\\relax\n'
           '\\RequirePackage[margin=1in]{geometry}\n\\RequirePackage{natbib}\n'
           '\\bibliographystyle{plainnat}\n')
    (destination / 'acl.sty').write_text(sty)
    return {'venue': 'arr', 'edition': edition, 'family': 'acl',
            'source_url': 'https://example.invalid/kit.zip',
            'landing_page': 'https://example.invalid', 'upstream_commit': 'f' * 40,
            'archive_sha256': 'e' * 64, 'kit': '', 'engine': 'pdflatex', 'year': 2026,
            'files': {'acl.sty': hashlib.sha256(sty.encode()).hexdigest()},
            'fetched_at': __import__('time').time(), 'page_limit': 8,
            'page_limit_source': 'test', 'anonymity': 'test',
            'unlimited_sections': ['Limitations'], 'forbidden_packages': []}


@pytest.mark.skipif(not _tex_ready(), reason='latexmk with pgfplots/natbib not installed')
def test_full_pipeline_delivers_and_resumes(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(FIXTURES))
    from build_sample_paper import build
    paper = build(tmp_path / 'paper')

    calls = []
    monkeypatch.setattr(pl, '_fetch_kit', _fake_acl_kit)
    monkeypatch.setattr(pl, '_academic_search', lambda q: (
        [{'title': 'Close 2025 Work', 'year': 2025, 'arxiv_id': '2501.00001',
          'authors': ['A. Author'], 'abstract': 'stochastic rounding'}],
        [{'source': 'arxiv', 'status': 'available'}]))
    monkeypatch.setattr(pl, '_chat', lambda ctx, role, messages, label: (
        calls.append(label), _fake_chat(ctx, role, messages, label))[1])
    artifacts = pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert artifacts['render']['failures'] == []
    assert artifacts['revise']['adopted'] is True
    assert artifacts['blind']['preferred'] is True
    assert artifacts['critique']['verdict']['calibrated'] in (True, False)
    manifest = json.loads((paper / 'delivered' / 'manifest.json').read_text())
    assert manifest['blind']['verdict'] == 'candidate'
    delivered_bytes = (paper / 'delivered' / manifest['pdf']).read_bytes()
    assert delivered_bytes == Path(artifacts['render']['pdf']).read_bytes()
    assert hashlib.sha256(delivered_bytes).hexdigest() == artifacts['render'][
        'conformance']['pdf_sha256'] == artifacts['render']['pdf_sha256']
    assert manifest['provenance']['unsourced'] == [] and manifest['provenance']['checked']['numbers']
    assert (paper / 'delivered' / 'experiment_requests.json').is_file()
    for index, name in enumerate(pl.STAGE_NAMES, 1):
        assert (tmp_path / 'work' / 'artifacts' / f'{index:02d}-{name}.json').is_file()
    first_run = len(calls)
    pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert all(label in ('render-repair', 'blind-ab') for label in calls[first_run:]), (
        'writing stages resume, but render/delivery must revalidate current official rules')
    before = hashlib.sha256((paper / 'delivered' / manifest['pdf']).read_bytes()).hexdigest()
    monkeypatch.setattr(pl, '_check_freshness', lambda eid: {
        'conference': eid, 'verdict': 'unverifiable', 'archive': {'error': 'offline'}})
    with pytest.raises(StageFailed):
        pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert hashlib.sha256((paper / 'delivered' / manifest['pdf']).read_bytes()).hexdigest() == before


@pytest.mark.skipif(not _tex_ready(), reason='latexmk with pgfplots/natbib not installed')
def test_blind_gate_refuses_delivery_when_previous_wins(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(FIXTURES))
    from build_sample_paper import build
    paper = build(tmp_path / 'paper')
    monkeypatch.setattr(pl, '_fetch_kit', _fake_acl_kit)
    monkeypatch.setattr(pl, '_academic_search', lambda q: (
        [{'title': 'Close 2025 Work', 'year': 2025, 'arxiv_id': '2501.00001'}],
        [{'source': 'arxiv', 'status': 'available'}]))

    def chat(ctx, role, messages, label):
        if label == 'blind-ab':
            pair = json.loads(messages[-1]['content'].split('Candidates:\n', 1)[1])
            return json.dumps({'winner': 1 if 'Possibly' in json.dumps(pair[0]) else 2})
        return _fake_chat(ctx, role, messages, label)
    monkeypatch.setattr(pl, '_chat', chat)
    with pytest.raises(StageFailed) as failure:
        pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert failure.value.stage == 'blind'
    assert not (paper / 'delivered').exists()
    assert (tmp_path / 'work' / 'artifacts' / '10-blind.json').is_file()


# ── venues ──────────────────────────────────────────────────────────────────

def test_unregistered_or_unpinned_venue_is_refused(tmp_path):
    from lib.longform.documents.venue_registry import TemplateRefused
    from lib.longform.documents.venues import fetch_official_kit, venue_terms
    with pytest.raises(TemplateRefused, match='has no official template in the registry'):
        venue_terms('emnlp-findings', year=2026)
    with pytest.raises(TemplateRefused, match='unverified; it has no buildable'):
        fetch_official_kit('icml-2028', tmp_path)


def test_venue_page_limit_bounds_the_brief(tmp_path):
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    kit = dict(_kit(), page_limit=8, page_limit_source='CFP')
    over = dict(ctx['brief'], page_limit=9)
    assert any('exceeds the venue' in e for e in pl._gate_intake(
        ctx, {'brief': over, 'kit': kit, 'run_started': 100.0}))
    unset = {k: v for k, v in ctx['brief'].items() if k != 'page_limit'}
    assert pl._gate_intake(ctx, {'brief': unset, 'kit': kit, 'run_started': 100.0}) == []


def test_document_cli_is_tracked_not_gitignored():
    proc = subprocess.run(['git', 'check-ignore', '-q', 'scripts/document_pipeline.py'],
                          cwd=Path(__file__).resolve().parents[2], check=False)
    assert proc.returncode == 1, 'scripts/document_pipeline.py is gitignored and would not land'


# ── existing drafts ─────────────────────────────────────────────────────────

def test_existing_draft_edits_are_targeted_and_unique():
    from lib.longform.documents import existing_draft as ed
    files = {'main.tex': 'We believe X holds. X holds.', 'sec/a.tex': 'Y.'}
    new, applied, rejected = ed.apply_edits(files, [
        {'old': 'We believe X holds.', 'new': 'X holds.'},
        {'old': 'X holds.', 'new': 'Z.'},
        {'old': 'Y.', 'new': 'Y holds.'}])
    assert new == {'main.tex': 'X holds. X holds.', 'sec/a.tex': 'Y holds.'}
    assert [row['file'] for row in applied] == ['main.tex', 'sec/a.tex']
    assert 'occurs 2 times' in rejected[0]['reason']
    patch = ed.unified_patch(files, new)
    assert '-We believe X holds. X holds.' in patch and '+Y holds.' in patch


def test_existing_draft_sections_and_body_end(tmp_path):
    from lib.longform.documents import existing_draft as ed
    (tmp_path / 'sec').mkdir()
    (tmp_path / 'sec' / 'intro.tex').write_text('Intro text.\n')
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\begin{document}\\begin{abstract}Abs.\\end{abstract}\n'
        '\\section{Introduction}\\input{sec/intro}\n\\section{Results \\& Analysis}R.\n'
        '\\section*{Limitations}L.\n\\bibliography{refs}\n\\appendix\\section{Extra}E.\n'
        '\\end{document}\n')
    rows = ed.sections(ed.expand(tmp_path, 'main.tex'))
    assert [row['id'] for row in rows] == ['abstract', 'introduction', 'results_analysis']
    assert rows[1]['text'] == 'Intro text.'
    marked = ed.mark_body_end((tmp_path / 'main.tex').read_text(), 'END', ('Limitations',))
    assert marked.index('\\label{END}') < marked.index('\\section*{Limitations}')


def test_writer_model_is_recorded_and_never_switched_silently(tmp_path, monkeypatch):
    ctx = {'workdir': str(tmp_path), 'brief': {'writer_model': 'w', 'judge_model': 'j'}}
    monkeypatch.setattr(pl, '_llm_chat', lambda messages, **k: ('ok', {'model': k['prefer_model']}))
    assert pl._chat(ctx, 'writer', [{'role': 'user', 'content': 'x'}], 'draft:intro') == 'ok'
    rows = json.loads((tmp_path / 'artifacts' / 'models.json').read_text())
    assert rows[0] | {'at': 0} == {'stage_call': 'draft:intro', 'role': 'writer',
                                   'requested': 'w', 'reported': 'w', 'at': 0}
    monkeypatch.setattr(pl, '_llm_chat', lambda messages, **k: ('ok', {'model': 'other'}))
    with pytest.raises(RuntimeError, match='refusing a silent switch'):
        pl._chat(ctx, 'writer', [{'role': 'user', 'content': 'x'}], 'outline')
    monkeypatch.setattr(pl, '_llm_chat', lambda messages, **k: (
        '', {'model': 'w', 'finish_reason': 'content_filter'}))
    with pytest.raises(RuntimeError, match='another model family'):
        pl._chat(ctx, 'writer', [{'role': 'user', 'content': 'x'}], 'outline')
    unnamed = {'workdir': str(tmp_path), 'brief': {'judge_model': 'j'}}
    seen = {}
    monkeypatch.setattr(pl, '_llm_chat', lambda messages, **k: (
        seen.update(k), ('ok', {'model': 'routed'}))[1])
    assert pl._chat(unnamed, 'writer', [{'role': 'user', 'content': 'x'}], 'outline') == 'ok'
    assert seen['prefer_model'] is None and seen['strict_model'] is False
    monkeypatch.setattr(pl, '_llm_chat', lambda messages, **k: ('ok', {'model': 'j'}))
    with pytest.raises(RuntimeError, match='routing default writer is the judge'):
        pl._chat(unnamed, 'writer', [{'role': 'user', 'content': 'x'}], 'outline')


_DRAFT_CLAIM = ('Stochastically rounding only the 8.7\\% of parameters that fail the test '
                'reaches validation loss 3.125, against 3.124 for stochastic rounding '
                'everywhere and 3.112 for FP32 master weights.')


def _draft_chat(calls):
    def chat(ctx, role, messages, label):
        calls.append(label)
        content = messages[-1]['content']
        if label == 'evidence-draft':
            return json.dumps([
                {'quote': _DRAFT_CLAIM, 'section': 'abstract', 'evidence': ['tab:loss'],
                 'strength': 'strong', 'rationale': 'table'},
                {'quote': 'A half-ulp test predicts stale updates with precision 0.98 and '
                          'recall 0.96.', 'section': 'abstract', 'evidence': [],
                 'strength': 'strong', 'rationale': 'no table shows it'}])
        if label == 'novelty':
            return json.dumps([{'cid': 'c1', 'what_they_do': 'SR everywhere',
                                'what_we_add': 'only flagged parameters',
                                'evidence': ['draft-1'], 'delta_clear': True}])
        if label == 'outline':
            ids = content.split('Section ids: ', 1)[1].split('\n', 1)[0].split(', ')
            return json.dumps({'sections': [{'id': sid, 'takeaway': f'point of {sid}'}
                                            for sid in ids]})
        if label == 'critique':
            return json.dumps({
                'scores': {name: 6 for name, _m in document_type('paper').rubric},
                'overall': 5, 'checklist': [{'rule': r.id, 'pass': True, 'quote': 'q'}
                                            for r in rules_for('paper') if r.kind == 'checklist'],
                'findings': [{'section': 'introduction', 'issue': 'hedged',
                              'fix': 'state it'}]})
        if label == 'revise:introduction':
            return json.dumps([
                {'old': 'but it seems to stall.', 'new': 'but it stalls.'},
                {'old': 'We believe the reason is simple: an', 'new': 'The reason is simple: an'},
                {'old': 'Pure BF16 training removes', 'new': 'It reaches loss 9.999 and removes'}])
        if label == 'revise:abstract':
            return json.dumps([
                {'old': 'is believed to lose accuracy, and we think the cause might be rounding.',
                 'new': 'loses accuracy, and rounding is the cause.'},
                {'old': _DRAFT_CLAIM, 'new': 'Selective rounding works.'}])
        if label in ('revise-ab', 'blind-ab'):
            pair = json.loads(content.split('Candidates:\n', 1)[1])
            newer = 0 if 'is believed to lose accuracy' not in ' '.join(
                json.dumps(pair[0]).replace('\\n', ' ').split()) else 1
            scores = [{'novelty': 5}, {'novelty': 5}]
            scores[newer] = {'novelty': 8}
            return json.dumps({'winner': newer + 1, 'reason': 'crisper', 'scores': scores})
        raise AssertionError(f'unexpected model call {label}')
    return chat


@pytest.mark.parametrize('label', ['revise-ab', 'blind-ab'])
@pytest.mark.parametrize('candidate_position', [0, 1])
def test_draft_chat_identifies_revised_fixture_in_both_orders(label, candidate_position):
    previous = (FIXTURES / 'sample_paper' / 'draft' / 'main.tex').read_text()
    old = 'is believed to lose accuracy, and we think the cause might be rounding.'
    new = 'loses accuracy, and rounding is the cause.'
    assert old in previous
    assert 'seems to stall' not in previous
    candidate = previous.replace(old, new)
    pair = [{'text': previous}, {'text': previous}]
    pair[candidate_position] = {'text': candidate}
    if label == 'blind-ab':
        pair = [{'text': row['text'].replace('is believed to lose', 'is believed to\nlose')}
                for row in pair]
    result = json.loads(_draft_chat([])(None, 'judge', [
        {'content': 'Candidates:\n' + json.dumps(pair)}], label))
    assert result['winner'] == candidate_position + 1
    assert result['scores'][candidate_position] == {'novelty': 8}
    assert result['scores'][1 - candidate_position] == {'novelty': 5}


@pytest.mark.skipif(not _tex_ready(), reason='latexmk with pgfplots/natbib not installed')
def test_existing_draft_is_revised_in_place_by_targeted_edits(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(FIXTURES))
    from build_sample_paper import build_draft
    paper = build_draft(tmp_path / 'paper')
    original = (paper / 'main.tex').read_text()
    figure_bytes = (paper / 'figures' / 'loss_curve.pdf').read_bytes()
    calls = []
    monkeypatch.setattr(pl, '_fetch_kit', _fake_acl_kit)
    monkeypatch.setattr(pl, '_academic_search', lambda q: (
        [{'title': 'Close 2025 Work', 'year': 2025, 'arxiv_id': '2501.00001'}],
        [{'source': 'arxiv', 'status': 'available'}]))
    monkeypatch.setattr(pl, '_chat', _draft_chat(calls))
    artifacts = pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert not any(label.startswith('draft:') for label in calls), 'nothing is rewritten'
    evidence = artifacts['evidence']
    gap = next(row for row in evidence['rows'] if row['claim_id'] == 'draft-2')
    assert gap['strength'] == 'none'
    assert any(req['claim_id'] == 'draft-2' for req in evidence['requests'])
    revise = artifacts['revise']
    assert revise['adopted'] is True
    refused = [row for row in revise['rejected_edits'] if '9.999' in row['new']]
    assert refused and 'no-invented-numbers' in refused[0]['reason']
    dropped = [row for row in revise['rejected_edits'] if row['new'] == 'Selective rounding works.']
    assert dropped and 'drops supported result' in dropped[0]['reason']
    assert artifacts['render']['failures'] == []
    assert artifacts['render']['replaced_template_files'] == ['acl.sty']
    assert artifacts['blind']['preferred'] is True
    delivered = paper / 'delivered'
    revised = (delivered / 'source' / 'main.tex').read_text()
    assert 'loses accuracy, and rounding is the cause.' in revised
    assert 'is believed to lose accuracy' not in revised
    assert 'tofu-pipeline-body-end' not in revised and '9.999' not in revised
    assert _DRAFT_CLAIM in revised, 'a supported result survives revision'
    assert (delivered / 'source' / 'figures' / 'loss_curve.pdf').read_bytes() == figure_bytes
    assert (paper / 'main.tex').read_text() == original, 'the owner\'s draft is untouched'
    patch = (delivered / 'revision.patch').read_text()
    assert '-Pure BF16 training without an FP32 master copy is believed' in patch
    assert '+Pure BF16 training without an FP32 master copy loses accuracy' in patch
    assert '9.999' not in patch
    assert '+' + _DRAFT_CLAIM not in patch, 'unflagged sentences are left alone'
    manifest = json.loads((delivered / 'manifest.json').read_text())
    assert manifest['mode'] == 'revised existing draft'
    assert manifest['replaced_template_files'] == ['acl.sty']


def test_render_judges_only_sources_the_document_inputs(tmp_path):
    from lib.longform.documents import existing_draft as ed
    (tmp_path / 'sec').mkdir()
    (tmp_path / 'kit_sample').mkdir()
    (tmp_path / 'main.tex').write_text('\\input{sec/a}\n\\include{sec/b.tex}\n')
    (tmp_path / 'sec' / 'a.tex').write_text('\\input{c}\n')
    (tmp_path / 'sec' / 'b.tex').write_text('B')
    (tmp_path / 'c.tex').write_text('C')
    (tmp_path / 'kit_sample' / 'sample.tex').write_text('\\includegraphics{myfile}')
    assert ed.reachable_tex(tmp_path, 'main.tex') == {'main.tex', 'sec/a.tex', 'sec/b.tex',
                                                      'c.tex'}


# ── pilot defects (mechanism paper, 2026-11-18) ─────────────────────────────

def _intake_for(ctx, **draft):
    ctx['artifacts']['intake'] = {
        'brief': ctx['brief'], 'kit': _kit(), 'owner_directives': {'items': []},
        'rules': [rule.id for rule in rules_for(ctx['doc_type'].name)],
        'draft': draft or None}
    return ctx


def test_dossier_stage_reruns_when_dossier_json_or_a_cited_file_changes(tmp_path):
    paper = _sample(tmp_path)
    ctx = _intake_for(_ctx(paper, tmp_path))
    state = tmp_path / 'work' / 'state' / 'dossier.json'

    def dossier_stage():
        pl.run_workflow(pl._workflow('dossier', ('dossier',),
                                     {'dossier': pl._input_version(ctx)}),
                        ctx, state_path=str(state))
        return ctx['artifacts']['dossier']
    first = dossier_stage()
    raw = json.loads((paper / 'dossier.json').read_text())
    raw['facts'][0]['text'] += ' Edited after the first run.'
    (paper / 'dossier.json').write_text(json.dumps(raw))
    second = dossier_stage()
    assert second['facts'][0]['text'].endswith('Edited after the first run.'), \
        'an edited dossier.json must rerun the dossier stage'
    assert second['digest'] != first['digest']
    before = pl._input_version(ctx)
    summary = paper / 'results' / 'summary.json'
    summary.write_text(summary.read_text() + '\n')
    assert pl._input_version(ctx) != before, 'a cited source file is a stage input'


def test_reading_text_expands_the_papers_own_number_macros():
    from lib.longform.documents import existing_draft as ed
    tex = ('\\makeatletter\n'
           '\\newcommand{\\nm}[1]{\\ifcsname nm@#1\\endcsname\\csname nm@#1\\endcsname'
           '\\else\\errmessage{undefined number #1}\\fi}\n'
           '\\expandafter\\def\\csname nm@gap.q06b\\endcsname{0.42}\n'
           '\\newcommand{\\method}{SwiftRound}\n\\newcommand{\\pct}[1]{#1\\%}\n'
           '\\begin{document}\\method{} closes \\nm{gap.q06b} of the gap '
           '(\\pct{\\nm{gap.q06b}}); \\nm{missing.key} stays.\\end{document}')
    macros = ed.macros(tex)
    body = ed.expand_macros(tex.split('\\begin{document}')[1], macros)
    assert 'SwiftRound closes 0.42 of the gap (0.42\\%)' in body
    assert '\\nm{missing.key}' in body, 'an undefined key is left visible, not invented'


_MACRO_DRAFT = (
    '\\documentclass{article}\n\\input{numbers}\n\\begin{document}\n'
    '\\begin{abstract}bf16 realizes only \\nm{cv.ratio} of the fp32 displacement.'
    '\\end{abstract}\n'
    '\\section{Introduction}\\label{sec:intro}\nbf16 realizes only \\nm{cv.ratio} of the '
    'fp32 displacement (\\cref{fig:a,tab:b}). We interpret this as a storage deficit.\n'
    '\\begin{figure}\\includegraphics{a.pdf}\\caption{A}\\label{fig:a}\\end{figure}\n'
    '\\begin{table}\\caption{B}\\label{tab:b}\\end{table}\n'
    '\\section{Results}\\label{sec:results}\nThe gap is \\nm{gap} EM.\n'
    '\\bibliography{refs}\n\\end{document}\n')
_NUMBERS_TEX = ('\\makeatletter\n\\newcommand{\\nm}[1]{\\ifcsname nm@#1\\endcsname'
                '\\csname nm@#1\\endcsname\\else\\errmessage{undefined number #1}\\fi}\n'
                '\\expandafter\\def\\csname nm@cv.ratio\\endcsname{0.31}\n'
                '\\expandafter\\def\\csname nm@gap\\endcsname{0.457}\n\\makeatother\n')


def _draft_ctx(tmp_path, *, role='', main=_MACRO_DRAFT, extra_files=None):
    from lib.longform.documents import existing_draft as ed
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    ctx['brief'] = dict(ctx['brief'], draft={'main': 'main.tex'}, **({'role': role} if role else {}))
    source = Path(ctx['workdir']) / 'source'
    source.mkdir(parents=True)
    (source / 'main.tex').write_text(main)
    (source / 'numbers.tex').write_text(_NUMBERS_TEX)
    for rel, text in (extra_files or {}).items():
        (source / rel).write_text(text)
    _intake_for(ctx, main='main.tex', files={}, stale_template_files=[], pdf='')
    ctx['artifacts']['dossier'] = {'facts': [], 'figures': [], 'bib': '', 'bib_keys': [],
                                   'number_gaps': {}, 'awaiting_owner': []}
    ctx['artifacts']['draft'] = pl._run_draft_existing(ctx)
    ctx['artifacts']['outline'] = {'existing': True, 'sections': [
        {'id': row['id'], 'title': row['title'], 'takeaway': 't', 'facts': [], 'figures': []}
        for row in ed.sections(pl._draft_text(ctx))]}
    return ctx


def test_review_and_evidence_read_expanded_numbers_not_placeholders(tmp_path, monkeypatch):
    ctx = _draft_ctx(tmp_path)
    seen = {}

    def chat(ctx_, role, messages, label):
        text = '\n'.join(m['content'] for m in messages)
        seen[label] = text
        if label == 'critique':
            placeholder = '\\nm{' in text
            return json.dumps({
                'scores': {name: 6 for name, _m in document_type('paper').rubric},
                'overall': 5, 'checklist': [{'rule': r.id, 'pass': not placeholder, 'quote': 'q'}
                                            for r in rules_for('paper') if r.kind == 'checklist'],
                'findings': ([{'section': 'abstract', 'issue': 'unresolved placeholders',
                               'fix': 'render the macros'}] if placeholder else [])})
        if label == 'evidence-draft':
            return json.dumps([{'quote': 'bf16 realizes only 0.31 of the fp32 displacement',
                                'section': 'introduction', 'evidence': ['fig:a'],
                                'strength': 'strong', 'rationale': 'figure'}])
        raise AssertionError(label)
    monkeypatch.setattr(pl, '_chat', chat)
    critique = pl._run_critique(ctx)
    assert '\\nm{' not in seen['critique'] and '0.31' in seen['critique']
    assert critique['review']['findings'] == []
    ctx['artifacts']['evidence'] = {}
    evidence = pl._run_evidence(ctx)
    assert '\\nm{' not in seen['evidence-draft'].split('Draft:', 1)[1]
    assert 'csname' not in seen['evidence-draft'], 'the definitions table is not prose'
    calibrated = next(r for r in rules_for('paper') if r.id == 'paper.calibrated-claims')
    assert calibrated.origin in seen['critique'], 'the reviewer sees the owner\'s words'
    assert not [e for e in pl._gate_evidence(ctx, evidence) if 'verbatim' in e]


def test_ab_judge_is_style_blind_task_venue_and_documents_only(tmp_path, monkeypatch):
    """A judge told the registry rules rewards rule-following itself (21-4-1
    vs 13-3-10 style-blind on 26 drafts, docs/PAPER_STYLE_EVIDENCE.md), so the
    delivery and adoption A/B never sees a rule; writer, reviewer and lints do."""
    ctx = _ctx(_sample(tmp_path), tmp_path)
    ctx['brief'] = {**ctx['brief'], 'venue': 'ICML', 'page_limit': 8}
    prompts = []
    monkeypatch.setattr(pl, '_chat', lambda c, role, messages, label: (
        prompts.append(messages[-1]['content']),
        json.dumps({'winner': 1, 'reason': 'r', 'scores': [{}, {}]}))[1])
    pl._judge_pair(ctx, 'blind-ab')([{'text': 'first doc'}, {'text': 'second doc'}])
    prompt = prompts[0]
    for type_name in ('paper', 'resume', 'longform'):
        for rule in rules_for(type_name):
            assert rule.id not in prompt and rule.text not in prompt, rule.id
            assert rule.origin not in prompt, rule.id
    assert 'rule' not in prompt.lower()
    assert ctx['doc_type'].reviewer in prompt
    assert ctx['brief']['title'] in prompt and ctx['brief']['goal'] in prompt
    assert 'ICML' in prompt and '8 body pages' in prompt
    assert 'first doc' in prompt and 'second doc' in prompt


def test_position_biased_blind_judge_reports_no_preference_with_both_scores():
    always_second = lambda pair: (1, 'second reads better', [{'clarity': 7}, {'clarity': 8}])
    result = blind_pairwise(always_second, {'v': 'new'}, {'v': 'old'}, seed='s')
    assert result['verdict'] == 'no preference' and not result['preferred']
    assert result['position_bias'] is True
    assert len(result['rounds']) == 2
    assert all(row['candidate_scores'] and row['baseline_scores'] for row in result['rounds'])
    assert result['totals'] == {'candidate': 15, 'baseline': 15}


def test_blind_report_states_no_preference_and_both_orders(tmp_path, monkeypatch):
    from lib.longform.documents import render as render_mod
    ctx = _ctx(_sample(tmp_path), tmp_path)
    new_pdf, old_pdf = tmp_path / 'new.pdf', tmp_path / 'old.pdf'
    new_pdf.write_bytes(b'new')
    old_pdf.write_bytes(b'old')
    ctx['artifacts']['render'] = {'pdf': str(new_pdf), 'baseline_pdf': str(old_pdf),
                                  'pdf_sha256': 'x'}
    monkeypatch.setattr(render_mod, 'pdf_pages', lambda p: 9)
    monkeypatch.setattr(render_mod, 'pdf_text', lambda p: Path(p).read_text())
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(
        {'winner': 2, 'reason': 'second', 'scores': [{'clarity': 7}, {'clarity': 8}]}))
    art = pl._run_blind(ctx)
    md = (Path(ctx['workdir']) / 'artifacts' / '10-blind.md').read_text()
    assert 'no preference' in md and 'position' in md
    assert md.count("{'clarity'") == 4
    errors = pl._gate_blind(ctx, art)
    assert errors and 'no preference' in errors[0] and 'position' in errors[0]


def _float_draft():
    """Five floats nobody references, like the mechanism paper's render failure."""
    floats = ''.join(f'\\begin{{table}}[t]\\caption{{T{i}}}\\label{{tab:t{i}}}\\end{{table}}\n'
                     for i in range(5))
    prose = ''.join(f'Claim {i} holds with a gap of \\nm{{gap}} EM in setting {i}.\n'
                    for i in range(21))
    return ('\\documentclass{article}\n\\input{numbers}\n\\begin{document}\n'
            '\\section{Results}\\label{sec:results}\n' + prose + floats
            + '\\bibliography{refs}\n\\end{document}\n')


def _final_repair_reply():
    refs = [{'section': 'results', 'old': f'Claim {i} holds with a gap of \\nm{{gap}} EM in '
             f'setting {i}.', 'new': f'Claim {i} holds with a gap of \\nm{{gap}} EM in '
             f'setting {i} (\\cref{{tab:t{i}}}).'} for i in range(5)]
    rewrites = [{'section': 'results', 'old': f'Claim {i} holds with a gap of \\nm{{gap}} EM '
                 f'in setting {i}.', 'new': f'Setting {i} shows the claim.'}
                for i in range(5, 21)]
    rewrites += [{'section': 'results', 'old': f'setting {i} (\\cref{{tab:t{i}}})',
                  'new': f'its setting (\\cref{{tab:t{i}}})'} for i in range(5)]
    placement = [{'section': 'results', 'old': '\\begin{table}[t]\\caption{T0}',
                  'new': '\\begin{table}[!htbp]\\caption{T0}'}]
    return refs + placement + rewrites


def test_final_render_repair_fixes_floats_and_refuses_prose_rewrites(tmp_path, monkeypatch):
    ctx = _draft_ctx(tmp_path, role='final', main=_float_draft())
    files = dict(ctx['artifacts']['draft']['files'])
    prompts = []
    monkeypatch.setattr(pl, '_chat', lambda c, role, messages, label: (
        prompts.append(messages[-1]['content']), json.dumps(_final_repair_reply()))[1])
    failures = [f'figure_not_referenced: \\label{{tab:t{i}}} inside a table is never '
                'referenced' for i in range(5)]
    new, applied, refused = pl._repair_edits(ctx, files, failures, 'graphicx')
    assert len(applied) == 6, refused
    assert sum('\\cref{tab:t' in row['new'] for row in applied) == 5
    assert '\\begin{table}[!htbp]\\caption{T0}' in new['main.tex'], 'float placement is layout'
    prose = [row for row in refused if 'role=final' in row['reason']]
    assert len(prose) == 21, refused
    assert 'never reword' in prompts[0]
    assert 'Setting 7 shows the claim.' not in new['main.tex']


def test_brief_schema_knows_role_and_source_and_refuses_unknown_fields(tmp_path):
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    (Path(ctx['workdir']) / 'source').mkdir(parents=True)
    (Path(ctx['workdir']) / 'source' / 'main.tex').write_text('x')
    (paper / 'main.tex').write_text('x')
    draft = {'main': 'main.tex', 'files': {}, 'stale_template_files': [], 'pdf': ''}
    final = dict(ctx['brief'], role='final', source=str(paper / 'main.tex'),
                 draft={'main': 'main.tex'})
    art = {'brief': final, 'kit': _kit(), 'run_started': 100.0, 'draft': draft}
    assert pl._gate_intake(ctx, art) == []
    assert any('role' in e for e in pl._gate_intake(
        ctx, {**art, 'brief': dict(final, role='polish')}))
    assert any('role=final' in e for e in pl._gate_intake(
        ctx, {**art, 'brief': {k: v for k, v in final.items() if k != 'draft'},
              'draft': None}))
    assert any('source' in e for e in pl._gate_intake(
        ctx, {**art, 'brief': dict(final, source='other.tex')}))
    unknown = pl._gate_intake(ctx, {**art, 'brief': dict(final, role_note='copy edits')})
    assert any("['role_note']" in e for e in unknown)


def test_revise_report_states_dropped_patches_truthfully(tmp_path, monkeypatch):
    ctx = _draft_ctx(tmp_path)
    assert pl._reference_check(ctx, ctx['artifacts']['draft']['sections']) == [], \
        'a cleveref list \\cref{fig:a,tab:b} names two defined labels'
    ctx['artifacts']['critique'] = {'review': {'findings': [
        {'section': 'introduction', 'issue': 'i', 'fix': 'f'}]}}
    ctx['artifacts']['evidence'] = {'rows': []}
    monkeypatch.setattr(pl, '_reference_check', lambda c, s: ['section x references '
                                                              "undefined labels ['fig:z']"])
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps([
        {'old': 'We interpret this as a storage deficit.',
         'new': 'This indicates a storage deficit.'}]))
    art = pl._run_revise_existing(ctx)
    md = (Path(ctx['workdir']) / 'artifacts' / '08-revise.md').read_text()
    assert 'not needed' not in md
    assert '1 edits dropped: reference check' in md and "fig:z" in md
    assert art['dropped'] == {'count': 1, 'reason': 'reference check',
                              'errors': ["section x references undefined labels ['fig:z']"]}


def test_revise_refuses_an_edit_that_adds_an_undefined_reference(tmp_path, monkeypatch):
    ctx = _draft_ctx(tmp_path)
    ctx['artifacts']['evidence'] = {'rows': []}
    _files, applied, rejected = pl._apply_checked_edits(ctx, ctx['artifacts']['draft']['files'], [
        ('results', {'old': 'The gap is', 'new': 'The gap (\\cref{fig:nowhere}) is'}),
        ('results', {'old': 'EM.', 'new': 'EM (\\cref{fig:a}).'})])
    assert len(applied) == 1 and 'fig:nowhere' in rejected[0]['reason']


def test_evidence_cites_excerpts_of_large_files_not_whole_files(tmp_path, monkeypatch):
    paper = _sample(tmp_path)
    ctx = _ctx(paper, tmp_path)
    big = {'runs': [{'seed': i, 'em': round(0.1 + i * 1e-6, 7)} for i in range(120000)],
           'summary': {'q06b': {'bf16': {'em': 0.006}}}}
    (paper / 'results' / 'big.json').write_text(json.dumps(big))
    assert (paper / 'results' / 'big.json').stat().st_size > 3_000_000
    fact = {'id': 'r_big', 'kind': 'result', 'status': 'sourced',
            'text': 'bf16 held-out EM is 0.006.', 'source': {'path': 'results/big.json'}}
    ctx['artifacts']['dossier'] = {'facts': [fact], 'figures': []}
    [material] = pl._evidence_material(ctx, ['r_big'])
    assert 'source_content' not in material
    assert 'summary.q06b.bf16.em = 0.006' in material['source_excerpt']
    assert len(json.dumps(material)) < 2000


def test_oversized_model_request_is_refused_before_it_is_sent(tmp_path, monkeypatch):
    from lib.http_transport import body_policy
    monkeypatch.setattr(body_policy, 'DEFAULT_BODY_CAP', 1000)
    sent = []
    monkeypatch.setattr(pl, '_llm_chat', lambda *a, **k: sent.append(1) or ('ok', {}))
    ctx = {'workdir': str(tmp_path), 'brief': {'judge_model': 'j'}}
    with pytest.raises(RuntimeError, match='body cap'):
        pl._chat(ctx, 'judge', [{'role': 'user', 'content': 'x' * 5000}], 'evidence')
    assert sent == []


def test_final_render_stage_lists_refused_prose_rewrites(tmp_path, monkeypatch):
    from lib.longform.documents import render as render_mod
    ctx = _draft_ctx(tmp_path, role='final', main=_float_draft())
    (Path(ctx['paper_dir']) / 'prev.pdf').write_bytes(b'%PDF')
    intake = ctx['artifacts']['intake']
    intake['kit'] = dict(intake['kit'], files={}, page_limit=8)
    intake['draft']['pdf'] = 'prev.pdf'
    ctx['artifacts']['revise'] = {'files': {}}
    reports = iter([5, 0, 0])

    def fake_compile(build, **_k):  # noqa: ARG001
        n = next(reports)
        return {'compile_status': 'success', 'pdf': 'x.pdf', 'pdf_sha256': str(n),
                'body_pages': 4, 'page_limit': 8, 'overfull': [], 'undefined': [],
                'failures': [f'figure_not_referenced: \\label{{tab:t{i}}}' for i in range(n)]}
    monkeypatch.setattr(render_mod, 'compile_and_check', fake_compile)
    monkeypatch.setattr(pl, '_chat', lambda *a, **k: json.dumps(_final_repair_reply()))
    art = pl._run_render(ctx)
    assert art['failures'] == []
    assert sum(len(log['refused']) for log in art['repair_edits']) == 21
    md = (Path(ctx['workdir']) / 'artifacts' / '09-render.md').read_text()
    assert '## Refused repair edits (21)' in md and md.count('role=final') == 21
    assert 'Setting 7 shows the claim.' not in art['files']['main.tex']
    assert art['files']['main.tex'].count('\\cref{tab:t') == 5


def test_final_revise_applies_copy_edits_only_and_defers_reviewer_findings(tmp_path, monkeypatch):
    main = _MACRO_DRAFT.replace('We interpret this as a storage deficit.',
                                'We do not claim more; we interpret this as a storage deficit.')
    ctx = _draft_ctx(tmp_path, role='final', main=main)
    ctx['artifacts']['critique'] = {'review': {'findings': [
        {'section': 'results', 'issue': 'scope unclear', 'fix': 'add a caveat sentence'}]}}
    ctx['artifacts']['evidence'] = {'rows': []}
    calls = {}

    def chat(c, role, messages, label):
        calls[label] = messages[-1]['content']
        if label == 'revise:introduction':
            return json.dumps([
                {'old': 'We do not claim more; we interpret', 'new': 'We interpret'},
                {'old': 'a storage deficit.',
                 'new': 'a storage deficit. Whether it generalizes remains unresolved.'}])
        if label == 'revise-ab':
            return json.dumps({'winner': 1, 'reason': 'r', 'scores': [{}, {}]})
        raise AssertionError(label)
    monkeypatch.setattr(pl, '_chat', chat)
    art = pl._run_revise_existing(ctx)
    assert 'revise:results' not in calls, 'reviewer findings reword claims; final defers them'
    assert art['deferred_findings'] and art['deferred_findings'][0]['section'] == 'results'
    assert [row['new'] for row in art['applied_edits']] == ['We interpret']
    assert 'sentence' in art['rejected_edits'][0]['reason']
    md = (Path(ctx['workdir']) / 'artifacts' / '08-revise.md').read_text()
    assert 'Reviewer findings not applied (role=final): 1' in md


def test_body_end_mark_ends_the_last_counted_paragraph():
    from lib.longform.documents import existing_draft as ed
    text = 'Conclusion text.\n\n\\section*{Impact Statement}\nI.\n\\bibliography{r}\n'
    marked = ed.mark_body_end(text, 'END', ('Impact Statement',))
    # On its own line before a heading the mark is set on the page where that
    # heading lands, one page late whenever a page break precedes it.
    assert 'Conclusion text.\\label{END}' in marked


def test_page_overrun_names_an_unexempt_unnumbered_section():
    from lib.longform.documents.render import body_end_location, page_overrun_message
    aux = ('\\newlabel{tofu-pipeline-body-end}{{8}{9}{AI Disclosure Statement}'
           '{section*.10}{}}\n')
    assert body_end_location(aux) == (9, 'AI Disclosure Statement', True)
    message = page_overrun_message(aux, 8, ('Impact Statement', 'AI Use Statement'))
    assert message.startswith('body ends on page 9, over the 8-page limit')
    assert "'AI Disclosure Statement'" in message and "'AI Use Statement'" in message
    numbered = '\\newlabel{tofu-pipeline-body-end}{{8}{9}{Conclusion}{section.8}{}}\n'
    assert page_overrun_message(numbered, 8, ()) == 'body ends on page 9, over the 8-page limit'


def test_blind_judges_both_versions_on_the_same_extent(tmp_path, monkeypatch):
    from lib.longform.documents import render as render_mod
    ctx = _ctx(_sample(tmp_path), tmp_path)
    new_pdf, old_pdf = tmp_path / 'new.pdf', tmp_path / 'old.pdf'
    # Two-column extraction found the References heading in one PDF only.
    new_pdf.write_text('Body new.\nReferences\nA. Appendix new.')
    old_pdf.write_text('Body old.   References   A. Appendix old.')
    ctx['artifacts']['render'] = {'pdf': str(new_pdf), 'baseline_pdf': str(old_pdf),
                                  'pdf_sha256': 'x'}
    monkeypatch.setattr(render_mod, 'pdf_pages', lambda p: 9)
    monkeypatch.setattr(render_mod, 'pdf_text', lambda p: Path(p).read_text())
    prompts = []
    monkeypatch.setattr(pl, '_chat', lambda c, role, messages, label: (
        prompts.append(messages[-1]['content']),
        json.dumps({'winner': 1, 'reason': 'r', 'scores': [{}, {}]}))[1])
    art = pl._run_blind(ctx)
    assert all('Appendix new.' in p and 'Appendix old.' in p for p in prompts)
    assert art['extent'] == 'whole documents'


@pytest.mark.parametrize('proof', [None, {}, {'passed': True, 'pdf_sha256': ''}])
def test_paper_delivery_requires_an_inspected_pdf_hash(tmp_path, proof):
    from types import SimpleNamespace
    pdf = tmp_path / 'render.pdf'; pdf.write_bytes(b'%PDF unproven')
    ctx = {'doc_type': SimpleNamespace(needs_venue=True), 'artifacts': {'render': {
        'pdf': str(pdf), 'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
        'conformance': proof}}}
    assert pl._gate_deliver(ctx, {'pdf': str(pdf), 'provenance': {'unsourced': []}}) == [
        'template-conformance PDF hash is missing']


def test_delivery_hash_refusal_happens_before_output_copy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lib.longform.documents import provenance
    pdf = tmp_path / 'render.pdf'; pdf.write_bytes(b'%PDF changed')
    ctx = {'paper_dir': str(tmp_path), 'brief': {'output_dir': 'delivered', 'title': 'Synthetic'},
           'doc_type': SimpleNamespace(needs_venue=True), 'artifacts': {'render': {
               'pdf': str(pdf), 'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
               'conformance': {'pdf_sha256': '0' * 64}}}}
    monkeypatch.setattr(pl, '_delivered_text', lambda ctx: {})
    monkeypatch.setattr(pl, '_provenance_pool', lambda ctx: {})
    monkeypatch.setattr(provenance, 'check', lambda *args: {'unsourced': []})
    monkeypatch.setattr(pl, '_persist', lambda ctx, stage, artifact, **kwargs: artifact)
    monkeypatch.setattr(pl.shutil, 'copy2', lambda *args: pytest.fail('unverified PDF copied'))
    refused = pl._run_deliver(ctx)
    assert refused['refused']
    assert pl._gate_deliver(ctx, refused) == ['delivered PDF differs from the template-conformance PDF']
    assert not (tmp_path / 'delivered').exists()
