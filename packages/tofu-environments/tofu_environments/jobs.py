"""Batch compute workflow: scoped plans, at-most-once submission and readback.

Embeddings supply the existing CAS repository and an actor-bound JobProvider.
No transport, credential discovery, thread or network work occurs on import.
"""
from __future__ import annotations
import json
from typing import Protocol
from .contracts import Actor, EnvironmentError, canonical, seal

class JobProvider(Protocol):
    def validate_job(self, actor: Actor, spec: dict, *, operation_id: str) -> dict: ...
    def submit_job(self, actor: Actor, spec: dict, operation_id: str, admission: dict) -> dict: ...
    # {'state', 'exit_code'}; a terminal state may add typed 'discrepancies'
    # ([{'code', 'message'}]). Raises ContractBreach when no read can admit the job.
    def job_status(self, actor: Actor, reference: dict, operation_id: str) -> dict: ...
    def reconcile_job(self, actor: Actor, record: dict) -> dict | None: ...
    def cancel_job(self, actor: Actor, reference: dict, operation_id: str) -> None: ...
    def read_job_output(self, actor: Actor, reference: dict, operation_id: str, name: str): ...


def validate_job_spec(spec):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name('job.schema.json').read_text())
    try:
        Draft202012Validator(schema).validate(spec)
    except Exception as exc:
        raise EnvironmentError('invalid_job_spec', 'Job specification does not match API v1') from exc
    clean = json.loads(canonical(spec))
    resources = clean['resources']
    attempts = clean.get('recovery', {}).get('max_attempts', 1)
    if resources['gpu_count'] * clean['limits']['runtime_seconds'] * attempts > clean['limits']['gpu_seconds']:
        raise EnvironmentError('invalid_job_spec', 'Worst-case runtime across all admitted attempts exceeds total GPU-seconds budget')
    return clean


def validate_reference(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'id'}:
        raise EnvironmentError('provider_protocol', 'Typed job reference required', uncertain=True)
    if not all(isinstance(value[k], str) and 0 < len(value[k]) <= 256 for k in value):
        raise EnvironmentError('provider_protocol', 'Bounded job reference required', uncertain=True)
    return json.loads(canonical(value))


