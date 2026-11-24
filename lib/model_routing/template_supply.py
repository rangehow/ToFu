"""Converge installed provider accesses with their source templates.

Provider templates are curated supply lists (for the YourProvider gateway:
ModelPlaza ∩ live probe), but they only seed NEW installs — a card added
last month never sees newly listed models, and gateways without a usable
``/models`` listing (your-llm-gateway.example.com returns an empty list) give
``provider_catalog_sync`` nothing to discover. This sync closes the gap:
riding health_local's shared monitor, it periodically converges every
installed access that fingerprint-matches a template toward a fresh
install of that template. Opt out with ``TOFU_TEMPLATE_SUPPLY_SYNC=0``.

Semantics:

* **Fingerprint match** — every template base URL (top-level + protocol
  faces) must appear among the access's connections, AND the access must
  already relate to the template (provider brand match or ≥1 shared
  recipe model), so a hand-built minimal access to the same host is never
  flooded.
* **Probe-gated** — a missing recipe is added only when a live probe on
  the access's own credential proves the gateway serves it (``ok`` /
  ``rate_limited``, the same served verdicts template curation uses),
  routed per recipe through ``build_probe_work`` so a Claude candidate is
  probed on the Anthropic face exactly like dispatch would, and each cell
  carries only its own face Connection's headers plus the credential
  envelope (a face selector is rejected on any other face). Ids the owner
  cleaned up because they probed dead never resurrect; transient probe
  failures simply retry on the next boot.
* **Insert-only for supply** — existing offerings, credentials and owner
  edits are never modified or removed; a recipe the access already serves
  is never re-added. New rows reuse the compiled template bundle verbatim
  (identity, capabilities, context window) with ids re-derived for the
  target access and deployments re-pointed at the access's own
  connections. Confirmed model references are appended to the access
  credentials' authorization so dispatch can actually route them.
* **Face-alias backfill** — the one exception: when a recipe later pins a
  wire id to a serving-selector face (``request_faces``, e.g. aigc's
  kunlun), a card installed before that pin lacks both the face
  Connection and the alias Deployment. ``_backfill_request_faces`` adds
  exactly those onto the existing Offering (creating the missing face
  Connection deterministically) so dispatch reaches the selector wire.
  Without it the new face URL also breaks the template fingerprint and
  the whole sync would skip the access. Existing wires, connections and
  credential exclusions stay untouched.
  Registry model rows converge upward (capabilities union, maximum
  context window) — the same convergence the domain alias merge applies
  to survivor rows — so a transplanted offering never out-claims the
  official row it validates against.
* **Optimistic concurrency** — the commit re-reads and recomputes after
  every revision conflict instead of failing the sweep, and a pass that
  changes nothing commits no revision at all. Every monitor used to CAS a
  no-op document, which woke the sibling monitors into another no-op CAS
  and logged 20 `aggregate changed concurrently` ERRORs in one window.
  The "no routable connection" verdict is likewise reported once per
  (access, recipe) instead of once per row per pass.
"""

from __future__ import annotations

import copy
import hashlib
import os
import threading
from typing import Any

from lib.log import audit_log, get_logger

from lib.model_routing.domain import ModelRoutingError
from lib.provider.provider_headers import merge_header_layers

logger = get_logger(__name__)

_SERVED_VERDICTS = frozenset({'ok', 'rate_limited'})
_PROBE_WORKERS = 8
_PROBE_TIMEOUT = 20

__all__ = [
    'poll_if_due',
    'probe_cell_connection_headers',
    'recipe_probe_entry',
    'start_template_supply_sync',
    'stop_template_supply_sync',
    'sync_template_supply',
    'trigger_template_supply_sync',
]


