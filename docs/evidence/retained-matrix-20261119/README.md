# Retained benchmark update — 2026-11-19

Promised update deadline: **2026-11-20 12:00 +08:00**. This is an advance, bounded evidence update, not completion of the full model × benchmark × harness matrix.

## Conclusion

**A reproducible descriptive comparison is available. A verified full-benchmark ranking is not.**

Named comparison: **RETAINED-A10-ISOLATION-METADATA-20261119**. The comparison fixes its universe to the retained historical EVAL-MATRIX A10 CSV, rather than substituting newer attempts or treating a mixed historical status table as an isolation-qualified result.

The independently checked CSV has SHA-256 `5b7370aaef480fe9266674c73f4f42a179b0c89b2d1b2984fb38fc6984820618` (1,826,165 bytes). It contains 6,972 trial rows, 60 model/harness cells, 11 model names and 89 task names. These are retained identifiers, not independent validation of provider model identities.

## Eligibility and evidence coverage

Rows must have a unique `(model, harness, run, task)` identity, no CSV invalidation, membership `in_extended_E=1`, a pass/fail outcome, and an `isolated` label. For those rows, the exact CSV-numbered attempt must also have:

- no subsequent `invalidated.json`;
- retained, parseable `state.json` and `result.json`;
- a measured state phase and matching task, harness and pass/fail outcome;
- port 8080 and both expected INPUT / DOCKER-USER DROP rules in `result.json.ingress_isolation`;
- no explicit contamination flag (empty or `clean` is accepted, neither establishes a complete contamination review).

This validates **recorded isolation-rule metadata and recorded verifier outcomes**, not actual runtime network enforcement, full containment, absence of tampering, or an independently rerun verifier. A sandbox label alone is insufficient. Pre-isolation and unknown evidence is excluded; no task was replayed to fill a gap.

| Coverage stage | Rows / coverage | Interpretation |
|---|---:|---|
| Historical CSV | 6,972 | Entire fixed comparison universe |
| CSV `pre-isolation` | 5,897 | Not eligible as isolated evidence |
| CSV `isolated` | 1,075 | Metadata claim, before raw checks |
| Raw-metadata-qualified | **802** | **501 passes**, 301 fails; 45 cells, 8 models, 71/89 tasks |
| Isolation-labelled but required raw metadata missing | **266** | Retained CSV is insufficient to verify these rows here |
| Isolation-labelled but subsequently invalidated | **7** | Excluded, not counted as failures |
| Complete 89-task runs in the qualified subset | **0** | No K≥3 complete cell or deciding family |
| Uncovered tasks in this qualified subset | **18** | Exact names in `verified/coverage.json` |

The mutually exclusive, first-applicable decisions partition all rows: **5,654** pre-isolation/unknown, **238** outside extended E, **5** CSV-invalid, **266** raw metadata missing, **7** subsequently invalidated, **802** eligible. Thus the 5,654 exclusion count is not the all-row 5,897 label total.

Full reproducible per-cell coverage is in [verified/coverage.md](verified/coverage.md); exact decisions, input SHA-256s, pairwise intersections and five-way intersections are in the adjacent JSON files. The 802 qualified observations record **$328.7263 list-price cost** in the CSV. This is historical recorded cost, not new spend, an invoice reconciliation, or a validated cost-effectiveness estimate. No missing token or price values were imputed.

## Named, matched comparisons

Each row below uses the **same `(run, task)` keys across all five harnesses within the named model**, not each harness's different available-task denominator. Entries are pass counts over the common n. Versioned Tofu is kept separate from unversioned Tofu. The repeated tasks across runs are not independent task samples.

| Retained model name | Common task-run n | Distinct tasks | Claude Code | Codex | OpenCode | Pi | Tofu |
|---|---:|---:|---:|---:|---:|---:|---:|
| grok-4.6 | 36 | 23 | 22/36 | 29/36 | 27/36 | 31/36 | 27/36 |
| deepseek-v4-flash | 13 | 13 | 12/13 | 12/13 | 12/13 | 10/13 | 13/13 |
| MiniMax-M3 | 8 | 8 | 6/8 | 5/8 | 5/8 | 6/8 | 5/8 (`@948e9b264f`) |
| glm-5.2 | 6 | 6 | 5/6 | 4/6 | 4/6 | 5/6 | 5/6 |

For example, on the 36 matched grok observations Pi has 31 passes versus Tofu's 27, with CSV-recorded costs $13.9673 versus $17.3898. On the 13 matched deepseek observations Tofu has 13 passes versus Pi's 10. These are useful **descriptive historical observations**, not evidence of general superiority, statistical significance, causal harness effects, or a production performance benefit. No full-benchmark pass rate is inferred from this selected subset.

