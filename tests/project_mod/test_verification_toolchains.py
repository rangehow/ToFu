"""Canonical isolated verification honors the frozen project's persistent tools.
No launcher PATH or production runtime cache is inherited.
"""
import json
import os
import shutil
import sys

import pytest

from lib.project_mod import landing_verification as verify
from lib.project_mod import toolchains

pytestmark = pytest.mark.unit


def registry(tmp_path, monkeypatch):
    # Synthetic test installation only. Production rejects temporary locations.
    monkeypatch.setattr(toolchains, 'is_temporary_location', lambda path: False)
    root = tmp_path / 'project'; root.mkdir()
    install = tmp_path / 'persistent-install'; (install / 'bin').mkdir(parents=True)
    for program in ('latexmk', 'pdftotext'):
        binary = install / 'bin' / program
        binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
    config = {'version': 1, 'toolchains': {'document-tools': {
        'location': str(install), 'path': ['bin'], 'env': {'TEXBIN': '{location}/bin'},
        'provides': ['latexmk', 'pdftotext'], 'install': 'documented test installation'}}}
    (root / toolchains.REGISTRY_FILENAME).write_text(json.dumps(config))
    return root, install, config


def test_registered_tools_reach_isolated_verification_not_launcher_path(tmp_path, monkeypatch):
    root, install, config = registry(tmp_path, monkeypatch)
    monkeypatch.setenv('PATH', '/unrelated/launcher')
    first = verify._test_environment(root)
    assert shutil.which('latexmk', path=first['PATH']) == str(install / 'bin/latexmk')
    assert shutil.which('pdftotext', path=first['PATH']) == str(install / 'bin/pdftotext')
    assert first['TEXBIN'] == str(install / 'bin')
    monkeypatch.setenv('PATH', '/another/launcher')
    assert verify._test_environment(root) == first
    assert '/unrelated/launcher' not in first['PATH'].split(os.pathsep)


def test_toolchain_configuration_invalidates_verification_key(tmp_path, monkeypatch):
    root, install, config = registry(tmp_path, monkeypatch)
    key = verify._environment_key(sys.executable, root)
    config['toolchains']['document-tools']['env']['TEXBIN'] = '{location}/updated'
    (root / toolchains.REGISTRY_FILENAME).write_text(json.dumps(config))
    assert verify._environment_key(sys.executable, root) != key


def test_missing_install_is_visible_not_inherited_from_launcher(tmp_path, monkeypatch):
    root, install, config = registry(tmp_path, monkeypatch)
    config['toolchains']['document-tools']['location'] = str(tmp_path / 'not-installed')
    (root / toolchains.REGISTRY_FILENAME).write_text(json.dumps(config))
    monkeypatch.setenv('PATH', str(install / 'bin'))
    env = verify._test_environment(root)
    assert env['TOFU_TOOLCHAINS_MISSING'] == 'document-tools'
    assert str(install / 'bin') not in env['PATH'].split(os.pathsep)
