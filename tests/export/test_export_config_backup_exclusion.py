#!/usr/bin/env python3
"""Personal exports never carry dead plaintext snapshots of the legacy config.

WHY
---
``data/config/server_config.json.bak*`` are snapshots of the legacy config
taken before past cleanups. They hold plaintext provider keys that no runtime
reads, yet personal mode mirrors ``data/`` verbatim, so every self-backup
replicated them onto its destination with the exporter's umask. The live
config, the credential vault and the other runtime configs still travel: a
self-backup must boot with the owner's settings and credentials. Internal and
opensource exports already exclude the whole root ``data/`` tree.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

_LEGACY_BACKUPS = (
    'server_config.json.bak.1783839754',
    'server_config.json.bak-20260727-204050',
    'server_config.json.bak-creator-cleanup-20260831',
)
_CARRIED_CONFIG = (
    'server_config.json',
    'server_config.json.lock',
    'credentials_vault.json',
    'auth_sources.json',
)


@pytest.mark.parametrize('name', _LEGACY_BACKUPS)
def test_personal_export_skips_legacy_config_backups(name):
    assert export._should_exclude(f'data/config/{name}', name, 'personal'), name


@pytest.mark.parametrize('name', _CARRIED_CONFIG)
def test_personal_export_still_carries_live_config_and_credentials(name):
    assert export._should_exclude(f'data/config/{name}', name, 'personal') is None, name


@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_shared_exports_exclude_the_whole_runtime_config(mode):
    for name in _LEGACY_BACKUPS + _CARRIED_CONFIG:
        assert export._should_exclude(f'data/config/{name}', name, mode), (mode, name)


@pytest.mark.skipif(shutil.which('tar') is None, reason='the personal copy pipeline streams through tar')
def test_personal_tar_pipeline_drops_legacy_config_backups(tmp_path):
    """The real copy path (tar excludes + stream) agrees with the predicate."""
    config = tmp_path / 'src' / 'data' / 'config'
    config.mkdir(parents=True)
    for name in _LEGACY_BACKUPS + _CARRIED_CONFIG:
        (config / name).write_text('{}\n', encoding='utf-8')
    dest = tmp_path / 'dest'

    excludes, _preserved = export._build_tar_excludes_for_mode('personal', dest)
    export._stream_tar_copy(tmp_path / 'src', dest, excludes)

    copied = sorted(path.name for path in (dest / 'data' / 'config').iterdir())
    assert copied == sorted(_CARRIED_CONFIG)
