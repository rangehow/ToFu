"""Explicit in-process extraction experiment, with one coarse call per batch.

No application selector or repository cache. Libraries/grammars remain resident
until process exit. Caller owns trusted artifacts and process isolation: this
boundary does not provide a killable timeout or production lifecycle admission.
"""
import ctypes
from threading import Lock

from native_extraction_scan import NativeBatchScanner


class NativeLibraryBatchScanner(NativeBatchScanner):
    def __init__(self, binary, grammar_directory, *, strict=True):
        super().__init__(binary, grammar_directory, strict=strict)
        self.grammar_bytes = str(self.grammar_directory).encode('utf8')
        self._binding = None
        self._binding_lock = Lock()

    def _ensure_binding(self):
        if self._binding is None:
            with self._binding_lock:
                if self._binding is None:
                    library = ctypes.CDLL(str(self.binary))
                    extract = library.tofu_repository_extract_json_v1
                    extract.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                       ctypes.c_char_p, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
                    extract.restype = ctypes.c_int
                    # Publish only the complete binding. Failure/interruption
                    # leaves it retryable; the tuple keeps the library alive.
                    self._binding = (library, extract)
        return self._binding

    @property
    def library(self):
        return self._ensure_binding()[0]

    @property
    def extract(self):
        return self._ensure_binding()[1]

    def invoke(self, payload, response_limit):
        if not isinstance(payload, bytes) or not payload or response_limit <= 0:
            raise ValueError('native extraction requires nonempty bytes and positive capacity')
        output = ctypes.create_string_buffer(response_limit)
        written = ctypes.c_size_t(0)
        status = self.extract(self.grammar_bytes, len(self.grammar_bytes),
                              payload, len(payload), output, response_limit, ctypes.byref(written))
        if status != 0:
            raise RuntimeError(f'native extraction refused with status {status}')
        if written.value > response_limit:
            raise RuntimeError('native extraction exceeded caller buffer')
        return ctypes.string_at(output, written.value)


class NativeCatalogBatchScanner(NativeLibraryBatchScanner):
    """Explicit v2 candidate: native pinned POSIX catalog, no Python parser imports."""
    format = 'tofu.repository-extraction/v2'

    def is_code_path(self, name):
        from native_source_admission import is_code_path
        return is_code_path(name)

    def source_request(self, name, text):
        return dict(rel_path=name, source=text)
