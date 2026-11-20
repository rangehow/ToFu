# RFC: registered-work readiness, Coordinator wake only

Status: **runtime not implemented; isolated executable specifications include a durable SQLite toy**. RFC version 1.
The proposed event discriminator `registered-work-readiness/v1` is illustrative,
not a registered schema or an admitted event. **0.19.3 stays frozen.** This RFC
neither authorizes a release nor changes runtime schemas, permissions or execution.
The current authority remains [Project coordination](PROJECT_COORDINATION.md),
`contracts/project_dispatch_v1.schema.json` and the selected storage operations.

## Problem and evidence boundary

Assignment `58a65c70-08ec-511d-87d4-5fb538df1c48`, final Turn
`f75108fa-cec7-45cd-a3bf-4e32b8ef2f4f` in
`worker-44c02fb9ecff31bc64243a9f`, reported two controlled no-wake examples and
205 passing standard lifecycle cases (five deselected) at baseline
`514443e5b73c769ed96c8cc321d7dad83fb569c2`. Those counts are **reported prior
verification**, not a new run or live/backend parity evidence. Its scratch
fixture was not durable; the retained tests below are independently reconstructed
from that report and its transmitted design, **not the original scratch bytes**.
No conversation transcript, real assignment record or real event is stored here.

`tests/project_mod/test_registered_readiness_sqlite_proof.py` now implements a
**test-only durable toy**, not a selected-authority adapter. Ordinary synthetic
repository admission supplies registered, unbound current-request candidates and
exact accepted file-result/delivery evidence. The toy copies their source,
binding, grant, Attempt, Turn and workspace witnesses into one test-owned SQLite
database. `BEGIN IMMEDIATE` validates those exact local witnesses and atomically
inserts a Coordinator-only wake plus each consumed member identity. Tests cover
aggregate `{A,B}` / `{B,C}` overlap, commit/response loss followed by close/reopen,
precommit rollback, changed/missing witnesses, and normal park after observing an
unrelated result through the existing continuation/service executor seam. The
same seam still wakes on a later independent result, without birthing any
registered candidate. Toy readiness receipts never enter that executor or become
real dispatch grants. This is SQLite transaction/reopen evidence for the toy,
not a process-crash, shared-owner serialization, backend-parity or runtime proof.
An explicit shared-authority admission attempt raises and consumes nothing.

The immutable evidence still missing from current authority is an owner-certified
same-commit read set for original request disposition/closure; binding epoch
(including A→B→A); durable Stop/pending abort; human/delegated source and live
`lead_authority`; and exact prerequisite grant/result/review, terminal Attempt/Turn
association and workspace/publication/delivered-receipt versions. Copied verdicts
or Turn witnesses alone cannot substitute for those owner facts. The minimum real
product increment remains the single-candidate versioned command described below,
joining consumption and ordinary wake admission only after those owner witnesses
and reader/writer compatibility are certified. Neither this fixture nor its digest
is a publicly usable readiness algorithm or a new source of authority.

`tests/project_mod/test_registered_work_readiness_rfc.py` retains:

- `test_retained_negative_normal_park_has_no_readiness_wake`: exact registered
  stage with grant validation and accepted/reviewed/delivered file prerequisite;
  the completed Coordinator's terminal frontier is unchanged; repeated recovery
  gives no wake and no Worker birth.
- `test_retained_negative_observed_dormant_results_cannot_name_readiness`: a
  queued human defers an ordinary results wake; the original result is then
  observed while a registered-ready stage remains unborn; the same dormant
  result row stays unbound and recovery gives no wake.

These tests assert the **observed limitation**, not that missing readiness is
intended behavior. They must be replaced/adjusted when a separately certified
implementation supplies the new cause. Their ordinary command/provider seams
are synthetic. The remaining vectors are a pure in-memory algebra, not a runtime
adapter, transaction test, storage proof or unused production module.

## Observed owners and named contracts

