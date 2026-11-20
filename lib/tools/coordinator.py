"""Model-visible dispatch-only Coordinator tools; handlers reuse existing owners."""

from lib.openapi import inline_schema_definition
from lib.project_dispatch.contract import schema as _dispatch_schema
from lib.router.contract import schema as _router_schema


def _tool(name, description, properties, required):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False},
    }}


COORDINATOR_TOOLS = [
    _tool('get_conversation_status',
          'Inspect the canonical execution state of an owned conversation: Turns, current Attempts, '
          'running/queued state, errors, settlement and available resume operations. Passive; never wakes it. '
          'Use when the owner delegates management of a concrete Session, then get_conversation for content. '
          'Project Coordinators inspect their project, and any owned conversation while the owner\'s full '
          'delegation is granted; the Global Router can inspect any owned conversation. '
          'Pass turn_id to read exactly one Turn of a conversation of any size. Without it you get the newest '
          'Turns of one lane (default main) that fit one readable result; pass window.next_before_ordinal as '
          'before_ordinal (with the same lane_id) for older Turns. '
          'Preserve the Turn currentAttemptId and projectionRevision for control_conversation.',
          {'conversation_id': {'type': 'string', 'minLength': 1},
           'turn_id': {'type': 'string', 'minLength': 1},
           'lane_id': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                       'description': 'Turn laneId to list; default main.'},
           'before_ordinal': {'type': 'integer', 'minimum': 0,
                              'description': 'Exact window.next_before_ordinal from the previous result.'}},
          ['conversation_id']),
    _tool('control_conversation',
          'Operate an existing Session within the current authorized request using the same commands as the owner: '
          'continue (Continue generating), checkpoint_resume, regenerate (retry from the start), or stop. '
          'Read get_conversation_status first. Pin the Turn, its source Attempt and observed projection revision; '
          'use only a resume operation offered by settlement.resumeOptions, copying its anchor when present. '
          'Resumes retain the target model, config, context and existing grants. Stop targets only that exact '
          'current Attempt and withdraws its conversation dispatch as the owner Stop does. Resuming an earlier '
          'Turn completes it in place and preserves every later Turn. You never discard history: regenerate on '
          'a Turn that is not the latest of its lane is refused with code later_turns_unacknowledged and the '
          'later Turns it would delete (later_turns); only the owner can approve that in their own UI. Stop '
          'does not need expected_revision. '
          'hold pauses a Worker session a lead dispatched without losing it: its exact current Attempt stops '
          'at its checkpoint (Turn interrupted, cause lead_hold) and the Worker shows as held, while its '
          'grant, checkout and assignment stay; use it to throttle lanes (for example under provider quota '
          'pressure) instead of stop, which withdraws the grant so the Worker can never continue. hold needs '
          'no expected_revision. resume continues a held Worker in place under the same assignment, with the '
          'resume operation its settlement offers: pin the held Turn, its Attempt and projectionRevision. A '
          'cancelled (revoked) Worker cannot be resumed. '
          'follow_up gives an ended Worker session you dispatched its next instruction (instruction): pin its '
          'latest settled Turn, Attempt and revision; the instruction becomes that conversation\'s next Turn and '
          'nothing is regenerated. An unreviewed, undelivered result continues under the same assignment, grant '
          'and checkout; a reviewed or delivered one continues as a new assignment in the same conversation '
          '(the result names assignment_id). An accepted result whose delivery a conflict holds, or whose accept a '
          'rework superseded, also continues as a new assignment that first merges its checkpoint, and the '
          'successor of a rework links to it (rework_of). Prefer follow_up to a new Worker for the same goal, and to '
          'review decision=rework when the same Worker should fix its own result; then poll its assignment_id. '
          'Never send a synthetic human message or approve '
          'a human question. Project Coordinators control their project; the Global Router can control any owned '
          'conversation. Any Turn of this Coordinator, an automatic continuation included, controls the Workers '
          'its binding dispatched in any request. '
          'Any lead (the Global Router, or a Project Coordinator under the owner\'s full grant): when your current '
          'request has no owner message of its own (a standing instruction, a nonhuman notice, a wake), or the '
          'session belongs to another project you were asked to help, pass owner_delegation for continue, '
          'checkpoint_resume, stop, hold or resume to use the grant ("Act for me"). The delegation never '
          'allows regenerate. Delegated control is '
          'recorded in the '
          'owner\'s audit log, and a session of another project tells that project\'s lead (target_lead_notified). '
          'set_model changes the model of an existing Session without replacing it: the conversation, context, '
          'grant, checkout and tool checkpoint are preserved, and only the next accepted attempt (Continue, '
          'follow_up or a continuation) uses the new model — an attempt already running is never rewritten. '
          'Use it for a legitimate provider/model routing change (for example a provider content-filter '
          'refusal), with model (a routable model id, or null to clear the session override) and '
          'reason; turn_id and attempt_id pin the session state you read. This changes no permission and '
          'never rewrites provider output. For set_model, omit owner_delegation for the current owner request '
          'within its scope or to use the live full standing grant as a bound lead. The server checks the grant '
          'and records the delegated action using reason. You may still pass owner_delegation to name a '
          'specific owner trigger; an explicit object always uses the existing delegated path. '
          'After a lost response, retry identical arguments and idempotency_key. Acceptance is not completion.',
          {'conversation_id': {'type': 'string', 'minLength': 1},
           'turn_id': {'type': 'string', 'minLength': 1},
           'attempt_id': {'type': 'string', 'minLength': 1},
           'action': {'type': 'string', 'enum': ['continue', 'checkpoint_resume', 'regenerate', 'stop', 'hold',
                                                 'resume', 'follow_up', 'set_model']},
           'model': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 256,
                     'description': 'set_model only: the session\u2019s next-attempt model; null restores its original.'},
           'reason': {'type': 'string', 'minLength': 1, 'maxLength': 2048,
                      'description': 'set_model only: why the model should change; audited (max 2000 characters when using standing delegation).'},
           'expected_revision': {'type': 'integer', 'minimum': 1},
           'resume_anchor': {'type': 'object', 'description': 'Exact anchor from settlement.resumeOptions.'},
           'instruction': {**_dispatch_schema('request')['properties']['brief'],
                           'description': 'follow_up only: the Worker\'s next instruction.'},
           'attachments': {**_dispatch_schema('leadAttachmentReferences'),
                           'description': 'follow_up only. '
                                          + _dispatch_schema('leadAttachmentReferences')['description']},
           'owner_delegation': _router_schema('ownerDelegationAuthority'),
           'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
          ['conversation_id', 'turn_id', 'attempt_id', 'action', 'idempotency_key']),
    _tool('create_conversation',
          'Create an empty independent conversation in this Coordinator project, '
          'contained in this Coordinator’s collapsible sidebar conversation group, not a loose ordinary-list row. Does not start a task, send '
          'a user message, or authorize execution. Reuse the same idempotency_key '
          'when retrying. For work within the current authorized request, prefer '
          'delegate_project_work: it creates and starts its own Worker without '
          'a separate per-worker authorization step. Use dispatch_project_assignment '
          'only for an already-authorized assignment.',
          {'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
           'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
          ['title', 'idempotency_key']),
    _tool('dispatch_project_assignment',
          'Dispatch an existing human-authorized Project assignment into its independent '
          'worker conversation, contained in this Coordinator’s collapsible sidebar conversation group. Uses ordinary conversation Turn/task admission and the '
          'existing exact grant, budget, concurrency and capability limits. Cannot create '
          'or enlarge a grant; reports unauthorized/stale assignments rather than running. '
          'Only the existing grant profile is permitted. For new work within the '
          'current authorized request, use delegate_project_work instead.',
          {'assignment_id': {'type': 'string', 'minLength': 1},
           'expected_revision': {'type': 'integer', 'minimum': 1}},
          ['assignment_id', 'expected_revision']),
    _tool('get_project_assignment',
          'Read one owned same-project worker assignment: its brief and acceptance checks, execution '
          'state, the Worker\'s whole final answer (execution.result.content), review, publication and '
          'delivery. Passive only: never starts, retries, resumes or wakes a task. Its tool rounds and '
          'reasoning are in the Worker conversation (request.recipientId) through get_conversation.',
          {'assignment_id': {'type': 'string', 'minLength': 1}}, ['assignment_id']),
]


