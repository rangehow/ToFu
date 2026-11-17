# Project dispatch authority

## Ownership and authority

`repository.py` owns authorization and evidence per owner/canonical project.
`segmented.py` reads selected documents and commits their deltas, including
resumable legacy cutover. A delta that creates evidence or decided from an index
walk commits through the shared project-head CAS; every other delta is a
document commit fenced only by the documents it writes, so Workers' occurrences
never move the head. Only index walks re-read the head; a point read is
consistent per document. Request/admission definitions come only
from `contracts/project_dispatch_v1.schema.json`. Current coordination metadata
belongs to `lib/conversations/project_coordination.py`; source turns belong to
`lib/turn_lifecycle.py`. Identity, service, routes and runtime integration are
separate owners. No repository method creates a task, invokes a model/tool,
adds a worker, or writes SQL.

## Ports and invariants

`ProjectDispatchRepository(owner, project, *, client, conversations, turn_get,
binding_repository, clock, attempt_get, workspace_get)` accepts self-contained test ports. Defaults use the
selected portable storage client, owner-scoped conversation repository,
`get_turn(conversation, turn, user_id=owner, include_projection=...)` and
coordination repository. Prerequisite evidence, the delegation source and the
publication result read Turn metadata only; the human source keeps its body read.
A steered source (`steerId`: an owner message in the reply itself) is read in
full only when a grant is admitted; an admitted grant's live gate
(`admitted=True`) reads that reply's metadata, since a steer's author never changes.
`_check_record` skips a stored row's schema walk only for the exact canonical
digests last validated under that definition and row identity, and walks only the
changed top-level members of a row whose definition is a plain closed object
(`_members`); a member whose schema only references a definition is skipped when
that exact content was already proven valid under it in this process (grants of
one request repeat its delegation policy and profile). It caches no row or
authority, and caller-supplied input is always walked. Recovery reads the
execution attempts of every lane it decides in one exchange (`read_attempts`),
and whole-project walks read each index page's documents in one exchange. `scope.validate_profile`
likewise skips its walk only for a profile content digest it already proved valid
(a pure function of the value), returns a fresh copy, and never remembers a failure.
`clock` and budget deadlines are Unix seconds; grant `createdAt` uses that clock.

Authorization replay preserves evidence, never renews a revoked/expired grant.
`validate(admission)` is the live gate: it rechecks owner/tenant/project/revision,
recipient metadata, current coordinator binding and genuine human source
projection revision. Caller API identity cannot promote synthetic input.
Initial execution binding is immutable; a restart-paused or background-completion
continuation, or a Coordinator follow-up Turn (`TrustedDispatch(follows=...)` pins
the association it read), advances only from its exact settled, unreviewed
predecessor through `continue_execution`, preserving one deterministic workspace
task identity (the work id). A follow-up's accepted config names that predecessor
(`FOLLOW_UP_KEY`), the witness `bind_account` checks before continuing the account.
A durable-worker claim names each attempt's physical task; `bind_account` stamps
the work id on it, which keys the checkout and the ledger root.
`revoke(id, expected_revision)` returns
that association (or `None`) for caller-owned existing cancellation.
`record_review` fences the grant revision and records an immutable decision
linked to a positive result revision; it does not assert delivery/completion.


`dependencies.py` owns bounded backward graph/readiness policy over the repository
snapshot and owner-scoped evidence ports. `validate_own` checks own authority;
`validate` additionally fences all direct/transitive result pins. Neither graph
projection nor acceptance creates a worker. File prerequisites require the exact
registered workspace, publication pin and durable delivery receipt.

`mutate(id, callback, check=None)` is trusted internal ledger access only.
Without `check` it reads and commits only its own row (decision "Point updates
commit their own documents, never the project head" in
`docs/PROJECT_COORDINATION.md`). An optional aggregate check reads the cohort
and executes in the same head CAS after the proposed mutation. No dispatch
writer holds a process lock or lane. Callbacks may run
again after CAS conflicts/uncertain commits; they must be deterministic,
idempotent and side-effect-free. They receive a deep copy and may edit it in
place or return a replacement. They cannot alter identity, request, grant,
execution association, review or publication; all other rows remain unchanged. Runtime owns
model-attempt ledger policy, not this repository.

