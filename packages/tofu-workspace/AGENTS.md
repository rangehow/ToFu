# Native workspace prototype

This standalone Rust package owns native immutable manifests, content-addressed file versions, branch/WIP state, declared-path capture, reconciliation, and checkout. It never invokes Git or imports application modules.

The public contract is `contracts/native_workspace_v1.schema.json`; design limits and falsifiers live in `docs/NATIVE_WORKSPACE.md`. Read the root repository guidance and `docs/MONOREPO.md` before changing package or distribution boundaries.

Declared tracked-path capture is distinct from complete reconciliation. Never infer complete working-tree state from declared paths or filesystem events. Opening loads durable state and does not replace a WIP recovery pointer; arbitrary external writers are outside snapshot isolation. Do not add Git-shaped compatibility state or silently turn this prototype into integration authority.

Use fresh-file write/sync/rename/directory-sync publication for durable state. Validate paths at every public boundary, preserve symlink targets and executable modes, and refuse to overwrite uncommitted or unexpected content. No hardlinked editable working files.

Run `cargo test --manifest-path packages/tofu-workspace/Cargo.toml`. Keep tests synthetic, local, parallel-safe, and independent of Git and the application.