# The same schema governs the tool, publication service and durable retry intent.
from lib.conversations.coordinator_reports import CONTRACT as _REPORT_CONTRACT
_report = _REPORT_CONTRACT['$defs']['reportRequest']
COORDINATOR_TOOLS.append(_tool('report_to_user',
    'Proactively send a concise message to the human in this Coordinator conversation. '
    'Persists an assistant message and an unread sidebar reminder, including while the user is elsewhere. '
    'Use kind=delivery for verified delivery and kind=progress for a meaningful milestone; do not report routine '
    'status changes (keep the task board current with update_task_progress instead). Never ask in a report: ask '
    'the human with ask_human (blocking=false keeps you working, blocking=true only when nothing can continue). '
    'kind=blocked only states what a finish_project_request(outcome=needs_user) is waiting for. '
    'Never ask for permission the system already grants (reviewing, merging or publishing verified '
    'work, retries, re-delegation, cost) and never present internal pending work (reviews, dependencies, delivery) as the human\'s task. '
    'Title is a short plain-text summary; message contains the outcome, evidence and next action. '
    'Attach every file the human should open (resume, deck, document, report, table) with files: '
    'the human finds deliverables through these attachments, never by searching paths in your message. '
    'Name the board tasks the report is about with tasks: the human reads each task\'s reports and files '
    'by expanding that task on the board. '
    'This grants no work or authority. Retry unchanged content with the same idempotency_key. '
    'Sending a report is not proof the human has read or agreed to it.',
    {**_report['properties'], 'files': {
        'type': 'array', 'uniqueItems': True,
        'items': {'type': 'string', 'minLength': 1,
                  'maxLength': _REPORT_CONTRACT['$defs']['reportFile']['properties']['path']['maxLength']},
        'description': 'Paths of existing files in this project to attach (PDF, Office, Markdown, HTML, SVG, '
                       'text or ZIP). Each is published as a file the human opens from the report and the '
                       'task board. The whole report is refused if any file cannot be published.'},
     'tasks': {'type': 'array', 'uniqueItems': True,
               'items': inline_schema_definition(_REPORT_CONTRACT, 'progressText'),
               'description': 'Titles of the tasks on your task board (update_task_progress) this report is about, '
                              'as written there. Optional; a title that matches no task leaves the report unlinked '
                              'from it, never refused. When you complete a task without naming it in a delivery '
                              'report, Tofu adds its final progress note as the task\'s work report.'}},
    _report['required']))

