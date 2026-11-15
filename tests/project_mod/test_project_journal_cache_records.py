"""New record files extend a warm history without rereading its prefix."""
from collections import OrderedDict
import json
from pathlib import Path
import uuid

import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.fixture
def history(tmp_path, monkeypatch):
    for name in ('_mods_cache_identity', '_mods_cache_bytes', '_JOURNAL_VALIDATED', '_RECORD_ROWS'):
        monkeypatch.setattr(mods, name, {})
    monkeypatch.setattr(mods, '_mods_cache', OrderedDict())
    monkeypatch.setattr(mods, '_MODS_CACHE_CAPACITY', 16)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', 32 * 1024 * 1024)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', 32 * 1024 * 1024)
    session = str(tmp_path / 'journal')
    Path(session).mkdir()
    monkeypatch.setattr(mods, '_state', {'sessionId': session, 'modifications': []})
    rows = [{'type': 'write_file', 'path': f'old-{i}', 'existed': True}
            for i in range(1000)]
    mods._flush_to_disk(session, rows)
    mods._assert_journal_ready(session)
    assert mods._cache_get(session) == rows
    return session, [row['path'] for row in rows]


def commit(session, path):
    assert mods._commit_record(session, {'type': 'write_file', 'path': path, 'existed': True,
                                         'changeId': uuid.uuid4().hex})


def paths(rows):
    return [row['path'] for row in rows]


def test_commits_and_following_reads_do_not_revisit_warm_history(history, monkeypatch):
    session, expected = history
    full_weight = mods._mods_cache_weight

    def forbidden(*args, **kwargs):
        pytest.fail('A small commit revisited the validated historical prefix')

    monkeypatch.setattr(mods, '_read_history', forbidden)
    monkeypatch.setattr(mods, '_mods_cache_weight', forbidden)
    for index in range(12):
        commit(session, f'new-{index}')
        expected.append(f'new-{index}')
        assert paths(mods._cache_get(session)) == expected
        assert paths(mods._state['modifications']) == expected
        assert mods._mods_cache_bytes[session] >= full_weight(
            session, mods._cache_get(session), mods._journal_identity(session))


def test_extension_reclaims_only_cache_when_resident_budget_is_exceeded(history, monkeypatch):
    session, expected = history
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', mods._mods_cache_bytes[session])
    commit(session, 'new')
    assert paths(mods._cache_get(session)) == expected + ['new']
    assert session not in mods._mods_cache
    assert mods._state['modifications'] == []
    assert paths(mods._load_from_disk(session)) == expected + ['new']


def test_inactive_extension_preserves_other_project_and_can_become_primary(history):
    session, expected = history
    other = [{'type': 'write_file', 'path': 'other-project'}]
    mods._state.update(sessionId='other-session', modifications=other)
    for index in range(20):
        commit(session, f'inactive-{index}')
        expected.append(f'inactive-{index}')
        mods._cache_get(session)
    assert mods._state['modifications'] == other
    with mods._lock:
        mods._sync_primary_view(session, mods._mods_cache[session])
    commit(session, 'now-primary')
    assert paths(mods._cache_get(session)) == expected + ['now-primary']
    assert paths(mods._state['modifications']) == expected + ['now-primary']
    assert mods._mods_cache_bytes[session] >= mods._mods_cache_weight(
        session, mods._mods_cache[session], mods._journal_identity(session))


def test_entry_evicted_between_commit_and_read_is_rebuilt_exactly(history):
    session, expected = history
    commit(session, 'new')
    mods._invalidate_cache(session)
    assert paths(mods._cache_get(session)) == expected + ['new']


def test_commit_refuses_an_externally_replaced_invalid_journal(history):
    session, _ = history
    path = Path(session, 'modifications.json')
    invalid = json.dumps({'modifications': [{'type': 'write_file'}]})
    path.write_text(invalid)
    with pytest.raises(ValueError, match='Invalid modification journal'):
        commit(session, 'new')
    assert path.read_text() == invalid
    assert not list(Path(session).glob('records/*.json'))


def test_a_replaced_index_is_never_labelled_with_the_old_prefix(history):
    session, _ = history
    commit(session, 'new')
    foreign = {'type': 'write_file', 'path': 'external', 'existed': True}
    replacement = Path(session, 'replacement.json')
    replacement.write_text(json.dumps({'modifications': [foreign]}))
    replacement.replace(Path(session, 'modifications.json'))
    assert paths(mods._cache_get(session)) == ['external', 'new']


def test_another_process_record_catches_a_warm_prefix_up(history, monkeypatch):
    session, expected = history
    # Another process committed after this cache was filled; it never saw it.
    foreign = {'type': 'write_file', 'path': 'other-process', 'existed': True,
               'changeId': 'f' * 32, 'journalKey': f'{1:020d}-{1:012d}'}
    directory = Path(session, 'records')
    directory.mkdir(exist_ok=True)
    (directory / f"{foreign['journalKey']}-{foreign['changeId']}.json").write_text(json.dumps(foreign))
    weigh = mods._mods_cache_weight
    monkeypatch.setattr(mods, '_read_history', lambda *a: pytest.fail('history re-read'))
    monkeypatch.setattr(mods, '_mods_cache_weight', lambda *a: pytest.fail('history re-weighed'))
    commit(session, 'mine')
    expected += ['other-process', 'mine']
    assert paths(mods._cache_get(session)) == expected
    assert mods._mods_cache_identity.get(session) == mods._journal_identity(session)
    assert paths(mods._state['modifications']) == expected
    assert mods._mods_cache_bytes[session] >= weigh(
        session, mods._cache_get(session), mods._journal_identity(session))
