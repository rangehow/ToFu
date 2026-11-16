"""Process-incarnation evidence shared by workflow recovery policies.

process_owner stamps host, OS boot, PID namespace, PID and kernel start time.
owner_state proves alive/dead from the kernel within the same observable
namespace. Outside it (another host, boot or PID namespace, e.g. after a host
move) only recorded evidence proves death: record_exit stores one fixed-shape
record per incarnation, written by an observer that proved the exit from the
kernel in the owner's namespace; record_host_retired stores one record per
host that handed this data root's serving lock to another host, so every
process on it belongs to a stopped serving era. Both live in the directory the
process family reads (exit_evidence_dir). A retirement never covers the
reading host itself. Missing or inaccessible evidence is unknown. This grants
no task or external-job authority.
"""
from pathlib import Path
import hashlib
import json
import os
import socket

EXIT_RECORD_FORMAT = 'tofu.process-exit/v1'
HOST_RETIREMENT_FORMAT = 'tofu.host-retired/v1'
_OWNER_FIELDS = ('host', 'boot', 'pid_namespace', 'pid', 'started')


def _process_stat(pid):
    # comm may contain spaces or parentheses; fields after its final ')' start
    # with field 3. num_threads is field 20; starttime is field 22.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    state, started, threads = fields[0], fields[19], fields[17]
    if (state not in {'R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I'}
            or not started.isascii() or not started.isdecimal()
            or not threads.isascii() or not threads.isdecimal() or int(threads) < 1):
        raise ValueError('Invalid local process state or start time')
    return started, state, int(threads)


def _started(pid):
    return _process_stat(pid)[0]


def process_owner():
    try:
        return {'host': socket.gethostname(), 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pid_namespace': str(os.stat('/proc/self/ns/pid').st_ino),
                'pid': os.getpid(), 'started': _started(os.getpid())}
    except (OSError, ValueError, IndexError):
        return None


def _well_formed(owner):
    return (isinstance(owner, dict)
            and all(isinstance(owner.get(key), str) for key in ('host', 'boot', 'pid_namespace'))
            and type(owner.get('pid')) is int and owner['pid'] >= 1
            and isinstance(owner.get('started'), str)
            and owner['started'].isascii() and owner['started'].isdecimal())


def _kernel_state(owner):
    current = process_owner()
    if not current or any(owner[key] != current[key] for key in ('host', 'boot', 'pid_namespace')):
        return 'unknown'
    try:
        started, state, threads = _process_stat(owner['pid'])
        # An exited leader can retain living worker threads. Only its final
        # unreaped thread proves whole-process exit. Other states remain alive.
        return 'dead' if (started != owner['started']
                          or (state in {'Z', 'X', 'x'} and threads == 1)) else 'alive'
    except FileNotFoundError:
        return 'dead'
    except (OSError, ValueError, IndexError):
        return 'unknown'


def exit_evidence_dir():
    """Where this process family records and reads exits; on the shared data root."""
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'process-exits')


def _exit_record_path(owner, directory):
    identity = {key: owner[key] for key in _OWNER_FIELDS}
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return Path(directory) / f'{hashlib.sha256(encoded).hexdigest()}.json', identity


def _exit_recorded(owner):
    path, identity = _exit_record_path(owner, exit_evidence_dir())
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return (isinstance(record, dict) and record.get('format') == EXIT_RECORD_FORMAT
            and record.get('owner') == identity)


def record_exit(owner, directory):
    """Record an exit this process proved from the kernel in the owner's namespace.

    A record never re-certifies another record: only kernel evidence observed
    here admits one, so the evidence cannot be laundered across hosts.
    """
    if not _well_formed(owner) or _kernel_state(owner) != 'dead':
        raise ValueError('process exit is not proved by local kernel evidence')
    from lib.json_store import write_json_atomic
    path, identity = _exit_record_path(owner, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_atomic(str(path), {'format': EXIT_RECORD_FORMAT, 'owner': identity,
                                  'observer': process_owner()}, mode=0o600)
    return str(path)


def _host_retirement_path(host, directory):
    digest = hashlib.sha256(host.encode('utf-8')).hexdigest()
    return Path(directory) / 'hosts' / f'{digest}.json'


def record_host_retired(host, directory, *, evidence):
    """Record that ``host`` no longer serves this data root; called by its successor.

    The successor holds the serving lock the retired host named, so the
    retired host's manager stopped (joining its execution host), or the
    operator started this host after it went away.
    """
    if not isinstance(host, str) or not host or host == socket.gethostname():
        raise ValueError('only another host can be retired')
    from lib.json_store import write_json_atomic
    path = _host_retirement_path(host, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_atomic(str(path), {'format': HOST_RETIREMENT_FORMAT, 'host': host,
                                  'successor': process_owner(), 'evidence': evidence}, mode=0o600)
    return str(path)


def _host_retired(owner):
    if owner['host'] == socket.gethostname():
        return False
    try:
        record = json.loads(_host_retirement_path(owner['host'], exit_evidence_dir())
                            .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return (isinstance(record, dict) and record.get('format') == HOST_RETIREMENT_FORMAT
            and record.get('host') == owner['host'])


def owner_state(owner):
    if not _well_formed(owner):
        return 'unknown'
    state = _kernel_state(owner)
    if state == 'unknown' and (_exit_recorded(owner) or _host_retired(owner)):
        return 'dead'
    return state


__all__ = ["EXIT_RECORD_FORMAT", "HOST_RETIREMENT_FORMAT", "exit_evidence_dir", "owner_state",
           "process_owner", "record_exit", "record_host_retired"]
