# Turn projection evidence

`witness.rs` owns the bounded, durable `frameWitness` carried by an existing
Turn projection head. It derives facts from actual materialization and updates
them only for supported top-level patches. It is not a new request, authority,
producer batch, process cache or passive replay protocol.

- Production entry: `turn.rs::event_record`; admission, lifecycle/owner fencing,
  checkpoint policy, exact publication and commit remain in the shared owners.
- Semantic proofs reuse the shared normalizer in its borrowed form
  (`turn_projection_segments::stable_fields`/`is_stable_for_patch`, which
  `stabilize_for_patch` applies to a copy), the existing guidance-preservation
  helper and `turn_projection_patch` validation. The normalizer rewrites only
  `fileChanges` and `segments`; a rewrite of any other field belongs in
  `StableFields`. New normalizer dependencies must invalidate reuse here before
  a fast write.
- `VerifiedStableSegments` is transaction-local: reuse it only beside the exact
  unchanged materialized revision/actor/status. Minting may inherit this proof
  only when the applied replay patch leaves every normalizer dependency intact.
  Materialization still verifies all persisted facts before any reuse.
- Missing/future evidence and unsupported transitions use materialization;
  known malformed evidence retains integrity errors. Text-tail and lifecycle
  writers must drop or replace facts when their state changes.
- Version 4 measures the current restart-settlement encoding. Version 3
  measured it before unproduced restart resume; version 2 was published with
  two evidence labels. Materialization verifies the exact historical encoding,
  then a write mints v4 before size-proof reuse. Keep the
  golden-length/reopen tests when changing recovery serialization. Its
  content/thinking/toolRounds inputs invalidate reuse. Version 1 retains
  conservative size admission.
- The containing `execution_frame.rs::record` and its multi-step IR port remain
  `cfg(test)` experiments. Do not infer producer batching from those tests.
- Runtime contracts: `src/turn_execution_frame_runtime_tests.rs`; real producer
  capture: `tests/turn_lifecycle/test_execution_frame_producer.py` and the shared
  root `tests/fixtures/execution_frame_producer.json` fixture. Opt-in work/timing
  cases use the same owner, with explicit profiling and durability settings.
- Decision, limitations and measurements: `docs/TOFUDB_EXECUTION_FRAMES.md`.
