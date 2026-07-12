"""tests/test_worktree_roots_isolation.py — FUSE validation V6 (design §3.3).

Worktree isolation step 2: the process-global ``_roots`` / ``_state['path']``
fall-through in ``resolve_namespaced_path`` / ``get_conv_roots`` is a latent
cross-conv leak — under per-conversation git worktrees, "the global tree" IS
either the shared primary checkout or ANOTHER conversation's worktree, so a
conv-scoped resolution that falls through to it can resolve conv A's tool into
conv B's worktree.

These pin the gated fix:
  * TOFU_WORKTREE_ISOLATION OFF (default) → BYTE-IDENTICAL to today: a conv with
    no registry still falls through to the global registry / primary (the
    2026-05 behavior every existing test relies on).
  * TOFU_WORKTREE_ISOLATION on → a conv-scoped call NEVER sees the global
    registry/primary: it fails closed (UnknownWorkspaceRootError) rather than
    leaking into the shared/global tree. A conv WITH its own registry keeps
    resolving strictly within it (unchanged), and a global (no-conv_id) call is
    unaffected (the UI/legacy path).
"""
from __future__ import annotations

import os
import unittest

import pytest

from lib.project_mod import config as cfg
from lib.project_mod.config import UnknownWorkspaceRootError, resolve_namespaced_path

pytestmark = pytest.mark.unit


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='wt-v6-')
        self.glob = os.path.join(self._tmp, 'global_primary')
        self.convA = os.path.join(self._tmp, 'convA_tree')
        self.convB = os.path.join(self._tmp, 'convB_tree')
        for d in (self.glob, self.convA, self.convB):
            os.makedirs(d)
        # A global primary registered as if by the UI.
        cfg._state['path'] = os.path.abspath(self.glob)
        cfg._roots.clear()
        cfg._roots[os.path.basename(self.glob)] = cfg._make_root_state(
            os.path.abspath(self.glob))

    def tearDown(self):
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        cfg._roots.clear()
        cfg._state['path'] = None
        os.environ.pop('TOFU_WORKTREE_ISOLATION', None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class OffModeByteIdentical(_Base):
    """Default (inproc/OFF): the legacy global fall-through is intact."""

    def setUp(self):
        super().setUp()
        os.environ.pop('TOFU_WORKTREE_ISOLATION', None)

    def test_unknown_conv_falls_through_to_global_namespaced(self):
        # conv 'ghost' has NO registry → resolves via the global _roots.
        base, rel = resolve_namespaced_path(
            f'{os.path.basename(self.glob)}:x.py', conv_id='ghost')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self.glob))
        self.assertEqual(rel, 'x.py')

    def test_unknown_conv_falls_through_to_global_primary(self):
        # No-colon path with an unregistered conv → global primary.
        base, rel = resolve_namespaced_path('x.py', conv_id='ghost')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self.glob))

    def test_get_conv_roots_unknown_conv_sees_global(self):
        view = cfg.get_conv_roots('ghost')
        self.assertIn(os.path.basename(self.glob), view)


class OnModeFailsClosed(_Base):
    """Isolation on: a conv-scoped call never leaks into the global tree."""

    def setUp(self):
        super().setUp()
        os.environ['TOFU_WORKTREE_ISOLATION'] = 'on'

    def test_unknown_conv_namespaced_fails_closed(self):
        with self.assertRaises(UnknownWorkspaceRootError):
            resolve_namespaced_path(
                f'{os.path.basename(self.glob)}:x.py', conv_id='ghost')

    def test_unknown_conv_primary_fails_closed(self):
        with self.assertRaises(UnknownWorkspaceRootError):
            resolve_namespaced_path('x.py', conv_id='ghost')

    def test_get_conv_roots_unknown_conv_is_empty(self):
        self.assertEqual(cfg.get_conv_roots('ghost'), {})

    def test_conv_with_registry_resolves_within_it(self):
        cfg.set_conv_roots('convA', self.convA)
        base, rel = resolve_namespaced_path(
            f'{os.path.basename(self.convA)}:foo.py', conv_id='convA')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self.convA))
        self.assertEqual(rel, 'foo.py')

    def test_conv_cannot_resolve_into_another_convs_root(self):
        # convA and convB each own their own tree; A must never resolve B's root.
        cfg.set_conv_roots('convA', self.convA)
        cfg.set_conv_roots('convB', self.convB)
        with self.assertRaises(UnknownWorkspaceRootError):
            resolve_namespaced_path(
                f'{os.path.basename(self.convB)}:secret.py', conv_id='convA')

    def test_global_call_still_works(self):
        # A UI/legacy call with NO conv_id still uses the global registry.
        base, rel = resolve_namespaced_path(f'{os.path.basename(self.glob)}:x.py')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self.glob))


class SelfHealGuard(_Base):
    """The tools.py self-heal must be conv-keyed under isolation (V6 guard)."""

    def setUp(self):
        super().setUp()
        os.environ['TOFU_WORKTREE_ISOLATION'] = 'on'

    def test_self_heal_refused_when_base_not_a_conv_root(self):
        from lib.project_mod.tools import _resolve_base
        # convA owns convA_tree; a tool call carries base_path=convB_tree (a
        # stale/mismatched base) with a 'convB_tree:...' spec. Under isolation
        # the self-heal must REFUSE (base is not a registered root of convA).
        cfg.set_conv_roots('convA', self.convA)
        with self.assertRaises(UnknownWorkspaceRootError):
            _resolve_base(self.convB, f'{os.path.basename(self.convB)}:x.py',
                          conv_id='convA')

    def test_self_heal_allowed_when_base_is_a_conv_root(self):
        from lib.project_mod.tools import _resolve_base
        # base_path IS convA's registered root → heal is legitimate.
        cfg.set_conv_roots('convA', self.convA)
        base, rel = _resolve_base(
            self.convA, f'{os.path.basename(self.convA)}:x.py', conv_id='convA')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self.convA))


if __name__ == '__main__':
    unittest.main()
