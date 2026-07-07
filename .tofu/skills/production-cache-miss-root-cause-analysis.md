---
name: production-cache-miss-root-cause-analysis
description: Sub-5min cache misses are a STOCHASTIC ~6-8% gap-independent server-side per-request failure; load-balancer routing UNPROVEN; single-baseline claim disproven
enabled: true
tags: [caching, anthropic, production, debugging]
created: 2026-04-10T09:32:27Z
updated: 2026-06-20T15:24:17Z
---

# Production Cache Miss Root Cause Analysis

## ⚠️ 2026-06-20 RE-INVESTIGATION (corrects the 2026-04-10 conclusions below)

Re-ran the analysis against production `logs/app.log*` (590 drop events, 8908
CacheStats rounds). Two of the 2026-04-10 claims did NOT hold up; treat the
mechanism as **unconfirmed server-side stochastic failure**, not proven routing.

### What the production logs DO show (high confidence)
- **Drop rate is FLAT vs inter-round gap under 5 min** — the dispositive result:
  - gap 0-10s → 6.6% | 10-30s → 5.8% | 30-60s → 7.8% | 60-300s → 7.7%
  - gap >300s → **33.7%** (this is the real 5m-tail TTL cliff)
  - A gap-independent constant ~6-8% miss rate = a *stochastic per-request*
    server failure. Rules OUT TTL for the sub-5min band; rules OUT
    "recent call = safer."
- **Not contention**: 335/534 server-side drops happened with only ONE
  conversation active within ±6s. Confirms the original no-contention finding.
- **Breakpoint advancement is NOT a cause** — Anthropic reads are automatic up
  to the longest matching cached prefix; advancing BP4 never loses an earlier
  write's read. The reason string conflating the two is misleading.

### What was DISPROVEN / overstated in the 2026-04-10 memory
- ❌ **"Drops to a single fixed baseline per conversation"** — FALSE in prod.
  conv=mqjkmn49 dropped to {0, 54066, 57801, 58225, 58973, 59397} — six values.
  There IS a recurring band 50-62k (54274×56, 54066×47) but it's the
  system+tools size shared across convs, NOT a per-conv routing fingerprint.
- ❌ **"Load-balancer routing across instances"** — PLAUSIBLE but UNPROVEN.
  The clean proof would be a REBOUND event (same conv: hit→~0→hit within 5min,
  same bytes). **Zero such events exist in the logs** — every drop-to-0 is
  followed by a full cache REWRITE, never instant recovery of the old prefix.
  So the production logs cannot demonstrate "identical bytes missed then
  recovered." The routing mechanism is a hypothesis, not a measured fact.

### Honest current conclusion
Sub-5min misses = **stochastic ~6-8% per-request server-side failure** in the
gateway/Bedrock layer: gap-independent, not contention, not TTL, mechanism
(eviction vs routing) NOT pinned down from logs alone. To actually pin it we'd
need data logs can't give: backend-instance response headers, or a controlled
identical-prompt replay against the live gateway.

### Client-side causes we CAN fix (proven from same logs, mechanism-independent)
- **140 PREFIX MUTATION warnings** — client mutating the cached prefix.
- **86 CONFIRMED client-caused breaks**: 38 system+tools changed, 34 system
  changed, 10 tools changed, 4 compaction. **48 involve a tools-array change
  across 36 distinct conversations** = mid-conversation feature toggles (Swarm/
  Scheduler/Browser/etc.) rebuilding the BP1-3 prefix. See the tools-array
  latch idea (analogous to existing `latch_extended_ttl`).

## Original 2026-04-10 analysis (A/B harness — kept for context, see caveats above)
- Identical-prompt test: same prompt 10× (2s gaps), calls 2 & 4 missed despite
  identical bytes, then 5-10 all hit. (Harness result — NOT reproduced as
  rebound events in production logs; see above.)
- A/B test: different conversations don't evict each other (±0.1%).
- 57% of drops within 10s → not TTL (consistent with 2026-06 flat-rate finding).
- Overall hit rate ~84%; per-conv miss 10-17%.

## What we CANNOT fix
- The stochastic server-side miss itself (gateway/Bedrock internal behavior).

## What we CAN do
- Accept ~6-8% sub-5min base miss as infra baseline; 1h TTL on BP1-3 works
  (system+tools survives multi-hour gaps).
- Eliminate the CLIENT-side breaks: prefix mutations + mid-session tools-array
  toggles (the latter is user-driven from the frontend tool switches).

