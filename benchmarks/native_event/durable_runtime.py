"""Owned native authority plus actual event-log/batcher and retention runtime.

Entry: DurableEventRuntime. Inject clients only in private function namespaces;
no application authority, batcher, storage getter or runtime global is replaced.
The shared native harness validates build/library identity and closes its engine.
"""
import inspect
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from benchmarks.knowledge_html.durable import DurableHtmlCorpus
from demand_runtime import create, normalized
from lib.storage.event_batcher import StorageEventBatcher
from lib.tasks_pkg import event_log


class FaultClient:
    """Inject definitive unavailability before delegation, never ambiguous commit."""
    def __init__(self, client):
        self.client = client
        self.fail_writes = False
        self.failed_commands = []
    def command(self, operation, *args, **kwargs):
        if self.fail_writes and operation in ('event.append_batch', 'event.append'):
            self.failed_commands.append(operation)
            raise OSError('owned pre-dispatch durability fault')
        return self.client.command(operation, *args, **kwargs)
    def __getattr__(self, name):
        return getattr(self.client, name)


def bind_log(client, batcher):
    namespace = dict(vars(event_log))
    for name, original in vars(event_log).items():
        if isinstance(original, types.FunctionType) and original.__globals__ is vars(event_log):
            clone = types.FunctionType(original.__code__, namespace, name,
                                       original.__defaults__, original.__closure__)
            clone.__kwdefaults__ = original.__kwdefaults__
            namespace[name] = clone
    namespace['_SIDECAR_BATCHER'] = batcher
    namespace['get_storage_client'] = lambda **_: client
    for name in ('append_persistent_event', 'read_events', 'read_event_bounds'):
        source = inspect.getsource(getattr(event_log, name))
        seam = 'from lib.storage import get_storage_client'
        assert source.count(seam) == 1, name
        source = source.replace(seam, 'pass  # client is explicitly owned by this private namespace')
        exec(compile(source, '<owned-native-event-log-'+name+'>', 'exec'), namespace)
    return types.SimpleNamespace(**namespace)


class DurableEventRuntime:
    def __init__(self, root, engine, variant, *, count=4096):
        self.original_batcher = event_log._SIDECAR_BATCHER
        self.authority = DurableHtmlCorpus(Path(root), Path(engine), variant='candidate', owner_user_id=900001)
        self.batcher = None
        self.runtime = None
        try:
            self.client = FaultClient(self.authority.client)
            self.batcher = StorageEventBatcher(client_provider=lambda **_: self.client)
            self.log = bind_log(self.client, self.batcher)
            self.runtime, self.task = create('native' if variant == 'candidate' else 'reference', count)
            self.identity = self.task['id']
        except BaseException:
            self.close()
            raise

    def seed(self, count):
        # Populate the actual producer first; commit all seed rows through the
        # native semantic batch operation before beginning measured operations.
        values = []
        for i in range(count):
            event = {'type': 'delta', 'index': i, 'delta': 'seed'}
            sequence = self.runtime.append_event(self.identity, event, deliver_push=False)
            values.append({'task_id': self.identity, 'sequence': sequence,
                           'event': self.log.project_persistent_event(self.identity, event)})
            if len(values) == 256 or i == count-1:
                result = self.client.command('event.append_batch', {'events': values}, None, priority='event', deadline=30)
                assert result['inserted'] == len(values)
                values = []

    def append(self, index, *, producer=0, authoritative=False):
        event = {'type': 'delta', 'index': index, 'producer': producer, 'delta': 'durable'}
        if authoritative:
            event['attemptId'] = 'owned-fault-fixture-attempt'
        return self.runtime.append_event(self.identity, event,
            before_push=lambda sequence: self.log.append_persistent_event(self.identity, sequence, event),
            deliver_push=False)

    def replay(self, cursor, limit=32):
        return self.log.read_events(self.identity, cursor-1, limit, raise_on_error=True)

    def memory_page(self, cursor):
        return normalized(self.runtime.poll(self.identity, cursor))

    def close(self):
        if self.batcher is not None:
            if not self.batcher.close(timeout=30):
                raise RuntimeError('owned event batcher has not joined; authority must remain open')
            self.batcher = None
        try:
            if self.runtime is not None:
                self.runtime.finish(self.identity, result='owned event fixture settled')
                self.runtime.discard(self.identity)
                assert self.runtime.task_count == 0
                self.runtime = None
        finally:
            self.authority.close()
        assert event_log._SIDECAR_BATCHER is self.original_batcher
