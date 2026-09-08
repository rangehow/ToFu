"""Immutable admitted request at provider ingress; no cache or prompt telemetry.

Admission counts canonical input locally. Provider translation then seals the
wire JSON; transports decode a fresh disposable envelope, never caller aliases.
Two bounded digests distinguish semantic input from provider prefix projection.
They are research evidence, not a claim about a provider's hidden cache key.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json

from types import MappingProxyType

def _encode(value, *, sorted_keys=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sorted_keys,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value, *, sorted_keys=False):
    return hashlib.sha256(_encode(value, sorted_keys=sorted_keys)).hexdigest()


def detach_input(body):
    """Detach count-bearing JSON before counting and subsequent translation."""
    for key in ('messages', 'system', 'tools'):
        if key in body:
            body[key] = json.loads(_encode(body[key]))


def semantic_request_id(body):
    """Content identity, independent of endpoint and generation parameters."""
    return _digest({key: body.get(key) for key in
                    ('model', 'messages', 'system', 'tools')}, sorted_keys=True)


@dataclass(frozen=True, slots=True)
class AdmittedRequest(Mapping):
    """One attempt's sole retained wire snapshot and content-bound evidence.

    Mapping reads return detached values for legacy plan consumers. No mutable
    prompt reference or caller-supplied token evidence can authorize this value.
    The snapshot expires with its request; only fixed-size IDs enter diagnostics.
    """
    payload: bytes = field(repr=False)
    model: str
    provider_id: str
    protocol: str
    endpoint: str
    input_tokens: int
    semantic_id: str
    provider_prefix_id: str

    _fields: Mapping = field(repr=False, compare=False)
    def wire_body(self):
        return json.loads(self.payload)

    def __getitem__(self, key):
        start, end, scalar, value = self._fields[key]
        return value if scalar else json.loads(self.payload[start:end])

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)

    def diagnostics(self):
        return {'semantic_request_id': self.semantic_id,
                'provider_prefix_id': self.provider_prefix_id,
                'admitted_input_tokens': self.input_tokens}


def seal_request(body, *, model, provider_id='', protocol='openai',
                 endpoint='', input_tokens, semantic_id):
    """Seal a translated body; admission evidence is supplied by ingress only."""
    wire = {key: value for key, value in body.items()
            if not key.startswith('_')}
    # Encode each field once; offsets retain no second image/prompt copy.
    chunks = [b'{']
    offsets = {}
    position = 1
    for key, value in wire.items():
        header = (b',' if offsets else b'') + _encode(key) + b':'
        encoded = _encode(value)
        start = position + len(header)
        position = start + len(encoded)
        scalar = value is None or isinstance(value, (str, int, float, bool))
        offsets[key] = (start, position, scalar, value if scalar else None)
        chunks.extend((header, encoded))
    chunks.append(b'}')
    payload = b''.join(chunks)
    # Hash the actual ordered provider-visible prefix, not sampling/output knobs.
    prefix = {key: value for key, value in wire.items() if key in (
        'model', 'messages', 'input', 'system', 'instructions', 'tools',
        'prompt_cache_key')}
    prefix_id = _digest([model, provider_id, protocol, endpoint, prefix])
    return AdmittedRequest(payload, model, provider_id, protocol, endpoint,
                           input_tokens, semantic_id, prefix_id,
                           MappingProxyType(offsets))


def recount_anthropic_input(body, *, model, provider_id=''):
    """Project final translated/cloaked input into the local counter vocabulary.

    Native tool use and images must not disappear during this last admission;
    opaque thinking signatures remain excluded, as in canonical admission.
    """
    from lib.llm.body._admission import enforce_body_input_limit

    messages = []
    for message in body.get('messages') or ():
        projected = dict(message)
        content = message.get('content')
        if isinstance(content, list):
            blocks, calls, thinking = [], [], []
            for block in content:
                kind = block.get('type') if isinstance(block, dict) else None
                if kind == 'tool_use':
                    calls.append({'function': {
                        'name': block.get('name', ''),
                        'arguments': _encode(block.get('input', {})).decode()}})
                elif kind == 'image':
                    blocks.append({'type': 'image_url'})
                elif kind == 'thinking':
                    thinking.append({'text': block.get('thinking', '')})
                else:
                    blocks.append(block)
            projected.update(content=blocks, tool_calls=calls,
                             reasoning_content=thinking)
        messages.append(projected)
    canonical = {'messages': messages, 'system': body.get('system'),
                 'tools': body.get('tools')}
    return enforce_body_input_limit(
        canonical, model=model, provider_id=provider_id)


def transport_body(body):
    """Fresh wire envelope at the existing fakeable HTTP/WS transport seam."""
    return body.wire_body() if isinstance(body, AdmittedRequest) else body