# The tool and browser views share the board's exact text and task definitions.
_progress = inline_schema_definition(_REPORT_CONTRACT, 'progressToolCall')['properties']
_progress_source = _REPORT_CONTRACT['$defs']['progressToolCall']['properties']
for _field in ('summary', 'tasks'):
    _progress[_field]['description'] = _progress_source[_field]['description']
COORDINATOR_TOOLS.append(_tool('update_task_progress',
    'Keep the human\'s task board current: one entry per task they care about, in their words, '
    'not sessions, Workers, internal steps or questions (use ask_human). Choose exactly one: '
    'changes for routine updates, tasks with summary for a complete replacement, '
    'or read to inspect the current board. Use changes and the exact title to avoid losing unrelated tasks. '
    'Title, progress, reason and summary are single-line text. A new or changed pending task must carry a typed '
    'blocker naming exactly one of: active_dispatch (assignmentId of a Worker this Coordinator dispatched that is '
    'running now), blocked_by_dependency (assignmentId and the revision that must settle), needs_external_resource '
    '(the exact resource, the check that proves it is present, and either ownerAction — the one concrete thing the '
    'owner does elsewhere to obtain it — or unavailable with the limit that stays unverified), needs_owner_decision '
    '(the ask_human questionId and its question in plain language), or next_action (the concrete action a Worker can '
    'start now). An unavailable resource is a terminal limit, not a wait. An in-progress task may carry one too. reason is '
    'only a supplemental note: prose such as "waiting for a receipt" never states the wait, and a pending row '
    'without a verifying blocker is refused. A pending row naming no running assignment, dependency or concrete '
    'next action is marked idle in your continuation context: start it, dispatch it, or name a typed wait. '
    'Pending is not a dispatch or a queue. '
    'Mark unwanted work cancelled with reason before removing it; never silently omit unfinished work. Update '
    'the board when work starts, advances or finishes. A task carrying due is a promise to the human: set due '
    'whenever you tell them you will do or report something later. You are woken once at that time if it is '
    'still open; finish it or report then, re-date with a reason, or cancel and say why. '
    'An open promise cannot be omitted or lose its due. Do not date ordinary tasks.',
    _progress, []))

