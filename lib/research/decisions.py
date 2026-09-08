"""Derive bounded research decisions and audit gates from current run receipts.

No tool execution or numerical confidence estimation occurs here. Protocol plans
are proposals; only structured completed-result receipts can satisfy audit gates.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

AUDIT_CHECKS = ('baseline_fairness', 'uncertainty', 'seeds', 'validation_split')


def normalize_portfolio(value):
    raw = value if isinstance(value, Mapping) else {}
    result = {}
    for key, fields in {
        'competing_hypotheses': ('id', 'statement', 'prediction', 'scope'),
        'discriminating_experiments': ('id', 'hypothesis_ids', 'design', 'decision_rule',
                                     'expected_information', 'expected_value', 'cost'),
    }.items():
        rows = raw.get(key)
        if not isinstance(rows, list):
            continue
        normalized = []
        for row in rows[:16]:
            if not isinstance(row, Mapping):
                continue
            item = {field: str(row.get(field) or '').strip()[:2000]
                    for field in fields if field != 'hypothesis_ids'}
            if 'hypothesis_ids' in fields:
                ids = row.get('hypothesis_ids')
                item['hypothesis_ids'] = [str(x)[:96] for x in ids[:16]] if isinstance(ids, list) else []
            if item['id']:
                normalized.append(item)
        if normalized:
            result[key] = normalized
    return result


def authoritative(run):
    return (run.get('validity') == 'current'
            and run.get('execution_status') == 'completed'
            and any(r.get('status') == 'done' and r.get('result_digest')
                    for r in run.get('tool_receipts', [])))


def research_decision(protocol, runs):
    observed = [r for r in runs if authoritative(r)]
    outcomes = {r.get('scientific_outcome') for r in observed}
    status = ('retain' if outcomes == {'supported'} else
              'refute' if outcomes == {'refuted'} else
              'refine' if {'supported', 'refuted'}.issubset(outcomes) else 'inconclusive')
    reason = {
        'retain': 'Retain only within the tested protocol scope; independent validation remains necessary.',
        'refute': 'The tested hypothesis is refuted in this protocol scope. A negative-result manuscript is valid.',
        'refine': 'Conflicting results require a narrower scope and a discriminating replication.',
        'inconclusive': 'No decisive current evidence; repair missing evidence or resolve uncertainty before claiming support.',
    }[status]
    plans = normalize_portfolio(protocol).get('discriminating_experiments', [])
    next_actions = []
    for index, plan in enumerate(plans):
        next_actions.append(dict(plan, priority=index + 1, requires_confirmation=True))
    if not next_actions:
        next_actions = [{
            'id': 'resolve-current-evidence', 'hypothesis_ids': [], 'priority': 1,
            'design': ('Replicate the frozen comparison on independent held-out data; '
                       'separate scope effects from the competing explanation.'),
            'decision_rule': str(protocol.get('falsifier') or 'Owner must freeze a falsifier before execution.'),
            'expected_information': 'Distinguish reproducible effects from uncertainty or protocol-specific failure.',
            'expected_value': 'Bound the claims that can be reported, including a negative result.',
            'cost': str(protocol.get('resources') or 'Unknown; estimate compute, data, time and tool costs before confirmation.'),
            'requires_confirmation': True,
        }]
    return {'status': status, 'reason': reason,
            'run_ids': [r['id'] for r in observed],
            'scope': str(protocol.get('dataset') or 'Unspecified tested population'),
            'next_actions': next_actions}


def manuscript_audit(runs, claims):
    checks = {key: False for key in AUDIT_CHECKS}
    eligible = [r for r in runs if authoritative(r)]
    # A receipt must explicitly attest each check with traceable evidence.
    # Prose mentioning a seed or baseline is never treated as verification.
    for key in checks:
        checks[key] = bool(eligible)
        for run in eligible:
            verified = False
            refs = {run.get('id'), run.get('artifact_ref'), *run.get('artifact_refs', [])} - {None, ''}
            for receipt in run.get('tool_receipts', []):
                if receipt.get('status') != 'done' or not receipt.get('result_digest'):
                    continue
                try:
                    payload = json.loads(receipt.get('result_excerpt') or '')
                except (ValueError, TypeError):
                    continue
                audit = payload.get('scientific_audit') if isinstance(payload, Mapping) else None
                check = audit.get(key) if isinstance(audit, Mapping) else None
                if isinstance(check, Mapping):
                    linked = check.get('evidence_refs')
                    verified |= (check.get('status') == 'verified' and isinstance(linked, list)
                                 and bool(linked) and all(isinstance(x, str) and x in refs for x in linked))
            checks[key] &= verified
    checks['claim_traceability'] = bool(claims) and all(
        c.get('status') == 'supported' and c.get('validity') == 'current' for c in claims)
    return {'passed': all(checks.values()),
            'checks': [{'id': key, 'ok': ok} for key, ok in checks.items()]}
