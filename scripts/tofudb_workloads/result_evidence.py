"""Exact seeded point-read evidence; no generic result normalization or digest.

Entry: compare_results, used by the operation runner and offline assessment.
"""
from __future__ import annotations

import json
import math
import sys

from scripts.tofudb_workloads.inputs import WorkloadInputs
from scripts.tofudb_workloads.latency import latency_metrics
from scripts.tofudb_workloads.records import record_value

FORMAT = 'tofu.seeded-record-results/v1'


def exact(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('non-JSON result key')
        for item in value.values():
            exact(item)
    elif type(value) is list:
        for item in value:
            exact(item)
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError('non-JSON result value')
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      allow_nan=False, separators=(',', ':'))


def _validate(stats):
    evidence = stats.get('result_evidence')
    if 'error' in stats or not isinstance(evidence, dict) or evidence.get('format') != FORMAT:
        raise ValueError('execution error or missing supported result evidence')
    namespace, seeds = evidence.get('namespace'), evidence.get('seed_records')
    warmup, count = evidence.get('warmup'), stats.get('iterations')
    samples = evidence.get('samples')
    clocks = stats.get('samples_ms')
    receipts = evidence.get('seed_receipts')
    if (not isinstance(namespace, str) or not namespace or type(seeds) is not int or seeds <= 0
            or type(warmup) is not int or warmup < 0 or type(count) is not int or count <= 0
            or not isinstance(samples, list) or len(samples) != count
            or stats.get('sample_order') != 'sequential-operation-start'
            or not isinstance(clocks, list) or len(clocks) != count
            or any(type(value) not in (int, float) or not 0 <= value <= sys.float_info.max
                   or (type(value) is float and not math.isfinite(value)) for value in clocks)
            or not isinstance(receipts, list) or len(receipts) != seeds
            or evidence.get('response_shape') not in ('bare', 'versioned')):
        raise ValueError('invalid or incomplete result population')
    if evidence['response_shape'] == 'versioned':
        for index, receipt in enumerate(receipts):
            if (not isinstance(receipt, dict) or set(receipt) != {'key', 'version', 'updated_at_ms'}
                    or receipt['key'] != WorkloadInputs.record_key(index)):
                raise ValueError('missing or invalid deterministic seed receipt')
            if (type(receipt['version']) is not int or not 0 < receipt['version'] < 2**64
                    or type(receipt['updated_at_ms']) is not int or not 0 < receipt['updated_at_ms'] < 2**64):
                raise ValueError('missing or invalid versioned record metadata')
    for index, sample in enumerate(samples, warmup):
        request = ['record.get', {'namespace': namespace,
                                 'key': WorkloadInputs.record_key(index % seeds)}, False]
        if (not isinstance(sample, dict) or type(sample.get('iteration')) is not int
                or sample['iteration'] != index or exact(sample.get('request')) != exact(request)):
            raise ValueError('timed request differs from deterministic population')
        if 'result' not in sample:
            raise ValueError('missing timed result')
        result = sample['result']
        value = result
        if evidence['response_shape'] == 'versioned':
            if (not isinstance(result, dict) or set(result) != {'value', 'version', 'updated_at_ms'}
                    or type(result['version']) is not int or not 0 < result['version'] < 2**64
                    or type(result['updated_at_ms']) is not int or not 0 < result['updated_at_ms'] < 2**64):
                raise ValueError('missing or invalid versioned record metadata')
            receipt = receipts[index % seeds]
            if (not isinstance(receipt, dict) or set(receipt) != {'key', 'version', 'updated_at_ms'}
                    or receipt['key'] != request[1]['key']
                    or exact(result['version']) != exact(receipt['version'])
                    or exact(result['updated_at_ms']) != exact(receipt['updated_at_ms'])):
                raise ValueError('record metadata differs from seed write receipt')
            value = result['value']
        if exact(value) != exact(record_value(index % seeds)):
            raise ValueError('timed result differs from seeded value oracle')
    for field, expected in latency_metrics(clocks).items():
        value = stats.get(field)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not math.isfinite(expected) or value != expected):
            raise ValueError(f'latency summary {field} differs from retained timing population')
    return evidence


def compare_results(rows, *, workload='record_get_points'):
    """Recheck raw evidence, not a supplied success flag; retain full metadata."""
    if workload != 'record_get_points':
        return {'qualified': False, 'reasons': ['no justified oracle for this workload']}
    if len(rows) < 2:
        return {'qualified': False, 'reasons': ['at least two measured engines required']}
    validated = []
    try:
        for name, stats in rows.items():
            if not isinstance(stats, dict):
                raise ValueError(f'{name}: missing measurements')
            evidence = _validate(stats)
            shape = 'bare' if name == 'rawsqlite' else 'versioned'
            if name not in ('tofudb', 'sqlite', 'postgres', 'rawsqlite') or evidence['response_shape'] != shape:
                raise ValueError(f'{name}: result shape contradicts engine contract')
            validated.append(evidence)
        if any(exact(item) != exact(validated[0]) for item in validated[1:]):
            raise ValueError('timed populations or complete returned results differ')
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {'qualified': False, 'reasons': [str(exc)]}
    return {'qualified': True, 'reasons': []}
