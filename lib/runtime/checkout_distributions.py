"""Installed first-party distributions load from the checkout that boots Tofu.

Responsibility: ``pip install ./packages/...`` copies a first-party
distribution into the interpreter environment, and nothing refreshes that copy
when the checkout changes. Distributions that lazily import application modules
then fail at first use. On 2026-11-12 ``compute_catalog(provider=hope)`` raised
``ModuleNotFoundError: No module named 'lib.runtime_paths'`` from a
tofu-meituan-environments 0.1.0 copy installed on 2026-10-11, long after the
checkout had moved that module and fixed the provider.

Installation stays the opt-in: only a distribution installed in this
interpreter is served, so a checkout never enables a provider by itself. Its
member directory goes ahead of the installed copy on ``sys.path``, and a
metadata finder ahead of ``PathFinder`` describes it with the name, version and
entry points of the tracked ``pyproject.toml``, read once at boot like the
pinned code. Rationale: docs/ENVIRONMENT_PROVIDERS.md, "Installation and opt-in".

Entry points: ``serve_installed_from_checkout`` (server.py, before the pinned
import source is installed) and ``installed_checkout_roots`` (the execution
plan's source identity). Dependencies: standard library only.
"""
from __future__ import annotations

from collections.abc import Mapping
import importlib
import importlib.machinery
import importlib.metadata
import os
from pathlib import Path
import re
import sys
import tomllib

# Canonical distribution name -> member directory in the checkout. Declare
# each first-party distribution whose code imports application modules.
CHECKOUT_DISTRIBUTIONS: Mapping[str, str] = {
    'tofu-environments': 'packages/tofu-environments',
    'tofu-meituan-environments': 'bundles/meituan/environment-provider',
}


def _canonical(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def installed_checkout_roots(project, distributions: Mapping[str, str] = CHECKOUT_DISTRIBUTIONS) -> tuple[str, ...]:
    """Member directories of declared distributions installed in this interpreter."""
    roots = {name: os.path.join(os.path.abspath(project), relative)
             for name, relative in distributions.items()}
    roots = {name: root for name, root in roots.items()
             if os.path.isfile(os.path.join(root, 'pyproject.toml'))}
    members = set(roots.values())
    # A member directory may hold a stray build's *.egg-info; that is not an install.
    elsewhere = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) not in members]
    return tuple(root for name, root in roots.items()
                 if next(iter(importlib.metadata.distributions(name=name, path=elsewhere)), None))


class _CheckoutDistribution(importlib.metadata.Distribution):
    """Metadata of one member, derived from its tracked pyproject.toml."""

    def __init__(self, root: str, declared: str) -> None:
        path = os.path.join(root, 'pyproject.toml')
        with open(path, 'rb') as handle:
            project = tomllib.load(handle).get('project') or {}
        if _canonical(str(project.get('name', ''))) != _canonical(declared) or 'version' not in project:
            raise ValueError(f'{path} must declare [project] name {declared!r} and a static version')
        groups = dict(project.get('entry-points') or {})
        for table, group in (('scripts', 'console_scripts'), ('gui-scripts', 'gui_scripts')):
            if project.get(table):
                groups[group] = project[table]
        self._root = root
        self._files = {
            'METADATA': f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {project['version']}\n",
            'entry_points.txt': ''.join(
                f'[{group}]\n' + ''.join(f'{key} = {value}\n' for key, value in entries.items()) + '\n'
                for group, entries in groups.items()),
        }

    def read_text(self, filename):
        return self._files.get(filename)

    def locate_file(self, path):
        return Path(self._root) / path


class _CheckoutDistributionFinder(importlib.metadata.DistributionFinder):
    """Offers member metadata wherever a search includes the member directory."""

    def __init__(self, distributions: Mapping[str, _CheckoutDistribution]) -> None:
        self._distributions = dict(distributions)

    @staticmethod
    def find_spec(fullname, path=None, target=None):
        return None

    def find_distributions(self, context=None):
        if context is None:
            context = importlib.metadata.DistributionFinder.Context()
        searched = {os.path.abspath(entry or os.curdir) for entry in context.path}
        wanted = None if context.name is None else _canonical(context.name)
        for root, distribution in self._distributions.items():
            if root in searched and wanted in (None, _canonical(distribution.metadata['Name'])):
                yield distribution


def serve_installed_from_checkout(project, distributions: Mapping[str, str] = CHECKOUT_DISTRIBUTIONS) -> tuple[str, ...]:
    """Serve installed members' code and metadata from ``project``; return their roots."""
    roots = installed_checkout_roots(project, distributions)
    declared = {os.path.join(os.path.abspath(project), relative): name
                for name, relative in distributions.items()}
    finder = _CheckoutDistributionFinder(
        {root: _CheckoutDistribution(root, declared[root]) for root in roots})
    if roots:
        sys.path[0:0] = [root for root in roots if root not in sys.path]
        position = next((index for index, item in enumerate(sys.meta_path)
                         if item is importlib.machinery.PathFinder), len(sys.meta_path))
        sys.meta_path.insert(position, finder)
        importlib.invalidate_caches()
    return roots
