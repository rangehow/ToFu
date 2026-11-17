"""Fault-injection (chaos) suite: real sessions through the two-process runtime.

Every test drives Turns through the public v4 API of one isolated instance
(tests/chaos/instance.py: resident storage host, resident execution host and a
separate API host, each with its own data root, port and TMPDIR) whose only
model is the scripted provider (tests/chaos/fake_provider.py). Each test
injects one fault and asserts the per-fault contract written in
docs/DEFENSIVE_PATTERNS.md, "Fault contracts of the chaos suite":

  C1  no committed user/assistant content is lost;
  C2  no tool side effect runs twice;
  C3  the Turn ends in a correct settled state, with resume options when it
      did not complete;
  C4  a failure carries a clear user-visible error;
  C5  no process or lock outlives its owner.

The whole module is the bounded CI smoke: one instance per xdist worker, every
wait bounded by ``SETTLE_DEADLINE_S`` or the resident owners' own deadlines,
no sleeps that race a product timer. Run it with ``make test-chaos``.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import threading
import time
import uuid

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.client import EventStream, InstanceClient, TERMINAL_TURN_STATUSES
from tests.chaos.instance import ChaosInstance

pytestmark = [pytest.mark.api, pytest.mark.timeout(600)]

# A suite bound on one Turn's settlement under a fault, not a product timer:
# every product retry/backoff the faults below trigger fits well inside it.
SETTLE_DEADLINE_S = 180.0
TOOL_MARKER = 'chaos-tool-'


class Chaos:
    def __init__(self, instance: ChaosInstance, provider: fp.ScriptedProvider):
        self.instance = instance
        self.provider = provider
        self.client = InstanceClient(instance.base_url)
        self.workspace = instance.root / 'workspace'

    # turns -----------------------------------------------------------------
    def start_turn(self, steps: list[dict], *, prompt: str = 'Run the chaos script.'):
        nonce = self.provider.script(steps)
        conversation = f'chaos{uuid.uuid4().hex[:16]}'
        user_text = f'{prompt} [chaos turn={nonce}]'
        response = self.client.create_turn(conversation, user_text, workspace=str(self.workspace), first=True)
        return conversation, nonce, user_text, response

    def settle(self, conversation: str) -> tuple[dict, dict]:
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while True:
            turns = self.client.wait_settled(conversation, timeout=max(1.0, deadline - time.monotonic()))
            human = [t for t in turns if t.get('actor') == 'human']
            assistant = [t for t in turns if t.get('actor') == 'assistant']
            assert human and assistant, turns
            # Recovery settles a restart's Turn, then its settlement drain resumes
            # it: a resumable restart interruption with no explanation is still
            # on its way back (a paused or held one names why in its error).
            if not _resumes_by_itself(assistant[-1]) or time.monotonic() >= deadline:
                return human[-1], assistant[-1]
            time.sleep(0.2)

    # side effects ----------------------------------------------------------
    def effect_command(self, nonce: str, then: str = '') -> str:
        """A tool command whose side effect is one line in effects.log."""
        return f'echo {nonce} >> effects.log{"; " + then if then else ""}; echo effect-recorded'

    def effect_count(self, nonce: str) -> int:
        log = self.workspace / 'effects.log'
        return log.read_text().split().count(nonce) if log.exists() else 0

    @staticmethod
    def hang_command(nonce: str) -> str:
        # argv carries the marker so the leak scan finds exactly this child.
        return f'python3 -c "import time; time.sleep(3600)" {TOOL_MARKER}{nonce}'

    def tool_processes(self, nonce: str = '') -> list[dict]:
        return [p for p in self.instance.leaked_processes() if f'{TOOL_MARKER}{nonce}' in p['cmdline']]

    def wait_tool_process(self, nonce: str) -> int:
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while time.monotonic() < deadline:
            found = [p for p in self.tool_processes(nonce) if p['cmdline'].startswith('python3')]
            if found:
                return found[0]['pid']
            time.sleep(0.1)
        raise TimeoutError(f'tool process {nonce} never started')

    def wait_effect(self, nonce: str) -> None:
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while self.effect_count(nonce) == 0:
            assert time.monotonic() < deadline, 'the tool side effect never happened'
            time.sleep(0.1)

    # contract --------------------------------------------------------------
    def assert_contract(self, conversation: str, user_text: str, *, outcomes: set[str],
                        final_text: str | None = None, effects: dict[str, int] | None = None) -> dict:
        human, assistant = self.settle(conversation)
        settlement = assistant.get('settlement') or {}
        # C1: the user's committed input is intact.
        assert human['projection']['content'] == user_text
        # C3: settled, in an allowed outcome, and resumable unless completed.
        assert assistant['status'] in TERMINAL_TURN_STATUSES
        assert assistant['status'] in outcomes, (assistant['status'], settlement)
        if assistant['status'] != 'completed':
            assert settlement.get('resumeOptions'), f'unresumable {assistant["status"]} Turn: {settlement}'
            # C4: a clear, user-visible error, unless the owner stopped it.
            if settlement.get('cause') != 'user_abort':
                error = settlement.get('error') or {}
                assert (error.get('message') or '').strip(), f'no user-visible error: {settlement}'
        elif final_text is not None:
            # C1: the assistant's committed answer is the provider's final answer, once.
            content = assistant['projection'].get('content') or ''
            assert content.count(final_text) == 1, content
        # C2: each side effect ran exactly as often as the script asked.
        for nonce, count in (effects or {}).items():
            assert self.effect_count(nonce) == count, f'side effect {nonce} ran {self.effect_count(nonce)}x'
        # C5: no tool process of this Turn survives its settlement.
        assert not self.tool_processes(), self.tool_processes()
        return assistant


def _resumes_by_itself(turn: dict) -> bool:
    settlement = turn.get('settlement') or {}
    return (turn.get('status') == 'interrupted' and settlement.get('cause') == 'server_restart'
            and not settlement.get('error')
            and any(option.get('operation') in ('continue', 'checkpoint_resume')
                    for option in settlement.get('resumeOptions') or ()))


@contextlib.contextmanager
def _running_chaos(root: Path):
    provider = fp.ScriptedProvider().start()
    instance = ChaosInstance(root)
    try:
        instance.start()
        harness = Chaos(instance, provider)
        harness.client.install_route(provider.base_url)
        yield harness
    finally:
        try:
            instance.stop()
        finally:
            provider.stop()
            leaked, locks = instance.leaked_processes(), instance.held_locks()
            instance.kill_leaked()
    # C5 for the whole instance: every owner joined what it started.
    assert not leaked, f'processes outlived the instance: {leaked}'
    assert not locks, f'locks held after the instance stopped: {locks}'


@pytest.fixture(scope='module')
def chaos(tmp_path_factory):
    with _running_chaos(tmp_path_factory.mktemp('chaos')) as harness:
        yield harness


@pytest.fixture
def chaos_restart(tmp_path_factory):
    """A private instance for explicit restarts: the shared instance's kill specs
    accumulate crash boots toward the restart-storm guard (lib/shutdown_marker.py),
    which stands automatic resume down; sharing them would change what the
    later kill-and-recover specs observe."""
    with _running_chaos(tmp_path_factory.mktemp('chaos-restart')) as harness:
        yield harness


# ── provider faults ────────────────────────────────────────────────────────

def test_baseline_tool_round_trip(chaos):
    conversation, nonce, user_text, _ = chaos.start_turn([
        fp.tool('run_command', {'command': None}), fp.text('Baseline done.')])
    # The command needs the nonce, which exists only after scripting.
    chaos.provider._scripts[nonce][0]['input'] = {'command': chaos.effect_command(nonce)}
    chaos.assert_contract(conversation, user_text, outcomes={'completed'}, final_text='Baseline done.',
                          effects={nonce: 1})


def _scripted(chaos, build):
    """Script steps that need the turn nonce (``build(nonce) -> steps``)."""
    nonce = chaos.provider.script([])
    chaos.provider._scripts[nonce] = build(nonce)
    conversation = f'chaos{uuid.uuid4().hex[:16]}'
    user_text = f'Run the chaos script. [chaos turn={nonce}]'
    response = chaos.client.create_turn(conversation, user_text, workspace=str(chaos.workspace), first=True)
    return conversation, nonce, user_text, response


@pytest.mark.parametrize('fault', [
    pytest.param(fp.drop('Partial answer that never fini'), id='stream-drop-mid-response'),
    pytest.param(fp.http_error(429, error_type='rate_limit_error', retry_after='1'), id='http-429'),
    pytest.param(fp.http_error(503), id='http-503'),
    pytest.param(fp.http_error(500, error_type='api_error'), id='http-500'),
    pytest.param(fp.malformed(), id='malformed-sse'),
])
def test_transient_provider_fault_before_any_effect(chaos, fault):
    """One transient fault, then a healthy provider: the Turn completes, or it
    settles failed with an error and resume options (never hangs, never runs
    the round's tool twice)."""
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fault, fp.tool('run_command', {'command': chaos.effect_command(n)}), fp.text('Recovered answer.')])
    assistant = chaos.assert_contract(conversation, user_text, outcomes={'completed', 'failed'},
                                      final_text='Recovered answer.')
    if assistant['status'] == 'completed':
        assert chaos.effect_count(nonce) == 1
        # The dropped partial text is not committed beside the retried answer.
        assert 'never fini' not in (assistant['projection'].get('content') or '')
    else:
        assert chaos.effect_count(nonce) == 0


