"""providerReplayPayload=none: a path-closed browser view of Turn projections.

Every snapshot, page and replayed patch omits provider replay evidence by path,
so a patch transformed for the view applies to the view exactly as the stored
patch applies to storage. Storage, model replay and whole-projection edits keep
the stored evidence.
"""

from __future__ import annotations

import random
from copy import deepcopy

import pytest

from lib.turn_projection_patch import apply_projection_patch, build_projection_patch
from lib.turn_projection_segments import (
    change_without_provider_replay,
    projection_patch_without_provider_replay,
    projection_with_restored_browser_evidence,
    projection_without_provider_replay,
)


pytestmark = pytest.mark.unit

_ROUND_EVIDENCE = ("thinkingSignature", "_anthropicContentBlocks", "_responsesItems")


def _signature(label: str) -> str:
    return f"sig-{label}-" + "s" * 512


def _round(call_id: str, thinking: str, *, content: str | None = None) -> dict:
    record = {
        "toolCallId": call_id, "toolName": "read_files", "toolArgs": f'{{"path": "{call_id}"}}',
        "thinking": thinking, "thinkingSignature": _signature(thinking),
        "_anthropicContentBlocks": [
            {"type": "thinking", "thinking": thinking, "signature": _signature(thinking)},
            {"type": "tool_use", "id": call_id, "name": "read_files", "input": {"path": call_id}},
        ],
        "status": "running",
    }
    if content is not None:
        record.update(toolContent=content, status="done")
    return record


def _thinking(block: str, text: str, signed: bool = True) -> dict:
    segment = {"type": "thinking", "blockId": block, "text": text}
    if signed:
        segment["signature"] = _signature(text)
    return segment


def _tool(call_id: str, content: str | None = None) -> dict:
    return {"type": "tool_use", "blockId": f"tool:{call_id}", "id": call_id, "name": "read_files",
            "input": f'{{"path": "{call_id}"}}',
            "result": {} if content is None else {"content": content, "status": "done"}}


def _terminal(text: str) -> dict:
    return {"type": "text", "blockId": "text:terminal", "text": text, "terminal": True}


def _live_revisions() -> list[dict]:
    """Committed revisions of one live Turn in stream order."""
    first = _round("call-1", "plan one")
    second = {**_round("call-2", "plan two"), "_responsesItems": [{"type": "reasoning", "enc": "e" * 256}]}
    revisions = [
        {"content": "", "segments": [], "toolRounds": []},
        {"content": "", "segments": [_thinking("thinking:1", "plan", signed=False)], "toolRounds": []},
        {"content": "", "segments": [_thinking("thinking:1", "plan one", signed=False)], "toolRounds": []},
        {"content": "", "segments": [_thinking("thinking:1", "plan one"), _tool("call-1"),
                                     _terminal("")], "toolRounds": [first]},
        {"content": "Reading", "segments": [_thinking("thinking:1", "plan one"), _tool("call-1", "body"),
                                            _terminal("Reading")],
         "toolRounds": [{**first, "toolContent": "body", "status": "done"}]},
    ]
    done = revisions[-1]
    # A second round lands before the terminal text: every later index shifts.
    revisions.append({"content": "", "segments": [
        *done["segments"][:2], _thinking("thinking:2", "plan two"), _tool("call-2"), _terminal(""),
    ], "toolRounds": [*done["toolRounds"], second]})
    # Rethinking in place changes thinking and every signature of round one.
    rethought = deepcopy(revisions[-1])
    rethought["segments"][0] = _thinking("thinking:1", "plan one revised")
    rethought["toolRounds"][0] = {**_round("call-1", "plan one revised"), "toolContent": "body",
                                  "status": "done"}
    revisions.append(rethought)
    # Compaction drops replay bodies; Responses items shrink.
    compacted = deepcopy(rethought)
    compacted["toolRounds"][0].pop("_anthropicContentBlocks")
    compacted["toolRounds"][1]["_responsesItems"] = []
    compacted["segments"][2].pop("signature")
    revisions.append(compacted)
    # A whole-document replacement and a truncated round list.
    revisions.append({"content": "final", "segments": [_terminal("final")],
                      "toolRounds": [compacted["toolRounds"][1]]})
    revisions.append({"content": "final", "segments": [_terminal("final")], "toolRounds": []})
    return revisions


