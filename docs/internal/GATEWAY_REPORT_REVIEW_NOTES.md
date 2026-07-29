# Internal review notes — gateway cache report

**Companion to** [`docs/GATEWAY_REPORT_CACHE_NOT_REUSED.md`](../GATEWAY_REPORT_CACHE_NOT_REUSED.md).
**INTERNAL ONLY — never forward this file.**

## Why this is a separate file

These notes used to live at the bottom of the report itself under a heading that
said "REMOVE THIS SECTION BEFORE SENDING". That made correctness depend on a
human remembering to delete a section from a document they are in the middle of
forwarding — and the content is precisely what we would least want to hand the
other side: our own list of the argument's weak points, the fact that our cost
figure is an upper bound, and the admission that we lack the decisive control.

Splitting the files makes the leak structurally impossible rather than
procedurally avoided. `tests/test_gateway_report_sendable.py` asserts the
sendable report contains no internal-only markers, so the sections cannot drift
back together.

---

## Review checklist — attack these first, not the prose

### 1. The CNY column is an upper bound. Checked; smaller than feared.

Per-round records carry no model id, so `--model` prices the whole table at one
rate. The fleet is genuinely mixed — by model mentions in the app log:

| model | share | cache-write rate (CNY/1k) |
|---|---|---|
| `aws.claude-opus-4.8` | 44.7 % | 0.04525 |
| `kimi-k3` | 11.4 % | 0.01998 |
| `claude-opus-5` | 5.9 % | 0.04525 |
| `yuju-claude-opus-5-evaDaily` | 5.8 % | 0.04525 |
| `gemini-3-flash-preview` | 4.5 % | 0.00109 |

Two facts contain the error:

* All four Claude Opus variants (`opus-5`, `opus-4.8`, `aws.opus-4.8`,
  `opus-4.7`) price **identically** at 0.04525 — so the dominant model costs
  exactly what the table assumes.
* `upstream_identical` requires a wire fingerprint, which only exists on the
  Anthropic-native path. A `kimi-k3` or `gemini` round **cannot enter that
  bucket**; it lands in the hedged "no wire fingerprint" classes.

**Recommendation:** lead with tokens and round counts; present CNY as an order
of magnitude, not a figure to the yuan. The argument does not depend on the
money, and an over-precise number invites the discussion to become about our
accounting instead of their cache.

**Permanent fix SHIPPED:** `pt_778c55d4` — the record now carries `model`, and
`scripts/cache_waste_report.py` prices each row at its OWN model's rate. So for
traffic logged from now on the CNY column is exact and this caveat disappears.

**But it does not apply retroactively.** Records are stamped at write time, so
every row in the window this report quotes predates the field and is still
priced at the `--model` fallback — verified: the pinned table is byte-identical
before and after the change, with `0 rows priced from their OWN recorded model,
2,113 from --model fallback`. The recommendation above therefore stands FOR
THIS REPORT. Re-running it in a few days will start showing exact per-model
costs, and the report also now prints a per-model split, which answers "which
model wastes the most cache" — previously unanswerable.

### 2. `ttl_expiry` reads empty, which looks like an omission.

It is a labelling artefact: buckets are stamped at write time, and rounds logged
before `b402b696` keep their old label (`body_change` / `other`). Disclosed in
the report's window note.

The honest answer to "how many TTL expiries did you see?" is **"we cannot
separate them from `body_change`/`other` for this window."** If that is too weak
a footing to send on, wait for new traffic to accumulate under the corrected
classifier — do **not** re-derive history in the report script, which would
create a second copy of the bucketing rule and reintroduce exactly the drift
this telemetry exists to prevent.

### 3. The `other` bucket (n=74, p50 314.6 s) is probably mostly TTL expiry.

Same artefact as (2). Does not touch the `upstream_identical` claim, but if a
reviewer asks "what is `other`?", that sentence is the answer — not a shrug.

### 4. The strongest single fact is R18/R19, not the fleet total.

A **14.8 s** gap that MISSED sitting next to a **100.7 s** gap that HIT — same
key, same conversation, strictly append-only prefix. Neither a TTL nor a
write-visibility window predicts that ordering. If the gateway team engages with
only one thing, it should be this. **Consider opening with it** rather than with
the fleet table.

### 5. The decisive control we lack — and it is now runnable.

We have not reproduced this against a second path, so we cannot yet say "the
same prefix hits on X and misses on Y". That comparison would move the report
from *"our telemetry says your cache is not reusing"* to *"the identical prefix
reuses on Anthropic direct and does not reuse through your gateway"* — a claim
that is very hard to argue with.

**A second path is configured and enabled:** `oauth_claude` →
`https://api.anthropic.com/v1` (`protocol: anthropic`, 1 key entry, subscription
-backed). So this is a scheduling decision, not a blocker.

`scripts/cache_ab_probe.py` implements the comparison and is **dry-run by
default**. Read its docstring before arming it — it costs real tokens and, on
the OAuth path, consumes the owner's subscription quota, so it needs an explicit
human go-ahead (charter #16). Design keeps it cheap: a ~4k synthetic prefix
(comfortably over the 1024-token cacheable minimum, ~0.18 CNY per cold write)
rather than reproducing the 100k–500k prefixes from production.

**Caveat to state if we publish its result:** a 4k prefix does not test the
capacity-eviction hypothesis, which is specifically about large prefixes. A
negative result (both paths hit) would therefore NOT clear the gateway — it
would only narrow the mechanism to something size-dependent. Say that
explicitly rather than letting a cheap null result read as exoneration.

### 6. Tone check.

The report claims an observation, not a diagnosis. Every section states what we
ruled out on our own side first, and the four closing questions are the ask —
none of them asserts their cache is broken. Keep it that way.

---

## Sending checklist

1. Reviewer reads the report and this file; raises corrections.
2. Apply corrections; re-run the pinned command and confirm the numbers still
   reproduce (`python3 scripts/cache_waste_report.py --until '2026-07-29 15:59:50'`).
3. Decide on notes (1) and (4): whether to drop the CNY column and whether to
   lead with R18/R19.
4. Optionally arm `scripts/cache_ab_probe.py` for the second-path control.
5. **A human** forwards `docs/GATEWAY_REPORT_CACHE_NOT_REUSED.md` — that file
   only, never this one — to the `sankuai_anthropic` gateway team.
6. Record their reply on the epic.
