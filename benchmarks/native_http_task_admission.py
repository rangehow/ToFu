"""Observe installed Granian task-mode admission without starting transport.

This detects silent fallback, not ASGI lifecycle or performance compatibility.
The only replaced method belongs to the isolated server instance.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import sys
import tomllib


async def check():
    from granian.constants import Interfaces, TaskImpl
    from granian.server.embed import Server

    async def application(scope, receive, send):
        pass

    server = Server(application, interface=Interfaces.ASGI, task_impl=TaskImpl.rust, log_enabled=False)
    observed = []

    async def no_transport(*args, **kwargs):
        observed.append(str(server.task_impl))

    server._serve = no_transport
    await server.serve()
    assert observed == [str(server.task_impl)]
    source = Path(inspect.getfile(Server))
    project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    return {'scope': 'installed Server.serve admission; transport startup intercepted; no performance evidence',
            'python': sys.version, 'granian': importlib.metadata.version('granian'),
            'requested': 'rust', 'effective': str(server.task_impl),
            'server_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'project_source_sha256': hashlib.sha256(project.read_bytes()).hexdigest(),
            'project_python_requirement': tomllib.loads(project.read_text())['project']['requires-python'],
            'verdict': ('unsupported_on_current_runtime' if server.task_impl != TaskImpl.rust else
                        'requested_mode_retained; lifecycle_and_performance_unverified')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check())
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
