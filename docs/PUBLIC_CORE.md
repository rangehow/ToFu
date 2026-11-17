# Public core and private overlay

Authority for how Tofu splits into a public core that runs with nothing
company-specific installed and private overlays that add company integrations.
Executable specifications: `tests/meta/test_public_core_boundary.py`
(no core → overlay imports; shrink-only ledgers for what remains),
`tests/meta/test_public_core_boots_without_overlay.py` (the real application
assembles and serves with every overlay module refused and no bundle installed),
`tests/providers/test_gateway_profiles.py` (the extension mechanism) and
`bundles/yourprovider/tests/test_yourprovider_gateway_profiles.py` (the overlay restores
the owner's exact behavior).

## Problem

The owner wants colleagues to pick up core (内核) updates easily and to make
the GitHub repository public (owner request 2026-11-15). The repository
already publishes twice, and both channels are wrong:

- **Open edition.** `export_pkg` copies the tree, drops `bundles/`,
  `benchmarks/`, `debug/`, root `scripts/` and some named files, then
  *rewrites* source text: internal hosts, keys, usernames and a few
  `_OPENSOURCE_BUILD` flags are regex-replaced (`_sanitize.py`,
  `_lists._ENDPOINTS`, `_INTERNAL_DOMAIN_LITERALS`, `_OPENSOURCE_FIXED_CANDIDATES`).
  It then reconstructs a sanitized Git DAG (`_history.py`) and pushes it to
  `github.com/rangehow/ToFu` and `github.com/NiuTrans/ToFu`. Both repositories
  are already publicly readable (anonymous GitHub API HTTP 200, 2026-11-15).
  So the public code is a *derived rewrite*, not the source. A colleague's fix
  against it cannot come back. Any core module that branches on
  `your-llm-gateway.example.com` behaves differently once rewritten, and every new
  literal is a leak until a sanitizer pattern catches it.
- **Internal edition.** Colleagues clone `git.internal.example.com/~your-username/tofu`
  `master`, a whole-tree snapshot carrying core, company code, a seeded
  configuration and the shared credential vault (`export_pkg/AGENTS.md`).
  Updating core means taking the next snapshot. A colleague cannot take core
  without company code, and cannot keep local changes cleanly.

The root cause was that core *contained* company specifics, so publication has to
subtract them by rewriting. Measured on 2026-11-15 (before this change):

| Company-specific code | Location | Core modules that reached it |
|---|---|---|
| FRIDAY gateway quirks: Claude tool-name budget, keyword (HTTP 450) sanitizer, Claude cache billing, proprietary image API | literals `your-llm-gateway.example.com`, provider id prefix `example-corp`, `('yourprovider','example-corp')` | `lib/llm/tool_wire.py`, `lib/llm/body/_build.py`, `lib/tasks_pkg/wire_messages.py`, `lib/pricing/_tables.py` + `_provider.py`, `lib/image_gen/_slots.py` |
| Release bundles (provider templates, site packs, search gateways, skills) | `bundles/yourprovider/` | five separate `<repo>/bundles` directory scanners: `bootstrap_pkg/providers.py`, `lib/provider/provider_template_recipes.py`, `lib/search/search_gateway.py`, `lib/site_packs/_loader.py`, `routes/api_v1/site_packs.py`; plus a hard-coded row in `lib/skills/paths.py` |
| 大象 / DX messaging adapter | `lib/channels/dx/` | `lib/channels/{ingress,policy,service}.py`, `lib/router/owner_settings.py`, `lib/router/watchdog/snapshot.py`, `lib/scheduler/manager.py`, `lib/server/server_background_services.py`, `serverctl_pkg/oob_alert.py`; frontend `features/settings/dx-robot.ts` |
| CatPaw gateway (`mcli.internal.example.com`) and local key import | `lib/llm/catpaw_gateway.py`, `lib/catpaw_local_import.py` | `lib/llm/_sse_core.py`, `lib/llm/chat.py`, `lib/provider/provider_probe.py`, `lib/llm/stream.py`, `routes/api_v1/providers.py` |
| Internal MCP servers (hope, llm, xuecheng) | sibling repos, launched through `lib/mcp/vendored.py` rows | `lib/mcp/vendored.py`, `lib/mcp/registry.py` (catalog/icon), `lib/mcp/project_names.py` (`km.internal.example.com` default), `lib/search/search_bridge.py` (学城 doc reader) |
| YourProvider SSO | `packages/tofu-mtsso-auth/` (separate distribution) | no core import (only `extra_sources` rows in `lib/mcp/vendored.py`) |
| MLP / CodeLab / Hope environment providers | `bundles/yourprovider/environment-provider/` (entry points `tofu.environment_providers`, `tofu.compute_providers`) | none — already a plugin |
| Repair relay upstream and DolphinFS placement | `serverctl_pkg/oob_relay.py` (`your-llm-gateway.example.com`, provider `example-corp`), `rootless_vm/host_paths.py` (formerly a hard DolphinFS mount guard, now the generic `/mnt/<volume>` rule) | — |

Note that 企业微信 (WeCom), QQ, 微信, GitHub and Feishu adapters are public
platforms and stay in core. DolphinFS mentions elsewhere are comments that
explain generic FUSE behavior (whole-second mtimes, per-directory entry caps).
They describe a filesystem class, not a dependency.

## Decision

### D1. Package boundaries

| Tree | Holds | Repository |
|---|---|---|
| Public core | everything except the rows below: `lib/`, `routes/`, `frontend/`, `serverctl_pkg/`, `bootstrap_pkg/`, `packages/` except `tofu-mtsso-auth`, `plugins/`, `tofu_extensions.py`, `docs/`, `tests/` | public GitHub `ToFu`, the source authority |
| Private overlay `tofu-yourprovider` | `bundles/yourprovider/**` (bundle data, `environment-provider`, its tests), `packages/tofu-mtsso-auth`, the vendored internal-MCP launcher rows, and, once moved, the 大象 adapter, CatPaw adapter and 学城 reader | private repo on dev.internal.example.com |
| Private toolchain | `export_pkg/`, `export.py`, `JOURNAL.md`, root `scripts/` that carry hosts | owner-only |

Dependency direction is one-way: the overlay depends on a core version range;
core never imports the overlay (`OVERLAY_MODULES` in the boundary test). Until
the physical split, the overlay lives at `bundles/yourprovider/` in the owner's
checkout. Core already treats it exactly as it will treat an installed
distribution.

### D2. Extension interface: data through bundles, code through entry points

Two channels, both pre-existing in shape. No new mechanism kind is introduced.

**Data — bundles.** A bundle is a directory with well-known subdirectories.
Core reads them through one owner, `tofu_extensions.py` (stdlib only, so the
stdlib bootstrap shares it):

- `bundle_roots()` returns the in-tree `<repo>/bundles/*` directories (source
  checkouts, read live), followed by roots that installed distributions
  contribute through the **`tofu.bundles`** entry-point group. Each entry
  resolves to a path or to a zero-argument callable returning one. Entries are
  read once per process, because installing requires a restart. Roots are
  deduplicated by resolved path, so an editable install of the in-tree bundle
  counts once.
- `bundle_directories(kind)` and `is_bundle_path(path)` replaced the five
  private scanners. Current kinds and their single core reader:
  `provider_templates/` (`lib/provider/provider_template_recipes.py`, bootstrap
  twin), `site_packs/` (`lib/site_packs/_loader.py`), `search_gateways/`
  (`lib/search/search_gateway.py`), `gateway_profiles/`
  (`lib/provider/gateway_profiles.py`), `local_credentials/`
  (`lib/provider/local_credentials.py`), `mcp_catalog/` (`lib/mcp/registry.py`),
  `mcp_launchers/` (`lib/mcp/vendored.py`) and `router_home/` (the owner
  resource registry seed `RESOURCES.md`, `lib/router/home.py`; the last
  bundle's file wins). Later layers still win by key in
  each consumer (`docs/SITE_PACKS.md` §4); core rows win over bundle rows for
  the MCP catalog and launchers.

**Code — entry points.** Overlay code registers through entry-point groups.
`tofu_extensions.extension_entry_points(group)` returns installed entries
followed by those an in-tree bundle declares in its own `pyproject.toml`
(`bundles/yourprovider/pyproject.toml`), so a source checkout that carries the
overlay without installing it behaves as if it had (loading such an entry puts
the bundle root on `sys.path`; an installed distribution of the same name
wins). Groups:

- `tofu.channel_adapters` (`lib/channels/adapters.py`): a contract channel the
  core does not ship. The port is the account commands, the command-chat robot,
  the owner-message binding, the supervisor lifecycle, the Router's login
  preconditions and the alert sender's robot. Without the adapter the channel
  answers `channel_unavailable`. The YourProvider overlay registers `dx`
  (`tofu_yourprovider.dx.plugin:ADAPTER`);
- `tofu.blueprints`, `tofu.startup`, `tofu.shutdown` and `tofu.task_runtimes`
  (`lib/app/plugin_registry.py`);
- `tofu.storage` (`lib/storage/plugin_registry.py`);
- `tofu.flags` (`lib/feature_registry.py`) and `tofu.tools`
  (`lib/tools/registry/_plugins.py`, `docs/contracts/TOOL_PLUGINS.md`);
- `tofu.providers` (`lib/llm_dispatch/provider_registry.py`);
- `tofu.experiments`;
- `tofu.environment_providers` and `tofu.compute_providers`
  (`docs/ENVIRONMENT_PROVIDERS.md`).

MCP servers stay configuration: an overlay ships catalog entries and launcher
rows as bundle data, and core's MCP client runs them. A catalog entry's
`document_reader` (hosts, path pattern, tool, argument names, link base) lets
the fetch reader read that connector's document links under the user's
identity (`lib/search/search_bridge.py`, `_ChatuiMcpDocumentReader`); 学城 is
the overlay's one declaration. `plugins/tofu-trading` is the reference shape
for a separately installed code plugin.

### D3. Gateway profiles replace host literals

**Problem:** gateway quirks are facts about one company gateway. Core encoded
them as hostname and provider-id literals, so a public core either carried the
company host or had it rewritten away. **Decision:** a bundle declares
`gateway_profiles/*.json` (`tofu.gateway-profile/v1`, owner
`lib/provider/gateway_profiles.py`). A profile has:

- `hosts`: exact, or a leading `.` for a dot-boundary domain;
- `provider_ids`: exact, or a trailing `*` for a prefix;
- closed-world `traits`. Each trait has exactly one core consumer and a value
  validator (`TRAIT_VALIDATORS`). Unknown or malformed traits are skipped with
  a warning.

`gateway_trait(name, base_url=, provider_id=)` returns the first matching
declaration. With no profile installed, every lookup is `None`, so requests
take the plain provider path. Traits today:
`anthropic_claude_tool_name_budget_bytes` (`lib/llm/tool_wire.py`),
`blocked_term_sanitize` (`lib.llm_sanitize.gateway_blocked_term_sanitize`,
shared by `build_body` and the wire preview), `claude_cache_multipliers`
(`lib/pricing/_provider.py`, exact provider ids only) and
`image_generation_api` (`lib/image_gen/_slots.py`), `anthropic_system_identity`
and `working_directory_header` (`lib/llm/gateway_shape.py`, the Anthropic
request fingerprint some gateways require). The YourProvider bundle declares
`friday.json`, `friday_keyword_filter.json` and `catpaw.json` with the exact
legacy gates.

A provider whose credential only a vendor CLI issues declares that source in
`local_credentials/<template_key>.json` (config path, format, field, token
pattern, login command). Only bundles declare sources, never the user drop-in
tier or a request, because a caller-controlled path or command would make the
import a file-read oracle or a command launcher. The CatPaw card's routes keep
their API paths and read the declaration; without it they report nothing
available.
Values are external provider facts recorded with their evidence, not tuning
knobs (AGENTS.md principle 5).

### D4. Configuration and secret separation

- Core ships only public defaults: no company host, endpoint, account, key or
  path in executable code. The boundary test's `HOST_LITERAL_DEBT` and
  `FRONTEND_HOST_LINE_DEBT` ledgers are exact and may only shrink. Comments and
  docstrings are excluded because they change no behavior; they are still
  subject to the publication gate.
- Overlays ship *declarations* (templates, profiles, packs, launchers), never
  credentials.
- Secrets stay where they already live: the owner credential vault and
  `<data>/config` under the runtime data root. They are not in either
  repository. The internal edition's decision to carry the shared vault
  (owner decision 2026-11-01) and the built-in 大象 robot (2026-11-08) becomes
  overlay *installer* behavior. Public core never sees it.
- The pre-publication audit (sibling Worker, 2026-11-15) found that
  `rangehow/ToFu` and `NiuTrans/ToFu` are already public, that refs other than
  the sanitized `main` (`master`, tag `search-v0.12.25`, `main`'s pre-baseline
  roots) carry raw private history, and that four internal gateway/Feishu
  credentials there must be rotated whatever is decided. This design does not
  change that history; it removes the reason the public tree needs rewriting.
  The audit's overlay list matches D1; its remaining items (Hope queue policy,
  internal eval infrastructure, MLP host recognition, `private_hosts`
  defaults, model-brand tables) are increments below.

### D5. Frontend pieces of plugins

The existing plugin host is the only path: `frontend/src/plugin.ts` and
`frontend/src/plugins/` expose explicit API and Markdown ports. A plugin page
ships its static assets inside its Python distribution and mounts with
`entry.js::mount(host)`, through the `tofu_plugin_assets()` helper
(`plugins/tofu-trading/AGENTS.md`).

Company facts reach the browser as server data from the installed adapter,
never as literals in the bundle: the 大象 robot card renders the console links
the dx adapter returns in `ChannelAccountView.robotSetupConsoles`. The 大象
settings panels themselves still ship in core, generic over the contract's
`dx` channel id, and without the adapter the account read answers
`channel_unavailable`. Moving those panels to a plugin-provided settings
surface needs a settings slot in the plugin host (increment 10).

### D6. Versioning and the update channel for colleagues

- Core keeps `VERSION` and SemVer release tags on the public repository. Before
  1.0, a minor bump marks a plugin-interface break: entry-point groups, bundle
  kinds and the profile contract version.
- `lib/self_update` already follows either the newest public release tag or a
  configured git upstream. Colleagues therefore install the public core by
  `git clone` (updates through the topbar "Update" / `git pull --ff-only`) or
  by `pip install tofu-agent`.
- The overlay is a separate distribution pinned to a compatible core range
  (`tofu-agent>=0.19,<0.20`, like `tofu-trading`). It installs from the
  private repo (`pip install git+ssh://…/tofu-yourprovider@vX`) or the internal
  index. It contributes `tofu.bundles` plus its code entry points. Updating
  core never merges private code; updating the overlay never touches core files.
- The owner develops both in one working tree during transition. The
  publication step then becomes *verification*: copy the public paths, run the
  boundary and boot tests, and push. Text rewriting stays only as a
  defense-in-depth gate until the ledgers reach zero, then it is deleted.

### D7. License placeholder

Core keeps the existing `LICENSE` (MIT, "Tofu contributors") as a placeholder.
The overlay gets `LICENSE` "Proprietary — internal use only" when it is split
out. **Owner decision required:** code written for and at the company may need
the company's open-source review before the development history, as opposed to
the already-public sanitized mirror, becomes public. Choosing MIT vs Apache-2.0
(patent grant) is part of that review.

## Alternatives considered

- **Keep sanitizing exports (status quo).** Rejected: the public tree is a
  rewrite. Behavior keyed on a rewritten literal silently differs, every new
  literal is a leak until a pattern exists, and contributions cannot flow back.
- **Long-lived private fork that merges public upstream.** Rejected: every
  core update becomes a merge of private code, which is exactly what
  colleagues must not do.
- **Git submodule for the overlay inside the public repo.** Rejected: it
  publishes the private repo's URL and pins, and couples core updates to
  overlay commits.
- **`if INTERNAL:` flags or a config list of company hosts in core.**
  Rejected: company code would still ship publicly, and a host list is a new
  knob (principle 5). Profiles are declarations owned by the overlay that
  describes the gateway.
- **Per-integration repositories.** Rejected for now: seven small private
  repos multiply release work. One overlay distribution can grow
  sub-distributions later.
- **Gateway quirks as provider-template route features.** Rejected for these
  four traits only. The consumers run where no selected route exists yet
  (`build_body` before dispatch, the legacy `LLM_BASE_URL` gate) or key on the
  provider id (pricing). Header-carried features such as
  `cache_affinity_mt_session_id` remain route features.

## Current state

- **Overlay code.** The 大象 adapter (`tofu_yourprovider.dx`: bridge, SSO login,
  robot, supervisor, account commands and its contract facts `facts.json`)
  lives in `bundles/yourprovider/tofu_yourprovider/`. Core reaches it only through
  `lib/channels/adapters.py`; process start, shutdown and the scheduler tick
  iterate installed adapters. CatPaw, FRIDAY, 学城 and the internal MCP servers
  are bundle declarations read by generic core owners.
- **Boundary.** Core imports no overlay or retired company module, and
  `INTERNAL_IMPORT_DEBT` is empty. `HOST_LITERAL_DEBT` holds one row
  (`serverctl_pkg/oob_relay.py`'s default upstream, increment 5b) and
  `FRONTEND_HOST_LINE_DEBT` is empty
  (`tests/meta/test_public_core_boundary.py`).
- **Boot.** With every overlay module refused and no bundle installed, the
  application assembles and serves; every gateway trait is absent; no company
  template, site pack, search gateway, MCP catalog entry or launcher is offered;
  the 大象 account answers `channel_unavailable`
  (`tests/meta/test_public_core_boots_without_overlay.py`).
- **Tests.** 大象, CatPaw, FRIDAY and 学城 specifications live in
  `bundles/yourprovider/tests/` (the 大象 conformance baseline in
  `bundles/yourprovider/tests/channels/dx_conformance_baseline.json`). Core tests that use 大象 as a
  fixture skip without the overlay (`tests/support/dx_overlay.py`,
  `tests/channels/conftest.py` `requires_dx`). Gateway behavior tests declare
  synthetic profiles on `*.example.test` hosts.

## Remaining increments

| # | Increment | Owner | Ledger / effect |
|---|---|---|---|
| 5b | The repair relay's default upstream comes from the watcher's `repairAgent` configuration (written when the owner enables it), not `your-llm-gateway.example.com`. It cannot read routing: it runs while Tofu may be down | serverctl/self-repair owner | last `HOST_LITERAL_DEBT` row |
| 6b | The `bundles/yourprovider/skills` row of `lib/skills/paths.py` becomes the bundle kind `skills/`; its export-visibility contract test moves with it | public-core Worker with the skills owner | removes the last company path from a core table |
| 9b | Core tests still naming company hosts or products as fixture data (`git grep -l -i example-corp -- tests` lists ~100 files, mostly provider-face, cache and visual fixtures) switch to `*.example.test` or move to the overlay; run the core suite in CI with the overlay absent | public-core Worker | public CI passes on the bare core |
| 10 | Plugin settings slot in the frontend plugin host; the 大象 panels (`frontend/src/features/settings/dx-*.ts`) and the CatPaw card move to the overlay | frontend owner | removes company UI from the core bundle |
| 11 | Remaining company integrations named by the audit: Hope queue policy (`lib/tools/hope_queue_policy.py`, its contract), `scripts/hope_patrol/`, internal eval infrastructure (`evaluations/{swebench/yourprovider_sandbox,terminal_bench,owner_memory}`), MLP host recognition (Android `ServerUrl.kt`), `lib/private_hosts.py` defaults, model-brand and canvas-link tables, the browser extension's Credit-Hub telemetry | their domain owners, through the same registries | extends the boundary test's scanned roots to `evaluations/`, `scripts/`, `browser_extension/`, `android/` |
| 7 | Physical split: move `bundles/yourprovider/` and `packages/tofu-mtsso-auth/` to the `tofu-yourprovider` repo and install it (`pip install -e`); its `pyproject.toml` already declares the entry points | owner approves; public-core Worker executes | `OVERLAY_PATHS` leave the tree |
| 8 | Publication becomes verification (D6); delete the rewrite patterns once the ledgers are empty; publish a fresh-history export after the audit's rotation and ref clean-up | owner (visibility, license review, audit §7); export owner | removes `export_pkg` rewriting |

## Open questions

- The `yourprovider` provider id (fresh install of the YourProvider template) bills FRIDAY
  Claude pricing but never ran the keyword sanitizer, while `example-corp*` did. The
  profile keeps that exact legacy behavior. If the gateway filters every
  account, `friday_keyword_filter.json` should list `yourprovider` too. That is an
  owner/overlay decision; no code change is needed.
- License and company open-source review (D7).
- Whether colleagues install from the internal PyPI index or a git URL (D6);
  both work with entry points.
