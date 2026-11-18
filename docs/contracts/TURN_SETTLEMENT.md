# Turn settlement contract

Settlement is a durable fact produced once by the turn authority. It answers
why an attempt ended and which recovery operations are truthful. The browser
projects this document; it does not recompute it from task events.

## Durable state

An output turn is live while its status is `pending` or `running`. Terminal
statuses are:

| Status | Meaning |
|---|---|
| `completed` | provider/agent finished normally |
| `interrupted` | cooperative user/supersession stop or service recovery |
| `truncated` | provider limit or content filter cut output short (cause `model_refusal` / `content_filter` presents as Refused) |
| `failed` | generation, provider stream, or executor startup failed |

### Content-filter outcomes

A verified HTTP 200 `content_filter` finish — Claude's `stop_reason: refusal`
with `stop_details`, or an OpenAI-compatible filter — is a model refusal. A
provider 5xx whose normalized error message starts with `user sensitive words
detected:` is the same deterministic refusal: it skips same-model/key retries
and takes the configured fallback immediately; other 5xx classifications stay
unchanged. The fallback layer (`lib/tasks_pkg/llm_fallback/_refusal.py`) bills the attempt as
a non-authoring `R<n>-REFUSED` round, retires its partial text and early tool
work (`delta_reset(discard=true)`), and retries the round once on the
configured fallback model.

The same rule applies wherever an attempt is adopted, not only on the primary
and the configured fallback: a refused pool rescue (`R<n>-RESCUE-REFUSED`)
settles typed on the model that actually answered, and a refused reactive
overflow retry (`R<n>-REACTIVE-REFUSED`) hands the round to the ordinary
configured-fallback policy. Neither adopts the empty answer as a successful
round, and neither attributes the provider's `stop_details` to the requested
model — `orchestrator/_finalize.py::_settle_unstamped_content_filter` remains
only the backstop for attempts no fallback layer classified. Prior tool
results and the turn's checkpoint stay intact, so the fallback resumes from
them rather than re-executing side effects. A content-policy refusal is a
separate lane from a genuine empty/truncated stop (`abnormal_stop` recovery)
and from 402 credit exhaustion / 429 rate limits (routing and quota), which
never stamp a content-filter settlement.

A `content_filter` refusal is a verdict on ONE request, never a demotion of
the conversation's primary model: **the next request returns to the primary**.
The refused round is served on the configured fallback; the following request
(the conversation's next Turn, or a round whose history was compacted,
cleared or otherwise changed) goes back to the primary, and a repeated
refusal there falls back again for that request only. Each switch — fallback
and return-to-primary — emits a `MODEL_FALLBACK` event (kind `content_filter`
out, `primary_retry` back) and, for a project Worker, a lead-audit
`model_switch` row with `direction` `fallback`/`primary` (docs/LOGGING.md),
so a lead follows the switches without opening the transcript.

The one exemption is the refused attempt's OWN tool loop. `_switch` stamps
the refused history (its length and digest) at the fallback;
`retry_primary_for_messages` keeps a request on the fallback only when its
history is exactly that refused history plus the rows the tool loop appends
between two provider requests (the served assistant tool-call row and its
tool results). Those rounds answer the same refused payload, so re-probing
the primary would add one refused, billed request per round of exactly the
lanes that refuse most. Anything else — a new user message, any other
injection, a rewritten/shorter prefix, an unknown stamp — returns to the
primary. The return probe is gated in the dispatch path
(`_root_agent_loop._dispatch_open_round`) so the body is built for the
primary, and in `_call._llm_call_with_fallback` for direct callers. The
review receipt (`project_dispatch` `execution.result.modelFallback`, from
the Turn's paid `apiRounds`) names the rounds that ran on the fallback.

Measured cost: a refused primary attempt returns in about 3 s (median; up to
38 s under load) and bills its full prompt, so the exemption above is limited
to one attempt's own tool rounds. Alternatives rejected: latching the
fallback until the refused history changes (the 2026-11-18 incident — a lead
whose tool loop only appended rounds never changed the refused prefix, so it
ran 32+ rounds on the fallback with zero `direction=primary` rows); probing
the primary on every tool round of the refused attempt (bills a refused
request per round); a cooldown or retry counter (a knob that does not track
the actual verdict trigger). A resume still returns to the accepted model.

Without a distinct fallback (none configured, `disableModelFallback`, or
already on it), or when the fallback refuses too, it settles without a
same-model resend or pool rescue. An HTTP 450 gateway block never switches
models.

Both settle `truncated`, cause `model_refusal` (model) or `content_filter`
(gateway), evidence `provider_limit`, and `settlement.error` carries the typed
`content_filter` envelope (`context` `model-refusal` / `gateway-content-filter`,
refusal-specific `titleKey`/`hintKey`; automatic turns get a hint that does
not ask the user to rephrase). The producer stamps the envelope under
`task._contentFilterSettlement`, never `task.error`, so the verdict stays
`truncated` and OpenAI-compatible adapters keep their `200
finish_reason=content_filter`; the Anthropic-compatible adapter answers
`stop_reason: refusal`. Clients label these turns Refused and render the
envelope beside the bubble; historical `content_filter` rows without an
envelope also present as Refused.

Problem: a refusal returned as a successful round, so no fallback ran, the
finalizer replaced the empty answer with canned "rephrase your question"
text, and the turn was labelled Truncated. Decision: retry on the configured
fallback and settle typed evidence through the existing truncated status
with a dedicated cause. Alternatives rejected: a new top-level `refused`
status — the closed enum is validated by both storage engines, the compiled
TofuDB binary, the v3 snapshot schema and the browser reducer, so any missed
site fails snapshots or leaves turns spinning, for no presentation the cause
cannot carry; `failed` — adapters would turn an ordinary provider refusal
into a server error; resending to the same model — Anthropic documents that
it usually refuses again.

#### What the owner can see when the fallback answered

The switch is not silent in a chat. `call_configured_fallback` emits
`EventType.MODEL_FALLBACK` with `fallbackModel` / `fallbackFrom` /
`fallbackKind` / `fallbackReason` plus a RETRYING phase before the fallback
streams, and the settled Turn keeps those four fields. The browser projects
them in `view-model/turn-metadata.ts` (`metadata.fallback`) and the fixed
composer notice (`ui/composer-model-notice.ts`) renders "Automatically
switched: {from} → {to}" with the refusal reason, alongside the actual
serving route resolved from the last round (`turn-serving-route.ts`). The
turn's `model`/`preset` fields keep the REQUESTED primary; the per-round
`apiRounds` and the serving-route resolver carry the model that actually
answered, so the two are not conflated in presentation.

Measured 2026-11-04 → 11-07 (robustness census, 272 conversations): 379
provider usage-policy refusals on `claude-opus-5.5` switched to
`kimi-k3.1-pro` — 249 in Workers, 70 in the Global Router, 40 in other
Coordinators, 20 in chats. Categories came from the provider's own
`stop_details`: `frontier_llm` 366, `cyber` 7, `user_sensitive_words` 4,
`reasoning_extraction` 2. Request size does not predict them: on the Router,
refused primary attempts had a median 454k prompt tokens against 419k for
answered ones on the same conversation, and 66 of 70 events refused at a
later round, after tool results and quoted conversation transcripts entered
the prompt. The classification itself is faithful — `stop_reason: refusal`
plus a verified complete `content_filter` finish, never an empty or
truncated stop, and an HTTP 450 gateway block is a separate class that never
switches models.

Trigger attribution (measured 2026-11-18; 22 `frontier_llm` refusals between
08:00 and 08:45 CST, 16 of them on the Watchdog). Problem: do Tofu's own
prompts (role text, coordinator guidance, agent-safety wording) trip the
category? Decision: no prompt-source change. Segment replays through the
production route used `/v1/chat/completions` with explicit tools, which is
one provider completion and no tool execution. Tofu-authored text was
refused 0/24: the Watchdog and Worker system prompts with their tool
catalogs, the Watchdog role block, and Worker context blocks without
memories. The owner's LLM-training research content was refused 18/18: a
Router-written paper assignment, prefetched project memories about
GRPO/delegation-reward runs, and research Worker objectives that
`watchdog_snapshot` carries verbatim. The Watchdog reaches that research
only through snapshot objectives and transcripts it reads with
`get_conversation`. Its refusals did not reproduce from content: all 12 of
its refused round-1 payloads from 08:30-08:45, replayed unchanged at 09:15,
were answered. Its refusal runs began and ended with changes to the shared
prompt prefix; each run ended when old tool results were cleared. Worker
payloads made of research content refused again on replay. Alternatives
considered: rephrasing the role or safety text does not help, because that
text does not trip the category. Trimming objectives or transcripts would
drop evidence the Watchdog needs and add a truncation knob. Refusals
therefore remain with the configured content-filter fallback above.

Open gap (proposal, not built): a Worker / Coordinator / Router lane has no
composer model notice, and the project surfaces show the lane's configured
model rather than the model that answered, so an owner watching a project
cannot tell that a refusal moved a lane onto the fallback. Smallest change:
project the settled turn's existing `fallback` metadata into the lane
header / worker detail view through the same `turn-metadata.ts` projection
the chat uses, reusing `turn-serving-route.ts` for the answering model — no
new event, no new settlement field, and no second route resolver.

### Abort causes

Problem: four readers decided who stopped a Turn from `task['_abort_reason']`,
an open string vocabulary: the Turn verdict, the tool-result label, the
post-loop finish reason with its error envelope, and the model-request activity
row. Each kept its own copy of the reason sets, one tested a bare literal, and
a reason none of them listed read as a person's Stop at all four.

Decision: `lib/turn_verdict.ABORT_CAUSES` maps every reason Tofu stamps to an
`AbortCause`. It names who stopped the work (`AbortInitiator`: a person, a server
restart, a project lead's hold (`lead_hold`, a resumable pause: see
`docs/PROJECT_COORDINATION.md` "A lead holds a Worker without losing it"),
a storage or attempt fence, project-dispatch authority, the stuck-task
reaper), the label tool results show, and the error-envelope kind of a stop
that fails the Turn. Four readers resolve a stamp through `abort_cause`:
`derive_turn_verdict`, `system_abort_label`,
`orchestrator._finalize._settle_post_loop_finish_reason` and
`manager._stream._abort_request_attribution`. `INTERNAL_FENCE_ABORT_REASONS`,
`DISPATCH_AUTHORITY_ABORT_REASONS` and `RESTART_ABORT_REASONS` are views of the
table. An unregistered reason keeps the historic reading of a person's Stop,
because a rehydrated task result can carry an earlier build's reason. It is
logged at ERROR once per process. `tests/turn_lifecycle/test_abort_cause_table.py`
pins each reason's answer at all four readers.

