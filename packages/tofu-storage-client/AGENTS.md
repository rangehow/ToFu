# Rust storage.v2 client

The one application-side storage client for the Rust phases: the counterpart
of `lib/storage/v2_client.py` and `tofu_db/wire.py`. Plan, coverage and
decisions: `docs/RUST_REWRITE_EVIDENCE.md`, "Phase 3a: Rust storage.v2 client".

- Python is the oracle. A behaviour change lands in the Python client first,
  then here, and `tests/storage/test_storage_v2_rust_client_differential.py`
  must stay identical. Extend its corpus for every new branch.
- Framing, message types and the Hello token come from `tofu_db::protocol` and
  `tofu_db::daemon`; never copy engine types. Every constant comes from
  `contracts/*.json` through `src/contract.rs`; never restate one by hand.
- Write-time credential redaction stays in `command()` (`WriteRedactor`);
  `src/redaction.rs` mirrors `lib/tool_results/credential_redaction.py` and
  `src/model_routing_secrets.rs` mirrors `lib/model_routing/known_secrets.py`
  (read-only). `command()` refuses payloads with no redactor installed;
  production installs `install_credential_redaction`.
- No host-sized pools, caps or queues; the pool bounds idle retention only.
- Standalone manifest and lock. Build with a local `CARGO_TARGET_DIR`
  (`$TMPDIR/target`) and verify `cargo test --locked`,
  `cargo clippy --all-targets -- -D warnings` and `cargo fmt --check`.