for _name, _definition, _description in (
    ('get_project_workers', 'getProjectWorkers',
     'List a page of registered Worker execution assignments for this original request, including work from earlier Coordinator turns. '
     'This is not a roster of every created conversation: use list_conversations with scope=project to find empty conversation shells. '
     'Use scope=project on new human input to find earlier work in this Coordinator binding (history pages of up to 32 rows). A page keeps only the rows that fit one readable result; use next_cursor as after_id to continue exactly after the last row shown. An empty filtered page can still have a cursor. Source Turn and current_request distinguish earlier objectives. '
     'Delegations whose Worker is still starting, or could not start, are listed under starting_workers. Passive facts only; get_project_assignment reads full evidence.'),
    ('cancel_project_work', 'cancelProjectWork',
     'Cancel a Worker this Coordinator binding dispatched, in any of its requests (an automatic continuation included). Withdraws its grant and stops every live attempt in its Worker conversation, including turns that ran without the assignment binding. '
     'Late background-command results stay attached to the cancelled attempt as assignment evidence and never start another automatic Worker turn; running commands are not killed. '
     'Preserves results and receipts. Cancel failed or superseded work before marking the request complete. '
     'Returns a compact receipt: assignment_id, decision, grant_revision, revoked, execution_status, delivery and next_step.'),
    ('finish_project_request', 'finishProjectRequest',
     'Stop automatic continuation of this original request after sending a report_to_user. '
     'Supply its exact report_turn_id. outcome=complete requires accepted current results and confirmed delivery '
     'for every non-cancelled Worker. outcome=needs_user stops the request until the human replies: use it only '
     'when nothing at all can continue without an answer to a question you already asked with ask_human, with a '
     'kind=blocked report saying what you wait for. '
     'Cancel unwanted work explicitly first. Do not finish after an intermediate stage; continue toward the full objective.'),
    ('request_user_decision', 'requestUserDecision',
     'After report_to_user(kind=decision) presents the options and your recommendation, register that decision. '
     'The request stays open and independent work continues. If the human replies, their message steers you. '
     'If they have not replied within wait_seconds (default 30 minutes), you are resumed automatically to apply '
     'the recommendation. Use this instead of stopping whenever a sensible default exists.'),
    ('review_project_work', 'coordinatorReviewRequest',
     'Review an exact completed Worker result this Coordinator binding dispatched, in any of its requests (an automatic continuation included). '
     'Read the evidence first. Accept, reject or request rework with a concrete note. '
     'When the assignment has acceptance checks (request.acceptance in get_project_assignment), judge each '
     'by its number in acceptance_results with the evidence you read: met only when the evidence shows it, '
     'unknown when you could not verify it. An accept must judge every check and is refused while any is '
     'unmet; choose rework or reject instead. An unknown check never blocks an accept or its publication: '
     'integrate the verified work and state in your report what could not be verified and what it would need. '
     'Do not rework work whose only gap is a precondition this project cannot obtain. '
     'To integrate verified code, include the exact delivery.checkpointSha from get_project_assignment; '
     'this uses the existing isolated-workspace publication checks. This records Coordinator review, '
     'never human acceptance. Publication is asynchronous: poll the assignment_id, which waits until its '
     'delivery is canonical or held, and confirm canonical_delivery before reporting completion. A review is immutable, '
     'with one exception: decision=rework of the same result_revision supersedes an accept or reject that was not '
     'delivered (it keeps the superseded judgment and stops a pinned publication); then repair with follow_up or '
     'delegate_project_work rework_of. Another exception: if delivery reports '
     'project_publication_result_changed, the Worker result gained a later revision after your accept; re-read it and, '
     'if still correct, accept again with the current result_revision and the same checkpoint_sha. '
     'project_publication_check_unavailable reads as pending: storage or Git did not answer the publication '
     'check, which repeats by itself, so never review again for it. project_publication_check_defect names a '
     'Tofu defect: report it. '
     'Returns a compact receipt (assignment_id, decision, grant_revision, result_revision, checkpoint_sha, delivery, '
     'canonical_delivery, next_step, and acceptance_counts for checked work), not the brief or transcript.'),
    ('delegate_project_work', 'delegateRequest',
     'Create and start an independent ordinary Worker conversation for part of the current human request. '
     'Returns at once with its assignment_id and conversation_id (status starting) while the Worker starts '
     'in the background, so delegate every independent part first, then poll their assignment IDs. '
     'For verified prerequisites, provide dependencies with exact assignment/grant/result revisions and file checkpoint SHA. '
     'Pending review/publication defers execution through the existing dependency gate. '
     'For repairs, review the prior result with decision=rework and pass its exact rework_of pin; the graph links the new assignment. '
     'Optionally name board_task_keys from update_task_progress(read={}).taskKeys for explicit many-to-many '
     'board coverage; it is descriptive only, never authority. Omitted coverage is unknown (rework inherits). '
     'No per-worker user action is required. The Worker lives in this Coordinator’s collapsible sidebar conversation group and remains directly openable. Supply a concrete brief with context, '
     'dependencies and ownership, and list each concrete, checkable requirement of the result in acceptance: '
     'the Worker reports evidence per numbered check and your review judges each one. '
     'For work whose defects a self-check misses (papers, data analyses, claims, references), add review '
     'with reviewer roles (correctness = data audit, overclaim, references, custom with focus): the result '
     'then goes through an automatic independent review loop. Each round starts every reviewer as a new Worker '
     'with no author context; failed findings go back to this Worker and fresh reviewers re-audit, until every '
     'reviewer passes or an impasse is reported. Poll the returned review_loop_id instead of auditing yourself. '
     'Worker capabilities and model are frozen from this request. There is no '
     'spending limit. '
     'Reuse exactly the same arguments and idempotency_key after an uncertain response. '
     'Workers run in isolated Git worktrees of this project. Tofu prepares a plain project folder itself '
     '(it adds .git and, when the folder has no agent guide, AGENTS.md; bulky data is shared, not versioned), '
     'so never ask the human to run Git. A root Tofu cannot prepare, such as a subdirectory of another '
     'repository, is refused before any Worker exists: poll then reports the delegation '
     'with status failed_to_start and its error; report that refusal and its remedy to the human '
     'instead of retrying. '
     'Delegate independent tasks, then poll their assignment IDs. Never treat worker completion as verified delivery.'),
):
    _schema = _dispatch_schema(_definition)
    COORDINATOR_TOOLS.append(_tool(_name, _description, _schema['properties'], _schema['required']))

