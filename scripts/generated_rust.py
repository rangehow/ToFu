"""Generated Rust is published in the form `cargo fmt` keeps.

Entry: format_generated_rust, for every generator that writes a ``.rs`` file
into a crate. Each crate's fmt gate formats generated files too, so a
generator's ``--check`` and ``cargo fmt --check`` agree only on rustfmt
output. The output's crate manifest owns the edition; the toolchain comes from
serverctl_pkg.native_engine.resolve_cargo, the single Rust discovery.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _crate_of(output: Path) -> Path:
    for directory in Path(output).resolve().parents:
        if (directory / 'Cargo.toml').is_file():
            return directory
    raise ValueError(f'{output} is not inside a Cargo crate')


def format_generated_rust(source: str, output: Path) -> str:
    """``source`` as rustfmt formats it inside ``output``'s crate, else an explicit failure."""
    from serverctl_pkg.native_engine import resolve_cargo
    crate = _crate_of(output)
    with open(crate / 'Cargo.toml', 'rb') as manifest:
        edition = tomllib.load(manifest)['package']['edition']
    cargo, environment = resolve_cargo(os.environ, root=ROOT)
    rustfmt = shutil.which('rustfmt', path=environment['PATH']) if cargo else None
    if rustfmt is None:
        raise RuntimeError('generated Rust needs rustfmt: no Rust toolchain found '
                           '(set CARGO_HOME/RUSTUP_HOME in the environment or .env)')
    # cwd selects the crate's toolchain override and any rustfmt.toml.
    completed = subprocess.run([rustfmt, '--edition', edition, '--emit', 'stdout'],
                               input=source, text=True, capture_output=True,
                               cwd=crate, env=environment, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f'rustfmt rejected generated Rust for {output}: {completed.stderr.strip()}')
    return completed.stdout
