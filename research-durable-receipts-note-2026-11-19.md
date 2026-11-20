# ToFu research restoration — durable receipts

**Decision — search closed 2026-11-19T08:29:45Z (UTC).** A bounded proposal independent of paper-style and workflow verification; not a research result, novelty certificate, manuscript assignment or experiment authorization.

## 1. Source and focal question

**Located evidence [L0].** Delivered ledger `ac8e3e5b9e5279ce7b1feca043c1154a6166f9c3:research-workflow-scoped-handoff-2026-11-19.md`, SHA-256 `5729918fd33ad3359d58136e13f8f40309520e1ad0ec09345173065262f97533` (exact Git bytes checked), line 8, names only **`agent-harness`** and **`low-precision`**. Its four cached successes do not establish novelty, reproducibility or superiority. Runtime cells were not reopened.

**Decision.** Focus on `agent-harness`; preserve `low-precision` as the second named topic, explicitly deferred/unsearched, not a variable here. L0's style FAIL and unproven isolation remain unchanged and separate. No current paper ownership or assignment is inferred.

**Idea — question.** After a ToFu interruption, is correct recovery of a benign experiment's retained status limited more by **claim-to-receipt identity binding** or **visibility of unresolved obligations**? Recovery means reading evidence, never automatically restarting work. **Assumption:** these are plausible ToFu failure mechanisms; their prevalence is not established by L0.

## 2. Current, bounded literature scope

**Located evidence.** This turn searched general Web for `Agent Workflow Memory 2024 ACL anthology`; `MLflow tracking experiments run metadata artifacts official`; `W3C PROV data model recommendation 2013 provenance entities activities agents`; `"MLflow" "Managing the Machine Learning Lifecycle"`; `"agent" "experiment" "provenance" "2026" paper`; `"PROV-DM" W3C`; `"Agent Workflow Memory" proceedings ICML 2025`. Academic discovery used `Agent Workflow Memory` and `agent memory provenance experiment reproducibility`. Several general queries failed or returned irrelevant results: no absence claim follows. No year filter excluded older anchors. Four primary abstract/metadata pages were opened; no PDFs or paper full texts were requested.

| Primary reference, date and located abstract evidence | Closest overlap / verification limit |
|---|---|
| **P1 — Wang, Mao, Fried & Neubig, Agent Workflow Memory**, submitted 2024-09-11. https://arxiv.org/abs/2409.07429 . Abstract: induces reusable workflows to guide offline/online web navigation. | Strong procedural-memory overlap with B. Generic workflow reuse is not new here. arXiv record verified; ICML venue lead unconfirmed because OpenReview challenged and ACM returned 403. Citation count unverified. |
| **P2 — Souza et al., PROV-AGENT: Unified Provenance for Tracking AI Agent Interactions in Agentic Workflows**, submitted 2025-08-04, v3 2025-08-20. https://arxiv.org/abs/2508.02866 . Abstract: extends W3C PROV to connect agent interactions and decisions to workflow outcomes, with provenance capture/query evaluation. | Strongest located direct provenance overlap with A. Agent provenance is already addressed. Author metadata reports IEEE e-Science 2025 acceptance; publisher confirmation and citation count unverified. |
| **P3 — Wu & Zhu, Agent Zero Memory: Provenance-Aware Long-Term Memory for LLM Agents**, submitted 2026-08-30. https://arxiv.org/abs/2608.29606 . Abstract: timestamps, evidence pointers, episodic/graph/documentary memory and citation-locked retrieval. | Strong overlap with both A's binding and B's retrieval. arXiv preprint; reviewed venue/citation count unverified. Its fabrication-exclusion claim is not adopted as established fact. |
| **P4 — Wang, Meng, Nam, Pfister & Yoon, ReproBridge: Building Reproducibility into ML Research Artifacts**, submitted 2026-11-05. https://arxiv.org/abs/2611.08452 . Abstract: preserves recovered procedures/decisions for fresh-session reproduction; separate execution-evidence auditor. | Strongest located overlap with the overall durable-receipt motivation and B. Preserving execution knowledge is not new here. arXiv preprint; reviewed venue/citation count unverified. Numerical gains not independently checked or transferred to ToFu. |

**Decision — both directions `search-incomplete`, challenge-located.** P2/P3 independently establish provenance-centered approaches; P1/P4 establish procedural preservation/reuse approaches. These are separate primary works, not independent replications. Individual paper metadata has only one successfully opened primary source; full two-source date/venue confirmation was not achieved.

**Located limit — retrieval deviation.** MLflow and Databricks software-documentation pages were also fetched before returning to abstracts. They are excluded from scientific evidence; no tutorial/API/code was executed. Thus strict metadata/abstract-only retrieval was **not met**, even though the comparisons above use only primary publication abstracts/metadata and L0.

