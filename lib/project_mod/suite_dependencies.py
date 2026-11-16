"""Declared test-runner dependencies of the project interpreter.

Entry points: missing(python, root) and ``python -m lib.project_mod.suite_dependencies
{check,install}``. The `test` extra in pyproject.toml is the one declaration of what
the suite needs (pytest, pytest-timeout, pytest-xdist, ...); the pytest addopts
load several of those plugins unconditionally, so an interpreter lacking one fails
every run. `check` names what is missing; `install` installs exactly the missing
requirements into the environment that owns the interpreter's packages: a task
worktree's bridged `.venv` (workspace_runtime) resolves to its source environment,
so one install serves every worktree of that checkout.

Dependencies: standard library, lib.tools.process_environment (the child
environment); `packaging` inside the checked interpreter.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

TEST_EXTRA = 'test'

_MISSING_PROBE = r'''
import importlib.metadata as metadata, sys
from packaging.requirements import Requirement
for text in sys.argv[1:]:
    requirement = Requirement(text)
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    try:
        version = metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        print(text)
        continue
    if not requirement.specifier.contains(version, prereleases=True):
        print(text)
'''


def missing(python: str, root: Path) -> list[str]:
    """Requirements of ROOT's `test` extra that `python` does not satisfy.

    A repository that declares no `test` extra declares nothing to check.
    """
    try:
        project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    declared = project.get('project', {}).get('optional-dependencies', {}).get(TEST_EXTRA, [])
    if not declared:
        return []
    from lib.tools.process_environment import inherited_child_environment
    environment = {key: value for key, value in inherited_child_environment().items()
                   if key != 'PYTHONPATH'}
    probe = subprocess.run([python, '-c', _MISSING_PROBE, *declared],
                           env=environment, capture_output=True, text=True, timeout=60)
    if probe.returncode:
        raise ValueError(f'cannot check declared test dependencies with {python}: '
                         f'{probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else probe.returncode}')
    return [line for line in probe.stdout.splitlines() if line]


def owning_interpreter(python: str) -> str:
    """The interpreter whose environment holds `python`'s shared packages."""
    from lib.project_mod.workspace_runtime import SOURCE_PACKAGES_PTH
    prefix = Path(subprocess.check_output([python, '-c', 'import sys; print(sys.prefix)'],
                                          text=True, timeout=30).strip())
    for bridge in [*prefix.glob(f'lib*/python*/site-packages/{SOURCE_PACKAGES_PTH}'),
                   *prefix.glob(f'Lib/site-packages/{SOURCE_PACKAGES_PTH}')]:
        match = re.search(r"addsitedir\((['\"])(.+?)\1\)", bridge.read_text(encoding='utf-8'))
        if match:
            for parent in Path(match.group(2)).parents:
                if (parent / 'pyvenv.cfg').is_file():
                    source = parent / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python3')
                    return str(source)
    return python


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (['check'], ['install']):
        print('usage: python -m lib.project_mod.suite_dependencies {check,install}', file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    from lib.project_mod.landing_verification import _project_interpreter
    python = _project_interpreter(root)
    absent = missing(python, root)
    if not absent:
        print(f'{python}: declared `{TEST_EXTRA}` dependencies are installed')
        return 0
    if argv == ['check']:
        print(f'{python} lacks declared `{TEST_EXTRA}` dependencies: {", ".join(absent)}\n'
              'Install them with `make test-deps`.', file=sys.stderr)
        return 1
    target = owning_interpreter(python)
    print(f'Installing into {target}: {", ".join(absent)}')
    return subprocess.run([target, '-m', 'pip', 'install', *absent]).returncode


if __name__ == '__main__':
    sys.exit(main())
