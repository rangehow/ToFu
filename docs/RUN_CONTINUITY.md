# Run continuity: interruption recovery and long-running execution

Design authority for making orchestration-backed execution survive process restarts without human action and remain stable across long horizons.
Domain behavior owners stay as mapped: [modules/orchestration_dag.md](modules/orchestration_dag.md),
[modules/task_engine.md](modules/task_engine.md).

**Status convention.** Sections marked *running contract* describe behavior
that ships today and name their code owner. Sections marked *target* are
decided direction with a named phase; each phase lands with its own tests and
rewrites its section here from plan into contract in the same change. Git
history retains the superseded plan text.

## Durable worker recovery ordering

**Problem:** API-independent conversation workers can retain an expired owner's
lease while boot continuity inspects its Goal. Treating that still-running
Attempt as a changed binding permanently refused a valid checkpoint continuation.
**Decision:** boot continuity defers while the exact durable source binding awaits
physical-exit settlement. After the current worker claim confirms process death
and commits the interrupted Attempt plus unknown-result receipt, the worker calls
the same continuity pass for that source Goal. The same accepted command ID,
source cursor, native checkpoint preflight, original account and Stop/lane guards
admit its successor. The issued application supplies native recovery policy
explicitly outside HTTP context. No effects are re-entered on the old Attempt.
**Alternative rejected:** changing a lease expiry into permission to replay the
old invocation loses both file-effect evidence and fee authority. Recovery remains
pending when external outcomes cannot be proved; it never creates a fresh budget.
Verify `test_worker_exit_recovery`, the continuity suites and the public native
Goal process-loss qualification, including one unchanged file publication and
exact original-price fees across registered actors.

## Restart-paused conversations resume by themselves

*(running contract; owner `lib/conversations/restart_resume.py`)*

