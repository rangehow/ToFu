# Native workspace prototype

`packages/tofu-workspace/` owns the standalone Rust workspace/revision prototype. Its public boundary is [`contracts/native_workspace_v1.schema.json`](../contracts/native_workspace_v1.schema.json). It has no Git, Python, application-storage, network, or production integration dependency.

## Problem

Git's current integration path discovers changes by walking and stating the working tree, then represents lanes through Git worktrees and indexes. We need evidence that a native engine can preserve workspace bytes and durable revisions while making declared-path capture and revision switching independent of unchanged-file contents.

## Decision

Use immutable, path-sorted manifests and content-addressed file versions. A revision identifies its manifest; branch and WIP state have native identities rather than Git SHA semantics. `Workspace::commit` accepts changed tracked paths explicitly, so modifying a known file costs that file plus its manifest, not a tree-wide content read. `Capture::Complete` covers complete discovery of additions, removals, and renames. `open` loads the saved WIP without silently replacing its recovery pointer; reconciliation happens when capture or status needs current bytes.

The first slice implements:

- complete initial capture and exact revalidation;
- declared changed-path commits, including modes and symbolic links;
- durable branch heads and crash-recoverable WIP saves;
- revision checkout preserving unrelated untracked files;
- same-content checkout leaving file identity and bytes untouched;
- changed-path materialization with backup/rollback on an I/O failure;
- explicit refusal when a target path contains uncommitted changes;
- branch-head compare-and-swap.

New objects are batched into immutable packs: one pack data file and index per capture batch, with per-object content verification on read. Legacy loose objects remain readable through an inventory built while opening the workspace. Pack publication precedes revision/WIP and state publication, so unreferenced data stays inert. State publication uses one prepared temporary state and one rename; every stored replacement uses write/sync/rename/directory-sync publication. WIP captures preserve their base revision and the complete captured manifest; restoring one requires the branch still to be at that base. Backup objects needed by checkout/restore are sealed before working files are mutated.

## Boundaries and falsifiers

This is an engine prototype, not a replacement Git executable, a live migration, a merge algorithm, or a virtual filesystem. It does not make arbitrary shell `git` commands native and does not replace integration/delivery contracts yet.

Capture is atomic against other calls through one `Workspace` instance. It does not promise a filesystem-wide snapshot while unrelated external processes write. Complete reconciliation is required after events are lost, after downtime, and for unrecognized changes. Any future watcher is only an invalidation hint until a certified filesystem contract proves stronger observation.

The prototype is rejected if committed or WIP bytes do not survive reopening, if a failed checkout can silently discard WIP, if an unchanged file is replaced during checkout, or if ordinary declared-path capture reads or hashes unchanged tracked files.

## Verification

```sh
cargo test --manifest-path packages/tofu-workspace/Cargo.toml
```

The suite uses synthetic local workspaces, real file identity checks, symlink/mode preservation, branch conflicts, reopen recovery, mixed loose/packed reads, pack corruption, containment, and interrupted-publication remnants. It intentionally does not claim production performance or FUSE observation guarantees. The pack implementation reduces object-publication syscalls; it does not remove the full-tree metadata/content reconciliation cost or prove power-loss recovery.
