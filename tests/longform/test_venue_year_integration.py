"""Source-only edition handoff; no archive, TeX, or submission verification."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lib.longform.documents import conformance, pipeline, render, venue_registry
from lib.longform.documents.types import document_type

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('date', ['2026-12-31', '2027-01-01'])
@pytest.mark.parametrize('year, refusal', [
    (2026, None),
    (None, '"venue_year" is required'),
    (2027, 'unable to verify template availability'),
    (2028, 'no neurips 2028 template is pinned'),
])
def test_retained_edition_intake_and_conformance_handoff(tmp_path, monkeypatch, date, year, refusal):
    retained = venue_registry.edition('neurips-2026')
    assert (retained['venue'], retained['year'], retained['status']) == ('neurips', 2026, 'pinned')
    now = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(pipeline.time, 'time', lambda: now)
    brief = {
        'type': 'paper', 'venue': 'neurips', 'venue_year': year,
        'title': 'Synthetic identity handoff', 'topic': 'identity',
        'audience': 'test', 'goal': 'test', 'voice': 'precise', 'lang': 'en',
        'judge_model': 'judge', 'writer_model': 'writer', 'project_lead': 'test',
        'output_dir': 'delivered', 'previous_pdf': 'previous.pdf',
    }
    from pypdf import PdfWriter
    with (tmp_path / 'previous.pdf').open('wb') as stream:
        pdf = PdfWriter()
        pdf.add_blank_page(width=612, height=792)
        pdf.write(stream)
    (tmp_path / pipeline.BRIEF_FILE).write_text(json.dumps(brief))
    ctx = {'paper_dir': str(tmp_path), 'workdir': str(tmp_path / 'work'),
           'run_started': now, 'artifacts': {}, 'doc_type': document_type('paper')}
    fetched, freshness, compiled, checked = [], [], [], []

    def fetch(edition_id, destination):
        fetched.append(edition_id)
        ed = venue_registry.edition(edition_id)
        destination.mkdir(parents=True)
        for leaf in ed['files']:
            (destination / leaf).write_text('% Unverified synthetic compiler-spy input\n')
        return {'edition': ed['id'], 'year': ed['year'], 'files': dict(ed['files']),
                'source_url': ed['archive']['url'], 'archive_sha256': ed['archive']['sha256'],
                'fetched_at': now}

    def fresh(edition_id):
        freshness.append(edition_id)
        return {'verdict': 'current'}

    def compile(build, **kwargs):
        assert (build / kwargs['main']).is_file()
        assert r'\usepackage[main]{neurips_2026}' in (build / kwargs['main']).read_text()
        compiled.append(kwargs['kit']['edition'])
        return {'compile_status': 'success', 'failures': [], 'pages': 1, 'body_pages': 1}

    def check(build, main, ed, **kwargs):
        assert compiled == ['neurips-2026']
        checked.append((ed, kwargs))
        return {'edition': ed['id'], 'handoff_only': True, 'passed': False,
                'failures': ['synthetic conformance is not verified']}

    # External boundaries are spies, not proof of original archive bytes or readiness.
    monkeypatch.setattr(pipeline, '_fetch_kit', fetch)
    monkeypatch.setattr(pipeline, '_check_freshness', fresh)
    monkeypatch.setattr(conformance, 'check_paper', check)
    monkeypatch.setattr(render, 'compile_and_check', compile)
    art = pipeline._run_intake(ctx)
    errors = pipeline._gate_intake(ctx, art)
    if refusal:
        assert refusal in art['kit']['refused']
        assert any(refusal in error for error in errors)
        assert fetched == freshness == compiled == checked == []
        return

    kit = art['kit']
    assert kit['edition'] == kit['target_edition'] == 'neurips-2026'
    assert kit['year'] == 2026 and kit['submission_ready'] and not kit['draft_label']
    assert kit['files'] == retained['files']
    assert fetched == freshness == ['neurips-2026']
    assert errors == []
    ctx['artifacts'].update({
        'intake': art, 'dossier': {'bib': '', 'figures': []}, 'draft': {},
        'revise': {'sections': {sid: 'Synthetic body.' for sid, _ in ctx['doc_type'].sections}},
        'outline': {'sections': [{'id': sid, 'title': title, 'figures': []}
                                 for sid, title in ctx['doc_type'].sections]},
    })
    result = pipeline._run_render(ctx)
    assert pipeline._gate_render(ctx, result) == [
        'paper.template-conformance: synthetic conformance is not verified']
    assert result['compile_status'] == 'success'
    assert result['conformance']['edition'] == 'neurips-2026'
    assert result['conformance']['handoff_only']
    assert compiled == ['neurips-2026']
    assert freshness == ['neurips-2026', 'neurips-2026']
    missing_previous = {**art, 'brief': {**brief, 'previous_pdf': 'absent.pdf'}}
    assert pipeline._gate_intake(ctx, missing_previous) == [
        "previous version 'absent.pdf' not found; the blind A/B needs it"]
    ed, options = checked[0]
    assert ed == retained
    assert options['stage'] == 'submission' and options['track'] == 'main'
    assert options['page_limit'] == 9 and options['submission_ready']
    assert options['portfolio_policy'] is None and options['commitment_venue'] is None


def _preview(monkeypatch, tmp_path, packages='neurips_2026', **extra):
    """An ordinary local preview over a synthetic template source."""
    from lib.paper.builds import service
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n\n'
        f'\\usepackage[main]{{{packages}}}\n\n'
        '\\begin{document}\nSynthetic.\n\\end{document}\n')
    monkeypatch.setattr(service, 'owned_settings',
                        lambda conv_id, owner_user_id: {'projectPath': str(tmp_path)})

    def past_year_check(document, payload, execution, scheduler, owner_user_id):
        raise RuntimeError('past preview year check')

    monkeypatch.setattr(service, '_start_bound', past_year_check)
    return service, {'source': 'local', 'conv_id': 'synthetic-conversation',
                     'request_id': 'synthetic-request',
                     'subscriber_id': 'synthetic-subscriber', 'path': 'main.tex', **extra}


def test_preview_matching_venue_year_passes(monkeypatch, tmp_path):
    service, payload = _preview(monkeypatch, tmp_path, venue_year=2026)
    with pytest.raises(RuntimeError, match='past preview year check'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_preview_mismatched_venue_year_is_refused(monkeypatch, tmp_path):
    from lib.paper.builds.errors import BuildError
    service, payload = _preview(monkeypatch, tmp_path, venue_year=2027)
    with pytest.raises(BuildError, match=r'expected year 2026.*received 2027'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_preview_without_venue_year_keeps_the_documented_default(monkeypatch, tmp_path):
    service, payload = _preview(monkeypatch, tmp_path)
    assert 'venue_year' not in payload
    with pytest.raises(RuntimeError, match='past preview year check'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


@pytest.mark.parametrize('year', [2027, 2028])
def test_2026_template_does_not_silently_accept_another_year(monkeypatch, tmp_path, year):
    from lib.paper.builds.errors import BuildError
    service, payload = _preview(monkeypatch, tmp_path, venue_year=year)
    with pytest.raises(BuildError) as refusal:
        service.start_preview(payload, owner_user_id=19, scheduler=object())
    message = str(refusal.value)
    assert 'expected year 2026' in message and f'received {year}' in message


def test_comma_separated_template_package_is_still_checked(monkeypatch, tmp_path):
    from lib.paper.builds.errors import BuildError
    service, payload = _preview(monkeypatch, tmp_path,
                                packages='neurips_2026,amsmath', venue_year=2027)
    with pytest.raises(BuildError, match=r'expected year 2026.*received 2027'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_template_in_an_included_preamble_is_still_checked(monkeypatch, tmp_path):
    from lib.paper.builds.errors import BuildError
    service, payload = _preview(monkeypatch, tmp_path, venue_year=2027)
    (tmp_path / 'preamble.tex').write_text('\\usepackage{neurips_2026}\n')
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n\\input{preamble}\n'
        '\\begin{document}\nSynthetic.\n\\end{document}\n')
    with pytest.raises(BuildError, match=r'expected year 2026.*received 2027'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_shared_style_name_does_not_invent_an_expected_year(monkeypatch, tmp_path):
    # acl.sty belongs to the rolling ARR edition, ACL 2027 and superseded EMNLP 2026.
    service, payload = _preview(monkeypatch, tmp_path, packages='acl', venue_year=2026)
    with pytest.raises(RuntimeError, match='past preview year check'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_overleaf_preview_refuses_a_year_it_cannot_read(monkeypatch):
    from lib.paper.builds import service
    from lib.paper.builds.errors import BuildError
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {})
    payload = {'source': 'overleaf', 'conv_id': 'synthetic-conversation',
               'project_id': 'a' * 24, 'request_id': 'synthetic-request',
               'subscriber_id': 'synthetic-subscriber', 'venue_year': 2027}
    with pytest.raises(BuildError, match='cannot be checked for an Overleaf preview'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())


def test_desktop_preview_refuses_a_year_it_cannot_read(monkeypatch):
    from lib.paper.builds import service
    from lib.paper.builds.errors import BuildError
    root = 'remote:device:test'
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {'projectPath': root})
    payload = {'source': 'local', 'conv_id': 'synthetic-conversation',
               'project_root': root, 'path': 'main.tex',
               'request_id': 'synthetic-request',
               'subscriber_id': 'synthetic-subscriber', 'venue_year': 2027}
    with pytest.raises(BuildError, match='cannot be checked for a desktop preview'):
        service.start_preview(payload, owner_user_id=19, scheduler=object())
