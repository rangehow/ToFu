# Swarm snapshot-cut prerequisite — verification receipts

Implementation source: commit `8d772d22dbc77ed2c03c95b8509df9550efa9eda`, tree `31b9790f69fdec669bff35a900ccd9228e347243`, based on canonical `67d0ae4ae0`. This page records original run receipts, not deployment certification. The evidence-only commit is not the implementation tested.

## SQLite and private PostgreSQL

Original background receipt: `bg_278e9fbdf542` (worker assignment transcript). The command printed the implementation commit and tree before validation and completed with exit 0:

```sh
export PYTHONPATH=$PWD
.venv/bin/python3 scripts/gen_tofudb_ir_contract.py --check
.venv/bin/python3 scripts/gen_storage_operation_catalog.py
.venv/bin/python3 -m pytest -v -rs -o addopts='' -p no:cacheprovider \
  tests/tofudb/test_tofudb_differential.py::test_swarm_checkpoint_cut \
  tests/tofudb/test_tofudb_differential.py::test_swarm_delivery_writer_epoch \
  tests/storage/test_storage_v2_client.py \
  tests/storage_authority_sql/test_swarm_checkpoint_writer_epoch.py \
  tests/storage_authority_sql/test_swarm_lifecycle_receipt.py \
  tests/swarm/test_swarm_restart_continue.py \
  -k 'not (native and (swarm_checkpoint_cut or swarm_delivery_writer_epoch))'
```

```text
8d772d22dbc77ed2c03c95b8509df9550efa9eda
31b9790f69fdec669bff35a900ccd9228e347243
...::test_swarm_checkpoint_cut[sqlite] PASSED
...::test_swarm_checkpoint_cut[postgres] PASSED
...::test_swarm_delivery_writer_epoch[sqlite] PASSED
...::test_swarm_delivery_writer_epoch[postgres] PASSED
247 passed, 4 deselected in 29.37s
```

The PostgreSQL cases launched a private cluster and temporary SQL supervisor. The same oracle asserts actual wire pagination, same-ID edits at unchanged header and agent timestamps, append and removal, owner/run/revision mismatch, null/malformed selectors, tampered/stale tokens, unguarded legacy paging, and lost delivery acknowledgement replay. The Rust daemon oracle additionally checks unsupported managed mutations remain refused. No production activation or restart, backend/store switch, migration, provider call, or original 401 session was involved.

## Off-pod Rust unit regressions

Original background receipt `bg_b3798c0dd2aa`, Hope run `69487701`. `scripts/remote_cargo.py -C packages/tofu-db test --locked --offline --lib swarm::` started after checking the checkout was clean and logging the implementation commit/tree above. Its regular test snapshot is distinct from the immutable release archive. Cargo reported `11 passed; 0 failed; 0 ignored; 2734 filtered out` and exited 0. This covers native legacy pagination, tenant-global ownership, append/marker state, delivery/reopen, and neighboring migration unit cases against disposable test data. Two unrelated `tofu-runtime` deprecation warnings were emitted. The initial `--clean-source test` invocation (`bg_8f814d8678ff`) was rejected before submission because that switch requires `--after-build`; only the release daemon oracle uses that mode.

## Clean-source release and disposable daemon

Original background receipt `bg_f7e9726e588e`, first build/oracle pass, Hope run `69487637`:

```sh
.venv/bin/python3 scripts/remote_cargo.py -C packages/tofu-db \
  --after-build release/tofu-db scripts/tofudb_swarm_delivery_oracle.sh \
  --clean-source build --locked --offline --release --bin tofu-db
```

Selected original output:

```text
remote-cargo: Hope run_id=69487637
src.tar.zst: OK
remote-cargo: cargo exited 0 at 2026-11-19 07:01:11 CST
remote-cargo: source snapshot sha256=0bf7b98c025f0042d113e15301298e2cbc8eb127f73fbbf7c62fe011c2b86b8a
remote-cargo: clean source commit=8d772d22dbc77ed2c03c95b8509df9550efa9eda tree=31b9790f69fdec669bff35a900ccd9228e347243
remote-cargo: binary sha256=b2edfeba5ce51d64643d00a8c52476efb4e826c7710bc9e3e920e4a7e7b85e13 path=/tmp/tofu-remote-cargo/target/release/tofu-db
...::test_swarm_delivery_writer_epoch[sqlite] PASSED
...::test_swarm_delivery_writer_epoch[native] PASSED
...::test_swarm_checkpoint_cut[sqlite] PASSED
...::test_swarm_checkpoint_cut[native] PASSED
...::test_native_adapter_preserves_swarm_checkpoint_cut_selectors[continuation0] PASSED
...::test_native_adapter_preserves_swarm_checkpoint_cut_selectors[continuation1] PASSED
21 passed, 2 skipped, 94 deselected in 9.27s
remote-cargo: post-build runner exited 0
```

Hope verified the archive and built the selected executable from the clean committed tree. The oracle checked SHA-256 of `/proc/<daemon-pid>/exe` after readiness, then exercised the same executable over storage.v2 on fresh disposable storage. The two Hope skips are the PostgreSQL cases; the run above on this same commit separately passed both against private PostgreSQL. These receipts bind the source, archive and daemon executable identities; the build archive and binary bytes are not attached or deployed.

## Typed selector parity follow-up (SQL only)

Fail-before receipt `bg_fa3c0da68c02`: the new public-operation tests found one failure (disposable PostgreSQL returned `database_internal` for a NUL-containing guarded key), five passes. Pass-after receipt `bg_e815363e65aa` on clean source `c596b1abe7e1e6d10dc57fd4e4036f65648464fa`, tree `96049fd434d634defb25e69a9956ee190f6c5e31`: SQLite and private PostgreSQL each returned `database_protocol_error` for malformed key, run and cursor; the snapshot-cut, delivery and writer-epoch regressions also passed (`20 passed, 5 deselected in 20.16s`, exit 0). The storage catalog check, Ruff and patch whitespace checks passed. Guarded selectors now validate before lock acquisition; source and page reads still share the session lock. Legacy reads are unchanged. The earlier native receipts are unchanged historical evidence, not a rerun on this source.

The unselected Rust SQL candidate registers this operation but its candidate handler ignores the three guard activation fields. The contract requires rejection rather than returning a legacy page when any of those fields is present. Until it implements the cut, candidate dispatch should reject guarded requests before SQL, including partial or null selectors, while retaining bare legacy reads. No candidate activation, Rust SQL change or parity claim is included in this SQL repair.