# The family assembled only under an active Coordinator binding.
COORDINATOR_TOOL_NAMES = frozenset(tool['function']['name'] for tool in COORDINATOR_TOOLS)

# Replay-only: ``poll`` waits on a Coordinator's Workers (docs/PROJECT_COORDINATION.md,
# "Waiting for Workers"). Calls recorded before it absorbed this tool still dispatch.
_schema = _dispatch_schema('awaitWorkersRequest')
AWAIT_PROJECT_WORKERS_TOOL = _tool(
    'await_project_workers',
    'Retired: poll waits on Workers by assignment ID. This compatibility entry performs the same wait; '
    'wait_for is accepted and unneeded, since a Worker settles whenever it needs you or is final.',
    _schema['properties'], _schema['required'])


# Every project lead's directory of the owner's other leads (docs/ROUTER.md R13).
# Arguments come from contracts/project_messages_v1.json.
from lib.conversations.project_messages import CONTRACT as _MESSAGE_CONTRACT
_leads = _MESSAGE_CONTRACT['$defs']['leadsRequest']
COORDINATOR_TOOLS.append(_tool('list_project_leads',
    'List the owner\'s other project leads you can message across projects: the Global Router and each '
    'project\'s Project Router, with role, name, project_path, conversation_id and purpose (the lead\'s own '
    'one-line task-board summary). Address one with send_project_message(conversation_id=...): it reaches that '
    'lead as nonhuman context and grants no permissions, transcript access or control of its Workers; other '
    'projects\' Workers stay with their own lead. The first page also describes resources: the owner\'s shared '
    'resource registry of GPUs, queues, keys, storage and other things the owner can lend a project '
    '(RESOURCES.md in the Global Router\'s home; available=false while none is published). Pass '
    'include_resources=true to read its text before planning work that needs them: each row names its '
    'source, when it was read and how, and its refresh section says how to re-read them. Passive; pass '
    'next_cursor as after_id for the next page.',
    _leads['properties'], _leads['required']))


