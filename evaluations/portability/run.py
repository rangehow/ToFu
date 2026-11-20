"""Run scenario x model x arm cells and append one JSONL record per task.

Responsibility: execute a declared Scenario's frozen tasks for one subject model
and one arm, score each artifact with the scenario's single metric (and its fixed
judge, when model-judged), and append resumable records. A cell is the unit a
compute backend runs (``python -m evaluations.portability cell``), so the same
harness runs inline, as a local process, over SSH, on Slurm or on Kubernetes.

Entry points: ``run_cell``, ``completed_keys``.
Dependencies: scenario.py, models.py.
"""
from __future__ import annotations

import hashlib
import json
import importlib.metadata
import os
import sys
import platform
import tempfile
import time
import traceback
import uuid
from pathlib import Path

from evaluations.portability import models
from evaluations.portability.journal import read_journal
from evaluations.portability.models import ModelClient, ModelInfrastructureError
from evaluations.portability.scenario import Scenario


def actual_environment() -> dict:
    packages = []
    for dist in importlib.metadata.distributions():
        metadata = dist.metadata
        packages.append((metadata.get('Name') or '', metadata.get('Version') or ''))
    packages.sort()
    return {'host': platform.node(), 'system': platform.system(),
            'release': platform.release(), 'machine': platform.machine(),
            'python': platform.python_version(), 'implementation': platform.python_implementation(),
            'python_executable': os.path.realpath(sys.executable),
            'packages_digest': hashlib.sha256(json.dumps(packages).encode()).hexdigest(),
            'endpoint': models.ENDPOINT}


def environment_provenance(environment: str) -> dict:
    measured = actual_environment()
    return {'environment': environment, 'actual_environment': measured,
            'environment_fingerprint': hashlib.sha256(
                json.dumps(measured, sort_keys=True).encode()).hexdigest()}


def artifact_binding(record: dict) -> dict:
    fields = ('scenario', 'version', 'comparison_version', 'metric', 'judge_model',
              'tasks_digest', 'environment', 'actual_environment', 'environment_fingerprint',
              'model', 'arm', 'task', 'subject_deployment', 'judge_deployment', 'score',
              'attempt_id', 'artifact_path')
    return {field: record.get(field) for field in fields}


