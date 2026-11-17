"""Structure rules of scripts/check_architecture.py: one project-registry owner, no new import cycles.

docs/ARCHITECTURE.md "Code structure: single owners and measured baseline": the cleanup removed a
duplicated "is this a registered project?" answer and froze the two remaining
module-level import cycles; these rules keep both from regressing.
"""
from __future__ import annotations

# Synthetic module paths of the fixture trees below, never repository files.
_AUDIT_SYNTHETIC_REPO_PATHS = (
    'lib/a.py', 'lib/b.py', 'lib/c.py', 'lib/d.py', 'lib/e.py', 'lib/f.py',
    'lib/feature/admit.py',
)

import pytest

from scripts import check_architecture as architecture
from scripts.code_structure_metrics import _eager_graph, _sccs

pytestmark = pytest.mark.unit


def test_reading_recent_projects_or_folders_outside_their_owners_is_rejected(tmp_path):
    (tmp_path / 'lib' / 'feature').mkdir(parents=True)
    (tmp_path / 'lib' / 'project_mod').mkdir(parents=True)
    rogue = tmp_path / 'lib' / 'feature' / 'admit.py'
    rogue.write_text('from lib.project_mod.config import get_recent_projects\n'
                     'from lib.conversations.folders import owner_folders\n')
    owner = tmp_path / 'lib' / 'project_mod' / 'config.py'
    owner.write_text('def get_recent_projects(*, user_id): ...\n')
    violations = architecture.project_registry_owner_violations([rogue, owner], root=tmp_path)
    assert [v.split(':')[:2] for v in violations] == [['lib/feature/admit.py', '1'],
                                                       ['lib/feature/admit.py', '2']]
    assert 'lib.router.registered_projects' in violations[0]


def test_the_repository_has_one_project_registry_owner():
    assert architecture.project_registry_owner_violations(architecture.authored_sources()) == []


def _cycles(modules: dict[str, str], tmp_path) -> list[list[str]]:
    import ast
    return _sccs(_eager_graph({name: ast.parse(source) for name, source in modules.items()}))


def test_import_cycles_are_detected_at_load_time_only(tmp_path):
    cycles = _cycles({
        'lib/a.py': 'from lib import b\n',
        'lib/b.py': 'import lib.a\n',
        'lib/c.py': 'def f():\n    from lib import d\n',
        'lib/d.py': 'from lib import c\n',
        'lib/e.py': 'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from lib import f\n',
        'lib/f.py': 'from lib import e\n',
    }, tmp_path)
    assert cycles == [['lib.a', 'lib.b']]


def test_only_the_frozen_import_cycles_may_exist():
    frozen = (frozenset({'lib.a', 'lib.b', 'lib.c'}),)
    assert architecture.import_cycle_violations([['lib.a', 'lib.b', 'lib.c']], frozen=frozen) == []
    [new] = architecture.import_cycle_violations([['lib.a', 'lib.b', 'lib.c'], ['lib.x', 'lib.y']],
                                                 frozen=frozen)
    assert new.startswith('new module-level import cycle: lib.x -> lib.y')
    [grown] = architecture.import_cycle_violations([['lib.a', 'lib.b', 'lib.c', 'lib.z']], frozen=frozen)
    assert grown.startswith('new module-level import cycle')
    [shrank] = architecture.import_cycle_violations([['lib.a', 'lib.b']], frozen=frozen)
    assert 'tighten _FROZEN_IMPORT_CYCLES' in shrank
    [gone] = architecture.import_cycle_violations([], frozen=frozen)
    assert 'is gone' in gone


def test_the_repository_import_cycles_match_the_frozen_baseline():
    assert architecture.import_cycle_violations() == []
