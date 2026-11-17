"""Inventory the shard fork server's preloaded image (run as a child process).

Prints one JSON object: threads, open descriptors other than stdio, and every
module-level value that is a lock, socket, connection-like object or a
uuid/pid-shaped identity, by ``module.attribute``. The fork-safety specs in
``test_execution_shards.py`` assert on it.
"""
import json
import os
import sys
import threading


def main(modules):
    from serverctl_pkg.execution_shards import preload_shard_image

    class Source:
        loaded_modules = tuple(modules)
    before = set(sys.modules)
    preload_shard_image(Source())
    import _thread
    import socket
    lock_types = (type(threading.Lock()), type(threading.RLock()), _thread.LockType)
    import re
    hex_identity = re.compile(r'[0-9a-f]{32}')
    found = {'locks': [], 'held_locks': [], 'sockets': [], 'connections': [], 'pid_values': [],
             'random_identities': []}
    pid = os.getpid()
    for name, module in list(sys.modules.items()):
        if module is None or not (name.startswith(('lib.', 'serverctl_pkg', 'tofu_', 'runtime_guards', 'server'))):
            continue
        for attribute, value in list(vars(module).items()):
            where = f'{name}.{attribute}'
            if isinstance(value, lock_types) or isinstance(value, threading.Condition):
                found['locks'].append(where)
                probe = value._lock if isinstance(value, threading.Condition) else value
                if hasattr(probe, 'locked') and probe.locked():
                    found['held_locks'].append(where)
                elif type(probe).__name__ == 'RLock' and probe._is_owned():
                    found['held_locks'].append(where)
            elif isinstance(value, socket.socket):
                found['sockets'].append(where)
            elif type(value).__name__ in {'Connection', 'StorageV2Client', 'Session', 'Client', 'HTTPConnection'}:
                found['connections'].append(where)
            elif type(value) is int and value == pid:
                found['pid_values'].append(where)
            elif isinstance(value, str) and hex_identity.fullmatch(value):
                found['random_identities'].append(where)
    descriptors = []
    for fd in sorted(int(entry) for entry in os.listdir('/proc/self/fd')):
        if fd <= 2:
            continue
        try:
            descriptors.append(os.readlink(f'/proc/self/fd/{fd}'))
        except OSError:
            continue
    print(json.dumps({'threads': [thread.name for thread in threading.enumerate()],
                      'descriptors': descriptors, 'imported': len(set(sys.modules) - before),
                      **found}))


if __name__ == '__main__':
    main(sys.argv[1:])
