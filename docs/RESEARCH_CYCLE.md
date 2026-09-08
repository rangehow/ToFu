# Research cycle

## Ownership

The existing five-stage production recipe remains the only runtime. Evaluation
contains a checkpointed critique/revision/re-review cycle. Contracts live in
`lib/research/contracts.py`, `lib/research/revision.py`, paper resource constants,
and `contracts/research_program_v1.schema.json`. Persistence remains owner-scoped
research artifacts and the existing Research Foundry workspace, not a new store.

## Revision

`idea_id` and `idea_version` identify candidate lineage. At most two revisions
and 24 issues are admitted. Issue overflow stops explicitly, preserving the
review rather than silently dropping objections. Evidenced medium/high
retrieval, survey and ideation objections trigger checkpointed targeted harvest,
survey rebuild, dependency revalidation and ordinary novelty/rubric gates.
Parser/infrastructure objections remain unresolved work. Explicitly closed gaps
retire linked candidates; missing IDs cannot retire unlinked ideas. Publication
uses the revised survey/corpus and candidate state, not the original artifact.

Silence is not resolution. Explicit resolution requires evidence agreement from
at least two valid judges. This is model review, not scientific proof or enforced
model diversity. No change, forced-final generation, errors and budget exhaustion
stop revisions. Original snapshots, target versions, issue IDs, retirement,
usage and stage checkpoints survive reload. Publication retries do not repeat
completed model phases. Upstream snapshot changes invalidate revision caches.

Ideation repairs only unambiguous metadata. Excluded papers cannot be direct
novelty neighbors; cross-domain inspiration remains separate. Ambiguous semantic
relinking is not guessed.

## Corpus and context budgets

Default target is 60 papers, with a 200-record ceiling including revision
admission. `discovery.py` and `literature.py` combine arXiv, Semantic Scholar and
CrossRef metadata, canonical DOI/arXiv identities and real directed citation
edges. Limits: six keyword rounds, 40 rows/call, eight graph calls, two hops,
18 academic HTTP requests, 1 MiB/response and 64 status receipts. Cancellation,
source failures, deduplication and stop reasons are explicit. DOI-only records
remain metadata-only: existing harvest does not pretend to read them.

Harvest reuses 40-paper batches and the existing parser lease. Survey reads at
most 200 × 6,000 stored original parsed characters (1.2 million total), never
substituting generated reports for original evidence. Every admitted nonempty
paper receives bounded excerpts, then map/reduce synthesis. This is all-paper
**excerpt coverage**, not full-text coverage. Coverage records failed/unread
papers; partial maps cannot report complete coverage. Prompts remain bounded by
40,000 characters and a shared dispatch/token envelope. Task-local source/model/
owner/version-keyed caches are reconstructible, not a new library authority.

Evidence cards retain exact character offsets, quotes, source version/hash and
snapshot SHA-256. `entailment.py` checks quote identity deterministically, then
independently assesses each scoped gap against bounded evidence batches.
Supported competing evidence can close a gap. Semantic verdicts remain explicitly
`model_assessed`; invalid or unvisited evidence leaves uncertainty. Neither
citation existence nor retrieval absence proves support or novelty.

## Experiment and manuscript validity

Runs separate execution state from scientific outcome. API submission success,
pending/missing/failed receipts and ordinary tool success cannot manufacture
scientific support. Current hypothesis/protocol and evidence digests invalidate
stale claims. Manuscript scientific snapshots must remain current independently
of compilation byte digests.

Protocols include competing hypotheses and minimal discriminating experiments
with decision rules, expected information/value and explicit costs. Current run
receipts derive `retain`, `refine`, `refute` or `inconclusive` decisions and
prioritized next-action proposals. These proposals require confirmation; they
are not autonomous compute authorization. Scoped negative claims can be valid
outputs of refuted hypotheses. Structured evidence-backed baseline fairness,
uncertainty, seeds, validation split and claim traceability gate manuscripts;
mentioning these terms in prose does not satisfy the audit.

Action context removes historical receipt prose but retains evidence references.
Input ceiling is 48k tokens; output ceiling 8k; unknown model windows use 32k.
Oversized exact sources fail visibly instead of truncating. Existing confirmation
boundaries for experiments, analysis, compilation and publication remain intact.

## Workbench progress