That test also requires the table to match the code, both ways. It scans
`lib/`, `routes/` and `tofu_agent/` for every string that reaches
`_abort_reason`, directly or through a function that passes its caller's
reason on. A stamped reason without a row fails, a row no code stamps fails,
and any other write whose value only a caller decides must be on its short
reviewed list.

Registering forced a decision on two reasons that read as a person's Stop:

- `hallucination_loop` is planted by the unattended tool-loop breaker on a run
  that keeps calling a tool that does not exist. It is a Tofu guard
  (`AbortInitiator.TOFU_GUARD`). The Turn settles `failed/generation_error`
  with a non-retryable `tool_loop` envelope: the same unattended Turn would
  loop again. Tool results name the breaker, and the request-complete activity
  row reports `failed`. Read as `user_abort`, a Worker's stop told its
  Coordinator that the owner had stopped it.
- `legacy_autopilot_retired` marks a retired carrier refused before any model
  call. It is a guard too. It never aborts, and its verdict stays
  `failed/generation_error`.

`awaiting_user_action` is a third guard. The user-action guard
(`lib/tasks_pkg/user_action_guard.py`) plants it after a round that kept a
call from its tool, because a typed error earlier in the Turn requires the user
(`TOOL_EXECUTION_POLICY.md`, "Errors that need the user"). The Turn settles
`failed/generation_error`. Finalization keeps the guard's own non-retryable
`tool_call_rejected` envelope, whose `userAction` names the tool, the error
code and the required action.

Alternatives rejected:

- Storing a closed `Enum` in `_abort_reason`. Persisted task results and
  worker cancellations carry strings, and every stamp site would change at
  once.
- Failing a Turn on an unregistered reason. A historical value would turn a
  resumable stop into a failure.

The native engine's `visible_settlement` (`packages/tofu-db/src/turn.rs`) still
has its own list of four fence reasons for legacy rows that it derives without
this table.

An `interrupted/user_abort` verdict is reserved for stops the user (or a
superseding task) actually requested. Internal storage/attempt fences —
`_abort_reason` in `INTERNAL_FENCE_ABORT_REASONS` (`storage_authority_integrity`,
`storage_frame_overflow`, `turn_attempt_stale_fence`,
`conversation_attempt_stale_fence`, planted by `lib/turn_lifecycle` /
`lib/tasks_pkg/manager/_events.py` when the authority deterministically
rejects this turn's writes, e.g. `database_integrity`, or refuses to grow a
Turn into its settleable reserve, which also stamps the `storage_fence`
envelope before the producer unwinds; see `docs/TASK_EVENT_DELIVERY.md`; and
`worker_lease_lost`, planted by `lib/conversation_sync/worker_recovery.py` only
when a takeover joins a worker whose claim authority ended while its job was
being cancelled, with a non-retryable `storage_fence` envelope; a claim that
storage fenced for any other reason is a restart interruption, see the worker
recovery paragraph below) — settle as
`failed/internal_fence_abort` with `evidence: external_authority` and a
`storage_fence` error envelope (warning, retryable). Mislabeling these as
`user_abort` once rendered turns as unexplained stops with no recovery path;
the fence aborts the in-flight request, so the request-complete activity row
also reports `failed` + `errorKind: storage_fence` instead of the user-abort
row. The engine (`tofu-db`) carries the exact fence diagnostic in the
response `error_message` while `error_code` stays `database_integrity`, so
the `[TurnLifecycle] conversation authority integrity failure` and
`[ProviderIngress] async delivery failed` log lines name the specific fence
that fired.

Service shutdown stamps `server_shutdown` through the shared task quiesce owner
before closing native clients; their terminal callbacks can settle immediately.
The existing verdict then records `interrupted/server_restart` with
`evidence: system_recovery`. A prior user Stop retains its original cause.
Problem: joining native clients first lost the shutdown source and fabricated
`user_abort`. Decision: declare the cause before physical cancellation, while
still joining native work before its dependencies close. Alternative rejected:
rewriting the verdict after restart would replace an already committed fact.
The full production shutdown test keeps a real native provider request and SSE
consumer active, verifies joined exit and the reopened verdict, and rejects a
new dispatch while the original usage is unknown, even if request config claims
it is complete (`tests/server/test_native_production_shutdown.py`).

The `server_shutdown` quiesce also closes turn admission for its process.
Problem (2026-11-06): the quiesce aborted existing tasks but refused nothing
new; killed background commands woke their conversations, a duplicate
executor submission settled as `task_start_failed` (forfeiting its restart
resume) and that settlement's hooks started two more turns during teardown.
Decision: `create_turn`/`create_attempt` then refuse with the transient
`lane_busy` before writing anything, so queue rows, background receipts and
dispatch grants stay durable for the next generation; a task registered
after the quiesce snapshot is born with the same `server_shutdown` cause; an
executor refusal of a quiesced task settles as that interruption rather than
a start failure. Only a starting application lifecycle reopens it, so an
in-process restart is admitted again while a replacement process starts open.
Alternatives
rejected: a new conflict code makes the queue retire linked pairs
permanently and needs handling at every producer; fencing the executor
refuses after the Turn exists, which is the failure cascade itself;
per-producer shutdown checks miss the next producer. Verify
`tests/tasks_pkg/test_shutdown_quiesce.py`.

Invocation guards stamp `_invocationStopReason` (`stalled` / `deadline`) and an
internal error envelope before signaling physical abort. They settle as
`failed/invocation_stalled` or `failed/invocation_deadline`, with existing
`generation_error` evidence. `client_aborted`, provider finish, partial output
and usage/tool receipts stay truthful; diagnostics expose numeric silence and
threshold only. Explicit Stop is checked first and remains `interrupted/user_abort`
(Swarm `cancelled`), with no cancelled-only automatic wake or new UI taxonomy.

The stuck-task reaper force-fails a wedged root from outside its worker
(`_abort_reason` `stuck_no_progress`, a `worker_lost` envelope). Its Turn settles
`failed/worker_lost` with `generation_error` evidence, the root counterpart of a
child's `failed/invocation_stalled`; the reaper only judges tasks no person has
stopped, so an earlier Stop keeps `interrupted/user_abort`. Problem (2026-11-09,
the autoharness Coordinator): read as `interrupted/user_abort`, the reaped Turn
showed a phantom Stop while its task result said `error`, and durable worker
recovery, which requires both to agree, retried `settlement_unconfirmed` for
hours. Alternatives rejected: settling the task result `aborted` would hide a
system failure behind a Stop; stamping the child's `_invocationStopReason` on a
root would give the reaper the invocation guard's cooperative-abort semantics.
Verify `test_reaped_root_settles_its_turn_failed_never_as_a_user_stop`.

Tool admission refusals raise `ToolAdmissionRefused`, an `AbortedError`
subclass, so every abort fence still halts model and tool effects. The fatal
path settles them as `failed` with a non-retryable `tool_call_rejected` envelope
that names the tool and the refusal, never as `interrupted/user_abort`.
Problem: project-dispatch refusals (for example a Worker without its registered
isolated worktree) rendered as unexplained user Stops with the reason erased.
Decision: the raise site types the refusal; only cooperative Stop keeps
`user_abort`. Alternative rejected: returning the refusal as a tool result would
let a fenced Worker continue past an authority check.

