"""The translation-recovery sweep reads translation state, never derived segments.

Opening a conversation schedules this sweep, and it walks the whole history.
On 2026-11-13 a native capture caught the production API host in a
multi-second full GC (gc_collect_main). The GC ran on the translate-recovery
worker, inside ``_stable_block_ids`` <- ``public_turn_with_stable_segments``
<- ``iter_turns_newest_first``: the sweep derived stable segments for every
settled Turn and kept only the translation verdict. Its decision needs only
status, actor, identity, text and the normalized translation verdict.
"""

from __future__ import annotations

import pytest

import lib.translate.skip_policy as skip_policy
import lib.translate.terminal as terminal
import lib.turn_lifecycle as lifecycle
import lib.turn_projection_segments as projection_segments
from lib.turn_projection_patch import normalize_projection_document


pytestmark = pytest.mark.unit


def _turn(ordinal: int, projection, *, status: str = "completed", actor: str = "assistant"):
    return {
        "turnId": f"t{ordinal:02d}", "conversationId": "conv", "laneId": "main",
        "ordinal": ordinal, "actor": actor, "kind": "reply", "status": status,
        "projection": projection, "projectionRevision": 3, "settlement": {},
        "createdAt": ordinal, "updatedAt": ordinal,
        "currentAttemptId": None,
    }


def _rounds(count: int) -> dict:
    """Settled tool history whose stable segments a full read would derive."""
    return {"toolRounds": [{
        "toolCallId": f"call-{index}", "toolName": "read_files", "llmRound": index,
        "toolArgs": {"path": f"lib/m{index}.py"}, "toolContent": "x" * 200,
        "status": "done",
    } for index in range(count)]}


# Oldest first. Each entry: (projection, status, actor).
_HISTORY = [
    ({"content": "older untranslated reply", **_rounds(6)}, "completed", "assistant"),
    ({"content": "legacy translated body", "translatedContent": "已翻译", **_rounds(4)},
     "completed", "assistant"),
    ({"content": "legacy done flag", "_translateDone": True}, "completed", "assistant"),
    ({"content": "failed translation", "_translateFailed": {"reason": "provider"},
      **_rounds(3)}, "completed", "assistant"),
    ({"content": "failed with error", "_translateError": "timeout"}, "interrupted", "assistant"),
    ({"content": "skipped reply", "_translateSkippedReason": "already_target"},
     "completed", "assistant"),
    ({"content": "verdict completed", "translation": {"status": "completed"}},
     "completed", "assistant"),
    ({"text": "legacy text field only"}, "completed", "assistant"),
    ({"content": "human prompt"}, "completed", "human"),
    ({"content": "   "}, "completed", "assistant"),
    ({"content": "still streaming"}, "running", "assistant"),
    ({"content": "awaiting the user"}, "waiting_for_user", "assistant"),
    ({"content": "being translated now", **_rounds(2)}, "completed", "assistant"),
    ({"content": "newest untranslated reply", **_rounds(8)}, "completed", "assistant"),
]


class _PagedClient:
    """turn.sync.page in pages of three, oldest-first within a page."""

    def __init__(self):
        self.turns = [_turn(ordinal, projection, status=status, actor=actor)
                      for ordinal, (projection, status, actor) in enumerate(_HISTORY)]

    def query(self, operation, payload):
        assert operation == "turn.sync.page", operation
        before = payload.get("before_ordinal")
        candidates = [turn for turn in self.turns
                      if before is None or turn["ordinal"] < before]
        page = candidates[-min(payload["limit"], 3):]
        has_more = len(candidates) > len(page)
        return {"turns": [dict(turn) for turn in page], "attempts": [],
                "hasMore": has_more, "syncSequence": 9,
                "nextBeforeOrdinal": page[0]["ordinal"] if has_more else None}