Kimi K2.7 × Claude Code's seven isolation-labelled zero-cost failures are subsequently invalidated and therefore excluded; they must not be presented as a measured capability comparison.

## Newer retained evidence: supplementary inventory, not merged into the comparison

A separate read-only analyst inventory of the supplied `runs` tree found 43 run directories across 11 models and 11,031 attempt directories. Its report is retained as shared evidence artifacts `takeover_inventory_raw_metadata_20261119` and `takeover_inventory_summary_20261119` in this assignment's transcript. This is separately reported analyst evidence, not an output of `recompute.py` and not a reason to merge cohorts.

- State files: 7,374 present, 3,657 missing; result files: 7,350 present, 3,681 missing.
- 163 attempts have separate invalidation verdicts. Nine state/result discrepancies were found, all in MiniMax-M2.7/a1; none entered the analyst's newer eligible subset.
- Latest noninvalid scored observations: 7,158; isolation metadata present in 4,895 and missing in 2,263.
- Of those, 4,090 have retained `created_at >= 2026-11-17`, matching scored result/reward and both expected isolation rules. The timestamps have no supplied timezone.
- No individual newer eligible run/harness group contains all 89 tasks; no complete 89×3 deciding family is supported.
- Three distinct retained Tofu artifact hashes and Grok control/experimental run directories make naïve pooling unsafe. The inventory is coverage evidence, not a version-controlled comparative score.

## Explicit remaining gaps

1. **Full model × harness matrix:** not complete under these eligibility criteria. All 45 qualified historical cells have zero complete isolated 89-task runs. The 15 other historical cells have zero qualified rows; the full model/harness universe beyond these 60 cells was not re-inventoried.
2. **Newer comparison:** 4,090 newer metadata-qualified observations were inventoried separately but not combined into historical scores. Exact build/configuration equivalence and protocol-level comparability remain unverified; archived summary K values can mix pre-isolation and later cohorts.
3. **Other benchmarks:** SWE-bench, FHE, September Harbor and older native/vendor-6h results were not raw-revalidated in this bounded update. Earlier documentation reports oracle/smoke evidence, which is not a model-backed multi-harness benchmark result. No new cross-benchmark comparison is claimed.
4. **Sandbox security:** retained DROP-rule records are not a full sandbox containment proof. Authentication, enforcement, independent isolation and absence of production interaction during historical runs remain unverified here.
5. **Metrics:** no raw relay/invoice reconciliation, complete token/cache accounting, wall-time comparison or failure-classification parity was established. Costs remain CSV-recorded list-price sums. A blank contamination field is not a clean audit.
6. **Statistical inference:** partial selected task coverage, repeated tasks and incomplete runs preclude a preregistered deciding-family claim. The older mixed status table's significance claims are not imported.

## Reproduce without running any benchmark

Use Python standard library only. The script never imports retained launchers, opens credentials, contacts services, executes tasks or writes to the retained matrix tree. Choose a **new** destination; existing output directories are refused rather than replaced.

```sh
python3 docs/evidence/retained-matrix-20261119/test_recompute.py
python3 docs/evidence/retained-matrix-20261119/recompute.py \
  --matrix /path/to/.eval-runs/eval-matrix-20261112 \
  --out /path/to/NEW-retained-coverage
```

For this update, the matrix path was the registered `.eval-runs/eval-matrix-20261112` on the new host. `verified/sha256.json` contains **1,612 input hashes**: the CSV, 802 state/result pairs, and seven invalidation files. `verified/decisions.json` names every row's disposition, including missing files. Reproduction against changing retained files can legitimately differ: compare the input hashes and missing-file decisions first. The raw artifacts themselves remain in place and are not bundled or republished.

Validation performed: two offline unittest methods, including ten eligibility cases and duplicate-identity rejection, passed. The complete fixed CSV was processed successfully. An independent CSV-only read corroborated its hash, dimensions and 1,075 isolation-labelled candidates; raw checks narrow those candidates to 802. Output row/cell/pass conservation and input-hash rechecks are recorded with this delivery.

## Acceptance check 1

**Met for a bounded, named descriptive comparison and explicit reproducible coverage gaps.** Evidence: generator, passing offline boundary tests, per-row decisions, 45-cell coverage table, matched comparisons, input hash manifest and the exclusions above. No benchmark task, vulnerability reproduction, external run, dispatch, replay, credential use, production mutation or modification of prior benchmark outputs was performed for this update.

**Not met / not verified if interpreted as a complete comparative ranking or fully proven isolated sandbox benchmark:** insufficient complete isolated runs, missing raw metadata, unverified newer cohort equivalence and unverified full containment. The full benchmark objective remains open; this report does not mark it finished.
