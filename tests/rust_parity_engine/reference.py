"""Python reference implementation of the Rust ``engine_session`` parity boundary.

README
======

One case drives the REAL root agent loop
(``lib/tasks_pkg/orchestrator/_root_agent_loop.run_root_agent_loop`` +
``_post_loop.finalize_after_loop``), a seeded conversation and a real
``create_task`` — the same mechanics as
``tests/llm/test_llm_replay_scenarios.py::_run_loop_scenario`` — with the
three LLM transports replaced by a ``tests/support/llm_replay.ReplaySession``
(``verify=False``) whose ``script.jsonl`` is SYNTHESISED from the case input.
Only tool HANDLERS are stubbed; dispatch, effect intents, settlement and
persistence stay real.

Isolation (mirrors ``tests/conftest.py`` + ``tests/_chat_sidecar.py``):
before the first project import, ``TOFU_DATA_DIR`` and
``TOFU_STORAGE_PROJECT_ROOT`` are pointed at one fresh per-case tempdir and
``TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE=1``; production lifecycle/port env is
cleared; one real SQLite Sidecar (``StorageRuntime`` backend ``sqlite``,
``install_runtime_for_test``) is started per case on a fresh project root.
Nothing touches production 15000/15001 or the shared data dir.
``pytest.MonkeyPatch`` is used standalone instead of the pytest fixture.

Line protocol
-------------

``python3 -m tests.rust_parity_engine.reference`` reads one JSON request per
stdin line ``{"id":..., "boundary":"engine_session", "input":{...}}`` and
writes one JSON answer per stdout line:
``{"id":..., "parts":[{"name":..., "b64":...}]}``,
``{"id":..., "unsupported":"<reason>"}`` or ``{"id":..., "error":...}``.
stdout is reserved for the protocol; the real ``sys.stdout`` is redirected
to stderr inside.

INPUT schema (stable; the Rust side implements the same)
-------------------------------------------------------

``{"user": str, "model": str (primary), "fallbackModel": str|null,
   "tools": [{"name": str, "readOnly": bool}],
   "requests": [step...], "toolOutcomes": {callId: outcome},
   "cancelAt": null|{"request": int, "afterText": bool},
   "storageFailure": null|{"op": "append_event"|"tool_effect_intent"},
   "restartAfterRequest": null|int}``

Model routing: each distinct model id gets one single-slot v2 route whose
deployment's ``wire_model_id`` is ``<model>-wire``; the requests part reports
that wire id (the engine should record the same logical-to-wire mapping).

``requests`` is consumed ONE STEP PER PROVIDER REQUEST actually issued
(0-based ``cancelAt.request`` / 1-based ``restartAfterRequest``).
A step is exactly one of:

* ``{"kind":"text", "text":str}`` — one streamed content chunk, finished
  ``stop`` (a non-empty ``text`` is split into two chunks so the request
  count is exercised; two chunks are concatenated).
* ``{"kind":"tool", "text":str, "calls":[{"id","name","arguments"}]}`` —
  ``arguments`` is JSON-text; every call is streamed as a ``tool_call``
  event AND carried on the terminal message (finish ``tool_calls``).
* ``{"kind":"content_filter", "partial":str}`` — HTTP-200 verified finish
  ``content_filter``; ``partial`` is the discarded partial content.
* ``{"kind":"http_error", "status":int, "partial":str,
   "midStream":bool}`` — raises ``RateLimitError`` (status carried as
  ``status_code``). ``midStream=false`` fails before any chunk;
  ``midStream=true`` streams ``partial`` first, then raises.
* ``{"kind":"drop", "partial":str}`` — streams ``partial`` then ends with
  the ``premature_close`` state (no finish evidence): Python's
  interrupted-request recovery resends the request, so the next step is
  consumed by the same round.

``toolOutcomes[callId]`` = ``{"kind":"ok"|"error"|"refused_before_entry"|
"lost_after_entry", "output":str}`` (default ``{"kind":"ok","output":""}``).

``cancelAt``: while request n (0-based) streams, the owner-cancel seam
(``task['aborted']=True``) is tripped like
``test_abort_mid_stream`` — after the first content chunk when
``afterText`` is true, before the first chunk otherwise.

``storageFailure.op``:
``"append_event"`` — every NON-terminal durable turn-event commit after the
first ``round_start`` raises ``StorageError`` at the REAL commit boundary
(``lib.turn_lifecycle.record_task_event`` / ``record_task_events``, which
transactionally own the turn projection AND the storage_events row);
terminal done/error/aborted frames still commit so the case can settle.
``"tool_effect_intent"`` is NOT injectable without replacing the storage
authority's transaction, so it is answered ``unsupported``.

``restartAfterRequest``: simulated process crash immediately after request
n (1-based) returns.  The resume restores the durable checkpoint frontier
(the message history captured at the last round close BEFORE that request)
and re-runs the loop on the SAME task with the remaining provider steps;
durable tool rounds and their effect intents survive and are never
re-executed.  This models the rewind/resume shape of
``tests/llm/test_upstream_turn_replay.py`` + ``partial_stream_cut`` —
Python's production crash recovery is the durable worker's recovery owner,
not this harness, so the crash window is documented per corpus case.

Tool-execution seam
-------------------

``lib/tasks_pkg/executor/_resolver.resolver_for(task)`` — the single
handler-lookup seam used by
``lib/tasks_pkg/executor/_execute.py::_dispatch_admitted_tool_one`` — is
replaced with a resolver that returns one scripted handler per declared
tool.  The handler runs at the ordinary point INSIDE the pipeline: after
``begin_tool_effect`` has committed the effect intent, with approval, tool
runtime contexts, result finalisation and settlement all real.

* ``ok``     — returns ``(callId, output, False)``; effect → ``returned``.
* ``error``  — raises ``RuntimeError(output)``; the real error projection
  builds the model-visible typed result; effect → ``unknown`` for a
  mutating tool (``reconcile`` recovery) / the read-only error envelope.
* ``refused_before_entry`` — raises
  ``lib.project_mod.run_command.CommandAdmissionRefused``: the production
  pre-execution refusal class whose effect the executor rewrites to
  ``not_started``.
* ``lost_after_entry`` — raises ``StorageError('database_busy', ... lost
  ACK ...)``: the handler's effect was possibly committed but its
  acknowledgement was lost, so a mutating tool's effect stays ``unknown``
  and the model receives ``tool_effect_unknown``.

Effect policy comes from the REAL ToolContractV2 documents compiled from
the declared ``tools`` (``readOnly: true`` → ``effectRecovery:
'read_only'``; otherwise ``reconcile``), exactly as the root tool assembly
compiles caller-supplied schemas.

PARTS (in this order; each canonical JSON text:
``sort_keys=True, separators=(',',':'), ensure_ascii=False``)
--------------------------------------------------------------

* ``outcome`` — ``{"status": "done"|"error"|"aborted",
  "content": final assistant text, "finishReason": str|null,
  "errorKind": str|null}``.  ``errorKind`` is the kind of Python's typed
  error envelope (``task['error']``, or the content-filter settlement's
  stamped envelope).  ``status`` is ``aborted`` when the owner cancelled,
  else ``error`` when an error envelope is present or the task settled
  ``error``, else ``done`` — note a verified content-filter refusal settles
  ``finishReason='content_filter'`` with ``errorKind='content_filter'``
  even though the durable task status is done/truncated, so the part's
  status is ``error`` there.
* ``requests`` — one entry per provider request actually issued:
  ``{"model": wire model id (the route's deployment wire id,
  ``<model>-wire``), "messages":[{role, content, toolCalls, toolCallId}]
  EXCLUDING the system message}``.  ``content`` is the text form (content
  part lists are flattened to their text parts); absent fields are
  ``null``/``[]``.  One harness-level projection keeps this part free of
  host facts: the ambient runtime's ``<system-reminder>`` injection on the
  seeded user turn is stripped back to the authored text in every request
  (it carries the wall-clock date). The loop's own intent-stall nudge (the
  ``[END_TURN: ...]`` user prompt) is engine policy and stays.
* ``tools`` — one entry per tool call in settlement order:
  ``{"id","name","effect","isError","result"}`` where ``effect`` is the
  durable ``executionEffect.state`` Python recorded on the tool round
  (``returned``/``unknown``/``not_started``; the successful durable state
  is ``returned``, never ``succeeded``), ``isError`` reflects the tool's
  terminal presentation status and ``result`` is the model-visible tool
  result text (the tool message content the NEXT request carries).
  ``operationId`` values are random UUIDs; they are normalised (see below)
  so the part is deterministic.  A read-only tool emits NO tool_effect
  event (its effect is process-scoped) but its round still records the
  terminal effect state.
* ``fallbacks`` — durable ``model_fallback`` events as
  ``{"from","to","kind"}``.
* ``events`` — ordered durable event TYPES restricted to ``round_start,
  round_end, model_fallback, tool_start, tool_complete, tool_effect, done,
  error, aborted, retry_reset, delta_reset``.

Determinism normalisation rules (nothing else is rewritten)
-----------------------------------------------------------

1. ``operationId`` UUIDs (Python mints one random UUID per effect
   occurrence) are replaced by ``op-<toolCallId>`` wherever they appear —
   in the tools part's result text AND in tool messages inside later
   request bodies.
2. The ambient ``<system-reminder>`` context injection (which embeds the
   current date) is stripped from the seeded user message in every request.
3. No timestamps, task ids or absolute paths appear in any part, so none
   are normalised.  Parallel tool settlement order is deterministic
   (declaration order), verified by running every case twice and comparing
   bytes.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import traceback


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def _parts(payloads: list[tuple[str, object]]) -> list[dict]:
    return [{'name': name,
             'b64': base64.b64encode(_canonical(value).encode('utf-8')).decode('ascii')}
            for name, value in payloads]


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        request_id = request.get('id')
        try:
            if 'process' in request:
                raise UnsupportedCase(
                    'actual process recovery requires a claimed Attempt and the common '
                    'worker_execution recovery adapter; engine_session only simulates restart')
            parts = engine_session(request['input'])
            answer = {'id': request_id, 'parts': parts}
        except UnsupportedCase as error:
            answer = {'id': request_id, 'unsupported': str(error)}
        except Exception as error:  # noqa: BLE001 - protocol boundary
            traceback.print_exc(file=sys.stderr)
            answer = {'id': request_id,
                      'error': f'{type(error).__name__}: {error}'}
        real_stdout.write(_canonical(answer) + '\n')
        real_stdout.flush()
    return 0


class UnsupportedCase(Exception):
    """The case uses a fault class this reference does not model."""


class SimulatedCrash(BaseException):
    """The case's ``restartAfterRequest`` process crash (not a provider error)."""


