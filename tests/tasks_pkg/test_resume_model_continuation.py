"""A resumed Attempt runs on the conversation's accepted model.

A content_filter refusal is a per-request verdict, not a demotion: after an
in-attempt fallback a restart resume returns to the accepted model instead of
staying on the fallback for hours. Only a deliberate model choice the owner or
a lead made (pause → switch-model → continue) survives the resume. The paid
ledger still covers the fallback rounds, and a refusal on that first primary
round falls back again for that round only.
"""
from __future__ import annotations

import threading

import pytest

import lib.tasks_pkg.llm_fallback._retry as _fallback_retry
import lib.tasks_pkg.orchestrator._run as run_mod
from lib.agent_core.events import EventType
from lib.tasks_pkg.orchestrator._completion_receipt import (
    _project_execution_facts,
)
from lib.tasks_pkg.orchestrator._model_continuation import (
    continue_resumed_model,
    last_authoring_model,
)

pytestmark = pytest.mark.unit

ACCEPTED = 'claude-opus-5.5'
FALLBACK = 'kimi-k3.1-pro'


def _task(**config):
    return {
        'id': 'resume-task-001', 'convId': 'conv-resume', '_userId': 1,
        'config': dict(config), 'events': [], 'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }


def _cfg(rounds, **extra):
    return dict({'model': ACCEPTED, 'checkpointApiRounds': rounds}, **extra)


def _row(round_id, model, *, tag=None, authoring=True):
    row = {'round': round_id, 'model': model,
           'tag': tag or f'R{round_id}', 'usage': {'prompt_tokens': 100}}
    if not authoring:
        row['responseAuthoring'] = False
    return row


@pytest.fixture
def admitted(monkeypatch):
    """The refusal-fallback admission boundary, scripted per test."""
    state = {'admitted': FALLBACK}
    monkeypatch.setattr(_fallback_retry, '_get_fallback_model',
                        lambda task: state['admitted'])
    return state


def test_resume_returns_to_accepted_model_after_a_fallback(admitted):
    """(a) opus refused at R171; R262-R281 ran on the fallback → resume on opus."""
    rounds = [
        _row(170, ACCEPTED),
        _row(171, ACCEPTED, tag='R171-REFUSED', authoring=False),
        _row(262, FALLBACK, tag='R262-FALLBACK'),
        _row(281, FALLBACK),
    ]
    task = _task()
    assert continue_resumed_model(task, _cfg(rounds), ACCEPTED) == ACCEPTED
    assert '_fallback_model' not in task


def test_refused_and_discarded_charges_never_elect_a_model(admitted):
    """(c) The last paid AUTHORING row wins; refused/discarded charges skip."""
    rounds = [
        _row(170, ACCEPTED),
        _row(171, ACCEPTED, tag='R171-REFUSED', authoring=False),
        _row(282, FALLBACK, authoring=False),
        _row(283, FALLBACK, tag='R283-DISCARDED'),
        _row(True, 'bool-round-is-not-a-round', tag='R284'),
    ]
    assert continue_resumed_model(_task(), _cfg(rounds), ACCEPTED) == ACCEPTED
    assert last_authoring_model(rounds) == ACCEPTED
    # Only non-authoring rows exist → no continuation at all.
    assert last_authoring_model([
        _row(1, FALLBACK, tag='R1-REFUSED', authoring=False)]) == ''


def test_explicit_model_switch_on_continue_wins(admitted):
    """(d) pause → switch-model → continue stamps the acceptance-time marker;
    the user's deliberate choice beats the interrupted Attempt's model."""
    cfg = _cfg([_row(281, FALLBACK)], _resumeModelSwitchFrom=ACCEPTED)
    task = _task()
    assert continue_resumed_model(task, cfg, 'gpt-6') == 'gpt-6'
    assert '_fallback_model' not in task


def test_non_resume_and_matching_model_are_untouched(admitted):
    task = _task()
    assert continue_resumed_model(task, {'model': ACCEPTED}, ACCEPTED) == ACCEPTED
    assert '_fallback_model' not in task
    # Already running on the last authoring model → nothing to switch.
    task = _task()
    assert continue_resumed_model(
        task, _cfg([_row(281, FALLBACK)]), FALLBACK) == FALLBACK
    assert '_fallback_model' not in task


def test_done_event_keeps_the_accepted_model_on_resume(admitted):
    """(e) A resumed attempt reports the accepted model it actually runs on."""
    task = _task()
    model = continue_resumed_model(task, _cfg([_row(281, FALLBACK)]), ACCEPTED)
    done = {}
    _project_execution_facts(task, done, cfg=task['config'], model=model,
                             thinking_depth='high', api_rounds=[])
    assert done['actualModel'] == ACCEPTED
    assert 'fallbackModel' not in done


def test_run_task_resumes_on_the_accepted_model(
        monkeypatch, admitted):
    """The first resumed request uses the accepted model with its own preset
    and thinking; tools/context resolve once for it."""
    calls = []

    def resolve(cfg, task):
        model = cfg.get('model', ACCEPTED)
        mcfg = {
            'model': cfg.get('model', ACCEPTED), 'thinking_enabled': True,
            'thinking_depth': 'high', 'preset': 'high-preset',
            'max_tokens': 4096 if cfg.get('model') == FALLBACK else 8192,
            'temperature': 1.0, 'search_mode': 'off', 'response_format': None,
            'search_enabled': False, 'fetch_enabled': False, 'project_path': '',
            'project_enabled': False, 'code_exec_enabled': False,
            'memory_enabled': False,
        }
        calls.append(('resolve', mcfg['model']))
        task['model'] = model
        return mcfg

    monkeypatch.setattr(run_mod, 'resolve_and_seed_model_config', resolve)
    monkeypatch.setattr(run_mod, 'bind_provider_and_affinity', lambda *a: None)
    monkeypatch.setattr(run_mod, 'run_turn_prelude', lambda task, cfg, tid: cfg)
    monkeypatch.setattr(run_mod, 'snapshot_turn_input', lambda task: None)
    monkeypatch.setattr(run_mod, 'log_task_open', lambda task, tid: 0.0)
    monkeypatch.setattr(run_mod, 'make_vu_phase', lambda task: None)
    monkeypatch.setattr(run_mod, 'handle_abort_during_prep',
                        lambda *a, **k: False)
    monkeypatch.setattr(run_mod, 'start_prefetches', lambda *a, **k: None)
    monkeypatch.setattr(run_mod, 'stop_prefetches', lambda *a, **k: None)
    monkeypatch.setattr(run_mod, 'inject_context_and_emit_chips',
                        lambda **kw: calls.append(('context', kw['model'])))
    monkeypatch.setattr(run_mod, 'restore_resume_view', lambda **kw: None)
    monkeypatch.setattr(run_mod, 'apply_resume_state', lambda **kw: None)
    monkeypatch.setattr(run_mod, 'append_interrupted_effect_note',
                        lambda *a: None)
    monkeypatch.setattr(run_mod, 'inject_continue_tool_history', lambda **kw: [])

    class _PreparedHistory:
        injected_calls = []

    monkeypatch.setattr(run_mod, 'prepare_continue_tool_history',
                        lambda **kw: _PreparedHistory())

    def assemble(cfg, task, mcfg):
        calls.append(('tools', mcfg['model']))
        return [], False

    monkeypatch.setattr(run_mod, 'assemble_round_tools', assemble)

    def run_loop(request):
        calls.append(('loop', request.state.model, request.state.preset,
                      request.state.thinking_enabled, request.max_tokens))
        from types import SimpleNamespace
        return SimpleNamespace(last_round_num=0)

    monkeypatch.setattr(run_mod, 'run_root_agent_loop', run_loop)
    monkeypatch.setattr(run_mod, 'finalize_after_loop', lambda *a, **kw: None)
    monkeypatch.setattr(run_mod, 'finalize_task_lane', lambda *a, **kw: None)
    import lib.tasks_pkg.input_translation as input_translation
    monkeypatch.setattr(input_translation, 'apply_input_translation',
                        lambda *a, **k: None)
    monkeypatch.setattr(run_mod, 'check_autopilot_kick', lambda task: False)

    task = _task(
        model=ACCEPTED, thinkingEnabled=True, thinkingDepth='high',
        checkpointApiRounds=[
            _row(171, ACCEPTED, tag='R171-REFUSED', authoring=False),
            _row(281, FALLBACK),
        ])
    task['messages'] = [{'role': 'user', 'content': 'continue'}]
    task['config']['messages'] = task['messages']

    from lib.tasks_pkg.invocation import ROOT_INVOCATION_POLICY
    run_mod._execute_task_invocation.__wrapped__(task, policy=ROOT_INVOCATION_POLICY)

    assert calls[0] == ('resolve', ACCEPTED)
    assert ('resolve', FALLBACK) not in calls
    assert ('tools', ACCEPTED) in calls
    assert ('context', ACCEPTED) in calls
    loop = next(c for c in calls if c[0] == 'loop')
    assert loop == ('loop', ACCEPTED, 'high-preset', True, 8192)
    assert task['model'] == ACCEPTED
    assert '_fallback_model' not in task
    assert not any(e.get('type') == EventType.MODEL_FALLBACK
                   for e in task['events'])