Project-dispatch authority stops settle `failed` with `evidence:
external_authority` and a cause naming the authority: `dispatch_grant_revoked`
(the grant was revoked, or withdrawn by a durable Stop in the Coordinator or
recipient conversation) or `dispatch_authority_lost` (lost grant carrier,
changed task identity, execution association or registered workspace, or an
unreadable grant record). The site where the authority fails stamps
`_abort_reason` with the cooperative abort, so the task stops before any further
provider or tool call; the grant-withdrawal cancel port
(`abort_project_attempt`) stamps `dispatch_grant_revoked` on the live executor
or on the settled Turn when none is live, together with its provenance
(`_abortReasonDetail`: who revoked it and through which path), which the error
detail appends. The settlement error is a
non-retryable envelope whose kind equals the cause, so whole-turn auto-retry
never applies (the manual resume options of a failed Turn are unchanged), and
the request-complete activity row reports `failed` with that `errorKind`. The first cause wins: a person's Stop
keeps `interrupted/user_abort` even when the grant is revoked or the port
arrives afterwards, and a dispatch stamp never replaces an earlier one. The
decision record lives in `docs/PROJECT_COORDINATION.md` ("Dispatch authority
stop causes").

Admission judges only calls that can execute. Project-dispatch batch admission
(`tool_dispatch/_pipeline.py`) admits every such call of a response before any
of them runs, so a refusal still precedes all sibling effects. A call that parse
or its tool contract already rejected never executes: it is not admitted, and
its existing rejection with the next_action is its result. Problem: admitting
those calls failed the whole Worker for an argument mistake (`read_files` with
neither `path` nor `reads`) or an ungranted tool name, hiding the next_action.
Alternatives rejected: returning admission refusals as tool results (above),
and admitting rejected calls under a relaxed scope (a second admission policy).
Specification: `tests/project_mod/test_project_dispatch_tool_contract_rejection.py`.

The file scope's argument shape is contract-class, not authority. The shared
`read_files` rule is at-least-one of `path`/`reads` (the canonical tool ignores
`path` beside `reads`), so the dispatch copy's "exactly one", nonempty batch,
ordered range and exact entry fields are checked by `project_dispatch/scope.py`
`_paths`. `admit_tool` types those refusals as `ToolContractError` (still a
`ValueError`; for `read_files` with the shared rule's next_action), and batch
admission turns them into that call's ordinary contract rejection. Path
canonicality, runtime metadata, prefix and workspace refusals stay authority
refusals and fail the turn. Problem: a plain `ValueError` failed the Worker for
a correctable shape. Alternatives rejected: a surface-keyed second alternatives
rule or a global exactly-one rule (two rule sources, or a changed canonical
contract), and a root `oneOf` (providers reject root combinators).

The existing error envelope carries the bounded `invocationStopReason` extension
(`stalled` / `deadline`), minted only by `context: invocation-guard`,
`source: task-engine`. Canonical error serialization preserves it for cold task
consumers even when private carrier fields are gone. Finish settlement records
`error` without erasing physical cancellation. Fatal transport exceptions retain
the guard diagnostic; the shared verdict reads the guarded extension rather than
inferring failure from arbitrary `aborted + error` combinations. Driver stall is
not forwarded through the owner Stop port: the shared child beacon owns its cause.

The terminal turn stores:

```json
{
  "outcome": "completed|interrupted|truncated|failed",
  "cause": "provider_finished|user_abort|generation_error|...",
  "evidence": "provider_finish|provider_stream_failure|...",
  "streamState": "provider_finished|premature_close|malformed_stream|...",
  "providerFinishReason": "stop",
  "error": null,
  "resumeOptions": []
}
```

`error`, when present, is a normalized error envelope. A terminal error event
without an error payload is itself a contract failure and is converted to an
actionable internal envelope at the authority boundary.

`streamState` is the closed parser verdict. `providerFinishReason` remains a
provider/compatibility detail and cannot prove success by itself on the typed
stream path. `evidence` records which positive or negative fact authorized the
turn verdict. A bare `done` event, non-empty content, or a parser default of
`stop` is not completion evidence and settles fail-closed. A named legacy seam
still accepts an explicitly supplied finish reason for non-stream producers;
new model-backed paths must carry `streamState`.

A provider stream that ends without its finish marker is never
`completed/provider_finished`. When partial prose exists, the live executor
discards that attempt's prose and reasoning (`delta_reset` with
`discard: true`), emits a `phase: retrying` status (`bucket:
partial_stream`), and resends the unchanged request within the classic
transport budget, exactly like a cut that produced only reasoning or truncated
tool arguments. If those retries are exhausted, the turn settles as
`failed/provider_stream_error` with `providerFinishReason: premature_close`;
the last attempt's partial projection stays visible until a resume rolls it
back.

### Interruption recovery: rewind and resend

Problem: a cut partial reply was continued with a trailing assistant prefill
(non-Claude models) or a `[SYSTEM: LOSSLESS STREAM CONTINUATION REQUIRED]`
user turn (Claude), and resume offered a prefill `continue` with a
`contentPrefix` display seed. No provider continues an unfinished reply from a
bare trailing assistant message: Claude 4.6+ and Gemini 3.6+ reject it with
HTTP 400, and OpenAI Chat Completions/Responses treat it as a finished earlier
reply, so the model answered again and Tofu concatenated both. The nudge
changed the model-visible history and the request prefix.

Decision: every interruption (transport cut, Stop, process restart) is
recovered by rewinding to the last protocol-valid boundary — the user message
or the last complete tool result — and resending that exact request. Nothing
synthetic is appended to model context; the unfinished tail (prose, reasoning,
unverified tool arguments) is regenerated. Live retries leave `messages`
untouched, so the resend is byte-identical and reuses the provider prompt cache
written by the interrupted request. A resume (Continue, checkpoint resume,
guidance answer) restores the interrupted attempt's checkpointed working view,
rewound to the same boundary, before any context is composed, so the resent
request keeps every context carrier, steer and tool result in place and only
genuinely changed context is appended at the resume point; the retained rounds
settled after that boundary replay in the same canonical form as settled
history. Without a valid checkpoint the resume restores the storage-validated
completed prefix and replays the retained checkpoint rounds
([details](../CONTEXT_COMPACTION.md#continuation-checkpoints)). A tool call interrupted after
entry is closed with its effect receipt (see
[tool effect recovery](TOOL_EFFECT_RECOVERY.md)), never re-executed blindly.
The cost is regenerating the cut output tokens.

Alternatives rejected: a bare trailing-assistant prefill (rejected or answered
as a new message by every documented provider); a continuation user prompt
(alters the history and the next request's prefix); provider-specific prefix
flags (DeepSeek/Mistral `prefix`, Kimi/Qwen `partial`, vLLM
`continue_final_message`) — they would have to be selected per dispatched
slot, and a retry may land on a slot without them; add them only with a
route-level capability and the same rollback as the fallback. Specs:
`tests/tasks_pkg/test_stream_truncation_guards.py` (G2),
`tests/llm/test_llm_replay_scenarios.py::test_partial_stream_cut_resends_the_identical_request`
(equal recorded request digests for the cut attempt and its retry),
`tests/tasks_pkg/test_resume_executor_config.py`.

For a tool-bearing stream, parseable argument JSON proves only a complete
prefix. Problem: the legacy recovery classifier admitted that prefix while
the shared execution loop rejected its unverified stream, bypassing recovery.
Decision: the typed stream verdict controls bounded tool-round retry, including
when a legacy salvage hint is present. A verified provider finish without a
`[DONE]` marker remains valid. The accumulator retires unverified announcements
and speculative results before adoption; each discarded announcement receives
an aborted completion receipt with no model-facing tool output. A retry closes
its physical round before the next round opens; exhausted recovery also closes
the rejected round. Alternatives rejected: treating
parseable arguments as completion permits a partial tool batch; relaxing the
common execution gate or dropping event invariants hides the defect. The real
`incomplete_tool_stream` replay proves no unverified write, one verified write,
paid-attempt accounting and paired durable round/tool events.

Cooperative Stop before runner entry, including workspace admission, uses the
existing aborted-task finalizer. Both the task flag and cancellation event are
checked. Problem: workspace admission reported Stop as an executor error and
could ignore an already-signalled event. Decision: preserve typed cancellation
through the shared worker wrapper, keeping prepared workspace preservation and
terminal persistence. Internal authority fences and binding failures retain
their failure lane; converting every admission exception to cancellation would
hide those failures.

For single-agent Responses, an assistant output explicitly marked `commentary`
is intermediate work even when the response closes with `stop`. With no tool
calls, a valid unchanged phase replay triggers at most two continuations per
task, independent of the failed-tool nudge and stream-retry budgets. The
existing `intent_stall_nudge` phase and durable intervention note show the
reason and limit. The vetoed commentary leaves the live answer and renders
once as process prose; the failed-tool nudge instead keeps its vetoed answer
as the provisional answer (see [vetoed answers](../RENDER_CONTRACT.md#vetoed-answers-have-one-owner)).
Exhaustion ends with `abnormal_stop`, never success.
Cancellation, malformed streams, provider errors, token limits, pending human
guidance and explicit end-turn declarations retain precedence. Unphased or
`final_answer` output is never reclassified by matching prose.

Repeated successful verification is not task-completion evidence. The root
loop's repeated-verification stop settles with `providerFinishReason: incomplete`
and the existing incomplete recovery semantics, while its display-only
`system_note` explains why execution stopped. It must not reuse the normal
completion marker for genuine background-task acceptance. Checklist snapshots
are advisory progress records: pending items neither force another model round
nor override terminal evidence, and a completed list is not verification.
Historical checklist-continuation events remain readable, but new execution
has no checklist-only continuation or completion gate.

A short, tool-call-free `stop` body that is a gateway throttle notice rather
than a model answer (some gateways return their hourly-limit text as a normal
HTTP 200 chat completion instead of HTTP 429) closes with
`streamState: gateway_limit_response`. It is unusable: the slot records a soft
failure and cools down, and the round retries so the cooled slot rotates onto
a healthy key rather than surfacing the notice as the answer. The retry shares
the per-phase counter with the other non-empty buckets (cap
`_GATEWAY_LIMIT_RETRY_MAX`); because a throttle notice is never a legitimate
answer, exhaustion settles `failed` with a `premature_close` error envelope
instead of delivering it (conv mtxxaf2l).

A clean blank `stop` response, or a tool-finish response without tool payload,
uses at most two abnormal-terminal recoveries per task across all round/phase
resets. Explicit compatibility-layer evidence of unresolved tool control text
shares that budget. Recovery preserves prior operation receipts and disables
whole-turn regeneration; exhaustion carries a typed `abnormal_stop` error,
not normal completion. Real tool batches and protocol-defined program
continuations do not consume this budget. This executor verdict remains
separate from a benchmark verifier's pass/fail result.

A short final answer after tool work remains byte-identical at settlement,
including JSON, numbers, booleans and concise prose. **Problem:** the former
length-only recovery guard overwrote valid compact answers with inter-round
narration. **Decision:** narration recovery requires a blank terminal body or
an incongruent canned greeting confirmed by the shared stream classifier,
substantial prior narration, a clean `stop`, and no user abort. The classifier
uses the actual conversation tail; missing context cannot establish greeting
incongruence. **Alternative rejected:** special-casing JSON would still corrupt
valid short text answers, while disabling recovery would reopen the upstream
greeting incident. Shortness remains diagnostic evidence, not overwrite
permission. The public task result and durable turn share this delivered body.

## Event-delivery cancellation

**Problem:** shutdown cancels event admission while a producer is still draining;
wrapping that cancellation as a generic delivery failure misreports capacity
pressure. **Decision:** the event admission fence preserves cancellation and
revocation types across later emissions. Closing delivery is cancellation, not
capacity rejection. The fatal sink uses the shared verdict matrix for typed
cancellation: interrupted work publishes an aborted terminal with no failure
envelope, preserving `server_shutdown` as `server_restart`. Invocation guards
and internal authority fences retain their failed verdicts and diagnostics.
Accepted event debt still drains before terminal persistence; cancellation does
not replay model work or discard accepted evidence. **Alternatives rejected:**
relabeling `server_busy` in the browser hides genuine durability failures;
treating every physically aborted task as cancellation hides guard/fence faults.
Specs: `tests/tasks_pkg/test_internal_delivery_error_attribution.py`.

## Delivery dependencies

A completed provider Turn is not a user-objective completion receipt. Existing
`waitingOn` metadata exposes unresolved required Swarm work on clean `stop`,
`end_turn` or `incomplete` boundaries: required children may be running or have
unread terminal evidence awaiting parent integration. Optional children do not
block this dependency observation. Provider stream status and finish reason are
preserved. The Swarm owner derives dependencies from durable specs, current
terminal receipts and unread inbox identities; advisory checklists/prose cannot
create or settle them. Existing continuation resumes after required receipts
settle, subject to cancellation, identity and budget fences. See
[delivery dependencies](../RETAINED_AGENTS.md#delivery-dependencies).

## Single producer

`lib/turn_lifecycle.py::_settlement` derives the document from the terminal
executor evidence through the pure `lib/turn_verdict/__init__.py` state matrix.
`turn.event.record` applies a revision-checked projection patch and commits the
resulting projection, attempt event, turn status, settlement, conversation
revision, and any carried task event in one Sidecar transaction.

Every terminal exporter uses the same matrix. In-process results and the
OpenAI/Anthropic compatibility surfaces call `derive_task_verdict`; a failed
verdict travels on the vendor error channel and is never normalized into
`stop` or `end_turn`.

Consequences:

- a client-visible terminal frame never precedes durable settlement;
- duplicate terminal frames are rejected by attempt state/CAS;
- a stale or superseded executor cannot overwrite the current attempt;
- reconnect and cold reload read the same settlement;
- queue draining happens only after the terminal transaction frees the lane.

## Visible Flow phase settlement

Problem: native `turn.visible.sync` previously returned an existing live child
without applying its terminal evidence, leaving completed work running until
restart recovery incorrectly interrupted it. Imported terminal phases were also
unconditionally completed. Decision: native and SQL authorities use the same
verdict precedence; missing finish evidence fails closed, provider limits remain
truncated, errors remain failed and user cancellation remains interrupted.
Native settlement uses the regular atomic event-record authority, retaining
projection block identities, owner/root/current-attempt/parent fences and index
updates. Exact terminal replay is read-only; changed output/outcome or reopening
a terminal phase fails. A conflicting batch rolls back its earlier mutations.
Alternatives rejected: completing all phases, fixing projections only at restart,
or adding a separate caller-supplied verdict beside message evidence.
Specs: `packages/tofu-db/src/turn_visible_settlement_tests.rs` and
`tests/storage_authority_sql/test_turns_delta_sync.py`.

Visible tool segments use the public constructor's metadata rules on both
authorities: retain exact nonnegative JavaScript-safe `roundNum`, preserve
`toolModelContent` as result `modelContent`, and omit a missing/non-string/empty
verdict instead of emitting `status: null`. The problem was a native constructor
that lost chronology/model evidence and produced malformed early tool results.
The decision is constructor parity at the durable projection boundary; ignoring
metadata during differential comparison or repairing it only in the browser
would leave replayed data inconsistent. Public sync and receipt replay verify
these fields; the differential suite also checks the resulting projection.

## Explicit user cancellation

A Stop acknowledgement means the owned attempt accepted a cancellation signal,
not that its workers have exited or its Turn has settled. All chat Stop entry
points delegate to the same owner-checked cancellation authority; a terminal or
terminalizing attempt cannot be rewritten by a racing Stop.


Post-Stop event admission raises typed `EventDeliveryCancelled`, never a new
persistence failure. Unaccepted late frames stay invisible without poisoning
admission or planting a delivery-wedge warning; accepted FIFO evidence still
drains before terminal settlement. Typed cooperative cancellation at a model or
persistence boundary uses the existing terminal owner with `aborted` and no
fatal error payload. Internal fences, invocation guards and genuine delivery
failures retain their failed verdicts. **Problem:** a cancelled child-panel write
poisoned parent admission, replacing cancellation with generic `server_busy`.
**Decision:** preserve cancellation identity at admission and settlement.
**Alternative rejected:** suppressing all failures on an aborted task hides
real storage faults. Specs: task-event-delivery and internal-delivery-attribution
regressions.

Explicit cancellation reaches the active child-agent session and tool resources
without waiting for the parent model/tool loop to finalize. Ordinary parent
completion and observer disconnect remain distinct: they do not cancel an
accepted detached swarm. Conversation-scoped reuse must not bind a new active
turn to a stale spawning-turn cancellation predicate.

`await_agents` treats cancellation as an independent exit condition, not as a
completion timeout and not as a requirement to wait for scheduler drain. Tool
batch collection must observe cancellation even when no future completes;
a blocking iterator or unconditional pool join cannot be its signal-delivery
mechanism. Queued work is prevented from starting; running work receives
cooperative cancellation. Resource callbacks send bounded signals only, outside
the task-authority lock; they must not join workers or perform network cleanup.
Already-running Python threads cannot be forcibly terminated: outstanding work
must retain bounded resource ownership and late-result fences rather than be
reported as physically stopped. Each visible tool occurrence settles once with
its actual completed receipt or an explicit interruption, never an invented
`Unknown tool` failure. Child cancellation is not a retryable provider failure
and must preserve accumulated usage and committed child evidence.

A normal parent settlement detaches its swarm even if the last child finished
between the final inbox drain and the parent's `done` commit. Natural completion
must neither call child cancellation nor delete unread result/inbox evidence.
After a successful terminal commit, `lib/swarm/turn_handoff.py` rechecks the same
session settle hook; the continuation policy, rather than the finalizer, owns
batch eligibility, Stop, deduplication and retry limits. A failed terminal commit
cannot authorize a successor. Explicit Stop signals the workers and clears only
the swarm notification lane, preserving unrelated peer/background-command
receipts and child results for inspection. Startup recovery checks owner-scoped
Turn authority and fails closed on a committed Stop or unavailable cancellation
authority. A terminated driver may replay terminal results, but never respawns
missing/nonterminal checkpoints from a user cancellation on its own. An exact
accepted Continue may reopen safe ordinary children carrying parent-Stop
provenance after the old workers drain; completed results, individual kills,
retained assignment CAS and unresolved tool effects are not replayed. See
[run continuity](../RUN_CONTINUITY.md).

Server shutdown is distinct: `abort_reason=server_shutdown` derives
`status=interrupted`, `outcome=interrupted`, `reason=server_restart`, with
`system_recovery` termination evidence. Shutdown cannot overwrite an earlier
user Stop. The session is durably restart-paused rather than terminated, and
cooperative child cancellation does not publish a terminal cancellation or end
a retained assignment. Startup alone does not authorize execution.

Restart recovery is the same cause seen from the executor's side. A stale-attempt
fence reads the Turn's settlement before it stamps the task:
`server_restart` stamps `restart_recovery`, which gets the same interrupted
`system_recovery` verdict. A Stop recorded elsewhere stamps
`attempt_stopped_by_user`, which keeps the user_abort reading. Anything else,
such as a newer attempt, keeps the failed fence stamp. Both storage authorities
settle restart-recovered Turns with `system_recovery` evidence. The fatal path
builds a revoked or system-stopped delivery's envelope from the stamp
(`storage_fence`, or the dispatch-authority kinds), never from the exception
class, which reads every cancelled or revoked delivery as `aborted` ("Stopped
by user"). A command killed by a system stop says who stopped it
(`[Command interrupted by server restart]`). So does a tool call the stop cut
off before or during its run (`Task interrupted by server restart.`, from
`turn_verdict.stopped_tool_result`). An owner Stop keeps `Task aborted by
user.`, and the round's verdict stays `aborted` either way. Project Brain boot recovery leaves
restart-paused work items active for their resumed attempt.

Problem: on 2026-11-06, the D1 backstop kills were persisted as "Stopped by
user", restart settlements carried `evidence=user_abort`, and paused lanes
reported that their commands had been aborted by a user. A consumer that reads
an owner Stop delivered early, and 18 resumed lanes' work items were closed as
cancelled. Decision: the abort site stamps the cause, and `derive_turn_verdict`
maps it next to `server_shutdown`. Delivery reads the same verdict (the
representation from the restart publication fix). Alternatives rejected: a
second cause field beside `_abort_reason`; reclassifying `EventDeliveryRevoked`
globally (the class alone cannot say who revoked the lane); mapping the
conversation-wide `superseded_by_new_task` stamp, which the HTTP
abort-by-conversation route also plants for a real Stop. Guards:
`tests/tasks_pkg/test_system_abort_cause.py` and the SQL restart-recovery
contract test.

The same rule holds when a project-dispatch gate inside the running call
rechecks a stopped task (`admission._validated_binding`, reached from the
call's own admission by the effect intent or a file scope, or meeting a stop
that landed while its live validation restored the grant). It raises the typed
`DispatchTaskInterrupted`, whose `abort_reason` is the stop's own cause, and
names the stop (`Task interrupted by server restart. The project dispatch grant
is unchanged.`); a stopped sub-agent carrier (`dispatch_root`) does the same.
Each identity fact the check compares (principal, attendance, recipient
conversation, child task) refuses with its own `DispatchIdentityMismatch`.
The executor types a restart-cut call by its effect evidence
(`_effects.tool_exception_result`): a call the effect fence proves never
entered its handler (no effect record, or `not_started`) and whose dispatch
intent settlement was not lost is `tool_execution_interrupted`, retryable, and
tells the model to repeat it when the work continues; an entered or unknown
effect never is, whatever its declared recovery. Without that proof it stays
`tool_execution_cancelled` with an envelope saying the restart revoked nothing
and asking to check whether it took effect, and an entered non-repeatable
effect stays `tool_effect_unknown`. A person's Stop and an authority stop keep
their own wording and "Respect the cancellation".
Problem: after the 2026-11-13 04:27 restart, such calls answered "Project
dispatch task identity is no longer authorized" and "do not resume this
operation automatically". Recovery continued each Turn in a successor attempt
with its grant intact, but four Workers read that as a revoked grant and ended
their Turns without another call, although none of the calls had started.
Alternatives rejected: restoring a grant or recovery path that was never lost;
hiding the refusal (the call must still stop); the rationale for the
repeatable type is in [RUN_CONTINUITY.md](../RUN_CONTINUITY.md#a-call-the-restart-stopped-before-it-ran-is-repeatable).
Guards: `tests/project_mod/test_project_dispatch_restart_interruption.py`,
`tests/project_mod/test_project_dispatch_restart_admission.py` and
`tests/tasks_pkg/test_tool_exception_recovery.py`.

Problem: the settlement drain of an attempt interrupted by that shutdown ran
project-dispatch recovery and queued/background dispatch at once, so the
stopping process started a Worker Continue that nothing quiesced; it ran until
the process exited and could leave unsettled tool intents that block the
resumed attempt. Decision: while this process shuts its application down
(`task_admission_closed()`, closed by the shutdown quiesce before it marks any
task) a settlement starts no successor; the next process resumes it through
startup recovery and redispatches queued input through its startup queue
drain. The process's own state decides, never the task's `server_shutdown`
stamp: on 2026-11-11 (NDP request attempt e11ce7a7) the stopping process could
not record its quiesce while storage was fenced, a serving process's durable
worker settled the Attempt with that stamp, and its drain skipped the dispatch
recovery that resumes the request, which waited 37 minutes for a Router
forward. Alternatives rejected: quiescing successors again re-creates the same
race one step later; keying the drain on the task-scoped cause (this decision's
first form) is wrong whenever another process settles the Attempt. A spec of a
stopping process stops its task through `quiesce_running_tasks('server_shutdown')`,
which closes admission before it stamps; a bare `server_shutdown` stamp models
the serving-process settlement above, whose drain continues the Worker at once.
Guards: `test_only_a_stopping_process_leaves_successors_to_startup_recovery`,
`test_the_serving_process_runs_dispatch_recovery_for_an_attempt_a_dead_process_left`,
and the restart cases of `tests/conversation/test_worker_attempt_real_start.py`.

A dead execution owner's Attempt, and a live owner's Attempt whose claim
storage fenced (a storage restart changes the claim epoch; an outage outlives
the lease), is settled by durable worker recovery
(`conversation_sync/worker_recovery.py`): `interrupted`, cause
`server_restart`, abort reason `restart_recovery` when a serving process
records it (`server_shutdown` only while the recovering process stops). A
storage outage alone never settles a Turn failed. Its
error names only the calls whose side effect is unknown: a call whose
committed `entered`/`unknown` intent has no recorded result and whose effect
class is `unknown` (`code: worker_effect_outcome_unknown`, `unknownEffects:
[{toolName, toolCallId, operationId}]`, `cutCalls` with every cut call's
`effectClass` and `decision`, manual retry policy). A Turn without such a call
carries no error and settles like the graceful quiesce: read-only, idempotent
and verifiable calls resume ([Run continuity](../RUN_CONTINUITY.md), "A
restart-cut call is classified by its declared effect"), and a call without a
committed intent never entered its handler. No automated owner continues a Turn that names
unknown effects (`restart_resume.unknown_effects_to_review`): the restart
resume pass leaves it for the owner, Worker recovery logs
`restart_paused_turn_effect_unknown` and leaves it for its Coordinator's
`control_conversation`, and a Coordinator request continues in a new wake whose
brief names the calls to check. Problem: at the 2026-11-11 21:32 restart eight
Workers whose execution host had died settled with a blanket "unknown external
outcome" and a manual retry policy, one with only `fetch_url` in flight, and
none was continued. Alternatives rejected: continuing every interruption (it
can repeat a write whose first execution landed); a blanket review (it strands
read-only work). Specs: `tests/conversation/test_worker_exit_recovery.py`
(`test_a_dead_worker_needs_review_only_for_a_call_cut_off_mid_effect`),
`tests/project_mod/test_coordinator_restart_recovery.py` and
`tests/conversation/test_restart_resume.py`.

No boot pass lists an Attempt that recovery settles in a serving process, so
its settlement drain resumes it: after queued input had its turn and before
project dispatch recovery (which continues dispatched Workers and Coordinator
requests itself), `restart_resume.resume_recovered_turn` applies the restart
resume rule to owner chats and Router Turns, with command id
`restart-resume:<Attempt>` and the stand-down of this generation (manual
shutdown, restart storm). Problem: in RESTART-DRILL-1's storage-replay drill
(2026-11-12; the engine was killed and relaunched 90 s later while the
execution host lived), every running Turn, reads and waits included, settled
`failed` with "worker lease ended before its result was saved": a storage
restart changes the claim epoch, so every live claim's next heartbeat was
refused as a stale fence, and nothing resumed them. Production hit the same on
2026-11-12 06:11-08:33: a Coordinator Turn waiting in `await_project_workers`
settled failed and was not continued, and its failure attribution blamed a
review call that had failed hours earlier. A terminal's attribution now names
only the newest model batch's call, its unanswered call first, else its failed
one (`lib/tasks_pkg/failure_attribution.py`). Alternatives rejected: keeping a
fenced claim alive across the storage restart (a storage contract change in
both authorities, and the epoch fence is what protects every writer from a
previous claim); a failed settlement with a manual Continue (it strands
read-only work, which a resume repeats safely). Specs:
`test_takeover_names_the_lost_lease_of_a_joined_worker`,
`test_takeover_settles_the_result_a_lost_lease_left_unsaved`,
`test_a_turn_recovery_settles_in_a_serving_process_resumes_without_a_boot_pass`
and `tests/turn_lifecycle/test_failure_attribution.py`.

Problem: the shutdown quiesce also kills a paused lane's handed-off commands,
and each completion woke its conversation in the stopping process (2026-11-06
14:10:52, lane 27ce29e9). The wake resumed the Worker's settled Turn under its
deterministic task identity while the aborted carrier was still finalizing.
The agent executor refused the duplicate job only after the new attempt had
become the Turn's current one, and that refusal settled the Turn failed.
Restart recovery skips failed Turns, so the lane ended. Decision: the
background-completion wake starts no successor for a conversation that has a
carrier paused by this server's shutdown. A trusted start whose task identity
this process still schedules (a registered carrier has not exited, or the
executor still holds its job) is refused as `lane_busy` before any durable
write. Dispatch defers it, and the next maintenance tick starts the
continuation after the predecessor has exited. Chat task registration never
replaces a live carrier of the same identity, so the predecessor's task row is
never rewritten. Only the task-recovery owner rebuilds collective background
wakes on the maintenance tick, as at boot: a personal API host sees neither
the execution host's commands nor its carriers. Alternatives rejected: waiting
inside the wake for the previous carrier blocks a command thread without a
bound; treating the executor's refusal as a deferral still leaves an accepted
attempt that has to be unwound; a process-wide shutdown flag (as above).
Guard: `tests/background/test_restart_duplicate_start.py`.

For a Project-dispatch Worker, the already-authorized grant is the resume
authority: startup recovery accepts an existing execution only when its attempt
and Turn are both `interrupted` with `settlement.cause=server_restart`. It calls
the ordinary Continue command in that same conversation, retaining the original
transcript, grant, workspace work id and checkout; under the durable worker the
resumed attempt runs as its own task inside that checkout. The dispatch
association follows the new attempt after exact predecessor/successor validation;
its status, result, delivery and terminal Coordinator wake then use that attempt.
Background completion uses this same resume rather than an unbound parallel turn.
A `blocked: task_background_commands_running` receipt remains an unfinished
publication, not proof that the old process survived: startup command recovery
publishes either the actual result or an explicit server-restart death result,
and the existing delivery owner clears the hold only after that work and the
resumed task have settled. Problem, alternatives and verification live in
[Project coordination](../PROJECT_COORDINATION.md#restart-paused-worker-continuation).
User Stop, completed/reviewed execution, a live predecessor or changed lineage
never acquires this recovery path.

A revoked grant is not restart-paused and cannot Continue. Its background batch
settles as evidence on the exact cancelled Turn, with no new attempt or synthetic
user turn. The existing delivery owner attaches the deterministic command receipt
before retiring only the automatic wake, including a wake already leased when
cancellation raced it. Durable results remain readable through the assignment;
this evidence preservation does not reclassify the cancelled attempt as completed,
claim model consumption, or turn an authority refusal into a user Stop. Receipts
without a binding in a Worker conversation settle the same way through that
conversation's assignment, and revocation stops every live attempt there.
A bound batch also settles once as evidence when its settled execution Turn no
longer offers Continue (a failed attempt start offers only Regenerate), so it never
re-issues a Continue the Turn authority refuses; a batch whose own deterministic
Continue already settled is not replayed and leaves its receipts to the next turn.

Accepted resume config carries authority-written `_resumeSource` (exact prior
attempt/task, status and settlement cause). SQL/native acceptance overwrite any
caller-supplied value in the same CAS transaction; execution reads this receipt,
not local operation flags or the successor's now-cleared Turn settlement.
An accepted Continue can resume only the same owner's saved roster bound to the
source Turn and active Attempt and its exact, currently running Continue
successor. Recheck durable Stop/supersession after the binding write and registry
publication, then at actual child admission before and after start observers;
local notification delay grants no execution. Admission checks run outside
registry/scheduler locks and are required steps, not best-effort observers.
Natural parent completion still permits its admitted detached children.
Use current principal and executable tools intersected with the saved scope. Completed children and acknowledged deliveries remain settled; pending
children and safe unfinished checkpoints reconstruct new execution instances.
Paid token/cost receipts, elapsed work and tool counters carry forward once.
Write-ahead model/tool intent and settled receipts define recovery. An interrupted
tool round is never replayed: its own agent resumes with each call's recorded result,
or an explicit unknown-outcome result, plus a recovery notice. Malformed transcripts
and rounds whose foreground command may still run fail visibly; their transitive
unfinished dependents fail with `restart_dependency_reconciliation_required` before
scheduler admission.
Finished evidence and unrelated children remain intact. Dependency input includes
canonical status/error, never partial prose presented as ordinary success.
In-flight model usage is incomplete;
finite budgets refuse unknown consumption, while safe ready receipts retain
completeness across shutdown. A prior request's retained usage cannot certify a
later failed/usage-free request; completeness uses that request's own receipt. Request-local costs and active time accumulate once;
restart never renews a child timeout. Explicit deliveries checkpoint their final
answer before teardown. Unactivated recovery retains an exact failed-activation
receipt; only its direct failed successor may retry, never Stop/regeneration.
Registry admission reserves bounded capacity before durable rebinding; active
and reserved membership of one key consumes one slot. Registration/timer-start
failure restores exactly its prior session, timestamp and alias state, retiring
the failed timer generation. Failed activation drains submitted workers before
unpublication, retaining exact retry provenance. Stop during publication removes aliases and cannot leave a dead driver.
Current subtree access intersects the original policy, including readonly subroots.
Legacy cancellation rows without restart provenance remain non-resumable; do
not retrospectively turn user Stop into Continue authorization.

Continue rebinds the current principal, ModelRef and narrowed tool catalog, not
expired provider slots. Isolated children retain their original checkout and
acknowledged root labels; current removed roots refuse recovery, and readonly
downgrades veto writes. New parent checkout/delivery ownership is not inherited.
Specs: `tests/swarm/test_swarm_turn_handoff.py`,
`tests/swarm/test_swarm_rehydrate_cancellation.py`,
`tests/swarm/test_swarm_restart_continue.py`, `tests/swarm/test_swarm_invocation.py`,
`tests/swarm/test_swarm_restart_authority_storage.py` (real SQL/native acceptance),
`tests/swarm/test_swarm_restart_dependencies.py`,
`tests/swarm/test_swarm_recovery_activation_fence.py`,
`tests/swarm/test_swarm_recovery_registry.py`.

A background command completion first commits one durable passive receipt row;
the inbox twin only accelerates delivery to a still-running turn. One receipt
does not itself authorize another model turn. When every command for the owner
and conversation has finished and published, one collective wake turn starts an
idle conversation. Existing queued work takes precedence and consumes the
receipts instead. The next turn rehydrates up to 32 receipts into
one inbox batch, including receipts left by a server restart; provider-
consumption acknowledgement removes their rows.
Problem: the live tool config can acquire process-private cache views that the storage JSON
codec cannot encode, losing the completion before queue commit. Decision: the
queued config retains public turn settings and strips every leading-underscore
runtime key. Alternative rejected: encoding private runtime objects into the
queue would persist stale state and cross the turn authority boundary.
Problem (idle continuation): dispatching every finished command as a workflow
turn made late test completions produce vacuous assistant replies after the
parent task was done. Decision: wait for every known command to publish, then
start one collective wake turn whose command identity derives from the receipt
set; startup and maintenance reconstruct missed wakes from durable receipts. A
failed publication remains in the command registry and retries on the existing
maintenance tick before its batch may wake. Alternative rejected: a fixed
debounce alone, which can fire while a slower sibling is still running.
Problem (cross-process wake): the API host and the resident execution host
both run executors and background commands, and each registers only its own.
Both scan every conversation in shared storage for wakes, so the execution host
judged a conversation idle while the API host ran its turn, and judged its
commands settled because it knew none of them. It persisted the wake as a
workflow queue row that the turn authority refused with `lane_busy` on every
drain; the live turn consumed the receipts, and the leftover row later started
an empty "review the results" turn (2026-11-06: seven conversations, up to 98
dispatch attempts each). Decision: the wake is the turn itself, never a queue
row. The `turn.create_pair` lane fence decides idleness in the transaction that
creates it, so a lane busy in any process persists nothing and the busy turn's
settlement evaluates again. Command settledness reads this process's registry
and the host's durable records (`data/background_commands/`), which exist from
launch until settlement, so another process's unsettled command holds the
wake. Work this process still runs for the conversation defers the wake but
never establishes idleness. Wake rows stored by earlier releases are retired
by the queue drain and re-derived from the remaining receipts. A wake that
settled before consuming its results is final for that receipt set: replaying
its command starts nothing, and the results wait for the next turn. Foreground
commands handed off after the elapsed-time threshold keep no durable record,
so only their own process holds the wake for them. Alternatives
rejected: retiring a stale row at dispatch alone keeps persisting intent from
a guess and keeps the refused-drain churn; a cross-process liveness channel or
executor ownership on every Attempt is a storage-contract change for a fact the
lane fence already owns; waking only from the process that launched the
commands fails once a later turn runs in the other process. Specs:
`tests/background/test_background_batch_dispatch.py`.

Regression tests must separately observe signal acceptance, wait release, child
exit, and authoritative terminal settlement. A fast HTTP response or a released
composer button alone is not a cancellation-latency test.

## Swarm failure visibility and continuation

Each child failure is surfaced immediately, independently of main-agent wakeup:
the panel retains its reason and warns that failed work may have partial edits.
Success, failure, cancellation, running and waiting counts are distinct. Transient
notifications are deduplicated per conversation/run/agent with a 256-key bound;
hydrated terminal snapshots do not replay old alerts.

Routine automatic integration waits for the whole swarm to settle. Unread stamped
successes authorize integration; a wave containing failures (including all-failed)
may authorize one diagnostic/recovery/closure turn per human-authorized chain,
not blind retries. Durable continuation receipts enforce that failure allowance
and the total unattended-chain ceiling across restart. Cancelled/unknown evidence
alone, Stop/interruption, unavailable authority, or exhausted budgets do not
authorize continuation; results remain inspectable. Failed dispatch does not spend
the recovery allowance. Specs: `tests/swarm/test_swarm_failure_continuation.py`,
`frontend/unit/swarm-failure.test.cjs`.

## Resume options

Recovery is explicit. Each entry has an `operation` and a durable `anchor`.

- `continue` (`replay_only` anchor): the interrupted tail has no prose —
  completed tool rounds and/or a reasoning tail. The executor replays the
  retained checkpoint rounds — the same pre-gap retention a
  `checkpoint_resume` anchor computes — so the resumed model keeps every tool
  fact it produced before the interruption; the reasoning tail moves into
  `rolledBack`. Replay inside the executor still uses the causal prefix.
  A restart interruption (cause `server_restart`: the shutdown quiesce, or
  recovery of a dead or storage-fenced worker) that holds no cut call offers
  `continue` even when it produced nothing yet, as with a model that does
  not stream its reasoning: no one stopped that work, and unlike
  `regenerate`, `continue` keeps the lane's later Turns, so restart resume
  takes it like any interrupted Turn (same command id, same manual/storm
  stand-down). With nothing retained the resume starts from the Turn's
  input. A cut call (a round that claims a call identity without its result:
  the replay scan's blocked round) is not nothing: resumed from the Turn's
  input, the model issues it again and repeats a side effect recovery could
  not confirm. That Turn keeps the settlement it had without this rule: in
  its first round `regenerate` alone, with recovery's unknown-effect review
  (`error.unknownEffects`) naming a call cut off mid-effect, so neither
  restart resume nor dispatch recovery continues it; after an answered round,
  that round's `continue` and `checkpoint_resume`. A failed Turn or an
  owner's Stop that produced nothing, or only display carriers, offers
  `regenerate` alone: that is the same request. Problem: RESTART-DRILL-1
  (2026-11-12) found Turns a restart interrupted before their first output
  stranded: with no completed round or reasoning tail the settlement offered
  only `regenerate`, so no restart resume took them and their work was lost
  across the restart. Offering `continue` to every restart interruption also
  covered a first-round cut call: on GATE-R4's carry (2026-11-13) the
  owner's Continue ran the cut call's side effect twice in every held
  kill-execution row. Decision: a restart's own interruption is resumable
  unless it holds a cut call. Alternatives rejected: `continue` for every
  prose-free interruption or failure also re-runs empty failures (a failed
  notice review's empty stop) through automatic resume; a
  `checkpoint_resume` with nothing retained would rewrite the projection for
  the same request; `continue` with the cut call amputated from the replay
  repeats its effect, since the model never learns the call was issued.
  Native `turn.recover` (`restart_settlement` in `turn.rs`) applies the same
  rule (it never settles a `conversation-attempt:` binding); its settlement
  size is pinned in each persisted execution-frame witness
  (`recovery_bytes`), so the rule change advanced the witness to v4
  (`docs/TOFUDB_EXECUTION_FRAMES.md`, "Unproduced restart resume"). Specs:
  `tests/turn_lifecycle/test_turn_lifecycle.py::test_a_restart_interruption_that_produced_nothing_offers_continue`,
  `tests/conversation/test_restart_resume.py::test_a_restart_interruption_that_produced_nothing_resumes_from_its_input`,
  `tests/turn_lifecycle/test_turn_lifecycle.py::test_a_restart_interruption_holding_a_cut_call_keeps_its_held_options`,
  `tests/conversation/test_restart_resume.py::test_a_restart_interruption_holding_a_cut_call_is_never_continued`,
  `tests/conversation/test_worker_exit_recovery.py::test_a_dead_worker_whose_first_call_was_cut_keeps_it_held`.
- `checkpoint_resume`: completed tool rounds or a prose tail. The anchor
  retains every pre-gap round, display/progress carriers included, and
  amputates the result-less in-flight tail; only explicitly superseded
  provider-attempt artifacts are filtered. With no retained round the
  checkpoint is the turn input. The prose and reasoning tail moves into
  `rolledBack`.

Every resume (`continue`, `checkpoint_resume`, `answer_guidance`) pairs each
call a retained batch announced with a result. A batch's native provider
content (`_anthropicContentBlocks`, replayed verbatim) rides on its first
tool round only and names every call of the response, while each call's own
round is recorded as it settles. A sibling whose round was lost, or amputated
as the gap, gets one typed occurrence after its batch in the executor's
checkpoint rounds: status `aborted`, `_resultNotRecorded: true` and the
result `Result not recorded: ... its effect is unknown`
(`tool_round_replay.pair_announced_tool_calls`), so the resumed attempt's
projection records it and the model sees the call and its unknown outcome.
The executor's replay applies the same pairing to records written before
this rule. Problem: a sidecar storage outage lost one parallel sibling's
result (production 2026-11-11, worker 9b6877e9 turn 577d66d9) and every
resume option sent its `tool_use` without a `tool_result`, so the provider
rejected the 221-round turn. Decision: a typed unknown outcome, never an
invented one. Alternatives rejected: amputating the whole batch erases its
completed siblings' known effects; dropping the call from the native content
hides from the model that it issued a possibly side-effecting call. Specs:
`tests/tasks_pkg/test_resume_executor_config.py`,
`tests/llm/test_anthropic_signed_thinking_replay.py`.

The anchor's `retainedToolRoundPositions` is a **derived index** of that
retention selection, not an independent choice: it is exactly the positions
before the causal gap that are not superseded provider-attempt artifacts.
A request that echoes every other anchor field but omits that list selects
the SAME durable rounds, so both authorities accept it and resume from the
stored canonical anchor (production 2026-11-09: a Coordinator copying a long
settlement by hand received a bare `database_conflict` and could not resume
a 367-round turn after a provider 402). An explicit position list, or any
other field that differs, is still a typed, recoverable conflict and writes
nothing. The accepted anchor is always the stored one, so a repeated command
replays the same Attempt and no round is gained or lost. The stored list is
substituted, never re-derived from the projection: the legacy no-positions
fallback keeps `rounds[:keptToolRounds]`, superseded provider attempts
included, so re-deriving would silently retain transport noise. Specs:
`tests/conversation/test_coordinator_session_control.py` (SQL and native),
`packages/tofu-db/src/turn_tests.rs`
(`checkpoint_resume_admits_the_shorthand_anchor_that_omits_derived_positions`).

Anchor admission failures keep the bare `Conflict` taxonomy
(`database_conflict`, HTTP 409), exactly like the surrounding live-attempt,
parent, submitted-turn and projection-CAS fences. They are ordinary
optimistic-concurrency conflicts, not the deterministic worker-claim
ownership fences that carry a compile-time literal reason through
`ConflictFence`. Routing them through `ConflictFence` changes the variant
callers match on, drops the explanatory message SQL already returns
("The requested resume anchor is not current.") and breaks
`semantic_executor::tests::regenerate_admits_the_anchor_its_resume_option_advertises`;
the 2026-11-10 freeze reverted the first landing for exactly that regression.
Decision: keep `Conflict` on this path and pin the wire shape in
`server::tests::resume_anchor_conflict_echoes_the_bare_conflict_code_on_the_wire`
instead of widening the message through a different error class. Alternatives
considered and rejected: moving every anchor conflict to `ConflictFence`
(changes a public taxonomy for a cosmetic gain), and making the native reason
text match SQL's (a separate, optional readability improvement that must not
alter the variant).
- `regenerate`: restart from turn input; always available for a non-successful
  terminal turn.

No resume continues a prose tail ([rewind and
resend](#interruption-recovery-rewind-and-resend)): the model wire ends at the
replayed checkpoint and the executor seeds no prose or reasoning. Settlements
written before this rule may still offer `continue` with a `lossless_prefill`
anchor; accepting one regenerates the tail the same way.

All resume operations, including `answer_guidance`, supersede the later lane
tail and its rooted branches atomically, just like regeneration. Resume keeps
the target's checkpoint, progress and paid usage, advancing logical rounds
without a visible attempt split. Live descendants refuse the whole command
before mutation. `deletedTurnIds` and `turn.deleted` replay converge clients;
rejected commands leave the tail intact. External effects are not undone.

The command service validates the selected operation against the stored
options and expected projection revision. The frontend displays exactly these
operations through `frontend/src/core/turn-presentation.ts`; it does not label
regeneration as continuation, and the rendered action label reflects the
granted operation, so a regenerate-only settlement reads as regeneration.

## Attempt startup failure

Creating a turn and starting its executor are separate durability phases:

1. create the input/output pair and pending attempt;
2. atomically claim the pending attempt;
3. register the executor task and bind its id before spawn returns;
4. if registration/start fails, settle the attempt with
   `task_start_failed`.

There is no post-spawn compatibility bind. Accepting an unbound worker would
recreate an orphan window in which two executors can own one attempt.

## Orchestration turns

Planner, worker, critic, virtual-user, scheduler, and swarm continuation rows
are explicit turns with stable identities and actors. They do not append
synthetic entries to an archived message array. A parent attempt may announce
related turn ids so its live stream exposes the complete orchestration shape.

## Invariants to test

- terminal state is monotonic;
- settlement and terminal projection commit together;
- stale attempts cannot mutate current turns;
- error envelopes survive replay;
- resume options correspond to actually executable anchors;
- startup failure leaves no pending/running orphan;
- terminal queue drain dispatches at most one successor.

Primary suites: `tests/turn_lifecycle/test_turn_lifecycle.py`,
`tests/turn_lifecycle/test_turn_event_carried_task_event.py`,
`tests/translate/test_translation_turn_authority.py`, and
`tests/frontend/test_frontend_conversation_surface_vite.py`.

Usage aggregation uses `lib.cost.accumulate_usage_into`: public numeric counters
and fixed-depth reasoning/cache detail counters sum across authoring and discarded
billed rounds. Private dispatch/wire evidence and nested discarded-round carriers
are never summed. Each discarded attempt retains its own model/provider ledger
record and is added exactly once; booleans and nonfinite values are not counters.

Terminal observers register once through `manager._terminal` (at most 16) and
receive only successful owner-fenced durable writes. Running checkpoints,
transient tasks, failed writes and stale finalizers do not notify. The per-task
execution receipt suppresses duplicate notifications; observer errors never undo
committed state. Callbacks must only enqueue bounded follow-up work.

## Whole-turn automatic retry receipts

Problem: the settlement producer retained discarded retry evidence, but the
strict wire schema did not declare `retryChain`, so both live attempt events
and cold snapshots could reject otherwise valid retried turns.

Decision: `TurnSettlement.retryChain` is optional and contains at most eight
typed `TurnRetryReceipt` entries (`attempt`, `kind`, `backoff_s`, `at`, optional
bounded `detail` and `failureAttribution`). The producer in
`lib/tasks_pkg/failure_attribution.py` remains the authority; snapshot and
attempt-event decoders accept the same durable receipts. Strict settlement
and receipt field validation remains enabled.


Failure attribution projects the loop's zero-based internal dispatch index into
one-based `roundNum`, including the first dispatch as round 1. The internal task
index stays zero-based for context composition. Invalid/negative indices do not
invent a round, and task-start failures never blame replayed rounds or tools.
Normalizing the internal carrier was rejected because it shifts context-policy
consumers; only the public diagnostic boundary performs the conversion.

Alternatives rejected: dropping retry evidence hides why another attempt ran;
allowing undeclared settlement fields removes the protocol safety boundary.
`tests/turn_lifecycle/test_failure_attribution.py` covers produced receipts in
both transport envelopes, the bound, and rejection of undeclared fields.

## Retrying a failed terminal commit

Problem: a terminal Turn event can fail while the diagnostic `task_results`
write later succeeds. Retrying only diagnostics clears the task eviction fence
but leaves the browser's authoritative Turn running indefinitely.

Decision: the Turn facade retains one task-local terminal event and its exact
replay carrier when settlement raises. The existing bounded terminal retry lane
replays this receipt before writing diagnostics or releasing projection state.
An ambiguous timeout invalidates the local projection baseline; retry reads the
current attempt and respects its terminal/supersession fence. No model/tool work
is repeated and no terminal push precedes the authoritative commit. A process
crash continues to use durable orphan-attempt recovery.

Alternatives: synthesizing browser completion would invent a second authority;
retrying only `task_results` cannot repair a Turn. A new retry worker or queue is
unnecessary because the existing task registry already bounds and owns this debt.
Fault-injection tests cover rejection before commit, lost acknowledgement after
commit, retained partial output, and the diagnostic-write ordering.


## Registered workspace delivery is not Turn completion

Problem: terminal model/task success and candidate integration can precede
canonical publication. Decision: `lib/tasks_pkg/workspace_delivery.py` derives
publication eligibility from the canonical verdict, while
`lib/project_mod/delivery.py` owns the separate durable `origin.delivery` receipt.
Only terminal success plus a completed verdict authorizes publication. Missing
finish evidence, truncation and internal fences fail closed; failed/cancelled/
stopped outcomes preserve checkpoints without automatic submission or adoption.
A capture error leaves explicit retry debt, not an invented preservation receipt.

Before tools run, writable configured roots bind only existing owner/task
registrations; read-only roots, cwd visits and task-stamped file receipts cannot
claim a checkout. Evidence is bounded (200 file/cwd facts each), diagnostic only.
Terminal intent must be acknowledged before heavy evidence release/eviction;
failure retains the existing terminal-persistence fence and retry lane. Git
checkpointing, gates and delivery run in the lifecycle-owned Integration worker,
not another task queue. Isolated roots never fall back to shared-root publication.
Startup leaves an orphaned Project Work active when intent cannot be persisted.

Candidate `merged`, Project Work `completed` and Turn `completed` are distinct
from delivery `delivered`. Busy/dirty/changed canonical targets retain blocked
receipts and checkpoints; gates/conflicts cannot be repaired by declaring task
success. Restart scans bounded receipt pages and uses checkpoint ancestry to
recognize an adoption whose acknowledgement was lost, without replaying model
work. See [`project_integration.md`](../modules/project_integration.md) for leases,
adoption guards and cooperative-writer limits. A second settlement authority,
magic success token or destructive cleanup was rejected; none proves delivery.

## Known external blockers and unattended recovery

Problem: a retryable refusal or completed child result does not prove that the
main task's external condition or request protocol has changed. Replaying it
can spend repeated model rounds without executing the blocked operation.

Decision: `lib.turn_lifecycle.automatic_recovery_blocker` reads typed failure
metadata. `git_guard` refusals carry `condition` and
`retryPolicy: {mode: after_condition_change, automatic: false}`; their Turn
remains **failed**, with the rejection preserved in the error envelope.
`retryable: true` permits retry after the condition changes, not immediate
unattended replay. `autoRetryExhausted` also fences the existing whole-turn
replay path. Request-prepare `protocol_conversion:*`, explicitly non-retryable
failures, and exhausted automatic budgets likewise cannot be revived by a
successful child. Swarm updates stay in the inbox for the next authorized turn;
Stop, successful result integration, and the single ordinary failure-recovery
allowance retain their existing authority.

Intent-stall and commentary continuation read the last execution batch's
structured Git guard evidence without requiring an `END_TURN` token. A
successful sibling is not evidence that a refused command ran; a subsequent
execution batch may supersede the old blocker. Alternatives rejected: treating
`retryable` as permission to replay immediately, asking the model to invent a
magic stopping token, or converting a tool refusal into successful settlement.

Whole-turn regeneration uses that same `automatic_recovery_blocker` before
spending an attempt or clearing presentation. Problem: the error classifier
marked provider 5xx and temporary gateway failures as retryable
`upstream_error`, while the outer retry policy omitted that kind; it also
ignored explicit manual/condition-change metadata on otherwise transient kinds.
Decision: eligible `upstream_error` failures join the existing bounded
whole-turn policy, and all eligible kinds obey the common typed blocker.
The configured inner gateway wait still defaults to waiting for recovery or
owner cancellation. If it exits, the existing whole-turn allowance, opt-out,
Stop, partial-reply protection and before-tool-entry guard govern regeneration.
Paid usage is carried forward, pristine input is restored, and ordinary
invocation owners run each admitted attempt with one terminal settlement.
Alternatives rejected:
changing error taxonomy hides the provider cause; raising inner budgets does
not repair outer-policy drift; a separate retry loop forks lifecycle ownership.
Verify `test_upstream_auto_recovery.py` and the real invocation/dispatch/storage
scenarios in `tests/llm/test_upstream_turn_replay.py`.

Announced model rounds must close before outer recovery or terminal failure.
Problem: dispatch exceptions bypassed normal round settlement, leaving an open
round across the retry reset or final error. Decision: the root adapter closes
the announced round when model dispatch/preparation raises, after dispatch
cleanup, then propagates the original exception. A failed close propagates as
an event-delivery failure, retaining the dispatch cause and blocking provider
replay. Successful dispatch keeps normal settlement ownership. Alternatives
rejected: treating `retry_reset` as an implicit close weakens durable event
pairing and leaves non-retried failures open; a second retry loop or persistent
round-state flag duplicates lifecycle ownership. Verify
`tests/tasks_pkg/test_model_round_failure_boundary.py` and the replay scenarios
above, including explicit retry opt-out.

Build-specific verification: `tofudb_native_parity_20261001.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
