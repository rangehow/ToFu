"""Generated Rust is published exactly as the crate's fmt gate keeps it.

`cargo fmt` formats generated files too, so a generator's ``--check`` and
``cargo fmt --check`` agree only when the generator emits rustfmt output.
"""
import os
import shutil
import subprocess
import sys

import pytest

from scripts import generated_rust
from serverctl_pkg.native_engine import resolve_cargo

pytestmark = pytest.mark.unit

ROOT = generated_rust.ROOT
CRATE_OUTPUT = ROOT / 'packages/tofu-db/src/generated_tofudb_ir.rs'
UNFORMATTED = ('pub const KINDS: &[&str] = &["adjust_credit", "adjust_debit", "bonus", "debit", '
               '"native_admission", "redeem", "refund"];\n')
RUST_GENERATORS = ('gen_tofudb_ir_contract.py', 'gen_storage_v2_contract.py',
                   'gen_conversation_sync_contract.py', 'gen_model_context_projection.py',
                   'gen_project_dispatch_storage.py', 'gen_agent_machine_tokens.py')


def _rustfmt_available():
    cargo, environment = resolve_cargo(os.environ, root=ROOT)
    return cargo is not None and shutil.which('rustfmt', path=environment['PATH']) is not None


needs_rustfmt = pytest.mark.skipif(not _rustfmt_available(), reason='no Rust toolchain with rustfmt')


@needs_rustfmt
def test_generated_rust_is_wrapped_the_way_cargo_fmt_keeps_it():
    formatted = generated_rust.format_generated_rust(UNFORMATTED, CRATE_OUTPUT)
    assert formatted.startswith('pub const KINDS: &[&str] = &[\n    "adjust_credit",\n')
    assert generated_rust.format_generated_rust(formatted, CRATE_OUTPUT) == formatted


@needs_rustfmt
def test_every_generated_rust_output_is_rustfmt_stable():
    from scripts.tofudb_contract_codegen.generation import build_outputs
    from scripts.tofudb_contract_codegen.inputs import ContractInputs
    from scripts import gen_tofudb_ir_contract as generator
    outputs = build_outputs(ContractInputs(ROOT, generator.SOURCE, generator.SEARCH_SOURCE))
    rust = {path: text for path, text in outputs.items() if path.endswith('.rs')}
    assert rust
    for path, text in rust.items():
        assert generated_rust.format_generated_rust(text, ROOT / path) == text


@needs_rustfmt
@pytest.mark.parametrize('script', RUST_GENERATORS)
def test_rust_generator_check_agrees_with_the_committed_formatted_output(script):
    completed = subprocess.run([sys.executable, f'scripts/{script}', '--check'], cwd=ROOT,
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_output_outside_a_crate_is_refused(tmp_path):
    with pytest.raises(ValueError, match='not inside a Cargo crate'):
        generated_rust.format_generated_rust(UNFORMATTED, tmp_path / 'generated.rs')


def test_missing_toolchain_is_an_explicit_failure_not_unformatted_output(monkeypatch):
    import serverctl_pkg.native_engine as native_engine
    monkeypatch.setattr(native_engine, 'resolve_cargo', lambda environment, root: (None, {'PATH': ''}))
    with pytest.raises(RuntimeError, match='needs rustfmt'):
        generated_rust.format_generated_rust(UNFORMATTED, CRATE_OUTPUT)
