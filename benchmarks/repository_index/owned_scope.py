"""Experimental terminal cache owner with an explicit epoch authority.

This is not installed in task state or the application. The caller owns the
authorized root, epoch producer, byte budget and invocation lifetime.
"""
from threading import RLock, get_ident
from index import Index, outline


class OwnedIndexScope:
    def __init__(self, owner_id, root, maximum_bytes, *, epoch_source):
        if not callable(epoch_source):
            raise TypeError('an explicit extractor epoch authority is required')
        self._index = Index(owner_id, root, maximum_bytes)
        self._owner_id = owner_id
        self._epoch_source = epoch_source
        self._lock = RLock()
        self._closed = False
        self._cleanup_complete = False
        self._active_scan_thread = None

    def _authorize(self, owner_id):
        if type(owner_id) is not int or owner_id != self._owner_id:
            raise PermissionError('foreign index scope owner')

    def scan(self, owner_id, **options):
        self._authorize(owner_id)
        if 'extractor_epoch' in options:
            raise ValueError('scope epoch authority owns extractor versions')
        with self._lock:
            if self._closed:
                raise RuntimeError('repository index scope is closed')
            if self._active_scan_thread is not None:
                raise RuntimeError('repository index scope has an active scan; recursive scan refused')
            self._active_scan_thread = get_ident()
            try:
                return self._index.scan(owner_id, extractor_epoch=self._epoch_source(), **options)
            finally:
                self._active_scan_thread = None

    def close(self, owner_id):
        self._authorize(owner_id)
        with self._lock:
            if self._active_scan_thread == get_ident():
                raise RuntimeError('cannot close repository index scope from its active scan')
            if self._cleanup_complete:
                return
            self._closed = True
            self._index.close()
            self._epoch_source = None
            self._cleanup_complete = True

    def retained_bytes(self, owner_id):
        self._authorize(owner_id)
        with self._lock:
            return self._index.retained_bytes

    def __enter__(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('repository index scope is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close(self._owner_id)
        except Exception:
            if exc_value is None:
                raise
            outline.logger.exception('Repository index cleanup failed while preserving caller failure')
