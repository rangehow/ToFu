"""Tests for lib.conversations.project_commit — the contamination-proof commit seam.

The LOAD-BEARING test is ``test_foreign_hunk_file_is_excluded``: a file that
carries BOTH this conversation's recorded edit AND a later foreign (sibling)
hunk must be classified CONTAMINATED and kept OUT of the commit — never
silently staged. Everything else (clean commit, ignored-bundle reject,
no-pathspec index verification) guards the surrounding sequence.

These run against a REAL temporary git repo and a REAL file-history store — no
mocks of git or fh — because the whole point is the byte-identity attribution,
which only means something end-to-end.
"""
import os
import subprocess
import tempfile
import unittest

import pytest

pytestmark = pytest.mark.unit


def _git(cwd, *args):
    p = subprocess.run(['git', *args], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


class ProjectCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tofu-commit-test-')
        self.conv = 'convA'
        _git(self.tmp, 'init', '-q')
        _git(self.tmp, 'config', 'user.email', 't@t')
        _git(self.tmp, 'config', 'user.name', 'T')
        # Seed a committed baseline so HEAD exists.
        self._write('seed.txt', 'seed\n')
        _git(self.tmp, 'add', '-A')
        _git(self.tmp, 'commit', '-qm', 'baseline')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, content):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)

    def _read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding='utf-8') as f:
            return f.read()

    def _record_write(self, rel, content, *, conv=None):
        """Write ``content`` to ``rel`` and record it in file-history as a
        snapshot authored by ``conv`` (simulating a Tofu write by that
        conversation)."""
        import lib.file_history as fh
        self._write(rel, content)
        fh.make_snapshot(self.tmp, task_id='t1', conv_id=conv or self.conv,
                        rel_paths=[rel], summary='edit')

    # ── LOAD-BEARING: file with my hunk + a foreign hunk must be excluded ──
    def test_foreign_hunk_file_is_excluded(self):
        from lib.conversations.project_commit import do_commit, plan_commit
        # convA edits shared.py and records it → this is convA's post-image.
        self._record_write('shared.py', 'def a():\n    return 1\n')
        # A SIBLING conversation then appends its own hunk to the SAME file,
        # WITHOUT convA recording it (foreign, uncommitted, in the working tree).
        self._write('shared.py', 'def a():\n    return 1\n\ndef sibling():\n    return 2\n')
        # convA ALSO cleanly edited its own file mine.py (no foreign touch).
        self._record_write('mine.py', 'print("mine")\n')

        plan = plan_commit(self.tmp, self.conv, files=['shared.py', 'mine.py'])
        clean = set(plan['clean'])
        contaminated = {c['path'] for c in plan['contaminated']}

        self.assertIn('mine.py', clean, 'clean own-file must be committable')
        self.assertIn('shared.py', contaminated,
                      'file with a foreign sibling hunk MUST be flagged contaminated')
        self.assertNotIn('shared.py', clean,
                         'contaminated file MUST NOT be in the clean set')

        res = do_commit(self.tmp, self.conv, 'commit mine only',
                        files=['shared.py', 'mine.py'])
        self.assertTrue(res['ok'], res.get('error'))
        self.assertEqual(sorted(res['committed']), ['mine.py'])
        self.assertIn('shared.py', {e['path'] for e in res['excluded']})

        # The commit must contain ONLY mine.py; the sibling's hunk stays on disk.
        _, files_out, _ = _git(self.tmp, 'show', '--pretty=format:', '--name-only', 'HEAD')
        self.assertEqual(sorted(p for p in files_out.splitlines() if p), ['mine.py'])
        # shared.py's foreign hunk is preserved in the working tree, uncommitted.
        self.assertIn('def sibling()', self._read('shared.py'))
        _, st, _ = _git(self.tmp, 'status', '--porcelain', '--', 'shared.py')
        self.assertTrue(st.strip(), 'shared.py must remain dirty (not swept in)')

    # ── REGRESSION: dotfile candidates must not be mangled by normalization ──
    # The old code used `str(f).lstrip('./')` — a CHARACTER SET strip — so
    # '.gitignore' → 'gitignore' and '.tofu/x' → 'tofu/x'. A mangled path never
    # matches its file-history record or its on-disk file, so a dotfile could
    # NEVER be classified clean → the tool would refuse to commit any dotfile.
    def test_dotfile_candidates_not_mangled(self):
        from lib.conversations.project_commit import plan_commit
        self._record_write('.gitignore', '*.log\n')
        self._record_write('.tofu/skills/note.md', 'skill\n')
        # Pass with a redundant './' prefix too, to prove only that is stripped.
        plan = plan_commit(self.tmp, self.conv,
                          files=['.gitignore', './.tofu/skills/note.md'])
        self.assertIn('.gitignore', plan['clean'],
                      "'.gitignore' must survive normalization and classify clean")
        self.assertIn('.tofu/skills/note.md', plan['clean'],
                      "path under .tofu/ must not be mangled to tofu/")
        # The mangled forms must NOT appear anywhere.
        all_paths = (plan['clean']
                     + [c['path'] for c in plan['contaminated']]
                     + [c['path'] for c in plan['ignored']])
        self.assertNotIn('gitignore', all_paths)
        self.assertNotIn('tofu/skills/note.md', all_paths)

    # ── clean own edits commit fully ──
    def test_clean_files_commit(self):
        from lib.conversations.project_commit import do_commit
        self._record_write('a.py', 'a\n')
        self._record_write('b.py', 'b\n')
        res = do_commit(self.tmp, self.conv, 'commit a and b', files=['a.py', 'b.py'])
        self.assertTrue(res['ok'], res.get('error'))
        self.assertEqual(sorted(res['committed']), ['a.py', 'b.py'])
        self.assertTrue(res['verified'])
        # Working tree clean for these paths afterward.
        _, st, _ = _git(self.tmp, 'status', '--porcelain')
        self.assertNotIn('a.py', st)
        self.assertNotIn('b.py', st)

    # ── generated bundle is refused even if this conv "wrote" it ──
    def test_generated_bundle_excluded(self):
        from lib.conversations.project_commit import plan_commit
        self._record_write('static/js/feature-133a319e.js', 'BUNDLE\n')
        self._record_write('real.py', 'x\n')
        plan = plan_commit(self.tmp, self.conv,
                          files=['static/js/feature-133a319e.js', 'real.py'])
        self.assertIn('real.py', plan['clean'])
        ignored = {c['path'] for c in plan['ignored']}
        self.assertIn('static/js/feature-133a319e.js', ignored)
        self.assertNotIn('static/js/feature-133a319e.js', plan['clean'])

    # ── an unattributed file (no fh record by this conv) is contaminated ──
    def test_unattributed_file_is_contaminated(self):
        from lib.conversations.project_commit import plan_commit
        # A file appears dirty but convA never recorded writing it.
        self._write('stranger.py', 'not mine\n')
        plan = plan_commit(self.tmp, self.conv, files=['stranger.py'])
        self.assertNotIn('stranger.py', plan['clean'])
        self.assertIn('stranger.py', {c['path'] for c in plan['contaminated']})

    # ── another conversation's recorded file is NOT attributed to me ──
    def test_sibling_recorded_file_not_mine(self):
        from lib.conversations.project_commit import plan_commit
        # A sibling conversation records writing sib.py.
        self._record_write('sib.py', 'sibling wrote this\n', conv='convB')
        plan = plan_commit(self.tmp, self.conv, files=['sib.py'])
        self.assertNotIn('sib.py', plan['clean'],
                         "another conv's file must not be attributed to me")

    # ── OPTION A: no files declared → clear error, NEVER a lifetime scan ──
    def test_no_files_returns_declare_error(self):
        from lib.conversations.project_commit import do_commit, plan_commit
        # Even with plenty of this-conv history on disk, an empty files list
        # must NOT derive a candidate set — it returns the declare-your-paths
        # error (the real-tree failure mode: 543/564 false-clean + timeout).
        self._record_write('recorded1.py', 'a\n')
        self._record_write('recorded2.py', 'b\n')
        plan = plan_commit(self.tmp, self.conv)  # no files=
        self.assertFalse(plan['ok'])
        self.assertIn('no files declared', plan['error'])
        self.assertEqual(plan['candidates'], [])
        self.assertEqual(plan['clean'], [])
        # do_commit must also refuse, committing nothing.
        res = do_commit(self.tmp, self.conv, 'msg')  # no files=
        self.assertFalse(res['ok'])
        self.assertIn('no files declared', res['error'])

    # ── LIVE-TREE SHAPE: a large multi-conv log must classify correctly AND
    #    fast (single-pass). This is the blind spot the temp-repo tests missed:
    #    the real store is 142 MB / 2000+ snapshots and the OLD per-file scan
    #    (O(files × log)) timed out at >200s. ──
    def test_scale_multiconv_log_single_pass(self):
        import time

        import lib.file_history as fh
        from lib.conversations.project_commit import plan_commit
        # My genuinely-own file + a file a sibling later diverged.
        self._record_write('mine_real.py', 'MINE v1\n')
        self._record_write('coedited.py', 'shared v1\n')  # I recorded it...
        # Simulate a busy shared tree: MANY snapshots across MY conv (repeated
        # turns), sibling convs, and empty/None convId pollution — like the
        # real 606-file tree where one convId accrues a lifetime of paths.
        for i in range(400):
            self._record_write(f'lifetime_{i}.py', f'v{i}\n')  # my lifetime sprawl
        for i in range(200):
            self._record_write(f'sib_{i}.py', f's{i}\n', conv='sibling-conv')
        # A SIBLING diverges coedited.py in the working tree AFTER my record.
        self._write('coedited.py', 'shared v1\n\n# sibling hunk\n')
        # Pollution: empty + None convId snapshots (real store has these).
        fh.make_snapshot(self.tmp, task_id='t', conv_id='',
                        rel_paths=['mine_real.py'], summary='noise')
        # DECLARE only the two files I actually care about this turn.
        t0 = time.time()
        plan = plan_commit(self.tmp, self.conv,
                          files=['mine_real.py', 'coedited.py'])
        elapsed = time.time() - t0
        # Correctness: my file clean, the sibling-diverged file contaminated.
        self.assertIn('mine_real.py', plan['clean'])
        self.assertIn('coedited.py', {c['path'] for c in plan['contaminated']})
        # Candidate set is EXACTLY what I declared — never the 600+ lifetime set.
        self.assertEqual(sorted(plan['candidates']), ['coedited.py', 'mine_real.py'])
        # Speed: single-pass over the whole log, not per-file. A 600+-snapshot
        # store must plan in well under the old timeout. Generous bound to stay
        # robust on slow CI/FUSE while still catching an O(files × log) regression.
        self.assertLess(elapsed, 20.0,
                        f'plan_commit took {elapsed:.1f}s — likely re-scanning '
                        f'the log per file (O(files × log) regression)')

    # ── empty message refused; nothing-clean refused ──
    def test_guards(self):
        from lib.conversations.project_commit import do_commit
        self._record_write('c.py', 'c\n')
        self.assertFalse(do_commit(self.tmp, self.conv, '', files=['c.py'])['ok'])
        # All candidates contaminated → refuse.
        self._write('foreign.py', 'foreign\n')
        res = do_commit(self.tmp, self.conv, 'msg', files=['foreign.py'])
        self.assertFalse(res['ok'])
        self.assertIn('nothing clean', res['error'])


