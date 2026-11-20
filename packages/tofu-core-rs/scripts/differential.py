"""Python-vs-Rust differential gate for the provider wire of ``tofu-core-rs``.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/differential.py``

1. Re-runs the Python oracle and requires the committed fixtures to be
   current (``gen_wire_golden.py``, ``gen_stream_golden.py``,
   ``gen_catalog_golden.py``, ``gen_tool_schema_golden.py`` and
   ``gen_tool_search_golden.py``, each with ``--check``).
2. Runs ``cargo test --locked``, whose ``wire_golden``, ``catalog_golden``,
   ``tool_schema_golden``, ``tool_search_golden`` and ``stream_golden``
   tests require the Rust port to reproduce every fixture byte for byte,
   then ``cargo clippy --all-targets -- -D warnings`` and ``cargo fmt --check``.

Exits non-zero on the first failing step. Compilation and Rust tests run
through ``scripts/remote_cargo.py`` off the production pod; only fmt is local.
"""
import os
import subprocess
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parents[1]
ROOT = CRATE.parents[1]


def main():
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    cargo = os.environ.get('CARGO', 'cargo')
    steps = [
        ([sys.executable, str(CRATE / 'scripts' / 'gen_wire_golden.py'), '--check'], ROOT),
        ([sys.executable, str(CRATE / 'scripts' / 'gen_stream_golden.py'), '--check'], ROOT),
        ([sys.executable, str(CRATE / 'scripts' / 'gen_catalog_golden.py'), '--check'], ROOT),
        ([sys.executable, str(CRATE / 'scripts' / 'gen_tool_schema_golden.py'), '--check'], ROOT),
        ([sys.executable, str(CRATE / 'scripts' / 'gen_tool_search_golden.py'), '--check'], ROOT),
        ([sys.executable, str(CRATE / 'scripts' / 'gen_recorded_golden.py'), '--check'], ROOT),
        ([sys.executable, str(ROOT / 'scripts' / 'remote_cargo.py'), '-C', str(CRATE),
          'test', '--locked', '--offline'], ROOT),
        ([sys.executable, str(ROOT / 'scripts' / 'remote_cargo.py'), '-C', str(CRATE),
          'clippy', '--locked', '--offline', '--all-targets', '--', '-D', 'warnings'], ROOT),
        ([cargo, 'fmt', '--check'], CRATE),
    ]
    for command, cwd in steps:
        print('$', ' '.join(command), flush=True)
        if subprocess.run(command, cwd=cwd, env=env).returncode:
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
