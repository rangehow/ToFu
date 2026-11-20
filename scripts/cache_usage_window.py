#!/usr/bin/env python3
"""Read-only, aggregate CacheStats × CacheRoundRecord trace reconciliation.

Supply retained application log files (including rotations) and a half-open
window in their local clock. Output contains counts and coarse totals only;
trace IDs, conversation IDs, task IDs and prefix digests never leave memory.
This report neither reprices input nor infers provider cache eligibility.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re


_STATS = re.compile(
    r'^\S+ conv=(\S+) R([1-9]\d*) model=(\S+) input=(\d+) '
    r'cache_w=(\d+|unknown) cache_r=(\d+) hit=\d+% key=\S+ retries=\d+ '
    r'trace=(\S+)(?: COLD)?$')
_MARKERS = {'stats': ('_roi', 'CacheStats'), 'records': ('_detect', 'CacheRoundRecord')}
_LINES = {kind: re.compile(
    rf' \[INFO\] lib\.tasks_pkg\.cache_tracking\.{owner} \[[^\]]+\]: '
    rf'(?:\[rid:[^\]]+\] )?\[{marker}\] ')
    for kind, (owner, marker) in _MARKERS.items()}
# Do not publish arbitrary strings from retained logs as group labels.
_MODELS = frozenset({'gpt-6.1-sol', 'gpt-6.2-sol'})


def _identity(trace):
    return isinstance(trace, str) and bool(trace.strip()) and trace != '-'


def _stats(payload):
    match = _STATS.fullmatch(payload)
    if not match:
        raise ValueError('malformed stats')
    conv, task_round, model, tokens, write, read, trace = match.groups()
    return trace, (conv, int(task_round), model, int(tokens),
                   None if write == 'unknown' else int(write), int(read))


def _record_read(row):
    read = row.get('cache_read')
    if type(read) is not int or read < 0:
        return None, None, 'unknown_rows'
    if 'responses_usage_status' not in row:
        # A pre-provenance zero may have been filled by usage normalization.
        return read, None, 'positive_rows' if read else 'legacy_zero_rows'
    status = row['responses_usage_status']
    total = row.get('input_tokens')
    if (not isinstance(status, dict) or status.get('cache_read') != 'observed'
            or status.get('input_tokens') != 'observed'
            or type(total) is not int or read > total):
        return None, None, 'unknown_rows'
    return read, total, 'positive_rows' if read else 'explicit_zero_rows'


def _record(payload):
    row = json.loads(payload)
    if not isinstance(row, dict) or not isinstance(row.get('conv'), str) or not isinstance(
            row.get('model'), str):
        raise ValueError('malformed record')
    read, reported_input, read_category = _record_read(row)
    identity = row.get('request_identity')
    prefix = identity.get('provider_prefix_id') if isinstance(identity, dict) else None
    if not isinstance(prefix, str) or not re.fullmatch('[0-9a-f]{64}', prefix):
        prefix = None
    baseline = ('absent' if row.get('baseline') == 'none' else
                'verified_unchanged' if (row.get('wire_available') is True
                                         and row.get('body_identical') is True
                                         and row.get('namespace_verified') is True)
                else 'changed_or_insufficient')
    return row.get('trace_id'), (row['conv'], row['model'], read,
                                 row.get('call') if type(row.get('call')) is int else None,
                                 prefix, baseline, reported_input, read_category)


def _model_group(model):
    return model if model in _MODELS else 'other'


def _read_coverage_total():
    return dict.fromkeys(('rows', 'positive_rows', 'explicit_zero_rows',
                          'legacy_zero_rows', 'unknown_rows'), 0)


def _detector_read_coverage(records):
    coverage = {'all': _read_coverage_total(), 'by_model': {}}
    for candidates in records.values():
        if len(candidates) != 1:
            continue  # separately counted as conflicting_trace_ids
        ((_, model, _, _, _, _, _, category),) = candidates
        group = coverage['by_model'].setdefault(_model_group(model), _read_coverage_total())
        for tally in (coverage['all'], group):
            tally['rows'] += 1
            tally[category] += 1
    return coverage


def _total():
    return dict.fromkeys(('rows', 'input', 'cache_read', 'input_minus_read',
                          'cache_write_reported', 'cache_write_unknown_rows',
                          'zero_read_rows'), 0)


def _accumulate(total, tokens, read, write):
    total['rows'] += 1
    total['input'] += tokens
    total['cache_read'] += read
    total['input_minus_read'] += tokens - read
    total['cache_write_reported'] += write or 0
    total['cache_write_unknown_rows'] += write is None
    total['zero_read_rows'] += read == 0


def reconcile(lines, *, start, end):
    """Deduplicate identical projected rows and exclude contradictory traces."""
    if start >= end:
        raise ValueError('window start must precede end')
    rows = {kind: defaultdict(set) for kind in _MARKERS}
    ingest = {kind: Counter(dict.fromkeys(('marker_rows', 'malformed', 'missing_trace',
                                          'duplicate_projected_rows'), 0)) for kind in _MARKERS}
    for line in lines:
        stamp = line[:19]
        if not start <= stamp < end:
            continue
        for kind, prefix_pattern in _LINES.items():
            prefix_match = prefix_pattern.match(line, 19)
            if prefix_match is None:
                continue
            counts = ingest[kind]
            counts['marker_rows'] += 1
            try:
                trace, row = (_stats if kind == 'stats' else _record)(
                    line[prefix_match.end():].rstrip('\r\n'))
            except (ValueError, TypeError, OverflowError):
                counts['malformed'] += 1
                break
            if not _identity(trace):
                counts['missing_trace'] += 1
                break
            candidates = rows[kind][trace]
            counts['duplicate_projected_rows'] += row in candidates
            candidates.add(row)
            break

    stats, records = rows['stats'], rows['records']
    for kind, side in rows.items():
        ingest[kind]['distinct_trace_ids'] = len(side)
        ingest[kind]['conflicting_trace_ids'] = sum(len(values) > 1 for values in side.values())
    join = Counter(stats_only=len(stats.keys() - records.keys()),
                   records_only=len(records.keys() - stats.keys()),
                   ambiguous=0, identity_or_usage_mismatch=0,
                   read_not_comparable=0, round_ordinal_mismatch=0)
    report = {'window': {'start_inclusive': start, 'end_exclusive': end},
              'evidence_scope': {'dispatch_coverage': 'unknown', 'provider_attribution': 'unavailable',
                                 'configured_estimate': 'unavailable', 'upstream_actual': 'unavailable'},
              'ingest': ingest, 'join': join, 'all': _total(), 'by_model': {},
              'detector_read_coverage': _detector_read_coverage(records)}
    prefixes = Counter()
    for trace in stats.keys() & records.keys():
        if len(stats[trace]) != 1 or len(records[trace]) != 1:
            join['ambiguous'] += 1
            continue
        ((conv, task_round, model, tokens, write, read),) = stats[trace]
        ((r_conv, r_model, r_read, call, prefix, overlap, r_input, _),) = records[trace]
        if (conv, model) != (r_conv, r_model) or read > tokens:
            join['identity_or_usage_mismatch'] += 1
            continue
        if r_read is None:
            join['read_not_comparable'] += 1
            continue
        if read != r_read or (r_input is not None and tokens != r_input):
            join['identity_or_usage_mismatch'] += 1
            continue
        join['round_ordinal_mismatch'] += call is not None and call != task_round
        if prefix:
            prefixes[prefix] += 1
        grouped = report['by_model'].setdefault(_model_group(model), {})
        for total in (report['all'], grouped.setdefault('all', _total()),
                      grouped.setdefault('first_task_round' if task_round == 1 else 'later_task_round', _total()),
                      grouped.setdefault(overlap, _total())):
            _accumulate(total, tokens, read, write)
    report['sealed_prefix_id_coverage'] = {
        'rows_with_id': sum(prefixes.values()), 'distinct_ids': len(prefixes),
        'additional_occurrences': sum(prefixes.values()) - len(prefixes),
    }
    return report


def _timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').isoformat(sep=' ')
    except ValueError as error:
        raise argparse.ArgumentTypeError('use YYYY-MM-DD HH:MM:SS (log local time)') from error


def _read_paths(paths):
    for path in paths:
        with path.open(encoding='utf-8', errors='replace') as stream:
            yield from stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, type=_timestamp)
    parser.add_argument('--end', required=True, type=_timestamp)
    parser.add_argument('logs', nargs='+', type=Path)
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error('--start must precede --end')
    print(json.dumps(reconcile(_read_paths(args.logs), start=args.start, end=args.end),
                     indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
