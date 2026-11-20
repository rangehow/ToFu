# Frozen a28b2 offline evidence continuation

Candidate: `a28b2b7bf4cc28b981720fbf36e535ea7034fc5c`; full Git tree `552cf8f87cab3905b7f94afc70726ecac97bcfa6`; manager tree `40c9eab67085a2112b8c5f28e5d74722065dec04`.

This supplemental evidence is outside the frozen release. No candidate source, tests, gates or release artifact were edited. No production controls, store opens, rollback, binary execution, compilation, upload, GitHub operation or remote dispatch occurred in this continuation. Source inspections use exact frozen Git objects, not the newer task HEAD.

## Evidence provenance: do not collapse these lanes

Historical result recovered from same-project independent review conversation `worker-5bc43d4b3f74fb277a7e2ba4`, assistant result (assignment dacadd18-cc76-579b-a62f-ba87855cf051). These are preserved transcript-level results, NOT new execution in this continuation:

- Prior Python/SQLite round: **225 passed / 16 skipped**. Sixteen consisted of two native witness discriminators, five Rust-manager cases, nine optional native-service cases.
- Independent dacadd18 native successor run: **2 passed / 0 skipped / 0 failures**, daemon and embedded parametrizations of `tests/server/test_native_authority_witness_engine.py:30`. Reported checks: compatible reopen, scope mismatch refusal, changed CONTROL refusal, byte-invariant refusal snapshots. Not full Rust lifecycle or formal rollback.
- Engine source fingerprint: `0b284ddc2682441ba665c4d97373d1577ba433c18986d010d47acac52bcc6651`.
- Engine SHA-256: `c2c3af67022320cafeadc32cfddeb3905994fc677b12285e4099be49a4c97e21`.
- Embedded library SHA-256: `a45764f59f2c5c49d0ed9d062acdf14b37c1a4916ea0656c85689b1b2bd3a35b`.
- Independent supported `--prepare-only --candidate` admission receipt SHA-256: `3c08126b59891a02cec011ee2aa6261c21530a357e4a6f9631e8c902d40fdc25`; witness version 2, mimalloc. It was the independent reviewer's new admission, not a recovered author receipt.
- The old ephemeral `REVIEW_RESULT.md` path supplied by that review was unavailable in this continuation. Original raw JUnit/build.json bytes were not recovered here; this document must not be described as their byte-for-byte archive or a newly verified binary admission. Stable transcript identity above preserves provenance after ephemeral cleanup.
- Exact-source Rust-manager executable remains absent/unverified. Older digest `a50da98727b58f8626073c64774f7a9e7b14ebfb83907b7eecb2b741abeac59e` was rejected by prior review without execution; do not reuse it.

## Check 1: narrow manager proof plan

`plan.py` is intentionally a **non-executing admission planner**, not a release runner or gate. It reads Git objects, checks the frozen full/manager tree identities, hashes the frozen fixtures and Cargo.lock, and emits exactly these Rust node IDs:

1. `tests/server/rust_manager_parity/test_storage_rollback_parity.py::test_a_new_engine_whose_first_start_fails_after_stop_is_rolled_back_and_service_restored[rust]`
2. `tests/server/rust_manager_parity/test_storage_rollback_parity.py::test_without_a_known_good_engine_the_failed_host_is_replaced_and_bounded[rust]`
3. `tests/server/rust_manager_parity/test_storage_rollback_parity.py::test_a_new_engine_that_cannot_start_on_a_disposable_authority_refuses_before_stop[rust]`
4. `tests/server/rust_manager_parity/test_storage_rollback_parity.py::test_unknown_existing_authority_leaves_both_serving_processes_unchanged[rust]`
5. `tests/server/rust_manager_parity/test_sql_storage_parity.py::test_the_sql_authority_is_hosted_exported_and_outlives_the_api_worker[rust]`

Exact source proof: rollback tests at frozen lines 96,126,149,166; SQL test at line 56. Frozen conftest uses explicit `TOFU_MANAGER_BINARY` or otherwise an unsafe-for-this-task default target/debug path, creates fake projects via tmp_path, loopback managers, scrubbed child environment, and fixture-owned teardown. Therefore a future admitted run must set an explicit verified binary and isolated HOME/TMPDIR/cache/basetemp and use an exact archived source tree, never the current checkout or live process. Preserve root conftest, fake-project helpers and all source unchanged. The four rollback tests use script engines/resident doubles; the SQL test uses the real disposable SQL host with fake API worker. Passing these would prove their bounded fixture behavior only, not native production rollback.

Executable prerequisite: independently reviewed build-time provenance tying executable SHA-256 to the full frozen source/archive, manager source tree, Cargo.lock/dependencies and actual build command. Manager `/health` sourceFingerprint hashes the supplied Python closure, not compiled Rust source (`packages/tofu-manager/src/fingerprint.rs:15–65`); its binarySha256 alone also does not prove source. No such executable receipt is present. Do not run any manager to discover whether it matches.

