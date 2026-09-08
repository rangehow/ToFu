# Model-routing v2 contract

The machine authority for models, service providers, access, and runtime route
selection is
[`contracts/model_routing_v2.schema.json`](../contracts/model_routing_v2.schema.json).
`lib/model_routing/` validates and persists one revisioned
`tofu.model-routing/v2` aggregate per owner. There is no configured `Route`,
provider model array, alias pool, or separate BYO routing authority.

## Entity meanings

```text
Creator ── Model
              ↑
Provider ── ProviderAccess ── Offering
                 ├─ Connection └─ Deployment → wire_model_id
                 └─ Credential ── authorizes Connection / Model
```

| Entity | Identity and responsibility |
|---|---|
| `Creator` | Public creator identity and display metadata. |
| `Model` | One official model, unique by `(creator_id, model_id)`. Owns official capabilities, list price, context limit, lifecycle, and quality rank. Preview and dated snapshots remain distinct models. |
| `Provider` | A service provider. Public rows are shared metadata; owner-scoped rows represent private services without weakening owner isolation. |
| `ProviderAccess` | The owner's one access pool for one Provider. Owns enablement and account-level quota/balance policy. |
| `Connection` | One protocol endpoint inside a ProviderAccess. Region and gateway namespace are connection metadata, not model identity. |
| `CredentialMetadata` | Redacted credential kind, hint, quota policy, and authorization over Connections and official Models. Plaintext is stored only by the repository's encrypted secret operation. |
| `Offering` | What one ProviderAccess actually supplies: confirmed or pending identity, actual capability subset, actual context, transaction price, priority, stale state, and enablement. |
| `Deployment` | Exactly one Offering on one Connection under one real upstream `wire_model_id`, with independent probe and identity-confidence state; optional `max_output_tokens` records the concrete route's verified output ceiling. |

One provider-scoped wire ID may identify only one Deployment. Every distinct
wire ID is a distinct Deployment; `aliases` and `request_ids` are rejected in
v2 documents. A pending identity is reachable only by an explicit
`{provider_id, offering_id}` selection and is never a cross-provider route.
Output ceilings are applied after slot selection. Provider-error learning is
scoped by Provider, Offering, Deployment, and protocol rather than globally by
model name; exclusive upper bounds normalize to the largest accepted integer
before one same-route retry.

## Prices, capabilities, and limits

Official `Model` facts and actual `Offering` facts are deliberately separate.
An Offering's capabilities must be a subset of its confirmed Model's
capabilities, and its effective context must not exceed the official limit.

Managed pricing-tier tags (`cheap`, owned by
`lib/llm_dispatch/config/_pricing.py`) are exempt from the subset check:
they derive from each provider's own price book and are re-synced from live
prices, so a reseller undercutting the creator's list price legitimately
carries a tier the official Model row lacks.
Prices use `{input, output, currency, unit}` with optional cache rates;
`unit` is `per_million_tokens`. A task price budget is a hard filter—missing or
currency-mismatched actual pricing does not pass a requested limit.

Account RPM, TPM, daily token allowance, and balance belong to
ProviderAccess/Credential quota policy. They are not copied into a Model or
used as model identity.

`lib/model_info/capability_taxonomy.py` remains the vocabulary authority for
capability tags and publishes the taxonomy through `/api/v1/capabilities`.
Adding a tag updates that one taxonomy and its parity test; routing consumes
the Offering subset and does not maintain another list.

## Selection and computed routes

Native chat and agent requests use a structured model reference:

```json
{
  "model": {"creator_id": "openai", "model_id": "gpt-x"},
  "routing": {
    "preferred_provider_id": "provider-a",
    "required_context": 131072,
    "required_capabilities": ["text", "thinking"],
    "price_budget": {
      "max_input": 2.0,
      "max_output": 8.0,
      "currency": "USD"
    }
  }
}
```

`preferred_provider_id` is a stable preference, not a hard Connection or
Deployment pin. Pending identity selection instead uses
`{"provider_id":"provider-a","offering_id":"offering-x"}` and cannot
escape that Provider.

OpenAI/Anthropic-compatible requests retain their standard string `model`.
They may add `tofu.creator_id` and `tofu.preferred_provider_id` (or the
documented top-level extension aliases). A non-unique string returns
`model_selector_ambiguous` with candidates. `model@provider` always returns
`legacy_model_selector_removed`; it is never guessed.