def _env_seconds(name: str, default: float, minimum: float,
                 maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_BOOT_DELAY = _env_seconds(
    'TOFU_TEMPLATE_SUPPLY_SYNC_BOOT_DELAY', 240.0, 30.0, 3600.0)
_SYNC_INTERVAL = _env_seconds(
    'TOFU_TEMPLATE_SUPPLY_SYNC_INTERVAL', 6.0 * 3600.0, 900.0, 86400.0)

_schedule_lock = threading.Lock()
_runtime_enabled = True
_next_sync_at: float | None = None
_force_sync = False
_schedule_generation = 0


def _disabled() -> bool:
    return os.environ.get('TOFU_TEMPLATE_SUPPLY_SYNC', '').strip() == '0'


def _norm_url(value: Any) -> str:
    return str(value or '').strip().rstrip('/')


def _template_urls(template: dict) -> set[str]:
    urls = {_norm_url(template.get('base_url'))}
    faces = template.get('faces') or {}
    if isinstance(faces, dict):
        for face in faces.values():
            if isinstance(face, dict):
                urls.add(_norm_url(face.get('base_url')))
    return {url for url in urls if url}


def _access_connections(document: dict, access_id: str) -> list[dict]:
    return [
        row for row in document.get('connections') or []
        if isinstance(row, dict)
        and row.get('provider_access_id') == access_id
    ]


def _gateway_namespace(row: dict) -> str:
    """The template face that created this Connection; '' for an owner one."""
    return str(row.get('gateway_namespace') or '').strip()


def _recipe_keys(recipe: dict) -> frozenset:
    from lib.model_catalog._creator_identity import merge_keys

    keys = set(merge_keys(recipe.get('model_id')))
    for wire in recipe.get('request_ids') or []:
        keys.update(merge_keys(wire))
    return frozenset(keys)


def _known_keys(document: dict, access_id: str) -> frozenset:
    """Every model identity the access already serves, in any state."""
    from lib.model_catalog._creator_identity import merge_keys

    keys: set = set()
    for row in document.get('offerings') or []:
        if not isinstance(row, dict):
            continue
        if row.get('provider_access_id') != access_id:
            continue
        model = row.get('model')
        if isinstance(model, dict):
            keys.update(merge_keys(model.get('model_id')))
        pending = str(row.get('pending_model_id') or '').strip()
        if pending:
            keys.update(merge_keys(pending))
    return frozenset(keys)


def _wire_face_key(wire: str, connection: dict, headers=None) -> tuple:
    from lib.model_routing.migration import _protocol

    return (wire, _norm_url(connection.get('base_url')),
            _protocol(connection.get('protocol')),
            tuple(sorted((name.lower() if name.isascii() else name, value)
                         for name, value in merge_header_layers(
                             connection.get('extra_headers') or {}, headers or {}).items())))


def _probe_transport_key(wire: str, url: str, protocol: str, headers: dict) -> tuple:
    # The synchronous HTTP transport collapses header names in insertion
    # order. Digest every effective routing value, including unknown headers:
    # an explicit credential-envelope header can route just like a face header.
    return _wire_face_key(wire, {
        'base_url': url, 'protocol': protocol,
        'extra_headers': {
            name: hashlib.sha256(str(value).encode('utf-8', 'surrogatepass')).hexdigest()
            for name, value in headers.items()},
    })


def _known_wire_faces(document: dict, access_id: str) -> set[tuple]:
    connections = {row.get('connection_id'): row
                   for row in _access_connections(document, access_id)}
    return {
        _wire_face_key(str(row['wire_model_id']).strip(),
                       connections[row['connection_id']], row.get('extra_headers'))
        for row in document.get('deployments') or []
        if isinstance(row, dict) and row.get('connection_id') in connections
        and str(row.get('wire_model_id') or '').strip()
    }


def _missing_probe_recipes(document: dict, access: dict, template: dict) -> list[dict]:
    from lib.llm_dispatch.provider_face import resolve_face

    access_id = access['provider_access_id']
    seen = _known_wire_faces(document, access_id)
    base_url, protocol, faces, headers = _probe_plan(document, access, template)
    provider = {'base_url': base_url, 'protocol': protocol, 'faces': faces,
                'extra_headers': headers}
    missing = []
    for recipe in template['offering_recipes']:
        wires = recipe.get('request_ids') or [recipe['model_id']]
        missing_wires = []
        for wire in wires:
            resolution = resolve_face(provider, recipe, wire_id=wire)
            if not resolution.ok:
                continue
            cell = _wire_face_key(wire, {
                'base_url': resolution.base_url, 'protocol': resolution.protocol,
                'extra_headers': resolution.extra_headers})
            if cell not in seen:
                missing_wires.append(wire)
                seen.add(cell)
        if missing_wires:
            missing.append(recipe if missing_wires == wires else {
                **recipe, 'request_ids': missing_wires})
    return missing


def _find_template(document: dict, access: dict,
                   templates: list[dict]) -> dict | None:
    """First template whose URLs the access fully hosts and which the
    access provably relates to (brand or ≥1 shared recipe identity)."""
    access_id = access['provider_access_id']
    access_urls = {
        _norm_url(row.get('base_url'))
        for row in _access_connections(document, access_id)
    } - {''}
    if not access_urls:
        return None
    provider = next(
        (row for row in document.get('providers') or []
         if row.get('provider_id') == access.get('provider_id')),
        {},
    )
    brand = str(provider.get('brand') or '').strip()
    known = _known_keys(document, access_id)
    for template in templates:
        template_urls = _template_urls(template)
        if not template_urls:
            continue
        # A face reached ONLY through a recipe's ``request_faces`` selector
        # (e.g. aigc's kunlun) may be absent from an older card: backfill is
        # what creates it, so it must not disqualify the match. Require every
        # other template URL (the default face and protocol faces) to be
        # hosted — that is what keeps a foreign same-host access unflooded.
        selector_urls = {
            _norm_url((template.get('faces') or {}).get(face, {}).get('base_url'))
            for recipe in template.get('offering_recipes') or []
            for face in (recipe.get('request_faces') or {}).values()
        } - {''}
        required_urls = template_urls - selector_urls
        if not required_urls or not required_urls <= access_urls:
            continue
        recipes = template.get('offering_recipes') or []
        if not recipes:
            continue
        if brand and brand == str(template.get('brand') or '').strip():
            return template
        if any(_recipe_keys(recipe) & known for recipe in recipes):
            return template
    return None


def _resolve_api_key(repository, boundary, document: dict,
                     access_id: str) -> tuple[str, dict]:
    """First enabled credential secret of the access → (api_key, headers)."""
    from lib.model_routing.dispatch_adapter import decode_credential_secret

    for credential in document.get('credentials') or []:
        if not isinstance(credential, dict):
            continue
        if credential.get('provider_access_id') != access_id:
            continue
        if not credential.get('enabled'):
            continue
        reference = str(credential.get('secret_reference') or '')
        if not reference:
            continue
        plaintext = repository.resolve_secret(boundary, reference)
        api_key, _oauth, headers = decode_credential_secret(
            plaintext, kind=str(credential.get('kind') or 'api_key'))
        if api_key:
            return api_key, dict(headers or {})
    return '', {}


def recipe_probe_entry(recipe: dict) -> dict:
    """``build_probe_work`` model entry for one template recipe.

    Its wire pool and face pins (``face`` / ``request_faces``) choose each
    wire's endpoint exactly as ``resolve_face`` does for the Deployment the
    recipe installs.
    """
    return {
        'model_id': recipe['model_id'],
        'capabilities': [],
        **({'request_ids': list(recipe['request_ids'])}
           if recipe.get('request_ids') else {}),
        **{field: recipe[field] for field in ('face', 'request_faces')
           if recipe.get(field)},
    }


def probe_cell_connection_headers(item: tuple, *, default_url: str,
                                  default_face_headers: dict | None) -> dict:
    """Connection headers one ``build_probe_work`` cell may send.

    A face selector (aigc's ``x-context-provider: kunlun``) is rejected on
    every other face (400 该模型在 provider 下无可用账号), so a cell carries
    only its own face's headers: a declared face's arrive in the cell
    context, ``default_face_headers`` reach only cells sent to
    ``default_url``, and a face that declares none sends none.
    """
    if len(item) > 7:
        return dict(item[7].get('extra_headers') or {})
    if _norm_url(item[5]) == _norm_url(default_url):
        return dict(default_face_headers or {})
    return {}


def _live_prober(*, base_url: str, protocol: str, faces: dict,
                 api_key: str, extra_headers: dict,
                 recipes: list[dict],
                 default_face_headers: dict | None = None,
                 inference_profile: dict | None = None) -> dict[str | tuple, str]:
    """Return recipe admission verdicts and exact per-cell serving evidence.

    ``extra_headers`` are the credential envelope's headers and ride every
    cell. Connection headers stay per face: a declared face's arrive in its
    cells' ``build_probe_work`` context, and ``default_face_headers`` reach
    only cells sent to the default endpoint. Exact evidence requires the
    final prepared values of all explicit headers; unobserved requests
    can inform recipe admission but cannot pass a deployment.

    ``inference_profile`` is the template's provider-level declaration. It
    supplies carriers every face inherits (for example OpenCode Go's required
    session header), so a probe answers the same wire contract as dispatch.
    """
    from lib.provider.provider_probe import build_probe_work, probe_one_cell

    provider = {'base_url': base_url, 'protocol': protocol, 'faces': faces,
                'inference_profile': dict(inference_profile or {})}
    models = [recipe_probe_entry(recipe) for recipe in recipes]
    work = build_probe_work(provider, models, [api_key])
    verdicts: dict[str | tuple, str] = {}

    from lib.llm.cache_affinity import probe_affinity_headers
    from lib.observability import InstrumentedThreadPoolExecutor
    with InstrumentedThreadPoolExecutor(max_workers=_PROBE_WORKERS,
            metric_pool='template-supply', thread_name_prefix='template-supply') as pool:
        futures = []
        for item in work:
            _idx, key, root, wire, _caps, url, proto = item[:7]
            face_headers = probe_cell_connection_headers(
                item, default_url=base_url, default_face_headers=default_face_headers)
            cell_profile = ((item[7].get('inference_profile') or {})
                            if len(item) > 7 else {})
            # As in dispatch, credential headers override face headers, and a
            # gateway-required session header rides the same declared carrier.
            sent_headers = {
                **probe_affinity_headers(cell_profile or inference_profile,
                                         protocol=proto),
                **merge_header_layers(face_headers, extra_headers),
            }
            evidence = []

            def observe(headers, evidence=evidence, root=root, wire=wire, url=url, proto=proto):
                evidence.append((root, *_probe_transport_key(wire, url, proto, headers)))
            try:
                future = pool.submit(probe_one_cell, url, key, wire, sent_headers,
                                     _PROBE_TIMEOUT, protocol=proto,
                                     on_prepared_headers=observe)
                futures.append((root, evidence, future))
            except Exception:
                # This cell never started. No serving evidence exists, but
                # accepted sibling probes retain ownership and still finish.
                verdicts.setdefault(root, 'unknown')
        for root, evidence, future in futures:
            try:
                verdict, _detail = future.result()
            except Exception as diagnostic_error:
                logger.debug('_live_prober failure handled: %s', type(diagnostic_error).__name__)
                verdict = 'unknown'
            if len(evidence) == 1 and verdicts.get(evidence[0]) != 'ok':
                verdicts[evidence[0]] = verdict
            previous = verdicts.get(root)
            if previous in _SERVED_VERDICTS:
                continue
            if verdict in _SERVED_VERDICTS or previous is None:
                verdicts[root] = verdict
    return verdicts


def _probe_plan(document: dict, access: dict,
                template: dict) -> tuple[str, str, dict, dict]:
    """(base_url, protocol, faces, default_face_headers) for this access.

    Headers are never unioned across the access, and a ``gateway_namespace``
    Connection belongs to its face alone, whatever the Connection order.
    Default-endpoint cells take the namespace-less (owner) Connection at the
    template's base URL, else one at no declared face URL, else the
    template's declared endpoint with no Connection headers. Each face
    carries the headers of its Connection at the face's declared URL (the
    one namespaced to it, else an unclaimed owner one) plus the selector the
    template declares for it (backfill creates and converges template face
    Connections with exactly that selector).
    """
    access_id = access['provider_access_id']
    connections = _access_connections(document, access_id)
    template_base = _norm_url(template.get('base_url'))
    raw_faces = template.get('faces') or {}
    declared_faces = {
        name: face for name, face in raw_faces.items() if isinstance(face, dict)
    } if isinstance(raw_faces, dict) else {}
    face_urls = {_norm_url(face.get('base_url'))
                 for face in declared_faces.values()}
    owner_rows = [row for row in connections if not _gateway_namespace(row)]
    primary = (
        next((row for row in owner_rows
              if _norm_url(row.get('base_url')) == template_base), None)
        or next((row for row in owner_rows
                 if _norm_url(row.get('base_url')) not in face_urls), None))

    faces: dict[str, dict] = {}
    for name, face in declared_faces.items():
        face_url = _norm_url(face.get('base_url'))
        at_url = [row for row in connections
                  if _norm_url(row.get('base_url')) == face_url]
        matched = (
            next((row for row in at_url
                  if _gateway_namespace(row) == name), None)
            or next((row for row in at_url
                     if not _gateway_namespace(row) and row is not primary),
                    None)
            or {})
        faces[name] = {
            **face,
            'base_url': (matched or face).get('base_url'),
            'extra_headers': merge_header_layers(matched.get('extra_headers') or {},
                                                 face.get('extra_headers') or {}),
        }

    if primary is None:
        return (template_base, str(template.get('protocol') or 'openai'),
                faces, {})
    return (
        _norm_url(primary.get('base_url')),
        str(primary.get('protocol') or 'openai'),
        faces,
        dict(primary.get('extra_headers') or {}),
    )


def _supersede_renamed_supply(working: dict, access: dict, template: dict,
                              stats: dict, previous: dict | None = None) -> bool:
    """Retire offerings whose whole wire pool a renamed recipe now owns.

    A logical id is renamed by authoring the SAME gateway spellings under the
    new identity (yourprovider.json: `deepseek-v4-flash` → `deepseek-v4.1-flash`
    with the v4-flash mirrors kept in its request_ids). Insert-only
    convergence then leaves the previous offering alive forever, so the model
    picker offers one gateway supply twice under two model identities, and
    both stay dispatchable.

    A replacement must have eligible exact recipe deployments on the affected
    faces, and its recipe must author the old offering's entire wire/face pool.
    A spelling, identity alias, or unrelated deployment is not replacement
    supply. Plan against one snapshot; an offering being replaced cannot be
    used as a replacement. Keep retired rows and their existing evidence.
    """
    from lib.model_catalog._creator_identity import merge_keys
    from lib.llm_dispatch.provider_face import resolve_face

    access_id = access['provider_access_id']
    base_url, protocol, faces, headers = _probe_plan(working, access, template)
    provider = {'base_url': base_url, 'protocol': protocol, 'faces': faces,
                'extra_headers': headers}
    authored: list[tuple[str, frozenset, set[tuple]]] = []
    for recipe in template.get('offering_recipes') or []:
        model_id = str(recipe.get('model_id') or '').strip()
        wires = {
            str(wire).strip()
            for wire in (recipe.get('request_ids') or [model_id])
            if str(wire).strip()
        }
        cells = set()
        for wire in wires:
            resolution = resolve_face(provider, recipe, wire_id=wire)
            if resolution.ok:
                cells.add(_wire_face_key(wire, {
                    'base_url': resolution.base_url, 'protocol': resolution.protocol,
                    'extra_headers': resolution.extra_headers}))
        if model_id and cells:
            authored.append((model_id, merge_keys(model_id), cells))
    if not authored:
        return False

    connections = {row['connection_id']: row
                   for row in _access_connections(working, access_id)}
    wires_by_offering: dict[str, set[tuple]] = {}
    for deployment in working.get('deployments') or []:
        if not isinstance(deployment, dict):
            continue
        if deployment.get('connection_id') not in connections:
            continue
        wire = str(deployment.get('wire_model_id') or '').strip()
        if wire:
            wires_by_offering.setdefault(
                deployment['offering_id'], set()).add(_wire_face_key(
                    wire, connections[deployment['connection_id']],
                    deployment.get('extra_headers')))

    active = [row for row in working.get('offerings') or []
              if isinstance(row, dict) and row.get('provider_access_id') == access_id
              and row.get('stale') is not True and row.get('enabled') is not False
              and row.get('identity_state') == 'confirmed'
              and wires_by_offering.get(row['offering_id'])]
    return _retire_replaced_offerings(working, access_id, active, authored,
                                      wires_by_offering, stats, previous)


def _retire_replaced_offerings(working, access_id, active, authored,
                                wires_by_offering, stats, previous) -> bool:
    from lib.model_catalog._creator_identity import merge_keys
    from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
    from lib.model_routing.routing import RouteCandidateCompiler

    installed = {}
    for row in active:
        model_id = (row.get('model') or {}).get('model_id')
        if model_id:
            installed.setdefault(model_id, []).append(row)
    compiler = None
    eligible_faces = {}
    replacements = {}
    for model_id, rows in installed.items():
        keys = merge_keys(model_id)
        for offering in rows:
            offering_id = offering['offering_id']
            wires = wires_by_offering[offering_id]
            if any(cells >= wires and keys & recipe_keys
                   for _, recipe_keys, cells in authored):
                continue  # its own recipe still authors this supply
            for heir_id, heir_keys, cells in authored:
                heirs = installed.get(heir_id, ())
                if len(heirs) != 1 or keys & heir_keys or not cells >= wires:
                    continue
                heir = heirs[0]['offering_id']
                if not wires_by_offering[heir] & cells:
                    continue  # no exact installed recipe cell, even before eligibility
                if compiler is None:
                    from lib.model_routing.registration import prepare_registration_document

                    # Check the admission view the CAS will store; a newly
                    # transplanted draft or invalid retarget is no heir.
                    admitted = (prepare_registration_document(working, previous=previous)
                                if previous is not None else working)
                    compiler = RouteCandidateCompiler(admitted, normalized=True)
                    provider_id = next(row['provider_id'] for row in working['provider_accesses']
                                       if row['provider_access_id'] == access_id)
                if heir not in eligible_faces:
                    selection = NativeModelSelection(None, ProviderOfferingRef(provider_id, heir))
                    eligible_faces[heir] = {
                        _wire_face_key(route.deployment['wire_model_id'], route.connection,
                                       route.deployment.get('extra_headers'))
                        for route in compiler.compile_deployments(selection)
                    }
                if {cell[1:] for cell in wires} <= {
                        cell[1:] for cell in cells & eligible_faces[heir]}:
                    replacements.setdefault(offering_id, []).append((heir, heir_id))

    changed = False
    for offering in active:
        heir_id = next((model_id for heir, model_id in replacements.get(offering['offering_id'], ())
                        if heir not in replacements), None)
        if heir_id is not None:
            offering['stale'] = True
            changed = True
            entry = f"{access_id}:{offering['model']['model_id']}->{heir_id}"
            if entry not in stats['superseded']:
                stats['superseded'].append(entry)
    return changed


def _backfill_request_faces(working: dict, access: dict, template: dict,
                            stats: dict) -> bool:
    """Converge a card's wire wiring onto the faces its template declares.

    Templates seed NEW installs only (insert-only), so a card built before a
    recipe gained a ``request_faces`` alias keeps the logical model but never
    grows the alias Deployment — worse, the new face's base URL enters the
    template fingerprint, so an old card that lacks that face Connection stops
    matching the template entirely and the whole supply sync skips it.

    Adding a request name through the Settings matrix had a second defect: it
    bound the new wire to "the first enabled Connection". A face-pinned wire
    (aigc's ``kunlunbedrock.*`` on ``/v1``) then sat on a face the gateway does
    not serve it from and answered 400 ``配置不存在`` forever, which is why the
    same name could be added repeatedly and never work.

    Both are the same gap, so this converges them together:

    * a wire the recipe authors but the access never deployed is appended onto
      the SAME Offering, creating its face Connection deterministically;
    * a wire sitting on the WRONG DECLARED face is retargeted and its stale
      probe evidence discarded (a 400 from the wrong endpoint is not evidence
      about the model);
    * a newly created face extends only credentials already authorized for
      that model/wire on an existing transport; existing face revocations and
      model/wire restrictions remain owner authority.

    The face per wire comes from ``resolve_face``, the shared resolver the
    provider probe already uses and the template compiler encodes (notably the
    dual-face rule that keeps Claude models on the Anthropic wire) — so this
    pass cannot invent an assignment the catalog does not claim. Only a
    Connection the template itself namespaced is ever retargeted: a generic
    (namespace-less) Connection is an owner endpoint no face claims, and
    duplicate spellings of one wire are left for the owner to disambiguate.
    """
    from lib.llm_dispatch.provider_face import DEFAULT_FACE, resolve_face
    from lib.model_routing.migration import _protocol, _stable_id

    access_id = access['provider_access_id']
    faces = template.get('faces') or {}
    if not isinstance(faces, dict):
        return False
    provider = {
        'id': str(template.get('key') or ''),
        'base_url': template.get('base_url'),
        'protocol': template.get('protocol'),
        'faces': faces,
    }

    connections = _access_connections(working, access_id)
    # A default face cannot inherit a selector face's namespace. This matters
    # for Claude: family resolution selects Anthropic, while a TokRouter wire
    # pinned to Responses must resolve to TokRouter on a later iteration.
    by_url = {}
    for row in connections:
        namespace = _gateway_namespace(row)
        if namespace in faces or not namespace:
            by_url.setdefault((_norm_url(row.get('base_url')), namespace), row)
    by_id = {row.get('connection_id'): row for row in connections}
    connection_ids = {row.get('connection_id') for row in connections}
    credentials = [row for row in working.get('credentials') or []
                   if isinstance(row, dict)
                   and row.get('provider_access_id') == access_id]

    changed = False
    new_connection_ids: set[str] = set()

    def _face_connection(face_name: str, resolution) -> str | None:
        """Connection id for a declared face, created or converged in place."""
        nonlocal changed
        url = _norm_url(resolution.base_url)
        spec = faces.get(face_name) or {}
        # Several selectors may share one Responses URL (kunlun/tokrouter).
        # A URL-only match can therefore send the wrong provider selector.
        # An unowned same-URL row remains a usable legacy face only while no
        # other declared face needs it; a different named face is never taken.
        existing = next(
            (row for row in connections
             if _norm_url(row.get('base_url')) == url
             and _gateway_namespace(row) == face_name),
            None,
        )
        other_face_urls = {
            _norm_url(spec.get('base_url'))
            for name, spec in faces.items() if name != face_name
        }
        if existing is None and url not in other_face_urls:
            existing = next(
                (row for row in connections
                 if _norm_url(row.get('base_url')) == url
                 and not _gateway_namespace(row)),
                None,
            )
        if existing is not None:
            if _converge_declared_face(existing, face_name, spec):
                stats['repaired_connections'] += 1
                changed = True
            return existing['connection_id']
        # Deterministic id matching migration._stable_id so a re-seeded fresh
        # install and this backfill land on the same row (idempotent).
        connection_id = _stable_id('conn', access_id, face_name, url)
        if connection_id in connection_ids:
            return connection_id
        row = {
            'connection_id': connection_id,
            'provider_access_id': access_id,
            'base_url': url,
            'protocol': _protocol(resolution.protocol, url),
            'enabled': True,
            'priority': 100,
            'extra_headers': dict(spec.get('extra_headers') or {}),
            **({'responses_profile': spec['responses_profile']}
               if spec.get('responses_profile') else {}),
            **({'inference_profile': spec['inference_profile']}
               if spec.get('inference_profile') else {}),
            'gateway_namespace': face_name,
        }
        working['connections'].append(row)
        connection_ids.add(connection_id)

        new_connection_ids.add(connection_id)
        by_url[url] = row
        by_id[connection_id] = row
        stats['added_connections'] += 1
        return connection_id

    for recipe in template.get('offering_recipes') or []:
        model_id = str(recipe.get('model_id') or '').strip()
        if not model_id:
            continue
        request_faces = recipe.get('request_faces')
        if not isinstance(request_faces, dict):
            request_faces = {}
        pinned = {str(wire or '').strip() for wire in request_faces} - {''}
        offering = next(
            (row for row in working.get('offerings') or []
             if isinstance(row, dict)
             and row.get('provider_access_id') == access_id
             and row.get('identity_state') == 'confirmed'
             and (row.get('model') or {}).get('model_id') == model_id),
            None)
        if offering is None:
            continue  # not installed here; the insert-only path owns new adds
        rows_by_wire: dict[str, list[dict]] = {}
        for row in working.get('deployments') or []:
            if not isinstance(row, dict):
                continue
            if row.get('offering_id') != offering['offering_id']:
                continue
            wire = str(row.get('wire_model_id') or '').strip()
            if wire:
                rows_by_wire.setdefault(wire, []).append(row)
        source_connection_ids = {
            row.get('connection_id')
            for rows in rows_by_wire.values() for row in rows
        } - new_connection_ids
        wires = {str(wire or '').strip()
                 for wire in (recipe.get('request_ids') or [model_id])}
        wires |= pinned
        for wire in sorted(value for value in wires if value):
            rows = rows_by_wire.get(wire) or []
            # A wire belongs to one deployment per access, even when two
            # selector faces share an endpoint. Never steal another offering.
            if not rows and any(
                    row.get('wire_model_id') == wire
                    and row.get('connection_id') in connection_ids
                    for row in working.get('deployments') or []):
                conflict = f'{access_id}:{wire}'
                conflicts = stats.setdefault('wire_conflicts', [])
                if conflict not in conflicts:
                    conflicts.append(conflict)
                continue
            resolution = resolve_face(provider, recipe, wire_id=wire)
            if not resolution.ok or not resolution.base_url:
                continue  # unresolved face — leave for migration to flag
            is_default_face = resolution.face_name == DEFAULT_FACE
            if is_default_face:
                connection_id = (by_url.get((_norm_url(resolution.base_url), ''))
                                 or {}).get('connection_id')
            else:
                connection_id = _face_connection(
                    resolution.face_name, resolution)
            if connection_id is None:
                continue
            if connection_id in new_connection_ids:
                # Absence from an existing face is an owner revocation, not
                # missing initialization. Only a face created in this CAS may
                # extend an already-authorized model/wire's transport grant.
                changed = _grant_face_connection(
                    credentials, connection_id, offering['model'], wire,
                    source_connection_ids) or changed
            if any(row.get('connection_id') == connection_id for row in rows):
                continue  # already converges on its face
            if rows:
                if len(rows) > 1:
                    continue  # duplicate spellings — owner territory
                current = by_id.get(rows[0].get('connection_id')) or {}
                if not str(current.get('gateway_namespace') or '').strip():
                    # A generic Connection: no template face claims that
                    # endpoint, so this is an owner endpoint we must not move.
                    continue
                # Wired to the wrong DECLARED face — added before the face
                # existed, or through the picker that bound every new wire to
                # "the first enabled Connection". Retarget, and drop evidence
                # measured against the endpoint that could never serve it.
                rows[0]['connection_id'] = connection_id
                rows[0]['probe_status'] = 'unprobed'
                stats['repaired_deployments'] += 1
                changed = True
                continue
            if wire not in pinned:
                continue  # only a template-pinned alias is ever appended
            deployment_id = _stable_id('dep', access_id, connection_id, wire)
            if any(row.get('deployment_id') == deployment_id
                   for row in working.get('deployments') or []):
                continue
            working['deployments'].append({
                'deployment_id': deployment_id,
                'offering_id': offering['offering_id'],
                'connection_id': connection_id,
                'wire_model_id': wire,
                'identity_confidence': 'high',
                'probe_status': 'unprobed',
                'priority': 110,
            })
            stats['added_deployments'] += 1
            stats['accesses'].setdefault(access_id, []).append(
                f'{model_id}+{wire}')
            changed = True
    return changed


def _converge_declared_face(row: dict, face_name: str, spec: dict) -> bool:
    """Apply a recipe's declared wire fields to its OWN face Connection.

    ``gateway_namespace`` marks a Connection the template created, so the
    template stays its owner: when a recipe later tightens what the endpoint
    is known to do — Kunlun streams opaque ``reasoning.encrypted_content``
    and therefore needs ``features.responses_reasoning_replay`` — a card
    installed before that fact was measured must follow, otherwise every
    tool round after the first opaque reasoning item dies in request-prepare
    as ``mandatory_continuity_unrepresentable`` (2026-10-21, live).

    Only keys the template declares are written, per-section for the
    inference profile; a namespace-less Connection is an owner endpoint and
    undeclared owner fields are never touched. Idempotent.
    """
    if str(row.get('gateway_namespace') or '') != face_name:
        return False
    changed = False
    declared_headers = spec.get('extra_headers')
    if isinstance(declared_headers, dict) and declared_headers:
        headers = row.get('extra_headers')
        headers = dict(headers) if isinstance(headers, dict) else {}
        merged = merge_header_layers(headers, declared_headers)
        if merged != headers:
            row['extra_headers'] = merged
            changed = True
    declared_profile_name = spec.get('responses_profile')
    if (declared_profile_name
            and row.get('responses_profile') != declared_profile_name):
        row['responses_profile'] = declared_profile_name
        changed = True
    declared_profile = spec.get('inference_profile')
    if isinstance(declared_profile, dict) and declared_profile:
        current = row.get('inference_profile')
        current = dict(current) if isinstance(current, dict) else {}
        if _declares_only(current, declared_profile):
            return changed
        from lib.model_routing.inference import resolve_inference_profile
        merged = resolve_inference_profile(
            connection={'inference_profile': current},
            deployment={'inference_profile': declared_profile})
        # The resolver replaces a non-section value wholesale, but a feature
        # map is per-key evidence: an owner-declared feature the template does
        # not mention must survive the convergence.
        features = {**(current.get('features') or {}),
                    **(declared_profile.get('features') or {})}
        if features:
            merged['features'] = features
        if merged != current:
            row['inference_profile'] = merged
            changed = True
    return changed


def _declares_only(current, declared) -> bool:
    """True when every declared key already holds in the installed profile.

    Convergence runs per recipe and wire on a periodic pass; the resolver
    validates and deep-copies, so steady state must not pay for it.
    """
    if isinstance(declared, dict):
        return isinstance(current, dict) and all(
            _declares_only(current.get(key), value)
            for key, value in declared.items())
    return current == declared

def _grant_face_connection(credentials: list[dict], connection_id: str,
                           model_ref: dict, wire: str,
                           source_connection_ids: set[str]) -> bool:
    """Initialize a NEW face for credentials already allowed this model/wire.

    Existing-face grants (including new credentials) remain as submitted at
    the credential creation/save boundary; sync cannot distinguish absence
    from an explicit revocation. Empty/restricted transport grants, revoked model
    grants and per-wire exclusions must never be expanded by backfill.
    """
    changed = False
    for credential in credentials:
        authorization = credential.get('authorization') or {}
        ids = authorization.get('connection_ids') or []
        if not source_connection_ids.intersection(ids):
            continue
        if not any(
                grant.get('creator_id') == model_ref.get('creator_id')
                and grant.get('model_id') == model_ref.get('model_id')
                and wire not in grant.get('excluded_wire_model_ids', [])
                for grant in authorization.get('models') or []):
            continue
        if connection_id not in ids:
            ids.append(connection_id)
            changed = True
    return changed


def _transplant(working: dict, access: dict, bundle: dict,
                verdicts: dict, stats: dict, unroutable: list, *,
                grantable_credentials: frozenset = frozenset()) -> bool:
    """Append the bundle's offerings/deployments for one access.

    Recomputes dedupe against the working copy so CAS retries stay
    idempotent. Returns True when anything was appended.
    """
    from lib.model_catalog._creator_identity import merge_keys
    from lib.model_routing.migration import _stable_id

    access_id = access['provider_access_id']
    known = set(_known_keys(working, access_id))
    wire_faces = _known_wire_faces(working, access_id)
    wires = {cell[0] for cell in wire_faces}
    offering_ids = {
        row.get('offering_id')
        for row in working.get('offerings') or []
    }
    deployment_ids = {
        row.get('deployment_id')
        for row in working.get('deployments') or []
    }
    connections = _access_connections(working, access_id)
    bundle_connections = {
        row['connection_id']: row for row in bundle['connections']
    }
    model_rows = {
        (row.get('creator_id'), row.get('model_id')): row
        for row in bundle['models']
    }
    creator_rows = {row['creator_id']: row for row in bundle['creators']}
    appended = False

    for source in bundle['offerings']:
        model = source.get('model') if isinstance(source.get('model'), dict) else None
        pending = str(source.get('pending_model_id') or '').strip()
        identity = (
            f"{model['creator_id']}/{model['model_id']}" if model
            else f'pending:{pending}')
        identity_keys = merge_keys(
            model.get('model_id') if model else pending)
        if identity_keys & known:
            continue
        offering_id = _stable_id('off', access_id, identity)
        if offering_id in offering_ids:
            continue
        root = model['model_id'] if model else pending

        deployments: list[dict] = []
        unrouted_wire = False
        for dep in bundle['deployments']:
            if dep.get('offering_id') != source['offering_id']:
                continue
            wire = str(dep.get('wire_model_id') or '').strip()
            if not wire:
                continue
            bundle_conn = bundle_connections.get(dep.get('connection_id')) or {}
            if wire in wires:
                # The domain permits only one deployment per access/wire,
                # even when another selector would serve that spelling.
                if _wire_face_key(wire, bundle_conn, dep.get('extra_headers')) not in wire_faces:
                    conflicts = stats.setdefault('wire_conflicts', [])
                    conflict = f'{access_id}:{wire}'
                    if conflict not in conflicts:
                        conflicts.append(conflict)
                continue
            unrouted_wire = True
            bundle_namespace = str(bundle_conn.get('gateway_namespace') or '')
            target = next(
                (row for row in connections
                 if _norm_url(row.get('base_url')) == _norm_url(bundle_conn.get('base_url'))
                 and str(row.get('gateway_namespace') or '') == bundle_namespace),
                None,
            )
            if target is None and not bundle_namespace:
                target = next(
                    (row for row in connections
                     if _norm_url(row.get('base_url')) == _norm_url(bundle_conn.get('base_url'))
                     and not row.get('gateway_namespace')),
                    None,
                )
            if target is None:
                target = next(
                    (row for row in connections
                     if row.get('protocol') == bundle_conn.get('protocol')
                     and row.get('enabled')
                     and str(row.get('gateway_namespace') or '') == bundle_namespace),
                    None,
                )
            if target is None:
                continue
            deployment_id = _stable_id(
                'dep', access_id, target['connection_id'], wire)
            if deployment_id in deployment_ids:
                continue
            # Match the complete explicit header set of the new Deployment
            # (Connection then Deployment), without adding credential-only
            # routing values to its declaration.
            declared_headers = merge_header_layers(target.get('extra_headers') or {},
                                                   dep.get('extra_headers') or {})
            cell_verdict = verdicts.get((root, *_probe_transport_key(
                wire, target.get('base_url'), target.get('protocol'), declared_headers)))
            deployments.append({
                **copy.deepcopy(dep),
                'deployment_id': deployment_id,
                'offering_id': offering_id,
                'connection_id': target['connection_id'],
                'wire_model_id': wire,
                # Only a measured successful inference is passed evidence.
                # Diagnostics do not control route enablement.
                'probe_status': 'passed' if cell_verdict == 'ok' else 'unprobed',
            })
        if not deployments:
            if unrouted_wire:
                unroutable.append((access_id, identity))
            continue

        offering = {
            **copy.deepcopy(source),
            'offering_id': offering_id,
            'provider_access_id': access_id,
        }
        working['offerings'].append(offering)
        working['deployments'].extend(deployments)
        offering_ids.add(offering_id)
        deployment_ids.update(row['deployment_id'] for row in deployments)
        wires.update(row['wire_model_id'] for row in deployments)
        wire_faces.update(_wire_face_key(
            row['wire_model_id'], next(connection for connection in connections
                                      if connection['connection_id'] == row['connection_id']),
            row.get('extra_headers')) for row in deployments)
        known.update(identity_keys)
        appended = True
        stats['added_offerings'] += 1
        stats['added_deployments'] += len(deployments)
        stats['accesses'].setdefault(access_id, []).append(
            model['model_id'] if model else pending)

        if model is None:
            continue
        row = model_rows.get((model['creator_id'], model['model_id']))
        if row is not None:
            existing = next(
                (entry for entry in working.get('models') or []
                 if entry.get('creator_id') == row.get('creator_id')
                 and entry.get('model_id') == row.get('model_id')),
                None)
            if existing is None:
                # Append even when the row respells an already-registered
                # trained model (dated snapshot, relay SKU): normalize's
                # alias merge absorbs it and rewrites offering/credential
                # refs onto the survivor row, so every appended ref stays
                # resolvable.
                working['models'].append(copy.deepcopy(row))
            else:
                # Recheck the current owner baseline on every CAS attempt;
                # a stale compiled template may not expand an owner edit.
                from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
                offering['capabilities'] = sorted(set(offering['capabilities']) & (
                    set(existing['capabilities']) | MANAGED_TIER_TAGS))
                offering['context_window'] = min(offering['context_window'], existing['context_window'])
        creator = creator_rows.get(model['creator_id'])
        if creator is not None and not any(
                row.get('creator_id') == model['creator_id']
                for row in working.get('creators') or []):
            working['creators'].append(copy.deepcopy(creator))
        ref = {'creator_id': model['creator_id'], 'model_id': model['model_id']}
        for credential in working.get('credentials') or []:
            if (credential.get('provider_access_id') != access_id
                    or credential.get('credential_id') not in grantable_credentials):
                continue
            authorized = credential.setdefault('authorization', {}).setdefault(
                'models', [])
            if not any(all(grant[key] == value for key, value in ref.items()) for grant in authorized):
                authorized.append(dict(ref))
    return appended


_unroutable_reported: dict[str, tuple[str, ...]] = {}


def _log_unroutable(identities: list[str]) -> None:
    """Warn once per (access, identity set) change, not once per row per pass.

    An unroutable pending recipe is re-encountered on every pass while the
    template names a face the access has no Connection for, so per-row logging
    flooded the ERROR/WARNING streams (188 lines in one 49-minute window).
    """
    for access_id, identity in identities:
        with _schedule_lock:
            key = f'{access_id}:{identity}'
            if key in _unroutable_reported:
                continue
            _unroutable_reported[key] = ()
        logger.warning(
            'template supply: %s has no routable connection for %s — skipped',
            access_id, identity)


def sync_template_supply(*, boundary=None, repository=None,
                         prober=None, templates=None,
                         provider_access_ids=None,
                         backfill_only=False) -> dict:
    """One convergence pass over every template-matched installed access.

    ``provider_access_ids`` narrows the pass to specific installed cards —
    the Settings matrix syncs ONE row without re-probing every other card.
    ``backfill_only`` skips the missing-recipe probe sweep and runs only the
    deterministic additions (renamed supply, face-pinned wire ids). That is
    the cheap incremental step: no network, no probe verdicts consumed, and
    an installed card gains a template's new face/alias immediately instead
    of waiting for the next six-hour cadence.
    """
    if boundary is None or repository is None:
        from lib.llm_dispatch.autodiscover_local import _runtime_context
        context = _runtime_context()
        if context is None:
            return {'added_offerings': 0, 'added_deployments': 0,
                    'accesses': {}, 'skipped': 'no_owner'}
        boundary, repository = context

    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates)

    if templates is None:
        templates = [
            row for row in load_provider_templates()
            if row.get('category') != 'local'
            and (row.get('offering_recipes') or [])
        ]
    prober = prober or _live_prober

    document = repository.get(boundary).document
    stats: dict[str, Any] = {
        'added_offerings': 0,
        'added_deployments': 0,
        'repaired_deployments': 0,
        'added_connections': 0,
        'repaired_connections': 0,
        'superseded_offerings': 0,
        'superseded': [],
        'wire_conflicts': [],
        'accesses': {},
    }
    plans: list[tuple[dict, dict, dict]] = []
    matched: list[tuple[dict, dict]] = []
    only_access_ids = ({str(value).strip() for value in provider_access_ids}
                       - {''} if provider_access_ids is not None else None)
    for access in document.get('provider_accesses') or []:
        if not isinstance(access, dict):
            continue
        if (only_access_ids is not None
                and access.get('provider_access_id') not in only_access_ids):
            continue
        template = _find_template(document, access, templates)
        if template is None:
            continue
        matched.append((access, template))
        if backfill_only:
            continue
        missing = _missing_probe_recipes(document, access, template)
        if not missing:
            continue
        try:
            api_key, secret_headers = _resolve_api_key(
                repository, boundary, document, access['provider_access_id'])
        except Exception as exc:
            logger.warning(
                'template supply: credential resolve failed for %s: %s',
                access['provider_access_id'], exc)
            continue
        if not api_key:
            logger.info(
                'template supply: %s has no usable api-key credential — skipped',
                access['provider_access_id'])
            continue
        base_url, protocol, faces, default_face_headers = _probe_plan(
            document, access, template)
        try:
            verdicts = prober(
                base_url=base_url, protocol=protocol, faces=faces,
                api_key=api_key, extra_headers=secret_headers,
                default_face_headers=default_face_headers, recipes=missing,
                inference_profile=template.get('inference_profile') or {})
        except Exception as exc:
            logger.warning(
                'template supply: probe failed for %s: %s',
                access['provider_access_id'], exc)
            continue
        served = sorted(
            recipe['model_id'] for recipe in missing
            if verdicts.get(recipe['model_id']) in _SERVED_VERDICTS)
        if not served:
            logger.info(
                'template supply: %s — %d candidate recipes probed dead',
                access['provider_access_id'], len(missing))
            continue
        try:
            bundle = compile_provider_template_bundle(
                template['key'], selected_model_ids=served,
                official_directory=document['models'])
        except Exception as exc:
            logger.warning(
                'template supply: compile failed for %s: %s',
                template['key'], exc)
            continue
        plans.append((access, bundle, verdicts))

    planned_credentials = {
        row['credential_id']: row for row in document.get('credentials', ())
        if plans and row.get('credential_id')}

    def _mutate(previous: dict, attempt_stats: dict, attempt_unroutable: list) -> dict:
        # A stale probe plan can add supply, but cannot grant permissions to
        # new or changed credentials (including a concurrent model revocation).
        grantable_credentials = frozenset(
            row['credential_id'] for row in previous.get('credentials', ())
            if planned_credentials.get(row.get('credential_id')) == row)
        working = copy.deepcopy(previous)
        current_accesses = {row['provider_access_id']: row
                            for row in working['provider_accesses']}
        for access, template in matched:
            current = current_accesses.get(access['provider_access_id'])
            if current is not None:
                _backfill_request_faces(working, current, template, attempt_stats)
        for access, bundle, verdicts in plans:
            current = current_accesses.get(access['provider_access_id'])
            if current is not None:
                _transplant(working, current, bundle, verdicts, attempt_stats,
                            attempt_unroutable, grantable_credentials=grantable_credentials)
        # Backfill may move the heir onto a disabled or ungranted face.
        # Decide retirement last against what this CAS would publish.
        for access, template in matched:
            current = current_accesses.get(access['provider_access_id'])
            if current is not None:
                _supersede_renamed_supply(working, current, template, attempt_stats, previous)
        return working

    if not plans:
        preview_stats = copy.deepcopy(stats)
        if _mutate(document, preview_stats, []) == document:
            stats['wire_conflicts'] = preview_stats['wire_conflicts']
            stats['revision'] = repository.get(boundary).revision
            return stats

    # Optimistic concurrency: every conflicting writer moved the aggregate,
    # so re-read and recompute instead of failing the sweep. An empty pass
    # must NOT commit — a no-op revision used to wake every sibling monitor,
    # which committed another, producing an ERROR storm of CAS conflicts.
    while True:
        authority = repository.get(boundary)
        attempt_stats = copy.deepcopy(stats)
        attempt_unroutable = []
        working = _mutate(authority.document, attempt_stats, attempt_unroutable)
        if working != authority.document:
            try:
                authority = repository.compare_and_swap(
                    boundary, working, expected_revision=authority.revision)
            except ModelRoutingError as exc:
                if getattr(exc, 'kind', '') != 'model_routing_revision_conflict':
                    raise
                continue
        stats, unroutable = attempt_stats, attempt_unroutable
        break
    stats['superseded_offerings'] = len(stats['superseded'])
    stats['revision'] = authority.revision
    _log_unroutable(unroutable)
    logger.info('template supply sync: %s', stats)
    audit_log('provider_template_supply_synced',
              accesses={key: sorted(value)
                        for key, value in stats['accesses'].items()},
              added_offerings=stats['added_offerings'],
              added_deployments=stats['added_deployments'],
              repaired_deployments=stats['repaired_deployments'],
              added_connections=stats['added_connections'],
              repaired_connections=stats['repaired_connections'],
              superseded_offerings=stats['superseded_offerings'],
              wire_conflicts=stats['wire_conflicts'],
              owner_user_id=boundary.owner_user_id,
              model_routing_revision=authority.revision)
    try:
        from lib.llm_dispatch import reset_dispatcher
        reset_dispatcher()
    except Exception as exc:
        logger.error('template supply: dispatcher reset failed: %s', exc)
    return stats


