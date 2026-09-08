#!/usr/bin/env python3
"""Disabled request names are probed for discovery, never for recommendations.

A manually added wire id lands as ``enabled=False, probe_status='unprobed'``
and the domain authority refuses to enable it before a passed probe — so the
matrix probe plan must include disabled deployments (otherwise such an id
could never earn its enable: a dead-end). But a failing disabled id is not
routed, so its verdict must never recommend disabling the offering's grant:

* ``matrix_probe_plan`` includes disabled wire ids in ``request_ids`` and
  lists their ``<root>::<wire>`` keys in ``disabled_cells``.
* ``run_cell_probe_task`` tags those cells ``deployment_enabled=False`` and
  forces ``recommend_disable=False``.
* ``normalize_probe_snapshot`` keeps them non-actionable even under the
  read-time 429 policy upgrade.
* ``probe_automation.apply_recommendations`` skips them defensively.
"""

import os
import tempfile
import unittest
from unittest import mock

import pytest

from lib.model_routing.probe_plan import matrix_probe_plan
from lib.model_routing import probe_automation


pytestmark = pytest.mark.unit


def _bundle(deployments):
    return {
        'provider': {'provider_id': 'p'},
        'provider_access': {'provider_access_id': 'a', 'enabled': True},
        'connections': [{'connection_id': 'c', 'enabled': True,
                         'base_url': 'https://gw.example/v1',
                         'protocol': 'openai', 'extra_headers': {}}],
        'credentials': [{'credential_id': 'k', 'kind': 'api_key',
                         'secret_reference': ''}],
        'offerings': [{'offering_id': 'o', 'identity_state': 'confirmed',
                       'model': {'creator_id': 'm', 'model_id': 'root'},
                       'capabilities': ['text']}],
        'deployments': deployments,
    }


def _deployment(wire_id, enabled):
    return {'deployment_id': 'd-%s' % wire_id, 'offering_id': 'o',
            'connection_id': 'c', 'wire_model_id': wire_id,
            'enabled': enabled, 'identity_confidence': 'high',
            'probe_status': 'passed' if enabled else 'unprobed',
            'priority': 100}


def test_plan_probes_disabled_wire_ids_and_tags_them():
    plan = matrix_probe_plan(None, None, _bundle([
        _deployment('root', True),
        _deployment('root-m17', False),
    ]))
    request_ids = plan['models'][0]['request_ids']
    # Enabled ids keep document order; disabled ids append after them.
    assert request_ids == ['root', 'root-m17']
    assert plan['disabled_cells'] == ['root::root-m17']


def test_plan_without_disabled_deployments_reports_no_disabled_cells():
    plan = matrix_probe_plan(None, None, _bundle([_deployment('root', True)]))
    assert plan['models'][0]['request_ids'] == ['root']
    assert plan['disabled_cells'] == []


class DisabledCellTaggingTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        import lib.provider_probe as pp
        self.cfg = pp
        self._orig_cache_path = pp.probe_cache_path
        pp.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

    def tearDown(self):
        self.cfg.probe_cache_path = self._orig_cache_path
        self._tmp.cleanup()

    def test_disabled_cell_is_never_recommended(self):
        cfg = self.cfg
        task = {
            'provider_id': 'mt-disabled',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 2, 'done_count': 0,
            'cells': {}, 'summary': {'ok': 0, 'disable': 0},
            'error': None, '_abort': False,
            '_base_url': 'https://gw.example/v1',
            '_extra_headers': {},
            '_disabled_cells': {'root::root-m17'},
        }
        work = [
            (0, 'sk-aaa', 'root', 'root'),
            (0, 'sk-aaa', 'root', 'root-m17'),
        ]
        with cfg.CELL_PROBE_LOCK:
            cfg.CELL_PROBE_TASKS['mt-disabled'] = task
        with mock.patch.object(
            cfg, 'probe_one_cell',
            side_effect=lambda *a, **kw: ('not_found', 'HTTP 404'),
        ):
            cfg.run_cell_probe_task(task, work, timeout=5)

        enabled_cell = task['cells'][cfg.probe_cell_key(0, 'root')]
        assert enabled_cell['recommend_disable'] is True
        assert 'deployment_enabled' not in enabled_cell
        disabled_cell = task['cells'][cfg.probe_cell_key(0, 'root-m17')]
        # The dead id still reports its truthful verdict — it just can never
        # turn into a grant-stripping recommendation.
        assert disabled_cell['status'] == 'not_found'
        assert disabled_cell['recommend_disable'] is False
        assert disabled_cell['deployment_enabled'] is False
        assert task['summary']['disable'] == 1

    def test_normalize_keeps_disabled_cells_non_actionable(self):
        snap = self.cfg.normalize_probe_snapshot({
            'cells': {'0::root-m17': {
                'status': 'rate_limited', 'detail': 'HTTP 429',
                'probe_surface': 'chat', 'recommend_disable': False,
                'deployment_enabled': False,
            }},
        })
        # The read-time 429 policy upgrade must not re-arm a disabled cell.
        assert snap['cells']['0::root-m17']['recommend_disable'] is False

    def test_daily_apply_skips_disabled_cells(self):
        ref = {'creator_id': 'm', 'model_id': 'root'}
        cred = {'credential_id': 'k', 'authorization': {'models': [dict(ref)]}}
        bundle = {'credentials': [cred],
                  'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
        doc = {'credentials': [dict(cred)]}
        snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
            '0::root-m17': {'key_idx': 0, 'root_model_id': 'root',
                            'model_id': 'root-m17', 'status': 'not_found',
                            'recommend_disable': True,
                            'deployment_enabled': False}}}
        assert probe_automation.apply_recommendations(doc, bundle, snap) == 0
        assert doc['credentials'][0]['authorization']['models'] == [ref]


if __name__ == '__main__':
    unittest.main()
