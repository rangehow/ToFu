# Domain-root experiment

This standalone Rust crate owns an experimental immutable domain-root forest.
It is not a production authority, semantic API, or persistent-format migration.
`src/lib.rs` owns the frontier/dependency/receipt protocol; `tree.rs` and
`directory.rs` own shared immutable construction. `store.rs` alone chooses one
native Engine or one SQLite WAL/FULL file. Keep both arms on that same algorithm.

`tests/protocol.rs` and `tests/support/` verify live concurrent reads, actual WAL
sync overlap, read-only dependencies, fault images and real SIGKILL recovery.
`measure.py` independently replays the measured serialization history and refuses
missing work, stale reads, weakened durability or changed source/library identity.
Its adversarial tests live in `tests/test_measure.py`.

Run focused Rust tests before release/pilot, then the Python oracle tests.
Coordinate formal measurements with the root agent; do not overlap them with
compilation or other timed work. Preserve failures and all hotspot retry costs.
Contract, interpretation and remaining limits: `docs/TOFUDB_DOMAIN_ROOTS.md`.