# Inherited production lifecycle/port/storage identity, as tests/conftest.py
# clears before any project import.  Never let a tofu-shell environment pick
# the live data dir, storage credential or supervisor.
_LIFECYCLE_ENV_NAMES = (
    'PORT', '_TOFU_RUNTIME_PORT', '_TOFU_REEXEC_PORT', 'TOFU_DATA_DIR',
    'TOFU_PROJECT_ROOT', 'TOFU_PROJECT_PATH', 'TOFU_MANAGED_BY',
    'TOFU_SERVER_WORKER', 'TOFU_RESTART_GATE_PASSED',
    'TOFU_LIFECYCLE_GATE_PASSED', 'TOFU_ALLOW_LIFECYCLE_TEST',
    'TOFU_LIFECYCLE_TEST_ROOT', 'TOFU_LIFECYCLE_TEST_PORT',
    'TOFU_LIFECYCLE_TEST_TARGET_PID', 'TOFU_TESTING',
    'TOFU_EXTERNAL_CONSOLE_LOG', 'TOFU_EXTERNAL_CONSOLE_STREAM',
    'TOFU_STORAGE_CONNECTION_FILE', 'TOFU_STORAGE_TOKEN',
    'TOFU_STORAGE_PARENT_PID', 'TOFU_STORAGE_PROJECT_ROOT',
    'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
    'TOFU_RESOURCE_BUDGET_POLICY_VERSION',
    'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS',
    'TOFU_SUPERVISOR_CONF', 'TOFU_SUPERVISOR_CONF_DIR',
    'TOFU_SUPERVISOR_HOME', 'TOFU_SUPERVISOR_HOST', 'TOFU_SUPERVISOR_PORT',
    'TOFU_SUPERVISOR_PROJECTS', 'TOFU_SUPERVISOR_PYTHON',
    'TOFU_SUPERVISOR_USER', 'TOFU_HEARTBEAT_DIR', 'TOFU_PYTEST_RUN_ROOT',
    'TOFU_PROCESS_ROLE', 'PYTEST_CURRENT_TEST',
)