`authorize_batch` commits exact approved child grants and a manifest in this
same CAS, not a new runtime. `batch_requests` owns child derivation and aggregate
allowance checks. `read_batch`/`find_batch` are read-only; service batch retry
uses the same ordinary commands. Physical launches are not atomic. Batch tests:
`tests/project_mod/test_project_dispatch_batches.py` and
`test_project_dispatch_batch_service.py`; the current API/resource boundary
lives in `docs/PROJECT_COORDINATION.md`.

## Runtime seams

`delegation.py` derives worker policy from the current accepted human Coordinator
Turn (or, in a Turn whose input is nonhuman, the first owner message steered into
it that its model received) and pre-role tool assembly, using the same grant CAS
and physical accounting.
Its review pins exact results/checkpoints as Coordinator judgment, never human
acceptance. `project` workers use normal tools in owned isolated workspaces;
legacy consult/file profiles retain their restrictions. Contract and operational
limits live in PROJECT_COORDINATION.md. Verify coordinator worker unit/native tests.
Physical resumes retain the first shared-request delegation. A delegation
returns before its birth ("Delegation returns at once" in
PROJECT_COORDINATION.md): `admit_in_memory` holds the refusals that need no
storage, `origin_attempt` keys it from the task, and `source_for_task(issued=True)`
admits a birth whose issuing attempt completed meanwhile, never one after Stop. `worker_control(session=True)`
admits every Turn of the Coordinator (an automatic continuation included) over the
Workers its binding dispatched in any request: session control, follow_up, review
and cancel, each audited as `coordinator_session_control`. Its request-scoped form
(Router owner delegation of a nonhuman principal) keeps a continuation to its
original request until an owner message reaches the reply (`_steered_owner_source`). Verify the handoff contracts on
all three selected authorities; passive project history grants no control rights.

`acceptance.py` owns acceptance checks: a request's immutable numbered checks,
their Worker-input section, the Coordinator review's per-check verdicts (an
accept judges every check, none unmet; refusals record nothing) and the counts
views derive, where an unjudged check is unknown. Verify
`test_coordinator_acceptance_checks.py`.

`continuation.py` derives result-driven grants and immutable completion receipts
from the original policy, with no spend ceiling or deadline. Stalled turns retry
with backoff before a blocked report; `request_decision` keeps a request open and
applies the recommendation when the human stays silent. Timed wakes are a
process-local reminder over durable due times; startup/settlement recovery is the
authority. No synthetic human, durable scheduler or new task store. Verify
continuation race/Stop/backoff/decision tests and the native Coordinator journey.

