#!/usr/bin/env python3
"""Reproduce and verify fix for the project state race bug.

The bug (2026-05-05):
    lib/project_mod/config.py keeps module-level _state and _roots
    shared across ALL tasks on the server. When set_project() is called
    with a different primary path, it does _roots.clear() — wiping any
    other concurrent task's root registration.

Observable symptom:
    Task A starts with projectPath=/ws/django-11815, registers its
    workspace root. Task B starts with projectPath=/ws/django-13925,
    set_project() clears _roots, only B's root remains. When task A's
    model later issues a tool call with path='django-11815:src/file.py',
    resolve_namespaced_path fails → "Unknown workspace root".

Real-world evidence:
    - 76 "Unknown workspace root" events in logs/app.log
    - django__django-13925__tofu-opus had 33 refused tool calls
    - django__django-12406__tofu-glm had 26 refused (and FAILED)
    - User chat sessions got clobbered by SWE-bench workspaces too
      ("chatui:lib/..." rejected because primary was now a django workspace)

This test reproduces the race without running actual LLM inference.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_fake_project(base_tmp: Path, name: str) -> Path:
    """Create a scratch dir that looks like a project."""
    p = base_tmp / name
    p.mkdir(parents=True, exist_ok=True)
    (p / 'hello.txt').write_text('hi')
    return p


def test_concurrent_set_project_clobbers_other_task():
    """
    CORE REPRO. Two tasks each register a different project_path with
    different basenames. Assert that *both* can still resolve their own
    namespaced path after the other has registered.
    """
    import lib.project_mod.config as cfg
    import lib.project_mod.scanner as scanner

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        ws_a = _make_fake_project(tmp_p, 'instance-A')
        ws_b = _make_fake_project(tmp_p, 'instance-B')

        # Clean global state before the test
        scanner.clear_project()

        # Task A registers
        scanner.set_project(str(ws_a))
        # A observes: can resolve 'instance-A:hello.txt' → (ws_a_path, 'hello.txt')
        base_a, rel_a = cfg.resolve_namespaced_path('instance-A:hello.txt')
        assert base_a == str(ws_a), f'Task A self-resolve broken: {base_a}'
        assert rel_a == 'hello.txt'

        # Task B arrives — clobbers _roots
        scanner.set_project(str(ws_b))

        # Task B can resolve its own
        base_b, rel_b = cfg.resolve_namespaced_path('instance-B:hello.txt')
        assert base_b == str(ws_b), f'Task B self-resolve broken: {base_b}'

        # ★ THE BUG: Task A cannot resolve its own root anymore,
        #   because _roots was wiped by Task B's set_project.
        try:
            cfg.resolve_namespaced_path('instance-A:hello.txt')
            # If it works now, we're past the bug (fixed at global level)
            print('  UNEXPECTED: Task A resolved via global _roots — fix present but different path')
            return True
        except ValueError as e:
            print(f'  REPRO confirmed: {e}')
            return False


def test_resolve_base_with_task_base_path():
    """After fix: _resolve_base(base_path=task_workspace, 'basename:foo')
    should resolve to task_workspace + foo even if global _roots has been
    wiped by a concurrent task."""
    from lib.project_mod import scanner
    from lib.project_mod.tools import _resolve_base

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        ws_a = _make_fake_project(tmp_p, 'instance-A')
        ws_b = _make_fake_project(tmp_p, 'instance-B')

        scanner.clear_project()
        scanner.set_project(str(ws_a))  # Primary = A
        scanner.set_project(str(ws_b))  # Primary = B — clobbers A

        # After fix: even though _roots only has B, a call with base_path=ws_a
        # and rel='instance-A:hello.txt' should succeed.
        try:
            abs_base, rel = _resolve_base(str(ws_a), 'instance-A:hello.txt')
            print(f'  After fix: _resolve_base(ws_a, "instance-A:hello.txt") → '
                  f'({abs_base!r}, {rel!r})')
            assert abs_base == str(ws_a), f'Wrong base: {abs_base}'
            assert rel == 'hello.txt', f'Wrong rel: {rel}'
            return True
        except ValueError as e:
            print(f'  After fix: STILL FAILS: {e}')
            return False


def test_per_conv_state_isolation():
    """After per-conv fix: two concurrent conversations with different
    project_paths should not interfere in root resolution, AT ALL."""
    try:
        from lib.project_mod.config import (
            clear_conv_state,
            ensure_project_state_for_conv,
            resolve_namespaced_path,
        )
    except ImportError:
        print('  SKIP — per-conv APIs not yet implemented')
        return None

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        ws_a = _make_fake_project(tmp_p, 'instance-A')
        ws_b = _make_fake_project(tmp_p, 'instance-B')

        conv_a = 'convA-12345'
        conv_b = 'convB-67890'

        ensure_project_state_for_conv(conv_a, str(ws_a))
        ensure_project_state_for_conv(conv_b, str(ws_b))

        # Both convs can resolve their own root
        base_a, rel_a = resolve_namespaced_path(
            'instance-A:hello.txt', conv_id=conv_a)
        base_b, rel_b = resolve_namespaced_path(
            'instance-B:hello.txt', conv_id=conv_b)

        assert base_a == str(ws_a), f'Conv A resolves wrong: {base_a}'
        assert base_b == str(ws_b), f'Conv B resolves wrong: {base_b}'

        # Cross-resolution should NOT work — conv_a cannot see conv_b's roots
        try:
            resolve_namespaced_path('instance-B:hello.txt', conv_id=conv_a)
            print('  WARN: Conv A could see Conv B root — no isolation')
            return False
        except ValueError:
            pass  # expected

        clear_conv_state(conv_a)
        clear_conv_state(conv_b)
        return True


def test_concurrent_set_project_stress():
    """Stress test: 8 threads each register a different project_path
    and immediately try to resolve their own basename prefix. Counts
    how many get 'Unknown workspace root'."""
    from lib.project_mod import scanner
    from lib.project_mod.tools import _resolve_base

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        scanner.clear_project()
        workspaces = [_make_fake_project(tmp_p, f'task-{i}') for i in range(8)]
        scanner.set_project(str(workspaces[0]))  # seed

        results = {'ok': 0, 'error': 0, 'errors': []}
        barrier = threading.Barrier(len(workspaces))

        def worker(idx, ws):
            barrier.wait()
            # Each thread registers its workspace then immediately
            # resolves its own namespaced path, mimicking the race.
            scanner.set_project(str(ws))
            for _ in range(3):
                try:
                    abs_base, rel = _resolve_base(
                        str(ws), f'task-{idx}:hello.txt')
                    if abs_base == str(ws) and rel == 'hello.txt':
                        results['ok'] += 1
                    else:
                        results['error'] += 1
                        results['errors'].append(
                            f'task-{idx}: bad resolve {abs_base}')
                except ValueError as e:
                    results['error'] += 1
                    results['errors'].append(f'task-{idx}: {e}')
                time.sleep(0.01)

        threads = [threading.Thread(target=worker, args=(i, ws))
                   for i, ws in enumerate(workspaces)]
        for t in threads: t.start()
        for t in threads: t.join()

        print(f'  Stress: ok={results["ok"]} error={results["error"]}')
        for e in results['errors'][:5]:
            print(f'    ERR: {e}')
        return results['error'] == 0


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', choices=[
        'repro', 'resolve_base_basename', 'per_conv', 'stress', 'all'],
        default='all')
    args = ap.parse_args()

    tests = {
        'repro':       ('Reproduce concurrent clobber bug',
                        test_concurrent_set_project_clobbers_other_task),
        'resolve_base_basename':
                      ('After-fix: _resolve_base uses task base_path basename',
                        test_resolve_base_with_task_base_path),
        'per_conv':    ('After-fix: per-conv state isolation',
                        test_per_conv_state_isolation),
        'stress':      ('Stress: 8 concurrent set_project + resolve',
                        test_concurrent_set_project_stress),
    }
    to_run = list(tests) if args.test == 'all' else [args.test]
    print()
    for name in to_run:
        label, fn = tests[name]
        print(f'━━━ {label} ━━━')
        try:
            ok = fn()
            if ok is None:
                print('  [SKIP]\n')
            elif ok:
                print('  PASS\n')
            else:
                print('  FAIL (expected pre-fix — bug reproduced)\n')
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'  CRASH: {e}\n')
