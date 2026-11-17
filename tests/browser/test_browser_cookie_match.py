"""The extension's ``match_cookies`` command, run in the real worker.

The command reports which browser cookies equal credentials the user's own
Tofu server already stores (lib/mcp/browser_credentials.py). It compares
one-time keyed digests in the browser and returns cookie identity only.
"""
import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def cookie_match_probe():
    return run_extension_probe('cookieMatch')


def test_matching_returns_cookie_identity_never_values(cookie_match_probe):
    matches = cookie_match_probe['result']['matches']
    assert matches == [
        {'id': 'whole', 'name': 'overleaf_session2', 'domain': '.overleaf.com',
         'path': '/', 'hostOnly': False, 'secure': True, 'httpOnly': True},
        {'id': 'pair', 'name': 'web_session', 'domain': '.xiaohongshu.com',
         'path': '/', 'hostOnly': False, 'secure': True, 'httpOnly': True},
    ]
    assert all('value' not in match for match in matches)


def test_a_named_candidate_matches_only_its_cookie(cookie_match_probe):
    ids = {match['id'] for match in cookie_match_probe['result']['matches']}
    assert 'other-name' not in ids and 'stale' not in ids


def test_the_worker_reads_its_own_cookie_store_and_advertises_the_capability(
        cookie_match_probe):
    assert cookie_match_probe['queries'] == [{}]
    assert cookie_match_probe['advertised'] is True


def test_no_candidates_or_key_reads_nothing(cookie_match_probe):
    assert cookie_match_probe['empty'] == {'matches': []}
    assert len(cookie_match_probe['queries']) == 1
