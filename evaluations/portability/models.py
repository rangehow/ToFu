"""Subject/judge model access for the portability harness through Tofu's model registry.

Responsibility: every model call goes to the running Tofu server's
OpenAI-compatible endpoint, which applies model-routing v2 (docs/MODEL_REGISTRATION.md),
provider credentials and accounting. The harness never holds provider keys and
never picks a vendor endpoint. ``families`` reads each model's registered
creator from ``/v1/models`` so "different vendors" is the registry's fact, not a
name heuristic.

Entry points: ``ModelClient``, ``registry_models``, ``ENDPOINT``.
Dependencies: stdlib.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get('TOFU_PORTABILITY_ENDPOINT', 'http://127.0.0.1:15000')
# Liveness bound of one completion (long generations included); not a quality knob.
REQUEST_DEADLINE_S = 900
# Transient (429/5xx/connection) attempts per call; agent failures are never retried.
TRANSIENT_ATTEMPTS = 4


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f'Invalid JSON constant: {constant}')


def _request(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(ENDPOINT + path, data=data,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_DEADLINE_S) as response:
            raw = response.read()
    except http.client.IncompleteRead as exc:
        raise ModelInfrastructureError('Model endpoint returned an incomplete response body') from exc
    try:
        payload = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise ModelInfrastructureError('Model endpoint returned invalid UTF-8 or JSON') from exc
    if not isinstance(payload, dict):
        raise ModelInfrastructureError('Model endpoint response must be a JSON object')
    return payload


def registry_models() -> dict:
    """model id -> registered creator id, as the server's model registry reports it."""
    payload = _request('/v1/models')
    return {item['id']: (item.get('tofu') or {}).get('creator_id') or item.get('owned_by')
            for item in payload.get('data', [])}


class ModelInfrastructureError(RuntimeError):
    """The model client could not provide a usable, attributable completion."""


class ModelIdentityError(ModelInfrastructureError):
    """A response cannot be attributed to the requested model."""


class ModelCompletionError(ModelInfrastructureError):
    """The no-tool request did not affirmatively reach terminal completion."""


def _completion_content(payload: dict) -> str:
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelCompletionError('Completion choices must begin with a choice object')
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict):
        raise ModelCompletionError('Completion choice must contain a message object')
    if (choice.get('finish_reason') != 'stop'
            or message.get('tool_calls') not in (None, [])
            or message.get('function_call') is not None):
        raise ModelCompletionError('No-tool completion is incomplete or unknown')
    content = message.get('content')
    if not isinstance(content, str):
        raise ModelCompletionError('No-tool completion message must contain text content')
    return content


def _completion_usage(payload: dict) -> dict[str, int]:
    usage = payload.get('usage')
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise ModelCompletionError('Completion usage must be an object when provided')
    counters = {key: usage.get(key, 0) for key in ('prompt_tokens', 'completion_tokens')}
    if any(type(count) is not int or count < 0 for count in counters.values()):
        raise ModelCompletionError('Completion token counts must be nonnegative integers')
    return counters


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode('utf-8', 'replace')[:300]
    except (http.client.HTTPException, OSError) as exc:
        # Status (not the optional error body) controls the retry policy.
        return f'response body unavailable ({type(exc).__name__})'


class ModelClient:
    """One subject (or judge) model. Thread-safe usage accounting for the run report."""

    def __init__(self, model: str, *, identity_reader=None):
        self.model = model
        self.identity_reader = identity_reader
        self.deployment_identity = None
        self._lock = threading.Lock()
        self.usage = {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

    def current_identity(self):
        """Only a trusted host port can attest deployment; response.model cannot."""
        if self.identity_reader is None:
            return None
        value = self.identity_reader(self.model, None)
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ModelIdentityError('Trusted deployment identity is unavailable')
        return value

    def chat(self, messages: list, *, max_tokens: int = 4096, temperature: float = 0.0) -> str:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ModelIdentityError('Model identity mismatch: requested identity is unknown')
        # ``tools: []`` selects one direct provider completion, not a task-backed agent loop.
        body = {'model': self.model, 'messages': messages, 'max_tokens': int(max_tokens),
                'temperature': float(temperature), 'tools': [], 'stream': False}
        expected_deployment = self.current_identity()
        last: Exception | None = None
        for attempt in range(TRANSIENT_ATTEMPTS):
            try:
                payload = _request('/v1/chat/completions', body)
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc)
                last = ModelInfrastructureError(f'HTTP {exc.code}: {detail}')
                if exc.code < 500 and exc.code != 429:
                    raise last from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            else:
                if payload.get('error'):
                    last = RuntimeError(str(payload['error'])[:300])
                else:
                    if payload.get('model') != self.model:
                        raise ModelIdentityError(
                            f'Model identity mismatch: requested {self.model!r}, '
                            f'reported {payload.get("model")!r}')
                    content = _completion_content(payload)
                    usage = _completion_usage(payload)
                    if self.identity_reader is not None:
                        actual = self.identity_reader(self.model, payload)
                        if (actual != expected_deployment or self.deployment_identity not in (None, actual)):
                            raise ModelIdentityError('Deployment changed across the measured completion')
                        self.deployment_identity = actual
                    with self._lock:
                        self.usage['calls'] += 1
                        for key, count in usage.items():
                            self.usage[key] += count
                    return content
            time.sleep(2 ** attempt)
        raise ModelInfrastructureError(f'{self.model}: transient failures exhausted: {last}')

    def ask(self, prompt: str, *, system: str | None = None, max_tokens: int = 4096) -> str:
        messages = ([{'role': 'system', 'content': system}] if system else []) + [
            {'role': 'user', 'content': prompt}]
        return self.chat(messages, max_tokens=max_tokens)
