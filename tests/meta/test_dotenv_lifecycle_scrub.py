"""The checkout's .env never re-establishes the production identity the conftest scrubs.

``tests/conftest.py`` removes this shell's ``PORT`` (and the rest of the
lifecycle authority) so a test never talks to the running Tofu. The
application's boot then loads the checkout's own ``.env``, which names
``PORT=15000``; ``tofu_dotenv.load_dotenv_file`` only skips values already
present, so the scrubbed name returned and the production guard refused every
child the booted server started. Observed as
``EnginePreparationError: Cannot read the storage engine source manifest ...
(ProductionConnectionRefused)`` for the isolated TOML parser, which made the
documented in-process ``pytest tests/blackbox`` path unusable in a checkout
with a ``.env``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

import tofu_dotenv  # noqa: E402
from tests.support import production_api_guard  # noqa: E402

pytestmark = pytest.mark.unit

NAMES = ('PORT', '_TOFU_RUNTIME_PORT', 'TOFU_SUPERVISOR_PORT')


@pytest.fixture
def dotenv(tmp_path: Path) -> Path:
    path = tmp_path / '.env'
    path.write_text('PORT=15000\nTOFU_PERSONAL_STORAGE_BACKEND=sqlite\n', encoding='utf-8')
    return path


def test_a_scrubbed_lifecycle_name_stays_scrubbed(dotenv: Path, monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('TOFU_PERSONAL_STORAGE_BACKEND', raising=False)
    load = production_api_guard.dotenv_without_lifecycle_authority(
        tofu_dotenv.load_dotenv_file, NAMES)
    load(dotenv)
    assert 'PORT' not in os.environ
    assert os.environ['TOFU_PERSONAL_STORAGE_BACKEND'] == 'sqlite'


def test_an_ordinary_setting_still_loads(dotenv: Path, monkeypatch):
    monkeypatch.delenv('TOFU_PERSONAL_STORAGE_BACKEND', raising=False)
    load = production_api_guard.dotenv_without_lifecycle_authority(
        tofu_dotenv.load_dotenv_file, NAMES)
    load(dotenv)
    assert os.environ['TOFU_PERSONAL_STORAGE_BACKEND'] == 'sqlite'


def test_an_explicitly_exported_value_is_preserved(dotenv: Path, monkeypatch):
    monkeypatch.setenv('PORT', '23456')
    load = production_api_guard.dotenv_without_lifecycle_authority(
        tofu_dotenv.load_dotenv_file, NAMES)
    load(dotenv)
    assert os.environ['PORT'] == '23456'


def test_an_explicit_environment_mapping_is_restored_too(dotenv: Path):
    target = {'TOFU_PERSONAL_STORAGE_BACKEND': 'sqlite'}
    load = production_api_guard.dotenv_without_lifecycle_authority(
        tofu_dotenv.load_dotenv_file, NAMES)
    load(dotenv, target)
    assert 'PORT' not in target
    assert target['TOFU_PERSONAL_STORAGE_BACKEND'] == 'sqlite'
