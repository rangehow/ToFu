"""Executable specification for the static vendor release-date knowledge."""

from __future__ import annotations

import re

import pytest

from lib.model_info import release_date
from lib.model_info._release import _RULES


pytestmark = pytest.mark.unit


def test_canonical_ids_resolve_to_vendor_dates() -> None:
    assert release_date('glm-5.3') == '2026-08'  # vendor card dated 2026-08-14
    assert release_date('claude-opus-4-8') == '2026-08'
    assert release_date('claude-fable-5-2') == '2026-11-09'  # not Fable 5's 2026-08
    assert release_date('claude-fable-5-1') == '2026-09-01'
    assert release_date('kimi-k3') == '2026-07'
    assert release_date('kimi-k3.1-pro') == '2026-10-23'  # not K3's 2026-07
    assert release_date('gemini-3.7-flash') == '2026-08'
    assert release_date('gemini-3.8-flash') == '2026-09'
    assert release_date('gemini-4-argon') == '2026-10'  # model page, Oct 2026
    assert release_date('gemini-4-carbon') == '2026-11-10'  # Gemini API changelog
    assert release_date('gemini-4-hydrogen') == '2026-11-12'  # model page + changelog
    assert release_date('hy4') == '2026-11-04'
    assert release_date('hy4-preview') == '2026-08-28'  # not Hy4's 2026-11-04
    assert release_date('hy3') == '2026-07-06'
    assert release_date('hy3-preview') == '2026-04'

    assert release_date('qwen4-max') == '2026-11-16'
    assert release_date('muse-spark-1.2') == '2026-08-05'
    assert release_date('muse-spark-1.3') == '2026-09-02'  # listed 2026-09-02
    assert release_date('muse-ember-1.0') == '2026-11-09'  # launch post
    assert release_date('MiniMax-M3.1-Flash') == '2026-10-19'
    assert release_date('inkling-2-small') == '2026-11-05'  # model card


# Maintained media identities without an officially published date. They
# stay withheld from the registry until one is recorded.
UNDATED_MEDIA_IDENTITIES = {'lyria-002', 'tts-1', 'tts-1-hd', 'whisper-1'}


def test_maintained_media_identities_have_their_own_dates() -> None:
    """A media Model publishes with its release date (world catalogue).

    Substring rules let an id borrow a neighbour's date: a preview contains
    its GA id, a dated snapshot its alias, and gemini-3.8-flash-tts the chat
    model gemini-3.8-flash. Every maintained media identity therefore states
    its own date here (or is knowingly undated), so a new media fact cannot
    publish with a borrowed one.
    """
    from lib.model_info.media_facts import maintained_media_identities

    expected = {
        'gemini-2.5-flash-image': '2025-08',
        'gemini-3.8-flash-tts': '2026-09-22',
        'gemini-3.8-flash-lite-tts': '2026-09-22',
        'gpt-image-1.5': '2025-12',
        'gpt-image-2': '2026-04',
        'gpt-image-2.5-sunburst': '2026-09-08',
        'gemini-3.1-flash-image': '2026-05-28',
        'gemini-3.1-flash-image-preview': '2026-03',
        'gemini-3-pro-image': '2026-05-28',
        'gemini-3-pro-image-preview': '2026-01',
        'gemini-3.1-flash-lite-image': '2026-06',
        'gemini-nano-banana-2.1': '2026-10-06',
        'gemini-omni-1.2-flash': '2026-11-05',
        'gemini-omni-1.1-flash': '2026-08-27',
        'lyria-3-clip-preview': '2026-03-25',
        'lyria-3-pro-preview': '2026-03-25',
        'lyria-3.5': '2026-09-03',
        'veo-3.1-generate-001': '2025-11-17',
        'veo-3.1-fast-generate-001': '2025-11-17',
        'veo-3.1-lite-generate-001': '2026-04-02',
        'gpt-image-1-mini': '2025-10-06',
        'gpt-4o-transcribe': '2025-03-20',
        'gpt-4o-mini-transcribe': '2025-12-15',
        'gpt-4o-mini-transcribe-2025-03-20': '2025-03-20',
        'gpt-4o-mini-tts': '2025-12-15',
        'gpt-4o-mini-tts-2025-03-20': '2025-03-20',
    }
    assert {model: release_date(model) for model in expected} == expected
    maintained = {model for _creator, model in maintained_media_identities()}
    assert maintained - UNDATED_MEDIA_IDENTITIES <= set(expected), (
        'state the official date of each new maintained media identity here')
    assert {model: release_date(model) for model in UNDATED_MEDIA_IDENTITIES} == dict.fromkeys(
        UNDATED_MEDIA_IDENTITIES)

    assert release_date('grok-4.7') == '2026-09-21'
    assert release_date('grok-4.20-multi-agent') == '2026-03'

def test_wire_respellings_hit_the_trained_model_rule() -> None:
    # Bedrock-style region prefix with dot-folded version.
    assert release_date('aws.claude-opus-4.8') == '2026-08'
    # Vendor snapshot stamp suffix.
    assert release_date('doubao-seed-2-0-pro-260215') == '2026-02'
    # Case folding.
    assert release_date('MiniMax-M3') == '2026-06'

    # Relay namespace prefix.
    assert release_date('moonshotai/kimi-k3.1-pro') == '2026-10-23'
    assert release_date('tencent/hy4') == '2026-11-04'
    assert release_date('google/gemini-4-carbon') == '2026-11-10'
    assert release_date('google/gemini-4-hydrogen') == '2026-11-12'
    assert release_date('thinkingmachines/inkling-2-small') == '2026-11-05'
    assert release_date('anthropic/claude-fable-5.2') == '2026-11-09'

    # Contributor SKU rides the same-day listing rule.
    assert release_date('muse-spark-1.3-contributor') == '2026-09-02'
    assert release_date('muse-spark-1.2-contributor') == '2026-08-05'


def test_unknown_models_return_none_instead_of_guessing() -> None:
    assert release_date('unseen-model-9000') is None
    assert release_date('') is None


def test_table_respects_granularity_and_specific_first_ordering() -> None:
    for needle, date in _RULES:
        assert re.fullmatch(r'\d{4}-\d{2}(-\d{2})?', date), (needle, date)
        assert needle == needle.lower() and '.' not in needle, needle
    # A dated variant must precede any prefix of it with a different date,
    # otherwise the generic rule would shadow the specific one.
    for later_index, (later, later_date) in enumerate(_RULES):
        for earlier, earlier_date in _RULES[:later_index]:
            if earlier in later and earlier_date != later_date:
                raise AssertionError(
                    f'{later!r} ({later_date}) must precede its prefix '
                    f'{earlier!r} ({earlier_date})')
