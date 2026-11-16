"""An installed first-party distribution runs the checkout's code and metadata.

A snapshot install of a distribution that imports application modules breaks
once the checkout moves one of them (compute_catalog(provider=hope) on
2026-11-12: ``No module named 'lib.runtime_paths'``). Each case runs in its own
interpreter because serving a distribution changes ``sys.path`` and
``sys.meta_path`` for the whole process.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MEMBER = 'bundles/fixture/provider'
GROUP = 'tofu.fixture_providers'


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture
def environment(tmp_path):
    """A stale installed copy (moved application import, old entry point) and a newer checkout member."""
    site = tmp_path / 'site-packages'
    _write(site / 'tofu_fixture_provider/__init__.py', 'from lib.moved_away import data_root\n')
    _write(site / 'tofu_fixture_provider-0.1.0.dist-info/METADATA',
           'Metadata-Version: 2.1\nName: tofu-fixture-provider\nVersion: 0.1.0\n')
    _write(site / 'tofu_fixture_provider-0.1.0.dist-info/entry_points.txt',
           f'[{GROUP}]\nlegacy = tofu_fixture_provider:factory\n')
    checkout = tmp_path / 'checkout'
    _write(checkout / MEMBER / 'pyproject.toml', f'''
        [project]
        name = "tofu-fixture-provider"
        version = "0.2.0"

        [project.entry-points."{GROUP}"]
        current = "tofu_fixture_provider:factory"
        ''')
    _write(checkout / MEMBER / 'tofu_fixture_provider/__init__.py',
           'def factory():\n    return "checkout"\n')
    return site, checkout


def _python(code):
    completed = subprocess.run([sys.executable, '-I', '-c', code],
                               capture_output=True, text=True, timeout=120)
    return completed


def _serve(site, checkout, report, path=()):
    prefix = [str(ROOT), *map(str, path), str(site)]
    code = textwrap.dedent(f'''
        import json, sys
        sys.path[0:0] = {prefix!r}
        from importlib.metadata import entry_points, version
        from lib.runtime.checkout_distributions import serve_installed_from_checkout
        roots = serve_installed_from_checkout({str(checkout)!r}, {{'tofu-fixture-provider': {MEMBER!r}}})
        ''') + textwrap.dedent(report)
    completed = _python(code)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_control_installed_copy_alone_fails_on_a_moved_application_module(environment):
    site, _ = environment
    completed = _python(f'import sys; sys.path[0:0] = {[str(ROOT), str(site)]!r}; import tofu_fixture_provider')
    assert "No module named 'lib.moved_away'" in completed.stderr


def test_installed_copy_is_replaced_by_checkout_code_and_metadata(environment):
    site, checkout = environment
    result = _serve(site, checkout, f'''
        import tofu_fixture_provider
        print(json.dumps({{"roots": roots, "version": version("tofu-fixture-provider"),
            "entry_points": {{ep.name: ep.load()() for ep in entry_points(group={GROUP!r})}},
            "module": tofu_fixture_provider.__file__}}))
        ''')
    assert result == {
        'roots': [str(checkout / MEMBER)], 'version': '0.2.0',
        'entry_points': {'current': 'checkout'},
        'module': str(checkout / MEMBER / 'tofu_fixture_provider/__init__.py')}


def test_checkout_member_without_an_install_is_not_offered(environment, tmp_path):
    _, checkout = environment
    # A build left in the member directory is not an installation either.
    _write(checkout / MEMBER / 'tofu_fixture_provider.egg-info/PKG-INFO',
           'Metadata-Version: 2.1\nName: tofu-fixture-provider\nVersion: 0.0.1\n')
    empty = tmp_path / 'empty-site'
    empty.mkdir()
    result = _serve(empty, checkout, f'''
        print(json.dumps({{"roots": roots,
            "entry_points": [ep.name for ep in entry_points(group={GROUP!r})]}}))
        ''')
    assert result == {'roots': [], 'entry_points': []}
    stray = _serve(empty, checkout, 'print(json.dumps({"roots": roots}))', path=[checkout / MEMBER])
    assert stray == {'roots': []}


def test_declared_members_describe_their_tracked_pyproject():
    import tomllib
    from lib.runtime.checkout_distributions import CHECKOUT_DISTRIBUTIONS, _CheckoutDistribution
    assert {'tofu-environments', 'tofu-meituan-environments'} <= set(CHECKOUT_DISTRIBUTIONS)
    for name, relative in CHECKOUT_DISTRIBUTIONS.items():
        if not (ROOT / relative).is_dir():
            # Only a release-tier overlay bundle may be absent (public core).
            assert relative.startswith('bundles/'), relative
            continue
        project = tomllib.loads((ROOT / relative / 'pyproject.toml').read_text())['project']
        distribution = _CheckoutDistribution(str(ROOT / relative), name)
        assert distribution.version == project['version']
        groups = {**project['entry-points'], 'console_scripts': project.get('scripts', {})}
        assert {(ep.group, ep.name, ep.value) for ep in distribution.entry_points} == {
            (group, key, value) for group, entries in groups.items() for key, value in entries.items()}