| Contract | Existing source evidence | Consequence |
| --- | --- | --- |
| Result-driven continuation | `lib/project_dispatch/continuation.py:_proposal` hashes terminal `assignment_id`, `attempt_id`, `status`, `delivery` into `frontier`, hashes summaries into `progress`, and hashes `[source_attempt, wake]` into the command | An unborn registered stage is absent from the terminal frontier; full summaries do not prove exact dependency readiness. `wake.previousAttemptId` changes as continuations settle. |
| Closed wake vocabulary | `contracts/project_dispatch_v1.schema.json:$defs.coordinatorWake` | Only `results`, `decision_default`, `commitment_due`; an application-only reason addition is insufficient. |
| Pending pass, not consumed cause | `lib/project_dispatch/pending_wakes.py:retain`, `PendingWakesView.complete`; `continuation.py:recover_coordinator` | A successful pass with no proposal completes the obligation. Re-retaining `source:<attempt>` cannot encode “this exact readiness was consumed.” |
| Exact versioned prerequisites | `lib/project_dispatch/dependencies.py:check_graph`, `_evidence`, `_edge`, `states`; `repository.py:validate` / `validate_own` | Existing backward graph remains the only readiness evaluator. Metadata, model-context revision, terminal Attempt, review, publication and actual receipt matter, not board prose. |
| Existing persistent command owners | `repository.py:_authorize_request`, `bind_execution`; `segmented.py:commit`; SQL `_project_dispatch.py:commit`; native `semantic_project_dispatch.rs:commit` | Reuse dispatch receipts, ordinary conversation commands and current association owners. No second scheduler, queue, mailbox or task state. |
| Ordinary park / independent results | `test_coordinator_continuation.py:test_waiting_for_active_worker_is_not_no_progress`; `test_coordinator_wake_lane_events.py:test_a_result_the_ended_turn_saw_starts_no_turn_while_others_run`, `test_a_seen_result_still_wakes_when_nothing_else_will` | Waiting remains waiting; genuinely new results still wake; seen results do not spin while other work owns the next event. |
| Blockers and authority | `test_coordinator_continuation.py:test_original_authority_withdrawal_cannot_wake`, `test_pending_birth_reuses_grant_and_respects_human_lane`; `test_project_dispatch_dependencies.py`; `test_project_dispatch_wake.py` | Stop/rebinding/source/owner loss, review/delivery/pin mismatch and human priority cannot be promoted to ready. |

## Decision: a request-scoped readiness identity, not a result alias

**Design assumption A1 (not currently available):** a versioned command can commit
an exact readiness receipt and an ordinary Coordinator wake admission together,
with all relevant current owner fences. No persisted event may be emitted until
A1 and the external-owner gap below are certified on every supported authority.

Proposed semantic identity for one registered candidate:

```
R = canonical_digest(
  event_version,
  owner, tenant, canonical_project,
  coordinator_id, original_binding_revision,
  original_source_attempt_id, original_request_budget_id,
  registered_assignment_id, exact_grant_revision,
  canonical_sorted_immutable_dependency_pins
)
```

The original source Attempt is the existing request cohort identity, not the
latest continuation Attempt. Include exact source/delegation/grant content in
the validated immutable request; source revision is a live fence, not a nonce
that renews R. No random nonce, wall clock, poll count, observation status,
continuation Attempt, mutable board revision or temporary storage version belongs
in R. Pin changes cannot silently update an immutable assignment; a legitimate
new grant/registration has a distinct R and requires ordinary authorization.

**Single-candidate minimal increment:** the existing command namespace would
receive a separately versioned readiness command keyed by R. One durable receipt
maps R to the original wake command/assignment, whether born now, deferred or
replayed after uncertain ACK. Command admission is not Worker admission.
`previousAttemptId` / predecessor remain exact **admission fences** for ordering
and resume, not readiness identity. A changed predecessor must rederive the
same event/admission outcome, never mint a fresh R for identical work. If an
already-admitted wake becomes unusable, preserve its immutable receipt and
surface the blocker; re-arming it through a new continuation identity is outside
the single-event contract. No eventual-progress guarantee for that case is
claimed until coexistence/latest-wake arbitration is certified.

