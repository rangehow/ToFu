"""Executable specification of the per-brief venue terms on the pinned
template registry.

The facts below were re-read from each venue's official source on 2026-11-18
and are pinned in ``lib/longform/documents/venue_templates.json``: ICLR 2027's
9/10-page limits and its exempt statements, NeurIPS 2026's Evaluations &
Datasets track option, ACL's short/long limits, ICML 2027's extra
camera-ready page and its two official AI-statement headings, COLM's 2026
tag, CVPR 2027's mandatory LLM Usage Statement.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.longform.documents import pipeline as pl
from lib.longform.documents import venue_registry as reg
from lib.longform.documents import venues as vn
from lib.longform.documents.types import document_type

pytestmark = pytest.mark.unit


# ── page limits by paper type, stage and year ───────────────────────────────

@pytest.mark.parametrize('venue, year, paper_type, stage, pages', [
    ('iclr', 2027, None, 'submission', 9),
    ('iclr', 2027, None, 'rebuttal', 10),
    ('iclr', 2027, None, 'camera_ready', 10),
    ('arr', None, None, 'submission', 8),
    ('acl', 2027, 'long', 'camera_ready', 9),
    ('naacl', 2027, 'short', 'submission', 4),
    ('eacl', 2027, 'short', 'camera_ready', 5),
    ('icml', 2027, None, 'submission', 8),
    ('icml', 2027, None, 'camera_ready', 9),
    ('neurips', 2026, None, 'submission', 9),
    ('neurips', 2026, None, 'camera_ready', 10),
    ('colm', 2026, None, 'submission', 9),
    ('cvpr', 2027, None, 'submission', 8),
    ('aaai', 2027, None, 'submission', 7),
    ('ijcai', 2027, None, 'submission', 7),
])
def test_page_limit_follows_paper_type_stage_and_year(venue, year, paper_type, stage, pages):
    terms = vn.venue_terms(venue, year=year, paper_type=paper_type, stage=stage)
    assert terms['page_limit'] == pages
    assert terms['stage'] == stage and terms['submission_ready'] is True


def test_unpublished_limits_and_unknown_choices_are_refused():
    with pytest.raises(ValueError, match="'short' is not one of \\['regular'\\]"):
        vn.venue_terms('iclr', year=2027, paper_type='short')
    with pytest.raises(ValueError, match="'poster' is not one of \\['long', 'short'\\]"):
        vn.venue_terms('arr', paper_type='poster')
    with pytest.raises(ValueError, match='no rebuttal page limit'):
        vn.venue_terms('icml', year=2027, stage='rebuttal')
    # COLM 2026's call states no camera-ready length; none is invented.
    with pytest.raises(ValueError, match='no camera_ready page limit'):
        vn.venue_terms('colm', year=2026, stage='camera_ready')
    with pytest.raises(ValueError, match="track 'datasets'"):
        vn.venue_terms('neurips', year=2026, track='datasets')
    with pytest.raises(ValueError, match='has no tracks'):
        vn.venue_terms('icml', year=2027, track='eandd')


# ── the year is explicit; another year's template is never used silently ──

def test_a_year_addressed_venue_needs_the_year():
    with pytest.raises(reg.TemplateRefused, match='"venue_year" is required'):
        vn.venue_terms('icml')


def test_an_unreleased_year_is_refused_naming_the_evidence():
    with pytest.raises(reg.TemplateRefused) as refusal:
        vn.venue_terms('neurips', year=2027)
    message = str(refusal.value)
    assert 'unable to verify template availability' in message
    assert 'https://neurips.cc/Conferences/2027/CallForPapers -> HTTP 404' in message
    assert '"template_draft_on": "neurips-2026"' in message


def test_a_year_missing_from_the_registry_is_refused():
    with pytest.raises(reg.TemplateRefused, match='no icml 2031 template is pinned'):
        vn.venue_terms('icml', year=2031)


def test_previous_year_draft_is_labelled_and_never_submission_ready():
    terms = vn.venue_terms('neurips', year=2027, draft_on='neurips-2026')
    assert terms['edition'] == 'neurips-2026' and terms['target_edition'] == 'neurips-2027'
    assert terms['submission_ready'] is False
    assert terms['draft_label'].startswith('DRAFT on the NeurIPS 2026 template')
    with pytest.raises(reg.TemplateRefused, match='pinned earlier icml edition'):
        vn.venue_terms('icml', year=2028, draft_on='neurips-2026')
    with pytest.raises(reg.TemplateRefused, match='remove template_draft_on'):
        vn.venue_terms('icml', year=2027, draft_on='icml-2027')


def test_superseded_and_unpinned_editions_are_refused():
    with pytest.raises(reg.TemplateRefused, match='EMNLP 2026 is superseded'):
        vn.venue_terms('emnlp', year=2026)
    with pytest.raises(reg.TemplateRefused, match='KDD 2027: the official template is not '
                       'pinned'):
        vn.venue_terms('kdd', year=2027)


# ── exempt and required statements by their official words ────────────────

def _edition(eid):
    return reg.edition(eid)


def test_iclr_exempts_its_required_ai_use_statement_by_its_official_heading():
    ed = _edition('iclr-2027')
    assert ed['exempt_sections']['headings'][0] == 'AI use statement'
    assert {'Ethics statement', 'Reproducibility statement'} <= set(
        ed['exempt_sections']['headings'])
    assert [row['heading'] for row in ed['required_sections']] == ['AI use statement']


def test_body_end_stops_at_an_exempt_unnumbered_subsection_in_any_case():
    from lib.longform.documents import existing_draft as ed
    exempt = tuple(_edition('iclr-2027')['exempt_sections']['headings'])
    text = ('\\section{Conclusion}\nDone.\n\n\\subsection*{AI use statement}\nWe used.\n'
            '\\subsection*{Ethics statement}\nE.\n\\bibliography{r}\n')
    assert 'Done.\\label{END}' in ed.mark_body_end(text, 'END', exempt)
    upper = text.replace('AI use statement', 'AI Use Statement')
    assert 'Done.\\label{END}' in ed.mark_body_end(upper, 'END', ('AI use statement',))
    renamed = text.replace('AI use statement', 'AI Statement')
    marked = ed.mark_body_end(renamed, 'END', ('AI use statement',))
    assert 'Done.\\label{END}' not in marked and 'E.\\label{END}' in marked


def test_overrun_inside_an_unexempt_unnumbered_subsection_is_named():
    from lib.longform.documents.render import body_end_location
    aux = ('\\newlabel{tofu-pipeline-body-end}{{6}{10}{AI Disclosure}'
           '{subsection*.12}{}}\n')
    assert body_end_location(aux) == (10, 'AI Disclosure', True)


def test_icml_2027_policy_heading_takes_precedence_over_kit_example():
    # The CFP and the generative-AI policy page say "AI Disclosure Statement";
    # the kit's example_paper.tex heading is "AI Use Statement".
    ed = _edition('icml-2027')
    exempt = set(ed['exempt_sections']['headings'])
    assert {'AI Disclosure Statement', 'AI Use Statement', 'Impact Statement',
            'Accessibility', 'Software and Data', 'Acknowledgements'} <= exempt
    ai = next(row for row in ed['required_sections'] if 'AI' in row['heading'])
    assert ai['heading'] == 'AI Disclosure Statement'
    assert not ai.get('alternatives')
    assert 'discrepancy is unresolved' in ai['note']
    assert 'UseOfGenerativeAIByAuthors' in ai['source']['url']


def test_cvpr_2027_requires_its_llm_usage_statement():
    ed = _edition('cvpr-2027')
    assert [row['heading'] for row in ed['required_sections']] == ['LLM Usage Statement']
    assert ed['exempt_sections']['headings'] == ['LLM Usage Statement']


# ── NeurIPS tracks ──────────────────────────────────────────────────────────

def _neurips_kit(**terms):
    return {'files': {'neurips_2026.sty': 'x'}, 'year': 2026,
            **vn.venue_terms('neurips', year=2026, **terms)}


@pytest.mark.parametrize('terms, option', [
    ({}, '\\usepackage[main]{neurips_2026}'),
    ({'track': 'eandd'}, '\\usepackage[eandd]{neurips_2026}'),
    ({'track': 'eandd', 'stage': 'camera_ready'}, '\\usepackage[eandd,final]{neurips_2026}'),
    ({'stage': 'camera_ready'}, '\\usepackage[main,final]{neurips_2026}'),
])
def test_neurips_frame_loads_the_track_option(terms, option):
    tex = vn.frame_main_tex('neurips', _neurips_kit(**terms), title='T', authors='A',
                            abstract='a', body='b', bibfile='r')
    assert option in tex
    assert tex.count('{neurips_2026}') == 1


def test_evaluations_and_datasets_follows_the_main_track_limits():
    assert vn.venue_terms('neurips', year=2026, track='eandd')['page_limit'] == 9
    assert vn.venue_terms('neurips', year=2026, track='eandd',
                          stage='camera_ready')['page_limit'] == 10


@pytest.mark.parametrize('family, review, final', [
    ('icml', '\\usepackage{icml2027}', '\\usepackage[accepted]{icml2027}'),
    ('acl', '\\usepackage[review]{acl}', '\\usepackage[final]{acl}'),
    ('colm', '\\usepackage[submission]{colm2026_conference}',
     '\\usepackage[final]{colm2026_conference}'),
    ('cvpr', '\\usepackage[review]{cvpr}', '\\usepackage{cvpr}'),
    ('aaai', '\\usepackage[submission]{aaai2027}', '\\usepackage{aaai2027}'),
])
def test_frames_switch_review_and_camera_ready_modes(family, review, final):
    files = {'icml2027.sty': 'x', 'colm2026_conference.sty': 'x', 'aaai2027.sty': 'x'}
    kwargs = dict(title='T', authors='A', abstract='a', body='b', bibfile='r')
    assert review in vn.frame_main_tex(family, {'files': files, 'stage': 'submission'},
                                       **kwargs)
    assert final in vn.frame_main_tex(family, {'files': files, 'stage': 'camera_ready'},
                                      **kwargs)


def test_a_family_without_a_fresh_paper_frame_is_refused():
    with pytest.raises(reg.TemplateRefused, match='no fresh-paper frame for the ijcai kit'):
        vn.frame_main_tex('ijcai', {'files': {}}, title='', authors='', abstract='',
                          body='', bibfile='')


# ── kit-required packages at render (ICML's forloop) ────────────────────────

def test_render_names_a_package_the_kit_requires_but_tex_lacks(tmp_path, monkeypatch):
    from lib.longform.documents import render
    from lib.paper import latex_layout
    sty = '\\ProvidesPackage{icml2027}\n\\RequirePackage{forloop}\n\\RequirePackage{fancyhdr}\n'
    (tmp_path / 'icml2027.sty').write_text(sty)
    (tmp_path / 'fancyhdr.sty').write_text('')
    kit = {'files': {'icml2027.sty': 'x', 'fancyhdr.sty': 'y'}}
    asked = []

    def kpsewhich(names):
        asked.extend(names)
        return set()
    monkeypatch.setattr(latex_layout, 'kpsewhich', kpsewhich)
    assert render.missing_kit_packages(tmp_path, kit) == ['forloop']
    assert asked == ['forloop.sty'], 'a package the kit itself ships is not looked up'
    monkeypatch.setattr(latex_layout, 'kpsewhich', lambda names: set(names))
    assert render.missing_kit_packages(tmp_path, kit) == []
    monkeypatch.setattr(latex_layout, 'kpsewhich', lambda names: None)
    assert render.missing_kit_packages(tmp_path, kit) == [], \
        'without a TeX toolchain the compile itself reports the failure'
    monkeypatch.setattr(latex_layout, 'kpsewhich', kpsewhich)
    report = render.compile_and_check(tmp_path, engine='pdflatex', page_limit=8, kit=kit)
    assert report['failures'] and 'forloop' in report['failures'][0]
    assert 'tlmgr install forloop' in report['failures'][0]


# ── the brief chooses: venue, year, paper_type, track, role ────────────────

def _brief(**extra):
    return {'type': 'paper', 'title': 'T', 'audience': 'a', 'goal': 'g', 'voice': 'v',
            'lang': 'en', 'judge_model': 'judge', 'writer_model': 'writer',
            'output_dir': 'out', 'venue': 'arr', 'project_lead': 'p', 'topic': 't',
            'previous_pdf': 'prev.pdf', **extra}


def _intake_ctx(tmp_path, brief):
    paper = tmp_path / 'paper'
    paper.mkdir(parents=True)
    (paper / 'prev.pdf').write_bytes(b'%PDF')
    (paper / pl.BRIEF_FILE).write_text(json.dumps(brief))
    return {'paper_dir': str(paper), 'workdir': str(tmp_path / 'work'),
            'brief': brief, 'doc_type': document_type('paper'), 'run_started': 100.0,
            'artifacts': {}, 'owner_user_id': None}


def _stub_kit(edition_id, destination):
    destination.mkdir(parents=True, exist_ok=True)
    return {'edition': edition_id, 'source_url': 'https://example.invalid/kit.zip',
            'upstream_commit': 'f' * 40, 'archive_sha256': 'e' * 64,
            'files': {}, 'fetched_at': 200.0, 'page_limit_source': 'test',
            'anonymity': 'test'}


def _current(edition_id):
    return {'edition': edition_id, 'conference': edition_id, 'verdict': 'current'}


def _seams(monkeypatch, freshness=_current):
    monkeypatch.setattr(pl, '_fetch_kit', _stub_kit)
    monkeypatch.setattr(pl, '_check_freshness', freshness)
    monkeypatch.setattr(pl, '_owner_directives', lambda uid: {'items': [], 'reason': ''})
    monkeypatch.setattr(pl, '_persist', lambda c, stage, art, markdown='': art)


@pytest.mark.parametrize('extra, pages', [
    ({}, 8), ({'paper_type': 'short'}, 4),
    ({'paper_type': 'short', 'role': 'camera_ready', 'draft': {'main': 'main.tex'}}, 5),
    ({'role': 'camera_ready', 'draft': {'main': 'main.tex'}}, 9),
    ({'role': 'final', 'draft': {'main': 'main.tex'}}, 8),
])
def test_acl_limit_is_chosen_by_paper_type_and_role(tmp_path, monkeypatch, extra, pages):
    ctx = _intake_ctx(tmp_path, _brief(**extra))
    if extra.get('draft'):
        (Path(ctx['paper_dir']) / 'main.tex').write_text('x')
    _seams(monkeypatch)
    art = pl._run_intake(ctx)
    ctx['artifacts']['intake'] = art
    assert pl._page_limit(ctx) == pages
    assert art['kit']['paper_type'] == extra.get('paper_type', 'long')
    assert art['kit']['edition'] == 'arr-rolling'
    assert pl._gate_intake(ctx, art) == []


def test_brief_terms_are_gated(tmp_path, monkeypatch):
    _seams(monkeypatch)
    ctx = _intake_ctx(tmp_path, _brief(paper_type='poster'))
    errors = pl._gate_intake(ctx, pl._run_intake(ctx))
    assert any("'poster'" in e for e in errors)
    over = _brief(paper_type='short', page_limit=8)
    ctx = _intake_ctx(tmp_path / 'b', over)
    assert any('exceeds the venue' in e for e in pl._gate_intake(ctx, pl._run_intake(ctx)))
    ctx = _intake_ctx(tmp_path / 'c', _brief(role='camera_ready'))
    assert any('role=camera_ready' in e for e in pl._gate_intake(ctx, pl._run_intake(ctx)))


def test_intake_refuses_an_unreleased_year_without_fetching(tmp_path, monkeypatch):
    fetched = []
    _seams(monkeypatch)
    monkeypatch.setattr(pl, '_fetch_kit', lambda *a: fetched.append(a))
    ctx = _intake_ctx(tmp_path, _brief(venue='neurips', venue_year=2027))
    errors = pl._gate_intake(ctx, pl._run_intake(ctx))
    assert fetched == []
    assert any('paper.official-template' in e and 'unable to verify template availability' in e
               for e in errors)


def test_previous_year_draft_cannot_be_finished_for_submission(tmp_path, monkeypatch):
    _seams(monkeypatch)
    brief = _brief(venue='neurips', venue_year=2027, template_draft_on='neurips-2026')
    ctx = _intake_ctx(tmp_path, brief)
    art = pl._run_intake(ctx)
    assert art['kit']['edition'] == 'neurips-2026' and not art['kit']['submission_ready']
    assert pl._gate_intake(ctx, art) == [], 'a labelled draft may be revised'
    final = _brief(venue='neurips', venue_year=2027, template_draft_on='neurips-2026',
                   role='final', draft={'main': 'main.tex'})
    ctx = _intake_ctx(tmp_path / 'b', final)
    (Path(ctx['paper_dir']) / 'main.tex').write_text('x')
    errors = pl._gate_intake(ctx, pl._run_intake(ctx))
    assert any('role=final finishes a paper for submission' in e and 'DRAFT on the '
               'NeurIPS 2026 template' in e for e in errors)


def test_intake_refuses_when_the_venue_changed_its_rules(tmp_path, monkeypatch):
    def changed(edition_id):
        return {'edition': edition_id, 'conference': 'ACL', 'verdict': 'changed',
                'archive': {'changed': True, 'now': 'a' * 64},
                'rules': [{'rule': 'page limits', 'url': 'https://x', 'present': False}],
                'newer': [], 'released': []}
    _seams(monkeypatch, freshness=changed)
    ctx = _intake_ctx(tmp_path, _brief())
    errors = pl._gate_intake(ctx, pl._run_intake(ctx))
    assert any('paper.template-freshness' in e and 'archive sha256 now aaaa' in e
               and 'page limits: quoted text no longer on https://x' in e for e in errors)

    def offline(edition_id):
        return {'edition': edition_id, 'conference': 'ACL', 'verdict': 'unverifiable',
                'archive': {'error': 'no route'}, 'rules': [], 'newer': [], 'released': []}
    _seams(monkeypatch, freshness=offline)
    ctx = _intake_ctx(tmp_path / 'b', _brief())
    errors = pl._gate_intake(ctx, pl._run_intake(ctx))
    assert any('could not verify the pinned edition is current: no route' in e
               for e in errors)


def test_render_gate_is_called_with_the_resolved_edition(tmp_path, monkeypatch):
    from lib.longform.documents import conformance
    seen = {}

    def check_paper(build, main, ed, **kw):
        seen.update(build=build, main=main, edition=ed['id'], **kw)
        return {'failures': []}
    monkeypatch.setattr(conformance, 'check_paper', check_paper)
    kit = {'edition': 'neurips-2026', 'stage': 'submission', 'track': 'eandd',
           'paper_type': 'regular', 'submission_ready': False,
           'draft_label': 'DRAFT on the NeurIPS 2026 template', 'page_limit': 9}
    ctx = {'workdir': str(tmp_path), 'brief': {'authors': 'Jane Roe and Ann Lee, Bo Chen'},
           'artifacts': {'intake': {'kit': kit}}}
    pl._conformance(ctx, tmp_path, 'main.tex')
    assert seen['edition'] == 'neurips-2026' and seen['track'] == 'eandd'
    assert seen['identities'] == ['Jane Roe', 'Ann Lee', 'Bo Chen']
    assert seen['page_limit'] == 9 and seen['submission_ready'] is False
    assert seen['draft_label'].startswith('DRAFT')


def test_camera_ready_copy_edits_like_final():
    assert pl._final({'brief': {'role': 'camera_ready'}})
    assert not pl._final({'brief': {'role': 'rebuttal'}})
