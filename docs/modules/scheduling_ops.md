# Scheduling and autonomous operations

This domain owns timers, scheduled conversation work, proactive dispatch,
daily reports, and bounded optimization. These paths can act without a user
click, so authority, idempotency, and blast-radius controls are contractual.

## Ownership

| Concern | Owner |
|---|---|
| Automation admission, occurrence/step records and worker composition | `lib/automations/`, `runtime.py`, [automation contract](../AUTOMATIONS.md) |
| Timer contract and CRUD | `lib/scheduler/contract.py`, `timer/_crud.py` |
| Timer polling/notification | `lib/scheduler/timer/` |
| Scheduled task execution | `lib/scheduler/executor/` |
| Conversation dispatch | `lib/scheduler/conversation_dispatch.py` |
| Proactive decisions | `lib/scheduler/proactive.py` |
| Process runner | `lib/scheduler/process_runner.py` |
| Daily report (archived day recap) | `lib/daily_report/` |
| My Day goals, items, progress, request evidence | `lib/myday/`, [contract](../../contracts/myday_goals_v1.schema.json) |
| Optimizer analysis/proposals | `lib/optimizer/analyzer/`, `proposer.py` |
| Optimizer application policy | `lib/optimizer/applier.py`, `actions/` |
| Durable state | owner-scoped Sidecar scheduler/optimizer operations |
| HTTP adapters | `routes/api_v1/automations.py`, `scheduler.py`, `daily_report.py`, `myday.py`, `optimizer.py` |
| Browser My Day goal board and day recap | `frontend/src/features/myday/goal-board.ts`, `goal-model.ts` |
| Browser My Day owner-scoped read cache | `frontend/src/features/myday/report-cache.ts` |
| Browser My Day digest/reminder lifecycle | `frontend/src/features/myday/background-controller.ts`, composed by `features/background.ts` |
| Browser My Day shell, date navigation, calendar | `frontend/src/runtime/sections/myday.js` (migration boundary) |

Already-cancelled subprocess jobs return typed cancellation without launch; success requires zero exit code without timeout, cancellation or error. Checking only after launch was rejected because termination cannot undo startup effects; cancellation during launch still joins process-tree cleanup. Scheduled subprocess output is drained in 8 KiB nonblocking reads, retaining at most 50,000 stdout and 10,000 stderr characters before accumulation. Continuous output still observes cancellation and the deadline, without extra reader threads. POSIX timeout/cancellation terminates the process group even after its leader exits, grants at most two seconds for graceful exit, then kills surviving descendants. `tests/scheduler/test_scheduler_process_runner.py` measures parent heap under a 16 MiB dual-stream flood (<2 MiB peak).

Owned task polling uses one terminal snapshot renderer in `lib/scheduler/executor/_await.py`: all status/wait paths return complete terminal evidence, errors/aborts remain partial output, and unavailable or nonterminal states never imply success. Waits require a handle, respect parent cancellation, return early on completion or pending turn input, and are bounded by the caller (60 seconds for the retired `await_task`, `POLL_WAIT_CAP_S` for `poll`); no-ID polling is a dashboard. This replaces length-only/tail-only results that contradicted the tool promise. Executable contract: `tests/scheduler/test_poll_task_results.py`.

## Governed scheduling: one primitive, scoped grants

**Problem (owner request 2026-11-10).** Workers could not schedule
(`workerDeniedTools`; `schedulerEnabled=False`) and Coordinators and the Global
Router had no scheduler tool, so the durable scheduler was reachable only from
the owner's own conversations. On 2026-11-09 a Worker paused and resumed the
nightly backup through the owner's admin route
`POST /api/v4/scheduler/tasks/system-1-storage-backup/pause|resume` over
loopback, with no grant, reason or record of who acted. Other timing
mechanisms grew beside the scheduler with different persistence and restart
behaviour (inventory below). The owner: 「这种定时器、这种机制，怎么正常到我们这个系统里面来？其实我觉得可以给。」

**Decision.** The scheduler (`manager.py` over `scheduler.task.*`) is the one
timer primitive; every role reaches it through the existing tool family under
its own grant.

- *Common owner.* A row is durable and owner-scoped; `scheduler.task.claim_due`
  takes the row lock, refuses a disabled row and claims at most once per
  `DUE_CLAIM_INTERVAL_SECONDS` (55 s, `contract.py`) on both authorities (SQL
  `_timers.py:704-748`, native `scheduler.rs` `Request::ClaimDue`), so two
  replicas or a restarted process never claim one occurrence twice. Claims are
  not leased: a crash after a claim loses that occurrence (at most once); cron
  minutes missed while the server is down are not replayed; a `once:` row past
  due fires on the first tick. A `once:` row is claimed at most once: a row
  whose claim column (`last_run`, `last_poll_at` for agents) is set never fires
  again, so a crash between its claim and its disable cannot fire it twice. A
  claim's command id names its claimant (one id per manager): storage replays a
  repeated command id as its original result, so two processes that claimed in
  the same instant under the formerly shared `task:now` id both read `claimed`
  and ran the occurrence twice, on both authorities.
  Leased, heartbeated execution with fencing is `lib/durable_worker`
  (`worker_job.*`), which the DB-4 durable-timer step may put under these rows;
  the tool and grant contract below does not depend on that substrate.
- *Scope is derived from the row, never from an argument* (`authority.py`):
  `system` (server built-ins: a built-in `task_type` or a process `system_key`),
  `service` (rows a feature owns for the owner: `router_watchdog_review`,
  `coordinator_commitment_due`), `owner` (the schedulable types an owner
  creates) and `project` (`task_type='project_wake'`).
- *A project row carries its grant in the row*, committed atomically with it:
  `command` is the canonical JSON grant (`projectPath`, the Coordinator
  `coordinatorId` and `bindingRevision`, the creator's role, conversation,
  Turn, Attempt and, for a Worker, `assignmentId`, plus `reason`);
  `target_conv_id` is the Coordinator it wakes. Its id derives from owner,
  creator conversation and the call's `idempotency_key`, so a retry never
  schedules twice and a different payload under the same key is refused.
- *A project row only wakes.* When due it delivers one nonhuman notice
  (`[Scheduled wake: …not the owner’s words and not authority]`) to the
  project's Coordinator through `dispatch_scheduled_turn(…, inject_mode='steer')`
  with a command id per row and occurrence, the commitment-wake path. Before
  delivering it re-reads its premise: a changed Coordinator binding or a
  revoked Worker grant makes it inert (disabled, result recorded). It runs no
  code, model or tool and grants nothing; the Coordinator acts under its own
  authority.
- *Who may act* (tool boundary; the owner's routes pause, resume and stop
  any row in one action, see "Agent-created schedules" below):

  | Actor | list | create | pause/resume/cancel/run/update |
  |---|---|---|---|
  | Worker (`project` grant) | every row, annotated | `project_wake` (`once:` only, waking its Coordinator), `prompt`, and `command`/`python` when its grant holds `run_command` | rows created under its own assignment |
  | Coordinator | every row, annotated | `project_wake` (cron or `once:`) waking itself, `prompt`; no code rows (its ceiling runs no code) | every `project` row of its project |
  | Global Router | as a Coordinator of its router home | as a Coordinator | every `project` row of every project (it leads every Coordinator); `owner`/`service`/`system` rows only pause/resume under the R9 `scheduler` scope (the owner's full grant covers it, no re-grant), with an owner trigger and a gate record each time ([ROUTER.md R9](../ROUTER.md#r9-the-owners-delegation-the-global-router-acts-for-the-owner-owner-decision-2026-11-06)) |
  | Owner's own conversation | unchanged | unchanged (`command`, `python`, `prompt`, `agent`) | unchanged |

  `update` on a `project` row sets only `name`, `schedule` and `description`;
  its grant never changes. No role resumes or fires a row the owner paused,
  and a role without code execution never fires a code row (`run`). Every outcome, including each refusal, writes
  `audit_log` (`scheduled_task_controlled` for creations and controls,
  `scheduled_task_refused`) with actor, row scope and grant; a delegated
  control also writes the gate's delegated-action record (the owner's
  "Act for me" log) and its `owner_delegation_*` events.