`pending_wakes.py` keeps only the obligation to run a recovery pass again when one
could not complete (`source:`, `recipient:`, `dependents:`, `project` entries in the
contract's `wakeNamespace`). It is not a queue: the wake is still derived from the
dispatch record and admitted by its own command identity and CAS. The project's
next dispatch commit in the process that wrote a document the entry's pass reads
(`written_entries`; a head commit concerns every entry), any later recovery pass,
the task-maintenance tick and startup run an entry until a pass completes it; it
adds no poller, count or interval. Each entry is one Watchdog fault episode, resolved on completion.
Verify `test_coordinator_wake_durability.py` and the storage contention specs.

`request_budget.py` registers the human source account once the accepted input is
final (an attempt owing a deferred input translation binds at worker entry in
`lib/tasks_pkg/input_translation.py`) and records occurrences into that same dispatch CAS for
usage projection only; it never refuses work for spend. A model dispatch is
validated live once before its intent is recorded; a tool reservation only
advances the count, flushed off the tool path one flush at a time. Its authority refusal at
dispatch is a typed `ModelAdmissionRefused`, never a builtin exception.
`accounting.py` reduces existing ExecutionBudget events and folds settled receipts
into counters with per-root watermarks so unbounded requests keep bounded records.
Earlier finite grants keep their recorded ceilings. Verify usage, uncertain ACKs,
fold/replay idempotency, restart and startup rollback.

`scope.py` owns the explicit file profile and descriptor fence; root dispatch
pins the first-party handler before plugin/MCP routing. `admission.py` resolves
exact bound physical task and registered workspace, never cwd or sibling tasks.
One admission validates once: `validated_dispatch` hands the validated row to that
same decision only, never to a later admission or request. A tool batch is one
admission: its `DispatchAdmission` reaches its calls' executors and, through
`admitted_call`, their nested gates, which recheck only the in-memory task; a
`run_command` view of the task (`CommandTaskView`) is that task to
`dispatch_root`, so its nested scope shares the call admission. Each
call's tool intent stays a live effect fence (intent CAS revocation check plus
one validation); a Worker's tool reservation validates nothing, and a model
dispatch validates live once, before its intent (`admit_dispatch`), then
rechecks that admission's in-memory fences after it. `bind_account` pins the immutable role
`coordinator_scope` returns, so request building reads no dispatch storage.
Verify `test_project_dispatch_admission_once.py`. In unbounded grants a call declared
read-only or idempotent opens no intent, and a continuation settles its
predecessors' open intents as `interrupted_effect_unknown` (decision "An
unconfirmed effect never wedges a Worker" in PROJECT_COORDINATION.md; verify
`test_project_dispatch_effect_fence.py`).
Within one live decision (`repository.one_decision`: a delegation's source,
a grant admission, an own-authority validation) `decision_read` reads each
external authority record once; separate fences never share one. A
`TrustedDispatch` serves one command: it validates before the Turn exists and
at the association bind, and its principal, birth time and work id reuse that
command's validation (`TrustedDispatch.admitted` hands it the dispatch
decision). Verify `tests/storage/test_coordinator_delegation_reads.py`.
Its `requires_isolated_workspace` resolves the validated file profile before
ordinary direct-mode selection; it grants no task or file execution authority.
`require_isolated_worker_root` refuses delegation for roots no Worker can be
isolated from; tool admission refusals raise `ToolAdmissionRefused` (failed,
never a user Stop), while argument-shape refusals are `ToolContractError` and
reject only that call. `WorkerWorkspaceUnavailable` carries the refusal's typed
`code`, its `details` (the owner's question and candidates for
`ambiguous_project_root`) and a recovery matched to that code; the Coordinator
tool renders both, so a refusal prepared on another host reaches it unchanged.
The root contract lives in PROJECT_COORDINATION.md.
Authority failures stop the task through `stop_on_authority_failure`, naming
`dispatch_grant_revoked` or `dispatch_authority_lost`; an earlier Stop keeps its
cause. Verify `test_project_dispatch_abort_cause.py` and its settlement specs.
An admission that meets an already-stopped task raises `DispatchTaskInterrupted`
(its `abort_reason` is that stop's cause), never an identity verdict; each
identity fact refuses with its own `DispatchIdentityMismatch`. Verify
`test_project_dispatch_restart_admission.py`.
`DispatchContention` is never an authority verdict: every in-task dispatch read
or ledger write goes through `wait_for_dispatch` or `reread_on_contention`, so a
lost head CAS or torn snapshot is read and validated again, never stopped or
returned. Verify `test_project_dispatch_contention.py`.
`scope.granted_tools` is the one admitted-name set of `admit_tool`, the tool
ledger and `_check_record`; never test `profile['tools']` directly in a new gate.
A swarm child's `RunArtifactTools` port is the only `_tool_env` admission
accepts, and only while it inherits none (`test_project_dispatch_executor_admission.py`).
`publication.py` stamps initial origin and guards all existing publication paths;
`service.publish` pins the checkpoint. It remembers per workspace row a refusal
only the assignment can lift (`DispatchGrantRevoked`, `PublicationAwaitsReview`),
so the delivery receiver skips that unchanged row; a new commit path that lifts
such a refusal must call `release_publication_hold` (decision "A hold only its
assignment can lift is decided once per row"). `recovery.py` retries existing versioned
grants at settlement/startup and withdraws authority through real Stop entry points.
Its whole-project read is `repository.discover()`, unwitnessed discovery that
concurrent writers cannot tear; every decision rereads its own snapshot or CAS.
Never base a write on it, and never use `list()` (a witnessed full snapshot) on a
recovery path.
Do not add a second queue, poller, budget account or delivery store. Current limits
and semantics are in `docs/PROJECT_COORDINATION.md` and the machine contract.

## Continuity owners

`scope_changes.py` owns pinned human withdrawal and receipts within the dispatch
record; existing exact-attempt cancellation/passive mailbox ports perform effects.
`responsibilities.py` owns finite conservative occurrence debits and original
human/binding epoch; its adapter uses only the existing automation registry.
`shared_files.py` owns declared metadata versions and exact untrusted-data reads,
not writes or scans. `continuity_contract.py` resolves only registered local
schemas for HTTP. Bounds and focused guards: `docs/PROJECT_COORDINATION.md`.

Grant/responsibility epochs are immutable. Returning to an old coordinator ID
cannot restore permission; legacy unpinned records remain readable evidence,
never upgraded authority.

## Boundedness decision

`contracts/project_dispatch_storage_v2.json` defines the selected owned segmented
storage port, implemented by SQL `_project_dispatch.py` and native
`semantic_project_dispatch.rs`. Queries never initiate migration. Mutations
freeze the legacy record with its existing CAS, copy atomic prefixes, then
activate the complete segmented authority. Old writers fail closed. Retain the
small legacy locator for startup discovery; only the redundant copy is removed.
Snapshot selections must include every row needed by admission and accounting.
`history.py` selects bounded passive history pages through owned range cursors;
its policy-free display selection must never be reused for admission/accounting.
`history.RecipientOrigins` reads catalog Coordinator provenance from the order
index, each immutable admission once per process; the head's assignment count is
its only freshness witness. It is display evidence, never admission input.
`service.HistoryPreviews` lets a page reuse a result preview only for the exact
Turn revision and status it read; the route keeps one page per owner/project.
A build reads its rows' Turns, attempts and delivery workspaces kind by kind in one
exchange each (`service._HistoryEvidence`), and `SegmentedDispatch.get_many` reads a
point selection's documents together; every value is still a live read of that build.
A build whose client disconnected stops before its next exchange or row (`HistoryBuildCancelled`).
Page rows are the contract's `historyRow` request projection; exact
commands/assignments remain point queries returning complete views. A page's
dependency states are display evidence evaluated against that page's `snapshot`;
admission never reuses it. Frontend
history replaces one page at a time; full recovery and allowance cohorts remain
separate consumers.
Head/count witnesses reject torn or incomplete allowance indexes; do not turn
missing evidence into free capacity. Full history and current-request cohort
reads still materialize their selected evidence; see PROJECT_COORDINATION.md.

Problem: the old 128-assignment/4 MiB project record exhausted durable history
and rewrote every grant on each receipt. Decision: individually bounded evidence,
ordered indexes and an atomic head witness without a lifetime assignment limit.
Alternatives rejected: enlarging the monolith, deleting authorization evidence,
independent reservation writes, and another task/worker state machine.

## Verification

`python3 -m pytest -q tests/project_mod/test_project_dispatch_repository.py`
retains deterministic legacy admission/boundary specifications.
`tests/storage/test_project_dispatch_repository_segments.py` exercises the
selected repository on SQLite, PostgreSQL and native authorities, including
cutover faults, racing writers, point access and retained evidence beyond 128.
`test_coordinator_segmented_allowance.py` replays physical conservation contracts
through actual segmented storage. Native journeys verify ordinary Worker birth,
review, restart and report delivery; no paid provider is used.

File-only work consumes complete granted sources and explicit batches without
private byte/count admission. Human call allowances and every live authority
fence remain mandatory. Default descriptor reads are complete; caller-supplied
limits stay explicit. Verify `test_project_dispatch_scope.py` with revocation,
reparenting, freshness and journal/undo failure boundaries.

`test_project_dispatch_lazy_validation.py` uses a fresh interpreter to verify
ordinary tool admission without loading the schema engine, refusal when a real
profile needs an unavailable validator, and valid/invalid first-use validation.
