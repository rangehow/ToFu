"""The Rust core prototype's provider-wire oracle is current.

``packages/tofu-core-rs`` must reproduce Python's request bytes and stream
parse results exactly. Its cargo tests compare the Rust port against fixtures
that the Python oracle generates (``scripts/gen_wire_golden.py`` for
``build_body`` + ``prepare_request`` on every route Tofu uses,
``scripts/gen_catalog_golden.py`` for every registered catalog model under
every reasoning setting, ``scripts/gen_tool_schema_golden.py`` for the
provider tool-schema preflight (Kimi MFJS), ``scripts/gen_tool_search_golden.py``
for the Tool Search catalog projection, ``scripts/gen_stream_golden.py`` for
``SSEAccumulator``). This test is the
Python half of that differential: a change to the oracle that alters any
recorded byte fails here until the fixture is regenerated, and the cargo test
then shows which Rust stage must follow. The full gate is
``packages/tofu-core-rs/scripts/differential.py``.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'packages' / 'tofu-core-rs' / 'scripts'


@pytest.mark.parametrize('script', ['gen_wire_golden.py', 'gen_stream_golden.py', 'gen_catalog_golden.py',
                                    'gen_tool_schema_golden.py', 'gen_tool_search_golden.py', 'gen_recorded_golden.py'])
def test_rust_core_fixture_matches_the_python_oracle(script):
    if not (ROOT / 'bundles' / 'yourprovider' / 'gateway_profiles').is_dir():
        pytest.skip('the route corpus pins the tracked FRIDAY/CatPaw gateway profiles')
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    result = subprocess.run([sys.executable, str(SCRIPTS / script), '--check'], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, (
        f'{script} --check failed: the Python oracle changed the recorded wire. Regenerate with '
        f'`PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/{script}`, then make the Rust '
        f'port follow (cargo test in packages/tofu-core-rs).\n{result.stderr[-4000:]}')