def _contains_phase(value, phase: str) -> bool:
    if isinstance(value, dict):
        return value.get('phase') == phase or any(_contains_phase(v, phase) for v in value.values())
    if isinstance(value, list):
        return any(_contains_phase(v, phase) for v in value)
    return False


def test_persistent_provider_outage_waits_visibly_until_stopped(chaos):
    """docs/modules/llm_io.md: the primary task call waits out a provider
    outage on the shared backoff schedule (failing strict turns fast was
    rejected); the wait is visible, paced, and Stop settles it resumably."""
    conversation, nonce, user_text, response = chaos.start_turn([fp.http_error(500, error_type='api_error')])
    started = time.monotonic()
    deadline = started + SETTLE_DEADLINE_S
    while True:
        turn = [t for t in chaos.client.turns(conversation) if t.get('actor') == 'assistant'][-1]
        if _contains_phase(turn.get('projection'), 'retrying'):
            break
        assert turn['status'] in ('pending', 'running'), turn.get('settlement')
        assert time.monotonic() < deadline, 'the outage never became visible'
        time.sleep(0.5)
    elapsed = time.monotonic() - started
    # Paced backoff, not a blind loop: well under one request per second.
    assert len(chaos.provider.requests(nonce)) <= max(3, elapsed), (len(chaos.provider.requests(nonce)), elapsed)
    chaos.client.request('POST', f"/api/v4/attempts/{response['attempt']['attemptId']}/abort", {})
    assistant = chaos.assert_contract(conversation, user_text, outcomes={'interrupted'})
    assert assistant['settlement']['cause'] == 'user_abort'


