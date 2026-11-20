"""The native-catalog microbenchmark measures complete catalogs without effects."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_catalog_measure_receipts_cover_complete_fixture():
    path = Path(__file__).resolve().parents[2] / 'benchmarks/native_catalog_processing.py'
    spec = importlib.util.spec_from_file_location('catalog_measure', path)
    measure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(measure)
    result = measure.measure(samples=2)
    assert result['tools'] == result['serializations_per_catalog'] == 12
    assert result['samples'] == len(result['cpu_ns']) == len(result['wall_ns']) == 2
    assert all(len(result[key]) == 64 for key in (
        'owner_sha256', 'fixture_sha256', 'output_sha256'))
    assert result['median_cpu_ns'] > 0 and result['median_wall_ns'] > 0
    assert measure.digest(measure.fixture()) == result['fixture_sha256']
