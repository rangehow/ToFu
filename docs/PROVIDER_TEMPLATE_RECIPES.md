# Provider offering recipes

**Owner:** `lib/provider_template_recipes.py`
**Machine contract:** `contracts/provider_offering_recipe_v1.schema.json`
**Bundled sources:** `lib/model_info/data/*.json` (package catalog), then
`static/provider_templates/*.json` (deployment overrides — wins by key)

The catalog is hot-reloaded on every read: editing or dropping in a template
file takes effect on the next picker/API call without a restart. A
transiently invalid file (mid-edit, malformed drop-in) is skipped with a
warning and never takes the rest of the catalog down.

Provider templates are first-run hints for provider accounts. They are not an
authority for logical models or runnable routes. The owner-scoped
`tofu.model-routing/v2` aggregate owns Providers, ProviderAccess resources,
Offerings, Deployments, and credentials.

## Authored shape

```json
{
  "recipe_version": "tofu.provider-offering-recipe/v1",
  "key": "example-relay",
  "name": "Example Relay",
  "base_url": "https://relay.example/v1",
  "offering_recipes": [
    {
      "model_id": "shared-model",
      "request_ids": ["vendor/shared-model-v2"],
      "capabilities": ["text", "thinking"],
      "rpm": 60,
      "pricing": {
        "input": 1.0,
        "output": 4.0,
        "currency": "USD"
      }
    }
  ]
}
```

`model_id` is the requested identity suggested during setup. A
provider-specific spelling belongs in `request_ids`. The authenticated probe
remains authoritative for what the account actually serves; unconfirmed names
enter v2 as provider-scoped pending identities and cannot be fuzzy-merged into
an official logical model.

A provider can have at most one offering recipe for a logical `model_id`.
Multiple wire deployments for that offering belong in its ordered
`request_ids` pool.

## Dynamic feed

**Owner:** `lib/provider_template_feed.py`
**Cache:** `data/config/provider_template_feed.json` (machine-written; holds
no secrets and inherits the `data/` export exclusion)
**Spec:** `tests/test_provider_template_feed.py`

The authored catalog rots one release behind each vendor. A background
refresh folds the PUBLIC OpenRouter model catalog
(`https://openrouter.ai/api/v1/models`) into the picker: every template key
mapped in `VENDOR_PREFIX_BY_TEMPLATE` gains candidate offering recipes for
vendor models the authored file does not cover yet.
`load_provider_templates()` merges the cached feed on every read and kicks a
one-flight daemon refresh when the cache is missing or older than the TTL
(default 24 h, `TOFU_TEMPLATE_FEED_TTL_S`). The picker never blocks on the
network; any fetch/parse/write failure keeps the authored catalog exactly as
shipped.

Safety rails:

- Authored recipes win every `model_id` conflict; curated rpm/capabilities
  are never overwritten by the feed.