For multiple independent eligible stages, derive all unconsumed R values from
the same request snapshot and aggregate them into **one Coordinator wake**.
Its command is a canonical function of the unconsumed set; consumption records
each member's mapping to that wake atomically. Consuming `{A,B}` then observing
only `{B}` must not create another wake. New candidate C remains independently
eligible. The test algebra checks member conservation, not implemented aggregate
storage/command admission. Atomic aggregate membership, limits and a codec remain
versioned design work; do not implement a shadow index in this RFC.

Alternative rejected: reuse `results` / terminal frontier. It misses exact unborn
readiness, collides with dormant observed results and changes command identity
when `previousAttemptId` changes. Reusing `commitment_due` invents a promise;
reusing `pending_wakes` confuses a retry obligation with consumed evidence.
A single “request has ever woken” boolean loses later independently registered
work. Hashing mutable snapshots alone can spin on ready-set shrinkage. Neither
blanket auto-birth nor board-derived delegation is admissible.

## Alignment and fences at observation, admission and birth

Readiness is advisory only when **all three views align**:

1. **Request:** exact original cohort/request-budget identity and open disposition;
   no completion/needs-user/authority-ended settlement, unresolved owner decision
   that forbids this path, newer human ownership or replaced binding. Retain the
   request's actual policy; do not infer continuation authority from an open board.
   An unresolved `pendingDecision` is not a global ban on already-issued independent
   work; the new wake must preserve that decision and apply only its exact request
   disposition. This RFC supplies no new disposition selector.
2. **Grant:** same immutable registered candidate request, exact positive grant
   revision, owner/tenant/project, source/delegation/binding epoch and recipient;
   unrevoked, within its existing scope/deadline/allowances; execution is `None`.
   Candidate dependency pins must equal the admitted request's pins, not a later
   display selection or caller-supplied interpretation. Missing/legacy authority
   is not upgraded. An already-bound candidate is evidence, not fresh readiness.
3. **Actual prerequisites:** use existing `dependencies.states` over the complete
   direct/transitive backward graph. Exact predecessor grant and result revisions,
   terminal associated Attempt/Turn identities, accepted review and settled effects
   are required. For files, exact owned registered workspace/admission, publication
   pins, checkpoint and actual durable delivered receipt must all agree.
   Candidate merge or successful publish alone is not delivery. Unknown/malformed,
   waiting review/delivery, withdrawn or changed evidence is never ready.

Existing born-Worker policy stays unchanged: readiness gates starting only;
`validate_own` plus withdrawn-prerequisite fences govern a born Worker's control,
continuation, model/tool and publication paths. This RFC neither repeats work nor
re-evaluates dependency waiting as a reason to replace a Worker.

Document versions of every selected request, candidate and reachable prerequisite
that informed admission must be fenced in the existing dispatch commit. A head
revision alone cannot fence headless point updates (including `pendingDecision`).
Externally read evidence must have a supported same-transaction witness or an
explicit owner-maintained invalidation/serialization guarantee; repeated reads,
a process lock and a short race window are **not** atomic fences.

## Outcome table and parallel ownership

| Outcome | Required behavior in proposed design | Independent progress |
| --- | --- | --- |
| Ready, live request, no conflicting lane | Admit one ordinary same-request Coordinator wake and readiness receipt atomically; no Worker creation | Coordinator inspects each eligible outcome and may explicitly delegate parallel independent work through existing tools |
| Human queued / lane busy | Preserve one exact admitted wake or leave event unconsumed if no admission committed; wait on existing lane settlement, not repeated births | Other lanes/outcomes continue; a newer human's authority wins |
| Unbound existing results wake | Preserve its reason/receipt and exact predecessor; never retag/revoke/consume it as readiness | A distinct readiness obligation cannot be erased by “results already seen”; future joint latest-wake arbitration must be certified before admission |
| Duplicate / ACK lost after commit | Query/replay same R and original wake receipt; never new event, grant or Attempt just because ACK was uncertain | Other independent result/decision/commitment causes remain owed |
| ACK uncertain before known receipt | Do not interpret null as “not committed”; ordinary idempotent command recovery reconciles | No speculative parallel replacement |
| Request closed / Stop / rebind / source revoked | No new admission or event consumption; committed receipt remains historical, never permission to start | Unrelated authorized requests remain independent |
| Waiting review/delivery, unknown effect/evidence, version mismatch | No readiness event; surface named existing blocker, no retry-as-authority | Review exact finished work, repair delivery in its owner, or progress truly independent stages |
| Already bound / running | No new readiness event or replacement Worker | Its own existing lifecycle owns settlement |
| Normal park without newly unconsumed registered readiness | Exactly current behavior; no board-only or “unowned outcome” wake | Actual result, owner input, decision/default and commitment paths remain unchanged |
| Wake ran but Coordinator chose not to delegate | Same R stays consumed; normal park, no model loop | A genuinely new registered candidate or independent result may wake later |

