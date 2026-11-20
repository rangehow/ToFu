# Contract guidance

## Scope and authority

This directory contains machine-readable wire and schema authorities. Read
`docs/README.md` and the owning authority document before changing a contract.
Generated clients and projections are consumers, not alternative sources of
truth.

Authority by API surface:

- **v4 / conversation-sync / events / domain schemas**: the YAML/JSON files
  here are canonical; generated clients and fixtures are outputs.
- **HTTP surface**: `api_v1.yaml` is canonical for every registered
  first-party HTTP/WebSocket operation (OpenAPI operation plus its
  `x-tofu-route` record). Handler `@api_meta` declarations implement it and
  `scripts/gen_api_v1_contract.py` rejects drift in either direction
  (`lib/openapi/AGENTS.md`). A handler's source directory name does not select
  the release major. `api_endpoint_index_v1.json` is derived from `api_v1.yaml`
  by `tofu-xtask contracts` (`make contracts`); the `v1` in both names
  identifies the document schema.
- **events**: `events_v1.json` is canonical for stream events, phases and push
  frames; `lib/agent_core/events.py` implements it (`gen_event_contract.py`
  checks). The clients derived from `api_v1.yaml`, `events_v1.json` and
  `storage_operations_v1.json` are rendered by `packages/tofu-xtask`
  (`make contracts`; `make contracts-check` rejects stale outputs).
- **storage operations**: `storage_operations_v1.json` is authored; the sidecar
  registry implements it (`gen_storage_operation_catalog.py` checks).
- **environment knobs**: `config_knobs_v1.json` is the complete inventory of
  env configuration reads. Adding or renaming an env read requires updating
  it via `python3 scripts/check_config_knobs.py --rescan` in the same change;
  new knobs must enter as `documented` (owner + description), never `legacy`.
  The scan counts only process-environment reads (`os.environ`, `os.getenv`
  and the helpers recognized in `scripts/check_config_knobs.py`); a literal
  key read from a mapping parameter is not a knob read. When a refactor makes
  a live knob "no longer read anywhere", make its owning helper read
  `os.environ` again; the suggested `--rescan` would drop a live knob.

## Editing rules

- Change the canonical schema first, then run its committed generator. Never
  patch a generated consumer to conceal contract drift.
- Preserve compatibility deliberately. A breaking removal or rename requires
  explicit migration, versioning, and failure behavior rather than permissive
  parsing scattered across consumers.
- Keep identity, ownership, bounds, nullability, defaults, and error shapes
  explicit and machine-checkable.
- Update every server, frontend, SDK, documentation, and fixture consumer in
  the same change. Delete superseded generated output.
- Add the contract and its guard to `docs/catalog.json` when introducing a new
  JSON/YAML authority.

## Verification

Run the focused generator in check mode and its cataloged guard test. Common
checks are `npm run check:conversation-sync`, `npm run check:api-v4`,
and `pytest -q tests/identity/test_identity_contract.py`. Finish with
the neighboring API/frontend contract gate for changed public wire behavior.

`native_aggregate_cost_v1` describes original Goal credits without settlement
authority. Generate standalone schemas via gen_native_checkpoint_control; verify
aggregate fee tests plus native Goal claim/consumption and passive replay.
