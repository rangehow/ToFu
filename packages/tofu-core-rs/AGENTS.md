# tofu-core-rs: core-loop prototype and execution engine

Two owners share this standalone crate:

- The evidence prototype (`canonical.rs`, `wire.rs`, `provider.rs`,
  `round.rs`, `store.rs`, `tools.rs`, binary `tofu-core-rs`): an agent round
  benchmarked against Python (`docs/RUST_REWRITE_EVIDENCE.md`, "Core loop
  prototype"). The request/stream wire (P4-wire) grows here too.
- The Phase 4 execution engine (`src/engine/`, binary `tofu-engine`): the
  Rust owner of a Turn's loop, settlement, tool effects, fallback,
  checkpoints and compaction admission. Not routed: nothing in the product
  spawns it until the strangler route opens (`docs/RUST_REWRITE_EVIDENCE.md`,
  "Phase 4"). `src/engine/mod.rs` maps its modules to the Python owners.

Rules:

- `canonical.rs`/`reasoning.rs`/`route.rs`/`schema.rs`/`tool_wire.rs`/
  `call_ids.rs`/`wire.rs` port `lib/llm/body` and `prepare_request` (reasoning
  compilation, cache markers, tool-name budget, gateway traits, tool-schema
  preflight with MFJS, Tool Search projection, tool-call IDs); Python is the
  oracle. An input the port does not cover returns `Error::Unsupported`,
  never guessed bytes; a Python exception is reproduced as `Error::Raised`.
  The maintained seed-catalog text/API-key tranche must stay ported
  (`tests/catalog_golden.rs`); this does not certify images, reasoning
  history, OAuth or all routes/features of those models. Cutover gaps remain
  owned in `docs/RUST_REWRITE_EVIDENCE.md`.
  Change Python first, then regenerate every fixture (`scripts/gen_*_golden.py`:
  wire, catalog, tool_schema, tool_search, stream), run
  `scripts/differential.py` and
  `benchmarks/harness_perf/byte_identity.py OUT --rust BIN` (all 75 lines).
  `tests/llm/test_core_rs_wire_oracle.py` fails while a fixture is stale.
- `store.rs` uses `tofu_db::protocol` framing and
  `tofu_db::daemon::derive_auth_token`; do not copy engine types.
- The engine core is sans-IO: side effects only through `engine/ports.rs`;
  storage through the storage.v2 client (`packages/tofu-storage-client`),
  events only through the generated `tofu_contracts` events_v1 types. Policy
  modules are pure; each easy-to-lose row they keep has a test naming the
  row and the Python test it mirrors. The binary exposes replay only; no resident scheduler is included.
- Engine parity uses the adopted shared harness (`tests/rust_parity/`,
  provenance in its README): run `scripts/remote_cargo.py -C
  packages/tofu-core-rs --after-build release/tofu-engine
  tests/rust_parity_engine/remote_gate.sh build --release --bin tofu-engine
  --locked --offline`. `engine_session_golden` replays the Python golden;
  no behavioural differences are suppressed. After an intended Python
  change, regenerate through `python3 -m
  tests.rust_parity_engine.generate_golden --output
  packages/tofu-core-rs/tests/fixtures/engine_session_golden.json`.
  Same-process replay is not crash evidence: `process_faults` requires a
  real authority receipt and fresh-process resume and stays red without them.
- Standalone manifest and lock like `packages/tofu-db`. Verify through
  `python3 scripts/remote_cargo.py -C packages/tofu-core-rs` with `test
  --offline` and `clippy --offline --all-targets -- -D warnings`
  (`docs/RUST_BUILDS.md`), and `cargo fmt --check` locally.
