#!/usr/bin/env python3
"""Measure provider token units against Tofu's local count from persisted rounds.

Reads conversation dumps written by ``debug/inspect_conversation.py <id> --json``
(or any JSON document with a ``messages`` list carrying ``apiRounds``) and, per
token-unit family (``lib/model_info/token_units.token_unit_family``), reports
the observed range of

    provider-reported prompt tokens / Tofu's local count of the same request

where the local count is ``usage._request_identity.admitted_local_tokens``.
Rows recorded before that field existed (2026-11-11) carried the local count
in ``admitted_input_tokens``, which now holds the serving-unit size; they are
measured from it. Rows whose provider input is provider-owned
(``effective_prompt_tokens`` differing from ``prompt_tokens``) are excluded.

``--write`` replaces ``lib/model_info/token_unit_facts.json`` with the measured
families after validating the document; review the diff like any maintained
fact. Measure every available conversation of each family: a bound narrower
than a measured request is not a bound. Read-only otherwise: no network,
credentials or storage access.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.model_info.token_units import (  # noqa: E402
    CATALOG_CONTRACT,
    CATALOG_PATH,
    token_unit_family,
    validate_token_unit_catalog,
)
from lib.token_counter.tiktoken_counter import encoding_for_model  # noqa: E402


def _provider_prompt_tokens(usage: dict) -> int | None:
    prompt = usage.get('prompt_tokens')
    effective = usage.get('effective_prompt_tokens')
    if (type(effective) is int and effective > 0 and type(prompt) is int
            and effective != prompt):
        return None
    if type(prompt) is int and prompt > 0:
        return prompt
    residual = usage.get('input_tokens')
    if type(residual) is not int:
        return None
    total = (residual + int(usage.get('cache_creation_input_tokens') or 0)
             + int(usage.get('cache_read_input_tokens') or 0))
    return total if total > 0 else None


def _local_tokens(usage: dict) -> int | None:
    identity = usage.get('_request_identity') or {}
    for key in ('admitted_local_tokens', 'admitted_input_tokens'):
        value = identity.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def measured_rows(paths):
    """Yield one deduplicated measurement per persisted provider request."""
    seen = set()
    for path in paths:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
        messages = document.get('messages') if isinstance(document, dict) else document
        conversation = (document.get('conversationId') if isinstance(document, dict)
                        else None) or str(path)
        for message in messages or ():
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            for row in message.get('apiRounds') or ():
                usage = row.get('usage') or {}
                dispatch = usage.get('_dispatch') or {}
                model = dispatch.get('logical_model') or row.get('model') or ''
                key = (conversation, message.get('_attemptId'), row.get('round'),
                       row.get('tag'), usage.get('trace_id'))
                if key in seen:
                    continue
                seen.add(key)
                provider = _provider_prompt_tokens(usage)
                local = _local_tokens(usage)
                if not model or provider is None or local is None:
                    continue
                started = dispatch.get('stream_started_at_unix_ns')
                day = (datetime.datetime.fromtimestamp(
                    started / 1e9, datetime.timezone.utc).date().isoformat()
                    if type(started) is int and started > 0 else None)
                yield {'model': model, 'provider': provider, 'local': local,
                       'day': day, 'conversation': conversation}


def measure(paths) -> dict:
    """Per-family facts; bounds round outward so they never shrink the range."""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for row in measured_rows(paths):
        family = token_unit_family(row['model'])
        if family is not None:
            groups[family].append(row)
    families = {}
    for family, rows in sorted(groups.items()):
        encodings = {encoding_for_model(row['model']) for row in rows}
        if len(encodings) != 1:
            raise SystemExit(f'{family}: members use several local encodings {sorted(encodings)}; '
                             'split the family before recording a bound')
        ratios = [row['provider'] / row['local'] for row in rows]
        days = sorted(row['day'] for row in rows if row['day'])
        families[family] = {
            'local_encoding': encodings.pop(),
            'provider_per_local': {
                'min': math.floor(min(ratios) * 10_000) / 10_000,
                'max': math.ceil(max(ratios) * 10_000) / 10_000,
            },
            'samples': len(rows),
            'members': dict(sorted(collections.Counter(
                row['model'] for row in rows).items())),
            'conversations': len({row['conversation'] for row in rows}),
            'measured_from': days[0] if days else None,
            'measured_to': days[-1] if days else None,
        }
    return families


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('dumps', nargs='+', help='inspect_conversation --json outputs')
    parser.add_argument('--write', action='store_true',
                        help=f'replace {CATALOG_PATH.relative_to(ROOT)} with the measurement')
    args = parser.parse_args(argv)
    families = measure(args.dumps)
    if not families:
        print('no measurable provider rounds', file=sys.stderr)
        return 1
    for family, fact in families.items():
        bound = fact['provider_per_local']
        print(f"{family:10s} N={fact['samples']:6d} convs={fact['conversations']:3d} "
              f"min={bound['min']:.4f} max={bound['max']:.4f} "
              f"{fact['local_encoding']} {fact['measured_from']}..{fact['measured_to']} "
              f"{fact['members']}")
    if args.write:
        document = {
            'contract': CATALOG_CONTRACT,
            'definition': ('provider_per_local = provider-reported prompt tokens / '
                           "Tofu's local count of the same request"),
            'generator': 'scripts/measure_token_units.py',
            'families': families,
        }
        validate_token_unit_catalog(document)
        CATALOG_PATH.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n',
                                encoding='utf-8')
        print(f'wrote {CATALOG_PATH.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
