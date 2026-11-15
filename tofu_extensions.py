"""Locate private-overlay bundles for the public core without importing them.

Responsibility: one answer to "which release bundles are installed", shared by
every consumer of bundle data (provider templates, site packs, search
gateways, gateway profiles). A bundle is a directory whose well-known
subdirectories (``provider_templates/``, ``site_packs/``, ``search_gateways/``,
``gateway_profiles/``, …) hold declarative JSON. Core reads the data; it never
imports overlay code (``docs/PUBLIC_CORE.md``).

Bundle roots, in override order (later wins by key in each consumer):
1. in-tree ``<repo>/bundles/<name>/`` directories of a source checkout, read on
   every call so a dropped-in bundle needs no restart, then
2. roots contributed by installed distributions through the
   ``tofu.bundles`` entry-point group. An entry resolves to a path, or to a
   zero-argument callable returning one. Installing a distribution already
   requires a restart, so this half is read once per process.

Overlay *code* registers through entry points. ``extension_entry_points(group)``
returns the installed distributions' entries for a group, plus the entries an
in-tree bundle declares in its own ``pyproject.toml`` (a source checkout that
carries the overlay without pip-installing it behaves like one that installed
it). Loading such an in-tree entry puts that bundle root on ``sys.path``
first; an installed distribution of the same name wins.

Entry points: ``bundle_roots``, ``bundle_directories``, ``is_bundle_path``,
``extension_entry_points``, ``clear_bundle_cache``. Dependencies: standard
library only, because the stdlib-only bootstrap (``bootstrap_pkg/providers.py``)
shares it.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import os
from pathlib import Path
import sys
import threading
import tomllib

BUNDLE_ENTRY_POINT_GROUP = 'tofu.bundles'
IN_TREE_BUNDLES_DIR = Path(__file__).resolve().parent / 'bundles'

_logger = logging.getLogger(__name__)
_installed_roots: tuple[Path, ...] | None = None
_installed_lock = threading.Lock()


def _in_tree_roots() -> list[Path]:
    try:
        children = sorted(IN_TREE_BUNDLES_DIR.iterdir())
    except OSError:
        return []
    return [child for child in children if child.is_dir()]


def _installed_bundle_roots() -> tuple[Path, ...]:
    global _installed_roots
    with _installed_lock:
        if _installed_roots is not None:
            return _installed_roots
        from importlib.metadata import entry_points
        roots = []
        try:
            entries = sorted(entry_points(group=BUNDLE_ENTRY_POINT_GROUP),
                             key=lambda entry: entry.name)
        except Exception as exc:
            _logger.warning('[Bundles] %s lookup failed: %s', BUNDLE_ENTRY_POINT_GROUP, exc)
            return ()
        for entry in entries:
            try:
                value = entry.load()
                root = Path(os.fspath(value() if callable(value) else value))
            except Exception as exc:
                _logger.warning('[Bundles] bundle %r failed to load: %s', entry.name, exc)
                continue
            if root.is_dir():
                roots.append(root)
            else:
                _logger.warning('[Bundles] bundle %r names a missing directory %s',
                                entry.name, root)
        _installed_roots = tuple(roots)
        return _installed_roots


def bundle_roots() -> list[Path]:
    """Return installed bundle roots in override order, without duplicates."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for root in (*_in_tree_roots(), *_installed_bundle_roots()):
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(root)
    return roots


def bundle_directories(kind: str) -> list[Path]:
    """Return each bundle's existing ``<root>/<kind>/`` directory in order."""
    return [root / kind for root in bundle_roots() if (root / kind).is_dir()]


def is_bundle_path(path: str | os.PathLike) -> bool:
    """Whether ``path`` lies inside an installed bundle root."""
    candidate = Path(path).resolve()
    return any(candidate.is_relative_to(root.resolve()) for root in bundle_roots())


def clear_bundle_cache() -> None:
    """Forget entry-point roots; for tests and embedders that swap loaders."""
    global _installed_roots
    with _installed_lock:
        _installed_roots = None


@dataclass(frozen=True)
class InTreeEntryPoint:
    """An entry point an in-tree bundle declares; loads like ``importlib.metadata.EntryPoint``."""

    name: str
    value: str
    group: str
    root: Path

    def load(self):
        root = str(self.root)
        if root not in sys.path:
            sys.path.insert(0, root)
        module_name, _, attribute = self.value.partition(':')
        target = importlib.import_module(module_name.strip())
        for part in filter(None, attribute.strip().split('.')):
            target = getattr(target, part)
        return target


def _in_tree_distributions() -> list[tuple[str, Path, dict]]:
    """``(distribution name, bundle root, entry-points table)`` of in-tree bundles."""
    found = []
    for root in _in_tree_roots():
        manifest = root / 'pyproject.toml'
        if not manifest.is_file():
            continue
        try:
            project = tomllib.loads(manifest.read_text(encoding='utf-8')).get('project') or {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _logger.warning('[Bundles] unreadable %s: %s', manifest, exc)
            continue
        found.append((str(project.get('name') or root.name), root,
                      project.get('entry-points') or {}))
    return found


def _normalized(name: str) -> str:
    return name.lower().replace('_', '-').replace('.', '-')


def extension_entry_points(group: str) -> list:
    """Installed entries of ``group`` followed by in-tree bundles' entries, by name."""
    from importlib.metadata import entry_points
    try:
        installed = list(entry_points(group=group))
    except Exception as exc:
        _logger.warning('[Bundles] %s lookup failed: %s', group, exc)
        installed = []
    installed_distributions = set()
    for entry in installed:
        distribution = getattr(entry, 'dist', None)
        if distribution is not None:
            installed_distributions.add(_normalized(distribution.metadata['Name'] or ''))
    in_tree = [
        InTreeEntryPoint(name, value, group, root)
        for distribution, root, table in _in_tree_distributions()
        if _normalized(distribution) not in installed_distributions
        for name, value in sorted((table.get(group) or {}).items())
    ]
    return sorted(installed, key=lambda entry: entry.name) + in_tree


__all__ = [
    'BUNDLE_ENTRY_POINT_GROUP', 'IN_TREE_BUNDLES_DIR', 'InTreeEntryPoint', 'bundle_directories',
    'bundle_roots', 'clear_bundle_cache', 'extension_entry_points', 'is_bundle_path',
]