The candidate compiler applies owner authorization, enabled/stale/pending
state, capabilities, context, protocol, cooldown, quota, and price gates. It
then orders by preferred Provider, operator priority, health, cache affinity,
connection/deployment priority, latency, and actual price. Cross-provider
failover occurs only after the preferred Provider is unavailable or fails.
After all routes for an official Model fail, a compatible higher-quality
fallback Model may be considered under the same hard request policy, preferring
the original Provider.

The dispatcher snapshots compiled candidates into its slot pool at
initialization. Every Settings mutation that commits the authority — the
model-routing CAS replace behind the provider template card, ProviderAccess
create/replace/delete, credential secret rotation, and migration commit —
rebuilds that pool in-process, so a saved disable/enable takes effect on
the next pick without a restart; the health sweep, template supply,
catalogue sync, and local-serve registration do the same after their own
authority writes. Executable specification:
`tests/test_api_v1_model_routing_contract.py` (`DispatcherHotReloadTest`).

## Health and traceability

Configured enablement and runtime health are separate. Route-missing affects a
Deployment; network failures affect a Connection; 401/402 affects a
Credential; 403 affects the Credential×Deployment authorization. Health rows,
probe work, candidate slots, and snapshots are bounded and expire through
their owning lifecycle.

The concrete personal-computer budget is: one owner aggregate is at most
8 MiB with per-entity ceilings (256 Providers/ProviderAccesses, 512
Connections, 1,024 Credentials, 4,096 Models/Offerings, and 8,192
Deployments); one request mints at most 64 ephemeral route slots and disposes
them at request teardown. Runtime health retains at most 4,096 rows for one
hour (the diagnostic projection returns at most 256). The shared optional
probe lane admits at most 4,096 cells per task, 4–32 queued tasks, at most
eight live cell workers, a 120-second timeout, and five attempts. A persisted
RouteSnapshot is at most 16 KiB, with at most 32 transitions and 16 degradation
reasons; oldest trace detail is trimmed before persistence, never durable user
state.

Every new turn persists a redacted `RouteSnapshot`: requested structured
identity, Provider preference, actual ProviderAccess/Offering/Deployment/
Connection, credential metadata, wire ID, and transition reasons. Provider or
model failover is appended to the activity timeline without changing the
conversation's saved preference. Old turns are projected with a legacy
snapshot at read time and are not rewritten.

## Detection matrix recommendations

A chat probe's `ok` verdict requires protocol-valid generated output, and reasoning output counts as generated output in every dialect: OpenAI `reasoning_content`/`reasoning` message fields, Anthropic `thinking`/`redacted_thinking` content blocks, Responses `reasoning` output items (id/encrypted payload suffices when the summary never materialized), and Anthropic SSE `thinking_delta`/`signature_delta` events. A 2xx carrying neither text nor reasoning stays `invalid_response`. Probe budgets are deliberately not raised to hide this — the validator must recognize reasoning instead. Executable specification: `tests/test_probe_cells.py`.

A final `rate_limited` (HTTP 429) verdict recommends disabling the credential's
model grant by default, including per-minute/per-hour rate limits. Existing
saved 429 snapshots receive the same recommendation on read. Probe retries
still treat any successful attempt as healthy; this policy does not change
transient 429 handling during ordinary dispatch. Applying recommendations
removes flagged grants only and does not automatically re-enable healthy
cells. Executable specification: `tests/test_probe_rate_limit_defaults.py`.

## Repository and migration

All reads and writes pass an explicit owner/tenant boundary to the repository.
The ProviderAccess aggregate is replaced with revision compare-and-swap;
credential secret writes use a separate encrypted operation. SQLite details,
filesystem paths, and single-user globals do not cross that layer.

The one-way importer converts legacy faces/endpoints, API keys, OAuth/local
identity, provider models, key access, and every alias/request ID into v2
entities. It first emits a redacted plan and backup, validates counts,
references, secrets, and candidate routes, then switches the authority in one
CAS commit. A rejected or failed plan stores a recovery receipt and leaves v2
inactive. After activation, legacy rows are migration input only: there is no
dual write or projection back to `providers[].models`.

If an interrupted first cutover left an active v2 aggregate without a
migration marker (for example, OAuth reconciliation committed before the
legacy aggregate could fit in a storage receipt), startup compares exact
legacy Provider identities with the active authority and imports only the
missing ProviderAccess bundles. The merge uses the same validation, encrypted
secret channel, and revision CAS, so resources already written to v2 are
preserved and the repair is idempotent.

## World catalogue sync

