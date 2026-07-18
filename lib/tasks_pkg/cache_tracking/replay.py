"""Offline cache-verdict REPLAY harness.

Verifies the always-on cache-miss instrument (the namespace + marker-ttl
fingerprints in ``wire_fingerprint`` + the 3-state verdict in
``detect_cache_break``) WITHOUT restarting the live server.

The restart deadlock this exists to break
=========================================
The namespace/ttl fingerprints live in the RUNNING process's imported bytecode,
so verifying them against live traffic would need an ``os.execv`` restart. But
in a multi-sibling concurrent deployment a restart is refused (HTTP 409) unless
``force=true``, and a forced restart ``os.execv`` kills EVERY in-flight task —
INCLUDING the verification conversation itself. The verifier would commit
suicide. That is structural, not flaky.

So instead of restarting, we REPLAY: reconstruct the exact per-round ``usage``
the live send path assembles (``lib/llm/_sse_core.py``) from a sequence of
post-translation bodies (captured ``.tofu_cache_probe`` dumps, or synthetic
sequences), feed each round through the SAME ``detect_cache_break``, and
classify the verdicts into hard-count buckets. Zero restart, zero task-kill.

Public API
==========
  - ``build_round_usage(body, cache_read, cache_write, routing, capture_routing=True)``
    → the ``usage`` dict, byte-for-byte the keys ``_sse_core`` relays.
  - ``replay_rounds(rounds, conv_id=..., capture_routing=True)``
    → ``{'rounds': [{round, verdict, bucket}], 'buckets': {bucket: n}}``.
  - ``load_probe_dump_rounds(dump_dir)`` → rounds list from a probe-dump dir
    (best-effort; cache token counts are unknown in a body-only dump so the
    caller supplies them or a monotonic-drop default is used).
  - ``classify_verdict(verdict)`` → the bucket name for one detector result.
"""

from __future__ import annotations

import json
import os
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ── Bucket names (the objective's classification) ──
BUCKET_NAMESPACE = 'cache_namespace_switch'
BUCKET_TTL_FLIP = 'ttl_flip'
BUCKET_BREAKPOINT_LOST = 'breakpoint_lost'
BUCKET_UPSTREAM = 'upstream_identical'
BUCKET_BODY_CHANGE = 'body_change'
BUCKET_NO_BREAK = 'no_break'
BUCKET_OTHER = 'other'


def build_round_usage(body: dict, *, cache_read: int, cache_write: int,
                      routing: dict | None = None,
                      capture_routing: bool = True) -> dict:
    """Reconstruct the per-round ``usage`` the live send path assembles.

    Mirrors EXACTLY the wire-fingerprint relay in ``lib/llm/_sse_core.py``
    (prepare_request + SSEAccumulator.finalize): every ``_wire_*`` key the
    detector reads is computed here from ``body`` via the SAME production
    functions, so a replayed round is byte-for-byte the input the live
    ``detect_cache_break`` would have seen. ``routing`` is a dict with
    ``key_hash`` / ``anthropic_beta`` / ``endpoint`` (as ``_sse_core`` builds
    into ``_routing``); when ``capture_routing`` is False the ``_wire_routing``
    key is omitted, reproducing a process that never captured it (the NEUTER /
    pre-fix state).
    """
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, marker_signature, routing_fingerprint,
        static_prefix_hash, system_fingerprint, wire_byte_field_prefix,
        wire_byte_prefix, wire_byte_region,
    )
    msgs = body.get('messages') or []
    usage: dict[str, Any] = {
        'cache_read_tokens': int(cache_read),
        'cache_creation_input_tokens': int(cache_write),
        '_wire_fp': canonical_messages(msgs),
        '_wire_static': static_prefix_hash(msgs),
        '_wire_bytes': wire_byte_prefix(msgs),
        '_wire_field_bytes': wire_byte_field_prefix(msgs),
        '_wire_region': wire_byte_region(body.get('system'), body.get('tools')),
        '_wire_markers': marker_signature(body),
        '_wire_system': system_fingerprint(body.get('system'), body.get('tools')),
    }
    if capture_routing and routing is not None:
        usage['_wire_routing'] = routing_fingerprint(
            key_hash=routing.get('key_hash', ''),
            anthropic_beta=routing.get('anthropic_beta', ''),
            endpoint=routing.get('endpoint', routing.get('url', '')))
    return usage


