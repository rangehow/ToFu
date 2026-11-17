# Routers: always-on, event-driven coordination

Responsibility: the Global Router and Project Router roles, forwarded human
requests, report relay, standing instructions, the long-lived context
contract that lets a router conversation run indefinitely, and the Router
Watchdog that monitors them. Channel ingress,
threads, subscriptions and the outbox are owned by [CHANNELS.md](CHANNELS.md).
Project workers, grants, delegation and reports remain owned by
[PROJECT_COORDINATION.md](PROJECT_COORDINATION.md); this document only adds the
router layer on top of them.

## Vocabulary

| Term | Meaning |
|---|---|
| **Router** | A long-lived, dispatch-only conversation that owns human-facing replies and coordination. It never executes concrete work itself. |
| **Project Router** | The existing bound Project Coordinator of one owner/project (`coordinator-*`). |
| **Global Router** | One per owner. The bound Coordinator of the owner's **router home** workspace, with the router addendum and router tools. Front door for Tofu and channels. |
| **Router home** | An owner-scoped Git repository created and owned by Tofu, used as the Global Router's project root (scratch worktrees for non-project work, project messages, dispatch records). |
| **Worker** | An ordinary, context-isolated conversation that executes one assignment. It has no human-interaction tools, but stays visible and the human may open and talk to it at any time. |
| **Forwarded request** | One genuine human message of the current Global Router request, delivered verbatim into a Project Router (`route_to_project`). |
| **Standing instruction** | Owner-authored text captured from a genuine human turn that lets the Global Router forward matching future channel events as work, without asking again. |
| **Report relay** | Project Router reports surfaced to the Global Router as passive notices. |
| **Lead** | The Global Router or a Project Router: the only conversations that message each other across projects (R13). |
| **Watchdog** | One per owner. The bound Coordinator of the `.tofu-watchdog` path inside the router home with the narrowest tool ceiling: it reads health, judges faults and Worker progress, and messages or notifies routers. It never executes, delegates, cancels or acts as the owner. |
| **Fault signal** | A durable, idempotent record of one infrastructure fault an existing owner observed (failed Turn, recovery failure, outbox send, channel state); a new one wakes the Watchdog. |

The owner never sees these internal names (R8): the Global Router is the
**main coordinator** (总负责人), a Project Router is the **project
coordinator** (项目负责人), a Worker is a **worker chat** (执行会话) and the
Watchdog is **health checks** (故障巡检).

Topology:

```
            Tofu UI ─┐                     ┌─ 大象 command chat (owner)
                     ▼                     ▼
               ┌──────────── Global Router ────────────┐   ◀─ channel events (CHANNELS.md)
               │ dispatch-only, owns replies/outbox     │   ─▶ outbox replies
               └──────┬────────────────┬────────────────┘
          route_to_project      delegate_project_work (router home)
                      ▼                ▼
              Project Router(s)    Workers (home worktrees)
                      │ delegate_project_work
                      ▼
                 Workers (project worktrees)

  Watchdog (router home/.tofu-watchdog) ◀─ fault signals, scheduled reviews, re-checks
      │ watchdog_message ─▶ any Router or Worker mailbox (nonhuman context)
      └ watchdog_notify_router ─▶ Global Router (peer input)

  Global Router ◀─ send_project_message ─▶ Project Routers ◀─▶ each other
      (leads only, nonhuman context, list_project_leads; R13)
```

## Decisions

### R1. A router is a Coordinator; the Global Router is the home Coordinator

**Problem:** the article-style Router/Worker split needs a resident, dispatch-only
session with context-isolated workers, durable reports, a progress view for the
human, questions that proceed on a stated default, result-driven continuation
and enforced tool policy. Tofu already has all of these in the Project
Coordinator, but only per project.

**Decision:** the Global Router is the bound Coordinator of the owner's router
home (`lib/router/home.py`), selected by the ordinary
`ProjectCoordinationRepository` binding on that path. No new binding namespace,
model loop, task state machine or presentation state machine exists. The router
role is the coordinator role plus a router addendum; its tool ceiling is
`ROUTER_TOOLS` (`COORDINATOR_TOOLS` plus the routing and channel tools), enforced by the same
`coordinator_call_rejection` / `filter_coordinator_tools` boundary. Non-project
work (summaries, drafts, investigations) is delegated to ordinary Workers in
home worktrees, exactly like project work.

