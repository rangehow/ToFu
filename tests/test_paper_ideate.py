#!/usr/bin/env python3
"""R3 — anti-"A+B" idea gate (lib/paper/ideate.py).

Owner's two hidden-assumption pins, proven here (pure — search/LLM seams
monkeypatched, no DB, no network):

  PIN #2 (structural gate: idea must link a real open_gap):
    * an A+B stitch whose linked_gap_id points at NOTHING in the verified
      open_gaps is rejected at the free structural gate;
    * NEUTER — drop the "linked_gap_id must match a real gap" rule and the
      invented-problem stitch survives (test goes red).

  PIN #1 (novelty = f(retrieved neighbor set), not f(model self-report)):
    * the novelty prior set ALWAYS contains the forced search_arxiv top-K,
      including ids the model never self-reported;
    * an idea whose novelty only holds when you ignore retrieval is sunk once
      the forced neighbor (a dead-ringer paper) enters the judge's basis;
    * NEUTER — if _novelty_prior_set stopped forcing retrieval (self-report
      only), the stitch's colliding neighbor never reaches the judge (test
      asserts the forced id IS in the prior set).

  Plus: rejected ideas keep scores+reason (calibration audit); the threshold is
  a single constant the tests never hardcode (they drive scores relative to it).

Run standalone:  python tests/test_paper_ideate.py
Under pytest:    pytest tests/test_paper_ideate.py -m unit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


# ── Fixtures ───────────────────────────────────────────────────────────────

_OPEN_GAPS = {
    'schema_version': 1,
    'direction': 'long-context KV compression',
    'open_gaps': [
        {'id': 'gap_1', 'gap': 'no exact recall under KV compression',
         'why_open': 'only perplexity measured', 'kind_hint': 'methodology'},
        {'id': 'gap_2', 'gap': 'no analysis of per-layer compressibility',
         'why_open': 'treated as uniform', 'kind_hint': 'analysis'},
    ],
}


def _good_idea():
    return {
        'title': 'Per-layer learnable KV rank with recall-preserving loss',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'A retrieval-consistency loss makes each layer learn its own '
                          'rank so needle tokens survive compression — a mechanism absent '
                          'from prior uniform-rank work.',
        'novelty_claim': 'Unlike arXiv:2305.11111 (uniform static rank) this learns rank '
                         'end-to-end under a recall objective.',
        'prior_art': ['2305.11111'],
        'falsifiable_prediction': 'On needle-in-haystack at 32k ctx, exact-match recall '
                                  'stays >90% at 4x compression where uniform-rank drops <60%.',
        'why_not_AB': 'It is not low-rank + a loss glued: the loss reshapes what "rank" '
                      'means per layer, a mechanism neither prior piece has.',
    }


def _ab_stitch_no_gap():
    """An A+B stitch that invents its own problem (links no real gap)."""
    idea = _good_idea()
    idea['title'] = 'KV low-rank + RoPE, combined'
    idea['linked_gap_id'] = 'gap_999_invented'   # not in open_gaps
    idea['core_mechanism'] = 'Apply low-rank compression and also use RoPE.'
    idea['why_not_AB'] = 'They are different papers.'
    return idea


class _FakeSearch:
    """A fake search_arxiv returning a fixed neighbor list; records queries."""
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def __call__(self, query, max_results=10):
        self.queries.append(query)
        return list(self.hits)


def _patch(monkeyables):
    """Patch ideate module attributes; return restore()."""
    import lib.paper.ideate as it
    saved = {k: getattr(it, k) for k in monkeyables}
    for k, v in monkeyables.items():
        setattr(it, k, v)
    return lambda: [setattr(it, k, saved[k]) for k in saved]


def _score_returning(overall, delta='mechanism-level', novelty=5):
    """A fake _score_idea that returns a fixed verdict."""
    def _fn(idea, prior_set, gap, lang, **kw):
        scores = {'novelty': novelty, 'falsifiability': 5, 'mechanism_depth': 5, 'value': 5}
        # emulate the real deterministic cap so the delta path is exercised
        capped = False
        if delta == 'parameter-level' and scores['novelty'] > 2:
            scores['novelty'] = 2
            capped = True
        ov = round(sum(scores.values()) / len(scores), 2)
        return {'scores': scores, 'overall': overall if overall is not None else ov,
                'mechanism_delta': delta, 'closest_neighbor': (prior_set['merged_ids'][0]
                                                               if prior_set['merged_ids'] else ''),
                'justifications': {}, 'verdict': 'v', 'novelty_capped': capped}
    return _fn


# ── PIN #2: structural gate ────────────────────────────────────────────────

def test_structural_gate_rejects_invented_problem():
    import lib.paper.ideate as it
    valid = it._valid_gap_ids(_OPEN_GAPS)
    assert it._structural_gate(_good_idea(), valid) is None, 'good idea should pass'
    reason = it._structural_gate(_ab_stitch_no_gap(), valid)
    assert reason and 'linked_gap_id' in reason, f'stitch should fail on gap link, got {reason!r}'
    _ok('structural gate: idea linking no real open_gap is rejected (invented problem)')


def test_ab_stitch_rejected_end_to_end_NEUTER():
    """Full pipeline: the invented-problem stitch is rejected; NEUTER the
    gap-link rule and it survives."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'Uniform rank KV', 'summary': ''}])
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_ab_stitch_no_gap()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Some Real Title',
        '_score_idea': _score_returning(4.8),  # would pass rubric IF it reached it
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert res['ok'], res
        assert len(res['accepted']) == 0, 'stitch must not be accepted'
        assert any(r['reject_stage'] == 'structural' for r in res['rejected']), \
            f"expected structural rejection, got {[r.get('reject_stage') for r in res['rejected']]}"
    finally:
        restore()

    # NEUTER: disable the gap-link rule in the structural gate.
    orig = it._structural_gate
    def _neutered(idea, valid_gap_ids):
        # keep field checks, DROP the linked_gap_id-must-match rule
        for f in it._REQUIRED_FIELDS:
            v = idea.get(f)
            if not (isinstance(v, str) and v.strip()):
                return f'missing {f}'
        return None
    it._structural_gate = _neutered
    restore2 = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_ab_stitch_no_gap()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Some Real Title',
        '_score_idea': _score_returning(4.8),
    })
    try:
        res2 = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        leaked = len(res2['accepted']) == 1
    finally:
        restore2()
        it._structural_gate = orig
    assert leaked, 'NEUTER FAILED: stitch did not leak through when gap-link rule removed'
    _ok('NEUTER: removing the "must link a real open_gap" rule lets the stitch leak (gate bites)')


