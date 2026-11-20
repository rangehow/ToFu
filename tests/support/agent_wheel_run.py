"""One scripted `tofu-agent run`, shared by the installed-wheel test and the storage-free probe.

A tool round against a loopback provider, started by the `tofu-agent` console
entry point in a ``-S`` child that runs outside the checkout. The child
reports where every loaded module came from and every application import that
nothing could satisfy, including swallowed ones.

``tests/tofu_agent/test_tofu_agent_run_installed.py`` runs it from an extracted
wheel. ``tests/tofu_agent/test_tofu_agent_storage_free_run.py`` runs it from the
checkout behind the wheel's import boundary: ``WheelBoundary`` takes the
standard ``PathFinder``'s place, so a checkout module that the package
manifest (``scripts/agent_wheel_manifest.py``) leaves out resolves exactly as
it would in the installed wheel, from outside the checkout or not at all.
"""
from __future__ import annotations

import dataclasses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import site
import subprocess
import sys
import threading
import tomllib

from tests.support.standalone_model_routing import standalone_model_routing_envelope

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_OWNER = ROOT / 'scripts' / 'agent_wheel_manifest.py'
MARKER = 'installed-wheel-marker.txt'
FINAL_ANSWER = 'The installed wheel wrote its marker.'
MODEL_SECRET = 'wheel-test-credential-0123456789abcdef'
# The one declared optional storage capability: lib/tasks_pkg/handlers/__init__.py
# drops the large tool-result continuation handler when lib.storage is absent.
OPTIONAL_STORAGE_IMPORTERS = frozenset({'lib.tool_results.tool_result_artifacts'})

# Mirrors the console script an installer generates from entry_points.txt.
_CONSOLE_SCRIPT = r'''
import importlib, importlib.machinery, importlib.util, json, os, sys
config = json.loads(sys.argv[1])
sys.argv = ['tofu-agent', *sys.argv[2:]]
missing, refused = [], []


def importer():
    frame = sys._getframe(2)
    while frame is not None and frame.f_globals.get('__name__', '').startswith('importlib'):
        frame = frame.f_back
    return frame and frame.f_globals.get('__name__')


boundary = config['boundary']
if boundary:
    spec = importlib.util.spec_from_file_location('_agent_wheel_manifest', boundary['manifest'])
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    manifest = owner.WheelManifest.load(boundary['root'])
    checkout = os.path.realpath(boundary['root'])

    def inside(entry):
        entry = os.path.realpath(entry or os.getcwd())
        return entry == checkout or entry.startswith(checkout + os.sep)

    class WheelBoundary(importlib.machinery.PathFinder):
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if manifest.is_repository_name(name) and not manifest.ships_module(name):
                outside = [entry for entry in (sys.path if path is None else path)
                           if not inside(entry)]
                found = super().find_spec(name, outside, target)
                if found is None:
                    refused.append([name, importer()])
                return found
            return super().find_spec(name, path, target)

    sys.meta_path[sys.meta_path.index(importlib.machinery.PathFinder)] = WheelBoundary


class Overlay:
    def find_spec(self, name, path=None, target=None):
        source = config['overlay'].get(name)
        return None if source is None else importlib.util.spec_from_file_location(name, source)


class UnsatisfiedImports:
    def find_spec(self, name, path=None, target=None):
        if name.partition('.')[0] in top_level:
            missing.append([name, importer()])
        return None


top_level = frozenset(config['top_level'])


sys.meta_path.insert(0, Overlay())
sys.meta_path.append(UnsatisfiedImports())
try:
    status = getattr(importlib.import_module(config['module']), config['attribute'])()
finally:
    with open(config['report'], 'w', encoding='utf-8') as stream:
        json.dump({'missing': missing, 'refused': refused, 'modules': {
            name: getattr(module, '__file__', None)
            for name, module in list(sys.modules.items())}}, stream)
sys.exit(status)
'''


@dataclasses.dataclass
class AgentRun:
    completed: subprocess.CompletedProcess
    result: dict | None
    report: dict
    requests: list[dict]
    workspace: Path


def dependency_paths() -> list[str]:
    """This interpreter's installed dependencies, which ``-S`` would not add.

    A venv overlay adds its base environment's site-packages through a .pth
    file, which site.getsitepackages() does not list and -S never processes.
    """
    return list(dict.fromkeys(
        path for path in [*site.getsitepackages(), *sys.path]
        if Path(path).name in {'site-packages', 'dist-packages'} and Path(path).is_dir()))


def module_name(path: str) -> str:
    parts = path[:-3].split('/')
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def manifest_modules(root: Path = ROOT) -> set[str]:
    """Every checkout module the package manifest says the wheel ships."""
    from scripts.agent_wheel_manifest import WheelManifest

    listed = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '*.py'],
        cwd=root, capture_output=True, text=True, check=True, timeout=60).stdout.split('\0')
    manifest = WheelManifest.load(root)
    return {module_name(path) for path in listed
            if path and (root / path).is_file() and manifest.ships_path(path)}


def console_entry(root: Path = ROOT) -> tuple[str, str]:
    """The checkout's declared `tofu-agent` console entry point (module, attribute)."""
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    module, _, attribute = project['project']['scripts']['tofu-agent'].partition(':')
    return module, attribute


