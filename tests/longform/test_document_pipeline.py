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
    return {'upstream_commit': commit, 'archive_sha256': 'b' * 64 if commit else '',
            'source_url': 'https://example.invalid/kit.zip', 'files': {'x.sty': 'c'},
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
    ctx['artifacts']['intake'] = {'kit': {'files': {}, 'page_limit': 8},
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


def _fake_acl_kit(venue, destination):
    """A stand-in for the fetched ACL kit (the real fetch is network)."""
    destination.mkdir(parents=True, exist_ok=True)
    sty = ('\\ProvidesPackage{acl}\n\\DeclareOption*{}\n\\ProcessOptions\\relax\n'
           '\\RequirePackage[margin=1in]{geometry}\n\\RequirePackage{natbib}\n'
           '\\bibliographystyle{plainnat}\n')
    (destination / 'acl.sty').write_text(sty)
    return {'venue': 'acl', 'source_url': 'https://example.invalid/kit.zip',
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

def _zip(entries: dict, comment: bytes = b'') -> bytes:
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        archive.comment = comment
    return buffer.getvalue()


_NOW_2026 = 1794960000.0   # 2026-11-17


def _venue_downloads():
    """Each venue's official locations, shaped like the real ones (2026-11-18)."""
    icml = _zip({'icml2027.sty': 's', 'icml2027.bst': 'b', 'fancyhdr.sty': 'f',
                 'example_paper.tex': 't', 'example_paper.bib': ''})
    neurips = _zip({'neurips_2026.sty': 's', 'neurips_2026.tex': 't', 'checklist.tex': 'c'})
    aaai = _zip({'AuthorKit27/aaai2027.sty': 's', 'AuthorKit27/aaai2027.bst': 'b',
                 'AuthorKit27/AnonymousSubmission2027.tex': 't',
                 'AuthorKit27/Figures/figure1.pdf': 'p'})
    cvpr = _zip({'cvpr.sty': 's', 'ieeenat_fullname.bst': 'b', 'preamble.tex': 'p',
                 'main.tex': 't', 'sec/1_intro.tex': 'i'})
    colm = _zip({'Template-master/colm2026_conference.sty': 's',
                 'Template-master/colm2026_conference.bst': 'b',
                 'Template-master/colm2026_conference.tex': 't',
                 'Template-master/math_commands.tex': 'm'}, b'9' * 40)
    iclr = _zip({'Master-Template-master/iclr2026/iclr2026_conference.sty': 'old',
                 'Master-Template-master/iclr2027/iclr2027_conference.sty': 's',
                 'Master-Template-master/iclr2027/iclr2027_conference.tex': 't',
                 'Master-Template-master/iclr2027/math_commands.tex': 'm'}, b'8' * 40)
    pages = {
        'https://icml.cc/Conferences/2027/AuthorInstructions':
            b'<a href="https://media.icml.cc/Conferences/ICML2027/styles/icml2027.zip">',
        'https://media.icml.cc/Conferences/ICML2027/styles/icml2027.zip': icml,
        'https://neurips.cc/Conferences/2026/CallForPapers':
            b'<a href="https://media.neurips.cc/Conferences/NeurIPS2026/'
            b'Formatting_Instructions_For_NeurIPS_2026.zip">',
        'https://media.neurips.cc/Conferences/NeurIPS2026/'
        'Formatting_Instructions_For_NeurIPS_2026.zip': neurips,
        'https://aaai.org/authorkit27/': aaai,
        'https://cvpr.thecvf.com/Conferences/2027/AuthorGuidelines':
            b'<a href="https://media.eventhosts.cc/Conferences/CVPR2027/'
            b'author-kit-CVPR2027-v1-latex.zip">',
        'https://media.eventhosts.cc/Conferences/CVPR2027/author-kit-CVPR2027-v1-latex.zip': cvpr,
        'https://github.com/COLM-org/Template/archive/refs/heads/master.zip': colm,
        'https://github.com/ICLR/Master-Template/archive/refs/heads/master.zip': iclr,
    }
    asked = []

    def download(url):
        asked.append(url)
        if url not in pages:
            raise RuntimeError(f'{url}: HTTP 404')
        return pages[url], url
    return download, asked


@pytest.mark.parametrize('venue, year, kit_file, sample, limit', [
    ('icml', 2027, 'icml2027.sty', 'example_paper.tex', 8),
    ('neurips', 2026, 'neurips_2026.sty', 'neurips_2026.tex', 9),
    ('aaai', 2027, 'aaai2027.sty', 'AnonymousSubmission2027.tex', 7),
    ('cvpr', 2027, 'cvpr.sty', 'main.tex', 8),
    ('colm', 2026, 'colm2026_conference.sty', 'colm2026_conference.tex', 9),
    ('iclr', 2027, 'iclr2027_conference.sty', 'iclr2027_conference.tex', 9),
])
def test_venue_kit_is_the_newest_official_one(tmp_path, venue, year, kit_file, sample, limit):
    from lib.longform.documents.venues import fetch_official_kit
    download, asked = _venue_downloads()
    kit = fetch_official_kit(venue, tmp_path / venue, download=download, now=_NOW_2026)
    assert kit['year'] == year and kit_file in kit['files']
    assert kit['page_limit'] == limit and kit['anonymity'] and kit['page_limit_source']
    assert kit['sample'] == sample
    assert (Path(kit['sample_dir']) / sample).is_file()
    assert (tmp_path / venue / kit_file).read_text() == 's'
    if venue == 'neurips':   # the coming year is probed first, then this year
        assert asked[0] == 'https://neurips.cc/Conferences/2027/CallForPapers'
    if venue == 'iclr':
        assert 'iclr2026_conference.sty' not in kit['files']
        assert kit['upstream_commit'] == '8' * 40


def test_unregistered_or_unpublished_venue_is_refused(tmp_path):
    from lib.longform.documents.venues import fetch_official_kit
    with pytest.raises(ValueError, match='no registered official'):
        fetch_official_kit('emnlp-findings', tmp_path)
    with pytest.raises(ValueError, match='no official icml kit'):
        fetch_official_kit('icml', tmp_path, download=lambda url: (_ for _ in ()).throw(
            RuntimeError(f'{url}: HTTP 404')), now=_NOW_2026)


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
            newer = 0 if 'seems to stall' not in json.dumps(pair[0]) \
                and 'seems to\\nstall' not in json.dumps(pair[0]) else 1
            scores = [{'novelty': 5}, {'novelty': 5}]
            scores[newer] = {'novelty': 8}
            return json.dumps({'winner': newer + 1, 'reason': 'crisper', 'scores': scores})
        raise AssertionError(f'unexpected model call {label}')
    return chat


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
    assert 'but it stalls.' in revised and 'seems to stall' not in revised
    assert 'tofu-pipeline-body-end' not in revised and '9.999' not in revised
    assert _DRAFT_CLAIM in revised, 'a supported result survives revision'
    assert (delivered / 'source' / 'figures' / 'loss_curve.pdf').read_bytes() == figure_bytes
    assert (paper / 'main.tex').read_text() == original, 'the owner\'s draft is untouched'
    patch = (delivered / 'revision.patch').read_text()
    assert 'seems to stall' in patch and '9.999' not in patch
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
