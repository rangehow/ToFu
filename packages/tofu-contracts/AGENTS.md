# tofu-contracts guidance

Serde types and metadata generated from `contracts/api_v1.yaml`
(`src/generated_api_v1.rs`; mapping rules in
`packages/tofu-xtask/src/api_v1.rs`) and `contracts/events_v1.json`
(`src/generated_events_v1.rs`), both by `tofu-xtask contracts`. Never edit
a `generated_*` module or hand-write a duplicate type: change the contract,
run `make contracts`. Only `src/lib.rs` and `src/wire.rs` (shared serde helpers)
are authored.

The crate is standalone (its own `[workspace]` and lock file) and has no
runtime consumer yet; Phase 1 of `docs/RUST_REWRITE_EVIDENCE.md` is its first.

Verify with the Cargo from `serverctl_pkg.native_engine.resolve_cargo`:
`cargo test`, `cargo clippy --all-targets -- -D warnings` and
`cargo fmt --check` in this directory. The tests round-trip the golden wire
fixtures under `contracts/fixtures/` and compare `OPERATIONS` with
`contracts/api_endpoint_index_v1.json`.
