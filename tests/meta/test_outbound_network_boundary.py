"""New integrations must inherit the shared network authority automatically."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
# These are concrete probe transports, below route admission, not app callers.
WIRE_OWNERS = {
    'lib/network_routes.py', 'lib/proxy.py', 'lib/netpath.py',
    'lib/project_mod/network_command.py',
}
RAW_CALLS = {
    *(f'requests.{name}' for name in (
        'Session', 'session', 'request', 'get', 'post', 'put', 'delete', 'head', 'patch')),
    'httpx.Client', 'httpx.AsyncClient', 'httpx2.Client', 'httpx2.AsyncClient',
    'urllib.request.urlopen', 'urllib.request.build_opener',
}


def raw_network_calls(source: str) -> list[tuple[int, str]]:
    """Calls whose root name is bound by an import to a raw HTTP client."""
    tree = ast.parse(source)
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                imports[entry.asname or entry.name.split('.')[0]] = (
                    entry.name if entry.asname else entry.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            for entry in node.names:
                imports[entry.asname or entry.name] = node.module + '.' + entry.name
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        first, separator, rest = ast.unparse(node.func).partition('.')
        # A local such as ``requests = queue.SimpleQueue()`` is not the module.
        if first not in imports:
            continue
        resolved = imports[first] + separator + rest
        if resolved in RAW_CALLS:
            calls.append((node.lineno, resolved))
    return calls


def test_only_imported_clients_count_as_raw_network_calls():
    assert raw_network_calls(
        'import requests\nrequests.get("u")\n') == [(2, 'requests.get')]
    assert raw_network_calls(
        'from urllib import request as r\nr.urlopen("u")\n') == [(2, 'urllib.request.urlopen')]
    assert raw_network_calls(
        'import queue\nrequests = queue.SimpleQueue()\nrequests.put(1)\nrequests.get()\n') == []


def test_application_network_calls_use_managed_transports():
    failures = []
    for root in ('lib', 'routes', 'plugins/tofu-trading/tofu_trading'):
        for path in (ROOT / root).rglob('*.py'):
            relative = path.relative_to(ROOT).as_posix()
            if relative in WIRE_OWNERS or relative.startswith('lib/http_transport/'):
                continue
            for lineno, resolved in raw_network_calls(path.read_text(encoding='utf-8')):
                failures.append(f'{relative}:{lineno}: {resolved}')
    assert not failures, 'Use managed network clients (docs/NETWORK.md):\n' + '\n'.join(failures)
