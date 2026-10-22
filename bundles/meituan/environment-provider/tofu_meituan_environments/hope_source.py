"""Actor-bound source preparation port for Hope batch jobs.

Entry: HopeSourcePreparer(actor, trusted_profile, capacity). It extracts owned ZIP
sources and renders a bounded worker runner. The host owns this preparer's scope
and must close it after dependent operations settle; it is not durable recovery.
No credentials, network, allocation or worker is started here.
"""
import hashlib
import json
import math
from pathlib import Path
import re
from threading import RLock
import tempfile
from tofu_environments.contracts import EnvironmentError, canonical
from tofu_environments.job_sources import extract_job_source
from tofu_environments import job_runner
from .hope_artifacts import owned_artifact_uri
from . import hope_publish

_RESERVED = ('tofu-job-runner.py', 'tofu-execution.json', 'tofu-job.hope', 'tofu-hope-publish.py', 'tofu-publication.json', 'tofu-publication-intent.json', 'tofu-parallel-upload.jar', hope_publish.RECEIPT_CONTRACT_FILE)

def json_copy(value):
    import json
    return json.loads(canonical(value))

def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''): digest.update(chunk)
    return digest.hexdigest()


def _source_files(directory):
    """Private staging must contain no link/special-file packaging escapes."""
    paths = sorted(directory.rglob('*'))
    if any(path.is_symlink() or (not path.is_file() and not path.is_dir()) for path in paths):
        raise EnvironmentError('source_changed', 'Prepared sources contain a link or special file')
    return [path for path in paths if path.is_file()]


def _manifest_sha(seal):
    # File count/bytes were admitted at extraction. Stream the same canonical
    # representation without applying the small operation-metadata JSON cap.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for chunk in encoder.iterencode(seal): digest.update(chunk.encode())
    return digest.hexdigest()

