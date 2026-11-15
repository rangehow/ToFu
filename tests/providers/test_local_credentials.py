"""Declared local CLI credentials (lib/provider/local_credentials.py).

Only installed bundles declare a source; the key is validated server-side and
never returned. Every source here is synthetic (the CatPaw declaration is
pinned in bundles/yourprovider/tests/test_catpaw_local_import.py).
"""
from __future__ import annotations

import io
import json
from unittest import mock

import pytest

import tofu_extensions
from lib.provider import local_credentials as lc

pytestmark = pytest.mark.unit

TOKEN = 'b' * 24


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / 'bundles' / 'acme' / 'local_credentials'
    root.mkdir(parents=True)
    monkeypatch.setattr(tofu_extensions, 'IN_TREE_BUNDLES_DIR', tmp_path / 'bundles')
    monkeypatch.setattr(tofu_extensions, '_installed_roots', ())
    return root


def _declare(directory, name, **overrides):
    document = {'contract': lc.LOCAL_CREDENTIAL_CONTRACT, 'template_key': 'acme',
                'config_path': str(directory.parent / 'cli.json'), 'format': 'json',
                'field': 'token', 'token_pattern': '^[a-z]{24}$',
                'cli': {'command': ['acme-cli', 'login'], 'login_url_pattern': r'https://\S+/login\S*'},
                **overrides}
    (directory / f'{name}.json').write_text(json.dumps(document))


def test_nothing_is_available_without_a_declaring_bundle(bundle):
    assert lc.local_credential_source('acme') is None
    assert lc.detect_local_key(None) == {'available': False}
    assert lc.cli_installed(None) is False
    assert lc.kick_cli_login(None) == {'started': False, 'installed': False, 'login_url': ''}
    with pytest.raises(lc.LocalCredentialError) as caught:
        lc.read_local_key(None)
    assert caught.value.reason == 'not_declared'


def test_declared_source_reads_and_validates_the_key(bundle):
    _declare(bundle, 'acme')
    _declare(bundle, 'broken', format='toml')
    source = lc.local_credential_source('acme')
    config = bundle.parent / 'cli.json'
    with pytest.raises(lc.LocalCredentialError) as caught:
        lc.read_local_key(source)
    assert caught.value.reason == 'cli_config_missing'
    config.write_text('{not json')
    with pytest.raises(lc.LocalCredentialError) as caught:
        lc.read_local_key(source)
    assert caught.value.reason == 'cli_config_unreadable'
    config.write_text(json.dumps({'token': 'SHORT'}))
    assert lc.detect_local_key(source) == {'available': False}
    config.write_text(json.dumps({'token': TOKEN}))
    assert lc.read_local_key(source) == TOKEN
    assert lc.detect_local_key(source) == {'available': True}


def test_secret_envelope_shape():
    envelope = json.loads(lc.secret_envelope(TOKEN))
    assert envelope == {'format': 'tofu.credential-secret/v1', 'api_key': TOKEN,
                        'oauth': '', 'extra_headers': {}}


def test_login_kick_runs_only_the_declared_command(bundle):
    _declare(bundle, 'acme')
    source = lc.local_credential_source('acme')

    class _Proc:
        stdout = io.StringIO('visit https://acme.example.test/login?uuid=1 now\n')

    with mock.patch.object(lc.shutil, 'which', return_value='/usr/bin/acme-cli'), \
            mock.patch.object(lc.subprocess, 'Popen', return_value=_Proc()) as popen:
        result = lc.kick_cli_login(source, timeout_s=1.0)
    assert popen.call_args.args[0] == ['acme-cli', 'login']
    assert result == {'started': True, 'installed': True,
                      'login_url': 'https://acme.example.test/login?uuid=1'}
    with mock.patch.object(lc.shutil, 'which', return_value=None):
        assert lc.kick_cli_login(source) == {'started': False, 'installed': False, 'login_url': ''}
