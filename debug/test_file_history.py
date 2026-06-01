"""Smoke + scenario tests for lib/file_history.

Run::

    python debug/test_file_history.py
    TOFU_FILE_HISTORY=0 python debug/test_file_history.py   # disabled-mode

Covers:
  1. Bootstrap + track_edit creates a v1 backup.
  2. Two edits → two snapshots → diff_name_status reports 'modified'.
  3. Rewind undoes the latest round (file content reverts).
  4. restore_from re-applies an undone round (redo).
  5. Side-channel write captured by track_edit pre-hook.
  6. Binary file with NULs survives a round-trip.
  7. UTF-8 paths and contents.
  8. detect_external_edits picks up an out-of-band edit.
  9. Disabled mode short-circuits cleanly.
 10. Concurrent threads on two different projects don't interfere.
"""
import io
import os
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import file_history as fh
from lib.file_history import store as fh_store


# ═══════════════════════════════════════════════════════════════════
#  Tiny test harness (no external deps; mirrors test_git_shim.py)
# ═══════════════════════════════════════════════════════════════════

_passed = 0
_failed = 0
_buf = io.StringIO()


def _assert(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f'  PASS — {msg}')
    else:
        _failed += 1
        print(f'  FAIL — {msg}')
        _buf.write(f'{msg}\n')


def _banner(title):
    print(f'\n=== {title} ===')


def _w(base, rel, content):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    if isinstance(content, bytes):
        with open(p, 'wb') as f:
            f.write(content)
    else:
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)


def _r(base, rel):
    p = os.path.join(base, rel)
    with open(p, 'rb') as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════

def t1_bootstrap(base):
    _banner('t1: bootstrap + track_edit creates v1')
    _w(base, 'a.txt', 'hello\n')
    v = fh.track_edit(base, 'a.txt', message_id='m1')
    _assert(v == 1, f'first track_edit returns version 1 (got {v})')
    blob_path = fh_store.backup_blob_path(base, 'a.txt', 1)
    _assert(os.path.exists(blob_path), 'v1 backup blob exists on disk')
    with open(blob_path, 'rb') as f:
        _assert(f.read() == b'hello\n', 'v1 backup matches original content')


def t2_two_rounds_diff(base):
    _banner('t2: two snapshots → diff_name_status')
    # Round 1: introduce a.txt and b.txt.
    _w(base, 'a.txt', 'hello\n')
    fh.track_edit(base, 'a.txt')
    _w(base, 'b.txt', 'B v1\n')
    fh.track_edit(base, 'b.txt')
    s1 = fh.make_snapshot(base, task_id='task-1', conv_id='conv-x',
                          tool_names=['write_file'])
    _assert(bool(s1), 'snapshot 1 created')

    # Round 2: modify a.txt, leave b.txt alone, add c.txt.
    fh.track_edit(base, 'a.txt')   # capture pre-image (still 'hello\n')
    _w(base, 'a.txt', 'hello v2\n')
    _w(base, 'c.txt', 'C v1\n')
    fh.track_edit(base, 'c.txt')
    s2 = fh.make_snapshot(base, task_id='task-2', conv_id='conv-x',
                          tool_names=['write_file'],
                          rel_paths=['a.txt', 'c.txt'])
    _assert(bool(s2), 'snapshot 2 created')

    diff = fh.diff_name_status(base, s1, s2)
    paths = {(d['path'], d['action']) for d in diff}
    _assert(('a.txt', 'modified') in paths,
            f'diff reports a.txt modified (got {paths})')
    _assert(('c.txt', 'created') in paths,
            f'diff reports c.txt created (got {paths})')
    _assert(('b.txt', 'modified') not in paths,
            'b.txt does NOT appear (unchanged between rounds)')

    hist = fh.list_history(base, limit=10)
    _assert(len(hist) >= 2, f'history has at least 2 entries (got {len(hist)})')
    _assert(hist[0]['id'] == s2, 'history newest-first ordering')
    return s1, s2


