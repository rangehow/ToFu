"""Private Hadoop result transport with explicit owner and operation binding.

Entry: HopeArtifactReader; shared owned_artifact_uri defines the publication path.
A trusted host supplies the root URI, CLI argv and credential environment for one
actor/project. Only declared outputs of sealed successful operations can be read.
No network, credentials, daemon or persistent cache is touched at construction.
"""
import math
import os
from pathlib import PurePosixPath
import re
import selectors
import signal
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from tofu_environments.contracts import EnvironmentError, seal
from tofu_environments.jobs import validate_reference
from .hope_publish import RECEIPT_CONTRACT_FILE, receipt_contract


def owned_artifact_uri(actor, operation_id, name, root_uri):
    if not isinstance(operation_id, str) or not re.fullmatch(r'job-[a-f0-9]{32}', operation_id):
        raise EnvironmentError('invalid_operation', 'Canonical compute operation required')
    if (not isinstance(name, str) or not name or '\\' in name or
            any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute() or
            any(p in ('', '.', '..') for p in name.split('/'))):
        raise EnvironmentError('invalid_output', 'Canonical relative output required')
    uri = urlsplit(root_uri)
    if (uri.scheme not in ('viewfs', 'hdfs') or not uri.netloc or uri.username or
            uri.password or uri.query or uri.fragment or not uri.path.startswith('/') or
            any(p in ('.', '..') for p in uri.path.split('/')) or
            any(ord(c) < 32 for c in root_uri)):
        raise EnvironmentError('hope_artifact_root', 'Trusted absolute Hadoop artifact root required')
    namespace = seal([actor.scope, actor.project_root, operation_id])
    return urlunsplit((uri.scheme, uri.netloc, uri.path.rstrip('/')+'/'+namespace+'/'+name, '', ''))


class HopeArtifactReader:
    def __init__(self, actor, repository, *, root_uri, command, environment):
        # Validate profile eagerly; owner namespace is derived, never model-selected.
        owned_artifact_uri(actor, 'job-'+'0'*32, 'probe', root_uri)
        if (not isinstance(command, list) or not command or
                not all(isinstance(x, str) and x for x in command) or
                not os.path.isabs(command[0])):
            raise EnvironmentError('hope_artifact_command', 'Explicit absolute Hadoop CLI argv required')
        if not isinstance(environment, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in environment.items()):
            raise EnvironmentError('hope_artifact_environment', 'Trusted actor-bound subprocess environment required')
        self.actor, self.repository = actor, repository
        self.root_uri, self.command, self.environment = root_uri, list(command), dict(environment)

    def _owned(self, actor, reference, operation_id, deadline, maximum_bytes):
        if actor != self.actor:
            raise EnvironmentError('hope_scope_mismatch', 'Artifact reader belongs to another actor/project')
        if (type(deadline) not in (int,float) or not math.isfinite(deadline) or
                type(maximum_bytes) is not int or maximum_bytes < 0):
            raise EnvironmentError('hope_artifact_limits', 'Finite deadline and nonnegative byte bound required')
        validate_reference(reference)
        row = self.repository.get(actor, operation_id)
        if not row:
            raise EnvironmentError('hope_binding_missing', 'Owned operation required')
        record = row['value']; plan = record['plan']
        if (record['operation_id'] != operation_id or plan['scope'] != actor.scope or
                plan['project_root'] != actor.project_root or seal(plan) != record['plan_hash'] or
                reference != record['reference'] or reference['kind'] != 'hope_run_id'):
            raise EnvironmentError('hope_scope_mismatch', 'Artifact operation seal/reference mismatch')
        return record

    def __call__(self, actor, reference, operation_id, name, *, deadline, maximum_bytes):
        record=self._owned(actor,reference,operation_id,deadline,maximum_bytes)
        plan=record['plan']
        if record['phase'] != 'succeeded' or name not in plan['spec']['outputs']:
            raise EnvironmentError('hope_artifact_unadmitted', 'Successful declared output required')
        if maximum_bytes > plan['spec']['limits']['result_bytes']:
            raise EnvironmentError('hope_artifact_limits', 'Read exceeds approved result budget')
        uri = owned_artifact_uri(actor, operation_id, name, self.root_uri)
        yield from self._stream(uri,deadline,maximum_bytes,exact=True)

    def worker_receipt(self, actor, reference, operation_id, *, deadline):
        """Reserved control bytes only, before output admission; no arbitrary reads."""
        contract=receipt_contract()
        record=self._owned(actor,reference,operation_id,deadline,contract['maximum_bytes'])
        if RECEIPT_CONTRACT_FILE not in record['plan'].get('admission',{}).get('include',[]):
            raise EnvironmentError('hope_receipt_missing','Legacy operation has no durable receipt contract')
        uri=owned_artifact_uri(actor,operation_id,contract['receipt_name'],self.root_uri)
        return b''.join(self._stream(uri,deadline,contract['maximum_bytes'],exact=False))

    def _stream(self, uri, deadline, maximum_bytes, *, exact):
        if time.monotonic() >= deadline:
            raise EnvironmentError('hope_output_timeout', 'Artifact read deadline exceeded')
        process = subprocess.Popen(self.command+['fs','-cat',uri], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=self.environment, start_new_session=True)
        selector = selectors.DefaultSelector(); size = 0
        try:
            for stream, kind in ((process.stdout,'stdout'),(process.stderr,'stderr')):
                os.set_blocking(stream.fileno(),False)
                selector.register(stream,selectors.EVENT_READ,kind)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise EnvironmentError('hope_output_timeout', 'Artifact read deadline exceeded')
                for key,_ in selector.select(min(remaining,0.1)):
                    chunk = os.read(key.fileobj.fileno(),65536)
                    if not chunk:
                        selector.unregister(key.fileobj);continue
                    if key.data == 'stderr':
                        # Drain without storing or exposing credential-bearing CLI diagnostics.
                        continue
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise EnvironmentError('hope_output_changed', 'Artifact exceeds expected Worker bytes')
                    yield chunk
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise EnvironmentError('hope_output_timeout', 'Artifact read deadline exceeded')
            try: code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise EnvironmentError('hope_output_timeout', 'Artifact read deadline exceeded') from exc
            if code != 0:
                raise EnvironmentError('hope_artifact_read_failed', 'Owned artifact transfer failed')
            if exact and size != maximum_bytes:
                raise EnvironmentError('hope_output_changed', 'Artifact is shorter than Worker receipt')
        finally:
            # Reclaim descendants even if the CLI leader exited or consumer closed early.
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass  # The owned process group is already gone.
            process.wait()
            selector.close(); process.stdout.close(); process.stderr.close()
