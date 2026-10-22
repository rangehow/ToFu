"""Actual Headless page/SSE and observer lifecycle over private native suffix ports.

Reuse the existing list/native public oracle and cancellation checks with a
private execution constructor. The application and module globals are unchanged.
No real TCP, durable recovery or production adoption is asserted.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import FunctionType

import check_public
from check import module, artifact
from public_tail import bind_native_public_reader


def execution_factory(*args, **kwargs):
    execution = check_public.AgentExecution(*args, **kwargs)
    if type(execution._task.get('events')) is module.ReplayWindow:
        bind_native_public_reader(execution)
    return execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    namespace = dict(check_public.check.__globals__, AgentExecution=execution_factory)
    namespace['observe'] = FunctionType(check_public.observe.__code__, namespace)
    check = FunctionType(check_public.check.__code__, namespace)
    names = ('benchmarks/native_event/check_public_tail.py',
             'benchmarks/native_event/public_tail.py',
             'benchmarks/native_event/native_reader.py',
             'benchmarks/native_event/check_public.py', 'benchmarks/native_event/runtime.py',
             'benchmarks/native_event/check.py', 'lib/agent_core/task_runtime.py',
             'lib/task_replay/task_replay.py', 'lib/agent_core/run_contract.py',
             'tofu_agent/runtime.py', 'tofu_agent/server.py')
    sources = {name: hashlib.sha256((check_public.ROOT/name).read_bytes()).hexdigest() for name in names}
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete', 'python': sys.version,
              'sources': sources, 'library_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
              'checks': asyncio.run(check())}
    for name, digest in sources.items():
        assert hashlib.sha256((check_public.ROOT/name).read_bytes()).hexdigest() == digest, name
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print('actual auth/page/SSE/terminal parity and disconnect/reconnect observer cleanup with native suffix ports')


if __name__ == '__main__':
    main()
