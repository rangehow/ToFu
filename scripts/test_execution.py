"""Pytest execution policy, shared by the selector and verification engine.

Entry points: build_pytest_command (the repository suite) and
build_pytest_sessions (one session per test root). This standalone owner uses
only stdlib; selection rules live in test_select.py. Its code is a global
execution input.
Contract: docs/modules/project_integration.md, Merge verification policy and
"Test roots".
"""
from __future__ import annotations

import sys

# Every outermost directory with this name is a test root. The root's parent is
# its package directory: '' (the repository) for tests/, else a package or
# bundle whose suite pytest runs from there, under that directory's own
# configuration and imports.
TEST_DIRECTORY = 'tests'


def _parallel_workers(existing: list[str] | None,
                      jobs: str | None) -> str | None:
    """Return the requested/adaptive xdist worker count, or ``None``.

    A pytest controller and every worker import the full test bootstrap. For a
    one-file inner loop that startup costs more than the tests, so the implicit
    default stays serial. Larger selections request ``auto``; the repository's
    xdist hook then applies the shared resource probe and selected-file cap. An
    explicit ``--jobs`` remains authoritative.
    """
    if jobs is not None:
        return None if jobs == '0' else jobs
    if existing is not None and len(existing) <= 1:
        return None
    return 'auto'


def build_pytest_command(existing: list[str] | None,
                         jobs: str | None) -> list[str]:
    """Build the hermetic selected-test command.

    ``main`` disables pytest entry-point autoload to keep host-only plugins out
    of the inner loop. pyproject.toml explicitly loads xdist and owns the
    ``worksteal`` scheduler, so this command only selects the worker count.
    """
    if existing == []:
        raise ValueError('empty selection must not launch unscoped pytest')
    workers = _parallel_workers(existing, jobs)
    cmd = [sys.executable, '-m', 'pytest', '-p', 'no:napari']
    if existing is not None:
        cmd += existing
    cmd += ['-m', '(unit or api) and not (visual or slow or live_llm)']
    if workers is not None:
        cmd += ['-n', workers]
    cmd += ['--timeout=300', '--tb=short', '-q']
    return cmd


def package_directory(test: str) -> str:
    """The directory pytest runs TEST (a file or node id) from; '' is the repository."""
    parts = test.split('::', 1)[0].split('/')
    if TEST_DIRECTORY not in parts[:-1]:
        raise ValueError(f'not inside a {TEST_DIRECTORY}/ directory: {test}')
    return '/'.join(parts[:parts.index(TEST_DIRECTORY)])


# The files pytest takes its configuration from, in its own lookup order.
PYTEST_CONFIGURATION_FILES = ('pytest.ini', '.pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg')


def session_inputs(test: str) -> set[str]:
    """Configuration files a package TEST's session may read, present or absent.

    pytest uses the first configuration file above its arguments, so every
    candidate between the test and the repository root is an input: editing
    one, or creating one that is absent now, can change the session. The
    repository root's own configuration is already an input of every test
    (test_select.BLAST_RADIUS), so a repository test has none here.
    """
    if not package_directory(test):
        return set()
    parts = test.split('::', 1)[0].split('/')[:-1]
    return {'/'.join([*parts[:end], name]) for end in range(1, len(parts) + 1)
            for name in PYTEST_CONFIGURATION_FILES}


# What a package session loads itself because it has no tests/conftest.py: the
# hang guard its --timeout needs while plugin autoload is off, and the
# priority yield that conftest applies to every repository pytest process.
PACKAGE_SESSION_PLUGINS = ('pytest_timeout', 'tests.support.lower_priority_plugin')


def build_pytest_sessions(tests: list[str], jobs: str | None,
                          repository: str) -> list[tuple[str, list[str]]]:
    """``[(package directory, argv)]``: one session per package, run from that directory.

    The repository suite keeps ``build_pytest_command``. A package session runs
    under the configuration pytest finds from its package directory, which does
    not carry the repository suite's tests/conftest.py, so the session states
    what that conftest otherwise supplies: every test except the opt-in tiers
    (the conftest tags untagged tests ``unit``), PACKAGE_SESSION_PLUGINS, and
    no workers (the ``-n auto`` sizing hook lives in that conftest too).
    ``--rootdir`` keeps node ids repository paths, so results and evidence name
    the same files the inventory does. The caller puts the checkout on
    ``PYTHONPATH`` after the package directory. TESTS are repository paths;
    REPOSITORY is the checkout.
    """
    if not tests:
        raise ValueError('empty selection must not launch unscoped pytest')
    groups: dict[str, list[str]] = {}
    for test in tests:
        package = package_directory(test)
        groups.setdefault(package, []).append(test[len(package) + 1:] if package else test)
    sessions = []
    for package, members in sorted(groups.items()):
        if not package:
            sessions.append((package, build_pytest_command(members, jobs)))
            continue
        command = build_pytest_command(members, '0')
        command[command.index('-m', 3) + 1] = 'not (visual or slow or live_llm)'
        plugins = [argument for plugin in PACKAGE_SESSION_PLUGINS for argument in ('-p', plugin)]
        sessions.append((package, [*command[:5], *plugins, *command[5:], '--rootdir', repository]))
    return sessions