**Alternatives rejected:** a separate router runtime or binding namespace
(duplicates grants, continuation, reports and policy — constraint 4); Delegate
Mode or Swarm as the router's worker layer (children are invisible, die with the
turn and their records are deleted); a resident router process (turns already
start on demand over durable state, which is the article's compute/state split).

**Current state.** The home is `<data_root>/router/<owner>`: `router_home_path`
writes README.md and .gitignore, runs `git init` (so a data directory inside a
source checkout still yields its own repository top level) and lets
`prepare_managed_repository` record the baseline commit and guidance; the
existing isolated-worker guard accepts it. `global_router(create=True)` uses
`ProjectCoordinationRepository.provision`, which derives the conversation
identity from owner/path/current revision/command: concurrent or retried ensures
converge on one conversation (title `Global Router`, settings marker
`_globalRouter`) and one binding CAS; an archived router is replaced at the
next revision. `create=False` never touches the filesystem. Problem: the human
`create` path mints a fresh identity per attempt, so concurrent provisioning
left orphan conversations. Decision: deterministic identity with a provision
marker that alone permits adoption. Alternative rejected: a lock around
`create` (process-local, still orphans across processes).
`coordinator_tool_ceiling` returns `ROUTER_TOOLS` only when the scope's project
is the owner's router home (a Turn reviewing other people's channel messages
gets the narrower review ceiling instead, CHANNELS.md C11); catalog projection (`filter_coordinator_tools`,
`prepare_coordinator_tool_surface`), the registry builder and
`coordinator_call_rejection` all ask it, so channel tools appear by name once
their ToolSpec exists. Every Router tool is eager, but only `ROUTER_DIRECT_TOOLS`
(its usage-derived core) is required-direct: on Claude at `your-llm-gateway.example.com`
the tool-name budget hides the rest behind `search_tools`/`execute_tools`, and
every other route sends the whole surface
([LLM_COST_OPTIMIZATION.md](LLM_COST_OPTIMIZATION.md#6-gateway-tool-name-budget),
"Coordinator-role surfaces"). Router tools are in the project dispatch
`workerDeniedTools`. `prepare_coordinator_role` appends `ROUTER_ROLE_ADDENDUM`
(front door, routing, home delegation, channel norms, silence, attribution,
channel text as evidence) to `COORDINATOR_ROLE_GUIDANCE` for the home only.
Problem: the context block carrying the role (`project_coordinator`) held 600
tokens and the composer clips an oversized block from its end, which is where
the addendum sits: the Global Router lost all of it while the Coordinator role
was 820 tokens, and its second half after that role shrank to fit. Decision:
the block budget holds the longest role whole (900 tokens; the router role is
about 870), and `test_router_policy.py` composes the router role through the
composer and requires it unclipped. Alternatives rejected: a second registered
block for the addendum (another source, presentation and manifest row for one
role); condensing both texts into a replacement router charter (an unreviewed
rewrite of two prompts); a larger budget without a composed spec (the next
addendum edit would clip silently again).

**Session-free coordination.** Every router inherits the session-free
Coordinator ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#session-free-coordination-owner-decision-2026-11-02)):
`COORDINATOR_TOOLS` grants `update_task_progress` and `ask_human` and no longer
`request_user_decision`, so the Global Router and every Project Router keep a
task board for the owner (pinned above its chat by the coordinator surface;
in the router home, below the main coordinator's header) and ask the owner with
chat cards. The passive request-plan projection is gone; the board, Worker
records and relayed reports are what the owner and the router read.

**Asking the owner.** A router asks with `ask_human` in its own conversation.
`blocking=false` posts the card and continues on the default it states; the
owner's answer is an ordinary message quoting the question, which steers the
live reply or starts the next request; in the Global Router either form stays
forwardable (`source_steer_id` or `source_turn_id`, R2). The Global Router,
every Project Router and the Watchdog are bound Coordinators, so their chats
steer a live reply without the steer/queue prompt (PROJECT_COORDINATION.md
"Session-free coordination"). `blocking=true` waits; a
human-authored message steered into the reply ends that wait as the answer.
Channel batches, relayed reports and other nonhuman steers share that lane but
never do (they are evidence, R2; the steer seam records an author only for
human input), so they wait at the round boundary. A Project Router's question,
blocking or not, also reaches the owner's Global Router as one passive notice
(R3), so an owner who talks only to the Global Router sees it; the router puts
it to the owner where they talk to it and forwards their answer verbatim with
`route_to_project`. Such a forward is the owner's own message only when
`require_human_turn` verifies it (R2): steered into the waiting reply, it ends
the wait exactly like the owner typing in the project chat, while an
unverifiable forward waits like any nonhuman steer. Cards render only in Tofu:
on 大象 the router sends the question to the owner's command chat with
`channel_send` (R3), and its answer to an owner command-chat message returns
there by C5 auto-reply, never as a card.

### R2. Forwarded human requests carry human authority only verbatim

**Problem:** the Global Router must hand project work to a Project Router, but
Project Router grants require a genuine human source Turn and synthetic input
can never authorize Workers.

**Decision:** `route_to_project(project_path, source_turn_id | source_steer_id,
note?)` forwards one human message of the router's *current* request (its own
Turn, or a message the owner steered into the running reply) into the Project Router of that
project (creating the binding and its empty conversation when absent). The
forwarded input is byte-identical to the human text and is submitted through the
ordinary turn command (`dispatch_scheduled_turn`, steer when busy) with
`origin = {initiator: 'human', forwardedFrom: {conversationId, turnId}}`.
`require_human_turn` admits a forwarded origin only when the source Turn is a
genuine human Turn of the same owner in the owner's bound Global Router and the
text is identical; it rejects every other cross-conversation origin as before.
The router's optional `note` is nonhuman context of that same request and never
becomes authority. Forwarding is idempotent per (source turn, project).

**Alternatives rejected:** cross-conversation grant sources (every grant reader
would change); synthetic forwards (cannot delegate, so useless); letting the
Global Router delegate inside other projects directly (two dispatchers per
project break the one-binding epoch fences); router paraphrases as authority
(prompt injection could mint instructions).

**Current state.** `TurnOrigin.forwardedFrom {conversationId, turnId,
instructionId?}` is part of `contracts/conversation_sync_v3.yaml` and survives
projection normalization only when well formed. `route_to_project` resolves the
current request's human Turn exactly as delegation does (`source_for_task`), so
synthetic input cannot forward; it forwards that Turn's authored text
(`originalContent` before any input translation). When that request is an owner
message steered into a reply to nonhuman input, the reply's own text is the
router's, so `source_turn_id` refuses and names the `source_steer_id` that
forwards it. A forward steered into a busy Project Router reply to nonhuman
input (a notice, a relay) is that reply's request once its model has received it
(PROJECT_COORDINATION.md, "The owner's message steered into a Turn without a
request"). The command id hashes
(router conversation, source, project path). A new Project Router is provisioned
when the project has none (deterministic identity, no folder; it inherits the
router request's composer capability switches so its Workers keep them). The
note and an instruction's trigger are nonhuman evidence **inside the same
forwarded input** ("Evidence rides the same request" below), never a second
input. `require_human_turn(turn, user_id=...)` admits a
forwarded origin only through `verify_forwarded_turn`: the owner's currently
bound router, an existing genuine human source Turn that was not itself
forwarded, a different target conversation, identical authored text and, for
instructions, a matching record; a call without the owner refuses.
`ProjectDispatchRepository.validate_human_source` passes its owner, so every
grant fence repeats this check.

**Evidence rides the same request.** Problem: a forward used to be delivered as
TWO inputs — the owner text, then a peer-initiated `:context` message carrying
the note and the instruction's trigger. Each input was delivered independently
(started, steered or queued). Whenever the evidence missed the steer window it
was durably queued and later activated as its OWN Coordinator request: a
synthetic, peer-initiated request that named the instruction and re-attempted
its work, so `source_for_task` refused it (`Synthetic authorization origin
denied`) and the Project Router stalled asking the owner for "继续" (observed
2026-11-09 on the parallel project's Project Router: four queued evidence
requests, every `delegate_project_work` and even the read-only
`get_project_workers` refused, while the original live reply that had actually
received the owner text kept working). The read no longer refuses: a Turn whose
input is nonhuman reads its binding's project Workers without a current request
(PROJECT_COORDINATION.md, `get_project_workers`); delegation and finish still do.

Decision: one forward is ONE Coordinator request. Its input `content` is the
owner's verbatim text followed by the labelled nonhuman evidence block (the
note, the instruction id and its triggering event); its `originalContent` is
the owner's verbatim text alone, which is exactly what `authored_text` returns
and `verify_forwarded_turn` compares. The owner words the authority is judged
by are unchanged, no human message is synthesized and no gate is bypassed; the
evidence is labelled nonhuman in the same model view, and it can no longer
become a standalone request on any delivery path (started, steered or queued).

Alternatives rejected: delivering the evidence through the R9 owner delegation
(the delegation is a UI grant for lifecycle/session control, not a carrier for
forwarded context, and it must not mint Worker authority); letting a verified
`forwardedFrom.instructionId` source pass the synthetic-input gate in
`source_for_task` (re-derives authority at read time, needs a per-application
link and a revocation model, and still leaves a duplicate synthetic request
running the same work); dropping the evidence on the floor (the trigger and the
note are how the Project Router knows why it was woken); merging the two
queue rows at activation time (changes generic queue semantics for one
caller); a mailbox-only evidence input (either it never reaches the model, or
it still starts an unauthorized request).

**Steered owner messages.** Problem: a message the owner steers into the
router's running reply has no Turn of its own, and
the steer lane kept only a 1,200-character display preview, so it could not be
forwarded verbatim. Decision: the steer itself carries a durable author.
`turn.steer.commit` (SQL and native, one transaction with the user-steer
record) stores `TurnSteerAuthor {actor, text, origin?}`
(`contracts/conversation_sync_v3.yaml`) for human-initiated input only, computed
at the single steer seam (`command_service._deliver_while_running` via
`turn_initiation.steer_author`): the input actor, the complete authored text
(`originalContent` before translation) and the origin normalized exactly like a
Turn's. Nonhuman, program-synthetic and text-less steers (channel event
batches, report relays, forward context) record none. Executor merges and client
whole-projection edits keep the stored value and cannot add one
(`_merge_user_steer_records`, `projection_with_committed_steer_authors`). The
steer is named by its command id inside the reply Turn: `forwardedFrom =
{conversationId, turnId: <reply Turn>, steerId}`, idempotent per (router
conversation, `{turnId, steerId}`, project). `route_to_project(source_steer_id)`
accepts only a steer of the task's own executing reply (current binding, the
task's live attempt, not stopped), whatever started that reply, because the
steer's author is the authority. `turn_initiation.steered_input_turn` turns the
record into a Turn-shaped view so `require_human_turn` and `verify_forwarded_turn`
apply the same checks as for a Turn: owner's bound router, genuine human, not
itself forwarded, identical text, another conversation; a queued steer (missed
the reply and became the next request), a steer on a human Turn, and `steerId`
with `instructionId` are refused. In the router's model view
(`orchestrator._swarm_inbox`), `owner_steer_carriers` prefixes each such owner
message with `[Owner message sent while you were replying · steer_id=<id>]`; the
role addendum tells the model to forward it with `source_steer_id`.
Alternatives rejected: turning every steer into its own Turn (changes transcript
and delivery semantics everywhere), a separate steer-author store (non-atomic
with the steer and a second authority), matching the steered text supplied by
the model (the model would have to reproduce the owner's bytes), and labelling
steers in every conversation (changes the prompt of every chat for a router-only
tool). Standing instructions are still created only from a whole Turn.

### R3. Project reports and questions relay to the Global Router

**Problem:** the owner talks to one agent; results produced by a Project Router
must reach the owner's current channel without the owner watching every
project conversation.

**Decision:** every published Project Router report (`report_to_user`) also
records a passive notice for the owner's Global Router (project, report kind,
report Turn ID, short body) and wakes it with a nonhuman initiator. The Global
Router decides whether and where to tell the owner (channel norms). The project
report Turn remains the canonical record; the notice is a pointer.

**Alternatives rejected:** polling project conversations (paid polling); routing
every report to 大象 directly (no judgment, violates low-disturbance norms).

**Current state.** `CoordinatorReports._deliver` calls `relay_project_report`
after the report Turn and attention pointer commit and before the publication
intent retires, so boot recovery replays a relay interrupted by a crash. The
notice is one `dispatch_scheduled_turn` input stamped `peer` with
`origin.sourceConversationId` = Project Router; it inlines project path,
Project Router conversation, report kind, report Turn ID, title and the full
report message (already bounded by the report contract). The command id hashes
(owner, Project Router conversation, report Turn), so replays deliver once.
Reports of the router home, and owners without an available Global Router,
relay nothing: relaying never creates a router. A relay failure is logged and
never fails publication. A relayed notice, like a colleague's channel event, is
a nonhuman request: it opens no request account (`bind_source_account` returns
nothing for a `SyntheticSource`), so an idle router starts its turn normally,
and delegation from that request is refused as before. Problem: the account
binding at task start treated every non-human source as an authority failure,
so an idle router could not start a turn for a relay or channel event at all
(steering into a live turn still worked). Decision: a typed `SyntheticSource`
refusal that the account binding, and only it, maps to "no account".
Alternative rejected: skipping the binding for routers (a human request would
lose its usage account and fences).

**Reviewed notices can conclude without an answer.** An exact `[SILENT]` is a
declared outcome only for a bound Global Router Turn whose parent is a
nonhuman notice of any kind (`turn_initiation.is_nonhuman_notice_turn`):
a channel event, a project report or question relay, a scheduled wake such
as the keepalive, a timer or any other nonhuman initiator. One quiet-terminal
contract covers every wake kind; human and unknown initiators never qualify.
Problem: the gate admitted only channel/peer parents while the Router prompt
prescribes `[SILENT]` for any event that needs nothing, so every quiet
`*/30` keepalive (a `proactive` scheduled wake) was refused, burned both
terminal-recovery attempts and settled `abnormal_stop` (empty_stop).
Decision: the parent test is "nonhuman", shared with the empty_stop resume
below. Alternative rejected: listing wake initiators one by one (the next
wake kind would fail the same way). The engine checks the durable parent and every
steer of this Turn by its recorded author: committed, pending in the inbox,
or drained into this request before its consumption is confirmed. Model
history is never steer evidence: it keeps earlier Turns' labelled owner-steer
carriers and compaction summaries quote them. A genuine human request,
missing or failed evidence, an unknown initiator or any human steer of this
Turn refuses the declaration and uses ordinary answer recovery. Ordinary
prose that mentions the token is never rewritten. Authorized silence publishes a
`deliberate-silence` system note, retains the provider declaration for replay
and has no deliverable text or automatic completion message. It cannot bypass
reply policy, drafts, approval or a required owner decision. Python and Native
stream/terminal projections share this policy; incomplete split markers flush
as ordinary text at structural boundaries, and discarded attempts drop their
uncommitted candidates. The control token never reaches public text: a
tool round's `[SILENT]` lives only in its private provider replay
(`assistantModelContent`), stored answers, segments and channel replies strip
it, and a refused declaration whose only text was the token is an empty
response that Python recovers as an empty stop and Native settles as
`abnormal_stop`, never a raw provider instruction shown to people. The native
Turn projection takes its system-note kinds from the contract (generated
`generated_system_note_vocabulary.rs`). Automatic review failures/interruption
send an out-of-band command-chat warning naming the failure cause; successful
silence does not suppress that failure. A review that exhausted the
terminal-response budget (empty_stop) is not stranded on that warning: the
existing resume owner (`lib/conversations/restart_resume.py`) admits an
in-place continue/checkpoint_resume only for that class — durable nonhuman
parent, no current-Turn owner steer, no answer or silence note, and an
existing resume option — and the terminal observer runs it as a bounded
follow-up with command id `notice-review-resume:<failed Attempt>`, preserving
the Turn's evidence and completed tool results and resuming once per failed
Attempt. The warning then names the real next action (continued in place when
accepted, the manual resend otherwise); the notice is never marked read or
reviewed, and genuine provider errors and owner-authored Turns are untouched. A human command steered into a notice supplies the reply origin,
so its final answer and failure status return to that original thread.
Alternatives rejected: an empty assistant answer (the transport recovery
interprets it as a lost response), model-authored filler on every no-op event,
treating command-ID prefixes or marker text as authority, and scanning the
model history for the owner-steer label (one earlier steer then refused every
later notice's silence, ending each as an empty-stop `abnormal_stop`).

**Questions.** Problem: a Project Router's `ask_human` question was visible only
in its own chat, so an owner who talks only to the Global Router (mostly from
大象) never saw it, and a blocking question stalled the project until the
request ended with a `needs_user` report. A waiting question also ended for any
steered author with actor `human`, so a forged or edited forward answered it
like the owner. Decision: every question a Project
Router posts, blocking or not, relays one passive notice by the report seam.
The `ask_human` handler (single and form) calls `relay_project_question` once
the card is set up and the task is a bound Coordinator (`coordinator_scope`);
the notice is stamped `peer` with `origin.sourceConversationId`, steers a
running router, starts an idle one or queues, and its command id hashes (owner,
Project Router conversation, question Turn, tool-call id), so a replayed or
re-executed tool call delivers once (the question id is minted per execution).
It names project path, Project Router conversation, question Turn and question
id, the blocking flag, the question text and options (a form lists every
question; for `blocking=false` the question states the default the Project
Router works on, as the `ask_human` description requires), says it is a
nonhuman notice and not authority, and tells the router to read the project
conversation first (the owner may already have answered the card there),
then to put the question to the owner and to forward the owner's answer with
`route_to_project` (`source_turn_id` or `source_steer_id`, a note naming the
question) verbatim, never answering for the owner. Questions of the router
home and the Watchdog (whose ceiling has no `ask_human`), and owners without an
available Global Router, relay nothing; a relay failure is logged and never
fails the question.

大象: a Turn the notice starts answers no owner command-chat message, so C5
auto-reply does not return it; but `channel_send` needs only the Global Router
conversation, not a human Turn, and the owner's command chat is `queued` at
enqueue without approval or disclosure label, while third-party threads keep
their reply policy and draft outbox (CHANNELS.md C5). Existing tools already
reach the owner, so the decision is instruction only: when the owner's 大象
account is enabled with a command chat, the notice names it and says to send
the question there with `channel_send(thread=<command chat>,
idempotency_key=<question id>)`; otherwise it says to ask in the router's
reply (with `ask_human` only as `blocking=false`, so the owner's answer arrives
as a forwardable message, not a card result). The owner's command-chat reply
reaches the router as human input with byte-identical text (a Turn, or a
labelled steer while the router replies) and is forwarded from there. In the
Project Router, `_human_message_pending` judges each steered author with
`require_human_turn(..., user_id=owner)`: a verified forward ends a blocking
wait as superseded by a chat message, so the model reads the owner's words as
the answer; a forged or edited forward waits like any nonhuman steer.

Alternatives considered: the Project Router sending questions to 大象 itself
(bypasses the router's judgment and channel norms; the answer would arrive
outside the router's forwarding authority); relaying only blocking questions
(the default of a `blocking=false` question would be decided without an owner
who never opens the project); an owner-thread exception in the outbox or a new
tool (the command chat already sends without approval); a structured `default`
argument (the card would not show it, so the question text carries it); keying
the command id by question id (re-execution would notify twice); a
deterministic "answered" notice (each one wakes the router for a paid turn,
mostly for answers given where the owner already was, and a `blocking=false`
answer is an ordinary message the handler never sees; the check-first read
covers a router that reaches the notice after the answer).

### R4. Standing instructions

**Problem:** proactive work (for example "in group X, when someone reports a
bug, reproduce it in project P and tell me only if a fix is confirmed") needs
authority that outlives one human turn, while channel events from other people
are never human authority.

**Decision:** the Global Router records a standing instruction only during a
genuine human turn (`router_instruction_create`); the record freezes the
verbatim owner text, its source Turn, an optional thread scope and target
project. The owner lists, pauses, resumes or cancels instructions in Tofu. When a channel
event matches (router judgment), `route_to_project(..., instruction_id)`
forwards the frozen instruction text verbatim plus the event as nonhuman
evidence; the instruction's source Turn is the authority. Every application is
recorded with the triggering event ID. The thread scope may be any channel
thread, including a GitHub issue or pull request: GitHub comments, the
owner's own included, are only evidence (CHANNELS.md C8), so an instruction
scoped to `github:<owner>/<repo>#<number>` is how the owner lets a later pull
request comment change work that an IM message started.

**Alternatives rejected:** treating router judgment as authority; deterministic
automation triggers only (cannot express "someone reports a bug"); renewing a
grant silently. Convergence with `project_dispatch/responsibilities.py`
(deterministic automation triggers, project scope) is a future unification, not
a reason to block the router path.

**Current state.** `RouterInstructions` stores one record per instruction
(`router.instruction.v1`) and one per application
(`router.instruction.application.v1`) in portable record CAS, so history has no
lifetime cap. The frozen text is the whole authored text of the source human
Turn (byte-identical, so the same forwarded-origin check verifies it); the
router adds a presentation label, an optional thread key and an optional target
project. Creating and cancelling from the router require the current request to
be a genuine human Turn; applying does not (the trigger is usually a channel
event), but needs an active instruction of the current router binding whose
source Turn is unchanged, an `event_id` or trigger `source_turn_id`, and the
instruction's project when it names one; an instruction that names no project
applies only to projects that already have a Project Router, so channel text
cannot steer it into creating one. Applications are idempotent per
(instruction, trigger, project). The owner lists, pauses, resumes and cancels
instructions over HTTP and in the router header (R5 UI); pause and resume are
owner-only (no model tool). States are `active`, `paused` (with `pausedAt`, null
after a resume) and the final `cancelled`; transitions are idempotent CAS writes
and a cancelled instruction can be neither paused nor resumed. Applying a paused
instruction is refused with a message telling the router to wait for the owner;
lists show active and paused instructions by default. Pausing and cancellation
stop future applications only; work already forwarded keeps the authority it was
admitted with (`verify_forwarded_turn` checks the record's text, not its state).

### R5. Router and Worker models are chosen separately

**Problem:** Workers inherit the Coordinator's model, so choosing a light,
fast router model downgrades all execution.

**Decision:** the coordinator conversation setting `workerModel` (validated by
the ordinary route compiler) selects the model for Workers it delegates;
absent, Workers use the owner's default model, not the router's. Models never
choose models.

**Current state.** `workerModel` is a server-authored key of
`contracts/conversation_settings_v1.json`, written only by
`ProjectCoordinationRepository.set_worker_model` (HTTP
`POST /api/v4/router/worker-model`) after `admit_worker_model` resolves it in
the owner's routing document; `null` clears it. `GET
/api/v4/router/worker-model?path=` reads it for any bound router. The owner
sees and sets it in the browser (`frontend/src/features/router-worker-model.ts`):
one compact labelled select on a single row (its closed value is the current
model, its tooltip the explanation) whose choices are the composer model
picker's catalog and visibility policy (hidden models, chat capability,
display-name order and labels), never a second model list; its first option,
the owner's default model, sends `null`, a refused choice keeps the stored
value with the server's message, and a failed read shows its error beside a
retry. The Global Router's control is in its header;
every other Project Router's leads the footer of the task board on its
coordinator conversation surface, beside 历史任务 ("Task history") rather than on a line
of its own (the board starts folded, so the control shows once it is opened),
where it stays attached across repaints and reads once per visit; the Watchdog
conversation has none. A
conversation is the Global Router when its project path is the router home the
Global Router read last reported (the `_globalRouter` setting is a server-side
presentation marker the settings contract does not project to the browser).
`CoordinatorWorkerPolicy`
separates `model` (Workers) from `coordinator_model` (the Coordinator's own
continuations). Tool assembly never reads storage for it: the first delegation
of a request resolves `resolve_worker_model` (setting, else the owner's default
model, else an explicit refusal) and pins it; continuation controls record
`workerModel` (grants issued earlier, without it, keep their single model). The
owner default is the server's configured default model today
(TODO(enterprise): a per-owner default).

**Changing the model of an existing session (owner decision 2026-11-09).**
Problem: a provider refusal (for example an Anthropic content filter) froze a
long Coordinator/Worker supervision session on its original model; the only
answer was a clean replacement session, losing its context, grant and checkout.
Decision: the owner, or the Global Router under the R9 delegation, changes the
model of an existing session with `control_conversation action=set_model`
(`lib/conversations/session_model.py`, `lib/conversations/session_control.py`)
or over HTTP with `POST /api/v4/router/session-model`
(`owner_set_session_model`). The choice is the server-authored
conversation setting `modelOverride`
(`contracts/conversation_settings_v1.json`), written through the ordinary
settings CAS after the same route admission as `workerModel`
(`admit_worker_model`), with the session's accepted execution tenant fenced
exactly as Continue fences it. Dispatch admission resolves it for every NEW
accepted attempt (`TrustedDispatch.capture`), for a Coordinator follow-up and
for continuations; an in-flight call is never rewritten. `_restored_identity`
admits a carrier's model only from durable evidence: the grant's model, the
session's current choice (what acceptance captured), or the model its bound
attempt's durable accepted config names, so an attempt accepted under an
earlier choice keeps its authority after the owner chooses again, while a
carrier rewritten to any other model loses its authority. Storage is read only
when the model differs from the grant's. The grant, conversation, tool
checkpoint, work id and authorization scope are unchanged, and no permission
widens: only the model changes. A Coordinator continuation inherits its human
request's route fields only when they name its effective model
(`request_selection`), so a changed model resolves its own route. Every change
writes one `session_model_changed` audit record with the reason and the
idempotency key. The Global Router acting under the owner's grant takes R9's
delegated session control (`delegated_conversation_control`, scope
`conversation_control`): the grant, the owner trigger and one audit record whose
target names the chosen model precede the same command. New Workers and
Coordinator continuations resolve the owner's CURRENT `workerModel` at
delegation instead of replaying the value the request's first grant pinned, so
one owner setting change re-routes the request's later Workers too; a session
override is deliberately per-session and never re-routes unrelated work.
Alternatives rejected: mutating the assignment request's model (immutable
grant evidence, and it would rewrite in-flight attempts); a per-request model
override (it would let a synthetic continuation route itself); a new
model-change tool (the session command already owns this authority); a
separate delegated-action gate for the Router's change (the session command's
R9 branch already resolves the trigger inside `owner_delegation` and records a
target the `ownerDelegatedAction` contract admits, which the owner's audit view
must always read); stamping
the accepted model into the attempt config (the live carrier would vouch for
itself, so rewriting both fields would pass the fence). This is
legitimate provider/model routing, never filter evasion: no tool output or
provider refusal is rewritten, and the owner can always return to the session's
original model with `model=null`.

**A Router without a model choice.** Problem: a Project Router made by `create`
or `provision` stores no model until the owner sends from its composer. A turn
the server started there (a forwarded request, a timer) resolved its config from
those settings alone and ran with an empty model, which the shared dispatcher
answers with any free route (2026-11-06: the autoharness Project Router ran on
GLM-5.3). Tool assembly then issued the Worker ceiling without a Coordinator
model, and `policy_for_task` refused every delegation with an error that blamed
tool assembly and advised retrying the same key. Decision: `build_task_config`
(`lib/scheduler/_shared.py`) gives a conversation without a model choice (no
name and no `modelRef`, the rule `resolve_conv_settings` applies) the owner's
default model as `resolve_conv_config`'s `serverModel`, as a fresh composer
would, the same fallback Workers use. An issued policy without a Coordinator
model is still refused, as `CoordinatorModelMissing`: effect-free
(`execution_started: false`), with a recovery that reports the remedy instead of
retrying. Alternatives rejected: writing a model into the settings at
`create`/`provision` (a server-authored composer choice the owner never made,
and existing Routers stay broken until migrated); forwarding with the Global
Router's model (router and project models are chosen separately, and a light
router model would silently become the project's); recording the routed model
after the fact (the Coordinator would still run on an arbitrary route); relaxing
the refusal (continuations would replay an empty model).

### R6. Long-lived context: a router conversation runs indefinitely

**Problem:** a router lives for months. Automatic L2 compaction already bounds
the model view per request and each completed turn publishes a durable
continuation checkpoint, so manual compaction was never the long-term
mechanism. Several ceilings still made long-lived conversations degrade.

**Decision:** each ceiling below is removed structurally; none is raised.

| Ceiling | Decision | Owner |
|---|---|---|
| Continuation proof limited to 10,000 Turns (`count < 10000`, `MAX_LIST_ROWS` on the prefix, `MAX_SCAN_PAGES`) | Frontier count is a plain counter; cold scans stream the digest through all headers; only the uncovered suffix is materialized and paged | [CONTEXT_COMPACTION.md](CONTEXT_COMPACTION.md), TofuDB `turn/model_context` |
| Turns with a human steer never publish a checkpoint | Steered turns publish their verified model view | `model_replay.py` |
| Manual compaction refused for coordinator and every project conversation | Protected conversations schedule a non-destructive working-view compaction applied at the next model call; canonical Turns are never deleted | `compaction_policy.py`, compaction pipeline |
| A policy change discards the checkpoint and rebuilds the whole history | Reuse the previous checkpoint's portable text; rebuild only the uncovered suffix | `model_replay.py` |
| A conversation cannot recall its own out-of-view history | `get_conversation` reads the current conversation's turns; summary carriers name the covered range and recall cursor | `conv_ref`, L2 carrier |
| Project mailbox holds 128 messages per recipient for its lifetime | Segmented history without a lifetime cap | [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md) |
| Workers cannot purchase auxiliary summaries | Workers summarize under their own grant's existing ExecutionBudget | `_summary_accounting.py` |

### R7. The Router Watchdog is an independent monitor session

**Problem:** routers and Workers fail in ways nobody watches. A Worker's turn
dies on a provider error, a Project Router's own turn fails, dispatch recovery
cannot retry a grant, the 大象 bridge disconnects or a reply sits in the outbox
as `failed`/`unknown`; a Worker loops, stalls or drifts from its assignment.
The Global Router wakes only on human input, channel events and reports, and
must not fill its long-lived context with routine health; the owner learns of
a fault only by opening each conversation.

**Decision:** the Watchdog is the ordinary bound Coordinator of one more
owner-scoped path, `<router home>/.tofu-watchdog` (ignored by the home's
`.tofu*/` rule), provisioned like the Global Router (deterministic identity,
title `Watchdog`, settings marker `_routerWatchdog`). It reuses the binding,
role, tool-ceiling and turn machinery: `coordinator_tool_ceiling` returns the
narrowest ceiling, `WATCHDOG_TOOLS` (its `watchdog_*` tools plus
`list_conversations`, `get_conversation`, `search_tools`, `execute_tools`, the
read-only owner-memory lookup `search_memories` and the
`read_tool_artifact`/`search_tool_artifact` readers that continue a partial
result it already received; no
`update_task_progress` or `ask_human`: it owns no tasks and reaches the owner
through the Global Router or its own chat), and
`prepare_coordinator_role` replaces the delegation charter with
`WATCHDOG_ROLE`. It is driven by timed and event messages, all nonhuman input
through `dispatch_scheduled_turn(..., inject_mode='steer')`:

- **Fault signals.** Existing owners report faults they already observe; each
  becomes one `watchdogSignal` record keyed by a deterministic identity in
  portable record CAS, so replays never duplicate a signal and a resolved one
  never reopens. A new signal wakes the Watchdog with the `peer` initiator and
  command id `router-watchdog-signal:<signalId>`. The signal is the authority,
  the wake a hint. Sources: failed Turns of router-managed conversations,
  dispatch recovery, outbox sends and channel accounts.
  (`patrol`) An off-host liveness patrol of the owner's compute account
  ([COMPUTE_PATROL.md](COMPUTE_PATROL.md)) cannot reach Tofu, so the
  periodic review imports its published flags before it builds the snapshot.
  The studio's stall dialogue opens the Watchdog through the same ensure as the
  sidebar entry.
- **Periodic review.** An owner-visible keyed scheduler task (type
  `router_watchdog_review`). Its run builds the deterministic health snapshot
  and wakes the Watchdog (`proactive`, one command id per task and minute)
  only when something needs judgment.
- **Re-checks.** The Watchdog arms one-shot `once:` scheduler tasks for
  intermittent faults and keeps re-arming until recovery; a re-check always
  wakes it.

Interventions use only existing authority-safe paths: a project-mailbox message
to any Router or Worker of the owner (nonhuman context at the recipient's next
step), or one peer notice to the Global Router, which decides whether, when and
where the owner hears about it. The Watchdog never executes, delegates, cancels,
holds a grant or speaks as the owner; correction, re-delegation and
cancellation stay with each Worker's Coordinator and the human. Health checks
are on by default (below); the owner opens the Watchdog to ask it questions
like any conversation from the main coordinator's header (R8), and changes or
stops its periodic review by asking it.

**Alternatives rejected:** monitoring inside the Global Router (routine health
floods its indefinite context and a failing router cannot watch itself); a
separate monitor runtime, daemon or polling loop (a second model loop and task
state machine, constraint 4); fixed-interval model polling regardless of state
(paid turns while idle, so idle reviews end without a model turn); letting the
Watchdog cancel or re-delegate Workers (it would need grants, and synthetic
input never gains human authority); a Watchdog-private timer or backoff (a new
interval knob; the owner-visible scheduler is the one timer).

**On by default (owner decision 2026-11-17).** Problem: health checks stayed
off until the owner found their setting, turned them on and then enabled the
periodic review with a cron expression, so faults went unwatched in the
meantime. The owner asked for them on by default, with nothing to configure
and the permission they need granted, and for fewer switches in the main
coordinator's header ("Don't make me configure it; just enable it and grant
this permission"). Decision: health checks come with the main coordinator.
`ensure_health_checks` (`lib/router/watchdog/review.py`) provisions the
Watchdog and, for an owner whose periodic review was never installed, installs
it enabled at the contract cadence under the Watchdog's least-privilege
scheduling identity (`watchdog_principal`, scope `agents:scheduler`): that is
the permission the owner granted, so neither the owner nor the calling route
needs the scheduler scope. It runs when the main coordinator is ensured
(`POST /api/v4/router`; a failure there is logged and never keeps the owner
out of the main coordinator, and the next ensure retries), on
`POST /api/v4/router/watchdog`, and at scheduler boot for the process owner
whose main coordinator exists, so an existing owner needs no click either. An
installed review row is the owner's choice: one turned off or given another
cadence (by asking the Watchdog, through the API or in the scheduler list)
keeps that choice through every later ensure, which writes nothing once the
row exists (and installs with `reconcile_enabled=False`). The header shows
health checks as a fold with their state, one sentence on what they do and a
way into the Watchdog chat; it has no switch or cron field. Alternatives
rejected: keeping the opt-in setting (the owner's request turned into a chore,
with faults unwatched until he found it); re-enabling the review on every
ensure (it would override an owner who turned it off); keeping a header
switch to turn it off (the owner asked for fewer switches, and the Watchdog
chat and the scheduler list already change it).

**Current state.** Signal sources and identities (`lib/router/watchdog/signals.py`):

| Source | Reporter | Identity (one signal per) |
|---|---|---|
| `turn` | `turn_lifecycle` after a committed `failed` terminal settlement, off the settlement path | conversation, Turn, Attempt; only router-managed conversations (Global Router, Project Router, Worker, the Watchdog itself) and infrastructure kinds (`watchdogTurnErrorKind`). When the dispatcher records a provider billing stop, the envelope's `billingStop` evidence adds the refused credential's key tail and wire model to the subject, and its status, deployment and observation time to the detail — a Turn's `routeSnapshot` names the last SUCCESSFUL round, which is a different credential after a failover, so the key that ran out of credit is never inferred from it |
| `recovery` | `project_dispatch/recovery.py` per assignment and per startup scope | project, assignment and grant revision, error class and code, startup episode; authority refusals (`PermissionError`/`ValueError`) are the pass's ordinary skips |
| `outbox` | `channels/outbox.py` when a send settles | item and state (`failed`, `unknown`) |
| `channel` | each channel supervisor's scheduler tick (`reconcile_channel_bridges` for 大象, `reconcile_github_sessions` for GitHub) | channel, unhealthy state and last connection time; detail text changes every restart and is not identity |
| `quota` | the production quota watch (`lib/llm_dispatch/quota_watch.py`) through `lib/router/watchdog/quota.py`, on its own delivery thread | one episode of one (AppId, wire model) lane: conversation rounds rejected by the provider's per-AppId minute limit and held by dispatcher backpressure; the watch closes it after a full provider minute of accepted rounds without such a rejection |
| `client` | browser crash capture (`lib/logging_pkg/client_crash.py`) after storing a crash record | browser page instance: the reloaded tab's unclean-exit report and Chrome's report of the same crash are one episode; typed facts only, never page content ([LOGGING.md](LOGGING.md)) |
| `storage` | the request-services process's `StorageRuntime` when readiness returns after a fence, through `lib/router/watchdog/storage.py` on the runtime's announcement thread | one fence episode, by its start under a `fence` tag and in its own `fence` fault episode (never folded into the supervisor's `authority` storage episodes); recorded for every owner with a router home, since no storage-backed signal can be written while the fence holds. The detail carries since, until, duration, cause, last restart error and restart attempts. While the fence holds, the manager log, `serverctl status` and the browser show it ([STORAGE.md](STORAGE.md)) |
| `patrol` | the periodic review, before its snapshot, through `lib/router/watchdog/compute_patrol.py`, from each `publication.json` under `<data>/compute_patrol/u<owner>/<patrol>/` | one published episode: (patrol, kind, job or condition, since) for a hung or stale-queued job (`compute_job_hung`, `compute_job_queued_stale`) or a degraded patrol (`compute_patrol_degraded`); one `compute_patrol_stale` per publication past its `staleAfter`. The intake closes an episode the patrol no longer publishes, and every one when the patrol was stopped on purpose; while the patrol is stale its job signals stay open. The patrol only notifies; the hint names the Global Router as the router to the owning lead ([COMPUTE_PATROL.md](COMPUTE_PATROL.md)) |

**Quota signals.** Problem: an external workload sharing an AppId's
per-minute limit (2026-11-09: a research pilot on Tofu's busiest production
AppIds) can throttle conversation rounds, while normal auxiliary traffic
(translation, fetch extraction) hits small models' limits on every AppId all
day. Decision: the dispatcher's existing per-attempt outcomes feed an
in-process watch; one corroborated episode is one `quota` signal (subject:
AppId by the last four characters the provider shows, and the wire model;
detail: the window's counts, holds, TTFT, Tofu's own load and the other
AppIds serving the model), and its hint tells the Watchdog that the Global
Router decides whether to pause the other workload. The watch resolves the
signal on recovery, so it never depends on the model to close it. Rules and
alternatives: [llm_io.md](modules/llm_io.md), "Production quota watch".

Reporting never creates a Watchdog (owners without an available one record
nothing) and never raises into the reporter. Open signals keep an index row
carrying source and conversation, so a conversation's open Turn failures are
found without reading every signal. A signal about the Watchdog's own Turn
never wakes it; while that signal is open and the periodic review is enabled,
new signals do not wake it either (the review will), and its next
`watchdog_snapshot` closes its own failures. Without an enabled review every new
signal still wakes it, so a failure cannot silence it for good. A wake lost
after its signal committed surfaces at the next review or snapshot.
`health_snapshot` (`snapshot.py`) is a deterministic read with no model call or
side effect: the Global Router and a page of Project Routers (availability,
last activity, open signal IDs) with their Workers of the current binding
epoch that are active, failed or have a held delivery (objective, status, last
activity, the settled Turn's error); outbox items in `failed`/`unknown`;
channel account and bridge state; and a page of open signals. Pages follow
the project dispatch history page. The periodic review is the owner's keyed
task `system-<owner>-router-watchdog-review` (`scheduler.task.ensure`, so
retries converge), installed on by default (above). The owner changes it by
asking the Watchdog (`watchdog_configure_review`, admitted only while the
current request is the owner's own genuine message, resolved like delegation
authority), through `POST /api/v4/router/watchdog/review` (scope
`agents:scheduler`) or on its row in the scheduler list; the contract default
cadence is `7,37 * * * *`. A review whose snapshot holds no
active, failed or held Worker, open signal, outbox item, unhealthy channel or
unreadable router ends without a model turn.

Every periodic review that ran writes `logs/watchdog-heartbeat-<owner>.json`
last (`heartbeat.py`, on disk, never TofuDB). The file records when the next
review must have ended: the next cron run plus the due-claim interval and the
review's 300 s deadline. Disabling the review records that too. The
out-of-band watcher alerts once on a heartbeat past that time
([Out-of-band outage alert](STORAGE_OPERATIONS.md#out-of-band-outage-alert)).
A review that raises or finds the Watchdog unavailable writes nothing, so its
heartbeat goes stale. A re-check is a `once:` task with
the deterministic id `watchdog-recheck-<digest(owner, Watchdog, key)>`; the
scheduler disables it after it fires. Watchdog-armed tasks use a
least-privilege `agents:scheduler` principal, like the scheduler tools.
`watchdog_message` resolves the target's role from durable metadata and
submits to that project's mailbox with the Watchdog as sender (labelled
nonhuman, kinds `reference`/`question`/`steer`, idempotent per key); mailbox
delivery admits the owner's currently bound Watchdog as a sender in every
project, and otherwise only another project's current lead (R13). `watchdog_notify_router` is one peer
input with `origin.sourceConversationId` = Watchdog, command id hashed from
(owner, Watchdog, key, message, signals), refused while the Global Router's own
latest Turn is failed.

**Problem:** a Turn signal never closes itself (the subject reports failures, not
recovery), so an open signal is not evidence that the Router is still failing. A
Router that recovered and kept working was still refused notice, and the refusal
itself told the Watchdog to resolve the signal before notifying — a deadlock.
**Decision:** before refusing, the tool reads durable Turns; when the newest
main-lane Turn is `completed` and newer than the failed one, it resolves the
signal with that Turn as recovery evidence and sends the notice. Only a
still-failing, running or interrupted Router is refused. A signal whose failed
Turn is on a branch lane, or whose Turn no longer exists, stays open for the
Watchdog to judge. Recovery is a durable fact, not the model's say-so.
**Alternatives rejected:** auto-resolving Turn signals on every completed
settlement (a storage read on every Turn for a condition only this tool needs);
merely rewording the refusal (the snapshot reports `last_activity_at` but no
latest-Turn state, so the Watchdog still cannot tell a stale signal from a live
one).

The Global Router neither lists the watchdog path as a project nor forwards into
it, and Workers are denied every `watchdog_*`
tool. TODO(enterprise): channel signals ride the personal scheduler tick that
hosts the bridges.

### R8. The owner sees one kind of coordinator, not routers and a watchdog

**Problem:** the first browser surface exposed the architecture: two pinned
sidebar buttons (全局路由, 看门狗) above New Chat, a separate 项目负责人 ·
Coordinator card in the list, a header that repeated the router's name next to
the English title "Global Router", and the router home's directory name (`1`)
as the chat's project location. Owners could not tell how the three related or
what the Watchdog was for, and the extra chrome cost space on every screen.

**Decision:** one role, two scopes, one slot. The Global Router is shown as
the main coordinator (总负责人) and a Project Router as the project
coordinator (项目负责人), with the same card in the same place: the leader
slot above the conversation list holds the project's coordinator when the
listed conversations belong to a project, and the main coordinator otherwise
(an uncategorized or plain folder view, or a router-managed chat). Neither
chat is also a list row. Both available coordinator cards expose a localized
"Copy conversation ID" action, without requiring debug mode. The dedicated
cards had removed the ordinary row's diagnostic action; reintroducing a second
conversation row was rejected. Copy reads the current binding without opening
or creating a conversation, and a clipboard failure exposes the exact ID for
manual copying. The Watchdog has no entry of its own: health checks
(故障巡检) are on by default (R7) and appear as a compact fold in the main
coordinator's header next to the Worker model, standing instructions and "Act
for me": their state, one sentence on what they do and the way into the
Watchdog chat (which offers a way back), with nothing to switch or schedule. The
server's fixed role titles are language-neutral sentinels the browser shows
through the same localized names, and a router-managed chat (router home,
Watchdog location, router Workers) shows no project location, since its
workspace is Tofu's, not a project the owner attaches or edits. Copy uses
these names everywhere the owner reads it: settings, tool descriptions and
hints.

Beneath both entries the classic sidebar lists the lead's 成果 and the
conversations it started as plain rows; no office scene is mounted there
(PROJECT_COORDINATION.md, "Lead office"); the conversation keeps the main view.

**Alternatives rejected:** keeping pinned entries and only renaming them (two
extra rows on every screen and still no visible relation to project
coordinators); showing the main coordinator card above the project card in
project folders (two stacked cards for one concept); a sidebar icon for the
main coordinator (hides the owner's primary entry); renaming the server titles
(language-specific durable data, and existing conversations keep the old
titles anyway).

### R9. The owner's delegation: the Global Router acts for the owner (owner decision 2026-11-06)

**Problem:** the owner wants the main coordinator to carry out owner
operations itself, starting with restarting Tofu, without a click each time.
The owner ruling of 2026-07-28 (`lib/lifecycle_approval.py`) requires a human
to approve every live-server restart: a request the owner approves in the UI,
the owner's interactive terminal, or the handoff variable of a caller that
already passed that gate. AGENTS.md principle 4 kept every synthetic user
from inheriting human authorization, and the Router's turns are often
nonhuman (channel events, relayed reports, standing instructions). So a
Router restart was always refused (2026-11-06 10:20: a non-interactive
`serverctl.py restart` stopped at the gate and created a pending request),
while two restarts had passed the gate without any human decision on record
(11-05: an agent approved a request with `decided_by=explicit-user-instruction-via-codex-chat`;
11-06 01:21: a terminal or handoff restart left no record).

**Decision:** the owner explicitly delegates operational authority to their
Global Router, and this narrows the 2026-07-28 ruling to: *a human approves
every live restart, or the owner has granted their Global Router a revocable
restart delegation*. The lifecycle approval gate stays the single decision
point. Scope:

- **One grant per owner**, held by the owner's current Global Router
  conversation and binding revision (it never carries over to a replaced
  router), stored in the gate's own document (`lifecycle_approvals.json`,
  `delegations`). It covers `lifecycle_approval.DELEGATION_SCOPES`: `restart`
  (`restart_server`), `conversation_control` (Continue, checkpoint resume
  and Stop of an exact Attempt through `control_conversation(owner_delegation=…)`
  when the Router's request has no owner message of its own), `owner_settings`
  (reversible owner settings through the Settings UI's own backend, and channel
  enable/disable; R11), `project_lifecycle` (creating and configuring
  projects and their Project Routers, R12), `project_repair` (opening one
  repair conversation in a project whose Project Router cannot run, below) and
  `scheduler` (pause or resume one of the owner's own, service or system
  scheduled tasks, such as the nightly storage backup, through
  `schedule_manage(action='disable'|'enable', owner_delegation=…, idempotency_key=…)`;
  the gate record's target is `{kind: 'scheduled_task', taskId, taskScope,
  action}`, [scheduling](modules/scheduling_ops.md#governed-scheduling-one-primitive-scoped-grants)).
  The owner's switch grants the **full** delegation (`coverage: 'full'`):
  every scope above and any scope added later, with no further action from
  the owner. A grant written before `coverage` existed lists the switch's
  scopes of its day and reads as full, so the owner's existing grant covers
  every scope above as it stands. An **enumerated** grant covers exactly the
  scopes it lists and stays that narrow (`owner_delegation_scope`); granting
  from the switch again makes it full. `lifecycle_approval.covered_scopes`
  decides, on read; nothing rewrites a stored grant. The "Act for me"
  disclosure shows the coverage and the scopes it covers
  (`ownerDelegationGrant.coverage`, `scopes`), and every use, refusals
  included, is a delegated-action record in the owner's log there and
  `owner_delegation_*` lines in audit.log. `restart`'s
  `accept_delegated_restart` re-check is unchanged. It is off until the owner
  turns it on; it is an authorization setting, not a tuning knob.
- **One switch, full by definition (owner decisions 2026-11-09 and
  2026-11-10).** The owner asked 「…就反正人能干的事情，总负责人都要能干」 and, for
  settings, "…if you need to adjust something in the settings, you can also
  have that permission yourself." The 2026-11-06 grant was one switch, not
  per-scope consent, so a record granted before a scope existed covers it too.
  Problem (2026-11-10): adding `scheduler` would have made the owner turn
  "Act for me" off and on, because a grant recorded the scopes it was given;
  the owner rejected that step (「以后不要搞代我操作了，直接给全权限」). Decision: the
  explicit `full`/`enumerated` coverage above. Alternatives rejected:
  refreshing every granted record to all scopes on read (the mechanism from
  2026-11-09 until this coverage replaced it: every grant becomes full, so no
  grant can stay narrow); a switch per scope (the owner asked for full
  permission, not more switches); rewriting stored grants at boot (a write to
  the approval store for a fact derivable on read).
- **Granting needs the owner in the Tofu UI.** `POST /api/v4/router/owner-delegation
  {"granted": true}` (scope `admin`) admits only a user principal over the UI
  session cookie, or the loopback open-mode UI, with same-origin browser
  evidence (Fetch Metadata where the browser sends it, else an `Origin` naming
  this host): `routes/api_v1/auth.human_ui_provenance`. API keys, scripts and
  agents get 403, and no model tool can write the grant. The record keeps that
  provenance (user agent, peer, credential) and an `assurance`:
  `authenticated` for a UI session, `advisory` in open mode.
- **Revoking** is any authenticated owner request (`{"granted": false}`) and
  takes effect at once: every delegated action re-checks the grant in the gate
  transaction that commits it, including restart acceptance after preparation.
- **Every action** needs the grant, a reason and an owner trigger: the owner's
  own message of the current request (`source_turn_id`), an owner message
  steered into the reply (`source_steer_id`), or an active standing
  instruction with exactly one of what triggered it (`instruction_id` +
  `event_id` or `source_turn_id`), resolved by the forwarding owner exactly as
  `route_to_project` resolves them, plus the live router attempt fences. The
  choice is the contract rule `ownerDelegationTrigger`
  (`contracts/router_v1.schema.json`), which `owner_delegation.resolve_trigger`
  validates before it reads anything, for every delegated scope. A call that
  names no trigger or breaks the rule is a typed refusal
  (`owner_delegation_trigger_missing` / `owner_delegation_trigger_malformed`),
  recorded like every other refusal under the grant; nothing runs and the
  idempotency key stays unused, so the corrected call is accepted once.
  Problem: at 2026-11-10 03:46 `restart_server(instruction_id=…)` without
  `event_id` or `source_turn_id` raised a plain `ValueError`: nothing was
  recorded and its `{"error": …, "execution_started": false}` result settled
  as a completed tool call, so the Router told the owner a restart would
  follow. Decision: the rule above, stated in every delegated tool's
  description, and failed Coordinator-family calls settle as failed
  ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#session-execution-control)).
  Alternatives rejected: the rule in the tool parameters (providers refuse
  root combinators, and the wire sanitizer isolates the whole tool once a
  nested `required` names properties its subschema does not declare);
  `ARGUMENT_ALTERNATIVES` admission (no conditional rule, and a call refused at
  admission never reaches the gate, so nothing would be recorded).
- **Audit.** The gate writes one `delegatedActions` record per action: who
  (`requester`: router conversation, binding revision, tenant, Turn, Attempt),
  why (`reason`, `trigger` with the owner's verbatim words), what (`target`),
  under which grant (`grantRevision`, `grantedBy`), when (`requestedAt`,
  `acceptedAt`) and the outcome (`status` `accepted`/`restarting`/`completed`/
  `refused`/`failed`, `code`, `detail`, `interrupted`). A refusal while a grant
  exists is recorded as well; without a grant only `audit.log` records it.
  A `restarting` record reads `completed` once a server process that booted
  after its acceptance answers (`lib.boot_identity`; every restart replaces
  the serving worker); its detail names every resident host the restart
  replaced after an unclean close, from the manager's recorded outcome
  (`server_manager.read_restart_outcome`). The manager decides every refusal
  before it stops the old server, which keeps serving and reads that
  refusal: the record settles `failed` with code
  `restart_refused_by_manager` and the manager's reason. Once the old server
  stopped, the manager always completes the replacement.
  Every transition also writes `audit.log` (`owner_delegation_*`). The
  document keeps the newest 50 records, the approval records' existing bound;
  `audit.log` keeps every event. The owner reads them under "Act for me".
- **Session model change (`set_model`, R5).** `conversation_control` also
  covers `control_conversation(action=set_model, owner_delegation=…)`: the
  grant, the owner trigger and one record precede `set_session_model`, exactly
  as for a delegated Continue. The record's target (`kind: conversation`,
  `action: set_model`) names the chosen `model`, null to restore the session's
  original. It changes which model the session's next attempt runs, never a
  permission. Its trigger obeys `ownerDelegationTrigger` like every scope: a
  missing or malformed trigger is a recorded refusal before anything is
  stored, and a model the owner's route resolver refuses settles the accepted
  record `failed`; both read as failed tool calls.
- **Restart (`restart_server`).** Grant → trigger → restart protection →
  audit record → a detached thread, so the restart outlives the calling turn:
  `server_reexec.prepare_server_reexec_frontend()` (the Settings preparation)
  → protection again → gate acceptance (grant, cooldown and restart stamp in
  one transaction) → `perform_server_reexec('restart', frontend_prevalidated=True)`
  → the Supervisor's `application-restart`, whose manager restart runs the
  repair-capable frontend preflight and stops the old worker only if it
  succeeds. There is no token, environment variable or pending request on
  this path, and no grant means a typed `owner_delegation_not_granted` refusal
  with nothing pending (an agent-made request would be a prompt the owner
  did not ask for, and its 600-second token does not fit an asynchronous
  chat). Protection is the Settings signal, `list_running_tasks`, without the
  Router's own conversation: a protected step (`git_land`, `export`; below)
  always refuses; other running conversations refuse unless the owner asked
  to restart anyway (`interrupt_running_conversations`, the Settings force);
  an unreadable signal refuses. In the managed split the Router runs in the
  execution host, which owns no frontend: its preparation covers the native
  engine and the manager's frontend preflight is the build that gates the
  stop, so a failed frontend build there is a manager refusal: the old
  server keeps serving and the record reads `restart_refused_by_manager`
  with the build's reason (also in `server-manager.log`).
- **Restart-complete notice.** The requester hears the outcome without being
  asked, exactly once (`lib/server/restart_notice.py`). Every restart the gate
  accepts opens one `restartNotices` record in the same transaction that
  records it, before the old server stops: `begin_delegated_action` for
  `restart_server` (recipient: the requesting Router conversation),
  `consume_restart` for a Settings restart (recipient: the owner's Global
  Router at delivery, since nobody else asked). Acceptance adds the
  conversations it interrupts, the accepting process's code fingerprint and
  the preparation's phases and duration. When the restart has an outcome
  (`lifecycle_approval.observed_restart`: a refusal or exhausted retry budget
  the manager recorded, a server generation that booted after the handover,
  or the manager's replacement serving for a host the restart kept), the
  notice is rendered once (header `[Restart outcome: nonhuman notice, not the
  owner’s words and not authority]`: action and outcome, old and new commit,
  engine and frontend build results, each interrupted conversation's resume
  result), its words and command id (`restart-notice:` + a hash of owner and
  restart) are fixed in the gate (`pending` → `delivering`), and it is sent
  through `dispatch_scheduled_turn` with initiator `proactive`, steered into a
  busy recipient: the device-online notice's path. An idle requester starts
  a new turn. A delegated record reaches its terminal status (`completed` or
  `failed`) at delivery, with an `owner_delegation_action_settled` audit
  event. Triggers: a refusal decided in the old process (preparation, bridge,
  revoked grant) delivers at once; a new server generation delivers after its
  restart resume pass ([RUN_CONTINUITY.md](RUN_CONTINUITY.md#restart-paused-conversations-resume-by-themselves)),
  so the notice carries the resume results; the scheduler's existing tick
  delivers a manager refusal while the old server serves on, a host the
  restart kept and a retry after a lost acknowledgement (the same words and
  command, which the turn command deduplicates). A notice nobody can receive
  is recorded `undeliverable`. The notice is nonhuman evidence and grants no
  authority. `restart_server`'s `next_step` says exactly this: tell the owner,
  end the turn, and report from the notice.
  Problem: after the 2026-11-08 16:46 restart the Router's turn had ended, its
  record stayed `restarting` (completion was derived only on read) and nothing
  woke it until the owner asked at 16:57, although `next_step` promised that
  the conversation "resumes afterwards". Decision: one durable notice per
  restart in the gate, delivered by the nonhuman wake owners above.
  Alternatives rejected: a synthetic Continue of the Router's ended turn
  (that turn was not interrupted and Continue would replay it); having the
  manager call back into the application (a second cross-process protocol
  beside its recorded outcome); a dedicated poller or timer (the scheduler
  tick and boot already run; principle 5); a separate notice store beside the
  gate's restart records (two authorities for one restart).
- **Protected steps.** Landings and exports change or publish the served
  checkout and often run outside any conversation (a terminal, a background
  command, another agent), so conversation activity could not see them.
  `scripts/git_land.py` (except `--check`) and `export.py` hold a process
  marker under `<data root>/protected-steps/`
  (`lib/runtime/protected_steps.py`); `list_running_tasks(user_id=None)`
  lists live ones as `protected-step:<id>` entries, so Settings restart,
  automatic restart and deferred-restart quiescence see them too. A local
  marker counts while its process lives; another host's marker counts until
  its writer removes it, and the refusal names its path.

**The boundary: act, never speak.** The grant lets the Router act as the
owner; it never lets it speak as the owner. It cannot answer questions
addressed to the owner or forward its own text as theirs
(`route_to_project` and `require_human_turn` still require the owner's
verbatim message), approve its own drafts or prompts meant for the owner
(`channel_outbox_approve` and `channel_configure_thread` keep the genuine
human-Turn gate; lifecycle approvals need the owner's UI decision), start
Workers or record standing instructions from nonhuman input
(`source_for_task`), or make an irreversible external publication the owner
has not approved (pushing an edition to GitHub runs in Workers or behind
owner approvals; the owner's question hg_844c7fcedf23 on the Internal edition
stays open). Under the grant, conversation control allows only Continue,
checkpoint resume and Stop of the pinned Attempt, never Regenerate, and
never creates an assignment: it runs the unchanged session command
(`session_control.control_session`) with the delegation standing in only for
the request's source principal, so the resumed Attempt keeps the target's own
accepted execution identity. Specifications: `tests/router/test_owner_delegation.py`.
The one conversation the grant can start is the repair conversation below, and
its request is the owner's own verbatim words, never the Router's text.

**Agents never approve, operators are recorded.** `lifecycle_approval.decide`
requires the `human-ui` provenance only the HTTP decide endpoint mints, so a
direct call such as the 11-05 one is refused and audited. `serverctl.py`
keeps the owner's interactive-terminal confirmation and the handoff variables
(`TOFU_LIFECYCLE_GATE_PASSED`, `TOFU_RESTART_GATE_PASSED`) but writes each pass
as a consumed record with its own provenance (`interactive-terminal` with the
tty, `environment-override` with the variable, pid, parent and user); a
broken store never blocks the owner's terminal.

**Extending the grant.** Another owner operation (settings, provider keys,
channel accounts, …) joins the same pattern rather than a new authority: add
a scope to `DELEGATION_SCOPES` (the grant stays one switch the owner turns on
or off; the full grant covers the new scope at once, an enumerated one does
not), and give the Router one tool that goes through the shared gate
`owner_delegation.delegated_action(task, scope, arguments, scope_name=…,
target=…, action=…)`. The gate resolves the owner trigger with
`owner_delegation.resolve_trigger` (the tool's arguments reuse the trigger
properties of `ownerDelegationAuthority`, and its description states the
`ownerDelegationTrigger` rule), records every refusal
(`record_delegated_refusal`) and the action through `begin_delegated_action`
before any effect, and settles it with the outcome, while that operation's
existing owner (the settings store, the credential vault, the channel service)
stays the writer. Operations whose result speaks for the owner (answers,
approvals, publications) stay outside the grant. `owner_settings` (R11) and
`project_lifecycle` (R12) are the first two scopes built this way; the helper
is the single gate both use, so the authority/audit mechanism exists once.

**Repairing a project whose Project Router cannot run (owner decision
2026-11-12).** Problem: from 15:57 on 2026-11-12 the tofu Project Router
failed every Turn (a pre-compaction archive above the engine's document bound,
refused non-retryably). The Global Router saw it at 16:20 but could start
nothing in that project: `delegate_project_work` reaches only its home,
`route_to_project` forwards only to the broken lead, `create_conversation`
makes an empty shell in the home, and a home Worker pointed at the project's
checkout (the 19:17 workaround) works outside its own workspace and delivery.
The owner opened a conversation in the project at 18:23 and pasted the error;
at 19:15 they asked the Router for 「等同我的权利」, and decided the same day
that none of the project's tasks should wait for their approval.

Decision: scope `project_repair` and the Router tool `repair_project`
(`lib/router/project_repair.py`). While a project's Project Router cannot
run, the Router may open one ordinary conversation in that project (the lead's
folder, the project root, the project's Worker model and the Router request's
capability switches) and start it with the owner's own words of the trigger
(the current request's message, a steered owner message, or a standing
instruction applied to that project exactly as R4 applies it), followed by its
repair brief as labelled nonhuman evidence. That is the single input
`route_to_project` delivers (`forwarding.forwarded_message`), so the
authority is still the owner's verbatim words. The conversation then works
like the owner's own conversation in that project, under that project's
guidance (lanes, landing). The grant, the trigger, the check and one audit
record precede any effect; a retry with the same key opens nothing again.
"Cannot run" has no threshold (`lead_state`): the binding is not available,
or the lead's newest Turn that did work (`restart_resume.newest_work_turn`,
shared with restart resume: input Turns, appended reports and queued pairs are
passed over) settled `failed` for a cause other than a dispatch-authority
withdrawal. A running lead, or one whose newest work Turn completed, was
interrupted (a Stop or a restart) or truncated, can run; its work belongs to
it (`route_to_project`, or `control_conversation` for a transient failure),
and the tool refuses with `project_lead_can_run`. When a repair Turn settles,
the project settlement port (`lib/tasks_pkg/project_settlement_sink.py`)
steers one nonhuman repair-outcome notice into the requesting Router (command
id per repair Turn), so it can verify the fix, restart under an owner message
or instruction that covers it, and resume the lead. A repair Turn resumed
after a restart reports through the restart-complete notice instead: restart
resume carries only the public config, not the repair marker.

Alternatives rejected: Router-owned Worker rows in the project's dispatch
record (six identity and lifecycle fences tie a row to the project's own
binding, request account and Coordinator; relaxing them would route the
project's wakes to the Router, hide the work from the lead when it returns and
widen a Router Stop to every project); home Workers working on another
project's checkout (their workspace, publication and delivery belong to the
home); reviving the lead only (`control_conversation` cannot fix a defect in
code); replacing the lead (every grant pins the binding epoch, so its running
Workers would stop); a Router-written request (the grant never lets the Router
speak as the owner, so its brief stays evidence); a failure count or waiting
time before a repair is allowed (principle 5: the newest work Turn is the exact
fact). Specifications: `tests/router/test_project_repair.py`.

**Residual risks (stated, not solved).** No Tofu tool, instruction or code
path can grant the delegation; only the HTTP grant above can. Agents run as
the server's operating-system user, though, so the server cannot prove that a
request came from the owner's hands. Open mode has no authentication: any
local process can send a loopback request with browser Fetch Metadata, so a
grant made there is recorded as `advisory` and the switch says so. Such a
forgery is deliberate, not an accident of a chat instruction, and it stays
visible in the grant record (user agent, peer, credential) and in `audit.log`:
the same limit as every human-only decision of this gate. An
agent that drives the owner's real browser acts through the owner's session
and cannot be told apart from the owner here; the existing browser controls
(page mutation needs the owner's per-domain approval and the Tofu client tab
is never an automation target, [browser automation](modules/browser_automation.md))
and that agent's durable tool rounds are the evidence, and every decision
keeps its user agent and peer in the record and in `audit.log`.

**Alternatives rejected:** a self-issued approval token or an environment
variable for the Router (a second, invisible approval path); creating a
pending approval on every refused Router restart (the owner's click is what
the grant replaces, and the Router could consume it only through a second
path); a grant written by the Router or a settings file (indistinguishable
from the forgery the gate exists to stop); a separate delegation store,
principal kind or Router-only restart path (a parallel authority beside the
gate, principle 4); letting the grant also authorize answers, approvals or
publications (the owner's voice is not an operation); per-operation switches
or rate knobs (principle 5).

### R10. Proactive coordination (owner request 2026-11-08)

**Problem.** The owner asked the main and project coordinators to design how
they act on their own: how to coordinate, when to infer his intent and hand
out work, and what to report to him and when (2026-11-08,
「我们的总负责人和项目负责人一定要思考清楚，怎么设计这个主动机制…」). The same
day showed the gaps:

- The restart he delegated woke nobody, so nothing was checked or reported
  until he asked (R9).
- "I'll report back when X" held only if a later relay happened to arrive,
  and nothing checked that one did.
- His decisions were spread over conversations and drifted.
- No lead had a view of active, stalled or silent work, or of his unanswered
  questions, and he kept nudging ("你怎么没…", "进展呢").

A wake is not free: a Global Router turn is about six model requests over a
350–800k-token context (latency study, 2026-11-08). A mechanism that wakes a
lead for every event would trade nudges for cost and noise.

**Decision.** Proactive means fewer owner prompts, not more messages.
Deterministic code detects, counts, assembles and deduplicates. A model is
woken only for an open obligation that needs its judgment, exactly once,
through one wake primitive. The owner is pushed only what the reporting rules
allow, and outside quiet hours only what cannot wait.

**Principles and authority.** Proactivity never enlarges authority.

- Inside an authorized objective, next steps, verification and repairs are
  just done: that is a Coordinator continuation
  ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#result-driven-coordinator-continuation)).
- When intent is unclear, take the reversible default and state the
  assumption (`ask_human(blocking=false)` names the default it applies).
- New scope, irreversible or externally visible actions, and quality or cost
  trade-offs are asked: batched, with a default and numbers.
- Only the owner's own messages, and his standing instructions (R4),
  authorize. Reports, relays, Watchdog notices, wakes and digests are
  evidence. A wake never widens what the woken turn may do: a nonhuman turn
  opens no request account (R3), so it reads, reports, asks and updates the
  board. Work that needs authority runs as a continuation of the request that
  authorized it, or waits for the owner.

**One wake primitive.** Every proactive wake has the same four parts:

1. A durable obligation in its owner's record.
2. A deterministic command id derived from (owner, obligation identity).
3. `dispatch_scheduled_turn(..., inject_mode='steer')` with a nonhuman
   initiator: it steers a live turn, starts an idle one, or queues behind a
   closed steer window.
4. The obligation settles on an accepted disposition; `busy` keeps it for its
   next trigger.

Time is the owner-visible scheduler: a `once:` row with a deterministic id
runs the obligation's handler, which re-reads durable state and wakes only
while the obligation is still open. No model call decides, no loop polls, and
no process-local timer is the authority. A Coordinator obligation inside an
open request is delivered as that request's continuation instead, so the
woken turn keeps the request's authority.

| Trigger | Detector (existing owner) | Obligation | Woken | Delivery |
|---|---|---|---|---|
| Restart done, failed or refused | restart gate and the new server generation, `lib/server/restart_notice.py` (R9) | `restartNotices` in `lifecycle_approvals.json` | the requesting conversation; for a Settings restart, the Global Router | `restart-notice:` notice, `proactive` |
| Restart-interrupted turn | resume pass, `lib/conversations/restart_resume.py` | the interrupted Attempt | its conversation | checkpoint Continue, `restart-resume:<attemptId>` |
| Device online | desktop bridge connection epoch | device wait (`await_device`) | the registering conversation | `device-wait:` notice |
| Worker settled, decision due | dispatch settlement and recovery | continuation grant | Coordinator | continuation |
| Project report or question | `report_to_user`, `ask_human` (R3) | report Turn, question card | Global Router | `router-relay:`, `router-question-relay:` peer notice; held in quiet hours |
| Commitment due (a promise) | task-board `due` | board task and its receipt | the lead that owns the board | continuation `commitment_due` inside an open request, else a `commitment-due:` notice |
| Scheduled deadline, e.g. a quota reset | a commitment due at that time | board task | the lead waiting for it | as above |
| Owner question unanswered | `ask_human` seam | owner question record | nobody: listed in the digest and the quiet-end release | deterministic message |
| Fault: failed Turn, recovery, outbox, channel, compute stall | existing reporters (R7) | fault signal | Watchdog, then Router or mailbox | `router-watchdog-signal:` |
| Stalled Worker | Watchdog review (owner-enabled); the lead's own commitment | fault signal, commitment | Watchdog; the lead at its due | as above |
| Channel reconnect | channel supervisor, outbox | queued outbox item | nobody: queued pushes send once on reconnect | outbox |
| Digest time, quiet-hours end | owner preferences | preference record | nobody | deterministic message |
| Cost or latency anomaly | cost and log owners | none | nobody until the owner chooses a threshold; daily figures go to the digest | digest |

**Commitments (the promise ledger).** A promise is a task-board task with a
`due` time ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#commitments-on-the-task-board-owner-request-2026-11-08)).
The board the owner already reads is the ledger (one progress source, owner
decision 2026-11-08), so he sees every commitment, its time and whether it is
overdue where he follows progress.

- The lead sets `due` whenever it tells the owner it will do or report
  something later.
- Keeping it is marking the task completed. Re-dating it is a visible new
  `due`. Dropping it is marking it cancelled. A board write that silently
  drops an open commitment is refused.
- The platform keeps one receipt per commitment in the same board head:
  first and current due, slips, wake and closure.
- At the due time the lead is woken once if the commitment is still open.
  It is never woken twice for the same due time: a commitment left open after
  its wake is surfaced in the digest and the cross-project view, not repeated.

**Owner-intent record.** No new store.

- His standing instructions (R4, e.g. `ri-9887c7de…`) hold his verbatim
  standing objectives, constraints and explicit don'ts as well as channel
  triggers. The Router records one only from his own message.
- My Context (`lib/memory/user_profile`, his owner scope) holds the stable
  preferences he stated or confirmed. Only `active` entries are injected;
  entries inherited from the legacy open-mode store stay inactive until he
  confirms them ([context engineering](modules/context_engineering.md)).
  Report language follows his `ui_language` setting, not a My Context entry.
- His own Turns and answered questions remain the canonical wording.

A model's summary of a decision is never the record: it cites the
instruction ID or the owner's Turn, which is what keeps decisions from
stretching. Every inferred task traces to an entry: the Global Router applies
an instruction with `route_to_project(instruction_id=…)`, which records the
trigger, and a Project Coordinator works inside the request it was given. An
inferred task without an entry is a question to the owner with a default, not
work.

**Coordination.**

- One voice per conversation, one pusher. A lead answers the owner in its own
  conversation. Unprompted pushes to his phone have exactly one sender, the
  Global Router, through its outbox (R3, [CHANNELS.md](CHANNELS.md) C5).
  Project Coordinators never push; their reports and questions relay to the
  Router, which pushes only what is new and unread.
- Deduplication is by identity, not text: one push per report Turn, question
  id, commitment (lead, title, due) or fault signal. The digest and the
  quiet-end release skip identities already pushed, except open decisions,
  which they repeat with their age until answered.
- The cross-project view is `get_router_projects`, extended with each
  project's board summary, open and overdue commitments, open owner questions
  with age and default, unread reports, active Workers and open faults. It is
  a deterministic read; the digest is assembled from the same read.
- Restarts: protection already lists running conversations (R9). Afterwards
  the restart notice gives the requester each interrupted conversation's
  resume result to compare with the view.
- Conflicts: the newer owner message wins; otherwise the reversible default,
  with the assumption stated.

**Reporting rules.**

- Push now, via 大象 so his phone notifies: decisions only he can make,
  blockers needing his action, verified deliveries, serious anomalies,
  corrections.
- Digest, at the owner's digest time and never when empty, most valuable
  first: open decisions (blocking first, then oldest), overdue commitments,
  open faults, verified deliveries, projects that were active but sent no
  update since the last digest, then daily figures.
- Silent: routine status changes, duplicate relays, acknowledgements
  (`[SILENT]`, R3).
- Quiet hours: urgent items only. Report and question relays that arrive in
  quiet hours do not wake the Router; they are held and released at quiet end
  as one deterministic message (decisions batched into one card) and one
  Router notice. Watchdog notices still wake the Router, which pushes at night
  only what cannot wait.
- Every message: conclusion first, numbers, verified and unverified marked,
  and the exact action needed from him. Related decisions go in one card.

**Owner preferences** are settings he chooses (principle 5): digest times,
quiet hours and push channel. Until he chooses: one digest at 21:00, quiet
hours 00:00–08:00, 大象 falling back to in-app. There is no other knob: no
reminder interval (a reminder is the next digest), no wake budget and no
debounce window. Batching comes from steering into a live turn, from quiet
hours and from the digest.

**Failure modes.**

| Failure | Prevention |
|---|---|
| Wake storm | Wakes only for open obligations. One wake per obligation identity (deterministic command id). A live lead is steered, not restarted. A lead's commitments due together share one wake, and each due time wakes once. Quiet hours hold relay wakes. |
| Duplicate or conflicting reports | One pusher. Identity-keyed deduplication. Reports the owner already read are not pushed. The digest repeats only open decisions. |
| Stale or over-stretched intent | Intent is the owner's verbatim words with IDs. The newer owner message wins. Pausing or cancelling an instruction stops future applications. |
| Authority creep | Wakes are nonhuman: no request account and no delegation. Authority-bearing follow-up runs only as a continuation of the authorizing request. Every inferred task cites an intent entry. |
| Cost blow-up from wakes | Detection and assembly are deterministic. The digest and the quiet-end release cost no model request. Wakes need an open obligation. Wake requests and tokens are measured per day. |
| Promise spam | A commitment is a board task, so the 24-task board bounds them. One wake per due time. A missed commitment surfaces in the digest instead of waking again. Dropping one needs an explicit cancel. |
| Quiet-hours violation | Deterministic messages run at owner-chosen times outside quiet hours. Relays are held. Notices that arrive inside the window name it. |

**Measuring the mechanism.** Deterministic, from durable records and logs:

- owner nudges per day (owner messages matching the nudge phrases);
- event-to-owner latency: report, question or commitment due to push or
  owner read;
- commitments kept on time, kept late, slipped, cancelled or missed;
- pushes never read, and replies that complain;
- proactive wakes per day by trigger, with their model requests and tokens.

**Portfolio ownership (owner request 2026-11-09).** The Global Router owns the
owner's portfolio, not only a relay of project reports: 「我们要让主router能发挥主人公意识，
有办法去迭代我的多个项目，能想到甚至我想不到的地方，要有价值」. It extends this R10
mechanism; there is no second wake loop, progress board or projection.

**Problem.** Finished work can be unrecoverable, blocked reports can contradict
raw artifacts, and a list of projects does not say which next action is worth
its GPU/model/time. An autonomous list of work without an authority fence or a
measured end would be activity, not ownership.

**Decision.** One owner-scoped durable record per opportunity
(`portfolioOpportunity` in `contracts/router_v1.schema.json`, record namespace
`portfolio`), read and written through the record-CAS repository port with an
explicit owner. It records project, problem, source and evidence paths;
expected amount, unit, probability and measured basis; the cheapest validation
and its GPU/model/wall cost; approval and its reason; dependencies; a
falsifiable kill condition; an optional value deadline; the standing
instruction authorizing a start; and its state. Sources are repeated failures,
stale projects, unverified claims, user friction, evaluation gaps, reusable
components, deadlines and report/evidence contradictions. Cross-project
leverage is `kind=cross_project`, owned by the project whose platform it
changes. The first audited registry entries belong in the owner's Global
Router after it reads this decision: this worker must not seed its live
registry or start projects itself.

Ranking is deterministic expected value per unit of resource. Each value unit
is a share of the open portfolio's amount x probability, so ¥/day, hours,
reliability and claims never acquire an invented exchange rate; the validation
costs are shares of demand on GPU hours, model CNY and wall hours, and the
largest share is the binding resource. `valueShare / dominantResourceShare`
ranks candidates, zero-cost validations first, then deadline, age and id. It is
not a novelty score. The result records both shares, so a human can audit it.

The existing owner-visible scheduler gains one daily
`router_portfolio_review` row (09:30 local, after quiet hours), installed by
the scheduler's keyed ensure with `reconcile_enabled=False` from one definition
(`REVIEW_TASK`) shared by the boot install, the registry's first proposal and
the owner's switch. A run closes expired proposals and started initiatives
with no outcome by their promised due time, retries an unconfirmed start with
the same command id, then admits candidates as below. Detection, ranking,
admission and settlement make no model request and do not poll conversations;
the Project Router of an authorized initiative is the only model spend.

**Admission (review 2026-11-09).** Problem: the first version started at most
three candidates per review, at most one per project. Three was an invented
count (principle 5): it neither follows the owner's authority nor any measured
capacity, so a fourth authorized, independent and affordable initiative waited
a day for no reason, while three expensive ones could start into a provider
that was refusing requests. Decision: rank the proposals by expected value per
unit of cost and, in that order, start **every** candidate that

1. passes the authority gate: an active standing instruction of the current
   Global Router scoped to its project (R4), or the owner's own approving
   message (R2). A candidate that needs approval never starts automatically;
   unfinished dependencies (read by id, so a delivered one unblocks) wait;
2. is neither running nor a duplicate of open work: a started record is never
   started again, and a proposal with the same work identity (project plus
   normalized problem statement) as an active initiative or a proposal ranked
   ahead of it waits as `duplicate:<id>`, so a twin cannot bypass the other's
   pending approval;
3. does not conflict with active work on the same project: each initiative is
   its own request in that project's Project Router, so a second one there
   waits as `project_busy:<id>` until the first settles (two concurrent
   initiatives would share one Coordinator, and their measured outcomes and
   costs could not be told apart);
4. has its resources measured available now (`lib/router/portfolio/capacity.py`),
   never guessed: the model the Project Router would run (resolved exactly as
   its scheduled turn would) needs a route the dispatcher could pick now
   (`LLMDispatcher.route_admission`); when every route is held, the deferral
   records each hold, and its `retryAt` is the reset the provider itself named
   (`Slot.provider_retry_at`) only when every hold carries one, so no review
   asks before it; our own steering/backoff timing is never a retry time and
   the next review measures again. A validation that needs GPU hours names its
   queue and GPU count, and the queue's real state, read through the
   registered read-only queue tool (`lib/response_surfaces/mcp.py`), must show
   that many GPUs available; an unreadable queue is recorded, never assumed
   free. Host CPU, memory and thread counts never admit, size or refuse a
   start (principle 3).

A start applies the authority through `route_to_project`'s owner-checked path
(`apply_standing_instruction`, or a forward of the approving message), never
the owner's voice or a synthetic human request, as one Coordinator request
whose evidence rides inside it (R2). Unlike an owner's own message, it queues
behind a busy Project Router's live request instead of steering into it, so
every initiative opens a request of its own and its usage account is its cost.
`begin` records the promised amount, unit, probability and due time and the
admission (rank, score and shares, authority, measured resources) **before**
the forward; the trigger names the start attempt, so an unconfirmed send is
repeated idempotently. A start the Project Router does not accept (its
executor could not start, for example a thread that could not be created; Plan
mode; a missing conversation) is not an initiative: the record returns to
`proposed` with a `start_failed` deferral and the next start is a new attempt,
never a replay of a forward that did not run. Each deferral (`portfolioDeferral`)
is the latest measured fact, not a backoff schedule. Closed or started work
never restarts to look busy; `settle` records a measured amount and evidence,
or an explicit negative result when the kill condition holds. The same record
is the promised -> delivered ledger; dropped proposals cannot masquerade as
value.

Alternatives rejected for admission: the top-three cap (an invented count that
neither authority nor capacity defines); starting everything authorized in one
pass with no conflict or capacity check (two initiatives in one Coordinator,
or a start into a provider that is refusing requests); a host-derived
concurrency ceiling (principle 3); a Tofu-chosen retry or backoff interval
after a rate limit (a knob, principle 5; only the provider's own reset delays a
re-check); asking a model whether capacity exists (paid guessing); steering an
initiative into the Project Router's live request (its cost and outcome would
merge with the owner's own request).

**On by default.** The owner asked the Global Router to iterate his projects
proactively, and his standing authority covers work that needs no new
approval, so the review is on unless he pauses it: a review never installed
reads as on, the scheduler installs it on, and the first proposal installs it
where the scheduler has no implicit owner. The owner pauses or resumes it on
its row in the scheduler list (Portfolio review) or through
`POST /api/v4/router/portfolio/review`; a pause persists across reinstallation
and stops new starts, not admitted work. Quiet hours start nothing.
Alternative rejected: off until the owner opts in (the owner already asked for
the behavior; an opt-in default turns his request into an extra chore and the
mechanism into a dormant one).

**No header switch (owner decision 2026-11-17).** Problem: the main
coordinator's header carried a "Portfolio: on" pause switch the owner could
not place ("I don't know what this project portfolio is, but in any case, we
need fewer switches"). Decision: the header has no portfolio control; the
review stays on by default and is paused where every other scheduled task is.
Alternative rejected: renaming the switch (still one more switch for a
mechanism that is on by default and needs no routine attention).

**Reporting.** The existing daily digest (no new push) reports, only when it
has something to say: approvals only the owner can give; each start since the
last digest with why (rank of the review's ranking, the authority applied, the
measured resources), what it has cost so far (tokens and model requests of its
Project Router request account, Coordinator plus Workers, flagged as a lower
bound while a receipt is open; the proposal's estimated ¥/GPU/wall cost beside
it, because usage receipts carry tokens, not prices) and its expected outcome;
measured value and explicit negative results; and every proposal a review
deferred with its measured reason.

**Current state.** `lib/router/portfolio/` owns registry, ranking, capacity,
review, router tool and read-only views; `router_portfolio` is
Global-Router-only. `routes/api_v1/router.py` exposes only the review
pause/resume, actual counts and ledger, never another registry writer; the
browser has no portfolio control. Specs: `tests/router/test_portfolio.py`,
`test_portfolio_review.py` (more than three eligible candidates all start in
value order; duplicate, conflict and dependency cases; a rate-limited provider
defers until its own reset; an executor start failure leaves the candidate
pending; an approval-needing candidate never starts; GPU queue admission; the
digest's reason, cost and expected outcome), `test_portfolio_authority.py`
(registry, admission, ledger and deferrals survive a stop and reopen of
isolated SQLite and native authorities; the default-on switch and lasting
pause), `tests/llm_dispatch/test_subscription_quota_phase.py`
(`route_admission`) and `tests/api/test_api_v4_router.py`. Production activation needs
the API and execution (scheduler) processes restarted by the owner.

**Current state (2026-11-08).** Implemented: commitments on the board
(`progressTask.due`, `lib/conversations/coordinator_commitments.py`), due wakes
through the scheduler (`coordinator_commitment_due`), continuation reason
`commitment_due` (`lib/project_dispatch/continuation.py`), the owner question
index (`lib/router/proactive/questions.py`), quiet-hours hold and release
(`lib/router/proactive/held.py`), the deterministic digest
(`lib/router/proactive/digest.py`), and the cross-project view extension
(`lib/router/projects.py`). Specs: `tests/project_mod/test_coordinator_commitments.py`,
`test_coordinator_commitment_continuation.py`, `tests/router/test_proactive.py`,
`test_proactive_replay.py`. Owner preferences (digest times, quiet hours, push
channel) are documented defaults; a settings UI for changing them is not built.
The Watchdog's fault signals, the restart notice (RESTART-NOTICE lane) and the
device-online wake are reused unchanged.

**Alternatives rejected.**

- A promise store and list beside the board: a second progress list, which
  the owner asked to remove on 2026-11-08.
- Waking a lead for every event in the catalog, as the Router's own proposal
  had it: most events need no judgment, and each wake is a full
  large-context request.
- A model-written digest: a daily Router turn of about six large-context
  requests for text the records already hold.
- A model-paraphrased decision log: paraphrase is how decisions stretch.
- The Router as the only voice: the owner talks to Project Coordinators
  directly, and routing every answer through the Router adds minutes and
  cost.
- Quiet hours only in the outbox: the Router would still be woken and billed
  at night for every relay.
- Reminder intervals, wake budgets and debounce windows: knobs (principle 5).

**Owner question answers are recorded when they happen (2026-11-14).**

*Problem.* On 2026-11-13 `GET /api/v4/router/projects` re-read every Turn
after each non-blocking question back to its ordinal, about 10k one-Turn
`turn.sync.page` reads a minute. Bounding those reads (one newest-first walk
per lane, the per-process `LaneScans` memo, `answeredAt` stamps) left the
office's cold pass walking every lead's lane again in each new process and on
each office re-entry: about 310 page reads and 13-20 s on production. A verdict
that must be re-derived from the lane after every process start is the cost.

*Decision* (`lib/router/proactive/questions.py`): the index records each
answer when it happens, so no reader derives it from a lane.

- A record holds its question Turn's `laneId` and `ordinal`. An answer is
  permanent: `answeredAt` and `answeredOrdinal` (the answering Turn) on the
  record, written by record CAS. Every index write only adds evidence, so a
  conflict re-reads the record and decides again.
- A non-blocking question is answered by a chat message (its card takes none).
  Every human Turn that enters a lane records itself as the answer of the open
  non-blocking questions before it there (`answer_lane_questions`). New human
  Turns enter lanes only through `lib.turn_lifecycle` (`create_turn_pair` unless
  queued, `activate_queued_turn_pair`, `append_settled_turn`; a steer joins the
  live reply, and visible phases are never human), in either process; each
  calls it after its commit. A failed index write never fails
  the Turn; it is logged, and the lane's next human Turn records the answer.
- A positioned non-blocking record whose answers are recorded that way is
  `answersRecorded`: its record alone says it is open, with no Turn read.
  Getting there is one read of its lane, after the position is written: a human
  Turn committed before that read is met by it, and every later one finds the
  position. `record_question` does this at ask time, when the question's Turn
  is the lane head (one Turn read). A record indexed before this change is
  positioned and decided once by the first listing that meets it, which records
  the outcome; `LaneScans` still lets one process's reads share their Turns.
- A blocking question is answered on its card (a response or supersession in
  the question's own Turn). While it is open, a listing reads that card; the
  listing that finds the answer records it.
- A failed index write never changes what a listing reports; the record stays
  for a later reader to decide.
- `_proactive_view` reads only its own lead's questions (`coordinator_id`), so
  the page never reads the Global Router's questions, which it omits.

On production, the first listing after the deploy reads each lead's lane once
(about what one cold office pass read before); from then on no listing, office
pass or new process reads a lane for an owner question.

*Alternatives rejected.*

- A cap on the questions or Turns checked per request, or a sample of them: a
  knob (principle 5) that hides answers.
- A time-to-live cache of verdicts: also a knob, and stale until it expires.
- A cache keyed on the conversation's sync head: a live reply advances the head
  with every event, so a busy lead would re-walk its suffix on every pass.
- Keeping the per-process lane walk with the memo (2026-11-13 decision): every
  process start and office re-entry walked again.
- A storage-side answer column or a Turn-write trigger in the engines: it
  changes the storage operations both engines must certify, for an index the
  record CAS already holds.
- Deleting answered records: a late `record_question` replay would re-create
  them as open.

Specs: `tests/router/test_owner_question_reads.py` (61 questions, one lead of
a thousand or more Turns: the route and the office read no lane after one
decision, a process start included; the lifecycle records answers for created
and appended human Turns), and `tests/router/test_proactive.py`: answer
recording per lane, ordinal and actor, the ask-time decision and its race,
permanence, failed writes, and the memo cases (a randomized full-walk parity
check, read-once and resume-below-floor).

### R11. Owner-action inventory and `owner_settings` (owner decision 2026-11-09)

**Problem:** 「…就反正人能干的事情，总负责人都要能干」 (coordinator-1a390889…, Turn
3315ee59) and, for settings, "…if you need to adjust something in the settings,
you can also have that permission yourself." No Router tool could change a
setting. To reconnect 大象 a Worker had to replay the Settings page's own
`PUT /api/v4/channels/dx`, and rightly stopped when the shared SSO login had
expired: the relaunch would have sent a login approval to the owner's phone
(lane DX-RELAUNCH-SELF, worker-71221ebaa23f71591e5dda9a).

**Authority classes.** (a) reversible local setting or action: the Router may
do it under the grant; (b) destructive or irreversible and (c) external or
visible to others (sending, publishing, inviting): never automatic, only on
the owner's explicit instruction in the current request, through the action's
own tool; (d) human-only by nature (entering a secret, approving a login,
scanning a QR code): never attempted, `needs_owner_action` with the exact
remedy; (e) cost-affecting (model, budget, concurrency): like (a) where
reversible.

**Inventory.** Uses are the owner's calls in `logs/access.log` on 2026-11-09
(read-only tally; GETs are mostly page loads). Projects are R12.

| Owner action | Route → code owner | Router tool | Uses | Class |
|---|---|---|---|---|
| Read settings | `GET /api/v1/server-config` → `routes/config.py:159` | `owner_settings` list/get (writable keys only) | 110 GET | (a) |
| Default / fallback model | `POST /api/v1/server-config` `model_defaults` → `routes/config.py:594` `apply_server_config` | `owner_settings` set | 2 POST (whole Save) | (e) |
| Hidden models, hidden image models | same Save, `hidden_models` / `hidden_ig_models` | `owner_settings` set | ↑ | (a) |
| Search blocklist | same Save, `search.skip_domains` | `owner_settings` set | ↑ | (a) |
| Proxy bypass list | same Save, `proxy_bypass_domains` | `owner_settings` set | ↑ | (a) |
| Search gateway, MT provider, 飞书, proxy URLs/pool | same Save; each section replaces its stored secret | never: `needs_owner_action` | ↑ | (d) |
| Presets, cost experiment | same Save, `presets` / `cost_experiment` | gap (plan 5) | ↑ | (e) |
| 大象 on/off | `PUT /api/v1/channels/dx` (`routes/api_v1/channels.py:116`) → `lib/channels/service.py:79` | `owner_settings` channel | 3 PUT, 56 GET | (a); a relaunch needing a login approval is (d) |
| GitHub, 企业微信, QQ, 微信 on/off | `PUT /api/v1/channels/github` (`channels.py:194`) → `lib/channels/github/account.py:142`; bots → `lib/channels/bots/account.py:132` | `owner_settings` channel | 0 | (a); tokens, QR pairing (d) |
| 大象 login MIS, robot secret, GitHub token | same PUTs | never | — | (d) |
| Thread mode, reply policy, owner note | `PUT /api/v1/channels/dx/threads/<key>` (`channels.py:175`) | `channel_configure_thread` (genuine human Turn, C11) | — | (c) |
| Send, approve or discard a draft | `channel_send`; `/api/v1/channels/outbox/<id>/approve|discard` (`channels.py:319`, `:328`) | `channel_send`, `channel_outbox_approve` (C5, C11) | 3 POST discard, 21 GET | (c) |
| Providers, offerings, enabled models | `PUT /api/v1/model-routing` (`routes/api_v1/providers.py:352`, revision CAS) | gap (plan 1) | 169 GET | (e); keys (d) |
| Reveal a credential | `POST /api/v4/model-routing/credentials/<id>/secret/reveal` | never | 91 POST | (d) |
| Subscription / device login | `routes/api_v1/oauth.py:225` | never | 31 GET status | (d) |
| Feature flags (debug, optimizer, PPTX, trading) | `POST /api/v1/features` (`routes/common.py:674`) | gap (plan 3) | 21 GET | (a) |
| MCP servers and tool toggles | `routes/api_v1/mcp.py:130`, `:172`, `:195`, `:320` | gap (plan 8) | — | (a); server env secrets (d) |
| Conversation title, settings | `PATCH /api/v1/conversations/<id>/title|settings` (`routes/conversations.py:727`, `:654`) | gap (plan 6) | 7 PATCH | (a) |
| Create a conversation | conversation create command | `create_conversation` | — | (a) |
| Delete a conversation | `DELETE /api/v1/conversations/<id>` (`routes/conversations.py:789`) | never automatic | — | (b) |
| Folders | `routes/api_v1/folders.py:71`, `:86`, `:121`; delete `:109` | gap (projects: R12); delete never automatic | 11 GET | (a); delete (b) |
| Stop / continue a conversation | `lib/conversations/session_control.py:146` | `control_conversation` (R9 `conversation_control`) | — | (a) |
| Scheduler tasks, timers | `routes/api_v1/scheduler.py:144`, `:156`, `:180`, `:261`; delete `:168` | gap (plan 7) | 70 GET | (a); delete (b) |
| Standing instructions | `routes/api_v1/router.py:132`–`:184` | `router_instruction_*`; pause/resume owner-only (R4) | — | (a) |
| Worker model | `POST /api/v1/router/worker-model` (`router.py:113`) | gap (plan 4) | 16 GET | (e) |
| "Act for me" grant | `POST /api/v4/router/owner-delegation` | never: the owner's UI only (R9) | 2 POST | (d) |
| My Day goals; memory | `routes/api_v1/myday.py:81`–`:166`; `routes/api_v1/memory.py:120`, `:161`, `:230`; delete/clear `:180`, `:261` | gap (plan 9) | — | (a); delete/clear (b) |
| Restart | `POST /api/v1/update/restart` (`routes/api_v1/update.py:420`) | `restart_server` (R9 `restart`) | — | (b), delegated |
| Open a repair conversation in a project whose Project Router cannot run | conversation create + turn command (the owner's own repair chat) | `repair_project` (R9 `project_repair`) | — | (a), delegated |
| Update apply, shutdown, lifecycle approvals | `update.py:222`, `:613`, `:679` | never automatic; approvals need the human UI | — | (b), (d) |
| Exports | `export.py` (protected step) | never automatic | — | (b)/(c) |
| Projects: create, default path, Project Router | R12 | `router_project` (`project_lifecycle`) | — | (a) |

**Decision:** one tool, `owner_settings` (`list`/`get`/`set`/`channel`,
`lib/router/owner_settings.py:316`), with no settings logic of its own:

- **The Settings page's own Save.** `lib/router/owner_settings.py` declares a
  port; the composition root (`lib/app/app_assembly.py:159`) registers
  `routes.config.SettingsBackend` (`routes/config.py:901`), which runs
  `apply_server_config` (`:594`, the Save body the HTTP view now delegates to)
  and `get_server_config` (`:159`) the way Quart runs these sync views: off the
  event loop, in the application's context. The Router meets the Save's own
  validation errors (`error.message` verbatim), hot-reload and
  `server_config_change` audit entry. A channel toggle is the Settings channel
  PUT's service call (`update_account`).
- **Writable keys only.** `WRITABLE_PATHS` (`owner_settings.py:60`) lists six
  keys, each sent in the body the Settings page posts: `model_defaults` carries
  its sibling from the current read (the Save replaces it whole); a search body
  carries no `gateway`, so the configured gateway and the Optimizer's fetch
  skips are kept. Where the Save does not validate a value because the
  Settings page builds it from a typed control (list keys, model ids), the
  shape is checked first; `proxy_bypass_domains` is left to the Save's own
  validation. A Save that leaves the value unchanged is
  `owner_settings_not_applied`, never a success. Reads return only these keys:
  the Settings read also carries secrets (`mt_provider.api_key`).
- **Classes.** `_classify` (`:151`): destructive or external names →
  `owner_settings_not_delegated`; secret-bearing sections and secret-named keys
  → `needs_owner_action` naming the Settings place; anything else →
  `owner_settings_unsupported` with the writable list. None of them reads,
  writes or records anything.
- **The R9 gate.** Writes go through `owner_delegation.delegated_action`
  (`lib/router/owner_delegation.py:371`), scope `owner_settings`
  (`lib/lifecycle_approval.py:163`): the grant, an owner trigger, one audit
  record before the effect, settled with the outcome; reads need the grant but
  no trigger. Revoking "Act for me" stops the next call.
- **Channels** (`channel_preconditions`, `:236`; `_channel`, `:264`). The
  preconditions are reported first: account state, running session, and the
  question the 大象 supervisor asks before every launch (`launcher_session` for
  the account's login MIS, then the SSO adapter's `session_status`, which never
  requests an approval). Enabling with no login account, or while a relaunch
  would request a login approval, is `needs_owner_action` and changes nothing:
  the Router never sends a login approval by itself. A `kicked` account stays
  down (CHANNELS.md C6) unless the owner's own message asks to reconnect,
  because reconnecting takes the session back from the other login; a standing
  instruction cannot. An enabled live session is left alone (connector_owned).
- **Reachability.** `owner_settings` is Router-only, Worker-denied and outside
  the 448-byte direct core: it is reached by exact name and through
  `search_tools`/`execute_tools`.
- **Read-back across processes.** In the managed split the Router saves in the
  execution host. The Settings read now takes `search.skip_domains` from the
  persisted document instead of the serving process's module copy, so a save
  made in either process (Router, Optimizer, the agent search settings tool)
  reads back identically in the Settings page.

**Alternatives rejected:** importing `routes.config` from `lib` (breaks the
lib ← routes direction that `check_architecture.py` enforces); a loopback HTTP
call to the API host (the execution host holds no owner credential, and in open
mode it would work only because nothing authenticates); moving the whole Save
into a lib service now (about 500 lines of churn in a hot route and ten test
patch sites, for the same single body the port already shares); building a
section from the Settings read (it masks or projects secrets, so writing it back
would overwrite them); per-scope grant switches (the owner asked for one
switch); relaunching a channel whose SSO login needs the owner (an unprompted
login approval on the owner's phone).

**Residual risks.** Hot-reload stays process-local, exactly like the Settings
Save: a Router save reloads the execution host (tasks, fetch tools, channel
bridges) while the API host keeps its module copy (its dispatcher, its proxy
bypass list) until it restarts, and a Settings Save leaves the execution host
stale in the same way (plan 2). Model ids for `model_defaults` are not admitted
against the routing catalog: the Settings Save accepts any text and only the
picker constrains the page (plan 1). The 大象 precondition runs the SSO
adapter's status query in a child process, which may refresh the shared
session but never requests an approval.

**Remaining gaps, ranked** (use, reversibility, cost of doing it once right):

1. Model routing (providers, offerings, enabled models): the most-read page
   (169 GETs); (e) and reversible, but its backend is `PUT /api/v1/model-routing`
   with revision CAS, a second backend port; then admit `model_defaults` ids
   through the same catalog. Keys stay (d).
2. Cross-process settings reload: both Save paths reload one process. Fix it
   once in the settings owner (the other managed process reloads after a Save),
   which also fixes the Settings page.
3. Feature flags (`POST /api/v1/features`): booleans, (a), 21 GETs.
4. Worker model: (e), already admitted by `admit_worker_model`; one call.
5. Presets and the cost experiment: (e); the Save already validates experiment
   transitions, so the work is specs for its errors.
6. Conversation title and settings; folder create, rename and reorder (projects
   are R12).
7. Scheduler pause, resume and trigger; timer cancel.
8. MCP server and tool toggles, limited to a secret-free subset.
9. My Day goals; memory add, edit and toggle (delete and clear stay (b)).

Never delegated: revealing a credential, subscription or device login, the
"Act for me" grant, lifecycle approvals, and deletes, exports, update or
shutdown without the owner's explicit instruction in the current request.

**Specs:** `tests/router/test_owner_settings.py` (Save bodies, read-back,
verbatim rejection, not-applied, shape refusals, secret-free reads, the grant,
trigger and revocation, classes (b)/(c)/(d) without side effects, channel
preconditions, login approval, kicked and connector_owned rules, reachability),
`tests/router/test_owner_settings_backend.py` (the real Save and read through
the composition root's backend: identical read-back in the Settings page, also
with a stale module copy, the gateway kept, the Save's own validation error),
`tests/router/test_delegated_action.py` (the shared gate) and the tool-name
budget guards in `tests/router/test_router_tool_name_budget.py` and
`tests/tasks_pkg/test_coordinator_tool_name_budget.py`.

### R12. A project always has its Project Router, and the Global Router can manage projects

Owner decision 2026-11-09, verbatim (Turn
`3315ee59-e227-4025-8246-2aee9a9a543e` of
`coordinator-1a390889aa954da2b18551d1b4a32625`):

> 总负责人要可以创建项目，设置它的默认路径啊啥的，并设置项目里的coordinator。以后我们创建项目之后，
> 就默认创建负责人吧，也不需要后面再创建。就反正人能干的事情，总负责人都要能干

**Problem.** A project (a sidebar folder preset) got a Project Router only when
something first forwarded work to it; the Router had no way to create or
register a project, set its defaults, or configure the Project Router, so the
owner had to do all of that by hand in the UI.

**Decision.** Creating a project creates its Project Router in the same
operation, and the Router can do every reversible thing the owner does with a
project.

- **One operation for the UI and the Router.** `lib/router/project_lifecycle.py`
  (`ProjectLifecycle`) is the backend of both the folders HTTP adapter
  (`POST/PUT /api/v4/folders`) and the Router tool. It persists the preset
  through `FolderRepository` — the project settings dialog's own validation and
  record — and then ensures the primary root's Project Router with
  `forwarding._ensure_project_router`, the deterministic provisioning
  `route_to_project` has always used. Repeated or concurrent ensures converge on
  one conversation and one binding, so there is never a second Router for one
  project, and two presets of one root share its one Router. A Router that
  cannot be provisioned while the preset saves (storage unavailable) never
  undoes the owner's save: the result names the cause and the next ensure
  provisions it.
- **Defaults are what the dialog edits**: `name`, `color`, `projectPaths` (the
  first is the primary root), `readOnlyPaths`, `collapsed` and `order`. A change
  to the roots ensures the new primary root's Router; the previous root keeps
  its own. A rename or a collapse does not touch coordination. Model, thinking
  depth and capabilities belong to the Router's conversation, not to the
  project.
- **The coordinator create button is idempotent.**
  `ProjectLifecycle.create_coordinator` provisions a missing Router (so repeated
  clicks never mint a second one), and a bound Router is replaced only by an
  explicit create at its current `expectedRevision` — the workspace's
  *Replace coordinator* still replaces it. No bulk migration of older projects:
  an existing project with no Router gets one at the existing first-access
  points — a forward, a preset save, this tool, or the create button.
- **Creating a folder writes only that folder and Tofu's standard project
  preparation.** `create` makes the folder with the file picker's own
  `create_directory`; registering an existing folder changes nothing in it.
  There is no eager Git initialization here: Worker delegation prepares a root
  on first use, exactly as before.

**Tools and authority.** One tool, `router_project`, with an `action` field:

| action | class | effect |
|---|---|---|
| `list`, `get` | passive read | projects with their Router's availability; the Router's settings |
| `create`, `register`, `update_defaults`, `configure_router` | reversible, under the R9 delegation (scope `project_lifecycle`) | one audit record per action, with the owner's trigger |

The writes need the owner's grant (R9, one switch now covering this scope too),
the owner's trigger and a `reason`; each takes an `idempotency_key` and one key
applies once (a retry reads the state back instead of acting again, and an
interrupted action is named as such). `configure_router` ensures the Router and
then writes its conversation's settings through the composer's resolver over
every current choice plus the change — so an unnamed setting keeps its value —
and its Worker model through `set_worker_model`, the backend of
`/api/v4/router/worker-model`. Both are the exact UI write paths; only
composer-authored settings are settable, and the project's roots are never
changed there. A model choice must be routable for the owner, like the Worker
model.

Deleting or unregistering a project and deleting files are **not** added: they
are destructive and need an explicit owner instruction in the current request,
never inference from this grant. The owner can widen this later.

**Alternatives rejected.** A second, simpler provisioning for project creation
would split the identity derivation between two owners and allow two Routers
for one project. Auto-creating on every passive read (the coordination GET,
the folders GET) would make a read a write. Eagerly running
`prepare_managed_repository` on registration would modify an owner's existing
folder at the moment it is merely registered. Deleting the preset or the
folder, or returning a replayed action without the current state, were also
rejected.

### R13. Project leads message each other across projects (owner request 2026-11-11)

Owner request, verbatim (relayed by the tofu Project Router, 2026-11-11 about
22:00 CST):

> 为什么ndp说他不能和其他项目负责人还有和你沟通，其实是不是应该允许，这样有些话我不用重复说了？
> 他的目标就和parallel那两篇一样啊，越快越好，顶级会议。……然后主要是为了共享资源，不然他不知道我有什么可以用。

**Problem.** A lead could message only conversations of its own project
(`send_project_message` checked the target against the sender's project). The
ndp Project Router could reach neither the parallel Project Router nor the
Global Router, so the owner repeated goals and context to each lead, and no
lead could learn which of the owner's resources (GPU queues, keys, storage) it
may use.

**Decision.** The owner's current *leads* (the Global Router, the bound
Coordinator of the router home, and every Project Router, the bound
Coordinator of a project) send, receive and consider project messages across
projects. Nothing else changes.

- **One live rule.** `project_messages.lead_role(owner, conversation)` reads
  the conversation's project and that project's binding (the Watchdog path is
  not a project). It is checked at send (`send_project_message`, and an
  outgoing `get_project_messages` read), at acquisition and at every logical
  and physical input admission (`validate_sender`, which now names the
  recipient). Both ends must lead at that moment: a rebound lead keeps its
  committed originals as evidence but sends or receives nothing new across
  projects.
- **Same semantics as every project message**: the recipient's mailbox
  (owner, recipient project, recipient), content bounds, idempotency per key,
  kinds, consideration receipts and `reply_to`. The original records
  `senderProject`. The recipient's model input marks it with `sender-role` and
  `sender-project` and the contract's `crossProject.label`: nonhuman context,
  not the owner's words, no permissions, no transcript access and no control
  of Workers in either project.
- **Who stays out.** Workers, ordinary chats and subagents stay inside their
  project and relay through their lead; a lead never addresses another
  project's Worker; the Watchdog keeps its own R7 path and is not a lead.
  Owner identity is explicit, so another owner's conversation is never a
  sender or recipient (default deny).
- **Discoverability.** `list_project_leads` (Coordinator family, outside
  continuation grants; Workers are denied it) lists the owner's other current
  leads: role, name (Global Router or the project directory name), project
  path, conversation ID and `purpose`, the lead's own one-line task-board
  summary. It pages like `get_router_projects`, and its first page describes
  the owner's resource registry. A continuation answers a lead it heard from
  with `send_project_message`, which every grant holds; keeping the directory
  out of grants keeps a grant's whole set in the direct core, and on the Claude
  route the name budget hides it behind `search_tools`/`execute_tools`
  ([LLM_COST_OPTIMIZATION.md section 6](LLM_COST_OPTIMIZATION.md#6-gateway-tool-name-budget)).
- **Resource registry.** `RESOURCES.md` at the root of the owner's router home
  (`<data root>/router/<owner>/RESOURCES.md`, `home.owner_resources`); the
  location was agreed with the registry lane (RESOURCE-REGISTRY-1). Leads are
  dispatch-only and have no file tools, so the tool returns the registry's
  path and size, and its text with `include_resources=true` (read without
  following a link; a long text arrives in pages through the artifact
  readers). Workers read it by absolute path; Global Router home Workers update
  it through ordinary reviewed publication.

  **Seed.** R13 named the file before anything wrote it, so a home whose
  registry lane never published reported `available=false` while leads were
  pointed at it. `router_home_path` now writes `RESOURCES.md` once — at
  creation and on the first open of a pre-existing home that lacks it — from
  `_RESOURCES` in `lib/router/home.py`: the queues of
  `scripts/hope_patrol/queues.json` (the compute patrol's documented seed,
  itself sourced from the owner's 2026-11-13 Hope account audit), the queue
  and resource-code observations of the Hope skill's `references/queues.md`
  and `references/rsi-queues-2026q4.md`, and key/storage rows that name only
  how to obtain each one. A seed row names its source and read date and is
  not a live capacity or grant. The write is write-once: an owner or
  reviewed-Worker edit — or deletion — is never overwritten or restored, so
  `available=false` remains truthful when the owner removes it. The seed
  holds no secret value and no secret-shaped material.
- **Audit.** Every cross-project message is an immutable original in the
  recipient's owner-scoped mailbox (sender, `senderProject`, kind, time and
  receipts). The sender reads it with `get_project_messages` (outgoing), the
  owner over `GET /api/v4/project/coordination/messages` (`senderProject` in
  `ProjectMessageView`). It shows in the recipient Router's conversation as
  the labelled `inbox_delivery` system note and in the sender Router's
  conversation as its tool call.
- **No new knobs**: no per-project quota, rate or allow-list. Owner scope and
  the existing message bounds are the only limits.

### R16. Owner off-limits Hope queues (Global Router)

**Problem.** An owner queue ban must bind every Tofu submission path, not only
the project wrapper that happened to contain a check. `HOPE_QUEUE_POLICY.json`
at `router_home_location(user_id)` is the machine-readable source, published by
the Global Router through ordinary reviewed publication. The schema is
`contracts/hope_queue_policy_v1.schema.json`; the v1 entry `m17` is global.
No code writes the file or supplies a second queue allow-list.

**Decision.** The path is derived from owner identity and runtime paths, not a
setting. The hope-mcp child receives it as `TOFU_HOPE_QUEUE_POLICY` beside
`TOFU_MCP_OWNER_ID`; submission tools declare `_meta["tofu/queue-policy"]` and
then receive the canonical project in `_meta["tofu/project"]`. Each submit,
rerun, clone and background retry reads the file again. A missing, unreadable,
or unparsable file enforces exactly the v1 `m17` fail-safe default and warns;
it never disables m17 or refuses all jobs. The `run_command` environment also
prepends generated `hope`/`mlp` launchers (after any project `.venv`) which
check the same file before executing the real CLI. A refusal is typed
(`queue_off_limits`, with the policy path and entry id) and names the entry.
The compute provider maps it to a definitive `hope_queue_off_limits`, never an
unknown write.

**Alternatives rejected.** A block in `RESOURCES.md`: parsing a large prose
registry is fragile. A configurable policy path, project allow-list, or
server-env override: a new knob or a bypass. Enforcing only in the bridge:
the final queue may live in a `.hope` body or a resumed retry, so the
submission boundary must read it too. A PATH shim alone: an absolute-path CLI
call bypasses it, so it is a guard, not a sandbox.

**Alternatives rejected.** Relaying every cross-project word through the
Global Router, today's practice: it costs a router turn per message, loses the
reply thread and is exactly the repetition the owner asked to end. Letting
every same-owner conversation, Workers included, message across projects:
Workers would commit to other projects behind their lead's back. Granting
leads `get_router_projects`: it exposes every project's activity facts (Worker
counts, reports, commitments, open questions), more than a directory needs.
Per-project allow-lists, quotas or rate settings: new knobs (constraint 5)
where owner scope and the message bounds already bound the traffic. A
separate cross-project audit ledger or a copy to the Global Router: a second
record of the same originals, or router context filled with traffic it never
asked for. The registry lane's first location under `tofu-artifacts/` or a
configurable path: product code may locate an owner-wide file only from owner
identity and runtime paths (constraint 1), so a host path is a single-user
shortcut and a setting a new knob. Injecting the registry into every lead
turn: tens of kilobytes in every round of every lead, read or not.

### R15. A continuation keeps its request's catalog; owner-only decisions are gated at execution (owner verdict 2026-11-13)

The owner, verbatim: 「自动续跑会改工具清单…？这是bug吧，不应该继承原始任务的吗，工具清单不也是上下文一部分吗」,
after 「之前不是发过吗？咋又缺工具」.

**Problem:** on 2026-11-13 the Global Router's automatic continuation could not
send the finished Tofu v6 deck to the owner's 大象 chat (no `channel_send`), had
no `restart_server` for the R4 release and could not forward an owner message as
authority (no `route_to_project`), and no Worker could stand in (`lib/channels/tools.py`
refuses a Worker's send; `workerDeniedTools` holds the Router tools). A continuation's
catalog came from a second, hand-maintained policy instead of its role's, cut twice:

Lines are at `07e871bbb7` (unchanged where this lane rebuilt it on canonical), before the fix.

1. `capture_worker_policy` froze `controls = continuationTools ∩ visible`
   (`lib/project_dispatch/delegation.py:218`, the list in
   `contracts/project_dispatch_v1.schema.json`), the continuation's profile carried
   those names (`lib/project_dispatch/continuation.py:324`), its assembly cut the
   role's catalog to `scope.granted_tools(profile)`
   (`lib/tasks_pkg/orchestrator/_tool_assembly_prep.py:287`) and every call was
   admitted against the same names (`scope.admit_call`, `lib/project_dispatch/scope.py:416`:
   "Coordinator call exceeds its original continuation policy"). Each later role tool needed a per-tool patch
   (`poll` for `await_project_workers`, the scheduler family); every other one was
   silently missing.
2. For a Router continuation `_build_coordinator` resolved no role (`scope = None`,
   `lib/tools/registry/_build.py:434`), so the Router's schemas were never assembled.

A nonhuman wake (report relay, Watchdog notice, scheduled wake, restart notice)
starts an ordinary Turn of the role conversation (`dispatch_scheduled_turn`), whose
catalog was already the request Turn's; it lost tools only when steered into, or
running as, an automatic continuation. Missing from continuations, measured on
`07e871bbb7` by `tests/project_mod/test_continuation_catalog.py` (GR: Global Router,
PC: Project Coordinator):

| Tool | Role | Deliberate or accident | Evidence |
|---|---|---|---|
| `create_conversation`, `dispatch_project_assignment` | PC, GR | deliberate at origin, superseded by the verdict above | the first `continuationTools` (2026-10-22, `91b4c7e205`) was `COORDINATOR_TOOLS` minus exactly these and `ask_human`: a continuation "can delegate and review Workers". Execution keeps their gates: a created conversation runs nothing, a dispatch needs the assignment's existing human grant at its current revision |
| `await_device` | PC, GR | accident | in the ceiling since 2026-11-02 (`a4077d6241`), never listed |
| `list_project_leads` | PC, GR | accident | R13 put it in the ceiling so leads reach each other without the owner; never listed |
| `read_tool_artifact`, `search_tool_artifact` | PC, GR | accident | in the ceiling since 2026-11-08 as the only recovery of a partial result; a continuation got unreadable pointers |
| `search_memories` | PC, GR | accident | in the ceiling so a model sees it has memory; never listed |
| `schedule_create`, `schedule_list`, `schedule_manage` | PC, GR | accident; SCHEDULE-AUTHORITY-1 (`07e871bbb7`) derived the family per grant, now part of the role catalog | [scheduling](modules/scheduling_ops.md#agent-created-schedules) |
| `channel_send`, `channel_read`, `channel_threads`, `channel_outbox`, `channel_outbox_approve`, `channel_configure_thread`, `channel_subscribe`, `channel_unsubscribe` | GR | accident | `CHANNEL_TOOLS` joined `ROUTER_TOOLS` (CHANNELS.md C3) and were never listed |
| `restart_server` | GR | accident | R9 serves Router Turns without an owner message of their own (standing instructions); HARNESS-1A-2 diagnosis `91e7b05323` |
| `route_to_project` | GR | accident | R2: a forward's authority is verified at execution |
| `get_router_projects`, `router_instruction_create`, `router_instruction_list`, `router_instruction_cancel`, `router_portfolio`, `router_project`, `owner_settings`, `repair_project` | GR | accident | R4, R10, R11 and R12 added them to `ROUTER_ONLY_TOOLS`; each has its own execution gate |

Deliberate in every Turn of a role, and unchanged: its ceiling (no file, shell,
browser, program or direct MCP work; `kill` outside), programmatic calling off, the
C11 review ceiling of a Turn reviewing other people's messages, the Watchdog's own
ceiling, and `workerDeniedTools` for Workers.

**Decision:** a continuation keeps the catalog of the request it continues. One
policy, the role's ceiling (`coordinator_policy.coordinator_tool_ceiling`), decides
the catalog of every Turn of a role conversation: the owner's request, a nonhuman
wake and an automatic continuation. A continuation grant names its role by the
conversation it is bound to (`scope.coordinator_grant_scope`: the grant's project
and owner, the scope `bind_account` pins), so `scope.granted_tools(profile,
assignment=...)` admits that role's ceiling plus the names the grant recorded,
`_build_coordinator` assembles that role's schemas, and the continuation's assembly,
from the request's frozen settings (`TrustedDispatch.capture`), yields the request
Turn's catalog, wire and direct core. No owner setting narrows a role's catalog
(`disabledTools` governs MCP tools only). The catalog carries no authority; every
owner-only decision is refused at execution with a typed reason:

- `restart_server` stays the Global Router's (`ROUTER_ONLY_TOOLS`,
  `execute_router_tool`, `workerDeniedTools`) and runs only through R9: the owner's
  grant (`owner_delegation_not_granted`), then the owner's explicit approval of that
  restart, which is the owner's own message of the request the continuation
  continues, an owner message steered into it or an active standing instruction
  (`owner_delegation_trigger_missing`, `_malformed`, `_invalid`), restart protection
  and the gate's acceptance. The approval is recorded verbatim in the delegated
  action; as for every R9 action, the gate verifies that the words are the owner's,
  not what they ask.
- `channel_send`: the owner's command chat always receives the Global Router's
  message; for other people, a Turn without the owner's own request (a
  continuation's input is nonhuman) only drafts, which the owner approves (C5, C11).
  Only the Global Router sends: a Project Router and a Worker report a file to it,
  and their refusal is unchanged.
- `route_to_project` carries authority only for a verified owner message: the one of
  the request the continuation continues (its authorization source, a steered one
  read in that request's own reply Turn), an owner message steered into the reply,
  or a standing instruction, which the Project Router admits only through
  `verify_forwarded_turn`. Anything else is the typed refusal
  `owner_message_required`, naming the plain path: `send_project_message`, whose
  message carries no authority.

The grant's recorded `continuation.tools` keeps its shape, because every release
validates it on read (`_check_record`, `validate_profile`) and a rolled-back server
must still read new grants; it records what the request froze and decides no
catalog. `asks_human` (the accepted config's `humanGuidanceEnabled`, fenced by
`_restored_identity`) still reads that record, so an accepted attempt's identity is
unchanged across the restart; every grant since 2026-11-02 records `ask_human`.
After the restart that loads this, every open request's continuations hold their
role's catalog, whatever their grant recorded.

**Alternatives rejected.** Listing the missing names in `continuationTools`, as
SCHEDULE-AUTHORITY-1 did for one family: the hand list the next role tool misses
again, which is how this happened twice in one night. Freezing the request Turn's
exact catalog into each grant: an earlier release refuses records naming tools
outside its schema, so a rollback could not read its own dispatch records, and a
role tool added later would again be missing from in-flight requests while the
owner's own Turns hold it. A Router-only admission rule for `restart_server` (the
`91e7b05323` proposal) or any per-tool admission: special cases where one role
policy decides. Delivering a non-owner `route_to_project` as a plain message: a
second `send_project_message`, which would also let a Turn reviewing other people's
messages (C11, which holds `route_to_project` to apply standing instructions) wake
Project Routers with their text, and a Router that believed it forwarded the
owner's request would report that it had. Letting Workers send to the owner's
channels: delivery is a lead's responsibility, and a Worker reports its file to its
lead.

**Verification.** `tests/project_mod/test_continuation_catalog.py` (per role: the
continuation's catalog, wire and direct core equal the request Turn's, also on the
Claude name-budget route; nonhuman wakes; the restart, forward and Coordinator
refusals); `tests/channels/test_tools.py` (a nonhuman Router Turn drafts for other
people, a non-Router conversation is refused); the
continuation and scheduler suites; and the isolated-instance journey
`tests/router/test_continuation_owner_journey.py`.

## Interfaces

Session execution control uses the common Coordinator tools
`get_conversation_status` and `control_conversation`. The Global Router can
operate any conversation of its owner; project Coordinators remain scoped to
their project. Continue and Stop reuse the browser command service and preserve
target execution authority. See
[session execution control](PROJECT_COORDINATION.md#session-execution-control).

- `lib/router/home.py` — `router_home_path(user_id) -> str` creates (idempotently)
  and returns the owner's router home Git repository; `router_home_location`
  and `is_router_home` compute the identity without effects;
  `owner_resources(user_id, *, content)` reads the owner's `RESOURCES.md`
  without following links (R13).
- Cross-project lead messages (R13) — `lib/conversations/project_messages.py`
  (`lead_role`, `validate_sender(..., recipient=)`, the `crossProject` contract
  section) and `list_project_leads` (`lib/router/projects.py` `project_leads`,
  through `execute_coordinator_tool`).
- `lib/router/binding.py` — `global_router(user_id, *, create, client=None) ->
  dict` returns the binding view (`conversationId`, `projectPath`, `revision`,
  `availability`, `capabilities`) and creates the router conversation on demand.
- `lib/router/policy.py` — `ROUTER_TOOLS`, `ROUTER_ONLY_TOOLS`, `CHANNEL_TOOLS`,
  `router_scope(task)`, `is_router_scope(scope)`, `ROUTER_ROLE_ADDENDUM`;
  enforcement stays in `lib/tasks_pkg/coordinator_policy.py`
  (`coordinator_tool_ceiling`).
- `lib/router/tools.py` — `execute_router_tool` behind
  `execute_coordinator_tool`: `route_to_project` (`forwarding.py`, also
  `verify_forwarded_turn`), `get_router_projects` (`projects.py`; pages follow
  the project dispatch history page), `router_instruction_create|list|cancel`
  (`instructions.py`; `RouterInstructions.pause|resume` are owner HTTP only),
  `restart_server` (`owner_delegation.py`, R9), `owner_settings`
  (`owner_settings.py`, R11), `router_project` (`project_lifecycle.py`, R12:
  create, register, read and configure projects) and `repair_project`
  (`project_repair.py`, R9 `project_repair`).
- `lib/router/project_repair.py` (R9) — `repair_project`, `lead_state`,
  `deliver_repair_outcome` (called by `lib/tasks_pkg/project_settlement_sink.py`),
  `repair_conversation_id`, `REPAIR_MARKER`; specs `tests/router/test_project_repair.py`.
- `lib/router/owner_settings.py` (R11) — `call` (list/get/set/channel),
  `channel_preconditions`, `WRITABLE_PATHS`, and the settings port
  `register_settings_backend`, filled by the composition root
  (`lib/app/app_assembly.py`) with `routes.config.SettingsBackend` (the Settings
  page's `apply_server_config` / `get_server_config`).
- `lib/router/owner_delegation.py` (R9) — `restart_server`,
  `delegated_conversation_control` (used by `session_control.control_session`),
  `delegated_action` (the shared gate for `owner_settings` and
  `project_lifecycle`), `resolve_trigger`, `router_requester`,
  `owner_delegation_state`; the grant,
  audit records and restart acceptance are `lib/lifecycle_approval.py`
  (`set_delegation`, `check_delegation`, `begin_delegated_action`,
  `record_delegated_refusal`, `settle_delegated_action`,
  `accept_delegated_restart`, `delegated_actions`, `observed_restart`, the
  `restartNotices` store `restart_notices`/`note_restart_notice`/
  `claim_restart_notice`/`finish_restart_notice`); protected steps are
  `lib/runtime/protected_steps.py`.
- `lib/server/restart_notice.py` (R9) — `deliver_restart_notice(restart_id)`,
  `deliver_restart_notices()`, `notice_command_id`, `render_notice`; specs
  `tests/router/test_restart_notice.py`.
  Schemas: `contracts/router_v1.schema.json` through `lib/router/contract.py`.
  `forwarding.owner_steer_carriers` labels steered owner messages in the router's
  model view; `lib/turn_initiation.py` owns `steer_author` and
  `steered_input_turn`.
- `lib/router/relay.py` — `relay_project_report(owner, report, *, client, dispatch)`,
  `relay_project_question(owner, question, *, client, dispatch)`; the `ask_human`
  handler (`lib/tasks_pkg/handlers/misc/_human.py`) relays questions and judges
  steered authors with `require_human_turn`.
- `lib/project_dispatch/delegation.py` — `resolve_worker_model`,
  `admit_worker_model`, `owner_default_model`.
- Channel tools (`channel_*`, [CHANNELS.md](CHANNELS.md)) are in `ROUTER_TOOLS` by
  name; channel delivery resolves the Global Router through `global_router`.
- `await_device` is in `ROUTER_TOOLS` through the coordinator ceiling: a router
  whose next dispatch needs an offline device of the owner registers a durable
  device-online wake of its own conversation and ends its turn; the wake is a
  nonhuman notice and grants no authority
  ([remote execution](modules/remote_execution.md#waiting-for-an-offline-device)).
  Workers cannot wait (`workerDeniedTools`); they report the dependency.
- `lib/router/watchdog/` — `binding.router_watchdog(user_id, *, create, client=None)`
  and `watchdog_location`; `policy.WATCHDOG_TOOLS`/`WATCHDOG_ROLE`;
  `signals.FaultSignals` and `report_turn_failure|report_recovery_failure|
  report_outbox_fault|report_channel_status|report_compute_stall|
  report_compute_breach|resolve_compute_stall`; `snapshot.health_snapshot`;
  `review.configure_review|review_state|schedule_recheck|run_scheduled_review`
  (the scheduler's `router_watchdog_review` handler); `tools.execute_watchdog_tool`
  behind `execute_coordinator_tool`. Schemas and records: the `watchdog*`
  definitions of `contracts/router_v1.schema.json`.
- `ScheduledTaskManager.ensure_owner_task(principal=..., ...)` installs one
  owner-authorized keyed task with the identity built-ins use; `create_task`
  accepts a caller-derived `task_id` so a retried creation never schedules twice.
- HTTP (`routes/api_v1/router.py`): `GET /api/v4/router` (read),
  `POST /api/v4/router` (ensure, Idempotency-Key), `GET|POST
  /api/v4/router/worker-model`, `GET /api/v4/router/instructions`, `POST
  /api/v4/router/instructions/{pause,resume,cancel}`, `GET
  /api/v4/router/watchdog` (read, never creates), `POST /api/v4/router/watchdog`
  (ensure, Idempotency-Key), `POST /api/v4/router/watchdog/review`, `GET|POST
  /api/v4/router/owner-delegation` (R9: read the grant and its audit records;
  grant only from the owner's UI session, revoke from any owner request);
  `GET /api/v4/router/projects` (`get_router_projects` in camelCase through
  `projects.router_projects_wire`; `cursor` is the previous `nextCursor`);
  DTOs in `frontend/src/api/router.generated.ts`.
- Browser (R8): `frontend/src/features/global-router.ts` renders the main
  coordinator card (总负责人 / Main coordinator) with the project coordinator
  card's classes into `#globalRouterEntry`, the leader slot just above
  `#projectCoordinatorEntry`; activating it ensures and opens the router. On
  the router conversation it shows a compact header: one sentence on what the
  main coordinator does, the Worker model control, the standing-instructions
  list (`router-instructions.ts`: read only when opened, state, scope, pause,
  resume, cancel, cursor paging) and the controls other owners contribute
  (`hosts.controls`). Project Routers show the same Worker model control in
  the task board's footer on their coordinator conversation surface
  (`project-coordination-surface.ts`). `router-owner-delegation.ts` contributes
  "Act for me" (代我操作, R9) to that header: the grant switch, its state, an
  advisory note for an open-mode grant and the last audited use (read when
  created and when opened, no polling). `router-watchdog.ts` contributes the
  health-check fold to that header (its state: on, on failures only once the
  owner stopped the periodic review, or off while no Watchdog is known; one
  sentence on what it does; a button that opens the Watchdog conversation,
  ensuring health checks first when no Watchdog is known) and, on the Watchdog
  conversation, a one-line header with a way back. It reads when created and
  again after each main coordinator ensure (`ensured()` of
  `global-router.ts`), since that ensure turns health checks on; its
  `location()` lets presence omit the
  Worker model control and the task board there, since the Watchdog never
  delegates and authors no board. Both owners are created before the locale
  catalog loads, so labels carry `data-i18n`/`data-i18n-title` keys that
  `ready()` and every language switch translate; parameterized text (the
  Worker model's read and write failures) re-renders on `tofu:language-change` and removes that
  listener on teardown. The retained presence section hides the card where a
  folder is selected or the listed conversations belong to a project (that
  project's coordinator card takes the slot; a folder that names no directory
  shows its coordinator row with the way to add one, see
  [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#coordinator-started-sidebar-conversations-owner-decision-2026-11-03)),
  gives the router home and Watchdog location no project Coordinator row,
  hides `#projectBar` for chats in those homes, and publishes
  `runtimeScope.routerConversationIds()` so the conversation list never shows
  either chat as a row or counts it in a folder count. Each owner's `conversationId()` is the bound
  conversation its header marks, which the composer steers without the
  steer/queue prompt (RENDER_CONTRACT.md "Translation attribution"), and each
  announces a new home or conversation through `changed()`, which re-renders
  the list. `shell-localization.ts` shows the server's role titles
  (`ROUTER_TITLE`, `WATCHDOG_TITLE`, `PROJECT_ROUTER_TITLE`) through the same
  localized names. The presence section bridges these owners to the API
  (`Api.project.routerWorkerModel`, `setRouterWorkerModel`,
  `routerInstructions`, `pause|resume|cancelRouterInstruction`,
  `routerWatchdog`, `ensureRouterWatchdog`) and to the composer model picker's
  catalog.

## Verification

Executable specifications live beside each owner (`tests/router/`,
`tests/channels/`, and the existing compaction/storage/coordination suites).
The router suites cover the home repository, policy ceiling and role,
forwarded-origin verification over a real storage authority (forged, edited,
synthetic, foreign-owner, non-router, self and re-forwarded origins), steered
owner messages on SQLite and native (`test_steer_forwarding.py`: author capture,
verbatim forward, forged/edited/synthetic/foreign-owner/non-router/self steers,
executor and client writes, model labels; `test_steer_authorship.py`),
forwarding replay, steer/start delivery, report relay, question relay
(`test_question_relay.py`; `test_question_answer_seam.py`, SQLite and native: a
blocking question's notice reaches the router naming the 大象 command chat, the
owner's command-chat answer forwarded verbatim ends the wait, forged/edited
forwards and nonhuman steers do not), standing instructions
including pause/resume, Worker model resolution, `tests/api/test_api_v4_router.py`
and the browser owners (`frontend/src/features/global-router*.test.ts`, including
the boot-before-catalog translation spec `global-router-i18n.test.ts`,
`router-worker-model.test.ts`, `router-instructions.test.ts`, the placement
spec in `project-coordination-entry.test.ts` and the live-reply delivery spec
in `conversation-project-draft-runtime.test.ts`); the role-title sentinels
stay paired by `tests/router/test_role_title_sentinels.py` and are rendered
as specified in `tests/i18n/test_shell_localization.py`. The real-browser
journey `tests/frontend/test_main_coordinator_browser.py` covers R8 end to
end: the single leader slot in both scopes and in a folder that names no
directory, the header with health checks on by default and no switch beside
them, the shared chevron on its folds, the Watchdog chat and back, no project
location and no rows or counts for router chats, in English and Chinese. The Watchdog suites are
`tests/router/test_watchdog_policy.py` (ceiling, role, registry, worker denial,
fault kinds, the terminal hook, review paging) and
`tests/router/test_watchdog_authority.py` (SQLite and native: provisioning, one
signal and one wake per fault and episode, the failing-Watchdog rule,
owner-scoped snapshots, resolution, messages and notices, the keyed review,
the default-on install that keeps the owner's later choice, idle-skipping
reviews and re-checks, owner-only review changes, and a real idle start
without a request account), `tests/api/test_api_v4_router.py` (the main
coordinator's ensure turns health checks on and never fails on them), plus
`frontend/src/features/router-watchdog.test.ts` and the Watchdog bridge in
`project-coordination-entry.test.ts`. The
end-to-end acceptance: a human message in the Global Router forwards to a
Project Router that delegates a Worker; the Worker's result produces a project
report that relays back to the Global Router; a 大象 command-chat message
reaches the Global Router as human input and its reply leaves through the
outbox exactly once.

Cross-project lead messages (R13) are specified by
`tests/router/test_cross_project_lead_messages.py` (SQLite and native: send,
receive and consider between leads and with the Global Router, the label and
the owner's audit view, Workers and the Watchdog kept out, another owner
denied, rebound leads, the directory and the resource registry); the name
budget facts by `tests/tasks_pkg/test_coordinator_tool_name_budget.py`.
