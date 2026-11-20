"""Per-project persistent toolchain registry: ``tofu.toolchains.json`` at the project root.

A project declares the toolchains its commands need (TeX Live, a conda or
Python environment, a CLI) once, with where each lives on persistent storage
and how to install it there. Every ``run_command`` child of that project, a
Worker's included (its checkout carries the committed file), gets the declared
directories on ``PATH`` and the declared variables, built by
``run_command._get_cmd_env`` next to the shared tool paths
(``process_environment.apply_tool_paths``). No ``env.sh`` needs sourcing and
nothing relies on ``/tmp``: a location under a temporary root is refused.

File shape (version 1)::

    {"version": 1,
     "toolchains": {
       "<name>": {
         "description": "what it is for",
         "location": "/absolute/persistent/prefix",
         "path": ["bin"],            # dirs under location, prepended to PATH
         "path_append": [],          # dirs under location, appended after the system PATH
         "env": {"TEXBIN": "{location}/bin/x86_64-linux"},
         "provides": ["pdflatex"],   # executables a complete install has
         "install": "shell recipe; $TOOLCHAIN_LOCATION is location"}}}

A missing location is not an error for commands that do not need it: the
toolchain is skipped, named in ``TOFU_TOOLCHAINS_MISSING``, and a command that
exits 127 (command not found) gets a notice naming its install recipe. A
malformed file refuses every command of the project with the reason, so a
broken declaration never silently changes which binaries run.

Entry points: ``load_registry``, ``apply_project_toolchains``,
``missing_toolchain_notices``, and ``python3 -m lib.project_mod.toolchains
status|install`` (install runs a recipe with ``sh -c``; run it through
``run_command`` so approval, Stop and output bounds apply).
Dependencies: standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

REGISTRY_FILENAME = 'tofu.toolchains.json'
_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_FIELDS = frozenset({'description', 'location', 'path', 'path_append', 'env', 'provides', 'install'})
# Environment entries a toolchain may not set: PATH is composed from path/path_append,
# and the rest are the command environment's own isolation and identity.
_RESERVED_ENV = frozenset({'PATH', 'HOME', 'TMPDIR', 'VIRTUAL_ENV', 'PYTHONHOME', 'TOFU_DATA_DIR',
                           'TOFU_TOOLCHAINS', 'TOFU_TOOLCHAINS_MISSING'})


class ToolchainRegistryError(ValueError):
    """The project's toolchain registry cannot be applied; names the file and the fix."""


def _temporary_roots() -> list[str]:
    roots = {'/tmp', '/var/tmp', '/dev/shm', tempfile.gettempdir()}
    return sorted({os.path.realpath(root) for root in roots})


def is_temporary_location(path: str) -> bool:
    """Whether ``path`` lies under a temporary root, which hosts and cleanups erase."""
    resolved = os.path.realpath(path)
    for root in _temporary_roots():
        if resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _fail(source: str, message: str):
    raise ToolchainRegistryError(
        f'{source}: {message}. Fix {REGISTRY_FILENAME} (see docs/PROJECT_TOOLCHAINS.md); '
        'commands of this project refuse to start until it is valid.')


