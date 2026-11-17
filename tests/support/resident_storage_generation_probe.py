"""Disposable resident storage host whose start fails for a chosen engine.

Used only by ``tests/server/test_restart_storage_rollback.py``. The manager
launches it with the resident protocol of ``serverctl_pkg.native_storage_host``.
When the plan's ``binarySha256`` is listed in ``bad-engines`` (in its working
directory, the project), the start fails the way the 2026-11-17 21:49 host did
and the host publishes that terminal verdict, then stays until stopped. Any
other engine publishes ``ready``. Manager EOF always ends the probe.
"""
import argparse
from pathlib import Path
import select
import signal
import threading

from serverctl_pkg.native_storage_service import RECORD_FORMAT
from serverctl_pkg.resident_service import (
    await_published_record, manager_control_matches, write_record,
)

FAILED_START = ('RuntimeError: sidecar health preflight did not pass '
                '(probe_error=timeout; unhealthy=)')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--service-id')
    parser.add_argument('--state-file')
    for name in ('--manager-pid', '--manager-start', '--manager-fd', '--lease-fd'):
        parser.add_argument(name, type=int)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    state = Path(args.state_file)
    record = await_published_record(
        state, service_id=args.service_id, manager_pid=args.manager_pid,
        manager_start=args.manager_start, record_format=RECORD_FORMAT, stopped=stopped)
    if not record or not manager_control_matches(record, args.manager_fd, write=False):
        return 1
    bad = Path('bad-engines')
    engine = str((record.get('plan') or {}).get('binarySha256') or '')
    failing = bad.exists() and engine in bad.read_text().split()
    record['state'] = 'failed' if failing else 'ready'
    record['error'] = FAILED_START if failing else ''
    write_record(state, record)
    while not stopped.wait(0.05):
        if select.select([args.manager_fd], [], [], 0)[0]:
            return 0
    record['state'], record['error'] = 'stopped', ''
    write_record(state, record)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
