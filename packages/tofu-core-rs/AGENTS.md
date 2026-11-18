# Rust core-loop prototype

Evidence only: an agent round in Rust (stored history → canonical body →
Anthropic/OpenAI/Responses wire → seal → stub provider → `read_files` /
`run_command` → TofuDB events and raw archive). Nothing in the product links,
spawns or imports it. Results and limits: `docs/RUST_REWRITE_EVIDENCE.md`,
"Core loop prototype".

- `canonical.rs`/`wire.rs` port a subset of `lib/llm/body` and
  `prepare_request`; Python is the oracle. An input the port does not cover
  returns `Unsupported`, never guessed bytes. Change Python first, then
  regenerate `tests/fixtures/wire_golden.json` with
  `scripts/gen_wire_golden.py` and rerun
  `benchmarks/harness_perf/byte_identity.py OUT --rust BIN` (all 75 lines).
- `store.rs` uses `tofu_db::protocol` framing and
  `tofu_db::daemon::derive_auth_token`; do not copy engine types.
- Standalone manifest and lock like `packages/tofu-db`; build with the Cargo
  that `serverctl_pkg.native_engine.resolve_cargo` resolves and a
  `CARGO_TARGET_DIR` on local disk (`$TMPDIR`). Verify `cargo test`,
  `cargo clippy --all-targets -- -D warnings` and `cargo fmt --check`.
