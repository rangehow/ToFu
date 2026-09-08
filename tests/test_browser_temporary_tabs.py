"""Search browser fallbacks own bounded temporary tabs until settlement."""

import pytest

from tests._browser_extension_probe import run_extension_probe


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('command', ['fetch_url', 'research_url'])
@pytest.mark.parametrize('phase', ['attach', 'enable', 'stop', 'extract'])
def test_search_timeout_closes_tab_even_when_browser_operation_is_stuck(command, phase):
    probe = run_extension_probe(f'temporaryTabTimeout:{phase}:{command}')
    assert probe['removedAtTimeout'] is True
    assert 'timed out' in probe['results'][0]['error']
    if phase in ('attach', 'enable'):
        assert not any(op.startswith('tabs.update:') for op in probe['operations'])
    assert probe['captures'] == 0
    assert probe['sessionData']['temporarySearchTabIds'] == []


@pytest.mark.parametrize('phase', ['create', 'probe'])
def test_timeout_cannot_navigate_or_leak_a_late_created_search_tab(phase):
    probe = run_extension_probe(f'temporaryTabTimeout:{phase}')
    assert 'timed out' in probe['results'][0]['error']
    assert not any(op.startswith('tabs.update:') for op in probe['operations'])
    assert probe['removedEventually'] is (phase == 'create')
    assert probe['captures'] == 0


def test_parallel_searches_cannot_accumulate_unbounded_browser_tabs():
    probe = run_extension_probe('temporaryTabCapacity')
    assert probe['peakTabs'] == 4
    assert probe['remainingTabs'] == 0
    assert len(probe['results']) == 12
    assert sum('capacity' in row['error'] for row in probe['results']) == 8
    assert probe['sessionData']['temporarySearchTabIds'] == []
    assert probe['recoveredResult']['error'] is None
    assert probe['recoveredResult']['result']['text'] == 'rendered page'


def test_worker_restart_reaps_only_recorded_temporary_tab_ids():
    probe = run_extension_probe('temporaryTabRecovery')
    assert sorted(probe['removedTabIds']) == [19, 23]
    assert probe['sessionData']['temporarySearchTabIds'] == []


@pytest.mark.parametrize('phase', ['storage', 'navigation'])
def test_search_tab_is_closed_when_ownership_storage_or_navigation_fails(phase):
    probe = run_extension_probe(f'temporaryTabFailure:{phase}')
    assert phase in probe['result']['error']
    assert probe['removedTabIds'] == [7]
    assert not any(op.startswith('tabs.update:') for op in probe['operations'])


def test_failed_chrome_cleanup_preserves_ownership_for_restart_and_reports_error():
    probe = run_extension_probe('temporaryTabFailure:cleanup')
    assert 'cleanup failed' in probe['result']['error']
    assert probe['result']['result'] is None
    assert probe['sessionData']['temporarySearchTabIds'] == [7]
