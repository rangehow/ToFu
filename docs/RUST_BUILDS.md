# Rust builds run off the production pod

Owner: `scripts/remote_cargo.py` (entry point), `scripts/cargo_footprint.py`
(read-only census). Specifications: `tests/meta/test_remote_cargo.py`,
`tests/meta/test_cargo_footprint.py`. Rule for agents: root `AGENTS.md`.

## Entry point

```sh
python3 scripts/remote_cargo.py -C <crate dir> <cargo arguments>
# e.g. python3 scripts/remote_cargo.py -C packages/tofu-db test --offline --lib turn::
```

Run it from any directory inside a checkout (task checkout, lane or the shared
checkout). It snapshots the checkout as Git sees it (`git ls-files -co
--exclude-standard`: tracked and untracked files with their current contents,
uncommitted edits included, ignored and deleted files excluded), runs exactly
those cargo arguments in `<crate dir>` on one Hope CPU node, streams cargo's
output and exits with cargo's exit status. Setup failures (queue policy, no
Hope token, a job that ends without a status) exit 125 with the reason.
Interrupting the command stops its own Hope job.

`cargo fmt`, and builds whose artifact the pod itself executes (engine
preparation and certification by `serverctl_pkg.native_engine`, native test
lanes' `TOFU_TEST_TOFUDB_BIN`), stay local. The remote path returns output and
an exit status, not downloadable build artifacts.

### Run an existing oracle next to a fresh binary

```sh
python3 scripts/remote_cargo.py -C <crate dir> \
  --after-build <target-relative-binary> <checkout-relative-runner.sh> \
  build --locked --offline --release --bin <binary-name>
```

For a release binary, the first path is normally `release/<binary-name>`.
The second path names a regular shell script included in the source snapshot,
with the same trusted-code boundary as the Cargo build. Both paths must be
normalized and relative to their designated roots. The job verifies the exact
submitted archive's SHA-256 on the node and removes the selected executable
from any restored seed before calling cargo. After a successful build it
requires a regular, executable output at that path and logs the snapshot
and executable SHA-256 values. This identifies the inputs and the executable
of that run; it is not a native deployment certificate.

Add `--clean-source` before `build` to require an empty tracked/staged/untracked
status and archive the immutable `HEAD` with `git archive` rather than packing
worktree files. The job logs that commit and tree, in addition to the verified
archive hash. The runner receives `TOFU_REMOTE_SOURCE_COMMIT` and
`TOFU_REMOTE_SOURCE_TREE` (empty without `--clean-source`),
`TOFU_REMOTE_SOURCE_SHA256` (the verified archive), and
`TOFU_REMOTE_BINARY_SHA256` (measured just after building, before running the
oracle). These are runner metadata, not product configuration. Use the supplied
expected digest to check the bytes of the running child, not a fresh hash of
its launch path.

The job invokes the script with `sh` from the unpacked checkout root, passing
only the absolute executable path as `$1`. It supplies the job's `PATH`,
scratch `HOME` and `TMPDIR`, and snapshot `PYTHONPATH`; it does not inherit
caller or job credentials, live storage configuration, or other environment
variables. The script must use an already available, authorized Python runtime
and dependencies (nothing is installed by the wrapper), pass `$1` to the
existing suite's binary override, and use disposable fixtures. For Python
oracles, `TOFU_REMOTE_PYTHON` names the shared checkout's existing
`.venv/bin/python3` on DolphinFS (not the task worktree's ignored `.venv`).
The runner can explicitly use that interpreter with `"$TOFU_REMOTE_PYTHON"`;
a missing or incompatible runtime is a runner failure, not a reason to
install packages or fall back to the production pod.
A failed Cargo invocation keeps its exit status and never starts the runner;
otherwise the runner's output is streamed and its exit status is returned.
Verification/setup failures return 125. There is no change without opt-in,
and no additional job, queue, network relay, or executable export.

## How a run works

1. The queue is checked against the owner's Hope queue policy
   (`lib/tools/hope_queue_policy.py`, the file named by
   `TOFU_HOPE_QUEUE_POLICY`, project = the repository root) before anything is
   written. The `hope` CLI it calls is also the policy-guarded shim on PATH.
2. On DolphinFS, under the repository's Git common directory
   (`<repo>/.git/tofu-remote-cargo/`, never versioned, shared by every
   worktree), it writes:
   - `jobs/<id>/src.tar.zst`: the snapshot (`tar | zstd -1`, 14.3k files,
     125 MiB, written in 0.9 s; the job directory is removed when the run
     ends, and a later run removes directories whose wrapper on this host died);
   - `packs/toolchain--<key>.tar.zst`: the sysroot `rustc --print sysroot`
     reports in the crate directory (rustup overrides respected), keyed by that
     path and `rustc -vV`; 446 MB, built once per toolchain;
   - `packs/registry--<key>.tar.zst`: `CARGO_HOME/registry/{index,cache}` and
     `config.toml`, keyed by their listing; 105 MB, rebuilt when the registry
     gains crates.
   A new pack replaces the older pack of its kind.
3. It submits one `ml-easy-job` (`hope run`) to the CPU queue. The job unpacks
   snapshot, toolchain and registry on the node's own disk at the fixed
   `/tmp/tofu-remote-cargo`, extracts the newest dependency seed of its family,
   runs `cargo` with `CARGO_HOME`, `RUSTC`, `RUSTDOC` and `CARGO_TARGET_DIR` on
   that disk, appends output to `jobs/<id>/log` and publishes `jobs/<id>/rc`
   (status and final log size; the wrapper reads the log up to that size,
   because the rename of `rc` can reach this DolphinFS client before the log's
   last bytes do).
4. Only `CARGO_*` and `RUST*` variables of the caller reach cargo, except the
   ones the job owns (`CARGO_HOME`, `CARGO_TARGET_DIR`, `RUSTC`, `RUSTDOC`,
   `RUSTC_WRAPPER`, `RUSTUP_*`). Other variables (`TOFU_DB_*` included) are the
   node's; the engine's specifications pin each knob they depend on
   (`packages/tofu-db/AGENTS.md`).

### Shared build output: one dependency seed per crate family

The target directory lives on the node and goes with the job. A successful run
publishes `seeds/<crate>--<subcommand>--<profile>--<key>.tar.zst` (its target
without `incremental/`), where the key covers the lockfile, the toolchain and
the forwarded environment; it replaces the family's older seed. The next run of
that family extracts it before cargo starts.

A seed records the sources it was built from: `target/.tofu-sources.sha256`
(`sha256sum` format) and `target/.tofu-sources.paths` (the sorted snapshot path
list). The snapshot still unpacks with extraction-time mtimes (`tar -m`), so
before cargo runs each file whose content still matches the seed is set back to
2000-01-01, and a directory is set back only when the snapshot's path set is
identical to the recorded one, so directory-scoped `rerun-if-changed` still
sees added or removed files. Cargo then rebuilds exactly the units whose inputs
differ — byte equality, which is stricter than the mtime order a local build
trusts — while unchanged workspace crates and registry dependencies keep their
artifacts. A seed published before this manifest existed republishes once on
the next run of its family; a family whose inputs never change does not
republish.

Files on DolphinFS: one seed per family, two packs, and three files per running
job; nothing per crate compiled. Measured: tofu-contracts seed 99 MB,
tofu-core-rs seed (with the tofu-db dependency graph) 328 MB.

### Queue and job size

`root.shxs_training_cluster.your-user.autoresearch_cpu`, 16 vcores and
64 GiB per job. The queue sits beside this host's DolphinFS pool (sh02/shxs).
The first design used `hlsc ... omni_production` (the compute patrol's queue):
from there rustc read the toolchain and registry through FUSE at about 21 ms a
request and took 5 min 24 s for 12 small crates that compile in about 1 min
here, and writing a 130 MB seed back ran at about 0.3 MB/s for 7 min before
the job was stopped. The job size is the remote container's request, a fixed
property of the Hope job; it sizes nothing on the pod and limits no local work
(design principle 3).

Capacity is the queue's: `autoresearch_cpu` has 160 vcores, of which three
long-lived 40-vcore debug machines held 120 on 2026-11-18 and 2026-11-19, so
about two builds run at once and the rest wait in Hope's queue (not on the
pod). Container start took 45 s to 8.5 min; on 2026-11-19 a submitted job waited
57 s and 98 s for a free node in two samples, which is the largest single part
of a run. `arl_cpu_job` (800 vcores, 668 free) and
`agentic_cpu_job` (1200, 358 free) are in the same cluster but belong to other
teams; moving there needs the owner's decision.

## Verification (2026-11-18)

| crate | local (`cargo test --offline`, pod) | remote | test names and results |
|---|---|---|---|
| packages/tofu-contracts | 1 min 05 s build, 6 tests ok | cold 58.8 s / seeded 56.5 s build, 6 tests ok | identical (6/6), same test-binary hashes |
| packages/tofu-core-rs (+ path dependency tofu-db) | 47-49 s build, 14 tests ok | seeded 44.9 s build, 14 tests ok | identical (14/14) |

End to end the remote run took 135-165 s once a container was free (snapshot
and packs 3.6-4.1 s, unpack on the node 2-4 s). The wrapper used 4.0 CPU-seconds
of the pod (2.32 user + 1.65 sys) where a local cold tofu-core-rs test build
used 118.6 CPU-seconds (94.8 user + 23.8 sys); four concurrent local builds used
about 680.

## Verification (2026-11-19): a reused seed compiles nothing

`python3 scripts/remote_cargo.py -C packages/tofu-channel-stdio test --offline`,
the same command twice against the real queue.

| run | snapshot and packs | queue and container | unpack | cargo | total |
|---|---|---|---|---|---|
| cold, publishes the seed | 24.0 s | 2 min 49 s | 3 s | 2.95 s (10 crates) | 3 min 01 s |
| seeded, sources unchanged | 24.5 s | 1 min 38 s | 49 s | 0.09 s (nothing compiled) | 2 min 31 s |

Both runs: 17/17 tests ok. The seeded run reports `15347 of 15380 snapshot files
match the seed`; the 33 differences are files another writer edited between the
two runs, and they are the only inputs cargo treated as new. A crate that
compiles for about 50 s (tofu-contracts, 2026-11-19: 136 s end to end, 57 s of
it compiling, no source change since its seed) now keeps that compile instead
of repeating it.

## Footprint before and after

`python3 scripts/cargo_footprint.py [--json]` lists every Cargo target
directory under this user's `/tmp/tofu-<uid>-*` placements, `/tmp/*target*`,
`~/.cache/*target*` and the shared checkout's `packages/`, `benchmarks/` and
root, plus `CARGO_HOME/registry` and the rustup home, with files, bytes and the
owning checkout and live cargo/rustc PIDs. DolphinFS bytes are apparent sizes:
its FUSE mount under-reports `st_blocks`.

Directories / files / bytes. Before: 2026-11-18 16:44 (the DolphinFS
`packages/tofu-db/target` row was measured at 16:55, after the census learned
to recognize targets without Cargo's marker files). After: 17:46, with this
work's own local baselines removed. The remote state directory adds 4 files
(0.98 GB) on DolphinFS: two packs and two seeds.

| where | before | after |
|---|---|---|
| DolphinFS: `CARGO_HOME/registry` | 1 / 37,510 / 0.6 GiB | 1 / 42,296 / 0.8 GiB |
| DolphinFS: rustup toolchains | 1 / 872 / 2.6 GiB | 1 / 872 / 2.6 GiB |
| DolphinFS: shared checkout targets (`packages/tofu-db/target`) | 1 / 366 / 0.7 GiB | 1 / 366 / 0.7 GiB |
| DolphinFS: `.git/tofu-remote-cargo` | none | 4 files / 0.98 GB |
| local `/tmp`: task checkouts | 27 / 89,014 / 60.2 GiB | 30 / 119,329 / 77.2 GiB |
| local `/tmp`: task scratch (`$TMPDIR`) | 29 / 70,702 / 33.4 GiB | 66 / 190,735 / 100.3 GiB |
| local `/tmp`: lanes | 9 / 42,166 / 30.5 GiB | 9 / 42,166 / 30.5 GiB |
| local `/tmp`: native compiler caches | 2 / 12,007 / 8.1 GiB | 2 / 12,660 / 8.4 GiB |
| agent-chosen (`~/.cache` on `/home`, `/tmp/*target*`) | 4 / 23,688 / 6.3 GiB | 4 / 24,507 / 7.6 GiB |

The largest single target held 8.4k files (13.9k after), not 10^5. During that
hour Workers that had not yet adopted the remote path created 46 new local
targets (153k files, 83 GiB), every one inside a task checkout or its
`$TMPDIR`. The 4,786 new registry files are crates that local builds extracted
into the shared `CARGO_HOME/registry/src` on DolphinFS. Remote jobs extract
into the node's own `CARGO_HOME`, so they add none.

The premise that per-worktree targets fill the DolphinFS file quota does not
hold on this host: task checkouts, lanes and their targets live on the pod's
node-local `/tmp` (an emptyDir on the node's md RAID). The account quota cannot
be read from inside the container (`statvfs` shows the 28 PiB pool with 0
inodes; `getxattr` is unsupported; the bundled `beegfs-ctl` has no management
host), and the account is shared by about 80 users, so its usage and limit
remain unknown; `docs/modules/infra_runtime.md` records the 11:15 EDQUOT
episodes of 2026-11-17 and 2026-11-18.

### Pod ephemeral storage

Read from the Kubernetes API with the pod's service account (2026-11-18): the
`pc` container declares only `cpu: 58` and `memory: 203Gi` (no
`ephemeral-storage` request or limit), and the `pscvn-tmp-root` emptyDir behind
`/tmp` has no `sizeLimit`. Eviction can therefore come only from the node's
disk-pressure threshold on the md RAID (`/tmp`: 5.9 TB, 371 GB used). Remote
builds write nothing to `/tmp` beyond the wrapper's transient staging directory
(two small files); the build output lives on the Hope node.

### Lifecycle of local targets that remain

Task-checkout targets go with the checkout: delivery and discard recycle it
(`task_spares.recycle` runs `git clean -ffdx`) or remove it, and
`task_spares.retire_scratch` removes its `$TMPDIR`. Checked 2026-11-18: of 109
scratch directories, the 3 without a checkout held no target. Lane and native
compiler caches keep their owners ("Filesystem and Git artifact lifecycles" in
`RESOURCE_CONTROL.md`). Targets agents put elsewhere (`~/.cache/tofu-node-target*`,
`/tmp/tofu-front-rs-target`) have no owner Tofu can prove and were left in
place; the remote path makes new ones unnecessary.

## Host CPU throttling

Pod cgroup `cpu.stat` sampled every 10 s, with the host's live `rustc`
count (2026-11-18). Window A: four concurrent local cold `cargo test
--offline` builds of tofu-core-rs, the way Workers build today. Window B: the
same four builds through `remote_cargo.py`. Other Workers' local builds kept
running in both windows, so B's throttling is theirs.

| | A: 4 local builds | B: 4 remote builds | other time, other Workers' rustc only |
|---|---|---|---|
| duration | 60 s | 425 s (Hope queue waits) | 54 intervals |
| throttled periods | 35 of 604 (5.8%), 35/min | 82 of 4,240 (1.9%), 12/min | 131 of 5,501 (2.4%) |
| throttled time | 128.8 s in 60 s | 308.3 s in 425 s (43.5 s/min) | |
| peak `rustc` on the pod | 59 | 12 (all other Workers') | |
| pod CPU the 4 builds used | about 680 s | 17.9 s (wrappers) | |
| results | 4 x 4 result lines, ok | 4 x 4 result lines, ok | |

The remote builds' share of pod CPU is the wrapper's: 3.2-6.9 CPU-seconds
each (snapshot, packs, polling).

## Decision record

**Problem.** About 14 Rust Workers ran cargo on the production pod at once.
Cargo's default `-j` follows the CPU count, so concurrent builds oversubscribed
the pod's 58-CPU CFS quota and throttled the server; `nice` does not prevent
throttling. Targets also accumulated per checkout.

**Decision.** Builds and tests run as Hope CPU jobs on a snapshot of the
checkout, with the toolchain, registry and a per-family dependency seed shipped
as single files on DolphinFS and the target on the node. The pod keeps only the
wrapper. No local cap: local work is unchanged when someone does run cargo
locally, and nothing on the pod is sized from its CPU count.

**Alternatives rejected.**
- Running the job against the worktree on DolphinFS: task checkouts and lanes
  are on the pod's node-local `/tmp`, which no Hope node can read; the shared
  checkout's tree on DolphinFS would not hold a Worker's uncommitted edits.
- A Git snapshot commit instead of a tar: writes loose objects for every
  changed file into the shared object store and needs a Git on the node that
  understands this repository's format.
- Reading toolchain and registry in place on DolphinFS: measured 5.4 min for
  12 small crates (per-request FUSE latency on the node).
- One shared `CARGO_TARGET_DIR` on DolphinFS or the pod: it serializes
  concurrent builds on Cargo's lock, workspace crates of different checkouts
  overwrite each other's fingerprints, and on DolphinFS it is exactly the
  unbounded small-file tree the quota counts.
- sccache: not installed here, needs a daemon or storage backend, and does not
  cache the workspace crates that dominate rebuilds.
- A `-j` or CPU cap for local builds: forbidden by "Hardware never limits work".