def _assert_no_evidence(value) -> None:
    for round_record in value.get("toolRounds") or []:
        assert not set(_ROUND_EVIDENCE) & set(round_record)
    for segment in value.get("segments") or []:
        assert "signature" not in segment


def _assert_view_patch(before: dict, after: dict, revision: int) -> None:
    patch = build_projection_patch(before, after, base_revision=revision, target_revision=revision + 1)
    assert apply_projection_patch(before, patch) == after
    view_patch = projection_patch_without_provider_replay(patch)
    assert (view_patch["baseRevision"], view_patch["targetRevision"]) == (revision, revision + 1)
    for operation in view_patch["operations"]:
        path = operation["path"]
        assert not (len(path) >= 3 and path[0] == "toolRounds" and path[2] in _ROUND_EVIDENCE)
        assert not (len(path) >= 3 and path[0] == "segments" and path[2] == "signature")
    view_after = apply_projection_patch(projection_without_provider_replay(before), view_patch)
    assert view_after == projection_without_provider_replay(after)
    _assert_no_evidence(view_after)


def test_view_patches_apply_to_view_bases_through_a_live_turn():
    revisions = _live_revisions()
    for revision, (before, after) in enumerate(zip(revisions, revisions[1:])):
        _assert_view_patch(before, after, revision)


def _random_edit(rng: random.Random, current: dict, revision: int) -> dict:
    after = deepcopy(current)
    rounds, segments = after["toolRounds"], after["segments"]
    choice = rng.randrange(11)
    if choice == 0:
        rounds.append(_round(f"call-r{revision}", f"plan {revision}"))
    elif choice == 1 and rounds:
        target = rng.randrange(len(rounds))
        rounds[target]["thinkingSignature"] = _signature(f"new {revision}")
    elif choice == 2 and rounds:
        rounds[rng.randrange(len(rounds))].pop(rng.choice(_ROUND_EVIDENCE), None)
    elif choice == 3 and rounds:
        target = rng.randrange(len(rounds))
        rounds[target] = {**_round(rounds[target]["toolCallId"], f"rethink {revision}"),
                          "toolContent": f"out {revision}", "status": "done"}
    elif choice == 4:
        segments.insert(rng.randrange(len(segments) + 1),
                        _thinking(f"thinking:r{revision}", f"plan {revision}", rng.random() < .7))
    elif choice == 5 and segments:
        segments.pop(rng.randrange(len(segments)))
    elif choice == 6 and segments:
        segment = segments[rng.randrange(len(segments))]
        if "signature" in segment:
            segment.pop("signature")
        else:
            segment["signature"] = _signature(f"late {revision}")
    elif choice == 7 and rounds:
        del rounds[rng.randrange(len(rounds)):]
    elif choice == 8 and rounds:
        # A growing evidence string is patched with append_text.
        target = rounds[rng.randrange(len(rounds))]
        if isinstance(target.get("thinkingSignature"), str):
            target["thinkingSignature"] += f"-{revision}"
    elif choice == 9 and rounds:
        blocks = rounds[rng.randrange(len(rounds))].get("_anthropicContentBlocks")
        if blocks:
            blocks[0]["signature"] = _signature(f"block {revision}")
    else:
        after["content"] += f" step {revision}"
    return after


def _revision_pairs(seed: int, sequences: int, length: int) -> list[tuple[dict, dict]]:
    """(before, after) committed revisions: a live Turn plus seeded random edits."""
    revisions = _live_revisions()
    pairs = list(zip(revisions, revisions[1:]))
    rng = random.Random(seed)
    for _sequence in range(sequences):
        current = deepcopy(revisions[5])
        for revision in range(length):
            after = _random_edit(rng, current, revision)
            pairs.append((current, after))
            current = after
    return pairs


def test_view_patches_apply_to_view_bases_for_random_edit_sequences():
    for revision, (before, after) in enumerate(_revision_pairs(20261109, 40, 60)):
        _assert_view_patch(before, after, revision)


