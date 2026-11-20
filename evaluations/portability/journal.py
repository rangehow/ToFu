"""Decode journal rows and recover the readable LF-terminated prefix.

Collection stages the backend's verbatim journal under a reserved discovery
prefix. Reading never edits the journal or asserts that LF implies fsync.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

COLLECTION_STAGE_PREFIX = '.portability-collect-'
COMPARISON_FIELDS = ('version', 'metric', 'judge_model', 'tasks_digest', 'comparison_version',
                     'environment', 'environment_fingerprint', 'actual_environment')
BINDING_FIELDS = ('attempt_id', 'artifact_path', 'artifact_sha256')
_REQUIRED_IDS = ('scenario', 'model', 'arm', 'task')
_COMPARISON_TEXT = ('version', 'metric', 'comparison_version', 'environment')
_HISTORICAL_ERROR_SLICE = 2000  # The old writer stored message[:2000].
_HISTORICAL_ENAMETOOLONG = 'OSError: [Errno 36] '
_HISTORICAL_PERSISTENCE_FRAME = re.compile(
    r'^  File "(?:[^"\n]*/)?evaluations/portability/run\.py", line \d+, in run_cell$')
_HISTORICAL_PERSISTENCE_LINES = {
    'destination.mkdir(parents=True, exist_ok=True)',
    "with artifact_path.open('x') as stream:",
    "with artifact_path.open('xb') as stream:",
    "stream.write(json.dumps(artifact, default=str)[:4_000_000])",
    "content = json.dumps({'binding': artifact_binding(record), 'artifact': artifact},",
    'stream.write(content)', 'stream.flush()', 'os.fsync(stream.fileno())',
    '''(artifacts / f'{task["id"]}.json').write_text(json.dumps(artifact, default=str)[:4_000_000])''',
}


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _digest(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{64}', value))


def _environment_matches(record: dict) -> bool:
    measured = record.get('actual_environment')
    return (isinstance(measured, dict) and bool(measured)
            and all(_text(key) and isinstance(value, str) for key, value in measured.items())
            and record.get('environment_fingerprint') == hashlib.sha256(
                json.dumps(measured, sort_keys=True).encode()).hexdigest())


def comparison_namespace(record: dict) -> dict | None:
    if (not all(_text(record.get(field)) for field in (*_REQUIRED_IDS, *_COMPARISON_TEXT))
            or 'judge_model' not in record
            or (record['judge_model'] is not None and not _text(record['judge_model']))
            or not _digest(record.get('tasks_digest'))):
        return None
    if not _environment_matches(record):
        return None
    return {'scenario': record['scenario'], **{field: record[field] for field in COMPARISON_FIELDS}}


def historical_persistence_failure(record: dict) -> bool:
    """Require the writer's explicit marker or a matching historical writer frame.

    A bare errno, free-text storage wording or a call name in an arm traceback
    is not provenance. Do not override a complete artifact binding.
    """
    error, trace = record.get('error'), record.get('traceback')
    if (not isinstance(error, str) or all(field in record for field in BINDING_FIELDS)
            or record.get('error_kind') not in ('agent', 'infrastructure')):
        return False
    if error.startswith('Artifact persistence failed: '):
        return True
    if not isinstance(trace, str):
        return False
    lines = trace.splitlines()
    # The first frame belongs to the run_cell try block; later frames can be
    # arbitrary user scenario code. The historical source statement matters.
    if (len(lines) < 4 or lines[0] != 'Traceback (most recent call last):'
            or not _HISTORICAL_PERSISTENCE_FRAME.fullmatch(lines[1])
            or lines[2].strip() not in _HISTORICAL_PERSISTENCE_LINES):
        return False
    terminal_error = lines[-1]
    return (terminal_error == error
            or (terminal_error.startswith(_HISTORICAL_ENAMETOOLONG)
                and len(terminal_error) > _HISTORICAL_ERROR_SLICE
                and terminal_error[:_HISTORICAL_ERROR_SLICE] == error))


def _artifact_reference(reference) -> bool:
    if not _text(reference) or '\\' in reference or '\x00' in reference:
        return False
    path = PurePosixPath(reference)
    return (path.parts[:1] == ('artifacts',) and len(path.parts) > 1 and '..' not in path.parts
            and path.as_posix() == reference and path.name != 'results.jsonl')


def _row_is_valid(record: dict) -> bool:
    if not all(_text(record.get(field)) for field in _REQUIRED_IDS):
        return False
    for field in ('creator', 'error', 'traceback', 'host'):
        if record.get(field) is not None and not isinstance(record[field], str):
            return False
    score, kind, error = record.get('score'), record.get('error_kind'), record.get('error')
    if 'score' not in record or not (
        (kind is None and error is None and type(score) in (int, float) and 0 <= score <= 1)
        or (kind == 'agent' and _text(error) and type(score) in (int, float) and score == 0)
        or (kind == 'infrastructure' and _text(error) and score is None)
    ):
        return False
    if any(record.get(field) is not None and not _digest(record[field])
           for field in ('subject_deployment', 'judge_deployment')):
        return False
    if record.get('judge_model') is None and record.get('judge_deployment') is not None:
        return False
    if any(field in record and not _text(record[field]) for field in _COMPARISON_TEXT):
        return False
    if record.get('judge_model') is not None and not _text(record['judge_model']):
        return False
    if 'tasks_digest' in record and not _digest(record['tasks_digest']):
        return False
    if {'actual_environment', 'environment_fingerprint'} & record.keys() and not _environment_matches(record):
        return False
    if 'seconds' in record and (type(record['seconds']) not in (int, float) or record['seconds'] < 0):
        return False
    if 'usage' in record and (not isinstance(record['usage'], dict)
                              or not all(_text(k) and type(v) is int and v >= 0
                                         for k, v in record['usage'].items())):
        return False
    binding_claimed = 'attempt_id' in record or 'artifact_sha256' in record
    modern = binding_claimed or any(field in record for field in ('subject_deployment', 'judge_deployment'))
    if (modern or all(field in record for field in COMPARISON_FIELDS)) and comparison_namespace(record) is None:
        return False
    # Legacy writers may have only a path (or a partial binding with proven
    # persistence failure). Neither is evidence of a verified artifact.
    if binding_claimed:
        validators = {'attempt_id': _text, 'artifact_path': _artifact_reference, 'artifact_sha256': _digest}
        if any(not validators[field](record[field]) for field in BINDING_FIELDS if field in record):
            return False
        return ((kind is None and all(field in record for field in BINDING_FIELDS))
                or historical_persistence_failure(record))
    return 'artifact_path' not in record or _text(record['artifact_path'])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if number in (float('inf'), float('-inf')):
        raise ValueError('nonfinite JSON number')
    return number


def _reject_constant(value):
    raise ValueError(f'non-JSON number: {value}')


def read_journal(path: Path) -> tuple[list[dict], list[dict]]:
    """Return the readable prefix and the location of the first invalid row."""
    records = []
    with path.open('rb') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith(b'\n'):
                kind = 'incomplete_final_row'
            elif not line.strip():
                continue
            else:
                try:
                    row = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_object,
                                     parse_float=_finite_float, parse_constant=_reject_constant)
                except (ValueError, RecursionError):
                    row = None
                if isinstance(row, dict) and _row_is_valid(row):
                    records.append(row)
                    continue
                kind = 'malformed_row'
            return records, [{'path': str(path), 'line': line_number, 'kind': kind}]
    return records, []