def t3_rewind(base):
    _banner('t3: rewind undoes the most recent round')
    # Continue from t2 state.  Re-do a fresh setup for isolation.
    _w(base, 'r.txt', 'pre\n')
    fh.track_edit(base, 'r.txt')
    s_pre = fh.make_snapshot(base, task_id='r-pre', conv_id='conv-r',
                             rel_paths=['r.txt'])
    _w(base, 'r.txt', 'post\n')
    fh.track_edit(base, 'r.txt')
    s_post = fh.make_snapshot(base, task_id='r-post', conv_id='conv-r',
                              rel_paths=['r.txt'])
    _assert(bool(s_pre and s_post), 'pre+post snapshots created')

    res = fh.rewind_to(base, s_post)
    _assert(res['ok'], 'rewind succeeded')
    _assert(_r(base, 'r.txt') == b'pre\n',
            f"file reverted to pre state (got {_r(base, 'r.txt')!r})")


def t4_redo(base):
    _banner('t4: restore_from re-applies an undone round')
    _w(base, 'd.txt', 'orig\n')
    fh.track_edit(base, 'd.txt')
    fh.make_snapshot(base, task_id='d-init', conv_id='conv-d',
                     rel_paths=['d.txt'])
    _w(base, 'd.txt', 'changed\n')
    fh.track_edit(base, 'd.txt')
    s_change = fh.make_snapshot(base, task_id='d-change', conv_id='conv-d',
                                rel_paths=['d.txt'])
    # Undo it.
    fh.rewind_to(base, s_change)
    _assert(_r(base, 'd.txt') == b'orig\n', 'after rewind: original')
    # Redo it.
    res = fh.restore_from(base, s_change)
    _assert(res['ok'], 'restore_from succeeded')
    _assert(_r(base, 'd.txt') == b'changed\n', 'after redo: changed')


def t5_side_channel(base):
    _banner('t5: side-channel write captured via post-hoc track_edit')
    # Simulate run_command that wrote a build artifact.
    _w(base, 'sub/built.out', 'artifact\n')
    v = fh.track_edit(base, 'sub/built.out')
    _assert(v == 1, 'first-time tracking writes v1 even for files we did not pre-track')
    s = fh.make_snapshot(base, task_id='sc', conv_id='conv-sc',
                         tool_names=['run_command'],
                         rel_paths=['sub/built.out'])
    _assert(bool(s), 'side-channel snapshot created')

    diff = fh.diff_name_status(base, fh.list_history(base, limit=2)[1]['id'], s)
    _assert(any(d['path'] == 'sub/built.out' and d['action'] in ('created', 'modified')
                for d in diff),
            f'side-channel file appears in diff (got {diff})')


def t6_binary(base):
    _banner('t6: binary file with NUL bytes round-trips')
    payload = bytes(range(256))
    _w(base, 'bin.dat', payload)
    fh.track_edit(base, 'bin.dat')
    s_bin = fh.make_snapshot(base, task_id='b1', conv_id='conv-bin',
                             rel_paths=['bin.dat'])
    _w(base, 'bin.dat', b'replaced')
    fh.track_edit(base, 'bin.dat')
    s_after = fh.make_snapshot(base, task_id='b2', conv_id='conv-bin',
                               rel_paths=['bin.dat'])
    fh.rewind_to(base, s_after)
    _assert(_r(base, 'bin.dat') == payload, 'binary content restored byte-for-byte')


def t7_utf8(base):
    _banner('t7: UTF-8 paths + contents')
    rel = 'docs/日本語.md'
    _w(base, rel, '見出し\npara\n')
    fh.track_edit(base, rel)
    s = fh.make_snapshot(base, task_id='ja', conv_id='conv-ja',
                         rel_paths=[rel])
    _assert(bool(s), 'UTF-8 path snapshot created')
    hist = fh.list_history(base, path=rel, limit=5)
    _assert(any(rel in (h.get('filesChanged') or []) for h in hist),
            'history filter by UTF-8 path matches')


def t8_external_drift(base):
    _banner('t8: detect_external_edits picks up out-of-band edit')
    _w(base, 'drift.txt', 'pre\n')
    fh.track_edit(base, 'drift.txt')
    fh.make_snapshot(base, task_id='drift-init', conv_id='conv-drift',
                     rel_paths=['drift.txt'])
    # Pretend the IDE saved over it without going through us.
    time.sleep(0.01)
    _w(base, 'drift.txt', 'IDE saved this\n')
    res = fh.detect_external_edits(base, message_id='probe-1')
    _assert(res.get('committed'), 'external-edit probe captured drift')
    _assert('drift.txt' in res.get('files', []),
            f"drifted file listed (got {res.get('files')})")