def valid_artifact(results: Path, record: dict) -> bool:
    if not all(isinstance(record.get(key), str) and record[key]
               for key in ('attempt_id', 'artifact_path', 'artifact_sha256')):
        return False
    try:
        root = results.parent.resolve()
        path = (root / record['artifact_path']).resolve()
        if not path.is_relative_to(root / 'artifacts'):
            return False
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record['artifact_sha256']:
            return False
        envelope = json.loads(content)
        # Require the payload member without narrowing the serialized payload type.
        return (isinstance(envelope, dict) and 'artifact' in envelope
                and json.dumps(envelope['binding'], sort_keys=True, allow_nan=False)
                == json.dumps(artifact_binding(record), sort_keys=True, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


def completed_keys(results: Path, scenario: Scenario, *, environment: str = 'inline',
                   deployment_identities=None) -> set:
    if not deployment_identities or not results.exists():
        return set()
    # Use the same validated prefix as reporting. A malformed suffix cannot
    # confer completion, even if its artifact bytes or a later row match.
    records, _ = read_journal(results)
    return _completed_keys(results, scenario, records, environment, deployment_identities)


def _completed_keys(results: Path, scenario: Scenario, records: list[dict],
                    environment: str, deployment_identities: dict) -> set:
    if not records:
        return set()
    done = set()
    provenance = {**scenario.provenance(), **environment_provenance(environment)}
    tasks = {task['id'] for task in scenario.tasks}
    arms = scenario.arms()
    for record in records:
        if (record['scenario'] == scenario.id
                and all(record.get(k) == v for k, v in provenance.items())
                and record['task'] in tasks
                and record['arm'] in arms
                and record.get('subject_deployment') is not None
                and record.get('subject_deployment') == deployment_identities.get(record['model'])
                and record.get('judge_deployment') == deployment_identities.get(scenario.judge_model)
                and (scenario.judge_model is None or record.get('judge_deployment') is not None)
                and record.get('error_kind') is None
                and valid_artifact(results, record)):
            done.add((record['scenario'], record['model'], record['arm'], record['task']))
    return done


def _sync_directories(directory: Path, boundary: Path) -> None:
    """Persist directory entries from the leaf through the output root's existing parent."""
    if not directory.is_relative_to(boundary):
        raise ValueError(f'Directory sync boundary {boundary} is not an ancestor of {directory}')
    while True:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if directory == boundary:
            return
        directory = directory.parent


def _persist_artifact(out_dir: Path, artifacts: Path, boundary: Path,
                      record: dict, artifact) -> dict:
    deployment = hashlib.sha256(json.dumps(
        [record['subject_deployment'], record['judge_deployment']]).encode()).hexdigest()
    destination = artifacts / deployment
    attempt_id = uuid.uuid4().hex
    artifact_path = destination / f'{attempt_id}.json'
    link = {'attempt_id': attempt_id, 'artifact_path': artifact_path.relative_to(out_dir).as_posix()}
    content = json.dumps({'binding': artifact_binding({**record, **link}), 'artifact': artifact},
                         default=str).encode('utf-8')
    destination.mkdir(parents=True, exist_ok=True)
    with artifact_path.open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    # Sync all artifact directory entries, not just the file. Do not expose a
    # journal link if any of these operations failed; an orphan can be ignored.
    _sync_directories(destination, boundary)
    return {**link, 'artifact_sha256': hashlib.sha256(content).hexdigest()}


def _record_failure(record: dict, message: str, kind: str) -> None:
    record.update(error=message[:2000], error_kind=kind,
                  score=0.0 if kind == 'agent' else None,
                  traceback=traceback.format_exc()[-4000:])


def run_cell(scenario: Scenario, model: str, arm: str, out_dir: Path, *, creator: str | None = None,
             environment: str = 'inline', identity_reader=None, sync_boundary: Path | None = None) -> Path:
    results = out_dir / 'results.jsonl'
    records = []
    if results.exists():
        records, issues = read_journal(results)
        if issues:
            issue = issues[0]
            raise ValueError(f'Cannot append to {results}: line {issue["line"]} '
                             f'({issue["kind"]}). Use a fresh output directory (--out).')
    out_dir = out_dir.resolve()
    boundary = sync_boundary or out_dir.parent
    while not boundary.is_dir():
        boundary = boundary.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    subject = ModelClient(model, identity_reader=identity_reader)
    judge = ModelClient(scenario.judge_model, identity_reader=identity_reader) if scenario.judge_model else None
    identities = {model: subject.current_identity()}
    if judge is not None:
        identities[scenario.judge_model] = judge.current_identity()
    done = _completed_keys(results, scenario, records, environment, identities)
    provenance = {**scenario.provenance(), **environment_provenance(environment)}
    run_arm = scenario.arms()[arm]
    comparison = hashlib.sha256(json.dumps(
        {'scenario': scenario.id, **provenance}, sort_keys=True).encode()).hexdigest()
    cell = hashlib.sha256(json.dumps([model, arm]).encode()).hexdigest()
    artifacts = (out_dir / 'artifacts' / provenance['environment_fingerprint']
                 / comparison / cell)
    for task in scenario.tasks:
        key = (scenario.id, model, arm, task['id'])
        if key in done:
            continue
        started = time.monotonic()
        before = dict(subject.usage)
        judge_before = judge.usage['calls'] if judge else 0
        record = {'scenario': scenario.id, **provenance,
                  'model': model, 'creator': creator, 'arm': arm, 'task': task['id'],
                  'environment': environment, 'host': platform.node()}
        with tempfile.TemporaryDirectory(prefix='portability-') as scratch:
            try:
                artifact = run_arm(task, subject, scratch)
                record['score'] = float(scenario.score(task, artifact, judge))
                if not 0.0 <= record['score'] <= 1.0:
                    raise ValueError(f'score {record["score"]} outside [0, 1]')
                record['error'] = None
                record['error_kind'] = None
                record['subject_deployment'] = (subject.deployment_identity
                    if subject.usage['calls'] > before['calls'] else None)
                record['judge_deployment'] = (judge.deployment_identity
                    if judge and judge.usage['calls'] > judge_before else None)
            except Exception as exc:  # an arm failure is a measured outcome unless infrastructure
                message = f'{type(exc).__name__}: {exc}'
                kind = 'infrastructure' if isinstance(exc, ModelInfrastructureError) else 'agent'
                _record_failure(record, message, kind)
            else:
                try:
                    record.update(_persist_artifact(out_dir, artifacts, boundary, record, artifact))
                except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
                    _record_failure(record, f'Artifact persistence failed: {type(exc).__name__}: {exc}',
                                    'infrastructure')
        record['seconds'] = round(time.monotonic() - started, 3)
        record['usage'] = {k: subject.usage[k] - before[k] for k in subject.usage}
        # A journal failure cannot be reliably recorded in the same journal.
        # Propagate it, rather than trying to write a replacement scored row.
        with results.open('a') as stream:
            stream.write(json.dumps(record) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directories(out_dir, boundary)
    return results