def test_browser_applier_keeps_mixed_representation_bases_safe(tmp_path):
    """A live tab can switch representation when a server of the other
    version takes over its stream (a restart between versions). The real
    browser applier must apply a view patch to a full base, and must apply a
    full patch to a view base exactly or fail closed (a projection gap, which
    resynchronizes), never leaving partial evidence."""
    import json
    import subprocess
    from pathlib import Path

    from tests._runtime_sections import native_module_path

    root = Path(__file__).resolve().parents[2]
    entry = tmp_path / "apply-patch.ts"
    entry.write_text(
        "export { applyProjectionPatch } from "
        f"{(root / 'frontend/src/core/projection-patch.ts').as_posix()!r};\n", encoding="utf-8")
    bundle = native_module_path("provider-replay/apply-patch.js", entry)
    pairs = _revision_pairs(20261110, 12, 40)
    cases = []
    for revision, (before, after) in enumerate(pairs):
        patch = build_projection_patch(before, after, base_revision=revision, target_revision=revision + 1)
        cases.append({"fullBase": before, "viewBase": projection_without_provider_replay(before),
                      "patch": patch, "viewPatch": projection_patch_without_provider_replay(patch)})
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    result = subprocess.run(["node", "-e", r"""
const fs = require('fs');
globalThis.window = globalThis;
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
fs.writeFileSync(process.argv[3], JSON.stringify(cases.map((item) => ({
  view: applyProjectionPatch(item.viewBase, item.viewPatch),
  forward: applyProjectionPatch(item.fullBase, item.viewPatch),
  rollback: applyProjectionPatch(item.viewBase, item.patch),
}))));
""", str(bundle), str(tmp_path / "cases.json"), str(tmp_path / "out.json")],
        cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    outcomes = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    gaps = 0
    for (before, after), outcome in zip(pairs, outcomes, strict=True):
        expected = projection_without_provider_replay(after)
        assert outcome["view"] == expected
        assert outcome["forward"] is not None
        assert projection_without_provider_replay(outcome["forward"]) == expected
        rollback = outcome["rollback"]
        if rollback is None:
            gaps += 1
            continue
        assert projection_without_provider_replay(rollback) == expected
        for field, keys in (("toolRounds", _ROUND_EVIDENCE), ("segments", ("signature",))):
            for got, want in zip(rollback.get(field) or [], after.get(field) or [], strict=True):
                assert all(got[key] == want.get(key) for key in keys if key in got)
    # The seeded edits include appends and nested writes inside omitted keys.
    assert 0 < gaps < len(pairs)


def test_view_keeps_positions_and_shares_what_it_does_not_change():
    source = _live_revisions()[6]
    before = deepcopy(source)
    view = projection_without_provider_replay(source)
    assert source == before
    assert [round_record["toolCallId"] for round_record in view["toolRounds"]] == ["call-1", "call-2"]
    assert [segment["blockId"] for segment in view["segments"]] == [
        segment["blockId"] for segment in source["segments"]]
    assert view["segments"][1] is source["segments"][1]
    assert view["toolRounds"][0]["thinking"] == "plan one revised"
    _assert_no_evidence(view)
    plain = {"content": "x", "segments": [_terminal("x")], "toolRounds": [{"toolCallId": "c"}]}
    assert projection_without_provider_replay(plain) is plain
    patch = {"version": 1, "baseRevision": 1, "targetRevision": 2,
             "operations": [{"op": "append_text", "path": ["content"], "value": "y"}]}
    assert projection_patch_without_provider_replay(patch) is patch


def test_change_view_covers_attempt_events_turn_changes_and_related_turns():
    revisions = _live_revisions()
    patch = build_projection_patch(revisions[2], revisions[3], base_revision=2, target_revision=3)
    turn = {"turnId": "t", "status": "running", "projection": revisions[3]}
    attempt = {"contract": "tofu.conversation-sync.event/v1", "type": "attempt.event", "syncSeq": 3,
               "payload": {"event": {"turnId": "t", "payload": {
                   "projectionPatch": patch, "projection": revisions[3], "turns": [turn],
                   "event": {"type": "messages_snapshot"}}}}}
    turn_patch = {"contract": "tofu.conversation-sync.event/v1", "type": "turn.patch", "syncSeq": 4,
                  "payload": {"turns": [turn], "turnPatches": [{
                      "turnId": "t", "baseProjectionRevision": 2, "targetProjectionRevision": 3,
                      "updatedAt": 1, "projectionPatch": patch}]}}
    original = deepcopy([attempt, turn_patch])

    body = change_without_provider_replay(attempt)["payload"]["event"]["payload"]
    assert body["projectionPatch"] == projection_patch_without_provider_replay(patch)
    _assert_no_evidence(body["projection"])
    _assert_no_evidence(body["turns"][0]["projection"])
    assert body["event"] is attempt["payload"]["event"]["payload"]["event"]
    payload = change_without_provider_replay(turn_patch)["payload"]
    _assert_no_evidence(payload["turns"][0]["projection"])
    assert payload["turnPatches"][0]["projectionPatch"] == projection_patch_without_provider_replay(patch)
    assert [attempt, turn_patch] == original
    quiet = {"type": "turn.deleted", "payload": {"deletedTurnIds": ["t"]}}
    assert change_without_provider_replay(quiet) is quiet


def test_edit_of_the_view_restores_exactly_the_stored_evidence():
    authority = _live_revisions()[6]
    browser = projection_without_provider_replay(authority)
    edit = {**browser, "content": "edited"}
    restored = projection_with_restored_browser_evidence(edit, authority)
    assert restored == {**authority, "content": "edited"}

    # A stale client copy never survives: the stored value replaces it.
    stale = deepcopy(edit)
    stale["toolRounds"][0]["thinkingSignature"] = "stale"
    stale["segments"][0]["signature"] = "stale"
    assert projection_with_restored_browser_evidence(stale, authority) == {
        **authority, "content": "edited"}

    # Changed reasoning can carry no evidence, stored or client-supplied.
    rethought = deepcopy(stale)
    rethought["toolRounds"][0]["thinking"] = "another plan"
    rethought["segments"][0]["text"] = "another plan"
    result = projection_with_restored_browser_evidence(rethought, authority)
    assert not set(_ROUND_EVIDENCE) & set(result["toolRounds"][0])
    assert "signature" not in result["segments"][0]
    assert result["toolRounds"][1] == authority["toolRounds"][1]


@pytest.mark.parametrize("call_id", ["reused", ""])
def test_edit_restores_ambiguous_rounds_by_position(call_id):
    authority = _live_revisions()[6]
    authority["toolRounds"] = [{**authority["toolRounds"][0], "toolCallId": call_id},
                               {**authority["toolRounds"][1], "toolCallId": call_id}]
    edit = {**projection_without_provider_replay(authority), "content": "edited"}
    restored = projection_with_restored_browser_evidence(edit, authority)
    assert restored["toolRounds"] == authority["toolRounds"]
    # Without positional alignment an ambiguous identity cannot be paired.
    shortened = {**edit, "toolRounds": edit["toolRounds"][1:]}
    assert projection_with_restored_browser_evidence(shortened, authority)["toolRounds"] == \
        shortened["toolRounds"]


def test_restored_edit_replays_the_stored_signatures():
    from lib.tasks_pkg.message_builder import inject_tool_history

    authority = _live_revisions()[6]
    restored = projection_with_restored_browser_evidence(
        {**projection_without_provider_replay(authority), "content": "edited"}, authority)
    rounds = [{
        "assistantContent": "", "thinking": round_record["thinking"],
        "thinkingSignature": round_record.get("thinkingSignature", ""),
        "toolCalls": [{"id": round_record["toolCallId"], "name": "read_files",
                       "arguments": round_record["toolArgs"]}],
        "toolResults": [{"tool_call_id": round_record["toolCallId"], "content": "r"}],
    } for round_record in restored["toolRounds"]]
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    inject_tool_history(messages, {"toolHistory": rounds}, {"id": "t" * 32, "convId": "c" * 32},
                        "claude-opus-4-7")
    signatures = [message.get("thinking_signature") for message in messages
                  if message.get("role") == "assistant"]
    assert signatures == [round_record["thinkingSignature"] for round_record in authority["toolRounds"]]
