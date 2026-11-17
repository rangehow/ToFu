"""routes/api_v1/client_support.py — native client support-report intake.

The Android shell's "Report a problem" flow builds a bounded diagnostic blob
(``com.tofu.client.session.ClientReport``) and POSTs it here when it can reach
the connected Tofu server. This route validates and bounds that blob, stamps
server-side arrival metadata, and hands it to
:mod:`lib.client_report_store`, which persists it under
``<data_root>/client_reports/`` for the operator to read.

  * ``POST /api/v1/mobile/client-report`` — ingest one report.
  * ``GET  /api/v1/mobile/client-report`` — list the newest stored reports, so
    the operator can read what was sent without shelling into the host.
    Owner-stamped browser crash records are listed only to their owner.
  * ``POST /api/v1/client/crash-report`` — the unclean-exit report a reloaded
    browser tab sends for its previous page instance (:mod:`lib.logging_pkg.client_crash`).
  * ``POST /api/v1/client/browser-reports`` — the same-origin Reporting API
    endpoint the main page names in ``Reporting-Endpoints``; Chrome sends the
    page's credentials to it, so it sits behind the ordinary auth gate.

The report is untrusted client input: every field is length-clipped, the body
is size-capped, and no field is ever interpreted as a path or command. Nothing
here is authenticated beyond the shared :func:`require_auth` gate, because a
report carries evidence the sender already chose to disclose.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from quart import Blueprint, request
from quart.utils import run_sync

from lib import client_report_store
from lib.api_response import api_bad_request, api_ok, api_payload_too_large
from lib.log import get_logger
from lib.logging_pkg import client_crash
from lib.openapi import api_meta, inline_schema_definition
from lib.request_parser import (
    BadRequest,
    async_parse_body,
    optional_dict,
    optional_int,
    optional_list,
    optional_str,
    parse_body,
)

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_client_support_bp = Blueprint('api_v1_client_support', __name__)

_REPORT_SCHEMA = 'tofu.client-report/v1'

# Bounds. The client already trims, but the server never trusts that: a
# compromised or future client must not be able to blow up memory or disk.
_MAX_BODY_BYTES = client_crash.MAX_BODY_BYTES
_MAX_NOTE_CHARS = 8000
_MAX_EVENTS = 300
_MAX_WEB_CHARS = 512 * 1024
_MAX_EVENT_MSG = 2000


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _clean_events(events: list[Any]) -> tuple[list[dict], bool]:
    truncated = len(events) > _MAX_EVENTS
    if truncated:
        events = events[-_MAX_EVENTS:]
    cleaned: list[dict] = []
    for event in events:
        cleaned.append({
            't': event.get('t'),
            'lvl': str(event.get('lvl', ''))[:4],
            'tag': str(event.get('tag', ''))[:64],
            'msg': str(event.get('msg', ''))[:_MAX_EVENT_MSG],
        })
    return cleaned, truncated


def _normalize(body: dict) -> tuple[dict, bool]:
    """Validate + bound an untrusted report body into a storable record."""
    truncated = False
    note, cut = _clip(optional_str(body, 'note'), _MAX_NOTE_CHARS)
    truncated = truncated or cut
    web, cut = _clip(optional_str(body, 'web'), _MAX_WEB_CHARS)
    truncated = truncated or cut
    events, cut = _clean_events(
        optional_list(body, 'events', item_type=dict, max_len=10_000),
    )
    truncated = truncated or cut
    record = {
        'schema': optional_str(body, 'schema', max_len=64) or _REPORT_SCHEMA,
        'reportId': optional_str(body, 'reportId', max_len=80),
        'clientCreatedAt': optional_str(body, 'createdAt', max_len=40),
        'receivedAt': int(time.time() * 1000),
        'note': note,
        'app': optional_dict(body, 'app'),
        'device': optional_dict(body, 'device'),
        'profile': optional_dict(body, 'profile'),
        'events': events,
        'web': web,
        'truncated': truncated,
    }
    return record, truncated


@api_v1_client_support_bp.route('/api/v1/mobile/client-report', methods=['POST'])
@require_auth
@api_meta(
    summary='Ingest a native client support report',
    description=(
        'Accepts the bounded diagnostic blob built by the Android shell\'s '
        '"Report a problem" flow and persists it under '
        '``<data_root>/client_reports/`` for the operator. Unknown fields are '
        'dropped; ``note``/``web``/``events`` are length-clipped.'),
    tags=['mobile'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'properties': {
                'schema': {'type': 'string'},
                'reportId': {'type': 'string'},
                'createdAt': {'type': 'string'},
                'note': {'type': 'string'},
                'app': {'type': 'object'},
                'device': {'type': 'object'},
                'profile': {'type': 'object'},
                'events': {'type': 'array', 'items': {'type': 'object'}},
                'web': {'type': 'string'},
            },
        },
    }}},
)
def mobile_client_report_post_v1():
    declared = request.content_length
    if declared is not None and declared > _MAX_BODY_BYTES:
        return api_payload_too_large(_MAX_BODY_BYTES)
    try:
        body = parse_body(strict=True, exact=(
            'schema', 'reportId', 'createdAt', 'note', 'app', 'device',
            'profile', 'events', 'web'))
    except BadRequest as error:
        return api_bad_request(str(error))
    if not body:
        return api_bad_request('empty report body')

    record, truncated = _normalize(body)
    if not record['reportId']:
        record['reportId'] = uuid.uuid4().hex[:12]
    record['remote'] = (request.remote_addr or '')[:64]

    stored = client_report_store.append_report(record)
    logger.info(
        '[client_support] report %s stored (%d bytes, truncated=%s)',
        record['reportId'], stored, truncated)
    return api_ok(reportId=record['reportId'], stored=stored,
                  truncated=truncated)


@api_v1_client_support_bp.route('/api/v1/mobile/client-report', methods=['GET'])
@require_auth
@api_meta(
    summary='List recent native client support reports',
    description=(
        'Newest-first view of the reports stored under '
        '``<data_root>/client_reports/`` so the operator can triage what '
        'testers sent. ``limit`` defaults to 20 and is capped at 200.'),
    tags=['mobile'],
    request_body=False,
)
def mobile_client_report_list_v1():
    # Clamp rather than reject: an over-large limit from a curious client
    # should return the cap, not a 400 that hides every report.
    limit = optional_int(
        {'limit': request.args.get('limit')}, 'limit', default=20, min=1)
    reports = client_report_store.read_recent(limit, owner=request_user_id())
    return api_ok(reports=reports, count=len(reports),
                  path=str(client_report_store.current_path()))


async def _bounded_body() -> bytes | None:
    declared = request.content_length
    if declared is not None and declared > _MAX_BODY_BYTES:
        return None
    data = await request.get_data()
    return None if len(data) > _MAX_BODY_BYTES else data


@api_v1_client_support_bp.route('/api/v1/client/crash-report', methods=['POST'])
@require_auth
@api_meta(
    summary='Record the unclean exit of a browser page instance',
    description=(
        'Sent once boot is ready by a page whose tab still held the previous '
        'page instance\'s alive marker (no clean pagehide): a suspected renderer '
        'crash, a tab discard or a session restore, with that instance\'s '
        'sessionStorage breadcrumbs. Stored as an owner-scoped '
        '``tofu.client-crash/v1`` record; a crash opens one Router Watchdog '
        'signal per page instance. Body bound: ``maxBodyBytes`` of '
        'contracts/client_crash_v1.schema.json.'),
    tags=['diagnostics'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': inline_schema_definition(client_crash.CONTRACT, 'clientCrashReport')}}},
)
async def client_crash_report_post_v1():
    if await _bounded_body() is None:
        return api_payload_too_large(_MAX_BODY_BYTES)
    try:
        body = await async_parse_body(strict=True)
        record = await run_sync(client_crash.record_client_report)(request_user_id(), body)
    except BadRequest as error:
        return api_bad_request(str(error), field=error.field)
    return api_ok(pageInstanceId=record['pageInstanceId'], classification=record['classification'])


@api_v1_client_support_bp.route('/api/v1/client/browser-reports', methods=['POST'])
@require_auth
@api_meta(
    summary='Receive same-origin Reporting API crash reports',
    description=(
        'The ``crash-reporting`` endpoint of the main page (Reporting-Endpoints). '
        'Body: ``application/reports+json`` (W3C Reporting API). Only reports of '
        'type ``crash`` describing a page of this origin are stored, typed fields '
        'only; other entries are acknowledged and dropped so the browser does not '
        'retry them. Body bound: ``maxBodyBytes`` of contracts/client_crash_v1.schema.json.'),
    tags=['diagnostics'],
    request_body={'required': True, 'content': {'application/reports+json': {
        'schema': inline_schema_definition(client_crash.CONTRACT, 'browserReportUpload')}}},
)
async def client_browser_reports_post_v1():
    data = await _bounded_body()
    if data is None:
        return api_payload_too_large(_MAX_BODY_BYTES)
    try:
        upload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return api_bad_request('Request body contains malformed JSON')
    try:
        records, ignored = await run_sync(client_crash.record_browser_reports)(
            request_user_id(), upload, origin_header=request.headers.get('Origin', ''),
            host=request.host or '')
    except BadRequest as error:
        return api_bad_request(str(error), field=error.field)
    return api_ok(recorded=len(records), ignored=ignored)