@pytest.fixture()
def sweep(monkeypatch):
    derivations = []
    derive = projection_segments.projection_with_stable_segments

    def counted(projection, **kwargs):
        derivations.append(kwargs.get("status"))
        return derive(projection, **kwargs)

    monkeypatch.setattr(projection_segments, "projection_with_stable_segments", counted)
    monkeypatch.setattr(lifecycle, "_turn_client", lambda write=False: _PagedClient())
    monkeypatch.setattr(skip_policy, "should_skip_automatic_translation",
                        lambda content, target, code: False)
    monkeypatch.setattr(terminal, "_active_whole_turn_keys", lambda: {("conv", "t12")})
    spawned = []
    monkeypatch.setattr(terminal, "_spawn_whole_turn_translation",
                        lambda **kw: spawned.append((kw["turn_id"], kw["content"])) is None)

    def run(**options):
        count = terminal.sweep_conversation_translations(
            "conv", user_id=1, config={"autoTranslate": True, "uiLang": "zh"}, **options)
        return count, spawned, derivations

    return run


def test_the_sweep_derives_no_stable_segments(sweep):
    _count, _spawned, derivations = sweep()

    assert derivations == []


def test_the_sweep_recovers_every_turn_that_needs_it_newest_first(sweep):
    count, spawned, _derivations = sweep()

    # Untranslated and failed settled replies are recovered across every page,
    # newest first. Already translated (verdict, legacy body or done flag),
    # skipped, human, empty, live, waiting and in-flight Turns are not.
    assert spawned == [
        ("t13", "newest untranslated reply"),
        ("t07", "legacy text field only"),
        ("t04", "failed with error"),
        ("t03", "failed translation"),
        ("t00", "older untranslated reply"),
    ]
    assert count == 5


def test_the_respawn_bound_still_stops_the_walk(sweep):
    count, spawned, _derivations = sweep(max_respawn=2)

    assert [turn_id for turn_id, _content in spawned] == ["t13", "t07"]
    assert count == 2


@pytest.mark.parametrize("raw", [
    {"content": "x", "translatedContent": "y"},
    {"content": "x", "originalContent": "y"},
    {"content": "x", "_translateDone": False, "_translateTaskId": "task"},
    {"content": "x", "_translateDone": True},
    {"content": "x", "_translateError": "boom", "translation": {"status": "completed"}},
    {"content": "x", "_translateSkippedReason": "already_target"},
    {"content": "x", "translation": {}},
    {"content": "x", "translation": {"unknown": 1}},
    {"content": "x", "translation": "not a mapping"},
    {"content": None},
    {"text": "legacy"},
    {"text": None},
    {},
    "plain text projection",
    None,
])
def test_translation_state_equals_the_normalized_document(raw):
    from lib.turn_projection_patch import projection_translation_state

    normalized = normalize_projection_document(raw)
    expected = {key: normalized[key] for key in ("content", "translation") if key in normalized}

    assert projection_translation_state(raw) == expected


def test_the_sweep_never_respawns_a_recorded_content_guard_refusal(sweep):
    """0b3b0163 boot: the sweep re-ran refused turns, 122 ERROR tracebacks."""
    client = _PagedClient()
    # A refusal recorded by the current worker (typed reason) ...
    client.turns[3]['projection'] = {
        'content': 'refused reply',
        '_translateFailed': {'reason': 'content_refused',
                             'error': 'translation refused by content guard: verdict=wrong_language'},
    }
    # ... and one recorded before the typed reason existed (text only).
    client.turns[4]['projection'] = {
        'content': 'older refused reply',
        '_translateFailed': {'reason': 'worker_error',
                             'error': 'translation refused by content guard: verdict=echo'},
    }
    # The normalized shape, as iter_turn_translation_states_newest_first returns it.
    client.turns[4]['projection'] = normalize_projection_document(
        client.turns[4]['projection'])
    client.turns[3]['projection']['_translateFailed'] = {
        'reason': 'content_refused',
        'error': 'translation refused by content guard: verdict=wrong_language'}

    import lib.turn_lifecycle as lifecycle_mod
    original = lifecycle_mod._turn_client
    lifecycle_mod._turn_client = lambda write=False: client
    try:
        count, spawned, _derivations = sweep()
    finally:
        lifecycle_mod._turn_client = original

    assert 'refused reply' not in [content for _turn_id, content in spawned]
    assert 'older refused reply' not in [content for _turn_id, content in spawned]
    assert 'failed with error' not in [content for _turn_id, content in spawned]
    assert count == 3