- `retired_model_ids` on a template is the authored kill-switch — the feed
  never re-adds those ids (DeepSeek's 2026-07-24 aliases stay dead).

  Retirement is global: no shipped template may author an offering whose
  identity any template retires (pinned by
  `tests/test_world_catalog_sync.py::test_no_template_authors_a_retired_identity`),
  and the world-catalogue sync prunes registry rows still carrying a
  retired identity.
- Dated checkpoints (`-0813`, `-20250805`, `-2026-04-01`) and billing
  variants (`:free`, `:batch`) are never imported.
- Feed rows carry catalog context windows and per-token pricing converted to
  per-million USD; tier tags (`cheap`) are re-evaluated from those prices by
  the same pricing authority used everywhere else.
- Only vendor brands whose official API ids match the OpenRouter `vendor/`
  spelling are mapped. Serving clouds, aggregators, and mismatched prefixes
  stay authored-only (the module docstring lists the deliberate omissions).
- `TOFU_TEMPLATE_FEED=0` disables refresh and merging (the test suite
  defaults to this). The stdlib bootstrap picker never fetches and keeps
  serving the authored catalog.

Do not confuse this layer with provider catalog sync (Lifecycle, step 6):
the feed keeps the ONBOARDING catalog current from public data before any
credential exists; catalog sync keeps a CONFIGURED provider current from its
authenticated `/models`.

## Bootstrap projection

`normalize_provider_template()` reads the v1 recipe and accepts old top-level
`models` only at the import boundary. New bundled files author
`offering_recipes` exclusively. The stdlib-only repair launcher derives a
temporary in-memory `models` view because it cannot import application code.
It never persists that view as `server_config.providers`.

After the user saves setup, bootstrap writes the credential to the existing
`.env` boundary and atomically stages a secret-free
`tofu.bootstrap-provider-stage/v1` draft. Once the storage sidecar is ready,
startup imports the draft into the explicit personal-owner v2 repository,
stores the credential through the repository secret channel, and consumes the
draft. This works both before and after v2 has already been activated.

The recipe compiler uses `lib.model_registration.normalize_model_entry`, so it
removes the obsolete blended `cost` hint, validates capabilities, context and
billable pricing, preserves unknown canonical registration fields, and never
activates process-global pricing or dispatch registries.

The settings UI reads the same catalog through
`GET /api/v1/providers/templates` and asks
`POST /api/v1/providers/templates/compile` for a selected, secret-free v2
bundle. Compilation is non-writing. The client stages the bundle under the
current revision and sends API-key material only through the encrypted
credential-secret envelope when the user explicitly saves.

## Lifecycle

1. The stdlib loader projects a bounded recipe list and performs authenticated
   model discovery when possible.
2. Bootstrap atomically stages transport facts without credential material.
3. Startup activates an empty owner v2 authority when no legacy route exists,
   or completes the one-way legacy import when it does.
4. The staged provider replaces only its deterministic Provider-owned v2
   resources through bounded CAS; user policy fields survive refresh.
5. Successful import deletes the stage. A missing credential leaves it intact
   for an idempotent retry.
6. After setup, `lib/llm_dispatch/provider_catalog_sync.py` keeps the
   configured provider current: on a bounded cadence it re-lists the
   provider's authenticated `/models` and CAS-appends NEW wire identities as
   pending offerings. The pass is strictly add-only (existing rows —
   including user-disabled offerings — are never modified or retired),
   remembers user-deleted synced models in a per-owner dismissed ledger so
   they never resurrect, skips aggregator-scale catalogs (>200 ids)
   wholesale, and caps one pass at 32 imports per provider. A disabled
   provider access is still synced — its switch gates routing, not
   catalog freshness, so re-enabling never reveals a stale list (a
   disabled connection or credential, a per-component revocation, stays
   untouched). It rides the shared local-endpoint monitor thread;
   `TOFU_MODEL_CATALOG_SYNC=0` opts out.
7. Independently, `lib/model_routing/template_supply.py` converges every
   INSTALLED access that fingerprint-matches a template toward a fresh
   install of that template, so a card added last month picks up recipes
   shipped in a later template revision (e.g. gemini-3.8-flash on the
   Meituan gateway, whose empty `/models` listing gives the catalog sync
   nothing to discover). Fingerprint match requires every template base
   URL (top-level + protocol faces) among the access's connections AND an
   existing relation (provider brand match or ≥1 shared recipe identity),
   so a hand-built minimal access to the same host is never flooded. A
   missing recipe lands only after a live probe on the access's own
   credential proves the gateway serves it (`ok` / `rate_limited`, routed
   per recipe through `build_probe_work` so Claude candidates probe the
   Anthropic face); `ok` deployments arrive enabled+passed, rate-limited
   ones disabled+unprobed, and dead ids never resurrect. New rows reuse
   the compiled bundle verbatim with ids re-derived for the target access
   and credential authorization extended to the new confirmed models.
   Strictly insert-only; a converged access makes later passes no-ops.
   It rides the same shared monitor; `TOFU_TEMPLATE_SUPPLY_SYNC=0` opts
   out.

## Migration guardrails

- Keep `recipe_version` explicit.
- Do not author both `offering_recipes` and `models`.
- Do not use `cost`; register `pricing.input` and `pricing.output` per million
  tokens with an explicit USD/CNY authority currency.
- Preserve the provider protocol and exact wire IDs; they belong to v2
  Connections and Deployments, not the logical model.
- Verify changes with `tests/test_provider_template_recipes.py` and the model
  routing bootstrap/strict-failover tests.
