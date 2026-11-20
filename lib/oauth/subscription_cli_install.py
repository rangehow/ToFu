"""Install the Antigravity native CLI from Google's release manifest.

The manifest and SHA512 scheme are published in Google's cli/install.sh.
Downloads use the shared subscription network authority; no remote shell runs.
Accepted installs settle across view retirement and serialize across processes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit

import anyio

from lib.identity import PrincipalContext
from lib.oauth import http
from lib.oauth.antigravity import antigravity_owner_supported
from lib.runtime.runtime_paths import data_root

_MANIFEST_BASE = 'https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/'
INSTALL_DEADLINE_SECONDS = 900
_MANIFEST_LIMIT = 1024 * 1024
_LOCK = threading.Lock()


class InstallFailure(Exception):
    pass


def _platform() -> str | None:
    os_name = {'Linux': 'linux', 'Darwin': 'darwin'}.get(platform.system())
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine())
    if not os_name or not arch:
        return None
    suffix = '_musl' if os_name == 'linux' and any(Path('/lib').glob('libc.musl-*.so.1')) else ''
    return f'{os_name}_{arch}{suffix}'


def _installed() -> Path | None:
    search = [part for part in os.get_exec_path() if os.path.isabs(part)]
    search.append(str(Path.home() / '.local' / 'bin'))
    path = shutil.which('agy', path=os.pathsep.join(search))
    return Path(path) if path else None


def _download(url: str, output, deadline: float, *, limit: int | None = None) -> str:
    if time.monotonic() >= deadline:
        raise InstallFailure('download_timeout')
    response = http.http_get(url, stream=True, allow_redirects=False,
                             timeout=min(60, deadline - time.monotonic()))
    try:
        if response.status_code != 200:
            raise InstallFailure('download_failed')
        digest, size = hashlib.sha512(), 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if time.monotonic() >= deadline:
                raise InstallFailure('download_timeout')
            if limit is not None and size > limit:
                raise InstallFailure('manifest_invalid')
            output.write(chunk)
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        response.close()


def _release(host_platform: str, directory: Path, deadline: float) -> tuple[str, str, str]:
    manifest = directory / 'manifest.json'
    with manifest.open('wb') as output:
        _download(f'{_MANIFEST_BASE}{host_platform}.json', output, deadline, limit=_MANIFEST_LIMIT)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise InstallFailure('manifest_invalid')
    version, url, checksum = (data.get(key) for key in ('version', 'url', 'sha512'))
    if (not isinstance(version, str) or not re.fullmatch(r'[0-9A-Za-z.+_-]{1,64}', version)
            or not isinstance(url, str) or not isinstance(checksum, str)
            or not re.fullmatch(r'[0-9a-fA-F]{128}', checksum)):
        raise InstallFailure('manifest_invalid')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'storage.googleapis.com'
            or not parsed.path.startswith('/antigravity-public/antigravity-cli/')
            or parsed.query or parsed.fragment):
        raise InstallFailure('manifest_invalid')
    return version, url, checksum.lower()


def _publish(payload: Path, target: Path, url: str) -> None:
    # A staging file in the target directory permits atomic no-overwrite
    # publication. Neither archive paths nor symlinks can select a destination.
    with tempfile.NamedTemporaryFile(prefix='.agy-', dir=target.parent) as staged:
        if urlsplit(url).path.endswith('.tar.gz'):
            with tarfile.open(payload, 'r:gz') as archive:
                member = archive.getmember('antigravity')
                if not member.isfile():
                    raise InstallFailure('archive_invalid')
                source = archive.extractfile(member)
                if source is None:
                    raise InstallFailure('archive_invalid')
                with source:
                    shutil.copyfileobj(source, staged)
        else:
            with payload.open('rb') as source:
                shutil.copyfileobj(source, staged)
        staged.flush()
        os.fchmod(staged.fileno(), 0o700)
        os.link(staged.name, target)


def _install(host_platform: str) -> dict:
    import fcntl

    lock = Path(data_root()) / 'subscription-install' / 'antigravity.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, lock.open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        found = _installed()
        if found:
            return {'provider': 'antigravity', 'status': 'already', 'executable': str(found)}
        target = Path.home() / '.local' / 'bin' / 'agy'
        if target.exists() or target.is_symlink():
            raise InstallFailure('destination_exists')
        deadline = time.monotonic() + INSTALL_DEADLINE_SECONDS
        with tempfile.TemporaryDirectory(prefix='antigravity-', dir=lock.parent) as scratch:
            directory = Path(scratch)
            version, url, checksum = _release(host_platform, directory, deadline)
            payload = directory / 'payload'
            with payload.open('wb') as output:
                actual = _download(url, output, deadline)
            if actual != checksum:
                raise InstallFailure('checksum_mismatch')
            target.parent.mkdir(parents=True, exist_ok=True)
            _publish(payload, target, url)
        return {'provider': 'antigravity', 'status': 'installed',
                'executable': str(target), 'version': version}


async def install_subscription_cli(provider: str, principal: PrincipalContext) -> dict:
    if provider != 'antigravity' or not antigravity_owner_supported(principal):
        return {'provider': provider, 'status': 'unsupported'}
    host_platform = _platform()
    if host_platform is None:
        return {'provider': provider, 'status': 'unsupported'}
    try:
        return await anyio.to_thread.run_sync(_install, host_platform)
    except InstallFailure as error:
        return {'provider': provider, 'status': 'failed', 'reason': str(error)}
    except (OSError, ValueError, KeyError, tarfile.TarError):
        return {'provider': provider, 'status': 'failed', 'reason': 'install_failed'}
    except Exception:
        # Transport errors may contain proxy credentials or upstream bodies.
        return {'provider': provider, 'status': 'failed', 'reason': 'download_failed'}
