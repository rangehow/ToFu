# tofu-xtask guidance

The repository's contract generators in Rust (Rust rewrite Phase 7,
`docs/RUST_REWRITE_EVIDENCE.md` "Progress"). `tofu-xtask contracts` renders
every artefact derived from `contracts/api_v1.yaml`, `contracts/events_v1.json`
and `contracts/storage_operations_v1.json` (the table in `src/main.rs`);
`make contracts` writes, `make contracts-check` fails on a stale output.

- One module per contract (`src/api_v1.rs`, `src/events.rs`,
  `src/storage_operations.rs`); shared spelling in `src/names.rs`, Python-style
  value reads in `src/json.rs`, rustfmt and atomic publication in
  `src/output.rs`. A renderer reads only its contract, never Python or the
  outputs of another renderer.
- Generated Rust passes through rustfmt in its crate (`RUSTFMT` overrides the
  binary), so `cargo fmt --check` of `tofu-contracts` agrees with `--check`.
- The storage outputs' bytes, header included, are part of the storage
  contract identity the engine shares: never change their text without a
  planned engine build.
- Porting another generator: reproduce its committed outputs byte for byte on
  the real contract and on edge cases, then delete the Python renderer and keep
  only the Python owner check.

Verify with the Cargo from `serverctl_pkg.native_engine.resolve_cargo`, under
`nice -n 19 ionice -c3` on a loaded host: `cargo test --release`,
`cargo clippy --release --all-targets -- -D warnings`, `cargo fmt --check`
here, then `make contracts-check` from the repository root.