def _relative_dirs(source, name, field, value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _fail(source, f'toolchain {name}: {field} must be a list of directory names under location')
    for item in value:
        if os.path.isabs(item) or '..' in item.replace('\\', '/').split('/'):
            _fail(source, f'toolchain {name}: {field} entry {item!r} must be relative to location '
                          'and stay inside it')
    return [item.strip() for item in value]


def parse_registry(document, source: str = REGISTRY_FILENAME) -> dict:
    """Validate a registry document; returns ``{name: normalized spec}`` in declaration order."""
    if not isinstance(document, dict) or document.get('version') != 1:
        _fail(source, 'the document must be an object with "version": 1')
    if set(document) - {'version', 'toolchains'}:
        _fail(source, f'unknown top-level fields {sorted(set(document) - {"version", "toolchains"})}')
    toolchains = document.get('toolchains')
    if not isinstance(toolchains, dict):
        _fail(source, '"toolchains" must be an object of name -> toolchain')
    parsed = {}
    for name, spec in toolchains.items():
        if not _NAME.match(name):
            _fail(source, f'toolchain name {name!r} must match {_NAME.pattern}')
        if not isinstance(spec, dict):
            _fail(source, f'toolchain {name} must be an object')
        unknown = set(spec) - _FIELDS
        if unknown:
            _fail(source, f'toolchain {name}: unknown fields {sorted(unknown)}')
        location = spec.get('location')
        if not isinstance(location, str) or not os.path.isabs(location):
            _fail(source, f'toolchain {name}: location must be an absolute path on persistent storage')
        location = os.path.normpath(location)
        if is_temporary_location(location):
            _fail(source, f'toolchain {name}: location {location} is under a temporary root '
                          f'({", ".join(_temporary_roots())}), which is erased between hosts and tasks; '
                          'install it on persistent storage instead')
        install = spec.get('install')
        if not isinstance(install, str) or not install.strip():
            _fail(source, f'toolchain {name}: install must be the shell recipe that recreates location')
        env = spec.get('env', {})
        if not isinstance(env, dict) or any(not isinstance(key, str) or not _ENV_NAME.match(key)
                                            or not isinstance(value, str) or '\0' in value
                                            for key, value in env.items()):
            _fail(source, f'toolchain {name}: env must map variable names to strings')
        reserved = sorted(set(env) & _RESERVED_ENV)
        if reserved:
            _fail(source, f'toolchain {name}: env may not set {reserved} (use path/path_append for PATH)')
        provides = spec.get('provides', [])
        if not isinstance(provides, list) or any(not isinstance(item, str) or not item or '/' in item
                                                 for item in provides):
            _fail(source, f'toolchain {name}: provides must list executable names')
        description = spec.get('description', '')
        if not isinstance(description, str):
            _fail(source, f'toolchain {name}: description must be a string')
        parsed[name] = {
            'description': description, 'location': location,
            'path': _relative_dirs(source, name, 'path', spec.get('path', [])),
            'path_append': _relative_dirs(source, name, 'path_append', spec.get('path_append', [])),
            'env': {key: value.replace('{location}', location) for key, value in env.items()},
            'provides': list(provides), 'install': install,
        }
    return parsed


def registry_path(root: str) -> str:
    return os.path.join(root, REGISTRY_FILENAME)


def load_registry(root: str | None) -> dict | None:
    """The project's parsed registry, or None when ``root`` declares none."""
    if not root:
        return None
    path = registry_path(root)
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ToolchainRegistryError(f'{path}: cannot be read ({error})') from error
    try:
        document = json.loads(text)
    except ValueError as error:
        _fail(path, f'invalid JSON ({error})')
    return parse_registry(document, path)


def installed(spec: dict) -> bool:
    return os.path.isdir(spec['location'])


def apply_project_toolchains(environment: dict[str, str], root: str | None) -> list[str]:
    """Put the project's installed toolchains on ``environment``; returns the missing names.

    Declared ``path`` directories go before the inherited PATH in declaration
    order, ``path_append`` ones after it. A project ``.venv`` activated later
    still wins for Python, as a virtualenv must.
    """
    registry = load_registry(root)
    if not registry:
        return []
    prepend, append, applied, missing = [], [], [], []
    for name, spec in registry.items():
        if not installed(spec):
            missing.append(name)
            continue
        applied.append(name)
        prepend.extend(os.path.join(spec['location'], item) for item in spec['path'])
        append.extend(os.path.join(spec['location'], item) for item in spec['path_append'])
        environment.update(spec['env'])
    existing = environment.get('PATH', os.defpath).split(os.pathsep)
    environment['PATH'] = os.pathsep.join(dict.fromkeys([*prepend, *existing, *append]))
    environment['TOFU_TOOLCHAINS'] = ','.join(applied)
    if missing:
        environment['TOFU_TOOLCHAINS_MISSING'] = ','.join(missing)
    else:
        environment.pop('TOFU_TOOLCHAINS_MISSING', None)
    return missing


def missing_toolchain_notices(environment, exit_code) -> list[str]:
    """Notices for a command that found no program (exit 127) while a declared toolchain is absent."""
    names = (environment or {}).get('TOFU_TOOLCHAINS_MISSING')
    if exit_code != 127 or not names:
        return []
    return [f'toolchain {name} declared in {REGISTRY_FILENAME} is not installed at its location, so its '
            f'programs are not on PATH; run its toolchains.{name}.install recipe from that file with '
            'TOOLCHAIN_LOCATION set to its location (on persistent storage, never /tmp), then rerun'
            for name in names.split(',')]


def status(root: str) -> list[dict]:
    registry = load_registry(root) or {}
    import shutil
    rows = []
    for name, spec in registry.items():
        dirs = [os.path.join(spec['location'], item) for item in spec['path'] + spec['path_append']]
        search = os.pathsep.join(dirs)
        rows.append({'name': name, 'location': spec['location'], 'installed': installed(spec),
                     'provides': {program: shutil.which(program, path=search) for program in spec['provides']}})
    return rows


def install(root: str, name: str, *, run=None) -> int:
    """Run ``name``'s install recipe with ``sh -c``; ``TOOLCHAIN_LOCATION`` names its location."""
    registry = load_registry(root) or {}
    if name not in registry:
        raise ToolchainRegistryError(f'{registry_path(root)} declares no toolchain {name!r}')
    spec = registry[name]
    import subprocess
    environment = {**os.environ, 'TOOLCHAIN_LOCATION': spec['location']}
    run = run or (lambda argv, env: subprocess.run(argv, env=env, cwd=root).returncode)
    return run(['sh', '-ec', spec['install']], environment)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='python3 -m lib.project_mod.toolchains',
                                     description='Inspect or install the project toolchains of tofu.toolchains.json.')
    parser.add_argument('--project', default=os.getcwd(), help='project root (default: current directory)')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status', help='list declared toolchains and whether each is installed')
    installer = sub.add_parser('install', help="run a toolchain's install recipe")
    installer.add_argument('name')
    arguments = parser.parse_args(argv)
    root = os.path.abspath(arguments.project)
    if arguments.action == 'status':
        print(json.dumps(status(root), indent=2))
        return 0
    return install(root, arguments.name)


if __name__ == '__main__':
    raise SystemExit(main())
