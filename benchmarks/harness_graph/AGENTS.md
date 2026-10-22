# Harness graph experiment

- `src/lib.rs` owns the experimental immutable-frame protocol and scoped row
  traits. `src/native.rs` adapts the existing AuthorityDatabase; the sibling
  `../harness_sqlite_reference.rs` owns the SQLite adapter.
- `src/main.rs` is the JSONL correctness interface. `src/bin/measure.rs` owns
  measured Rust regions; `measure.py` owns fresh-process ABBA, identity and
  completeness checks. Both backends run the same graph algorithms.
- `tests/graph.rs`, `tests/test_measure.py`, and
  `../harness_graph_controls.py` are the test ladder; see README for commands.
- This is an offline prototype. Keep result directories, Cargo targets, and
  disposable databases outside tracked source. Experiment design lives in
  `docs/TOFUDB_HARNESS_DATABASE.md`; production contracts remain `docs/STORAGE.md`
  and `docs/STORAGE_TOFUDB.md`. Do not imply real Turn integration.