class ConvIdRoundTripTest(unittest.TestCase):
    """The whole tool rests on ONE assumption: the convId that the WRITE path
    (commit_round → make_snapshot) stamps on a snapshot equals the convId the
    READ path (misc._handle_board_tool → execute_commit_tool) passes as
    current_conv_id. If they diverge, the tool classifies the user's OWN work
    as contaminated (unattributed) and commits nothing — the exact "failure to
    commit" we're ending. Both read task['convId']; this proves the round-trip
    end-to-end AND guards the two call sites against future key drift."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tofu-commit-rt-')
        _git(self.tmp, 'init', '-q')
        _git(self.tmp, 'config', 'user.email', 't@t')
        _git(self.tmp, 'config', 'user.name', 'T')
        with open(os.path.join(self.tmp, 'seed.txt'), 'w') as f:
            f.write('seed\n')
        _git(self.tmp, 'add', '-A'); _git(self.tmp, 'commit', '-qm', 'baseline')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_side_convid_reads_back_on_read_side(self):
        import lib.file_history as fh
        from lib.conversations.project_commit import plan_commit
        # One task dict, exactly as the orchestrator builds it.
        task = {'id': 'task-1', 'convId': 'conv-XYZ'}
        # WRITE side — mirror commit_round.py:324-328 verbatim (same key).
        with open(os.path.join(self.tmp, 'edited.py'), 'w') as f:
            f.write('x = 1\n')
        fh.make_snapshot(self.tmp, task_id=task['id'],
                        conv_id=task.get('convId'), rel_paths=['edited.py'])
        # READ side — mirror misc.py:442 verbatim (same key), feed plan_commit.
        current_conv_id = task.get('convId', '')
        plan = plan_commit(self.tmp, current_conv_id, files=['edited.py'])
        self.assertIn('edited.py', plan['clean'],
                      'a file written under task["convId"] must read back as '
                      'clean under the same task["convId"]')
        # A DIFFERENT convId must NOT see it as its own (attribution is real).
        other = plan_commit(self.tmp, 'conv-OTHER', files=['edited.py'])
        self.assertNotIn('edited.py', other['clean'])

    def test_both_call_sites_read_same_convid_key(self):
        # Source-drift guard: if either site is changed to read a different key,
        # this fails — cheap tripwire for the silent "own work looks foreign" bug.
        import inspect

        from lib.tasks_pkg import commit_round
        from lib.tasks_pkg.handlers import misc
        write_src = inspect.getsource(commit_round._run_commit_round_async)
        read_src = inspect.getsource(misc._handle_board_tool)
        self.assertIn("conv_id=task.get('convId')", write_src,
                      'write path must stamp snapshots with task["convId"]')
        self.assertIn("task.get('convId'", read_src,
                      'read path must pass task["convId"] as current_conv_id')


class ProjectCommitRegistrationTest(unittest.TestCase):
    """The tool must be reachable end-to-end, not just a core function: it has
    to be in the model-facing schema (BOARD_TOOLS), in the dispatch gate set
    (BOARD_TOOL_NAMES), and routed by execute_board_tool — else it's a phantom
    the agent can never call."""

    def test_in_schema_and_name_set(self):
        from lib.tools import BOARD_TOOLS, BOARD_TOOL_NAMES
        names = [t['function']['name'] for t in BOARD_TOOLS]
        self.assertIn('project_commit', names)
        self.assertIn('project_commit', BOARD_TOOL_NAMES)
        spec = [t for t in BOARD_TOOLS if t['function']['name'] == 'project_commit'][0]
        props = spec['function']['parameters']['properties']
        for p in ('message', 'files', 'dry_run'):
            self.assertIn(p, props)
        # files is REQUIRED (Option A: no lifetime-derived default).
        self.assertIn('files', spec['function']['parameters'].get('required', []))
        self.assertNotIn('all_tracked_dirty', props)

    def test_registry_routes_to_board_handler(self):
        from lib.tasks_pkg.executor import tool_registry
        from lib.tasks_pkg.handlers.misc import _handle_board_tool
        self.assertIs(tool_registry.lookup('project_commit', {}), _handle_board_tool)

    def test_project_mode_gate(self):
        # No project → refused with the project-mode message (same gate as board).
        from lib.conversations.project_board import execute_board_tool
        out = execute_board_tool('project_commit', {'dry_run': True, 'files': ['x']},
                                 current_conv_id='c', project_path='')
        self.assertIn('project mode', out)


if __name__ == '__main__':
    unittest.main()