_EVENT_PART_TYPES = frozenset({
    'round_start', 'round_end', 'model_fallback', 'tool_start',
    'tool_complete', 'tool_effect', 'done', 'error', 'aborted',
    'retry_reset', 'delta_reset',
})


def _isolated_environment(scratch: str) -> dict[str, str | None]:
    """Point all writable state at *scratch*, before project imports."""
    saved = {name: os.environ.get(name) for name in _LIFECYCLE_ENV_NAMES}
    saved.update({name: os.environ.get(name) for name in (
        'TOFU_DATA_DIR', 'TOFU_STORAGE_PROJECT_ROOT',
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', 'TOFU_DEPLOYMENT_MODE',
        'TOFU_PROCESS_ROLE', 'LLM_API_KEY', 'LLM_API_KEYS',
        'TOFU_AUTH_MODE', 'TOFU_GATEWAY_OUTAGE_BUDGET_S',
    )})
    for name in _LIFECYCLE_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ['TOFU_DATA_DIR'] = os.path.join(scratch, 'data')
    os.environ['TOFU_STORAGE_PROJECT_ROOT'] = os.path.join(scratch, 'storage')
    os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
    os.environ['TOFU_DEPLOYMENT_MODE'] = 'personal'
    os.environ['TOFU_PROCESS_ROLE'] = 'all'
    os.environ['LLM_API_KEY'] = 'test-key-placeholder'
    os.environ['LLM_API_KEYS'] = 'test-key-placeholder'
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')
    # Never inherit a tofu shell's production address book.
    for name in ('TOFU_API_URL', 'TOFU_API_BASE', 'TOFU_ORIGIN'):
        saved.setdefault(name, os.environ.get(name))
        os.environ.pop(name, None)
    return saved


# ── Script synthesis ────────────────────────────────────────────────


def _stream_step(events, message, finish, state='provider_finished',
                 provider_finish=None):
    return {
        'kind': 'stream', 'request_digest': 'engine-session',
        'events': events,
        'result': {
            'message': message,
            'compatibility_finish_reason': finish,
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                      'total_tokens': 15},
            'state': state,
            'provider_finish_reason': provider_finish or finish,
            'saw_done': state == 'provider_finished',
            'saw_finish_reason': state == 'provider_finished',
            # ProviderStreamResult invariant: saw_finish_reason and
            # provider_finish_reason must agree; a drop has neither.
            **({} if state == 'provider_finished'
               else {'provider_finish_reason': None}),
            'malformed_frame_count': 0,
        },
    }


def _content_events(text):
    if not text:
        return []
    midpoint = len(text) // 2
    if midpoint == 0:
        return [{'t': 'content', 'text': text}]
    return [{'t': 'content', 'text': text[:midpoint]},
            {'t': 'content', 'text': text[midpoint:]}]


def _synthesise_script(case_input):
    """Convert case ``requests`` steps into replay script rows + markers."""
    from lib.llm.llm_errors import RateLimitError

    rows, after, errors = [], {}, {}
    for index, step in enumerate(case_input.get('requests') or []):
        kind = step.get('kind')
        if kind == 'text':
            text = str(step.get('text') or '')
            rows.append(_stream_step(
                _content_events(text),
                {'role': 'assistant', 'content': text}, 'stop'))
        elif kind == 'tool':
            text = str(step.get('text') or '')
            calls = []
            for call in step.get('calls') or []:
                arguments = call.get('arguments')
                if not isinstance(arguments, str):
                    raise UnsupportedCase(
                        f'requests[{index}]: tool arguments must be JSON-text')
                json.loads(arguments)
                calls.append({'id': call['id'], 'type': 'function',
                              'function': {'name': call['name'],
                                           'arguments': arguments}})
            if not calls:
                raise UnsupportedCase(f'requests[{index}]: tool step without calls')
            rows.append(_stream_step(
                _content_events(text)
                + [{'t': 'tool_call', 'call': dict(call)} for call in calls],
                {'role': 'assistant', 'content': text, 'tool_calls': calls},
                'tool_calls'))
        elif kind == 'content_filter':
            partial = str(step.get('partial') or '')
            rows.append(_stream_step(
                _content_events(partial),
                {'role': 'assistant', 'content': partial}, 'content_filter'))
        elif kind == 'http_error':
            status = int(step['status'])
            message = f'HTTP {status} provider error'
            error = RateLimitError(message, status_code=status)
            if step.get('midStream'):
                partial = str(step.get('partial') or '')
                rows.append(_stream_step(
                    _content_events(partial),
                    {'role': 'assistant', 'content': partial}, 'stop'))
                after[index] = 'error'
                errors[index] = error
            else:
                rows.append({'kind': 'error',
                             'request_digest': 'engine-session',
                             'exception': 'RateLimitError', 'message': message,
                             'attributes': {'status_code': status,
                                            'is_gateway': status >= 500,
                                            'is_quota': False,
                                            'reason': message}})
        elif kind == 'drop':
            partial = str(step.get('partial') or '')
            rows.append(_stream_step(
                _content_events(partial),
                {'role': 'assistant', 'content': partial}, 'stop',
                state='premature_close', provider_finish=None))
        else:
            raise UnsupportedCase(f'requests[{index}]: unknown kind {kind!r}')
    if not rows:
        raise UnsupportedCase('requests is empty')
    return rows, after, errors