def test_invalid_tool_json_is_answered_without_running_the_tool(chaos):
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.invalid_tool_json('run_command', '{"command": "echo ' + n + ' >> effects.log", '),
        fp.text('Handled invalid arguments.')])
    chaos.assert_contract(conversation, user_text, outcomes={'completed'}, final_text='Handled invalid arguments.',
                          effects={nonce: 0})
    # Either the model is told its call failed, or the unusable round is
    # regenerated; the malformed call itself never runs (effects above).
    assert len(chaos.provider.requests(nonce)) >= 2


# ── tool faults ────────────────────────────────────────────────────────────

def test_tool_subprocess_killed_mid_call(chaos):
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.hang_command(n)}), fp.text('Saw the killed tool. [END_TURN: done]')])
    pid = chaos.wait_tool_process(nonce)
    os.kill(pid, signal.SIGKILL)
    chaos.assert_contract(conversation, user_text, outcomes={'completed'}, final_text='Saw the killed tool.')
    result = chaos.provider.requests(nonce)[1]['tool_results'][0]
    assert 'exit' in str(result.get('content')).lower(), result


def test_hung_tool_is_stopped_by_the_owner(chaos):
    conversation, nonce, user_text, response = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.hang_command(n)}), fp.text('unreachable')])
    chaos.wait_tool_process(nonce)
    attempt = response['attempt']['attemptId']
    chaos.client.request('POST', f'/api/v4/attempts/{attempt}/abort', {})
    chaos.assert_contract(conversation, user_text, outcomes={'interrupted'})


# ── host faults ────────────────────────────────────────────────────────────

def _held_turn(chaos, final: str):
    """A Turn whose provider stream holds open until ``release`` is set."""
    release = threading.Event()
    conversation, nonce, user_text, response = _scripted(chaos, lambda n: [
        fp.hold('Streaming before the fault. ', release, fp.text(final))])
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not chaos.provider.held.is_set():
        assert time.monotonic() < deadline, 'the provider stream never opened'
        time.sleep(0.05)
    return conversation, nonce, user_text, response, release


def test_execution_host_killed_during_a_tool_and_recovered(chaos):
    """SIGKILL of the execution host while its tool runs: recovery retires the
    orphaned tool group and never replays the side effect."""
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.effect_command(n, chaos.hang_command(n))}),
        fp.text('Continued after recovery.')])
    chaos.wait_tool_process(nonce)
    chaos.wait_effect(nonce)
    chaos.instance.kill_execution_host()
    chaos.instance.recover_execution_host()
    chaos.assert_contract(conversation, user_text, outcomes={'completed', 'interrupted', 'failed'},
                          final_text='Continued after recovery.', effects={nonce: 1})


def test_execution_host_restarted_during_a_tool_drains_and_continues_once(chaos_restart):
    """An explicit restart while a tool runs: the old host drains within the
    manager's stop budget without a kill, and the next generation continues
    the Turn without replaying the side effect."""
    chaos = chaos_restart
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.effect_command(n, chaos.hang_command(n))}),
        fp.text('Continued after the restart.')])
    chaos.wait_tool_process(nonce)
    chaos.wait_effect(nonce)
    drained = chaos.instance.restart_execution_host()
    print(f'execution host drained in {drained:.1f}s')
    chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                          final_text='Continued after the restart.', effects={nonce: 1})


