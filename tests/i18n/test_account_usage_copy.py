"""The account-usage copy names Tofu's local record and its limitation."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LOCALES = Path(__file__).resolve().parents[2] / 'frontend/src/i18n/locales'
KEYS = (
    'settings.oauthAccountsUsageNote',
    'settings.oauthAccountsUsageWindow',
    'settings.oauthAccountsUsageUnavailable',
    'settings.oauthAccountUsage',
    'settings.oauthAccountUsageCost',
    'settings.oauthAccountLastUsed',
    'settings.oauthAccountUnknownUsage',
)


def catalog(code):
    return json.loads((LOCALES / f'{code}.json').read_text())


def test_source_locales_state_the_local_record_limitation():
    english = catalog('en')['settings.oauthAccountsUsageNote']
    chinese = catalog('zh')['settings.oauthAccountsUsageNote']
    assert 'local record' in english and 'not provider billing' in english
    assert '本地记录' in chinese and '不是服务商账单' in chinese


def test_every_locale_carries_the_usage_copy():
    for path in LOCALES.glob('*.json'):
        catalog = json.loads(path.read_text())
        missing = [key for key in KEYS if key not in catalog]
        assert not missing, (path.name, missing)