class _CaseReplaySession:
    """ReplaySession with post-delivery faults (mid-stream error / crash)."""

    def __init__(self, script_path, *, cwd, after_event, after, errors):
        from tests.support.llm_replay import ReplaySession
        self._after, self._errors = after, errors
        self.requests = []
        self._session = ReplaySession(script_path, cwd=cwd, verify=False,
                                      after_event=self._observe(after_event))
        original = self._session._deliver_stream

        def deliver(step, index, **callbacks):
            result = original(step, index, **callbacks)
            marker = self._after.get(index)
            if marker == 'error':
                raise self._errors[index]
            if marker == 'crash':
                raise SimulatedCrash()
            return result

        self._session._deliver_stream = deliver

    def _observe(self, after_event):
        def observe(index, event):
            if after_event is not None:
                after_event(index, event)
        return observe

    @staticmethod
    def _mark_dispatched(body):
        # The scripted transport replaces HTTP, not the shared dispatch-policy
        # boundary. Preserve its off-wire callbacks or a refusal exemption
        # would never be consumed by this reference's fake physical attempts.
        from types import SimpleNamespace
        from lib.llm.request import mark_model_request_dispatched
        mark_model_request_dispatched(SimpleNamespace(
            body=body, model_budget_lease=None,
            on_model_request_dispatching=body.get('_on_model_request_dispatching'),
            on_model_request_dispatched=body.get('_on_model_request_dispatched'),
        ))

    def stream_chat(self, body, **kwargs):
        self._mark_dispatched(body)
        self.requests.append(body)
        return self._session.stream_chat(body, **kwargs)

    def async_stream_chat(self, body, **kwargs):
        self._mark_dispatched(body)
        self.requests.append(body)
        return self._session.async_stream_chat(body, **kwargs)

    def chat(self, messages, model=None, **kwargs):
        return self._session.chat(messages, model=model, **kwargs)

    def install(self, monkeypatch):
        import importlib
        astream_mod = importlib.import_module('lib.llm.astream')
        chat_mod = importlib.import_module('lib.llm.chat')
        monkeypatch.setattr('lib.llm.stream_chat', self.stream_chat)
        monkeypatch.setattr(astream_mod, 'async_stream_chat', self.async_stream_chat)
        monkeypatch.setattr(chat_mod, 'chat', self.chat)
        monkeypatch.setattr('lib.llm.chat', self.chat)
        return self


# ── Scripted tool handlers ──────────────────────────────────────────


def _scripted_handler(outcomes):
    def handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                project_path, project_enabled, all_tools):
        outcome = outcomes.get(tc_id) or {'kind': 'ok', 'output': ''}
        kind = outcome.get('kind', 'ok')
        output = str(outcome.get('output') or '')
        if kind == 'ok':
            return tc_id, output, False
        if kind == 'error':
            raise RuntimeError(output or 'scripted tool failure')
        if kind == 'refused_before_entry':
            from lib.project_mod.run_command import CommandAdmissionRefusal
            raise CommandAdmissionRefusal(
                output or 'scripted refusal before handler entry')
        if kind == 'lost_after_entry':
            from lib.agent_core.storage_errors import StorageError
            raise StorageError('database_busy',
                               output or 'scripted lost acknowledgement', True)
        raise UnsupportedCase(f'toolOutcomes[{tc_id!r}]: unknown kind {kind!r}')
    return handler


class _ScriptedResolver:
    """Drop-in for executor._resolver.HandlerResolver (lookup-only seam)."""

    def __init__(self, handler):
        self._handler = handler

    def lookup(self, fn_name, round_entry=None):
        return self._handler


# ── Route / tool-surface installation ───────────────────────────────