def test_execution_host_restart_hands_a_background_command_to_the_next_generation(chaos_restart):
    """A background command still running at an explicit restart does not hold
    the old host past its stop budget; it survives the drain by design
    (docs/modules/task_engine.md, durable background records), the next
    generation reattaches it, delivers its one result, and never starts it again."""
    chaos = chaos_restart
    release = chaos.workspace / f'release-{uuid.uuid4().hex}'
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.effect_command(
            n, f'while [ ! -e {release.name} ]; do sleep 0.1; done # {TOOL_MARKER}{n}'),
            'background': True}),
        fp.text('Started it in the background.'),
        fp.text('Saw the background result.')])
    chaos.wait_effect(nonce)
    chaos.settle(conversation)
    assert chaos.tool_processes(nonce), 'the background command ended before the restart'
    drained = chaos.instance.restart_execution_host()
    print(f'execution host drained in {drained:.1f}s')
    assert chaos.tool_processes(nonce), 'the restart lost the running background command'
    release.touch()
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while True:
        turns = chaos.client.wait_settled(conversation, timeout=SETTLE_DEADLINE_S)
        answers = [t['projection'].get('content') or '' for t in turns if t.get('actor') == 'assistant']
        if any('Saw the background result.' in answer for answer in answers):
            break
        assert time.monotonic() < deadline, answers
        time.sleep(0.2)
    assert sum(answer.count('Saw the background result.') for answer in answers) == 1, answers
    assert chaos.effect_count(nonce) == 1
    assert not chaos.tool_processes(nonce), chaos.tool_processes(nonce)


def test_execution_host_killed_mid_stream_and_recovered(chaos):
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer after recovery.')
    try:
        chaos.instance.kill_execution_host()
    finally:
        release.set()
    chaos.instance.recover_execution_host()
    chaos.assert_contract(conversation, user_text, outcomes={'completed', 'interrupted', 'failed'},
                          final_text='Answer after recovery.')


def test_api_host_restart_mid_stream_resumes_the_client_stream(chaos):
    """The execution host keeps the Turn running; the reconnected client
    resumes from Last-Event-ID without a gap or a duplicate."""
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer across the API restart.')
    stream = EventStream(chaos.instance.base_url, conversation)
    seen = []
    for event_id, _event, _data in stream.events():
        seen.append(event_id)
        if len(seen) >= 2:
            break
    cursor = stream.last_id
    stream.close()
    try:
        chaos.instance.restart_api()
    finally:
        release.set()
    chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                          final_text='Answer across the API restart.')
    # Everything committed after the client's cursor replays; the stream then
    # idles (heartbeats are SSE comments), which ends the read.
    resumed = EventStream(chaos.instance.base_url, conversation, after=cursor, idle_seconds=5)
    replayed = []
    try:
        for event_id, _event, _data in resumed.events():
            if event_id:
                replayed.append(event_id)
    finally:
        resumed.close()
    assert len(replayed) == len(set(replayed)), 'the replay repeated an event'
    assert not set(replayed) & set(filter(None, seen)), 'reconnect replayed events the client already had'
    assert replayed, 'reconnect delivered nothing after the restart'


def test_client_disconnect_does_not_cancel_the_turn(chaos):
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer nobody watched live.')
    stream = EventStream(chaos.instance.base_url, conversation)
    for _event in stream.events():
        break
    stream.close()  # the browser tab went away mid-stream
    release.set()
    chaos.assert_contract(conversation, user_text, outcomes={'completed'}, final_text='Answer nobody watched live.')


def test_storage_host_frozen_mid_turn(chaos):
    """An engine that stops answering (SIGSTOP), then resumes: the Turn settles."""
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer after the storage stall.')
    frozen = chaos.instance.freeze_storage()
    try:
        release.set()
        time.sleep(5)  # the stall the attempt experiences; not a product timer
    finally:
        chaos.instance.thaw_storage(frozen)
    chaos.assert_contract(conversation, user_text, outcomes={'completed', 'failed', 'interrupted'},
                          final_text='Answer after the storage stall.')


def test_storage_host_killed_mid_turn_and_restarted(chaos):
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer after the storage restart.')
    record = chaos.instance.storage_record()
    try:
        for pid in chaos.instance._storage_pids():
            os.kill(pid, signal.SIGKILL)
        chaos.instance._wait(lambda: not chaos.instance._storage.host_matches(record), 'storage host death')
    finally:
        release.set()
    chaos.instance._storage.stop(30, retire_failed_close=True)
    chaos.instance.start_storage()
    chaos.assert_contract(conversation, user_text, outcomes={'completed', 'failed', 'interrupted'},
                          final_text='Answer after the storage restart.')
    # The instance still serves new work afterwards.
    after, nonce2, text2, _ = chaos.start_turn([fp.text('Storage is back.')])
    chaos.assert_contract(after, text2, outcomes={'completed'}, final_text='Storage is back.')


def test_disk_full_on_the_data_dir(chaos):
    conversation, nonce, user_text, _, release = _held_turn(chaos, 'Answer written to a full disk.')
    chaos.instance.set_medium(enospc=True)
    try:
        release.set()
        time.sleep(5)  # writes during this window fail with ENOSPC
    finally:
        chaos.instance.set_medium(enospc=False)
    assert chaos.instance.medium_stats().get('enospc_returns', 0) > 0, 'the fault never reached storage'
    chaos.assert_contract(conversation, user_text, outcomes={'completed', 'failed', 'interrupted'},
                          final_text='Answer written to a full disk.')
    after, _n, text2, _ = chaos.start_turn([fp.text('Disk has space again.')])
    chaos.assert_contract(after, text2, outcomes={'completed'}, final_text='Disk has space again.')