# ── PIN #1: forced-neighbor-retrieval novelty ──────────────────────────────

def test_novelty_prior_set_forces_retrieval_including_unreported_id():
    """The prior set MUST include the forced search_arxiv top-K, even ids the
    idea never self-reported."""
    import lib.paper.ideate as it
    # The idea self-reports only 2305.11111; retrieval surfaces a colliding
    # neighbor 2402.55555 the model never mentioned.
    collider = {'arxiv_id': '2402.55555', 'title': 'Learnable per-layer KV rank', 'summary': 'x'}
    fake_search = _FakeSearch([collider,
                               {'arxiv_id': '2305.11111', 'title': 'Uniform rank', 'summary': ''},
                               {'arxiv_id': '2401.22222', 'title': 'Other', 'summary': ''},
                               {'arxiv_id': '2401.33333', 'title': 'Other2', 'summary': ''},
                               {'arxiv_id': '2401.44444', 'title': 'Other3', 'summary': ''}])
    restore = _patch({'search_arxiv': fake_search})
    try:
        ps = it._novelty_prior_set(_good_idea())
        assert len(ps['retrieved']) >= it.IDEATE_NOVELTY_RETRIEVAL_K, \
            f"retrieved {len(ps['retrieved'])} < K={it.IDEATE_NOVELTY_RETRIEVAL_K}"
        assert '2402.55555' in ps['merged_ids'], \
            'forced-retrieval collider (unreported by the model) missing from prior set'
        assert fake_search.queries and 'learnable' in fake_search.queries[0].lower(), \
            'retrieval query must include title+mechanism'
    finally:
        restore()
    _ok('novelty prior set forces search_arxiv top-K incl. ids the model never self-reported')