def classify_verdict(verdict: dict | None) -> str:
    """Map ONE ``detect_cache_break`` result to a bucket name.

    The detector returns ``None`` (no break) or a single-key dict whose key +
    cause string name the cause. We key off the RETURN KEY first (the most
    authoritative signal) then the cause wording for the byte-identical
    sub-classes that all share the ``server_side`` key.
    """
    if not verdict:
        return BUCKET_NO_BREAK
    if BUCKET_NAMESPACE in verdict:
        return BUCKET_NAMESPACE
    # Named client body/prefix causes (system/tools/model change, prefix
    # mutation, hoisted-region byte flip, reasoning_details rebuild, …).
    if any(k in verdict for k in ('prefix_mutation', 'system_prompt', 'tools',
                                  'model', 'no_cache_reuse')):
        # These can also carry a ttl-flip / breakpoint-lost culprit in the
        # cause string — surface those first (they are the marker sub-causes).
        _cause = ' '.join(str(v) for v in verdict.values()).lower()
        if 'ttl marker flipped' in _cause or 'new cache key' in _cause:
            return BUCKET_TTL_FLIP
        if 'breakpoint lost' in _cause:
            return BUCKET_BREAKPOINT_LOST
        return BUCKET_BODY_CHANGE
    # server_side key — the byte-identical family. Disambiguate by cause string.
    _cause = ' '.join(str(v) for v in verdict.values()).lower()
    if 'ttl marker flipped' in _cause or 'new cache key' in _cause:
        return BUCKET_TTL_FLIP
    if 'breakpoint lost' in _cause:
        return BUCKET_BREAKPOINT_LOST
    if 'upstream cache miss' in _cause:
        return BUCKET_UPSTREAM
    return BUCKET_OTHER


def replay_rounds(rounds: list[dict], *, conv_id: str = 'replay',
                  capture_routing: bool = True) -> dict:
    """Drive ``detect_cache_break`` across a sequence of rounds; bucket the
    verdicts.

    Each round dict: ``{'body': <post-translation body>, 'cache_read': int,
    'cache_write': int, 'routing': {'key_hash','anthropic_beta','endpoint'}}``.
    Uses a FRESH isolated cache-state key (per ``conv_id``) and clears any prior
    state for it first, so replays are deterministic and independent.

    Returns ``{'rounds': [{'round', 'verdict', 'bucket'}], 'buckets': {name:n}}``.
    """
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import _state_key

    # Isolate this replay: drop any residual state for the conv key.
    _k = _state_key(conv_id)
    _cache_states.pop(_k, None)

    out_rounds: list[dict] = []
    buckets: dict[str, int] = {}
    for i, rd in enumerate(rounds):
        body = rd['body']
        usage = build_round_usage(
            body, cache_read=int(rd.get('cache_read', 0)),
            cache_write=int(rd.get('cache_write', 0)),
            routing=rd.get('routing'), capture_routing=capture_routing)
        verdict = detect_cache_break(
            conv_id, body.get('messages') or [], body.get('tools'),
            body.get('model', 'claude-opus-4'), usage=usage)
        bucket = classify_verdict(verdict)
        # Round 0 establishes the baseline (detector returns None on call 0);
        # only tally rounds that actually produced a classification signal.
        if i > 0:
            buckets[bucket] = buckets.get(bucket, 0) + 1
        out_rounds.append({'round': i, 'verdict': verdict, 'bucket': bucket})
    # Clean up so the replay leaves no residual live state.
    _cache_states.pop(_k, None)
    return {'rounds': out_rounds, 'buckets': buckets}


def load_probe_dump_rounds(dump_dir: str,
                           cache_reads: list[int] | None = None,
                           cache_writes: list[int] | None = None) -> list[dict]:
    """Load ordered rounds from a ``.tofu_cache_probe`` dump directory.

    Each ``round_NNNN.json`` is a captured post-translation body
    (``{round, model, system, tools, messages, [routing]}``). Cache token
    counts are NOT in a body-only dump, so the caller may pass parallel
    ``cache_reads`` / ``cache_writes`` lists (from the matching ``[CacheStats]``
    log lines); absent that, a benign default (read=write=0) is used so the
    replay still exercises the fingerprint/verdict path structurally.

    Best-effort: unreadable / malformed files are skipped with a warning.
    """
    rounds: list[dict] = []
    try:
        names = sorted(n for n in os.listdir(dump_dir)
                       if n.startswith('round_') and n.endswith('.json'))
    except OSError as e:
        logger.warning('[CacheReplay] cannot list dump dir %s: %s', dump_dir, e)
        return rounds
    for idx, name in enumerate(names):
        path = os.path.join(dump_dir, name)
        try:
            with open(path, encoding='utf-8') as f:
                body = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning('[CacheReplay] skip %s: %s', name, e)
            continue
        cr = cache_reads[idx] if cache_reads and idx < len(cache_reads) else 0
        cw = cache_writes[idx] if cache_writes and idx < len(cache_writes) else 0
        rounds.append({
            'body': body,
            'cache_read': cr,
            'cache_write': cw,
            'routing': body.get('routing'),
        })
    return rounds


def format_report(result: dict) -> str:
    """Render a compact human report of a ``replay_rounds`` result."""
    b = result.get('buckets', {})
    total = sum(b.values())
    lines = ['Cache-verdict replay — %d classified round(s)' % total]
    for name in (BUCKET_NAMESPACE, BUCKET_TTL_FLIP, BUCKET_BREAKPOINT_LOST,
                 BUCKET_BODY_CHANGE, BUCKET_UPSTREAM, BUCKET_NO_BREAK,
                 BUCKET_OTHER):
        if b.get(name):
            lines.append('  %-24s %d' % (name, b[name]))
    return '\n'.join(lines)