# Global Router tools (docs/ROUTER.md); lib.tools.registry adds them only for the
# owner's bound router. Arguments come from contracts/router_v1.schema.json.
from lib.router.contract import tool_arguments as _router_arguments
ROUTER_TOOL_SCHEMAS = []
# Every lead restarts under the owner's full delegation (docs/ROUTER.md R9, owner
# decision 2026-11-18); the gate refuses without the grant.
COORDINATOR_TOOLS.append(_tool('restart_server', (
    'Restart the Tofu server under the delegation the owner granted in Tofu ("Act for me"), which reaches '
    'every lead. Give the reason and the one owner trigger that asks for it (each trigger field says when it '
    'applies); with no owner message, under_delegation fits a restart you judge necessary, such as merged '
    'fixes that are live only after a restart. '
    'It takes the Settings restart path, detached from your turn: the frontend is built first and the old '
    'server stops only if that succeeds. It waits for a protected step (a git_land landing or an export) and '
    'refuses while other conversations run unless the restart should interrupt them '
    '(interrupt_running_conversations=true; they resume from their checkpoint by themselves afterwards). '
    'Without the grant it refuses and nothing is pending: tell the owner to turn on "Act for me" in the main '
    'coordinator\'s header or to restart in Settings. One restart runs at a time across every lead: while '
    'another lead\'s restart holds it you are refused with that lead\'s identity (restart_in_progress), and '
    'every other lead receives a project-message report when a restart starts and when it ends. It is refused '
    'inside a restart blackout window the owner declared (restart_blackout), and before anything runs unless '
    'HEAD\'s storage engine is certified (restart_engine_uncertified) and the server manager runs this '
    'checkout\'s source generation (a stale manager is refreshed first; restart_supervisor_stale if that '
    'fails). Only status accepted means a restart is under way: an '
    'error result (execution_started=false) restarted nothing and no notice follows. The grant lets you act '
    'for the owner, never speak for them: it answers no question and approves no prompt meant for the owner. '
    'Every call is in the owner\'s audit log; a retry with the same idempotency_key never restarts twice. When '
    'the restart ends, completed or not, you receive one nonhuman restart-complete notice as a new turn; '
    'report the outcome from it.'),
    _router_arguments('restartServer')['properties'], _router_arguments('restartServer')['required']))
_repair = _router_arguments('projectRepair')
COORDINATOR_TOOLS.append(_tool('repair_project', (
    'Project Lead: action=replace_own_binding, under_delegation=true, reason and idempotency_key. '
    'Replaces only your current binding with a fresh empty Coordinator in your project; do not pass '
    'project_path, title or note. Existing work and receipts stay in their conversations, but grants pinned '
    'to the old binding lose authority. Use only when binding replacement is necessary, not for transient '
    'failures. If you cannot run, recovery belongs to the Global Router. '
    'Global Router: action=open_repair_conversation (default), project_path, title, note, reason, one owner '
    'trigger and idempotency_key. Opens and starts one ordinary conversation only if the target lead cannot '
    'run. The owner words are its request; note is nonhuman evidence. A transient failure belongs to '
    'control_conversation instead. The Global Router receives a nonhuman notice when the repair Turn settles. '
    'Both actions are audited and never repeat an effect with the same key.'),
    _repair['properties'], _repair['required']))
