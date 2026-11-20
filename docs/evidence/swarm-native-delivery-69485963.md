# Native Swarm delivery fence — original verification receipts

Evidence for implementation commit `1f0661b5e6b398c923baf46a8cf6ce85aaa33fe7`, not a deployment certification or proof of managed Continue parity. This file transcribes selected lines from the original tool completions; no build or tests were rerun to produce it. The later evidence-only commit is not the build source.

## Off-pod build and disposable daemon oracle

Original worker transcript receipt: `functions.run_command` background handle `bg_6e21148cb213`, final `functions.poll` and matching automatic `background-command` completion. Search for the handle or `Hope run_id=69485963` in the assignment worker transcript. Prior discovery/implementation swarm run: `conversation-attempt:11c2e5c7-1190-4b26-8c97-13b838f7ad9a` (not the build job).

Command invoked after committing the implementation, from the clean worker checkout:

```sh
.venv/bin/python3 scripts/remote_cargo.py -C packages/tofu-db \
  --after-build release/tofu-db scripts/tofudb_swarm_delivery_oracle.sh \
  --clean-source build --locked --offline --release --bin tofu-db
```

Selected original completion lines (2026-11-19 CST):

```text
remote-cargo: Hope run_id=69485963
src.tar.zst: OK
remote-cargo: cargo build --locked --offline --release --bin tofu-db
    Finished `release` profile [optimized] target(s) in 4m 42s
remote-cargo: cargo exited 0 at 2026-11-19 06:36:48 CST
remote-cargo: source snapshot sha256=3aa293a105180d8dab6bb6cda2f85ea6a0cc5fe3074b9e897f488809c8c915ea
remote-cargo: clean source commit=1f0661b5e6b398c923baf46a8cf6ce85aaa33fe7 tree=c26d8b10192d68cd92ea7a36a1bb27c92a69bb56
remote-cargo: binary sha256=1e38c0280300635515c5b2d4a6f5b4dd975b2dd1fde54358c9c12a5ecddb647d path=/tmp/tofu-remote-cargo/target/release/tofu-db
.s.................                                                      [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/support/postgres_cluster.py:85: Install PostgreSQL test binaries to exercise row locks
18 passed, 1 skipped, 94 deselected in 4.01s
remote-cargo: post-build runner exited 0
[exit code: 0]
```

`--clean-source` archived the immutable Git tree, and the node verified that archive before building. The shell oracle required the build's expected SHA-256 and selected its executable with `TOFU_TEST_TOFUDB_BIN` and `TOFUDB_DIFFERENTIAL_BIN`. In the passing native test, `_running_tofudb_owner` checked the executable by hashing `/proc/<daemon-pid>/exe` after daemon readiness, not by rehashing the launch path; the test then used the daemon protocol against a new disposable authority. The archive and executable bytes are not attached; the receipts bind their identities. Private PostgreSQL did not run on the Hope node.

## Local SQL and neighboring regressions

Original worker transcript receipt: `functions.run_command` background handle `bg_36031fa8720c`, final `functions.poll` and matching automatic `background-command` completion. Search for that handle or `245 passed, 8 deselected`.

```sh
set -e
export PYTHONPATH=$PWD
.venv/bin/python3 scripts/gen_tofudb_ir_contract.py --check
.venv/bin/python3 scripts/gen_storage_operation_catalog.py
.venv/bin/python3 -m pytest -q -rs -o addopts='' -p no:cacheprovider \
  tests/tofudb/test_tofudb_differential.py::test_swarm_delivery_writer_epoch \
  tests/storage/test_storage_v2_client.py \
  tests/storage_authority_sql/test_swarm_lifecycle_receipt.py \
  tests/swarm/test_swarm_restart_continue.py \
  tests/swarm/test_continue_run_mailbox.py \
  tests/swarm/test_swarm_continuation_identity.py \
  tests/swarm/test_swarm_continuation_authority.py \
  -k 'not native or (native and (adapter or storage_requests))'
```

```text
245 passed, 8 deselected in 25.36s
[exit code: 0]
```

The local receipt is aggregate (not a verbose per-case log). The selected delivery test has `sqlite`, `postgres`, and `native` parameters. The local filter excludes `native` for that test, not `sqlite` or `postgres`; the aggregate reports no skips. The PostgreSQL parameter starts `owned_postgres_cluster()` and passes only its private Unix-socket DSN to the temporary SQL supervisor. Catalog checks ran before pytest under `set -e`. Workspace-wide Rust formatting still has unrelated baseline drift.

## Scope and remaining gaps

The single operation change is epoch-zero fencing for legacy native result delivery on the existing contract route. Wrong run, mismatched/malformed epoch, current epoch, and original receipt replay without a second write after a later checkpoint are covered by the shared test. The managed-cut selector probe verifies refusal, not immutable paging. Native managed launch, roster/update, accepted-Attempt rebind and admission, failed-activation succession, managed checkpoint mutations, and exact teardown/lifecycle reads remain unsupported. Resident TTL was not changed. No production activation, migration, restart, live database, provider, or original 401 session was used.
