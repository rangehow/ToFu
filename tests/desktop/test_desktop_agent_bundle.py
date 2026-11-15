"""The client-diag inbox and the probe's transport alignment.

The controlled-end installer routes (agent-installer, agent-bundle,
agent-attach-bundle) and the agent-side attach-bundle import are retired with
the separate controlled-end artifact (owner decision 2026-11-15); every
desktop install joins servers by pairing.

Pinned:

DIAG side (``/api/v1/desktop/client-diag`` + ``_probe.probe_server``):
 16. a pasted diagnostics bundle lands as ONE JSONL line bound to the
     caller; empty / oversize pastes are 400; GET replays newest-first
     with a truncated preview; no file ⇒ an empty list;
 17. probe_server and the poll loop use the shared network policy so they
     measure the same eligible routes.

Run:  pytest tests/desktop/test_desktop_agent_bundle.py -q -p no:napari -o addopts=
"""


from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import json
import os

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]

OWNER_USER_ID = 17


# ── server-side helpers ───────────────────────────────────────────────

def _bearer(label='bundle'):
    from lib.api_keys import create_key
    _row, token = create_key(
        owner_user_id=OWNER_USER_ID,
        name=f'test-{label}',
        scopes=['chat'],
    )
    return {'Authorization': f'Bearer {token}'}


def _clear_keys():
    from lib import api_keys
    for row in api_keys.list_keys(owner_user_id=OWNER_USER_ID):
        api_keys.revoke_key(row['id'], owner_user_id=OWNER_USER_ID)


class TestClientDiagInbox:
    """The 2026-08-06 owner ask: a controlled machine that cannot reach the
    server cannot push its logs — the user pastes the agent's「复制诊断信
    息」bundle into the panel and it lands on the server's disk."""

    @pytest.fixture
    def diag_log(self, tmp_path, monkeypatch):
        import routes.api_v1.desktop as d
        path = tmp_path / 'diag.log'
        monkeypatch.setattr(d, '_DIAG_LOG', str(path))
        return path

    def setup_method(self):
        _clear_keys()

    def test_16_post_stores_a_jsonl_line_bound_to_the_caller(
            self, flask_client, diag_log):
        r = flask_client.post('/api/v4/desktop/client-diag',
                              json={'text': 'Tofu Remote diagnostics\nlink: proxy'},
                              headers=_bearer('diag'))
        assert r.status_code == 200, r.get_data(as_text=True)
        lines = diag_log.read_text(encoding='utf-8').strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry['owner_user_id'] == OWNER_USER_ID
        assert 'link: proxy' in entry['text']
        assert entry['ts'] > 0

    def test_16b_empty_paste_is_a_400(self, flask_client, diag_log):
        r = flask_client.post('/api/v4/desktop/client-diag',
                              json={'text': '   '}, headers=_bearer())
        assert r.status_code == 400
        assert not diag_log.exists()

    def test_16c_oversize_paste_is_refused(self, flask_client, diag_log):
        import routes.api_v1.desktop as d
        r = flask_client.post('/api/v4/desktop/client-diag',
                              json={'text': 'x' * (d._DIAG_MAX_CHARS + 1)},
                              headers=_bearer())
        assert r.status_code == 400

    def test_16d_get_returns_newest_first_with_preview(
            self, flask_client, diag_log):
        for i in range(3):
            flask_client.post('/api/v4/desktop/client-diag',
                              json={'text': 'report-%d %s' % (i, 'y' * 300)},
                              headers=_bearer())
        r = flask_client.get('/api/v4/desktop/client-diag',
                             headers=_bearer())
        assert r.status_code == 200
        entries = r.get_json()['entries']
        assert len(entries) == 3
        assert 'report-2' in entries[0]['preview'], 'newest first'
        assert entries[0]['chars'] > 200
        assert len(entries[0]['preview']) == 200, 'preview is truncated'

    def test_16e_get_without_any_submission_is_an_empty_list(
            self, flask_client, diag_log):
        r = flask_client.get('/api/v4/desktop/client-diag',
                             headers=_bearer())
        assert r.status_code == 200
        assert r.get_json()['entries'] == []

    def test_16f_unreadable_inbox_is_an_error_not_an_empty_list(
            self, flask_client, diag_log):
        """Only an absent inbox is empty. A refused read (here the
        O_NOFOLLOW guard on a symlinked inbox) must not read as 'nothing
        was ever pasted'."""
        if not getattr(os, 'O_NOFOLLOW', 0):
            pytest.skip('platform has no O_NOFOLLOW')
        target = diag_log.parent / 'elsewhere.log'
        target.write_text(json.dumps({'owner_user_id': OWNER_USER_ID,
                                      'text': 'redirected'}) + '\n',
                          encoding='utf-8')
        diag_log.symlink_to(target)
        r = flask_client.get('/api/v4/desktop/client-diag',
                             headers=_bearer())
        assert r.status_code == 500
        assert 'redirected' not in r.get_data(as_text=True)


class TestProbeTransportAlignment:
    def test_17_probe_uses_shared_network_policy_like_the_poll_loop(
            self, monkeypatch):
        """Probe and poll inherit the same policy without a pinned override."""
        import lib.desktop_agent._probe as probe_mod
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {'bootId': 'x'}

        def _get(url, **kw):
            captured.update(kw)
            return _Resp()

        monkeypatch.setattr('lib.http_transport.client.http_get', _get)
        ok, reason = probe_mod.probe_server('http://10.0.0.1:15000')
        assert ok and reason == ''
        # The common transport owns proxy/NO_PROXY configuration for both
        # probe and poll; neither pins an explicit route in default mode.
        assert 'proxies' not in captured, (
            'the probe must measure the SAME transport the poll loop uses')