def _install_route(models):
    """One single-slot v2 route per case model (mirrors the replay tests)."""
    from lib.llm_dispatch import reset_dispatcher
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    from tests.support.model_registration import complete_registration
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    current = repository.get(boundary)
    creators, providers, accesses, connections = [], [], [], []
    credentials, offerings, deployments = [], [], []
    rows = []
    for position, model in enumerate(models):
        creator = f'engine-session-creator-{position}'
        ref = {'creator_id': creator, 'model_id': model}
        rows.append({**ref, 'display_name': f'Engine session model {model}',
                     'capabilities': ['text'], 'context_window': 1_000_000,
                     'quality_rank': position + 1})
        creators.append({'creator_id': creator, 'name': creator})
        providers.append({'provider_id': f'provider-{position}',
                          'name': f'provider-{position}', 'scope': 'owner'})
        accesses.append({'provider_access_id': f'access-{position}',
                         'provider_id': f'provider-{position}',
                         'enabled': True, 'quota_policy': {}})
        connections.append({
            'connection_id': f'connection-{position}',
            'provider_access_id': f'access-{position}',
            # Transport is substituted before any socket exists; a public
            # domain avoids self-hosted reachability probes (replay tests).
            'base_url': 'https://api.openai.com/v1', 'protocol': 'openai',
            'enabled': True, 'priority': position, 'extra_headers': {}})
        credentials.append({
            'credential_id': f'credential-{position}',
            'provider_access_id': f'access-{position}', 'kind': 'local_identity',
            'secret_reference': '', 'key_hint': '', 'enabled': True,
            'authorization': {'connection_ids': [f'connection-{position}'],
                              'models': [dict(ref)]},
            'quota_policy': {}})
        offerings.append({'offering_id': f'offering-{position}',
                          'provider_access_id': f'access-{position}',
                          'identity_state': 'confirmed', 'model': dict(ref),
                          'enabled': True, 'capabilities': ['text'],
                          'context_window': 1_000_000, 'priority': position})
        deployments.append({'deployment_id': f'deployment-{position}',
                            'offering_id': f'offering-{position}',
                            'connection_id': f'connection-{position}',
                            'wire_model_id': f'{model}-wire', 'enabled': True,
                            'identity_confidence': 'high',
                            'probe_status': 'passed', 'priority': position})
    document = {
        'contract_version': 'tofu.model-routing/v2',
        'revision': current.revision,
        'creators': creators, 'models': rows, 'providers': providers,
        'provider_accesses': accesses, 'connections': connections,
        'credentials': credentials, 'offerings': offerings,
        'deployments': deployments,
    }
    completed = complete_registration(document)
    repository.compare_and_swap(boundary, completed,
                                expected_revision=current.revision)
    reset_dispatcher()


def _tool_surface(case_input):
    """OpenAI schemas + REAL ToolContractV2 documents for declared tools."""
    from lib.tools.contracts import adapt_legacy_tool_contract
    schemas, documents = [], {}
    for declaration in case_input.get('tools') or []:
        name = declaration['name']
        read_only = bool(declaration.get('readOnly'))
        schema = {'type': 'function',
                  'function': {'name': name,
                               'description': f'Engine session scripted tool {name}.',
                               'parameters': {'type': 'object', 'properties': {},
                                              'additionalProperties': True}},
                  'write': not read_only}
        schemas.append(schema)
        documents[name] = adapt_legacy_tool_contract(
            schema, namespace='custom',
            permission='read' if read_only else 'write',
            idempotency='read_only' if read_only else 'non_idempotent',
            effect_recovery='read_only' if read_only else 'reconcile',
        ).search_document()
    return schemas, documents


# ── Case driver ─────────────────────────────────────────────────────


