"""Executable spec for browser crash capture (contracts/client_crash_v1.schema.json).

Covers both intake channels behind the ordinary auth gate: the unclean-exit
report a reloaded tab sends (``POST /api/v1/client/crash-report``) and the
same-origin Reporting API endpoint (``POST /api/v1/client/browser-reports``).
Valid reports become one owner-scoped ``tofu.client-crash/v1`` record kind in
the client-report store; malformed, oversized, foreign-type and foreign-origin
input is refused or dropped, and a crash reaches the Watchdog producer once
per page instance.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.api

_CRASH = '/api/v1/client/crash-report'
_BROWSER = '/api/v1/client/browser-reports'
_LIST = '/api/v1/mobile/client-report'


def _report(**overrides) -> dict:
    report = {
        'schema': 'tofu.client-crash/v1', 'pageInstanceId': '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10',
        'classification': 'renderer_crash_suspected', 'startedAt': 1794186600000,
        'lastSeenAt': 1794187396000, 'detectedAt': 1794187420000, 'navigationType': 'reload',
        'environment': {'userAgent': 'Mozilla/5.0 (Macintosh) Chrome/155.0.0.0', 'platform': 'macOS',
                        'brands': ['Google Chrome/155'], 'deviceMemoryGiB': 8,
                        'extensions': ['grammarly', 'immersive-translate']},
        'context': {'at': 1794187300000, 'view': 'chat',
                    'conversationId': 'coordinator-1a390889aa954da2b18551d1b4a32625', 'streamActive': True},
        'freeze': {'at': 1794187396000, 'evidence': {
            'event': 'browser.main_thread_stall', 'durationMs': 24300,
            'usedJSHeapSize': 4_000_000_000, 'jsHeapSizeLimit': 4_294_705_152, 'domElements': 152340}},
        'errors': [{'at': 1794187000000, 'kind': 'console', 'message': '[sync] failed', 'count': 3,
                    'lastAt': 1794187100000}],
        'droppedErrors': 0,
    }
    report.update(overrides)
    return report


def _crash_upload(**body) -> list[dict]:
    return [{'type': 'crash', 'age': 42, 'url': 'http://localhost/?token=secret#frag',
             'user_agent': 'Mozilla/5.0 Chrome/155.0.0.0', 'body': body}]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv('TOFU_CLIENT_REPORTS_DIR', str(tmp_path))
    from lib import client_report_store
    return client_report_store


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Calls into the Watchdog producer (owned by lib/router/watchdog/signals.py)."""
    import lib.router.watchdog.signals as module
    calls: list[dict] = []
    # raising=True: patching a producer that does not exist must fail, not create it.
    monkeypatch.setattr(module, 'report_client_crash',
                        lambda owner, **kwargs: calls.append({'owner': owner, **kwargs}))
    return calls


def _app(*, authenticated: bool = True):
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import client_support as route

    app = Quart(__name__)
    if authenticated:
        @app.before_request
        async def _auth():
            g.auth_ctx = local_admin_context()
    app.register_blueprint(route.api_v1_client_support_bp)
    return app


def _call(app, method: str, path: str, **kwargs):
    async def _run():
        response = await getattr(app.test_client(), method)(path, **kwargs)
        return response.status_code, await response.get_json()
    return asyncio.run(_run())


def _crash_records(store) -> list[dict]:
    lines = store.current_path().read_text(encoding='utf-8').splitlines() if store.current_path().is_file() else []
    return [record for record in map(json.loads, lines) if record.get('schema') == 'tofu.client-crash/v1']


def test_unclean_exit_report_is_stored_owner_scoped_and_signalled_once(store, signals):
    status, body = _call(_app(), 'post', _CRASH, json=_report())
    assert status == 200 and body['ok'] is True
    [record] = _crash_records(store)
    assert record['kind'] == 'client_crash' and record['channel'] == 'unclean_exit'
    assert record['classification'] == 'renderer_crash_suspected' and record['reason'] is None
    assert isinstance(record['owner'], int)
    assert record['freeze']['evidence']['usedJSHeapSize'] == 4_000_000_000
    assert record['errors'][0]['count'] == 3
    [signal] = signals
    assert signal['owner'] == record['owner']
    assert signal['page_instance_id'] == '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10'
    assert signal['kind'] == 'browser_renderer_crash'
    for fact in ('renderer_crash_suspected', 'Chrome/155', 'coordinator-1a390889aa954da2b18551d1b4a32625',
                 'stream active', 'last freeze 24300 ms', 'JS heap 3815/4096 MiB', '152340 DOM elements',
                 'grammarly, immersive-translate', '1 recent error(s), newest kind console'):
        assert fact in signal['detail'], fact
    # Error messages are page content: the stored record keeps them, the signal never does.
    assert '[sync] failed' not in signal['detail']
    # The owner lists it; another owner's crash record is never listed.
    store.append_report({**record, 'owner': record['owner'] + 1, 'pageInstanceId': 'other-owner'})
    status, listing = _call(_app(), 'get', _LIST)
    pages = [item.get('pageInstanceId') for item in listing.get('data', listing)['reports']]
    assert pages == ['0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10']


