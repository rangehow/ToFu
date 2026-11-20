"""Owner portfolio restriction is opt-in invocation policy, not a global tier rule.
Synthetic official-list evidence exercises A/non-A; live unavailable evidence
must never be replaced by model-memory tiers.
"""
from copy import deepcopy

import pytest

from lib.longform.documents import portfolio_policy as policy
from lib.longform.documents.venues import venue_terms

pytestmark = pytest.mark.unit


def official():
    return {'status': 'verified', 'source': {
        'edition': 'fixture edition', 'edition_date': '2026-01-01',
        'retrieved': '2026-11-18', 'url': 'https://www.ccf.org.cn/fixture'},
        'venues': {v: {'tier': tier, 'url': 'https://www.ccf.org.cn/fixture', 'quote': v}
                   for v, tier in [('icml', 'A'), ('acl', 'A'), ('iclr', 'B')]}}


def test_owner_a_passes_only_with_valid_pinned_edition_and_official_tier():
    assert venue_terms('icml', year=2027)['submission_ready']
    assert policy.assess(policy.OWNER_CCF_A, 'icml', evidence=official())['submission_ready']
    with pytest.raises(ValueError, match='unable to verify'):
        venue_terms('icml', year=2028)


@pytest.mark.parametrize('venue', ['colm', 'iclr', 'unknown'])
def test_owner_colm_non_a_unknown_fail(venue):
    result = policy.assess(policy.OWNER_CCF_A, venue, evidence=official())
    assert not result['submission_ready']
    if venue == 'colm':
        assert 'never submit to COLM' in ' '.join(result['failures'])


@pytest.mark.parametrize('commitment', [None, 'iclr', 'unknown', 'colm'])
def test_arr_requires_named_a_commitment( commitment):
    assert not policy.assess(policy.OWNER_CCF_A, 'arr', commitment_venue=commitment,
                             evidence=official())['submission_ready']


def test_arr_with_a_commitment_and_generic_colm_unchanged():
    assert policy.assess(policy.OWNER_CCF_A, 'arr', commitment_venue='acl',
                         evidence=official())['submission_ready']
    assert venue_terms('colm', year=2026)['submission_ready']
    assert policy.assess(None, 'colm')['submission_ready']


def test_missing_edition_date_or_non_official_source_fails_closed():
    for key in ['edition', 'edition_date', 'url', 'retrieved']:
        e = deepcopy(official()); e['source'].pop(key)
        assert not policy.assess(policy.OWNER_CCF_A, 'icml', evidence=e)['submission_ready']
    e = official(); e['source']['url'] = 'https://ccf.org.cn.attacker.test/list'
    assert not policy.assess(policy.OWNER_CCF_A, 'icml', evidence=e)['submission_ready']


def test_unavailable_live_list_fails_owner_not_generic(monkeypatch):
    from lib.longform.documents import venue_registry

    def offline(url):
        raise RuntimeError('source unavailable')
    monkeypatch.setattr(venue_registry, 'http_download', offline)
    r = policy.assess(policy.OWNER_CCF_A, 'icml')
    assert not r['submission_ready'] and r['tier'] is None
    assert policy.assess(None, 'icml')['submission_ready']


def test_shipped_official_pdf_edition_not_stale_html():
    import json
    e = json.loads(policy.EVIDENCE_PATH.read_text())
    assert e['source']['edition_date'] == '2026-03-31'
    assert e['source']['corrected_date'] == '2026-04-09'
    assert e['venues']['iclr']['tier'] == 'A'
    assert e['venues']['ijcai']['tier'] == 'B'
    short = policy.assess(policy.OWNER_CCF_A, 'acl', paper_type='short', evidence=e)
    assert short['submission_ready'] and short['tier'] == 'A'
    assert short['recognition_scope']['status'] == 'outside_list_scope'


@pytest.mark.parametrize('profile', [None, policy.OWNER_CCF_A])
def test_common_launcher_preserves_profile_to_render_boundary(tmp_path, monkeypatch, profile):
    """Real launcher adapter -> real pipeline context -> render conformance port.

    Only workflow/model work and PDF compilation are substituted. This proves
    propagation, not adoption by an external project's launcher.
    """
    import threading
    from lib.production import deliverable_launch as launch
    from lib.longform.documents import pipeline, conformance

    class ReachedRender(Exception):
        pass

    seen = []

    def check(*args, **kwargs):
        seen.append(kwargs['portfolio_policy'])
        # COLM demonstrates the owner-only difference without fetching CCF.
        owner = policy.assess(kwargs['portfolio_policy'], 'colm', evidence=official())
        assert owner['submission_ready'] is (profile is None)
        raise ReachedRender

    def workflow(descriptor, ctx, **kwargs):
        artifacts = ctx.setdefault('artifacts', {})
        if 'intake' not in artifacts:
            artifacts['intake'] = {'brief': {'type': 'paper'}, 'kit': {
                'edition': 'colm-2026', 'stage': 'submission', 'page_limit': 9,
                'submission_ready': True}}
        elif 'dossier' not in artifacts:
            artifacts['dossier'] = {'digest': 'synthetic'}
        else:
            pipeline._conformance(ctx, tmp_path, 'main.tex')
        return artifacts

    monkeypatch.setattr(pipeline, 'run_workflow', workflow)
    monkeypatch.setattr(pipeline, '_input_version', lambda ctx: 'synthetic')
    monkeypatch.setattr(conformance, 'check_paper', check)
    contract = {'inputs': {'doc_dir': str(tmp_path)}, 'output_dir': str(tmp_path / 'out'),
                'parameters': {'portfolio_policy': profile}, 'model': {}, 'owner_user_id': 42}
    with pytest.raises(ReachedRender):
        launch._run_document(contract, threading.Event(), lambda *args: None)
    assert seen == [profile]
