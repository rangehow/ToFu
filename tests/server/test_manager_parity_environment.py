"""Offline source-slice checks: no parity imports, child processes or host-state probes.

Run directly with ``/usr/bin/python3 -I -S tests/server/test_manager_parity_environment.py``.
Only the named fixture bodies execute, against dictionary/filesystem/process captures.
"""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


PARITY = Path(__file__).with_name('rust_manager_parity')


def load_functions(filename, names, namespace):
    tree = ast.parse((PARITY / filename).read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in names]
    assert {node.name for node in selected} == set(names)
    for node in selected:
        node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, str(PARITY / filename), 'exec'), namespace)


class CapturedPath(PurePosixPath):
    directories = {}
    files = {}
    opens = []

    def mkdir(self, **kwargs):
        self.directories[str(self)] = kwargs

    def resolve(self):
        return self

    def write_text(self, text):
        self.files[str(self)] = text

    def unlink(self, *, missing_ok=False):
        self.files.pop(str(self), None)

    def open(self, mode):
        self.opens.append((str(self), mode))
        return SimpleLog()


class SimpleLog:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class ManagerParityEnvironmentTests(unittest.TestCase):
    def setUp(self):
        CapturedPath.directories = {}
        CapturedPath.files = {}
        CapturedPath.opens = []
        self.root = CapturedPath('/synthetic/fixture')
        self.ambient = {
            'PATH': '/synthetic/bin:/usr/bin', 'HOME': '/synthetic/unowned-home',
            'LANG': 'unrelated-locale', 'TMPDIR': '/synthetic/unowned-tmp',
            'XDG_CACHE_HOME': '/synthetic/unowned-cache', 'POISON_MARKER': 'do-not-copy',
            'TOFU_PROJECT_PATH': '/synthetic/unowned-project', 'PORT': '15000',
            'TOFU_SUPERVISOR_OWNER_PID': '999', 'TOFU_MANAGER_BINARY': '/unattested/binary',
        }
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = Mock(return_value=self.process)
        self.retire = Mock(return_value=[])
        ports = iter([31001, 31001, 31002])
        self.namespace = {
            'Path': CapturedPath, 'REPO': CapturedPath('/synthetic/source'),
            'os': SimpleNamespace(environ=self.ambient, getpid=lambda: 123, chmod=Mock()),
            'sys': SimpleNamespace(platform='linux', executable='/synthetic/python'),
            'subprocess': SimpleNamespace(Popen=self.popen, DEVNULL=-3, STDOUT=-2,
                                          TimeoutExpired=TimeoutError),
            'signal': SimpleNamespace(SIGTERM=15), 'WAIT_SECONDS': 60.0, 'SOURCE': 'parity',
            'free_port': lambda: next(ports), 'wait_for': Mock(),
            '_launch_argv': Mock(side_effect=lambda impl, *args: [f'/synthetic/{impl}']),
            '_request': Mock(return_value=(200, {'ok': True})),
            'retire_processes_under': self.retire,
            'WORKER': 'inert worker text', 'SERVERCTL': 'inert preparation text',
        }
        load_functions('fake_project.py', ['build_project'], self.namespace)
        load_functions('conftest.py', ['_scrubbed_environment', 'manager', 'ManagerHarness'], self.namespace)
        load_functions('test_restart_parity.py', ['_launch'], self.namespace)

    def assert_owned(self, environment):
        self.assertEqual(environment['HOME'], str(self.root / 'home'))
        for key in ('TMPDIR', 'TMP', 'TEMP'):
            self.assertEqual(environment[key], str(self.root / 'tmp'))
        for key, suffix in (('XDG_CACHE_HOME', '.cache'), ('XDG_CONFIG_HOME', '.config'),
                            ('XDG_DATA_HOME', '.local/share'), ('XDG_STATE_HOME', '.local/state')):
            self.assertEqual(environment[key], str(self.root / 'home' / suffix))
        for key in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            self.assertEqual(CapturedPath.directories[environment[key]],
                             {'mode': 0o700, 'parents': True, 'exist_ok': True})
        self.assertEqual(environment['PATH'], self.ambient['PATH'])
        self.assertEqual(environment['PYTHONPATH'], '/synthetic/source')
        for key in ('POISON_MARKER', 'LANG', 'TOFU_PROJECT_PATH', 'TOFU_MANAGER_BINARY'):
            self.assertNotIn(key, environment)

    def test_environment_is_owned_idempotent_and_does_not_mutate_input(self):
        original = self.ambient.copy()
        scrub = self.namespace['_scrubbed_environment']
        first = scrub(self.root)
        self.assert_owned(first)
        self.assertEqual(scrub(self.root), first)
        self.assertEqual(self.ambient, original)
        self.ambient.clear()
        empty = scrub(self.root)
        self.assertNotIn('PATH', empty)
        self.assertEqual(empty['HOME'], str(self.root / 'home'))

    def test_both_actual_routes_and_implementations_capture_owned_environment(self):
        for route in ('manager', '_launch'):
            for impl in ('python', 'rust'):
                with self.subTest(route=route, impl=impl):
                    self.setUp()
                    project = self.namespace['build_project'](self.root)
                    self.assertEqual(project, self.root / 'project')
                    generator = None
                    if route == 'manager':
                        prepare = Mock()
                        generator = self.namespace['manager'](
                            impl, False, False,
                            {'TOFU_TEST_TAG': '{project}/tag', 'LD_PRELOAD': '/synthetic/shim.so',
                             'TOFU_SUPERVISOR_PYTHON': '/synthetic/explicit-python'},
                            prepare, self.namespace['REPO'], self.root)
                        harness = next(generator)
                        prepare.assert_called_once_with(project)
                    else:
                        harness = self.namespace['_launch'](impl, project, 31001, 31002, 123, self.root)
                    args, options = self.popen.call_args
                    self.assertEqual(args[0], [f'/synthetic/{impl}'])
                    environment = options['env']
                    self.assert_owned(environment)
                    self.assertEqual(environment['TOFU_SUPERVISOR_PROJECTS'], str(project))
                    self.assertEqual(environment['TOFU_SUPERVISOR_OWNER_PID'], '123')
                    self.assertEqual(environment['TOFU_SUPERVISOR_PORT'], '31001')
                    self.assertEqual(environment['PORT'], '31002')
                    self.assertEqual(environment['TOFU_SUPERVISOR_HOST'], '127.0.0.1')
                    self.assertEqual(environment['TOFU_MANAGER_INTERVAL'], '0.2')
                    self.assertEqual(environment['TOFU_HEARTBEAT_DIR'], str(self.root / 'hb'))
                    self.assertEqual(options['cwd'], self.namespace['REPO'])
                    self.assertEqual((options['stdin'], options['stderr'], options['start_new_session']),
                                     (-3, -2, True))
                    self.assertEqual(CapturedPath.opens, [(str(project / 'logs/server-manager.log'), 'ab')])
                    self.assertEqual((harness.impl, harness.project, harness.process), (impl, project, self.process))
                    harness.control('stop')
                    self.assertEqual(self.namespace['_request'].call_args.kwargs['body'],
                                     {'projectPath': str(project), 'source': 'parity'})
                    if generator is not None:
                        self.assertEqual(environment['TOFU_TEST_TAG'], str(project / 'tag'))
                        self.assertEqual(environment['LD_PRELOAD'], '/synthetic/shim.so')
                        self.assertEqual(environment['TOFU_SUPERVISOR_PYTHON'], '/synthetic/explicit-python')
                        with self.assertRaises(StopIteration):
                            next(generator)
                        self.namespace['os'].chmod.assert_called_once_with(project / 'data', 0o700)
                        self.assertEqual(CapturedPath.files[str(project / 'probe-stop-mode')], 'clean')
                        self.assertEqual(CapturedPath.files[str(project / 'exec-stop-mode')], 'clean')
                        self.process.send_signal.assert_called_once_with(15)
                        self.process.wait.assert_called_once_with(timeout=60.0)
                        self.retire.assert_called_once_with(self.root)
                    else:
                        self.assertEqual(environment['TOFU_SUPERVISOR_PYTHON'], '/synthetic/python')
                        self.retire.assert_not_called()

    def test_scrub_reads_only_path_and_timeout_cleanup_remains_owned(self):
        class LookupOnly(dict):
            def __getitem__(self, key):
                if key != 'PATH':
                    raise AssertionError(f'unneeded ambient value read: {key}')
                return super().__getitem__(key)

        self.namespace['os'].environ = LookupOnly(self.ambient)
        generator = self.namespace['manager'](
            'python', False, False, {}, None, self.namespace['REPO'], self.root)
        next(generator)
        self.assert_owned(self.popen.call_args.kwargs['env'])
        self.process.wait.side_effect = [TimeoutError(), None]
        generator.close()
        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.wait.call_count, 2)
        self.retire.assert_called_once_with(self.root)

    def test_oracle_rejects_legacy_ambient_home_inheritance(self):
        legacy = {key: self.ambient[key] for key in ('PATH', 'HOME', 'LANG')}
        legacy['PYTHONPATH'] = '/synthetic/source'
        with self.assertRaises(AssertionError):
            self.assert_owned(legacy)
        for route in ('manager', '_launch'):
            with self.subTest(route=route):
                self.namespace['_scrubbed_environment'] = lambda root: legacy.copy()
                project = self.namespace['build_project'](self.root)
                if route == 'manager':
                    generator = self.namespace['manager'](
                        'python', False, False, {}, None, self.namespace['REPO'], self.root)
                    next(generator)
                else:
                    generator = None
                    self.namespace['_launch']('rust', project, 31001, 31002, 123, self.root)
                with self.assertRaises(AssertionError):
                    self.assert_owned(self.popen.call_args.kwargs['env'])
                if generator is not None:
                    generator.close()


if __name__ == '__main__':
    unittest.main()
