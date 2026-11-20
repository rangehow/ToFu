#!/usr/bin/env python3
"""Check the streaming-event and push-frame registry against contracts/events_v1.json.

Authority: ``contracts/events_v1.json`` (Rust rewrite Phase 0, see
docs/RUST_REWRITE_EVIDENCE.md "Contracts as the single source of Rust types").
Change the contract first. The Python registry in ``lib/agent_core/events.py``
(EventSpec / PhaseSpec / PushFrameSpec, the emission gate) is checked against
it in both directions: an event, phase, push frame or field that exists on one
side only fails with the difference. This script writes nothing.

The clients derived from the contract
    frontend/src/api/event-contract.generated.ts   TypeScript interfaces
    packages/tofu-contracts/src/generated_events_v1.rs   serde types + metadata
are rendered by ``tofu-xtask contracts`` (packages/tofu-xtask; ``make
contracts`` writes them, ``make contracts-check`` checks them).

Usage:
    python3 scripts/gen_event_contract.py   # registry == contract, else exit 1
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CONTRACT = os.path.join(ROOT, 'contracts', 'events_v1.json')
CONTRACT_ID = 'tofu.events/v1'


# ── Registry projection (the transition's Python side) ──────────────────

def _field(spec) -> dict:
    return {'name': spec.name, 'kind': spec.kind, 'required': spec.required}


def registry_document() -> dict:
    """The contract as lib/agent_core/events.py implements it today."""
    from lib.agent_core import events

    categories = [value for name, value in vars(events.EventCategory).items()
                  if not name.startswith('_') and isinstance(value, str)]
    return {
        'contract': CONTRACT_ID,
        'eventContractVersion': events.EVENT_CONTRACT_VERSION,
        'pushFrameContractVersion': events.PUSH_FRAME_CONTRACT_VERSION,
        'fieldKinds': sorted(events.FIELD_KINDS),
        'categories': categories,
        'readPolicies': ['durable', 'informational'],
        'transportTypes': sorted(events.TRANSPORT_TYPES),
        'clockStampedTypes': sorted(events._CLOCK_STAMPED_TYPES),
        'taskEventEnvelope': [
            {**_field(spec), 'description': prose}
            for spec, prose in events._TASK_EVENT_ENVELOPE],
        'events': [{
            'type': spec.type,
            'category': spec.category,
            'purpose': spec.purpose,
            'terminal': spec.terminal,
            'requiresResponse': spec.requires_response,
            'readPolicy': spec.read_policy,
            'since': spec.since,
            'fields': dict(spec.fields),
            'schema': (None if spec.schema is None
                       else [_field(item) for item in spec.schema]),
        } for spec in events.all_event_specs()],
        'phases': [{
            'phase': spec.phase,
            'domains': list(spec.domains),
            'purpose': spec.purpose,
            'since': spec.since,
            'fields': dict(spec.fields),
        } for spec in events.all_phase_specs()],
        'pushTransportEnvelope': [
            {**_field(spec), 'description': prose}
            for spec, prose in events.PUSH_TRANSPORT_ENVELOPE],
        'pushFrames': [{
            'type': spec.type,
            'channel': spec.channel,
            'task': spec.task,
            'purpose': spec.purpose,
            'since': spec.since,
            'fields': dict(spec.fields),
            'schema': (None if spec.schema is None
                       else [_field(item) for item in spec.schema]),
        } for spec in events.all_push_frame_specs()],
    }


def load_contract() -> dict:
    with open(CONTRACT, encoding='utf-8') as handle:
        document = json.load(handle)
    if document.get('contract') != CONTRACT_ID:
        raise ValueError(f'{CONTRACT} is not {CONTRACT_ID}')
    return document


def _keyed(items: list[dict], key: str) -> dict[str, dict]:
    return {item[key]: item for item in items}


def registry_drift(contract: dict, registry: dict | None = None) -> list[str]:
    """Human-readable differences; empty when registry == contract exactly."""
    registry = registry_document() if registry is None else registry
    body = {key: value for key, value in contract.items() if key != '$comment'}
    if body == registry:
        return []
    problems: list[str] = []
    for section, key in (('events', 'type'), ('phases', 'phase'),
                         ('pushFrames', 'type')):
        in_contract = _keyed(body.get(section, []), key)
        in_code = _keyed(registry[section], key)
        for name in sorted(in_code.keys() - in_contract.keys()):
            problems.append(f'{section}: {name!r} is registered in lib/agent_core/events.py '
                            'but missing from contracts/events_v1.json')
        for name in sorted(in_contract.keys() - in_code.keys()):
            problems.append(f'{section}: {name!r} is in contracts/events_v1.json but '
                            'not registered in lib/agent_core/events.py')
        for name in sorted(in_code.keys() & in_contract.keys()):
            if in_code[name] != in_contract[name]:
                problems.append(f'{section}: {name!r} differs:\n' + _diff(
                    in_contract[name], in_code[name]))
        if [item[key] for item in body.get(section, [])] != [item[key] for item in registry[section]] \
                and not problems:
            problems.append(f'{section}: order differs from the registry')
    for key in registry:
        if key not in ('events', 'phases', 'pushFrames') and body.get(key) != registry[key]:
            problems.append(f'{key} differs:\n' + _diff(body.get(key), registry[key]))
    for key in body.keys() - registry.keys():
        problems.append(f'{key}: not represented by the registry')
    return problems or ['contract and registry differ in key order']


def _diff(contract_value, registry_value) -> str:
    left = json.dumps(contract_value, indent=1, ensure_ascii=False).splitlines()
    right = json.dumps(registry_value, indent=1, ensure_ascii=False).splitlines()
    return '\n'.join(difflib.unified_diff(left, right, 'contract', 'registry', lineterm=''))


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split('\n', 1)[0]).parse_args()
    problems = registry_drift(load_contract())
    if problems:
        print('event contract drift between contracts/events_v1.json and '
              'lib/agent_core/events.py:', file=sys.stderr)
        for problem in problems:
            print(f'  - {problem}', file=sys.stderr)
        return 1
    print('event registry matches contracts/events_v1.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
