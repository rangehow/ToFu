# Native workspace workflow measurements

This document records a **component** comparison of the standalone native
prototype and Git on identical synthetic workspaces. It does not select a
production default or replace the existing lane authority.

## Reproduction

Build the release measurement worker and run:

```sh
CARGO_TARGET_DIR=/tmp/tofu-native-workspace-target \
  cargo build --release --locked --manifest-path packages/tofu-workspace/Cargo.toml \
  --bin workspace_measure
python3 benchmarks/native_workspace_compare.py \
  --repository . --binary /tmp/tofu-native-workspace-target/release/workspace_measure \
  --files 100 --samples 3 --output /tmp/native-workspace-100.json
python3 benchmarks/native_workspace_compare.py \
  --repository . --binary /tmp/tofu-native-workspace-target/release/workspace_measure \
  --files 1000 --samples 3 --output /tmp/native-workspace-1000.json
python3 benchmarks/existing_lane_workflow.py \
  --repository . --files 100 --samples 3 --output /tmp/existing-lane-100.json
```

Each native/Git comparison uses fresh alternating workers, identical file
bytes, and a sorted full-content digest after every operation. A failed or
unequal outcome aborts the report. Native declared-path capture is compared
with Git's path-scoped add/commit; full capture, WIP capture and checkout are
separate operations. The existing-lane collector measures the production
`lib.project_mod.lanes` owner independently. Child-process CPU is reported
separately from collector CPU in the final harness; the raw reports below
also retain their original scope metadata.

## Observed component results

Three paired samples per engine and file count on the local benchmark host.
Times are wall-clock medians; ratios are Git median divided by native median.

| Workload | Native median | Git median | Git/native |
|---|---:|---:|---:|
| 100 files, full capture | 2,638 ms | 308 ms | 0.117× |
| 100 files, declared-path commit | 81.8 ms | 182.3 ms | 2.23× |
| 100 files, WIP save | 118.1 ms | 926.5 ms | 7.85× |
| 100 files, checkout | 46.6 ms | 208.2 ms | 4.47× |
| 1,000 files, full capture | 29,352 ms | 822.8 ms | 0.028× |
| 1,000 files, declared-path commit | 90.9 ms | 271.7 ms | 2.99× |
| 1,000 files, WIP save | 1,911.8 ms | 1,215.5 ms | 0.636× |
| 1,000 files, checkout | 139.6 ms | 417.1 ms | 2.99× |

Existing Git lane workflow, 100 files, three samples:

- First-lane creation: **802.2 ms** median.
- Second-lane creation: **747.5 ms** median.
- WIP save: **1,024.9 ms** median.
- Restore: **999.5 ms** median.

Raw observations are retained in external `/tmp` report paths; those files
and the local benchmark scripts are ignored by the repository's `/benchmarks/*`
rule. The small 32-file smoke run is not a performance baseline. The checked-in
self-check at `tests/meta/test_native_workspace_benchmark.py` covers canonical
digest ordering, loud worker failure, and refusal to publish a malformed report.

## Interpretation and rejected shortcut

The initial prototype favored declared-path capture and changed-path
checkout, but full-tree capture was unacceptable. The packed-object
optimization changes that boundary: new objects are sealed in one immutable
pack per capture batch rather than individually synced loose files. The
format keeps content-addressed entries, verifies object digests on reads,
and retains legacy loose-object reads.

Five direct release-worker samples at 1,000 files gave **147.31 ms median /
185.56 ms worst** for full capture, down from **29,352 ms median** in the
initial three-sample benchmark (about **199× lower median latency**). The
same samples gave 77.66 ms median for declared-path commit, 129.48 ms for WIP
save, and 86.22 ms for checkout. These are fresh native-worker measurements,
not a new Git median; the earlier paired Git baseline remains 822.8 ms for
full capture. A syscall profile after packing reduced approximately 2,025
`fsync` calls to 33 and about 205,000 syscalls to 135,000. Remaining metadata
and content reads are still required for reconciliation.

The optimization does not prove power-loss recovery, eliminate full-tree
scans, or justify production cutover by itself. It preserves pack →
revision/WIP → state publication order and tests packed corruption,
reopen, mixed loose/packed reads, and WIP/checkout recovery.

During harness construction, an unchanged-WIP restore left its recovery
pointer behind and blocked a later checkout. The engine now releases that
pointer after a successful no-op restore; `unchanged_wip_restore_releases_recovery_pointer`
protects the behavior. A per-process atomic temporary-name counter also
replaced the collision-prone temporary publication naming. These correctness
fixes are included in the benchmark lane and verified by the full Cargo suite.

The next integration candidate, if pursued, is an explicit native path for
known changed files or changed-file checkout with exact Git export at the
boundary. Full-tree capture must first address its syscall/durability cost;
it must not be promoted on the favorable declared-path numbers. These
component measurements do not establish faster agent tasks, network-volume
performance, or arbitrary Git compatibility.
