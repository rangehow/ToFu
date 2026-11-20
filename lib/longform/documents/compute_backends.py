"""Host-bound experiment cells on the current durable JobService authority.

No backend factory, retry journal or scheduler. The embedding supplies Actor,
exact-plan authority, actual immutable source bundles and sealed job bindings.
JobService owns pre-submit claims, persisted cancellation and reconciliation.
Collected receipt bytes, not a re-opened local JSON file, feed the analysis.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
import time
from dataclasses import dataclass, replace
from pathlib import Path

RESULT_NAME = 'result.json'


@dataclass(frozen=True)
class Cell:
    name: str
    argv: tuple[str, ...]
    out: Path
    operation_id: str
    plan_hash: str
    provider: str
    project: str
    scientific_plan_sha256: str
    audited_code_sha256: str
    source_sha256: str
    attempt: int
    source_uri: str
    source_code: bytes
    scientific_plan: dict
    arm: str
    seed: int


def _decode_result(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate result JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError('Nonfinite result JSON value')

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Nonfinite result JSON value')
        return number
    return json.loads(payload, object_pairs_hook=unique, parse_constant=nonfinite, parse_float=finite)


@dataclass(frozen=True)
class _Publication:
    created: bool
    durability_error: str | None = None
    cleanup_errors: tuple[str, ...] = ()

    def with_cleanup_error(self, label, error):
        return replace(self, cleanup_errors=(*self.cleanup_errors, f'{label}: {error}'))


def _publish_output(path, payload, directory, allowed):
    """Return visibility and sync separately; the Stop check and link are not atomic."""
    temporary = '.owned-result-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                 0o600, dir_fd=directory.fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        publication = _link_output(path, payload, directory, temporary) if allowed() else None
    except BaseException as primary:
        try:
            os.unlink(temporary, dir_fd=directory.fd)
        except OSError as cleanup:
            raise primary from cleanup
        raise
    try:
        os.unlink(temporary, dir_fd=directory.fd)
    except OSError as cleanup:
        if publication is None:
            raise
        publication = publication.with_cleanup_error('Temporary file cleanup failed', cleanup)
    return publication


def _link_output(path, payload, directory, temporary):
    created = True
    try:
        os.link(temporary, path.name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd,
                follow_symlinks=False)
    except FileExistsError:
        created = False
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory.fd)
        with os.fdopen(descriptor, 'rb') as stream:
            if stream.read(len(payload) + 1) != payload:
                raise ValueError('Existing output differs from owned result')
            try:
                os.fsync(stream.fileno())
            except OSError as exc:
                return _Publication(created, f'Output file sync failed: {exc}')
    try:
        os.fsync(directory.fd)
    except OSError as exc:
        # The final name is already visible. Do not unlink it or claim it
        # survived a crash. A later pass can verify bytes and retry sync.
        return _Publication(created, f'Output directory sync failed: {exc}')
    return _Publication(created)


def _same_json_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_same_json_value, left, right))
    return left == right


def validate_observation(data, arm, seed, metric_names):
    """One scientific observation contract for fresh collection and resume."""
    if (not isinstance(data, dict) or data.get('arm') != arm
            or type(data.get('seed')) is not int or data['seed'] != seed):
        raise ValueError('Collected arm/seed does not match sealed cell')
    metrics = data.get('metrics')
    try:
        invalid = (not isinstance(metrics, dict) or any(name not in metrics for name in metric_names)
                   or any(type(value) not in (int, float) or not math.isfinite(value)
                          for value in metrics.values()))
    except OverflowError:
        invalid = True
    if invalid:
        raise ValueError('Collected metrics do not match registered plan')


def _registered_metrics(cell):
    return [m['name'] for m in [cell.scientific_plan.get('primary_metric'),
            *(cell.scientific_plan.get('secondary_metrics') or [])] if m]


class JobsBackend:
    """One pass over explicitly bound jobs; expires_at is host admission policy.

    A direction's compute estimate never supplies expiry or execution authority.
    Generated-code isolation remains a separate provider admission requirement;
    these seals alone do not admit smoke execution.
    """

    def __init__(self, service, actor, authorize, *, bindings, expires_at=None, cancel_authorize=None):
        if not service.repository.durable:
            raise ValueError('Experiment execution requires a durable JobService repository')
        self.service, self.actor, self.authorize = service, actor, authorize
        self.cancel_authorize = cancel_authorize or (lambda *_: False)
        self.bindings = dict(bindings)
        self.expires_at = expires_at
        self.name = 'sealed-jobs'

    def _verify_source(self, cell):
        from .experiment_source import verify_experiment_source
        manifest = verify_experiment_source(
            {'uri': cell.source_uri, 'sha256': cell.source_sha256}, actor=self.actor,
            code=cell.source_code, code_sha256=cell.audited_code_sha256,
            plan=cell.scientific_plan, provider=cell.provider, cell_name=cell.name,
            arm=cell.arm, seed=cell.seed, attempt=cell.attempt)
        if manifest['scientific_plan_sha256'] != cell.scientific_plan_sha256:
            raise ValueError(f'Cell {cell.name}: scientific plan binding mismatch')

    def _view(self, cell):
        # Control remains possible after source corruption; only admission and
        # scientific output consumption require the current source bytes.
        view = self.service.inspect(self.actor, cell.operation_id)
        plan, spec = view['plan'], view['plan']['spec']
        expected = {'scope': self.actor.scope, 'project_root': self.actor.project_root,
                    'provider': cell.provider}
        if (view['plan_hash'] != cell.plan_hash
                or any(plan.get(k) != v for k, v in expected.items())
                or spec['project'] != cell.project
                or spec['source'] != {'uri': cell.source_uri, 'sha256': cell.source_sha256}
                or spec['command'] != list(cell.argv) or spec['outputs'] != [RESULT_NAME]):
            raise ValueError(f'Cell {cell.name}: sealed job binding mismatch')
        return view

    def validate_receipt(self, cell, receipt, text, observation):
        view = self._view(cell)
        self._verify_source(cell)
        if (view['phase'] != 'succeeded' or view.get('discrepancies')
                or receipt.get('operation_id') != cell.operation_id
                or receipt.get('reference') != view['reference']
                or receipt.get('outputs') != view.get('outputs')
                or not self.service.repository.durable):
            raise ValueError('Observation lacks an owned durable successful receipt')
        outputs = receipt.get('outputs') or []
        if (not isinstance(observation, dict) or not isinstance(text, str) or len(outputs) != 1 or outputs[0]['name'] != RESULT_NAME
                or outputs[0]['sha256'] != hashlib.sha256(text.encode()).hexdigest()
                or outputs[0]['bytes'] != len(text.encode())):
            raise ValueError('Observation bytes do not match owned receipt')
        decoded = _decode_result(text)
        validate_observation(decoded, cell.arm, cell.seed, _registered_metrics(cell))
        if not _same_json_value(decoded, observation):
            raise ValueError('Observation copy differs from decoded receipt')
        return decoded

    def cancel(self, cells):
        """Persist every stop intent, collecting failures without abandoning siblings."""
        rows, failures = {}, []
        for cell in cells:
            try:
                rows.update(self.run([cell], abort_check=lambda: True))
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup('Some owned cancellations require reconciliation', failures)
        return rows

    def _collect(self, cell, allowed):
        from tofu_environments.job_files import OutputDirectory
        with OutputDirectory(cell.out.parent, project_root=self.actor.project_root, create=True) as root:
            receipt = self.service.collect(self.actor, cell.operation_id, root)
            name = Path(receipt['directory']).name
            with root.cleanup_child(name):
                with root.child([name]) as snapshot:
                    descriptor = os.open(RESULT_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=snapshot.fd)
                    with os.fdopen(descriptor, 'rb') as stream:
                        payload = stream.read()
                digest = hashlib.sha256(payload).hexdigest()
                sealed = receipt['outputs']
                if (len(sealed) != 1 or sealed[0]['name'] != RESULT_NAME
                        or sealed[0]['sha256'] != digest or sealed[0]['bytes'] != len(payload)):
                    raise ValueError('Collected output does not match JobService receipt')
                text = payload.decode('utf-8')
                data = _decode_result(text)
                validate_observation(data, cell.arm, cell.seed, _registered_metrics(cell))
            # The private snapshot is not needed after verifying its bytes.
            # Reclaim it before linking, so cleanup cannot mask a visible result.
            publication = _publish_output(cell.out, payload, root, allowed)
            if publication is None:
                return None
            return publication, receipt, data, text

    def run(self, cells, *, deadline=None, emit=None, abort_check=None):
        from tofu_environments.contracts import EnvironmentError
        deadlines = [value for value in (deadline, self.expires_at) if value is not None]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in deadlines):
            raise ValueError('Host and caller deadlines must be finite timestamps')
        deadline = min(deadlines) if deadlines else None
        rows = {}
        for cell in cells:
            if self.bindings.get(cell.name) != cell:
                raise ValueError(f'Cell {cell.name}: host-approved binding required')
            view = self._view(cell)

            def allowed():
                return (not (deadline is not None and time.time() >= deadline)
                        and not (abort_check and abort_check()))

            stopping = not allowed()
            submitted = False
            intent = view.get('cancellation', {}).get('state')
            if stopping and intent is None and view['phase'] not in {'succeeded', 'failed', 'cancelled'}:
                view = self.service.cancel(self.actor, cell.operation_id, cell.plan_hash,
                                           self.cancel_authorize)
                intent = view.get('cancellation', {}).get('state')
            if intent == 'requested' and view['reference'] is None and view['phase'] in {'submitting', 'uncertain'}:
                view = self.service.reconcile_reference(self.actor, cell.operation_id)
            if intent in {'requested', 'claimed'}:
                view = self.service.progress_cancellation(self.actor, cell.operation_id)
            elif not stopping and view['phase'] == 'planned':
                self._verify_source(cell)
                try:
                    view = self.service.submit(self.actor, cell.operation_id, cell.plan_hash,
                                               self.authorize, before_submit=allowed)
                    submitted = True
                except EnvironmentError as exc:
                    if exc.code != 'conflict':
                        raise
                    view = self._view(cell)
            if not stopping and not submitted:
                view = self.service.status(self.actor, cell.operation_id)
            stopping = not allowed()
            if (stopping and not view.get('cancellation')
                    and view['phase'] not in {'succeeded', 'failed', 'cancelled'}):
                view = self.service.cancel(self.actor, cell.operation_id, cell.plan_hash,
                                           self.cancel_authorize)
            if view.get('cancellation', {}).get('state') == 'requested' and view['reference'] is not None:
                view = self.service.progress_cancellation(self.actor, cell.operation_id)
            phase = view['phase']
            row = {'state': 'in_flight', 'operation_id': cell.operation_id,
                   'plan_hash': cell.plan_hash, 'attempt': cell.attempt, 'job_state': phase}
            cancellation = view.get('cancellation', {}).get('state')
            if (cancellation == 'rejected' or phase in {'cancel_uncertain', 'uncertain'}
                    or cancellation == 'requested' and view['reference'] is None and phase not in {'failed', 'cancelled'}):
                row.update(state='needs_attention', errors=[view.get('error') or 'Cancellation reference unresolved'])
            elif phase in {'failed', 'cancelled'}:
                row.update(state=phase, errors=[view.get('error') or phase])
            elif phase == 'succeeded' and not stopping:
                if view.get('discrepancies'):
                    row.update(state='failed', errors=['Job execution disagrees with sealed plan'])
                else:
                    self._verify_source(cell)
                    collected = self._collect(cell, allowed)
                    if collected is None:
                        row.update(state='cancelled', errors=['Stopped before output publication'])
                    else:
                        publication, receipt, data, text = collected
                        synced = publication.durability_error is None
                        row.update(publication_committed=True, publication_created=publication.created,
                                   publication_durability_confirmed=synced,
                                   stop_after_publication=not allowed())
                        if publication.cleanup_errors:
                            row['cleanup_errors'] = list(publication.cleanup_errors)
                        if synced:
                            row.update(state='done', receipt=receipt, observation=data, receipt_text=text)
                        else:
                            row.update(state='needs_attention', errors=[publication.durability_error])
            elif phase == 'succeeded':
                row.update(state='cancelled', errors=['aborted after job completion; not consumed'])
            rows[cell.name] = row
            if emit:
                emit({'type': 'cell_state', 'cell': cell.name, **row})
        return rows