def test_discards_and_restores_are_recorded_but_never_signalled(store, signals):
    for classification in ('tab_discarded', 'session_restored'):
        status, _ = _call(_app(), 'post', _CRASH, json=_report(classification=classification))
        assert status == 200
    assert [record['classification'] for record in _crash_records(store)] == ['tab_discarded', 'session_restored']
    assert signals == []


@pytest.mark.parametrize('body', [
    _report(extra='field'),
    _report(classification='renderer_crash'),
    _report(pageInstanceId='../../etc/passwd'),
    _report(errors=[{'at': 1, 'kind': 'console', 'message': 'x' * 4001}]),
    _report(environment={'userAgent': 'UA', 'extensions': ['unknown-extension']}),
    {key: value for key, value in _report().items() if key != 'droppedErrors'},
])
def test_malformed_unclean_exit_reports_are_refused(store, signals, body):
    status, response = _call(_app(), 'post', _CRASH, json=body)
    assert status == 400 and response['ok'] is False
    assert _crash_records(store) == [] and signals == []


def test_oversized_reports_are_refused_on_both_channels(store, signals):
    from lib.logging_pkg.client_crash import MAX_BODY_BYTES
    padding = 'x' * MAX_BODY_BYTES
    status, _ = _call(_app(), 'post', _CRASH, json=_report(navigationType=padding))
    assert status == 413
    status, _ = _call(_app(), 'post', _BROWSER, data=json.dumps(_crash_upload(stack=padding)),
                      headers={'Content-Type': 'application/reports+json'})
    assert status == 413
    assert _crash_records(store) == [] and signals == []


def test_browser_crash_report_keeps_only_typed_fields(store, signals):
    upload = _crash_upload(
        reason='oom', is_top_level=True, visibility_state='visible', stack='at loop (app.js:1:2)',
        unknown_member='dropped',
        crash_report_api={'pageInstanceId': '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10', 'view': 'chat',
                          'conversationId': 'conv-a', 'streamActive': '1', 'freezeMs': '24300',
                          'secret': 'never stored'})
    upload.append({'type': 'deprecation', 'age': 1, 'url': 'http://localhost/', 'body': {'id': 'x'}})
    status, body = _call(_app(), 'post', _BROWSER, data=json.dumps(upload),
                         headers={'Content-Type': 'application/reports+json'})
    assert status == 200
    assert (body.get('data', body)['recorded'], body.get('data', body)['ignored']) == (1, 1)
    [record] = _crash_records(store)
    assert record['channel'] == 'reporting_api' and record['classification'] == 'renderer_crash'
    assert record['reason'] == 'oom' and record['isTopLevel'] is True
    assert record['url'] == 'http://localhost/'
    assert record['crashContext'] == {'pageInstanceId': '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10', 'view': 'chat',
                                      'conversationId': 'conv-a', 'streamActive': '1', 'freezeMs': '24300'}
    assert 'unknown_member' not in json.dumps(record) and 'never stored' not in json.dumps(record)
    [signal] = signals
    assert signal['kind'] == 'browser_renderer_oom'
    assert signal['page_instance_id'] == '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10'


