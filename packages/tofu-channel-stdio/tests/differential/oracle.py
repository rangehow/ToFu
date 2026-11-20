"""Python oracle for the Rust stdio binding's differential tests (packages/tofu-channel-stdio).

Responsibility: drive ``sequence.json`` against the reference adapter
(``python -m tests.channels.conformance.reference``) through a core-side link
and print one ``json.dumps(record, ensure_ascii=False)`` line per record, so
the Rust test compares bytes. Modes: ``python`` uses
``lib.channels.protocol.stdio.StdioLink``; ``rust-link`` uses
``lib.channels.protocol.rust_stdio.RustStdioLink`` (``TOFU_CHANNEL_STDIO_LINK_BIN``);
``json`` prints ``json.dumps(json.loads(line), ensure_ascii=False)`` (or
``ERROR``) for each stdin line. Run with the repository root as cwd and on
``PYTHONPATH``. Dependencies: the two links, the reference adapter.
"""
from __future__ import annotations

import json
import sys
import time

from lib.channels.protocol.contract import ProtocolError


class Host:
    """Records the adapter's calls in a time-independent summary."""

    def __init__(self):
        self.calls = []

    def events(self, batch):
        self.calls.append(['events', [[event['eventId'], event['parts'][0]['text']] for event in batch['events']]])
        return {}

    def health(self, health):
        self.calls.append(['health', health['state'], health['code']])


def drive(link, host, sequence, emit) -> None:
    for index, step in enumerate(sequence):
        if step.get('wait_idle'):
            deadline = time.monotonic() + 30
            while not (link.request('test/idle', {})['idle'] and any(c[0] == 'events' for c in host.calls)):
                if time.monotonic() > deadline:
                    raise SystemExit('the reference adapter never went idle')
                time.sleep(0.01)
            continue
        try:
            result = link.request(step['method'], step['params'])
        except ProtocolError as error:
            emit({'step': index, 'error': {'code': error.code, 'message': str(error), 'data': error.data}})
            continue
        if step.get('volatile'):
            emit({'step': index, 'keys': sorted(result)})
        else:
            emit({'step': index, 'result': result})
    emit({'host': host.calls})
    closed = link.close(5.0)
    emit({'closed': closed, 'returncode': link.returncode})


def main() -> int:
    mode = sys.argv[1]
    emit = lambda record: print(json.dumps(record, ensure_ascii=False), flush=True)  # noqa: E731
    if mode == 'json':
        for line in sys.stdin.buffer:
            try:
                print(json.dumps(json.loads(line.decode('utf-8')), ensure_ascii=False), flush=True)
            except (UnicodeDecodeError, ValueError):
                print('ERROR', flush=True)
        return 0
    with open(sys.argv[2], encoding='utf-8') as handle:
        sequence = json.load(handle)
    argv = [sys.executable, '-m', 'tests.channels.conformance.reference']
    if mode == 'python':
        from lib.channels.protocol.stdio import StdioLink as Link
    else:
        from lib.channels.protocol.rust_stdio import RustStdioLink as Link
    link = Link(argv)
    host = Host()
    link.bind(host)
    drive(link, host, sequence, emit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
