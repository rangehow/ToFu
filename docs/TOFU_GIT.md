# `tofu-git`: native Git command surface and exchange

`packages/tofu-workspace/src/bin/tofu_git.rs` provides a native executable
backed by **libgit2**, not by spawning a system `git` binary. It uses ordinary
Git repositories and real Git object IDs; native workspace revision IDs remain
a separate exchange concern.

## Supported in this slice

- `init`
- `status`
- `add <literal repository-relative paths>`
- `commit -m <message>`
- `log`
- `diff`
- `rev-parse <revision>`
- `checkout <branch-or-revision>`
- `shim <directory>`

`-C <path>` is supported before the command. The shim command creates a `git`
symlink to the native executable. Users explicitly prepend that directory to
`PATH`; no system binary is overwritten or globally intercepted.

The tests execute the CLI with `PATH=""` and cover initialization, staging,
commits, logs, diffs, checkout, unsupported-command refusal, and shim startup.
The exchange implementation in `service.rs` uses libgit2 for Git objects,
recursive tree traversal, tree updates, signatures and refs. No external Git
process is required at runtime.

## Explicit non-goals for this slice

This is not yet complete Git compatibility. Merge/rebase, remotes, hooks,
submodules, GPG, pager behavior, general pathspec, configuration parity,
checkout collision semantics, exact porcelain output/exit codes, and every
command/option outside the documented subset require separate implementations
and differential tests. Unsupported commands fail nonzero rather than
silently delegating to system Git or claiming success.

The native workspace engine remains responsible for packed capture and
changed-path performance. `tofu-git` is a real Git-format CLI and exchange
boundary, not a claim that all Tofu Git calls have been migrated.

Verify with:

```sh
cargo test --locked --manifest-path packages/tofu-workspace/Cargo.toml
```