- *Tools.* The existing names, projected per role: `schedule_list`,
  `schedule_create`, `schedule_manage` (`disable` pauses, `enable` resumes,
  `delete` cancels, `run` fires now). A refusal or error is a failed call
  (`TrustedResultText` with `execution_status='error'`), never `[done]`, on
  every surface, the ordinary conversation included; it says nothing changed
  and names the remedy: who can act (the Coordinator, the owner, the Global
  Router under the owner's delegation) or what to call instead.
- *Recurring work from a check-in.* A wake grants nothing, so a cron check-in
  becomes real recurring work only through authority its Coordinator already
  holds when it fires:
  1. *An open owner request.* A wake that fires while the request's Turn or
     its continuation runs is steered into it, and that Turn delegates,
     reviews and re-delegates Workers under the request's grant as usual. A
     promised time ("re-check the eval at 23:00") belongs on the task board
     as a commitment: its due wake is the request's continuation (reason
     `commitment_due`) and may re-delegate under it
     ([PROJECT_COORDINATION.md](../PROJECT_COORDINATION.md#commitments-on-the-task-board-owner-request-2026-11-08)).
  2. *A standing instruction*, for work the owner wants every time ("run the
     full suite every night at 22:00 and tell me only about failures"). The
     Global Router records the owner's words for the project
     (`router_instruction_create`) and schedules its own check-in
     (`schedule_create(schedule='0 22 * * *', purpose='apply standing instruction
     ri-… to <project>')`). Each wake starts or steers a Router Turn that
     applies it: `route_to_project(project_path, instruction_id,
     source_turn_id=<the Turn the wake started or steered>)`. The project's
     Coordinator receives the owner's frozen words as a genuine request and
     delegates Workers under it. Pausing or cancelling the instruction stops
     the work; cancelling the row stops the check-ins.
  3. *Neither.* An idle Coordinator's wake starts a nonhuman Turn that can
     read, report, ask and update its board but cannot delegate
     (`delegation.source_for_task` refuses synthetic input). It reports what
     needs the owner, or asks the Global Router to schedule the work under a
     standing instruction.

  A Worker's one-shot wake is a reminder to its Coordinator, which decides
  under its own request.

**Alternatives rejected.** A new timer engine or store, or the proposed
`schedule_task`/`pause_task`/`resume_task`/`list_tasks`/`cancel_task` family:
a second surface over the same rows, panel and claims (principle 4), names
that collide with running-task and Worker-cancel vocabulary, and five more
names on every Coordinator surface. Moving rows onto durable-worker leases now:
automation production composition is still disabled and the substrate change
is the DB-4 step, not this increment. A grant record beside the row: a row
whose record write failed would be ungoverned. Worker control of system rows (the backup pause):
system rows are the owner's; the Router acts for the owner only under R9.
Per-role caps, minimum intervals or switches: tuning knobs (principle 5);
Worker wakes are one-shot and each needs an explicit call inside a live grant.

**Timing mechanisms and their owner.**

| Mechanism | Fires from | Persistence and restart | Duplicate/loss | Control |
|---|---|---|---|---|
| Scheduler rows (cron/`once:`) | 30 s tick, `manager.py:1041`, `:1619` | rows; `once:` past due fires on first tick; missed cron minutes are not replayed | claim CAS per 55 s window; crash after claim loses the occurrence | per scope above |
| Built-ins: backup 02:00, optimizer 03:30, provider probe 08:00, model intelligence 08:30, report backfill every 6 h, reserve reclaim every 5 min, digest 21:00, quiet release 08:00 | the tick | keyed `scheduler.task.ensure` at boot (`manager.py:233`); a pause survives restarts unless `reconcile_enabled` | as rows | owner panel/admin routes; Router under R9 |
| Watchdog review and re-checks | `router_watchdog_review` rows, `router/watchdog/review.py` | rows | command id per row and minute | Watchdog service; owner via `watchdog_configure_review` |
| Coordinator commitment wakes | `coordinator_commitment_due` `once:` rows, `coordinator_commitments.py:214`, `:251` | rows, deterministic ids | delivery keyed by commitment identity | commitment service |
| Coordinator timed wake | process-local `threading.Timer`, `project_dispatch/continuation.py:378` | none; boot and settlement recovery re-derive it | lost on restart, recovered | none |
| Worker-settlement drain | one-shot thread, `turn_lifecycle.py:2075` | none | dropped wakes after restart (WAKE-DRAIN-DURABLE) | none |
| Boot recovery, restart resume | boot passes, `project_dispatch/recovery.py:340`, `restart_resume.py:363` | dispatch and Turn records | per pass | none |
| Tick riders: channel bridges, GitHub, bots, restart notices, timer adoption | `manager.py:1571-1615` | their own records | per owner record | their owners |
| Timer watchers | `timer.*` rows with watcher leases, `timer/_lease.py:30`, `_loop.py:583` | rows, resumed at boot | continuation command identity | creating conversation |
| Device, background-batch and fault-signal wakes | `desktop/device_waits.py:543`, `message_queue.py:887`, `router/watchdog/signals.py:241` | their own records | command identities | their owners |
| Durable worker jobs | `worker_job.*` leases, `durable_worker/__init__.py:427` | leased, fenced, defer to a wake time | lease expiry reclaims | domain registrations |

Remaining instances, by value and risk: the settlement drain's durable
pending wakes (WAKE-DRAIN-DURABLE) are delivered from the existing tick, as
restart notices are, not a new timer; the Coordinator timed wake becomes a
keyed row per owner and project (both paths run the idempotent
`recover_coordinator`, so overlap during migration cannot double-act); event
triggers (Worker settled, channel message, backup epoch) need an event row kind
and stay with their owners until then. Watchdog premise checks
(WATCHDOG-A-GROUNDING) and durable-job settlement (ZOMBIE-DURABLE-JOB) change
their handlers, not the primitive. Boot passes and timer watchers stay separate.

Specifications: `tests/scheduler/test_scheduler_governance.py` (roles, grants,
refusals and their remedies, delegation, wake delivery and surfaces),
`tests/scheduler/test_scheduler_exactly_once.py` (restart and concurrent
claimers on SQLite and native; serial),
`tests/scheduler/test_router_pauses_backup_end_to_end.py` (the Global Router
pauses and resumes `system-1-storage-backup` through its own tool under the
owner's 11-06 grant: real binding, owner Turn, row, gate, tick and audit, no
HTTP) and `tests/scheduler/test_commitment_wake_liveness.py` (a due commitment
starts a Turn in an idle Coordinator and reaches a busy one as a steer its next
round reads, for the Global Router and a project Coordinator).

## Agent-created schedules (owner verdict 2026-11-13)

**Problem.** A Hope liveness patrol needed a host-side probe every 30 minutes
(`13,43 * * * *`). No Global Router, Coordinator or Worker could create it, so
the Coordinator asked the owner to paste a command into an ordinary chat. The
owner: 「这个没有定时任务的设计记得要补上。然后你不能派发一个Worker让他去做吗？」
Needing him is a design defect; agents hold capabilities equal to his, with
audit and his visibility (ROUTER.md R9). The mechanism: delegated Workers'
grants (`delegation.capture_worker_policy`) froze `schedulerEnabled=false` into
`request.delegation.policy.config` and canonical's `workerDeniedTools` removed
`schedule_create`/`schedule_manage`, leaving `schedule_list`; the governed
increment above let roles create only wakes, which run nothing; and only the
owner's conversations created `command`, `python`, `prompt` or `agent` rows.

**Decision.** Recurring agent work is a scheduler row like the owner's, on the
same manager, storage operations, tick and claims; no second scheduler.

- *By default.* Every Worker grant carries the family (`workerTools`, since
  `workerDeniedTools` no longer names it), the Coordinator and Global Router
  ceilings already do, every Coordinator continuation and project Worker grant
  admits it whenever its policy was frozen (`scope.granted_tools` derives it,
  as it derives `poll` for old continuation policies), and the role projection
  (`tool_defs.project_role_scheduler_surface`) offers each role exactly what it
  may create: `project_wake`, `prompt` (one model call without tools) and, only
  when the role's admitted catalog holds `run_command`, `command` and `python`.
  `schedulerEnabled` gates nothing (`registry/_build.py` `_build_scheduler`
  attaches the family whenever durable state and a base tool exist); it only
  puts the family on the wire directly instead of behind Tool Search, and a
  brief that mentions scheduling selects it either way (`tools/routing.py`).
  The Worker policy keeps it `false` because that config is also its
  Coordinator's continuation config, which must match the Coordinator's owner
  Turns (`pin_coordinator_request_policy`) to keep their prompt-cache prefix.
- *No more capability than the creator holds.* Code rows need code execution:
  the actor's `code_execution` comes from the Worker's validated grant
  (`run_command` in `scope.granted_tools`) or the Coordinator-family ceiling,
  never an argument, and the executor refuses otherwise
  (`scheduler_capability_code_execution`; a Coordinator delegates a Worker
  that holds it, as the owner asked). A code row runs as the owner's
  least-privilege scheduled process, exactly as the creator's `run_command`
  does (the host's network included), with its working directory the project
  its creator's grant names (`grant.workspace`), never the Worker's ephemeral
  checkout; a gone directory fails the run visibly and runs nothing. `agent`
  rows are refused to roles (`scheduler_task_type`): they run a full-tool Turn
  with the target conversation's capabilities, which no role grant holds; a
  wake starts a Turn in the Coordinator under its own grant instead. A role
  without code execution cannot `run` a code row either.
- *Attributed and owner-visible.* The row carries its creator grant in
  `tools_config.creatorGrant` (a wake's in `command`), committed with the row;
  only the scheduler writes it, and a caller's `tools_config` naming it is
  refused. The grant records the creator's role, conversation, task, Turn,
  Attempt, assignment and the delegating request's Attempt
  (`requestAttemptId`), the project and lead, and the required `purpose`. The
  owner's list and task routes (`GET /api/v4/scheduler/tasks[/<id>]`) and his
  `schedule_list` show `created_by`, `purpose` and `creator_state`; runs are in
  `/tasks/<id>/history` and the row's last status.
- *Stop.* The owner pauses, resumes or stops it in one action
  (`POST /tasks/<id>/pause|resume`, `DELETE /tasks/<id>`, or his
  `schedule_manage`, all through `manager.control_task`). The owner's pause is
  recorded on the row (`tools_config.ownerPaused`): no agent resumes or fires
  it (`scheduler_owner_paused`); his resume clears it. The creator controls its
  own rows, its lead controls them too: a Worker's Coordinator, and the Global
  Router for every project's rows.
- *Outlives its creator.* A row keeps running when its creator's work ends;
  `creator_state` shows `ended` with the reason (`grant_revoked`,
  `reviewed_accept`/`reviewed_reject`, `binding_changed`) or `unknown` when the
  lookup fails. Wakes keep their premise checks above, since a wake whose
  Coordinator moved has no one to wake.
- *Audit.* `scheduled_task_created` (with `created_by_role`,
  `created_by_conversation`, `created_by_assignment`, `purpose`) and
  `scheduled_task_controlled` for every create, update, pause, resume, run and
  delete by a role or the owner (`role='owner'`, `via` `http` or `tool`), plus
  `scheduled_task_refused` for each refusal.
- *No knob.* No per-role quota, cap, minimum interval or switch. The limits
  that apply are the scheduler's own: one-minute cron resolution on a 30 s
  tick with one claim per `DUE_CLAIM_INTERVAL_SECONDS` (55 s, `contract.py`);
  a code run's `max_runtime` (default 300 s) and the process runner's output
  retention (50,000 stdout and 10,000 stderr characters); the deployment
  switch `SCHEDULER_ALLOW_CODE_EXEC`, which disables code rows for everyone;
  the storage field bounds (name and schedule 512, command 20,000
  characters). The Sankuai Claude tool-name budget (440 bytes) keeps the
  family outside every role's direct core: a route's budget may defer it to
  `search_tools`/`execute_tools`. A Worker grant that admits the family
  without listing it keeps it off the direct list on every route
  (`_tool_assembly_prep.assemble_round_tools`), so its frozen wire and prompt
  prefix stay; a Coordinator continuation holds its request's catalog, family
  included, and its wire ([ROUTER.md R15](../ROUTER.md#r15-a-continuation-keeps-its-requests-catalog-owner-only-decisions-are-gated-at-execution-owner-verdict-2026-11-13)).
- *Frozen catalogs.* Tools and policy are frozen per request (the first
  delegation records them; later Workers and continuations reuse them), but
  the scheduler family is derived at admission, not frozen: after the restart
  every open request's Workers hold it, whatever their policy recorded, and a
  continuation holds its role's catalog (R15); Coordinator and Router ceilings
  are code, so their next Turn has it. What stays frozen is each grant's own capability: a Worker
  creates code rows only when its frozen tools hold `run_command`.

**Alternatives rejected.** A second scheduler, agent-only task store or
separate tool names: a parallel surface over the same rows (principle 4). An
owner-only scheduler with agents asking the owner: the defect itself. Setting
`schedulerEnabled=true` in the Worker policy: it is the Coordinator
continuation's config too, so its replay witness would drift from the owner
Turns and rebuild full history (the 11-08 Router incident,
`tests/project_mod/test_coordinator_replay_policy.py`), while the grant already
carries the family. Adding the family to `continuationTools`: the lists frozen
by open requests would still lack it, and the next role tool would be missing
again (resolved for every role tool by R15). Code rows
for every role, or `agent` rows with a captured tool ceiling: a Coordinator or
the Router would run code it does not hold, and an `agent` row needs a ceiling
port in scheduled dispatch and the poll model, which a wake already provides
for roles. Running code rows in the Worker's checkout: the checkout is retired
with its work, which would end a patrol silently with its creator. Retiring
rows when their creator ends: patrols outlive the Worker that set them up; the
end is shown instead. New storage columns for creator and purpose: a schema
change across both storage authorities for data the row's existing JSON
field carries atomically. Per-role quotas or minimum intervals: tuning knobs
(principle 5).

Specifications: `tests/scheduler/test_agent_scheduling.py` (default grant and
surfaces through real assembly, the Router's and a Coordinator's continuation
Turns and grants frozen before the family, creation by each role, attribution in the
owner's list and routes over SQLite storage, one-action owner pause and stop
honored by the tick, the lead's control, capability refusals, workspace,
creator state and audit) and `tests/scheduler/test_agent_schedule_end_to_end.py`
(the patrol probe created by a Worker, refused to a Coordinator, listed for the
owner and run by the tick).

## Timer lifecycle

1. An authenticated owner creates a watcher with a validated readiness check and continuation.
2. The Sidecar persists identity, owner, next due time, and revision.
3. Polling claims due work idempotently.
4. The executor invokes a declared action with bounded context and runtime.
5. Outcome is recorded before the next schedule is advanced.
6. Notification/push is a projection of the recorded outcome.

Restart must neither lose a durable timer nor execute one occurrence twice; process-local wakeups are hints, the Sidecar row is authoritative, and parse/timezone failures suspend visibly instead of entering a tight poll loop.

Timer Watchers register every accepted live watcher without process/owner count admission quotas. `TOFU_TIMER_LIVE_CAP` and `TOFU_TIMER_RESUME_CAP` are retired. Startup recovery uses the internal active feed with limit=0 (all rows through native index pages) and starts every eligible definition; declared positive query limits still select bounded projections. Cross-replica leases and local duplicate registration retain single ownership. Actual thread-start failure rolls back local registration/lease and reports startup failure; tool creation keeps its existing durable cancellation rollback. Native total/active counts remain exact integrity observations and migration no longer refuses definition 513. Waiting watchers share one lazy event loop; blocking storage/model calls use demand workers with no active ceiling or retained idle workers. Cancel wakes waits. Shutdown joins physical calls and releases leases before task quiescence, HTTP/runtime-store closure; failed joins keep dependencies live. Gates include `tests/scheduler/test_timer_wait_runtime.py` (32 blocked peers plus healthy work, stop/cancellation drain) and 512 waiting watchers sharing one thread.

Clock jobs use `schedule_create` (cron/`once:`); readiness waiters use `timer_create` (single-shot continuation, no cron). Schemas remain within 600/450/1000 combined tokens. Timer creation is resident/direct-call under routed exposure, Tool Search and programmatic compaction; other scheduler tools remain searchable. SQL/native admission accepts predicate-only watchers, and native `max_polls=0` preserves unlimited checks. Human-only restart/redeploy waits remain forbidden. A continuation or `current` agent job targets only a conversation whose turn the creating task owns (`completion_conversation_id`): child invocations, conversation-less tasks and headless requests are not offered `timer_create` and are refused both with a typed error (task engine, "Background command delivery owner").

**Problem:** equal command output can hide model recovery or semantic changes; negative agreement does not validate a future positive edge. **Decision:** predicates use zero LLM calls; hybrid/semantic watchers retain verification and require JSON booleans. Cancellation fences model/tool boundaries; the shared no-progress guard stops repeated tool/result loops. **Alternatives rejected:** permanent output caching and promotion after three negative agreements can miss or falsely accept readiness; code-only errors never acquire an ungrounded model fallback.

**Problem:** a hybrid proactive agent's poll LLM has no tools and its status snapshot never carried the predicate result, so its authoritative verdict was blind and hybrid-to-code promotion depended on chance agreement. **Decision:** code and hybrid polls evaluate the predicate once, before the LLM, and append its verdict, note and fenced output to the status snapshot that the poll LLM, the reconcile and the poll ledger all use (`proactive.predicate_status`). **Alternatives rejected:** a second evaluation for the prompt (two runs can disagree with the reconciled one). Specification: `tests/scheduler/test_proactive_status_budget.py`.

**Problem:** a scheduled agent's `command` was both the poll LLM's standing instruction and the instruction executed when ready. The schema told callers to pass an empty `command` for a pure-code agent, which `execute_proactive_task` cannot run (both storage authorities also reject an empty `command` at creation, with an opaque protocol error), while `command` plus `condition_command` derived `hybrid`: the poll LLM, prompted to prefer acting when unsure, stayed authoritative until enough agreements promoted the predicate, so an agent ran although its decisive predicate exited 1. **Decision:** for `task_type='agent'`, `command` is always the instruction executed when ready. `create_task`, and every `update_task` that would leave an agent's `command` empty or whitespace, raise `ValueError` before storage; `schedule_create` and the `schedule_manage` update return it as `Error:`. `derive_scheduled_condition_kind` makes an agent with a non-empty `condition_command` `code` from creation: the predicate decides every poll with zero LLM calls, a match dispatches `command`, exit 1 waits, and an ambiguous or errored predicate never triggers. `hybrid` is reached only by the existing ambiguity demotion (sustained predicate errors in `reconcile_and_decide`), whose poll LLM reads `command` as its standing instruction. Without a predicate the agent stays `llm`. `update_task` cannot change the predicate; a real `task_type` change re-derives the kind as creation would, and otherwise stored rows keep their kind (no migration). Timers keep `derive_condition_kind`: their check instruction and continuation are separate fields. **Alternatives rejected:** a persisted `check_instruction` column for scheduled tasks adds a field across the SQL and TofuDB storage authorities and their migrations to separate roles the predicate already decides; keeping hybrid-at-creation with an LLM veto still puts two roles in one field and pays an LLM call per poll. Specification: `tests/scheduler/test_scheduled_agent_condition_contract.py`.

**Problem:** a row's `task_type` selects the authority its execution runs with: built-in types (`optimizer`, `provider_probe_daily`, `model_intelligence_daily`, `storage_backup`, `reserve_reclaim`, `daily_report_backfill`) mint system-scoped principals or sweep the whole deployment, and `router_watchdog_review` belongs to the Router Watchdog. `schedule_manage` update forwarded any `updates` object, and `update_task` accepted `task_type`, so a model could turn its own row into a built-in type (or set poll/execution bookkeeping, conversation routing and agent tool settings); `schedule_create` never checked `task_type` server-side; and an agent's `condition_regex` was not validated as a timer's is, so an invalid one kept every code-tier poll ambiguous. **Decision:** `tool_defs` declares `SCHEDULABLE_TASK_TYPES` (command, python, prompt, agent) and `TOOL_UPDATABLE_FIELDS`, and the published schemas are built from them. The tool boundary refuses other create types and other update fields by name; `update_task` lets a row switch only among the schedulable types and applies the code-execution gate to the new type, so built-in and service rows keep the type their owner created whatever the caller. Agents and timers share `validate_condition_regex`. **Alternatives rejected:** an execution-time owner check per built-in handler (each new handler must remember it, and the row is already corrupted); refusing service types in `create_task` (the Router Watchdog legitimately creates its review rows with an owner principal). Specification: `tests/scheduler/test_scheduler_tool_boundary.py`.

Restart resumes durable active timers regardless of creation age, including
`max_polls=0` watchers. Offline elapsed time does not consume actual poll budget
or justify rewriting a timer as expired. `TOFU_TIMER_MAX_AGE_HOURS` is retired
and ignored; cancellation, exhausted actual poll counts, leases and terminal
states still govern execution. Startup restoration carries each timer's owner
and never mutates durable status based on wall-clock age.

Committed `ready` is a dispatch obligation: busy/unavailable lanes retry without new checks, poll exhaustion or resume age expiry. Exact conversation command identity deduplicates continuation.
Committed `ready` is a dispatch obligation: busy/unavailable lanes retry without new checks, poll exhaustion or resume age expiry. A busy target lane is delivered, not deferred: the continuation enters the SAME conversation command service with `injectMode='steer'`, which commits it into the live attempt as a durable injection block the model sees on its next round and falls back to the durable queue when the steer window has closed. `steered` and `queued` therefore settle the watcher exactly like `started`, and the recorded `last_poll_decision` is the delivery receipt rendered by the timer panel. Exact conversation command identity deduplicates continuation.
 Cancelled/foreign-owner calls have no local watcher authority; exceptional loop exit releases the registry/lease for resume. Commands use the shared bounded process runner, explicit owner/project directory; regex readiness requires successful stdout. The 16 MiB flood test retains <2 MiB parent heap and at most 3,500 stdout + 500 stderr characters. No resident worker is added; default 120 polls bound ledger text to about 0.8 MiB, while explicitly unlimited watchers retain growing audit history. Gates: `tests/scheduler/test_timer_readiness_reliability.py`, SQL storage contract and native semantic timer tests.

**Problem:** executor acceptance can precede failure of the timer's triggered receipt, and a browser list request can predate a successful mutation. **Decision:** a ready watcher retires only after the triggered status CAS is acknowledged; failure retains the obligation and replays the same Turn command, without another executor. A lost CAS never announces success. Browser mutations retain their pending row until a new list read follows any older in-flight read. Click containment uses the original event path, so synchronous pending-row replacement cannot turn an internal action into an outside click and close it. **Alternatives rejected:** retiring on executor acceptance strands active rows; reusing a pre-mutation list restores stale actions. These changes add no worker, cache or periodic read.
**Alternatives rejected:** retiring on executor acceptance strands active rows; reusing a pre-mutation list restores stale actions. A second watcher-owned lane for continuations was rejected as a duplicate dispatch path; waiting for the target turn to end was rejected because a long agentic turn never yields and the watcher's one-shot obligation would be spent on retries that re-poll the model.

**Problem:** a readiness watcher never owns its target lane, and the conversation is very often still running when its condition becomes true — a bare `lane_busy` refusal made every trigger a paid no-op. **Decision:** the watcher asks for the steer lane, so `steered`/`queued` are accepted deliveries that retire it; only `retry` (lane unavailable) keeps watching. The trigger route maps `retry` to 409 `timer_delivery_retry` and a settled row to 409 `timer_inactive`, and the panel announces every outcome — delivery lane on success, retry/closure/failure otherwise — instead of rolling the row back silently.

A steer reaches the model only at its attempt's next round boundary. When the attempt ends first, the task finalizer re-queues the undelivered steer as the conversation's next turn (`_salvage_undelivered_steer`) and `record_requeued_continuation` moves the watcher's receipt from `steered` to `queued` (CAS on `triggered`, best-effort; the queued turn is the delivery authority). An already rendered final answer is never reopened to consume a late steer: it would stop being the answer it was rendered as.

**Problem:** a poll still in flight when its watcher settled (operator delivery, check-now delivery, cancel, dispatch failure) committed its verdict afterwards, overwriting the delivery receipt, spending budget and appending a ledger row on a settled row. **Decision:** `timer.poll.commit` advances only an active watcher in both authorities; a settled one returns `{inserted: false, id: null, advanced: false, status}` with no ledger row, poll-ID claim or progress change, while a replayed poll ID still returns its original row. Predicate reconcile writes carry `expected_status: active`. **Alternatives rejected:** a caller-side status re-read narrows but cannot close the window; gating only the progress update would still leave a ledger row and claim on the settled watcher. Contracts: `tests/storage/test_storage_authority_sql_contract.py`, the native `timer_poll_commit_*` tests, the timer script in `tests/tofudb/test_tofudb_differential.py` and `test_timer_continuation_integration.py`.

**Problem:** the panel's only action, 触发, was labelled in most locales as running one check but delivered the continuation unchecked, so a continuation could assert a condition that did not hold. **Decision:** `POST /api/v1/timer/<id>/check` (`check_timer_now`) runs one poll with a background poll's semantics — it spends one poll of `max_polls` (a spent budget retires the watcher as exhausted), writes the same ledger row, and a committed `ready` is delivered without re-checking — and delivers only on `ready`. The committed row, not the call's own verdict, decides: a poll number is one command identity, so a check that loses it to a background poll gets a command conflict and reports the row that poll committed. The response carries `ready`, `decision`, `reason` and, when delivered, `delivery`; delivery refusals reuse the trigger route's 409 codes. The panel makes checking the default action and names an unmet condition with its reason; `/trigger` remains forced delivery, labelled as skipping the check. **Alternatives rejected:** changing `/trigger` in place would silently alter a documented API; waking the watcher loop early yields no verdict for the operator and needs a cross-replica signal. Specifications: `test_timer_check_now.py`, the timer routes in `tests/api/test_api_v1_integration.py` and `tests/frontend/test_frontend_timer_trigger_feedback.py`.

These changes add no worker, cache or periodic read.
 Fault and real-storage specifications live in `test_timer_dispatch_outcomes.py` and `test_timer_continuation_integration.py`; `test_timer_mutation_reconciliation.py` and the visual timer-panel test cover stale responses and failure recovery.

Timer/proactive conversation-dispatched runs retain `build_task_config` in
`lib/scheduler/_shared.py`: the timer's `tools_config` wins on conflict for tool
choices, otherwise conversation settings carry through — including the
translation pair (`autoTranslate`, `uiLang`) that every translation trigger
resolves from `task['config']` alone. The model route, reasoning depth, extra
and read-only roots and Manual write approval (`autoApply`) come from the
conversation through inactive `resolve_conv_config`; its `modelRef` is the
routing authority, so the `model` name is aligned to it, as task registration
does. A conversation without a model choice (no name and no `modelRef`) runs on
the owner's default model (`owner_default_model`), never with an empty model
([ROUTER.md R5](../ROUTER.md#r5-router-and-worker-models-are-chosen-separately)).
A `schedule_create` definition's model-authored `tools_config` never selects
the model. Attended capabilities (`autopilot`, `desktopEnabled`,
`humanGuidanceEnabled`) are explicitly off: never granted, and recorded as a
human turn records them. While the conversation is in Plan mode the dispatch
returns `busy` (nothing accepted); a timer keeps its ready verdict and retries
without a new check, and an `await_device` wake stays pending for the device's
next connection, so a continuation can neither escape the read-only plan nor
gain the human guidance Plan requires.

**Problem:** a continuation ran on a different model than its creator, at the
default depth, without the owner's prompt, and rebuilt its model context because
the replay policy witness differed (stored settings had merged the model name
and `modelRef` from different sources; see `resolve_conv_settings`).
**Decision:** `timer_create` and `await_device` capture the creating task's
`CONTINUATION_POLICY_KEYS` server-side (`capture_continuation_policy`): route,
reasoning, `maxTokens`/`temperature`, system prompt and its mode/blocks,
`chatMode`, `delegateMode`, `schedulerEnabled`, the
workspace binding (`projectPaths`, `readOnlyPaths` with `projectPath`) and the
owner preference blocks `tools`/`cache`/`responses`/`compaction` (mappings only;
a caller's tool-schema list is never replayed). `continuation_policy` replays
them as the dispatch's `config_overrides`, replacing the route as a unit so a
captured name is never paired with the conversation's `modelRef`. Chat mode is
dropped when its flag expansion would grant a tool (the swarm rule). A
browser-shaped creator and its continuation then share one policy witness.
Rows without a capture (created before it) follow the conversation's route and
still rebuild once. Flow and attended capabilities are never inherited; a
creator with any of them on therefore still rebuilds. **Alternatives
rejected:** conversation settings alone cannot reconstruct the global prompt or
owner preferences; copying the whole parent config would carry attended
capabilities and internal carriers into an unattended turn; equating an absent
witness field with an explicit value was rejected for replay ingress
(`docs/CONTEXT_COMPACTION.md`), so server-built turns state explicit values
instead. Replay treats only spellings every consumer resolves alike as equal
("Equivalent witness spellings" there), so a captured nondefault prompt,
chat expansion or tool preference must still be stated. Contract: `tests/scheduler/test_timer_continuation_policy.py`.

**Problem:** separate swarm defaults invalidated identical strict/portable replay policy.
**Decision:** only `swarm_autocontinue_reply` uses inactive `resolve_conv_config`, sharing human prompt/route/depth defaults.

Swarm still disables Flow/autopilot/desktop/guidance/scheduling and rejects tool/noncontrol overrides;
chat-mode expansion cannot add tools. Read-only/manual-approval settings, virtual-user provenance,
Stop/parent CAS and budgets remain. Plan returns `start_failed` before admission rather than enable guidance.

**Alternatives rejected:** weaker hashes or unvalidated parent config copying. This wake has no accepted attempt
config witness; nondefault global prompt/tools still differ and rebuild. Contract: `tests/scheduler/test_swarm_continuation_config.py`.

## Autonomous authority

Scheduled and proactive work acts only for the owner recorded on the durable
row. It receives no ambient administrator capability. The action catalogue is
an allow-list with explicit input limits; arbitrary code, routes, or tool names
cannot be smuggled through timer payloads.

The scheduler worker itself starts only with an explicit system principal
carrying `scheduler:run`. Personal composition binds that principal to the
personal owner and may install the four personal built-ins plus run the
project/peer sweeps. Distributed composition is ownerless: it still claims
durable task rows and rebuilds a least-privilege principal from each row's
owner and the composed worker's tenant, but it does not invent owner 1,
install personal built-ins, or perform global project/peer scans. Child user
and maintenance principals retain that tenant while narrowing scopes; provider
maintenance carries the same owner/tenant boundary into repository reads and CAS.

Daily Detect rows have no independent tenant field. Only the composed,
owner-bound worker may run them: the row owner must match the worker owner,
and the `providers` child principal retains the worker tenant. Manual runs
through the shared scheduler tool require an ordinary Turn admitted by a user
with `agents:scheduler` (including `admin` under the shared scope rules). The
[execution identity contract](../../contracts/conversation_execution_identity_v1.json)
records only the exact Detect action; it still restores an empty principal scope
set. Turn acceptance atomically binds the grant to the original conversation,
Turn and Attempt. Durable restore proves the owned Attempt belongs to that Turn
and is still current; it never rebinds copied or legacy unbound grants. The
scheduler matches the live carrier against that binding and rechecks the durable
running Attempt and task ID before checking the row owner and worker tenant.
Neither raw invocation scopes nor the project row-creation grant authorize
manual Detect; a legacy owner stamp or tool argument cannot supply the admission.
An unbound or mismatched run cannot read model-routing authority or queue
probes. In the managed personal topology, an execution shard sends the exact
running Attempt and scheduler-row references once to the verified primary
through [private command control](../../contracts/command_control_v1.json).
The primary reconstructs the accepted principal from storage, checks current
Attempt and uncancelled WorkerJob state and applies its own scheduler owner and
tenant fence. No principal, scopes, or arbitrary scheduler action cross the
wire. The primary joins the existing probe and returns its result within the
existing Detect runtime budget; missing acknowledgement is unconfirmed and is
never replayed. Shard drain withdraws the request through the host stop-entry
signal and a half-close before worker teardown. The primary observes EOF as
loss of relay publication authority, not as durable Stop. The primary checks
withdrawal and durable Stop before probe work and again at dispatch of a
recommendation commit or scheduler result. A command already dispatched is
not rolled back. Entered provider I/O is joined under its existing timeouts;
provider cells do not start another attempt after observing probe abort.
Probe-cache progress is diagnostic, not a recommendation or scheduler result.
Shards never install a scheduler principal or attempt local Detect as a fallback.

The billing-reserve built-in is the sole periodic recovery owner and is policy reconciled disabled unless multi-user relay billing is active. The scheduler's durable claim prevents duplicate sweeps; request-worker startup creates no parallel janitor. Daily-report backfill also uses this worker: it runs every six hours, queues a startup hint only when yesterday is absent, and has no second six-hour sleeper. Optimizer and backup built-ins preserve user-controlled enablement during definition reconciliation. The personal backup's finite `max_runtime` derives from the launch-probed recovery-copy budget (30 minutes..6 hours by default, explicit ceiling 24 hours). If retained recovery points make only the transient peak exceed the copy budget, a fastpath run may hard-link and fully publish the new point before budget-retiring older verified backups; it never rotates the unique deep-clean rollback or falls back to a full copy for that exception.

Personal built-ins have an owner-scoped `system_key`; display names are not
their authority. During reconciliation, an exact-name-and-type row created
before that key existed is adopted in the same locked Sidecar transaction. If
a newer keyed duplicate already exists, the oldest legacy identity is kept and
the reconstructible duplicate is retired before the definition is refreshed,
so one due interval can produce only one built-in execution.

An LLM poll materializes one exact tool-schema epoch and compiles its
`ToolContractV2` execution documents before dispatch. The owner identity and
the same documents reach the shared executor. A missing, malformed, filtered,
or argument-invalid call is returned as a typed non-execution; contract
compilation failure disables tools for that poll instead of falling back to an
ambient registry.

One proactive status snapshot feeds both LLM dispatch and audit; its two-message
tail and scheduler/project settings/existence probes never hydrate full transcripts; frozen legacy suffix scans have a 128 KiB work budget before authoritative full decode.

Optimizer analysis is read-only. One run snapshots each eligible bounded log tail once; distributed mode still excludes raw application/error lines and audit rows remain owner-filtered. The one exception to the tail is prompt-cache split evidence (`analyzer/_cache.py`): in personal mode it streams the window's `[CacheRoundRecord]` lines from `app.log` and its plain rotated siblings into per-group counters, because a busy log's 4 MiB tail covers only a few hours (docs/LLM_COST_OPTIMIZATION.md section 6). Each audit/error snapshot streams once into all projections without retaining parsed entries; application lines without a possible signal skip timestamp/regex work, with an 8,192-character fast-path ceiling.
All block-domain post-apply metrics share one application-log pass. Transcript-derived signals select owner-scoped metadata, then use bounded repository hydration; a transcript too large for one response is read back to the signal's cutoff in repository windows, and a scan the authority still cannot serve is recorded on the bundle as unavailable evidence, which the proposer prompt names explicitly (unknown, never a zero count). Proposals are durable facts. Automatic application is
limited to reversible actions with a bounded verified write set. High-impact or
unknown proposals require human approval and never fall through to execution.
The optional LLM proposer uses request-local strict billing-stop admission: a
recorded 402/quota stop cannot be retried through a stale manual ON or direct
fallback. It also shares the finite optional-enrichment 429 allowance (personal
2, distributed 8, hard ceiling 16); failure leaves the evidence intact with
zero proposals.

Every optimizer run receives a principal with `optimizer:maintain` (an admin
request satisfies that scope) and a positive owner. Sidecar proposal/action
identities, joins, status changes, expiry scans, and indexes are keyed by
`(user_id, id)`. Schema v37 assigns historical personal rows to owner 1 only
during migration; new operation payloads have no owner default. In distributed
mode structured audit evidence is filtered to the run owner and unowned raw
application/error logs are excluded. The current file-backed
`block_search_domain` action is personal-only, so distributed runs stage it for
review instead of mutating deployment-global configuration. It writes
`search.fetch_skip_domains`, a fetch-quality skip rather than an operator ban:
search listings, the browser tools and `run_command` still reach the domain
(`docs/contracts/TOOL_EXECUTION_POLICY.md`).

Optimizer package and HTTP discovery do not initialize the analysis pipeline.
The route retains only its narrow storage/action authorities; analyzer,
proposer, applier, and orchestrator modules resolve when an authorized run
starts. Package-level compatibility exports must remain lazy so a storage-only
consumer cannot accidentally initialize autonomous execution.

## Daily report

Daily provider detection: see [model registration](../MODEL_REGISTRATION.md#daily-provider-detection).

Daily-report readers gather owner-scoped state. Historical extraction bounds candidates before hydration, then derives statistics and its 800-character transcript once with a 128-turn retained prefix; lazy failure drops the day. Calendar/day counts send explicit local intervals; storage projects Turn timestamp scalars, bounds frozen decoding, and returns only distinct counts. The calendar month overview (`lib/daily_report/calendar_overview.py`) runs one shielded computation per owner month: concurrent requests await it, a departed request cancels nothing, and report or cost invalidation detaches it so its result is never cached. Its costs come from the per-Turn usage projection `conversation.turn_usage` ([STORAGE.md](../STORAGE.md#per-turn-usage-projection)), never from transcripts; settled past days persist in `daily_cost`. Reports commit idempotently; the durable owner reconstructs `reports:maintain`. Its reconstructible LLM analysis uses the same strict billing-stop admission; the same finite actual-429 allowance applies. Explicit scheduler prompts, timer/proactive polls, and attended Agent calls do not inherit either narrower policy.

**Problem:** the LLM analysis copies whatever it saw in transcripts, and a stored report carried a full `sk-` provider key in a `tomorrow[].quick_action.prefill`; reports were also created 0644. **Decision:** every report write in `lib/daily_report/storage.py` (manual save, generated save, mutation) passes the payload through the owner's known-credential redactor (`lib/tool_results/credential_redaction.py`: vault, `server_config.json` and the owner's model-routing keys become `[redacted credential: <label>]`), then replaces each remaining bare `sk-` key shape with `[redacted credential: sk-key sha256:<12 hex>]`, and writes `mode=0o600`. **Alternatives rejected:** redacting only the LLM prompt (manual edits and model paraphrase still reach disk); a generic long-token rule (reports legitimately quote commit SHAs and ids, and unknown hex keys such as R2's are caught by the known set instead). Specs: `tests/daily_report/test_daily_report_secret_redaction.py`, `tests/json_store/test_private_config_files.py`.

Daily-report route discovery likewise keeps storage readers, cost aggregation,
conversation/todo collectors, LLM generation, and scheduler integration
dormant. The `lib.daily_report` facade resolves each focused owner on the first
authorized request or scheduled invocation while preserving the same explicit
owner and principal through the call. Its dynamic exports have unknown types,
not module-object types, so request adapters retain callable/list contracts
without eagerly importing every domain owner.

The typed report repository is an owner-scoped, reconstructible read cache: 96 reports of at most 512 KiB plus 24 month overviews of at most 128 KiB bound estimated storage at 51 MiB. Schema v3 discards the former unscoped/unbounded cache instead of migrating it across owners. The idle background feature owns one cache-first digest revalidation and one three-hour reminder timer; its owner/date ledger retains at most 16 entries and `beforeunload` destroys both timers. The retained panel writes only the month cache.

## My Day token costs

The current date shows a compact estimate in the My Day paper-and-ink palette,
not an invoice. The keyboard-accessible disclosure builds the itemized ledger
and model comparison only when opened. The calendar usage scan folds original request quotes when present,
not a new price for the Turn's flattened counters. Request-local token counts
are disjoint: uncached input, output, input cache reads and input cache writes.
There is no cached-output counter. Zero-priced requests still contribute usage.
Repeated receipt identities count once per owner scan. Date placement still
uses the Turn's date; a call spanning a different calendar day is not yet
independently windowed. Legacy aggregate-only Turns are estimated; unpriced
rounds aren't treated as zero-priced requests. See the
[usage projection contract](../STORAGE.md#per-turn-usage-projection).

Fallback prices and incomplete scans are disclosed; a failed scan does not look
like an empty day. The live calendar and disclosure accumulate the same original
CNY components (new Python receipts retain unrounded components; old receipts
retain only their recorded precision) and round after aggregation. Today's
conversation counts sum the same four disjoint buckets as the disclosure,
including free usage. Existing settled past-day snapshots remain unchanged.

The model picker reprices that same observed token/cache mix at the selected
model's standard catalog rates (first context tier where applicable, no peak
adjustments). It does not simulate tokenization, cache hits, long-context
premiums, discounts, or subscription quotas. Applying a context tier to the
entire day's prompt total was rejected because that would treat the day as one
request. Selecting a model makes no new request or setting change. The
calendar's existing single-flight response carries the dated breakdown only
for today. The browser can paint that day's last estimate from the existing
owner-scoped month cache while revalidating; IndexedDB does not gate the network
read, and an unavailable fresh result does not erase a known estimate. No
per-conversation usage is persisted in the browser. See
`frontend/src/features/myday/token-costs.ts`.

**Problem:** Once the month response cache expired, every refresh re-read every
candidate conversation's usage and folded all recorded requests again.
**Decision:** Keep one volatile live-day snapshot per owner under the calendar's
existing retention budget. `lib/conversations/usage_snapshot.py` reconciles the
current owner/day candidate catalog against the previous snapshot. A changed
revision, attribution header or lane count re-reads only that conversation;
missing IDs leave the fold. The source catalog must agree across a newly read
projection for it to be reused. Unchanged projections and pricing revisions
reuse the completed fold, including model estimates. Calendar flights and
cached results are date-scoped across midnight; settled history still uses the
existing backfill. On process restart, the first live scan is cold again.
**Alternatives rejected:** appending by wall-clock timestamp alone would miss
same-time corrections and deletions; summing independently cached conversation
costs would double-count receipts shared by clones. A changed snapshot still
runs the common receipt-aware day fold over the retained projections. The
256-conversation regression checks zero usage reads on unchanged refresh and
one usage read after a single change.

## My Day goals

**Problem:** My Day generated a daily report from the day's conversations and
asked the owner to work from it. The owner's verdict (2026-11-08): the page
went the wrong way. He wants to write what he means to get done, see it broken
into small items, and see roughly how far along each goal is: which items are
done, in progress or not started. Unfinished items continue the next day, and
he checks items off himself.

**Decision:** the page is a goal tracker (`lib/myday/`,
`contracts/myday_goals_v1.schema.json`, HTTP `routes/api_v1/myday.py`).

- One portable record CAS per goal (`myday.goal.v1`, key `<owner>:<goalId>`)
  holds the goal's items. Goals are not dated: an unfinished item stays open
  across days, so carry-over needs no copy and nothing is reset per day. The
  day view is derived from item timestamps (`startedAt`, `completedAt`).
- Items keep stable IDs: `i-` random for owner items, `a-` derived from the
  goal and normalized text for suggestions, `w-` derived from the assignment
  for Worker evidence. Item status uses the checklist and task-board
  vocabulary (`pending`, `in_progress`, `completed`), with `statusChangedAt`,
  `startedAt`, `completedAt` and a `manual` flag.
- Provenance: `origin` owner, agent or evidence. An agent suggestion is a
  proposal (`proposal` pending, accepted or dismissed). A pending or dismissed
  proposal never counts. Any owner action on it (accept, set status, edit)
  adopts it; dismissing it keeps a tombstone so it is not proposed again.
- Manual state is authoritative: neither regeneration nor evidence changes an
  item the owner set. Regeneration (`record_decomposition`) matches proposals
  by normalized text and derived ID. It never adds an item that exists in any
  state, keeps every adopted, owner, evidence and dismissed item as stored, and
  replaces only untouched pending proposals. One run per goal per process: a
  reload joins it, a client disconnect never cancels it. The model call uses
  this page's optional-analysis policy (strict billing-stop admission, the
  optional 429 allowance, `dispatch_chat` defaults). A failed call changes
  nothing.
- Progress is an estimate with one explainable formula:
  `percent = floor(100 * completed / counted)`, null when nothing counts.
  Counted items are owner items, adopted suggestions, and Worker evidence that
  is not cancelled; every counted item weighs the same. Floor means 100 only
  when everything counted is done. The server computes it on every view.
- A goal can map to one Coordinator request (`link`: project, Coordinator,
  `request.delegation.attemptId`, the owner's request Turn). Its Workers are
  evidence items read fresh on every view, through one bounded source-cohort
  read (`ProjectDispatchRepository.request_rows`) and the owner's live-task
  set. No attempt, delivery or transcript is read: grant revoked → cancelled;
  reworked by a later Worker → cancelled (superseded); Coordinator review
  accept → completed; no execution → pending; live task → in progress
  (running); review rework or reject → in progress; otherwise in progress
  (awaiting review). A settled request is reported as such and is not turned
  into done items. The owner's status on a Worker item is stored as an
  override (`origin` evidence, `manual`); "follow Worker" removes it.
- Bounded load (DB measurement 2026-11-08: 249 concurrent calendar scans
  stalled the API host): one in-flight view load per owner and write
  generation on the server, and one in-flight goal load and one month load
  per page. The browser joins a running decomposition instead of starting
  another, and a load that started before a local change never overwrites that
  goal. Rendering the calendar never fetches. The route loads `lib.myday` with
  its module, so no request imports a module on the serving loop: a first
  import there stalled the API host for 5.9 s on 2026-11-09 (a DolphinFS stat
  in the pinned import source).

**Migration: archive.** The generated reports stay byte-identical under
`data/config/daily_reports/<owner>/` and render read-only as a past day's
recap beside that day's goal progress. Nothing is converted: every legacy
TODO of the owner was model-generated (203 reports, 1,137 TODOs, 0 manual on
2026-11-08), so converting them would have planted 1,137 stale proposals. The
report generator and its backfill built-in are unchanged; whether to retire
them is a separate owner decision.

**Alternatives rejected:** a goal per day copied forward (duplicates items and
loses their identity); one record per owner (unbounded growth and contention
on every click); a weighted formula giving partial credit for items in
progress (a tuned constant, not an explainable count); mirroring Workers into
stored items at link time (stale as the Coordinator keeps delegating);
per-Worker attempt and delivery reads (about two storage calls per Worker on
every open); writing the Coordinator task board (model-authored and owned by
the Coordinator; it is a summary, not evidence); converting the archive.

Bounds: goal text up to 1,000 characters; up to 128 items of 200 characters
per goal; about 200 KB per record at the worst case, 1–5 KB in practice.

## Failure semantics

- Invalid schedule/action: reject at creation/update.
- Claim conflict: another worker owns the occurrence; do not execute.
- Action failure: record typed terminal outcome and apply bounded retry policy.
- Process crash: lease/claim expires and a later poll safely resumes.
- Owner/resource missing: terminal non-retryable outcome unless the contract
  declares temporary unavailability. Optimizer verification failure rolls back/
  rejects the action and retains the proposal plus diagnostics.

## Invariants

- Sidecar state is authoritative; polling memory is a cache. Timer, occurrence,
  and report identities are idempotent. Owner/tenant identity is explicit from
  storage through execution; a missing owner or scope fails before a scheduler/
  optimizer storage call.
- Only registered actions run autonomously. Claims/leases have bounded lifetimes
  and recovery semantics. Retry loops are capped and never duplicate completed
  side effects. Optimizer writes are allow-listed, reversible, and post-verified.
- Notifications reflect stored outcomes; they do not define completion.
- My Day item state belongs to the owner: a manual status is never changed by
  regeneration or evidence; adopted, dismissed and completed items are never
  removed or recreated; pending suggestions never count toward progress.
- My Day check-offs are optimistic and adopt the server's goal view; on
  failure the board re-reads the server instead of keeping its guess.
- My Day cache keys require a resolved positive owner, cache bytes/entries are
  bounded, and the retained panel cannot recreate its own IndexedDB or timer.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Timer field/schedule | scheduler contract and timer CRUD | parser, Sidecar, resume |
| Poll/claim behavior | timer poll/state modules | multi-worker/idempotency |
| New scheduled action | action catalogue + focused service | owner, input bounds, retry |
| Daily report input | focused reader | owner isolation, period idempotency |
| My Day goal rule or record | `lib/myday/goals.py`, contract | carry-over, manual authority, idempotent regeneration, progress formula, restart |
| My Day request evidence | `lib/myday/evidence.py` | worker state mapping, one cohort read, unavailable evidence |
| My Day board | `features/myday/goal-board.ts` | one load per page, stale-load guard, joined decomposition, read-only recap |
| My Day cache/background | `features/myday/{report-cache,background-controller}.ts` | owner isolation, byte/entry ceilings, single probe/reminder, teardown |
| Optimizer signal | analyzer | proposal determinism |
| Auto-applied action | `actions/`, applier | write set, rollback, verification |

## Test map
```bash
pytest -q tests/scheduler/test_scheduler_tool_schema.py tests/llm_dispatch/test_timer_parse_failure.py \
  tests/llm_dispatch/test_timer_poll_agent_loop.py tests/scheduler/test_timer_resume_guardrails.py
pytest -q tests/scheduler/test_timer_dispatch_outcomes.py tests/scheduler/test_scheduler_process_runner.py \
  tests/scheduler/test_timer_check_now.py tests/scheduler/test_timer_continuation_policy.py
pytest -q tests/scheduler/test_scheduled_agent_condition_contract.py tests/scheduler/test_scheduler_predicate_promotion.py \
  tests/scheduler/test_proactive_status_budget.py
pytest -q tests/scheduler/test_scheduler_governance.py tests/scheduler/test_scheduler_exactly_once.py \
  tests/scheduler/test_router_pauses_backup_end_to_end.py tests/scheduler/test_commitment_wake_liveness.py
pytest -q tests/conversation/test_daily_report.py tests/daily_report/test_daily_report_storage.py \
  tests/startup/test_daily_report_startup_boundary.py
pytest -q tests/myday/ tests/api/test_api_v4_myday_goals.py
npx vitest run frontend/src/features/myday/
pytest -q tests/optimizer/test_optimizer.py tests/optimizer/test_optimizer_sidecar_storage.py \
  tests/startup/test_optimizer_startup_boundary.py tests/conversation/test_optional_llm_billing_admission.py
```


## Complete scheduled-task admission and dispatch

Problem: owner creation/migration stopped at 100 definitions, and the runtime read only a 1,000-task prefix. Decision: exact native counts observe integrity; SQL creation keeps identity locking without quota-only owner locking. Native count overflow fails before staging documents, claims or indexes. Internal owner/global list limit=0 reads every matching definition; positive limits are explicit projections without a separate 2,000-row ceiling. Native traversal uses bounded index windows in one snapshot and retains every OCC witness; built-in adoption traverses the complete owner catalog. Migration preserves its explicit cursor pages. Due claims remain atomic and owner-scoped, so the complete feed does not duplicate occurrences. Alternatives: larger caps or restoring only a prefix still strand accepted work; weakening identity/claim fences introduces duplicate effects. Gates: native scheduler demand/migration and semantic tests, SQL scheduler contract and due-feed reconciliation tests.

Problem: the 30-second tick ran command, python, prompt, optimizer and proactive-agent tasks inline (only four maintenance types had their own thread), so one slow task delayed every other due task, and because a cron expression matches only the current minute, occurrences that came due while the tick was blocked were lost. Decision: `_dispatch_claimed_task` runs every claimed occurrence on its task's own thread and returns; the tick only lists, claims and dispatches. Runs are single-flight per task id: an occurrence claimed while the same task still runs becomes one pending rerun (further claims coalesce into it) that starts when the run ends, so a task neither overlaps itself nor silently drops due work. Stop joins runs by its deadline and drops pending reruns. Threads follow claimed occurrences; there is no worker pool or concurrency ceiling. Alternatives rejected: a bounded pool reintroduces cross-task waiting; dropping the occurrence while the task runs (the old maintenance policy) loses claimed work; allowing self-overlap lets stateful tasks race themselves; queueing every missed occurrence grows without bound for a fast cron. Gate: `tests/scheduler/test_scheduler_due_tasks_run_off_tick.py`.

## Atomic execution settlement

**Problem:** terminal bookkeeping hydrated every owner schedule, calculated cron presentation and then updated rows selected by an earlier execution witness. **Decision:** `scheduler.task.settle_execution` atomically matches owner, agent type and exact execution task ID. Rust maintains an owner-scoped execution index with exact document verification; legacy owners backfill once in the same transaction. SQL uses an owner/execution partial index and conditional UPDATE. Receipts replay the original result, and stale execution callbacks change zero rows. One index entry per eligible definition plus one native owner marker is retained until membership changes or deletion; no worker, cache or admission quota is added. A 256-definition fixture retained 257 index entries totaling 14,387 encoded key/value bytes; page, blob and receipt overhead are excluded. Settlement first uses the read-only `scheduler.task.execution_lookup` port. A proven absence skips command/receipt creation; matching or uninitialized legacy state enters atomic settlement with its ordinary receipt/outbox lifecycle. An empty native owner is checked against count and created-index witnesses; a nonempty legacy owner never treats a missing execution index as absence. End-to-end performance remains pending. **Alternatives rejected:** hydrating schedules does work unrelated to settlement; an application reverse map loses restart authority; read-then-update permits a later execution to be overwritten. Verify scheduler execution-settlement port, storage receipt/owner tests and native scheduler execution tests, including legacy backfill, OCC conflict and reopen.
