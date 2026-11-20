"""Executable SQLite-only candidate; deliberately absent from production catalogs.

The existing assignment commit/receipt owns every derived write. No full-writer
coverage capability exists, so reads are positive-only, including empty reads.
"""
from copy import deepcopy
import hashlib
import json
import re

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _project_dispatch as dispatch
from lib.storage_authority_sql.operations_pkg._records import (
    _record_get_owned, _record_put_owned,
)

FORMAT = 'tofu.board-membership-positive/v1'
PREFIX = 'board-positive-v1:'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def fail(message):
    raise StorageError('database_integrity', message)


def linkage(envelope, owner, project):
    record = envelope.get('record', {})
    request, grant = record.get('request', {}), record.get('grant', {})
    if not request:
        return None
    if grant.get('owner') != owner or request.get('projectPath') != project:
        fail('Assignment differs from committed owner/project')
    return {'assignmentId': record['id'], 'request': deepcopy(request),
            'bindingRevision': grant.get('bindingRevision'), 'owner': owner,
            'projectPath': project, 'sequence': envelope['sequence']}


def selected(link):
    if link is None:
        return False
    epoch = link['bindingRevision']
    return (type(epoch) is int and epoch > 0
            and isinstance(link['request'].get('coordinatorId'), str)
            and isinstance(link['request'].get('boardTaskKeys'), list))


def scope_prefix(coordinator, binding, task):
    return PREFIX + digest([coordinator, binding, task]) + ':'


def index_payload(payload, identity):
    return {**payload, 'collection': 'indexes', 'id': identity}


def put_index(session, payload, identity, value):
    _, _, scope = dispatch._scope(payload)
    row = {'namespace': dispatch.DOCUMENTS, 'key': f'{scope}:indexes:{identity}'}
    old = _record_get_owned(session, row)
    return _record_put_owned(session, {**row, 'expected_version': old['version'] if old else 0,
                                      'value': value})


def commit(session, payload):
    owner, project, _ = dispatch._scope(payload)
    assignments = []
    for change in payload.get('changes', []):
        if change.get('collection') == 'indexes' and change.get('id', '').startswith(PREFIX):
            raise StorageError('database_protocol_error', 'Derived candidate indexes are authority-owned')
        if change.get('collection') != 'assignments' or 'value' not in change:
            continue
        old = dispatch.get(session, {**payload, 'collection': 'assignments', 'id': change['id']})
        link = linkage(change['value'], owner, project)
        if old is not None and digest(linkage(old['value'], owner, project)) != digest(link):
            fail('Immutable assignment linkage changed')
        assignments.append((change['id'], link))
    receipt = dispatch.commit(session, payload)
    for identity, link in assignments:
        marker_id = PREFIX + 'source:' + identity
        marker = dispatch.get(session, index_payload(payload, marker_id))
        if marker is not None:
            if digest(marker['value']) != digest({'format': FORMAT, 'linkage': link}):
                fail('Candidate source marker changed')
            continue
        put_index(session, payload, marker_id, {'format': FORMAT, 'linkage': link})
        if not selected(link):
            continue
        source_digest = digest(link)
        for task in link['request']['boardTaskKeys']:
            prefix = scope_prefix(link['request']['coordinatorId'], link['bindingRevision'], task)
            posting_id = prefix + 'p:' + identity
            put_index(session, payload, posting_id, {'format': FORMAT, 'sourceId': identity,
                                                    'sourceLinkageDigest': source_digest})
            count_id = prefix + 'count'
            count = dispatch.get(session, index_payload(payload, count_id))
            if count is not None and (count['value'].get('format') != FORMAT
                    or type(count['value'].get('count')) is not int or count['value']['count'] < 0):
                fail('Unsupported membership count')
            put_index(session, payload, count_id, {'format': FORMAT,
                'count': (count['value']['count'] if count else 0) + 1})
    return receipt


def query(session, payload):
    if payload.get('format') != FORMAT:
        raise StorageError('database_protocol_error', 'Unknown membership candidate version')
    owner, project, _ = dispatch._scope(payload)
    coordinator, binding, task = (payload.get(key) for key in ('coordinator_id', 'binding_revision', 'task_key'))
    if (not isinstance(coordinator, str) or not coordinator or type(binding) is not int or binding < 1
            or not isinstance(task, str) or not re.fullmatch('[0-9a-f]{32}', task)):
        raise StorageError('database_protocol_error', 'Exact binding and board key required')
    prefix = scope_prefix(coordinator, binding, task)
    count = dispatch.get(session, index_payload(payload, prefix + 'count'))
    matches, after = [], ''
    while True:
        rows = dispatch.list_documents(session, {**payload, 'collection': 'indexes',
                    'prefix': prefix + 'p:', 'after_key': after})
        if not rows:
            break
        for row in rows:
            posting = row['value']
            if posting.get('format') != FORMAT:
                fail('Unknown posting version')
            source = dispatch.get(session, {**payload, 'collection': 'assignments', 'id': posting['sourceId']})
            link = linkage(source['value'], owner, project) if source else None
            if (digest(link) != posting['sourceLinkageDigest'] or not selected(link)
                    or link['bindingRevision'] != binding
                    or link['request']['coordinatorId'] != coordinator
                    or task not in link['request']['boardTaskKeys']):
                fail('Posting differs from immutable source')
            matches.append({'sourceId': posting['sourceId'], 'sourceVersion': source['version'],
                            'assignment': source['value']['record']})
            after = row['key']
    if count is not None and (count['value'].get('format') != FORMAT
                              or type(count['value'].get('count')) is not int
                              or count['value'].get('count') != len(matches)):
        fail('Membership posting count differs')
    if matches and count is None:
        fail('Membership posting count missing')
    return {'format': FORMAT, 'coverage': 'unknown', 'complete': False,
            'reason': 'full-writer-capability-unproven', 'matches': matches}
