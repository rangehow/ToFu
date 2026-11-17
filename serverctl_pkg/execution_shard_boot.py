"""Start an execution shard from its primary's exported source generation.

Standard library only: the primary spawns this file from
``PinnedHelperSource`` (serverctl_pkg/execution_shards.py), so a merge that
rewrote the checkout after the primary booted changes neither this bootstrap
nor the application code the shard runs. Arguments:
``<source snapshot> <host module> <host arguments...>``, or
``<source snapshot> <host module> --fork-server <socket fd>`` for the primary's
fork server: it preloads the shard image once and forks each shard from it
(``_serve_forks``), so a shard skips interpreter start, imports and warm-up.

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

FORK_SERVER_FLAG = '--fork-server'


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
    if arguments[:1] == [FORK_SERVER_FLAG]:
        # Returns only in a forked shard, with that shard's host arguments.
        arguments = _serve_forks(source, int(arguments[1]))
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


def _serve_forks(source, channel_fd):
    """Preload the shard image once, then fork one shard per request.

    Protocol (SOCK_SEQPACKET, one JSON message each way): this process sends
    ``{"ready": true}`` once preloaded, or ``{"error": ...}`` and exits. Each
    request ``{"argv": [...], "fdArguments": [flag, ...], "modules": [...]}``
    carries one descriptor per flag (SCM_RIGHTS) and the modules the primary
    has imported by now, which this image imports before it forks; the reply
    is ``{"pid": N}`` or ``{"error": ...}``. EOF from the primary ends this process; shards it
    forked are their own sessions, bound to the primary by their manager pipe.

    Fork is safe only from a single-threaded process: nothing here starts a
    thread, and every fork re-checks that, refusing instead of inheriting a
    held lock.
    """
    import json
    import select
    import signal
    import socket
    import threading
    channel = socket.socket(fileno=channel_fd)
    try:
        from serverctl_pkg.execution_shards import import_primary_modules, preload_shard_image
        preload_shard_image(source)
        if threading.active_count() != 1:
            raise RuntimeError('preloading the shard image started a thread; fork is unsafe')
    except BaseException as error:
        channel.send(json.dumps({'error': f'{type(error).__name__}: {str(error)[:300]}'}).encode())
        raise SystemExit(1)
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    previous_wakeup = signal.set_wakeup_fd(wake_write)
    previous_child = signal.signal(signal.SIGCHLD, lambda *_: None)
    channel.send(b'{"ready": true}')

    def reap():
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return

    while True:
        readable = select.select([channel, wake_read], [], [])[0]
        if wake_read in readable:
            while True:
                try:
                    if not os.read(wake_read, 512):
                        break
                except BlockingIOError:
                    break
            reap()
        if channel not in readable:
            continue
        message, descriptors, _flags, _address = socket.recv_fds(channel, 1 << 16, 8)
        if not message:
            raise SystemExit(0)
        try:
            request = json.loads(message)
            argv = [str(value) for value in request['argv']]
            flags = list(request['fdArguments'])
            if len(flags) != len(descriptors):
                raise ValueError('one descriptor per descriptor argument is required')
            # What the primary has imported since: once here, shared by later shards.
            import_primary_modules([str(name) for name in request.get('modules', ())])
            positions = [argv.index(flag) + 1 for flag in flags]
            if max(positions, default=0) >= len(argv):
                raise ValueError('a descriptor argument has no value')
            if threading.active_count() != 1:
                raise RuntimeError('the fork server is no longer single-threaded')
            pid = os.fork()
        except Exception as error:
            for descriptor in descriptors:
                os.close(descriptor)
            channel.send(json.dumps({'error': f'{type(error).__name__}: {str(error)[:300]}'}).encode())
            continue
        if pid == 0:
            # The shard: drop every fork-server resource, then run as if spawned.
            signal.set_wakeup_fd(previous_wakeup)
            signal.signal(signal.SIGCHLD, previous_child)
            channel.close()
            os.close(wake_read)
            os.close(wake_write)
            os.setsid()
            for position, descriptor in zip(positions, descriptors):
                argv[position] = str(descriptor)
            return argv
        for descriptor in descriptors:
            os.close(descriptor)
        channel.send(json.dumps({'pid': pid}).encode())


if __name__ == '__main__':
    main()
