"""Start an execution shard from its primary's exported source generation.

Standard library only: the primary spawns this file from
``PinnedHelperSource`` (serverctl_pkg/execution_shards.py), so a merge that
rewrote the checkout after the primary booted changes neither this bootstrap
nor the application code the shard runs. Arguments:
``<source snapshot> <host module> <host arguments...>``.

The snapshot holds the bytes the primary's frozen ``PinnedImportSource``
retained. A minimal finder serves them until the application's own
``PinnedImportSource`` (loaded from those same bytes) replaces it; every
application import after that comes from the snapshot, never the checkout.
"""
import importlib.machinery
import importlib.util
import marshal
import os
import runpy
import sys
import zlib


def _load(path):
    with open(path, 'rb') as stream:
        status = os.fstat(stream.fileno())
        # The primary wrote it 0600 in its private directory; anything else
        # could hand this process foreign code.
        if status.st_uid != os.geteuid() or status.st_mode & 0o077:
            raise SystemExit('execution source snapshot is not private to this user')
        return marshal.load(stream)


def main():
    snapshot_path, module, *arguments = sys.argv[1:]
    snapshot = _load(snapshot_path)
    pins = snapshot['pins']
    roots = [os.path.abspath(root) for root in snapshot['roots']]

    class _Loader(importlib.machinery.SourceFileLoader):
        def path_stats(self, path):
            if path == self.path:
                return {'mtime': pins[path][0], 'size': pins[path][2]}
            return super().path_stats(path)

        def get_data(self, path):
            if path == self.path:
                return zlib.decompress(pins[path][3])
            return super().get_data(path)

        def set_data(self, path, data, *, _mode=0o666):
            # Bytecode belongs to the checkout's revision; the application's
            # own loader writes it only while the checkout still holds the pin.
            return None

    class _Finder:
        @classmethod
        def find_spec(cls, fullname, path=None, target=None):
            name = fullname.rpartition('.')[2]
            for entry in (roots if path is None else path):
                location = os.path.join(os.path.abspath(entry), name)
                for origin, locations in ((os.path.join(location, '__init__.py'), [location]),
                                          (location + '.py', None)):
                    if origin in pins:
                        return importlib.util.spec_from_file_location(
                            fullname, origin, loader=_Loader(fullname, origin),
                            submodule_search_locations=locations)
            return None

    # As ``python -m`` from the checkout: the working directory, not this
    # helper's private directory, heads the import path.
    sys.path[0] = os.getcwd()
    sys.meta_path.insert(0, _Finder)
    try:
        from lib.runtime import pinned_import_source
        source = pinned_import_source.PinnedImportSource.from_snapshot(snapshot)
        source.install_frozen()
        pinned_import_source.adopt_bootstrapped(source)
    finally:
        sys.meta_path.remove(_Finder)
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
