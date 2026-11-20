"""Hermetic Google-manifest installation: no real home, vendor call, or executable runs."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tarfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.identity import PrincipalContext
from lib.oauth import subscription_cli_install as install

pytestmark = pytest.mark.unit
OWNER = PrincipalContext.user(subject_id='owner', owner_user_id=1, scopes=('admin',))
URL = 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.2.3/linux-x64/release.tar.gz'


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_AUTH_MODE', 'open')
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    monkeypatch.setattr(install, 'data_root', lambda: str(tmp_path / 'data'))
    monkeypatch.setattr(install, '_platform', lambda: 'linux_amd64')
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        info = tarfile.TarInfo('antigravity')
        info.size = len(b'synthetic binary')
        archive.addfile(info, io.BytesIO(b'synthetic binary'))
    state = {'payload': payload.getvalue(), 'calls': [], 'closed': 0}
    state['manifest'] = {'version': '1.2.3', 'url': URL,
                         'sha512': hashlib.sha512(state['payload']).hexdigest()}

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def iter_content(self, size):
            yield self.body

        def close(self):
            state['closed'] += 1

    def get(url, **options):
        assert options['allow_redirects'] is False and options['stream']
        state['calls'].append(url)
        if url == install._MANIFEST_BASE + 'linux_amd64.json':
            return Response(json.dumps(state['manifest']).encode())
        assert url == URL
        return Response(state['payload'])

    monkeypatch.setattr(install.http, 'http_get', get)
    return state, tmp_path / 'home/.local/bin/agy'


def run():
    return asyncio.run(install.install_subscription_cli('antigravity', OWNER))


def test_install_verified_idempotent_and_atomic(release):
    state, target = release
    result = run()
    assert result == {'provider': 'antigravity', 'status': 'installed',
                      'version': '1.2.3', 'executable': str(target)}
    assert target.read_bytes() == b'synthetic binary'
    assert target.stat().st_mode & 0o777 == 0o700
    assert state['closed'] == 2
    assert run()['status'] == 'already'
    assert len(state['calls']) == 2
    assert list(target.parent.iterdir()) == [target]
    assert list((target.parents[3] / 'data/subscription-install').iterdir())[0].name == 'antigravity.lock'


@pytest.mark.parametrize('kind', ['checksum', 'foreign_url', 'bad_manifest', 'symlink', 'traversal'])
def test_bad_release_never_publishes(release, kind):
    state, target = release
    if kind == 'checksum':
        state['manifest']['sha512'] = '0' * 128
    elif kind == 'foreign_url':
        state['manifest']['url'] = 'https://evil.invalid/install'
    elif kind == 'bad_manifest':
        state['manifest']['sha512'] = None
    else:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w:gz') as archive:
            info = tarfile.TarInfo('antigravity' if kind == 'symlink' else '../antigravity')
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc/passwd'
            archive.addfile(info)
        state['payload'] = payload.getvalue()
        state['manifest']['sha512'] = hashlib.sha512(state['payload']).hexdigest()
    assert run()['status'] == 'failed'
    assert not target.exists()
    assert not target.parent.exists() or not list(target.parent.iterdir())


def test_existing_nonexecutable_is_preserved(release):
    state, target = release
    target.parent.mkdir(parents=True)
    target.write_text('user file')
    assert run()['reason'] == 'destination_exists'
    assert target.read_text() == 'user file'
    assert not state['calls']


def test_concurrent_installs_coalesce_at_destination(release):
    state, _ = release
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: run(), range(2)))
    assert sorted(row['status'] for row in results) == ['already', 'installed']
    assert len(state['calls']) == 2


def test_other_owner_and_multiuser_do_not_download(release, monkeypatch):
    state, _ = release
    other = PrincipalContext.user(subject_id='other', owner_user_id=2, scopes=('admin',))
    assert asyncio.run(install.install_subscription_cli('antigravity', other))['status'] == 'unsupported'
    monkeypatch.setenv('TOFU_AUTH_MODE', 'multi-user')
    assert run()['status'] == 'unsupported'
    assert not state['calls']