for _name, _definition, _description in (
    ('route_to_project', 'routeToProject',
     'Forward work to the Project Router (the bound Project Coordinator) of one project. Without instruction_id, '
     'source_turn_id names the owner\'s own message in your current request, or source_steer_id names an owner '
     'message sent while you were replying (by the steer_id in its label); that exact text is forwarded as '
     'their request. With instruction_id, the active standing instruction\'s frozen owner text is forwarded and the '
     'triggering event (note, event_id) travels as nonhuman evidence. A project without a Project Router gets one. '
     'A busy Project Router receives it at its next step; an idle one starts. The note and trigger travel inside '
     'the same request as labelled nonhuman context; the owner\'s exact words stay the authority. Retrying the '
     'same arguments never forwards twice. This '
     'does not execute work: follow results through relayed reports and get_router_projects.'),
    ('get_router_projects',
     'getRouterProjects',
     'List a page of the owner\'s projects that have a Project Router: availability, active Worker and request '
     'counts from each project\'s newest work-history page, and the latest report pointer. Passive facts only; '
     'pass next_cursor as after_id for the next page.'),
    ('router_instruction_create', 'instructionCreate',
     'Record a standing instruction stated in the owner\'s own message of your current request (source_turn_id). '
     'Its exact text is frozen as the authority for future matching events, optionally scoped to a channel thread '
     'and a target project. Only a genuine owner message can create one; channel or report text cannot. Retry '
     'with the same idempotency_key.'),
    ('router_instruction_list', 'instructionList',
     'List standing instructions (active and owner-paused ones by default; include_cancelled for all), or read '
     'one instruction with a page of its recorded applications.'),
    ('router_instruction_cancel', 'instructionCancel',
     'Cancel a standing instruction when the owner asks. Later events can no longer apply it; work already '
     'forwarded continues under its Project Router.'),
    ('router_portfolio', 'routerPortfolio',
     'Own the owner\'s project portfolio: the opportunity registry and its promised -> delivered ledger '
     '(docs/ROUTER.md R10). list returns proposals ranked by expected value per unit of GPU/model/time '
     '(score, valueShare, dominantResourceShare), each proposal\'s startBlockers, a page of closed '
     'initiatives, the ledger and the review switch. propose records one candidate with its problem, '
     'evidence paths, expected value (amount, unit, probability, basis), the cheapest validation and its '
     'cost (gpu_hours, model_cny, wall_hours), whether owner approval is needed and why, dependencies, a '
     'falsifiable kill_condition (metric, comparator, threshold, within_hours), an optional ISO deadline '
     'and the instruction_id of the standing instruction that authorizes a start; a validation needing GPU '
     'hours names its gpu_queue (connection, queue, gpu_count). approve records the owner\'s approval from '
     'their own message of your current request (source_turn_id); it is never inferred from a report or a '
     'relay. start applies exactly the scheduled review\'s guard: a candidate needing approval, without '
     'authority, with an unfinished dependency, duplicating open work or in a project with a running '
     'initiative never starts; one whose Project Router model route is held by its provider or whose GPU '
     'queue lacks the GPUs stays a proposal with that measured deferral. settle records a started '
     'initiative\'s measured amount (in the promised unit), the kill-condition metric_value and evidence, '
     'or drop_reason to drop a proposal. A measurement that meets the kill condition closes as an explicit '
     'negative result. Nothing here wakes the Global Router or polls; the scheduled review (on unless the '
     'owner paused it) starts every admitted candidate in value order, and the daily digest reports each '
     'start with its reason, cost and expected outcome.'),
    ('owner_settings', 'ownerSettings',
     'Read or change a reversible owner setting through the Settings page\'s own Save, under the owner\'s '
     'delegation (R9, R11). action=list shows the reversible keys and current values; get reads one key; set writes '
     'one key with the Settings backend\'s own validation and hot-reload (needs reason, one owner trigger and '
     'idempotency_key so one change applies once). channel '
     'reads (enabled omitted) or toggles a messaging channel, reporting preconditions first: the SSO login '
     'state, whether a relaunch would push a 大象 login card (never triggered automatically — that returns '
     'needs_owner_action), and connector_owned; a kicked 大象 account reconnects only on the owner\'s own '
     'message. Human-only actions (secrets, login approval, QR pairing) return '
     'needs_owner_action with the Settings remedy; destructive or external actions are refused, never inferred. '
     'Every change is in the owner\'s audit log; revoking "Act for me" stops it.'),
    ('broadcast_project_message', 'broadcastProjectMessage',
     'Send one fix, workaround or instruction to every Project Router and every running Worker at once '
     '(targets ["all"]) or to the projects you name, instead of relaying it lead by lead. Each recipient '
     'receives it as nonhuman context at its next step (kind steer by default); it never wakes anyone, carries '
     'no authority and never quotes the owner. Each project\'s lead also gets a report naming the Workers of '
     'its project that received it; include_workers=false reaches only the leads. Give reason (recorded in '
     'audit.log) and idempotency_key: a retry never sends twice.'),
    ('router_project', 'routerProject',
     'Create a project (a new folder), register an existing folder, read a project or its Project Router, change '
     'the project defaults (update_defaults) and configure the Project Router (configure_router). Every create '
     'or register binds exactly one Project Router through the same backend operation the UI uses, so a project '
     'never needs a second one. Writes act under the owner\'s delegation: give reason and one owner trigger, '
     'they require the owner\'s "Act for me" grant, and each '
     'one is recorded in the owner\'s audit log. list and get need no grant. create writes only the new folder; '
     'register changes nothing in the existing one. update_defaults writes the project settings dialog\'s fields '
     '(name, color, projectPaths, readOnlyPaths, collapsed, order) and ensures the primary root\'s Router. '
     'configure_router writes the Router conversation\'s composer settings (model, thinkingDepth, capability '
     'toggles) and its Worker model. Retry a write with the same idempotency_key: it is never applied twice. '
     'Deleting or unregistering a project and deleting files are not available.'),
):
    _schema = _router_arguments(_definition)
    ROUTER_TOOL_SCHEMAS.append(_tool(_name, _description, _schema['properties'], _schema['required']))


