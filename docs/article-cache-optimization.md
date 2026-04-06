# How We Cut Claude's API Cost by 27%: A 5-Strategy A/B Test on Prompt Caching

![Cover](../static/images/tofu-cache-article-cover.png)

> **TL;DR** — We ran a rigorous 5-arm A/B test comparing different prompt caching strategies for Claude on real multi-round, multi-tool-calling workloads. Our best strategy (4 breakpoints + mixed TTL) reduced API costs by 27.2% compared to a naive baseline, and outperformed Claude Code's own single-breakpoint approach in 3 out of 4 scenarios.

---

## The Problem: Prompt Caching Is Easy to Get Wrong

If you're building an AI coding assistant that uses Claude, you're probably familiar with Anthropic's [prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) feature. The idea is simple: mark parts of your prompt with `cache_control`, and Anthropic caches the KV attention state so subsequent requests don't recompute them. Cache reads cost only **10%** of normal input pricing.

Sounds great — until you realize:

1. **Breakpoint placement matters enormously.** A single misplaced breakpoint can invalidate the entire cache every round.
2. **Server-side cache has a 5-minute TTL.** Long-running tasks or user think-time causes silent evictions.
3. **The cost model is asymmetric.** Cache writes cost **1.25×** (5-min) or **2.0×** (1-hour) — so excessive re-writes can make caching *more expensive* than no caching at all.
4. **Different API conventions complicate monitoring.** Anthropic returns `prompt_tokens` as the uncached portion only, while OpenAI-compatible proxies may vary — if your dashboard assumes the wrong convention, your cost display is wrong and you can't even see the problem.

