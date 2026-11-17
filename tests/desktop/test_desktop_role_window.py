"""tests/desktop/test_desktop_role_window.py — the startup role window's pure core.

The desktop app (the one Tofu node artifact) says at startup what this
machine is and keeps its node controls out of tray-only menus. Contract:
docs/modules/remote_execution.md ("Nodes and pairing").

The tk renderer cannot run on headless CI, so every FACT the window shows
comes from the pure builder (role_state_full / server_rows) and the startup
gate (should_show_at_startup):

* **Role sentence** in both languages.
* **Joined servers** — every attachment is listed with its live link
  verdict; a revoked or unreachable server is visible, the local link is not
  listed as a joined server.
* **Startup gate** — absent config key shows the window; an explicit
  "don't show" persists and is honored.
* **Wiring ratchets** — the launcher imports role_window, shows it at
  startup, wires join/leave/diagnostics, and the tray has the
  "Control panel…" re-entry.
"""

import ast
import os
import sys
import unittest
from unittest import mock

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

pytestmark = pytest.mark.unit


def _rw():
    import desktop.role_window as rw
    return rw


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
        return f.read()


class FullRoleStateTest(unittest.TestCase):

    def test_role_sentence_declares_server_both_languages(self):
        rw = _rw()
        st = rw.role_state_full(15000, {}, (), lang='zh')
        self.assertIn('服务器', st['role'])
        st = rw.role_state_full(15000, {}, (), lang='en')
        self.assertIn('server', st['role'].lower())

    def test_server_url_uses_the_actual_port(self):
        rw = _rw()
        st = rw.role_state_full(14963, {}, (), lang='en')
        self.assertEqual(st['server_url'], 'http://127.0.0.1:14963')

    def test_joined_servers_are_listed_with_their_link_verdict(self):
        """A server this node joined is never invisible: revoked and
        unreachable links render as errors; the local link is not a
        joined server."""
        rw = _rw()
        links = [
            {'id': 'local', 'label': 'http://127.0.0.1:15000', 'local': True,
             'state': 'active', 'enabled': True, 'last_status': {'state': 'ok'}},
            {'id': 'aaaaaaaaaaaa', 'label': 'https://lab/proxy/15000',
             'local': False, 'state': 'active', 'enabled': True,
             'last_status': {'state': 'ok'}},
            {'id': 'bbbbbbbbbbbb', 'label': 'http://nas:15000', 'local': False,
             'state': 'revoked', 'enabled': False, 'last_status': {}},
            {'id': 'cccccccccccc', 'label': 'http://box:15000', 'local': False,
             'state': 'active', 'enabled': True,
             'last_status': {'state': 'unreachable'}},
        ]
        st = rw.role_state_full(15000, {}, links, lang='en')
        rows = {row['id']: row for row in st['servers']}
        self.assertEqual(set(rows), {'aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'cccccccccccc'})
        self.assertEqual(rows['aaaaaaaaaaaa']['tone'], 'ok')
        self.assertEqual(rows['bbbbbbbbbbbb']['tone'], 'error')
        self.assertIn('pairing link', rows['bbbbbbbbbbbb']['text'])
        self.assertEqual(rows['cccccccccccc']['tone'], 'error')
        self.assertIn('http://nas:15000', rows['bbbbbbbbbbbb']['text'])
        self.assertEqual(rw.role_state_full(15000, {}, (), lang='en')['servers'], [])

    def test_autostart_none_means_unsupported(self):
        rw = _rw()
        self.assertIsNone(rw.role_state_full(15000, {}, (), lang='en')['autostart'])
        self.assertTrue(rw.role_state_full(15000, {}, (), lang='en',
                                           autostart=True)['autostart'])

    def test_cc_facts_pass_through(self):
        rw = _rw()
        cc = {'enabled': True, 'perms': {'allow_write': True,
                                         'allow_exec': False}}
        st = rw.role_state_full(15000, cc, (), lang='en')
        self.assertTrue(st['cc_enabled'])
        self.assertEqual(st['perms'], {'allow_write': True,
                                       'allow_exec': False})
        self.assertEqual(st['tiers'],
                         ['allow_write', 'allow_exec', 'allow_gui',
                          'allow_egress', 'allow_codex_login'])

    def test_show_flag_defaults_true_in_state(self):
        rw = _rw()
        self.assertTrue(rw.role_state_full(15000, {}, (), lang='en')
                        ['show_at_startup'])
        self.assertFalse(rw.role_state_full(15000, {}, (), show_flag=False,
                                            lang='en')['show_at_startup'])

    def test_stopped_server_is_an_error_with_its_exit_code(self):
        """2026-11-10: the server child died at boot while this panel still
        looked healthy and every click silently failed."""
        rw = _rw()
        st = rw.role_state_full(15000, {}, (), lang='zh',
                                server_state='stopped', server_exit_code=1)
        self.assertEqual(st['server_tone'], 'error')
        self.assertIn('退出码 1', st['server_detail'])
        running = rw.role_state_full(15000, {}, (), lang='en')
        self.assertEqual((running['server_tone'], running['server_detail']),
                         ('ok', ''))
        starting = rw.role_state_full(15000, {}, (), lang='en',
                                      server_state='starting')
        self.assertEqual(starting['server_tone'], 'muted')

    def test_failed_enable_shows_the_reason(self):
        rw = _rw()
        st = rw.role_state_full(15000, {'enabled': False,
                                        'error': 'boom'}, (), lang='en')
        self.assertEqual(st['cc_tone'], 'error')
        self.assertIn('boom', st['cc_text'])
        off = rw.role_state_full(15000, {}, (), lang='en')
        self.assertEqual(off['cc_tone'], 'muted')

    def test_enabled_agent_reports_its_local_link(self):
        rw = _rw()
        cases = {None: 'muted', 'ok': 'ok', 'unreachable': 'error',
                 'auth': 'error'}
        for code, tone in cases.items():
            cc = {'enabled': True,
                  'last_status': {'state': code} if code else {}}
            st = rw.role_state_full(15000, cc, (), lang='en')
            self.assertEqual(st['cc_tone'], tone, code)


class ShowAtStartupGateTest(unittest.TestCase):
    """The gate reads the agent config blob. NEUTER target: flip the
    absent-key default to False and the first test goes red — the window
    must appear for fresh installs AND for upgrades from pre-window builds
    (their config has no key either), or nobody ever learns it exists."""

    def _with_tmp_config(self, tmp, initial=None):
        import json
        path = os.path.join(tmp, 'desktop_agent.json')
        if initial is not None:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(initial, f)
        return mock.patch.dict(os.environ, {'TOFU_DESKTOP_CONFIG': path})

    def test_absent_key_shows(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp):
                self.assertTrue(rw.should_show_at_startup())

    def test_persisted_false_hides(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp, {'show_role_window': False}):
                self.assertFalse(rw.should_show_at_startup())

    def test_persist_round_trip(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp):
                rw.persist_show_at_startup(False)
                self.assertFalse(rw.should_show_at_startup())
                rw.persist_show_at_startup(True)
                self.assertTrue(rw.should_show_at_startup())

    def test_persist_does_not_clobber_other_keys(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp, {'agent_id': 'abc'}):
                rw.persist_show_at_startup(False)
                from lib.desktop_agent.config import load_config
                self.assertEqual(load_config().get('agent_id'), 'abc')


class RendererDesignRatchetTest(unittest.TestCase):
    """The 2026-08-04 redesign anchors: the window is centred and branded
    (no more tk-feather icon in the taskbar), and every tier carries its
    one-line explanation — the fact the tray never had room for. NEUTER
    target: delete a desc key from _TIER_DESC_KEYS or let one go missing
    from STRINGS."""

    def test_window_is_centered_and_branded(self):
        src = _src('desktop/role_window.py')
        self.assertIn('theme.center_on_screen(', src,
                      'the window opens wherever the WM drops it again')
        self.assertIn('theme.set_window_icon(', src,
                      'the tk-feather taskbar icon is back')

    def test_every_tier_desc_key_exists_both_languages(self):
        rw = _rw()
        import desktop._tk_theme as theme
        for tier, desc_key in rw._TIER_DESC_KEYS.items():
            self.assertIn(tier, rw._TIER_KEYS,
                          '%s has a desc but no label key' % tier)
            pair = theme.STRINGS.get(desc_key)
            self.assertIsNotNone(pair, '%s missing from STRINGS' % desc_key)
            self.assertIn('en', pair, '%s missing en' % desc_key)
            self.assertIn('zh', pair, '%s missing zh' % desc_key)

    def test_panel_uses_compact_tiles_and_a_join_action(self):
        src = _src('desktop/role_window.py')
        self.assertIn('index // 2', src,
                      'permission controls are no longer a two-column grid')
        self.assertIn("_act('join')", src,
                      'joining a server vanished from the main panel')
        self.assertIn("_act('leave'", src,
                      'leaving a server vanished from the main panel')
        self.assertIn('width=px(680)', src,
                      'the redesigned panel regressed to the cramped width')

    def test_tier_keys_cover_every_tier(self):
        rw = _rw()
        for tier in rw.role_state_full(15000, {}, (), lang='en')['tiers']:
            self.assertIn(tier, rw._TIER_KEYS,
                          '%s lost its label key' % tier)
            self.assertIn(tier, rw._TIER_DESC_KEYS,
                          '%s lost its desc key' % tier)


class WiringRatchetTest(unittest.TestCase):
    """Source-level pins so the panel can never silently vanish again."""

    def _imports_role_window(self, rel):
        tree = ast.parse(_src(rel), filename=rel)
        full = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                full.add(node.module)
                full.update('%s.%s' % (node.module, a.name)
                            for a in node.names)
            elif isinstance(node, ast.Import):
                full.update(a.name for a in node.names)
        return any('role_window' in m for m in full)

    def test_launcher_imports_role_window(self):
        for rel in ('desktop/launcher.py',):
            self.assertTrue(self._imports_role_window(rel),
                            '%s does not import desktop.role_window — the '
                            'startup role declaration is gone' % rel)

    def test_launcher_shows_window_at_startup(self):
        for rel in ('desktop/launcher.py',):
            src = _src(rel)
            self.assertIn('should_show_at_startup', src,
                          '%s lost the startup gate — the role window is '
                          'never offered' % rel)
            self.assertIn('show_role_window(', src,
                          '%s never calls show_role_window' % rel)

    def test_window_wires_the_copy_diag_button(self):
        """Debugging a dead link on a machine with no shell access needs the
        evidence pack (routes / candidates / verdicts / log tail) one click
        away. NEUTER: drop the action or the button and this goes red.
        """
        launcher = _src('desktop/launcher.py')
        self.assertIn("'copy_diag'", launcher,
                      'the window lost its diagnostics action')
        report = self._diag_report_for_one_joined_server()
        self.assertIn('server Lab: url=https://lab.example', report)
        self.assertIn('secret=set (13 chars)', report)
        self.assertNotIn('tok_SECRET123', report)
        self.assertIn('link-ready', report)
        window = _src('desktop/role_window.py')
        self.assertIn('desktop.role.copyDiag', window,
                      'the agent role window lost the copy-diag button')
        import desktop._tk_theme as theme
        for key in ('desktop.role.copyDiag', 'desktop.role.copyDiagDone'):
            pair = theme.STRINGS.get(key)
            self.assertIsNotNone(pair, '%s missing from STRINGS' % key)
            self.assertIn('en', pair, '%s missing en' % key)
            self.assertIn('zh', pair, '%s missing zh' % key)

    def _diag_report_for_one_joined_server(self):
        import tempfile
        from desktop import launcher

        class _Links:
            def statuses(self):
                return [{'id': 'lab', 'last_status': {'state': 'ok'}}]

        record = {'id': 'lab', 'label': 'Lab', 'url': 'https://lab.example',
                  'candidates': [], 'state': 'active', 'secret': 'tok_SECRET123'}
        with tempfile.TemporaryDirectory() as scratch:
            log_path = os.path.join(scratch, 'desktop.log')
            with open(log_path, 'w', encoding='utf-8') as handle:
                handle.write('link-ready\n')
            with mock.patch.object(launcher, '_LOG_PATH', log_path), \
                    mock.patch('lib.desktop_agent.config.attachments',
                               return_value=[record]), \
                    mock.patch('lib.desktop_agent.config.config_path',
                               return_value=os.path.join(scratch, 'node.json')):
                return launcher._diag_report({'links': _Links()})

    def test_tray_gains_control_panel_reentry(self):
        for rel in ('desktop/launcher.py',):
            self.assertIn('desktop.tray.controlPanel', _src(rel),
                          '%s tray lost the Control-panel re-entry — the '
                          'panel is tray-hidden again' % rel)


if __name__ == '__main__':
    unittest.main()