def test_slow_storage_medium(chaos):
    chaos.instance.set_medium(fsync_us=200_000)
    try:
        conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
            fp.tool('run_command', {'command': chaos.effect_command(n)}), fp.text('Slow but done.')])
        chaos.assert_contract(conversation, user_text, outcomes={'completed'}, final_text='Slow but done.',
                              effects={nonce: 1})
    finally:
        chaos.instance.set_medium(fsync_us=0)


# ── MCP faults ─────────────────────────────────────────────────────────────

def _mcp_state(chaos) -> Path:
    return chaos.instance.root / 'mcp-state'


def _mcp_calls(chaos) -> list[str]:
    log = _mcp_state(chaos) / 'calls.log'
    return log.read_text().splitlines() if log.exists() else []


def _mcp_turn(chaos, calls: list[tuple[str, dict]], final: str):
    """One Turn that reaches MCP tools through Tool Search's execute_tools."""
    from tests.chaos.client import CHAT_CONFIG
    steps = [fp.tool('execute_tools', {'calls': [{'name': name, 'arguments': arguments}]})
             for name, arguments in calls] + [fp.text(f'{final} [END_TURN: done]')]
    nonce = chaos.provider.script(steps)
    conversation = f'chaos{uuid.uuid4().hex[:16]}'
    user_text = f'Use the chaos MCP server. [chaos turn={nonce}]'
    chaos.client.create_turn(conversation, user_text, workspace=str(chaos.workspace), first=True,
                             config={'tools': {**CHAT_CONFIG['tools'], 'toolSearch': 'local'}})
    return conversation, nonce, user_text


def _mcp_results(chaos, nonce: str) -> list[str]:
    return [str(result.get('content')) for request in chaos.provider.requests(nonce)
            for result in request['tool_results']]


@pytest.fixture
def chaos_mcp(chaos):
    """The chaos MCP server, added and connected at runtime through the API."""
    import sys
    from tests.chaos.instance import CHECKOUT
    state = _mcp_state(chaos)
    state.mkdir(exist_ok=True)
    (state / 'schema').write_text('v1')
    chaos.client.request('POST', '/api/v4/mcp/servers', {
        'name': 'chaos', 'command': sys.executable, 'transport': 'stdio',
        'args': [str(CHECKOUT / 'tests' / 'chaos' / 'mcp_server.py'), str(state)]})
    chaos.client.request('POST', '/api/v4/mcp/connect', {'server': 'chaos'})
    try:
        yield chaos
    finally:
        chaos.client.request('DELETE', '/api/v4/mcp/servers/chaos')


def _wait_mcp_reachable(chaos) -> None:
    """The execution host connects asynchronously after the API acknowledged."""
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    # A note of its own: an earlier probe's line must not answer this wait.
    note = f'probe-{uuid.uuid4().hex[:8]}'
    while True:
        conversation, nonce, user_text = _mcp_turn(
            chaos, [('mcp__chaos__chaos_record', {'note': note})], 'probe')
        chaos.settle(conversation)
        if f'record {note}' in _mcp_calls(chaos):
            return
        assert time.monotonic() < deadline, _mcp_results(chaos, nonce)


def test_mcp_server_added_at_runtime_serves_execution_host_turns(chaos_mcp):
    """Found by this suite (R4): the API host's bridge connected the server,
    the execution host's never did, so every Turn answered tool_not_enabled."""
    chaos = chaos_mcp
    # The execution host connects after the API acknowledged; each Turn either
    # runs the call once or reports the tool as not yet available.
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while True:
        conversation, nonce, user_text = _mcp_turn(
            chaos, [('mcp__chaos__chaos_record', {'note': 'first'})], 'Recorded.')
        chaos.assert_contract(conversation, user_text, outcomes={'completed'})
        if 'record first' in _mcp_calls(chaos):
            break
        assert time.monotonic() < deadline, _mcp_results(chaos, nonce)
    assert _mcp_calls(chaos).count('record first') == 1


def test_mcp_server_crash_mid_call_then_restart_with_a_new_schema(chaos_mcp):
    """A server that dies mid-call: the call fails visibly and is not replayed
    (C2); the restarted server's changed schema is what the next call uses."""
    chaos = chaos_mcp
    _wait_mcp_reachable(chaos)
    # The upgraded server is installed while the old process still runs; its
    # crash is what brings the new version (``message`` instead of ``note``) up.
    (_mcp_state(chaos) / 'schema').write_text('v2')
    conversation, nonce, user_text = _mcp_turn(chaos, [('mcp__chaos__chaos_crash', {})], 'Saw the crash.')
    chaos.assert_contract(conversation, user_text, outcomes={'completed'})
    assert _mcp_calls(chaos).count('crash') == 1, 'a crashing call was replayed'
    crash_result = ' '.join(_mcp_results(chaos, nonce)).lower()
    assert 'error' in crash_result, crash_result
    conversation, nonce, user_text = _mcp_turn(
        chaos, [('mcp__chaos__chaos_record', {'message': 'after-restart'})], 'Recorded after restart.')
    chaos.assert_contract(conversation, user_text, outcomes={'completed'})
    assert 'record after-restart' in _mcp_calls(chaos), _mcp_results(chaos, nonce)
    launches = (_mcp_state(chaos) / 'launches.log').read_text().split()
    assert launches[-1] == 'v2', launches