def _scripted_provider(requests_seen: list[dict], *, credential_refusal=False) -> ThreadingHTTPServer:
    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests_seen.append(request)
            if len(requests_seen) == 1:
                delta = {'tool_calls': [{
                    'index': 0, 'id': 'call-installed-marker', 'type': 'function',
                    'function': {'name': 'run_command', 'arguments': json.dumps({
                        'command': f'printf installed-wheel > {MARKER}; printf "$WHEEL_TEST_VALUE"'})},
                }]}
                if credential_refusal:
                    delta['tool_calls'].append({
                        'index': 1, 'id': 'call-unavailable-credential', 'type': 'function',
                        'function': {'name': 'run_command', 'arguments': json.dumps({
                            'command': 'printf forbidden > credential-refusal-must-not-exist',
                            'credentials': ['wheel-smoke-deliberately-missing-credential']})},
                    })
                finish = 'tool_calls'
            else:
                delta, finish = {'content': FINAL_ANSWER}, 'stop'
            frames = [
                {'choices': [{'index': 0, 'delta': delta}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                 'usage': {'prompt_tokens': 200, 'completion_tokens': 10}},
            ]
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write((wire + 'data: [DONE]\n\n').encode())
            self.wfile.flush()
            self.close_connection = True

    return ThreadingHTTPServer(('127.0.0.1', 0), Provider)


def run_tofu_agent(tmp_path: Path, *, application: Path, entry: tuple[str, str],
                   top_level, boundary: bool = False,
                   overlay: dict[str, Path] | None = None, credential_refusal=False) -> AgentRun:
    """Run the scripted tool round with APPLICATION as the only application path.

    TOP_LEVEL names the wheel's top-level packages, whose unsatisfied imports
    the report lists. BOUNDARY applies the wheel's import boundary to a
    checkout APPLICATION; the report then also lists every refused checkout
    module. OVERLAY substitutes module sources (a regression to reproduce).
    """
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    requests_seen: list[dict] = []
    server = _scripted_provider(requests_seen, credential_refusal=credential_refusal)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret=MODEL_SECRET,
        model_id='installed-wheel-test')
    for model in envelope['model_routing']['models']:
        model['capabilities'].append('tools')
    for offering in envelope['model_routing']['offerings']:
        offering['capabilities'].append('tools')
    environment = {**os.environ,
                   'PYTHONPATH': os.pathsep.join([str(application), *dependency_paths()]),
                   'TOFU_AGENT_MODEL_ROUTING': json.dumps(envelope),
                   'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                   'TOFU_DATA_DIR': str(tmp_path / 'data'),
                   'WHEEL_TEST_VALUE': MODEL_SECRET}
    result_path = tmp_path / 'run.json'
    report_path = tmp_path / 'imports.json'
    config = {
        'report': str(report_path), 'module': entry[0], 'attribute': entry[1],
        'top_level': sorted(top_level),
        'boundary': {'root': str(ROOT), 'manifest': str(MANIFEST_OWNER)} if boundary else None,
        'overlay': {name: str(path) for name, path in (overlay or {}).items()},
    }
    try:
        completed = subprocess.run([
            sys.executable, '-S', '-c', _CONSOLE_SCRIPT, json.dumps(config),
            '--env-file', str(tmp_path / 'absent.env'),
            'run', '--task', f'Create {MARKER} in the project root.',
            '--cwd', str(workspace), '--output', str(result_path), '--timeout-s', '90',
        ], cwd=workspace, env=environment, capture_output=True, text=True, timeout=150)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else None
    report = json.loads(report_path.read_text(encoding='utf-8'))
    return AgentRun(completed, result, report, requests_seen, workspace)


def assert_tool_round(run: AgentRun) -> None:
    """The run completed its one tool round and returned the final answer."""
    assert 'ModuleNotFoundError' not in run.completed.stderr, run.completed.stderr[-4000:]
    assert run.completed.returncode == 0, run.completed.stdout[-2000:] + run.completed.stderr[-4000:]
    assert run.result['ok'] is True, run.result.get('error')
    assert run.result['status'] == 'done' and run.result['content'] == FINAL_ANSWER
    assert (run.workspace / MARKER).read_text(encoding='utf-8') == 'installed-wheel'
    assert len(run.requests) == 2
    assert MODEL_SECRET not in json.dumps(run.result)
    assert MODEL_SECRET not in json.dumps(run.requests[1])
    assert 'redacted credential' in json.dumps(run.requests[1])
    assert '[CredentialRedaction]' not in run.completed.stderr
    assert any(message.get('role') == 'tool'
               and message.get('tool_call_id') == 'call-installed-marker'
               for message in run.requests[1]['messages'])


def unsatisfied_imports(run: AgentRun) -> list[list]:
    """Imports under the wheel's packages nothing satisfied, except the declared optional storage capability."""
    attempts = {tuple(entry) for entry in run.report['missing']}
    return sorted(([name, importer] for name, importer in attempts if not (
        importer in OPTIONAL_STORAGE_IMPORTERS
        and (name == 'lib.storage' or name.startswith('lib.storage.')))), key=str)


def assert_credential_refusal(run: AgentRun) -> None:
    """An unavailable credential refuses its command, not the safe sibling call."""
    assert run.completed.returncode == 0, run.completed.stderr[-4000:]
    assert run.result['status'] == 'done'
    assert (run.workspace / MARKER).read_text() == 'installed-wheel'
    assert not (run.workspace / 'credential-refusal-must-not-exist').exists()
    messages = [message for request in run.requests[1:] for message in request['messages']]
    refused = [message for message in messages if message.get('role') == 'tool'
               and message.get('tool_call_id') == 'call-unavailable-credential']
    assert refused and 'Credential request rejected' in str(refused[0])
    assert MODEL_SECRET not in json.dumps(run.result)
    assert MODEL_SECRET not in json.dumps(run.requests[1:])
