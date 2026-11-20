"""Every boot phase the server can project has UI copy in every UI language.

WHY THIS EXISTS: the restart card renders ``update.phase.<key>`` from the
server-projected phase key. A phase added on the server without a catalog
entry reached the user as the raw key (``update.phase.execution_service``),
which reads as a defect, not as progress. The frontend now degrades to the
server's English label, but the catalog is what makes the phase readable in
every UI language, so a new phase key must arrive with its copy.
"""

import json
from pathlib import Path

import pytest

from lib.startup_phases import (
    EXECUTION_SERVICE_PHASE_KEY,
    STARTUP_PHASES,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LOCALES = ROOT / 'frontend/src/i18n/locales'
LANGUAGES = [
    entry['code']
    for entry in json.loads(
        (ROOT / 'frontend/src/i18n/languages.json').read_text(encoding='utf-8')
    )['languages']
]
PHASE_KEYS = [phase.key for phase in STARTUP_PHASES] + [EXECUTION_SERVICE_PHASE_KEY]


@pytest.mark.parametrize('key', PHASE_KEYS)
def test_every_phase_key_has_copy_in_every_ui_language(key):
    missing = []
    for language in LANGUAGES:
        catalog = json.loads(
            (LOCALES / f'{language}.json').read_text(encoding='utf-8'))
        if not catalog.get(f'update.phase.{key}'):
            missing.append(language)
    assert not missing, f'update.phase.{key} is missing for {missing}'