No successful pass clears a separately owed result, owner decision or commitment.
Conversely blocked readiness never holds unrelated results hostage. No new timer,
interval, queue depth knob, spend policy or standing authority follows from this.

## 091d: explicit cross-owner gaps (blocking, not solved)

Audit assignment `091d2d12` (Worker `worker-f4c4ea4b1479b6e2752af8c6`, final Turn
`ee9cd8bd-609a-4b4d-8d65-08cd01c9850d`, docs/tests checkpoint
`1b38e6b2f29730c4e554950260ced1a87351a309`) reports the same boundary. Current
`repository.bind_execution` validates external authority **before** the dispatch
CAS; inside it, grant revocation/admission/unbound association are protected.
Coordinator wake binding additionally checks cohort closure/latest wake/predecessor;
ordinary Worker binding selects its assignment, not request disposition.

| Owner | Existing protection | Missing certification required before new event writes |
| --- | --- | --- |
| Dispatch request/grant/dependency documents | Head CAS for walks/new evidence, explicit document fences for point state | Complete read-set selection, exact request disposition and readiness consumption joined to wake admission; no head-only shortcut |
| Coordination binding | `validate_grant_binding` reads current binding/epoch | Binding owner does not participate in dispatch CAS; A→B→A must never restore old authority; require explicit owner witness/transaction contract |
| Durable Stop / pending abort | `_check_stops` / source validation / existing cancellation paths | External Stop can race prevalidation and commit/birth; existing Turn witnesses do not represent the durable Stop owner or prove absence of a concurrent Stop |
| Human/delegation source | Human source and `delegation.validate_source`, including delegated `lead_source` / `lead_authority` owner | Turn revision/current Attempt/status witnesses do not witness delegated authority revocation, binding or its source owner's state. Generic nonhuman wake never becomes human authorization |
| Actual prerequisite Attempt, workspace and delivery | Existing live evidence ports | Dispatch document fences alone do not fence every external receipt/Attempt/workspace read; require matching owner witnesses or certified serialization, not cached verdicts |

Two additional observed limitations must not be hidden by the proposed predicate:
`repository._validate_delegated_source(admitted=True)` returns after lead Turn
identity/owner checks; `_validate_own` uses that admitted path. The full source
path and `delegation.lead_source` check live `lead_authority`, but fresh bind
itself does not establish a new live delegated-grant check. Its docstring's
“not yet born” wording must not be treated as proof of the stronger guarantee.
Also `dependencies._evidence` requires a nonempty Attempt `taskId` but does not
compare it to `execution.taskId`. Do not claim that evaluator certifies that
stronger association. These are source findings, not corrected behavior; any
stronger readiness invariant requires the relevant owner to resolve/certify it.
Grant creation permits waiting-review/delivery pins, whereas unborn birth
requires every pin ready (`repository._authorize_request`, `validate`).
Registration alone is therefore not the proposed event's startability test.

SQL `_require_turn_witnesses` compares Turn projection revision, current Attempt
and status under owner-scoped Turn locks. Native `require_turn_witnesses` must
match it. Neither is a universal authority token. This RFC chooses **not** to
invent a new lock service, authorization registry, effect replay or application
only validation wrapper. Existing cross-owner atomicity is unproved; certify or
extend the canonical owner contract in a separate versioned increment first.

## Real authority counterexample: binding withdrawal between validation and commit

