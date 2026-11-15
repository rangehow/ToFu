"""Open publication never carries ancestry the export pipeline did not produce.

Executable form of docs/PUBLICATION_AUDIT.md §4/§6.4: public ``main`` once kept
three pre-pipeline roots under its sanitized baseline. Every Open push now
admits only commits whose whole lineage is pipeline output and passes the
known-value scan, and ``--fresh-root`` replaces a branch with one parentless
root that later incremental runs continue from.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    is_opensource_build(), reason='The private publication toolchain is not shipped')]

TRAILER = '\n\nPublic-Source-Id: ' + 'a' * 24


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def init(repo: Path, *, bare=False) -> Path:
    repo.mkdir(parents=True)
    git(repo, 'init', '-q', *(['--bare'] if bare else ['-b', 'main']))
    if not bare:
        git(repo, 'config', 'user.name', 'Someone')
        git(repo, 'config', 'user.email', 'someone@example.org')
    return repo


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def test_pipeline_commit_on_private_ancestor_is_refused(tmp_path):
    from export_pkg._publish import ExportPublishSafetyError, _verify_public_lineage
    repo = init(tmp_path / 'public')
    private = commit(repo, 'raw private history', {'app.py': 'A = 1\n'})
    commit(repo, 'sanitized baseline' + TRAILER, {'app.py': 'A = 2\n'})
    with pytest.raises(ExportPublishSafetyError, match=private[:12]):
        _verify_public_lineage(repo, 'HEAD')


def test_pipeline_only_lineage_is_admitted(tmp_path):
    from export_pkg._publish import _verify_public_lineage
    repo = init(tmp_path / 'public')
    commit(repo, 'first' + TRAILER, {'app.py': 'A = 1\n'})
    tip = commit(repo, 'second' + TRAILER, {'app.py': 'A = 2\n'})
    assert _verify_public_lineage(repo, 'main') == tip


def test_known_secret_in_an_ancestor_blob_is_refused(tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    known = 'synthetic-known-value-' + 'q' * 16
    pattern = (publish.re.compile(publish.re.escape(known.encode())), 'Known secret')
    monkeypatch.setattr(publish, '_OPENSOURCE_SECRET_PATTERNS', (pattern,))
    repo = init(tmp_path / 'public')
    commit(repo, 'leaky' + TRAILER, {'app.py': f'KEY = "{known}"\n'})
    commit(repo, 'clean' + TRAILER, {'app.py': 'KEY = ""\n'})
    with pytest.raises(publish.ExportPublishSafetyError, match='leak'):
        publish._verify_public_lineage(repo, 'HEAD')


def test_fresh_root_requires_a_single_parentless_commit(tmp_path):
    from export_pkg._publish import ExportPublishSafetyError, _verify_public_lineage
    repo = init(tmp_path / 'public')
    commit(repo, 'first' + TRAILER, {'app.py': 'A = 1\n'})
    commit(repo, 'second' + TRAILER, {'app.py': 'A = 2\n'})
    with pytest.raises(ExportPublishSafetyError, match='parentless'):
        _verify_public_lineage(repo, 'HEAD', fresh_root=True)


def _publish_config(monkeypatch, remote: Path):
    from export_pkg import _publish as publish
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(remote)}]})
    return publish


def test_divergence_ours_merge_cannot_graft_remote_history(tmp_path, monkeypatch):
    """The default non-ff resolution merges the remote tip; old history must not ride along."""
    remote = init(tmp_path / 'remote.git', bare=True)
    old = init(tmp_path / 'old')
    old_tip = commit(old, 'pre-pipeline public history', {'app.py': 'OLD = 1\n'})
    git(old, 'push', '-q', str(remote), 'main')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'app.py').write_text('NEW = 1\n')
    publish = _publish_config(monkeypatch, remote)
    with pytest.raises(publish.ExportPublishSafetyError, match='did not produce'):
        publish._git_push(dest, 'opensource', 'export')
    assert git(remote, 'rev-parse', 'refs/heads/main') == old_tip


def test_fresh_root_replaces_remote_history_and_seeds_the_cursor(tmp_path, monkeypatch):
    from export_pkg._history import (
        PUBLIC_IDENTITY, establish_fresh_public_root, synchronize_public_history)
    remote = init(tmp_path / 'remote.git', bare=True)
    old = init(tmp_path / 'old')
    old_tip = commit(old, 'pre-pipeline public history', {'app.py': 'OLD = 1\n'})
    git(old, 'push', '-q', str(remote), 'main')
    git(old, 'tag', 'v0.1.0')
    git(old, 'push', '-q', str(remote), 'v0.1.0')
    source = init(tmp_path / 'source')
    source_tip = commit(source, 'private work', {'app.py': 'NEW = 1\n', 'VERSION': '1.2.0\n'})
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'app.py').write_text('NEW = 1\n')
    (dest / 'VERSION').write_text('1.2.0\n')
    publish = _publish_config(monkeypatch, remote)

    def builder(public, tree, message):
        return establish_fresh_public_root(source, public, source_tip, tree, message).public_tip

    publish._git_push(dest, 'opensource', 'Fresh public history', is_release=True,
                      fresh_root_builder=builder)
    root = git(remote, 'rev-parse', 'refs/heads/main')
    assert git(remote, 'rev-list', '--count', root) == '1'
    assert git(remote, 'show', '-s', '--format=%P', root) == ''
    assert git(remote, 'show', '-s', '--format=%an <%ae>|%cn <%ce>', root) == \
        '{0} <{1}>|{0} <{1}>'.format(*PUBLIC_IDENTITY)
    assert git(remote, 'rev-parse', 'refs/tags/v1.2.0^{commit}') == root
    assert subprocess.run(['git', 'merge-base', '--is-ancestor', old_tip, root], cwd=remote,
                          capture_output=True).returncode != 0
    assert subprocess.run(['git', 'cat-file', '-e', source_tip], cwd=dest,
                          capture_output=True).returncode != 0

    # The next routine release continues from the root without --history-base.
    next_tip = commit(source, 'more private work', {'bee.py': 'B = 1\n'})
    (dest / 'bee.py').write_text('B = 1\n')
    git(dest, 'add', '-A')
    result = synchronize_public_history(source, dest, next_tip, git(dest, 'write-tree'),
                                        published_tip=root)
    assert git(dest, 'merge-base', '--is-ancestor', root, result.public_tip) == ''
    assert git(dest, 'rev-list', '--max-parents=0', result.public_tip) == root
    publish._verify_public_lineage(dest, result.public_tip)


def test_fresh_root_refuses_a_destination_with_existing_git(tmp_path, monkeypatch):
    remote = init(tmp_path / 'remote.git', bare=True)
    dest = init(tmp_path / 'dest')
    commit(dest, 'stale export' + TRAILER, {'app.py': 'A = 1\n'})
    publish = _publish_config(monkeypatch, remote)
    with pytest.raises(publish.ExportPublishSafetyError, match='without .git'):
        publish._git_push(dest, 'opensource', 'x', fresh_root_builder=lambda *a: pytest.fail('built'))
    assert git(remote, 'for-each-ref') == ''


def test_cli_rejects_fresh_root_outside_open_push(monkeypatch, capsys):
    import export
    for argv in (['--mode', 'opensource', '--fresh-root'],
                 ['--mode', 'internal', '--push', '--fresh-root'],
                 ['--mode', 'opensource', '--push', '--fresh-root', '--history-base', 'HEAD']):
        monkeypatch.setattr(export.sys, 'argv', ['export.py', *argv])
        with pytest.raises(SystemExit):
            export.main()
    assert '--fresh-root' in capsys.readouterr().err


@pytest.mark.parametrize('text', ['under /mnt/' + 'dolphinfs/ssd', 'sets `/mnt/' + 'dolphinfs`'])
def test_internal_mount_is_refused_with_or_without_a_path(text):
    from export_pkg._publish import ExportPublishSafetyError, _verify_opensource_blob
    with pytest.raises(ExportPublishSafetyError):
        _verify_opensource_blob(text.encode(), 'doc.md')
