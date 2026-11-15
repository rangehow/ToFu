"""Lazy, reconstructible runtime dependencies for an owned task worktree.

Entry point: prepare_workspace_runtime(source, workspace). Python gets a
private environment prefix with the source's installed packages readable;
pip writes stay local. Node packages are shared through an ignored private directory;
the frontend toolchain's caches stay local to each checkout.
The caller owns task authorization, serialization and durable path receipts.
"""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
import subprocess

# Caches the frontend toolchain writes inside node_modules/ on every build or
# test run: Vite's default cacheDir (.vite) and config-bundle directory
# (.vite-temp), and Vitest's default module cache (.vitest-cache). They are
# not packages, so they are never linked to the source checkout.
NODE_TOOL_CACHE_DIRECTORIES = frozenset({'.vite', '.vite-temp', '.vitest-cache'})

SOURCE_PACKAGES_PTH = 'tofu-source-packages.pth'


def _includes_system_site_packages(environment: Path) -> bool:
    for line in (environment / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'include-system-site-packages':
            return value.strip().lower() == 'true'
    return False


def _align_system_site_packages(source_env: Path, task_env: Path) -> None:
    # A source environment created with --system-site-packages (a venv over a
    # conda base, where pytest and its plugins often live) resolves packages
    # its own site-packages lacks; the bridged prefix must see the same set.
    # Repairs bridges created before the flag was mirrored.
    if not list(task_env.glob(f'lib*/python*/site-packages/{SOURCE_PACKAGES_PTH}')) \
            and not (task_env / 'Lib/site-packages' / SOURCE_PACKAGES_PTH).is_file():
        return
    wanted = _includes_system_site_packages(source_env)
    if _includes_system_site_packages(task_env) == wanted:
        return
    config = task_env / 'pyvenv.cfg'
    lines = [line for line in config.read_text(encoding='utf-8').splitlines()
             if line.partition('=')[0].strip() != 'include-system-site-packages']
    lines.append(f"include-system-site-packages = {'true' if wanted else 'false'}")
    config.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def prepare_workspace_runtime(source: str, workspace: str, *, restricted: bool = False) -> list[str]:
    source_root, task_root = Path(source), Path(workspace)
    created = []
    source_env = source_root / '.venv'
    task_env = task_root / '.venv'
    python = source_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    source_marked = (source_env / 'pyvenv.cfg').is_file()
    if source_marked and restricted and not source_env.resolve().is_relative_to(source_root.resolve()):
        raise ValueError('Project .venv escapes its authorized environment root')
    if source_marked and not task_env.exists():
        if not python.is_file() or not os.access(python, os.X_OK):
            # Preserve the unavailable Python selection in the private tree;
            # unrelated commands still run, Python cannot select server packages.
            task_env.mkdir()
            try:
                shutil.copyfile(source_env / 'pyvenv.cfg', task_env / 'pyvenv.cfg')
            except BaseException:
                shutil.rmtree(task_env)
                raise
            created.append('.venv')
        else:
            try:
                from lib.tools.process_environment import inherited_child_environment
                environment = inherited_child_environment()
                environment['PYTHONNOUSERSITE'] = '1'
                for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'LD_LIBRARY_PATH'):
                    environment.pop(key, None)
                system_site = (['--system-site-packages']
                               if _includes_system_site_packages(source_env) else [])
                subprocess.run([str(python), '-m', 'venv', '--without-pip', *system_site, str(task_env)],
                               env=environment, check=True, capture_output=True, timeout=60)
                code = 'import sysconfig; print(sysconfig.get_path("purelib"))'
                packages = subprocess.run([str(python), '-c', code], env=environment,
                                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()
                local_packages = subprocess.run(
                    [str(task_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')), '-c', code],
                    env=environment, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
                package_root = Path(local_packages)
                if (not package_root.is_relative_to(task_env)
                        or not Path(packages).resolve().is_relative_to(source_env.resolve())):
                    raise ValueError('Workspace Python environment returned an unrelated package directory')
                (package_root / SOURCE_PACKAGES_PTH).write_text(
                    f'import site; site.addsitedir({packages!r})\n', encoding='utf-8')
                if os.name != 'nt':
                    script = '#!/bin/sh\nexec ' + shlex.quote(str(task_env / 'bin/python')) + ' -m pip "$@"\n'
                    for name in ('pip', 'pip3'):
                        pip = task_env / 'bin' / name
                        pip.write_text(script, encoding='utf-8')
                        pip.chmod(0o700)
                created.append('.venv')
            except BaseException:
                if task_env.exists():
                    shutil.rmtree(task_env)
                raise
    elif source_marked and (task_env / 'pyvenv.cfg').is_file():
        _align_system_site_packages(source_env, task_env)
    dependencies = source_root / 'node_modules'
    destination = task_root / 'node_modules'
    if dependencies.is_dir() and not destination.exists():
        # Keep the directory real: conventional node_modules/ ignore rules
        # do not match a root-level symlink. Only installed package entries
        # are links; task-local installs can replace them without replacing
        # the source project's node_modules directory, and the toolchain
        # creates its caches here on first use.
        destination.mkdir()
        for entry in dependencies.iterdir():
            if entry.name not in NODE_TOOL_CACHE_DIRECTORIES:
                (destination / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        created.append('node_modules')
    return created
