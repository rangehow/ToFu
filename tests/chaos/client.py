"""HTTP client of the isolated instance: the owner's browser, minus the page.

Responsibility: install the chaos model route through the public model-routing
API, submit Turns through the v4 create-turn command, read the authoritative
sync snapshot, and follow the replayable ``/events`` Server-Sent Events stream
with ``Last-Event-ID`` reconnects -- the same surfaces the browser uses.
Entry: ``InstanceClient(base_url)``. Standard library only.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from tests.chaos.fake_provider import MODEL_ID

CREATOR_ID = 'tofu-chaos'
TERMINAL_TURN_STATUSES = frozenset({'completed', 'interrupted', 'truncated', 'failed'})
CHAT_CONFIG = {
    'chatMode': 'chat', 'autoApply': True, 'autoTranslate': False, 'autopilot': False,
    'browserEnabled': False, 'codeExecEnabled': True, 'desktopEnabled': False, 'fetchEnabled': False,
    'humanGuidanceEnabled': False, 'imageGenEnabled': False, 'memoryEnabled': False,
    'planMode': False, 'schedulerEnabled': False, 'searchMode': 'off', 'systemPrompt': '',
    'systemPromptMode': 'append', 'tools': {'executionScope': 'available', 'programmaticCalling': 'off',
                                            'toolSearch': 'off'},
}


class ApiError(RuntimeError):
    def __init__(self, status: int, body):
        super().__init__(f'HTTP {status}: {str(body)[:500]}')
        self.status, self.body = status, body


def model_routing_document(provider_url: str, revision: int) -> dict:
    model_ref = {'creator_id': CREATOR_ID, 'model_id': MODEL_ID}
    capabilities = ['text', 'tools']
    return {
        'contract_version': 'tofu.model-routing/v2', 'revision': revision,
        'creators': [{'creator_id': CREATOR_ID, 'name': 'Chaos suite'}],
        'models': [{**model_ref, 'display_name': 'Chaos model', 'capabilities': capabilities,
                    'context_window': 200_000, 'quality_rank': 1,
                    # Synthetic registration facts: this model exists only in the isolated instance.
                    'registration_metadata': {'release_date': '2026-11-17', 'checked_at': '2026-11-17',
                                              'source_urls': ['https://example.invalid/chaos']},
                    'list_pricing': {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'},
                    'inference_profile': {'reasoning': {'support': 'unsupported'}}}],
        'providers': [{'provider_id': 'chaos', 'name': 'Chaos provider', 'scope': 'owner'}],
        'provider_accesses': [{'provider_access_id': 'chaos-access', 'provider_id': 'chaos',
                               'enabled': True, 'quota_policy': {}}],
        'connections': [{'connection_id': 'chaos-anthropic', 'provider_access_id': 'chaos-access',
                         'base_url': provider_url, 'protocol': 'anthropic',
                         'enabled': True, 'priority': 0, 'extra_headers': {}}],
        'credentials': [{'credential_id': 'chaos-identity', 'provider_access_id': 'chaos-access',
                         'kind': 'local_identity', 'secret_reference': '', 'key_hint': '', 'enabled': True,
                         'authorization': {'connection_ids': ['chaos-anthropic'], 'models': [model_ref]},
                         'quota_policy': {}}],
        'offerings': [{'offering_id': 'chaos-offering', 'provider_access_id': 'chaos-access',
                       'identity_state': 'confirmed', 'model': dict(model_ref), 'enabled': True,
                       'capabilities': capabilities, 'context_window': 200_000, 'priority': 0}],
        'deployments': [{'deployment_id': 'chaos-deployment', 'offering_id': 'chaos-offering',
                         'connection_id': 'chaos-anthropic', 'wire_model_id': MODEL_ID, 'enabled': True,
                         'identity_confidence': 'high', 'probe_status': 'passed', 'priority': 0}],
    }


class InstanceClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')

    def request(self, method: str, path: str, body=None, *, timeout: float = 60.0,
                headers: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json',
                                                  'Accept': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw.decode(errors='replace')
            raise ApiError(error.code, payload) from None

    def install_route(self, provider_url: str) -> None:
        current = self.request('GET', '/api/v4/model-routing')
        data = current.get('data', current)
        revision = data['revision']
        self.request('PUT', '/api/v4/model-routing', {
            'expected_revision': revision,
            'model_routing': model_routing_document(provider_url, revision)})

    def create_turn(self, conversation_id: str, text: str, *, workspace: str, first: bool,
                    command_id: str | None = None, config: dict | None = None) -> dict:
        body = {'commandId': command_id or f'chaos-{uuid.uuid4().hex}',
                'config': {**CHAT_CONFIG, **(config or {}), 'model': MODEL_ID,
                           'projectPath': workspace},
                'message': {'text': text}}
        if first:
            body['conversation'] = {'allowCreate': True, 'title': f'chaos {conversation_id}', 'settings': {}}
        return self.request('POST', f'/api/v4/conversations/{conversation_id}/turns', body)

    def snapshot(self, conversation_id: str) -> dict:
        response = self.request('GET', f'/api/v4/conversations/{conversation_id}/sync')
        return response.get('data', response)

    def turns(self, conversation_id: str) -> list[dict]:
        return list(self.snapshot(conversation_id).get('turns') or [])

    def wait_settled(self, conversation_id: str, *, timeout: float) -> list[dict]:
        """Every Turn of the conversation reached a terminal status within ``timeout``.

        A restart pause is not where a Turn ends: recovery settles a fenced
        attempt ``interrupted`` (cause ``server_restart``) and its settlement
        drain then resumes it in place (``restart_resume.resume_recovered_turn``).
        Accepting the pause as terminal read it, under load, as the outcome.
        """
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                turns = self.turns(conversation_id)
            except (ApiError, OSError) as error:  # an API restart window answers nothing
                last = error
                time.sleep(0.2)
                continue
            assistant = [t for t in turns if t.get('actor') == 'assistant']
            if (assistant and all(t.get('status') in TERMINAL_TURN_STATUSES for t in assistant)
                    and not _awaiting_restart_resume(assistant[-1])):
                return turns
            last = [(t.get('actor'), t.get('status')) for t in turns]
            time.sleep(0.2)
        raise TimeoutError(f'turns of {conversation_id} did not settle within {timeout:.0f}s: {last}')


def _awaiting_restart_resume(turn: dict) -> bool:
    """The newest Turn is a restart pause whose automatic resume has not replaced it yet.

    A pause recovery holds instead (a cut tool call whose effect it could not
    confirm) is final and carries its error; only a silent pause is resumed.
    """
    settlement = turn.get('settlement') or {}
    return (turn.get('status') == 'interrupted' and settlement.get('cause') == 'server_restart'
            and not ((settlement.get('error') or {}).get('message') or '').strip())


class EventStream:
    """One replayable ``/events`` subscription; ``close`` is a client disconnect."""

    def __init__(self, base_url: str, conversation_id: str, *, after: str | None = None,
                 idle_seconds: float = 30.0):
        parsed = urllib.parse.urlsplit(base_url)
        # A read idle this long ends ``events()``: the replay has been drained.
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=idle_seconds)
        headers = {'Accept': 'text/event-stream'}
        if after:
            headers['Last-Event-ID'] = after
        self.connection.request('GET', f'/api/v4/conversations/{conversation_id}/events', headers=headers)
        self.response = self.connection.getresponse()
        if self.response.status != 200:
            raise ApiError(self.response.status, self.response.read()[:500])
        self.last_id: str | None = after

    def events(self):
        """Yield ``(id, event, data)`` until the server ends the stream."""
        event_id = event = None
        data: list[str] = []
        while True:
            try:
                line = self.response.fp.readline()
            except TimeoutError:
                return
            if not line:
                return
            line = line.decode().rstrip('\r\n')
            if not line:
                if data:
                    if event_id:
                        self.last_id = event_id
                    payload = '\n'.join(data)
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        pass
                    yield event_id, event, payload
                event_id = event = None
                data = []
                continue
            field, _, value = line.partition(':')
            value = value[1:] if value.startswith(' ') else value
            if field == 'id':
                event_id = value
            elif field == 'event':
                event = value
            elif field == 'data':
                data.append(value)

    def close(self) -> None:
        self.connection.close()