**Problem:** after the 2026-11-08 16:55 restart only dispatched Workers
continued in place. Both Coordinators got a new "Continue the original project
request" wake that re-read every Worker, one chat continued only because a
background-command wake happened to exist, two swarm-master chats stayed
interrupted (their swarms stood down, waiting for the parent's Continue) and the
Global Router, whose turn had ended, was never woken. The settlement hook
promised that startup recovery resumes restart-paused work, yet only project
dispatch did; the browser's batch-resume hint needs a human click, lists only
Turns boot recovery settled (a graceful restart settles its Turns before boot),
and lives in the execution host's in-process store, which the API host serving
the browser never reads in the managed split.

**Decision:** the server continues every Turn a restart interrupted, from its
checkpoint, exactly as the owner's Continue would, once.

- *Candidates.* The shutdown quiesce of each stopping process records the Turns
  it aborts (namespace `conversation.restart_paused.v1`, one record per
  stopping process, written before they settle while storage still serves;
  each entry names the interrupted Turn and Attempt). Boot recovery hands the
  orphans of a killed server to the same pass, and an open restart notice's
  `interrupted` list is the third source. An Attempt that durable worker
  recovery settles in a serving process (its process died, or a storage
  restart fenced its claim while the process lived) is in none of them: its
  settlement drain resumes it by the same rule through
  `resume_recovered_turn`, after queued input, with this generation's
  stand-down ([Turn settlement](contracts/TURN_SETTLEMENT.md)). A recorded candidate is validated
  against exactly the Turn its record names, never the lane tail: a
  Coordinator appends report Turns after its long Turn. A conversation named
  without a Turn stands for its newest main-lane Turn that did work (appended
  records, queued pairs and inputs are passed over). Records are consumed by
  the pass. Only the immediately previous generation's records and still open
  notices count, so an interruption the owner left alone days ago never
  restarts by itself.
- *Resume.* After readiness, once the owners that resume their own work have
  run (project dispatch recovery, background batch wakes, swarm rehydration),
  `resume_restart_paused_turns` continues each such Turn still `interrupted`
  with cause `server_restart` that no later input superseded, and whose current
  Attempt is still the interrupted one, through the ordinary `create_attempt` command:
  the first of `continue`/`checkpoint_resume` its settlement offers, with that
  option's anchor, the Turn's own accepted execution identity and public
  config, and command id `restart-resume:<interrupted Attempt>`, so a repeated
  pass, process or restart resumes each interruption once. A swarm master's
  Continue carries the receipt its detached swarm needs to re-attach.
- *A Turn no later input superseded resumes in place.* A resume completes the
  Turn under a new Attempt and preserves every later Turn of its lane on both
  authorities (only regenerate discards later Turns, on the owner's
  acknowledgement); the resumed model reads the lane prefix through the Turn's
  ordinal. `restart_resume.lane_tail_verdict` is the one rule, shared with
  project dispatch recovery. When a later input Turn exists, it superseded the
  interrupted Turn: the result is outcome `superseded` with `supersededBy`,
  reported as "superseded by queued input <id>" when that input was queued
  before the interruption and "newer input" otherwise. Later appended records
  alone (the reports a long Turn posted) do not stop the resume, and they stay
  on the lane. Nothing is written to a Turn that is not resumed. A failed notice
  review is resumed by the same rule.
- *Left alone, with the reason in the restart notice:* dispatched Workers and
  Coordinator wakes (project dispatch recovery continues them), Goal
  (`autopilot`) and Flow (a selected flow) runs (D1/D2 above), a Turn waiting for
  the owner's answer (`answer_guidance`), a Turn whose settlement names calls
  the restart cut off with an unknown side effect (`error.unknownEffects`,
  [Turn settlement](contracts/TURN_SETTLEMENT.md): the owner checks them first;
  only calls of effect class `unknown` are named, see below),
  a Turn that offers only Regenerate (the application's settlement of a
  restart interruption offers Continue or a checkpoint unless the restart
  cut a call in its first round, which stays held for its owner, or its
  usage refusal forbids every resume; native legacy boot recovery keeps its
  own rule: [Resume options](contracts/TURN_SETTLEMENT.md#resume-options)), cause
  `user_abort` (the owner stopped it),
  a Turn a later input superseded and a Turn that ended before the stop. After a manual shutdown or in a
  restart storm (`lib.shutdown_marker` classification) nothing resumes by
  itself. The restart notice describes the selected Turn, never a report after
  it, and says "not interrupted by this restart" for a listed conversation
  whose latest work ended before it.
- *A restart-cut call is classified by its declared effect (owner follow-up
  2026-11-15).* Problem: every restart cut off a few Workers' `run_command`
  calls (a test run, a `git ls-remote`); each such call counted as an unknown
  side effect, so the Worker settled `worker_effect_outcome_unknown` with a
  manual retry policy and waited for its Coordinator's `checkpoint_resume`.
  Decision: durable worker recovery
  (`worker_recovery.classify_cut_calls`) classifies every call still
  `entered`/`unknown` with no result through `lib/tools/restart_effect_class.py`:
  `read_only` and `idempotent` by the tool's own declaration; for
  `run_command`, `read_only` when every segment provably only reads (the Git
  guard's read-only verdict, the read-only utility set), and `verifiable` when
  the call ran in the Worker's own isolated worktree and is a test/check run, a
  local Git write of that checkout (add, commit, merge, reset, ...), or a
  literal file utility whose every write target is inside it: no network side
  effect, and the resumed Worker re-checks that state itself. Everything else is
  `unknown`: an unparsed segment, a computed program or operand, an arbitrary
  interpreter, a package install, any remote or shared-ref Git command (push,
  fetch, branch, tag, worktree), and every write of a task without an isolated
  worktree. Read-only, idempotent and verifiable calls settle the interruption
  like the graceful quiesce and it resumes from its checkpoint by itself (project
  dispatch recovery for a Worker, this pass for a Turn no grant runs); only
  `unknown` calls are named in `error.unknownEffects`, which the Worker's
  Coordinator receives once as a `needs_decision` settlement through `poll`
  ([Project coordination](PROJECT_COORDINATION.md), "Settlement is news, reported
  once"). The decision for every cut call (`toolName`, `toolCallId`,
  `operationId`, `effectClass`, `decision`: `resume` or `needs_decision`) is
  recorded with the exit receipt (task result metadata `restartCutCalls`), in
  `error.cutCalls` when one needs a decision, and logged by
  `[WorkerRecovery]`. The classifier is pure and conservative: what it cannot
  prove is `unknown`, so it adds no knob, timer or retry. A call cut in the
  Turn's first round keeps the existing hold (only Regenerate) whatever its
  class. Alternatives rejected: re-running every cut `run_command`
  (a push or deploy would run twice); a per-tool allow-list setting (a tuning
  knob, principle 5); probing the target after boot to prove the effect (a new
  recovery-time process and network path; the Worker already re-checks its
  own state when it resumes). Specifications:
  `tests/tools/test_restart_effect_class.py` and the effect-class cases of
  `tests/conversation/test_worker_exit_recovery.py`.
- *Coordinators in place.* A restart-paused Coordinator wake continues in its
  own Turn through the restart continuation of project dispatch
  (`project-dispatch-resume:`), like a Worker. A restart-paused human request
  Turn without a wake continues in place too, unless a later input superseded
  it: Coordinator recovery
  (`project_dispatch/continuation.recover_coordinator`, at boot and every
  settlement) calls this module's `resume_conversation` with the request Turn and
  Attempt, so it needs none of the candidate records above (the restart that
  deploys this runs the old code, which writes none) and the same command id
  makes it idempotent with this pass. A request Turn continued in place, by either or by the owner's
  Continue, owns follow-through until its current Attempt settles; no wake
  races it.
- *Unreviewed notices resume too.* The same ordinary command recovers a Global
  Router notice whose review exhausted the terminal-response budget
  (empty_stop) and settled failed with no answer, which used to be stranded as
  an unread failed card. `failed_notice_resume_option` admits only that class
  (durable nonhuman parent, no current-Turn owner steer, no answer or silence
  note, an existing resume option) and `resume_failed_notice_turn` issues it
  with command id `notice-review-resume:<failed Attempt>`, so a replayed pass
  resumes once and completed tool results are replayed, never re-executed.
  The Router's terminal observer is the only caller, as a bounded follow-up
  rather than a model call inside settlement; the notice is never marked read
  or reviewed, and owner-authored Turns and genuine provider errors are left
  alone (docs/ROUTER.md R3).
- *Failures.* A transient storage failure keeps its record and repeats the
  pass through the boot step's existing retry; every other refusal is final
  and reported. The restart's requester hears each conversation's result in
  the restart-complete notice ([ROUTER.md R9](ROUTER.md#r9-the-owners-delegation-the-global-router-acts-for-the-owner-owner-decision-2026-11-06)),
  which a new generation sends only after this pass.

Cost: one resume Attempt per interrupted Turn from its checkpoint, with no new
brief, no re-read results and no poller; one small record per stopping process.

**Problem (2026-11-10 restart):** both Coordinators' long Turns (1a390889
02bde90f, edae46bf 060bc407) were restart-paused with continue and
checkpoint_resume intact. The pass and the notice read only the lane tail,
which was a Coordinator report, so the notice said "completed (ingested)".
The owner's inputs that had queued behind those Turns took over at 04:37:22.
Resuming the long Turns would have deleted those inputs and every report.
**Decision:** select the Turn from the interruption record, and report a
Turn a later input superseded as superseded instead of resuming it.
**Problem (2026-11-11 18:28):** the first version of this rule also left
alone every Turn that was not its lane's tail, because `attempt.create` then
deleted later Turns. The parallel Router's request Turn 8c1d64fe, answering
owner-forwarded input ca86e183, had posted a progress report mid-turn, so it
was never continued. The next Turn on its lane was another request's wake,
scoped to that request's Workers. Since resume operations preserve later Turns
(e986bbaf8e), appended records alone no longer stop a resume: the Turn
continues in place with its own authority, and its reports stay.
**Alternatives rejected:**
- Resuming the recorded Turn when a later input superseded it: the newer input
  owns the lane.
- Continuing a reported-on Turn in a new wake at the lane tail: a synthetic
  continuation is scoped to its own request's Workers, so the owner's input
  loses its authority, and the interrupted round is lost.
- Marking the Turn superseded durably: a second writer, while the verdict is
  re-derived from durable lane state on every pass.

**Alternatives rejected:** running the browser's batch resume server-side
unchanged (its hint is incomplete and invisible to the API host); resuming
every interrupted Turn regardless of age (an abandoned interruption would
restart days later); scanning storage for interrupted Turns at boot (no index,
so every boot would pay for history); a boot hook per conversation kind
(Router, Watchdog, chats) instead of the common turn command (principle 4); a
new Coordinator wake after a restart (a fresh brief re-reads every Worker and
the interrupted round is lost); a synthetic Continue for a conversation whose
turn had already ended (it was not interrupted; the restart notice wakes it).

Verification: `tests/conversation/test_restart_resume.py` (SQLite and native
authorities), `tests/project_mod/test_coordinator_continuation.py`
`::test_a_restart_paused_*`, `tests/router/test_restart_notice.py`, and the
real two-process `tests/server/test_restart_resume_application.py` (a resident
storage service and a real headless execution host booting as the next
generation in a private project).

### A call the restart stopped before it ran is repeatable

*(running contract; owners `lib/project_dispatch/admission.py`,
`lib/tasks_pkg/executor/_effects.py`)*

**Problem (2026-11-13 04:28):** the restart's quiesce stamped `server_shutdown`
on 15 running tasks. Four Worker calls (`edit_file`, `run_command`) were inside
dispatch tool-intent admission. Admission counted the stop as lost identity
("Project dispatch task identity is no longer authorized"), the collector typed
it `tool_execution_cancelled` with "do not resume this operation
automatically", and that verdict was stored in the old attempt's durable tool
round. Recovery resumed all four correctly, yet each resumed model read the
verdict and stopped, although no call had started (no command start, no
`apply_diff`). The first repair (`ba37ee75e7`) named the restart and the
unchanged grant, but still stored a call that never started as a non-retryable
cancellation asking the model to verify an effect that could not exist.

**Decision:** a restart's stop is a pause, and the call's own evidence decides
what the resumed model reads ([Turn settlement](contracts/TURN_SETTLEMENT.md)).
Admission reports any stop with the typed `DispatchTaskInterrupted` carrying
its `abort_reason`, checked before the grant is restored and again after it
(the quiesce can land while the restore reads storage); each identity fact
refuses with its own `DispatchIdentityMismatch`. The executor stores a
restart-cut call as `tool_execution_interrupted`, retryable, telling the model
to repeat it when the work continues, only when non-entry is proven by the
effect fence, never inferred from timing: the handler runs only after
`begin_tool_effect` records the effect `entered`, and the pre-entry abort
rewrites it `not_started`, so no record or `not_started` proves the handler
never ran. The dispatch ledger then settles the occurrence `not_entered`, so
the repeat is admitted without a fence and runs once. An entered, unknown or
returned effect is never repeatable, whatever its declared recovery, and
neither is a call whose dispatch intent may still be open: a lost `not_entered`
settlement marks the task's actuals incomplete
(`_executionActualsComplete=False`), and a continuation settles that intent
`interrupted_effect_unknown` and names it. Only an intent the dispatch
observer went on to write is settled (`admission.observe_tool`): a refusal by
the live validation before that write has nothing to settle and touches no
storage. Settling it anyway let a restart that also stops storage mark the
account incomplete for a call that never started, so it was stored as a
non-retryable cancellation and the resumed Worker lost the work (drill
`admission-cut`, window A, on `1e678963a4`). Without the proof the call keeps
the restart-worded cancellation (check whether it took effect), an entered
non-repeatable effect stays `tool_effect_unknown`, and a person's Stop or an
authority stop keeps `tool_execution_cancelled` with "Respect the
cancellation". The task's first stop cause (`_abort_reason`, written once by
the quiesce, the owner's Stop or an authority stop) decides, never exception
text. One ordering remains: a call aborted after its intent and before
handler entry gets its result before the ledger's `not_entered` write; a
failed write is retained and rewritten by the next call or the root's
settlement (`ExecutionBudget.finish_tool_attempt`), and the handler provably
never ran, so no effect can run twice.

**Alternatives rejected:** re-running stopped calls automatically on resume
(Tofu never replays a call; the resumed model decides from the typed result,
and the ledger still fences effects); typing every restart-cut call repeatable
(an entered write may have taken effect); inferring the cause from refusal
prose; keeping the restart a cancellation with softer wording (a resumed model
reads a non-retryable cancellation as "stop", which is the observed failure);
repeating an entered idempotent call (its effect is unknown, not absent).

Verification: `tests/project_mod/test_project_dispatch_restart_admission.py`
(the real shutdown quiesce lands inside a Worker call's admission at either
live validation; the continuation runs the call exactly once; an owner Stop in
the same window stays a cancellation; a lost `not_entered` settlement leaves
the call non-repeatable and the continuation names the open intent; a
restart that also stops storage during the first live read leaves the call
repeatable and it runs once; each identity fact's typed refusal),
`tests/project_mod/test_project_dispatch_restart_interruption.py` and
`tests/tasks_pkg/test_tool_exception_recovery.py`.

**Problem (2026-11-13 12:31, after the R3 restart):** two resumed attempts read
the restart's cut as an order to stop and ended with zero tool calls. An owner
chat's swarm `poll` returned "Wait cancelled: the task was stopped." and its
model answered "I stopped as you asked". Worker `d8e7021b`'s `run_command`
read "Project dispatch task identity is no longer authorized"; that text came
from the stopping process's pre-R3 code (`01f24726f205`), which folded
`task['aborted']` into the identity check, a case the Decision above already
closes. RESTART-DRILL-1 also found window R: the quiesce cut a call's tool
reservation write, before its intent. The budget marked the account's
actuals incomplete, so the call that never started was stored as a
non-retryable cancellation and its work was lost.

**Decision:** every text a restart's cut produces names the restart and says
to continue (`turn_verdict.RESTART_CONTINUE_GUIDANCE`): `stopped_tool_result`
for calls cut before they ran, `turn_verdict.aborted_wait_text` for a `poll`
wait (command, swarm and task handles) and the Coordinator's Worker wait (a
person's Stop keeps each surface's own words), and the projection of a restart-cut cancellation,
which never repeats the cause's own text ("Agent stopped while waiting for
persistence"). A cancellation raised by the tool reservation under a restart
is stored `tool_execution_interrupted`, never started and repeatable
(`executor._execute_admitted_tool_one`): the reservation only advances a usage
counter and precedes the call's intent and every effect, so the account
incompleteness its lost write records never makes this call's non-entry
unprovable. **Alternatives rejected:** clearing the account's incompleteness
for a lost reservation write (its count is unknown; usage accounting keeps
that fact); a new Coordinator wait outcome value (the instruction carries the
cause without changing the result shape); rewording only the poll (every
surface that reads a stop's cause shares one text). Residual: a call cut in
its intent window after another call's lost reservation write in the same
batch still reads the account as incomplete and keeps the restart-worded
cancellation. Verification:
`test_project_dispatch_restart_admission.py` (window R and its owner-Stop
twin; the `d8e7021b` pin, which fails with the pre-R3 `_validated_binding`),
`test_poll_kill.py` (a restart-ended command and swarm wait),
`tests/scheduler/test_poll_task_results.py` (a task-handle wait),
`test_coordinator_workers.py` (the Worker wait), `test_system_abort_cause.py`
and `tests/conversation/test_worker_attempt_real_start.py` (real quiesce, boot
recovery, and a resumed Worker that makes tool calls and completes).

## Problem

Goals run for hours to days. Committed-effect recovery now spans
execution and conversation authority, but restart continuity still has gaps:

1. **Effect recovery needs a complete admission chain.** Retirement, normal
   conversation admission and ready-journal acknowledgement now survive
   preparation crashes. Unproven accepted config, changed human decisions,
   or a new effect without a complete successor seed remain refusal boundaries.
2. **The likeliest interruption point refuses recovery.** A run interrupted
   while a role invocation was in flight leaves an unmatched agent admission
   in the execution journal. A matching registered child completion can close
   the handoff; without it `read_journal` refuses unknown external effects. Long runs spend most of their wall time
   inside model/tool invocations, so most real crashes land exactly here.
3. **Goal continuity needs complete evidence.** Clean v2 launches and settled
   receipts can rebind to a normal successor Goal. Older launches, an incomplete
   inherited seed, or a changed human/Turn decision remain terminal/refused.

A secondary continuity gap remains for in-flight invocation-tree reservations.
Completed registered Goal leaves now carry canonical cumulative consumption;
unresolved reservations still cannot become a complete recovered balance.

### Registered native actor checkpoint capture

Problem: Flow children use transient runtime carriers, so native checkpoint
composition discarded their paid model/tool frontiers even when conversation
birth records were durable. Decision: explicit `checkpoint_actor_account`
composition admits `NativeCheckpointPolicy(registered_child=True)` only for the
unchanged owned child birth and its existing shared `ExecutionBudget`. Each
callback checks that registration remains live and has no completion receipt.
Headless children remain storage-free. The shared snapshot's `actor_task_id`
identifies local consumption; `root_task_id` remains the real shared account.
Actor model/tool ordinals retain their last assigned values when siblings advance.
Local original-price fee rows carry no root wallet or descendant invoice capsule.
Whole `write_file` batches persist invocation-scoped operation references before
handler entry; pending-to-complete validation freezes the paid prefix.

Alternative rejected: giving each child a fresh root account duplicates spending
authority; using global counters as local usage duplicates parallel charges.
Actor restore and file reconciliation require the explicitly composed aggregate
Goal claim/whole-set handoff described in EXECUTION_ACCOUNT_CONTINUITY. Personal
launches retain cost evidence without wallet authority; defaults remain opt-in. Verify
`test_native_flow_actor_checkpoint.py`, `test_native_actor_checkpoint_policy.py`
and native journal/handoff tests. Two ordinal maps follow active participants
and delete child entries at close. At 1/32/256 actors their table storage was
368/1,664/13,152 bytes, excluding referenced objects; one local snapshot was 370
JSON bytes. Over 3,000 admissions, median reserve/dispatch/settle was 17.023 µs
before and 17.078 µs after. The first capture measurement spent about 5.5 ms
recompiling the same schema. One lazy cached generated-schema validator now
serves root and actor capture/restore; validation remains strict. Warm capture
medians became 103.5/102.8/106.4 µs over 100 samples at each size. Admission
remeasurement was 17.899/17.151 µs before/after (3,000 calls); this noise does not
establish a throughput gain. These local microbenchmarks are not task-speed
claims. Each selected-store policy call adds birth and conversation reads, with
the existing checkpoint journal retaining storage ownership and lifecycle. Native Goal recovery defaults its finite I/O deadline from the selected storage client’s read-only `request_timeout_seconds`; byte bounds still come from the issued transport, and explicit recovery I/O overrides remain. Problem: reusing terminal ACK grace made a valid nullable production ACK erase the recovery deadline. Decision: resolve the same selected storage once for both preflight/execution and derive milliseconds with positive finite/u64 admission. Alternatives: another timeout constant drifts from storage policy; deriving it from terminal or shutdown grace conflates independent lifecycles. No additional RPC, retry, store selection or worker is introduced. Verification covers nullable production profiles, selected storage policy, actor recovery and original-fee settlement.

Problem: boot continuity appended its deferred/resumed audit after an asynchronous Goal successor had already read the source cursor, creating a false native claim conflict. Decision: the common continuity owner exposes an explicit source-cursor policy; Chat Goal calls its synchronous `before_submit` hook only after preflight and before the normal conversation command. The audit is acknowledged before submission, and claim/consumption own subsequent source writes. An unknown submission result keeps election pending and logs its reason without altering the issued source; a refusal before submission retains the existing durable refusal event. An already-claimed successor also receives no post-return audit. Alternatives rejected: relaxing native cursor checks admits unverified effects; replaying a conflicted claim repeats authority; sleeping after submission leaves a scheduling race. No worker, cache or new durable store is added. Verify `test_continuity_source_cursor` across all selected authorities and full public SQLite/PostgreSQL/native Goal restart tests; `system_native_sql_checkpoint_application_20261016.json` retains the real cursor-11 versus cursor-12 reproduction and fixed lifecycle.

## Decision

### Read-only native actor frontier preflight

Problem: aggregate Goal recovery needs every actor's original paid state before
it can admit a successor account; acquiring actors one at a time changes leases
before the complete set is known. Decision: the native checkpoint control
`frontier` operation reads one owned completed or paid-pending snapshot after
trusted process-death proof. Its closed request declares header/snapshot/identity
byte bounds; response capacity is admitted before the read. The shared journal
compares header versions before and after the exact-version snapshot read.
Missing state, live/unknown writers, inflight requests, changed versions and
oversized content cannot produce a usable observation.

`invocation_frontier.py` binds this observation to the unchanged registered
direct child birth and original account root, and rechecks registration after
reading. Completed children use their immutable completion receipts. No lease,
capability, account restoration, wallet settlement or replay permission is
created. A later aggregate admission must revalidate versions and accepted Goal
lineage; this port alone does not resume an interrupted child. Alternative
rejected: acquiring individual leases during discovery partially mutates a set
that may ultimately be refused. Verification covers actual SIGKILL, original
prices, live-writer fencing, foreign ownership, read races and capability counts
in `test_native_checkpoint_frontier.py`, `test_invocation_frontier.py` and native
checkpoint journal/control tests. Storage remains with the selected journal;
each successful observation performs three bounded header reads and one bounded
snapshot read, without writes, additional workers or retained per-actor state.
On a private debug engine after SIGKILL, 100 warm C ABI reads of 2,021/34,789/
264,165-byte snapshots had median 0.82/2.65/15.48 ms and p95 0.85/2.73/15.99 ms.
These include response decoding, not a production task or aggregate recovery.

### D1 — Continuity policy is launch-stamped run metadata *(running contract, phase 1)*

A durable run's continuity policy is fixed at creation by its launching
domain and persisted with the run row. Phase 1 carries the policy on the
launch-stamped `created_by` provenance field: the composition root passes the
continuity pass one explicit registry of `auto` domains —
`chat_goal_mode` (`lib/goal_runs/contract.py` is its single source of truth)
— and every other run is `manual`. The recovery path never infers policy
from run names, orchestration ids, definition content, or event names. Owner
identity for any successor comes from the durable run row, exactly as the
explicit resume path reads it today — never from a process-global or
hardcoded user. A dedicated per-run policy column is the enterprise seam if a
per-run override is ever needed; the registry deliberately cannot express
one today.

### D2 — Boot continuity pass performs the existing resume protocol *(running contract, phase 1)*

Startup's retire maintenance operation (`orchestration.run.retire_interrupted_all`)
settles lost workers before clients reconnect. In that same transaction it marks
restart-interrupted Chat Goals as `continuity_pending` in private run state and
indexes them. SQL uses the v66 expansion and a partial index; native storage uses
a default-false field and the contract's pending index. The bounded result contains
fresh retirements plus up to 1,000 durably pending descriptors. The process-local
post-ready slot is delivery, while the stored election is authority: a crash
before command admission or a capacity deferral is re-elected on next startup.

Problem: retirement was durable while its only candidate list was process-local.
Decision: keep the pending bit with the run, and close it atomically with a source
recovery claim, `continuity_resumed`, `continuity_refused`, or
`continuity_exhausted`. `continuity_deferred` preserves election. Retryable typed
storage failures, including wrapped repository failures, defer rather than close.
Alternatives: rewriting old terminal runs would change their meaning; scanning
all historical terminal runs on each boot would charge startup for history;
independent queue records would need another ownership and deletion lifecycle.
The pending bit and bounded index avoid those costs and survive storage WAL replay.
A source audit or deletion releases only its own Goal active claim; it cannot
release the successor's claim on the same conversation.

Pending growth is at most one index entry per elected source, reclaimed by the
closing transaction or source deletion. No worker, timer, or queue process is
added. Five isolated sweeps of 100 synthetic pending Goals measured median
1.55 ms (SQLite) and 9.47 ms (native), returning 8,401 JSON bytes. Peak traced
Python allocation was 178,232 / 175,839 bytes respectively; no models ran, and
these are neither process RSS nor end-to-end executor latency. The SQLite query
plan proves use of the partial pending index. Offline migration carries the bit
and binds optional source-column bytes; absent older columns mean false, never
an invented election. Imported closure receipts reclaim the native pending index.
Existing historical terminal runs are not silently reopened by schema expansion.

For each candidate whose stamped domain is `auto`, the pass performs exactly
the explicit-resume protocol (`load_recovery_records` → deterministic
`recovery_run_id` successor → `record_recovery_claim` on the source → spawn
with `recovery_records`), binding the successor to the owner recorded on the
run row. Journal validation is unchanged: a clean seed resumes, a refused
seed leaves the source settled. Every outcome lands as a typed durable audit
event on the source run's stream: `continuity_resumed` (with the successor
id), `continuity_refused` (with reason), `continuity_deferred` (retryable
recovery failure or awaiting successor journal). Historical `continuity_exhausted`
records remain readable and preserve their recorded closure. These are durable
replay-visible facts, not log lines; a generic Studio timeline projection is
deliberately not added by phase 1.

Demand and guards: candidates are processed sequentially and every eligible
candidate is handed to the shared task runtime. No per-boot resource capacity
limits recovery; legacy `TOFU_CONTINUITY_AUTO_RESUME_MAX` values are ignored.
Worker/inflight/RSS settings grant no boot-recovery refusal. A candidate already carrying a recovery claim is skipped, so repeated passes
never spawn twice. Restart count, lineage length and elapsed restart timing
grant no new continuation refusal. Recovery still validates the complete source
journal, owner, definition/input identity, budget state and unknown effects.
Operator kill switch: `TOFU_CONTINUITY_AUTO_RESUME=off`. The pass is fail-soft per
candidate and overall: it runs after readiness and must never wedge the
process. No new resident daemon, timer, or queue is added.

Completeness verification: forty storage-backed candidates are handed off in
one pass despite one-worker/one-inflight and one-resume legacy settings.
Each source records its durable successor receipt, no `boot_capacity` deferral
is produced, and a repeated pass skips all forty without another spawn.

### D3 — Chat Goals rebind through normal conversation commands *(running contract)*

Problem: Studio recovery cannot bind a chat Goal's conversation, attempt and lifecycle. Decision: boot and authenticated task-resume dispatch `chat_goal_mode` to `resume_chat_goal` after owned-source lookup; other domains retain generic recovery.
Refused/deferred admission returns 409. The server rechecks Stop, lane and effect guards; action visibility grants no authority.
Every live/settled phase carries `orchestration.goalRunId`, distinct from the Turn attempt `runId`. Restart evidence stays on its interrupted phase; only a current tail offers “Check and resume”.
One in-flight UI request preserves focus and delivery uncertainty. Acceptance is not completion; detached observers ignore replies without cancelling commands. No timer or pool is added.
Alternative rejected: inferring IDs from attempts or verifier markers misses first-worker interruptions and can select the wrong run.
The production boot pass resolves its owner-bound run-service factory. Native first-phase admission updates the existing recovery byte witness in the same transaction; crash sweeps keep their strict projection-size check.

A source must have the exact restart interruption, a valid v2 launch archive,
and fully settled agent receipts. The source attempt/Turn/task binding remains
unchanged; a later Goal is allowed only when its accepted recovery-command
chain proves an unfinished pre-effect handoff from that source. A
bounded conversation snapshot selects the source lane's current tail. The
ordinary conversation command then atomically checks that exact parent is
still the lane tail, that its settlement permits continuation, that the lane
is idle and that no newer human input is queued. SQL and native creation both
honor `require_parent_not_stopped`; only `interrupted/server_restart` joins
completed/failed parents as eligible. Human Stop and live parents stay rejected.
The cancellation watermark follows the source's accepted command, so Stop
before recovery preparation also wins; the new executor's execution clock
and resources remain fresh. Public commands cannot inject the recovery source
or its cancellation watermark.

The source retains its honest `failed/worker_lost` terminal meaning. A stable
source-derived command id elects one normal continuation input/output pair
and one bound executor. Its new GoalRun retains the full objective. Before
model admission the executor revalidates its accepted attempt identity, restores
the archived launch/messages/checklist/child policy and claims a new journal
with inherited committed receipts and a parent link. Replay restores each
committed worker working set without repeating model/tool work. Fresh native
route groups are minted through normal task/child creation, never recovered
from stale handles. Source claims are idempotent and stay with source history.

Problem: acknowledging the source when a task was merely bound closed its
pending election before the successor had a recoverable journal. Decision:
`create_goal_execution_journal` commits the complete header, inherited seed and
ready marker, then the source acknowledgement, before any model/tool admission.
Boot admission without that acknowledgement writes `continuity_deferred` with
`awaiting_successor_journal`; its `resumed` summary counts command admission,
not durable readiness. A live accepted executor is reused or deferred, never
spawned again. A crash after ready but before acknowledgement reconciles the
validated child archive and leaves that child's own election responsible for replay.

Before ready, recovery walks at most 16 ordinary continuation pairs to the
original source. Every abandoned attempt must be `interrupted/server_restart`;
its owner-proved private accepted config must carry the exact server-authored
source/objective/tenant, and its command ID must bind the durable predecessor.
Missing Goals, partial header chunks and interrupted inherited-receipt copies
are pre-effect preparation. A ready marker or new effect admission prevents
rewinding the original source. A fresh normal command can replace only a proven
pre-effect handoff, retaining the original Stop watermark and frozen seed.
Alternatives: trusting public IDs/virtual actors permits forged recovery intent;
reusing an interrupted attempt reopens terminal state; acknowledging task birth
alone loses election on a second crash. The private config getter is opt-in,
owner-filtered and absent from public HTTP reads; SQL and native share its contract.
Abandoned turn pairs and partial journals follow existing conversation/run deletion;
no separate queue, daemon or lifecycle is introduced.
Five isolated recoveries after one pre-Goal handoff crash, each with a 1 MiB
archived message, measured median 93.28 ms on SQLite and 606.90 ms on native
for chain proof, fresh command binding, journal publication and acknowledgement.
Peak traced Python allocation was 20,732,402 / 20,746,943 bytes respectively;
this includes tracing overhead, no model execution and no process-RSS claim.

Alternatives considered: reopening the old terminal Goal changes an already
observed outcome; a standalone Studio worker loses conversation authority;
starting from the visible transcript loses compacted tool context; replaying
an unresolved agent can repeat external effects. Fresh normal commands preserve
one lifecycle and its existing CAS, cancellation, deletion and rollback seams.
No daemon, polling timer or extra execution pool is added. Recovery eligibility
and ordinary journal/budget checks remain D2's authority; chunked launch parent
links are decoded across page boundaries during full lineage validation.

Capacity measurement: five synthetic isolated SQLite recoveries with a 1 MiB
launch message and one 1 MiB completed working-set receipt measured median
145.01 ms for source validation, normal command binding, successor Goal start
and inherited journal publication. The successor used 2,801,552 wire-event
bytes, including the copied launch and receipt. Peak traced Python allocation
across the five samples was 50,959,279 bytes; this is not process RSS. No model
ran in this measurement. Recovery copies committed receipts once per successor;
durable growth is proportional to those receipt sizes plus one normal turn pair,
under the run/conversation's existing deletion lifecycle.

Limits: unresolved model/tool invocations still refuse under D5. An interrupted
inherited-seed copy is replaced only through the proven pre-effect handoff above,
using the original complete seed. Human input, Stop and unproved lane changes
remain admission fences. Preparation chains beyond the structural proof bound refuse.
Branching conversations retain the existing snapshot topology fallback.
Automatic continuity is not a proof of research quality or benchmark dominance.
Tests: `tests/goals/test_goal_handoff_recovery.py` covers pre-bind, pre-Goal,
partial header, inherited copy, pre-ready and ready-before-ack crashes, repeated
preparation, private config ownership and public forgery.
`tests/goals/test_goal_conversation_recovery.py` uses real SQLite/native
stores and a source-matched native engine; PostgreSQL shares the same fixture
and runs when its isolated test binaries are available.

### D3a — Chat Goal launch and effect archives *(running contract)*

Problem: chat Goals previously persisted lifecycle events without execution
journals. A worker result alone cannot restore the model's tool history,
compacted working set or checklist. Decision: `lib/goal_runs/execution_journal.py`
claims a launch on the existing owner-bound orchestration event stream before
model dispatch. The shared journal header carries the immutable launch plus
its digest: original objective, model messages, resolved launch policy/tool
declarations, initial checklist and conversation/Turn/attempt/owner bindings.
Launch format v2 also freezes the ordinary child's inherited model configuration,
owner-authorized model reference/provider preference, execution-tool discovery
policy, contract documents and script permissions. The public projections in
`lib/tasks_pkg/child_invocation.py` own both normal launch and archival; after
the durable claim succeeds, every Goal leaf consumes the frozen policy instead
of the mutable parent config. A conflicting model/project/thinking selection
refuses before child allocation. Credentials, tool environment, principal
handles and native slot groups are not copied: recovery must rebind resources
through their existing owners. Version 1 archives lack this input policy and
cannot reconstruct a Goal; the generic resume guard still recognizes them.
`goal_execution_launch_contract()` in `lib/goal_runs/contract.py` owns its
machine-readable schema. Competing starters use distinct launch witnesses at
the same sequence, so only one may admit effects; terminal/superseded runs
reject subsequent effect admissions through ordinary atomic event projection.

The cross-process lifecycle vocabulary lives in `run_status.py`; the event
registry and storage election consume that same dependency-light contract.
Production leaves now publish `execution_effect_prepared` before child setup.
The child invocation owner awaits `execution_effect_started` only after setup
and execution-capacity waiting, immediately before entering its model/tool
executor. A restart with preparation alone can reconstruct the leaf from the
sealed launch; a started effect without completion remains ambiguous. Admission
publication failure releases child ingress, budget, registry and monitor through
the ordinary teardown and dispatches nothing. Preparation cannot complete a
receipt, change its input hash or admit twice. Injected/custom runners retain
admission before their opaque call, because their setup boundary is unproven.
This resolves setup crashes without treating model/tool timeouts as replayable;
inferring dispatch from a tool-free display preview was rejected. Each new leaf
adds one small durable event on the existing deletion lifecycle; 5 synthetic
1,000-leaf runs measured 175–177 UTF-8 payload bytes per preparation (before
storage framing). No resident worker, queue, cache or timer is added.

Conversation-policy agent receipts include a detached model working set and
checklist snapshot with the completed result in the same durable event.
The existing runner restores this state when replaying a committed result,
without another model/tool call; normal worker/verifier isolation remains.
Missing context, an invalid launch digest, changed ownership/binding, or an
unresolved model invocation refuses replay. Handoff-policy journals cannot
consume conversation-policy receipts. The ordinary Studio resume path refuses
archived chat Goals: D3 owns their conversation/lifecycle rebind, and a
transient standalone worker is not a valid substitute.

Alternatives considered: removing the journal's worker-context guard without
storing context loses tool evidence; output-only receipts lose compaction and
checklist state; storing all parent config leaks runtime handles and duplicates
child policy; re-deriving configuration after restart can silently change tool
permissions or provider selection; a separate recovery store introduces another lifecycle and
deletion owner. Archives live with the durable run and are reclaimed by its
existing delete operation. `lib/goal_runs/journal_codec.py` splits large logical
events into digest-bound 512 KiB fragments, encoded below the native 1 MiB
document limit. Only the final reference commits the logical event; partial
completion writes leave the preceding effect unresolved. The Goal publisher
uses shared task persistence waits for timeout, busy and unavailable errors, resending identical
owner/run/sequence/payload packets and advancing only after ACK. Lost responses reconcile through
idempotent projection without re-running effects; source ACKs reuse their immutable successor claim.
Problem: short retries abandoned accepted effects during storage pressure. Decision: retain frozen writes
until ACK, Stop before admission, permanent refusal or shutdown. Stop permits completed-effect/account
evidence to settle; shutdown leaves unresolved recovery debt. Shared backoff/liveness adds no worker,
queue, store or timer. Alternatives: longer retry ceilings still fail outages; effect replay duplicates work.
Cancellation, receipt preparation and replay/context restoration failures revoke sibling admission while
preserving original errors and already-admitted completions. Durable human suspension remains valid.
`test_goal_journal_backpressure.py` injects 600 lost ACKs across 100 effects; with `test_journal_failure_fence.py`
it covers exact retries, Stop/shutdown and concurrently settling siblings. Native and SQL event pages
stop before their 8 MiB byte boundary and continue from the last physical
sequence, retaining every packet without clipping context. PostgreSQL/SQLite
UTF-8 sizing stays behind the SQL adapter boundary.
Complete context is retained per agent receipt, so durable growth follows
the sum of recorded working-set sizes, not just output text. A synthetic
1 MiB-message launch and completed-context receipt against isolated SQLite are
covered by the archive tests, including Unicode and history exceeding one page.
The packet regression bounds each wire event below 1 MiB. With allocation
tracing enabled, five synthetic isolated SQLite starts measured median launch
publication 30.16 ms and completed-receipt publication 15.67 ms, maximum wire
packet 699,198 bytes, and peak traced allocation 45,691,734 bytes across all five
starts. Each start plus one receipt used eleven physical events. Traced Python
allocation is not process RSS; this measures archive overhead, not model
quality or native-storage throughput.
Tests: `tests/goals/test_goal_execution_journal.py` and the real child-invocation
replay case in `tests/goals/test_goal_worker_context.py`.

Goal's derived lane-context publication also detaches the settled worker's
messages/checklist before a reviewer or next invocation can mutate them.
Problem: removing the pending snapshot before storage I/O lost retry evidence;
borrowing its message list could bind later review text to an earlier phase.
Decision: retain one pending snapshot and its original owner/attempt/Turn
binding until acknowledgement or definitive refusal. A failed write retries
the identical settled witness through ModelContinuationStore; a newer phase
cannot relabel it, and a newer snapshot survives an older write's ACK.
Alternatives: re-reading a new frontier after a lost ACK grants stale messages
new authority; a separate recovery log duplicates the existing journal/store.
This remains a replace-in-place derived cache, never effect replay authority
or a condition for committing visible history. There are no new durable rows;
pending state references one detached working set and retires with that task.
Fault specifications: `tests/goals/test_worker_context_publication.py`.

### Outstanding work accompanies the model prefix

Problem: `ready` describes a loop boundary, but a delegated child, reserved model
request or explicitly backgrounded command may still exist. A model prefix alone
cannot distinguish these facts after process loss.
Problem: a pending request count loses the distinction between admission and
physical dispatch, and a child may close while its model request is unfinished.
Decision: account snapshots additionally retain each unfinished model admission's
account-local ordinal, original task ID, dispatch flag and reserved tokens under
the same account lock. Old snapshots may omit `model_attempts`; omission means
unrecorded details. Settled attempts disappear, and ordinals never recycle within
an account. Reservation amounts remain authorization holds, not consumed tokens
or guaranteed provider usage bounds. No request body, credentials or task object
is retained in the snapshot. Alternatives: deriving dispatch from task status or
assuming zero pending requests from an empty active-child list loses late work.
Measurement: a synthetic one-child/one-lease account (five-character child ID)
added 99 serialized JSON bytes; 10,000 lock-only snapshots averaged about 1.14 µs
on this host. Snapshot space scales with existing unfinished admissions; no new
resident queue, worker or persistence cycle is introduced. This is a local
microbenchmark with no model calls, not a throughput or scientific-quality claim.

### Model send intent and settlement witnesses

Problem: children may admit provider requests after a parent working checkpoint,
so its pending-request inventory can be stale before process death.
Decision: an observed composition binds an injected observer to the shared account
before model admission. The physical-send port acknowledges an immutable
`dispatch_intent` under the frozen stage/owner/birth identity and account ordinal
before allowing transport, then rechecks cancellation, deadline and allowances
after the acknowledgement. Each attempt subsequently records one `settled` or
provably unsent `voided` observation, with reported token totals and explicit
usage completeness. Detached and late children retain the same original account
port. The core imports no repository; transient invocations remain unchanged.
An intent proves authorization to begin sending, not provider acceptance.
Reservations are not observed costs or guaranteed usage bounds.

The existing owner-scoped record operation provides creation CAS and exact-value
conflict checks. Lost intent acknowledgement blocks sending; a subsequent
unsent observation can resolve an intent that committed without acknowledgement.
Failed settlement observation releases leases and retains paid lower bounds,
marks account completeness false and preserves existing finite-budget refusal.
It never refunds paid usage or blocks teardown. Owner reads recheck the complete
frozen binding, not only the record address. Context-cache cleanup preserves
these witnesses, whose lifecycle follows retained stage evidence.
Alternatives: trust a parent checkpoint's empty inventory; infer dispatch from
missing task rows; require successful storage during teardown. These respectively
miss later child requests, guess effects, or leak live reservations.
The witnesses add no worker or replay permission. Automatic executor continuity
still needs tool/child lifecycle resolution and an authoritative quiescence fence.
Measurement: 201 synthetic native development-engine admissions (no model
calls) averaged 0.96 ms for the intent and 0.87 ms for settlement. The two events
serialized to about 1,092 JSON bytes per attempt, excluding storage/index overhead.
A three-page recovery read took about 119 ms: an old parent checkpoint of
37 tokens was raised to 2,449 by acknowledged account observations, with 2,412
reported request tokens and one additional unfinished attempt. Memory retains
one bounded page plus scalar counters; no total-history list or attempt set is
built. These local sequential measurements are not throughput guarantees.
Recovery streams the exact owner/task key range in bounded pages, validates
stage, intent, birth, account identity and canonical addresses. It counts distinct
observed attempts (including unfinished ones), sums terminal reported-token
observations and retains the largest observed shared-account token snapshot.
New send/settlement witnesses take that snapshot under the account lock; earlier
witnesses without it remain valid. Send intents add no request consumption and
reservation holds never enter observed totals.
It combines those observations with task/working-checkpoint token lower bounds by maximum,
never duplicate addition. A journal-only receipt records its observed count and
token sum and optional account snapshot without inventing a checkpoint version. Consumed task receipts retain
the existing run CAS fence. No journal count, complete individual usage report or
empty page proves total task consumption, a verdict or replay eligibility.
Problem: acknowledged late-child consumption was ignored when the parent
checkpoint was older or absent. Decision: retain it as another authenticated
lower bound. Alternatives: add overlapping totals, scan all owner history, or
promote settled requests into task completeness. These respectively double-count,
scale with unrelated goals, or ignore unknown tools/children.
Retained witness growth is linear in provider attempts and includes no request
body or output. Account locking serializes each witness with admission; this
small sequential storage measurement is not a provider-throughput benchmark.

### Shared child membership witnesses

Problem: child creation may happen after a parent checkpoint, and failed budget
attachment previously left an allocated child registered. Decision: an observed
composition also injects a child observer into the shared account. Before granting
membership it acknowledges `attach_intent`, then rechecks the live control parent,
cancellation and deadline. Failed attachment emits `attachment_voided` when
possible and grants no membership. Account ordinals do not recycle after failures.
Detached Swarm carriers explicitly borrow their original live control identity;
joined children become their own control parents, so a closed child cannot admit
new descendants through an old proxy. Non-observed transient compositions keep
their existing admission behavior and add no storage writes.

Detachment records the original control parent/child identities and outstanding
model-request count and pending tool-executor count. New tool observations retain
the original control identity separately from the carrier's own identity, so a
Swarm proxy's unfinished tool remains attributed to the child that granted control.
Legacy records may omit these fields; absence never certifies zero pending tools.
Failed detachment observation still releases membership and
retains paid usage, but marks accounting incomplete. Detachment is not terminal
result, background-command completion, or a global quiescence certificate.
The observer retains frozen binding data, not task objects; active child ordinal
metadata is removed at detachment. Construction attaches budget before registering
conversation lineage; failure closes ingress, rejects the unstarted execution
session/any durable row and unregisters it through the existing manager port.
Alternatives: infer child absence from a stale checkpoint; discard only registry
state on admission failure; treat detachment as completed effects. These lose
later children, strand sessions/rows, or ignore unfinished external work.
The existing owner-scoped immutable record CAS carries witnesses outside the run
header; reads validate frozen stage, intent, birth, owner and ordinal identity.
Measurement: the native development-engine proof retained one attachment and one
detachment in 1,077 serialized JSON bytes (excluding storage/index overhead),
accurately reporting one pending model request at detachment. No model calls
were made. Active ordinal metadata contains only control IDs and ordinals, grows
with currently attached children, and is removed on close; historical witnesses
remain proportional to retained child attachments. No new worker, timer, model loop, or replay authorization is introduced.

### Tool admission follows live account ownership

Problem: tool reservation checked shared quotas without checking active ownership,
and a caller with a shared account could bypass it by omitting local limits.
Decision: atomic tool reservation validates the caller's declared live control
identity under the same account lock as quota charging. Closed children, proxies
of closed owners and cancelled owners receive `execution_owner_cancelled`; no
shared or local slot is charged. Live detached Swarm control carriers preserve
the original account identity. Missing local limits do not remove shared policy.
Unaccounted unbounded transient calls retain their existing fast path.
Alternatives: check only `task.aborted`; rely on a previous membership snapshot;
check ownership outside the reservation lock. These miss detached identities or
allow a close/reserve race. Existing quota failure reasons retain precedence.
Measurement: 10,000 synthetic in-memory admissions averaged about 12.07 µs
on this host, with no storage write or model call; this is not a handler-throughput
benchmark. No additional record, worker, pool or resident task is introduced. This admission
boundary does not certify handler result, hook effects or background completion;
tool-effect journaling and executor continuity remain separate unfinished work.

### Tool executor intent and return witnesses

Problem: a finished model round alone cannot identify which handler entered or
whether its original return was retained. Decision: an observed composition injects
one tool observer into the shared account before any tool slot has been charged.
Bundle binding validates every port before changing any observer. Existing tool
charges, including restored charges with no journal ordinal, refuse late binding.
Problem: ordinal zero alone did not prove that tools had never run. Alternatives:
retroactively label old work as observed or install a partial bundle on refusal;
both lose the boundary between recorded and unrecorded execution. The common executor acknowledges an
immutable intent after quota admission and before resolver/handler entry, then
rechecks ownership, cancellation and deadlines. Intent failure refuses entry and
records `not_entered` when possible. Tool slots retain their existing no-refund
semantics. Return records bind a SHA-256 of the original text or portable JSON
before presentation clipping; unsupported values omit the digest, never invent it.
An escaping executor exception is observed separately. A returned error tuple or
background handle is still an executor return, not successful external work.

Pending tool observations participate in account snapshots and completeness.
Terminal observation failure releases pending state but marks completeness false
while preserving the handler's original result/error. These ports add no store
imports to the core and no writes to ordinary transient invocations. Payloads
retain identities/digests, not tool arguments, credentials or duplicate results.
Owner reads recheck frozen stage/intent/birth identity and ordinal. Text digests
encode bounded character slices, avoiding a complete extra byte copy; JSON is
incrementally encoded. Measurement: the native development-engine proof retained
two tool intent/terminal pairs in 2,370 JSON bytes, including original control
identity and an exact original-return digest,
excluding storage/index overhead, with no model calls.
Alternatives: infer success from displayed status; replay an unmatched intent;
keep pending leases when terminal persistence fails. These respectively confuse
background/error returns, guess effects, or leak execution state.
Registered pre/post-tool hook batches now enter the same optional account port
with an explicit observation scope and a distinct batch identity. Empty hook
registries allocate no account or record. Pre-hook intent refusal blocks entry;
post-hook refusal skips callbacks and retains the original result. Existing
callback exception handling, argument modification and result modification stay
owned by the hook layer. Terminal hook observations retain the count of caught
callback exceptions; a returned batch can contain failed callbacks. Legacy records
without this count remain unknown. One integer per terminal batch adds no writes
or workers. Alternatives: infer success from batch return or rely on ephemeral
logs; both lose durable evidence of swallowed callback errors. Hook observations charge no tool slot and never imply
that callback-spawned external work completed. Legacy scope-less records remain
executor-only observations. Native development-engine verification retained two
registered batches, including one caught error each, in 2,564 JSON bytes (excluding storage/index overhead), charged
zero extra tool slots and made no model calls. Additional growth is proportional
to registered batch executions; empty registries retain the existing zero-write
path. The witnesses grant no global quiescence or replay authority. Original-result
inspection and independently verified effect settlement remain required.

### Tool quota continuity after process loss

Problem: parent checkpoints could omit charged tool slots from later executions.
Decision: new tool executor witnesses retain the shared account's observed charged
slot counter at intent and terminal observation. Recovery streams only the exact
owner/task key range, validates frozen binding and canonical ordinal addresses,
and takes the maximum charged counter across witnesses and older checkpoints.
A journal-only lower-bound receipt records observed attempt count and charged
slots without inventing a checkpoint version. Existing run CAS consumes it once.
Tool attempts, charged slots and completed effects remain separate facts; unknown
or older witnesses without counters cannot manufacture a slot count.
Alternatives: count terminal replies; add overlapping checkpoint and journal
counts; infer unrecorded slots from historical ordinals. These omit interrupted
or nested work, double-charge, or guess missing facts. No verdict, completeness
or replay permission follows. Reads retain one bounded page and scalar counters;
there is no new worker or total-history allocation.
Verification: the native development-engine proof recovered two charged slots
from witnesses while the older parent checkpoint recorded zero. It retained
incomplete consumption and granted no replay/verdict. No model calls were made.

### Terminal actors cannot issue new execution

Problem: a completed root can remain in its account for late child/request cost
settlement; membership alone therefore did not prove that it could still execute.
Decision: model reservation/send, tool reservation/intent and new-child attachment
reject the shared terminal statuses from the existing terminal-delivery contract.
A live detached carrier cannot resurrect authority through a terminal control
owner. No cancellation flag or account removal is required for this refusal.
Already dispatched model requests retain their settlement path and reported
usage; an unsent reservation can still void after its owner becomes terminal.
Alternatives: remove the root immediately (loses late settlement); inspect only
`aborted` (misses successful completion/error/interruption); let stale carriers
continue (creates effects after terminal delivery). No new state, worker or
storage write is introduced. This lifecycle fence is not a replay permission.

### Detached carriers share their control owner's local tool slice

Problem: independent proxy-local counters could multiply a child's explicit tool
slice while remaining below the larger tree quota. Decision: the shared account
tracks charged slots per active control identity under the reservation lock. Every
carrier of that identity intersects the same owner's current local cap and the
tree cap. Restored charges initialize the owner counter once; removing a carrier's
local configuration cannot erase the owner slice. Allowance snapshots include
charges made through proxies, and detachment removes the active owner counter
while global spent slots remain retained.
Alternatives: charge only each proxy dictionary; copy the counter when cloning;
charge outside the account lock. These respectively multiply slices, retain stale
copies, or race overlapping waves. Counters retain only active IDs/integers and
scale with existing account membership; no worker or persistence operation is added.
Measurement: a root plus one active child used 240 bytes for the counter dictionary
and integer shallow sizes on Python 3.12 (excluding already-shared identity strings).
Closing the child returned the map to one root entry. This is representation size,
not whole-process RSS, and counter lifetime follows existing active membership.

### Model carriers intersect the active owner's slice

Problem: a detached same-identity carrier could expand its local token cap or
retain an older deadline/usage view while borrowing a live owner's account.
Decision: reservation, physical-send recheck and allowance projection intersect
the carrier and active owner's current limits with the tree limit. Local usage
uses the maximum of the owner, carrier and account-reported tokens; overlapping
observations are never added. A stricter carrier remains stricter, while wider
or stale carrier configuration never weakens its owner's authorization.
Alternatives: trust only the copied configuration or usage dictionary; rely only
on the larger tree cap. These lose the owner's narrower slice. Late reported
settlement remains unaffected. No new state, store operation or worker is added.

### Carrier copies cannot clear unknown owner consumption

Problem: a globally unbounded account with a locally finite owner could admit a
fresh same-identity carrier whose dictionary omitted the owner's incomplete-usage
flag. Decision: local finite model authorization checks both carrier and active
owner completeness at reservation and physical send. A new carrier never restores
finite headroom after missing usage; unsent refusal still releases its reservation.
An unrelated child's missing usage in an unbounded tree does not erase an
independent known child's local slice. Alternatives: trust the copied flag or
refuse every bounded child whenever any unbounded tree member is unknown. These
respectively permit unknown spending or unnecessarily halt independent work.
No state, worker or persistence write is added; incomplete aggregate accounting
remains explicit and never trains a complete cost prior.

### D5 — In-flight attempt outcome resolution *(target, phase 4)*

The journal's refusal of unmatched agent admissions stays correct for truly
unknown effects, but today it cannot distinguish three cases that durable
evidence does distinguish. The journal admission gains a bound attempt
identity from the shared invocation pipeline, and recovery resolves the
pending admission against durable evidence before refusing:

1. **Attempt committed.** The invocation's durable terminal record exists →
   the pending admission converts into a completed receipt carrying that
   recorded result. No re-execution, no new effect.
2. **Attempt provably never dispatched.** Crash landed between journal
   admission and spawn, evidenced by a durable unchanged birth plus an explicit
   unsubmitted binding (never merely an absent row) →
   recovery journals an explicit `execution_effect_voided` receipt and
   re-executes. The retry is an audited decision, never a silent replay.
3. **Attempt genuinely unknown.** An attempt row exists without a terminal
   record (provider/tool call in flight at crash) → refusal stands; the run
   surfaces a typed `recovery_ambiguous` state with explicit owner choices
   (retry-with-void, or abandon as `worker_lost`). Tool side-effect
   ambiguity resolves through the same rule keyed on tool-settlement
   receipts. Automatic recovery never guesses.

`read_journal` gains a resolution seam; the replay interpreter and the
receipt taxonomy are unchanged. New event kinds are additive — old journals
remain valid, so `JOURNAL_VERSION` does not change for this phase; an
incompatible interpreter change would require a version bump per the existing
rule.

### D6 — One budget-continuity rule *(running contract, phase 1)*

Whole-account Goal observations follow [Execution account continuity](contracts/EXECUTION_ACCOUNT_CONTINUITY.md).
The shared invocation account now reserves the final locally admitted input
estimate and output ceiling before each provider attempt. The transport's required
settlement port records reported usage once, including retries and bridge handoffs.
Only a proven unsent request voids its lease; each transport marks dispatch at
the physical send boundary. Preflight failures refund without inventing usage.
Live siblings wait for reservations to settle under the existing deadline and cancellation controls.
Unobserved consumption blocks finite-token authorization and marks usage as a lower
bound. Provider counts can exceed local estimates, so
this remains an admission bound, not an exact invoice or crash-persistent escrow.
The counters/active leases use existing task/model concurrency and add no worker.
An owned benchmark measured 12.91 µs median reserve/dispatch/settle over 1,000 calls,
18.4 µs p99 and 640 retained bytes per live lease; settlement leaves no lease row.
The same limits apply to model fallback and native sync/async/desktop transports.
Problem: round-end checks admitted an already-unaffordable next prompt. Decision:
reserve at sealed request ingress. Alternatives: a root-only check misses children
and retries; treating missing usage as zero refunds unknown provider work.

A recovered Goal must retain spent authorization. Problem: replaying a completed
child projected its cost but left a fresh account at zero, and child-policy
projection omitted root limits. Decision: registered Flow receipts freeze the
existing `ExecutionBudget` through `budget_snapshot.py`; `recovery_account.py`
selects one complete settled frontier dominating all model/tool counters. Goal
recovery restores it once after accepted source/target lineage and ready-journal
acknowledgement, before dispatch. The accepted source Attempt supplies identical
token/tool caps and absolute expiry, including zero. Unknown consumption, later
unsettled spending, missing bounded evidence or changed policy refuse. Identical
restore retries never rewind new spending; inherited generations retain totals.
Alternatives rejected: adding cumulative samples double-charges parallel leaves;
fresh caps refund spent authorization. No wallet settlement or invoice is minted
by this port. Verify `test_budget_snapshot.py` and `test_goal_recovery_account.py`
across semantic authorities; snapshot growth/capture measurements live in
`docs/contracts/TOOL_EFFECT_RECOVERY.md`. The run-tree agent-start budget counts only
genuine dispatches — a replayed receipt reaches the claim site before the
journal returns its record, so the engine wires
`OrchestrationAgentBudget.release` as the journal's replay hook and the
phantom reservation is refunded (`engine.py` →
`ExecutionJournal.runner(on_replay=...)`). Reservation stays live-only:
crash-persistent budget reservation remains rejected, and in-flight
over-admission at crash is absorbed by the recorded-usage lower bound, not
pretended away.

### D7 — Long-horizon stability posture (running contract, retained)

These mechanisms already ship and are the stability floor this program builds
on; they are restated here so the continuity target names one place:

- Goal budget is an evidence-extended checkpoint window, never a stop reason;
  a zero-progress window ends as `stalled` → `blocked/no_verified_progress`.
- Durable human gates park worker-free across restarts with wall-clock
  approval deadlines; expiry is settled lazily through the same CAS as
  answers.
- Terminal result delivery requires acknowledgement; failed handoffs retry
  behind the maintenance fence; reconciliation sweeps re-drive lost
  transitions idempotently.
- One liveness judge (`lib/tasks_pkg/liveness.py`) bounds wedged execution for
  root and child agents alike: the stuck-task reaper applies it, the Swarm
  beacon reports and backstops with it, and explained waits (including a stale
  filesystem mount) and silence before their end never count.
- Event logs stay bounded: recovery replays pages, validates journal segments
  by hash/count, and never copies result bodies across generations.
- The launch probe sizes every pool/queue introduced above; probe failure
  falls back lean, and explicit operator overrides remain.

## Alternatives considered

- **Blindly auto-replay ambiguous admissions.** Rejected: replaying an
  interrupted provider/tool call cannot prove whether an external effect
  committed; duplicates are worse than a visible refusal. D5 resolves the
  provable cases instead.
- **Serialize interpreter control state (checkpoint the program counter).**
  Rejected when receipt journaling shipped and rejected again here: a second,
  brittle control-state schema that drifts from the interpreter. Receipt
  replay through the existing graph interpreter remains the only recovery
  authority.
- **One durable interpreter for every workflow (chat, Swarm,
  goals).** Rejected by the execution-unification decisions in
  TASK_EXECUTION_MODES.md; this program reuses the shared journal and
  invocation boundaries instead of replacing domain lifecycles.
- **Process-supervisor restart as the recovery story.** Orthogonal: a
  supervisor restarts a process, not runs, and conflicts with zero-config
  desktop deployment. Continuity must be a property of the runtime.
- **Exactly-once external I/O via provider idempotency keys.** Not generally
  supported across providers and tools; D5's attempt-outcome resolution
  covers the realistic crash window without it.

## Phasing and verification

| Phase | Scope | Focused verification |
|---|---|---|
| 1 *(landed)* | D1 launch-stamped policy, D2 boot pass, D6 replay refund, continuity events | `tests/orchestration/test_orchestration_continuity.py` (election, owner-bound successor, refusal audit, idempotency, safe deep lineage, complete handoff, kill switch, replay refund); `test_continuity_durable_election`, `test_orchestration_startup_recovery`, `test_server_startup_recovery`, `test_orchestration_continuation`, `test_orchestration_run_service`, `test_storage_application_contract` |
| 2 | D3 GoalRun continuation | Goal run survives restart with working set restored from the lane checkpoint; terminal truth preserved on spawn failure/refusal; `test_goal_worker_context`, goal invocation integration |
| 4 | D5 attempt resolution | Committed-attempt conversion, void-receipt retry, ambiguous operator flow, journal additive compatibility; journal/recovery suites |

Each landing phase updates its domain map and rewrites its section above from
*target* into *running contract* in the same change, and extends
[RELIABILITY_RUNBOOK.md](runbooks/RELIABILITY_RUNBOOK.md) with the corresponding
continuity incident playbook.


## Native completed-round checkpoint authority

Agent.checkpoints borrows an explicit trusted load/begin/complete port. The host
holds an exclusive eligible completed frontier and restores financial/time
budgets before load returns. begin must CAS to inflight before effects; complete
commits joined model/tool/progress state before a Suspend can stop the loop.
Inflight or terminal frontiers cannot load an older snapshot. Failure stops,
never reexecutes effects. The native_agent_checkpoint_v1 schema binds identity,
opaque model/tool/account-policy binding, cumulative counts, history/usage and
native progress digests. Round/tool limits must match; memory is readmitted and
past usage never becomes a second settlement. No monotonic deadline is persisted.
Native stdio/application checkpoint storage and default-root recovery remain
unbound; the core port grants no automatic storage, recovery or financial credit.
## Shared loop completed-round state

Problem: restoring messages/usage alone reset round identity, premature-close
retry credit, timeout and progress/failure streaks, allowing restart to evade
semantic breakers. Decision: run_agent_loop exposes optional on_checkpoint and
resume_checkpoint ports. State is emitted only after all tools, breaker checks,
after_tools and on_round_end settle successfully. Sink refusal propagates before
another dispatch. Terminal, aborted or halted rounds emit no resumable state.

The versioned JSON shape contains next_round, retry_bonus_used,
consecutive_tool_timeouts, claimed_tool_call_ids, progress and unchanged limits.
ProgressLedgerV2.snapshot/from_snapshot retains full call/world/evidence and
terminal-failure signatures, streaks and bounded evidence IDs. Restoration
validates version, complete fields, counter types, correlation-ID uniqueness,
signature shape and breaker/retry limits before provider dispatch. Changing
limits or restoring a state that already exhausted a breaker is refused.
Serial tool ID claims survive; batch adopters still own their ID normalization.

Replaying completed tools or restoring only the next round was rejected because
it duplicates effects or resets semantic safety. These ports do not restore
caller messages, budget meters, world state, batch-owned IDs, abort state or
execution ownership. Workflow repositories must bind those materials to a task,
acknowledge them atomically with the chassis state, and fence stale attempts.
Research automatic continuation is not enabled by this chassis interface alone.
`validate_agent_loop_checkpoint` owns complete shape, integer counters, admitted
limits, tool identities and progress-ledger validation for both persistence and
execution. Valid halted state can be inspected; execution additionally checks
the caller's exact limits and refuses exhausted breakers. Problem: partial
durable validation admitted hash-correct state that the execution chassis later
refused. Decision: share the validator; rejected: separate persistence checks or
resetting missing counters/ledger state. Validation never invokes tools/models
or grants execution ownership.
Tests interrupt at the checkpoint port and resume the real shared loop to verify
no-progress/terminal-failure/timeout streaks, retry credit and serial ID history.

Measurement: 1,000 synthetic serial tool rounds with changing result evidence
produced a 78,545-byte final checkpoint (1,000 ID claims and evidence IDs) in
269.1 ms on this host, with no provider calls. Evidence history remains bounded
at 2,048 IDs; correlation claims retain existing per-loop growth. Checkpoint
serialization therefore scales with ID claims plus bounded evidence. Durable
workflow adapters must choose checkpoint cadence and page long histories rather
than claim constant-space persistence from this interface.

The shared budget's `restore_model_account` port restores cumulative model
consumption and the last physical attempt ordinal into a pristine model-only
account exactly once, before admission. Problem: a reconstructed account lost
consumed authorization and restarted attempt numbering. Decision: carry both
values forward atomically; incomplete usage remains incomplete and prevents
finite model admission. Alternative rejected: initialize recovery at zero or
reuse old attempt keys. The caller must prove old-writer quiescence, validate
receipts and bind the journal observer before restoration. The port grants no
ownership, settlement or replay and is not yet activated by discovery recovery.
Verify `tests/agent_core/test_model_account_restore.py` and model budget suites.
Its sibling `restore_tool_account` continues the last tool-occurrence and
tool-reservation ordinals of a pristine, observed, uncapped account once, before
tool admission. It restores no spent slot, so a finite tool cap refuses; the
restart-paused Worker decision is in
[PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#restart-paused-worker-continuation).
Verify `tests/agent_core/test_tool_account_restore.py`.

Per-boot recovery demand — Problem: a completed boot pass stopped at a private
resource-derived resume count and stranded remaining durable candidates until
another startup. Decision: remove the boot capacity calculation and deferral
producer; keep owner/tenant election, recovery eligibility, duplicate-claim
fences, accounting checks, failure classification and operator disable intent.
Alternative rejected: larger defaults still strand enough interrupted work;
resuming without the claim protocol risks duplicate side effects. Historical
`boot_capacity` events remain readable and pending candidates remain eligible.
The independent rapid-lineage guard is unchanged and remains a separate audit
item. No new recovery daemon, worker pool or durable authority is added.


Restart lineage is evidence, not a continuation quota — Problem: three recent
retired links permanently stopped recovery even when journals were safe and
legitimate work spanned repeated host restarts. Decision: remove the private
600-second/three-restart guard and its 16-link counting walk. The existing
recovery loader owns full lineage validation, cycle/refusal classification,
unknown-side-effect fences and durable successor identity. Alternative rejected:
higher counts still strand enough safe restarts; skipping snapshot and accounting
checks risks replayed side effects. Storage-backed three- and twenty-link tests
resume the eligible leaf exactly once; ambiguous in-flight effects still refuse.
Historical exhausted decisions are neither deleted nor automatically reopened.
The legacy summary field remains zero for this retired policy.
## Swarm Continue after manual parent Stop

**Problem:** Stop is signal-only; child cancellation checkpoints and a terminated
swarm row retained useful progress, but Continue only admitted restart provenance
and skipped the old live registry entry.

**Decision:** `lib/swarm/restart_recovery.py` admits the exact authority-written
accepted Continue predecessor with cause `user_abort` (interrupted/cancelled),
including a terminated swarm row. Owner, conversation, Turn, source task/attempt,
current attempt and successor checks remain mandatory. Cold startup never gains
manual-Stop execution permission. Failed activation retry receipts still cannot
be used to cross an intervening Stop.

Before replacing an aborted live session, Continue serializes on the existing
recovery lock, waits cancellably for its driver (at most 30 seconds), refuses
remaining live workers, reloads/revalidates final durable evidence, and removes
only the old registry handle. Durable checkpoints are not deleted. A drain
failure is explicit; it never launches overlapping workers.

The swarm owner stamps unsettled children cancelled by parent Stop with
`recovery_reason=parent_stop`. Accepted Continue reopens only these ordinary
child checkpoints and never individually cancelled, completed or failed work.
Messages, tool receipts, rounds, usage, timeout accounting, launch workspace and
completed delivery acknowledgements follow the existing restore path. Pending
specs without a checkpoint remain fresh. An interrupted tool round resumes its
own agent with recovery notices (next section); no external effect is blindly
replayed.
Retained assignments already settled as cancelled remain terminal under their
own assignment CAS; this path does not reset that authority. Legacy cancelled
checkpoints without parent-Stop provenance also remain terminal.

**Alternative rejected:** reset cancelled states or rerun all specs. This loses
progress, repeats completed effects and resurrects individually cancelled or
already-settled retained assignments. No frontend or parent verdict change is
part of this owner change.

Executable specifications: `tests/swarm/test_swarm_restart_continue.py` and
`tests/swarm/test_swarm_invocation.py::test_parent_stop_checkpoint_continue_does_not_repeat_committed_tool`. <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

## Interrupted tool rounds resume their own agent

**Problem:** a restart, crash or parent Stop kills in-flight commands, leaving a
child checkpoint at `tools_in_flight`. Recovery turned every such child into a
permanent `restart_tool_outcome_unknown` failure and failed its unfinished
dependents. That bought no safety: the killed call's outcome is just as unknown
after a timeout, where the agent continues and reconciles it. The failure only
discarded progress, and the coordinator then started fresh workers that knew
nothing about the interrupted call.

**Decision:** `lib/swarm/restart_recovery.py` treats an open tool round as
resumable evidence. `checkpoint_recovery_error` refuses only transcripts that
cannot show what was in flight: malformed call/result pairs, or a
`tools_in_flight` phase without tool calls. `settle_interrupted_tool_round`
closes the newest round without re-running anything. Each recorded result keeps
its content plus a recovery notice naming the cause (restart, Stop, or restart
or crash when unknown) and stating that the call may not have run, or may have
run partially or completely. A call without a recorded result gets an explicit
unknown-outcome result. The resumed agent, which issued the calls, reconciles
them as after a timeout. Its round counter moves past the closed round, and
dependents wait for it as usual. Resume is refused with
`restart_command_still_running`, which also fences the unfinished dependency
closure, only while a foreground command record of that round survives the
boot sweep or the records cannot be read. Terminal agents keep their receipts.
An open round in a terminal transcript no longer fences dependents, which
matches behavior without a restart. Rows already failed with
`restart_tool_outcome_unknown` keep fencing their unfinished descendants.

`lib/project_mod/foreground_command_records.py` writes one record per
foreground `run_command` spawn under `data/foreground_commands/`. It holds the
owner's host, OS boot, PID namespace, PID and start time plus the per-exec
`BOOT_ID`, the leader PID and start time, and the conversation and tool-call
ids. The runner removes the record once the group has retired or exited; a
failed retirement keeps it. Background commands keep their own reattachable
records and are never recorded here. The `orphaned command retirement` boot
step runs before task and turn recovery. It retires the records of a dead
owner, including this process before an `execv` restart (same PID and start
time, different instance id), through `process_group`. A live leader must match
its recorded start time. With the leader gone, surviving members can only
belong to the recorded group, because Linux keeps a group id allocated while
any member lives. Records from an earlier OS boot are dropped; live, foreign
and unknown owners are left alone. Growth is one small file per concurrently
running foreground command plus any failed retirements, which every boot
sweep revisits. Measured on the DolphinFS data root: record plus release cost
5.5 ms per command (median, p90 5.9 ms); an empty boot sweep takes 0.6 ms.

**Alternatives rejected:**
- Keep the permanent failure: it loses hours of progress for no safety gain.
- Re-run the interrupted call automatically: a silent replay of an unknown
  effect.
- Mark aborted rounds `ready` in the shared root loop: it hides the
  interruption from the model.
- Drain in-flight tool rounds before shutdown: the 30-second hard deadline,
  half of it reserved for storage, cannot cover multi-minute builds and test
  runs, the commands a restart most often interrupts. A short drain only
  saves commands that are cheap to repeat.
- Durable foreground output with reattach after restart: the structural fix
  for long commands, but it replaces `run_command`'s pipe-based I/O and
  belongs in a separate change.

**Verification:** `tests/swarm/test_swarm_restart_continue.py`,
`tests/swarm/test_swarm_restart_dependencies.py`,
`tests/swarm/test_swarm_invocation.py::test_shutdown_during_tool_resumes_with_notice_and_never_reruns_the_tool`
(the real pipeline from a shutdown mid-tool to the persisted checkpoint and the
resumed model request; the tool is not executed again) and
`tests/project_mod/test_foreground_command_records.py` (real Linux process
groups: `execv` incarnation, crashed owner, exited leader with a surviving
member, live and foreign owners, previous boot, PID reuse, runner record
lifecycle and failed retirement).

## A background handle outlives its process only through its durable receipt

*(running contract; owners `lib/tasks_pkg/handlers/poll.py`,
`lib/tasks_pkg/background_commands.py`, `lib/message_queue.py`)*

**Problem (2026-11-12):** Worker `worker-00faef47` launched `bg_384ba49b4ce8`
at 06:10:29 in attempt `2bb7cd70`; it finished at 06:13 with exit 0. Storage
was unavailable from 06:13. The completion's durable delivery failed ("Storage
writes are fenced while the sidecar is unavailable", 06:15, 06:18, 07:28) and
was retried by maintenance until it was queued (receipt `b15df8be`, present by
the 08:39 batch drain). The attempt itself waited from 06:18 on the persistence
of its next model occurrence, so it never reached a round boundary to drain its
inbox; it was settled at 08:50. After the 09:07 re-exec the continuation
`55c82f63` (09:19) polled the handle and read "Unknown handle", then reran the
command in the foreground; the receipt stayed queued a day later. Three
mechanisms combined:

1. `poll` consulted only the process-local registry
   (`background_commands.py:55`; `poll.py` `_route`). A queued delivery settles
   the command (`_SETTLED_DELIVERY`, `background_commands.py:61`) and removes
   its record directory (`:376-377`), so after a restart the conversation's
   receipt is the only evidence, and `poll` never read it.
2. A closed-Worker settlement fences the receipt out of a later attempt.
   `dispatch_completed_background_batch` (`message_queue.py:887`, call at `:947`)
   hands receipts to `settle_closed_dispatch_completion`
   (`background_command_delivery.py:168`). For an execution whose settled Turn
   offers no `continue` option (`_settled_without_continue`, `:156`; cause
   `execution_not_continuable`, `:233`), it attaches the receipt as round-0 late
   evidence (`:281`; the Turn holds one such block for `2bb7cd70` and one for
   `55c82f63`). It then fences the conversation inbox with
   `agent_inbox.consume_peer` (`:298`) and deliberately leaves the durable row
   unacknowledged (`:301`). Yet that Turn could still be continued
   (`checkpoint_resume`, `restart_resume.py:77`). The continuation's prelude
   rebuilds inbox twins from the durable receipts (`_turn_prelude.py:88-90`,
   `pending_background_command_injections` at `message_queue.py:851`), and
   `agent_inbox.enqueue` silently drops any `queueId` in the process-local
   consumed fence (`agent_inbox/_queue.py:95`; the fence lives until process
   exit, `:270`). `55c82f63` logged no prelude failure, but its Turn holds no
   injection block for the receipt, so the drop must have happened. The INFO
   lines that would show which process recorded `2bb7cd70`'s evidence have
   rotated away; the fence (same process) or an attempt-bound row (`:872`) is
   inferred, not observed.
3. Nothing acknowledges the row afterwards. The only acknowledgement is
   provider consumption (`acknowledge_background_command_receipt`,
   `storage_authority_sql/operations_pkg/_queue.py:425`). A closed execution
   never consumes it, and every later drain re-classifies the execution as
   closed. The row therefore stays queued and is listed again for every later
   Turn of the conversation: in a fresh process a follow-up Turn receives a
   stale receipt.

**Decision (mechanism 1, landed):** `poll` answers a `bg_*` handle that this
process's registry lacks from durable evidence before calling it unknown. It
reads only the task's own completion conversation, owner-scoped
(`message_queue.background_command_receipt`). The receipt comes back with its
status, exit code and text. A record directory that startup recovery has not
settled (`background_commands.recorded_background_command`; id format checked
before any path is built, owner and conversation must match) is reported with
its exit code and "do not rerun it". With neither, the handle is typed "lost
with a server restart" and its outcome unknown: the result was either delivered
earlier or lost, and the model is told to check its effects before running it
again. Reading claims no consumption, wait, reattachment or kill authority; a
leaf worker's scope keeps its foreign-handle refusal.

**Decision (mechanisms 2-3):** (a) `settle_closed_dispatch_completion`
(`lib/background_command_delivery.py`) treats a settled execution as closed
only when its Turn offers no operation in `restart_resume.RESUME_OPERATIONS`.
A Turn offering only `checkpoint_resume` keeps its receipts pending, and the
batch issues no Continue; restart or stall recovery, or its owner, resumes it,
and that attempt's prelude delivers them once. (b) A closed settlement attaches
the round-0 evidence block and retires the queue rows in one
`turn.projection.update` (`background_command_retirement`, disposition
`settled_as_evidence`; the queue authority's rules and cost are in
[STORAGE_OPERATIONS.md](STORAGE_OPERATIONS.md), "Background receipt removal").
The `consume_peer` fence is gone. The prelude enqueues its rebuilt twins as
durable (`agent_inbox.enqueue(durable=True)`): the standing row is the
authority, so the process-local consumed fence never silently drops it and
only warns. While an engine without the member serves the conversation (today
the native engine), the evidence attaches and the rows stay listed: the settlement logs a WARNING,
remembers the written revision and never rewrites the Turn for them again. A
later Turn then receives the stale receipt, as after any restart before this
change.

**Alternatives rejected:** re-registering every handle at startup (a completed,
delivered command has no live state to own, and registry retention would grow
with history); acknowledging the receipt from `poll` (acknowledgement is
provider-consumption authority, and a poll result is not yet consumed);
answering "Unknown handle" with a hint to search the transcript (the model
reran the command, the failure observed); scanning every conversation's receipts
or other owners' records for the handle (it crosses the owner boundary).

**Verification:** `tests/tasks_pkg/test_poll_kill.py::TestPollAfterRestart`.
The specs cover: a receipt returned; another conversation's receipt and another
owner's or conversation's record never shown; an unsettled record reported; no
evidence typed lost; a malformed id never reaching the record store.
`tests/project_mod/test_coordinator_workers.py`
(`test_a_checkpoint_resumable_worker_turn_keeps_its_late_receipt_for_its_resume`:
delivered exactly once when the Turn resumes;
`test_worker_execution_without_continue_settles_its_background_batch_once` and
`test_a_receipt_attached_before_retirement_is_retired_with_the_unchanged_turn`:
a closed Turn's receipt is retired, never listed again, and an older authority
is never rewritten in a loop),
`tests/background/test_background_command_injection_lane.py::test_a_turn_prelude_never_drops_a_standing_durable_receipt_that_is_fenced`
and `tests/background/test_background_receipt_retirement.py` (SQLite and
PostgreSQL; `test_background_receipt_retirement_without_member.py` pins the
native engine, which keeps the rows).
