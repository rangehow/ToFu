"""Which modules the tofu-agent wheel ships, read from the build's own package manifest.

Responsibility
--------------
Answer "does the wheel ship this module / this repository path?" from
``pyproject.toml`` exactly as the build computes its package list: setuptools
``find_packages`` over ``[tool.setuptools.packages.find]`` and the top-level
``[tool.setuptools] py-modules``. With ``namespaces = false`` a package is a
directory holding ``__init__.py`` that the walk reaches through packages;
``include``/``exclude`` are fnmatch patterns over the dotted name, setuptools
always excludes ``ez_setup`` and ``*__pycache__``, and a literal
``<package>*`` or ``<package>.*`` exclusion stops the walk below that package.
Every ``.py`` file directly inside a shipped package ships with it.

The answers are per name (a few ``stat`` calls), never a tree walk, so they
hold on the FUSE checkout, and the module is standard library only: the
change-based selector (``scripts/test_select.py``) and the storage-free probe's
child process load it by path. A manifest shape this module does not implement
(another ``where``, ``package-dir``, namespace packages, an explicit package
list) is refused, never approximated.

Entry points: ``WheelManifest.load(root)``, then ``ships_module``,
``ships_path`` and ``is_repository_name``. Specs:
``tests/meta/test_agent_wheel_manifest.py``; the installed-wheel test asserts
these answers equal the modules of the wheel it builds.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import os
import tomllib

# setuptools.discovery.PackageFinder.ALWAYS_EXCLUDE
ALWAYS_EXCLUDE = ('ez_setup', '*__pycache__')
_FIND_KEYS = frozenset({'where', 'include', 'exclude', 'namespaces'})
_LAYOUT_KEYS = ('package-dir', 'namespace-packages')


class UnsupportedManifest(ValueError):
    """The package manifest uses a shape this reading does not implement."""


class WheelManifest:
    """The build's package rules for one checkout ROOT."""

    def __init__(self, root, include=(), exclude=(), py_modules=()):
        self.root = os.path.abspath(root)
        self.include = tuple(include)
        self.exclude = ALWAYS_EXCLUDE + tuple(exclude)
        self.py_modules = frozenset(py_modules)

    @classmethod
    def load(cls, root) -> 'WheelManifest':
        path = os.path.join(root, 'pyproject.toml')
        if not os.path.isfile(path):
            return cls(root)  # No project file, no wheel.
        with open(path, 'rb') as stream:
            project = tomllib.load(stream)
        if 'project' not in project:
            return cls(root)  # Not a Python distribution.
        config = project.get('tool', {}).get('setuptools', {})
        layout = [key for key in _LAYOUT_KEYS if key in config]
        if layout:
            raise UnsupportedManifest(f'[tool.setuptools] {layout[0]} is not implemented')
        packages = config.get('packages')
        if not isinstance(packages, dict) or set(packages) != {'find'}:
            raise UnsupportedManifest(
                '[tool.setuptools] packages must be a find table (automatic discovery '
                'and explicit package lists are not implemented)')
        find = packages['find']
        if set(find) - _FIND_KEYS:
            raise UnsupportedManifest(f'packages.find keys {sorted(set(find) - _FIND_KEYS)}')
        if find.get('where', ['.']) not in (['.'], '.'):
            raise UnsupportedManifest('packages.find where must be the project root')
        if find.get('namespaces', True) is not False:
            raise UnsupportedManifest('packages.find namespaces must be false')
        return cls(root, find.get('include', ['*']), find.get('exclude', ()),
                   config.get('py-modules', ()))

    def _selects(self, package: str) -> bool:
        return (any(fnmatchcase(package, pattern) for pattern in self.include)
                and not any(fnmatchcase(package, pattern) for pattern in self.exclude))

    def _prunes(self, package: str) -> bool:
        return f'{package}*' in self.exclude or f'{package}.*' in self.exclude

    def _reached(self, parts: list[str]) -> bool:
        """The walk descends through every proper ancestor of PARTS."""
        return all(not self._prunes('.'.join(parts[:depth])) for depth in range(1, len(parts)))

    def ships_package(self, name: str) -> bool:
        parts = name.split('.')
        if not all(parts) or not self._reached(parts):
            return False
        return (all(os.path.isfile(os.path.join(self.root, *parts[:depth], '__init__.py'))
                    for depth in range(1, len(parts) + 1))
                and self._selects(name))

    def ships_module(self, name: str) -> bool:
        if name in self.py_modules or self.ships_package(name):
            return True
        package, _, leaf = name.rpartition('.')
        return bool(package) and self.ships_package(package) and os.path.isfile(
            os.path.join(self.root, *package.split('.'), leaf + '.py'))

    def ships_path(self, path: str) -> bool:
        """Whether the repository-relative PATH is, or before its deletion was, a shipped module."""
        if not path.endswith('.py'):
            return False
        parts = path[:-3].split('/')
        if len(parts) == 1:
            return parts[0] in self.py_modules
        package = parts[:-1]
        if self.ships_package('.'.join(package)):
            return True
        if os.path.exists(os.path.join(self.root, path)):
            return False
        # A deleted module or package can no longer be inspected: its old path is
        # judged by the manifest's patterns alone, so a deletion never escapes.
        return all(package) and self._reached(package) and self._selects('.'.join(package))

    def is_repository_name(self, name: str) -> bool:
        """Whether NAME's top level is a module or directory at the checkout root."""
        top = name.partition('.')[0]
        return (os.path.isdir(os.path.join(self.root, top))
                or os.path.isfile(os.path.join(self.root, top + '.py')))


__all__ = ['ALWAYS_EXCLUDE', 'UnsupportedManifest', 'WheelManifest']