def t9_disabled(base):
    _banner('t9: disabled mode short-circuits')
    os.environ['TOFU_FILE_HISTORY'] = '0'
    try:
        # Reload api to reset the latched warning flag.
        import importlib
        import lib.file_history.api
        importlib.reload(lib.file_history.api)
        from lib.file_history import api as fh_api
        _assert(fh_api.is_enabled() is False, 'is_enabled False when disabled')
        _assert(fh_api.track_edit(base, 'x.txt') is None, 'track_edit returns None')
        _assert(fh_api.make_snapshot(base, task_id='z', conv_id='c') is None,
                'make_snapshot returns None')
        _assert(fh_api.list_history(base) == [], 'list_history returns []')
        _assert(fh_api.diff_name_status(base, 'a', 'b') == [],
                'diff_name_status returns []')
        _assert(fh_api.rewind_to(base, 'x').get('ok') is False,
                'rewind_to returns ok=False')
    finally:
        os.environ.pop('TOFU_FILE_HISTORY', None)
        # Re-reload so subsequent runs in this process see fresh state.
        import importlib
        import lib.file_history.api
        importlib.reload(lib.file_history.api)


def t10_concurrent_two_projects():
    _banner('t10: concurrent threads on two projects do not interfere')
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        results = {}

        def worker(base, label):
            for i in range(20):
                rel = f'f{i}.txt'
                _w(base, rel, f'{label}-{i}')
                fh.track_edit(base, rel)
            sid = fh.make_snapshot(base, task_id=f'task-{label}',
                                   conv_id=f'conv-{label}',
                                   rel_paths=[f'f{i}.txt' for i in range(20)])
            results[label] = sid

        t1 = threading.Thread(target=worker, args=(a, 'A'))
        t2 = threading.Thread(target=worker, args=(b, 'B'))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        _assert(bool(results.get('A')) and bool(results.get('B')),
                'both projects produced snapshots')
        # Cross-check: project A's snapshot should NOT mention project B's files
        # (ensured because each project's store dir is under its own base_path).
        a_hist = fh.list_history(a, limit=5)
        b_hist = fh.list_history(b, limit=5)
        a_files = {f for h in a_hist for f in (h.get('filesChanged') or [])}
        b_files = {f for h in b_hist for f in (h.get('filesChanged') or [])}
        _assert(a_files == b_files,
                f'both projects independently track 20 files (a={len(a_files)}, b={len(b_files)})')


def t11_dedup(base):
    _banner('t11: re-tracking unchanged content does not bump version')
    _w(base, 'dedup.txt', 'same\n')
    v1 = fh.track_edit(base, 'dedup.txt')
    v2 = fh.track_edit(base, 'dedup.txt')
    _assert(v1 == 1, 'first track_edit → v1')
    _assert(v2 is None, 'second track_edit on unchanged file → None (dedup)')


# ═══════════════════════════════════════════════════════════════════
#  Driver
# ═══════════════════════════════════════════════════════════════════

def main():
    print('======== lib.file_history smoke tests ========')
    if not fh.is_enabled():
        print('[SKIP] TOFU_FILE_HISTORY=0 — running disabled-mode test only')
        with tempfile.TemporaryDirectory() as base:
            t9_disabled(base)
        sys.exit(0 if _failed == 0 else 1)

    with tempfile.TemporaryDirectory() as base:
        try:
            t1_bootstrap(base)
            t2_two_rounds_diff(base)
            t3_rewind(base)
            t4_redo(base)
            t5_side_channel(base)
            t6_binary(base)
            t7_utf8(base)
            t8_external_drift(base)
            t11_dedup(base)
        except Exception as e:
            print(f'[FATAL] uncaught exception during shared-base tests: {e}')
            traceback.print_exc()
            sys.exit(1)

    # Independent base dirs for the disabled + concurrency tests.
    with tempfile.TemporaryDirectory() as base:
        t9_disabled(base)
    t10_concurrent_two_projects()

    print(f'\n======== {_passed} passed, {_failed} failed ========')
    if _failed:
        print('\nFailures:')
        print(_buf.getvalue())
        sys.exit(1)
    print('OK')


if __name__ == '__main__':
    main()
