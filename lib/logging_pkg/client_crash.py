"""lib/logging_pkg/client_crash.py — typed browser crash records and their Watchdog signal.

Responsibility: validate the two browser crash channels against
``contracts/client_crash_v1.schema.json``, store one owner-scoped record kind
(``schema: tofu.client-crash/v1``) in the client-report store, log it, and
report each crash episode to the owner's Router Watchdog. The channels are the
unclean-exit report a reloaded tab sends for its previous page instance
(``frontend/src/core/page-crash-journal.ts``) and Chrome's Reporting API crash
report (WICG crash-reporting), delivered with the page's own credentials
because the endpoint is same-origin. Classification separates crashes from
tab discards and session restores; application errors stay breadcrumbs.

Entry points: record_client_report, record_browser_reports, MAX_BODY_BYTES,
REPORTING_ENDPOINT_NAME, RECORD_SCHEMA.
Dependencies: lib.client_report_store, lib.error_envelope, jsonschema and
(lazily) lib.router.watchdog.signals; never imports routes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from lib import client_report_store
from lib.error_envelope import BadRequest
from lib.log import get_logger

logger = get_logger(__name__)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'client_crash_v1.schema.json').read_text(encoding='utf-8'))
RECORD_SCHEMA: str = CONTRACT['$id']
MAX_BODY_BYTES: int = CONTRACT['maxBodyBytes']
REPORTING_ENDPOINT_NAME: str = CONTRACT['reportingEndpointName']
_CRASH_CONTEXT_KEYS: dict[str, int] = CONTRACT['crashContextKeys']
_ID_PATTERN = re.compile(CONTRACT['$defs']['id']['pattern'])
_ID_LENGTH: int = CONTRACT['$defs']['id']['maxLength']

# Browser-reported reason -> Watchdog error kind (contracts/router_v1 watchdogClientCrashKind).
_SIGNAL_KINDS = {None: 'browser_renderer_crash', 'oom': 'browser_renderer_oom',
                 'unresponsive': 'browser_renderer_unresponsive'}
_SIGNALLED = frozenset({'renderer_crash', 'renderer_crash_suspected'})


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + name})


def _validate(value, name: str) -> None:
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or name
        raise BadRequest(f'Invalid {name} ({location}): {error.message}', field=location)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return '?'
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _page_url(url: str) -> str:
    """Origin and path only: the query of a page URL may carry credentials."""
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}{parts.path}'


def same_origin(report_url: str, *, origin_header: str, host: str) -> bool:
    """Whether a browser report describes a page of the origin it was posted to.

    Chrome attaches credentials only to same-origin uploads and Tofu answers no
    CORS preflight, so an authenticated browser upload always comes from a page
    of this origin; this rejects bodies inconsistent with that. Chrome names the
    reporting page's origin in the upload's ``Origin`` header
    (ReportingApiCorsOriginHeader, on by default), which also holds behind a
    proxy that rewrites ``Host``; without it the request host stands in.
    """
    page = urlsplit(report_url or '')
    if page.scheme not in ('http', 'https') or not page.netloc:
        return False
    if origin_header:
        origin = urlsplit(origin_header)
        return (origin.scheme, origin.netloc.lower()) == (page.scheme, page.netloc.lower())
    return page.netloc.lower() == (host or '').strip().lower()


def _detail(record: dict) -> str:
    """One line of typed facts for the Watchdog; the stored record is the evidence.

    Error messages and stacks can quote page content, so the signal names only
    how many errors there were and the newest one's kind.
    """
    if record['channel'] == 'reporting_api':
        reason = f" ({record['reason']})" if record.get('reason') else ''
        parts = [f"{record['classification']}: Chrome reported a renderer crash{reason} at about "
                 f"{_iso(record.get('crashedAt'))}, page {record.get('visibilityState') or 'visibility unknown'}"]
    else:
        parts = [f"{record['classification']}: page instance ended without a clean pagehide; noticed on "
                 f"{record['navigationType'] or 'load'} at {_iso(record['detectedAt'])}, "
                 f"last alive {_iso(record['lastSeenAt'])}"]
    environment = record.get('environment') or {}
    browser = ', '.join(environment.get('brands') or []) or environment.get('userAgent') or record.get('userAgent')
    if browser:
        parts.append(f"browser {browser}" + (f" on {environment['platform']}" if environment.get('platform') else ''))
    context = record.get('context') or record.get('crashContext') or {}
    if context.get('conversationId') or context.get('view'):
        stream = context.get('streamActive')
        active = stream is True or stream == '1'
        parts.append(f"view {context.get('view') or '?'}, conversation {context.get('conversationId') or '-'}"
                     f", {'stream active' if active else 'no active stream'}")
    evidence = (record.get('freeze') or {}).get('evidence') or {}
    if evidence:
        freeze = f"last freeze {evidence.get('durationMs', '?')} ms"
        if isinstance(evidence.get('usedJSHeapSize'), (int, float)) and evidence.get('jsHeapSizeLimit'):
            freeze += (f", JS heap {evidence['usedJSHeapSize'] / 2 ** 20:.0f}"
                       f"/{evidence['jsHeapSizeLimit'] / 2 ** 20:.0f} MiB")
        if evidence.get('domElements') is not None:
            freeze += f", {evidence['domElements']} DOM elements"
        parts.append(freeze)
    if environment.get('extensions'):
        parts.append('extensions ' + ', '.join(environment['extensions']))
    errors = record.get('errors') or []
    if errors:
        parts.append(f"{len(errors)} recent error(s)"
                     + (f" ({record['droppedErrors']} older dropped)" if record.get('droppedErrors') else '')
                     + f", newest kind {errors[-1]['kind']}")
    return '; '.join(parts)


def _hint(record: dict) -> str:
    return (f"Evidence: client-report records with pageInstanceId {record['pageInstanceId']} "
            f"(GET /api/v4/mobile/client-report, schema {RECORD_SCHEMA}). A browser crash report "
            "for the same page may follow and carry Chrome's reason (oom, unresponsive).")


def _store(owner: int, record: dict) -> dict:
    client_report_store.append_report(record)
    context = record.get('context') or record.get('crashContext') or {}
    log = logger.warning if record['classification'] in _SIGNALLED else logger.info
    log('[client_crash] %s channel=%s page=%s reason=%s view=%s conversation=%s owner=%s',
        record['classification'], record['channel'], record.get('pageInstanceId') or '-',
        record.get('reason') or '-', context.get('view') or '-', context.get('conversationId') or '-', owner)
    if record['classification'] in _SIGNALLED and record.get('pageInstanceId'):
        try:
            from lib.router.watchdog.signals import report_client_crash
            report_client_crash(owner, page_instance_id=record['pageInstanceId'],
                                kind=_SIGNAL_KINDS[record.get('reason')], detail=_detail(record),
                                hint=_hint(record))
        except Exception:
            # The signal is a best-effort wake; the stored record stays authoritative.
            logger.warning('[client_crash] Watchdog report failed page=%s owner=%s',
                           record['pageInstanceId'], owner, exc_info=True)
    return record


def record_client_report(owner: int, body, *, received_at: int | None = None) -> dict:
    """Store one unclean-exit report of a previous page instance (BadRequest if invalid)."""
    _validate(body, 'clientCrashReport')
    record = {
        'schema': RECORD_SCHEMA, 'kind': 'client_crash', 'owner': owner, 'channel': 'unclean_exit',
        'pageInstanceId': body['pageInstanceId'], 'classification': body['classification'], 'reason': None,
        'receivedAt': received_at or _now_ms(),
        **{field: body[field] for field in ('startedAt', 'lastSeenAt', 'detectedAt', 'navigationType',
                                            'environment', 'context', 'freeze', 'errors', 'droppedErrors')},
    }
    return _store(owner, record)


def record_browser_reports(owner: int, upload, *, origin_header: str, host: str,
                           received_at: int | None = None) -> tuple[list[dict], int]:
    """Store the same-origin ``crash`` entries of one Reporting API upload.

    Returns ``(records, ignored)``: other report types and other origins are
    ignored and never stored. Only the typed crash fields are kept; the
    window.crashReport context keeps only the contract's declared keys.
    """
    _validate(upload, 'browserReportUpload')
    now = received_at or _now_ms()
    records, ignored = [], 0
    for report in upload:
        if report['type'] != 'crash' or not same_origin(report['url'], origin_header=origin_header, host=host):
            ignored += 1
            continue
        body = report.get('body') or {}
        _validate(body, 'browserCrashBody')
        api = body.get('crash_report_api') or {}
        context = {key: api[key][:length] for key, length in _CRASH_CONTEXT_KEYS.items()
                   if isinstance(api.get(key), str)}
        page_instance_id = context.get('pageInstanceId', '')
        record = {
            'schema': RECORD_SCHEMA, 'kind': 'client_crash', 'owner': owner, 'channel': 'reporting_api',
            'pageInstanceId': (page_instance_id if _ID_PATTERN.fullmatch(page_instance_id)
                               and len(page_instance_id) <= _ID_LENGTH else None),
            'classification': 'renderer_crash', 'reason': body.get('reason'),
            'receivedAt': now, 'crashedAt': now - int(report.get('age') or 0),
            'url': _page_url(report['url']), 'userAgent': report.get('user_agent', ''),
            'isTopLevel': body.get('is_top_level'), 'visibilityState': body.get('visibility_state'),
            'stack': body.get('stack'), 'crashContext': context,
        }
        records.append(_store(owner, record))
    return records, ignored


__all__ = ['CONTRACT', 'MAX_BODY_BYTES', 'RECORD_SCHEMA', 'REPORTING_ENDPOINT_NAME',
           'record_browser_reports', 'record_client_report', 'same_origin']
