# Context engineering

This domain turns durable conversation/project state, memory, tool history, and model limits into bounded LLM messages, and owns automatic/user-requested compaction. Durable transcript authority is [`../STORAGE.md`](../STORAGE.md).

Model continuation bounds live in dependency-free `lib/agent_core/model_context_limits.py`.
Problem: importing storage for limits crossed core boundaries. Decision: share core policy; reject duplicated limits that drift.
`lib/agent_core/tool_errors.py` owns declaration assembly failures; core never imports repositories for exception types.

## Ownership

| Concern | Owner |
|---|---|
| Context assembly and provider ordering | `lib/tasks_pkg/context_composer/` |
| Snapshot selection, history reconciliation, budget, carrier placement and receipts | `lib/tasks_pkg/context_composer/_render.py::render_context` (ordered stages; [contract](../CONTEXT_INJECTIONS.md#composition-stages-and-investigation-boundary)) |
| Exported request/source attribution and first changed prefix | `scripts/inspect_context.py`, `lib/tasks_pkg/context_composer/inspection.py` ([usage and evidence limits](../CONTEXT_INJECTIONS.md#source-attribution-from-exported-requests)); shared section identity in `lib/llm_sanitize/context_sections.py` |
| Source ranges through sanitization and protocol emission | `lib/llm_sanitize/lineage.py`; `contracts/context_lineage_v1.schema.json` |
| Sealed request binding, export validation and exact cache-record join | `lib/llm/request.py`, `context_composer/_lineage_inspection.py`; existing `cache_tracking` owns verdicts |
| Automatic injection inventory, necessity, authority, placement, and cost policy | `lib/tasks_pkg/context_composer/registry.py` |
| Chronological/auxiliary injection writer and bounded debug receipts | `lib/tasks_pkg/context_composer/injections.py` |
| System/project/user context sources | `lib/tasks_pkg/context_composer/_providers.py` |
| Demand provider read execution | `lib/tasks_pkg/context_composer/_provider_executor.py` |
| Model context-window policy | `lib/context_limits/` |
| Token counting | `lib/token_counter/` |
| Automatic compaction | `lib/tasks_pkg/compaction/` |
| Automatic handoff identity, pre/mid-turn placement and restoration ordering | `lib/tasks_pkg/compaction/_handoff.py` |
| L2 carrier covered-Turn range and recall cursor | `lib/tasks_pkg/compaction/_coverage.py`; [contract](../CONTEXT_COMPACTION.md#summary-coverage-and-recall) |
| Required ordinary Turn acceptance scope | `lib/tasks_pkg/context_composer/turn_requirements.py`; [contract](../contracts/ARTIFACT_REVIEW.md#shared-execution-context) |
| Task-state projection / required checklist restoration | `lib/tasks_pkg/context_composer/task_state.py`, `checklist.py` |
| Persistent manual compaction | `lib/tasks_pkg/compaction/_manual.py`, `_persist/` |
| Scheduled working-view compaction (protected or busy conversations) | `lib/conversations/compaction_policy.py` marker, `_manual.py` schedule/cancel, `manager/_registry.py` wake/claim, `_pipeline.py` apply/settle; [contract](../CONTEXT_COMPACTION.md#coordinator-context-lifecycle) |
| Long-term memory, My Context, and [working facts](../contracts/WORKING_FACTS.md) | `lib/memory/`; `frontend/src/features/memory/preference-actions.ts` |
| Conversation message construction | `lib/tasks_pkg/conv_message_builder/` |
| Completed-task model view recovery | `lib/tasks_pkg/model_replay.py`, `lib/storage/model_continuation.py` |
| Cost experiments and trace | `lib/cost_experiments/cost_experiments.py`, context trace modules |
| Price tiers and per-round cost aggregation | `lib/pricing/`, `lib/cost.py` |

## Assembly flow

1. A task receives an immutable view of authenticated conversation and project state.
2. `context_composer` invokes declared providers in deterministic order behind one 15-second request deadline, records per-provider status/timing, and reuses the task-owned project-prefetch future; timed-out providers operate on detached carriers, so late completion cannot mutate the task or prompt.
3. Memory prefetch selects bounded evidence; it does not mutate the transcript.
4. The token counter resolves the model-specific counter, with an explicit heuristic fallback when no exact counter exists. Repeated per-round counts of one task's request pass the task-owned `RequestTextCounts` memo (`task['_requestTextCountMemo']`), so local tokenizers encode only texts new since that task's previous count (record: *Round-prep token counting* below).
5. Context-limit policy computes the usable input budget after output and safety reserves.
6. Compaction removes, folds, or summarizes only through registered steps.
7. The final request body is built once by the LLM body builder. Budget pressure is **telemetry only**: `context_budget` records content-free working-message estimates once per compaction window; it never appends a model message or records an intervention `system_note`. Automatic compaction and final-wire admission remain the capacity authorities.

Provider reads inherit the shared [worker startup transaction](../DEFENSIVE_PATTERNS.md#worker-startup-transactions): one ordinary physical startup fault can recover on the original unpublished Future, without replaying the callable. Successful admission does not imply immediate callable completion. Two failed attempts return an immediately terminal typed-error Future with both startup causes, not a pending read that waits for the request deadline; accepted siblings retain their ownership. Context regressions in `tests/context/test_context_provider_executor_lifecycle.py` cover bounded results, exactly-once entry, persistent refusal and joined cleanup; shared worker recovery tests retain cancellation, initializer and control-exception fences.

Tool-round continuations preserve round-zero provider/model binding and stable prefix order; providers do not silently reread mutable globals later. Responses assistant phase replay and scoped task/authorization continuity follow [the context contract](../CONTEXT_COMPACTION.md#assistant-phase-and-task-continuity); regression and budget checks: `tests/llm/test_responses_message_phase.py`.

Cache-stable prefix: runtime context never rewrites operator/user-authored base policy or canonical included tool declarations. Four registry blocks own system carriers after explicit policy and before dialogue: `platform_static` (reused unchanged, preserved by local lossy compaction), `ui_nav_directory` (browser-only, withdrawn off-surface/ablation/replace-mode without changing sibling policy), `parallel_execution` (available Spawn Agent lane), and `delegate_mode` (the narrowed-root contract that replaces generic Spawn Agent guidance). Verified legacy user-role orchestration envelopes migrate into this prefix without retaining a duplicate tail copy.
Other composer blocks are content-addressed appended user-role TAIL carriers: identical bytes keep position; changes append versions/retractions. Exception: accepted checklist recovery is visibility-driven and standalone, and survives compaction without new notes; superseded recovery sections are withdrawn only by projections that rewrite them, never from history a provider request already carried ([checklist restoration](../CONTEXT_COMPACTION.md#authoritative-checklist-restoration)).

- `environment` (cwd / is-git / platform / model) ships as a tail block, never inside `platform_static`, so changing the project path does not rewrite the cached prefix. The model perceives the path from that block each turn; a move appends a separately addressed one-shot event and the turn projection gains a `projectPathChange` provenance chip. Baselines are in-memory, conversation-scoped (task-scoped without a conversation), and bounded (256 scopes), so a restart re-baselines without false transitions. A composition with neither identity keeps no baseline. Problem: such compositions (the debug panel's snapshot builds a throwaway task without a conversation) shared the MCP selector's `task:anonymous` fallback scope across owners, so one snapshot's project path fired a project-path-change event in the next; the cold `/debug-messages` wire form then depended on process history and diverged from the hot build (`test_cold_hot_wire_form_byte_identical` failed under xdist after any earlier anonymous composition with a project). Decision: `_tail_transition_scope` returns no scope for an anonymous composition, for the project-path and MCP-delta baselines alike. Alternatives rejected: a fresh scope per snapshot (fills the bounded store and evicts real conversations' baselines); resetting the store in the test (hides the product divergence). Specs: `tests/tasks_pkg/test_wire_messages_fidelity.py`, `tests/context/test_context_composer.py::test_anonymous_compositions_keep_no_transition_baseline`.
- Environment-specific multi-root, remote-worktree, programmatic, and
  multi-agent routing guidance also lives in the tail. Included tool schemas
  retain their canonical descriptions; a schema budget may omit an optional
  tool but never strip annotations from an included tool.
- MCP tools are search-only (owner decision 2026-11-13, [tools_execution.md](tools_execution.md#mcp-boundary)): no MCP schema, name or listing enters the wire `tools` array or any composed block, so connecting, reconnecting or dropping a server never changes the request prefix. Every connected tool stays in the task's executable catalog; `search_tools` returns its complete schema on demand and `execute_tools` runs it. Sub-agents inherit MCP tools through the parent executable catalog and reach them the same way. The retired `mcp_tools_delta` block and its `mcpToolsDelta` provenance chip remain only on historical Turns. Specs: `tests/mcp_protocol/test_mcp_search_only.py`.
- Programmatic and multi-agent wire projection is latched from the task's first orchestration decision. Observed fan-out, later round numbers, retries, serial-gateway experiments, and program fallback state may update telemetry or execution behavior, but cannot add/remove tools or switch an existing tool schema mid-task. Local programmatic projection remains additive.

Preparation reuse — Problem: My Context acquisition reread one owner’s profile for prompt and event projections, and plan rendering serialized the same tool schema twice. Decision: derive prompt, character count, and structured event items from one acquired profile snapshot; reuse the render-local canonical tool-manifest digest for cache epochs. A populated profile now needs one read instead of three (empty: one instead of two); schema serialization needs one pass instead of two per planned render. No bytes, hash formats, epoch inputs, or token budgets change. Alternatives: cross-request caches risk stale owner state; parsing event items from Markdown loses multiline work-rule actions. Block tokenization remains unchanged rather than adding another cache. Count/parity specifications: `tests/context/test_context_preparation_work.py`.

My Context lifecycle and legacy inheritance — Problem: tasks read My Context from scope `str(owner_user_id)`, which is `'1'` for the personal owner since open mode began to carry `PERSONAL_USER_ID` (`lib/api_keys/_context.py` `local_admin_context`). The populated document stayed under the legacy open-mode scope `''`, so nothing was injected for the owner, and its entries carried no provenance or trust state. Decision: a one-time, idempotent migration inside the profile owner. When the personal owner's scope has no document, `load_context` copies the legacy document into it (`_inherit_legacy_open_scope`, `TODO(enterprise)`: scope `''` had a single operator). Each copied entry carries `provenance` (`source_class=legacy_import`, origin scope, file, source and creation time) and a `status` (`CONTEXT_STATUSES`, reasons in `CONTEXT_STATUS_REASONS`). The status comes from the audit verdict bound to the entry's id and exact text hash (`lib/memory/user_profile/_legacy_audit.py`); unaudited or edited entries become `needs_confirmation`. That audit module is one install's data (owner entry hashes and Turn ids): it is optional at runtime, excluded from Internal and Open exports and refused by name at the Open publish gate. Without it nothing is audited, so every inherited entry stays `needs_confirmation`. Composition reads only the scope stamped at task creation. An authenticated composition without `_profileScope`, such as a debug or bypass task, gets no My Context (reason `profile_scope_missing`, an authoritative retraction); only the offline lane with no user still reads the legacy scope `''`. Only `active` entries stay in `items`, where they are injected, mirrored and counted against the cap. Superseded and unconfirmed entries go to the same document's `inactive_items`, which pre-change readers, the Settings full-replace PUT and the learner never see. Writes keep inactive entries and the server-owned lifecycle fields; naming an inactive id in a replacement activates it. The legacy file is only read. The `inherited` record (source SHA-256, audit id, time, entry fingerprint) is the idempotency marker, so removing every entry never re-inherits. `undo_legacy_context_inheritance` empties the scope and marks the record undone; it refuses once the context has changed since the inheritance. Alternatives: read-side resolution would store no status until the owner's first write, which would then either copy or drop the statuses, and deleting the last entry would bring the legacy entries back. Hand-editing production data is unaudited and is not reversible through code. Specs: `tests/users/test_user_context_inheritance.py`.

### Snapshot freshness and provenance

Problem: metadata-only supersession left contradictory instructions visible; retrieval misses were mistaken for knowledge invalidation and redundant ambient facts polluted the model input.
Decision: changed contracts use scoped precedence notices and verified withdrawal. Memory no-match is diagnostic-only, not a withdrawal. Date reuse checks retained owned clock sections and top-level system/developer facts, never user/tool quotes or examples; missing/cross-day facts append once. Optional Project Brain pages reach only the available owner/project-bound coordinator, not every worker, and may await success ACK only after complete admission. Unchanged states append zero bytes; open tool batches defer updates.
Read failures retain last-known contract snapshots with typed receipts; optional absent/failed project pages add no model notice. Historical evidence and ordinary worker inbox/overlap delivery remain unchanged.
Rejected alternatives: rewriting consumed history breaks replay stability; withdrawing on a miss confuses relevance with truth; independent date watermarks and a second coordination state machine duplicate authority. Tests: `tests/context/test_context_integrity.py`, `test_context_composer.py`, `test_context_injection_registry.py`, `test_context_necessity.py`.

### Central automatic-injection contract

The public `context_composer` interface and `registry.py` own every automatic source. [CONTEXT_INJECTIONS.md](../CONTEXT_INJECTIONS.md) specifies all injection necessity/admission decisions, protected lanes, optional controls, bootstrap authority, tool-batch deferral and bounded Inspector receipts. Add a registry entry before any provider or chronological writer; no raw synthetic-message bypasses.

### Runtime message-mutation boundary

Ordinary completed chat tasks prepare their final model view before settlement and publish it atomically with Turn terminal state. The next task validates owner, lane, Turn content witnesses, model/system/permission/experiment settings and attachment availability before restoring it and appending new Turns; only the new suffix goes through same-role merging.
Edits, fresh plans, incompatible policies, missing media and unsupported recovery modes rebuild from authority, never an unvalidated checkpoint. Restart and disposable-cache eviction preserve the Sidecar-owned continuation.
Policy-refusal observability (2026-10-25): a refused restore used to log only an opaque `policy_changed` digest mismatch, so R1 cold rebuilds (e.g. the swarm auto-continuation, conv mvl5wxpj 22:56) left no evidence about WHICH setting moved. Decision: the checkpoint now stores a `policyFields` canonical field map beside its `policy` digest, and `restore_prefix` names the drifted policy fields on `config['_modelReplayPolicyDrift']` (plus a WARNING log) when it refuses. `policyFields` is additive — `encode_projection` and the `policy` digest are byte-identical, so existing checkpoints and replay fixtures are untouched; legacy checkpoints without the map report only the digest mismatch. Alternatives rejected: relaxing the witness to ignore ambiguous fields would weaken the valid restore gate; logging the full config would leak content. Specs: `tests/scheduler/test_swarm_autocontinue_replay_warmth.py`, `tests/model_routing/test_model_continuation_policy.py`.
Every producer of a continuation config must reproduce the policy its request was pinned with, not an equivalent-looking subset: an absent field is drift even when its runtime default matches. Coordinator continuations take their model-view selection from the accepted request attempt, and Coordinator ingress applies the role's tool settings before the pin (`docs/PROJECT_COORDINATION.md`, continuation model selection; `tests/project_mod/test_coordinator_replay_policy.py`).
Text-only history may cross a route or effort change when every other policy witness matches: old composer carriers are removed, current rules recomposed, provider reasoning/signatures/opaque native items stripped, and model-specific wire latches not restored; L2 history-carrier and inbox provenance is portable text metadata, while multimodal and unknown private state remains conservatively nonportable ([continuation checkpoints](../CONTEXT_COMPACTION.md#continuation-checkpoints)).
Replay restores only the orchestration latch from a compatible checkpoint; it never carries MCP declarations (MCP tools are search-only, and a legacy checkpoint `mcpWire` is ignored). Current gateway permissions always govern execution.

Text tool receipts retain the exact L0/V2-bounded model text, including results over 50,000 characters. Ordinary replay and Continue retain every todo receipt. Screenshot
receipts separate display text from `toolModelContent`; images reuse the existing owner-scoped immutable media store and its per-turn count/byte budgets.
Missing/foreign/unretained images produce explicit unavailable markers; media-store failure permits at most 256 KiB of inline image text per image and 1 MiB per Turn.
MCP tool results carry ImageContent out-of-band on a `str` subclass (`MCPToolResult.image_contents`): capture is ordered before every plain-`str` re-minting transformation (content filtering, L0/aggregate budgeting) and projects through the same `toolModelContent` lane as screenshots (tests: `tests/tasks_pkg/test_fetch_url_mcp_images.py`, `tests/mcp_protocol/test_mcp_result_images.py`).
Reconstruction returns detached blocks; request-local image normalization pins the first observed
bytes, so later tool rounds do not reread mutable local paths. The image L1 step protects the entire latest unconsumed batch until a model response proves consumption.

Context providers detach metadata without traversing executor-owned `messages`, `_turn_input_messages`, `events`, `result` or `toolRounds`; the synchronous renderer and state projection keep the original message/task authority. This removes history-sized copy allocation at every provider acquisition. A controlled offline sample (2026-10-08, 5,000 synthetic messages, 20,000 events, seven runs) measured median 71.136 ms / 15,404,024 peak allocated bytes for the prior full copy versus 0.003 ms / 2,792 bytes for metadata only; this excludes upstream TTFT and storage/queue delays. Measure with `python3 benchmarks/send_startup.py`; verify with `tests/context/test_context_provider_snapshot.py`. Read-only compaction hooks and context providers receive detached nested JSON containers; mutation or late completion cannot edit live history/config. The bounded continuation lifecycle is in [`STORAGE.md`](../STORAGE.md). Publication/restore report missing base, unsupported/resume mode, absent checkpoint, failed settlement, changed content and unavailable storage. Preparation logs `checkpoint_prepared`; only the terminal storage receipt logs `checkpoint_settled`, with conversation/Turn/attempt identity, so a candidate is never reported as a committed continuation. Tests: `tests/model_routing/test_model_replay_stability.py`, `tests/model_routing/test_model_continuation_policy.py`, `tests/model_routing/test_model_continuation_store.py`. The orchestrator deep-copies its nested projection before context composition, cache ordering, compaction, sanitization or provider conversion. Durable Turns stay immutable; automatic Layer 1 never reads or writes conversation authority. Dynamic guidance—MCP deltas, skills/memory, environment, lifecycle recovery, URL prefetch, branch context, Paper gateway routing, local PTC, and native multi-agent hints—is an appended tail `user` carrier. Provider converters must not add ephemeral per-round copies: stable PTC and multi-agent policy is composer-owned, while dynamic stage/capacity stays in execution telemetry rather than prompt bytes.

Tool inclusion may still change at an explicit capability/permission boundary,
but an included first-party tool keeps its canonical name, description, and
schema; runtime project roots, remote bindings, memory scope, filter settings,
swarm capacity, routing stage, and schema pressure do not rewrite those bytes.

The remaining transformations are narrow exceptions, not context injection:

- conversation projection renders structured user-owned fields (reply quotes,
  referenced conversations, plans, documents, images, and videos) into the API
  representation without changing stored Turns;
- `UserPromptSubmit` hooks may deliberately replace the newest request text
  because PII redaction and safety filtering would be defeated otherwise;
- automatic L2/advanced/reactive compaction may remove or summarize history in
  the request-local working copy to satisfy a hard provider context limit;
  manual compaction is the separately authorized persistent operation;
- provider adapters operate on fresh wire bodies and may normalize roles,
  tool-call/result blocks, images, cache-control annotations, or alternating
  message constraints required by the destination protocol;
- Claude OAuth cloaking rebuilds the provider-only system envelope but moves
  the user's system text to a stable carrier after the first user; swarm checkpoint
  recovery replaces only a stale provider-policy system carrier;
- token counters may construct synthetic counting-only messages. These never
  enter the task projection or conversation authority.

Natural assistant/tool output remains execution-owned. Loop-breaker, inbox and recovery context uses the registered injection interface, never history rewrites. Explicit user projections, tool-result envelopes, compaction and separate workflow requests retain their own owners; interruption recovery resends the interrupted request and appends nothing. Any new exception requires an executable regression and a boundary update.

Provider read ownership — Problem: a fixed process pool and pending count degraded context solely because unrelated reads were blocked; one-second joins also claimed close before reads ended. Decision: early prefetch and final acquisition share the application-observed `tofu_workers` demand kernel. Every independent read may start, with no queue/count refusal; the immutable launch probe sizes only idle reuse and scales above eight. Physical startup errors preserve their original failed Future, withdraw only unstarted work and leave accepted siblings owned. Optional acquisition deadlines, request-local cancellation, detached inputs, authorization and late-result exclusion remain separate authorities. Alternatives: larger queues preserve omission; private request pools duplicate scheduling; detached close loses physical ownership. Verify `tests/context/test_context_composer.py` and `test_context_provider_executor_lifecycle.py`: 32 blocked reads with legacy hints 1 still permit a healthy 33rd read, failed startup is retryable, races drain or refuse at close, and waiting close outlives the old one-second limit. Controlled measurement: 32 actual read workers rendezvoused in 7.02 ms; joined close leaves zero workers. This measures scheduling, not adapter latency.

Executor shutdown fences publication and wakes accepted workers. Nonwaiting close retains physical ownership for a later joined close; waiting close joins peers to completion and skips its caller. Request leases cancel only their own not-yet-started Futures and never close the shared executor; timed-out provider results cannot merge into the live request.

`ContextPlanV2` is the request-local global budget authority. It classifies objective/constraints, structured task state, evidence/recovery, hot tail, and recoverable cold history.
Required blocks lock first; permission, priority, freshness, access value, and token cost deterministically select optional blocks.
Its manifest records selection/suppression, hashes, token counts, recovery handles, stable segment hashes, and cache epoch. Per-block `max_tokens` remains a hard ceiling but cannot bypass the global budget. Multi-agent task shapes receive only a bounded tail trigger/discipline block; the canonical `spawn_agents` schema is never rewritten with per-round stage or capacity prose.

`TaskStateSnapshotV1` is rebuilt from turn/tool events: goal, hard constraints, decisions, completed work, files, tests, errors, open questions, TODOs, evidence IDs, observation time, and world version.
It is a request projection, never a second transcript authority; independent required checklist restoration follows [the continuity contract](../CONTEXT_COMPACTION.md#authoritative-checklist-restoration); specs: `tests/goals/test_checklist_context.py`.

The static prompt profile resolves once here. Kimi `auto` remains the full control contract; `lean` and named ablations require explicit experiment policy.
Every request stamps bounded `tofu.prompt-profile/v1` evidence—requested/resolved/effective profile, status/reason, model, character/token counts, SHA-256, and disabled blocks—on the task, `platform_static` provenance, and each round snapshot; replace mode records an empty effective profile.

## Automatic versus persistent compaction

Automatic, manual and provider-native compaction share token/summarization primitives, not persistence authority; preservation, cache economics, bounded receipts and owner-scoped summary routing follow [the compaction contract](../CONTEXT_COMPACTION.md). Local L2 uses a fallible user-role handoff, never a fictitious tool execution. `_handoff.py` derives pre-turn placement (before the latest human) or mid-turn placement (after retained execution/restored context) from source chronology. Later steering/replay cannot move an already-consumed boundary; manual/native authority stays separate. Specs: `tests/compaction/test_compaction_handoff.py`.

### L1 sent-prefix admission

Every api-form message the previous request sent is provider-cached. `cache_tracking.get_cache_prefix_count` returns that whole sent count (own thread, sibling thread, invocation snapshot or durable high-water mark, which records the same count), clamped to the live list after a history shrink. Default L1 (`micro_compact`) and the advanced host leave those bytes identical with one exception, the sent request's last tool step: from the last assistant message inside the sent prefix to its end, since the request before cached everything earlier. L1 runs its step list on a detached copy with only that step unprotected (dry run: no evidence, archives or telemetry). If the step would change, `sent_step_rewrite_economics` prices the very next request on the main route's verified rates. Rewriting costs `after_tokens x cacheWriteMul`; keeping costs `before_tokens x cacheReadMul`; new content after the sent prefix is written either way. Only a strictly cheaper rewrite is applied, and L1 advertises it to the cache-break detector as a bounded rewrite starting at that step. Unverified or unknown pricing keeps the step. Each decision logs one `[L1] ... sent-step edit admitted|refused` INFO line with the region's token estimates and both priced sides. On Claude routes the region estimate prices each image from its data-URL pixel size (Anthropic's documented `width x height / 750` after its downscale); other routes keep the flat estimate. `EDITABLE_TAIL_COUNT` remains only a cache-break diagnosis tolerance for provider-wire tail representation; the transport never edits api-form messages in place. Specs: `tests/compaction/test_l1_sent_step_admission.py`, `tests/cache/test_cache_prefix_cross_thread_freeze.py`.

**Problem:** the guard protected `sent count - EDITABLE_TAIL_COUNT` (2) api-form messages and never priced an edit of sent content. Counting api-form messages split parallel tool batches, whose results the Anthropic wire merges into one user message: on conversation `mvywcu5qqy5ocy` L1 replaced a ~80-token image crop with a ~60-token stub and re-wrote the whole 3,624-token merged message at 1.25x input. **Decision:** protect the whole sent request and admit an edit of its last step only under the exact next-request cost rule. A consumed 20K-token result shrunk to a 200-token stub still compacts (375 < 2,010 input-token equivalents at 1.25x/0.10x); marginal edits such as the incident are refused. **Alternatives rejected:** protecting everything sent leaves large consumed results until L2; keeping the count-2 tail splits batches and stays unpriced; a longer payback horizon would be a tuned knob, while the next request is the one cost the rule prices exactly.
## Memory boundary

Relevance uses the latest authored human turn, not machine-authored carriers, attachment helpers, quoted conversation or context markers.
Augmented projections retain `_authoredUserText` locally; plain text needs no duplicate. Same-role merges keep the newest value, text-only replay permits it, and provider sanitization removes it. `UserPromptSubmit` replacements update provenance too, preserving redaction.
An empty/image-only request cannot reuse an older topic. Each selection resets held matches and distinguishes disabled from failed status. Legacy projections strip whole composer sections and generated image hints, not arbitrary authored tool identifiers.

Project memories belong to the configured canonical project, not a task-owned execution checkout.
When workspace binding records `canonicalProjectPath` / `canonicalProjectPaths`, prefetch, search,
CRUD and merge use those roots; file execution stays in the isolated checkout. Legacy configs
without the canonical mapping retain their `projectPath` / `projectPaths` behavior. Existing
memories stranded in an old worker checkout remain there; recreating their verified lesson through
the supported `create_memory` tool writes the canonical store, with no database or file migration.
The mapping belongs to those memory consumers (`handlers/memory.py`, `memory_prefetch_corpus_roots`),
never to the task's resolved `project_path`: that is the execution root every tool call runs in and
project dispatch admits against the Worker's registered workspace, so a canonical value there refuses
every Worker call. Specification: `tests/memory/test_memory_canonical_workspace.py` and
`test_bound_worker_root_loop_runs_tools_in_its_registered_workspace`.

Memory retrieval is precision-first and bounded: metadata-only prefetch ranks the process's resident memory listing (`lib/memory/storage/_resident_listing.py`; no filesystem I/O on the startup path, freshness rules in [DEFENSIVE_PATTERNS.md](../DEFENSIVE_PATTERNS.md), "Startup reads a resident memory listing") and hydrates selected evidence only, explicit search streams one 2,000-character body prefix at a time, and BM25 retains query-term statistics rather than corpus tokens. Stored memories, My Context facts,
and session context stay distinct. After a clean interactive turn, one bounded
learner accepts at most two concise items backed by verbatim real-user evidence;
it rejects uncertain, one-off, synthetic, assistant, or tool content, and every accepted change is undoable. As reconstructible background work, it honors billing stops and active shared contention before transport, with at most one actual upstream 429; skips and failures leave the profile unchanged.

**Owner memory: self-query, trust classes and lesson lifecycle.** This is the experiment-independent part of the owner-understanding design record: Phase 2 (self-query and citations) and Phase 4 (lesson source classes and a disproved state).

- **Problem.** Memory was passive: no role could ask what Tofu knows about the owner, or why. The Router, Coordinators and Watchdog had no memory tool, yet their guidance advertised memory CRUD. A lesson a Worker disproved stayed injectable, nothing told a role whether an item came from the owner or from another agent, and a credential could be saved as a memory.
- **Decision.**
  - `search_memories` gains `mode=owner` and `memory_id` (`lib/memory/owner_memory.py`). Owner mode returns every My Context item (active, needs confirmation, superseded) and the matching lessons, disproved ones included. `memory_id` returns one item's text, provenance, its supersession chain in both directions, and the Turns of the current conversation whose `preferencesApplied.ids` or `memoryPrefetch.memories[].id` name it. The tool is read-only and sits in `COORDINATOR_TOOLS` (so in `ROUTER_TOOLS`) and in `WATCHDOG_TOOLS`; the memory write tools stay outside every coordination ceiling. Delegated Workers and sub-agents never see My Context `identity` items, and a third-party-text turn cannot call the lookup.
  - Every memory a model reads shows a trust class, `memory_trust_class` in `lib/memory/contracts.py`: `owner-stated`; `verified` (the owner confirmed or edited it, or the legacy audit bound it to an owner Turn); `inferred` (an agent report, an unrecorded source, an item awaiting confirmation); `disproved` (disproved or superseded). It derives from the status and the write path's `provenance.source_class`, never from a model argument. A model tool write is `agent_report`; an API write is `owner_stated` only with same-origin Tofu UI evidence (`auth.request_provenance`), otherwise `agent_report`.
  - Lessons reuse My Context's lifecycle vocabulary: `status` (`active`, `disproved`, `superseded`), `status_reason`, exactly one `superseded_by` pointer, server-owned `provenance` (`edited_*` keys record the last change) and an owner-set `sensitive` flag. Only active, non-sensitive lessons reach prefetch, default search and `load_eligible_memories`; an unknown hand-written status fails closed. A model cannot set `sensitive`, `enabled` or provenance.
  - Prefetch selects by the owner's words: a forward's appended Router evidence is dropped, and a Worker's delegation envelope contributes the owner's request and the brief, not its scaffolding. A lesson body is shown only through a tool the role holds (`search_memories` by id, `read_files` by path); with neither, the lesson is left out.
  - `memory_guidance` names only the memory tools the request holds. With `search_memories` it says when to consult the lookup: before asking the owner about a preference, before an irreversible or external act that relies on remembered state, and when asked why or what the role knows.
  - `lib/memory/secret_scan.py` refuses a lesson or My Context write whose new text has a credential shape that browser-evidence redaction removes (`network_evidence.secret_shape_findings`). The refusal names field, key and line, never the value. Text stored before the refusal existed can still hold one, so every model-facing memory read redacts the same shapes (`network_evidence.redact_text`): the owner lookup in both modes, default lesson search and the prefetched `relevant_memories` block. The owner's API returns stored text unredacted so the owner can find and remove it. A bare token without a credential key (for example a provider key pasted alone) has no shape and is not caught.
  - Owner API: `GET /api/v4/owner-memory` (`query`, `top_k`) and `GET /api/v4/owner-memory/<id>` (`conversation_id`, `top_k`) return the same records, with sensitive lessons included and flagged. The owner edits and deletes through `PUT`/`DELETE /api/v4/memory/<id>` (lifecycle and `sensitive` included) and the My Context routes.
- **Alternatives.** Excluding every agent-report lesson from prefetch, as Decision 8 words it, was not done: every model-written lesson is an agent report, so exclusion would end the prefetch the design kept after MEMORY-EVAL measured its benefit. Injected lessons are labelled `inferred` instead. A second owner-memory tool would add a tool name on budget-limited routes when `search_memories` already reads the lesson corpus. Keyword detection of sensitive categories would be a tuned classifier; sensitivity is the owner's flag, and models are told not to store personal details.
- **Not shipped.** The always-present profile (Phase 1) waits on the Coordinator's decision: experiment 1c did not pass (K3), and FM-2x2 answered K3's open question (see the design record below). Consolidation (Phase 3) has not started. Also absent: the My Context page and chat "forget X" (Decision 6), and code-checked sensitive categories (Decision 7, with consolidation).
- **Specs.** `tests/memory/test_owner_memory_lookup.py`, `tests/memory/test_memory_lesson_lifecycle.py`, `tests/memory/test_memory_secret_refusal.py`, `tests/memory/test_memory_role_fit.py`, `tests/router/test_owner_memory_roles.py`, `tests/api/test_api_v1_owner_memory.py`.

Memory is not a fallback transcript store or source of owner identity. New writes share one bounded payload contract and reject before corpus scans or mutation; one directory sidecar serializes ID selection and atomic publication across threads/cooperating POSIX processes, and suffix collisions use one directory snapshot. Direct-ID CRUD probes the canonical store/root order without enumerating the corpus; update/delete/toggle/clear/merge re-resolve under sorted directory-local mutation locks (at most 16 shards per directory), so cooperating writers are linearizable while unrelated shards remain parallel and non-cooperating edits retain revision-conflict protection.
Each corpus directory uses one closed `scandir` snapshot; retrieval retains a ten-field summary view (plus `status` or `sensitive` only on a lesson that is not active or is sensitive, so selection drops it without a body read) and hydrates selected IDs by exact canonical probes rather than rebuilding the corpus.
A bounded launch-probed LRU may retain recursively frozen parsed frontmatter only; matching fingerprints newer than 2.1 seconds are conservatively re-read because valid coarse-clock filesystems can preserve every stat field across a same-size edit, and every access carries its authenticated owner.

### Owner understanding: design record

**Status (2026-11-13).** The design below is decided.

- **Also landed:** the self-query, trust-class and lesson-lifecycle parts of Phases 2 and 4 ("Owner memory: self-query, trust classes and lesson lifecycle" above).
- **Phase 0, landed:** MY-CONTEXT-FIX ("My Context lifecycle and legacy inheritance" above) repaired the scope; its audit activates none of the 17 legacy items.
- **Measured baseline:** MEMORY-EVAL ([benchmark](../evaluations/OWNER_MEMORY_BENCHMARK.md), hand labels).
  - Roles applied a stated preference in a later turn 31.7% of the time.
  - 31 of 38 tested preferences are stored nowhere a role can retrieve.
  - Prefetch precision is 33.3% (7 of 21 injected lessons relevant), recall 100%.
  - Both measured false memories came from a stale lesson that prefetch injected.
- **Experiments so far** (see "Owner-profile experiments" below):
  - experiment 1 was killed (K5);
  - experiment 1b is inconclusive (K4);
  - experiment 1c did not pass (K3, after its registered resolution);
  - FM-2x2, which tests 1c's open K3 question with statistical power, passed.
- **Phase 1:** not yet authorized. That decision is the Coordinator's.
  - Experiment 1c passes every rule except K3. K3's only false memory is one S16/reduced response that states the stale no-SSH-key lesson as fact.
  - In FM-2x2 the always-present 1c profile does not raise "states a stale claim as current fact" by 10 pp or more. This holds with and without MEMORY-SELF-QUERY's `inferred` lesson labels.
  - Recommendation: ship Phase 1 with those labels.
    - Canonical carries them since MEMORY-SELF-QUERY's delivery (`1d5db9eae8`).
    - Its renderer is byte-identical to the tested `524844f02e`.
  - 1b's K4 resolution was not dispatched.
- **Everything else:** not implemented. That covers Phase 3 consolidation, the My Context page, chat "forget X" and code-checked sensitive categories.

This record guides the next changes; landed parts are described where they run.

**Owner-profile experiments.** How each arm is built:

- It adds exactly one `user_context` carrier to MEMORY-EVAL's recorded round-0 requests, rendered by the production `render_profile_block` and composer envelope at the composer's tail position. Nothing else in the request changes.
- Both arms are graded by the benchmark's own judge (claude-opus-5.5, rubric v2) against MEMORY-EVAL's baseline, with the scenario file the baseline was judged with.
- The unit is a (scenario, mode) cell. Every decision rule below was fixed before the first paid call.
- The arm code is evaluation-only and stays unpublished in the MEMORY-DESIGN lane. This record carries aggregates and scenario ids only.

| Rule | Measure, arm vs baseline | Kill when |
|---|---|---|
| K1 | application in reduced context (a later, unrelated turn) | the lower bound of the 95% scenario-bootstrap interval is ≤ 0 |
| K2 | application in full context | the interval's upper bound is < 0 |
| K3, K4 | false memory; unnecessary asks (summed cell means) | the excess is > 1 cell; an excess in (0, 1] is inconclusive and is resolved with 5 trials in both arms |
| K5 | necessary asks on should-ask controls (fewer is worse) | as K3 |
| K6 (from 1b) | turns that end in a provider refusal | as K3 |

- **Experiment 1 (2026-11-09): killed by K5.**
  - The arm put all 31 active owner-stated directives, as one profile, in front of every role.
  - K1–K4 passed: application rose 28.3% → 50.0% (+21.7 pp, 95% CI [+10, +34]; reduced context +25.0 pp [+8, +42]), unnecessary asks fell 23% → 7%, and false memory did not rise.
  - Necessary asks fell 31% → 13% (2.5 → 1.0 of 8 cells). The three always-present authorization grants read as a standing "do not ask".
  - Found afterwards, neither of which changes the rule's verdict:
    - In 21 of 76 arm cells and 28 of 75 baseline cells, trial 1 was a cache replay of trial 0, so those cells hold one sample. Both decisive K5 cells (S36 and S38 reduced) rest on a single response on at least one side.
    - On the eight should-ask cells, experiment 1's provider refusals also rose, from 0.5 to 2.0 cell-events, all of them on S38, the "you decide is not a blank cheque" control. Over all cells it had fewer refusals than the baseline (2.5 vs 3.5).
    - Prefetch injected a different lesson set in 5 cells (S10, S24, S33 full), because the shared lesson corpus changed between the baseline and the arm run.
- **Experiment 1b (pre-registered 2026-11-10):**
  - **Profile:** the same arm without the `authorization_boundaries` items (P01–P03), which stay with the delegation mechanism.
  - **Scope:** each role gets only its own scope. The Global Router gets global items only (23); the tofu Coordinator and Worker get global plus `project:tofu` (24).
  - **Usage rule:** one added sentence, that a directive never supplies a missing fact and never approves an irreversible or external act, so the role asks.
  - **Trials** are independent samples (harness fix, 2026-11-10). A cell whose two trials replayed counts as one sample.
  - **Stage 1, screening:** the four should-ask controls (S35–S38) in both modes, with 2 trials (about $20).
    - Any kill under K5 or K6 stops the experiment.
    - An inconclusive result gets 3 more independent trials in both arms before deciding.
    - A pass leads to stage 2.
  - **Stage 2:** the full benchmark under K1–K6. It reuses the stage-1 rows and is reported against both the baseline and experiment 1.
  - **Sensitivity:** an analysis excludes every cell whose injected lesson set differs from the baseline's.
  - **Stage 1 result: pass**, at the threshold. 16 independent calls, $15.63.
    - K5 necessary asks: 2.5 → 2.5 cell-events. S36 reduced −0.5 and S38 full +0.5 cancel.
    - K6 refusals: 0.5 → 0.5.
    - Experiment 1 on the same cells: 1.0 and 2.0.
  - **Stage 2 result: INCONCLUSIVE, by K4.** 152 usable rows, all judged, no replayed trial; $206.3 for the whole experiment.
    - K1 passed: reduced-context application rose 16.7% → 36.7% (+20.0 pp, CI [+8.3, +33.3]).
    - K2 passed: full-context application rose 40.0% → 53.3% (+13.3 pp, CI [0.0, +28.3]).
    - K3 passed: no false memory in either arm.
    - K4 inconclusive: unnecessary asks went from 14.0 to 15.0 cell-events over 60 cells (23.3% → 25.0%, CI [−7.5, +10.8] pp). Excluding the 5 drifted cells, the excess is +2.0, a kill in the sensitivity analysis.
    - K5 passed: 2.5 → 2.5. K6 passed: refusals fell 3.5 → 0.5.
    - Against experiment 1: application 50.0% → 45.0% (CI [−15.8, +5.0]); unnecessary asks 6.7% → 25.0% (CI [+10.8, +26.7]); necessary asks 12.5% → 31.2%.
    - Reading: experiment 1's drop in unnecessary asks came from the grants, the same lines that suppressed necessary questions. Without them, roles ask at about the baseline rate, and more often for the Router in full context (0% → 18%, 2 of 11 cells).
    - Not run: the pre-registered K4 resolution, 3 more trials of all 60 apply cells in both arms (about $500, about 6 h). The Coordinator chose experiment 1c instead.
- **Experiment 1c (pre-registered 2026-11-12): NOT PASSED, by K3 after its registered resolution.**
  - **Profile:** 1b's profile byte for byte, plus one section after the directives.
    - The grants P01–P03 appear in the owner's own words, each quote with its source turn.
    - Their bound is five sentences quoted verbatim from existing policy, with no new wording: the Coordinator policy's "Ask only what only the user can decide or provide", `docs/ROUTER.md` R9 and R11's authority classes, and the platform's action policy.
    - Carriers: 1,867 tokens (Router) and 1,917 (tofu Coordinator and Worker), 538 more than 1b's.
  - **Corpus:** the lesson corpus is frozen at 1b's state. One global lesson was deleted from it on the owner's decision; offline parity still reproduces every recorded 1b request.
    - 1c and 1b therefore send identical plain requests, so 1c against 1b isolates the delegation section.
    - The primary analysis excludes the 5 drifted cells; all cells is the sensitivity. Both give the same verdict.
  - **Stage 1: pass.** K5 necessary asks 2.5 → 3.0; K6 refusals 0.5 → 0.5.
  - **Stage 2, primary analysis:**
    - K1 passed: reduced-context application +16.1 pp, CI [+5.4, +28.6].
    - K2 passed: full-context application +14.3 pp, CI [−3.6, +32.1].
    - K3 inconclusive: false memory went from 0 to 0.5 cell-events, an excess of +0.5.
    - K4 passed: unnecessary asks 12.5 → 12.0 cell-events. 1b on the same cells: 14.5, a kill.
    - K5 passed (2.5 → 3.0) and K6 passed (2.5 → 0.5).
  - **Reading.** Bounded delegation removes 1b's needless-ask regression without experiment 1's loss of necessary questions.
    - Needless asks only return to the baseline level: 21%, against 7% in experiment 1. The change against 1b is −4.5 pp, CI [−12.5, +2.7].
    - Application equals 1b's (45.5% against 47.3%).
    - The grant scenarios (S01, S02) show no consistent gain.
  - **K3 resolution (2026-11-13): not passed.** Trials 2–4 of the 8 negative cells ran in both arms, on the frozen corpus.
    - Over trials 0–4, false memory went from 0.0 to 0.2 cell-events, an excess of +0.2. An inconclusive result after the resolution counts as not passed. It is not a kill.
    - Trials 2–4 alone are 0 against 0. The only event is still the stage-2 row: S16/reduced stating the stale "no SSH key on the dev platform" lesson as current fact.
  - **Hand labels** (one labeller for both arms, blinded, reported only).
    - Under the scenario's own definition (S16: the dev permission is closed) both arms have 0. Under the ledger's (any disproved belief) they reproduce the judge on 80 of 80 rows.
    - All six borderline rows are S16/reduced: 4 of 5 1c responses against 2 of 5 baseline responses. These state the claim, then qualify it as old data to be re-checked. Reduced context has no record of the 0.19.0 SSH push, so the stale lesson is the only evidence about the key.
    - Stage 2 read MEMORY-EVAL's labels as counting the claim twice in the baseline. With one labeller across both arms, the baseline has no such row; one of those two labels is on a response that calls the memory stale.
  - **Reading.** The claim comes from the stale lesson, not from the profile. But in the one cell where that lesson is the only evidence, 1c states it more assertively. Whether an always-present profile makes prefetched lessons sound authoritative was the open question; FM-2x2 (below) answers it: it does not.
  - **In production** the live lesson was corrected on 2026-11-12. MEMORY-SELF-QUERY's lesson lifecycle keeps disproved and superseded lessons out of prefetch and labels injected lessons `inferred`. The frozen corpus keeps the stale state every arm was measured on.
  - **Cost:** about $244 for 154 rows plus judging, against an estimate of $206; the K3 resolution added $77.52 for 50 rows. Median full-context output rose 30–50% over 1b.
  - **Transport:** one production AppId, at most 2 calls in flight, the first 429 (HTTP or in-stream) ending a run. No 429 occurred.
  - **Injection precision, fixed harness.** The baseline is 7/21 = 33.3%. Experiments 1 and 1b are 7/24 = 29.2%: their 3 extra lessons, from the drifted corpus, are all irrelevant. Recall is 7/7 in all three. Every lesson injected for the Global Router was irrelevant.
- **FM-2x2 (pre-registered 2026-11-13): PASS for both label formats.** It asks 1c's open question directly: does an always-present profile make a prefetched stale lesson sound authoritative?
  - **Design:** a 2 × 2 inside each of six frozen stale-but-corrected scenarios, all in reduced context on the frozen corpus.
    - The scenarios are S16's request plus five new ones. They cover two stale facts: the no-SSH-key lesson and a since-removed tool-schema token budget.
    - One factor is 1c's profile carrier, absent or present.
    - The other factor is the prefetched lessons, either exactly as MEMORY-SELF-QUERY `524844f02e` renders them (`- trust: inferred (unrecorded source)`) or with only those labels removed.
    - Everything else in a scenario's four requests is byte-identical.
  - **Rule:** PASS when the upper bound of the 95% interval of the profile's effect is below +10 pp. n = 25 trials per cell came from a blinded interim, for 80% power. The judge and blind hand labels (one labeller, arm hidden) must both pass.
  - **Result, with R4's labels** (the decision): −2.0 pp [−8.8, +5.1] by the judge, 0.0 [−4.1, +4.1] by strict hand labels.
  - **Result, without labels:** −6.7 [−13.1, +0.7] by the judge, −1.9 [−6.7, +3.3] by hand.
  - **The labels' own effect:**
    - They lower the judged rate by 11.0 pp [5.3, 15.1].
    - All 6 strict, unhedged assertions among 474 hand labels are in unlabelled arms.
    - In S16's request the profile lowers the claim: judge 18/50 → 2/50.
  - **Caveats:**
    - Leaving S16's request out, the labelled profile effect is +3.2 [−4.3, +10.2].
    - Only two stale facts are tested, in reduced context only.
    - The judge's `falseMemory` also counts claims that are stated, then hedged. It agrees with strict hand labels at κ 0.07 and with lenient ones at κ 0.72.
    - With the profile, Coordinator output rises by about 47%.
    - About 39% of responses still state the stale claim as current and then hedge it, with or without the profile. That comes from the stale lesson being injected at all, which is what the lesson lifecycle is for.
  - **Cost and transport:**
    - $354.58 for 600 responses plus judging.
    - 1,861 calls on one production AppId, at up to 16 in flight and 12 starts per minute: 0 retries and 0 429s. Production opus traffic on the same AppId ran at 11–35 rounds per minute meanwhile.
  - The protocol (PREREG A4) and the code are lane-only: `refs/worktree/memory-design/fm2x2` and the bundle `.eval-runs/owner-memory/fm2x2/lane-experiments-fm2x2.bundle`.

**Problem.** Roles do not come to know the owner, and memory is passive.

- **My Context was empty for the owner, and still holds no active item.**
  - My Context is the only always-on, cross-project owner channel.
  - Tasks read scope `str(owner_user_id)` (`manager/_registry.py`, `_profileScope`; `_providers.py::_profile_block`).
  - The populated 17-item document sat in legacy scope `''`, which no path read since `local_admin_context()` set `owner_user_id`. Phase 0 inherits it, but its audit activates none of the 17.
  - The learner is switched off (`profile_consolidate=false`). Across 209 Global Router turns and 116 Coordinator turns, `preferencesApplied` appeared 0 times.
- **Automatic memory is round-0 prefetch only.**
  - It runs metadata-only BM25 over project lessons, admits on two shared tokens and injects at most two items.
  - For coordination roles, 97–99% of hits were boilerplate overlap.
  - It has surfaced a sensitive personal memory into unrelated Router turns.
  - It points at `read_files`, which those roles do not have.
- **Router, Coordinators and Watchdog have no memory tools.**
- **Lessons have no provenance, validity or disproved state,** so a memory a Worker disproved is still injected.
- **Preferences are retyped and still missed.** 40 of 281 Router delegations restate "in Chinese".
- **Research (2026-11-09).**
  - Presence in context is necessary but not sufficient (PrefEval, HorizonBench).
  - Gains from consolidation ("dreaming") are vendor-reported.
  - Memory increases sycophancy.
  - Memory injection is a demonstrated attack.

**Decision.**

1. **One owner store.**
   - Owner-stated preferences live in My Context (`lib/memory/user_profile`), owner-scoped and cross-project.
   - They do not go into project or global lessons, or into a new store.
   - A project's product decisions and facts (a repository layout, say) stay in that project's `AGENTS.md`/docs, which every role of that project already receives as project rules. They never become owner preferences.
   - **Fail closed on a missing scope:** in place since MY-CONTEXT-FIX's repair (`profile_scope_missing`; see "My Context lifecycle and legacy inheritance" above). Later phases keep it.
2. **One lifecycle vocabulary,** MY-CONTEXT-FIX's stored fields: `status` (`active | superseded | needs_confirmation`; missing means active), `status_reason`, `superseded_by`, and server-owned `provenance`. Only `active` is injected; other items live in the same document's `inactive_items` list.
   - **Provenance.** Every item records `provenance.source_class`, plus conversation, Turn and time; owner quotes stay in owner-visible evidence, not in shipped code.
   - **Source classes** are fixed by the write path, never by a model field:
     - `owner_stated`: verbatim from a genuine human Turn, checked by the standing-instruction fence (`require_human_turn`, quote within the authored text); or typed by the owner in My Context.
     - `owner_confirmed`: a proposal the owner accepted.
     - `legacy_import`: audited by MY-CONTEXT-FIX.
   - **Unverified content.** Inferred or agent-reported content can only be `needs_confirmation`. A Router or Worker report is never owner speech.
   - **Supersession** sets `superseded_by` to exactly one of an item id, an owner Turn (`conversation_id`, `turn_id`, `at`) or a repository evidence pointer, plus a `status_reason`: `stale`, `disproved`, `duplicate`, `conflicts_with_owner_decision` or `owner_revoked` (`needs_owner_confirmation` marks proposals). It is never "latest wins" by similarity. The superseded record stays, so "why" can be answered.
   - **Expiry.** A scope or end condition is kept only as the owner stated it. There is no TTL.
3. **An always-present profile.**
   - The existing `user_context` block renders every active item as one directive line with id, date and source, grouped by scope.
   - Workers and sub-agents never receive `identity` items. Project-scoped items reach only that project's roles.
   - **Authorization grants are not profile items.** "Decide and proceed", "drive it without routine approvals" and restart timing stay with the delegation mechanism (`docs/ROUTER.md` R9, restart delegation), each with its stated scope. Present on every turn, they suppressed necessary questions in the first experiment.
     - Experiment 1c tested the alternative: the grants in the owner's own words, bounded by existing policy quoted verbatim. It removed 1b's needless-ask regression but did not pass (K3), so this rule stands.
    - FM-2x2 used 1c's carrier, bounded grants included, and found that it does not make stale lessons sound authoritative. Whether bounded grants replace this rule is the Coordinator's decision.
   - **The usage rule** says a directive never supplies a missing fact (who, which, where) and never approves an irreversible or external act; when either is missing, the role asks.
   - **Size is set by content.** The owner's own statements bound it. Measured: 31 owner-stated directives render to 1,699 tokens.
   - The `user_context` ceiling (1,000 tokens) and `CONTEXT_CHAR_CAP` (2,500 characters) are removed, because they would silently clip directives. `ContextPlanV2` stays the only budget authority.
4. **Self-query.**
   - One read-only owner-memory tool for every role. It searches and explains My Context items, superseded ones included with their provenance, and lessons with their source class.
   - Role guidance makes it mandatory in three cases:
     - before asking the owner about a preference;
     - before an irreversible or external act that depends on remembered state;
     - when the owner asks "why" or "what do you know".
   - Answers name the item ids they used.
5. **Bounded consolidation ("dreaming").**
   - **Triggers are events, never timers:** a settled genuine owner Turn, an owner edit, or a verified disproof.
   - **Inputs:** genuine human Turns and verified outcomes only. It never reads assistant, tool, web or channel-relay text, nor recalled carriers.
   - **Outputs:** `needs_confirmation` proposals with verbatim evidence. Code checks the quote match, the named predecessor, sensitive categories and secrets.
   - **Confirmation:** the owner confirms proposals in My Context or in chat.
   - **Explicit requests.** An explicit "remember this" or "from now on" in a genuine owner Turn is `owner_stated`: active at once, with visible undo.
   - **Cost** is accounted as its own task kind.
6. **An owner surface.**
   - **My Context page:**
     - lists active, needs-confirmation and superseded items, each with a link to its source;
     - supports edit, delete and undo;
     - confirms `needs_confirmation` items one at a time, each shown with its evidence; there is no confirm-all, and an item without evidence stays unconfirmed;
     - answers "why" with the provenance chain plus the turns that applied the item (`preferencesApplied`).
   - **Chat channels:**
     - The Router answers "what do you remember" and "forget X" through the same fence.
     - Proposals reach chat only through the Router's digest rules.
7. **Privacy and safety.**
   - **Sensitive categories** are never inferred or stored unless the owner explicitly asks. Even then they never reach Workers or channels.
   - **Secrets:** a write-time secret scanner checks every memory and My Context write. It reuses the browser-evidence redaction shapes rather than adding a third copy.
   - **Untrusted content is never promoted.** Tool, web and file output, other participants, Tofu's own sent messages and recalled carriers all fall under a structural taint gate, not a score.
8. **Prefetch for coordination roles** (Global Router, Coordinators, Watchdog) stays. MEMORY-EVAL's variants decided this, not a threshold.
   - With prefetch off, Coordinator full-context application fell 56.2% → 43.8% and Router re-asks in reduced context doubled.
   - The stale lesson it injects caused both of MEMORY-EVAL's measured false memories, and experiment 1c's one.
   - Had prefetch been turned off, those roles would rely on the profile and the self-query tool, with Workers keeping prefetch. The data rejected that outcome.
   - Kept prefetch gets only structural fixes, with no score floor, top-k or cap change:
     - query from the owner's own words, not delegation boilerplate;
     - never inject a body the role cannot open;
     - exclude superseded, disproved, agent-report and sensitive memories.
9. **Phases.**
   - Phase 0: scope fix.
   - Phase 1: profile, after an owner-profile experiment passes. Experiment 1c did not (K3). FM-2x2 answered K3's question (PASS), so authorization is the Coordinator's decision.
   - Phase 2: self-query and citations.
   - Phase 3: consolidation, proposal-only. It starts only after an offline precision test against MEMORY-EVAL's owner ledger.
   - Phase 4: lesson source classes and a disproved state.

**Alternatives considered.**

- **Better retrieval only** (embeddings, score floors, larger top-k): rejected.
  - These are tuned knobs.
  - Presence is not use.
  - It fixes neither the scope, nor provenance, nor the roles without tools.
- **Nightly dreaming that writes memory directly** (OpenClaw, ChatGPT): rejected for now.
  - Its evidence is vendor-only.
  - False-memory, sycophancy and self-reinforcement failures are documented.
  - A timer is a tuning knob.
  - Synthetic actors may not speak for the owner (`docs/ROUTER.md` R9).
- **A new owner-preference store or record type** beside My Context, standing instructions and working facts: rejected.
  - Standing instructions remain trigger-scoped verbatim forwards.
  - Working facts remain conversation evidence.
- **Owner preferences as project memories,** today's workaround: rejected. They are invisible across projects and to the Router, and carry no provenance.
- **Ranking or truncating the profile to a token budget:** rejected, because it silently drops owner directives.
- **A model-written source class or confidence:** rejected, because provenance must come from the write path.

## Failure semantics

- Exact tokenization unavailable: use the declared heuristic and expose its
  provenance; do not pretend the count is exact. Tokenizer vocabularies ship
  with the install, so a count never waits on the network. An encoding whose
  vocabulary fails to load is not retried until the install is repaired and
  the process restarts (record: *Shipped tokenizer vocabularies* below).
- Context over budget: compact through registered stages, then return a typed
  context-limit failure if the irreducible payload still does not fit.
- Final-admission summary failure: preserve live/transient source and refuse
  an overfull main dispatch, retaining only a current issued safe credential
  cause. Unquoted auxiliary credential outages may requalify within the existing
  owner/quote policy; explicit utility recovery remains separate. Auxiliary
  selection and dispatch never challenge a recorded billing stop
  (`docs/CONTEXT_BUDGET_POLICY.md`). Retain usable complete model text after
  shaping faults.
- Concurrent manual compaction: reject/retry from a fresh transcript revision.
- Invalid tool history: fail or repair at the single message-construction
  boundary, with diagnostics.

## Invariants

- One context-window authority and one token-counter resolution API.
- `lib/tasks_pkg/message_semantics.py` owns human-source classification and the private
  source-field allowlist. Cold history reconstruction preserves those fields
  through counting, task-goal extraction and compaction retention; protocol
  sealing strips them before provider wire output. Automatic user-role carriers
  cannot become human goals simply by passing through a rebuild. The canonical
  initiator reader returns a non-human `unknown` sentinel for malformed explicit
  values; absent/null legacy initiators remain human, and writers cannot stamp
  the unknown sentinel.
- Explicit Turn identity takes precedence over legacy timestamps during
  request-local deduplication. Equal timestamps never collapse distinct Turns
  or different source kinds; explicit Turn/source boundaries survive same-role
  merging until protocol conversion.
- Deterministic provider order and observable segment provenance.
- Durable messages are never mutated by request-local preparation; same-role diagnostics classify original pair boundaries using short-lived producer identity deleted before provider/debug wire output, so synthetic carriers, relocated objective anchors and a request whose empty interrupted/failed reply history projection dropped (`_replyDropped`) stay silent while naked real-to-real duplicates still warn.
- Human-facing delivery records are not model turns: history projection omits Coordinator report Turns (`_turnKind` `coordinator_report.*`), whose content reaches the model as the publishing reply's own `report_to_user` call/result ([`PROJECT_COORDINATION.md`](../PROJECT_COORDINATION.md), "Model view").
- Runtime facts are append-only tail user carriers; `platform_static`,
  `ui_nav_directory`, `parallel_execution`, and `delegate_mode` hold dedicated
  system-prefix carriers.
- Persistent compaction is atomic and revision-guarded.
- System, project, identity, and safety instructions retain declared priority.
- Tool call/result pairs remain adjacent and correctly identified.
- Memory retrieval is owner-scoped and bounded; My Context review uses the latest real user turn, strict optional billing/contention admission, and at most one actual upstream 429; skips and failures cannot erase base context.
- Prompt-cache decisions stay stable for the lifetime of one task. Explicit zero/floor reads replace the predecessor; missing usage does not. Owner-scoped completed-turn publication uses the task-result checkpoint, never scope teardown; children/transients cannot publish parent facts. Replay and restart semantics are in [`STORAGE.md`](../STORAGE.md).
- Automatic semantic compaction follows the shared route-window safety
  boundary with output/recovery reserves; retired economic settings and price
  tiers cannot move it earlier ([context budgets](../CONTEXT_BUDGET_POLICY.md)).
- Turn cost aggregates per API round under that round's model/provider/tier,
  and the UI exposes total, uncached, and cache-read tokens together.
- Required `ContextPlanV2` blocks cannot be evicted by optional evidence.
- Mutable facts carry observation/world-version metadata and are revalidated
  before a claim depends on their freshness.
- Provider usage reuse requires a complete measured-prefix digest, exact model,
  and unchanged system/tool envelope; a historical count cannot downclamp a
  newly reconstructed request. Selected-route input above its hard window is
  refused locally before transport and enters the existing compaction recovery.
- Per-round measurement uses the shared local token authority. Its bounded
  digest entries carry encoding-scoped BPE and entropy counts without prompt
  text under a launch-probed 4,096-entry ceiling. Admission may hand off 4,096
  text identities, one schema integer, and one exact digest synchronously;
  identical list/model/ordered schema objects are required; none is serialized.

**Measured-prefix encoding — Problem:** identical encoder configuration was rebuilt per message. **Decision:** reuse one request-local encoder; full canonical digests, encoding failures and mutation denial remain exact.
**Alternatives:** identity checks miss edits, prompt caches retain history, and changing encoders can alter numeric/error semantics. Public usage-cache fixtures measured completion CPU 12–19% lower for short/history/mixed content.
Three 1,000-operation wide pairs passed unchanged tail/RSS guards: CPU 1–3% lower, P99 1–3% higher. `system_usage_signature_encoder_admission_20261009.json` retains earlier failures; inference and whole-context latency are excluded.

**Round-prep token counting — Problem:** every model round counts the whole append-mostly request three to five times (compaction, its gate, prompt admission). Each count derived a SHA-256 cache key for every history text of at least 512 characters and re-tokenized every shorter or uncached text through `Encoding.encode_batch`. CPython hashlib releases the GIL for each update of 2,048 bytes or more, tiktoken releases it on every `encode`, and `encode_batch` also starts an eight-thread pool per call; beside other runnable threads each release waited about a switch interval, so one counting step of a 290-message history took about 1.8 s beside two busy threads (a 240 s live py-spy profile put 62% of compaction time in cache-key hashing). **Decision:** (1) text digests come from `lib.utils.sha256_holding_gil`, byte-identical and never handing the GIL away. (2) `_encode_batch_counts` tokenizes one `<|endoftext|>`-joined batch with only that special token allowed and splits counts at its token id; this equals per-text `encode(text, disallowed_special=())` because tiktoken cuts the text at an allowed special before regex pre-tokenization. Texts that spell the separator keep their own call, and the borrowed native encoder counts its batch natively. (3) The task-owned `RequestTextCounts` memo maps `SHA-256(text) → exact count` for exactly the latest counted request. Misses try the shared digest cache, then one batch call. A hit therefore means an equal text, never a hash collision. The memo retains no text and has no capacity knob (about 130 bytes per distinct text); every count replaces it, and it dies with the task. Concurrent counts read and publish one immutable `(encoding, counts)` snapshot in a single assignment. A racing count, including one through a memo shared by a copied task dict or a detached provider snapshot, can therefore only lose reuse and never mix encodings. An encoding change discards the previous snapshot, and a failed tokenizer call publishes nothing. (4) The network-tier prefilter reuses digest-cached entropy estimates. Token counts, gate measurements and wire bytes are unchanged. **Alternatives:** keying the memo by `(len, hash(text))`, as first drafted, let two same-length texts whose 64-bit string hashes collide share one count. Keying by the `str` itself skips the digest (27 instead of 31 ms per idle round on the real history below), but it would pin the last request's text after terminal persistence releases `messages`. A larger shared LRU still tokenizes every text under its 512-character floor. Lowering the interpreter switch interval taxes every thread, counting a plain concatenation is inexact, and sequential `encode` still releases the GIL once per text. **Measurement** (`benchmarks/round_prep_body.py`, Python 3.12.13, 8 rounds, p50 per round of compaction + gate + admission, median of two alternating fresh-process runs; per-round body, row and measurement digests identical across all compared trees): on a real 290-message Request Inspector history (1.18 M text characters, 53 tools), idle time fell from 102 to 31 ms. With two CPU-bound threads it fell from 5,469 to 80 ms; the batch and digest changes alone reached 126 ms and the memo alone 1,898 ms. With four threads (one run) it fell from 9,160 to 97 ms. A synthetic 400-message history fell from 117 to 43 ms idle and from 4,467 to 96 ms with two threads. The remaining idle time is mostly the digest-cached entropy prefilter and heuristic floor (about 26 of 34 ms per round). Storage commits and the real serving thread mix are excluded. Specs: `tests/token_counter/test_token_text_cache.py`, `tests/token_counter/test_token_counter_dispatch.py`, `tests/utils/test_gil_held_digest.py`, `tests/meta/test_round_prep_body_measure.py`.

**Shipped tokenizer vocabularies — Problem:** Tier 2 got its vocabulary from tiktoken's download cache (`TIKTOKEN_CACHE_DIR`, else `$TMPDIR/data-gym-cache`). That cache starts empty after a reboot (`/tmp` is emptied at boot), under a private `TMPDIR` and on a fresh host, and it is refetched when a file fails its hash. On a miss, `tiktoken.get_encoding` downloaded the file through a raw `requests.get` with no timeout. That call ran outside the network authority and honoured only environment proxies. `_get_encoder` held one module-wide lock across that download, and cached hits took the same lock. Measured on R3 (canonical `1d5db9eae8`):
- With black-holed egress, the first count, later counts and counts on an already loaded encoding were all still blocked after 12 s. A SYN-dropping route waits for the kernel connect timeout, about 127 s.
- Refused egress cost one attempt per encoding per process, then the heuristic. A process counting both encodings made two attempts.
- `supports()` loaded cl100k for every model. A cold gpt-4o count built both encodings in 0.87 s, and 40 concurrent cold callers each waited 1.23 s for two loads.

**Decision:**
- Both vocabularies ship with the install as `lib/token_counter/vocabularies/{cl100k_base,o200k_base}.tiktoken`.
  - Provenance: byte-identical to `https://openaipublic.blob.core.windows.net/encodings/<name>.tiktoken`. They are pinned by the SHA-256 that tiktoken itself pins (`2239…b2a7`, `446a…1a2d`). They are the same files as the tiktoken-rs 0.11.0 assets that the native rank tables embed (`packages/tofu-runtime/src/token_ranks/`).
  - License: MIT, Copyright (c) 2022 OpenAI, Shantanu Jain (openai/tiktoken). The notice ships beside the files as `LICENSE-openai-tiktoken`, the same notice the native archives carry.
  - Distribution: they are `lib.token_counter` package data (`pyproject.toml`), PyInstaller data (`tofu.spec`) and release-gate members (`scripts/check_developer_runtime_artifacts.py`).
  - Updates carry them with the code. No install-time or update-time fetch exists, so no download has to be routed through the network authority.
- `tiktoken_vocabulary.load_encoding` builds the `tiktoken.Encoding` from the shipped file.
  - It verifies the SHA-256 and uses tiktoken's split patterns and special tokens, copied from `tiktoken_ext/openai_public.py` and checked against the installed tiktoken.
  - The count path never calls tiktoken's URL loader and never reads its cache, so no count opens a connection.
- Each encoding has its own load lock (single-flight per encoding).
  - Loaded encoders are read without a lock, and no module-wide lock exists.
  - Concurrent cold callers of an encoding wait for its one local load. Cached hits and counts on the other encoding never wait for it.
- A failed load is remembered per process with its reason, and one warning is logged.
  - A missing `tiktoken` disables Tier 2. An unreadable or damaged vocabulary disables only that encoding.
  - Its counts fall through to the next tier (normally the heuristic, which declares its provenance) without another load.
  - Recovery: restore the file (it is tracked in Git; reinstalling or updating also restores it), then restart. The API host and the execution host each keep their own memo.
- `supports(model)` loads only `encoding_for_model(model)`.
- No setting, environment variable, cap, retry or timeout is added.

**Cost:**
- Startup and idle: none. Import neither loads tiktoken nor reads a vocabulary, and the new module imports in about 1 ms.
- First count of an encoding in a process: about 0.15 s CPU and +41 MiB RSS for cl100k, and about 0.31 s and +75 MiB for o200k. This equals tiktoken's own load from a warm cache. Measured as three paired fresh-process runs per encoding (Python 3.12.13, tiktoken 0.14.0), reading the files from local disk and from DolphinFS alike.
- A process that counts only o200k models no longer also loads cl100k.
- Disk: each checkout, wheel and frozen app grows by 5,296,126 bytes. Git storage grows by about 2.5 MB compressed.
- tiktoken's cache directory is no longer written.

**Alternatives:**
- Fetch at install or update through the network authority. Offline installs and allowlisted evaluation sandboxes would still lack exact counts. Every install path (installer, image, frozen app, self-update, runtime replicas, lane worktrees) would need the hook. A skipped or failed fetch silently degrades every count to the heuristic.
- Fetch lazily on the first count through the authority. A count would be bounded by the shared client's 30 s deadline instead of by nothing, but it would still wait on the network, and refused egress would still cost one attempt per encoding.
- Pre-seed tiktoken's cache, as the agent-loop load harness did. A reboot or a private `TMPDIR` empties the cache, and a miss falls back to the unbounded download.
- Point `TIKTOKEN_CACHE_DIR` at the shipped files. That is a process-global environment change that children inherit. An operator's own value would win, and a missing file would still download.
- Default to the native counter, which embeds the vocabularies. Its default activation is not admitted (`docs/DEVELOPER_RUNTIME.md`).
- Serve the heuristic while a cold load runs. Concurrent counts of one request would then disagree, and compaction would depend on timing.
- Keep one lock and give it a timeout. That adds a knob, and cached hits would still wait.
- Load both encodings at startup. That taxes startup and idle by about 116 MiB.
- Retry a failed load after a TTL or within an attempt budget. Both are tuning knobs and still repeat the failure.

**Specs:**
- `tests/token_counter/test_tiktoken_cold_start.py`: cold start after reboot with refused or black-holed egress, at most one attempt per process, 40 cold callers with one load, `supports()`, and a black-holed load that blocks neither encoding's other counts. All 7 failed before this change.
- `tests/token_counter/test_tiktoken_shipped_vocabulary.py`: provenance pins, parity with the installed tiktoken, and distribution.
- `tests/token_counter/test_tiktoken_encoder_failure_memo.py`: the failure memo and its recovery.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| New automatic context source | `context_composer/registry.py` first, then provider/event producer via public facade | injection registry/no-bypass, ordering, provenance, budget, protected-authority and tool-batch tests |
| Global context budget or task projection | `context_composer/_render.py`, `task_state.py` | required-block, determinism, permission, cache-epoch tests |
| Model context window or tokenizer | `lib/context_limits/`, `lib/token_counter/` resolver | self-heal, model-profile, exact/fallback parity tests |
| Compaction stage or manual compaction | `lib/tasks_pkg/compaction/_steps.py`, `_manual.py`, `_persist/` | anchors, tool pairs, usage counting, route/concurrency/durable-reload tests |
| Memory selection / My Context learning | `lib/memory/prefetch/`, `relevance/`, `profile_consolidate.py` | owner isolation, grounding, specificity |
| Owner-memory lookup, trust classes, lesson lifecycle | `lib/memory/owner_memory.py`, `contracts.py`, `secret_scan.py`, `storage/` | trust, audience (identity, sensitive), lifecycle reload, credential refusal, role ceilings |

## Test map

```bash
pytest -q tests/context/test_context_composer.py tests/context/test_context_limits_selfheal.py tests/context/test_context_injection_registry.py tests/context/test_context_injection_aux.py tests/context/test_context_trace.py
pytest -q tests/tasks_pkg/test_token_budget_reminder.py tests/agent_core/test_agent_notes_journal.py tests/project_mod/test_journal_maintenance.py tests/diagnostics/test_request_inspector.py tests/token_counter/test_token_counter_heuristic.py
pytest -q tests/compaction/test_compaction_invariants.py tests/compaction/test_compaction_anchor.py tests/compaction/test_compaction_receipt.py tests/compaction/test_compaction_handoff.py tests/compaction/test_compaction_history_wire.py tests/compaction/test_compaction_history_providers.py tests/compaction/test_l1_sent_step_admission.py
pytest -q tests/compaction/test_scheduled_working_view_compaction.py tests/compaction/test_summary_coverage.py tests/tasks_pkg/test_coordinator_context_lifecycle.py
pytest -q tests/tasks_pkg/test_manual_compaction_engine.py tests/tasks_pkg/test_manual_compaction_route.py tests/memory/test_memory_prefetch_local.py tests/memory/test_memory_global_server_store.py tests/memory/test_memory_create_allocation.py tests/memory/test_memory_direct_lookup.py tests/memory/test_memory_mutation_concurrency.py
pytest -q tests/tasks_pkg/test_long_agent_v2_contracts.py -k 'context_plan or task_state or l2_preserves' tests/tasks_pkg/test_prompt_profile_adoption.py
pytest -q tests/memory/test_owner_memory_lookup.py tests/memory/test_memory_lesson_lifecycle.py tests/memory/test_memory_secret_refusal.py tests/memory/test_memory_role_fit.py tests/router/test_owner_memory_roles.py tests/api/test_api_v1_owner_memory.py
```

Profile review demand — Problem: one process-wide semaphore dropped eligible reviews across unrelated owners, and file-backed context mutations lacked a complete read/mirror/history transaction. Decision: eligible reviews start independently; ordinary provider/billing policy remains while immediate-only contention admission is retired. The captured task owner reaches model dispatch. context_transaction(scope) uses the shared weak JSON-path/process locking seam, reentrant only inside one storage transaction; model calls stay outside it. Assistant updates recheck their original item snapshot under the write transaction, and new-item/tombstone checks share that transaction. Alternatives: larger semaphore counts still drop work; a global learner mutex serializes independent owners and paid calls; unlocked parallel writes lose items/undo evidence. Concurrent writes, duplicate learning, stale manual edits and per-task event tests verify the boundary.
<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->


### Cache diagnostic module boundaries

**Problem:** a single cache detector mixed provider-byte comparisons, policy,
state mutation, durable checkpoints and human diagnostics; task startup also
carried duplicate project preparation. **Decision:** `_detect` remains the sole
locked round coordinator. `_wire` compares the prior snapshot, `_policy` owns
pure thresholds and buckets, `_advance` updates the baseline and guarded sinks,
and `_report` formats confirmed evidence. `_health` measures same-route
read-share splits across rounds; it only labels records and reports, never
buckets. Observation order and one round record remain unchanged. The shared `_startup` owns project preparation for
every role; `_vu_startup` only supplies role-specific phase feedback.

`wire_fingerprint_diff.py` owns pure canonical and raw field comparisons. The
canonical comparator skips field-set construction for equal native dictionaries;
changed fields retain ordering, truncation and missing-value semantics.
**Alternatives considered:** a second detector or independent policy per mode
would drift; moving code without naming side-effect boundaries would preserve
the original coupling; dropping evidence to save work would weaken diagnosis.
A universal comparison loop and a zip-based variant were measured and rejected:
they slowed the existing raw-field comparison. Its specialized loop is retained.

Verification: `tests/cache/`, startup/abort tests and wire fingerprint comparison
contracts. Measure component CPU, wall time and allocation with
`python3 benchmarks/wire_fingerprint_compare.py --baseline-source PATH
--output PATH`; freeze `lib/tasks_pkg/wire_fingerprint_diff.py` from Git as the
baseline source and retain the JSON source hashes and paired samples. These
clocks do not measure complete task latency.

A controlled 10,000-row unchanged-prefix comparison (11 alternating sample
pairs, ten calls per sample) measured median CPU 14.461 ms before and 2.693 ms
after, with transient traced allocations 2,028 and 148 bytes. Raw-field
comparison retains its original loop and stayed near baseline. Small, late-edit,
length-change and early-truncation cases use the same full-output parity gate;
this is a component measurement, not an end-to-end latency guarantee.

**Prefix-mutation attribution — Problem:** the Anthropic translation merges a
round's tool results into one user message whose canonical entry is keyed by
its first result, so a change in any later result was named after the first.
The prefix-mutation cause always claimed a whole-body re-bill even when most
of the prompt was still read from cache, and the inside-prior-prefix position
compared a wire-message index with the API-form message count, which also
counts the hoisted system message and every merged tool result.
**Decision:** a turn merging several results keeps one `(result key, hash)`
pair per result (`blocks`), and `diff_canonical` reports each changed result
under its own key. Fields, alignment keys, marker slots and first-changed
indices stay per wire message; a lone result stays identical to the OpenAI
`tool` message, and snapshots without `blocks` keep the message-level report.
The billing clause states the round's `cache_read`/`cache_write`: the whole
prompt only when nothing was read, and no cache write when the provider
reports none. Reason codes, evidence labels and buckets are unchanged; the
browser phrase table keeps the fixed sentence of persisted rounds. The prior
prefix boundary is the previous round's wire message count minus
`EDITABLE_TAIL_COUNT`, the same space as the canonical or raw-byte index, so
editing the previous round's final wire message without a read drop is an
editable-tail re-bill, not a prefix-mutation break.
**Alternatives considered:** one canonical entry per result would also align
separate and merged results across envelopes, but moves positions out of
wire-message space, where the raw-byte index, marker slots and the editable
tail are defined. Per-result field names would break the `key.field` format.
Mapping the API-form boundary into wire space needs a per-round index map the
detector does not keep. Raw-byte `<bytes>` culprits still name a merged turn
by its first result: they hash top-level message fields only.
