# Retained prompt-cache usage, 2026-11-19 morning

Scope: application log clock (CST), from `2026-11-19 00:00:00` inclusive to
`07:00:00` exclusive. Read-only recomputation used the active application log
and seven dated rotations; the matched observation range was 00:00:00–06:59:58.
This is evidence about retained observations, not a complete dispatch ledger,
pricing reconciliation, or post-fix measurement. No provider calls were made.

Recompute using the retained files, if still available locally:

```sh
python3 scripts/cache_usage_window.py --start '2026-11-19 00:00:00' \
  --end '2026-11-19 07:00:00' "$LOG_DIR"/app.log "$LOG_DIR"/app.log.2026-11-19*
```

The checksum below is for the historical JSON schema from `2433dee388`.
The current tool also emits `detector_read_coverage`: one count per distinct,
nonconflicting detector trace, including traces without a stats pair. Positive,
explicit zero, legacy zero without provenance, and unknown reads are separate;
unknown reads cannot join the numeric totals. The `zero_read_rows` count in
those totals still describes numeric marker pairs, not cache misses. Neither
emitter supplies an authoritative provider grouping, configured estimate, or
upstream charge. The physical-attempt denominator outside the markers is unknown.

The historical parser accepted only lines from the two cache-diagnostic
emitters in the local log window, parsed `[CacheStats]` usage and
`[CacheRoundRecord]` comparison metadata, deduplicated identical projected
rows per nonempty trace, and excluded conflicting or unmatched traces. A join
must agree on model, conversation and read count and have a read count no
larger than reported prompt input. Timestamp, rotation file, task
round and cache-lineage ordinal are not join keys. Only aggregate categories
and counts are emitted; no row identities, request material or credentials are
retained in this report.

## Bounded recomputation output

SHA-256 of the sanitized JSON stdout (not of any raw log):
`1e9e2ccf0f03ab31f56a3584a96edce54511a7fe51f5b5307f22d39c6a521308`.

- **Ingest:** 32,638 marker rows and 32,638 distinct trace IDs on each side.
  Both sides: 0 malformed, 0 missing trace, 0 projected duplicates and 0
  conflicting traces. Join: 0 unmatched on either side, 0 ambiguous and 0
  identity/usage mismatches. In 1,798 matched pairs the ordinals differed.
- **Tokens:** input **3,031,090,988** minus read **2,856,964,992** equals
  **174,125,996** residual input. Reported cache writes were zero throughout
  (0 unknown write rows); no write convention assumption is needed for this
  cohort. There were 1,184 zero-read rows. The formula is `sum(input - cache_r)`
  over matched rows, not a provider bill, recoverable-waste estimate or a cache
  hit counter.

| Model | Matched rows | Input | Cache read | Input minus read |
| --- | ---: | ---: | ---: | ---: |
| `gpt-6.1-sol` | 5,036 | 868,520,180 | 791,632,128 | 76,888,052 |
| `gpt-6.2-sol` | 27,602 | 2,162,570,808 | 2,065,332,864 | 97,237,944 |

| Group (within each dimension, mutually exclusive) | Rows | Input minus read |
| --- | ---: | ---: |
| First task round (`CacheStats R1`) | 1,586 | 93,921,885 |
| Later task round | 31,052 | 80,204,111 |
| No detector comparison baseline | 1,092 | 11,647,944 |
| Verified unchanged overlapping wire prefix and routing | 31,025 | 77,448,566 |
| Changed or insufficient comparison evidence | 521 | 85,029,486 |

Task round one is not necessarily the first use of a conversation or provider
cache; 701 first task rounds reported positive cache reads. A zero read does not
prove coldness, and neither log marker inventories all physical attempts.
Every joined row had a sealed full provider-prefix ID: 32,638 distinct, zero
additional occurrences. No exact repeat of a complete prefix ID does **not**
mean no shared leading bytes. The overlap row requires the detector's
`wire_available`, `body_identical`, and `namespace_verified` flags plus a
non-absent comparison baseline. It does not prove provider eligibility,
residency, which upstream cache served a request, the length of its token match,
or how much of the residual was a legitimate fresh tail. No billed savings can
be inferred from this cohort or the offline request goldens.
