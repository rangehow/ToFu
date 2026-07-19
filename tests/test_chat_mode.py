"""tests/test_chat_mode.py — Three-tier chat mode (air/pro/studio).

Pins the backend half of the single-source-of-truth contract:

  * ``chat_mode.apply_chat_mode`` expands a declared tier into atomic flags,
    tier defaults OVERRIDE explicit flags, absent tier = pass-through.
  * The ``air`` lean tier drops the always-on capability tools
    (memory / todo / scheduler) so the leanest tier ships only the base
    search/fetch/read tools — the "cleaner than ChatGPT" goal.
  * ``pro`` keeps the full default tool set (memory/todo/scheduler on).
  * ``studio`` ⟺ a project is attached (project tools present).

The FE↔BE table equality is guarded separately by
``tests/test_chat_mode_parity.py``.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.chat_mode import (
    CHAT_MODES,
    apply_chat_mode,
    chat_mode_defaults,
    is_lean_mode,
    normalize_chat_mode,
)
from lib.tasks_pkg.model_config import _assemble_tool_list


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _assemble(cfg, **kw):
    """Assemble via the public shim with sensible defaults for a chat turn.

    Mirrors the production path: the resolver expands the tier
    (``apply_chat_mode``) into atomic flags BEFORE assembling, so the derived
    ``code_exec_enabled`` etc. flow through exactly as ``_resolve_model_config``
    would produce them.
    """
    cfg = apply_chat_mode(cfg)
    base = dict(
        project_path=cfg.get('projectPath', ''),
        project_enabled=bool(cfg.get('projectPath')),
        task_id='t-mode', search_mode=cfg.get('searchMode', 'multi'),
        search_enabled=cfg.get('searchMode', 'multi') in ('single', 'multi'),
        fetch_enabled=cfg.get('fetchEnabled', True),
        code_exec_enabled=cfg.get('codeExecEnabled', False),
        browser_enabled=False, desktop_enabled=False, swarm_enabled=False,
        messages=[],
    )
    base.update(kw)
    return _assemble_tool_list(cfg, **base)


class TestNormalize(unittest.TestCase):
    def test_known_modes(self):
        for m in CHAT_MODES:
            self.assertEqual(normalize_chat_mode({'chatMode': m}), m)

    def test_case_and_whitespace(self):
        self.assertEqual(normalize_chat_mode({'chatMode': ' Air '}), 'air')

    def test_absent_and_invalid_are_none(self):
        self.assertIsNone(normalize_chat_mode({}))
        self.assertIsNone(normalize_chat_mode({'chatMode': 'turbo'}))
        self.assertIsNone(normalize_chat_mode(None))


class TestApply(unittest.TestCase):
    def test_absent_mode_is_passthrough(self):
        cfg = {'searchMode': 'off', 'codeExecEnabled': True}
        self.assertEqual(apply_chat_mode(cfg), cfg)

    def test_tier_defaults_override_explicit_flags(self):
        # Declaring air must FORCE codeExec/memory off even if the caller sent
        # them on — the tier is the higher-level intent.
        out = apply_chat_mode({'chatMode': 'air', 'codeExecEnabled': True,
                               'memoryEnabled': True})
        self.assertFalse(out['codeExecEnabled'])
        self.assertFalse(out['memoryEnabled'])
        self.assertEqual(out['searchMode'], 'multi')

    def test_pro_enables_code_exec(self):
        out = apply_chat_mode({'chatMode': 'pro'})
        self.assertTrue(out['codeExecEnabled'])
        self.assertTrue(out['memoryEnabled'])

    def test_studio_leaves_code_exec_alone(self):
        # studio does NOT pin codeExec (run_command supersedes it downstream).
        out = apply_chat_mode({'chatMode': 'studio'})
        self.assertNotIn('codeExecEnabled', chat_mode_defaults('studio'))
        self.assertTrue(out['memoryEnabled'])

    def test_input_not_mutated(self):
        cfg = {'chatMode': 'air'}
        apply_chat_mode(cfg)
        self.assertEqual(cfg, {'chatMode': 'air'})


class TestLeanGate(unittest.TestCase):
    def test_is_lean_only_air(self):
        self.assertTrue(is_lean_mode('air'))
        self.assertFalse(is_lean_mode('pro'))
        self.assertFalse(is_lean_mode('studio'))
        self.assertFalse(is_lean_mode(None))

    def test_air_drops_memory_todo_scheduler(self):
        tl, has_real, _ = _assemble({'chatMode': 'air'})
        names = _names(tl)
        # Base tools still present.
        self.assertIn('web_search', names)
        self.assertIn('fetch_url', names)
        self.assertIn('read_files', names)
        self.assertIn('inspect_image', names)
        self.assertTrue(has_real)
        # Lean drops the always-on capability tools.
        self.assertNotIn('create_memory', names)
        self.assertNotIn('todo_write', names)
        self.assertNotIn('schedule_create', names)
        # No project / code-exec tools in air.
        self.assertNotIn('run_command', names)
        self.assertNotIn('code_exec', names)

    def test_air_is_a_small_set(self):
        # The whole point: air is a handful of tools, not ~15. Guard the
        # order-of-magnitude so a future always-on tool can't silently
        # re-inflate the lean tier (MCP may add a few if a bridge is up in
        # some envs, so assert a generous ceiling rather than exact count).
        tl, _, _ = _assemble({'chatMode': 'air', 'mcpEnabled': False})
        self.assertLessEqual(len(_names(tl)), 6)

    def test_pro_keeps_default_capability_tools(self):
        tl, _, _ = _assemble({'chatMode': 'pro', 'mcpEnabled': False})
        names = _names(tl)
        self.assertIn('create_memory', names)
        self.assertIn('todo_write', names)
        self.assertIn('schedule_create', names)
        # pro enables code exec (no project) → the standalone code-exec tool,
        # whose function name is 'run_command' (CODE_EXEC_TOOL is a copy of
        # PROJECT_TOOL_RUN_COMMAND). The project-ONLY tools stay absent.
        self.assertIn('run_command', names)
        self.assertNotIn('grep_search', names)
        self.assertNotIn('write_file', names)

    def test_studio_has_project_tools_not_code_exec(self):
        tl, _, _ = _assemble({'chatMode': 'studio', 'projectPath': '/tmp/x',
                              'mcpEnabled': False})
        names = _names(tl)
        # Project family present; run_command supersedes code_exec.
        self.assertIn('run_command', names)
        self.assertIn('grep_search', names)
        self.assertNotIn('code_exec', names)
        # Everyday capability tools still on in studio.
        self.assertIn('create_memory', names)

    def test_no_chatmode_is_unchanged_legacy(self):
        # A legacy caller with no chatMode keeps memory/todo/scheduler
        # (has_base_tools path) — proves the gate is opt-in via air only.
        tl, _, _ = _assemble({'mcpEnabled': False})
        names = _names(tl)
        self.assertIn('create_memory', names)
        self.assertIn('todo_write', names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
