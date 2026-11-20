"""The simulated crash frontier's text must already be in the authority.

This checks same-process reference semantics, not process-crash recovery.
"""

import base64
import json
from pathlib import Path

import pytest

from tests.rust_parity_engine import reference

pytestmark = pytest.mark.api


@pytest.mark.parametrize(('name', 'frontier_content', 'final_content'), [
    ('restart_after_first_request', 'Writing. ',
     'Writing. Confirmed after the restart.'),
    ('restart_after_tool_round', 'Confirmed after the restart.',
     'Confirmed after the restart.Final confirmation after the resume.'),
])
def test_restart_text_is_durable_before_resume(
        monkeypatch, name, frontier_content, final_content):
    cases = [json.loads(line) for line in
             Path(__file__).with_name('cases.jsonl').read_text().splitlines()]
    case = next(case for case in cases if case['name'] == name)
    install = reference._CaseReplaySession.install
    observed = []

    def install_with_frontier_check(session, patch):
        install(session, patch)
        from lib.tasks_pkg.orchestrator import _root_agent_loop
        from lib.turn_lifecycle import get_turn

        drive = _root_agent_loop.run_root_agent_loop

        def check_frontier(request):
            try:
                return drive(request)
            except reference.SimulatedCrash:
                task = request.task
                # get_turn queries turn.get; it does not overlay the live task.
                turn = get_turn(task['convId'], task['_turnId'], user_id=1)
                observed.append(turn['projection']['content'])
                assert observed[-1] == frontier_content
                assert observed[-1] == task['content']
                raise

        patch.setattr(_root_agent_loop, 'run_root_agent_loop', check_frontier)
        return session

    monkeypatch.setattr(reference._CaseReplaySession, 'install',
                        install_with_frontier_check)
    parts = {part['name']: json.loads(base64.b64decode(part['b64']))
             for part in reference.engine_session(case['input'])}
    assert observed == [frontier_content]
    assert parts['outcome']['content'] == final_content