def test_mcp_tool_disabled_in_settings_is_refused_in_execution_host_turns(chaos_mcp):
    """Found by this suite (R5): disabling a tool updated only the API host's
    bridge, so Turns in the execution host kept running it."""
    chaos = chaos_mcp
    _wait_mcp_reachable(chaos)
    chaos.client.request('PUT', '/api/v4/mcp/servers/chaos/tools', {'disabled_tools': ['chaos_record']})
    before = list(_mcp_calls(chaos))
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while True:
        conversation, nonce, user_text = _mcp_turn(
            chaos, [('mcp__chaos__chaos_record', {'note': 'forbidden'})], 'Refused.')
        chaos.assert_contract(conversation, user_text, outcomes={'completed'})
        if 'record forbidden' not in _mcp_calls(chaos)[len(before):]:
            break
        before = list(_mcp_calls(chaos))
        assert time.monotonic() < deadline, 'the disabled MCP tool kept running in Turns'
    assert 'record forbidden' not in _mcp_calls(chaos)[len(before):]


def test_mcp_server_connecting_after_a_restart_serves_resumed_and_new_turns(chaos_mcp):
    """Found live 2026-11-17: after a restart the execution host resumed a
    Turn and admitted new ones before its MCP auto-connect finished. Their
    tool authority was assembled with the server absent, and the per-round
    refresh only re-admitted names present at startup, so for the rest of
    the Turn every call answered tool_not_enabled although the server was
    enabled and connected."""
    chaos = chaos_mcp
    _wait_mcp_reachable(chaos)
    state = _mcp_state(chaos)
    harmless = fp.tool('run_command', {'command': 'echo waited-for-mcp'})

    def held_then(release, note):
        # The hold opens a model round assembled while the server is still
        # starting; the harmless call ends it, so the MCP call that follows
        # comes from a fresh round after the server connected.
        return [fp.hold('Waiting for MCP. ', release, harmless),
                fp.tool('execute_tools', {'calls': [{'name': 'mcp__chaos__chaos_record',
                                                     'arguments': {'note': note}}]}),
                fp.text(f'Recorded {note}. [END_TURN: done]')]

    from tests.chaos.client import CHAT_CONFIG
    config = {'tools': {**CHAT_CONFIG['tools'], 'toolSearch': 'local'}}

    def start(steps, prompt):
        nonce = chaos.provider.script(steps)
        conversation = f'chaos{uuid.uuid4().hex[:16]}'
        user_text = f'{prompt} [chaos turn={nonce}]'
        chaos.client.create_turn(conversation, user_text, workspace=str(chaos.workspace), first=True,
                                 config=config)
        return conversation, nonce, user_text

    def wait_requests(nonce, count, what):
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while len(chaos.provider.requests(nonce)) < count:
            assert time.monotonic() < deadline, what
            time.sleep(0.05)

    interrupted, resumed_release, new_release = threading.Event(), threading.Event(), threading.Event()
    resumed = start([fp.hold('Before the restart. ', interrupted, fp.text('unreachable')),
                     *held_then(resumed_release, 'resumed')], 'Use the chaos MCP server.')
    wait_requests(resumed[1], 1, 'the Turn to resume never reached the provider')
    (state / 'hold-initialize').touch()
    try:
        try:
            chaos.instance.recover_execution_host()
        finally:
            interrupted.set()
        new = start(held_then(new_release, 'new'), 'Use the chaos MCP server.')
        # Both Turns now run a round assembled before the server connected.
        wait_requests(resumed[1], 2, 'the restart never resumed the interrupted Turn')
        wait_requests(new[1], 1, 'the new Turn never reached the provider')
        (state / 'hold-initialize').unlink()
        _wait_mcp_reachable(chaos)
    finally:
        (state / 'hold-initialize').unlink(missing_ok=True)
        resumed_release.set()
        new_release.set()
    for conversation, nonce, user_text in (resumed, new):
        chaos.assert_contract(conversation, user_text, outcomes={'completed'})
    calls = _mcp_calls(chaos)
    assert calls.count('record resumed') == 1, _mcp_results(chaos, resumed[1])
    assert calls.count('record new') == 1, _mcp_results(chaos, new[1])