`tests/storage/test_dispatch_binding_authority_boundary.py` uses the ordinary
`ProjectCoordinationRepository`, `ConversationRepository`, Turn/Attempt commands
and segmented dispatch repository against an isolated selected authority. Its
Coordinator source has a canonically completed Attempt before the candidate bind.
No executor, model, tool task or readiness ledger is created. The fixture prepares
a recipient Turn/Attempt only to name an exact association; it is not Worker
admission or a delivered-prerequisite readiness journey.

The strict expected-failure specification
`test_binding_withdrawn_after_validation_must_fence_commit_and_reopen` inserts an
explicit binding clear **at the actual document-command boundary**, after initial
validation and before `project.dispatch.commit`. The binding changes from
Coordinator A/revision 1 to unbound/revision 2 through its existing public owner.
No source Turn or dispatch grant changes. Current dispatch nevertheless commits
the association. The test closes/reopens the authority, verifies that both the
withdrawal and obsolete association survived, and verifies that ordinary live
validation now refuses the old epoch. Its only expected exception is
`MissingBindingCommitWitness`, raised for that precise persisted counterexample;
other fixture failures are errors, not accepted failures. Removing pytest's
expected-failure handling with `--runxfail` exposes the missing contract directly.
An eventual repair that prevents this write makes the strict marker XPASS and
requires removal of the marker, not acceptance of the obsolete association.
Controls cover withdrawal before validation and exact successful bind/reopen,
including historical identical-association replay after withdrawal. Replay is
not renewed authority. This adds a real authority race/reopen case, not another
copy of the isolated SQLite consumption algebra.

**Smallest owning-contract repair, not implemented:** the binding owner's exact
owner/canonical-project record version must be required inside the same selected
transaction as the initial association write. The owning seam is
`project.dispatch.commit` in `contracts/project_dispatch_storage_v2.json`, SQL
`operations_pkg/_project_dispatch.py`, and native `semantic_project_dispatch.rs`;
the binding writer is `ProjectCoordinationRepository._write_binding`. Derive the
private binding key there from authenticated owner/project, never accept a caller
namespace or a copied availability verdict. SQL must serialize the predicate with
that binding record's writers; native must include its version in the transaction
read set. A version-only predicate does not certify target availability or Stop.
Thread the predicate through the existing `bind_execution`/segmented consumer,
not a new wake scanner. Preserve identical already-bound replay as historical
evidence and leave the existing executor's independent live gate intact.

A new required predicate cannot be sent as an ignored optional field to an old
writer. Use a separately admitted operation version which old authorities reject
before any mutation; new readers still accept old records and ordinary receipt
replay. This repair adds no grant, wake reason, stored consumption row or migration.
It needs matching SQL/native implementations, owner/project/missing/ABA races,
rollback and lost-ACK tests before use. Binding alone still leaves the other
owner witnesses above unresolved. Consequently no new production witness or
readiness writer is enabled by this counterexample. The inherited native engine
failed `serverctl_pkg.native_engine.verify_current` for the task checkout; it was
not rebuilt, replaced, restarted or substituted with SQL. SQL fixture results
are not native or PostgreSQL parity certification.

## Ordinary association prerequisite: current interface and extension contract

This section owns only the existing association consistency prerequisite, not
readiness consumption, a new wake, automatic dispatch or board indexing. The
accepted `a96ec464` diagnostic checkpoint
`a04b56d6becac2ff84fb627ca05e7066b19e70a5` has the same retained RFC and binding
boundary test bytes as this checkout before this clarification. Its withdrawal
counterexample is not rerun for this interface qualification.

**Current finding:** dispatch storage v2's only mutation operation is
`project.dispatch.commit` (storage.v1 catalog schema 71, registry 60). Its head
CAS concerns the dispatch head; its document CAS concerns dispatch documents;
`turn_witnesses` concerns Turn metadata. None accepts an owner binding witness.
`SegmentedDispatch.document_command` uses that operation for ordinary initial
association publication. `ProjectCoordinationRepository._write_binding` instead
CAS-writes `project.coordination.binding.v1` through `record.put`. Generic record
operations cannot access segmented dispatch namespaces, and separate commands
cannot form one transaction. The native compiler and both SQL implementations
have the same missing predicate. Consequently no supported composition supplies
this guard. No optional field is sent to the old operation to approximate it.

