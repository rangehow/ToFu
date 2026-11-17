"""A landing that changes the frontend's inputs rebuilds the bundle off the restart path.

Audit 2026-11-18 #12 (P4): a 01:48 landing changed locale sources; the 02:09
restart's preflight found "Vite i18n chunks are stale" and rebuilt for 106.6 s
before anything stopped. ``checkout_landing.publish`` now starts the checkout's
own frontend preparation in the background as soon as such a landing is
published, so the restart preflight finds the bundle valid.
"""
import json
from pathlib import Path
import subprocess

import pytest

from lib.project_mod import frontend_refresh

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def test_only_vite_authoring_inputs_select_a_refresh():
    landed = ['frontend/src/i18n/locales/zh.json', 'frontend/src/runtime/sections/chat.js',
              'package-lock.json', 'vite.config.mjs', 'frontend/authoring-inputs.json',
              'lib/tasks_pkg/context_composer/registry.py',
              'lib/conversations/restart_resume.py', 'frontend/src/notes.md', 'docs/README.md']
    assert frontend_refresh.changed_authoring_inputs(REPO, landed) == landed[:6]


def _checkout(tmp_path):
    """A Tofu-shaped checkout whose serverctl records how it was started."""
    checkout = tmp_path / 'checkout'
    (checkout / 'frontend').mkdir(parents=True)
    (checkout / 'frontend' / 'authoring-inputs.json').write_bytes(
        (REPO / 'frontend' / 'authoring-inputs.json').read_bytes())
    record = tmp_path / 'started.json'
    (checkout / 'serverctl.py').write_text(
        'import json, os, sys\n'
        f'open({str(record)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), '
        '"role": os.environ.get("TOFU_PROCESS_ROLE"), "sid": os.getsid(0) == os.getpid()}))\n')
    return checkout, record


def test_a_frontend_landing_starts_the_checkouts_own_preparation_detached(tmp_path, monkeypatch):
    checkout, record = _checkout(tmp_path)
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'execution')
    monkeypatch.setattr('lib.runtime.runtime_paths.logs_root', lambda: str(tmp_path))
    process = frontend_refresh.refresh_after_landing(
        checkout, ['lib/x.py', 'frontend/src/i18n/locales/en.json'])
    assert process is not None and process.wait(60) == 0
    started = json.loads(record.read_text())
    assert started['argv'] == ['prepare-frontend', '--parts', 'frontend',
                               '--operation', frontend_refresh.OPERATION]
    assert started['cwd'] == str(checkout)
    # The checkout's deployment (.env) decides, not the landing process's role.
    assert started['role'] is None
    # Its own session: it outlives the landing process and its process group.
    assert started['sid'] is True


@pytest.mark.parametrize('paths', [['lib/x.py', 'docs/README.md'], []])
def test_a_landing_that_changes_no_frontend_input_starts_nothing(tmp_path, paths):
    checkout, record = _checkout(tmp_path)
    assert frontend_refresh.refresh_after_landing(checkout, paths) is None
    assert not record.exists()


def test_another_project_never_starts_a_tofu_build(tmp_path):
    checkout, record = _checkout(tmp_path)
    (checkout / 'serverctl.py').unlink()
    assert frontend_refresh.refresh_after_landing(
        checkout, ['frontend/src/i18n/locales/en.json']) is None


def test_publishing_a_landing_starts_the_refresh_with_the_landed_paths(tmp_path, monkeypatch):
    """The shared publication owner (git_land and reviewed deliveries) triggers it."""
    from lib.project_mod import checkout_landing
    top = tmp_path / 'repo'
    top.mkdir()
    git = ['git', '-C', str(top), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    subprocess.run([*git, 'init', '-q', '-b', 'main'], check=True)
    (top / 'frontend' / 'src').mkdir(parents=True)
    (top / 'frontend' / 'src' / 'a.ts').write_text('one\n')
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-qm', 'base'], check=True)
    head = subprocess.run([*git, 'rev-parse', 'HEAD'], check=True, capture_output=True,
                          text=True).stdout.strip()
    subprocess.run([*git, 'checkout', '-qb', 'work'], check=True)
    (top / 'frontend' / 'src' / 'a.ts').write_text('two\n')
    subprocess.run([*git, 'commit', '-qam', 'change'], check=True)
    commit = subprocess.run([*git, 'rev-parse', 'HEAD'], check=True, capture_output=True,
                            text=True).stdout.strip()
    subprocess.run([*git, 'checkout', '-q', 'main'], check=True)
    tree = subprocess.run([*git, 'rev-parse', commit + '^{tree}'], check=True, capture_output=True,
                          text=True).stdout.strip()
    started = []
    monkeypatch.setattr(frontend_refresh, 'refresh_after_landing',
                        lambda checkout, paths: started.append((Path(checkout), list(paths))))
    steps = checkout_landing.plan(top, head, tree, True)
    checkout_landing.publish(top, 'refs/heads/main', head, commit, steps, 'test landing')
    assert (top / 'frontend' / 'src' / 'a.ts').read_text() == 'two\n'
    assert started == [(top, ['frontend/src/a.ts'])]
