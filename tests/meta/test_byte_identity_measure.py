"""benchmarks/harness_perf/byte_identity.py stays runnable and complete.

The script is the byte-identity check of request-preparation changes: two
trees must write identical lines. This keeps it covering every recorded
request on every wire face, with sealed lineage on each line.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / 'benchmarks' / 'harness_perf' / 'byte_identity.py'


def test_every_recorded_request_is_sealed_on_every_wire_face():
    spec = importlib.util.spec_from_file_location('byte_identity', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorded = sum(len(steps) for _scenario, steps in module.recorded_requests())
    out = io.StringIO()
    count = module.run(out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert recorded > 0 and count == len(lines) == recorded * len(module.PROTOCOLS)
    assert {line['protocol'] for line in lines} == set(module.PROTOCOLS)
    null_lineage = module._digest(None)
    for line in lines:
        assert len(line['payload']) == 64 and line['bytes'] > 0
        assert line['lineage'] != null_lineage