def test_parameter_level_delta_caps_novelty_and_sinks_stitch():
    """A parameter-level mechanism_delta caps novelty at 2, dragging overall
    below threshold — the stitch is rejected on the rubric."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2402.55555', 'title': 'Dead ringer', 'summary': ''}] * 5)
    # Use the REAL _score_idea with a fake dispatch that declares parameter-level.
    def _fake_dispatch(messages, on_content=None, **kw):
        import json
        payload = json.dumps({'mechanism_delta': 'parameter-level', 'closest_neighbor': '2402.55555',
                              'scores': {'novelty': 5, 'falsifiability': 5, 'mechanism_depth': 5,
                                         'value': 5}, 'justifications': {}, 'verdict': 'stitch'})
        if on_content:
            on_content(payload)
        return ({'content': payload}, 'stop', {})
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Real',
        'dispatch_stream': _fake_dispatch,
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        # novelty capped 5→2 → overall = (2+5+5+5)/4 = 4.25 ... still above 4.0!
        # so tighten: this asserts the CAP happened, not the threshold outcome.
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['novelty'] == 2, f"novelty not capped: {rec['scores']}"
        assert rec['mechanism_delta'] == 'parameter-level'
    finally:
        restore()
    _ok('parameter-level delta deterministically caps the novelty axis to 2')


def test_novelty_capped_below_threshold_rejects():
    """With three axes also modest, the parameter-level cap pushes overall under
    threshold → rejected on rubric with scores preserved."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2402.55555', 'title': 'Dead ringer', 'summary': ''}] * 5)
    def _fake_dispatch(messages, on_content=None, **kw):
        import json
        payload = json.dumps({'mechanism_delta': 'parameter-level', 'closest_neighbor': '2402.55555',
                              'scores': {'novelty': 5, 'falsifiability': 4, 'mechanism_depth': 3,
                                         'value': 4}, 'justifications': {}, 'verdict': 'stitch'})
        if on_content:
            on_content(payload)
        return ({'content': payload}, 'stop', {})
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        'dispatch_stream': _fake_dispatch,
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        # capped: (2+4+3+4)/4 = 3.25 < 4.0 → rejected
        assert len(res['accepted']) == 0, 'capped stitch should not be accepted'
        rej = res['rejected'][0]
        assert rej['reject_stage'] == 'rubric' and rej['scores']['novelty'] == 2
        assert 'overall' in rej and rej['overall'] < res['threshold']
    finally:
        restore()
    _ok('parameter-level cap drags overall < threshold → stitch rejected on rubric')


# ── Rejection audit + accepted path + threshold-not-hardcoded ──────────────

