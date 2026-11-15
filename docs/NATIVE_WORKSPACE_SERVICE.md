# Native WorkspaceService and Git exchange

`packages/tofu-workspace/src/service.rs` adds a semantic `WorkspaceService`
boundary around the native revision engine. It is an **exchange-only** slice:
no task admission, lane replacement, publication, global Git interception, or
second repository authority is enabled.

## Interface

- `NativeWorkspaceService::create/open`
- `import_git_into_new_root(root, repository, commit)`
- `export_git(repository, revision, branch)`
- `capture(changed_paths | complete)`
- `materialize(revision)`
- `manifest(revision)`

Native revision IDs and Git commit IDs are separate `RevisionInfo` fields.
Import materializes a named immutable Git tree into a new native root and
captures it natively. Export reads native object bytes and writes Git blobs,
then builds a private temporary index with explicit `update-index --add
--cacheinfo`, a tree and a commit. Export updates only the explicitly named
branch ref; it never changes the source checkout's HEAD, working tree, index,
configuration, or integration refs.

Import requires a new native root with no existing native revision or `.git`.
Git submodules, special entries, non-UTF-8 path names and unsupported entry
kinds are refused rather than silently approximated. Empty trees, binary
bytes, executable files and symlinks are supported. Native→Git→native tests
verify byte/mode/link equality and source HEAD/index immutability.

## Performance boundary

The native capture path remains responsible for fast packed publication and
changed-path operations. Git exchange is intentionally explicit and separate:
its cost includes Git plumbing and is not included in the earlier
`workspace_measure` component result. A production cutover would need service
benchmarks including import/export, paired native/Git runs on local and FUSE
volumes, and exact final-tree comparisons. These results do not establish a
production lane or application speedup.

## Remaining boundaries

- No merge algorithm, general Git CLI emulation, or live repository migration.
- No continuous synchronization between `.git` and native state.
- No implicit fallback from failed native operations to Git publication.
- No power-loss certification or external-writer snapshot isolation.
- No production task integration in this slice.

Verify with:

```sh
cargo test --locked --manifest-path packages/tofu-workspace/Cargo.toml
```