def _run_case(case_input, scratch: str):
    import uuid
    from pathlib import Path
    from pytest import MonkeyPatch

    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
    from lib.tasks_pkg.orchestrator._config_resolution import (
        resolve_and_seed_model_config,
    )
    from lib.tasks_pkg.orchestrator._post_loop import finalize_after_loop
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools,
    )

    runtime = StorageRuntime(
        StorageSupervisor(project_root=Path(scratch) / 'authority',
                          backend='sqlite', startup_timeout=60),
        auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch = MonkeyPatch()
    holder = {}
    try:
        primary = str(case_input['model'])
        fallback = case_input.get('fallbackModel') or ''
        models = [primary] + ([fallback] if fallback and fallback != primary else [])
        _install_route(models)
        import lib as lib_module
        monkeypatch.setattr(lib_module, 'FALLBACK_MODEL', fallback)
        from lib.tasks_pkg.stream_handler import _budget
        monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *args: None)
        monkeypatch.setattr(
            'lib.tasks_pkg.turn_retry.auto_turn_backoff_seconds', lambda _: 0)
        monkeypatch.setenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', '0.001')
        from lib.tasks_pkg.executor import _resolver as resolver_module
        monkeypatch.setattr(
            resolver_module, 'resolver_for',
            lambda task, *a, **k: _ScriptedResolver(
                _scripted_handler(case_input.get('toolOutcomes') or {})))
        # The boundary owns ALL tool execution, including the streaming
        # read-prefetch fast path: the accumulator only pre-executes this
        # fixed builtin set, so removing it leaves every declared tool on the
        # real dispatch/effect path (announcements stay real).
        from lib.tasks_pkg import streaming_tool_executor
        monkeypatch.setattr(streaming_tool_executor, '_STREAMABLE_TOOLS', frozenset())
        from lib.tools import environment_capabilities as environment
        monkeypatch.setattr(environment, '_snapshot',
                            environment.EVERY_CAPABILITY_AVAILABLE)
        monkeypatch.setattr(environment, '_stale', False)
        monkeypatch.setattr(environment, 'probe_environment',
                            lambda environ, *, local_kernel: {})
        monkeypatch.delenv('TOFU_DISABLE_CONFIGURED_SLOTS', raising=False)
        from lib.model_routing import health as routing_health
        routing_health.reset_owner_health_registries()

        from tests._seed import seed_conversation
        from lib.tasks_pkg.manager import create_task
        conv_id = f'cv-engine-session-{uuid.uuid4().hex[:8]}'
        user_prompt = str(case_input.get('user') or '')
        seed_conversation(conv_id, messages=[
            {'role': 'user', 'content': user_prompt, 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'thinking': '',
             'toolRounds': [], 'timestamp': 2},
        ], title='engine-session')
        schemas, documents = _tool_surface(case_input)
        # Real conversation turn/attempt identity: durable tool effects
        # (tool_effect events) are scope='attempt' only with it.
        from lib.conversation_sync.runtime import conversation_turn_commands
        turn_outcome = conversation_turn_commands.create_turn(
            conv_id, 1,
            {'commandId': f'engine-session-{uuid.uuid4().hex}',
             'config': {'model': primary, 'webSearchEnabled': False,
                        'plugins': [],
                        'disableModelFallback': not bool(fallback)},
             'message': {'text': user_prompt}},
            request_started_at=0.0)
        turn_result = turn_outcome.value
        from lib.conversation_sync.command_service import (
            restore_accepted_attempt_config,
        )
        config = restore_accepted_attempt_config(
            turn_result['turn'], turn_result['attempt'], user_id=1)
        config.update({'model': primary, 'webSearchEnabled': False,
                       'plugins': [], 'tools': schemas,
                       'disableModelFallback': not bool(fallback),
                       '_projectDispatchWorkspaceTaskId': f'conversation-attempt:{turn_result["attempt"]["attemptId"]}'})
        # Production conversation execution identity: the durable worker's
        # task id is conversation-attempt:<attemptId>, bound atomically by
        # turn.attempt.dispatch_worker, then started at physical worker entry
        # (lib/conversation_sync/worker_execution.py).  Claim the durable job
        # and bind its real WorkerExecutionContext to the task so every
        # attempt-scoped durable write carries the claim's fencing proof.
        from lib.turn_lifecycle import dispatch_attempt_to_worker, mark_task_started
        from lib.durable_worker import (
            StorageWorkerJobStore, WorkerExecutionContext, WorkerJobClaim,
        )
        from lib.identity import PrincipalContext
        attempt_id = turn_result['attempt']['attemptId']
        task_id = f'conversation-attempt:{attempt_id}'
        principal = PrincipalContext.user(subject_id='user:1', owner_user_id=1)
        dispatch_attempt_to_worker(principal, attempt_id)
        import time as _time
        store = StorageWorkerJobStore()
        document = store.claim_next(
            worker_id='engine-session-worker',
            task_kinds=('conversation-attempt',),
            now_ms=int(_time.time() * 1000), lease_ms=60_000)
        claim = WorkerJobClaim.from_document(
            document, expected_worker_id='engine-session-worker')
        worker_context = WorkerExecutionContext(claim)
        started = mark_task_started(
            attempt_id, task_id, user_id=1,
            worker_claim=worker_context.publication_claim(), worker_process=None)
        task = create_task(conv_id, [{'role': 'user', 'content': user_prompt}],
                           config, user_id=1, principal=principal,
                           worker_context=worker_context)
        # create_task(worker_context=...) already bound the claim context;
        # bind_conversation_worker is idempotent only before that binding.
        from lib.tasks_pkg.worker_binding import record_worker_entry
        record_worker_entry(task, started)
        holder['task'] = task
        # Production turn-retry policy (tests/llm/test_upstream_turn_replay.py):
        # one bounded automatic turn retry, and the configured finite exit from
        # an inner gateway-outage wait so a persistent 5xx settles instead of
        # waiting forever.
        task['config']['autoTurnRetryMax'] = 1

        mcfg = resolve_and_seed_model_config(task['config'], task)
        tool_list, _ = assemble_round_tools(task['config'], task, mcfg)
        task['_toolContractDocumentsByName'] = documents
        task['config']['_toolContractDocumentsByName'] = documents
        state = RoundState(model=mcfg['model'], preset=mcfg['preset'],
                           thinking_enabled=bool(mcfg['thinking_enabled']))
        system_prompt = ('You are Tofu, a coding assistant running an engine '
                         'session parity case. Answer the user directly and '
                         'use the available tools when asked.')
        seed_messages = [{'role': 'system', 'content': system_prompt},
                         {'role': 'user', 'content': user_prompt}]
        original_messages = list(seed_messages)
        messages = [dict(message) for message in seed_messages]
        request = root_loop.RootLoopRequest(
            task=task, state=state, messages=messages, tool_list=tool_list,
            all_search_results_text=[], cfg=task['config'],
            tid=task['id'][:8], thinking_depth=mcfg['thinking_depth'],
            temperature=mcfg['temperature'], max_tokens=4096,
            response_format=None, project_path=None, project_enabled=False,
            search_enabled=False)

        rows, after, errors = _synthesise_script(case_input)
        crash_after = case_input.get('restartAfterRequest')
        if crash_after is not None:
            after[int(crash_after) - 1] = 'crash'
        script_path = Path(scratch) / 'script.jsonl'
        script_path.write_text(
            ''.join(_canonical(row) + '\n' for row in rows), encoding='utf-8')

        cancel_at = case_input.get('cancelAt')

        def after_event(index, event):
            if cancel_at is None or index != int(cancel_at['request']):
                return
            if cancel_at.get('afterText') and event.get('t') != 'content':
                return
            task['aborted'] = True

        session = _CaseReplaySession(script_path, cwd=scratch,
                                     after_event=after_event, after=after,
                                     errors=errors)
        session.install(monkeypatch)

        _install_storage_failure(case_input.get('storageFailure'), monkeypatch,
                                 holder)

        crashed = False

        def drive():
            # The invocation-level error wrapper (what execute_invocation's
            # owned lane does around the loop): a fatal provider error becomes
            # the task's typed error, then finalize_after_loop settles it.
            try:
                return root_loop.run_root_agent_loop(request)
            except SimulatedCrash:
                raise
            except Exception as fatal:
                from lib.error_envelope import from_exception
                task['error'] = from_exception(fatal, model=state.model)
                task['status'] = 'error'
                state.last_finish_reason = 'error'

                class _Settled:
                    last_round_num = 0
                return _Settled()

        try:
            loop_result = drive()
        except SimulatedCrash:
            # Simulated process crash AFTER a provider request returned:
            # rewind to the last pre-request checkpoint and resume with the
            # remaining script.  Python's real crash recovery is the durable
            # worker's recovery owner; this models the same observable
            # rewind/resume shape (the request after the crash re-sends the
            # pre-request history, the interrupted round's tool effects stay
            # durable with their intent, never silently re-run).
            crashed = True
            # A crash loses everything the crashed attempt had not settled.
            # Rebuild the durable frontier from the DURABLE event log: a
            # tool_effect intent that reached storage is the only evidence a
            # tool round was admitted; the response being streamed when the
            # crash landed was never adopted, so the frontier is the history
            # at that request's start.
            frontier = session.requests[int(crash_after) - 1].get('messages') \
                if session.requests else None
            if frontier is not None:
                messages[:] = [dict(message) for message in frontier]
            # Durable tool rounds survive the crash: the pre-crash effect
            # intent and its settlement are still the authority (they are NOT
            # silently re-run).  Only in-memory stream accumulators reset.
            state.assistant_msg = None
            state.last_stream_result = None
            state.last_finish_reason = None
            state.last_usage = None
            loop_result = drive()

        finalize_after_loop(
            task, cfg=task['config'], tid=request.tid, model=state.model,
            preset=state.preset, thinking_depth=mcfg['thinking_depth'],
            thinking_enabled=state.thinking_enabled,
            temperature=mcfg['temperature'], max_tokens=4096,
            messages=messages, original_messages=original_messages,
            tool_list=tool_list, assistant_msg=state.assistant_msg,
            round_num=loop_result.last_round_num,
            accumulated_usage=state.accumulated_usage,
            api_rounds=state.api_rounds,
            last_finish_reason=state.last_finish_reason,
            last_usage=state.last_usage,
            last_stream_result=state.last_stream_result,
            tool_call_happened=state.tool_call_happened,
            all_search_results_text=[], project_path=None,
            project_enabled=False, loop_exit_reason=state.exit_reason,
            abort_detected_phase=state.abort_phase)
        durable_rounds = []
        try:
            from lib.turn_lifecycle import get_turn
            turn = get_turn(task['convId'], task['_turnId'], user_id=1)
            durable_rounds = (turn.get('projection') or {}).get('toolRounds') or []
        except Exception as error:
            print('turn projection read failed:', error, file=sys.stderr)
        rounds = list(task.get('toolRounds') or []) + list(durable_rounds)
        return _project_parts(task, state, session.requests, crashed,
                              rounds, messages)
    finally:
        _stop_event_workers()
        monkeypatch.undo()
        try:
            from lib.llm_dispatch import reset_dispatcher
            reset_dispatcher()
        except Exception:
            pass
        try:
            install_runtime_for_test(None)
        finally:
            with contextlib.suppress(Exception):
                runtime.stop()