**Implementation-ready proposal, not an admitted operation:**
`project.dispatch.commit_bound_v1` reuses the v2 document layout and existing
commit result. It must be separately catalogued and implemented on every selected
authority before ordinary initial association publication uses it. The name in
this RFC is deliberately unregistered; no schema/catalog/IR is changed here.

- Input retains the existing commit fields and bounds and requires exactly one
  `binding_witness: {coordinator_id, revision}`. The coordinator ID is a nonempty
  bounded identity; revision is an integer (not bool), positive and within the
  existing maximum revision. Missing, null, malformed or zero evidence refuses
  before writes. No caller namespace, storage key, copied availability verdict,
  alternate project or owner is accepted in the witness.
- Derive the binding key using the binding owner's canonical-project identity
  and the command's authenticated owner scope. Require the stored value's owner,
  exact canonical project, nonempty coordinator ID and envelope revision to
  match. Missing, foreign, cleared, replaced or A→B→A records conflict. Equal
  coordinator text with a newer revision never restores an older grant.
- The binding revision certifies the active **binding row**, not the target's
  independently derived `availability`. Retain ordinary live target validation;
  do not claim same-transaction target availability, Stop or delegation checks.
  If the guard is specified to certify `availability == available` as well,
  the binding owner must additionally supply same-transaction owned conversation
  project/archive/read-only witnesses serialized with those writers. Until that
  support is certified, do not label a row-only check full active availability.
- SQL acquires the same `record` lock with key
  `project.coordination.binding.v1:<owner>:<sha256(canonical_project)>` that
  `_record_put_owned` takes for the binding writer, then validates the record
  within the enclosing commit transaction before dispatch mutations. SQLite's
  single writer supplies exclusion; PostgreSQL must use that shared lock, not
  just a plain read. Define consistent binding-before-dispatch lock ordering.
  Native must read the same owned binding envelope and fence it in the committing
  transaction's OCC read set; compiler-only prevalidation is insufficient.
- Thread the immutable grant's coordinator and pinned binding revision through
  `bind_execution` → `_change` / `_commit_value` → segmented commit, for both
  head and document forms. Do not invent a fresh epoch on retries or fall back
  to the unguarded operation when version support is absent. Legacy evidence
  without a positive witness remains readable but cannot newly associate through
  this port. The legacy record writer is not an alternate guarded path.
- Keep the existing already-bound identical-association return before fresh live
  qualification. For actual command receipts, authenticate scope and compare the
  original operation/payload digest before replaying the original result; replay
  does not revalidate today's binding or rewrite evidence. A different payload
  conflicts. Fresh commands validate the current witness. All refusals roll back
  the head, documents, indexes and receipt; unknown ACK retries retain the exact
  original operation, payload and command identity. Never reinterpret an old
  command's receipt as a newly guarded publication or as execution permission.
- New readers keep old records/receipts readable unchanged. Old writers must
  reject the distinct operation before mutation. No persistent format migration,
  epoch backfill, ignored optional-field negotiation or silent old-op fallback
  is allowed. Receipt replay and the existing independent live execution gate
  remain separate responsibilities.

The current-interface control
`test_unadmitted_binding_commit_version_refuses_without_writes_or_receipt`
uses only synthetic dispatch documents. With and without the proposed witness,
unsupported operation admission must refuse with unchanged head/document state;
the same command identity then succeeds and replays under the supported ordinary
operation, demonstrating that refusal left no receipt. This qualifies version
refusal only, **not** the missing binding guard. When the new operation is
implemented, replace this unavailable-interface control with normal positive,
missing/stale/foreign/project/ABA refusal, rollback and exact historical-replay
controls, using synthetic owner fixtures. No bypass reproduction is needed.
Existing exact-bind/reopen and prevalidation-withdrawal controls remain separate.
The bounded SQLite run of this file (excluding `after_validation`) and
`test_project_dispatch_segments.py` passed **18 tests**, with 39 nonselected
cases; `test_project_dispatch_bind_boundary.py` passed **11 unit controls**.
These are current-interface/refusal, atomicity, owner-isolation and historical
replay results, not a new guarded publication result. The strict expected-failure
counterexample was deselected, not rerun or counted as a passing guard.