# ── Scheduling on the shared local-endpoint monitor ──────────────────────

def _inactive_poll_stats(reason: str) -> dict:
    return {reason: True, 'scheduled': False, 'next_poll_s': None}


def poll_if_due(*, now: float | None = None, prober=None,
                boundary=None, repository=None) -> dict:
    """Run one due template-supply pass from the shared monitor thread."""
    global _next_sync_at, _force_sync
    import time

    if _disabled():
        return _inactive_poll_stats('disabled')
    clock_now = time.monotonic() if now is None else float(now)
    with _schedule_lock:
        if not _runtime_enabled:
            return _inactive_poll_stats('stopped')
        if _next_sync_at is None:
            _next_sync_at = clock_now + _BOOT_DELAY
        if clock_now < _next_sync_at and not _force_sync:
            return {
                'scheduled': False,
                'next_poll_s': _next_sync_at - clock_now,
            }
        # Claim this pass before any network I/O so a concurrent caller
        # joins the next cadence instead of duplicating it.
        _force_sync = False
        _next_sync_at = clock_now + _SYNC_INTERVAL

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=prober)
    stats['scheduled'] = True
    stats['next_poll_s'] = _SYNC_INTERVAL
    return stats


def trigger_template_supply_sync() -> bool:
    """Request an immediate pass after a template install/update."""
    global _next_sync_at, _force_sync, _schedule_generation
    if _disabled():
        return False
    with _schedule_lock:
        if not _runtime_enabled:
            return False
        _schedule_generation += 1
        _force_sync = True
        _next_sync_at = 0.0
    try:
        from lib.llm_dispatch.health_local import wake_local_health_checker
        return wake_local_health_checker()
    except Exception as exc:
        logger.debug('template supply: shared-monitor wake failed: %s', exc)
        return False


def start_template_supply_sync() -> bool:
    """Enable template-supply sync on the shared monitor."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    import time

    if _disabled():
        logger.info('template supply sync disabled via '
                    'TOFU_TEMPLATE_SUPPLY_SYNC=0')
        return False
    with _schedule_lock:
        was_enabled = _runtime_enabled
        _runtime_enabled = True
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = time.monotonic() + _BOOT_DELAY
    from lib.llm_dispatch.health_local import (
        start_local_health_checker,
        wake_local_health_checker,
    )
    started = start_local_health_checker()
    wake_local_health_checker()
    return bool(started or not was_enabled)


def stop_template_supply_sync() -> bool:
    """Disable template-supply sync; the shared monitor keeps running."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    with _schedule_lock:
        _runtime_enabled = False
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = None
    try:
        from lib.llm_dispatch.health_local import wake_local_health_checker
        wake_local_health_checker()
    except Exception as exc:
        logger.debug('template supply: shared-monitor wake failed: %s', exc)
    return True
