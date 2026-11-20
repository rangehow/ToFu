# Newer 4,090: metadata-only qualification ledger

## Decision

**No newer ordinary descriptive comparison is established from the accessible retained aggregate receipt. No leaderboard is produced.** This does not establish that an ordinary comparison is impossible. The promised prior report from assignment `587cdf6f-9ec8-54c1-8cc7-ea7d981d4488` is unchanged; this is a separately scoped continuation, not a new benchmark run or a replacement report.

The primary numerical source for the newer cohort is the retained [summary receipt](../retained-matrix-20261119/README.md), lines 55–69. Its SHA-256 is `c93c3df7bc1f5da14ceb8d0b3b63230b73f3a1a58f3422d8097769f91acda515`. It reports a separate inventory of **4,090 metadata-qualified observations**. This assessment does not independently recount their membership. The two referenced inventory artifacts, `takeover_inventory_raw_metadata_20261119` and `takeover_inventory_summary_20261119`, were not accessible as full underlying membership receipts in this assessment. Bounded filename discovery and the unavailable shared-artifact index do not establish that those artifacts are absent everywhere. The Coordinator confirmed that no authorized direct metadata-only receipt path had surfaced at that point. Subsequently, a bounded search of the source analyst's retained transcript corroborated compact totals: scored/measured 7,158; isolation metadata present 4,895 / missing 2,263; newer eligible 4,090; newer missing isolation and newer mismatch/missing-result each 0; complete isolated 89-task groups empty. These are nested analyst-report aggregates, not a new membership recount or a second independent inventory. The supplemental compact totals do not establish exact build/policy groups or matched/non-overlap support and do not enter the ledger arithmetic. Provenance: conversation `worker-50a8a6083ebe5ed9bf53aada`, Turn `0cf88024-3f0f-4e92-8468-cc1b18014979`, nested analyst `swarm-agent:7a156d2a344440a1a40355a25ea176e6`, retained artifact search `full_89_isolated`.

## Exact supported counts and unknowns

| Quantity | Count / status | Meaning |
|---|---:|---|
| Reported newer metadata-qualified observations | **4,090** | Retained analyst-summary claim, not independently recounted rows |
| Demonstrably comparison-qualified from accessible evidence | **0** | No group can yet be admitted; not an estimate of actual eligible rows |
| Demonstrably ineligible | **0** | Missing group evidence is not a failure verdict |
| Qualification unresolved / excluded from current comparison | **4,090** | Aggregate coverage ledger, not invented per-row exclusions |
| Exact model × harness × build × policy group count and eligible counts | **Unknown** | No membership mapping supplied |
| Ordinary matched denominators and matched outcome counts | **Unknown** | Neither valid matches nor their absence established |
| Canonical overlap with historical cohort or between candidate groups | **Unknown** | Separate reporting/date cutoff do not prove disjointness |
| Complete newer 89-task run/harness groups | **0 reported** | Not a requirement for a limited ordinary comparison |

Conservation is `0 + 0 + 4,090 = 4,090`. The historical 802 are not included in this universe, and no union count is claimed. Zero demonstrable admission must not be described as zero actual ordinary-eligible observations.

## Why pooling is not justified

The source reports three distinct retained Tofu artifact hashes and Grok control/experimental directories, but does not expose their mapping to the newer observations. Exact model identity, harness/build attribution, policy/configuration and protocol equivalence remain unresolved. Therefore distinct or unknown versions/arms must not be pooled. A retained model name is not independent provider-identity validation. The cutoff `created_at >= 2026-11-17` has no supplied timezone and cannot establish historical non-overlap.

Isolation-rule records and labels are not runtime enforcement or containment proof. This assessment neither reads raw state/result/task files nor certifies sandbox containment. It is distinct from sandbox task eligibility, authorization, dispatch or attempts.

## Minimal qualification gap (metadata only)

The following existing retained evidence, if accessible, is enough to reassess without any task content or benchmark execution:

