# tofu-front-push guidance

Phase 1e of `docs/RUST_REWRITE_EVIDENCE.md`: a standalone `WS /api/push`
library. No parent executable host or manifest is tracked under
`packages/tofu-front-rs`; historical scratch-host integration is not current
routing or runtime qualification. The tracked Python handler remains
`routes/push.py::push_ws`. Module map in `src/lib.rs`; the Python peer of the bridge is
`lib/agent_core/push_front_door.py` and the shared handshake policy is
`routes/push.py::_decide_push_handshake`.

- Python's `PushHub`/`PushClient` (`lib/agent_core/push.py`) and
  `routes/push.py::push_ws` are the behavioural oracle until Python's socket
  path is deleted: change both sides together and keep
  `tests/blackbox/test_blackbox_push*.py` green against both servers.
- Frames Rust builds must be Python's exact bytes (`python_json.rs`); frames
  Python publishes arrive encoded and are never re-encoded.
- Constants are named after their source (`socket.rs`); add no timers,
  retries or caps of our own. The crate spawns no runtime or pool.
- Standalone manifest and lock (own `[workspace]`). If a host adds an enclosing
  workspace, that workspace must `exclude = ["push"]`.

Rust build/check/test/clippy follow the off-pod workflow in `docs/RUST_BUILDS.md`,
subject to the current task's authorization; never run them locally. For runtime
qualification, an integrating host needs an approved executable path, SHA-256,
exact source identity and applicable paired execution receipt. URL comparisons
alone do not establish binary provenance; the Python bridge fixture does not
qualify Rust.
