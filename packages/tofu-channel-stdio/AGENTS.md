# Rust channel stdio binding

Phase 5 of `docs/RUST_REWRITE_EVIDENCE.md`: the channel protocol's stdio
binding in Rust. The oracle is `lib/channels/protocol/stdio.py` (`_Peer`,
`StdioLink`, `serve`); method directions and error codes come from
`contracts/channel_protocol_v1.schema.json` (`x-methods`, `x-errors`),
embedded at build time. Change Python first, then port and rerun the
differential tests.

- `peer.rs` keeps Python's wire bytes (`pyjson.rs`: `json.dumps(ensure_ascii=False)`
  separators and `float.__repr__`) and its answers: a response with a
  non-integer `id` matches nothing, and a malformed error response fails only
  its own request (Python `_response_error`); the binding closes only at EOF.
- `link.rs` (core side) and `serve.rs` (adapter side) use std threads only:
  reader, dispatcher, stderr drain and exit waiter per child, never a pool.
- `src/bin/tofu-channel-stdio-link.rs` serves `lib/channels/protocol/rust_stdio.py`
  (`RustStdioLink`, binary from `TOFU_CHANNEL_STDIO_LINK_BIN`); its `$/close`,
  `$/status` and `$/exit` controls are defined in that module header.
- Known differences from Python: `NaN`/`Infinity` tokens and nesting deeper
  than 128 are not JSON here; an error `code` beyond i64 fails its request
  as a malformed error response (Python keeps the big integer); `repr` escapes only
  controls and whitespace beyond Latin-1.
- Standalone manifest and lock; build with a `CARGO_TARGET_DIR` on local disk
  (`$TMPDIR`). Verify from this directory: `cargo test`,
  `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`; then
  `PYTHONPATH=<repo> <repo>/.venv/bin/python3 -m pytest -q tests/channels/conformance`
  (includes `test_rust_stdio_link.py`, which builds the binary with Cargo
  when `TOFU_CHANNEL_STDIO_LINK_BIN` is unset).