def _stop_event_workers():
    event_log = sys.modules.get('lib.tasks_pkg.event_log')
    if event_log is None:
        return
    with contextlib.suppress(Exception):
        event_log.stop_storage_maintenance(timeout=3.0)
    with contextlib.suppress(Exception):
        event_log.stop_sidecar_batcher(timeout=3.0)


def _install_storage_failure(spec, monkeypatch, holder):
    if not spec:
        return
    op = spec.get('op')
    from lib.tasks_pkg.manager import _events as task_events
    from lib.agent_core.events import EventType
    real_append = task_events.append_event

    if op == 'append_event':
        # The durable event write fails at its real commit boundary
        # (lib/tasks_pkg/event_log.append_persistent_event), after the first
        # ROUND_START, for every non-terminal frame.  Terminal
        # done/error/aborted frames still commit so the case can settle.
        from lib import turn_lifecycle
        from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_EVENT_TYPES
        real_record = turn_lifecycle.record_task_event

        def failing_record(task, event, *args, **kwargs):
            if (event.get('type') not in TASK_REPLAY_TERMINAL_EVENT_TYPES
                    and holder.get('round_started')):
                from lib.agent_core.storage_errors import StorageError
                raise StorageError(
                    'database_internal',
                    'engine_session scripted storage failure: append_event')
            return real_record(task, event, *args, **kwargs)

        monkeypatch.setattr(turn_lifecycle, 'record_task_event', failing_record)

        def failing_batch(task, entries, *args, **kwargs):
            for entry in entries:
                failing_record(task, entry.raw_event)
            return real_batch(task, entries, *args, **kwargs)

        real_batch = turn_lifecycle.record_task_events
        monkeypatch.setattr(turn_lifecycle, 'record_task_events', failing_batch)
        return
    if op == 'tool_effect_intent':
        from lib.tasks_pkg.executor import _effects as effects_module
        real_begin = effects_module.begin_tool_effect

        def failing_begin(task, name, call_id, number, round_entry):
            effect = real_begin(task, name, call_id, number, round_entry)
            return effect

        monkeypatch.setattr(effects_module, 'begin_tool_effect', failing_begin)
        raise UnsupportedCase(
            'storageFailure.op=tool_effect_intent is not injectable at the '
            'effect-intent barrier without replacing the storage authority; '
            'use op=append_event')
    raise UnsupportedCase(f'storageFailure.op={op!r} is not modelled')


# ── Projection into parts ───────────────────────────────────────────


def _scrub_context_injections(messages):
    """Remove environment/runtime injections from the projected history.

    The requests part must carry only the conversation and tool evidence the
    engine owns.  The host process injects ambient reminders
    (tofu-context:memory_guidance / current_date) into the user turn and the
    loop's recovery path appends its own continuation user prompt; neither is
    a model conversation fact and both carry wall-clock data, so they are
    removed rather than normalised.  The ORIGINAL user message is preserved
    verbatim.  The loop's own intent-stall nudge (a user prompt teaching
    ``[END_TURN: ...]``) is engine-owned conversation evidence and is kept.
    Content-part lists are flattened first: a resent request may carry the
    seeded turn as parts, and the ambient block must be stripped there too.
    """
    cleaned = []
    for message in messages:
        if message.get('role') == 'user':
            content = _text_content(message.get('content'))
            if ('<system-reminder>' in content
                    or 'tofu-context:memory_guidance' in content):
                if not any(m.get('role') == 'user' for m in cleaned):
                    # The seeded human turn: keep only the authored prefix.
                    prefix = content.split('<system-reminder>')[0].rstrip('\n')
                    cleaned.append({**message, 'content': prefix})
                continue
        cleaned.append(message)
    return cleaned