# Router Watchdog tools (docs/ROUTER.md R7); lib.tools.registry adds them only
# for the owner's bound Watchdog. Arguments come from contracts/router_v1.schema.json.
WATCHDOG_TOOL_SCHEMAS = []
for _name, _definition, _description in (
    ('watchdog_snapshot', 'watchdogSnapshot',
     'Read the deterministic health snapshot of the owner\'s routers: the Global Router and a page of Project '
     'Routers with last activity, open signal IDs and their Workers that are active, failed or have a held '
     'delivery (objective, status, last activity time, settled error); outbox items that failed or are unknown; '
     'channel health; and a page of open fault signals. Passive facts only; page with after_id (projects) and '
     'signals_after_id (signals).'),
    ('watchdog_resolve_signals', 'watchdogResolveSignals',
     'Close open fault signals once you verified recovery or handled them, stating what you verified. Closed '
     'signals stay readable; the same fault never reopens them.'),
    ('watchdog_schedule_recheck', 'watchdogScheduleRecheck',
     'Arm a one-shot re-check that wakes you after after_minutes with your reason, as an owner-visible '
     'scheduled task. Use it for intermittent faults and keep re-arming until recovery. Retrying the same '
     'idempotency_key never arms twice.'),
    ('watchdog_message', 'watchdogMessage',
     'Send a project message to one of the owner\'s Project Routers, the Global Router or a Worker: the '
     'evidence and a concrete correction. It is delivered as nonhuman context at the recipient\'s next step, '
     'never wakes it and carries no authority. Retrying the same idempotency_key never sends twice.'),
    ('watchdog_notify_router', 'watchdogNotifyRouter',
     'Tell the owner\'s Global Router about an anomaly the owner may need to know about or decide; it decides '
     'whether, when and where to tell the owner. An idle router starts, a busy one receives it at its next '
     'step. Refused while the router\'s own latest turn failure is an open signal. Retrying the same arguments '
     'never notifies twice.'),
    ('watchdog_configure_review', 'watchdogReviewRequest',
     'Enable or disable your periodic review and set its cron cadence, only when the owner asks in their own '
     'message of your current request. Omitting schedule keeps the current cadence.'),
    ('resume_stalled_continuation', 'watchdogResumeStalledContinuation',
     'Wake a lead (the Global Router or a Project Router) that the snapshot marks stalled: its main lane is '
     'idle while its Workers need it. Re-verifies the stall from durable state (not_stalled otherwise), '
     'requires the owner\'s full delegation to every lead (refused otherwise), resumes the lead\'s own '
     'continuation and, if it is still idle, starts one notice Turn naming those Workers. Every outcome is '
     'audited; retrying the same idempotency_key never wakes twice.'),
):
    _schema = _router_arguments(_definition)
    WATCHDOG_TOOL_SCHEMAS.append(_tool(_name, _description, _schema['properties'], _schema['required']))
