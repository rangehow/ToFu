"""Daily reports never store a raw provider key.

The report generator summarises conversations through an LLM, and a stored
report (2026-06-11) carried a full ``sk-`` key the model copied from a
transcript into a ``tomorrow[].quick_action.prefill``. Every report write
(manual save, generated save, mutation) now replaces each known credential of
the owner with its label and every bare ``sk-`` key shape with a non-reversible
fingerprint before the payload reaches disk.
"""
from __future__ import annotations

import json

import pytest

from lib.daily_report import storage
from lib.tool_results import credential_redaction

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41
RAW_SK_KEY = 'sk-' + 'Q7m2' * 8
KNOWN_R2_KEY = 'r2' + '9f3a' * 8


@pytest.fixture
def report_store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, '_REPORTS_DIR', str(tmp_path))
    monkeypatch.setattr(storage, '_invalidate_calendar', lambda _date, *, owner_user_id: None)
    seen_owners = []
    known = credential_redaction.KnownCredentialRedactor.build(
        [('provider cloudflare-r2', KNOWN_R2_KEY)])

    def redactor(*, owner_user_id=None):
        seen_owners.append(owner_user_id)
        return known

    monkeypatch.setattr(credential_redaction, 'known_credential_redactor', redactor)
    return tmp_path, seen_owners


def _report_with_keys():
    return {
        'streams': [{'id': 's', 'summary': f'rotated key {KNOWN_R2_KEY} today'}],
        'tomorrow': [{'quick_action': {'prefill': f'export OPENAI_API_KEY={RAW_SK_KEY}'}}],
    }


def _stored_text(root, day):
    return (root / str(OWNER_USER_ID) / f'{day}.json').read_text()


@pytest.mark.parametrize('save', [
    lambda day, report: storage._save_report(day, report, owner_user_id=OWNER_USER_ID),
    lambda day, report: storage._save_generated_report(day, report, owner_user_id=OWNER_USER_ID),
    lambda day, report: storage._update_report(
        day, lambda _current: report, owner_user_id=OWNER_USER_ID, default={}),
])
def test_report_writes_store_labels_and_fingerprints_not_keys(report_store, save):
    root, seen_owners = report_store
    payload = save('2026-11-18', _report_with_keys())
    text = _stored_text(root, '2026-11-18')

    assert RAW_SK_KEY not in text and KNOWN_R2_KEY not in text
    assert RAW_SK_KEY not in json.dumps(payload) and KNOWN_R2_KEY not in json.dumps(payload)
    stored = json.loads(text)
    assert stored['streams'][0]['summary'] == (
        'rotated key [redacted credential: provider cloudflare-r2] today')
    prefill = stored['tomorrow'][0]['quick_action']['prefill']
    assert prefill.startswith('export OPENAI_API_KEY=[redacted credential: sk-key sha256:')
    assert RAW_SK_KEY[-4:] not in prefill
    assert OWNER_USER_ID in seen_owners


def test_the_fingerprint_is_stable_for_one_key_and_distinct_across_keys(report_store):
    root, _ = report_store
    other = 'sk-' + 'Zz09' * 8
    storage._save_report('2026-11-18', {'tomorrow': [RAW_SK_KEY, RAW_SK_KEY, other]},
                         owner_user_id=OWNER_USER_ID)
    first, again, different = json.loads(_stored_text(root, '2026-11-18'))['tomorrow']
    assert first == again != different


def test_short_placeholders_and_ordinary_text_are_left_alone(report_store):
    root, _ = report_store
    text = 'set sk-xxx then ask-me about task-123 and the sk-learn docs'
    storage._save_report('2026-11-18', {'tomorrow': [text]}, owner_user_id=OWNER_USER_ID)
    assert json.loads(_stored_text(root, '2026-11-18'))['tomorrow'] == [text]