def _text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(str(part.get('text') or '') for part in content
                       if isinstance(part, dict))
    return '' if content is None else str(content)


def _operation_ids(task, durable_rounds=()):
    operations = {}
    for entry in list(task.get('toolRounds') or []) + list(durable_rounds or []):
        if isinstance(entry, dict):
            effect = entry.get('executionEffect') or {}
            if effect.get('operationId') and entry.get('toolCallId'):
                operations[effect['operationId']] = entry['toolCallId']
    return operations


def _project_requests(bodies, operations=None):
    operations = operations or {}
    # Normalisation rule 1: every random effect operation UUID is replaced by
    # its deterministic op-<toolCallId> token wherever it appears (tool result
    # text inside later request bodies included).  The tools part carries the
    # same normalisation in its result text, never the raw UUID.
    def normalize(text):
        if not isinstance(text, str):
            return text
        for operation, call_id in operations.items():
            text = text.replace(operation, f'op-{call_id}')
        return text

    projected = []
    for body in bodies:
        messages = []
        for message in _scrub_context_injections(body.get('messages') or []):
            if not isinstance(message, dict) or message.get('role') == 'system':
                continue
            calls = []
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                calls.append({'id': call.get('id'),
                              'name': function.get('name'),
                              'arguments': function.get('arguments')})
            messages.append({
                'role': message.get('role'),
                'content': normalize(_text_content(message.get('content'))),
                'toolCalls': calls,
                'toolCallId': message.get('tool_call_id'),
            })
        projected.append({'model': body.get('model'), 'messages': messages})
    return projected


def _project_parts(task, state, bodies, crashed, durable_rounds=(), messages=()):
    # toolRounds may be folded into the turn projection at finalize; the
    # operation-id normalisation needs the union of both projections.
    operations = _operation_ids(task, durable_rounds)
    from lib.tasks_pkg.event_log import read_events
    rows = read_events(str(task['id']), raise_on_error=True) or []
    events = [row['payload'] for row in rows
              if isinstance(row, dict) and isinstance(row.get('payload'), dict)]
    types = [event.get('type') for event in events
             if event.get('type') in _EVENT_PART_TYPES]
    fallbacks = [{'from': event.get('fallbackFrom'),
                  'to': event.get('fallbackModel'),
                  'kind': event.get('fallbackKind')}
                 for event in events if event.get('type') == 'model_fallback']

    # Settlement order = the task's own tool rounds (the pipeline's replay
    # order), with the round's recorded executionEffect.  tool_effect events
    # are the write-ahead INTENT (state 'entered'); the round carries the
    # terminal effect state ('returned'/'unknown'/'not_started').
    # The model-visible tool result text is the tool message the NEXT request
    # carries (settlement's canonical content), not the round's display rows.
    result_text = {}
    for message in messages or []:
        if isinstance(message, dict) and message.get('role') == 'tool':
            result_text[message.get('tool_call_id')] = _text_content(
                message.get('content'))
    tools = []
    seen_calls = set()
    for entry in durable_rounds or task.get('toolRounds') or []:
        if not isinstance(entry, dict):
            continue
        call_id = entry.get('toolCallId')
        if not call_id or call_id in seen_calls:
            continue
        seen_calls.add(call_id)
        effect = entry.get('executionEffect') or {}
        operation = effect.get('operationId')
        result = result_text.get(call_id)
        if result is None:
            result = entry.get('toolContent')
        if result is None:
            result = _text_content(
                (entry.get('results') or [{}])[0].get('content')
                if entry.get('results') else '')
        if operation:
            result = str(result).replace(operation, f'op-{call_id}')
        status = entry.get('status')
        tools.append({'id': call_id,
                      'name': entry.get('toolName'),

                      'effect': effect.get('state') or 'not_started',
                      'isError': status not in (None, 'done'),
                      'result': result})
    error = task.get('error') if isinstance(task.get('error'), dict) else None
    # A content-filter outcome settles the typed envelope on the turn
    # settlement record, not on task['error'] (task status stays done /
    # truncated with finishReason content_filter).
    filter_settlement = task.get('_contentFilterSettlement') or {}
    if error is None and isinstance(filter_settlement, dict):
        stamped = filter_settlement.get('error')
        if isinstance(stamped, dict):
            error = stamped
    aborted = bool(task.get('aborted')) or task.get('status') == 'aborted'
    status = 'aborted' if aborted else (
        'error' if error is not None or task.get('status') == 'error' else 'done')
    outcome = {'status': status,
               'content': task.get('content') or '',
               'finishReason': task.get('finishReason') or state.last_finish_reason,
               'errorKind': (error or {}).get('kind')}
    return [('outcome', outcome),
            ('requests', _project_requests(bodies, operations)),
            ('tools', tools),
            ('fallbacks', fallbacks),
            ('events', types)]


def engine_session(case_input: dict) -> list[dict]:
    """Run one engine_session case; return protocol parts (name/b64)."""
    import tempfile
    if not isinstance(case_input, dict) or 'model' not in case_input:
        raise UnsupportedCase('input must be an object with a primary "model"')
    with tempfile.TemporaryDirectory(prefix='tofu-engine-session-') as scratch:
        saved = _isolated_environment(scratch)
        try:
            return _parts(_run_case(case_input, scratch))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            _stop_event_workers()


if __name__ == '__main__':
    raise SystemExit(main())
