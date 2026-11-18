"""Materialize model-routing v2 candidates and select authorized runtime targets."""

import json
import math
import os
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace

# A set-once Event with race-free thread and asyncio wake-ups (no polling): a
# contention gate's queued waiters block on one until the gate opens.
from lib.agent_core.abort_signal import AbortSignal
from lib.log import get_logger
from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS

from .config import MANAGED_TIER_TAGS, MODEL_ALIASES
from .conv_affinity import (
    get_conv_affinity,
    get_preferred_key,
    record_conv_key,
    sticky_routing_enabled,
)
from .credential_reservation import active_reservations, reserved_slot
from .measured_speed import measured_speed_evidence
from .quota_watch import WINDOW_S
from .slot import Slot
from .slot_pool import SlotPool

logger = get_logger(__name__)

__all__ = [
    'LLMDispatcher',
]


def _model_identity(slot) -> str:
    """The logical Model a slot serves; keys rotation and speed evidence."""
    return slot.logical_model or slot.model


@dataclass(frozen=True)
class _SharedContentionProbeState:
    """Process-local admission state for one upstream quota gate."""

    strikes: int
    last_strike_at: float
    next_probe_at: float
    # Set once when this run of rejections ends: a request sent after the
    # last rejection succeeded, or the quiet gate retired. Queued waiters
    # block on it, so the opening reaches every one of them at once.
    opened: AbortSignal
    # Start of the gate's current unbroken run of rejections; None once the
    # gate stayed quiet through the decay grace.
    rejecting_since: float | None = None
    # End of the hold the provider named for the latest rejection
    # (retry_after_s). No probe may go out before it, so the gate is not
    # quiet until it has passed.
    held_until: float | None = None


@dataclass(frozen=True)
class _SharedContentionAdmission:
    """One bounded wait slice and whether it owns the following probe.

    ``openings`` are the armed gates' signals and ``gates`` their keys: a
    waiter proceeds at once when every one of them opens.
    """

    delay_s: float
    admitted: bool
    openings: tuple[AbortSignal, ...] = ()
    gates: tuple[tuple[str, str, str], ...] = ()


class _KeyAdmission:
    """Key-health verdicts for one selection.

    A key may be auto-disabled for the rest of today when its success rate
    dropped below the threshold, or a user may have toggled it off in
    Settings. Reads use the credential-health identity the recorder writes
    (``Slot.key_stats_provider_id`` / ``key_stats_key_name``), share one
    key-store freshness check (``key_admission_batch``) and read each slot
    at most once per selection. ``lib.key_stats.is_key_enabled`` is resolved
    per selection so the package facade remains the patch seam.
    """

    def __init__(self, *, enforced: bool = True, credentials=None,
                 billing_stops_only: bool = False, probes=None):
        self._read = None
        self._batch = nullcontext()
        self._verdicts: dict[int, bool] = {}
        # ``probes``: the dispatcher's in-flight billing probes, passed only
        # by a reserving pick; ``probing`` names the admitted probe slots.
        self._probes = probes
        self.probing: dict[int, tuple[str, str]] = {}
        if not enforced:
            return
        try:
            if billing_stops_only:
                from lib.key_stats import is_key_refused_for_optional_work
                self._read = lambda provider, key, model: not (
                    is_key_refused_for_optional_work(
                        provider, key, model=model, honor_override=True))
            else:
                from lib.key_stats import is_key_enabled
                self._read = is_key_enabled
            from lib.key_stats import key_admission_batch
        except ImportError as e:
            logger.debug('[Dispatch] key_stats.is_key_enabled unavailable: %s', e)
            return
        self._batch = key_admission_batch(credentials=credentials)

    def __enter__(self):
        self._batch.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._batch.__exit__(*exc_info)

    def admits(self, slot) -> bool:
        verdict = self._verdicts.get(id(slot))
        if verdict is None:
            verdict = self._verdicts[id(slot)] = self._probe(slot)
        return verdict

    def _probe(self, slot) -> bool:
        if self._read is None:
            return True
        try:
            admitted = bool(self._read(
                slot.key_stats_provider_id(),
                slot.key_stats_key_name(),
                model=slot.model))
        except Exception as e:
            logger.debug('[Dispatch] is_key_enabled probe failed for %s/%s: %s',
                         slot.provider_id, slot.key_name, e)
            return True
        if not admitted and self._probes is not None:
            admitted = self._billing_probe_admits(slot)
        return admitted

    def _billing_probe_admits(self, slot) -> bool:
        """Admit one request to a key-wide 402 stop past the day boundary.

        The provider refills a daily credit at an instant its responses do
        not state, so the next real request is the typed probe
        (``lib.key_stats.billing_stop_probe_due``). While that request is in
        flight on its credential, every other request is refused.
        """
        identity = (slot.key_stats_provider_id(), slot.key_stats_key_name())
        in_flight = self._probes.get(identity)
        if in_flight is not None and in_flight.inflight > 0:
            return False
        try:
            from lib.key_stats import billing_stop_probe_due
            due = billing_stop_probe_due(*identity)
        except Exception as e:
            logger.debug('[Dispatch] billing probe check failed for %s/%s: %s',
                         slot.provider_id, slot.key_name, e)
            return False
        if due:
            self.probing[id(slot)] = identity
        return due


def _observe_route_selection(seconds: float, pinned: bool, chosen) -> None:
    try:
        from lib.observability import record_route_selection
        record_route_selection(
            seconds, scope='pinned' if pinned else 'shared',
            outcome='selected' if chosen is not None else 'none')
    except Exception as e:
        logger.debug('[Dispatch] route-selection metric skipped: %s', e)