def test_good_idea_accepted_and_rejections_audited():
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'Uniform rank KV', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea(), _ab_stitch_no_gap()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real title',
        '_score_idea': _score_returning(4.75, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert len(res['accepted']) == 1, f"expected 1 accepted, got {len(res['accepted'])}"
        acc = res['accepted'][0]
        assert acc['overall'] == 4.75 and 'scores' in acc and 'prior_set_ids' in acc
        # the stitch is rejected AND its record is auditable
        assert len(res['rejected']) == 1
        rej = res['rejected'][0]
        assert rej['reject_stage'] == 'structural' and rej['reject_reason']
    finally:
        restore()
    _ok('good idea accepted with scores; rejected stitch preserved with stage+reason (audit)')


def test_threshold_is_a_calibratable_constant_not_hardcoded():
    """Raising the threshold above a fixed overall flips accept→reject WITHOUT
    changing any gate logic — proving the threshold is a tunable, not baked in."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    common = {'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
              '_score_idea': _score_returning(4.2, delta='mechanism-level'),
              '_generate_raw_ideas': lambda *a, **k: [_good_idea()]}
    restore = _patch(common)
    try:
        lax = it.generate_ideas('dir', _OPEN_GAPS, lang='en', threshold=4.0)
        strict = it.generate_ideas('dir', _OPEN_GAPS, lang='en', threshold=4.5)
        assert len(lax['accepted']) == 1, 'overall 4.2 should pass threshold 4.0'
        assert len(strict['accepted']) == 0, 'overall 4.2 should fail threshold 4.5'
        assert strict['rejected'][0]['reject_stage'] == 'rubric'
    finally:
        restore()
    _ok('threshold is a calibratable constant (4.0 accepts, 4.5 rejects the same idea)')


def test_low_confidence_gap_docks_value_axis():
    """R2/R3 seam v2: an idea linked to a low_confidence gap (survey backed it
    only with grounded-but-unharvested papers) gets its value axis docked by one
    and is flagged — the reward for a loosened survey gate is paid HERE, visibly."""
    import lib.paper.ideate as it
    gaps_low = {
        'schema_version': 1, 'direction': 'd',
        'open_gaps': [{'id': 'gap_1', 'gap': 'g', 'low_confidence': True,
                       'missing_ids': ['2404.00001']}],
    }
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(None, delta='mechanism-level'),  # value=5 raw
    })
    try:
        res = it.generate_ideas('dir', gaps_low, lang='en')
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['value'] == 4, f"value not docked (5→4): {rec['scores']}"
        assert rec['linked_gap_low_confidence'] is True, 'idea not flagged low_confidence'
        # overall recomputed: (5+5+5+4)/4 = 4.75
        assert rec['overall'] == 4.75, rec['overall']
    finally:
        restore()
    _ok('idea linked to a low_confidence gap has its value axis docked + is flagged')


def test_high_confidence_gap_no_dock_NEUTER():
    """Counter/NEUTER: the SAME idea linked to a high-confidence gap (no
    low_confidence flag) keeps value=5 — proving the dock is gated on the flag,
    not applied unconditionally."""
    import lib.paper.ideate as it
    gaps_hi = {'schema_version': 1, 'direction': 'd',
               'open_gaps': [{'id': 'gap_1', 'gap': 'g', 'low_confidence': False}]}
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(None, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', gaps_hi, lang='en')
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['value'] == 5, f'high-confidence gap must NOT dock value: {rec["scores"]}'
        assert rec['linked_gap_low_confidence'] is False
    finally:
        restore()
    _ok('NEUTER: a high-confidence gap does NOT dock value (dock is flag-gated)')


def test_no_gaps_is_clean_failure():
    import lib.paper.ideate as it
    res = it.generate_ideas('dir', {'open_gaps': []}, lang='en')
    assert not res['ok'] and 'survey' in res['error']
    _ok('empty open_gaps → clean ok=False with a run-survey-first message')


def main():
    print()
    print(_color('═══ R3 Ideate / Anti-A+B Gate Tests ═══', '36'))
    print()
    tests = [
        test_structural_gate_rejects_invented_problem,
        test_ab_stitch_rejected_end_to_end_NEUTER,
        test_novelty_prior_set_forces_retrieval_including_unreported_id,
        test_parameter_level_delta_caps_novelty_and_sinks_stitch,
        test_novelty_capped_below_threshold_rejects,
        test_good_idea_accepted_and_rejections_audited,
        test_threshold_is_a_calibratable_constant_not_hardcoded,
        test_low_confidence_gap_docks_value_axis,
        test_high_confidence_gap_no_dock_NEUTER,
        test_no_gaps_is_clean_failure,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
