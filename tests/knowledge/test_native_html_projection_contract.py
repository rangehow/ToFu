"""HTML ABI and parser profile derive from their canonical owners."""
import json
import subprocess
import sys
from pathlib import Path
import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_native_html_projection_contract_is_generated():
    subprocess.run([sys.executable, str(ROOT / 'scripts/gen_native_html_projection_abi.py'), '--check'], check=True)
    spec = json.loads((ROOT / 'contracts/native_html_projection_abi_v1.json').read_text())
    assert len(set(spec['status'].values())) == len(spec['status'])
    from scripts.gen_native_html_projection import outputs, profiled_tokenizer
    if not profiled_tokenizer():
        pytest.skip('profile drift on other tokenizers: tests/meta/test_native_html_projection_generator.py')
    for path, content in outputs().items():
        assert path.read_text() == content
