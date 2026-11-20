"""SIGKILL/reopen driver for real engine_session child adapters.

Adapters receive --storage-config FILE --data-root ROOT, and one JSON line with
process.phase initial/resume. The authority must outlive both children. Initial
execution flushes {kind: fault_ready, requestId, storageAuthority, checkpoint}
then blocks; resume loads that receipt through the real recovery owner and emits
{id, parts, recovery: {storageAuthority, checkpoint}}. Neither this driver nor
its protocol tests implement storage or certify an adapter's storage semantics.
Missing adapters/receipts are failures, never explained differences or skips.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

from .compare import CHECKOUT, HERE, compare_answers


class ProcessFaultError(RuntimeError):
    pass


def _read_frame(process: subprocess.Popen, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ProcessFaultError('child did not acknowledge the crash barrier')
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise ProcessFaultError('child exited before the crash barrier')
            pending.extend(chunk)
            if b'\n' in pending:
                line, extra = pending.split(b'\n', 1)
                if extra.strip():
                    raise ProcessFaultError('child emitted output past the crash barrier')
                try:
                    frame = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise ProcessFaultError('invalid crash barrier JSON') from error
                if not isinstance(frame, dict):
                    raise ProcessFaultError('crash barrier must be an object')
                return frame


def _environment(root: Path) -> dict[str, str]:
    environment = {name: os.environ[name] for name in (
        'PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'LD_LIBRARY_PATH',
    ) if name in os.environ}
    for directory in ('home', 'tmp', 'data', 'heartbeats'):
        (root / directory).mkdir()
    environment.update({
        'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
        'PYTHONPATH': str(CHECKOUT), 'TOFU_DATA_DIR': str(root / 'data'),
        'TOFU_PROJECT_PATH': str(root), 'TOFU_STORAGE_PROJECT_ROOT': str(root),
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE': '1',
        'TOFU_HEARTBEAT_DIR': str(root / 'heartbeats'),
        'PORT': '9', 'TOFU_SUPERVISOR_PORT': '9',
        'TOFU_API_URL': 'http://127.0.0.1:9',
        'TOFU_API_BASE': 'http://127.0.0.1:9', 'TOFU_ORIGIN': 'http://127.0.0.1:9',
    })
    return environment


def _kill_and_join(process: subprocess.Popen) -> None:
    # Children are session leaders: include any transport/tool descendants.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _crash_at_barrier(process, request, encoded, timeout):
    process.stdin.write(encoded)
    process.stdin.flush()
    barrier = _read_frame(process, timeout)
    for failure in ('unsupported', 'error'):
        if failure in barrier:
            raise ProcessFaultError(f'initial child {failure}: {barrier[failure]}')
    if (barrier.get('kind') != 'fault_ready'
            or barrier.get('requestId') != request['id']
            or not barrier.get('storageAuthority')
            or not barrier.get('checkpoint')):
        raise ProcessFaultError('missing exact durable crash barrier receipt')
    if process.poll() is not None:
        raise ProcessFaultError('child must block alive at the crash barrier')
    _kill_and_join(process)
    if process.returncode != -signal.SIGKILL:
        raise ProcessFaultError('initial child was not killed by SIGKILL')
    return barrier


def _resume_answer(process, request, encoded, barrier, timeout):
    stdout, _ = process.communicate(encoded, timeout=timeout)
    if process.returncode:
        raise ProcessFaultError(f'resume child failed: exit {process.returncode}')
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ProcessFaultError('resume must emit exactly one answer')
    try:
        answer = json.loads(lines[0])
    except (ValueError, UnicodeError) as error:
        raise ProcessFaultError('invalid resume JSON') from error
    if not isinstance(answer, dict) or answer.get('id') != request['id']:
        raise ProcessFaultError('resume answer identity mismatch')
    expected = {key: barrier[key] for key in ('storageAuthority', 'checkpoint')}
    if answer.get('recovery') != expected:
        raise ProcessFaultError('resume did not acknowledge the exact durable source')
    if 'error' in answer or 'unsupported' in answer or 'parts' not in answer:
        raise ProcessFaultError('resume did not produce supported engine parts')
    return answer


def run_crash_case(command: list[str], request: dict, *, data_root: Path,
                   storage_config: Path, timeout: float) -> dict:
    """Run actual child death/reopen; timeout is supplied by the owning test."""
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError('command must be a nonempty argv list')
    crash_after = request.get('input', {}).get('restartAfterRequest')
    if type(crash_after) is not int or crash_after <= 0:
        raise ValueError('process faults require restartAfterRequest > 0')
    storage_config = storage_config.resolve(strict=True)
    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=False)
    environment = _environment(data_root)
    argv = [*command, '--storage-config', str(storage_config),
            '--data-root', str(data_root)]
    processes = []
    with (data_root / 'child.stderr').open('wb') as stderr:
        try:
            for phase in ('initial', 'resume'):
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=stderr, cwd=CHECKOUT, env=environment,
                                           start_new_session=True)
                processes.append(process)
                payload = {**request, 'process': {'phase': phase}}
                encoded = (json.dumps(payload, ensure_ascii=False) + '\n').encode()
                if phase == 'initial':
                    barrier = _crash_at_barrier(process, request, encoded, timeout)
                else:
                    answer = _resume_answer(process, request, encoded, barrier, timeout)
            if processes[0].pid == processes[1].pid:
                raise ProcessFaultError('restart reused the initial process')
            return {'answer': answer, 'barrier': barrier,
                    'initialPid': processes[0].pid, 'resumePid': processes[1].pid,
                    'initialExit': processes[0].returncode}
        except subprocess.TimeoutExpired as error:
            raise ProcessFaultError('resume child did not complete') from error
        finally:
            for process in processes:
                _kill_and_join(process)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-command', type=json.loads, required=True)
    parser.add_argument('--rust-command', type=json.loads, required=True)
    parser.add_argument('--python-storage-config', type=Path, required=True)
    parser.add_argument('--rust-storage-config', type=Path, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in (HERE / 'cases.jsonl').read_text().splitlines()
             if line.strip()]
    results = []
    with tempfile.TemporaryDirectory(prefix='engine-process-faults-') as temporary:
        for case in cases:
            if case['input'].get('restartAfterRequest') is None:
                continue
            request = {'id': case['name'], 'boundary': 'engine_session', 'input': case['input']}
            row = {'case': case['name'], 'identical': False}
            try:
                for side in ('python', 'rust'):
                    row[side] = run_crash_case(
                        getattr(args, f'{side}_command'), request,
                        data_root=Path(temporary) / case['name'] / side,
                        storage_config=getattr(args, f'{side}_storage_config'),
                        timeout=args.timeout)
                row.update(compare_answers(case['name'], row['python']['answer'],
                                           row['rust']['answer']))
            except (ProcessFaultError, OSError, ValueError) as error:
                row['error'] = str(error)
            results.append(row)
    report = {'scope': 'actual_child_sigkill_restart', 'cases': results,
              'identical': bool(results) and all(row['identical'] for row in results)}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'cases': len(results), 'identical': report['identical']}))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
