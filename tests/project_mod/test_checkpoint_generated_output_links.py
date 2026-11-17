"""Task capture never versions a link that stands in for generated frontend output.

A worker that needs a built frontend for its tests may link ``static/vite`` to
another checkout's build. A directory-only ignore rule (``static/vite/``) does
not match a symlink, so ``add -A`` staged the link; landing it then met the
shared checkout's real, ignored build directory ("a directory or special file
is in the way"). The repository's own ignore rules must cover the link.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import integration_control as ic

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=t',
                           '-c', 'user.email=t@example.invalid', *args],
                          check=True, capture_output=True, text=True, timeout=60).stdout


@pytest.mark.parametrize('kind', ['link', 'directory'])
def test_capture_never_stages_generated_vite_output(tmp_path, kind):
    root = tmp_path / 'checkout'
    (root / 'static').mkdir(parents=True)
    shutil.copyfile(REPO / '.gitignore', root / '.gitignore')
    (root / 'static' / 'styles.css').write_text('body {}\n', encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    build = tmp_path / 'shared-build'
    (build / 'assets').mkdir(parents=True)
    (build / 'manifest.json').write_text('{}', encoding='utf-8')
    if kind == 'link':
        os.symlink(build, root / 'static' / 'vite')
    else:
        shutil.copytree(build, root / 'static' / 'vite')
    (root / 'static' / 'new.css').write_text('p {}\n', encoding='utf-8')

    tree = ic._alternate_index_tree(root, git(root, 'rev-parse', 'HEAD').strip(),
                                    str(tmp_path / 'index'))

    paths = set(git(root, 'ls-tree', '-r', '--name-only', tree).split())
    assert 'static/new.css' in paths
    assert not any(path == 'static/vite' or path.startswith('static/vite/') for path in paths)
