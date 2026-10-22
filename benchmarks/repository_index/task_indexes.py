"""Experimental multi-root cache group owned by one execution reservation.

Caller supplies authorized roots and root-aware epochs. Scans on different roots
may overlap; terminal release closes admission, joins scans, then returns quota.
No production registry/default or durable authority is installed.
"""
import os
import sys
from threading import Condition, get_ident

from index import outline
from owned_scope import OwnedIndexScope


class RootScanPort:
    def __init__(self, group, root):
        self.group, self.root = group, root

    def scan(self, owner_id, **options):
        return self.group.scan(owner_id, self.root, **options)


class ResizableIndexScope(OwnedIndexScope):
    def resize(self, maximum_bytes):
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise ValueError('positive root cache budget required')
        with self._lock:
            if self._closed or self._active_scan_thread is not None:
                raise RuntimeError('root cache is closed or busy')
            with self._index._lock:
                self._index.maximum_bytes = maximum_bytes
                while self._index.retained_bytes > maximum_bytes:
                    self._index.remove(next(iter(self._index.entries)))


class ExecutionRepositoryIndexes:
    def __init__(self, pool, owner_id, execution_id, authorized_roots, *, epoch_source,
                 scope_factory=ResizableIndexScope):
        if not callable(epoch_source):
            raise TypeError('root-aware epoch authority required')
        roots = frozenset(os.path.abspath(root) for root in authorized_roots)
        if not roots:
            raise ValueError('explicit authorized repository roots required')
        self._owner_id = owner_id
        self._roots = roots
        self._epoch_source = epoch_source
        self._factory = scope_factory
        self._condition = Condition()
        self._scopes = {}
        self._limits = {}
        self._used = {}
        self._busy = {}
        self._active = 0
        self._threads = {}
        self._accepting = True
        self._cleanup_complete = False
        self._reservation = pool.reserve(owner_id, execution_id)
        self._total_bytes = self._reservation.maximum_bytes

    def _authorize(self, owner_id):
        if type(owner_id) is not int or owner_id != self._owner_id:
            raise PermissionError('foreign execution index owner')

    def for_root(self, owner_id, root):
        self._authorize(owner_id)
        root = os.path.abspath(root)
        with self._condition:
            if not self._accepting:
                raise RuntimeError('execution repository indexes are closed')
            if root not in self._roots:
                raise PermissionError('repository root is not authorized for this execution')
        return RootScanPort(self, root)

    def scan(self, owner_id, root, **options):
        self._authorize(owner_id)
        root = os.path.abspath(root); thread = get_ident()
        with self._condition:
            if not self._accepting:
                raise RuntimeError('execution repository indexes are closed')
            if root not in self._roots:
                raise PermissionError('repository root is not authorized for this execution')
            while root in self._busy:
                if self._busy[root] == thread:
                    raise RuntimeError('recursive repository root scan refused')
                self._condition.wait()
                if not self._accepting:
                    raise RuntimeError('execution repository indexes are closed')
            self._active += 1
            self._threads[thread] = self._threads.get(thread, 0)+1
            self._busy[root] = thread
            committed = sum(self._limits.values()) + sum(
                size for key, size in self._used.items() if key not in self._limits and key != root)
            limit = max(0, self._total_bytes-committed)
            self._limits[root] = limit
            scope = self._scopes.get(root)
            try:
                if scope is None and limit:
                    scope = self._factory(owner_id, root, limit,
                                          epoch_source=lambda: self._epoch_source(root))
                    self._scopes[root] = scope
            except BaseException:
                self._leave_scan(thread, root, self._used.get(root, 0))
                raise
        try:
            if scope is not None and limit:
                scope.resize(limit)
                return scope.scan(owner_id, **options)
            return outline.scan(root, **options)
        finally:
            primary = sys.exc_info()[1]
            try:
                used = scope.retained_bytes(owner_id) if scope else 0
            except BaseException as cleanup:
                self._leave_scan(thread, root, limit)
                if primary is None:
                    raise
                primary.add_note('retained-byte accounting also failed: '+type(cleanup).__name__)
            else:
                self._leave_scan(thread, root, used)

    def _leave_scan(self, thread, root, used):
        with self._condition:
            self._used[root] = used
            self._limits.pop(root)
            self._busy.pop(root)
            self._active -= 1
            self._threads[thread] -= 1
            if not self._threads[thread]:
                del self._threads[thread]
            self._condition.notify_all()

    def retained_bytes(self, owner_id):
        self._authorize(owner_id)
        with self._condition:
            scopes = tuple(self._scopes.values())
        return sum(scope.retained_bytes(owner_id) for scope in scopes)

    def close(self, owner_id):
        self._authorize(owner_id)
        with self._condition:
            if get_ident() in self._threads:
                raise RuntimeError('cannot close execution indexes from their active scan')
            if self._cleanup_complete:
                return
            self._accepting = False
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._active == 0)
            scopes = tuple(self._scopes.values())
        primary = None
        for scope in scopes:
            try:
                scope.close(owner_id)
            except Exception as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note('another root cleanup failed: '+type(error).__name__)
        if primary is not None:
            raise primary
        self._reservation.release(owner_id)
        with self._condition:
            self._scopes.clear()
            self._used.clear()
            self._epoch_source = None
            self._cleanup_complete = True

    def environment_release(self, owner_id, dispose_environment):
        self._authorize(owner_id)
        def release():
            primary = None
            try:
                dispose_environment()
            except Exception as error:
                primary = error
            try:
                self.close(owner_id)
            except Exception as error:
                if primary is None:
                    raise
                primary.add_note('execution index release also failed: '+type(error).__name__)
            if primary is not None:
                raise primary
        return release
