"""poll/kill rows name the work a handle stands for, never only the raw id.

A ``Poll: bg_42e122c32e6b (wait)`` row tells the user nothing about what the
turn is waiting on. The handle is resolved to its command at display time
(live registry first, durable record after a restart) and the raw id is kept
only when nothing resolves it (swarm agent ids, task ids).
"""

import pytest

pytestmark = pytest.mark.unit

BG = 'bg_42e122c32e6b'
PRECERTIFY = ('.venv/bin/python3 -m serverctl_pkg.engine_precertify '
              '--run --project ROOT')


def test_poll_label_names_the_running_command():
    from lib.tasks_pkg.tool_display import tool_round_label

    label = tool_round_label('poll', {
        'id': BG, 'wait_s': 1500, '_handle_labels': {BG: PRECERTIFY}})
    assert label == f'Poll: {PRECERTIFY} (wait)'
    # Without wait_s the same row does not claim to be waiting.
    assert tool_round_label('poll', {
        'id': BG, '_handle_labels': {BG: PRECERTIFY}}) == f'Poll: {PRECERTIFY}'


def test_poll_multiple_handles_show_first_command_and_remainder():
    from lib.tasks_pkg.tool_display import tool_round_label

    other = 'bg_0000000000ff'
    label = tool_round_label('poll', {
        'ids': [BG, other], '_handle_labels': {BG: PRECERTIFY}})
    assert label == f'Poll 2 handles: {PRECERTIFY} +1'


def test_poll_keeps_raw_handle_when_nothing_resolves_it():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label('poll', {'id': 'agent-7', 'wait_s': 30}) == (
        'Poll: agent-7 (wait)')
    assert tool_round_label('kill', {'id': 'agent-7'}) == 'Kill: agent-7'


def test_kill_label_names_the_command():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label('kill', {
        'id': BG, '_handle_labels': {BG: PRECERTIFY}}) == f'Kill: {PRECERTIFY}'


def test_enrichment_resolves_live_registry_and_never_mutates_args(monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    seen = []

    def _check(command_id, *, user_id=None, wait_s=0, task_id=None):
        seen.append((command_id, user_id))
        if command_id == BG:
            return {'command_id': BG, 'command': PRECERTIFY}
        return None

    monkeypatch.setattr(background_commands, 'check_background_command', _check)

    args = {'id': BG, 'wait_s': 1500}
    enriched = enrich_display_args('poll', args, task={'_userId': '7'})
    assert enriched['_handle_labels'] == {BG: PRECERTIFY}
    assert '_handle_labels' not in args
    assert ('7' in [uid for _, uid in seen]) or (7 in [uid for _, uid in seen])


def test_enrichment_falls_back_to_durable_record_after_restart(monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    monkeypatch.setattr(background_commands, 'check_background_command',
                        lambda *a, **k: None)
    calls = []

    def _record(command_id, *, user_id, conv_id):
        calls.append((command_id, user_id, conv_id))
        return {'command_id': command_id, 'command': PRECERTIFY}

    monkeypatch.setattr(background_commands, 'recorded_background_command',
                        _record)

    enriched = enrich_display_args(
        'poll', {'id': BG}, task={'_userId': '7'}, conv_id='mwn45kg4ryb95h')
    assert enriched['_handle_labels'] == {BG: PRECERTIFY}
    assert all(c[2] == 'mwn45kg4ryb95h' for c in calls)


def test_enrichment_skips_non_background_handles(monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    def _boom(*a, **k):
        raise AssertionError('task/agent handles must not hit the bg registry')

    monkeypatch.setattr(background_commands, 'check_background_command', _boom)
    monkeypatch.setattr(background_commands, 'recorded_background_command', _boom)

    args = {'id': 'agent-7'}
    assert enrich_display_args('poll', args, task={'_userId': '7'}) is args


def test_long_command_is_a_single_bounded_line(monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    long_command = 'cd /x && ' + 'echo step; ' * 40 + '\nsecond line'
    monkeypatch.setattr(
        background_commands, 'check_background_command',
        lambda *a, **k: {'command': long_command})

    label = enrich_display_args(
        'poll', {'id': BG}, task={'_userId': 7})['_handle_labels'][BG]
    assert '\n' not in label and len(label) <= 80 and label.endswith('…')


def test_build_tool_round_entry_labels_poll_from_live_command(monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry

    monkeypatch.setattr(
        background_commands, 'check_background_command',
        lambda *a, **k: {'command': PRECERTIFY})

    _, entry, event = _build_tool_round_entry(
        'poll', {'id': BG, 'wait_s': 1500}, 'tc_poll', '{}', 0, False,
        task={'_userId': 7})
    assert entry['query'] == f'Poll: {PRECERTIFY} (wait)'
    assert event['query'] == entry['query']