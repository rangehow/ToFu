# Export and publication guidance

## Scope

`export_pkg/` owns reproducible source snapshots and publication sanitization.
It does not own product behavior or source selection merely because it packages
those files.

## Publication boundary (hard rule)

`export_pkg/` NEVER ships in internal or opensource exports: `_lists.py` carries
private markers (real API keys, Feishu secrets, internal usernames/domains) as
sanitization patterns. The package is in `ALWAYS_EXCLUDE_DIRS` and destination
cleanup removes stale copies (`export.py` is root-excluded for the same reason).
It is a registered keeper prefix in
`tests/meta/test_gitignore_covers_export_excludes.py` (tracked privately, never
published). Personal backups retain the toolchain. No shipped runtime module may
import `export_pkg` — only the `export.py` facade and `tests/` do.

## Module layout and import direction

`export.py` is a thin facade that re-exports this package's public surface for
tests and `main()`. Implementation lives here; imports flow strictly upward:

- `_state` — mutable export-run globals (leaf)
- `_lists` — inclusion/exclusion sets, secret/endpoint patterns, colors (leaf)
- `_excludes` — `_should_exclude` predicate + glob/text helpers (← `_lists`)
- `_tar` — streaming tar copy + progress (← `_lists`)
- `_version` — VERSION bump/read/commit (← `_lists`)
- `_verify` — exported-tree syntax/integrity verification (← `_lists`, `_state`)
- `_snapshot` — committed-HEAD staging, untracked-file scan
  (← `_lists`, `_state`, `_verify`)
- `_sanitize` — post-copy sanitization pipelines (← `_lists`, `_excludes`, `_state`)
- `_internal` — internal-mode retention: seeded config, portable MCP
  registrations, installed skill packages, vendored MCP wheels, install.sh
  proxy patch (← `_lists`, `_excludes`, `_state`, `_sanitize`). Its three
  user-state passes restore what the bulk policy strips: `mcp_servers.json`
  holds personal tokens (only bare-launcher/npx entries survive), and
  `.tofu/skills` + `data/skills` hold installed packages. Both are internal
  only; opensource and personal exports carry neither.
- `_handoff` — cross-cluster handoff: bootstrap.sh + gitignored-state tarball
  instead of a full FUSE copy (← `_lists`, `_internal`, `_snapshot`)
- `_export_core` — skeleton creation, dest cleanup, export summary
  (← `_lists`, `_internal`)
- `_publish` — publication gate + authenticated git push
  (← `_lists`, `_excludes`, `_version`)

- `_history` — incremental sanitized public Git DAG reconstruction, private
  cursor and exact verified-tree reconciliation (← `_lists`, `_excludes`,
  `_sanitize`, `_publish`). No raw private Git objects/refs/alternates enter
  the destination; first use requires an explicit source cutoff.
- `_pipeline` — tar exclude bundles + personal/opensource copy pipelines
  (← `_lists`, `_excludes`, `_sanitize`, `_snapshot`, `_tar`)
- `_orchestrate` — `export_project` top of the graph (← all of the above)

Never import downward (e.g. `_lists` must not import `_sanitize`); add a helper
to the lowest owning module instead. `export.py` facade imports are exempt —
they ARE the compatibility surface.

## Editing rules

- Keep inclusion/exclusion policy centralized in the package's list owners.
  `.gitignore`, manifests, build inputs, and drift tests must agree where their
  purposes overlap.
- Export from an explicit immutable snapshot. Preserve tracked user changes;
  never mutate the working tree to make an archive.
- Normalize archive metadata and ordering so equivalent inputs produce
  equivalent output. Bound temporary disk use and clean only paths created by
  the current run.
- Fail closed on secrets, credentials, private-only files, unsafe links,
  traversal, duplicate members, and verification mismatches. Sanitization must
  be observable and tested, not a silent best effort.
- Public-export exclusions do not authorize deleting private repository state.
  Keep packaging and publication as distinct, verified stages.

## Verification

Run the focused `tests/export/test_export*.py`, the gitignore/export drift guard, and a
dry-run snapshot verification. Inspect the resulting member list rather than
printing archive contents that may include sensitive material.
