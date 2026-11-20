"""Regenerate packages/tofu-core-rs/tests/fixtures/catalog_golden.json from Python.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/gen_catalog_golden.py [--check] [--dump DIR]``

Every model of the maintained seed catalog (``current_model_defaults()``,
consulted by registration but not the complete set of admitted registrations)
runs on one selected fixture route with text-only history and API-key auth,
through ``build_body`` + ``prepare_request`` once per listed reasoning setting: the default, the enablement toggle, every depth
(off/lowest/enabled/minimal..ultra), every exact effort and two explicit
thinking budgets. The route profile is the catalog profile resolved with the
face's declared features, as a selected slot carries it. Each case records
the URL, the headers and the SHA-256 of the sealed payload, or the exception
Python raises; the cargo test requires Rust to reproduce each one and fails
on any ``Unsupported``. ``--dump DIR`` writes the payloads for debugging.
``--check`` exits 1 when the committed fixture differs from a regeneration.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_wire_golden as wire  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'catalog_golden.json'
MAX_TOKENS = 1024
TOOLS = [copy.deepcopy(wire.TOOLS[0])]
# One tool round minted by another model: Gemini needs the skip signature,
# Mistral's fixed nine-character ID format respells `call_a1`.
MESSAGES = [
    {'role': 'system', 'content': 'You are a test harness.'},
    {'role': 'user', 'content': 'List the files.'},
    {'role': 'assistant', 'content': 'Listing.', 'tool_calls': [
        {'id': 'call_a1', 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{"command": "ls"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_a1', 'content': 'a\nb'},
    {'role': 'user', 'content': 'Thanks.'},
]
MISTRAL_API = 'https://api.mistral.ai/v1'
ANTHROPIC_FACE = {'features': {'cache_affinity_mt_session_id': 'supported'}}
CHAT_FACE = {'features': {'cache_affinity_mt_session_id': 'supported'}}
KUNLUN_FACE = wire.KUNLUN
# name -> (base_url, protocol, face profile)
ROUTES = {
    'friday-anthropic': (wire.FRIDAY_ANTHROPIC, 'anthropic', ANTHROPIC_FACE),
    'friday-chat': (wire.FRIDAY_CHAT, 'openai', CHAT_FACE),
    'friday-kunlun-responses': (wire.FRIDAY_RESPONSES, 'responses', KUNLUN_FACE),
    'mistral-api': (MISTRAL_API, 'openai', None),
}
_DEPTHS = ('off', 'lowest', 'enabled', 'default', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'no_think')
# name -> build_body reasoning arguments (thinking_enabled/thinking_depth/effort/extra)
INTENTS = {
    'default': {},
    'enabled': {'thinking_enabled': True},
    'disabled': {'thinking_enabled': False},
    **{f'depth-{depth}': {'thinking_depth': depth} for depth in _DEPTHS},
    **{f'effort-{effort}': {'effort': effort} for effort in _EFFORTS},
    'budget-2048': {'extra': {'thinking': {'type': 'enabled', 'budget_tokens': 2048}}},
    'budget-512': {'extra': {'thinking': {'type': 'enabled', 'budget_tokens': 512}}},
}


def home_route(model_id, creator_id):
    """Select a representative fixture face, not an exhaustive routing proof.

    This deliberately does not enumerate installed registrations, all provider
    offerings or images/OAuth/native capabilities of the same model.
    """
    from lib.model_info import is_claude, is_gpt5, is_kimi_k3
    if is_claude(model_id) or is_kimi_k3(model_id):
        return 'friday-anthropic'
    if creator_id == 'openai' and is_gpt5(model_id):
        return 'friday-kunlun-responses'
    if creator_id == 'mistral':
        return 'mistral-api'
    return 'friday-chat'


def _error(exc):
    return {'class': type(exc).__name__, 'message': str(exc)}


def build(dump=None):
    for name in ('TOFU_CACHE_MID_MODE', 'TOFU_CACHE_MARKERS_NONCLAUDE'):
        os.environ.pop(name, None)
    profiles = wire._install_profiles()
    os.chdir(wire.WORKING_DIR)
    import lib
    lib.CACHE_EXTENDED_TTL = False
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    from lib.model_routing.inference import resolve_inference_profile
    from lib.model_routing.model_defaults import current_model_defaults
    catalog = sorted(current_model_defaults().exact.items(),
                     key=lambda item: (item[0].creator_id, item[0].model_id))
    models, cases = [], []
    endpoints = {}
    for ref, (profile, _previous) in catalog:
        route = home_route(ref.model_id, ref.creator_id)
        base_url, protocol, face = ROUTES[route]
        selected = resolve_inference_profile(
            model={'inference_profile': profile},
            offering={'inference_profile': face} if face else None,
            connection={'protocol': protocol})
        models.append({'creator_id': ref.creator_id, 'model_id': ref.model_id, 'route': route,
                       'inference_profile': selected})
        for intent, arguments in INTENTS.items():
            case = {'model_id': ref.model_id, 'intent': intent}
            try:
                body = build_body(ref.model_id, copy.deepcopy(MESSAGES), max_tokens=MAX_TOKENS,
                                  tools=copy.deepcopy(TOOLS), provider_id='golden',
                                  precomputed_input_tokens=100, context_window=200000,
                                  inference_profile=copy.deepcopy(selected), **copy.deepcopy(arguments))
                plan = prepare_request(body, api_key='sk-golden', base_url=base_url, api_protocol=protocol)
            except Exception as exc:  # the oracle's exception is the expected outcome
                case['error'] = _error(exc)
            else:
                # Without lineage or extended TTL the endpoint is fixed per route.
                endpoint = {'url': plan.url,
                            'headers': [[k, v] for k, v in plan.hdrs.items() if k != 'M-TraceId']}
                if endpoints.setdefault(route, endpoint) != endpoint:
                    raise SystemExit(f'{route}: endpoint differs for {ref.model_id} {intent}')
                case['payload_sha256'] = hashlib.sha256(plan.body.payload).hexdigest()
                if dump:
                    (Path(dump) / f'{ref.model_id}__{intent}.json').write_bytes(plan.body.payload)
            cases.append(case)
    return json.dumps({
        'generator': 'packages/tofu-core-rs/scripts/gen_catalog_golden.py',
        'max_tokens': MAX_TOKENS, 'temperature': 1.0, 'api_key': 'sk-golden', 'working_dir': wire.WORKING_DIR,
        'gateway_profiles': profiles, 'messages': MESSAGES, 'tools': TOOLS,
        'routes': {name: {'base_url': url, 'protocol': protocol, **endpoints.get(name, {})}
                   for name, (url, protocol, _) in ROUTES.items()},
        'intents': INTENTS, 'models': models, 'cases': cases,
    }, indent=1, ensure_ascii=False) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--dump', default='')
    args = parser.parse_args()
    if args.dump:
        Path(args.dump).mkdir(parents=True, exist_ok=True)
    text = build(dump=os.path.abspath(args.dump) if args.dump else None)
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding='utf-8') != text:
            print(f'{OUT} is stale; rerun {Path(__file__).name}', file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
