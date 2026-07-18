# Prompt-Cache: Adding a Second Claude Key (the single-key warm-miss fix)

> **TL;DR** — On a single upstream key, the big-prefix admission gate is a
> deliberate no-op (`big_prefix_slot`, `lib/llm_dispatch/api.py`). Adding a
> **second Claude upstream key** gives Claude a second server-side cache
> namespace, auto-activates the residency admission machinery (no code change),
> and lets concurrent big conversations spread across two pools instead of
> LRU-evicting each other on one. This is the measured true fix for the
> residual "warm prefix, then evicted within ~23s" cache misses.

---

## Why this is needed (measured, not assumed)

Anthropic's prompt cache is **keyed per API key**: each upstream key has ONE
server-side cache namespace with a finite working set. From production
`logs/app.log` (2026-07-18, single key `sankuai_key_0`, 1581 cache-stat rounds):

| Signal | Value | Meaning |
|---|---|---|
| Rounds with `cache_r>0` | 1542 / 1581 (97.5%) | binary hit/miss is NOT the problem |
| Rounds `hit ≥95%` | 1097 | healthy majority |
| Big-write low-hit rounds (`cache_w>100k`, `hit<50%`) | 215 | the cost leak |
| …of those, prior same-conv round was ≥90% hit, ≤300s earlier | **151 (70%)** | **prefix was warm, then evicted** |
| Median gap to that prior round | **23s** | not TTL expiry (TTL is 5min) |
| Peak distinct big convs in a 5-min window (one key) | **8** | pool can't hold 8× ~250k prefixes |

The dominant remaining leak is **cross-conversation LRU eviction on the single
shared key pool** — many concurrent large (~250k-token) conversations evict
each other's warm prefixes.

### What does NOT fix it (ruled out by measurement)

- **Client-side byte drift** — already fixed. In-prefix `assistant/tool_call`
  re-serialization dropped from 342 rounds (07-17) to ~0 (07-18) after the
  `build_assistant_tool_call_message()` single-source extraction. Not the
  residual.
- **Compaction** — L2 force-compact fired only 1–2× all day;
  `notify_compaction` / "Expected cache drop" = 0. The 172 R3+ low-hit rounds
  are NOT compaction-driven.
- **Single-key serialization** — refuted by the flat miss-rate curve: miss%
  is `{conc1:23%, conc2:13%, conc4:9%, conc5:7%, conc6:14%, conc8:11%}` —
  non-monotonic, and a **lone** big conv (concurrency=1, nobody to evict it)
  still misses 23%. Bounding concurrent residents (what serialization controls)
  does not track the miss rate, so serializing one pool cannot reliably remove
  these misses. See the `big_prefix_gate.py` module docstring's HONEST LIMIT.
- **The concurrency=1 residual (13 rounds)** breaks down as: (a) 2 = TTL >5min
  expiry (normal), (b) 1 = retry/settle race (the `cache_settle` gate already
  handles this — it fired 1166× today with 1 residual), (c) **9 = gateway
  nondeterministic eviction of a lone warm prefix**. Bucket (c) is a gateway
  pool-policy artifact that **adding a key does not fix either** — it is a small
  (~9-round) irreducible floor, called out here for honesty, not a lever.

**Net:** there is no measured in-process lever left on a single key. The true
capacity fix is a second key.

---

## How to add the second key

The mechanism is already in the code; you only add configuration.

1. **Add a second Claude upstream key** in the provider/server config for the
   Claude model(s) — a second slot with a DISTINCT `key_name` (and its own
   `api_key`) serving the same `model`. Both slots keep `protocol` as-is
   (OpenAI-compat gateway = `''`/`openai`, or `anthropic` for the native path).

2. That's it. No code change. The dispatcher already computes:
   ```python
   # lib/llm_dispatch/api.py — _model_key_count
   _model_key_count = len({s.key_name for s in dispatcher.slots
                           if s.model == slot.model})
   ```
   With two distinct `key_name`s serving the model, `_model_key_count` becomes
   `2`.

3. `big_prefix_slot(..., key_count=_model_key_count)` then **stops no-op'ing**
   (the `key_count <= 1` early-return in `lib/llm_dispatch/big_prefix_gate.py`
   no longer triggers) and the **residency-aware admission** path activates:
   distinct big conversations are bounded to `TOFU_BIG_PREFIX_RESIDENCY_MAX`
   resident prefixes per key and steered so a held-back big prefix routes to the
   OTHER key's namespace.

### Expected effect

- 8 concurrent big convs across **2** keys ≈ 4 per pool → roughly halves the
  eviction pressure that produced the 151 warm-then-evicted rounds.
- The `cache_r` floor-pinning (recurring `~79393`) on contended windows should
  lift as prefixes stay resident in their own key's pool.
- Conversation-sticky routing (`lib/llm_dispatch/conv_affinity.py`) keeps a
  conversation on the same key round-to-round, so a conv's own prefix stays
  warm in one namespace rather than bouncing.

### Tuning knobs (only meaningful once `key_count >= 2`)

| Env var | Default | Effect |
|---|---|---|
| `TOFU_BIG_PREFIX_RESIDENCY_MAX` | = `max_per_key` (2) | max distinct big prefixes counted resident per key |
| `TOFU_BIG_PREFIX_RESIDENCY_TTL_MS` | 300000 (5min) | how long a prefix counts as resident after last use |
| `TOFU_BIG_PREFIX_THRESHOLD_TOKENS` | 150000 | prefix size above which a request is "big" and gated |

On a single key these are inert (the gate no-ops before reading them) — do not
expect tuning them to change anything until a second key exists.

---

## Verifying it worked (post-deploy)

Re-run the same log analysis after the second key is live:

```bash
# warm-then-evicted big-write low-hit rounds should drop sharply
grep '\[CacheStats\]' logs/app.log | grep -oE 'cache_w=[0-9]+ cache_r=[0-9]+ hit=[0-9]+%'
# key distribution should now show BOTH keys carrying big convs
grep '\[CacheStats\]' logs/app.log | grep -oE 'key=[a-z0-9_]+' | sort | uniq -c
```

Acceptance: the `hit<50% & cache_w>100k` round count falls materially and the
`key=` histogram shows both keys sharing the big-conv load. The residual
concurrency=1 gateway-eviction floor (~9 rounds/day class) is expected to
remain — it is not key-count-fixable.