def _bind_coordinator(chaos) -> str:
    """Make the workspace a Git project and bind a Project Coordinator to it."""
    import subprocess
    from tests.chaos.fake_provider import MODEL_ID
    workspace = str(chaos.workspace)
    if not (chaos.workspace / '.git').exists():
        for argv in (['init', '-q'], ['-c', 'user.name=chaos', '-c', 'user.email=chaos@example.invalid',
                                      'commit', '-q', '--allow-empty', '-m', 'chaos project']):
            subprocess.run(['git', '-C', workspace, *argv], check=True, capture_output=True)
    folder = chaos.client.request('POST', '/api/v4/folders', {
        'name': 'Chaos project', 'projectPaths': [workspace]})
    folder = folder.get('data', folder)
    key = {'Idempotency-Key': f'chaos-{uuid.uuid4().hex}'}
    binding = chaos.client.request('POST', '/api/v4/project/coordination/create', {
        'path': workspace, 'folderId': folder['id'], 'expectedRevision': 0}, headers=key)
    binding = binding.get('data', binding)
    chaos.client.request('POST', '/api/v4/router/worker-model', {
        'path': workspace, 'workerModel': MODEL_ID},
        headers={'Idempotency-Key': f'chaos-{uuid.uuid4().hex}'})
    return binding['conversationId']


def test_worker_delegated_by_a_coordinator_that_started_before_mcp_connected_reaches_it(chaos_mcp):
    """Found 2026-11-17: a Worker's grant was the tool names its Coordinator's
    Turn held when it delegated. A Coordinator that started after a restart,
    before MCP auto-connect finished, granted no MCP tool, so its Workers
    never reached a server the owner had enabled, connected or not. The grant
    now names the enabled servers, and the Worker reaches them once they connect."""
    chaos = chaos_mcp
    _wait_mcp_reachable(chaos)
    state = _mcp_state(chaos)
    coordinator = _bind_coordinator(chaos)
    worker_release = threading.Event()
    worker_nonce = chaos.provider.script([
        fp.hold('Waiting for MCP. ', worker_release, fp.tool('run_command', {'command': 'echo waited'})),
        fp.tool('execute_tools', {'calls': [{'name': 'mcp__chaos__chaos_record',
                                             'arguments': {'note': 'worker'}}]}),
        fp.text('Recorded worker. [END_TURN: done]')])
    coordinator_nonce = chaos.provider.script([
        fp.tool('delegate_project_work', {
            'title': 'Record through the chaos MCP server',
            'brief': f'Use the chaos MCP server. [chaos turn={worker_nonce}]',
            'idempotency_key': 'chaos-mcp-worker'}),
        fp.text('Delegated. [END_TURN: done]')])

    def wait_requests(nonce, count, what):
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while len(chaos.provider.requests(nonce)) < count:
            assert time.monotonic() < deadline, what
            time.sleep(0.05)

    (state / 'hold-initialize').touch()
    try:
        chaos.instance.recover_execution_host()
        coordinator_text = f'Delegate the MCP work. [chaos turn={coordinator_nonce}]'
        chaos.client.create_turn(coordinator, coordinator_text, workspace=str(chaos.workspace),
                                 first=False)
        # Both the Coordinator's Turn and its Worker's first round are
        # assembled while the server's initialize is still held.
        chaos.assert_contract(coordinator, coordinator_text, outcomes={'completed'})
        wait_requests(worker_nonce, 1, 'the delegated Worker never reached the provider')
        (state / 'hold-initialize').unlink()
        _wait_mcp_reachable(chaos)
    finally:
        (state / 'hold-initialize').unlink(missing_ok=True)
        worker_release.set()
    wait_requests(worker_nonce, 3, 'the Worker never finished its MCP call round')
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while 'record worker' not in _mcp_calls(chaos):
        assert time.monotonic() < deadline, _mcp_results(chaos, worker_nonce)
        time.sleep(0.1)
    assert _mcp_calls(chaos).count('record worker') == 1, _mcp_results(chaos, worker_nonce)


# ── MCP across execution shards (lib/mcp/bridge_relay.py) ──────────────────