class JobService:
    def __init__(self, repository, resolve_provider):
        self.repository, self.resolve_provider = repository, resolve_provider

    def catalog(self, actor: Actor, provider_id: str) -> dict:
        """Read bounded capabilities without a durable claim or allocation."""
        from pathlib import Path
        from jsonschema import Draft202012Validator
        if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 64:
            raise EnvironmentError('invalid_provider', 'Bounded provider identifier required')
        provider = self.resolve_provider(actor, provider_id)
        reader = getattr(provider, 'compute_catalog', None)
        if not callable(reader):
            raise EnvironmentError('catalog_unsupported', 'Provider has no compute catalog capability')
        result = reader(actor)
        try:
            raw = json.dumps(result, allow_nan=False, ensure_ascii=False)
            if len(raw.encode()) > 65536:
                raise ValueError('catalog size')
            schema = json.loads(Path(__file__).with_name('compute_catalog.schema.json').read_text())
            Draft202012Validator(schema).validate(result)
            if result['provider'] != provider_id:
                raise ValueError('provider identity')
        except Exception as exc:
            raise EnvironmentError('catalog_invalid', 'Provider catalog failed bounded contract validation') from exc
        return json.loads(raw)

    def plan(self, actor, provider_id, spec, request_id):
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            raise EnvironmentError('invalid_request', 'Bounded request identifier required')
        if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 64:
            raise EnvironmentError('invalid_provider', 'Bounded provider identifier required')
        clean = validate_job_spec(spec)
        provider = self.resolve_provider(actor, provider_id)
        operation_id = 'job-' + seal([actor.scope, request_id])[:32]
        plan = {'scope': actor.scope, 'project_root': actor.project_root, 'provider': provider_id,
                'spec': clean, 'admission': provider.validate_job(actor, clean, operation_id=operation_id)}
        digest = seal(plan)
        record = {'operation_id': operation_id, 'plan': plan, 'plan_hash': digest,
                  'phase': 'planned', 'reference': None, 'error': ''}
        old = self.repository.get(actor, operation_id)
        if old:
            if old['value']['plan_hash'] != digest:
                raise EnvironmentError('request_conflict', 'Request already names another job plan')
            return self._view(old['value'])
        try:
            self.repository.put(actor, operation_id, record, 0)
        except EnvironmentError as exc:
            if exc.code != 'conflict': raise
            old = self.repository.get(actor, operation_id)
            if not old or old['value']['plan_hash'] != digest: raise
            record = old['value']
        return self._view(record)

    def _load_record(self, actor, operation_id):
        """Load and validate owned durable state without constructing a provider."""
        row = self.repository.get(actor, operation_id)
        if not row:
            raise EnvironmentError('not_found', 'No job in this owner scope')
        record = json.loads(canonical(row['value']))
        plan = record['plan']
        if not operation_id.startswith('job-') or plan['scope'] != actor.scope or plan['project_root'] != actor.project_root:
            raise EnvironmentError('scope_changed', 'Job belongs to another project scope')
        if seal(plan) != record['plan_hash']:
            raise EnvironmentError('plan_corrupt', 'Job plan seal does not match')
        return row['version'], record

    def submit(self, actor, operation_id, plan_hash, authorize, *, before_submit=None):
        version, record = self._load_record(actor, operation_id)
        if plan_hash != record['plan_hash'] or not authorize(actor, plan_hash):
            raise EnvironmentError('approval_required', 'Exact job plan authorization required')
        if record['phase'] != 'planned':
            return self._view(record)
        plan = record['plan']
        provider = self.resolve_provider(actor, plan['provider'])
        if provider.validate_job(actor, plan['spec'], operation_id=operation_id) != plan['admission']:
            raise EnvironmentError('provider_changed', 'Job admission changed; replan required')
        if before_submit is not None and before_submit() is False:
            raise EnvironmentError('submission_aborted', 'Submission is no longer admitted')
        record['phase'] = 'submitting'
        try:
            self.repository.put(actor, operation_id, record, version)
        except EnvironmentError as exc:
            if exc.code != 'conflict':
                raise
            _, record = self._load_record(actor, operation_id)
            return self._view(record)
        outcome = {}
        guard_error = None
        _, latest = self._load_record(actor, operation_id)
        try:
            if latest.get('cancellation'):
                raise EnvironmentError('submission_aborted', 'Cancellation requested before physical submit')
            if before_submit is not None and before_submit() is False:
                raise EnvironmentError('submission_aborted', 'Submission is no longer admitted')
        except BaseException as exc:
            guard_error = exc
            outcome = {'phase': 'failed', 'error': 'submission_aborted'}
        else:
            try:
                _, latest = self._load_record(actor, operation_id)
                if latest.get('cancellation'):
                    raise EnvironmentError('submission_aborted', 'Cancellation requested before physical submit')
                try:
                    if before_submit is not None and before_submit() is False:
                        raise EnvironmentError('submission_aborted', 'Submission is no longer admitted')
                except BaseException as exc:
                    guard_error = exc
                    outcome = {'phase': 'failed', 'error': 'submission_aborted'}
                if guard_error is not None:
                    raise EnvironmentError('submission_aborted', 'Submission is no longer admitted')
                outcome['reference'] = validate_reference(provider.submit_job(actor, plan['spec'], operation_id, plan['admission']))
                outcome['phase'] = 'queued'
            except Exception as exc:
                outcome['phase'] = 'failed' if isinstance(exc, EnvironmentError) and not exc.uncertain else 'uncertain'
                outcome['error'] = getattr(exc, 'code', 'external_outcome_unknown')
        # Cancellation can commit while submission is outside the repository.
        # Never replace its intent with the pre-submit record.
        version, record = self._load_record(actor, operation_id)
        while record['phase'] in {'submitting', 'uncertain'} and record['reference'] is None:
            record.update(outcome)
            try:
                self.repository.put(actor, operation_id, record, version)
                break
            except EnvironmentError as exc:
                if exc.code != 'conflict': raise
                version, record = self._load_record(actor, operation_id)
        if guard_error is not None:
            raise guard_error
        return self._view(record)

    def cancel(self, actor, operation_id, plan_hash, authorize):
        version, record = self._load_record(actor, operation_id)
        if plan_hash != record['plan_hash'] or not authorize(actor, plan_hash):
            raise EnvironmentError('approval_required', 'Exact job cancellation authorization required')
        while True:
            if record['phase'] not in {'planned', 'submitting', 'uncertain', 'queued', 'running', 'failed'}:
                return self._view(record)
            if record['phase'] == 'failed' and record['reference'] is None:
                return self._view(record)
            intent = record.get('cancellation')
            if intent is not None and intent.get('state') != 'rejected':
                break
            record['cancellation'] = {'plan_hash': plan_hash, 'state': 'requested'}
            if record['phase'] == 'planned':
                record['phase'] = 'cancelled'
            try:
                self.repository.put(actor, operation_id, record, version)
                break
            except EnvironmentError as exc:
                if exc.code != 'conflict':
                    raise
                # Retry only the durable intent CAS under the already checked
                # exact authorization, never an external mutation or new grant.
                version, record = self._load_record(actor, operation_id)
                if record['plan_hash'] != plan_hash:
                    raise EnvironmentError('approval_required', 'Cancellation plan changed') from exc
        return self.progress_cancellation(actor, operation_id)

    def progress_cancellation(self, actor, operation_id):
        """Advance only a recorded exact-plan cancellation; never grant new authority."""
        version, record = self._load_record(actor, operation_id)
        intent = record.get('cancellation')
        if intent == {'plan_hash': record['plan_hash'], 'state': 'claimed'}:
            return self._view(record)
        if intent != {'plan_hash': record['plan_hash'], 'state': 'requested'}:
            raise EnvironmentError('approval_required', 'Recorded exact job cancellation authorization required')
        if record['phase'] not in {'queued', 'running', 'failed'} or record['reference'] is None:
            return self._view(record)
        provider = self.resolve_provider(actor, record['plan']['provider'])
        previous = record['phase']
        if previous == 'failed':
            record['execution_failure_before_cancellation'] = {
                'phase': previous, 'exit_code': record.get('exit_code'), 'error': record.get('error')}
        record['phase'] = 'cancelling'
        record['cancellation']['state'] = 'claimed'
        try:
            claimed = self.repository.put(actor, operation_id, record, version)
        except EnvironmentError as exc:
            if exc.code != 'conflict':
                raise
            _, record = self._load_record(actor, operation_id)
            return self._view(record)
        try:
            provider.cancel_job(actor, record['reference'], operation_id)
        except Exception as exc:
            rejected = isinstance(exc, EnvironmentError) and not exc.uncertain
            record['phase'] = previous if rejected else 'cancel_uncertain'
            if rejected:
                record['cancellation']['state'] = 'rejected'
            record['error'] = getattr(exc, 'code', 'cancel_outcome_unknown')
        try:
            self.repository.put(actor, operation_id, record, claimed['version'])
        except EnvironmentError as exc:
            if exc.code != 'conflict': raise
            _, record = self._load_record(actor, operation_id)
        return self._view(record)

    def inspect(self, actor, operation_id):
        """Read owned durable state without resolving or calling a provider."""
        return self._view(self._load_record(actor, operation_id)[1])

    def reconcile_reference(self, actor, operation_id):
        """Recover an owned reference without a subsequent provider status read."""
        version, record = self._load_record(actor, operation_id)
        if record['phase'] in {'submitting', 'uncertain'}:
            provider = self.resolve_provider(actor, record['plan']['provider'])
            reference = provider.reconcile_job(actor, json.loads(canonical(record)))
            if reference is None:
                return self._view(record)
            record['reference'] = validate_reference(reference)
            record['phase'] = 'queued'
            try:
                self.repository.put(actor, operation_id, record, version)
            except EnvironmentError as exc:
                if exc.code != 'conflict': raise
                _, record = self._load_record(actor, operation_id)
                return self._view(record)
        return self._view(record)

    def status(self, actor, operation_id):
        self.reconcile_reference(actor, operation_id)
        version, record = self._load_record(actor, operation_id)
        if record['phase'] in {'queued', 'running', 'cancelling', 'cancel_uncertain'}:
            provider = self.resolve_provider(actor, record['plan']['provider'])
            snapshot = json.loads(canonical(provider.job_status(actor, record['reference'], operation_id)))
            discrepancies = snapshot.pop('discrepancies', None)
            if set(snapshot) != {'state', 'exit_code'} or snapshot['state'] not in {'queued', 'running', 'succeeded', 'failed', 'cancelled'}:
                raise EnvironmentError('provider_protocol', 'Invalid job status')
            # A finished job reads finished; evidence that disagrees with its sealed
            # plan travels with that terminal state instead of blocking it.
            if discrepancies is not None and (
                    snapshot['state'] not in {'succeeded', 'failed', 'cancelled'}
                    or not isinstance(discrepancies, list) or not discrepancies
                    or not all(isinstance(item, dict) and set(item) == {'code', 'message'}
                               and isinstance(item['code'], str) and item['code']
                               and isinstance(item['message'], str) for item in discrepancies)):
                raise EnvironmentError('provider_protocol', 'Discrepancies are typed evidence of a terminal status')
            exit_code = snapshot['exit_code']
            if exit_code is not None and type(exit_code) is not int:
                raise EnvironmentError('provider_protocol', 'Exit code must be an integer or null')
            if snapshot['state'] == 'succeeded' and exit_code != 0:
                raise EnvironmentError('provider_protocol', 'Success requires proven zero exit code')
            if record['phase'] in {'cancelling', 'cancel_uncertain'} and snapshot['state'] in {'queued', 'running'}:
                return self._view(record)
            record['phase'] = snapshot['state']
            record['exit_code'] = exit_code
            if discrepancies:
                record['discrepancies'] = discrepancies
            try:
                self.repository.put(actor, operation_id, record, version)
            except EnvironmentError as exc:
                if exc.code != 'conflict': raise
                _, record = self._load_record(actor, operation_id)
        return self._view(record)

    def collect(self, actor, operation_id, destination):
        """Read owned outputs into a private new directory; seal durable receipts.

        Provider returns bounded byte chunks from the exact owned job. Destination
        is a trusted caller path, never interpreted as a remote job path.
        Partial snapshots are reclaimed on failure. A lost receipt acknowledgement
        requires durable readback, never replay of job execution.
        """
        import os
        import uuid
        from contextlib import nullcontext
        from .job_files import OutputDirectory
        version, record = self._load_record(actor, operation_id)
        if record['phase'] != 'succeeded':
            raise EnvironmentError('job_not_succeeded', 'Collect requires proven successful job exit')
        provider = self.resolve_provider(actor, record['plan']['provider'])
        scope = nullcontext(destination) if isinstance(destination, OutputDirectory) else OutputDirectory(destination)
        with scope as root:
            name = 'compute-result-' + uuid.uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=root.fd)
            try:
                with root.child([name]) as staging:
                    return self._collect_pinned(actor, operation_id, version, record, provider, staging)
            except BaseException:
                with root.cleanup_child(name):
                    raise

    def _collect_pinned(self, actor, operation_id, version, record, provider, staging):
        import hashlib
        import os
        from pathlib import PurePosixPath
        limit = record['plan']['spec']['limits']['result_bytes']
        total = 0
        receipts = []
        for name in record['plan']['spec']['outputs']:
            remote = PurePosixPath(name)
            if remote.is_absolute() or '..' in remote.parts or not remote.parts or '\\' in name:
                raise EnvironmentError('invalid_output', 'Relative output path required')
            digest = hashlib.sha256()
            size = 0
            with staging.child(remote.parts[:-1], create=True) as parent:
                fd = os.open(remote.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent.fd)
            with os.fdopen(fd, 'wb') as output:
                for chunk in provider.read_job_output(actor, record['reference'], operation_id, name):
                    if not isinstance(chunk, bytes) or len(chunk) > 1048576:
                        raise EnvironmentError('provider_protocol', 'Output chunks must be bytes of at most 1 MiB')
                    total += len(chunk)
                    if total > limit:
                        raise EnvironmentError('result_limit', 'Collected output exceeds approved result-byte bound')
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            receipts.append({'name': name, 'bytes': size, 'sha256': digest.hexdigest()})
        previous = record.get('outputs')
        if previous is not None and previous != receipts:
            raise EnvironmentError('output_changed', 'Terminal job outputs differ from sealed receipts')
        record['outputs'] = receipts
        self.repository.put(actor, operation_id, record, version)
        return {'operation_id': operation_id, 'reference': record['reference'],
                'directory': str(staging.path), 'outputs': receipts, 'total_bytes': total,
                'durable': self.repository.durable}

    def _view(self, record):
        return {**json.loads(canonical(record)), 'durable': self.repository.durable}