**Decision — unsearched gaps / no guarantee.** No full-text method audit, systematic venue sweep, citation closure, workflow-engine/event-sourcing comparison, W3C specification review, comprehensive MLflow review, accepted-paper recall estimate or complete November 2026 coverage. Recent discovery leads TeleTune and Automata from Agent Traces were not opened or used as evidence. Low-precision remains unsearched. An exact ToFu comparison not located in these abstracts may already exist. Full-survey skill gates were neither attempted nor passed: the owner's bounded, no-mass-scraping scope governs.

## 3. Two directions: prospective local preregistration v1

**Decision.** Freeze the following contrasts, metrics and decision rules in this tracked note before observations; not an external registration. No cases assembled, readers recruited, model called, replay run or measurements collected. Future execution requires authorization, a dated/hashed eligible-case manifest and a locked estimator/sample-size justification. No present power claim.

**Assumption — future population.** Owner-approved, de-identified retained ToFu snapshots only. Human readers inspect frozen evidence and report status/next obligation, without commands, task continuation, models or other-project access. Gold labels distinguish request, attempt, observed outcome, artifact revision and unresolved obligation. Ambiguous gold truth is excluded before assignment, with counts retained; eligible cases are enumerated/hashed before scoring, not cherry-picked after outcomes.

| Direction / hypothesis | Falsifier and strongest overlapping work |
|---|---|
| **A — identity-bound receipts (mechanism lens). Prediction H-A:** structured claim links to exact request/attempt/artifact/outcome reduce false completion beyond identical facts in prose. Rival: more information or next-step salience explains the effect. | Binding main effect must reduce false-completion rate by **≥10 percentage points**, while omission rises by **≤2 points**. Otherwise reject the practical prediction for this population. Prespecified stratum: superseded attempts/unknown outcomes. Strongest overlaps P2/P3; candidate distinction is a controlled identity-conflict reading test, not invention of provenance. |
| **B — unresolved-obligation indexing (retrieval lens). Prediction H-B:** surfacing the next unresolved obligation reduces missed follow-through at fixed receipt evidence. Rival: benefit disappears once evidence is identity-bound. | Indexing main effect must reduce omission by **≥10 percentage points**, while false completion rises by **≤2 points**. Otherwise reject the practical prediction. Prespecified stratum: dispersed obligations. Strongest overlaps P1/P4/P3; candidate distinction is obligation salience at fixed evidence, not generic workflow memory. |

**Decision — controls.** A 2×2 desk-study: binding absent/present × indexing absent/present. All cells contain identical facts, artifact availability, question and visible text budget. Absent binding retains identity facts in prose; absent indexing uses neutral ordering, not deletion. Counterbalance allocation; each reader sees only one version per case. Independent gold adjudicators are blind to condition. Stratify by identity conflict, unknown outcome and dispersed/explicit obligations; balance completed/incomplete/unknown cases. No live state changes.

**Decision — preregistered benign metrics.**
- **False-completion rate:** incomplete/unknown cases labelled complete ÷ all gold-incomplete/unknown cases. A request, historical pass or another attempt's receipt cannot prove completion.
- **Omission rate:** omitted gold unresolved obligations ÷ all gold unresolved obligations. A valid next step can be “obtain missing evidence,” never automatic execution.
- Report abstention separately, definitive-label accuracy on definitive-status cases, and completed-case sensitivity, preventing blanket abstention from masquerading as success.
- Report per-case outcomes, both main effects, interaction and prespecified strata, with uncertainty clustered by workflow/reader. Lock the estimator and sample-size justification before data collection; no significance claim now.

**Decision — interpretation and stopping.** A-only favors binding; B-only favors retrieval; both favor complementary mechanisms; neither rejects both practical predictions. Joint-cell-only benefit is interaction, not either independent mechanism. Threshold-crossing uncertainty is inconclusive, not proof of benefit. Ceiling/floor or too few independent cases are uninformative. Stop at the frozen set, retain nulls/regressions and do not retune thresholds. Controls assume information equivalence and stable gold truth; failure invalidates comparison. Retire the binding intervention if ordinary equivalent receipts eliminate its incremental effect; retire indexing if dispersed obligations are absent or its contrast vanishes.

## 4. Acceptance / execution receipt

1. **Partly met.** §2 supplies dated search scope, four opened primary references, source-bound comparisons, strongest located overlaps, gaps and no novelty guarantee. Exhaustive coverage, citation counts and independent two-source metadata/venue checks could not be verified. Strict metadata/abstract-only retrieval **not met**, as disclosed above.
2. **Met for proposal scope.** §3 fixes two distinguishable hypotheses, metrics/controls and falsifiers prospectively. No experiment, external model call, submission, external write, exploitation, offensive benchmark, unrelated-project access or manuscript assignment occurred. The supplied coordinator Turn was read only for delegation context; local content reads were L0 and this note. Future tests are unexecuted and remain gated on authorization and the manifest/analysis lock.
