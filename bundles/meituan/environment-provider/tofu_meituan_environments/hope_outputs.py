"""Owned Hope result streams verified against the terminal Worker receipt.

Entry: HopeOutputPort. Trusted host supplies fresh execution proof, receipt text
and an actor-bound byte transport with an absolute monotonic deadline. Transport
owns remote namespace resolution; no model-supplied URI or credential is accepted.
No transfer, cache, worker or credential discovery occurs at construction.
"""
import hashlib
import math
import time
from pathlib import PurePosixPath
from tofu_environments.contracts import EnvironmentError, seal
from tofu_environments.jobs import validate_reference
from .hope_evidence import verify_worker_receipt


class HopeOutputPort:
    def __init__(self, actor, repository, prove_success, read_receipt, read_bytes,
                 *, timeout_seconds, clock=time.monotonic):
        if (type(timeout_seconds) not in (int, float) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise EnvironmentError('hope_output_timeout', 'Positive finite transfer deadline required')
        if not all(callable(x) for x in (prove_success, read_receipt, read_bytes, clock)):
            raise EnvironmentError('hope_output_unconfigured', 'Owned evidence and byte transport required')
        self.actor, self.repository = actor, repository
        self.prove_success, self.read_receipt, self.read_bytes = prove_success, read_receipt, read_bytes
        self.timeout_seconds, self.clock = timeout_seconds, clock

    def __call__(self, actor, reference, operation_id, name):
        if actor != self.actor:
            raise EnvironmentError('hope_scope_mismatch', 'Output port belongs to another actor/project')
        validate_reference(reference)
        row = self.repository.get(actor, operation_id)
        if not row:
            raise EnvironmentError('hope_binding_missing', 'Owned durable operation binding required')
        record = row['value']; plan = record['plan']
        if (record['operation_id'] != operation_id or plan['scope'] != actor.scope or
                plan['project_root'] != actor.project_root or seal(plan) != record['plan_hash'] or
                reference != record['reference'] or reference['kind'] != 'hope_run_id'):
            raise EnvironmentError('hope_scope_mismatch', 'Output operation seal/reference mismatch')
        if record['phase'] != 'succeeded':
            raise EnvironmentError('job_not_succeeded', 'Output requires proven successful operation')
        if (not isinstance(name, str) or name not in plan['spec']['outputs'] or
                PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name):
            raise EnvironmentError('invalid_output', 'Declared relative output required')
        deadline = self.clock() + self.timeout_seconds
        def check_deadline():
            if self.clock() >= deadline:
                raise EnvironmentError('hope_output_timeout', 'Owned output read deadline exceeded')
        check_deadline()
        proof = self.prove_success(actor, reference, operation_id, deadline=deadline)
        check_deadline()
        if proof != {'state': 'succeeded', 'exit_code': 0}:
            raise EnvironmentError('hope_terminal_unverified', 'Fresh owned terminal proof required')
        receipt = verify_worker_receipt(
            self.read_receipt(actor, reference, operation_id, deadline=deadline), plan['spec'])
        check_deadline()
        if receipt['state'] != 'succeeded' or receipt['exit_code'] != 0:
            raise EnvironmentError('hope_terminal_unverified', 'Successful Worker receipt required')
        expected = next(item for item in receipt['outputs'] if item['name'] == name)
        digest = hashlib.sha256(); size = 0
        stream = iter(self.read_bytes(actor, reference, operation_id, name,
                                     deadline=deadline, maximum_bytes=expected['bytes']))
        try:
            for chunk in stream:
                check_deadline()
                if not isinstance(chunk, bytes) or len(chunk) > 1048576:
                    raise EnvironmentError('provider_protocol', 'Output chunks must be bytes of at most 1 MiB')
                size += len(chunk)
                if size > expected['bytes']:
                    raise EnvironmentError('hope_output_changed', 'Result exceeds Worker byte receipt')
                digest.update(chunk)
                yield chunk
            check_deadline()
            if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
                raise EnvironmentError('hope_output_changed', 'Result differs from Worker size/hash receipt')
        finally:
            close = getattr(stream, 'close', None)
            if callable(close): close()