def _shard_pids(chaos) -> list[int]:
    pids = []
    for row in chaos.instance.leaked_processes():
        try:
            environ = Path(f'/proc/{row["pid"]}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        if b'TOFU_PROCESS_ROLE=execution_shard' in environ:
            pids.append(row['pid'])
    return pids


def _launches(chaos, name: str) -> int:
    log = chaos.instance.root / 'mcp-pair' / name / 'launches.log'
    return len(log.read_text().split('\n')) - 1 if log.exists() else 0


@pytest.fixture
def chaos_mcp_pair(chaos):
    """Mocks named like the live owner's hope and github servers."""
    import sys
    from tests.chaos.instance import CHECKOUT
    for name in ('hope', 'github'):
        state = chaos.instance.root / 'mcp-pair' / name
        state.mkdir(parents=True, exist_ok=True)
        (state / 'schema').write_text('v1')
        chaos.client.request('POST', '/api/v4/mcp/servers', {
            'name': name, 'command': sys.executable, 'transport': 'stdio',
            'args': [str(CHECKOUT / 'tests' / 'chaos' / 'mcp_server.py'), str(state)]})
        chaos.client.request('POST', '/api/v4/mcp/connect', {'server': name})
    try:
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        note = f'probe-{uuid.uuid4().hex[:8]}'
        while True:
            conversation, nonce, _text = _mcp_turn(chaos, [
                ('mcp__hope__chaos_record', {'note': note}),
                ('mcp__github__chaos_record', {'note': note})], 'probe')
            chaos.settle(conversation)
            if all(f'record {note}' in _pair_calls(chaos, name) for name in ('hope', 'github')):
                break
            assert time.monotonic() < deadline, _mcp_results(chaos, nonce)
            note = f'probe-{uuid.uuid4().hex[:8]}'
        yield chaos
    finally:
        for name in ('hope', 'github'):
            chaos.client.request('DELETE', f'/api/v4/mcp/servers/{name}')


def _pair_calls(chaos, name: str) -> list[str]:
    log = chaos.instance.root / 'mcp-pair' / name / 'calls.log'
    return log.read_text().splitlines() if log.exists() else []


def _held_mcp_turns(chaos, count: int, tool: str, release: threading.Event):
    from tests.chaos.client import CHAT_CONFIG
    config = {'tools': {**CHAT_CONFIG['tools'], 'toolSearch': 'local'}}
    turns = []
    for index in range(count):
        note = f'{tool}-{index}-{uuid.uuid4().hex[:6]}'
        calls = [{'name': f'mcp__{server}__{tool}', 'arguments': {'note': note}}
                 for server in (('hope', 'github') if tool == 'chaos_record' else ('hope',))]
        nonce = chaos.provider.script([
            fp.hold('Holding. ', release, fp.tool('execute_tools', {'calls': calls})),
            fp.text(f'Done {note}. [END_TURN: done]')])
        conversation = f'chaos{uuid.uuid4().hex[:16]}'
        user_text = f'Use the MCP servers. [chaos turn={nonce}]'
        chaos.client.create_turn(conversation, user_text, workspace=str(chaos.workspace), first=True,
                                 config=config)
        turns.append((conversation, nonce, user_text, note))
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not all(chaos.provider.requests(nonce) for _c, nonce, _u, _n in turns):
        assert time.monotonic() < deadline, 'the held Turns never all reached the provider'
        time.sleep(0.1)
    return turns


def test_concurrent_sessions_across_shards_reach_one_mcp_server_set(chaos_mcp_pair):
    """Found live 2026-11-17: every execution shard ran its own MCP auto-connect
    (53 processes x 7 servers) and a shard whose npx launch lost the npm-cache
    race lacked github for its life. Shards now relay to the execution host's
    servers: 20 concurrent sessions across shards call both mocks, every call
    runs once, and no shard launches a server."""
    chaos = chaos_mcp_pair
    launches = {name: _launches(chaos, name) for name in ('hope', 'github')}
    release = threading.Event()
    try:
        turns = _held_mcp_turns(chaos, 20, 'chaos_record', release)
        assert _shard_pids(chaos), 'the burst ran without execution shards'
    finally:
        release.set()
    for conversation, _nonce, user_text, _note in turns:
        chaos.assert_contract(conversation, user_text, outcomes={'completed'})
    for name in ('hope', 'github'):
        calls = _pair_calls(chaos, name)
        for _c, nonce, _u, note in turns:
            assert calls.count(f'record {note}') == 1, (name, note, _mcp_results(chaos, nonce))
        assert _launches(chaos, name) == launches[name], f'a shard launched its own {name}'


def test_execution_shards_killed_mid_mcp_call_never_replay_it(chaos_mcp_pair):
    """A shard dies while its Turn's MCP call is in flight on the execution
    host's server: the owner withdraws the call, the Turn settles, and the call
    is never sent again by the Turn's takeover."""
    chaos = chaos_mcp_pair
    hold = chaos.instance.root / 'mcp-pair' / 'hope' / 'hold-call'
    hold.touch()
    release = threading.Event()
    try:
        turns = _held_mcp_turns(chaos, 6, 'chaos_wait', release)
        release.set()
        deadline = time.monotonic() + SETTLE_DEADLINE_S
        while not all(f'wait {note}' in _pair_calls(chaos, 'hope') for *_rest, note in turns):
            assert time.monotonic() < deadline, 'the MCP calls never reached the server'
            time.sleep(0.1)
        shards = _shard_pids(chaos)
        assert shards, 'no execution shard held a call'
        for pid in shards:
            os.kill(pid, signal.SIGKILL)
    finally:
        release.set()
        hold.unlink(missing_ok=True)
    for conversation, _nonce, user_text, _note in turns:
        chaos.assert_contract(conversation, user_text, outcomes=set(TERMINAL_TURN_STATUSES))
    calls = _pair_calls(chaos, 'hope')
    for _c, nonce, _u, note in turns:
        assert calls.count(f'wait {note}') == 1, (note, _mcp_results(chaos, nonce))
