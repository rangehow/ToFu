"""The tofu-agent wheel's manifest reading follows setuptools' own package rules.

``scripts/agent_wheel_manifest.py`` answers per name what setuptools
``find_packages`` (``namespaces = false``) and ``py-modules`` ship, so the
change-based selector and the storage-free probe agree with the build. The
installed-wheel test proves equality with an actual build of this repository;
these specs pin the rules on small projects and refuse unimplemented shapes.
"""
from pathlib import Path

import pytest

from scripts.agent_wheel_manifest import UnsupportedManifest, WheelManifest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/guide.md'}

PYPROJECT = '''
[project]
name = "example"
version = "1"

[tool.setuptools]
py-modules = ["helper"]

[tool.setuptools.packages.find]
namespaces = false
include = ["app*"]
exclude = ["app.private", "app.private.*", "app.generated", "app.tests*"]
'''
FILES = (
    'app/__init__.py', 'app/core.py', 'app/sub/__init__.py', 'app/sub/leaf.py',
    'app/private/__init__.py', 'app/private/secret.py',
    'app/generated/__init__.py', 'app/generated/raw.py',
    'app/generated/kept/__init__.py', 'app/generated/kept/mod.py',
    'app/tests/__init__.py', 'app/tests/deep/__init__.py', 'app/tests/deep/case.py',
    'app/loose/orphan.py', 'other/__init__.py', 'other/thing.py', 'helper.py', 'script.py',
)


def _project(root: Path, pyproject: str = PYPROJECT) -> Path:
    (root / 'pyproject.toml').write_text(pyproject, encoding='utf-8')
    for relative in FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')
    return root


def test_modules_follow_the_setuptools_package_walk(tmp_path):
    manifest = WheelManifest.load(_project(tmp_path))

    for shipped in ('app', 'app.core', 'app.sub', 'app.sub.leaf', 'helper',
                    # An exclusion without a wildcard leaves the walk below it running.
                    'app.generated.kept', 'app.generated.kept.mod'):
        assert manifest.ships_module(shipped), shipped
    for left_out in ('app.private', 'app.private.secret', 'app.generated', 'app.generated.raw',
                     # A trailing-wildcard exclusion stops the walk at that package.
                     'app.tests.deep', 'app.tests.deep.case',
                     'app.loose.orphan', 'other', 'other.thing', 'script'):
        assert not manifest.ships_module(left_out), left_out


def test_changed_paths_are_judged_like_the_build_including_deletions(tmp_path):
    manifest = WheelManifest.load(_project(tmp_path))

    for shipped in ('app/core.py', 'app/sub/__init__.py', 'helper.py',
                    'app/removed.py', 'app/retired/module.py'):
        assert manifest.ships_path(shipped), shipped
    for left_out in ('app/private/secret.py', 'app/private/removed.py', 'app/loose/orphan.py',
                     'other/thing.py', 'script.py', 'docs/guide.md', 'app/data.json'):
        assert not manifest.ships_path(left_out), left_out


def test_repository_names_are_the_checkout_root_entries(tmp_path):
    manifest = WheelManifest.load(_project(tmp_path))

    assert manifest.is_repository_name('app.private.secret')
    assert manifest.is_repository_name('script')
    assert not manifest.is_repository_name('json')


@pytest.mark.parametrize('pyproject', [
    '[project]\nname = "x"\n',
    '[project]\nname = "x"\n[tool.setuptools]\npackages = ["app"]\n',
    '[project]\nname = "x"\n[tool.setuptools.packages.find]\ninclude = ["app*"]\n',
    '[project]\nname = "x"\n[tool.setuptools.packages.find]\nwhere = ["src"]\nnamespaces = false\n',
    '[project]\nname = "x"\n[tool.setuptools]\npackage-dir = {"" = "src"}\n'
    '[tool.setuptools.packages.find]\nnamespaces = false\n',
])
def test_manifest_shapes_this_reading_does_not_implement_are_refused(tmp_path, pyproject):
    (tmp_path / 'pyproject.toml').write_text(pyproject, encoding='utf-8')

    with pytest.raises(UnsupportedManifest):
        WheelManifest.load(tmp_path)


def test_a_checkout_that_builds_no_distribution_ships_nothing(tmp_path):
    _project(tmp_path, pyproject='[tool.ruff]\nline-length = 100\n')
    assert not WheelManifest.load(tmp_path).ships_path('app/core.py')
    (tmp_path / 'pyproject.toml').unlink()
    assert not WheelManifest.load(tmp_path).ships_module('app.core')


def test_this_repository_ships_its_agent_runtime_without_its_storage_authorities():
    manifest = WheelManifest.load(ROOT)

    for shipped in ('lib.router.instructions', 'lib.agent_core.storage_json',
                    'tofu_agent.cli', 'chromium_env'):
        assert manifest.ships_module(shipped), shipped
    for left_out in ('lib.storage', 'lib.storage.errors', 'lib.storage_authority_sql',
                     'serverctl_pkg.local_placement', 'routes', 'tests.support.agent_wheel_run'):
        assert not manifest.ships_module(left_out), left_out
