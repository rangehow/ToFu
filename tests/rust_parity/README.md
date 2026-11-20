# Shared Rust differential runner

The runner and JSONL protocol are adopted from parity checkpoint `0efbc876c3`.
This tranche does not include the separate private corpus, capture tooling, or
live-deployment fault runner. Run the fixture-backed `engine_session` gate with
a fresh Python oracle and the Rust binary from the same remote build:

```sh
python3 scripts/remote_cargo.py -C packages/tofu-core-rs \
  --after-build release/tofu-engine tests/rust_parity_engine/remote_gate.sh \
  build --locked --offline --release --bin tofu-engine
```

Unsupported answers, count mismatches, nonzero exits and byte differences fail
this gate. No output normalization applies to `engine_session`. Same-process
replays do not certify recovery after an engine process kill.