class LLMDispatcher:
    """Manages a pool of (key, model) slots and picks the best one per request."""

    def __init__(self):
        self._pool = SlotPool()
        self._initialized = False
        self._lock = threading.Lock()
        self._billing_probes: dict[tuple[str, str], Slot] = {}
        # One configured-pool build at a time. Builders read the routing
        # authority (storage, secrets) without holding ``_lock``; the
        # generation tells a finished build whether a reload arrived meanwhile.
        self._build_lock = threading.Lock()
        self._configuration_generation = 0
        # name → logical model_id, built ONLY from each entry's own
        # {model_id} ∪ wire ids — never merged across entries/providers.
        # Owner directive 2026-08-06: routing keys on model_id alone; config
        # aliases are wire spellings, NOT routing widenings. Rebuilt by
        # _build_logical_index during slot build.
        self._logical_index: dict[str, str] = {}
        self._direct_models: set = set()
        # Shared-contention gates are keyed by quota lane (Slot.quota_lane):
        # one credential's limit for a wire model, or with an empty credential
        # the provider/model project that every credential shares. Every
        # request reaching a gate shares it, whichever route group it uses.
        self._contention_strikes: dict[
            tuple[str, str, str], _SharedContentionProbeState
        ] = {}
        # Compatibility projection retained for the diagnostics endpoint.
        # Explicit v2 Connections make protocol-face refusal unnecessary.
        self.face_refusals: list[dict] = []

    @property
    def slots(self) -> SlotPool:
        """Every live slot; selection reads its pin-scoped views (slot_pool.py)."""
        return self._pool

    @slots.setter
    def slots(self, slots) -> None:
        # Assignment copies into a new indexed pool: later mutations of the
        # assigned list are not seen; mutate ``dispatcher.slots`` instead.
        if slots is not getattr(self, '_pool', None):
            self._pool = SlotPool(slots)

    def _credential_index(self, namespace: str) -> list:
        """Key-health siblings from this pool. Key admission calls it with the
        lock held, so it never resolves the dispatcher through the factory."""
        return self._pool.key_stats_key_names(namespace)

    def _selection_scope(self, pinned_provider: str | None) -> list[Slot]:
        """Live slots this execution context may select. Caller holds the lock.

        A pinned context sees only its own provider/route group; an unpinned
        one sees only configured slots, never another request's group.
        Fallback dispatch additionally never sees an experiment-reserved
        credential (credential_reservation.py, docs/NETWORK.md).
        """
        scope = self._pool.scope(pinned_provider)
        reserved = active_reservations()
        if not reserved:
            return scope
        return [slot for slot in scope if not reserved_slot(slot, reserved)]

    def resource_snapshot(self) -> dict:
        """Observe native slot reservations without initializing providers."""
        with self._lock:
            used = sum(s.inflight for s in self.slots)
            return {'capacity': max(1, used), 'used': used,
                    'blocked': 0,
                    'completed': sum(max(0, s.total_requests - s.inflight)
                                     for s in self.slots),
                    'errors': sum(s.total_errors for s in self.slots),
                    'pressure': any(time.monotonic() - s.last_strike_at < 15
                                    for s in self._contention_strikes.values())}

    def resize_resource_capacity(self, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError('model request capacity must be positive')
        # Legacy feedback may observe demand, but cannot cap request entry.

    def invalidate_configuration(self):
        """Rebuild configured routes on the next use, retaining live requests.

        The dispatcher and request-owned Slot objects survive reload so an
        in-flight response and its next round share reservations, route pins,
        health and disposal. No request group is reminted or widened here.
        """
        with self._lock:
            self._initialized = False
            self._configuration_generation += 1

    def initialize(self):
        """Build configured slots, then swap them in around request-owned ones.

        The build reads the routing authority and resolves secrets (storage
        round trips) without the dispatcher lock, so picks, mints and
        disposals proceed meanwhile; callers needing the configured pool wait
        on ``_build_lock`` instead. The swap is one critical section that
        keeps every request-scoped slot live at that moment. A reload during
        the build publishes the finished build but leaves the next call to
        rebuild, as a reload right after a build always did.
        """
        if self._initialized:
            return
        with self._build_lock:
            if self._initialized:
                return
            with self._lock:
                generation = self._configuration_generation
            try:
                configured, logical_index = self._build_slots()
                # Benchmark seeds/pruning apply only to configured slots.
                self._load_benchmark_data(configured)
            except Exception:
                # A partial configured pool is never eligible, and a failed
                # rebuild must not orphan the handles of active requests.
                with self._lock:
                    self._pool.replace_configured([])
                    self._logical_index = {}
                    self._direct_models = set()
                raise
            with self._lock:
                self._pool.replace_configured(configured)
                self._logical_index = logical_index
                self._direct_models = set()
                self._initialized = (
                    generation == self._configuration_generation)
        logger.info('Initialized %d configured slots', len(configured))
        for s in configured:
            caps = ','.join(sorted(s.capabilities))
            logger.debug('  %s:%s rpm=%.0f '
                  'lat=%.0fms caps=[%s]', s.key_name, s.model, s.rpm_limit, s.latency_ema, caps)

    def _build_slots(self) -> tuple[list[Slot], dict[str, str]]:
        """Create operator slots exclusively from model-routing v2.

        Returns the configured slots and their logical-model index; never
        touches the live pool.
        """
        # ── Benchmark / multi-tenant fail-loud mode ──
        # When set, build NO operator-curated slots at all. The only way to
        # dispatch is then an owner-authorized model-routing v2 candidate
        # group (ephemeral slots, injected at request time). This is
        # defense-in-depth on top of the per-task provider pin: with the
        # operator's configured slots absent there is literally no key
        # to leak onto, so an isolation bug fails LOUDLY (clean "no slot"
        # error) instead of silently consuming shared internal quota.
        if os.environ.get('TOFU_DISABLE_CONFIGURED_SLOTS', '').strip().lower() \
                in ('1', 'true', 'yes', 'on'):
            logger.warning('[Dispatch] TOFU_DISABLE_CONFIGURED_SLOTS set — '
                           'building 0 operator slots; request-scoped v2 '
                           'route groups remain available')
            return [], {}
        return self._build_slots_from_model_routing()

    def _build_slots_from_model_routing(self) -> tuple[list[Slot], dict[str, str]]:
        """Materialize the personal operator pool from the v2 repository.

        Request handlers use owner-specific ephemeral route groups. The shared
        pool exists for personal background tasks that have no HTTP request
        boundary; distributed mode intentionally requires owner enumeration.
        Returns the built slots and their logical-model index.
        """
        from lib.identity import PERSONAL_USER_ID
        from lib.model_routing import (
            ModelRef,
            ModelRoutingError,
            ModelRoutingRepository,
            NativeModelSelection,
            OwnerBoundary,
            RouteCandidateCompiler,
            RoutePolicy,
            RouteSnapshotBuilder,
            decode_credential_secret,
        )
        from runtime_guards import load_deployment_configuration

        deployment_configuration = load_deployment_configuration()
        if deployment_configuration.mode != 'personal':
            logger.info(
                '[Dispatch] shared v2 slots disabled in %s mode; requests '
                'must supply an authenticated owner route group',
                deployment_configuration.mode,
            )
            return [], {}
        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(PERSONAL_USER_ID)
        try:
            authority = repository.get(boundary)
        except ModelRoutingError as exc:
            # The shared dispatcher is also used by request-scoped ephemeral
            # routes.  A temporarily unavailable durable authority must not
            # make that in-memory capability impossible to initialise.  Only
            # the typed authority outage degrades to an empty operator pool;
            # malformed routing documents and programming errors still fail
            # loudly instead of being hidden by this lean fallback.
            if exc.kind != 'model_routing_storage_unavailable':
                raise
            logger.warning(
                '[Dispatch] model-routing authority unavailable; starting '
                'with 0 shared slots: %s', exc)
            return [], {}
        if not authority.revision:
            logger.error('[Dispatch] model-routing v2 authority is not active')
            return [], {}

        emitted: set[tuple[str, str]] = set()
        built: list[Slot] = []
        entry_groups: list[tuple[str, set[str]]] = []
        # One compiler for the whole build: re-normalizing and re-indexing the
        # full aggregate per model made this loop quadratic in document size.
        # The compiler is build-local, so it can never leak a stale revision.
        compiler = RouteCandidateCompiler(authority.document)
        # Secret resolution hits the durable store; one read per unique
        # reference, not per slot (a shared key backs every model slot).
        resolved_secrets: dict[str, str] = {}
        for model in authority.document['models']:
            selection = NativeModelSelection(
                ModelRef(model['creator_id'], model['model_id']), None, '')
            # The shared personal pool serves non-chat subsystems too (speech,
            # embedding, image generation). Chat selection applies its own
            # capability gate later; requiring ``text`` here would silently
            # erase a valid transcription-only Offering.
            candidates = compiler.compile(
                selection,
                policy=RoutePolicy(required_capabilities=frozenset()),
            )
            entry_group = {model['model_id']}
            for candidate in candidates:
                unique = (
                    candidate.deployment['deployment_id'],
                    candidate.credential['credential_id'],
                )
                if unique in emitted:
                    continue
                emitted.add(unique)
                reference = str(
                    candidate.credential['secret_reference'] or '')
                if reference and reference not in resolved_secrets:
                    resolved_secrets[reference] = repository.resolve_secret(
                        boundary, reference)
                secret = resolved_secrets.get(reference, '')
                api_key, oauth, secret_headers = decode_credential_secret(
                    secret, kind=candidate.credential['kind'])
                quota = candidate.credential.get('quota_policy') or {}
                access_quota = candidate.provider_access.get('quota_policy') or {}
                pricing = candidate.offering.get('actual_pricing') or {}
                snapshot = RouteSnapshotBuilder(selection)
                snapshot.record_transition(
                    source=None,
                    target=candidate,
                    reason='computed_v2_route',
                    kind='initial',
                )
                from lib.model_routing.inference import candidate_inference_metadata
                from lib.model_routing.serving_locality import candidate_is_local_inference
                slot = Slot(
                    key_name=(candidate.credential['credential_id'] + ':'
                              + candidate.deployment['deployment_id']),
                    api_key=api_key,
                    model=candidate.deployment['wire_model_id'],
                    logical_model=model['model_id'],
                    capabilities=set(candidate.offering['capabilities']),
                    base_url=candidate.connection['base_url'],
                    provider_id=candidate.provider_id,
                    routing_provider_id=candidate.provider_id,
                    routing_owner_user_id=boundary.owner_user_id,
                    routing_tenant_id=boundary.tenant_id,
                    route_offering_id=candidate.offering['offering_id'],
                    route_deployment_id=candidate.deployment['deployment_id'],
                    route_connection_id=candidate.connection['connection_id'],
                    route_credential_id=candidate.credential['credential_id'],
                    local_inference=candidate_is_local_inference(candidate),
                    max_output_tokens=int(
                        candidate.deployment.get('max_output_tokens') or 0),
                    route_snapshot=snapshot.finalize(candidate),
                    # Deployment (request-name) selectors WIN over the
                    # Connection's: a gateway may expose one wire spelling
                    # only under a serving-provider header while the
                    # Connection is shared with spellings that reject it.
                    # Credential headers still win over both.
                    extra_headers={
                        **(candidate.connection.get('extra_headers') or {}),
                        **(candidate.deployment.get('extra_headers') or {}),
                        **secret_headers,
                    },
                    **candidate_inference_metadata(candidate),
                    protocol=candidate.connection['protocol'],
                    oauth=oauth,
                    adapter=dict(candidate.connection.get('adapter') or {}),
                    rpm_limit=float(
                        quota.get('rpm') or access_quota.get('rpm') or 30),
                    latency_ema=float(
                        candidate.deployment.get('latency_ms') or 3000),
                    cost_per_1k_tokens=(
                        float(pricing.get('input') or 0.0)
                        + float(pricing.get('output') or 0.0)
                    ) / 2000.0,
                )
                built.append(slot)
                entry_group.add(candidate.deployment['wire_model_id'])
            entry_groups.append((model['model_id'], entry_group))
        logger.info(
            '[Dispatch] Built %d slots from model-routing v2 revision %d',
            len(built), authority.revision)
        return built, self._build_logical_index(entry_groups)

    def _load_benchmark_data(self, slots: list[Slot]) -> None:
        """Seed configured *slots* from benchmark_results.json; prune dead ones in place."""
        benchmark_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'debug', 'benchmark_results.json'
        )
        if not os.path.exists(benchmark_file):
            logger.info('No benchmark data found — using defaults')
            return

        try:
            with open(benchmark_file) as f:
                data = json.load(f)
        except Exception as e:
            logger.error('Failed to load benchmark data: %s', e, exc_info=True)
            return

        models_data = data.get('models', {})

        # Build reverse map: benchmark key label -> our key_name
        # e.g. benchmark has "primary"/"secondary", we have "key_0"/"key_1"
        bench_keys = data.get('keys', {})        # {"primary": "...8427", ...}
        bench_label_to_ours = {}
        for bench_label, bench_suffix in bench_keys.items():
            for slot in slots:
                if slot.api_key.endswith(bench_suffix.lstrip('.')):
                    bench_label_to_ours[bench_label] = slot.key_name
                    break

        updated = 0
        dead_slots = []

        for slot in slots:
            entry_key = f'{slot.key_name}:{slot.model}'
            entry = models_data.get(entry_key)
            # Also try matching via benchmark label mapping
            if not entry:
                for bench_label, our_name in bench_label_to_ours.items():
                    if our_name == slot.key_name:
                        entry = models_data.get(f'{bench_label}:{slot.model}')
                        if entry:
                            break
            if not entry:
                continue

            # Check if probe showed this pair is *permanently* dead
            # Only prune on clear "invalid model" / HTTP 400 — NOT on
            # transient errors, parsing bugs, or rate-limiting (429)
            probe = entry.get('probe', {})
            if not probe.get('alive', True):
                err = str(probe.get('error', '')).lower()
                if 'invalid model' in err or ('http 400' in err and 'rate' not in err):
                    dead_slots.append(slot)
                    continue
                # Otherwise treat as transient — keep the slot

            # Seed RPM from benchmark
            rpm_data = entry.get('rpm', {})
            if rpm_data and 'rpm_effective' in rpm_data:
                rpm_val = rpm_data['rpm_effective']
                if rpm_val <= 0:
                    # All requests got 429 — this key has no quota for this model
                    dead_slots.append(slot)
                    continue
                slot.set_rpm_ceiling(rpm_val)

            # Seed latency from benchmark (use speed data first, then latency)
            speed = entry.get('speed', {})
            lat = entry.get('latency', {})

            if speed and 'avg_ttft_ms' in speed:
                slot.ttft_ema = speed['avg_ttft_ms']
            if lat and 'avg_latency_ms' in lat:
                slot.latency_ema = lat['avg_latency_ms']
            elif speed and 'avg_ttft_ms' in speed:
                # Estimate E2E latency from TTFT + generation time
                tps = speed.get('avg_tokens_per_sec', 30)
                avg_tokens = speed.get('avg_total_tokens', 100)
                slot.latency_ema = speed['avg_ttft_ms'] + (avg_tokens / max(tps, 1)) * 1000

            # Update vision capability from benchmark
            vision = entry.get('vision', {})
            if vision.get('vision_ok') is True:
                slot.capabilities.add('vision')
            elif vision.get('vision_ok') is False:
                slot.capabilities.discard('vision')

            updated += 1

        # Remove dead slots
        if dead_slots:
            for s in dead_slots:
                slots.remove(s)
                logger.debug('  [Dispatch] Removed dead slot: %s:%s', s.key_name, s.model)

        logger.info('Loaded benchmark data: %d slots updated, %d dead removed',
                    updated, len(dead_slots))

    def _build_logical_index(self, entry_groups: list) -> dict[str, str]:
        """Build name → logical model_id from each entry's OWN identity set.

        Owner directive 2026-08-06: routing keys on ``model_id`` ALONE.
        ``entry_groups`` is a list of ``(model_id, {model_id} ∪ wire ids)``
        pairs — one per configured model entry. A name resolves back to the
        entry that owns it, but two entries are NEVER unioned into one
        routing group. The old union-find over ``{model_id} ∪ aliases`` ×
        static ``MODEL_ALIAS_GROUPS`` let one provider's alias hijack
        another provider's model (2026-08-06 incident: the official-DeepSeek
        entry ``deepseek-v4-flash1`` carried alias ``deepseek-v4-flash`` →
        connected-component merging glued it to the gateway's same-named
        deployment, and every «official» request silently routed to the
        gateway mirror).

        Conflict rule: an exact ``model_id`` always owns its own name; a
        wire id claimed by two entries resolves to the FIRST claim and is
        logged — an ambiguous config must be loud, not silently widen.
        """
        index: dict[str, str] = {}
        direct = {mid for mid, _ in entry_groups if mid}
        for model_id, _ in entry_groups:
            if model_id:
                index[model_id] = model_id
        claimed_by: dict[str, str] = {}
        conflicts: dict[str, set] = {}
        for model_id, names in entry_groups:
            for name in (names or ()):
                if not name or name == model_id:
                    continue
                if name in direct:
                    # Another entry's model_id owns this name outright.
                    conflicts.setdefault(name, set()).update({name, model_id})
                    continue
                prior = claimed_by.get(name)
                if prior is not None and prior != model_id:
                    conflicts.setdefault(name, set()).update({prior, model_id})
                    continue  # first claim wins
                claimed_by[name] = model_id
                index[name] = model_id
        for name, claimants in sorted(conflicts.items()):
            logger.warning('[Dispatch] model id %r is claimed by multiple '
                           'entries %s — routing it by exact model_id only; '
                           'rename or drop the duplicate alias to silence '
                           'this', name, sorted(claimants))
        return index

    def _route_logical(self, model: str) -> str | None:
        """Resolve a picker/preset/stored-conv name to its logical model_id.

        Returns None when nothing configured owns the name. Order:
        the name→entry index (covers every entry's own wire spellings),
        exact configured model_id, then a static ``MODEL_ALIAS_GROUPS``
        group with EXACTLY ONE configured member (lets a conversation
        persisted against a legacy cross-naming spelling — e.g. a
        Bedrock-native id — keep routing to its one configured home).
        """
        if not model:
            return None
        hit = self._logical_index.get(model)
        if hit:
            return hit
        if model in self._direct_models:
            return model
        group = MODEL_ALIASES.get(model)
        if group:
            configured = [m for m in group if m in self._direct_models]
            if len(configured) == 1:
                return configured[0]
        return None

    def _prefer_matcher(self, prefer_model):
        """Return a slot predicate for ``prefer_model`` (None → no preference).

        model_id-only routing: a slot serves the preference iff its
        ``logical_model`` equals the resolved logical id. When the name
        resolves to nothing configured (unknown / removed entry), the
        predicate falls back to exact wire-id equality so a conversation
        persisted against such a spelling still lands on a slot literally
        serving it. Request-scoped v2 slots are injected after this index is
        built, so their explicit ``logical_model`` is also an exact match.
        """
        if not prefer_model:
            return None
        logical = self._route_logical(prefer_model)
        if logical is not None:
            return lambda s: s.logical_model == logical
        return lambda s: (
            s.model == prefer_model or s.logical_model == prefer_model)

    def pick_slot(self, capability='text', prefer_model=None,
                  exclude_models=None, exclude_keys=None,
                  exclude_pairs=None, strict_model=False,
                  prefer_fast=False, rotate_models=None) -> Slot | None:
        """Pick the best available slot for the given capability.

        Args:
            capability: Required capability ('text', 'vision', 'thinking', 'cheap')
            prefer_model: If set, prefer this specific model name
            exclude_models: Set of model names to exclude
            exclude_keys: Set of key names to exclude (e.g. after a key-level failure)
            exclude_pairs: Set of (key_name, model) tuples to exclude (e.g. after
                           a permission error on a specific key+model combination)
            strict_model: If True AND prefer_model is set, NEVER fall back to a
                          different model — return None instead.  Use this when the
                          frontend user explicitly chose a model.
            prefer_fast: Prefer the fast tier without narrowing eligibility:
                          among candidates whose Model is fast (Artificial
                          Analysis evidence, else this process's measured
                          route throughput) and that are not cooling, take
                          the soonest-admissible gates, then the highest AA
                          Intelligence Index. Otherwise every candidate is
                          considered as usual. An explicit ``prefer_model`` or
                          provider pin outranks it.
            rotate_models: ``{logical Model: ordinal}`` of the Models this
                          dispatch already saw rate-limited (latest limit
                          last). A stateless pick (no explicit model,
                          provider pin or conversation warm key) takes other
                          Models first, preferring those no gate reports as
                          rejecting; once every candidate Model was limited,
                          the one limited longest ago. It orders candidates
                          and never removes the last of them.

        Returns:
            Best Slot, or None if nothing is available.
        """
        return self._pick(capability, prefer_model, exclude_models,
                          exclude_keys, exclude_pairs=exclude_pairs,
                          reserve=False, strict_model=strict_model,
                          prefer_fast=prefer_fast,
                          rotate_models=rotate_models)

    def pick_and_reserve(self, capability='text', prefer_model=None,
                         exclude_models=None, exclude_keys=None,
                         exclude_pairs=None, strict_model=False,
                         prefer_fast=False, rotate_models=None) -> Slot | None:
        """Atomically pick the best slot AND increment its inflight counter.

        This prevents the thundering-herd problem where N concurrent threads
        all see inflight=0 and pick the same slot.  The caller MUST call
        ``slot.record_success(...)`` or ``slot.record_error(...)`` when done
        to decrement inflight.

        Args:
            exclude_pairs: Set of (key_name, model) tuples to exclude (e.g. after
                           a permission error on a specific key+model combination)
            strict_model: If True AND prefer_model is set, NEVER fall back to a
                          different model — return None instead.
            prefer_fast: See :meth:`pick_slot`.
            rotate_models: See :meth:`pick_slot`.

        Returns:
            Best Slot with inflight already incremented, or None.
        """
        return self._pick(capability, prefer_model, exclude_models,
                          exclude_keys, exclude_pairs=exclude_pairs,
                          reserve=True, strict_model=strict_model,
                          prefer_fast=prefer_fast,
                          rotate_models=rotate_models)

    # Capabilities that are NOT chat-compatible — after pricing-only tags are
    # ignored, a slot is treated as non-chat when its remaining capabilities
    # are a subset of ``_NON_CHAT_CAPS``. 'transcription' (audio → text via
    # /audio/transcriptions) is selected directly by lib/transcription.py
    # scanning the slot pool, never through the chat picker; 'audio_chat'
    # is here so a slot carrying ONLY {audio_chat} (no text) is excluded,
    # while real omni chat slots carrying {text, audio_chat, ...} are NOT
    # a subset and remain chat-eligible. Single source of truth lives in
    # lib.model_info.capability_taxonomy (DISPATCHER_NON_CHAT_CAPS is
    # CHAT_EXCLUDED_CAPS | {'audio_chat'} — the difference is intentional).
    _NON_CHAT_CAPS = DISPATCHER_NON_CHAT_CAPS

    def _is_chat_compatible(self, slot) -> bool:
        """Return whether operational capabilities expose a chat endpoint.

        Pricing tiers such as ``cheap`` describe cost, not protocol support.
        Ignoring them here prevents a stale ``{embedding, cheap}`` model from
        escaping the non-chat guard and reaching ``/chat/completions``.
        """
        operational_capabilities = (
            set(slot.capabilities) - set(MANAGED_TIER_TAGS)
        )
        return bool(operational_capabilities) and not (
            operational_capabilities.issubset(self._NON_CHAT_CAPS)
        )

    def _pick(self, capability, prefer_model, exclude_models,
              exclude_keys, *, exclude_pairs=None, reserve=False,
              strict_model=False, prefer_fast=False,
              rotate_models=None) -> Slot | None:
        """Select one slot and record the selection latency for every thread.

        ``route_selection`` is otherwise timed only inside an admitted
        translation timing scope; agent and background threads report here.
        """
        started = time.monotonic()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        chosen = None
        try:
            chosen = self._pick_in_scope(
                capability, prefer_model, exclude_models, exclude_keys,
                exclude_pairs=exclude_pairs, reserve=reserve,
                strict_model=strict_model, pinned_provider=pinned_provider,
                prefer_fast=prefer_fast, rotate_models=rotate_models)
            return chosen
        finally:
            _observe_route_selection(
                time.monotonic() - started, bool(pinned_provider), chosen)

    def _pick_in_scope(self, capability, prefer_model, exclude_models,
                       exclude_keys, *, exclude_pairs, reserve, strict_model,
                       pinned_provider, prefer_fast=False,
                       rotate_models=None) -> Slot | None:
        """Internal pick logic — optionally atomic with record_request.

        Args:
            strict_model: When True AND prefer_model is set, the picker will
                NEVER fall back to a different model.  If no slot serving
                the preferred model's logical id is available, returns
                None so the retry loop can wait for cooldown to expire.
                Use this for **user-facing requests** where the frontend
                explicitly chose a model (e.g. "opus" preset).
                Leave False (default) for **backend auto tasks** (compaction,
                daily reports, analysis) where cross-model fallback is fine.
        """
        self.initialize()

        def selectable(slot, required) -> bool:
            """Pool-state filters, cheapest first; key health comes last."""
            if required not in slot.capabilities or not slot.is_available:
                return False
            if exclude_models and slot.model in exclude_models:
                return False
            if exclude_keys and slot.key_name in exclude_keys:
                return False
            if exclude_pairs and (slot.key_name, slot.model) in exclude_pairs:
                return False
            # Guard: never dispatch embedding/image_gen-only slots
            #   for chat operations (safety net against capability leaks).
            #   Skip the guard when the caller explicitly asks for a
            #   non-chat capability (image_gen, embedding).
            return (required in self._NON_CHAT_CAPS
                    or self._is_chat_compatible(slot))

        # Speed evidence is read before any lock (it may stat the shared AA
        # cache). An explicit model or a provider pin outranks the tier.
        speed_index = None
        if prefer_fast and not prefer_model and not pinned_provider:
            from lib.model_catalog.aa import routing_speed_index
            speed_index = routing_speed_index()

        # ── Hard provider pin (multi-tenant isolation) ──
        # When the current task thread is bound to a request-scoped v2 route
        # group, the picker may ONLY select that group's slots — for EVERY
        # capability, never silently falling back to an operator-curated
        # key. See lib/llm_dispatch/provider_pin.py for the full rationale
        # (the cross-request credential leak this prevents). The pool scope
        # is that group alone; an unpinned context sees only configured
        # slots (slot_pool.py), so neither scans the other's slots.
        # Entering the key-health batch checks key-store freshness once,
        # before the global lock. Key health is then read under the lock:
        # serialized picks keep the key-stats lock uncontended, whereas
        # concurrent picks interleaving one acquisition per slot convoyed on
        # it under GIL pressure (docs/modules/llm_io.md, "Slot pool scopes").
        probes = self._billing_probe_registry() if reserve else None
        # A Watchdog Turn steps off throttled AppIds (quota_watch); read
        # before the lock, since the open alerts are durable records.
        from lib.llm_dispatch.quota_watch import throttled_appids
        avoided_appids = throttled_appids()
        with _KeyAdmission(credentials=self._credential_index,
                           probes=probes) as key_health:
            with self._lock:
                return self._select_locked(
                    self._selection_scope(pinned_provider), selectable,
                    key_health, capability=capability,
                    prefer_model=prefer_model, reserve=reserve,
                    strict_model=strict_model,
                    pinned_provider=pinned_provider,
                    speed_index=speed_index, rotate_models=rotate_models,
                    avoided_appids=avoided_appids)

    def _select_locked(self, scope, selectable, key_health, *, capability,
                       prefer_model, reserve, strict_model,
                       pinned_provider, speed_index=None,
                       rotate_models=None,
                       avoided_appids=None) -> Slot | None:
        """Choose among *scope* while holding ``self._lock``."""
        candidates = [slot for slot in scope
                      if selectable(slot, capability) and key_health.admits(slot)]
        if not candidates:
            # Pinned-provider isolation: a pinned task whose own slot
            #   is momentarily unavailable (cooldown/excluded) must WAIT,
            #   never widen onto an operator key. Return None so the
            #   dispatch retry loop keeps cycling within the provider.
            if pinned_provider:
                return None
            # strict_model: if the user chose a specific model and all
            #   its slots are in cooldown, return None immediately so the
            #   retry loop waits — do NOT fall back to another model.
            if strict_model and prefer_model:
                return None
            # Fallback: try ignoring capability constraint for text
            if capability != 'text':
                candidates = [slot for slot in scope
                              if selectable(slot, 'text')
                              and key_health.admits(slot)]
            if not candidates:
                return None

        # ── Conversation-sticky routing ──
        # Anthropic's prompt cache is keyed per API key, so a conversation
        # must keep landing on the SAME key round-to-round or every flip
        # costs a full cache_creation write + 0% read. When this thread is
        # bound to a conv (run_task sets it) and that conv has a recent
        # sticky key, prefer the eligible candidate on that key over the
        # raw min-score pick. The sticky key is a SOFT preference: if it's
        # not among the eligible candidates (cooled down / excluded /
        # disabled), we fall through to score-based selection and rebind.
        _sticky = (sticky_routing_enabled() and get_conv_affinity()) or None
        _sticky_route_key = str(prefer_model or f'cap:{capability}')
        _sticky_affinity = (get_preferred_key(
            _sticky, route_key=_sticky_route_key) if _sticky else None)

        def _sticky_in(pool):
            """The eligible slot of *pool* on this conversation's warm key."""
            if not _sticky_affinity:
                return None
            # Match on the STABLE affinity identity (route credential
            # + deployment), not the request-random key_name, so a
            # failover rebind survives the next turn's fresh mint.
            on_key = [s for s in pool
                      if s.affinity_key() == _sticky_affinity]
            if not on_key:
                return None
            # Only honor the sticky key when it isn't in cooldown
            # (score=inf). Otherwise let the normal picker route around it.
            best_sticky = min(on_key, key=lambda s: s.score())
            return best_sticky if best_sticky.score() != float('inf') else None

        def _select(pool):
            """Pick the best slot in *pool*, honoring the sticky key when eligible."""
            selection_now = time.monotonic()
            self._expire_shared_contention_locked(selection_now)
            if avoided_appids is not None:
                pool = self._off_credentials(
                    pool, avoided_appids | self._rejecting_credentials_locked())
            warm = _sticky_in(pool)
            # A paced gate is no reason to leave the warm key, whose prompt
            # cache a switch would re-bill; only a gate that has rejected for
            # a full provider window, or a provider pacing queue a full
            # window deep, yields to the slot that can send sooner.
            if (warm is not None
                    and not self._rejecting_past_window_locked(
                        warm, selection_now)
                    and self._pacing_delay(warm) < WINDOW_S):
                return warm
            pool = self._earliest_shared_contention_pool_locked(
                pool, selection_now)
            warm = _sticky_in(pool)
            if warm is not None:
                return warm
            if (pinned_provider and pool
                    and all(getattr(s, 'route_rank', -1) >= 0 for s in pool)):
                # Eligibility and contention already filtered this
                # request's authorized group, and scores cannot reorder
                # its compiler-owned provider/deployment preference.
                # Cooldown is NOT a candidate filter (score() reports it
                # as inf), so readiness gates the rank order here: a
                # cooling top rank must not shadow a ready sibling. With
                # every route cooling, rank order names the fallback and
                # a strict caller waits on it.
                ready = [s for s in pool if s.score() != float('inf')]
                return min(ready or pool, key=lambda s: s.route_rank)
            return min(pool, key=lambda s: s.score())

        if prefer_model:
            # model_id-only routing (owner directive 2026-08-06): a slot
            # is preferred iff it serves the resolved logical model_id.
            matcher = self._prefer_matcher(prefer_model)
            preferred = [s for s in candidates if matcher(s)]
            if preferred:
                chosen = _select(preferred)
            elif strict_model:
                # User explicitly chose this model — all its slots are
                #   in candidates but none match the logical id (shouldn't
                #   happen normally, but guard against it).  Return None.
                return None
            else:
                chosen = _select(candidates)
        else:
            # A warm conversation key keeps its cache-preserving pacing in
            # ``_select``; a pinned group keeps its compiler-owned rank order.
            pool = (candidates if pinned_provider or _sticky_affinity
                    else self._rotation_pool_locked(candidates, rotate_models))
            fast = []
            if speed_index is not None:
                measured = measured_speed_evidence(
                    slot for slot in scope if capability in slot.capabilities)
                fast = self._fast_preferred_pool_locked(
                    pool, speed_index, measured)
            chosen = _select(fast or pool)

        # strict_model: if the best candidate has score=inf it means
        #   all matching slots are in cooldown.  Return None so the
        #   retry loop waits — don't silently dispatch a cooldown'd slot
        #   or fall back to a different model.
        if strict_model and chosen.score() == float('inf'):
            return None

        # ── Record the chosen key as this conv's sticky key ──
        # Done for every pick (not just sticky hits) so the FIRST round of
        # a conversation seeds the affinity, and a forced fallback (sticky
        # key cooled down) rebinds to the healthy key it landed on.
        if _sticky and chosen is not None:
            # A churn signal worth grepping: the conv had a sticky key but
            # the picker landed elsewhere (cooled down / excluded), which
            # costs a fresh per-key prompt-cache write this round. Logged at
            # INFO (not DEBUG) because this is the exact event that re-bills
            # the prompt cache — production app.log is INFO+, so a DEBUG line
            # left us blind to the most expensive routing decision.
            _chosen_affinity = chosen.affinity_key()
            _fell_back = bool(_sticky_affinity
                              and _chosen_affinity != _sticky_affinity)
            if _fell_back:
                logger.info('[Dispatch] conv=%s sticky key %s unavailable '
                            '— rebinding to %s (model=%s); prompt cache will '
                            'be re-written on the new key',
                            _sticky[:8], _sticky_affinity, _chosen_affinity,
                            chosen.model)
            # Diagnostic: record WHY the key differed (soft-fallback vs
            # no-affinity) so the cache byte-probe can classify a routing
            # flip. Best-effort, never affects routing.
            try:
                from lib.llm_dispatch.conv_affinity import record_pick_decision
                record_pick_decision(
                    preferred_key=_sticky_affinity,
                    chosen_key=_chosen_affinity,
                    fell_back=_fell_back)
            except Exception as _pd_err:
                logger.debug('[Dispatch] pick-decision record failed: %s', _pd_err)
            record_conv_key(_sticky, _chosen_affinity,
                            route_key=_sticky_route_key)

        # ── Isolation observability ──
        # One line per pick so a provider leak is a single grep:
        #   pinned=ephemeral:… but provider=… mismatched → leak.
        # Only emitted when a pin is active (operator UI traffic stays
        # quiet). debug-level: high volume, on the hot path.
        if pinned_provider:
            logger.debug('[Dispatch] pick model=%s provider=%s key=%s '
                         'pinned=%s', chosen.model, chosen.provider_id,
                         chosen.key_name, pinned_provider)

        if reserve:
            chosen.record_request()  # atomic: inflight++ while still holding lock
            probe = key_health.probing.get(id(chosen))
            if probe is not None:
                self._billing_probe_registry()[probe] = chosen
                logger.info('[Dispatch] %s/%s:%s is the one probe of a 402 '
                            'stop past the provider\'s day boundary',
                            probe[0], probe[1], chosen.model)

        return chosen

    def _billing_probe_registry(self) -> dict:
        """In-flight billing probes by credential-health identity.

        At most one entry per credential; an entry whose slot has nothing in
        flight is finished and admits the next probe.
        """
        registry = getattr(self, '_billing_probes', None)
        if registry is None:
            registry = self._billing_probes = {}
        return registry

    def _rotation_pool_locked(self, candidates, rotate_models) -> list[Slot]:
        """The candidates on the Models a stateless pick should try next.

        A rate limit is evidence about a Model's quota, not only one key's: on
        2026-11-12 every gateway AppId serving four Gemini Models answered 429
        for hours while other Models of the same tier served, and rotating
        keys within one Model spent a translation's whole 429 budget on it.
        Models this dispatch saw limited go last, behind Models whose gate is
        rejecting other requests right now (an unbroken rejection run that a
        success or the decay grace ends). Once every candidate Model was
        limited, the one limited longest ago is retried on its soonest gate.
        The order never empties the candidates.
        """
        limited = rotate_models or {}
        self._expire_shared_contention_locked(time.monotonic())
        fresh = [slot for slot in candidates
                 if _model_identity(slot) not in limited]
        if not fresh:
            oldest = min(limited[_model_identity(slot)] for slot in candidates)
            return self._off_rejecting_credentials_locked(
                [slot for slot in candidates
                 if limited[_model_identity(slot)] == oldest])
        rejecting = {_model_identity(slot) for slot in fresh
                     if self._rejecting_locked(slot)}
        return self._off_rejecting_credentials_locked(
            [slot for slot in fresh
             if _model_identity(slot) not in rejecting] or fresh)

    def _off_rejecting_credentials_locked(self, pool) -> list[Slot]:
        """Prefer *pool*'s slots whose credential is not in a rejection run.

        A gateway AppId throttled on one Model is usually throttled on its
        others too (2026-11-18: AppId …5409 refused gemini-3.8-flash and then
        gpt-6-luna within one fetch_url extract while other AppIds served
        both). Only a per-credential lane gate is evidence about an AppId; a
        provider/model project limit is not. Never empties the pool.
        """
        return self._off_credentials(pool, self._rejecting_credentials_locked())

    def _rejecting_credentials_locked(self) -> set:
        """``(provider, credential)`` of every per-credential lane gate in a rejection run."""
        return {(provider, credential)
                for (provider, credential, _model), state
                in self._contention_strikes.items()
                if credential and state.rejecting_since is not None}

    @staticmethod
    def _off_credentials(pool, credentials) -> list[Slot]:
        """*pool*'s slots on other credentials than *credentials*; never empties the pool."""
        if not credentials:
            return pool
        return [slot for slot in pool
                if slot.quota_lane()[:2] not in credentials] or pool

    def _rejecting_locked(self, slot) -> bool:
        """Whether a gate of *slot* is inside an unbroken rejection run."""
        return any(state.rejecting_since is not None
                   for _, state in self._armed_gates_locked(slot))

    def _fast_preferred_pool_locked(self, candidates, speed_index,
                                    measured) -> list[Slot]:
        """The best-quality, soonest-admissible fast candidates, or ``[]``.

        Fast means Artificial Analysis measured the slot's Model above the
        snapshot's median output speed or, for a Model without fresh AA
        evidence, that this process measured its routes above the median
        measured Model of the capability (``measured``). Cooling slots never
        qualify, so a cooled-down fast tier defers to the whole pool instead
        of parking the request. Gateway 429s arm admission gates rather than
        cooldowns: the soonest gate comes first, so a fast model admissible
        now beats a smarter one still waiting, then the highest Intelligence
        Index wins (unscored last). ``_select`` spreads load across that
        Model's slots.
        """
        now = time.monotonic()
        self._expire_shared_contention_locked(now)
        evidence = {}
        for slot in candidates:
            identity = _model_identity(slot)
            speed = speed_index.evidence(identity)
            if speed is None:
                speed = measured.get(identity)
            if speed is not None and speed.fast and slot.score() != float('inf'):
                evidence[id(slot)] = speed
        if not evidence:
            return []
        soonest = self._earliest_shared_contention_pool_locked(
            [slot for slot in candidates if id(slot) in evidence], now)

        def quality(slot) -> float:
            intelligence = evidence[id(slot)].intelligence
            return float('-inf') if intelligence is None else intelligence

        best = max(quality(slot) for slot in soonest)
        return [slot for slot in soonest if quality(slot) == best]

    def pick_top_n(self, n=2, capability='text', prefer_model=None,
                   exclude_models=None, reserve=True) -> list[Slot]:
        """Pick the top N slots for racing (dispatch_fastest).

        Args:
            reserve: If True, atomically increment inflight on each
                     returned slot (default True).
        """
        self.initialize()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()

        with _KeyAdmission(credentials=self._credential_index) as key_health, \
                self._lock:
            candidates = []
            for slot in self._selection_scope(pinned_provider):
                if capability not in slot.capabilities:
                    continue
                if exclude_models and slot.model in exclude_models:
                    continue
                if not slot.is_available or not key_health.admits(slot):
                    continue
                candidates.append(slot)

            if not candidates:
                return []

            # Sort by score (lower = better)
            candidates.sort(key=lambda s: s.score())

            # If prefer_model, ensure its logical-model slots lead the list
            if prefer_model:
                matcher = self._prefer_matcher(prefer_model)
                preferred = [s for s in candidates if matcher(s)]
                others = [s for s in candidates if not matcher(s)]
                result = preferred[:n]
                for s in others:
                    if len(result) >= n:
                        break
                    result.append(s)
            else:
                result = candidates[:n]

            if reserve:
                for s in result:
                    s.record_request()

            return result

    def pick_best_slots(self, capability='text', n=5) -> list[Slot]:
        """Return the top-N available slots for a capability, sorted by score.

        Useful for callers that need a list of models for their own
        round-robin or parallel dispatch (e.g. pdf_parser VLM).
        """
        self.initialize()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        with _KeyAdmission(credentials=self._credential_index) as key_health, \
                self._lock:
            candidates = [s for s in self._selection_scope(pinned_provider)
                          if capability in s.capabilities and s.is_available
                          and key_health.admits(s)]
            candidates.sort(key=lambda s: s.score())
            return candidates[:n]

    def record_truncation(self, key_name: str, model: str, error: str = '') -> bool:
        """Record a truncated/empty-output event against a specific (key, model) slot.

        This is a soft failure path used by callers like the translate retry
        loop: the HTTP call succeeded but the body was unusable (mid-output
        truncation, blank reply on non-empty input). Bumping the slot's
        consecutive_errors makes ``score()`` deprioritize it on the next pick
        across the whole process — not just within the current retry loop.

        Returns True if a matching slot was found and recorded.
        """
        with self._lock:
            for s in self.slots:
                if s.key_name == key_name and s.model == model:
                    s.record_truncation(error=error)
                    return True
        logger.debug('[Dispatch] record_truncation: no slot %s:%s found',
                     key_name, model)
        return False

    def mark_model_route_missing(
            self, *, provider_id: str, model: str, error: str = '') -> int:
        """Disable an unserved provider/wire-model route until pool rebuild.

        The gateway's explicit route-missing verdict is model-scoped: trying
        another key on the same provider repeats the same deterministic 400.
        Disable every matching slot in this process so later LLM rounds do not
        pay for the same failed probe.  Settings/catalog refresh calls
        ``reset_dispatcher()``, which rebuilds the slots and is the explicit
        recovery boundary if the upstream route later appears.

        Returns the number of slots whose availability changed.
        """
        provider_id = str(provider_id or '')
        model = str(model or '')
        if not model:
            return 0
        changed = 0
        with self._lock:
            for slot in self._pool.by_provider(provider_id):
                if slot.model != model:
                    continue
                if slot.mark_route_missing(error):
                    changed += 1
        if changed:
            logger.warning(
                '[Dispatch] Disabled %d slot(s) for unserved route %s/%s '
                'until the dispatcher is rebuilt',
                changed, provider_id or '?', model)
        return changed

    def pick_key_for_model(self, model: str) -> tuple:
        """Pick the best API key for a given model based on current load.

        This is the **key rotation** API — for callers who already know which
        model they want (e.g. orchestrator with user-selected preset) but need
        to spread load across keys.

        Returns ``(api_key, key_name, slot)``. Unknown models fail closed as
        ``('', '', None)``; a process-global key must never substitute for an
        owner-authorized route.
        """
        self.initialize()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        with _KeyAdmission(credentials=self._credential_index) as key_health, \
                self._lock:
            candidates = [s for s in self._selection_scope(pinned_provider)
                          if s.model == model and s.is_available
                          and key_health.admits(s)]
            if not candidates:
                return '', '', None

            best = min(candidates, key=lambda s: s.score())
            return best.api_key, best.key_name, best

    def has_capable_slots(self, capability: str = 'text',
                          exclude_models=None, exclude_keys=None,
                          exclude_pairs=None, prefer_model=None) -> bool:
        """True if at least one slot CAN serve ``capability`` ignoring
        transient cooldown / rpm state.

        Used by the dispatch retry loops to distinguish two ``pick_slot``
        ``None`` outcomes that need OPPOSITE handling:
          * slots exist but are all in 0.5s rate-limit cooldown → the
            request should keep fast-polling (a 429-equivalent), NOT give
            up — otherwise a fresh concurrent request that arrives while
            every slot is cooling fails immediately on attempt 1.
          * no slot has the capability at all (or all are permanently
            excluded) → genuinely unservable, give up.

        Only durable disqualifiers (capability, hard exclusions,
        chat-compatibility) are checked here; cooldown / inflight / rpm are
        deliberately ignored. Inside ``strict_billing_stop_admission()``,
        key-health rejection is durable for the optional request too: treating
        a recorded 402/quota stop as cooldown would otherwise poll every 300ms
        until the caller deadline despite having no admissible route.

        ``prefer_model`` (passed by the dispatch loops ONLY under
        ``strict_model``) narrows the answer to the preferred model's
        logical id: a strict-pinned loop can never pick outside that
        model, so healthy slots of OTHER models must not keep it cycling
        (2026-08-03 incident: kimi-k3's sole key permission-excluded →
        the pool's healthy opus/glm slots answered True here, the loop
        spun ~2min resurrecting the dead pair every 60s instead of
        failing over to the pool rescue immediately)."""
        self.initialize()
        ex_models = exclude_models or set()
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        matcher = self._prefer_matcher(prefer_model) if prefer_model else None
        # Respect the thread's hard provider pin (same isolation rule as
        # _pick): a pinned task only "has capable slots" among its own
        # provider's slots, so the retry loop waits for THAT provider to
        # recover instead of treating operator slots as a fallback.
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        from lib.key_stats import is_strict_billing_stop_admission

        # Key health counts for a recorded billing stop under EVERY admission
        # policy: a key the provider has already refused for lack of credit is
        # a durable disqualifier, not a transient cooldown, so treating it as
        # healable capacity would poll this pool forever without sending a
        # request. Credential-health (success-rate) disables still count only
        # under strict billing-stop admission, where last-resort promotion is
        # disabled too; outside it they keep their last-resort reprieve.
        with _KeyAdmission(credentials=self._credential_index,
                           billing_stops_only=not is_strict_billing_stop_admission()) as key_health:
            with self._lock:
                return any(
                    capability in s.capabilities and s.is_available
                    and s.model not in ex_models and s.key_name not in ex_keys
                    and (s.key_name, s.model) not in ex_pairs
                    and self._is_chat_compatible(s)
                    and (matcher is None or matcher(s))
                    and key_health.admits(s)
                    for s in self._selection_scope(pinned_provider))

    def route_admission(self, prefer_model: str, capability: str = 'text') -> dict:
        """Read-only: could a request strictly for ``prefer_model`` start now?

        ``{'admissible': True}`` when the picker would select one of its slots
        now (nothing is reserved). Otherwise ``holds`` lists each capable slot of
        that model that is cooling: its reason, its end and whether that end is
        the time the provider itself named (``providerNamed``) rather than our
        own steering/backoff timing. No holds means no admissible route exists.
        Scheduled admission (docs/ROUTER.md R10 portfolio) reads this instead of
        guessing provider capacity.
        """
        if self.pick_slot(capability, prefer_model=prefer_model, strict_model=True) is not None:
            return {'admissible': True, 'holds': []}
        matcher = self._prefer_matcher(prefer_model)
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        now = time.time()
        with self._lock:
            holds = [{'key': s.key_name, 'model': s.model, 'reason': s.cooldown_reason or 'error',
                      'until': s.cooldown_until,
                      'providerNamed': s.provider_retry_at == s.cooldown_until}
                     for s in self._selection_scope(pinned_provider)
                     if capability in s.capabilities and self._is_chat_compatible(s)
                     and matcher(s) and s.cooldown_until > now]
        return {'admissible': False, 'holds': holds}

    def responses_state_binding(self, origin_deployments, *,
                                prefer_model=None) -> frozenset:
        """``(key_name, model)`` pairs that cannot replay this request's state.

        Encrypted Responses state decrypts only on the upstream that minted it
        (2026-10-31: failover from the Kunlun Deployment of ``gpt-6.1-sol`` to
        its public Deployment was rejected ``invalid_encrypted_content``, and
        so was the next turn). ``origin_deployments`` names the Deployments
        that minted the state a request replays. When a slot on one of them is
        in this request's scope (provider pin, preferred model), every other
        Responses slot in that scope is returned for the caller to exclude for
        the whole dispatch: failover may rotate the origin's credentials, wait
        for it or fail with its own error, never resend the ciphertext
        elsewhere. Other protocols are not returned; checked conversion omits
        or refuses the ciphertext there. Availability and exclusions are
        ignored on purpose, so a cooling or excluded origin still binds. An
        origin outside the scope (model switched, Deployment removed) binds
        nothing.
        """
        origins = frozenset(origin_deployments or ())
        if not origins:
            return frozenset()
        self.initialize()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        matcher = self._prefer_matcher(prefer_model) if prefer_model else None
        with self._lock:
            scope = self._selection_scope(pinned_provider)
            if not any(s.route_deployment_id in origins
                       and (matcher is None or matcher(s)) for s in scope):
                return frozenset()
            return frozenset((s.key_name, s.model) for s in scope
                             if s.protocol == 'responses'
                             and s.route_deployment_id not in origins)


    # ── Shared-project contention (external saturation) ──
    # A 429 naming a shared limit (RateLimitError.is_shared_contention) means
    # an upstream quota is full: a gateway AppId's minute limit belongs to
    # that credential's lane (routed provider, credential, wire model;
    # Slot.quota_lane), a vendor project limit to the provider/model project
    # every credential shares. Never park slots or feed health: serialize
    # only this process's admission at that gate. Request-scoped route groups
    # reach a gate through per-request pins, so a pin-keyed gate let every
    # waiting request probe alone and keep the provider's minute window full.
    # The first strike arms the existing 0.3s retry. Repeated rejections
    # increase the gate's probe interval 1→2→4→8→15s while every wait remains
    # abortable in bounded 3s slices. This is admission timing, never slot
    # parking or health. Automatic work prefers the slot whose gates are due
    # first, but a conversation keeps its warm key until one of its gates has
    # rejected for a full provider window (WINDOW_S): prompt caches live per
    # credential and deployment. Explicit model/provider boundaries remain
    # unchanged.
    # A success of a request sent after the gate's last rejection opens the
    # gate: every queued waiter dispatches at once, including one holding a
    # later probe slot. A success of a request sent before that rejection
    # says nothing about capacity after it and changes nothing, so settling
    # streams cannot release a herd into a full window. The next 429 re-arms
    # the gate, no sooner than the provider's own Retry-After.
    # A shared RPM/TPM window commonly survives 30 seconds. After that quiet
    # period retain only a one-second serialization seed: the first recovery
    # probe remains immediate, but concurrent arrivals cannot recreate a herd.
    # Two full minute windows without another rejection retire the state.
    _CONTENTION_DECAY_GRACE_S = 30.0
    _CONTENTION_RESET_GRACE_S = 120.0
    _CONTENTION_BASE_RETRY_S = 0.3
    _CONTENTION_PROBE_SPACING_S = 1.0
    _CONTENTION_MAX_PROBE_SPACING_S = 15.0
    _CONTENTION_STRIKES_PER_SPACING_STEP = 2
    _CONTENTION_MAX_WAIT_SLICE_S = 3.0
    _CONTENTION_MAX_GATES = 256

    @classmethod
    def _contention_probe_spacing_s(cls, strikes: int) -> float:
        """Return a bounded exponential interval for one rejection streak."""
        normalized_strikes = max(1, int(strikes))
        step = min(
            16,
            (normalized_strikes - 1)
            // cls._CONTENTION_STRIKES_PER_SPACING_STEP,
        )
        return min(
            cls._CONTENTION_MAX_PROBE_SPACING_S,
            cls._CONTENTION_PROBE_SPACING_S * (2 ** step),
        )

    @staticmethod
    def _contention_quiet_from(state) -> float:
        """When the gate's quiet time starts: its last rejection, or the end
        of the hold the provider named, whichever is later. Waiting out a
        named hold is not quiet, so it neither decays nor retires the gate
        and the rejection run (``rejecting_since``) continues across it."""
        if state.held_until is None:
            return state.last_strike_at
        return max(state.last_strike_at, state.held_until)

    def _expire_shared_contention_locked(self, now: float) -> None:
        """Decay then retire quiet gates while ``self._lock`` is held."""
        expired_keys = [
            gate
            for gate, state in self._contention_strikes.items()
            if now >= (self._contention_quiet_from(state)
                       + self._CONTENTION_RESET_GRACE_S)
        ]
        for expired_key in expired_keys:
            self._contention_strikes.pop(expired_key).opened.set()
        for gate, state in tuple(self._contention_strikes.items()):
            if now < (self._contention_quiet_from(state)
                      + self._CONTENTION_DECAY_GRACE_S):
                continue
            if state.strikes > 1:
                self._contention_strikes[gate] = replace(
                    state,
                    strikes=1,
                    next_probe_at=min(state.next_probe_at, now),
                    rejecting_since=None,
                )
            elif state.rejecting_since is not None:
                self._contention_strikes[gate] = replace(
                    state, rejecting_since=None)

    @staticmethod
    def _contention_gates(slot) -> tuple[tuple[str, str, str], ...]:
        """Gates a request on *slot* passes: its credential's quota lane and
        the provider/model project that every credential shares (``''``)."""
        provider, credential, model = slot.quota_lane()
        return (provider, credential, model), (provider, '', model)

    @staticmethod
    def pacing_lane(slot) -> tuple[str, str, str]:
        """The provider pacing lane a request on *slot* sends in: its quota
        lane, because a gateway limits each credential per wire model
        (docs/NETWORK.md "Provider pacing")."""
        return slot.quota_lane()

    def _pacing_delay(self, slot) -> float:
        """Seconds a request on *slot* would wait for its pacing turn now."""
        from lib.http_transport.provider_pacing import shared_pacer
        return shared_pacer().admission_delay(self.pacing_lane(slot))

    def _armed_gates_locked(self, slot) -> list:
        """``(gate, state)`` for each armed gate of *slot*; lock held."""
        return [(gate, self._contention_strikes[gate])
                for gate in self._contention_gates(slot)
                if gate in self._contention_strikes]

    def _earliest_shared_contention_pool_locked(
        self,
        slots: list[Slot],
        now: float,
    ) -> list[Slot]:
        """Prefer the slots that can send soonest, within the caller's set.

        A slot sends when every armed gate's probe is due and its provider
        pacing lane grants the request, so automatic work spreads onto the
        keys whose provider windows have room.
        """
        if len(slots) < 2:
            return slots
        delays = [
            max([0.0, self._pacing_delay(slot)]
                + [state.next_probe_at - now
                   for _, state in self._armed_gates_locked(slot)])
            for slot in slots
        ]
        earliest_delay = min(delays)
        return [
            slot for slot, delay in zip(slots, delays)
            if delay <= earliest_delay + 1e-6
        ]

    def _rejecting_past_window_locked(self, slot, now: float) -> bool:
        """Whether a gate of *slot* has rejected without a break for a full
        provider window, so waiting on it no longer pays for a warm cache."""
        return any(
            state.rejecting_since is not None
            and now - state.rejecting_since >= WINDOW_S
            for _, state in self._armed_gates_locked(slot))

    def shared_contention_rejecting_past_window(self, slot) -> bool:
        """Public read of :meth:`_rejecting_past_window_locked` for dispatch."""
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            return self._rejecting_past_window_locked(slot, now)

    def note_shared_contention(self, slot, *,
                               per_credential: bool = False,
                               retry_after_s: float | None = None) -> float:
        """Arm the rejected pre-request gate; never park or penalize slots.

        ``per_credential`` marks a limit of the slot's own credential (a
        gateway AppId's minute limit): only that quota lane waits and the
        model's other credentials stay open. Otherwise the provider/model
        project every credential shares waits. ``retry_after_s``, the hold
        the provider named, delays the gate's next probe at least that long.
        """
        lane, project = self._contention_gates(slot)
        gate = lane if per_credential else project
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            previous = self._contention_strikes.get(gate)
            if previous is None:
                if len(self._contention_strikes) >= self._CONTENTION_MAX_GATES:
                    oldest_key = min(
                        self._contention_strikes,
                        key=lambda candidate: self._contention_strikes[
                            candidate
                        ].last_strike_at,
                    )
                    self._contention_strikes.pop(oldest_key).opened.set()
                strikes = 1
                next_probe_at = now + self._CONTENTION_BASE_RETRY_S
                rejecting_since = now
                opened = AbortSignal()
            else:
                strikes = previous.strikes + 1
                probe_spacing = self._contention_probe_spacing_s(strikes)
                next_probe_at = max(
                    now + probe_spacing,
                    previous.next_probe_at,
                )
                rejecting_since = (
                    now if previous.rejecting_since is None
                    else previous.rejecting_since)
                opened = previous.opened
            held_until = None
            if retry_after_s is not None and math.isfinite(retry_after_s):
                next_probe_at = max(next_probe_at, now + retry_after_s)
                held_until = now + retry_after_s
            next_probe_delay = max(
                self._CONTENTION_BASE_RETRY_S, next_probe_at - now)
            retry_delay = min(
                self._CONTENTION_MAX_WAIT_SLICE_S, next_probe_delay)
            self._contention_strikes[gate] = _SharedContentionProbeState(
                strikes=strikes,
                last_strike_at=now,
                next_probe_at=next_probe_at,
                opened=opened,
                rejecting_since=rejecting_since,
                held_until=held_until,
            )
        spacing_changed = (
            strikes > 1
            and self._contention_probe_spacing_s(strikes)
            != self._contention_probe_spacing_s(strikes - 1)
        )
        provider, credential, model = gate
        if strikes <= 3 or spacing_changed or strikes % 100 == 0:
            logger.info('[Dispatch] shared-project contention on %s/%s:%s — '
                        'next probe no sooner than %.2fs (streak %d)',
                        provider, credential or '*', model,
                        next_probe_delay, strikes)
        else:
            logger.debug('[Dispatch] shared-project contention on %s/%s:%s '
                         '(next probe %.2fs, streak %d)',
                         provider, credential or '*', model,
                         next_probe_delay, strikes)
        return retry_delay

    def reserve_shared_contention_probe(
            self, slot) -> _SharedContentionAdmission:
        """Reserve the next probe of every armed gate, or one wait slice.

        No state means normal admission. If the queue is deeper than one wait
        slice, the caller sleeps and rechecks without reserving; this prevents
        every caller beyond the three-second horizon from waking together.
        Either way the caller waits on the returned openings, so a gate that
        opens releases it at once, whatever slot it held.
        """
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            gates = self._armed_gates_locked(slot)
            if not gates:
                return _SharedContentionAdmission(delay_s=0.0, admitted=True)
            openings = tuple(state.opened for _, state in gates)
            keys = tuple(gate for gate, _ in gates)
            delay_s = max(0.0, max(
                state.next_probe_at for _, state in gates) - now)
            if delay_s > self._CONTENTION_MAX_WAIT_SLICE_S:
                return _SharedContentionAdmission(
                    delay_s=self._CONTENTION_MAX_WAIT_SLICE_S,
                    admitted=False,
                    openings=openings,
                    gates=keys,
                )
            probe_at = now + delay_s
            for gate, state in gates:
                self._contention_strikes[gate] = replace(
                    state,
                    next_probe_at=(
                        probe_at
                        + self._contention_probe_spacing_s(state.strikes)
                    ),
                )
            return _SharedContentionAdmission(
                delay_s=delay_s,
                admitted=True,
                openings=openings,
                gates=keys,
            )

    def reserve_shared_contention_probe_now(
            self, slot) -> _SharedContentionAdmission:
        """Reserve a probe only when it can start now; otherwise do not mutate.

        Reconstructible background work uses this atomic admission form to
        yield instead of joining an already-known gate wait. A due probe is
        still reserved so concurrent optional arrivals cannot all observe the
        same zero-delay window and form a new request herd.
        """
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            gates = self._armed_gates_locked(slot)
            if not gates:
                return _SharedContentionAdmission(
                    delay_s=0.0,
                    admitted=True,
                )
            delay_s = max(0.0, max(
                state.next_probe_at for _, state in gates) - now)
            if delay_s > 0:
                return _SharedContentionAdmission(
                    delay_s=delay_s,
                    admitted=False,
                )
            for gate, state in gates:
                self._contention_strikes[gate] = replace(
                    state,
                    next_probe_at=(
                        now + self._contention_probe_spacing_s(state.strikes)
                    ),
                )
            return _SharedContentionAdmission(
                delay_s=0.0,
                admitted=True,
            )

    def note_shared_success(self, slot, *, admitted_at: float) -> bool:
        """Open the slot's gates that last rejected before this request left.

        ``admitted_at`` is the monotonic instant the succeeding request was
        cleared to send. Its success proves the provider accepted traffic
        after such a gate's last rejection: the gate retires and every waiter
        queued on it dispatches at once, whatever later probe slot it held,
        and a warm conversation's rejection run ends with it. A request sent
        at or before the latest rejection proves nothing about capacity after
        it, so that gate stays armed. Returns whether any gate opened.
        """
        now = time.monotonic()
        opened = []
        with self._lock:
            self._expire_shared_contention_locked(now)
            for gate, state in self._armed_gates_locked(slot):
                if admitted_at <= state.last_strike_at:
                    continue
                del self._contention_strikes[gate]
                state.opened.set()
                opened.append((gate, state))
        for (provider, credential, model), state in opened:
            logger.info('[Dispatch] shared-project contention on %s/%s:%s '
                        'opened by a success %.1fs after its last '
                        'rejection (streak %d)', provider, credential or '*',
                        model, now - state.last_strike_at, state.strikes)
        return bool(opened)

    def get_shared_contention_info(self) -> list[dict]:
        """Return the bounded live gate state for diagnostics.

        ``scope`` is ``credential`` for one credential's quota lane and
        ``project`` (``credential`` empty) for the provider/model project.
        """
        self.initialize()
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            return [
                {
                    'provider_id': provider_id,
                    'credential': credential,
                    'scope': 'credential' if credential else 'project',
                    'model': model,
                    'strikes': state.strikes,
                    'probe_spacing_s': self._contention_probe_spacing_s(
                        state.strikes),
                    'next_probe_in_s': round(
                        max(0.0, state.next_probe_at - now), 3),
                    'rejecting_for_s': (
                        None if state.rejecting_since is None
                        else round(now - state.rejecting_since, 3)),
                }
                for (provider_id, credential, model), state in sorted(
                    self._contention_strikes.items())
            ]

    def cooling_cause_summary(self, capability: str = 'text',
                              exclude_models=None, exclude_keys=None,
                              exclude_pairs=None) -> set:
        """Set of ``Slot.cooldown_reason`` values among capable slots that are
        currently IN cooldown (``cooldown_until > now``).

        Mirrors :meth:`has_capable_slots` filtering (capability, durable
        exclusions, chat-compatibility, provider pin) and answers the ONE
        question the dispatch wait-loop needs for an honest HUD label: is
        this wait rate-limit contention, or error/upstream backoff? The old
        wait loop hardcoded the "rate-limited" label for EVERY cooldown —
        so a hard-error 300s backoff masqueraded as 限流排队 (yuju opus-5
        vendor-4xx storm, 2026-07-26). Reading guide: empty set → nothing
        is cooling (caller falls back to the legacy rate-limit label, the
        common contention case); 'rate_limit' present → per-key contention;
        anything else → error/upstream backoff.
        """
        self.initialize()
        ex_models = exclude_models or set()
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        now = time.time()
        causes = set()
        with self._lock:
            for s in self._selection_scope(pinned_provider):
                if capability not in s.capabilities:
                    continue
                if s.model in ex_models or s.key_name in ex_keys:
                    continue
                if (s.key_name, s.model) in ex_pairs:
                    continue
                if not self._is_chat_compatible(s):
                    continue
                if s.cooldown_until > now:
                    # A cooldown stamped before cooldown_reason existed
                    # ('') is bucketed 'error' — it self-heals within one
                    # cooldown lifetime and never mislabels as rate-limit.
                    causes.add(s.cooldown_reason or 'error')
        return causes

    def dominant_cooldown_cause(self, capability: str = 'text',
                                exclude_models=None, exclude_keys=None,
                                exclude_pairs=None) -> str:
        """Single most user-actionable cooldown cause among cooling slots.

        Precedence mirrors ``cooldown_wait_label``: a subscription/billing
        'quota' hold outranks everything (it won't recover for hours/days),
        then external 'contention', then per-key 'rate_limit', then the
        generic error/upstream backoff. Returns '' when nothing is cooling.
        """
        causes = self.cooling_cause_summary(
            capability, exclude_models=exclude_models,
            exclude_keys=exclude_keys, exclude_pairs=exclude_pairs)
        for cause in ('quota', 'contention', 'rate_limit'):
            if cause in causes:
                return cause
        return next(iter(causes), '') if causes else ''

    def sticky_cooldown_remaining_s(self, conv_id: str, prefer_model=None,
                                    *, exclude_keys=None, exclude_pairs=None):
        """Seconds until ``conv_id``'s warm sticky key becomes pickable again.

        Returns ``(remaining_seconds, key_name)`` when the conversation has a
        recorded sticky key, that key has a slot serving ``prefer_model``'s
        logical id, the slot is NOT hard-excluded, and its ONLY disqualifier is a
        live cooldown (``now < cooldown_until``). Returns ``None`` when there is
        no warm key worth waiting for (no affinity, key excluded, or the slot is
        already eligible — in which case the normal picker will land on it).

        Used by the dispatch retry loop to decide whether to briefly HOLD for
        the conv's warm key (preserving its prompt-cache prefix) instead of
        rebinding to a cold key. The caller gates the returned ``remaining`` on
        a budget, which is what distinguishes a transient 0.5s rate-limit nudge
        (worth waiting) from a long consecutive-error / quota cooldown (not).
        """
        if not conv_id:
            return None
        sticky_affinity = get_preferred_key(
            conv_id, route_key=str(prefer_model or 'cap:text'))
        if not sticky_affinity:
            return None
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        matcher = self._prefer_matcher(prefer_model) if prefer_model else None
        # The recorded identity is stable (route credential+deployment), shared
        # across request-scoped groups; the pinned-provider scope confines the
        # hold to slots the current execution context is actually authorized
        # to pick — the same gate the picker applies.
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        now = time.time()
        best_remaining = None
        held_slot = None
        with self._lock:
            for s in self._selection_scope(pinned_provider):
                if s.affinity_key() != sticky_affinity:
                    continue
                # Exclusions name this dispatch attempt's failed key_names.
                if s.key_name in ex_keys:
                    continue
                if (s.key_name, s.model) in ex_pairs:
                    continue
                if matcher is not None and not matcher(s):
                    continue
                if not self._is_chat_compatible(s):
                    continue
                remaining = s.cooldown_until - now
                if remaining <= 0:
                    # The warm key is already eligible — no need to wait; the
                    # normal picker will choose it. Signal "nothing to hold for".
                    return None
                if best_remaining is None or remaining < best_remaining:
                    best_remaining = remaining
                    held_slot = s
        if best_remaining is None:
            return None
        s = held_slot
        # A recorded 402/quota stop is durable evidence, not a warm 429
        # nudge: a key the provider already refused must never become a
        # "warm" hold, or the strict loop parks on it (2026-11-09 R247).
        with _KeyAdmission(credentials=self._credential_index) as key_health:
            if not key_health.admits(s):
                return None
        return (best_remaining, sticky_affinity)

    def summarize_slots(self, capability: str = None) -> str:
        """Return a compact one-line summary of the selectable slots for logging.

        Format: ``key_0/model:rpm=45/60 inf=2 err=0 | key_1/model:rpm=...``
        Lists the current execution context's selection scope (its provider
        pin's group, or the configured pool), only slots matching
        *capability* if specified.
        """
        self.initialize()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned_provider = get_pinned_provider()
        parts = []
        with self._lock:
            scope = [s for s in self._selection_scope(pinned_provider)
                     if not capability or capability in s.capabilities]
            for s in sorted(scope, key=lambda s: s.score()):
                rpm = s.current_rpm_usage
                parts.append(
                    f'{s.key_name}/{s.model}:'
                    f'rpm={rpm:.0f}/{s.rpm_limit:.0f} '
                    f'inf={s.inflight} err={s.consecutive_errors}'
                )
        return ' | '.join(parts) if parts else '(no slots)'

    def get_slots_info(self) -> list[dict]:
        """Return current slot info for monitoring."""
        self.initialize()
        return [
            {
                'key': s.key_name,
                'model': s.model,
                'capabilities': sorted(s.capabilities),
                'rpm_limit': s.rpm_limit,
                'rpm_current': s.current_rpm_usage,
                'rpm_headroom_pct': round(s.rpm_headroom * 100, 1),
                'latency_ema_ms': round(s.latency_ema, 1),
                'ttft_ema_ms': round(s.ttft_ema, 1),
                'throughput_ema_tps': round(s.throughput_ema, 1),
                'inflight': s.inflight,
                'consecutive_errors': s.consecutive_errors,
                'success_rate': round(s.success_rate, 3),
                'total_requests': s.total_requests,
                'total_errors': s.total_errors,
                'contention_errors': s.contention_errors,
                'gateway_errors': s.gateway_errors,
                'requests_5h': s.requests_5h,
                'provider_id': s.provider_id,
                'base_url': s.base_url,
                'available': s.is_available,
                'cooldown_until': s.cooldown_until,
                'cooldown_reason': s.cooldown_reason,
                'last_success_time': s.last_success_time,
                'last_error_time': s.last_error_time,
                'last_error_msg': s.last_error_msg,
                'score': round(s.score(), 1),
            }
            for s in sorted(self.slots, key=lambda s: s.score())
        ]