The Models settings page is the registry of models that EXIST, not models a
configured provider currently supplies. At startup
`lib/model_routing/world_catalog.sync_world_model_catalog` compiles every
package provider template and inserts the missing Creator/Model rows into the
owner authority. The sync is insert-only and idempotent: provider supply
collections are never touched and existing registry rows (including owner
edits) are never overwritten, so a newly shipped model appears in the catalog
even before any provider bundle carries it. Executable specification:
`tests/test_world_catalog_sync.py`.

The Codex OAuth catalogue request carries a `client_version` that upstream
uses to gate newly released models. `lib/oauth/codex_version.py` resolves it
at fetch time from the npm `@openai/codex` `latest` dist-tag (bounded cache,
floor at the version pinned in `lib/oauth/outbound.py`), so new models reach
the catalogue without a Tofu release; a registry outage degrades to the floor
rather than failing the refresh. Executable specification:
`tests/test_codex_version.py`.
## Browser projection

The browser projects the v2 authority along the same semantic boundary as the
dispatcher:

- The chat picker lists each confirmed `(creator_id, model_id)` once. Its
  secondary control is `Auto Provider` or a preferred Provider; choosing a
  Provider never changes the Model identity. Pending identities remain in a
  separate Provider-scoped section.
- Settings has separate **Models** and **Providers** pages. Models is a
  read-only Creator/Model catalog for official models: it groups Models by
  Creator and may project Model-owned quality rank and list pricing, but it
  never reads ProviderAccess, Offering, Deployment, wire identifier, alias, or
  route state. The catalog renders one row per semantic Model: provider
  namespaces (``kimi/kimi-k2.5``) and supply-channel variants
  (``Qwen3_8-27B-Meituan``, FP8 re-registrations, a trailing ``instruct`` tag
  on an otherwise identical registration) are the same Model rented through
  different supply paths, so they collapse into the canonical bare
  registration under its developer, with facts the canonical row lacks
  (pricing, benchmark score) filled from the hidden twins. Near-name twins
  that are genuinely distinct Models never merge: ``gpt-3.5-turbo-instruct``
  (own pricing, 4096 context) is pinned as separate from ``gpt-3.5-turbo``.
  The adjacent Artificial Analysis source is external benchmark
  enrichment keyed by that exact Creator/Model identity; its encrypted
  owner-scoped key, cache status, and score attribution do not create or alter
  a Provider. A future owner-created custom Model uses an explicit editable
  branch. Providers owns the supply projection: each Provider card names its
  canonical Models and may show that Provider's differing wire identifiers as
  aliases. The same exact Model may therefore appear under several Providers.
- The catalog's **Cost × AA Index** Pareto chart plots a Model only when it
  has both an external quality score and an official `list_pricing`
  (3:1 blended input/output USD cost). Scores attach by exact normalized
  identity only: a variant the external source never benchmarked (dated
  snapshot, turbo/highspeed spin, regional supply alias) carries no score
  rather than inheriting the base model's result. Registrations that resolve
  to the same benchmarked model (a bare id and a provider-namespaced id
  matched to the same external row) collapse to a single chart point, keeping
  the priced, canonical registration. Scored-but-unpriced Models are not
  points and are named in the chart note instead. A $0 official price renders
  in a dedicated $0 lane left of the log axis while its tooltip keeps the raw
  value. Markers that
  would overlap (identical or colliding positions) fan out deterministically
  inside the plot bounds; data positions, frontier computation, and tooltips
  are unaffected by the visual spread.
- A Provider can own multiple Credentials. Credential rotation is attempted
  inside the Provider before the dispatcher crosses to another Provider that
  supplies the same Model.
- Connection and wire request identifiers are operational details. They are
  bounded and lazy-rendered only in the Provider manager's connection and route
  diagnostics views; ordinary chat cannot pin either resource.

## Daily provider detection

`Daily Provider Detection` is enabled on personal scheduler startup at `0 8 * * *`
(08:00 server-local time); reconciliation preserves user-disabled state. The
`provider_probe_daily` action delegates to `lib/model_routing/probe_automation.py`
with the row owner's `providers` principal. It probes fresh matrices, including
ungranted cells, through the existing shared lane (no additional cell pool).
One run is capped at 4,096 cells and 30 minutes; cancellation prevents queued
cells from starting. In-flight HTTP calls retain their normal timeout. Actual
probes can incur provider charges, including image generation.
Only complete fresh results remove recommended model grants (including 429s);
healthy cells are not automatically enabled. All changes use one revision CAS:
a failed/incomplete/cancelled run or concurrent configuration edit leaves grants
unchanged. Results remain visible in the matrix cache and scheduler history.
The server must be running at 08:00; missed cron occurrences are not backfilled.
Tests: `tests/test_provider_probe_automation.py`.