We discovered all of these the hard way while building [Tofu (豆腐)](https://github.com/anthropics/tofu), a self-hosted AI coding assistant. This article shares what we learned and how we tested it.

---

## The Discovery: A ¥198 Conversation

It started with a single conversation: 64 rounds of Claude Opus working on a coding task. The token tag showed:

```
Input:       66 tokens   → ¥0.00
Cache write: 1,200.3k    → ¥162.94
Cache read:  2,656.1k    → ¥28.85
Output:      12.3k       → ¥6.66
─────────────────────────────────
Total:                      ¥198.44
```

Two things jumped out:

**"Input: 66 tokens"** — After 64 rounds of back-and-forth with tool calls, how could total uncached input be only 66 tokens (~1 per round)? Either our caching was miraculously perfect, or we had a display bug.

**¥162.94 in cache writes** — That's 82% of the total cost. Cache writes should be a one-time cost, not the dominant expense. Something was forcing repeated full-prefix rewrites.

### What We Found

**Bug #1: Display convention mismatch.** Anthropic's API returns `prompt_tokens` as the **uncached portion only** (additive: `total = prompt_tokens + cache_write + cache_read`). Our frontend assumed OpenAI convention where `prompt_tokens` = total input. Result: the token tag showed "66 → 12.3k" instead of the true "3.9M → 12.3k", and the "cache savings" indicator always showed ¥0.

**Bug #2: Server-side TTL evictions.** Our cache tracking system detected **14 out of 64 rounds** (22%) where cache reads suddenly dropped from 50-80K tokens to exactly **13,988 tokens** — the size of the system prompt + tool definitions. The conversation tail's cache was being evicted by Anthropic's 5-minute TTL when individual rounds took too long (tool execution + streaming).

**Bug #3: Suboptimal breakpoint placement (already fixed).** An earlier version placed the 4th breakpoint on `msg[-2]` instead of `msg[-1]`, causing the most recent tool result to always be sent uncached. After fixing to `msg[-1]`, uncached tokens dropped from ~250/round to ~1/round.

---

## The Hypothesis: Can We Do Better?

With the bugs fixed, we had a working 4-breakpoint strategy:

- **BP1–BP2**: System message blocks (stable, rarely change)
- **BP3**: Last tool definition (stable within a session)
- **BP4**: Conversation tail (changes every round — the most recent message)

But we noticed Claude Code (Anthropic's own CLI tool) uses a **completely different strategy**: just **one breakpoint** on `msg[-1]`. Their reasoning (from their source code comments):

> *Mycro's eviction mechanism retains local-attention KV pages at each cache_control marker location. With 2 markers, the second position's KV pages are retained but never used, reducing cache efficiency.*

We also discovered Anthropic's **extended 1-hour TTL** option (beta), which costs 2.0× for writes but eliminates the 5-minute eviction problem. This led to a natural question:

**Which combination of breakpoint count (1 vs 4) × TTL duration (5m vs 1h) actually wins?**

---

## The Test: 5 Arms × 4 Scenarios × 12 Rounds

We built a test harness that runs controlled multi-round conversations against the real Claude API, with precise per-round token tracking. Each conversation simulates an AI coding assistant session with real tool calls.

### The 5 Strategies

| Arm | Breakpoints | TTL | Description |
|-----|-------------|-----|-------------|
| **OLD** | 4 BPs, BP4=msg[-2] | 5 min | The buggy version (baseline) |
| **NEW** | 4 BPs, BP4=msg[-1] | 5 min | Fixed placement, standard TTL |
| **NEW_1h** | 4 BPs, BP4=msg[-1] | Mixed (1h + 5m) | 1h for stable prefix, 5m for tail |
| **SINGLE** | 1 BP on msg[-1] | 5 min | Claude Code's approach |
| **SINGLE_1h** | 1 BP on msg[-1] | 1 hour | Claude Code + extended TTL |

### The 4 Scenarios

Each scenario tests a different real-world usage pattern:

#### Scenario A: Single Query → Multi-Tool Execution
```
User: "Analyze the error handling in this project"
→ 12 rounds: read_files, grep_search, list_dir, web_search, fetch_url, run_command
```
The most common pattern — one user request triggers a long chain of tool calls. Tests how well the cache handles a steadily growing conversation with diverse tool results.

#### Scenario B: Multi-Turn with Interleaved User Messages
```
User: "Set up a new API endpoint"
→ 3 rounds of tool calls
User: "Also add input validation"    ← injected at round 4
→ 4 more rounds of tool calls
User: "Now write the tests"          ← injected at round 8
→ 4 more rounds of tool calls
```
Tests cache stability when user messages are inserted mid-conversation, potentially shifting all subsequent message positions.

#### Scenario C: Parallel Tool Calls (Batched)
```
User: "Compare the config, routes, tests, and docs directories"
→ Model issues 4 tool_calls in a single assistant message
→ 4 tool_results returned together
→ Repeat for 12 rounds
```
Tests how the cache handles multiple tool call/result pairs per round — a pattern that creates larger per-round message increments.

#### Scenario D: Mixed Content Assistants (The Edge Case)
```
User: "Refactor this module step by step"
→ Assistant: "I'll start by reading the file." + tool_call   (text + tool)
→ tool_result
→ Assistant: "" + tool_call                                   (empty content + tool)
→ tool_result
→ Assistant: "Here's what I found..." + tool_call             (text + tool)
```
The trickiest pattern. Some assistants have `content=""` (empty text before tool calls), others have substantive text. This was the specific bug trigger for the old BP4 placement — the empty-content assistants caused the breakpoint scan to skip too far.

### Execution Protocol

For each arm × scenario combination:
1. Start fresh — no prior cache state
2. Run 12 API rounds against `aws.claude-opus-4.6`
3. Record per-round: `prompt_tokens`, `cache_write_tokens`, `cache_read_tokens`, `completion_tokens`
4. All arms share the same system prompt and tool definitions (deterministic)
5. Tool results are simulated (no actual file I/O) to ensure cross-arm reproducibility

---

## The Results

### Overall Ranking

| Rank | Strategy | Total Cost (4 scenarios) | Savings vs OLD |
|------|----------|--------------------------|----------------|
| 🥇 | **NEW_1h** | **$1.5242** | **-27.2%** |
| 🥈 | NEW | $1.5746 | -24.8% |
| 🥉 | SINGLE_1h | $1.7552 | -16.2% |
| 4th | SINGLE | $1.9431 | -7.2% |
| 5th | OLD | $2.0938 | — |

### Per-Scenario Winners

| Scenario | 🏆 Winner | Cost | Runner-up | Gap |
|----------|-----------|------|-----------|-----|
| A (multi-tool) | **NEW_1h** | $0.40 | NEW | -6.1% |
| B (multi-turn) | **NEW** | $0.29 | NEW_1h | -5.9% |
| C (parallel) | **NEW_1h** | $0.39 | OLD | -1.4% |
| D (mixed) | **SINGLE** | $0.40 | NEW_1h | -4.1% |

### The Uncached Token Metric

This is the clearest indicator of cache effectiveness — how many tokens per round are sent *without* cache coverage (after cache is established):

| Strategy | Scenario A | B | C | D | Average |
|----------|-----------|---|---|---|---------|
| OLD | 251.9 | 189.4 | 161.3 | 369.0 | **242.9** |
| NEW | 1.0 | 1.4 | 1.0 | 1.0 | **1.1** |
| NEW_1h | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |
| SINGLE | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |
| SINGLE_1h | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |

The OLD strategy leaked **240×** more uncached tokens than any of the fixed versions. After the BP4 fix, all strategies achieve near-perfect caching (~1 uncached token/round).

---

## Analysis: Why 4 Breakpoints Beat 1

Claude Code's single-breakpoint rationale is about KV page efficiency inside Anthropic's cache engine. But our data shows a different picture at the **cost** level:

**4 breakpoints create independent cache segments.** When the conversation tail changes (every round), only BP4's segment is invalidated. BP1–BP3 (system prompt ~14K tokens, tool definitions) remain cached. With a single breakpoint, any change to the last message invalidates everything before it.

**The mixed TTL amplifies this advantage.** With `NEW_1h`, the system prompt and tools (BP1–BP3) get a 1-hour TTL — they're written once at 2.0× and then read for free for up to an hour. The tail (BP4) uses 5-minute TTL at the cheaper 1.25× write rate. This hybrid approach gives you the best of both worlds.

**SINGLE only wins Scenario D** because the empty-content assistant messages create a degenerate case where the 4-BP scan sometimes picks a suboptimal tail position. The single breakpoint on `msg[-1]` sidesteps this entirely.

### The Cache Activation Threshold

One important detail: Claude's cache has a **minimum block size** — 4,096 tokens for Opus, 1,024 for Sonnet. In our tests with a ~2,500 token system prompt, cache only activated at Round 3 (when total input exceeded 4,096). In production with a ~14K system prompt, cache hits from Round 1.

---

## The Mixed TTL Innovation

Anthropic's documentation says you can mix TTL durations in a single request, with one constraint: **longer TTL breakpoints must appear before shorter ones.** This is exactly what our 4-breakpoint layout naturally provides:

```
[System prompt ──── BP1:1h ── BP2:1h]  [Tools ── BP3:1h]  [Conversation ── BP4:5m]
         stable content (1h TTL)                              high-churn (5m TTL)
```

The cost trade-off:

| Component | Tokens | Write Rate | Frequency | Annual Amortized |
|-----------|--------|------------|-----------|-----------------|
| System prompt + tools | ~14K | 2.0× ($30/M) | Once per hour | Negligible |
| Conversation tail | ~500-5K/round | 1.25× ($18.75/M) | Every round | Dominant cost |

The 1-hour writes for the stable prefix cost $0.42 once, then save $0.21 per avoided eviction re-write. After just 2 evictions, the strategy pays for itself.

---

## Implementation Notes

### Cache Breakpoint Placement

```python
def add_cache_breakpoints(messages, model, tools=None):
    """Place up to 4 cache breakpoints with mixed TTL."""
    _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}  # BP1-3
    _cc_tail   = {'type': 'ephemeral'}                 # BP4 (default 5m)

    # BP1-BP2: system message blocks (scan from start)
    # BP3: last tool definition
    # BP4: last message in conversation (scan from end, skip empty-content assistants)
```

### Token Convention Detection

```javascript
// Anthropic: prompt_tokens = uncached only (additive)
// OpenAI:    prompt_tokens = total input (inclusive)
const isAnthropicConvention = (inp <= cacheWrite + cacheRead) && (cacheWrite + cacheRead > 0);
const totalInput = isAnthropicConvention ? inp + cacheWrite + cacheRead : inp;
const uncachedInput = isAnthropicConvention ? inp : Math.max(0, inp - cacheWrite - cacheRead);
```

### Beta Header for Extended TTL

```python
if any_breakpoint_has_1h_ttl:
    headers['anthropic-beta'] = 'extended-cache-ttl-2025-04-11'
```

---

## What About Production?

Our controlled 12-round tests complete in ~60 seconds — well within the 5-minute TTL, so server-side evictions don't occur. But production is different.

In a real 64-round Opus conversation (11 minutes):
- **22% of rounds** experienced server-side cache eviction (cache reads dropped to 13,988 = system prompt only)
- **Cost without caching**: ¥425
- **Cost with NEW_1h**: ¥198 (**53% savings**)
- **Estimated cost with OLD strategy**: ¥340+ (based on the 240× uncached token leak)

The mixed TTL strategy's value increases with conversation length. For conversations under 5 minutes, it's approximately break-even. For conversations over 10 minutes, the 1-hour TTL on the stable prefix prevents the most expensive kind of cache miss — full system prompt re-writes.

---

## Takeaways

1. **Test your caching empirically.** Theory (KV page efficiency, single vs multiple breakpoints) didn't match our real-world cost data. The only way to know is to measure.

2. **Breakpoint placement is the #1 lever.** The fix from `msg[-2]` → `msg[-1]` saved 24.8% — more than any TTL optimization.

3. **Mixed TTL is a free lunch for multi-breakpoint strategies.** 1h for stable content + 5m for dynamic content costs almost nothing extra and prevents the most expensive cache misses.

4. **Monitor your actual cache hit rate.** We built a `detect_cache_break()` system that tracks per-round cache reads and flags anomalies. Without it, we would never have noticed the 22% eviction rate.

5. **Watch out for API convention differences.** If your proxy translates between Anthropic and OpenAI formats, verify whether `prompt_tokens` means "total" or "uncached only" — getting this wrong makes your cost dashboard useless.

---

## Try It Yourself

Tofu is open-source. The complete test harness is at `debug/test_cache_validation.py`:

```bash
# Quick validation (no API calls)
python debug/test_cache_validation.py --dry-run --arms all

# Full 5-arm A/B test
python debug/test_cache_validation.py \
  --model claude-opus-4-20250514 \
  --arms OLD,NEW,NEW_1h,SINGLE,SINGLE_1h \
  --scenario all \
  --rounds 12
```

The cache breakpoint implementation is in `lib/llm_client.py:add_cache_breakpoints()`, and the cache tracking system is in `lib/tasks_pkg/cache_tracking.py`.

---

*Built with Tofu (豆腐) — a self-hosted AI assistant that takes API costs seriously.*

*2026-04-05*