`contracts/research_progress_v1.schema.json` owns the additive
`research_progress` projection carried by the existing research task events.
`scripts/generate_research_progress.py` generates TypeScript types and runtime
validation from that same schema. Projections carry a run ID, monotonic
revision, business-progress clock and bounded blocking reason; heartbeat/ping
observations cannot advance the revision or business clock.
Execution settlement remains generic task status; connection/reconnect state is
client-owned and quality is separate. A running projection is not permission to
revive a terminal task. Cancelled/terminal research callbacks are suppressed;
publication checks cancellation before each owner-scoped repository write.

Search-round receipts report actual retrieved/new identities and bounded unique
discovery counts. Per-paper receipts follow harvest persistence; admitted,
parsed, cache-hit and failed counts stay distinct. Stage done/recovery receipts
are emitted only after checkpoint acknowledgement. Candidate counts come from
checkpointed ideation; published-artifact readiness follows the publish stage,
not streamed model prose. Projection counts cap at 200, with unknown excerpt
coverage and candidate totals represented as null, never fabricated percentages.
Progress reuses the bounded generic task replay buffer; it is not a new durable
event journal. Recovery reconstructs stage summaries from existing checkpoints.

Survey/evaluation currently expose stage/tool activity, not a complete semantic
per-paper reading or per-judge review timeline. Excerpt coverage remains unknown
in the workbench projection until an owning coverage receipt is integrated.
Checkpoint durability and repository publication are distinct: a survey stage
checkpoint alone does not advertise a repository-ready survey artifact.

## Durable run records and reopening

`contracts/research_record_v1.schema.json` owns run snapshots. The repository
`lib/research/records.py` writes through existing Sidecar `record.*` operations
in owner-prefixed `research.runs.v1` / `research.run_index.v1` namespaces; no
parallel local database is opened. Start acknowledgement requires a durable
record before spawning. Business receipts (stage boundaries and individual
harvest results) replace the snapshot before live publication. Write refusal
stops execution with an error, never clean success. If terminal persistence
also fails, the last committed snapshot remains recoverable as interrupted.

Snapshots retain up to 200 captured paper metadata rows, bounded titles/errors,
progress and acknowledged pipeline checkpoints; each record caps at 8 MiB.
Token deltas and heartbeats do not cause durable writes. The compact index is
separate from checkpoint bodies and lists at most 200 newest-created runs.
Index and snapshot commits are sequential, not atomic; an index-write failure
can leave the snapshot addressable by task ID before it appears in the list.
Run records have no TTL; task replay and local workdirs remain separate runtime
resources. Original paper content stays in the owner-scoped paper library.

Reopening a record checks the live registry. An absent pending/running task is
`interrupted`, not running, and exposes explicit resume. Resume restores the
acknowledged pipeline checkpoint into the existing production workdir and
uses the same recipe; an incomplete stage may repeat and reuse paper cache.
A still-registered terminal task is not revived. The existing startup manifest
sweep may resume eligible local manifests, but database records alone do not
promise restart auto-execution. Snapshot reads never dispatch work. Revision
substage scratch files are not included in the pipeline snapshot; losing those
files may repeat unfinished revision work. Generic task events remain bounded
live replay. Completed artifacts remain on the existing lookup endpoint, and
run detail retains its own `result` so repeated directions cannot mix outputs.

## Workbench presentation

Captured paper receipts render in a primary, bounded 200-card library with title,
source identity, processing state and reader access. Cards appear before the
stage completes; counts remain secondary. The landing page reads durable run
history first. Reopening uses task identity and its frozen result, not the latest
artifact for the same direction. Interrupted runs offer an explicit resume button.
The browser retains one existing task subscription and no local persistence store.

## Verification

`tests/test_research_*.py`, `tests/test_paper_survey*.py`,
`tests/test_paper_ideate*.py`, and `tests/test_paper_harvest.py` specify offline
execution, resume/cancellation, storage/usage, evidence bounds and stale science.
`quality_comparison.compare_quality` accepts at most 100 independently audited
matched run pairs with frozen task/corpus/model/budget identities. It excludes
missing metrics, self-scores and unmatched budgets; reports descriptive paired
differences only. References still require external audit. No live-model quality,
statistical significance, reproduction success or conference acceptance is
inferred from passing software tests.


Real Chromium specifications in `tests/test_research_runtime_browser.py` and
`tests/test_research_reading_browser.py` cover synthetic transport failure,
Stop rejection, late snapshots, long Chinese headings, narrow screens, keyboard
focus and 200-paper reading continuity. These do not establish external-provider
long-task reliability or an end-to-end performance baseline.