class HopeSourcePreparer:
    """Request/run-scoped callable; explicit capacity and teardown, no global cache."""
    def __init__(self, actor, profile, *, byte_limit, file_limit, total_byte_limit, staging_root=None, artifact_profile=None):
        for value in (byte_limit, file_limit, total_byte_limit):
            if type(value) is not int or value < 1:
                raise EnvironmentError('source_limits_required', 'Positive host-derived staging bounds required')
        required = {'usergroup', 'queue', 'image', 'gpu_resource_key'}
        if set(profile) != required or any(not isinstance(v, str) or not v or '\n' in v or '\r' in v for v in profile.values()):
            raise EnvironmentError('hope_profile_invalid', 'Explicit trusted image, account, queue and GPU field required')
        if not re.fullmatch(r'worker\.[a-zA-Z0-9_.-]+', profile['gpu_resource_key']):
            raise EnvironmentError('hope_profile_invalid', 'Invalid scheduler GPU field')
        if artifact_profile is not None:
            if set(artifact_profile) not in ({'root_uri', 'command'}, {'root_uri', 'command', 'parallel_upload'}):
                raise EnvironmentError('hope_artifact_profile', 'Trusted root URI and Hadoop argv required')
            owned_artifact_uri(actor, 'job-'+'0'*32, 'probe', artifact_profile['root_uri'])
            command = artifact_profile['command']
            if not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command) or not Path(command[0]).is_absolute():
                raise EnvironmentError('hope_artifact_profile', 'Absolute Hadoop command required')
        if artifact_profile is not None and 'parallel_upload' in artifact_profile:
            upload = artifact_profile['parallel_upload']
            if (not isinstance(upload,dict) or set(upload) != {'jar_path','sha256','threshold_bytes'}
                    or not isinstance(upload['jar_path'],str) or not Path(upload['jar_path']).is_absolute()
                    or not isinstance(upload['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',upload['sha256'])
                    or type(upload['threshold_bytes']) is not int or upload['threshold_bytes'] < 1):
                raise EnvironmentError('hope_artifact_profile','Explicit pinned host upload artifact required')
        self.artifact_profile = json_copy(artifact_profile)
        self.actor, self.profile = actor, dict(profile)
        self.byte_limit, self.file_limit, self.total_byte_limit = byte_limit, file_limit, total_byte_limit
        self.root, self.lock, self.entries, self.used, self.closed = staging_root, RLock(), {}, 0, False

    def __call__(self, actor, spec, *, operation_id):
        with self.lock:
            if actor != self.actor or self.closed:
                raise EnvironmentError('hope_scope_invalid', 'Preparation belongs to one active actor/project scope')
            options = spec['provider_options']
            if options['usergroup'] != self.profile['usergroup'] or options['queue'] != self.profile['queue'] or options['job_type'] != 'ml-easy-job':
                raise EnvironmentError('hope_profile_mismatch', 'Job account/queue/type differs from trusted profile')
            if options.get('conf_name', 'tofu-job.hope') != 'tofu-job.hope':
                raise EnvironmentError('hope_profile_mismatch', 'Prepared configuration name must be tofu-job.hope')
            identity = hashlib.sha256(canonical([actor.scope, actor.project_root, spec, self.profile, operation_id, self.artifact_profile])).hexdigest()
            # Re-extract even cached sources: verify current ZIP bytes and content at
            # the final submission boundary instead of trusting an earlier digest.
            temporary = tempfile.TemporaryDirectory(prefix='tofu-hope-source-', dir=self.root)
            try:
                directory = Path(temporary.name)
                extract_job_source(actor, spec['source'], directory, byte_limit=self.byte_limit,
                                   file_limit=self.file_limit, reserved=_RESERVED)
                execution = {'command': spec['command'], 'outputs': spec['outputs'],
                             'limits': {'runtime_seconds': spec['limits']['runtime_seconds'],
                                        'result_bytes': spec['limits']['result_bytes']}}
                (directory/'tofu-execution.json').write_bytes(canonical(execution))
                (directory/'tofu-job-runner.py').write_bytes(Path(job_runner.__file__).read_bytes())
                worker_script = 'python3 tofu-job-runner.py tofu-execution.json'
                if self.artifact_profile is not None:
                    profile = self.artifact_profile
                    outputs = [{'name':name,'uri':owned_artifact_uri(actor,operation_id,name,profile['root_uri'])} for name in spec['outputs']]
                    namespace = owned_artifact_uri(actor,operation_id,'probe',profile['root_uri']).rsplit('/',1)[0]
                    publication = {'command':profile['command'],'namespace':namespace,'outputs':outputs}
                    contract=hope_publish.receipt_contract()
                    if contract['receipt_name'] in spec['outputs']:
                        raise EnvironmentError('reserved_output','Worker control receipt is not a user output')
                    publication['worker_receipt']={'uri':namespace+'/'+contract['receipt_name']}
                    (directory/hope_publish.RECEIPT_CONTRACT_FILE).write_bytes(canonical(contract))
                    if 'parallel_upload' in profile:
                        upload=profile['parallel_upload'];jar=Path(upload['jar_path'])
                        if jar.is_symlink() or not jar.is_file() or jar.stat().st_size > 16777216:
                            raise EnvironmentError('hope_upload_artifact','Bounded regular host upload artifact required')
                        with jar.open('rb') as stream: raw=stream.read(16777217)
                        if len(raw)>16777216 or hashlib.sha256(raw).hexdigest()!=upload['sha256']:
                            raise EnvironmentError('hope_upload_artifact','Upload artifact differs from trusted pin')
                        (directory/'tofu-parallel-upload.jar').write_bytes(raw)
                        publication['parallel_upload']={'jar':'tofu-parallel-upload.jar','sha256':upload['sha256'],
                            'threshold_bytes':upload['threshold_bytes'],'lanes':spec['resources']['cpu']}
                    (directory/'tofu-publication.json').write_bytes(canonical(publication))
                    (directory/'tofu-hope-publish.py').write_bytes(Path(hope_publish.__file__).read_bytes())
                    worker_script = 'python3 tofu-hope-publish.py tofu-execution.json tofu-publication.json'
                resources = spec['resources']
                gpu = '' if resources['gpu_count'] == 0 else self.profile['gpu_resource_key']+' = '+str(resources['gpu_count'])+'\n'
                config = ('[base]\ntype = ml-easy-job\n[resource]\nusergroup = '+self.profile['usergroup']+'\nqueue = '+self.profile['queue']+
                          '\n[roles]\nworkers = 1\nworker.vcore = '+str(resources['cpu'])+'\nworker.memory = '+str(math.ceil(resources['memory_gib']*1024))+
                          '\n'+gpu+'worker.script = '+worker_script+'\n[am]\nam.vcore = 1\nam.memory = 2048\n[docker]\nafo.docker.image.name = '+self.profile['image']+
                          '\n[failover]\nafo.app.support.engine.failover = false\n[others]\nafo.role.worker.task.attempt.max.retry = '+str(spec.get('recovery',{}).get('max_attempts',1)-1)+'\n')
                (directory/'tofu-job.hope').write_text(config)
                files = _source_files(directory)
                seal = {str(p.relative_to(directory)): _sha(p) for p in files}
                count = sum(p.stat().st_size for p in files)
                include = sorted({Path(name).parts[0] for name in seal})
                if len(include) > 200:
                    raise EnvironmentError('source_limit', 'Source has too many top-level packaging entries')
                previous = self.entries.get(identity)
                if previous:
                    old, old_seal, old_count = previous
                    actual = {str(p.relative_to(Path(old.name))): _sha(p) for p in _source_files(Path(old.name))}
                    if actual != old_seal or seal != old_seal:
                        raise EnvironmentError('source_changed', 'Prepared sources/configuration changed')
                    directory = Path(old.name)
                else:
                    if self.used + count > self.total_byte_limit:
                        raise EnvironmentError('source_limit', 'Active staged jobs exceed host-derived capacity')
                    self.entries[identity] = (temporary, seal, count); self.used += count
                    temporary = None
                # The MCP accepts at most 200 include entries. Each admitted
                # top-level directory is sealed recursively, so thousands of
                # retained files remain bounded without weakening their hash.
                return {'workdir': str(directory), 'include': include,
                        'source_sha256': spec['source']['sha256'], 'configuration_sha256': _sha(directory/'tofu-job.hope'),
                        'limits_enforced': True, 'binding_revision': identity,
                        'prepared_sha256': _manifest_sha(seal)}
            finally:
                if temporary is not None: temporary.cleanup()

    def close(self):
        """Host calls only after all dependent operations have settled."""
        with self.lock:
            self.closed = True
            for temporary, _, _ in self.entries.values(): temporary.cleanup()
            self.entries.clear(); self.used = 0
