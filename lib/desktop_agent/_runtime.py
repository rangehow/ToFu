"""Bounded process-local poll state; no durable execution authority.

Admission and validated batch limits reserve terminal-result space before work. IDs are never
forgotten within a run: at the ledger ceiling new work fails closed rather than
replaying old side effects. Stream producers backpressure, never the poller.
"""
import json
import random
import threading


class Outbox:
    def __init__(self, condition, stop, max_items, max_bytes):
        self.condition = condition
        self.stop = stop
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.items = []
        self.sizes = []
        self.bytes = 0

    def append(self, item):
        size = len(json.dumps(item, ensure_ascii=False, default=str).encode('utf-8'))
        if size > self.max_bytes:
            raise ValueError('Desktop transport frame exceeds outbox byte budget')
        with self.condition:
            while len(self.items) >= self.max_items or self.bytes + size > self.max_bytes:
                if self.stop.is_set():
                    raise RuntimeError('Desktop transport stopped before delivery')
                self.condition.wait(0.2)
            self.items.append(item)
            self.sizes.append(size)
            self.bytes += size

    def __iter__(self):
        return iter(self.items)

    def __delitem__(self, key):
        self.bytes -= sum(self.sizes[key])
        del self.items[key]
        del self.sizes[key]
        self.condition.notify_all()


def execution_capacity():
    """Use the shared cached launch probe; lean fallback, four-worker cap."""
    import os
    try:
        from runtime_guards import probe_system_resources
        snapshot = probe_system_resources()
        memory = min(snapshot.effective_memory_capacity_mb or 1024,
                     snapshot.effective_memory_available_mb or 1024)
        default = min(4, max(1, snapshot.effective_cpu_count),
                      max(1, memory // 1024))
    except Exception:
        default = 1
    try:
        return max(1, min(4, int(os.environ.get(
            'TOFU_DESKTOP_EXECUTION_WORKERS', default))))
    except (TypeError, ValueError):
        return default


class RuntimeState:
    MAX_RUNNING = 4
    MAX_IDS = 65536

    def __init__(self, stop):
        self.stop = stop
        self.MAX_RUNNING = execution_capacity()
        self.lock = threading.Condition(threading.RLock())
        self.results = Outbox(self.lock, stop, 512, 16 * 1024 * 1024)
        self.streams = Outbox(self.lock, stop, 256, 2 * 1024 * 1024)
        self.seen = set()
        self.running = set()

    def admit(self, cmd_id):
        with self.lock:
            if cmd_id in self.seen:
                return False
            if len(self.seen) >= self.MAX_IDS:
                error = 'Execution ID ledger full; restart agent before submitting new work'
            else:
                self.seen.add(cmd_id)
                error = ('Desktop execution capacity exhausted; submit a new command ID'
                         if len(self.running) >= self.MAX_RUNNING else None)
            if error:
                self.results.append({'id': cmd_id, 'result': None, 'error': error})
                return False
            self.running.add(cmd_id)
            return True

    def finish(self, cmd_id):
        with self.lock:
            self.running.discard(cmd_id)


def validate_poll_response(data):
    """Validate the complete command batch before acknowledging any outbox."""
    if not isinstance(data, dict) or not isinstance(data.get('commands'), list):
        raise ValueError('Invalid desktop poll envelope')
    commands = data['commands']
    if len(commands) > 256:
        raise ValueError('Desktop command batch exceeds admission budget')
    for cmd in commands:
        if (not isinstance(cmd, dict) or not isinstance(cmd.get('id'), str)
                or not cmd['id'] or len(cmd['id']) > 256
                or not isinstance(cmd.get('type'), str) or not cmd['type']
                or not isinstance(cmd.get('params', {}), dict)):
            raise ValueError('Invalid desktop command envelope')
    return commands


def retry_delay(interval, failures):
    ceiling = min(max(float(interval), 0.001) * 2 ** min(failures, 8), 60.0)
    return random.uniform(ceiling / 2, ceiling)