def test_browser_reports_from_other_origins_or_without_page_id(store, signals):
    foreign = [{**_crash_upload(reason='oom')[0], 'url': 'https://evil.example/'}]
    status, body = _call(_app(), 'post', _BROWSER, data=json.dumps(foreign),
                         headers={'Content-Type': 'application/reports+json'})
    assert status == 200 and body.get('data', body)['ignored'] == 1
    # The Origin header names the reporting page when the browser sends one.
    status, _ = _call(_app(), 'post', _BROWSER, data=json.dumps(_crash_upload()),
                      headers={'Content-Type': 'application/reports+json', 'Origin': 'https://evil.example'})
    assert status == 200
    assert _crash_records(store) == []
    # A same-origin crash without a page instance id is evidence, not a signal.
    status, _ = _call(_app(), 'post', _BROWSER, data=json.dumps(_crash_upload(reason='unresponsive')),
                      headers={'Content-Type': 'application/reports+json', 'Origin': 'http://localhost'})
    assert status == 200
    [record] = _crash_records(store)
    assert record['pageInstanceId'] is None and signals == []


@pytest.mark.parametrize('payload', [
    '{"type": "crash"}', '[]', 'not json', json.dumps([{'type': 'crash', 'url': 'http://localhost/'}]),
    json.dumps(_crash_upload(reason='segfault')), json.dumps(_crash_upload(crash_report_api={'view': 3})),
])
def test_malformed_browser_uploads_are_refused(store, signals, payload):
    status, _ = _call(_app(), 'post', _BROWSER, data=payload,
                      headers={'Content-Type': 'application/reports+json'})
    assert status == 400
    assert _crash_records(store) == [] and signals == []


def test_main_page_names_the_same_origin_crash_endpoint():
    from tests._jsdom import require_shipped_graph
    require_shipped_graph()
    import routes.common as common
    from quart import Quart

    async def _get_twice():
        app = Quart(__name__, static_folder=None)
        app.register_blueprint(common.common_bp)
        client = app.test_client()
        return [await client.get('/') for _ in range(2)]  # assembled, then cached

    for response in asyncio.run(_get_twice()):
        assert response.status_code == 200
        assert response.headers['Reporting-Endpoints'] == 'crash-reporting="api/v4/client/browser-reports"'
        assert response.headers['Document-Policy'] == 'include-js-call-stacks-in-crash-reports'


def test_both_channels_require_authentication(store, signals):
    app = _app(authenticated=False)
    assert _call(app, 'post', _CRASH, json=_report())[0] == 401
    assert _call(app, 'post', _BROWSER, data=json.dumps(_crash_upload()),
                 headers={'Content-Type': 'application/reports+json'})[0] == 401
    assert _crash_records(store) == [] and signals == []


def test_auth_gate_grants_no_exception_to_the_crash_routes(store, signals, monkeypatch):
    """The gate stays default-deny: no public path, no open-mode CSRF bypass."""
    from lib import auth_mode
    from quart import Quart
    from routes.api_v1.auth import _is_public, attach_rate_headers, bearer_auth_before_request
    from routes.api_v1 import client_support as route

    def gated():
        app = Quart(__name__)
        app.before_request(bearer_auth_before_request)
        app.after_request(attach_rate_headers)
        app.register_blueprint(route.api_v1_client_support_bp)
        return app

    async def _public(path):
        async with Quart(__name__).test_request_context(path):
            return _is_public(path)

    for path in (_CRASH, _BROWSER, '/api/v4/client/crash-report', '/api/v4/client/browser-reports'):
        assert asyncio.run(_public(path)) is False, path
    upload = json.dumps(_crash_upload())
    monkeypatch.setenv('TOFU_AUTH_MODE', 'private')
    auth_mode.get_state(refresh=True)
    try:
        assert _call(gated(), 'post', _CRASH, json=_report())[0] == 401
        assert _call(gated(), 'post', _BROWSER, data=upload,
                     headers={'Content-Type': 'application/reports+json'})[0] == 401
        monkeypatch.setenv('TOFU_AUTH_MODE', 'open')
        auth_mode.get_state(refresh=True)
        # Control: the loopback same-origin page is admitted in open mode ...
        same_origin = {'Sec-Fetch-Site': 'same-origin'}
        assert _call(gated(), 'post', _CRASH, json=_report(classification='tab_discarded'),
                     headers=same_origin)[0] == 200
        store.current_path().unlink()
        # ... while another site's page is not.
        cross_site = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}
        assert _call(gated(), 'post', _CRASH, json=_report(), headers=cross_site)[0] == 401
        assert _call(gated(), 'post', _BROWSER, data=upload,
                     headers={'Content-Type': 'application/reports+json', **cross_site})[0] == 401
    finally:
        monkeypatch.delenv('TOFU_AUTH_MODE')
        auth_mode.get_state(refresh=True)
    assert _crash_records(store) == [] and signals == []
