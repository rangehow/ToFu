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

FIXTURES = Path(__file__).parent / 'fixtures'


# ── rules ───────────────────────────────────────────────────────────────────

def test_confident_voice_lint_flags_hedges_outside_limitations():
    rules = rules_for('paper')
    text = 'We believe our method is arguably better.'
    findings = lint_text(rules, 'introduction', text)
    assert any("'we believe'" in item for item in findings)
    assert any("'arguably'" in item for item in findings)
    assert lint_text(rules, 'limitations', text) == []
    assert lint_text(rules, 'introduction', 'Our method is better.') == []


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
    assert {'paper.official-template', 'paper.confident-voice', 'paper.figure-qa',
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
                          seed='s')['verdict'] == 'split'
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
    return {'upstream_commit': commit, 'archive_sha256': 'b' * 64, 'files': {'x.sty': 'c'},
            'fetched_at': fetched_at}


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
    assert any('upstream commit' in e for e in unproven)


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


def test_draft_gate_refuses_invented_numbers_hedges_and_unknown_keys(tmp_path, monkeypatch):
    ctx = _with_evidence(_with_dossier(_ctx(_sample(tmp_path), tmp_path)), monkeypatch)
    ctx['artifacts']['outline'] = _outline()
    errors = pl._section_check(ctx, 'introduction',
                               r'We believe loss is 2.718~\citep{nobody2020}.')
    assert any('no-invented-numbers' in e for e in errors)
    assert any('confident-voice' in e for e in errors)
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
    ctx['artifacts']['intake'] = {'kit': {'preamble': '\\usepackage{amsmath}', 'files': {}},
                                  'owner_directives': {'items': []}}
    ctx['artifacts']['revise'] = {'sections': {sid: f'v0 {sid}' for sid, _t in
                                               document_type('paper').sections}}
    reports = iter([3, 1, 2])
    compiled = []

    def fake_compile(build, **_k):
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
    art = pl._run_render(ctx)
    assert repairs == ['render-repair', 'render-repair']
    assert compiled == [3, 1, 2, 1]
    assert art['sections']['method'] == 'v1 method' and art['overfull'] == ['1pt']
    assert pl._gate_render(ctx, art) == ['1 overfull box(es)']


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


@pytest.mark.skipif(not _tex_ready(), reason='latexmk with pgfplots/natbib not installed')
def test_full_pipeline_delivers_and_resumes(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(FIXTURES))
    from build_sample_paper import build
    paper = build(tmp_path / 'paper')

    def fake_kit(venue, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'tofukit.sty').write_text('\\ProvidesPackage{tofukit}\n')
        return {'source_url': 'https://example.invalid/kit.zip', 'upstream_commit': 'f' * 40,
                'archive_sha256': 'e' * 64, 'kit': '', 'engine': 'pdflatex',
                'files': {'tofukit.sty': hashlib.sha256(
                    (destination / 'tofukit.sty').read_bytes()).hexdigest()},
                'fetched_at': __import__('time').time(),
                'preamble': ('\\documentclass[11pt]{article}\n\\usepackage{tofukit}\n'
                             '\\usepackage[margin=1in]{geometry}\n\\usepackage{natbib}\n'
                             '\\usepackage{graphicx}\n\\usepackage{booktabs}\n'),
                'bibstyle': 'plainnat'}
    calls = []
    monkeypatch.setattr(pl, '_fetch_kit', fake_kit)
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
    assert (paper / 'delivered' / 'experiment_requests.json').is_file()
    for index, name in enumerate(pl.STAGE_NAMES, 1):
        assert (tmp_path / 'work' / 'artifacts' / f'{index:02d}-{name}.json').is_file()
    first_run = len(calls)
    pl.run_document_pipeline(str(paper), str(tmp_path / 'work'))
    assert len(calls) == first_run, 'a rerun with unchanged inputs resumes every stage'


@pytest.mark.skipif(not _tex_ready(), reason='latexmk with pgfplots/natbib not installed')
def test_blind_gate_refuses_delivery_when_previous_wins(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(FIXTURES))
    from build_sample_paper import build
    paper = build(tmp_path / 'paper')
    monkeypatch.setattr(pl, '_fetch_kit', lambda venue, destination: (
        destination.mkdir(parents=True, exist_ok=True),
        (destination / 'k.sty').write_text('\\ProvidesPackage{k}\n'),
        {'source_url': 'x', 'upstream_commit': 'f' * 40, 'archive_sha256': 'e' * 64,
         'engine': 'pdflatex', 'fetched_at': __import__('time').time(),
         'files': {'k.sty': hashlib.sha256(b'\\ProvidesPackage{k}\n').hexdigest()},
         'preamble': '\\documentclass{article}\n\\usepackage{k}\n\\usepackage{natbib}\n'
                     '\\usepackage{graphicx}\n', 'bibstyle': 'plainnat'})[2])
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