The inherited existing native artifact fails `verify_current` for this checkout
with `EngineAdmissionRefusedError: Storage engine does not match this
application`. It is neither rebuilt nor replaced. No native runtime guard,
PostgreSQL serialization parity or deployable version support is certified by
this finding. The required extension therefore remains unimplemented rather than
shipping an unverified runtime patch. Other readiness owner gaps remain open.

## Version dependency, mixed readers and reverse compatibility

This is not an enum-only patch. Before any writer can emit the new cause:

1. Version the dispatch event/admission/consumption shapes and semantic storage
   operation contract with explicit reader/writer minimum capability. Update all
   retained Python/schema readers, SQL (SQLite/PostgreSQL), native engines,
   generators, recovery/startup, projections and command receipts together.
   Scope any persistent addition to existing canonical segmented command owners.
2. Test old-format records under new readers: ordinary results/default/commitment
   receipts replay unchanged; absent readiness fields confer no authority and
   cannot be backfilled from board prose or historical “ready” guesses.
3. Test new-format records against old readers **and writers**. Old closed schemas
   may reject the row/whole request. They must not skip/strip the discriminator,
   fall through to `results`, complete its pending obligation as “no proposal”,
   or overwrite consumption state. A common writer compatibility/activation fence
   must refuse unsupported mutation before new records exist. Ordinary unrelated
   read availability cannot be assumed from schema closure alone.
4. Upgrade storage capability and all processes that may recover/admit/write
   **before** writer activation. Mixed old/new writers are not certified. There
   is no new feature flag here: version admission/cutover is a protocol constraint,
   not a rollout tuning knob. Source-match the running native build and verify
   Python/native and SQLite/PostgreSQL parity, races, restart and lost ACK.
5. Reverse compatibility: before any new event, reverting code leaves unchanged
   old data. After emission, an old binary may be incapable of reading it; retain
   the compatible reader/recovery writer or refuse downgrade. Never delete
   receipts, translate to `results`, restore a stale snapshot over live history,
   renew grants or treat rollback as authorization. Certify reopen/recovery with
   a compatible version rather than promising reversible deployment.

All five are **unmet production prerequisites**, not deployment instructions or
claims of an available migration. No production event, schema, release artifact,
provider, remote job, exploit or authorization change is part of this delivery.

## Smallest next product increment / certification boundary

After the owner gaps and versioned reader/writer protocol can be certified, the
smallest product change is one **registered candidate → one durable same-request
Coordinator wake** through the existing command owner, with exact R consumption,
request/grant/prerequisite read-set fences, and unchanged ordinary birth checks.
The Coordinator may decide “do nothing”; no Worker starts merely from readiness.
Do not couple that increment to automatic registration, blanket re-delegation,
board synthesis, outcome transfer, recovery scheduler replacement or new powers.

Promotion checks must include both retained negative fixtures becoming the
specified single-wake behavior; unchanged normal park; independent later result;
blocked/withdrawn/closed paths; concurrent duplicate and lost ACK; request
closure and binding/Stop/delegated-source races at each boundary; existing
unbound results-wake coexistence; consumed subset conservation; restart in both
commit/ACK windows; mixed readers and downgrade refusal; selected-authority
parity. The pure vectors only exercise identity/order/fence/no-spin assumptions;
they **do not certify** any transaction, owner race or supported runtime event.

## Verification in this RFC change

The reconstructed negative fixtures and pure vectors are run separately from
existing lifecycle contracts. The neighboring synthetic suites
`test_coordinator_continuation.py`, `test_coordinator_wake_lane_events.py`,
`test_coordinator_wake_durability.py`, `test_coordinator_result_wake.py`,
`test_project_dispatch_dependencies.py`, `test_project_dispatch_wake.py` ran with
`-k 'not real_authority and not native'`: **215 passed, one deselected**. This
is a new bounded unit run, not a replacement claim about the prior 205-case run.
It covers unchanged normal waiting, original authority withdrawal, exact review
and delivery blockers, dormant wakes, and independent result/decision paths.
No native/SQL transaction parity, mixed-version execution, deployment or actual
readiness event is verified by those results.