Future authorized runner must require exactly five passes and zero skips/failures/errors plus process cleanup, and retain sanitized exact-source/executable receipt and JUnit. This planner deliberately has no execute switch. Existing broad remote_authority_gate.sh is not invoked or edited. No remote build command is executed.

Verification in this continuation: `.venv/bin/python3 plan.py --repo .` exited 0, returned the expected trees, exactly five Rust IDs and `execution: false` with `BLOCKED_NO_REVIEWED_EXACT_SOURCE_MANAGER_EXECUTABLE`. No lifecycle case was run. Frozen suite SHA-256 values:

- rollback module: `e0c25727f015eb362ca396c551c22f84b3d331287fa3775376972f8718f050cb`
- SQL module: `96f07fd277092b01f399530106be8e7dddf68809d9de3f3f320ee95b66d94196`
- parity conftest: `b9e42a960a43f15cbe8d588eff3ebf6711644fda1c9a967783c79b05b73a039a`
- fake project: `39d34277aa8a4a8dcc2bce1e97db3fc84b85291eef177637dc2f27b2f9220777`
- Cargo.lock: `534a60aedc4acd904a90c1209f801582a922bf3b21dd205387f5ee9a28115f79`

## Check 2: one bounded schema71 alternative, source proof versus option

Independent read-only frozen-source assessment examined only a **prior schema71 executable bound to the unchanged storage contract**, preserving authentication and the same selected authority. This is an eligibility option, not an available or release-ready path. Schema70 fallback remains forbidden and incompatible; no store is downgraded or opened.

Exact catalog (`contracts/storage_operations_v1.json:2–8`, generated Rust agrees): schema71, registry60, 391 operations; semantic operations digest `4449e88482bf9ced4e02c19fdb061b6e7263864489e4193b770980023116f5ca`. Parent independently cross-checked the frozen catalog. Independent source analyst computed contract digest from 34 frozen blobs using native_engine framing: `03d2ab9a9419cff82af19ea5e5f8bc3583fd6071e9838e47df0c6717ade6e994`. This is a contract digest, not engine source/executable SHA.

Source-supported requirements:

- `serverctl_pkg/native_engine.py:883–902,964–1007`: keep requires exact reported source, configured pin/library where applicable, binary digest and deployment receipt or verified certification binding to current storage contract. Keep may permit an uncertified existing engine; keep is NOT rollback certification.
- `serverctl_pkg/native_storage_service.py:244–269`: restore requires preserved unaltered known-good bytes and current-contract equality. Automatic rollback concerns failed first start that never served, not arbitrary downgrade after serving. `store_features.py:105–126` independently rejects unsupported persisted features.
- `packages/tofu-db/src/daemon.rs:528–539`: secret 32–256 ASCII bytes; SHA-256 domain-separated transport token. `server.rs:392–430`: invalid auth configuration rejected; constant-time witness comparison; PermissionDenied before schema negotiation; only current schema offered. `protocol.rs:58–77` refuses no shared schema.
- `semantic.rs:226–238`: request must match negotiated/current schema and authenticated owner/tenant. `lib/storage/v2_client.py:511–516` rejects changed recovery scope. `authority_continuity.rs:73–105` binds owner/tenant, directory/LOCK/CONTROL identities, UUID and nonregressing generation. Connection contract/validator require private regular non-symlink identity records (`contracts/native_storage_connection_v2.json:7–32`; `lib/storage/connection_file.py:42–118`).
- Application login additionally requires active account, stored password verification and successful login record (`lib/billing/users.py:101–128,230–253`; native `tenant_user.rs:526–541`). Matching schema or RPC hello alone does not prove retained login operations, projections or credentials.
- Formal gate (`scripts/engine_certification/gate.py:115–170,371–417`) requires ordered shutdown/checkpoint/WAL-zero seed; clean/crash-build/killed-checkpoint scenarios; acknowledged commit/write/digest parity and offline verify ok/zero missing blocks. Authenticated probes exist (`probes.py:43–51,73–114`) but conversation/cost workload does not establish existing password/login continuity, negative authentication behavior or original live-authority witness continuity. Automatic prior selection (`gate.py:420–450`) is not same-schema/current-contract qualification.

Missing: identified prior authenticated schema71 source/executable/library hashes, valid exact contract/certification binding, intact known-good copy, feature compatibility proof and passing bounded rollback/auth continuity evidence. Original authority/key material was not inspected. No admissible execution prerequisite exists; stop at source assessment. Formal rollback and release readiness remain UNVERIFIED.

Acceptance: (1) proof plan met; executable/lifecycle execution intentionally unmet. (2) offline source assessment met; executable alternative and actual rollback unverified. No gate edits, release changes, sends/uploads, deployment/restart or privileged tool invocation.