1. A content-free membership receipt or auditable aggregate receipt with input digest, precise selection, invalidation and deduplication rules, and conservation to the newer 4,090.
2. Exact model identity, harness identifier/version, artifact/build digest, policy/configuration/protocol digest and control/experimental arm for each group, with group counts. Unknown keys stay unknown, not a common pooled value.
3. Canonical opaque observation identities or independently auditable intersection counts proving non-overlap with the historical cohort and preventing duplicate observations across candidate groups. Canonicalization must remain stable across renames/aliases; a newly hashed path is not proof of unique execution identity.
4. Within compatible groups, common opaque task-revision/trial keys, eligible common denominators and matched outcome counts. Each observation contributes at most once within a declared comparison; reuse across separately reported comparisons must be disclosed, not pooled as independent evidence.

A limited ordinary descriptive comparison can use partial, nonduplicated common support; it does **not** require all 89 tasks or three complete repetitions. It must identify the compared harness/build arms while keeping shared model, task revision, repeat/trial protocol and policy dimensions compatible. This assessment adds no arbitrary sample threshold and no statistical, causal or global-superiority claim.

## Reproduction and evidence boundaries

- [receipt.json](receipt.json) is a content-free, manually transcribed aggregate fixture. It does not pretend to enumerate 4,090 observations.
- [assess.py](assess.py) reads only that fixed fixture, itself, and the fixed prior README for hashing. It never traverses benchmark directories, imports retained launchers, contacts services or opens task data. It rejects populated membership claims rather than silently trusting new unreviewed evidence.
- [verified/ledger.json](verified/ledger.json) contains the aggregate exclusion/coverage ledger and minimal gap. Unknown group and intersection counts are JSON `null`, not zero.
- [verified/manifest.json](verified/manifest.json) binds the exact source, fixture and assessor bytes and generated ledger hash. There are no timestamps or environment-dependent output fields.
- [test_assess.py](test_assess.py) passed **9 offline unittest methods**, including fixed-universe/count validation, 15 synthetic unknown-membership cases, conservation, claim boundaries, deterministic hashes, reproduced bytes, checked-in output equality and existing-destination refusal.

Run only the offline assessor and tests from this directory's retained code:

```sh
.venv/bin/python3 docs/evidence/newer4090-metadata-20261119/test_assess.py
.venv/bin/python3 docs/evidence/newer4090-metadata-20261119/assess.py --out /path/to/NEW-ledger
```

Existing destinations are refused. No downloads, credential access, exploit data, vulnerability targets, task descriptions/prompts/outputs/commands/files, external benchmark calls, dispatch, replay or benchmark execution are inputs. No prior report is modified. Local synthetic tests are not benchmark attempts.

Independent source-receipt audit: reviewer `8c318729` confirmed the same limits and explicitly rejected treating missing complete runs as proof that ordinary matching is impossible. Implementation reviewer `1f3a2d74` independently ran the original 8 synthetic test methods and a fresh assessor invocation, recalculated all manifest hashes and found no blocking implementation/claims defect. Their nonblocking suggestion to compare checked-in outputs against regenerated bytes was added as the ninth test and passed. These subsequent documentation and regression-test changes do not alter assessor, fixture or generated ledger bytes.

## Acceptance checks

1. **Met for the permitted explicit data-only gap outcome.** The latest cohort is separate; exact supported aggregate counts and unresolved exact group counts are distinguished above and in the ledger. Nonpoolability and the minimal qualification gap are explicit. **Could not verify** exact per-group eligible counts, canonical non-overlap or any sufficient ordinary matched cohort, because underlying membership/comparability receipts are inaccessible. No containment certification or global ranking is claimed.
2. **Met for reproducible data-only artifacts and scope restrictions.** Fixture, deterministic code, manifest and passing synthetic checks are retained separately. Prior-report immutability is checked through Git's scoped diff at delivery. No benchmark task or offensive content is accessed or executed; no credentials, download, dispatch, replay or external effects are involved. Source-receipt and implementation reviews are independent; their exact verification limits are recorded above.
