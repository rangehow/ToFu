"""The browser JS-error gate treats each engine's request cancellation alike.

A navigation, reload or page close cancels in-flight requests (the
conversation-events stream, settings reads, lazy Vite chunks). Each engine
reports that one event in its own words; every other request failure stays a
hard finding, in every engine.
"""
from __future__ import annotations

import pytest

from tests.conftest import _attach_js_error_capture, _is_cancelled_request_failure

pytestmark = pytest.mark.unit

_CANCELLED = {
    'chromium': ['net::ERR_ABORTED'],
    'firefox': ['NS_BINDING_ABORTED', 'NS_ERROR_ABORT'],
    'webkit': ['Load request cancelled'],
}
_REAL_FAILURES = {
    'chromium': ['net::ERR_CONNECTION_REFUSED', 'net::ERR_FAILED', 'net::ERR_EMPTY_RESPONSE',
                 'net::ERR_NAME_NOT_RESOLVED', 'net::ERR_ABORTED_BY_PROXY', ''],
    'firefox': ['NS_ERROR_CONNECTION_REFUSED', 'NS_ERROR_NET_RESET', 'NS_ERROR_FAILURE',
                'NS_ERROR_UNKNOWN_HOST', 'NS_ERROR_NET_INTERRUPT', ''],
    'webkit': ['Could not connect to server', 'The network connection was lost.',
               'Load request cancelled because of a timeout', 'cancelled', ''],
}


@pytest.mark.parametrize('engine', sorted(_CANCELLED))
def test_each_engine_ignores_only_its_own_cancellation(engine):
    for failure in _CANCELLED[engine]:
        assert _is_cancelled_request_failure(engine, failure), failure
    for failure in _REAL_FAILURES[engine]:
        assert not _is_cancelled_request_failure(engine, failure), failure
    for other, failures in _CANCELLED.items():
        if other != engine:
            for failure in failures:
                assert not _is_cancelled_request_failure(engine, failure), (other, failure)


class _Request:
    def __init__(self, url, failure):
        self.url, self.failure = url, failure


class _Page:
    def __init__(self, engine):
        browser_type = type('BrowserType', (), {'name': engine})()
        browser = type('Browser', (), {'browser_type': browser_type})()
        self.context = type('Context', (), {'browser': browser})()
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


@pytest.mark.parametrize('engine', sorted(_CANCELLED))
def test_page_capture_reports_real_failures_and_drops_cancellations(engine):
    page = _Page(engine)
    _attach_js_error_capture(page)
    failed = page.handlers['requestfailed']
    for failure in _CANCELLED[engine]:
        failed(_Request('http://127.0.0.1:1/api/v4/conversation-events', failure))
    assert page._tofu_js_errors == []
    real = _REAL_FAILURES[engine][0]
    failed(_Request('http://127.0.0.1:1/static/vite/assets/main.js', real))
    assert page._tofu_js_errors == [
        f'request failed: http://127.0.0.1:1/static/vite/assets/main.js ({real})']


_PRESENTATIONS = '/api/v4/automations/conversations/c1/attempts/a1/presentations'


@pytest.mark.parametrize('reported', [
    f'/127.0.0.1:40907{_PRESENTATIONS} due to access control checks.',
    f'Fetch API cannot load http://127.0.0.1:40907{_PRESENTATIONS} due to access control checks.',
])
def test_webkit_same_origin_access_control_report_is_its_navigation_cancel(reported):
    """A same-origin load never runs CORS: WebKit uses this text for a load it cancelled."""
    page = _Page('webkit')
    page.url = 'http://127.0.0.1:40907/'
    _attach_js_error_capture(page)
    page.handlers['pageerror'](Exception(reported))
    assert page._tofu_js_errors == []


@pytest.mark.parametrize('engine, page_url, reported', [
    ('webkit', 'http://127.0.0.1:40907/',
     'Fetch API cannot load https://api.example.com/v1 due to access control checks.'),
    ('webkit', 'http://127.0.0.1:40907/',
     f'/127.0.0.1:1{_PRESENTATIONS} due to access control checks.'),
    ('webkit', 'http://127.0.0.1:40907/', "TypeError: undefined is not an object (evaluating 'x.y')"),
    ('chromium', 'http://127.0.0.1:40907/',
     f'/127.0.0.1:40907{_PRESENTATIONS} due to access control checks.'),
])
def test_every_other_uncaught_exception_stays_hard(engine, page_url, reported):
    page = _Page(engine)
    page.url = page_url
    _attach_js_error_capture(page)
    page.handlers['pageerror'](Exception(reported))
    assert page._tofu_js_errors == [f'uncaught exception: {reported}']
