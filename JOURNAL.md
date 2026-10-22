### 2026-10-20 (翻译慢：一次调用的总预算低于它自己授权的单次超时，超时又整段丢弃)

- **问题（用户报告）：** 会话末尾的自动翻译“迟迟不完成”，看起来像翻译功能很差；翻译本身只是
  一次 LLM 调用，不该这么慢。
- **根因（三层）：**
  1. `lib/translate/runtime/_worker.py` 给整篇翻译一个**固定 90s** 总预算，而同一调用按长度
     授权的单次 transport 超时最高 180s（`_translation_chunk_attempt_limits`）。7244 字符那次
     provider 首字 23s、到 90s 仍在流式输出，于是惟一的尝试被中止；引擎把被中止的流当失败，
     已产出的译文一并丢弃，最后报 `Empty translation result ... elapsed=90s`。
  2. `ResponseReadTimeoutError` 混入了 `RetryableAPIError`，却被 `RequestScopedError` 分支当作
     **确定性**拒绝，一次 14s 读超时就结束整篇翻译（且不走隔离重试）。
  3. 段落补翻（enrichment）只有 **15s** 共享预算和 **1 次** 429 尝试：36 段的会话在第一个批次
     后就被判超时，其余段落永久留在英文；增量预览更是“第一次失败即整轮关闭”。
  4. 另有一处渲染缺口：worker 只推 `partial`，但转录区按 `partialByRound[blockId]` 渲染，
     于是译文在流式传输的全过程中对读者不可见——这正是“一直在翻译、什么都看不到”的观感来源。
- **修法：** 总预算改为按长度推导（90s 地板，6000+ 字符按 1.5×180s，引擎 600s 上限）；
  `RetryableAPIError` 走有界瞬时重试（换槽/退避，最多 6 次）而非终止；段落补翻预算按字数缩放
  （15s 地板、每 80 字 +1s、180s 上限）并用配置的 429 预算；增量预览改为连续失败 3 次才停；
  worker 同时按 `text:terminal` 块 id 推流式译文，poll 返回 `partialByRound`。
- **被否方案：** 重新按段落并发切分——实测更慢（2 路 78s、4 路 96s，单次调用 28s）：并发请求
  抢同一档廉价池，反而各自撞 429/慢槽（单块 dispatch 77s、84s）。固定把预算调到很大的常数——
  短文本也会被慢槽占住 worker。超时后提交半段译文——会把半篇答案标记成 `_translateDone`。
- **验证：** `tests/translate` 272 passed，新增 `test_translate_call_budget.py`（预算覆盖自身
  transport 超时、读超时重试）、`test_segment_enrichment_budget.py`（按字数缩放、429 预算）、
  `test_incremental_preview_retry.py`（连续失败才停）。`tests/api -k translate` 6 passed；
  `scripts/check_architecture.py` OK。实测（旧进程，健康槽）：7244 字符 27.6s、7274 字符 28.4s，
  其中 26s 是 provider 首字延迟——剩余耗时在上游排队，不在本地处理。
- **生效条件：** 服务端需重启（当前进程 16:18 启动，仍跑旧代码；上述实测因此是旧行为基线）。

### 2026-10-20 (浏览器自动化：工作标签只能是自己开的标签，且不再抢焦点)

- **问题（用户报告）：** 浏览器控制一直在用户正在用的标签里操作；即使不崩，也应该自己开新标签，
  而且不该抢焦点。
- **根因：** `resolve_work_tab` 的种子来自浏览器**当前活跃标签**。`isClient` 只挡了 Tofu 页面本身
  —— 用户正在读的页面、填了一半的表单仍会被隐式接管：第一次不带 `tab_id` 的调用直接
  `tabs.update` 掉用户页面。`cmdNavigate` 保护分支还硬编码 `active: true`，把窗口从聊天里拽走。
- **修法：** 用**归属**而不是**活跃**选目标。扩展在唯一确知的时刻（自己 `tabs.create`）记账
  （`_automationTabIds` + `chrome.storage.session`，MV3 worker 重启可恢复；tab 关闭即退役 id），
  `list_tabs` 返回 `automation`；服务端只从这些标签里 seed/记忆。没有可续用的自动化标签时
  `browser_navigate` 自己开一个 `active:false` 的后台标签。用户标签只能通过显式 `tab_id` 触达。
  `list_tabs` 输出加 `[automation tab]` 标记，工具描述同步改写（"去 list_tabs 再重试"的提示原本会
  把调用者引回用户标签，一并改掉）。`_resolve.py` 的模块头与 `docs/modules/browser_automation.md`
  的 Problem/Decision/Alternatives 同步更新。
- **被否方案：** 只靠 `isClient` 保护聊天页（用户的其它标签仍会被接管）；每次调用都开新标签
  （标签栏爆炸，多步流程没法停在同一页）；按 URL 标记自动化标签（空白/新开页无法区分）。
- **验证：** `tests/browser` 621 passed（2 项环境性失败已 deselect：`tofu-search` 依赖摘要
  固定版本、`Wasmtime` 运行时缺失，均与本改动无关）；`tests/release` 169 passed（含 manifest/
  store 版本一致性与权限推导）。新增规格：automation-only seed、无可续用标签时后台开标签、
  显式 `tab_id` 仍可触达用户标签、MV3 重启恢复归属、关闭退役归属。扩展版本 5.7.1 → 5.8.0
  （自更新看门狗靠版本差触发重载，否则新代码不会替换到已安装的扩展）。
- **生效条件：** 扩展侧需重新加载（或用 5.8.0 包覆盖后由看门狗重载）；服务端需重启。

### 2026-10-20 (AGENTS.md 新增「Tool and harness quality」：工具缺陷属于交付范围)

- **问题：** 模型执行任务时只关心任务本身；工具/脚本/MCP 的机制缺陷（参数校验缺失、报错含糊、
  描述误导、静默降级）只要「绕过一次」，下一次还会再踩，且从不进入交付报告。
- **决策：** 在 `AGENTS.md` 增加一节，把 harness（模型可见工具、`debug/`、`scripts/`、MCP、
  tool schema/描述、本指南）定义为产品的一部分：遇到缺陷必须记录、复现、区分「误用/已知限制」
  与真缺陷；**污染任务结果的缺陷，或本地可测且不改公开契约的缺陷，必须在本任务内修完**；
  其余以「发现 + 最小修法 + 未验证项」报告。并要求修机制而非修实例、禁止把 workaround 写成契约
  （调用方 fallback / 断言 bug 行为的测试 / 把缺陷写成 intended API）。同一缺陷第二次命中不再是
  report-only：落项目记忆并修复或升级。
- **刻意不做（被否方案）：** 不引入「Harness Squared」式的按任务增量迭代 harness 的自动回写
  闭环——需要逐任务 harness 变异与回归归因，违背「保持通用 harness」与 `AGENTS.md` 的简洁约束；
  也不新增 checklist 文档或生成物，规则只有这一处权威。
- **验证：** 仅文档变更（`docs/README.md` 声明文档无 catalog/长度/链接/散文门禁）；
  `scripts/check_architecture.py` 不校验 `AGENTS.md` 正文；新增段落无 >80 列行。

### 2026-10-20 (重试风暴折叠行：告警图标与标签文字基线错位 4px 根修)

- **起因（用户截图）：** 折叠的「N 次失败 / 重试的请求」行里，左侧告警图标比文字低约 4px
  （用户原话「logo 和文字都没对齐」）。同一处 `.activity-event__marker` 在展开后的每条成员行
  也一样偏。
- **根因：** `23-reading-activity.css` 给 16px 方形 marker 写死 `margin-block-start: var(--space-1)`
  ——4px 的固定偏移只在「行高恰好 24px」时成立；实际行是 `--conversation-font-size-meta`(12px)
  × 1.45 = 17.4px 的行盒，方形盒居中后必然低 4px。group 摘要（`activity-event-group__marker`）
  与成员行（`activity-event__marker`）是同一处写死值，所以两处一起错。
- **修法：** 新增 token `--conversation-activity-line-height`（= meta 字号 × 1.45），两处 marker
  改成 `margin-block-start: calc((line-height - 16px) / 2)`，让字形盒在行盒里居中；group 标签与
  activity 摘要的 `line-height` 也换成该 token，字号/行高一变不会再次错位。
- **规格：** `tests/visual/test_visual_retry_storm_group.py` 加 `ALIGNMENT_PROBE`——真实 Chromium
  里比对 marker 盒中心与标签首行行盒中心，折叠摘要与 12 条展开成员都必须 ≤1.5px；反向验证：
  注入旧 `margin-block-start: var(--space-1)` 时偏差 4px，断言失败。
- **验证：** 新视觉规格 2 passed（en-light-1440 / zh-dark-390）；`activity-event-group.test.ts` 3
  passed；`test_visual_compaction_trigger_wording.py` 4 passed；`check:styles` 净
  （static/styles.css 1364891B / 1500000B）；compose `--check` 绿；`build:frontend` +
  `verify:frontend` 绿，新 bundle main-D-WaRSqv.js。
- **兄弟在飞（与本改动无关）：** `test_frontend_conversation_surface_vite.py` 5 红
  （`copyButtons: [2,1]`——classic-conversation-renderers.ts 的未提交改动），本地未触碰。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-21 (execution-modes unification: remaining migration items completed)

- **Scope:** finished the open items of the in-flight execution-modes
  unification (docs/proposals/EXECUTION_MODES_UNIFICATION_PLAN.md, 2026-09-20) that were not
  covered by the working tree or were explicitly rejected/deferred by the
  decision table in `docs/TASK_EXECUTION_MODES.md` (single-field mode
  contract, one-boundary normalization, presence registry, artifact-store
  replacement and nested-Turn storage remain rejected as recorded there).
- **Legacy Autopilot file retirement (Phase 0-1 completed):** deleted
  `lib/tasks_pkg/autopilot.py` (1367 LOC) plus `autopilot_baton.py`,
  `autopilot_event_forwarding.py`, `autopilot_markers.py`,
  `stdin_autopilot.py` (~2,700 LOC total) and the dead queue API surface
  (`message_queue.arm_autopilot_marker` / `has_autopilot_marker` /
  `_get_autopilot_marker`; `clear_autopilot_marker` stays — Goal controls
  clear historical markers). Kept the `autopilot_run_lifecycle` /
  `autopilot_state` alias modules (Goal close-out / run-pin owners with a
  live consumer in compaction `_anchor`). Admission stays guarded by
  `check_autopilot_kick` + `test_legacy_autopilot_retirement`.
  15 interpreter-bound test files deleted; guard suites re-pointed to the
  surviving owners (`agent_verdict` VU persona, `autopilot_state`,
  `autopilot_run_lifecycle`); ordering ratchets re-keyed to the post-retirement
  finalize contract (persist(defer) → done → commit capture → release) in
  `test_finalize_persist_before_autopilot` / `test_autopilot_three_step_split`.
- **Task Mode frontend cleanup:** removed the 24 test suites pinned to the
  deleted `task-mode-*` sources; dropped the dangling `taskModeShellMarkup`
  registry import from `manifest.json` (regenerated the presenters bundle);
  pruned 92 dead `tm.*` i18n keys from both locales (110 live keys kept —
  trace/inspector projections still consume them) and regenerated the i18n
  contract; `docs/FRONTEND_ARCHITECTURE.md` now describes the Studio-only
  graph and the chat-projection run surface.
- **Gate repairs:** `test_phase_registry` carves out the database-panel
  benchmark job-state `prepare` label; `.ignore` now lists
  `slides-editor-presenters.generated.js` (runtime-composition freshness
  gate); `vh_guard` sweep lists updated for the retired `.orch-shell` /
  `.orch-tpl-menu` selectors; `autopilot_state.py` unused aliases removed.
- **tofudb lane:** the release engine binary was stale relative to
  `packages/tofu-db/src/orchestration.rs` (missing the uncommitted
  `durable_human_wait` addition), which retired human-wait runs as
  `worker_lost` on restart; rebuilt the binary —
  `test_durable_human_wait_restarts_and_resumes_once[tofudb]` green again.
  Lesson: after touching `packages/tofu-db/src/`, rebuild
  `packages/tofu-db/target/release/tofu-db` before trusting Python lanes.
- **Verification:** 58 new-contract tests, 249 autopilot/retirement battery,
  287 + 423 modified-suite battery, 167 post-integration re-run, guard suites
  (`test_swarm_agent_loop_chassis`, `test_goal_worker_context`,
  `test_vu_prompt_single_source`) all green; `check:frontend` full chain
  green (i18n closed world 5,899 keys, 162 sections / 14 lazy bundles, build
  + canary + publish); ruff clean on touched files.
- **Pre-existing, not from this work:** cargo
  `engine::tests::adoption_isolation::full_catalog_adoption_reclaims_bounded_files_and_recovers`
  fails identically at its introducing WIP commit 9cd6dd048 (orphan scan
  admission bound vs full catalog + 2 unpublished segments) — owned by the
  pack-adoption/WAL-retirement stream, reported not fixed. 7 ruff findings in
  HEAD-clean files unrelated to this migration.



- **Scope:** every isolated worktree/branch audited against the tip; uncommitted
  work rescued first (merge-gate tofu-db stack, git workspace guard, edit-failure
  compact UI, project-brain narrative, cert rustfmt fix), orphan commits branched
  (`rescue/model-onboarding`, `rescue/model-registration-fix`, `rescue/export-scrub`,
  `rescue/harness-export-redaction`, `rescue/db-cert-snapshot`), then integrated:
  conversational artifact editing (slides patch/theme/versions, motion-video scene
  edit, deck editor overlay), swarm delegation contract (adjudicator, MCP delegate
  exposure), evolution exploration loop (observations, friction, reindex, frontier),
  tofu-db roadshow (rotation parking, convoy fix, pacing cap, soak harness),
  pain-gate redaction/watchdog fixes, terminal observer queue, model registration
  lifecycle hardening, eval harness widenings (verifier timeout multiplier, QMP/
  preflight/git-probe timeouts, TCG prompt guidance).
- **Method:** real merges for close branches; cherry-pick for snapshot-carrying
  branches (their "preserve baseline" commits drag stale trees); `git checkout
  --ours` only after proving the pick's tests/markers already exist in the tip
  (the pre-integration squash and a concurrent writer had already integrated
  large parts of the tofu-db, robustness and eval streams).
- **Notable catch:** the rotation-finish drain-before-destructure atomicity fix
  was silently lost by merge auto-resolution (destructure ran before the drain,
  dropping the covered-prefix restore); the branch's own
  `rotation_finish_defers_whole_snapshot` test caught it — restored the order
  and adapted rotation parking to the `writer_engine` API.
- **API v4 adaptation:** merged pre-cutover code re-pointed to `/api/v4`
  (tool result URLs, workUnit href templates, slides/motion routes and the
  `_wuUnitTaskId` links regex); the test ladder's cached `get_json()` keeps
  v1-shaped bodies while the wire carries `{data, meta}`.
- **Verification:** tofu-db 1026 lib tests green; 40+ focused Python suites green
  (evolution, slides/motion, delegation, git-guard, storage v2, undo/write tools,
  model registration, eval framework); i18n contract + tool inventory regenerated;
  frontend bundle rebuilt.
- **Pre-existing failures (not from this integration):** 4×
  `test_empty_v2_profile_never_invokes_name_heuristics` (candidate metadata with
  empty offering), 8× public/opencode agent-runtime tests (registration-gating
  fixture drift) — confirmed failing on `backup/pre-integration-20260919`.

### 2026-09-11 (debug panel root fix + redesign)

- Root cause of the dead panel: the conversation-sync migration removed the
  per-frame transport that fed the debug drawer; `lib/turn_lifecycle.py`
  `_DEBUG_EVIDENCE_EVENTS` now bridges live `messages_snapshot` /
  `tool_compacted` frames onto durable attempt events, and
  `core/debug_panel.js` ingests `convDebugFrame`. Verified by
  `tests/diagnostics/test_request_inspector.py` (32/32) and the full inspector/debug
  battery (74 passed).
- The orchestration/flow refactor silently dropped seams that only the jsdom
  raw-section fixtures and static wiring pins cover: `openStateInspector`
  and the drawer state-mirror row list were gone from
  `core/request_inspector.js`, its i18n keys (`ri.states` / `ri.stateNote` /
  `ri.stateRowTip`) were missing, and `tests/_runtime_sections.py` composed
  the `core/debug_panel.js` fixture without the prelude's
  `retainedCompositionLifecycle` and without the legacy `_debugRequests`
  binding (now aliased to `_debugRuntimeOwner.shellState.requests`).
  Six suites failed at harness eval with
  `ReferenceError: retainedCompositionLifecycle is not defined`; restoring
  the composition layer plus the dropped entries returns them to green.
  The same class of gap surfaced once more in
  `test_frontend_debug_toggle_conv_list.py`: the two-tier search moved to
  the typed owner `frontend/src/sidebar-search-controller.ts`, so
  `ui/conversation_list.js` raw fixtures now compose it via
  `native_module_path`, and the prelude's `retainedCompositionLifecycle`
  (full `add`/`destroy`/`listen`/`interval`/`timeout` surface from
  `frontend/src/lifecycle.ts`) is stubbed in the SHARED
  `tests/_jsdom_harness.js::_stdStubs` — the canonical home when more than
  one eval'd file needs a prelude seam.
- Neuter anchors in `test_frontend_request_inspector*.py` address source text
  verbatim: when the request fallback moved into
  `frontend/src/features/inspector-evidence.ts` (Vite-bundled, iife shape)
  and `_riRoundScopedMessages` gained the `turn` parameter, three anchors
  drifted. They were re-pointed at the current composed text (the
  `for (const kind of ["state", "request"])` loop shape) so the
  red-flip guards keep protecting the round-scoping and mirror-less-round
  contracts.
- Layout: hardcoded greys in the legacy debug panel replaced by theme
  tokens, header/body padding tightened, drawer debug header made sticky,
  and the `.ri-states-head` / `.ri-state-row` styles restored.
  Verification: `tests/frontend/test_debug_panel_contrast.py` 13/13 and
  `tests/_ri_panel_visual_check.py` PASS (every painted token ≥4.5:1 in
  dark/light/tofu), plus real-browser screenshots of the full drawer in all
  three themes.

### 2026-09-11 (reasoning coverage and route availability)


## 2026-09-11 — Model/request audit corrections

- Verified the three shipped Astra contract failures. `ultra` now belongs to
  Astra's exact model contract; shared GPT-5.6 remains capped at `max`, and
  provider-specific recipe overrides remain supported.
- Connected saved-depth restoration to the server's final route intersection,
  with a bounded projection consumed by desktop/mobile controls. Removed the
  dead `supported_controls` reader and TypeScript effort-ranking copy; shared
  display labels also have one owner. Unknown/no-route preferences stay intact.
- Responses now validates direct native effort fields against selected route
  capabilities, rejects conflicting aliases and unknown reasoning fields, and
  reports omitted optional sampling hints. Legacy GPT-5.6 and older Codex regain
  encrypted replay/cache; selected feature declarations remain authoritative.
  GPT-5.6 `max` itself is valid under the shipped contract: the audit's blanket
  claim to the contrary was not evidence of a provider rejection.
- Configuration validation rejects contradictory effort defaults/disable/alias
  targets, while preserving partial scope overrides, binary switches and
  portable plugin names. Narrowing removes only incompatible inherited fields.
- Coverage: 378 focused/API contract tests and 181 neighboring checks passed;
  the updated Meituan wire-alias expectation passed in the final focused run.
  Frontend gate, lint and architecture checks passed. The owner revision 82
  snapshot validated 108 profiles and 660 compiled choices with zero failures;
  this is offline evidence, not an upstream capability probe. Concurrent edits
  to compiler policy were retained and the affected contracts rechecked.
  All 11 relevant browser cases (desktop/mobile and the running local app),
  22 slider unit tests and the 14 retry lifecycle contracts passed.

- Missing credential grants erased Opus 5's known effort controls. Controls now
  retain declared Model/Offering capability with `availability=no_route` for a
  disabled slider and explicit notice; execution authorization is unchanged.
  Unknown profiles are visible configuration gaps, not silent non-reasoning.
- Completed/corrected exact defaults for 32 installed Models, including GPT-5.5,
  DeepSeek V4, Gemini variants and Claude effort vs extended-thinking models.
  Binary controls use Off/On without invented effort levels. Exact previous
  shipped profiles migrate; owner overrides remain authoritative.
- The read-only audit reports configuration and route gaps independently from
  compilation failures. Validation: 71 inheritance/control/metadata tests,
  107 neighboring contracts, 22 slider tests and the relevant real-app browser
  cases, frontend gate and architecture check pass. Owner CAS 81→82 changed
  only Model profiles; provider configuration and grants remained identical.

### 2026-09-11 (native reasoning levels and editable model baseline)

- Composer controls now use ordered native efforts, not a fixed maximum of
  max or compatibility aliases. Kimi K3 has low/high/max with no off; GPT-6
  Astra's declared catalog ladder includes ultra. Native tokens take priority
  over old aliases of the same name.
- Models settings edits reasoning through the existing draft/CAS owner;
  provider forms save differing Offering fields and can restore inheritance.
- The slider has a separate Default button, dynamic ticks and native labels.
  The API reports a shared default only when eligible routes agree, preserving
  the same eligibility and inference compiler as actual model requests.
- AA repaints preserve the visible reasoning form and focus; detached forms
  are not cached. Provider edits resolve the current Settings draft after CAS.
- Validation: 93 reasoning/metadata/owner tests, 21 slider unit tests, and all
  8 relevant real-app browser cases pass (including Model save, Offering-only
  override/reset and asynchronous AA refresh while editing).

### 2026-09-11 (Responses image tool-result continuity)

- Conversation `mtws88slnx29ws` failed at round 75 after `inspect_image`
  returned image+text. Responses validation rejected tool-role images; the
  retry retained that valid history and failed before reaching the provider.
- Tool image outputs now use native `function_call_output` content arrays,
  preserving pixels, text order and call association. User/tool images share
  the same projection. Strict malformed-content checks and the program
  interpreter's text-budget boundary remain intact.
- Replayed the 23-message redacted failing request through both converter
  profiles; focused tests cover stream modes, final request preparation,
  sealed payloads, input immutability and rejection boundaries.

### 2026-09-11 (live model validation and request ingress parity)

- Approved restart activated metadata inheritance: startup reconciled 163
  legacy Offerings; 171 of 200 now have explicit inheritance/override markers.
  Grok's CatPaw row inherits the configured Model's 500,000 context; this is
  configuration provenance, not a successful 500K capacity probe.
- Live CatPaw requests returned HTTP 400 unsupported-model for Astra and
  Grok; disabled exactly those two Offerings by owner CAS (revision 77→78).
  CatPaw/Sankuai Kimi, Sankuai Grok and OAuth Astra returned generated output.
- Probe success exposed a remaining actual-request bug: direct ingress
  ignored depth and coerced absence to false. All dispatch surfaces now carry
  depth and preserve absent enablement; task/default/UI resolution keeps the
  same distinction. Bounded preparation reasons reach the direct SSE client.

### 2026-09-11 (model metadata authority and legacy provenance)

- Removed provider-import, subscription and cutover-recovery union/max writes
  into existing public Models; a single catalog merge now preserves owner data.
- Discovery, local and desktop registration share declared context handling.
  Subscription catalogs retain context evidence instead of injecting 128K.
  Provider templates no longer apply a second model-name context ceiling.
- Managed refresh retains confirmed identities, overrides and wire exclusions.
  Startup migrates legacy metadata using explicit markers, catalog evidence,
  cutover receipts and generated-value fingerprints; unmatched limits remain
  scoped. Fingerprints cannot reconstruct edits identical to old defaults.
- Browser verification caught a pointer regression: custom dragging updated
  fill but skipped the actual range, then repaint erased the candidate before
  commit. Range/fill now move together and release captures the candidate first.
- Added all-template reasoning/name-invariance coverage and an offline owner
  export audit. These verify declared semantics, not live provider capacity.

### 2026-09-11 (write failure receipts and complete compaction timing)

- Conversation `mtwe3v3ctnm2cg` safely salvaged a rejected system-root write,
  but its handler settled as done and its card hid the returned error behind
  an unapplied content diff. Failed write metadata now settles an error through
  the canonical receipt and activity event; the card exposes the actual reason
  and recovery instructions. A later successful recovery remains a separate call.
- The displayed 251 ms timed archive publication after a roughly 239-second
  compaction. New receipts preserve the preceding phase start and the summary
  duration lower bound. Historical display may use one enclosing completed
  span from the same Turn without rewriting durable state.
- Kimi's affected rounds contained the literal single character `空`; earlier
  rounds and direct synthetic CatPaw Messages probes returned normal thinking.
  This is not evidence of a renderer dropping text. Existing effort projection
  passes sealed stream/nonstream wire tests; the original gateway/model cause
  is not proven. No text suppression or speculative compaction worker was added.

### 2026-09-11 (context provenance and durable provider metadata inheritance)

- CatPaw's imported Grok row retained a 32,768-token discovery fallback after
  identity repair. The old listing parser discarded provider context fields;
  neither the stored fallback nor a small chat probe measured capacity.
- Offerings can now declare inherited capability/context fields. Repository
  normalization materializes the current Model values for display and routing
  together; explicit provider overrides and legacy unmarked fields remain.
- Discovery retains provider-declared context limits and distinguishes absent
  metadata from fallback values. Template compilation and model additions
  preserve inheritance intent instead of freezing copied defaults.
- The provider price fallback is labeled 参考价/Reference, with source and
  per-million-token units in its tooltip; inherited context identifies its
  source and lack of capacity-probe verification.

### 2026-09-11 (public Model inheritance and continuous depth gestures)

- Identical composer refreshes preserve pointer capture; an unavailable saved
  depth is a display value, not a selectable extra rung that can hide itself.
- Developer catalog defaults now seed shared Model inference profiles and
  repair missing fields in installed authorities without replacing owner
  edits. Provider overrides and real Deployment request names remain scoped.
- Exact catalog identities bind both newly discovered and saved pending
  offerings, retaining IDs, probe verdicts and credential exclusions. Grants
  preserve previously reachable wires; ambiguous identities remain pending.
- The Sankuai template drops its cross-model enable_thinking default.
  CatPaw's discovered Grok identity inherits public metadata, while its actual
  Messages probe returns unsupported-model; catalog identity grants no probe
  success. Bound v2 routes never fall back to
  model-name reasoning inference when declared information is absent.
- Tests cover shared Kimi/Grok presets, opaque request names, provider
  overrides, owner baseline preservation, idempotent saved-state repair,
  authorization, mouse/touch gestures and full-app composer persistence.

### 2026-09-11 (actual route ancestry and pre-dispatch compaction status)

- The compiler's one-millisecond rank offsets were overruled by randomized
  slot scores; request groups now retain explicit order after admission while
  preserving eligible conversation affinity and authorized fallback.
- Candidate creation no longer claims a failover occurred. One bounded trace
  per dispatch records actual provider/deployment/model transitions across
  sync streaming, async streaming and non-streaming calls; reusable slot
  snapshots remain immutable and subsequent rounds start fresh.
- L2 announces its compaction phase before summary model I/O, after declining
  preflights. Previously a 202-second summary left the composer claiming it
  was generating a reply and announced compaction only after completion.
- Regressions cover priority inversion, first-attempt backup selection, real
  provider/model fallback, trace bounds, slot release and phase-before-I/O.

### 2026-09-11 (subscription route admission and reasoning intent)

- Local DNS failure removed subscription routes before proxy/desktop egress
  could run; exact approved HTTPS subscription origins now retain that path,
  while resolved dangerous addresses and unknown BYO hosts remain denied.
- Saved thinking depth no longer becomes strict off through a synthesized
  legacy switch. Declared Responses effort ladders acquire their protocol wire
  binding, including already stored catalogs; explicit route overrides remain.
- Browser toolbar submissions omit their capability-derived display switch:
  unknown reasoning support must not override a saved max/default choice.
  Real-app browser tests cover config resolution and the final turn command.
- Local fallback preparation failures no longer trigger another model, and
  zero-attempt dispatch failure no longer claims the retry ceiling was spent.
- Offline specifications cover config-to-final-Responses-wire behavior, DNS
  boundaries, profile precedence, fallback settlement and slot cleanup in
  `tests/llm/test_subscription_route_preparation.py`.

### 2026-09-10 (distributed runtime state: item-3 externalization complete)

- Gap (ROADMAP item 3): six categories of process-local runtime state were
  unsafe across replicas — task events, aborts, scheduler leases,
  human-input waits, quotas, invalidation.
- Audit (two read-only passes, evidence in
  `data/swarm/fec23a61-*/audit-events-abort.log` and `audit-waits-quotas.log`):
  task events were already durable-before-visible in sidecar `task_events`;
  invalidation caches were already mtime/TTL self-healing; the wallet,
  key_stats stops and the scheduler claim were already sidecar-atomic. The
  real gaps were abort reachability, human gates, API quota buckets, and
  timer watcher duplication.
- Fixes, all behind the `TOFU_DEPLOYMENT_MODE=distributed` seam with
  personal mode byte-equivalent (no new threads/IO in personal mode):
  - Abort: `lib/agent_core/task_abort_signal.py` — store flag
    (`kind='task-abort'`, owner-scoped value) planted on registry miss, one
    shared watcher delivers via the runtime's own `abort_owned`; wired into
    `routes/api_v1/tasks.py` + `routes/_task_routes.py`.
  - Per-command interrupt (`_cmd_interrupt`): same module,
    `kind='cmd-interrupt'` flag; the sweep delivers it as a pid-stamped
    `task['_cmd_interrupt']` only while a live `_subprocess_pid` exists
    (no-active-command flags are dropped, mirroring the local
    `no_active_command` answer); route fallback in
    `routes/chat_poll_abort.py`.
  - Human gates (stdin/guidance/write-approval):
    `lib/tasks_pkg/human_gate_registry.py` mirrors pending waiters
    (`human-gate-req`) and answers (`human-gate-ans`, first-writer-wins via
    `set_value_nx`, owner re-validated on adoption) onto
    `runtime_state_store`; the three waiters slice their `Event.wait` and
    adopt cross-replica answers via `poll_store_resolution`. Local
    first-resolution-wins and owner-as-oracle-blind semantics preserved.
  - API quotas: `lib/rate_limit_store.RedisTokenBucketStore` (HASH per
    bucket, WATCH/MULTI/EXEC, no Lua) behind `get_token_bucket_store()`;
    distributed = redis fail-closed, personal = in-process unchanged;
    `TOFU_RATE_LIMIT_BACKEND` now defaults to `db` in distributed mode.
  - Timer watchers: `lib/scheduler/timer/_lease.py` — per-timer watcher
    lease (`set_value_nx` + owner token, TTL 120s, renewed inside the
    existing poll loop, released with a 0.2s tombstone on exit); the
    scheduler tick re-runs `resume_active_timers()` every 4th beat as the
    crash-adoption sweep. Duplicate condition-check spend across replicas
    eliminated; dispatch was already idempotency-keyed.
- Verification: abort/interrupt 9 + regression 86; human gates 15 +
  neighboring 104; quota 48 + neighboring 64; timer lease 7 + timer 34 +
  scheduler 72 — all green. Known trade-offs recorded with each module:
  lease renew is get+set (no Lua), one beat fail-open on store error;
  redis TPD peeks refill at `now` (sub-token boundary difference vs the
  in-process stale-token check).

### 2026-09-10 (offline migration: paper budget ledger import)

- Gap: the source keeps two single-record tenant-global budget ledgers
  (`paper.build.budget.v1` = retained build reservations,
  `paper.document.budget.v1` = saved-document/association counts) as generic
  `storage_records` rows, but the native authority stores them in a fixed
  16-byte capacity ledger plus one 8-byte reservation record per retained
  build. Generic replay could not reproduce that layout, so both namespaces
  were excluded without a dedicated adapter: the preflight failed closed on
  any source containing them and the importer silently skipped the ledgers.
- Fix: new native adapter `packages/tofu-db/src/paper_budget_migration.rs`
  (`migration.paper.build.budget.restore/get`,
  `migration.paper.document.budget.restore/get`, dispatched from
  `migration_session`). It parses the exact source snapshot, enforces the
  live bounds, cross-validates every build reservation against the restored
  `paper.build.artifact.v1` metadata (orphans fail closed), and rewrites the
  native capacity ledger, owner-bound `r:` reservation claims and a
  `migration` provenance record in one transaction. Insert-or-match keeps
  replays read-only; divergent or foreign-owner occupants fail closed; get
  reconstructs the exact source snapshot for independent verification.
- Trap worth remembering: the budget ledgers are tenant-global, so a plain
  `database.begin(tenant, owner)` rejects them with PermissionDenied
  "entity key is outside the transaction owner scope" — the adapter must
  begin with `begin_with_paper_document_scope`, which admits both
  `generated_paper_build::BUDGET_NAMESPACE` and `DOCUMENT_BUDGET_NAMESPACE`.
  Five of six initial adapter tests failed on exactly this.
- Contract: `contracts/tofudb_ir_v1.json` gained the four operations plus
  snapshot semantics; IR regenerated, `--check` clean. Generic record replay
  now rejects both budget namespaces ("requires its domain migration
  adapter"); the session round-trip test drops `paper.document.budget.v1`
  accordingly.
- Importer: plan bumped to `tofu-db.offline-import.v8` with
  `paper_build_budget`/`paper_document_budget` domains running AFTER the
  records domain (build-budget restore cross-validates artifact metadata).
  Resume re-verifies the prefix and completes through the same
  checkpoint/digest machinery as continuations; completion also proves an
  absent source row means an absent target ledger.
- Tests: Rust `paper_budget_migration` 5/5 (restore exactness,
  divergence/foreign-owner/orphan rejection, bound validation,
  commit-fault atomicity matrix), cargo migration suite 34/34, Python
  `test_tofudb_migration_import.py` + `test_tofudb_migration_boundaries.py`
  73/73 including new budget resume/divergence cases; FakeSession gained
  insert-or-match budget handlers mirroring the native canonical value_json.
  `cargo fmt --check` repo-wide currently disagrees with the installed
  rustfmt on many untouched files (artifact.rs, at_rest.rs, authority.rs),
  so fmt was not used as a gate this run.

### 2026-09-09 (translate commit resilience + writer-contention audit)

- Symptom: auto-translate computed translations but turns stayed untranslated
  (20/128 completed turns since 09-08). Cause: `Storage writer acquisition
  timed out` (249 since 09-08 17:00) failing `commit_translation_to_turn`,
  which then settled the task as a permanent error.
- `lib/translate/commit.py` now wraps both projection-commit CAS loops in
  `_with_storage_retry`: retryable `database_timeout/busy/unavailable`
  StorageErrors get 6 attempts with 0.5→8s exponential backoff, riding out
  writer bursts far past the 2s user-lane acquisition cap.
- `lib/translate/runtime/_worker.py` keeps the task running when a commit
  still fails after inline backoff: the already-translated text (never a
  re-translation) is retried out-of-band via timer-delayed
  `submit_reconstructible_translation` at 30s/120s/300s; only exhaustion
  settles an error. Done/aborted settlement factored into `_finish_done` /
  `_finish_aborted` shared by both paths.
- Writer-pressure findings (no code change): `turn.event.record` itself is
  healthy (group commit batches 3-5, commit phase 0.1-0.5s). The >2s writer
  holds are `raw_archive.put` (≤16 MiB compressed blobs per LLM round) and
  `turn.events.prune` execute phases. The systemic amplifier is the 104 GiB
  authority (storage_events ~7.9M rows, attempt_events ~2.6M,
  conversation_changes ~1.7M, task_events ~2.1M, 0 freelist) on beegfs FUSE
  under cgroup memory pressure (76-79%, LoopWatch 5-6s process stalls) with
  `TOFU_STORAGE_FASTPATH=off`, so page-cache misses push any write op past
  seconds. Remediation options, in order: enable `TOFU_STORAGE_FASTPATH=auto`
  (built for exactly this), reclaim + repack the event streams, reduce
  concurrent swarm/agent load.
- Separate bug spotted in the same logs: recurring user-lane failures
  `project_brain.work.start: Command response is too large for a receipt`.
- Specs: `tests/translate/test_translation_turn_authority.py` (retry, exhaustion,
  non-retryable), `tests/translate/test_translation_worker.py` (out-of-band retry,
  exhaustion, non-retryable).
### 2026-09-09 (offline migration: conversation turns import)

- The `tofu-db.offline-import.v7` importer now streams
  `storage_conversation_turns` after conversation headers. Native
  `turn::migration` (`migration.turn.restore/get/scan`) preserves source turn
  identity: exact projection/settlement snapshots, lane ordinals, counts,
  heads, and identity claims, advancing the conversation header
  (`msg_count`/`rev`/`updated_at`) exactly once and refusing live attempts
  (`current_attempt_id`) and cross-owner/ordinal occupancy. Resume re-verifies
  the hashed target prefix and fails closed on divergence.
- Verified: `cargo test --lib turn::migration` 4/4, `migration_session` 7/7,
  `tests/tofudb/test_tofudb_migration_import.py` 37/37, `test_tofudb_migration_boundaries`
  26/26, IR contract `--check` clean. One migration_session paper-record test
  flaked once under the parallel harness and passes in isolation and on rerun.

### 2026-09-09 (project bar leftover names)

- Multi-root project location no longer collapses to `first +N` while leftover
  pane width is idle. The project cluster packs to its fitted names; leftover
  is the strip gap before the meter, not a `flex:1` trigger that clipped Auto.
  Fitting reads that strip leftover and measures labels at `max-content`, so a
  content-sized bar cannot circularly keep `tofu +1`. Names that fit join with
  a middle dot; `+N` only counts the rest.
  Specs: `tests/project_location.test.cjs` and the conversation-bar Chromium
  contract.

### 2026-09-09 (desktop downloads + Windows/CI/Android release hardening)

- Settings → About now hosts the full Windows/macOS desktop installs. Local
  control keeps only the lightweight controlled-side agent.
- Windows desktop smoke/startup no longer calls `os.getuid` (missing on NT).
  `StaticViteMirror` and the storage fast-path temp namespace use getattr
  fallbacks; POSIX ownership chmod stays Unix-only.
- CI: the frontend job `uv sync`s extras and prepends `.venv/bin` to PATH
  before `npm run check:frontend` (contract gens need PyYAML). The Docker
  python-dependencies stage copies `packages/tofu-search` and
  `plugins/tofu-trading` before `uv sync --frozen`.
- Android 0.1.17 (`versionCode` 18) turns on R8 minify + resource shrink,
  keeping `@JavascriptInterface` methods and the Room profile store.

### 2026-09-09 (TofuSQL P0: read-only SQL front)

- Implemented the TofuSQL P0 slice from `docs/TOFUDB_TOFUSQL_DESIGN_PROPOSAL.md`.
  `packages/tofu-db/src/tofusql.rs` serves a single `SELECT` over one catalog
  table from `contracts/tofusql_catalog_v1.json` (generated by
  `scripts/gen_tofusql_catalog.py`, which fails the build on catalog/IR drift).
  A bundled-SQLite virtual table evaluates each statement; leading key-part
  equality and trailing-restText range predicates push down to
  `EntityKey::prefix_range` scans. Read-only is enforced by authorizer, never
  convention, plus a progress-handler deadline and the catalog statement
  ceilings. The `tofusql.query` storage.v2 operation rides the authenticated
  wire path; `tofu-db sql` is the offline operator CLI over the same engine.
- Two decisions worth recording. The no-SQLite rule in `packages/tofu-db/AGENTS.md`
  gains a single carve-out: only `tofusql.rs` may link the bundled `rusqlite`,
  it never opens a database file, and it persists nothing. And the scan window
  derives its unbounded end through `EntityKey::prefix_range` rather than the
  raw composite bytes — that is what lets a whole-namespace (empty-prefix)
  scan resolve to the namespace's own successor inside owner scope instead of
  erroring on a non-existent composite-key successor, a gap the seeded-prefix
  unit tests never reached.
- Verification: 6 tofusql tests (owner isolation, authorizer read-only,
  restText range pushdown, whole-namespace scan, unknown table, no-join P0),
  full tofu-db lib suite 704 passed / 0 failed, clippy all-targets clean, and
  all three generator `--check` gates green.

### 2026-09-09 (offline conversation-header import)

- The operator importer `tofu-db.offline-import.v6` streams
  `storage_conversations` after tenant users. It preserves id, title,
  settings, and created/updated timestamps through owner-scoped
  `conversation.create`, then independently pages `conversation.list`
  catalog plus `conversation.count` and point `conversation.get`.
  Empty titles become `New Chat`. Turns remain a later domain.
- Resume refuses a divergent header or an extra target conversation.
  A source owner other than the operator session fails closed before any
  write. Remaining tables, rollback, and application selection stay open.

### 2026-09-09 (NSIS wizard blank pages: full-page SS_BITMAP overlay)

- Owner screenshot of the Tofu Agent installer: purple header band with
  logo/name/version painted correctly, inner page empty, Cancel/Install
  (NLF) buttons visible. Same 2026-08-05 class: a 100%×100% SS_BITMAP
  sibling overlaps every live control; HWND_BOTTOM and opaque labels did
  not recover WM_PAINT on real Windows.
- Fix: `lib/desktop_dist/installer_art.py` renders a header band only
  (798×~127, brand text baked in). `desktop/installer.nsi.tmpl` places it
  at `0 0 100% 38u` and stretches via `NSD_SetStretchedImage`. Live
  controls stay at y ≥ 54 du on native COLOR_3DFACE and must not overlap
  the band. The four page filenames stay 1:1; pixels are identical.
- Pins: `tests/desktop_dist/test_installer_art.py` (band size, below-band geometry,
  no 100%×100%), `tests/desktop_dist/test_installer_parity.py` ratchet,
  `tests/desktop_dist/test_installer_nsi_compile.py` still the makensis gate.
  Pixel proof still rides real-machine acceptance (32-bit SIGSYS here).

### 2026-09-09 (Warm incremental TeX compilation)

- Paper builds were slower than Overleaf because every build ran cold on the
  dolphinfs network cache volume: kpathsea probing and font/format cache
  rebuilds paid per-build metadata round trips, and an empty output tree
  forced the full multi-pass plus bibtex cycle on every compile.
- Three changes, all reconstructible cache owned by the paper-build contract.
  Staging moved to a local spool volume (`TOFU_PAPER_BUILD_SPOOL_DIR`,
  default per-user system temp, fallback to the cache root on any resolution
  failure). Each document keeps warm state as a stable latexmk output
  directory under an exclusive lease, so unchanged sources compile in zero
  engine passes and small edits in the minimum; a failed warm run wipes the
  state and retries once cold, and a contended lease degrades to cold. The
  TEXMFVAR/font cache and generated formats are shared per toolchain, while
  document writes stay confined to the per-build scratch (TEXMFOUTPUT).
  Shell escape stays disabled and the PDF publication gate is unchanged.
- Contract limits: `warm_state_bytes` 32 MiB per document,
  `warm_state_total_bytes` 256 MiB with LRU reclamation,
  `toolchain_state_bytes` 256 MiB. An earlier seed/save copy design with a
  `warm_state_files` cap was replaced by the stable output directory, which
  is what makes latexmk's dependency database actually reusable.
- Benchmark on dolphinfs (12-section article, 8-entry bibliography): cold
  12.0s/3 passes; cold via local spool 5.1s/3 passes; warm unchanged
  3.2s/0 passes. Interleaved warm-edit vs cold repeats (2 vs 3 passes):
  9.6/12.7s, 15.3/21.0s, 12.0/16.0s — warm edits are consistently ~25-30%
  faster, with high dolphinfs variance dominating the absolute numbers.
- Environment repair: the project .venv lacked the declared `pypdf>=6.17,<7`
  (16 pre-existing `test_paper_build_pdf_validation` failures, worker exit 4
  with `pdf_validation_unavailable`); installed 6.18.0. A dead conda env path
  still in PATH masked this during diagnosis.
- Tests: new `tests/paper/test_paper_build_warm.py` (6 cases: pass elimination,
  corrupt-state cold retry, oversized-state wipe, lease contention, spool
  staging/orphan reclaim, spool root fallback). All 193 paper-build tests
  pass, including the previously red PDF validation suite.

### 2026-09-09 (Tofu-DB offline domain importer)

- SQLite → Tofu-DB domain import is now an explicit offline owner in
  `lib/storage_sidecar/tofudb_migration_import.py`. It is never a startup or
  wire path. The operator binds a stopped SQLite snapshot to an exclusive
  `tofu-db migration-session` child through private inherited pipes.
- Credential, billing, and record current-state replay stream declared primary
  keys, reconstruct billing wallets in chronological ledger order, and publish
  checksummed progress only after independent target enumeration matches the
  hashed source prefix. Resume refuses a replaced source, plan, or target UUID.
- BYO `provider.*` current-state replay is in the same offline importer. It
  restores owner-scoped documents through create/update/touch, verifies each
  snapshot via `provider.get` (ciphertext stays opaque), and independently
  enumerates `provider.list` which must omit ciphertext. The mapping plan is
  `tofu-db.offline-import.v4`.
- `python -m lib.storage_sidecar.tofudb_migration_import` is the operator CLI.
  Offline `migration-session` child tests for credentials, providers, billing,
  and records pass against a rebuilt `tofu-db` binary. Remaining domains including
  `tenant.user.*`, rollback, and application selection stay prohibited: native
  `tenant.user.create` still allocates a new `owner_user_id` and cannot preserve
  SQLite identity.

### 2026-09-09 (identity-preserving tenant.user restore)

- Offline `migration.tenant.user.restore` claims the source `owner_user_id`
  and advances the owner sequence high-water in the same authority
  transaction as profile, state, email, and created/status indexes. Insert-or-match
  keeps an identical snapshot; a divergent password, email, or stolen owner
  fails closed. Live `tenant.user.create` still allocates the next identity.
- The operator importer `tofu-db.offline-import.v5` streams `tenant_users` first
  and independently enumerates `migration.tenant.user.scan` / `get` before
  publishing a checksummed prefix. Public `tenant.user.get` still omits the
  password hash. Rollback and remaining source tables stay separate.

### 2026-09-09 (schedule_create vs timer_create schema split)

### 2026-09-09 (Tofu-DB system.reclaim single physical walk)

- `system.reclaim` now plans and executes in one exclusive-session walk.
  Below `min_free_pages` it reports the planned freelist without a second
  reachability pass or deletion. CONTROL still republishes before the first
  referenced-file delete, live MVCC snapshots still defer, and operator batch
  rounds still reuse marks.
- Native tests compare the RPC trace to `collect_garbage` / `plan_garbage_collection`
  and keep the skip threshold (`collect_if_at_least`). Write-tail vs SQLite,
  remaining importer domains, rollback, and application selection stay open.

- Kept the two scheduler create tools separate. The model confusion was the
  shared "durable reminder/timer" wording, not a missing combined API.
- `schedule_create` first sentence is now a calendar/clock job; `timer_create`
  is a readiness watcher. Each description names the sibling as the wrong tool.
- Search hints moved countdown/计时器/提醒 onto `schedule_create`. `timer_create`
  ranks for wait-until-ready / poll / watcher / 条件满足, not countdowns.
- Token ceilings remain 600 / 450 / 1000 combined. Inventory confusability is
  first-sentence Jaccard; these two stay below the 0.5 flag.

### 2026-09-09 (Tofu-DB bounded read-cache metadata and segment membership)

- The previous 10,000-append lifecycle missed its 600-second/1-GiB reclamation
  budget. A production-allocator in-process probe measured about 0.3 seconds
  for open and 3.9 seconds for each subsequent GC plan. User-space CPU sampling
  attributed 60.86% of samples to memcmp, mostly from probing absent block IDs
  in overlapping payload-segment indexes. Kernel-inclusive profiling could not
  resolve symbols in this environment; the retained profile attaches after open
  with user-space-only sampling and uses the same allocator as the daemon.
- Each verified immutable segment index now owns a small reconstructible
  negative membership filter. Positive probes retain exact-ID lookup and every
  payload read still checks CRC32C and its content hash. Filters and actual
  index allocations share the existing 64-MiB cache budget; one filter is at
  most 128 KiB. No persisted framing or authority publication changes.
- Both segment-index and verified block-payload caches appended an unbounded
  recency record on every hit. The segment regression retained 16,385 records
  for one segment after 16,384 hits. An unchanged extraction of the block-cache
  implementation retained 100,001 records for one payload after 100,000 hits.
  Both now compact stale records while preserving LRU order, before allocation
  growth reaches their explicit metadata limits. Segment cache bounds are
  4,096 entries/8,192 recency slots; block cache bounds are 65,536 entries and
  131,072 recency slots alongside its existing launch-derived byte capacity.
- Shared payload vectors are charged by allocated capacity. Disabling the
  payload cache releases payload and metadata allocations and refuses even
  zero-byte population. Contract-generated bounds and tests cover exact reads,
  saturated filters, bounded false positives, hot-entry eviction order, removal,
  maximum-size index/filter admission and shared-handle release. Final release
  `1031fe88a6c435a4bc6d5335a5a78c34180c1b70cc6a6fd8abe7456aa5cf0415`
  passed 647 Rust tests, strict Clippy, 146 SQLite differential cases, five
  pre-authority contracts and real PostgreSQL paginated-search parity. One
  unchanged full-budget mark-spill test remains default-ignored in this run.
- Same-fixture A-B-B-A-A-B GC plans retained identical candidate metrics.
  Median native open/plan time fell 5,185.306 -> 2,652.990 ms (48.84%);
  median peak child RSS fell 505,996 -> 133,408 KiB (73.63%). Baseline is
  `c360a7a0...`, and all 96 final build inputs remained sealed throughout.
- The final 10,000-append lifecycle preserved every full response, authority
  UUID and active-WAL byte across 316 GC rounds and reopen. It still failed
  the experimental 600-second/1-GiB reclamation gate: 600.647 seconds left
  1,470,847,721 file bytes, without a full no-reclamation catalog pass. The
  whole-probe 512-MiB RSS check passed but includes old reference binaries;
  its peak of 524,224 KiB came from the first baseline plan, while candidate
  plan samples were 133,248-133,648 KiB. Owned synthetic fixtures were removed.
- Alternating wire means were creation 0.586 -> 0.556 ms and append 1.256 ->
  1.314 ms; append P99 increased 5.243 -> 5.569 ms. The full medium client-stack
  sample won 8/8 means against PostgreSQL and 6/8 against SQLite, with creation
  and append still losing to SQLite and both write P99s losing to both
  references. These results do not establish a foreground-wide improvement.
  The isolated fully durable PostgreSQL authority was verified stopped and
  removed after comparison. Ten shared documentation line budgets still fail.
- Reclamation must stop repeating full unchanged reachability walks. In
  particular, `execute_system_reclaim` currently plans and collects separately,
  rebuilding marks twice for one request. Follow-up must preserve mutation
  invalidation, current metadata roots, snapshot deferral and fault behavior.
  Detailed evidence: `/tmp/tofudb-read-cache-results.md`. Production remains
  pre-authority; migration, cutover/rollback and launch certification are open.

### 2026-09-09 (Tofu-DB active-WAL history retirement and bounded GC planning)

- Automatic history rotation precedes the next foreground append, so requiring
  an empty active WAL prevented ordinary background history retirement. The
  scheduler now trims an over-budget checkpointed suffix while preserving the
  active log, staged history prefix, checkpoint anchor and current authority
  root. It first publishes the already-durable tail to CONTROL, then publishes
  the smaller history manifest; it never forces a foreground checkpoint.
  Legacy history-only engines still require their full durable history.
- Deterministic tests reproduce the old WouldBlock refusal and cover reopen,
  continued writes, staged history and every I/O error, short write and lost
  sync during retirement. A real 10,000-Turn fixture retired 192 history
  segments, retained 64 and reduced GC live references from 507,223 to 154,263,
  preserving every full response and the exact active WAL bytes.
- Empty loose-block directories formerly triggered a full mark lookup each:
  adding empty hash directories increased identical-data mark reads from 1 to
  256. GC now skips those lookups after directory validation. Packed membership
  uses the existing bounded negative filter plus exact validated segment indexes,
  replacing a catalog-wide retained set of every packed ID. Forced filter-hit
  tests preserve exact membership and the no-I/O negative path.
- The full Rust gate passes 640 tests with one default-ignored full-budget
  mark-spill test. A preexisting CONTROL-publication fault fixture depended on
  random hash-directory creation shifting an absolute I/O ordinal. That fixture
  now prepares all hash directories and asserts injection reaches SyncAll;
  restart-required, no-further-I/O and reopen behavior remain covered. Sixteen
  repeated executions passed before the successful full gate.
- The first retirement-only lifecycle probe preserved data but failed its
  600-second/1-GiB reclamation check: 121 rounds left 1,800,900,578 file bytes.
  The later bounded-planning repair is measured separately; this failed result
  is retained as evidence, not reclassified as certification. The package stays
  pre-authority while lifecycle, write-tail and migration/release gates remain.

- Final native release
  `c360a7a01fadab0698ce5eb4ccdbf189ab551793c97ce5f8ec1ced7572993c4d`
  passed strict Clippy, all 146 SQLite differential cases, five pre-authority
  contracts and real PostgreSQL paginated-search parity. The separately invoked
  full-budget mark-spill test also passed: 6.62 seconds, 17,432 KiB peak child
  RSS, exact spill-limit rejection and zero leftover fixture files.
- Same-fixture A-B-B-A-A-B GC plans produced identical candidate metrics;
  median time fell from 5,084.778 to 4,895.503 ms, with unchanged median RSS.
  Final lifecycle reclamation still failed: 149 rounds in 601.48 seconds left
  1,724,730,175 bytes, above the experimental 1-GiB post-GC budget. All 10,000
  responses and WAL bytes remained identical, peak child RSS was 351,976 KiB,
  and the synthetic fixture was removed. No complete no-reclamation catalog
  pass was reached; the round quota was not the limiting budget.
- Alternating wire runs showed creation means 0.533 -> 0.539 ms and append
  means 1.214 -> 1.246 ms; append P99 was 5.395 -> 5.194 ms. These samples do
  not establish a foreground mean improvement. The final medium client-stack
  harness recorded lower means in 8/8 workloads against PostgreSQL and 7/8
  against SQLite, but creation's SQLite lead was below 1%, and append still
  lost in mean and P99. Both creation and append P99 lost to both references.
  The private PostgreSQL authority used fsync, synchronous commits, full-page
  writes and checksums; it was stopped and its synthetic files removed.
- Contract prose now describes the bounded catalog window and cross-shard loose
  compaction batch precisely. Generation checks passed without changing generated
  Rust; all compiled Rust inputs remained sealed. Ten shared documentation line
  budgets remain failing. Detailed results and both failed lifecycle probes are
  recorded in `/tmp/tofudb-active-wal-gc-results.md`; no production authority
  switch or complete-launch claim is made.

### 2026-09-09 (Tofu-DB 4 KiB packing, search page exhaustion, and mixed-segment GC)

- New entity pages target 4 KiB under the unchanged 16 KiB format ceiling.
  Minimum internal fanout and tail balancing repair maximum-size-key splits;
  adjacent modified leaves repack before staging, preserving tombstones, OCC
  witnesses and untouched references. The original 5,000-row tool-result prune
  now fits the existing 2,048-block transaction bound. Fault injection covers
  maximum-size splits and stale absence/range witnesses survive insert/delete.
- Exhausting the shared 1,024-page Turn/archive search budget now publishes
  explicit partial coverage and ACKs exact observed dirty tokens. An 8,192-row
  fixture reproduced the old archive-boundary panic; overflow, unchanged retry,
  later-conversation progress, source shrink and source-pin release are covered.
- Alternating A-B-B-A-A-B wire runs measured median run means of 0.719 ->
  0.610 ms for creation and 1.658 -> 1.468 ms for append. Append P99 worsened
  from 6.131 -> 6.763 ms. The 2,000-append microbenchmark mean fell from
  1,257.1 -> 1,023.8 us and peak RSS median from 152,356 -> 123,344 KiB.
  The full medium client-stack harness won all eight means against PostgreSQL
  and six against SQLite; creation and append still lost to SQLite.
- A 10,000-append growth probe exceeded its experimental 1 GiB disk check in
  both arms (3,547,861,250 -> 2,037,477,795 file bytes). Subsequent real GC
  exposed a preexisting mixed-hash-segment liveness bug: only the catalog
  reference's shard was consulted, so other live hashes could be deleted.
  The 8 KiB baseline independently failed after 1,000 appends plus GC.
- GC now intersects complete block IDs across all completed mark runs while
  retaining one bounded segment ID set and decoding one run at a time. Failed
  reads leave candidates intact. A durable generation cursor selects at most
  16 catalog candidates per round, including catalogs with more than 16 entries
  in one bucket. Tests cover exact dead-block classification after retiring the
  active WAL, every mixed-segment I/O fault/short write/lost sync, and later-window
  progress. This avoids a weak fixture whose WAL replay repaired the misdeletion.
- Final release `cdcccdcf48eb3d215cd5cb00401000d22f8925ac02a3d3e5ed137697befc05d6`
  passed 635 Rust tests, strict Clippy, and five pre-authority contracts; the
  separate full-budget 1 GiB mark-spill test remains default-ignored. Real GC
  preserved all 1,000 Turn responses over 16 rounds and all 10,000 over 12 rounds
  across reopen, with identical authority UUIDs and full response digests.
  The larger run still occupied 2,017,521,940 bytes and did not report idle.
- Disk/GC convergence, retained-history lifecycle, catalog-wide compaction
  planning scratch memory, SQLite write tails, migration and launch certification
  remain open. The package stays pre-authority. Page-format bidirectional wire
  compatibility, SQLite differential and real PostgreSQL paginated parity passed
  for the page/search candidate. The final GC release also passed all 146 SQLite
  differential tests (`/tmp/tofudb-mixed-shard-gc-final-differential.log`). Ten shared documentation
  line budgets remain failing. Source seals, raw results and the combined report
  are `/tmp/tofudb-{four-kib,mixed-shard-gc}-*` and
  `/tmp/tofudb-four-kib-certification-results.md`; all experiment databases were
  removed and private PostgreSQL was stopped before cleanup.

### 2026-09-08 (Skip disabled Offerings when probing model availability)

- Availability probes no longer spend cells on Offerings the owner turned
  off. `matrix_probe_plan` is the seam for HTTP matrix probes and daily
  detection; `Offering.enabled` is the skip, not retired Deployment flags
  and not credential grants. Daily detection continues past a provider that
  has no confirmed enabled Offering instead of failing the whole run.
- Specs: `tests/model_routing/test_probe_plan_request_names.py`,
  `tests/providers/test_provider_probe_automation.py`. Contract:
  `docs/MODEL_REGISTRATION.md`.

### 2026-09-08 (Continue on interrupted turns rejected as non-integer input fence)

- Continue/checkpoint_resume/answer_guidance now attach a contextSnapshot-only
  `inputUpdate` so a pause → switch-model → resume records the live rail.
  `buildTurnOperationRequest` copied `expectedInputProjectionRevision` even when
  the resume adapter omitted it, leaving the key present as `undefined`.
  Generated `CreateAttemptRequest` validation then failed with
  `$.expectedInputProjectionRevision: expected integer` before the request left
  the browser, so interrupted history could not resume.
- The request builder now always sends a non-negative integer when
  `inputUpdate` is present; the retained adapter supplies the submitted parent
  turn's `projectionRevision`.
- Specs: `tests/turn_command.test.cjs`, `tests/turn_lifecycle/test_turn_command.py`.

### 2026-09-09 (Hardware-driven concurrency and nonblocking embedded observers)

- Frozen source `28cb4908bc27f0cc406a7f1636e7e5b19c2dfeb3`; throughput v7 registered and running, 89 tasks × 1 attempt per arm, 10 concurrent trials per arm, zero Harbor retries. v6 stopped with owned VM cleanup and verified full-domain report. Private evidence: `.eval-runs/tb21-traceability-20260908/diagnosis-throughput-v7.{md,json}`. Project `.env` now selects throughput for the next full-app launch.
- A reproducible 40-agent client-tool test saturated the shared asyncio executor; native agent workers, event observers, and result waits could starve the I/O needed to release one another. Embedded runtimes now own separate lazy executors, and async observers wait on loop notifications. Both one-runtime and per-trial-runtime compositions are covered.
- The fixed SDK/CLI default of four becomes shared resource-budget resolution. `TOFU_RESOURCE_PROFILE=throughput` uses effective CPU and memory headroom, records its profile, and preserves explicit overrides across profile changes. Managers carry the selection before process launch. Cancellation and queue teardown remain authoritative.
- Rootless paired evaluation now plans equal width from all task resource requests and existing VM reservations. Runtime resource environment is frozen in each run manifest and compared before dispatch; dataset denominator remains all 89 tasks.
- The throughput experiment is a new registration based on the repaired native runtime; a higher score and +10 percentage-point advantage remain unmeasured until all registered outcomes and audits finish.

### 2026-09-08 (xAI Grok thinking wire: reasoning_effort ladder + legacy-format unshadowing)

- **起因（用户提问）：** 会话 mtsri6xvoq6ke4 全程 grok-4.6（sankuai 网关）无任何思考过程展示，用户怀疑 thinking 参数没设上。
- **根因（双层）：** ① `build_body` 无 grok 分支，且 meituan.json 的 provider 级 `thinking_format:"enable_thinking"`（Qwen 取向）抢先认领请求体——grok 实际被发了一个网关上无效的死字段 `enable_thinking:true`（GLM-5.2+ 遮蔽缺陷同款，e92aae8d 先例）。② 即使补对参数，该网关 grok-4.6 也永不回传推理增量（六种参数变体实弹全 0 字符；GLM 在同网关 delta.reasoning_content 正常——grok 后端特异性，xAI 官方文档载明 summarized reasoning 经 chunk.reasoning_content 流出）。
- **官方契约（docs.x.ai Reasoning 2026-08-12 / Grok 4.6 页 2026-09-02）：** reasoning 永开不可关；`reasoning_effort` = low/medium/high(默认)/xhigh（4.6+ 专属，4.5 视为 high）；presencePenalty/frequencyPenalty/stop 对 reasoning 模型硬报错。
- **实弹验证（debug/probe_grok_thinking.py / probe_grok_effort.py，aigc.sankuai.com）：** effort 阶梯单调生效——同题 low 837 / high 1024 / xhigh 1169 reasoning_tokens；trace 全档 0 字符（网关侧限制，明示）。
- **改动：** ① `_family.is_grok` + `_capabilities.grok_reasoning_effort`（off→low 降级、max/ultra→xhigh 钳档，K3/GLM-5.3/Muse Spark 先例）；② `_build.py` grok 分支（reasoning_effort + temperature），enable_thinking 旧格式对 grok 归一化为 ''，post-merge 拔除 presence_penalty/frequency_penalty/stop（extra 注入也兜住）；③ `_api_stream_state.py` 同款解锁 + failover 分支 + 换模拔参；④ discovery `_THINKING_FORMAT_HINTS` 补 grok→reasoning_effort 名投。
- **规格：** `tests/llm/test_grok_reasoning_effort.py` 25 针——谓词/阶梯/build_body/遮蔽矩阵/failover/discovery 名投。
- **验证：** 新规格 25 passed；邻近阶梯 muse_spark+glm53+backend_unit+claude_parity 207 passed、gpt_spark+replay+glm53_registration 37 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 后生效。
- **未核实项（明示）：** sankuai 网关 grok-4.6 不回传推理增量是网关后端行为（xAI 官方链路可流出 summarized trace）；effort 阶梯为单发测量（837/1024/1169 单调，叠加官方文档采信）。

### 2026-09-08 (Unified conversation bar; pet removed)

- Replaced the separate project card, context sphere and numbered turn chips
  with one 40px conversation bar aligned to the composer. Typed project,
  context and navigation views share native-button disclosures. Full paths,
  permissions, Auto/Manual, context accounting and jump history remain reachable;
  no pet runtime, scenes or controls remain. Removed 40 unused pet assets.
- Jumps use canonical TurnStore IDs and Surface-owned bounded window reveal;
  the jump list mounts at most 50 rows. History loads and project permission/
  detach completions reject stale navigation generations. Manual compaction
  is single-flight and its live preview cannot attach to another conversation.
- Two final browser regressions were reproduced before fixing: compact desktop
  panes retained wide labels, and appending an in-flow panel to the flex body
  displaced the anchor before measurement. Named container queries compact by
  pane width; panels become fixed before measuring. Both have executable specs.
- Verification: 10 Node interaction/lifecycle tests and 11 real-browser cases
  passed, including 320px, a 308px desktop pane, short landscape viewports,
  themes, long roots, 1,203 turns, background activity, focus and busy actions.
  All 76 Surface cases passed across two filtered runs; project-binding cases
  passed across complementary 18/16-case runs. Neighbor integration finished
  54 passed / 2 failed; both Vite assertion conflicts also exist in HEAD
  (admin serving source location and sessionStorage mistaken for a new global).
- TypeScript, production build and asset verification passed. Isolated live
  shell checks at 1440/320px showed 40px bars, correct panel anchoring, no pet,
  no horizontal overflow and no page errors. The user's tab was not reloaded.
  The full frontend gate remains blocked by concurrent generated-runtime drift
  (earlier event-contract drift); documentation gate reports six over-budget
  shared documents. No unrelated source or gate assertions were rewritten.
- Current behavior: `docs/FRONTEND_ARCHITECTURE.md` and `docs/RENDER_CONTRACT.md`.
  Review artifacts: `designs/conversation-bar-review/`.

### 2026-09-08 (Terminal-Bench GLM-5.3-M17 paired evidence and client handoff)

- Final frozen diagnostic source is
  `af7e86ac9059db217c22c76b7a9ab861ba08db4e` (2026-09-09).
  Real OpenCode preflight completed 11 GLM calls and numeric grading, but its
  ATIF correlation key was redacted. Publish that non-secret hash under
  `trial_id_sha256`, retain credential redaction, reject conflicting identities,
  and exercise the actual exporter in tests. A separate derived export of the
  same live inputs now passes reconciliation; the original zero reward and
  audit-failed artifacts remain untouched. Export/comparison neighbors: 73 passed.
  The preregistered v6 full-domain pair is now running, 89 tasks per arm;
  v3/v4 stopped artifacts and unstarted v5 registration remain preserved.
  +10pp and official-score recovery are still unmeasured.
- The missing egress gate repair was reproduced independently: two active
  contenders with 64 configured slots retained 130 descriptors. Retain one
  directory and one candidate/owned slot per contender, reclaiming both on
  acquire/release failures. Keep the configured process fd/memory ceilings
  and default-deny destination checks. The frozen v5 source is
  `402baf901c01c62306ffb2152dbd9f8a4f668723`; OpenCode end-to-end preflight
  has produced a completed physical GLM-M17 response and native tool reads.
- Comparing the old evaluation worktree with the main-source snapshot exposed
  two environment fixes present only in the old worktree: destination-volume
  staging and guestfwd proxy bypass. V4 was also stopped before accepting a
  comparison. Bring these boundaries into production source and regression
  tests; exempt only declared service hosts from the public-egress proxy, not
  private-network ranges. Pinning a revision alone does not prove it contains
  the operational fixes used by a prior evaluator.
- Full-domain startup caught a shared environment defect beyond the native
  runtime probe: guest evidence downloaded through `/tmp` then `os.replace`
  failed with EXDEV on the shared artifact filesystem. V3 was stopped with
  evidence preserved; no baseline failures are counted as candidate gains.
  File downloads now stage privately on the destination volume, preserving
  atomic replacement, transfer ceilings and failure cleanup. Regression
  injection covers cross-mount success and old-file preservation on failures.
- Live stack evidence established a cancellation leak: 31 of 32 default
  executor workers in the four-concurrent historical Harbor process waited on
  client-tool results. SDK abort signalled only an event, bypassing the canonical
  task flag and resource callbacks. Public runtime abort/close now reuse owned
  task cancellation; client waiters also observe the abort event defensively.
  Fail-first tests cover both entry points, cleanup callbacks, event-only stop
  and 40 successive lifecycles without accumulating waiters. The leaking old
  process was stopped with a receipt; its trial evidence was retained.
- A real GLM/QEMU probe exposed an independent current-source assembly defect:
  exclusive custom schemas were advertised without ToolContractV2 execution
  documents, so every model call was rejected. Compile contracts from the
  exact filtered tool list, replacing stale authority. Fail-first assembly
  tests cover valid calls, out-of-budget arguments and stale-tool rejection;
  strict validation is retained. Historical v2 predates this source state.
- Frozen repair revision `7fc5d6372f017df6bff12b71c17df00337dea60b`
  passed 221 evaluation tests (3 opt-in skips), 113 runtime-neighbor tests
  (overlapping sets), and a rebuilt wheel boundary check. Real GLM-M17 + QEMU
  verified guest output and tool submission; withholding a client result then
  cancelling settled in about one second with no tool heartbeat left behind.
  The preregistered v3 pair launched all 89 tasks once per arm, four
  concurrent trials, no destructive retries, 65536 output tokens and 21600-second
  native/Harbor agent deadlines, then stopped on the environment defects above.
  This is a diagnostic run, not a +10pp result.
  Private diagnosis, hashed evidence, launch receipt and bounded offline observer
  live under `.eval-runs/tb21-traceability-20260908/`.
- Full-domain correction: report schema v2 always includes all 89 pinned tasks,
  with planned repeats in the denominator. Passed, scored-failed, unscored and
  missing attempts reconcile to that denominator; no common-valid subset score
  or subset confidence interval is produced. A partial history cannot inflate
  the headline by excluding failures to execute or score.
- The official GLM-5.3 model card reports 88.2 with Claude Code 2.1.207, max
  reasoning, 65536 output tokens and six hours. Historical M17 runs used other
  budgets. The source and exact published parameters are recorded in the
  swebench README; local results do not establish official reproduction.
- Local GLM comparison runs can explicitly pin Harbor's outer agent deadline
  as well as the native timer; mismatched timers and drift on resume fail.
  OpenCode context/output budgets are exposed through the existing launcher.
  Defaults and formal Kimi release budgets are preserved; a six-hour Tofu timer
  alone no longer masquerades as a six-hour Harbor execution budget.
- Historical native events show completed model streams observed over two
  minutes later in 14 of 639 rounds (maximum 1353 seconds). The adapter performed
  synchronous event fsync/audit writes on Harbor's shared asyncio loop. Move
  these writes and runtime shutdown off-loop, serialize them, and drain owned
  writes on cancellation. Retain durability and emit bounded I/O metrics;
  delivery lag alone does not prove disk I/O caused every historical stall.
- Retained v2 trajectories contain 12 `production runtime rejected its owned
  custom tool result` failures. The adapter allowed 1800-second commands while
  the production client waiter expired after 120 seconds. Client definitions
  can now declare a finite timeout (hard ceiling 3600); the benchmark freezes
  1830 seconds, including callback headroom, with a separate execution digest.
- Register the owned waiter before emitting its event; use a monotonic deadline
  and reclaim failed publication. Preserve rejected tool results in the adapter
  audit before propagating failure. Ownership, capacity, cancellation and
  exactly-once resolution remain unchanged.
- Added pre-dispatch paired registration to the existing Harbor launcher and
  bounded, hashed, task-paired reports with explicit missing/surplus outcomes,
  seeded task-cluster intervals and a fail-closed +10pp gate. Historical and
  active runs remain diagnostic. Native harness configuration is frozen per
  arm; equal wire sampling is not inferred from a shared model name.
- The production-Tofu auditor and evidence projector now consume the registered
  expected model instead of hardcoding Kimi K3. GLM projection must reconcile
  that exact model across request, prompt profile, physical dispatch, usage and
  ATIF; mixed-model/fallback rejection and Kimi release defaults stay intact.
- An observed model tool-call finish without a client handoff is a dispatch
  investigation, not proof of reasoning starvation. Reports preserve that
  evidence distinction without copying prompts, commands or reasoning text.
- Executable contracts: `test_custom_tool_isolation.py`,
  `test_harbor_tofu_runtime_agent.py`, and `test_tb21_comparison.py`.
- Full-domain follow-up verification: 175 passed / 3 opt-in tests skipped across
  the adapter, launcher, projection, OpenCode and TB contracts. Slow-fsync and
  cancellation injection passed. The shared documentation gate still reports
  five unrelated documents above their existing line budgets. No repaired
  full-domain live run has been measured; historical scores are not fix gains.

### 2026-09-08 (Skills discovery, explicit activation, and authorized installation)

- Observation: retained September 7–8 logs showed 66 skill searches and no
  instruction loads. Only a travel guide was visible; host-home Codex packages
  were outside Tofu's declared owner/project sources.
- Decision: discover standard project skill folders under project authority;
  include three repository development guides. Explicit `$skill-id` requests
  use a bounded context provider; implicit use remains model-selected. Record
  instruction disclosure separately from search metadata.
- Installation follows the existing Auto / Manual mode, as requested. ToolSpec
  declares the Auto-grant exception; the gateway still issues exact, expiring,
  single-use receipts. Verified origin, owner isolation, atomic rollback and
  archive limits remain enforced; installation never executes bundled scripts.
  Three attempts bound new installed content to 75 MiB per task, while a
  32-entry content-free receipt tail bounds runtime observability.
- `test_skill_activation.py` covers real request phrasing, root escape denial,
  owner isolation, bounded disclosure and historical catalog misrouting.
  Installation tests exercise attended/unattended Auto, Manual rejection,
  single-use receipts and the attempt budget.

### 2026-09-08 (上下文注入统一登记：预算倒计时退为遥测，39 类来源逐项准入与 Inspector 回执)

- **起因：** 用户指出 `context_budget` 与工具栏预算观感冲突，要求逐项审查并统一管理注入。工具栏上一轮 prompt/full-window 与旧提醒 working-message estimate/usable-window 的分子、分母和时点均不同；截图不能证明同时测量，容量估算也不应催促模型收工。
- **契约：** `context_composer/registry.py` 登记 39 个 ID 的来源、必要性、触发、权限层级、生命周期、必需性与预算。快照用 `context_block`/`append_context_blocks`，恢复/inbox/Paper/child/autopilot 用 `append_injection`，预算只走 `record_injection_diagnostic`；统一 facade 继承 task 可选开关。必需规则、checklist、人类输入和恢复不受可选预算挤压；`worker_policy` 保持对话前 system 权限及 resume 边界。工具批次未闭合时快照延后、inbox 先检查再 drain，ACK 保留原所有者。
- **减负：** 预算不再发模型文本/intervention card；JOURNAL 仅留定位与历史证据边界，采集不读/创建/轮转日志、不写 `.gitignore`；memory guidance 缩短；并行/PTC 按实际能力准入；MCP delta 给有界名称与发现入口，不塞截断 schema；modified-files 仅实际写路径、写后间隔触发一次；checklist 仅完整当前状态/ID 缺失时恢复。
- **调试：** `contextManifest` 复用 Inspector，记录追加/复用/抑制及来源、原因、hash、token、round。原追加回执不变，最新非追加决策每 ID 一条，事件 UUID 随 carrier 保存；128 行/128 KiB 上限，不记正文/密钥值。子代理用自己的 Inspector ID，Paper 留自己的 manifest。
- **验证：** 聚焦 232 绿；邻套 347 绿/3 红，其中 todo 空回复断言已修为允许且仅允许 `terminal_recovery`、不能误判 todo nudge。三入口 task-config 新回归先复现 3 红，修复后相关 202 针全绿；最终 facade/child/autopilot/Paper 110 针全绿，完整改动域 ruff 净。未放宽两项域外邻测：gateway token cap（564 > 522）与 lean URL 旧文案期望。全库 architecture 的 provider route→dispatcher 内部依赖及其他领域文档超行限仍失败，本领域文档已满足行限/目录约束。并行改动保留，未重启服务。
- **维护入口：** `docs/CONTEXT_INJECTIONS.md` 是逐项必要性、准入、预算与接口的当前契约，domain map、docs map/catalog、局部 AGENTS 指向该单一来源。

### 2026-09-08 (批量编辑轮头部去重：标题「(N edits)」计数与右侧「N/N edits」徽标双双移除)

- **起因（用户截图+裁定）：** edit_file 完成轮头部同时显示「Edit 8 files (18 edits) — …」文本计数和右侧绿色「18/18 edits」徽标，而展开的列表已逐条列出每个 edit 的状态——两者皆冗余，用户裁定移除。
- **修法（三处）：** ① `lib/project_mod/tools.py` 标签：edit_file 的 `(N edits)` 后缀、apply_diffs 的 `(N edits)`、insert_contents 的 `(N insertions)` 全部移除（同一冗余模式，三个姊妹工具一并收敛）；② `lib/tools/meta.py`：`_build_apply_diff`/`_build_insert_contents` 的计数徽标（`x/y edits`、`x/y inserted`）移除——仅无「Applied/Inserted X/Y」头的硬失败保留 `failed` 红徽标，门拒绝仍由下游覆写为 read first/stale；部分失败经头部内联失败原因 + 行内红叉呈现，writeOk 不变；③ `frontend/src/runtime/sections/ui/tool_rounds.js::_computeToolBadgeHtml`：isWrite 判定提升到函数顶部，write 工具无 meta.badge 时不再落 token/chars 兜底徽标（否则右侧会换个「x.xk tok」chip 顶上，违背「移除」本意）。
- **规格：** `test_tool_display_readability.py` 三处标签期望去计数；`test_gate_refusal_meta.py` 末针改断「真实批头无 badge 且 writeOk=False」。wire-parity 电池为静态夹具输入（徽标取自夹具 meta、token 轮均非 write 工具），基线零漂移。
- **验证：** 目标五套 114 绿；邻套 project_tools/tool_loop_breaker/intent_stall/desktop_agent 292 绿；ruff 净；compose runtime+styles 重生成后 `check:frontend` 全链绿（121 assets）；styles 陈旧为并行会话预存状态，随本轮一并重生成。生效需重启（后端 label/meta）+硬刷新（前端）。

### 2026-09-08 (科研首页突破性重设计：英雄区 + 作曲家卡片 + 五步旅程 + 模板卡 + 续探索区；真机浏览器走查零 JS 错误)

- **起因（用户截图+裁定）：** 科研铸造台首页「还很难看、UI/UX 不友好」——旧版是一个窄卡片里塞一个 textarea + 按钮 + 三颗模板 chip，信息密度低、没有产品感。用户要求突破性重设计。
- **修法一（`frontend/src/features/research/components/view.ts` 的 `showResearchLanding()` 全重写）：** 单卡片布局 → 五段式工作台 `.rs-landing`：① `.rs-hero`——flask 图标 kicker chip + 衬线大标题「从研究方向，到经评审的实验简报。」+ 副文案；② `.rs-composer`——大号圆角输入卡：textarea 自动增高（上限 260px）、非空才显 `n / 2000` 计数、提示行内嵌 `⌘/Ctrl + Enter` kbd、琥珀色「开始研究」按钮；③ `.rs-journey`——五步旅程（编号圆点 01-05 由连线串起：建立语料库/绘制空白图/形成假设/质询主张/封装证据，各配一句描述，文案走 `paper.research.step.*`）；④ `.rs-templates`——三张编号模板卡（点击经既有 `_fillResearchTemplate(n)` 灌入输入框）；⑤ `.rs-recent`——「继续之前的探索」异步区（`Api.research.records(4)`，点击 `openResearchRecord` 续跑，空/不可用则整区隐藏不打扰）。挂载后 rAF 自动聚焦输入框。
- **修法二（`03-paper-reader.css` 着陆区块重写，+205 行）：** 英雄区居中排版、作曲家卡暖底 + 聚焦内描边、旅程节点连线、模板卡 hover 琥珀环；移动端旅程塌为双列网格（390px 实测无横向溢出）；reduced-motion 规约随块登记。
- **i18n：** `paper.research.*` 新增 18 键 × 双语（step.{harvest,survey,ideate,evaluate,publish}、continueTitle、heroTitle/heroBody、workbenchLabel、directionHint、entryPlaceholder、directionLabel、startBtn、templatesLabel、template1-3、recentUnavailable），契约重生成。
- **规格：** `workbench.test.cjs` 着陆针（英雄区/旅程五步/模板灌入/续探索渲染/提交入口）；`test_research_discoverability.py` 更新为着陆结构针。
- **验证：** check:research 7 绿；discoverability 15 绿；generate:i18n+check:i18n、generate:styles+check:styles 净；typecheck:modules 净；check:frontend 全链绿（121 assets 发布）；verify:frontend 通过；浏览器面 `test_auto_research_entry_is_reachable_without_a_paper` 绿。**真机走查（:15000 实服 + Playwright 截图）**：桌面 1440 与移动 390 双宽度截屏核对视觉，模板卡点击灌入 53 字符、计数器实时显 `68 / 2000`、390px 无横向溢出、全程零 JS 错误；走查脚本只读未提交任何任务。生效需硬刷新。

### 2026-09-08 (模板供给 transplant 双根修：别名碰撞断引用 unknown_model 400 + 同名注册行窗口过小 context 越界 400；真机收敛 52 配方 revision 34)

- **起因（用户裁定「全部同步」）：** 对活机（:15000）跑 meituan 模板供给收敛——52 个缺失配方（含新模型 kimi-0902-preview）全部探测 served，但 CAS 提交连撞两道 domain 校验 400，同步完全无法落库。
- **根因一（unknown_model）：** `_transplant` 的 models 行去重用的是 merge_keys 碰撞——注册表里已有 `(openai, o3)` 行（另一个 access 留下的）时，`o3-2025-04-16` 行被跳过；但 offering 的 model 引用与凭证授权仍按字面 ref 指向这条从未落库的日期快照行 → `credential authorization references unknown model ModelRef('openai','o3-2025-04-16')`。该场景恰是 domain 别名合并（`_merge_trained_model_alias_rows`）的设计目的：重名行被吸收为别名、能力并集、窗口取 max、所有引用经 alias_map 规范化到幸存行。
- **根因二（context 越界）：** 精确同名的注册行已存在但 context_window 更小/能力更少（旧来源登记）时，新 offering 按配方值申报 → `offering context_window exceeds the official model limit`。
- **修法（`lib/model_routing/template_supply.py`）：** ① 去重改精确 ref（creator_id, model_id）——merge 碰撞的行照常追加，交给 domain 别名合并吸收并规范化全部引用；② 精确行已存在时对其做与别名合并同语的收敛：capabilities 并集、context_window 取 max（提行绝不降级，domain 不变式 offering⊆model 天然保序）。模块 docstring 的 insert-only 条款补登记行向上收敛一句。
- **规格：** `test_template_supply_sync.py` 新针 `test_recipe_respelling_a_registered_model_converges`——预置 `(openai,o3)` 全局行（本 access 无 offering）与 `(google,gemini-3.8-flash)` 小窗口行，断言收敛成功、offering 被规范化为 `openai/o3`、凭证授权同 ref、注册表无日期快照残留、gemini 行窗口被抬到 ≥ offering 申报值。NEUTER 验证：回退修复后该针以与真机完全相同的 unknown_model 错误失败。顺带修正既有针的过期期望 113→111（本轮模板编辑后 114 配方 −1 种子 −2 个 gpt-4o 日期快照；claude-opus-4.7/4.8 的 model-id 键不相交，各成一 offering）。
- **验证：** 本套 9 绿 + 邻套 meituan_template_sync/provider_template_recipes/model_routing_contract/bootstrap/api_v1_model_routing_contract/aa_evidence 共 78 绿；ruff 净。真机收敛落库 revision 34（+52 offerings/+52 deployments，热重建槽位免重启），二跑幂等空转。
- **kimi-0902-preview 现状：** 配方在 `/v1/openai/native` 面孔（`/v1/openai` 探测会 404，属面孔错配非模型缺失）；正确面孔探测为 `rate_limited`——网关认得模型但 App 级每分钟配额被打满（HTTP 429 按 app+model 限流）。按设计落 `enabled=False, probe_status=unprobed` 可重探，健康巡检探测生成成功即自动点亮。
- **注意：** 运行中服务进程仍持旧模块内存，但缺失清单已空，内部周期同步不会再触 transplant；下次重启自然拾起修复。

### 2026-09-08 (腾讯 TokenHub 模板添加被拒 + hy3/hy4 缺席模型配置：家族身份表缺 tencent 一族 + `cheap` 定价层级误闯能力子集校验，双根修)

- **起因（用户截图+裁定）：** ① 设置里添加腾讯 TokenHub 模板弹「添加模板失败：offering claims capabilities absent from the official model: ['cheap']」；② 模型配置界面没有 hunyuan-2.0 (hy3/hy4)——然而 meituan.json/tencent.json 模板早已收录 hy3-preview/hy4-preview 配方。用户要求根修并全力优化。
- **根因一（模板添加被拒）：** TokenHub 的 glm-5/glm-5.1 价格（$0.829/3.315 每 M）落在 cheap 档位（<3/15），`_pricing.py` 给 offering 落 `cheap` 标签；但世界目录里 glm-5/glm-5.1 的 Model 行来自 zhipu 模板（创作者官方价 $5/25 不 cheap），且世界目录合并是**先注册者赢**——Model 行缺 `cheap`。`domain.py` 的 offering⊆model 子集校验把定价层级当身份能力拒绝。修法：校验豁免托管定价标签（`cheap`），豁免源由 `_pricing.PRICING_TIER_TAGS` 单一权威供给——定价标签是各服务商自己的价格书派生物且会被 live 价格重同步，分销商补贴价低于官方价时天然比官方 Model 行多一层标签，不算能力虚报。
- **根因二（hy3/hy4 不可见）：** `_creator_families.py` 家族表从未登记 tencent/hunyuan 一族——hy3-preview/hy4-preview/hunyuan-* 的 `creator_family` 解析为 None，全部被打成 `pending_identity`（provider 作用域），而世界目录（模型配置注册表）只收 `confirmed` 官方身份行，故整族隐形；连带 meituan 模板的 hy3/hy4 配方也卡在 pending。修法：家族表登记 tencent 一族（`hy3/hy4` 前缀 + `hunyuan` 关键词 + `HY3/HY4` 词干），meituan/tencent 两份模板配方即刻全部 confirmed，世界目录同步后 10 行入库。
- **顺修（种子漂移）：** `_slots.py` 的 hy4-preview 种子缺 `cheap`（其定价 0.829/2.486 明明在档内），补齐使槽位与定价推导一致。
- **规格（+6 针）：** `test_creator_identity` +3（tencent 家族解析/pending 变 confirmed 含 meituan 路径/家族注册表含 tencent）；`test_provider_template_recipes` +2（tencent 模板全配方 confirmed 且三 hunyuan id 在册、**回归针**：世界目录 Model 行来自 zhipu 官方价时，合并 tencent bundle 经 `normalize_document` 全文档归一化不再抛 cheap 违规）；`test_model_routing_contract` +1（cheap 豁免但 vision 等真能力仍被子集校验拒绝）；`test_world_catalog_sync` 针更新（hy3/hy4 行 confirmed 且在册）。
- **验证：** 四套 26 绿；邻套 api_v1_model_routing_contract/api_v1_byo_surface_polish/provider_model_add/template_supply_sync/provider_catalog_sync 55 绿、pricing_tier_sync 4 绿（2 红为并行会话 `_pricing.py`+新模板 WIP 移动靶，stash 我的改动后复红，与本改无关）；ruff 净。文档：MODEL_REGISTRATION.md 不变式节补定价层级豁免条款。生效需重启（世界目录同步在 boot 时跑）+硬刷新。

### 2026-09-08 (LaTeX 实时预览泛化为双源：本地 .tex 编辑走宿主 TeX 引擎编译 + 自研 SyncTeX 解析定位，未提 Overleaf 时默认本地)

- **起因（用户裁定）：** 功能定位是通用 LaTeX 而非只支持 Overleaf（工具命名空间叫 overleaf 不代表产品边界）；桥接本地项目与 Overleaf——用户未明确指定 Overleaf 时默认本地项目。即 agent 用 write_file/edit_file/apply_diff(s)/insert_content(s) 改工作区 .tex 时，同一右侧面板应实时重编译并滚动定位。
- **后端（`lib/latex_preview.py` + `routes/api_v1/latex.py`）：** `POST /api/v1/latex/preview {path, needle?, line?, conv_id?}`：① `resolve_tex_path`——模型写的路径（绝对/~/相对 primary root/`rootname:rel` 经 `_resolve_base`）解析为绝对 .tex，且必须在已知 workspace root 内（commonpath 校验，拒绝逃逸）；② needle→行号复用 `overleaf_preview.needle_to_line`；③ `find_main_document`——被编辑文件自身含 `\documentclass` 则用它，否则扫同目录候选按名字偏好（main>paper>root>ms>thesis>index）+mtime 排序；④ `pick_engine`——主文档含 fontspec/xeCJK/ctex/unicode-math 选 xelatex 否则 pdflatex，都没有则 PreviewError；⑤ `_compile` 两趟策略（log 含 Rerun 再跑一趟），`-synctex=1 -output-directory=<data>/cache/latex/<sha1(main)[:16]>/`，cwd=主文档目录——**aux 全进缓存目录，源树零污染**（e2e 针锁定 listdir 不变）；⑥ **自研 SyncTeX 解析**（本机 TeX Live 精简包无 synctex CLI）：gzip 文本，Input 表 normpath 归一化（pdflatex 写 `./main.tex` 形式），`{N`/`}N` depth-0 分页标记，record 坐标为页面绝对 sp（÷65536 得 pt），hbox 的 v 是**基线**——输出矩形契约 v=顶=baseline−height（与 Overleaf locate 工具同契约，前端零改动）；最近行选择 + order 平手取先；⑦ 同文档 per-key 锁串行，PDF 改名 current.pdf 供 `GET .../pdf` 二进制服务，缓存保留 8 个文档。
- **前端：** ① hook（`overleaf-preview-hook.js`）新增本地轮扫描：六工具集合 + `.tex/.ltx` 后缀 + 别名容错（file_path/new_string/file_text——tool rounds 存的是模型原始 emission，repair 层只改执行副本）+ refusal/rejection/unsettled 跳过；候选带 `source:'overleaf'|'local'`，同 round 多 tex 文件取最后一个。② owner（`live-preview.ts`）按 source 分支 POST（local → `api.latex.preview({path, needle})`），面板标签显 file||path||projectId；observeRounds 校验放宽为按 source 分派。③ api.js 加 `latex` 命名空间。④ i18n 键名保留 `overleaf.*`（内部连续）但值泛化为 LaTeX 措辞（契约 4905 键 `b2956ff219b7`）。
- **实测探针（写解析器前的格式验证）：** /tmp 下真跑 pdflatex -synctex=1 确认：分页标记是 `{N`/`}N` 而非 'Page' 文本；嵌套 record 的 h/v 与外层相同（绝对坐标非相对偏移）；textwidth record W=345pt 与 letter article 理论值精确吻合——坐标系原点页左上、v 向下、单位 sp，X/Y Offset=0。
- **规格（+43 针）：** `test_latex_preview.py` 26 针（resolve 六针含 root 逃逸拒绝、主文档发现四针、引擎选择四针、synctex 解析六针[内嵌手工样本：最近行/矩形换算/基线修正/分页/平手/坏 gzip]、**真引擎 e2e 三针**（skipUnless pdflatex：子文件 needle 定位+源树零污染+同 key 复用、编译失败含日志尾、缓存剪枝）、路由五针）；hook +4 针（本地 write_file 派发、apply_diff 别名、batch 只挑 tex、非 tex/拒绝/未落锤全跳过）共 11；vite +2 针（local POST 体形状 path+needle 无 project_id、面板标签显 path）共 10。
- **验证：** 四套 45 绿；api_contract_drift 6 绿；ruff 净；i18n 契约重生成 + compose runtime 重生成后 `check:frontend` 全链绿（121 assets）；`verify:frontend` 通过；`typecheck:modules` 净。文档：FRONTEND_ARCHITECTURE 特性条目改双源、API_CONTRACT §4 二进制行加 latex。生效需硬刷新；真机走查（对话里改本地论文观察面板）尚未做。

### 2026-09-08 (对话内 Overleaf 实时预览：TeX 编辑触发重编译，右侧 PDF 面板滚动定位到改动处并脉冲高亮)

- **起因（用户愿景）：** 希望对话里改 TeX 论文时有 Overleaf 手感——右侧实时编译 PDF，编辑某处后自动滚到该位置（带动画）。
- **架构（四段管线）：** ① **retained 扫描器**（manifest 节 `overleaf-preview-hook.js`）：挂在 `renderToolRoundsHTML` 唯一接缝上，每次 paint 纯扫描——只有 LIVE 流中**新落锤**的 `mcp__overleaf__edit_file/rewrite_file/update_section` 成功轮（非 rejected、status=done、24-hex project_id）才生成候选，按 toolCallId 去重（上限 256），needle 取 new_string/section_title/rewrite content 头 800 字符（上限 1200）；历史重绘（isStreaming=false）永不编译。候选经懒加载特性桩 `_overleafPreviewRounds` 进入 typed owner——无真实编辑时 pdf.js 管线零加载。② **后端**（`lib/overleaf_preview.py` + `routes/api_v1/overleaf.py`）：`POST /api/v1/overleaf/preview` 先经 Overleaf MCP bridge 读当前源文件把 needle 映射为行号，再 `download_pdf` 编译并暂存到 `<data>/cache/overleaf/`（每项目一份，保留最新 8 个项目——PDF 是可重建传输缓存非耐久态），再 `locate_in_pdf` 拿 SyncTeX 页号+矩形（PostScript 点，v 自页顶）；同项目编译经 per-project 锁串行，编辑风暴不会并发编译同一远端树。`GET .../pdf` 以二进制服务缓存 PDF（API_CONTRACT §4 二进制 carve-out 行已登记）。③ **前端 owner**（`features/overleaf/live-preview.ts`，deps 全注入可测）：trailing-latest 泵串行编译——编辑爆发塌缩为最新候选而非排队陈旧 PDF；用 Paper 共享 pdf.js loader 按 fit-width 渲染全部页，然后平滑滚动 SyncTeX 矩形到视口中部并加 2.6s 脉冲高亮（`olp-highlight` 入场→淡出动画）；面板头有重编译/关闭按钮。④ **样式**：`24-overleaf-live-preview.css`（33 节组成校验通过），i18n 双 locale 各 +8 键（契约 4905 键 `0ee5ff9dc006`）。
- **踩坑：** ① 新 manifest 节首行必须是 `/* ===== migrated source: <name> ===== */` 标记（compose_frontend_runtime 强校验）；② 改 runtime/styles 源后须先跑对应 compose 再 check:frontend（stale 检查失败）；③ i18n `t` 是变参泛型，deps 签名直接类型为 `Translator` 传 `t` 本体，不要包 `(key) => t(key)`（key:string 推不出 K）。
- **规格（+26 针）：** `test_overleaf_preview.py` 17 针（needle→行解析、缓存剪枝、per-project 锁、路由校验）；`test_overleaf_preview_hook.py` 7 针（jsdom 驱动真实节：done/live 才派发、toolCallId 跨 paint 去重、历史 paint 不编译、unsettled/rejected/非编辑/坏参数全跳过、update_section needle=标题、rewrite needle 截 800、owner 异常不断 paint）；`test_overleaf_live_preview_vite.py` 8 针（vite 打包真实 TS owner + jsdom：POST 体形状、面板开合、全页按序渲染、目标页脉冲高亮、smooth 滚动、爆发塌缩为最新候选、拒编译出 error toast + 失败状态）。
- **验证：** 三套新规格 26 绿；api_contract_drift 6 绿、style_composition 2 绿；ruff 净；compose runtime+styles 重生成后 `check:frontend` 全链绿（121 assets 发布）；`verify:frontend` 通过；`typecheck:modules` 净。文档：FRONTEND_ARCHITECTURE.md 特性条目 + API_CONTRACT §4 二进制行。生效需硬刷新；端到端真机走查（对话里真改一次 Overleaf 论文观察面板）尚未做。

### 2026-09-08 (服务商子下拉重设计：原生 select 弃用，改 chip + body 锚定浮层菜单，与面板同套设计语言)

- **起因（用户裁定）：** 模型下拉精修后，行内「自动服务商」仍是原生 `<select>`——OS 弹出层完全无视页面样式，在打磨过的面板里是个异质控件。用户要求子下拉也按同一设计取向重做。
- **修法（`main_toolbar_ui.js` + 两处 CSS）：** ① JS：新增 `_toggleProviderMenu`/`_closeProviderMenu`——chip 按钮（label + 细 chevron，aria-haspopup/expanded）点击后在 `document.body` 挂 `#psProviderMenu`（role=listbox，首项「自动服务商」+ 各 provider，当前项 `.active` + ✓ + aria-selected），fixed 定位按 chip rect 向上弹出、空间不足翻下、右对齐收边；body 锚定使滚动列表永不裁剪菜单，任何外部 scroll/resize/外点/Escape 即关（listener 随 cleanup 全卸，全局同时只存在一个菜单）；选中项沿用 `selectModelRoute` 同一身份展开。`_populateModelDropdown` 重建与面板外点关闭路径均先 `_closeProviderMenu()` 防孤儿菜单。② base CSS：`.ps-dd-provider-select` 两规则删除，换 chip/menu 全套（菜单与父面板同语言：圆角浮层 + presetDropIn 入场 + active=底色+inset 细环）。③ tofu CSS：重输入框规则换奶油 chip（#FBF9F3/#D6CFB8/8px）+ 面板同款暖影浮层（#F4F2EB + 双层 rgba(90,74,52,*) 影）+ active 暖 tint 内描边 + 琥珀 ✓。`ps-dd-provider-tag`（pending 行）保留。
- **既有测试契约冲突一处：** `test_turn_serving_route.py` 以 `'\n}\n\n/* Populate model dropdown'` 分隔符抽取 `_modelRoutingDropdownModels`——helper 块初版插在该函数与注释之间破坏了邻接，按「保持抽取边界」修：helper 块移到 `_modelRoutingDropdownModels` 之前。
- **规格：** 新建 `tests/providers/test_provider_preference_menu.py`（jsdom 驱动真实 retained section）10 针——chip 渲染且无 select、自动态 label、点击开菜单 listbox、选项=自动+全部 provider、当前项 active+✓+aria-selected、二次点击合拢、选 provider 后 `config.preferredProviderId/routing/modelRef` 落锤（selectModelRoute 在 section 内定义会遮蔽 stub，故观测 config 副作用）、选后关菜单、外点关菜单不改路由、Escape 关菜单且焦点回 chip。
- **验证：** 新套 10 绿；邻套 model_group_convergence/style_composition/turn_serving_route/model_brand_resolver/chat_mode_parity 14 绿（`test_settings_runtime_has_one_v2_provider_editor` 红为并行会话 settings/ 千行 WIP 移动靶——我的 JS stash 后仍红，与本改无关）；compose_frontend_runtime 重生成后 check:runtime/check:styles/check:frontend 全链绿（119 assets 发布）；verify:frontend 通过。生效需硬刷新。

### 2026-09-08 (服务商模板卡禁用模型不生效根修：HTTP 适配层提交权限后热重建 dispatcher 槽位池)

- **起因（用户报告+裁定）：** 服务商模板卡里关掉模型开关、保存后什么都不变——保存路径（`PUT /api/v1/model-routing` CAS 替换）确实把 `offerings[i].enabled=false` 提交了，但运行中的请求照旧走旧模型。用户问「不是热重载的吗」——产品意图就是热重载，判为缺陷。
- **根因：** `LLMDispatcher.initialize()` 在首次使用时把候选（offering 启用态、端点、明文 key、配额）快照进槽位池并置 `_initialized`，之后永不主动重建；重建触发器 `_rebuild_dispatcher_slots()`（`lib/llm_dispatch/health_local.py`）只有健康巡检、template_supply、catalogue sync、local_serve 四处调用，而 Settings 的 HTTP 适配层（`routes/api_v1/providers.py`）六个提交点全部漏调——同类后果：删服务商后槽位继续服役、轮换 key 后旧 key 用到重启。
- **修法（`routes/api_v1/providers.py`）：** 新增 `_hot_reload_dispatcher_slots()`（懒导入 + try/except，重建失败不拖垮已落库的请求），在全部六个成功提交点后调用：model-routing CAS 替换（模板卡保存路径）、ProviderAccess 增/改/删、凭证 secret 轮换、migration commit。未放在 `compare_and_swap` 仓储层：分层方向（llm_dispatch 依赖 model_routing，不反向）+ 测试进程会因此真建 dispatcher。
- **规格：** `tests/api/test_api_v1_model_routing_contract.py` 新增 `DispatcherHotReloadTest` 3 针——PUT 成功触发一次重建；revision 冲突 409 与契约拒绝 400 均不触发；create→patch→secret 轮换→delete 各触发一次（共 4 次）。
- **验证：** 本套 8 绿；邻套 provider_catalog_sync/health_local_heterogeneous/api_v1_byo_surface_polish/api_v1_agent_run 共 50 绿；ruff 净。`docs/MODEL_REGISTRATION.md` 选路节已写明「提交即重建、免重启」契约。nc-guard 提示 `lib/message_queue.py` 等三处为并行会话 WIP，与本改无关。

### 2026-09-08 (科研页重设计为对话形态：时间线线程 + 类型过滤 chip + 侧栏历史主题；英雄区首页改紧凑作曲家)

- **起因（用户截图+裁定）：** 科研铸造台布局信息密度过低——巨大英雄区 + 空旷卡片；用户裁定：每个研究方向应设计成像对话一样便于持久翻阅的形态，并把历史探索主题放进侧栏。
- **修法一（侧栏轨道，前轮已完成）：** 研究方向以会话式条目渲染进侧栏覆层（含历史主题），刷新接线进 paintResearch/showResearchLanding/newResearchDirection；首页英雄区与最近记录区移除，改紧凑作曲家。
- **修法二（运行视图 v2，`frontend/src/features/research/components/view.ts`）：** 五个互斥 tab（overview/evidence/ideas/workspace）改为**一条按管線顺序的时间线线程**——papers → evidence → ideas → review → ledger 五种 `data-rs-kind` 条目共享一根竖向脊线（running 时末尾追加脉冲 live 条目）；导航条 = 左侧类型过滤 chip（每类一枚、带计数徽标：文献数/空白数/接受数/复评分）+ 右侧三个界面 tab（线程/实验/论文）。过滤经 shell 的 `data-research-filter` 属性纯 CSS 隐藏其他条目；tab/过滤选择在重绘与界面切换间存活，仅换方向时重置。失败/降级警报上移至管線条之下（原埋在 finishedBody）。`stable-dom.ts` 的 key 函数增认 `data-rs-kind`，条目跨重绘保持 DOM 身份（开着的 details/滚动位置不丢）。实验/论文界面共用 `#researchWorkspace` 面板（仍由 workspace 控制器自持，遥测不重绘）。
- **修法三（样式，`03-paper-reader.css`）：** `.rs-nav` 布局、`.rs-chip` 胶囊、`.rs-thread-*` 脊线（首尾条目线条截断、live 节点 rsPulse）、五条 `data-research-filter` 隐藏规则；`.rs-thread[hidden]` 显式归零（flex 覆盖 UA hidden）。工作区面板弃用会强制 display:none 的 `.rs-tab-panel` 类，改 `.rs-workspace-panel` 纯 hidden 控制。
- **i18n：** zh/en 各 +7 键（tabThread/filterAll/kindPapers/kindEvidence/kindIdeas/kindReview/kindLedger）；契约 4897 键 `97d8905e37a1`。
- **规格：** `test_research_discoverability.py` 渲染 harness 增 thread 场景 + 2 针（线程顺序 papers,evidence,ideas,ledger/chip 与条目镜像/重绘保 evidence 节点身份/过滤驱动 shell 状态/界面切换/过滤存活；CSS 五类隐藏规则存在针）；`test_research_runtime_browser.py` 真实 Chromium 视觉针改写为线程契约（filterResearchThread 后他类条目 computed display:none、运行态顺序 papers,evidence,ideas,ledger,live、chip 焦点跨重绘存活、390/1280 无横向溢出）。
- **并行会话协调：** `frontend/src/core/turn-command.ts` 被 resume-ctx 并行任务插入 WIP 时复制了一行未闭合的 `createTurnCommandId` 签名，全项目 tsc 挂掉；仅删该行使树可解析，其 `resumeCtxInputUpdate` 新函数原样保留（该任务随后自行完成并记日志于上一条）。
- **验证：** discoverability 14 绿；browser 3 绿；check:research 4 绿；typecheck:modules 净；check:i18n/actions 净；ruff 净；check:frontend 全链绿（119 assets）；verify:frontend 通过。生效需硬刷新。

### 2026-09-08 (暂停→换模型→继续双端根修：turn ctx 栏随续跑换绑 + 轮块内落「模型已切换」提示)

- **起因（用户截图+裁定）：** 暂停后切换模型再点继续，① 输入轮的 turn ctx 栏仍显示旧模型（rail 快照是提交时刻物证，续跑不换绑）；② 轮块内没有任何「模型被用户换掉」的提示——截图里 claude-opus-5 连番 502 后用户手动停掉并改发 kimi-k3，界面上无从分辨。
- **修法（全链四处，语义各自单点）：**
  ① **存储**（`lib/storage_sidecar/operations_pkg/_turns_write.py`）：resume 操作（continue/checkpoint_resume/answer_guidance）新增 `_resume_ctx_rebound_projection`——只许 `contextSnapshot` 单键的 inputUpdate，外科式合并进**当前**父轮投影（顺带清掉遗留 `_ctx`），内容保持不可变历史物证；任何其他键仍被「Only regenerate may edit its submitted turn」拒绝。无需 revision 栅栏：续跑轮自身的投影 CAS 已门控，rail 快照本就是 last-writer-wins 展示态。
  ② **服务**（`command_service.create_attempt`）：续跑前比对父轮 rail 快照模型与本次 config 模型，不一致则在**存储换绑抹去证据之前**把旧模型戳进持久化 config 的 `_resumeModelSwitchFrom`——标记随 attempt 配置耐久存续，detached worker 也拿得到。
  ③ **task_start**：见到标记即在 bind 之后、spawn 之前向 attempt 事件流发一条 `model_fallback`（`fallbackKind=user_switch`，from=旧模型，to=新模型），并同步戳 `_fallback_from/_fallback_model/_fallback_kind` 任务字段——复用既有 MODEL_FALLBACK 折叠与横幅管线，前端零新事件类型。
  ④ **前端**：`turn-command.ts` 新增 `resumeCtxInputUpdate`（活快照浅拷贝 → `{contextSnapshot:{blockId:'turn-context',snapshot}}`，无快照返回 null 整体省略 inputUpdate），`_operateNativeTurn`/`_answerHumanGuidanceLate` 发送时挂上；`error-presentation.ts` + 双 locale 增 `user_switch` 文案（中「已按你的要求切换模型」/ 英 "Model switched at your request"），轮块内出现切换提示行。
- **规格（+7 针）：** `test_turn_lifecycle.py` 3 针——ctx-only 换绑只动 rail 且父轮 revision +1、content 改写仍拒、换模型戳标记/同模型不戳（服务级，stub start_task 取证 config）；`test_task_start_ownership.py` 2 针——标记在则 bind 后 spawn 前发 user_switch 事件且任务字段齐、无标记零事件；`test_frontend_turn_command_vite.py` 1 针——helper 浅拷贝防后发突变、null/数组快照返 null。另修复 `test_turn_activity_timeline.py` 既有红：`model_fallback` schema 的 fallbackKind/Reason 早已 required 而旧针未同步（checkpoint c88182604 遗留漂移，与本改同域顺手补齐）。
- **验证：** lifecycle 新 3 针 + 全套、task_start_ownership 7、turn_command_vite 4、turn_activity_timeline 32、resume_executor_config/attempt_stream_vite/turn_failure_error_ui/turn_store_owner_migration 邻套全绿；ruff 净；`check:frontend` 全链绿（166 节重组 + 32 样式节重组 + 119 assets 发布）；`verify:frontend` 通过。生效需硬刷新。

### 2026-09-08 (探测矩阵假红根修：思考块跨方言全部计入「已生成输出」，不动 64 token 探测预算)

- **起因（用户裁定）：** 检测矩阵 339 格中 26 格红「返回无有效内容」却全部 HTTP 200。持久化快照（`data/config/probe_cache/95a225612ce5cf84.json`）取证：17 格响应体明确为 `content:[{"type":"thinking"…`（glm-4.5-air/4.6/5-turbo、MiniMax-M2.x），8 格截断在同类思考系模型（LongCat-2.0、glm-4.5-flash/v），1 格 `content:[]`（gemini-3.1-pro-preview）。用户裁定：预算不加，做根本性修复。
- **根因：** 2026-08-18 事件只为 OpenAI 方言补了 `reasoning_content`/`reasoning` 回退；Anthropic 方言验证器（`_chat_json_text` 经 `_content_text` 只认 text/output_text 部件）从未获得等价物——思考原生模型在 64 token 探测预算下把全部 token 烧进一个 thinking 块（stop_reason=max_tokens），被判 invalid_response 且部分格带 `recommend_disable:true`，假红不只是展示问题。
- **修法（`lib/provider/provider_probe.py`，预算不变）：** 新增 `_reasoning_text` 部件表提取器（thinking/reasoning 部件的 thinking/reasoning/text 键 + redacted_thinking 的 data）；`_chat_json_text` 三个方言分支全部接入——Anthropic content 思考块、Responses output 中 reasoning 项（summary 未物化时 id/encrypted_content 即为物证）、OpenAI message content 内的思考部件（网关映射形态）；`_sse_generated_text` 增认 Anthropic `thinking_delta`/`signature_delta`。`content:[]` 零部件响应（gemini 网关缓冲思考后被截断的形态）保持诚实红，加针钉住。
- **规格：** `tests/provider_probe/test_probe_cells.py` +7 针——Anthropic thinking/redacted_thinking 块判 ok、空 content 仍红、Responses reasoning 项判 ok、OpenAI 思考部件判 ok、SSE thinking_delta 判 ok。
- **验证：** 本套 32 绿；邻套 probe 九套共 93 绿；ruff 净。`docs/MODEL_REGISTRATION.md` 探测裁决段已写明「推理输出全方言计入、预算不得上调掩盖」。既有红格需重跑矩阵探测翻绿（gemini-3.1-pro-preview 的 content:[] 格预期仍红——该网关形态下 64 token 内无任何可观测输出）。

### 2026-09-08 (模型选择下拉 tofu 主题精修：圆角暖影容器 + 暖色选中卡 + 胶囊服务商 chip；base 选中态改内描边)

- **起因（用户截图+裁定）：** 模型下拉在 tofu 主题下视觉粗糙——2px 硬斜面边框方角容器、当前模型行是一块无圆角的灰板（base 的 `outline:1.5px` 在浅米底上对比过低）、每行右侧「自动服务商」是重边框原生 select、section 小标半透明发虚。用户要求更精致并参考主流设计。
- **修法（纯 CSS，两处源文件；行为零改动）：** ① `13-tofu-theme-composer-tools.css` 下拉段重写：容器 4px 方角斜面边 → 12px 圆角 + 单条 1px 暖灰发线 + 双层柔和暖影（Linear/Raycast 式浮层）；行 radius 2→8、hover 淡暖米；`.active` 灰板 → 暖米 22% tint + 1px 内描边细环 + 深棕加粗 label + 琥珀色品牌 icon（主题既有 `.selected` 语言对齐）；`.ps-dd-provider-select` 重输入框 → 奶油底胶囊 chip（8px 圆角、细边、hover 加深）；section 小标/深度标题去 opacity 改实色 `#A89B7B`；sticky 搜索条与深度页脚背景/边线对齐 tofu 面板色，并把负边距/吸附偏移从 5px 改为与新容器 padding 一致的 6px（否则 1px 露底）。② `05-paper-report-media.css`（全部主题受益）：`.preset-dropdown-item.active` 的 outset `outline` 改 inset `box-shadow` 细环——圆角行上的外描边永远画成错位矩形，内描边才贴形；附注释说明 tofu 覆盖色。
- **参考的设计取向：** Linear/Raycast 命令面板的选中行（accent tint + 细环而非整块灰）、Notion 式 9px 大写稀疏字距分区小标、Claude.ai/Cursor 式圆角浮层 + 胶囊次级控件；全部映射到 tofu 既有黏土暖色板（#F4F2EB/#C2BCA4/#5C4A33/rgba(196,149,106,*)）。
- **验证：** 无新规格（纯表现层）；`check:styles` 32 节组成校验通过、`test_style_composition.py` 2 绿、`check:frontend` 全链绿（119 assets 发布）、`verify:frontend` 通过。视觉走查：`lib/browser/preview.py::render_page_preview` 直调 + `inspect_image` 截图核对（harness 复刻 `main_toolbar_ui.js` 行结构：搜索条/最近使用/分区/5 行含 active/hint/深度页脚）——桌面 1000px 宽完整渲染确认深度页脚与 Max 金块按钮；520px 宽触发 ≤900px 移动断点 `ps-dd-depth-wrap{display:none!important}`（既有行为，非回归）。生效需硬刷新。
### 2026-09-08 (模板问候拦截误报根修：用户真说「hi」被重试 2/2 —— tofu-context 信封残留伪装成非闲聊)

- **起因（用户截图+裁定）：** 用户只发了一个「hi」，Agent 轮却打出「⚠️ 上游返回了与任务无关的模板问候（30字符），重试中(2/2)…」。canned-greeting 检测器的互补闸 `last_user_is_smalltalk` 本应判「用户自己说了闲聊 → 问候合法不重试」，生产中却恒判非闲聊。
- **根因（两层）：** ① Context Composer 的托管块以 `<!-- tofu-context:ID:start/end -->` HTML 注释信封包裹 `<system-reminder>` 体（`_render.py::_envelope`），`_user_own_text` 只剥 reminder 体，信封标记残留；② 托管块经 `_append_tail` 作为**独立尾随 user 消息**追加，反向扫描第一条 user 消息就是载体而非用户的「hi」——残留标记 >30 字符且不匹配闲聊正则，互补闸永假，合法问候被误判为上游模板工件并烧掉两次重试。`project_context` 信封内连 reminder 都没有，同理。2026-08-02 的 reminder 剥除修复只覆盖了拼进用户消息内的形态，没覆盖独立载体 + 信封形态。
- **修法（`_canned_greeting.py`，纯决策逻辑）：** 新增 `_TOFU_CONTEXT_ENVELOPE_RE` 整信封剥除（含无 reminder 的 project_context 形态）；`last_user_is_smalltalk` 反向扫描跳过剥除后无用户文字的 user 消息（注入载体/纯 tool_result 块），继续回溯到最后一条用户自己说的话——carrier 不再当 alibi，真实工作尾（_WORK_TAIL/kickoff/纯载体）仍判工件重试。autopilot VU 守卫共用同一函数，自动受益。
- **规格：** `test_canned_greeting_retry.py` +6 针（2c 节）——composer 尾随载体后「hi」不拦截、载体不掩护真实工作尾、无 reminder 的 project_context 信封、sanitize 剥私有字段后的 wire 形态、纯载体无用户轮仍判工件、analyse 级重放截图形态 action=break 且零重试。
- **验证：** 本套 33 绿；邻套 context_composer/same_role_carriers/abnormal_terminal/autopilot_state/suspicious_short/discard_receipt 共 154 绿；ruff 净。`test_empty_stop_not_content_filter.py` 两红为并行会话 `llm_fallback/_retry.py` 重写（删 round≠0 闸）未同步规格的移动靶，与本改无关。

### 2026-09-08 (前端三修：编辑拒写卡简报化 + 调度轮补齐 turn ctx 栏 + ctx 栏滚动吸附至下一轮)

- **起因（用户截图×2+裁定）：** ① 工作区外编辑拒绝卡顶栏重复展示 110 字符截断路径 chip、每条 edit 还整段倾倒 OutsideWorkspaceError 全文——用户裁定太笨拙，「在项目外就简单提一句」；② 定时代理（Proactive Agent）输入轮没有任何 turn ctx 栏——人工轮的 `_ctx` 由浏览器发送时 info-rail.js `buildTurnCtxSnapshot` → `payload['ctx']` 提供，调度轮服务端 `dispatch_scheduled_turn` 直发不经浏览器，投影无 `_ctx`，栏/折叠行无从渲染；③ 用户要求会话滚动时 ctx 栏跟随移动，直到下一轮。
- **修法一（拒写卡，`frontend/src/conversation/presentation/write-gate-refusal.ts` + `tool-result-presentation.ts`）：** 拒写卡渲染单条本地化简文案 + gate 徽标，抑制顶栏截断路径 chip、逐条详情换成一行 brief；模型侧 `[N] FAIL` 行保留全文不动（携带 re-issue 指令）。非拒写失败保持既有 chip + 全文路径。
- **修法二（调度轮 ctx，`lib/scheduler/_shared.py`）：** 新增 `build_turn_ctx_snapshot`——从合并任务配置派生与前端契约同形的快照（model、tools label/tone 表、roots；depth/modes 留空由 done 帧 fact 卡落锤），dispatch 时随轮写入；`normalize_projection_document` 走与人工路径相同的 `_ctx`→`contextSnapshot` 映射，调度轮由此与人工轮同构一轮一栏。
- **修法三（滚动吸附，`info-rail.js` + `17-compaction-context-rail.css`）：** rail 本是 `.message` 内绝对定位在轮顶的脱离流盒子，输入轮常只有一行高，滚动时随气泡滚走、下方回复填满几屏。新增 sticky follower：scroll/resize 时按 `off = clamp(stickTop − hostTop, 0, nextRailTop − railHeight − hostTop)` 平移每条 rail——钉在滚动窗顶部、下一轮的 rail 到达时被连续顶走；无 rail 的轮（历史遗留）不作边界，上一条 rail 保位到下一条有 rail 的轮。几何全部从未变换盒（宿主 rect）测量，已施加的 transform 不会回馈进下一帧；每动画帧至多跑一次。travel 使 rail 越出宿主 border box，故宿主 `.message:has(> .turn-ctx)` 放弃 content-visibility 的绘制包含；`.chat-inner` 经 ResizeObserver 覆盖流式增长与侧栏/抽屉缩放两个 scroll 不覆盖的触发源。
- **规格：** 新建四套 13 针——`test_outside_workspace_card_vite.py` 2 针（拒写卡无截断路径 chip/有简文案徽标，非拒写失败保留 chip+全文）；`test_scheduled_turn_ctx.py` 7 针（快照形状与前端契约、调度投影出 contextSnapshot）；`test_turn_ctx_rail_sticky.py` 1 针 + `test_turn_ctx_rail_geometry.py` 3 针（跟随公式/边界/无 rail 轮几何扫描）。
- **验证：** 四套 13 绿；ruff 净；`check:frontend` 全链绿（119 assets 发布）；`verify:frontend` 通过。生效需硬刷新。

### 2026-09-08 (tofu-db 认证矩阵真实挂载 cell 落地：beegfs-fuse 6/6 全绿，挂载取证缺口关闭)

- **目标（上线标准 §pre-authority 硬门收尾）：** 上一条目遗留「mount.opt-in cell 需操作者显式网络/FUSE 挂载路径」的真实挂载取证缺口，本机主工作区即 beegfs-fuse 挂载，直接补证。
- **证据（机器可读，tee 留存）：** `TOFU_DB_CERT_MATRIX_MOUNT_DIR=<beegfs 挂载下空目录> packages/tofu-db/target/release/tofu-db certify-filesystem-matrix`——mount.opt-in cell result=pass（跨进程写→重开→读回，authority_uuid:c84de894-…，durable_sequence:3，retained 7 files/2,106,468 bytes，128ms）；local.native 与 vfs-fault 四 cell 同轮复跑全 pass；`matrix_result=pass cells=6 passed=6 failed=0 skipped=0`。输出与挂载 store 证据留存于 `mount-beegfs-20260908/`（工作区外操作者证据目录）。
- **过程排障：** 首轮失败报 `tofu-db: No such file or directory (os error 2)`——非二进制缺失，是我把 `--data-dir` 指向的本地空目录误 `rmdir`，矩阵要求该路径**存在且为空**（与挂载目录同约）；重建后一次通过。
- **文档同步：** `docs/STORAGE.md` 缺失清单删去「representative network/FUSE mount cell」项，耐久性证据段改记 beegfs-fuse 挂载 cell 已于 2026-09-08 通过（6/6），其余挂载类型仍需操作者供路径。
- **仍缺（更新后清单）：** TofuSQL、迁移域扩展（credential/billing/record 之外随操作稳定入注册表）、块加密、correlated 多 sync 丢失 + extended rotation 认证、签名发布打包、Supervisor 选择；30 天 shadow run 要求未动。下一道硬门按 STORAGE.md 顺序为块加密设计/实现。

### 2026-09-08 (tofu-db 迁移门 + 认证矩阵落地：integrate 合并、迁移认证三域全绿、全量规模 GC spill 认证转绿)

- **目标（上线标准 §pre-authority 硬门）：** 推进迁移门与认证矩阵两道硬门。拓扑事实：`tofudb/par-mig` 已完全含于 `tofudb/integrate`，单分支合并即得迁移器 + 矩阵。
- **封存先行：** 主工作区 335-op 语义移植此前全部未提交且与大量兄弟 WIP 混杂；选择性提交 `6a2c96880`（packages/tofu-db 全量 + IR 契约 + 生成器 + 两个 tofudb 测试 + CHANGELOG/JOURNAL/STORAGE 本工作流 hunk），并将 3394 个被误跟踪的 cargo target 构建产物（target-differential/target-worker3642）`git rm --cached` + `.gitignore` `target-*/` 根治仓库卫生。
- **隔离合并（worktree `tofudb/merge-gate`）：** merge-base 即 integrate 首父，合并面极小。两处冲突：① `backup_gc.rs` 双方在同锚点后各加 spill 测试——取并集（我方 release 全量认证 + 对方预算针/4-run 缩放执法），冲突边界切断函数闭括号一处已修；② `scripts/tofudb_v2_client.py` add/add——我方为严格超集（仅多 native CRC32C 加速，公开 API 逐符号一致），取我方。合并 diff 纯增量 730 行，queue.rs/turn.rs/semantic_executor.rs/generated_tofudb_ir.rs/IR 契约与合并前逐字节一致。
- **合并暴露的深层问题（根修非回退）：** ① 语义移植提交带了生成物（IR 335/注册表 40）但漏了其 Python 注册源——merge worktree 重生成 331≠335 parity 红、`gen --check` 红。补登 Python 侧注册集（queue.injection.ack handler+capture+注册+常量、model_continuation 三操作注册、turn.queue.activate/cancel 免 receipt 与 turn.cleanup receipted 翻转），重生成与已提交 .rs 逐字节一致。② 差分门 9 红：committed Rust 认证所依据的 legacy Python 行为在 storage_sidecar 脏文件里，逐一补齐 storage 域 ops/codecs + background_command_projection/delivery + conversation_sync_v3.yaml（BackgroundCommandCompletion schema）后 153/153 全绿。③ `spill_set_certifies_full_budget_scale` 自出生即红（#[ignore] 从未入门禁）：insert 在 IDS_PER_RUN 急切 flush，第 512 个 run 由补满 insert 拒绝而非 finish()；对齐实现后 **release 全量规模认证转绿**——33,488,896 marks/1 GiB 预算内、逐分片精确回读、预算精确拒绝（6.45s）。
- **认证矩阵证据（对本树计数）：** `tofu-db certify-filesystem-matrix` 机器可读输出——local.native 跨进程 cell + vfs-fault 四 cell（syscall-error interrupted/storage-full 各 88 case、short-write 13、lost-sync 37）全 pass；mount.opt-in cell 需操作者显式网络/FUSE 挂载路径，仍为真实挂载取证缺口。correlated 多 sync 联合丢失与 extended rotation 经代码核查（certification.rs/matrix 无 pair/rotation 逻辑）确认仍未认证，保留在缺失清单。
- **迁移认证（验收标准逐项）：** `tests/tofudb/test_tofudb_migration.py` 3/3——真实 legacy SQLite authority 经生产 Sidecar 操作填充（credential 四态/billing 双钱包多条目/record CAS 梯）→ 真 daemon 端到端迁移，逐域精确 count/digest 校验 + 机器可读 JSON 报告 + legacy DB sha256 字节级不变（回滚=继续服务 legacy）；重跑幂等收敛；`TOFUDB_MIGRATE_FAULT_AFTER_OPS` 故障注入中断后 resume 收敛。
- **最终门禁（merge-gate 分支）：** cargo test 596 绿 · clippy --all-targets -D warnings 0 错 · fmt --check 净 · pytest（pre_authority+differential+migration）153/153 绿 · release --ignored 全量 spill 认证绿。
- **仍缺（更新后清单）：** TofuSQL、迁移域扩展（credential/billing/record 之外随操作稳定入注册表）、块加密、correlated 多 sync 丢失 + extended rotation 认证、真实网络/FUSE 挂载 matrix cell、签名发布打包、Supervisor 选择；30 天 shadow run 要求未动。兄弟工作区的 STORAGE.md/JOURNAL 交错 hunk 未纳入本分支（归属各自工作流落地）。

### 2026-09-08 (tofu-db 语义移植封板：`queue.injection.ack` 上线，335/335 操作全集可执行 + 三处既有分歧收敛 + 差分构建环境根修)

- **目标（上线标准推进）：** IR 契约与 legacy 目录比对显示 `queue.injection.ack` 是最后一个 default-denied 操作（catalog 335，IR executable 334）。按既有移植认证标准落地：契约先行（`contracts/tofudb_ir_v1.json` executableOperations + 生成器重出 `generated_tofudb_ir.rs`）、executor 编译臂、`queue.rs` 域执行、差分脚本、单测、穷举故障注入。
- **语义（与 legacy `_queue_injection_ack` 逐点对齐）：** 单命令至多排空 256 个队列身份；仅删除**未被接受的** peer_msg（`_peerMessage` 真且非 `_peerHuman`）与 workflow_step（`_backgroundCommand` 非空串）收件行——已绑定 input/output/attempt 的 Turn 对行绝不被取消；忽略未知/重复身份；删除后按 (priority, position) 重编号，回执按 legacy (position, id) 顺序报告；同事务发布一条**不带 conversationRevision** 的 `turn.upsert` sync 事件（payload 仅 `{turns:[],attempts:[],removedQueueIds}`，与 legacy capture 形状逐键一致，会话头部 revision 不前进）；空排空不发事件。
- **差分探针暴露并收敛的三处既有分歧（非本次引入）：** ① Rust `enqueue_item` 在 kind=real 时无条件重编号，legacy 仅在真实取代 goal_continuation 后才重编号（`_queue_enqueue` 的 `if deleted:`）；② Rust `Clear/KindClear` 无条件重编号，legacy `_queue_kind_clear` 同样 `if deleted:` 才重编号——create_pair 的 human 路径内部调 KindClear(goal_continuation)，空扫也重编号，导致 injection.ack 的 (position,id) 报告顺序与 legacy 分歧；③ `public_item` 丢 `backgroundCommand`（commandId≤128、command/resultPreview≤2000 字符、超长截断并置 resultTruncated）与 `steerFallback` 预览字段。三处均按「向 legacy 收敛」修复并加针。
- **差分 harness 环境根修：** `_resolve_tofudb_binary` 用 `--manifest-path` 从仓根调 cargo，而 cargo 从**工作目录**（非 manifest 目录）发现 `.cargo/config.toml`——包内配置的 devtoolset-8 C 工具链钉（libmimalloc-sys 必需，基础镜像 gcc 4.8 会失败）不生效，target-differential 构建静默失败后回退到**陈旧的** target/debug 预建二进制，差分门由此可能在测旧代码。修为 `cwd=manifest.parent`；此次实测证明该陷阱为真（修复前差分跑的是缺三个修复的旧二进制）。
- **规格：** 差分脚本 `queue injection ack drains only never-accepted inbox rows`（六类行过滤矩阵 + 非列表/空串/257 身份校验 + 空排空幂等 + (position,id) 报告顺序钉 + 重 ack 不发事件 + 结尾 queue.clear 防模块域 legacy 权威全局馈残留污染邻脚本）；单测 `queue_injection_ack_drains_only_never_accepted_inbox_rows`（含 create_pair 绑定行不被取消、revision 不变、sync 事件无 conversationRevision）；穷举故障注入 `every_queue_injection_ack_commit_fault_recovers_one_atomic_prefix`（每 op 中断 + 每写短写，恢复只许完整前/后两态）。
- **验证：** cargo test 574 绿（6 target 含故障注入族）；`cargo clippy --all-targets -- -D warnings` 0 错（顺手修复 WIP 引入的 9 个 rust-1.98 新 lint 错：authority_gc unwrap-after-is_some、五处 chunks_exact→as_chunks、fsync_stats 内部可变 const→inline const 数组初始化、turn.rs 8 参数函数 allow、一处 needless borrow，外加 turn_append_bench 示例 manual_checked_ops 与两处 fmt 漂移）；`cargo fmt --check` 净；`test_tofudb_pre_authority.py + test_tofudb_differential.py` 150/150 绿（含 IR 契约再生成校验、目录奇偶 ratchet——ratchet 现探得全集可执行且每操作有差分覆盖）。
- **上线意义与仍缺（docs/STORAGE.md 硬门）：** 语义移植封板——335/335 操作可执行，「remaining operation payload compilers」从剩余清单划掉（STORAGE.md 已同步）。仍缺：迁移分支 `tofudb/integrate`/`tofudb/par-mig` 合并 + 多域迁移认证、真实挂载故障/规模矩阵（correlated sync-loss、extended rotation、GC spill scale，须对实际发布树取证）、块加密（STORAGE.md §92 明示缺失）、TofuSQL、签名发布打包、Supervisor 选择。性能侧：后台打包器已把 create/append p99 压到 ~6ms（见下一条日志），turn_append O(N) 随会话增长的结构性项与 search 投影完整度成本（倒排索引立项）仍开。

### 2026-09-08 (tofu-db 后台打包器上线就绪：create/append p99 20ms→6ms；搜索“回归”查明为陈旧投影假象)

- **背景：** tofu-db medium 规模 perf 对比中 `conversation_create_burst`/`turn_append_burst` p99 飙到 ~20ms——rotation 提交在提交路径上同步打包 staged 块（8MiB 段写 + fsync）。本次工作为上线前最后一项性能攻坚。
- **修法（后台打包器）：** 新建 `packages/tofu-db/src/background_pack.rs`——受监督 worker 预留 ≥1MiB 的 staged 批次，在不持有 authority mutex 的情况下构建并 fsync 8MiB 段文件；提交路径（`Engine::adopt_background_pack`）只发布 manifest 引用并退役 staged 副本。worker 失败一律让 payload 保持 staged，同步 checkpoint drain 仍是完整确定性兑底；in-flight create 超过等待预算则放弃预留改同步重打包。`daemon.rs` 接线并加 `TOFU_DB_DISABLE_BACKGROUND_PACK` 环境开关作运维逃生门；`TOFU_DB_FSYNC_STATS=1` 关闭时输出 stage_report + packer_metrics。
- **验证：** 全量 Rust 测试 572 绿；medium perf 两轮（e/e2）create p50 0.64ms/p99 5.4ms、append p50 1.41ms/p99 6.2ms（原 p99 ~20ms）；packed_bytes ~615MB/211 段由 worker 异步完成。
- **搜索延迟“回归”调查（结论：非回归）：** e/e2 中 `conversation_search` p50 从 0.16ms 升到 3.77ms。A/B 证据链：① 全套件禁用打包器 → 0.16ms；② 但探针（`scripts/tofudb_search_probe.py`）在**语料完全相同**（2000 create + 1000-turn 热会话 + 40 文档）时，开/关打包器搜索延迟一致（6.98 vs 6.81ms）；③ 套件中“快”的运行测的是**未排空的陈旧投影**（search worker 尚在排空写突发，索引里几乎没有数据——此时搜热会话词会漏结果）。`conversation.search` 是有界线性 header 扫描设计，延迟 ∝ 已索引语料；投影完整后 medium 语料 ~4–7ms/查询。打包器让 worker 跟上提交、投影保持完整，从而暴露了真实成本。后续若该成本用户可感，上真正的倒排索引。
- **文档：** README 增后台打包器段落（含逃生门开关）并改写 perf 现状段（create/append 不再是 deficit；search 延迟与投影完整度的关系已注明）。

### 2026-09-07 (编辑工具失败分类：edit_file/apply_diff/insert_content 全线错误带类别标签)

- **起因（用户裁定）：** edit_file 失败现在会直接抛原因（好），但所有失败都是同构纯文本，用户/模型无法一眼分辨是哪一类——要求分类（例：串匹配失败）。
- **修法：** ① `write_tools/_ops.py` 新增九个 `EK_*` 类别常量（`match-not-found`/`ambiguous-match`/`file-not-found`/`path-invalid`/`read-failed`/`write-failed`/`echo-content`/`mapping-failed`/`invalid-edit`）+ `_fail_tag(result)` 渲染助手；所有编辑面失败 dict 携带结构化 `errorKind`（含 `_finalize_insertion` echo 错误经 `_compute_insert` 透传）。② 三条批量面（edit_file/apply_diffs/insert_contents）的每行 FAIL 与预检失败（缺字段/非法 operation/position/条目）嵌入方括号标签：`[1] FAIL a.txt [replace] [match-not-found]: …`。③ `tools.py` 单编辑包装器渲染 `Diff failed [match-not-found]: …`——标签在前缀**之后**，`Diff failed`/`Insert failed` 前缀与 `[N] FAIL` 批次标记不变（read-gate `_result_indicates_success` 与 meta.py `'] FAIL' in line` 解析契约双双保持）。
- **规格：** 新建 `tests/project_mod/test_edit_error_kinds.py` 14 针——五类 edit_file 标签、两条 legacy 批量面、单编辑 dict `errorKind`、tools.py 包装器前缀+标签、read-gate 探针对带标签文本仍判失败、meta 解析器同时兼容带/不带标签行。
- **验证：** 新规格 14 绿；邻套 edit-file-unified/read-gate/write-tools×3/desktop-agent/schema 六套 189 绿；ruff 净。
- **教训（已存记忆）：** edit_file schema 描述有硬 token 预算（kimi-k3 ≤385，实测 378，余量 ~7）——补一句失败标签说明（+51 token）就把 `fit_tool_schema_budget` 投影整个工具丢掉（assert []），已回退；类别标签靠输出自解释，不进 schema 描述。`test_project_write_tool_schemas.py` 两红（write_file 400>360 + oneOf 投影）为并行会话 append 参数 WIP 的移动靶，与本改无关。

### 2026-09-07 (Codex 用量重置「立即重置」按钮上线：前端兑换通路 + 幂等消费命令)

- **起因（用户截图+裁定）：** 订阅登录卡的「已获得用量重置机会」框只展示信息，提示语还写「通过 Codex 的 /usage 兑换」——用户问是否缺了重置按钮，裁定补上。
- **设计：** Tofu 永不自动消耗；兑换必须是显式确认、幂等的命令。按钮只出现在 fresh available 卡上（stale 投影不渲染），点击经浏览器确认对话框 → `POST /api/v1/oauth/reset-offer/consume` → 结果 alert + 强制刷新状态卡。
- **修法：** ① `lib/oauth/codex_usage.py` `consume_codex_usage_reset`——owner/account 刷新锁内重读 credit details、选最早过期 available credit、上游 POST 前先把 `redeem_request_id`（UUID）持久化进缓存：歧义结局（传输错误/5xx/未知 2xx）重试复用同一幂等键（24h 窗、仅同 credit），确定性结局（`code=reset` / `nothing_to_reset` / `already_redeemed` / 4xx）清除记录；每次尝试结束强制 refresh + owner-scoped push，响应携带新 `reset_offer` 投影。② `routes/api_v1/oauth.py` 新路由，错误码映射 401/409/502，成功写 `oauth_codex_reset_consumed` 审计。③ 前端：`api.js` 加 `resetOfferConsume`；`oauth.js` `_renderOAuthResetOffer` 渲染 `btn-small btn-primary` 按钮（继承 tofu 金块语言）+ `_oauthConsumeReset` 处理器（in-flight 防重、按错误码映射gone/unresolved/failed 文案）；manifest runtimeExports + i18n en/zh 各 6 键 + hint 文案改为「在此点击或通过 /usage 兑换」。
- **规格：** `test_codex_usage_reset.py` +11 针（最早过期选择、幂等键复用/换 credit 换新键、三类确定性结局清 pending、5xx 留 pending、无 credit/详情故障不发 POST、未认证跳过 settle、账号变更零网络、路由 envelope + 五档错误码映射 + 审计纪律）；`test_codex_usage_reset_notice.py` 设置卡 harness 13→19 针（按钮存在/样式、stale 无按钮、确认拒绝不调 API、成功 alert+刷新、gone 文案映射）。
- **验证：** 两套 42 绿；邻套 egress/device-flow/density/button-language/api-contract-drift 51 绿；ruff 净；styles 重组（32 sections）+ `generate:runtime`（166 sections，`runtimeScope._oauthConsumeReset` 已导出）+ `generate:i18n` + `check:frontend` 全链绿 + `verify:frontend` 通过（119 assets 发布）。生效需硬刷新。

### 2026-09-07 (订阅登录页信息密度提升：按钮进标题行、email+egress 并一行、全卡间距收紧)

- **起因（用户截图+裁定）：** 订阅登录页信息密度太低——很多元素独占一整行（按钮一行、email 一行、egress 一行），页面冗长，要求做得更精致。
- **修法（`static/settings_panels/oauth.html` + `frontend/src/styles/settings/01-core-panels.css`）：** ① 结构——两张供应商卡的 `.oauth-provider-actions`（登录/断开按钮）移进 `.oauth-provider-header`，与图标/名称/状态徽标共享一行（`.oauth-provider-name{flex:1}` 天然右推，窄屏自动换行）；email 条 `.oauth-provider-info` 与 `.oauth-egress-line` 包进新增 `.oauth-meta-row` flex 行并一行。② 样式——卡 padding 16→12/14、间距 margin 12→10、步骤条/责任框/quota/reset-offer 内边距与外边距全面收紧（步骤圆 24→20、字号微调）；重复 `.oauth-provider-desc` 规则合并为一条。③ 隐藏组合纪律——meta-row 的外边距只挂在 `:not(:empty)` 上、子元素 margin 全归 0：JS 渲染器（runtime/sections/settings/oauth.js）用 style.display ''/'none' 按 id 开关这些元素，display:none 子项在 flex 行里不留 gap、全隐藏时整行塌陷不留杂散空隙。手动 fallback 区与按钮几何（38px/内边距）不动；非 tofu 主题无感知。
- **规格：** 新建 `tests/frontend/test_settings_oauth_density.py` 三针——两卡 actions 必须在 header 内针、email+egress 必须在 meta-row 内针、meta-row `:not(:empty)` margin 归属 + 子元素 margin:0 针。
- **验证：** 新规格 3 绿；邻套 button-language/save-feedback 13 绿、style-composition 2 绿；ruff 净；`compose_frontend_styles.mjs` 重组（32 sections，settings.css 已含 meta-row 规则）；`check:frontend` 全链绿（119 assets 发布）。生效需硬刷新。

### 2026-09-07 (Settings 面包按钮二扫：stg-btn-balance 金面包 + oauth 红/黑面包全部归一到 tofu 方块)

- **起因（用户截图×2+裁定）：** 「编辑系统提示词…」（金色渐变面包）与「断开连接」（红色渐变面包）都丑——上一轮只改了页脚按钮，卡片正文的组件类按钮仍是面包。
- **根因：** 这两类不是 `.btn .btn-primary`，全局 tofu 按钮规则够不到：① `.stg-btn-balance`（06-file-changes-settings-shell.css，全主题金色渐变面包 + 大写），另有 `settings-comfort.css`（Vite 注入，0-2-0）只拍扁边框/阴影、保留渐变；② 订阅登录卡 `.btn-small.btn-danger/.btn-primary/.btn-small`（settings 表 01-core-panels.css `.oauth-provider-actions/.oauth-manual-fallback` 作用域，0-2-0，加载晚于 styles.css）的红/黑渐变面包。
- **修法：** 12-tofu-theme-project-controls.css 追加 0-3/0-4 优先级 tofu 覆盖（压过后加载的 settings 表与 comfort 注入）：stg-btn-balance → #C9993F 金块；oauth btn-small → #faf8f3 奶白块、btn-primary → 金块（连接/继续/打开授权页一并归一）、btn-danger → #C0392B/#D0492F/#A93324 红块；全部 2px 墨边 + --radius-sm + clay 软影 + 去大写/text-shadow，hover/active 行为与全局 tofu 按钮逐值一致。几何（38px 高/6px 16px 内边距）不动。非 tofu 主题不变。
- **规格：** `tests/frontend/test_settings_button_language.py` +1 针——金块/红块/奶白块 token 存在针 + stg-btn-balance 与 oauth btn-small 规则体无 linear-gradient 针（选择器逗号分组的锚定坑已在针内处理）。
- **验证：** 规格 15 绿；ruff 净；styles.css 重组（32 sections）；`check:frontend` 全链绿、新 bundle 已发布。生效需硬刷新。
### 2026-09-07 (OpenBMB 服务商模板上线：MiniCPM 官方 API 四模型 + 砖墙徽章)

- **起因（用户裁定）：** 制造商侧昨日已入册（_creator_families/_creator_identity/前端品牌表），服务商侧仍缺模板；用户明确「只加模板，不动实时路由注册表」。
- **事实核查（官方一手源）：** MiniCPM-V 仓库 `docs/api.md`——官方托管 API 为 ModelBest 平台：`https://api.modelbest.cn/v1`（OpenAI 兼容，Bearer，key 在 platform.modelbest.cn 申请），在售模型恰四款：`MiniCPM-V-4.6-Thinking`（reasoning 经 `message.reasoning` 返回）、`MiniCPM-V-4.6-1B`、`MiniCPM-V-4.5-9B`、`MiniCPM-O-4.5-9B`，V/O 全系支持文本+图像+视频理解。定价/上下文窗口官方文档未载 → 按 stepfun 先例不杜撰，两字段留空、不打 cheap 标（pricing-tier 契约：无定价证据的 cheap 必须进白名单）。
- **修法：** ① 新建 `lib/model_info/data/openbmb.json`（brand/key=openbmb、category=official、name="OpenBMB (ModelBest)"、balance_url 指控制台，四模型 exact 官方拼写、capabilities 按文档能力集）。② 模板品牌徽章契约（`tests/providers/test_provider_template_brand_icons.py`）强制每品牌有 SVG+颜色，而 lobe-icons/simple-icons 无 OpenBMB 标——从 OpenBMB GitHub 组织头像实测几何逐块复刻：双色圆角砖墙（面壁=「facing the wall」），五行交替半砖错位，PIL 采样得 #315EFF/#00D3F0；`model-brand-icons.ts` 加 `openbmb` 图标（逐块硬编码 fill，继承规则见文件内既有注释）与品牌色，模型目录的 openbmb 品牌键自动脱离 generic 回退。③ `provider_template_feed.py` docstring 未映射名单补 `openbmb`（ModelBest `MiniCPM-*` 拼写 ≠ OpenRouter；2026-09-07 目录实测无 openbmb 行，feed 缓存 grep 零命中），不做 vendor 前缀映射防近重复注入。
- **验证：** 模板 live 加载+`compile_provider_template_bundle` 通过（4 offerings，creator 归因 openbmb）；模板六套件（recipes/brand-icons/feed/pricing-tier/world-catalog/deprecated-sku）+creator_identity 157 绿、brand-resolver/bootstrap-catalog 等 13 绿；ruff 净；`generate:runtime`（166 sections）+ `check:frontend` 全链绿 + `verify:frontend` 通过，新 bundle 已发布；PIL 重渲染徽章与官方头像目检一致。`test_developer_runtime_distribution.py::test_public_artifacts_share_one_release_version` 红为并行会话版本钉未同步（clients/python 0.17.0 vs 0.17.2），与本改无关属移动靶。
- **流程教训（已存记忆）：** 勿在本仓用 `git stash` 做「去掉新文件再测」的 A/B——untracked pathspec 静默不入栈，紧随的 `git stash pop` 会弹到并行会话的 stash 条目（共享栈），本次险些把 8-19 的 sidebar WIP 弹进工作区。
### 2026-09-07 (Settings 按钮语言统一：退役面包渐变/SNES 像素两套 Settings 专属皮肤)

- **起因（用户截图+裁定）：** 设置弹窗底部的「取消/保存」按钮是面包状渐变胶囊（金色渐变 + 大圆角 + 多层软阴影），丑；要求换成项目面板「取消/清空/应用更改」那一排的标准按钮样式。
- **根因：** 设置按钮与项目面板按钮本就是同一套 class（`.btn .btn-primary/.btn-secondary`），但 `12-tofu-theme-project-controls.css` 里有两层 Settings 专属覆盖把它们改造成了别的语言：① tofu 主题 SNES 像素斜角页脚皮肤（`[data-theme="tofu"] .settings-footer .btn-*`）；② 2026-09-04 确立的全主题「 tactile 面包块」（`.settings-panel[ .settings-footer] .btn-primary/.btn-secondary`，0-3-0 优先级压过全局 tofu 按钮，渐变大圆角大写）。
- **修法：** 两块整体删除（本裁定取代 2026-09-04 方向），Settings 按钮回落到全局 tofu 新粗野主义方块（#C9993F 金块/奶白块 + 2px 墨边 + 硬投影，与项目面板逐像素一致）；仅补一条 `[data-theme="tofu"] .settings-footer .btn{text-transform:none}` 抵消 07 全主题基底的 uppercase 残留。非 tofu 主题视觉基本不变（07 基底渐变与 tactile 块相同）；系统提示词编辑器页脚同为 `.settings-footer`，自动受益。07 中指向 12 的过期注释同步改写。
- **规格：** 新建 `tests/frontend/test_settings_button_language.py`——六条退役选择器不得回归针 + tofu uppercase 复位存在针。
- **验证：** 新规格 2 针 + 邻套 save-feedback/style-composition 12 绿；ruff 净；`compose_frontend_styles.mjs` 重组 32 sections，`check:frontend` 全链绿（119 assets 发布）。生效需硬刷新。
### 2026-09-07 (本地 TeX 系统真实渲染三连根修：章节落页错位 + 充盈度虚标 + CJK 字体克隆缺失)

- **起因（用户要求）：** 用真实机器渲染核查本地 TeX 写作系统（`.texlive` + `lib/paper/latex_layout.py`）的前后端设计并优化。三份探针文档（pdflatex 短文档、pdflatex 多页短末页、xelatex ctex 中文）实编译暴露三个真 bug。
- **根因与修法：**
  ① **章节落页错位**——TeX 把整段 hbox 统一打上段落*结束*时的行号；`\section` 紧接段落（无空行）时与前段共享行标签，`by_line` 取首箱 → 映到前段所在页（实测 "Two" 标题在 p.2 却报 p.1，pymupdf 文本层实锤）。修：`parse_synctex` 新增 `hbox_last_by_line`（每行最后一个 hbox——标题箱恒在同行前段箱之后），`_page_for_line` 精确/最近行查找经 `_box_for_line` 优先取末 hbox；`by_line` 首箱语义保留（既有规格钉死）。
  ② **末页充盈度恒 ~100%**——输出例程的正文容器 vbox（`[`-开）每页都报 V+depth=正文底，页脚容器更在其下，与内容多少无关。修：`page_box_vs`/`page_bottoms` 排除 `[` 容器记录。
  ③ **xelatex 幽灵空箱 + CJK 克隆缺失**——xelatex 流里输出例程留下仅含 kern 的空 hbox（无字形），嵌套/叶箱过滤无法与真实文本行区分；且 ctex `fontset=windows` 直接编译失败（`.texlive/fonts/` 只有 Latin 克隆，无 SimSun/SimHei 等 CJK 克隆）。修：充盈度改以 **PDF 可见内容为权威**——`_content_bottom_from_pdf`（pymupdf 软依赖，文本/图像块+矢量绘图，页脚经 geometry text_bottom 滤除，空正文页回 text_top），失败回退 SyncTeX 叶箱估值；`end.bottom_source` 标明 `pdf`/`synctex`，CLI 输出同步。CJK 克隆：新脚本 `scripts/make_texlive_font_clones.py`（fonttools 重写 name 表，23 枚 Latin+CJK 克隆，`recalcTimestamp=False` 保字节确定、重跑 no-op，`--check` 校验家族名），生成后 ctex/xelatex 真实编译通过。
- **规格：** `tests/paper/test_latex_layout.py` 20→25 针——共享行标签末 hbox 优先针、容器 vbox 充盈排除针、PDF 兜底 fail-closed 针、两条真实编译 e2e（无空行标题落页 p.2 + 末页 <90%；xelatex ctex `bottom_source=='pdf'` + 充盈 <60%）。
- **验证：** 25/25 绿；ruff 净；三探针真实重渲染——probe1 100%→58.6%、probe2 "Two" p.1→p.2 且末页 100%→13.7%、probe3 xelatex 中文 94.2%→46.7%（v=336.511 与 pymupdf 实测 336.5 一致）；pdflatex 下 pdf 与 synctex 两源互证（58.6/58.3、13.7/13.7）。克隆生成器二次运行 0 written。
- **遗留（设计缺口，未动）：** 本系统暂无模型侧工具面——模型只能经 run_command 调 CLI；overleaf-mcp 的 compile/page_map 工具仍走 MCP。是否注册本地 tex 工具（front-end 面）属产品决策，待用户裁定。

### 2026-09-07 (OpenBMB/MiniCPM 开发者家族入册 + AA 指数创作者别名两缺口根修)

- **起因（用户提问）：** 模型制造商/服务商名单里没有 MiniCPM 团队（OpenBMB/面壁智能）——全库四层（live 注册表、后端 `_creator_families` 分类、前端 `model-brand-detection` 字形表、`vendor.ts` 标签表）均零命中；用户同时要求核查 AA 指数侧是否有团队缺失。
- **AA 侧核查（live 缓存 643 行 × live 注册表 508 模型逐行投影）：** 上游 Artificial Analysis 本身有 OpenBMB（3 行 MiniCPM）；真正缺的是**别名桥**——AA 把 26 行 Grok 记在 `SpaceXAI` 名下、把 Ling/Ring 记在 `InclusionAI` 名下，而家族品牌表无对应键，`_score_matching` 的创作者过滤把整族行全部滤掉：xai 13 个模型 0 分、bailing 的 Ling-1T 0 分。其余 20 个创作者均有 AA 覆盖；upstage 唯一未中（`solar-10.7b-instruct`）是真身份未中（AA 只测新 Solar 系），exact-identity 纪律不动。
- **修法：** ① `_creator_families.py` 新增 openbmb 家族三表（providers `('openbmb',)`、brand keys `('openbmb','minicpm')`、id 前缀 `('minicpm',)`）；xai 品牌键补 `spacex`（覆盖 spacexai）、bailing 补 `inclusionai`。② `_creator_identity.py` 命名空间正则与 `_NAMESPACE_FAMILIES` 补 `openbmb`（`openbmb/minicpm-*`、`siliconflow/minicpm-*` 等转发 id 可剥离可归因）。③ 前端 `model-brand-detection.ts` 补 `openbmb/minicpm` 字形键与 `minicpm|openbmb|面壁` 模式；`vendor.ts` 补标签 `OpenBMB`。图标有意回退 generic——lobe-icons/simple-icons（项目既定 SVG 来源）均无 OpenBMB 官方标，不杜撰。
- **规格：** `tests/model_catalog/test_creator_identity.py` +5 针——`openbmb/MiniCPM-V-4.6` 剥离针、3 条归因针（含 siliconflow 转发 id）、新 `test_match_model_bridges_external_creator_spellings`（SpaceXAI→xai、InclusionAI→bailing、OpenBMB→openbmb 三桥 + meta 错配不中的反针）。
- **验证：** 直接规格 97 绿；邻套 catalog/pareto/identity/bootstrap/brand-resolver/picker 123 绿；ruff 净；`generate:runtime`（166 sections）+ `check:frontend` 全链绿 + `verify:frontend` 通过，新 bundle 已发布。live 投影复跑：xai 0→9/13、bailing 0→1/1（未中 4 条为 AA 快照名与注册名真不同，非别名问题）。注册表未动——注册 MiniCPM 模型属用户数据写，仍需授权走 model-routing CAS。

### 2026-09-07 (agent_inbox 命名/注释去 swarm 化：mode 改为必填 lane 标签)

- **起因（用户裁定）：** 桶里以后不只有 swarm 信息——共享 inbox 的变量名与注释应当反映多 lane 事实，不该再以 swarm 为中心。
- **盘点结论：** 变量名本身已通用（`task_id`/`bucket`/`item`/`_inboxes`/`_tombstones`），swarm 框架残留在三处：① 包/模块 docstring 标题即「async swarm updates」、`enqueue` docstring 称 payload「Typically a <swarm-update>」、并发注释只提 swarm scheduler 线程；② `enqueue`/`enqueue_after_durable_commit` 的 `mode='swarm-update'` 默认值——共享桶里某一条 lane 不应享有默认特权；③ `_state.py` 校准/墓碑注释以 swarm-update 为唯一想象项。
- **修法：** ① `__init__.py` 包 docstring 重写为「Per-key model-facing inbox for async notifications」，四条 lane（swarm-update/background-command/user-steer/peer-msg）各自注明生产者与送达纪律，lane 标签是 drain/clear/has_pending/pending_items 过滤键；`_queue.py`/`_state.py` 同步多 lane 措辞（生产者线程、~2KB per item、late-arriving producers）。② 两个 enqueue 的 `mode` 提为必填关键字参数并置于签名首位——生产调用点（swarm/master×2、_background_command、command_service 的 steer）本就全部显式传 mode，零行为变化；`untombstone` docstring 从「fresh spawn_agents wave」泛化为「new live turn」（_turn_prelude 的会话复活路径同样是调用方）。③ `format_swarm_update`/`consume`/`consume_peer` 等 lane 专属helper 名字不动——它们本就是 swarm/peer lane 的格式化与去重器，命名准确。
- **规格同步：** `tests/agent_core/test_agent_inbox.py` 与 `tests/background/test_background_completion_cancel.py` 的全部缺省 mode 调用点补显式 lane（队列机制针用 `mode='test'`，语义针用真实 lane 名）。
- **验证：** agent_inbox 相关全套 21 个测试文件 326 绿 + segment_model 81 绿；ruff 净。
### 2026-09-07 (添加请求名即死锁根修：禁用部署纳入探测计划 + 探测通过自动启用)

- **起因（用户截图+提问）：** 给 kimi-k3 添加请求名 kimi-k3-m17 后，该行直接显示「未启用」且无处可启用、也无探测入口——矩阵把禁用行渲染成斜纹死行，行/格探测钮被抑制。用户预期：应有探测选项。
- **根因（三段闭环死锁）：** ① 出生即禁用——新请求名按契约落库为 `enabled:false + probe_status:'unprobed'`，而域权威（`lib/model_routing/domain.py`）拒绝无 passed 探测的启用，界面上也根本没有启用开关；② 探测计划跳过禁用部署——`probe_plan` 只把 enabled 的 deployment 排进计划，禁用行永远等不到判决；③ 前端把禁用行当无 ModelRef 行处理——斜纹 + 无探测钮。三段叠加：禁用→探测不到→永远无法启用。
- **修法（打通「探测即启用」唯一通路）：** ① `lib/model_routing/probe_plan.py`（新模块）：探测计划纳入禁用部署，单元格打 `deployment_enabled:false` 标记；`lib/provider/provider_probe.py` 归一化时对该标记格强制 `recommend_disable:false`（禁用格的失败判决不可行动——它本来就不参与路由），`routes/api_v1/providers.py` 与 `lib/model_routing/probe_automation.py` 应用建议时同样跳过禁用格。② 前端 `access_matrix.js`：禁用请求名行渲染行级/格级探测 ⚡（斜纹只留真正无 ModelRef 的待确认行）；`_pruneProbeCellsToGrid` 把禁用 wire id 计入合法网格，判决不被剪除；`_stageProbePassedWireIds`——任一格探测通过即把该 deployment 直接暂存为 `enabled:true + probe_status:'passed' + identity_confidence:'high'`（与添加模型选择器同一形状，域权威才接受），弹 toast 提示「保存后参与路由」，全局保存提交。③ `_reconcileProbeProofContract` 解耦：遗留状态迁移仍门控于 `effectiveProbeStatus` 存在，禁用格 `recommend_disable` 拔除无条件执行（此前被同一 early-return 误绑，行为针曝光）。
- **规格：** `tests/test_probe_plan_disabled.py`（新建，计划含禁用部署+标记+建议跳过）；`tests/providers/test_provider_model_add.py` +2 针——禁用行探测钮/无斜纹针（斜纹全页恰剩待确认行 1 格），探测通过自动启用针（失败判决不改启用、不拔 toast、拔除 recommend_disable；通过判决暂存为 enabled+passed+high、toast 一枚、未启用芯片消失）；模块 docstring 同步新契约。
- **验证：** 直接规格 9 针全绿；邻套 probe_cells/persistence/nonchat/per-cell-face/automation/oauth/budget + api-v1-model-routing-contract 93 绿；`test_frontend_model_routing_v2.py` 3 红为并行会话 WIP（provider 页/目录重写删除了 `上游部署标识`/`_renderModelCatalogPanel` 旧契约钉，HEAD 版本存在、工作区 WIP 版本不存在，与本改无关，属移动靶）；ruff 净；`generate:runtime` 重组（166 sections）+ `check:frontend` 全链绿 + `verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。

### 2026-09-07 (后台命令结果滞留根修：swarm 回收误清共享 inbox → clear 按 lane 过滤)

- **起因（用户截图）：** 「后台命令已完成·结果送达未确认」横幅常驻——命令 `bg_a234dfd76597` 早已跑完且耐久队列入库（`715c28cb`），但结果既不注入当前轮也不作为新轮派发。
- **根因（两段）：** ① 会话级 `agent_inbox` 是多 lane 共享的——`swarm-update` / `background-command` / `user-steer` / `peer-msg` 四种 mode 共用一个桶；后台命令完成时的「快速通道孪生」（`enqueue_after_durable_commit`，成功后删耐久行保 exactly-once）与 swarm 完成通知同住其中。② `spawn_agents` 回收上一波已终结 session（`_tools.py:271-274`）、TTL 清扫与容量逐出、`_remove_session` 三处 swarm 生命周期点都调 `agent_inbox.clear(key)` 全桶清空——把尚存活轮次的后台命令孪生一并抹掉。耐久行仍在（故横幅显示未确认），但 `dispatch_next_queued` 的 live-task 护栏拒派、当前轮的回合边界 drain 又拿不到被抹的孪生 → 两头都走不通，只能等 settle。
- **修法：** ① `agent_inbox._queue.clear(task_id, *, modes=None)` 新增过滤参数：`None` 保持任务终态全清语义；`modes=[...]` 只丢弃命中 mode 的项，其余 lane 存活；两种形态都照常 tombstone（迟到的 swarm 完成仍不得重建桶——steer/后台命令的生产者对 tombstone 拒绝有耐久行兜底，容忍短暂窗口）。② `_state.py` 三处 swarm 生命周期点（TTL 清扫、容量逐出、`_remove_session`）改为 `clear(key, modes=['swarm-update'])`。任务真正结束时 `_finalize.py:1316` 的全清不变（steer 打捞先于它执行，后台命令耐久行由 settle 派发，本就无误）。
- **未采纳方案（用户提问的拆 inbox）：** 不拆。共享桶的冲突已由 mode 过滤解决；拆分会引入双桶 drain 顺序、双 tombstone 生命周期等新状态面，收益不抵复杂度。
- **规格：** `tests/agent_core/test_agent_inbox.py` +3 针（选择性清理保异 lane 项/仍 tombstone 且 revive 后幸存项可送达/无命中 no-op）；`tests/swarm/test_swarm_cleanup_lifecycle.py` 的 clear 打桩 lambda 补 `**_kw`。
- **验证：** 直接规格+邻套（swarm cleanup/inbox-yield/completion-publication/async、background-completion-cancel、user-steer exactly-once/injection）141/141 绿；ruff 净。
### 2026-09-07 (Models 表按语义同一性去重：服务商变体折叠进开发者正名行)

- **起因（用户截图+裁定）：** 模型目录表格把同一模型按注册行逐条列出——`kimi-k2.5` 与 `kimi/kimi-k2.5` 并存、`-Meituan` 区域供应变体（Qwen3_8-27B-Meituan、deepseek-v31-meituan、Llama-4-*-Meituan）与正名行并存。用户裁定：本页是只读模型目录，不关心供应渠道变体，按语义同一性去重、一律归到开发者（creator）名下记一行。
- **修法（纯展示层，注册表未动）：** `frontend/src/features/model-catalog/model.ts` 的 `buildVendorGroups` 在编译行之前先做身份折叠——`semanticModelKey(creator, id)`＝creator + 叶名（取 `/` 后）去标点小写化并剥掉供应词表 {meituan, fp8, instruct}；同键多行由 `collapseRegistrations` 收敛，存活优先级＝裸 id > 无供应词 > 有价 > 有 AA 分 > 短 id > 字典序，缺失字段（pricing/AA/release_date/context/quality_rank/lifecycle）从被隐藏的孪生行回填，capabilities 取并集。`gpt-3.5-turbo-instruct` 钉入 `IDENTITY_MERGE_EXCLUSIONS` 永不合并——OpenAI 把它当独立遗产模型卖（自有定价、4096 窗口），并进 gpt-3.5-turbo 会双双错价。Pareto 图消费同一编译结果自动继承去重，图自身的 aa_slug 层继续处理 effort 变体（o4-mini-high 同分行）。
- **词表边界（live 注册表验证）：** instruct 剥离在全库只触发 6 组合并，5 组为真别名（qwen3-235b-a22b-(instruct-)2507、qwen3.5-397b-a17b、qwen3-vl 两档、Scout-Meituan），唯一假合并 gpt-3.5 已被排除钉拦截；supply={meituan,fp8} 是库内仅有的区域/量化后缀。
- **规格：** 新建 `tests/model_routing/test_model_catalog_identity.py`（node 行为针，走 vite_test_bundle.mjs）：12 注册→8 模型、kimi/qwen/llama 三组存活者钉名、孪生回填（kimi-k2.6 得价、kimi-k2.5 得 AA 分）、存活者自有字段不被覆盖、gpt-3.5 双行各自存活且定价不串。
- **验证：** 新规格 + 邻套 pareto 3/3 绿；ruff 净；`generate:runtime` 重组（166 sections）后 `check:frontend` 全链绿；`verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。用真实注册表离线模拟全管线：表格行 508→493，15 行隐藏项逐一核对均为真重复、存活者全为正名裸 id。
- **遗留（仍待用户授权，未动）：** 注册表里的 15 行重复注册与错误数据行（如 Scout-Meituan 64K 窗口无价卡）仍在权威文档里，删除属用户数据写操作，需明确授权后走 model-routing CAS。

### 2026-09-07 (发送按钮常驻角标 1 根修：队列僵尸行双根修 + 存量行排水自愈)

- **起因（用户截图）：** 输入框右下角发送按钮的排队角标常显 1，但排队列表为空。排查实锤：`mtq3ev44samfhr` 与 `mtnutburlq1uc7` 各有一行 09-05/09-06 遗留的队列行，其物化 pair 的 attempt 已是 interrupted 终态，排水线程每轮 activate 都被 `database_conflict("Queued attempt is no longer pending")` 拒绝、释放租约、下轮再试——无限循环；queue.depth 永续为 1 顶起角标，而前端排队列表的过滤条件（`!item.inputTurnId`）又恰好不渲染这类物化行，用户无处可见、无法取消。
- **根因（两段）：** ① 出生路径——`turn_lifecycle.abort_attempt` 对 queue-bound pending attempt（从未绑定 executor task）走通用 Stop 路径，`record_task_event('aborted')` 把它盖棺 interrupted，耐久的 queue 行就此成为永远不可激活的僵尸（前端停止键会 abort 会话内所有 live attempt，含排队中的 pair）。② 循环路径——`message_queue._dispatch_queued_turn` 的 `except LifecycleConflict` 不分青红皂白一律按「lane busy 瞬时竞争」释放租约；对 linked pair 而言 `database_conflict`（attempt 已终态）与 `database_integrity`（pair 行被删）都是永久性失败，释放=永动重试。
- **修法：** ① `abort_attempt` 在 LIVE 检查后识别 `queueBinding`：queue-bound attempt 没有 executor 可停，停止语义=撤回排队意图本身，改走 `cancel_queued_turn_pair`（queue 行+pair 同事务删除，与队列条取消按钮同一语义），回执 `status:'cancelled'` 带 queueId/deletedTurnIds（前端 allSettled 只看 ok 信封，形状自由）。② `_dispatch_queued_turn`：linked pair 上 `LifecycleConflict` 且 code≠`lane_busy` → 永久性冲突，`_finalize_queue_dispatch` 退休该行；通用 `except Exception` 补 `StorageError database_integrity`（pair 行已被用户删除）同款退休；lane_busy 瞬时语义不变。模块 docstring 同步「可重试失败放租、永久冲突退休」契约。③ 存量两行无需手术：重启后下一轮排水即按新逻辑退休自愈。
- **规格：** `test_turn_lifecycle.py` 新针（queue-bound abort → cancelled、queue 空、pair 双轮删除、活跃轮不受影响、二次 abort 报 not-found——cancel 连 attempt 行一并删）；`test_queue_lease.py` 新针（record_task_event 直接盖棺 queue-bound attempt 复刻修复前 Stop 竞态 → 排水退休该行、queue 空、下轮不再重租）。
- **验证：** 两规格文件 48 绿；邻套 message_queue/conversation_command_abort/cancel_task_authority 等 85 绿；ruff 净。排障中坐实两枚工作区既有 WIP 红（与本次改动无关，回退我的 hunk 后照样红）：`test_v3_registry_miss_preserves_owner_tombstone_and_projection` 的 mock 行缺 `attemptId`（WIP 版 abort_attempt 需要）——已顺手补进 mock；`test_conversation_commands_v3.py::test_busy_lane_queue_returns_real_pair_and_cancel_deletes_it` 的 cancel 重放 deletedTurnIds 为空，疑似 WIP 回执轻量化收窄了 turn.queue.cancel 的重放负载，留待该 WIP 属主收口。

### 2026-09-07 (Astra 集成三缺口根修：gpt-6 代际门、272K 窗口钉、Codex 套餐表同步）

- **起因（用户要求对齐 CLIProxyAPI 新特性）：** 对照 router-for-me/CLIProxyAPI@main 注册表核查 Astra 集成，发现三个缺口。证据源：`internal/registry/models/models.json`（astra 服务 team/plus/pro，context_length 272000，思考档 low/medium/high/xhigh/max 无 none）与 `codex_client_models.json`（context_window 272000 / max 872000，minimal_client 0.153.0）。
- **缺口与根因：** ① `build_body` 的 reasoning_effort 盖章门是 `is_gpt5()`＝`'gpt-5' in name`——`gpt-6-astra` 永远不进分支，用户选的 high/xhigh/max 被静默丢弃，线上回退 medium（`_api_stream_state.py` 同谓词同病）。② `context_profile` 对 astra 命中 GPT-5.6 合同分支返回 1.05M，但上游两份注册表都钉 272K（max 872K），压缩预算 4 倍高估 → 272K 处 400 溢出风险。③ `lib/oauth/outbound.py` 静态套餐兜底停在 2026-08-08：上游已下架 gpt-5.4/5.4-mini（全套餐），astra 已在 team+。
- **修法（代际化，非一次性特判）：** ① `_family.is_gpt5` 改正则 `gpt-[56](?!\d)`（文档化覆盖 gpt-5/6 推理代际，挡假设性 gpt-50）；`is_gpt_56` 兑现 docstring 的「5.6+」契约——`(major, minor) ≥ (5,6)`，整条 gpt-6 线合格。② `_capabilities._gpt5_minor` 升级为 `_gpt_generation` 返回 `(major, minor)` 元组；`gpt_reasoning_effort` 新增 gen≥(6,0) 分支：disabled/显式 off·minimal·none 一律地板 `low`（astra 注册表无 none 档，400 风险，Spark 先例），其余走 `normalize_gpt56_reasoning_effort`（ultra→max，max 保留）；既有 5.6/5.3/legacy 三分支对 5.x 行为逐值不变。③ `_context.py` 在合同分支前插 astra 钉（272_000, vendor_official, exact），注释记证据与顺序约束。④ `_CODEX_MODEL_TIERS` 全表重同步（free 无 astra/spark，team 起有 astra，plus/pro 全表），注释更新同步日期与下架说明。
- **规格：** `test_gpt_spark_reasoning_effort.py` +14 针（astra disabled→low、off/minimal/none→low、六档映射含 ultra→max、is_gpt5/is_gpt_56 家族钉、build_body 端到端盖章钉）；`test_context_profile_contract.py` astra 进 codex 目录窗口钉表；`test_oauth_claude_cloak.py` 套餐钉改 5.5/astra 门控 + 退役清单加 5.4/5.4-mini。
- **验证：** 三直接规格 58 绿；邻套 meituan/gpt56-contract/codex-catalog/codex-version/capability-profiles/agents-drift 53 绿 2 红——`test_openai_gpt56_contract.py` 的 lean-prompt 与 multi-agent beta-header 两针为工作区既有 WIP（`system_prompt_cc/_sections.py`、`lib/llm/_sse_core.py` 未提交改写）所致，对 gpt-5.6-sol 行为在本改前后逐值等价，与本改无关；更广门 responses_outbound/llm_body/model_info/context_limits 等 262 绿；ruff 净。
- **未引用上游新特性（已记录未接线）：** codex_client_models.json 的 `support_verbosity`/`default_verbosity: low`（astra 经 codex profile 不出 verbosity 字段，public_openai_features 门控不变）；`max_context_window: 872000`（pro 档扩展窗，静态钉取保守下限 272K，learned expand 机制可在运行时抬升）。
### 2026-09-07 (AA 评分收口 exact-only + Pareto 图同模型去重：虚框聚簇清零)

- **起因（用户截图）：** 模型目录「成本 × AA Index」图的虚线框（同坐标聚簇）里大多是有问题的点——同一模型被记两次（`kimi-k2.5` 与 `kimi/kimi-k2.5` 等带服务商前缀的重复注册），或不同时期的模型挂着同一个 AA Index 分。用户裁定：别人没测过的就不许写分。
- **根因（两段）：** ① `_score_matching.match_model` 留有 ≥5 字符前缀回退，把被测模型的分数继承给未测变体——`claude-sonnet-4` 拿了 Sonnet 4.6 的 38.5、`deepseek-r1-distill-qwen-7b` 拿了完整 R1 的 13.9、`qwen3.8-flash` 拿了 Flash-Next 的 45.6，直接违背模块自己的「宁可缺分不可错挂」契约。② 注册表里同一被测模型存在裸 id 与服务商命名空间 id 双份（7 组：kimi-k2.5/kimi-k2.6/glm-5.3/mimo-v2.5-pro/deepseek-v3.2-exp/llama-4 双子），两条都 exact 命中同一 AA 行、同分同价落同坐标，前端 `spreadMarkers` 于是画出虚线聚簇框。
- **修法：** ① `lib/model_catalog/_score_matching.py` 删除前缀回退，只留规范化 exact 恒等匹配（model_id/display_name/`/` 叶名，多行歧义仍返回 None），docstring 明示未测变体不得继承。② `frontend/src/features/model-catalog/pareto-chart.ts` 新增 `collectPlotCandidates`：以 `(creatorId, aa_slug||叶名)` 为键，每个被测模型只出一个图候选；存活优先级＝可绘制（有价）> 裸 id > 短 id > 字典序，`buildParetoPoints`/`buildParetoExclusions` 共用，排除注释同样去重。③ docs/MODEL_REGISTRATION.md 浏览器投影一节同步新契约。
- **规格：** `tests/model_routing/test_model_intelligence_aa.py` 新增未测变体不得分针（0731/vision-exp/highspeed 全空）；`tests/model_routing/test_model_catalog_pareto.py` 新增 node 行为针（同 AA 行四组重复：裸 id 胜、有价命名空间胜、双裸短 id 胜、全无价只留一条排除）。
- **验证：** 邻套 22 针全绿（pareto×4 文件 + aa + routing evidence + openrouter）；ruff 净；`check:frontend` 全链绿 + `verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。用真实注册表+AA 缓存离线模拟全管线：打分模型 273→180（93 个继承分清零），图上点 150、排除 22，同（价,分）坐标零重合——虚线框在现有数据下不再出现。
- **遗留（待用户确认，未动）：** 注册表里的 7 组重复模型行与 `-Meituan` 区域供应变体仍在 Models 列表里（图上去重只是投影层收敛）；删除属于用户数据写操作，需明确授权后走 model-routing CAS。
### 2026-09-07 (tofu-db steer.commit 语义收敛：轻量回执 + conversation.activity 快照失效，差分门回绿）

- **起因：** legacy Python 存储权威把 `turn.steer.commit` 改为轻量语义（回执只留 `{steered, injectionId, blockId, conversationId, turnId, conversationRevision}`，sync 事件由 `turn.patch` 改为 `conversation.activity` + `requiresSnapshot`），Rust 移植仍实现旧语义，差分针 `turn steer commit atomically appends one live operator injection` 翻红灯。
- **修法（tofu-db 侧对齐，非回退 legacy）：** ① `turn.rs::steer_commit` 不再回传完整 public Turn、改发 `conversation.activity` 失效事件（复用 compact 同款通道，`store_compaction_sync_event` 通用化为 `store_activity_sync_event`，compact 调用点同步改名）；投影写入与 CAS 围栏（`FencedLiveSteer`）、检查点退役、会话 rev 推进等状态语义不变。② 单测 `turn_steer_commit_fences_the_live_window_and_replays_once` 改为断言轻量回执形状（无 `turn` 键）+ `turn.list` 读回验证投影截断/去重/revision，sync 事件断言改钉 `conversation.activity`/`requiresSnapshot`/`conversationRevision`；故障注入矩阵（每 op 中断 + 短写）断言不变（事件计数与 outbox 序列不受影响）。
- **动机（legacy 注释明示）：** public Turn 与注入数组补丁随历史增长，回执与 sync 帧必须 content-free，避免重演 oversized-frame 事故；对端凭一个事务性失效通知拉取权威快照。
- **验证：** steer 差分针单跑绿；`test_tofudb_differential.py + test_tofudb_pre_authority.py` 149/149 绿（54s）；cargo test 559/559 绿（20s）。docs/STORAGE.md §145-152 已在 legacy 变更时同步新语义，无需再改。
- **距上线仍缺（docs/STORAGE.md 硬门）：** 迁移分支 `tofudb/integrate`/`tofudb/par-mig` 合并且多域迁移认证、真实挂载故障/规模矩阵（correlated sync-loss、extended rotation、GC spill scale）、性能门槛（turn_append O(N) 增长、SQLite conversation_create 延迟、search 尾延迟）。

### 2026-09-07 (edit_file 被拒原因行内直显：失败行/卡片头渲染截断理由 + input-repair 吞 allow_outside_workspace 根修)

- **起因（用户截图）：** 会话 mtq3ev44samfhr 中 edit_file 反复被拒，卡片只显示红色 `0/1 edits` 徽章，拒绝原因藏在失败行的 <details> 里，用户要求「下次拒绝时把原因渲染在这一行」。
- **根因（两段）：** ① 拒绝本体——input-repair 变换（`lib/tool_input_repair/_transform.py`）无条件采用变换后字典，丢弃模型已传的 `allow_outside_workspace=true`，写门禁按缺省拒绝（`Refusing to write to path outside workspace roots …`）。② 展示缺口——`tool-result-presentation.ts` 的批量编辑渲染器把 `[N] FAIL …` 原因只放进折叠的 `.ptool-batch-fail-detail`，行级 summary 与卡片头均无痕迹。
- **修法：** ① `_transform.py` 保留授权标记（前半程已修并加针）。② 前端 `renderBatchEditResultHtml`：新增 `inlineFailureReasonHtml`（单行化 + 110 字符截断 + title 悬停全文），失败行 summary 与批量卡片头（首个带原因的失败）各渲染一枚 `.ptool-batch-fail-reason`；数据源复用既有优先级（权威结果文本的 `[N] FAIL` 行 > metadata detail 兜底），展开区全文保留；无原因不渲染。CSS `09-tool-terminal-details.css` 加对应样式（红色 mono、ellipsis、max-width 46%）。
- **规格：** `tests/frontend/test_frontend_tool_rounds_render.py` +4 针（无原因保持安静 / 行+头双处渲染 / 权威 FAIL 行优先且截断 / metadata 兜底且 XSS 转义），owner 检查计数钉 29→33；wire-parity 基线按原 indent=1 格式重生成，仅 `batch-diffs-done`、`batch-diffs-fail-compacted` 两轮漂移（均新增原因 span）。
- **验证：** render/parity/approval/batch-visibility 邻套 19 绿；`generate:styles` 重组（32 sections）；`check:frontend` 全链绿；`verify:frontend` 通过，新 bundle 已发布（`ptool-batch-fail-reason` 已进 static/styles.css 与 tool-presentation chunk，硬刷新生效）。

### 2026-09-07 (「我的上下文」删除不生效双根修：垃圾桶点击即走 DELETE API + 删除留墓碑防学习器回填)

- **起因（用户截图）：** 「我的上下文」页垃圾桶怎么点都删不掉、下次又出现，要求根因修复。
- **根因（两个独立 bug）：** ① 前端 `preferences.ts` 的垃圾桶点击只在面板本地数组 splice，不落库——删除要等「保存更改」整单 PUT 才生效，用户删完直接关页等于没删。② 后端删除连墓碑都不留：profile_consolidate 学习器每轮从对话重新提炼偏好，已删条目会被原样学回，即使用户走了保存路径也会「复活」。
- **修法：** ① 前端 `removeContext` 对持久化条目立即调 `Api.userContext.remove`（`DELETE /api/v1/context/<id>`），成功后重基线 dirty tracker（幽灵未保存态一并消掉）；失败保留条目并红字报错；未保存的新行（无 id）纯本地移除不调接口。② 后端 `_context.py` 删除/整单替换移除条目时写入 `deleted_context_items` 墓碑（含文本指纹），undo 可恢复；`profile_consolidate.py` 学新条目前先查墓碑，命中即跳过。
- **规格：** `tests/users/test_user_context.py` +2 针（删除墓碑阻止学习器回填；整单替换路径的移除也留墓碑且 undo 恢复）；新建 `tests/context/test_preferences_context_remove.py` 3 针 jsdom 行为约（垃圾桶点击即发 DELETE 并重基线无脏态 / 删除失败保留条目报错 / 未保存行纯本地移除）。
- **验证：** 两规格文件 20/20 绿；邻套 test_user_profile + test_frontend_user_context + test_llm_json_consolidation 58 绿；ruff 净；`generate:i18n` 重生成（4876 keys）；`check:frontend` 全链绿；`verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。

### 2026-09-07 (后台命令完成轮 UI 根修：专用 DOM class + tofu/light 主题 + 注入轮去掉 edit/regenerate)

- **起因（用户截图）：** 后台命令完成后的「已作为新轮输入提交」轮渲染难看——label 裸 `<p>` 按用户正文字号显示、`<details>`/`<summary>`/命令 `<pre>` 全无 class（浏览器默认黑三角）、`proactive-banner` 只有深色主题 rgba 紫、暖米气泡上洗成灰紫通栏、原始 `bg_xxx` id 正文字号直出，且机器注入的通知轮挂着 edit/regenerate 按钮。
- **修法：** ① `classic-conversation-renderers.ts` 的 `renderBackgroundCompletion`/freshTurn label 全部挂专用 class（`bgcmd-label/details/summary/id/command/note`），summary 中命令 id 改 `<code>` mono。② `23-turn-reading-system.css` 新增 bgcmd 一节（caption 字号、quiet disclosure、复用 `--turn-rule`/`--turn-process-surface` 变量，tofu 主题自动一致）；`08-swarm-project-tools.css` 补 `.proactive-banner` 的 light/tofu 覆盖（沿用 `.sw-a-model` 的 `#5b4a8a`/`rgba(124,92,184,*)` 调色）。③ `conversation-view-model.ts` `actionsFor` 新增 `backgroundCommandInjected` 参数（调用点取 `turn.projection.backgroundCommand`）：注入轮只留 copy/inspect/delete，普通 human 轮行为不变。
- **规格：** `tests/background/test_background_completion_ui.py` 3→6 针——新增 DOM class 钉（`p.bgcmd-label`/`details.bgcmd-details > summary.bgcmd-summary > code.bgcmd-id`/`pre.bgcmd-command`）与 `selectConversationViewModel` 双轮动作钉（注入轮无 edit/regenerate 保留 copy/delete；普通 human 轮保留 edit/regenerate）。
- **验证：** 本规格 6/6 绿；邻套 inject_row_positioning/turn_store_owner_migration/turn_projection_segments/tool_lifecycle_timestamps/tool_injection/tool_command_execution 59 绿 1 红——`test_emitted_at_is_stamped_at_one_chokepoint` 为工作区既有 WIP（`lib/agent_core/events.py` + `contracts/fixtures/events/tool_result.json` 未提交改动）所致，与本改无关；`generate:styles` 重组 + `check:frontend` 全链绿；`verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。

### 2026-09-06 (tofu-db 差分门收尾
### 2026-09-06 (tofu-db 差分门收尾：cleanup/backfill/model_continuation 五操作脚本化 + legacy superseded_at 空列缺陷实锤)

- **起因：** cleanup-agent 落地 `model_continuation.{get,put,delete}`、`turn.cleanup`、`turn.search.backfill` 五个 Rust 移植后未补差分脚本，`test_tofudb_port_ratchet` 红（`_PORT_BACKLOG` 已清零，任何新可执行 op 直接翻红灯）。
- **新增脚本（tests/tofudb/test_tofudb_differential.py，均在 `_turn_scripts`）：** ① `model continuation checkpoint frontier CAS witness and deletion`——缺失会话不写不删、畸形 projection/伪造见证拒绝、frontier=ordinal+1 首写、同 frontier 幂等确认/分歧拒绝、第二 Turn 推进 frontier 后旧 payload 拒绝、delete 幂等布尔；新增 `_ContinuationWitnessRef`，从各驱动自己的 `turn.get` 结果用 Python `settled_witness` 重算见证，两引擎各自内部复算对账（跨引擎 digest 一致性由此被门直接断言）。② `turn cleanup deletes superseded attempts behind retention and fences`——limit=0 双引擎同报 `database_protocol_error`；`create_pair → event.record(terminal superseded) → attempt.create(regenerate, expected_projection_revision=2) → sync.prune 解围栏 → cleanup`；事件时钟钉真实过去时（retention 0 下任何 superseded_at 取值解释都 < cutoff）。③ `turn search backfill global sweep contract`——合法全局扫描、携带 user_id、超长 cursor 三步。
- **实锤的 legacy 缺陷（非移植 bug）：** lib/ 全仓从未写 `storage_generation_attempts.superseded_at`（列在 schema 里，全仓 7 处引用无一写入），legacy `turn.cleanup` 谓词永不命中、永远返回 0——superseded attempt 永久滞留；tofu-db 在终态事务里维护 superseded 索引真实回收。三条 `KNOWN_DIVERGENCES` 登记：报告形状与租户范围（legacy 租户全局 int vs tofu-db owner 作用域结构化报告）、回收语义差异（被回收 attempt 读回 null vs legacy 滞留文档）、backfill 兼容壳（忽略 payload 回固定回执）vs 真实全局扫描（payload 身份 fail-closed）。
- **验证：** 三新脚本 + ratchet 单跑绿；全量 `test_tofudb_pre_authority.py + test_tofudb_differential.py` 149/149 绿（47s）；cargo test 559 passed 维持。

### 2026-09-06 (swarm 面板子代理工具结果不再渲染：推送帧归属/落定防改 + TOFU_SWARM_GLOBAL_WORKERS 上限移除)

- **起因（用户报告）：** swarm 面板里各 sub-agent 的工具调用结果不再渲染；并要求一并移除 `TOFU_SWARM_GLOBAL_WORKERS` 上限。
- **根因：** ① 推送侧（swarm-presentation-overlay.ts）多 swarm/多 task 下同一会话的推送帧会绑错 turn——`hasSwarm` 只查「有没有 swarm」，不按 swarm_id/agentId 归属匹配，已落定（settled）的快照轮仍会被后续帧合并覆盖；无 toolCallId 的临时推送面板在 rebase 时对不上权威 spawn_agents 轮就错挂下标。② 同轮 round 判等（sameSwarmRound）不查 taskId/attemptId，跨 task 的轮可被误并。
- **修法（前端）：** `swarm-presentation-overlay.ts`——新增 `swarmRunId`/`swarmSettled`，`ownsFrame` 按 swarm_id+agentId 精确归属、settled 轮拒并拒覆（`if (!target || swarmSettled(target)) continue`）；rebase 增加「无 callId 临时面板唯一 launch 绑定」路径（仅 spawn_agents 且同 taskId、唯一才绑）；`taskId` 上下文改用 frame 的 swarmId 而非 convId。`sameSwarmRound` 先比 taskId/attemptId 再比 toolCallId。后端 `lib/swarm/{agent,master,scheduler,snapshot,execution_gate,resource_policy,routing}.py` 与 `integration/*` 配套加固（快照 settled 语义、调度与门禁证据）。
- **TOFU_SWARM_GLOBAL_WORKERS 上限移除：** `resource_policy.swarm_global_workers()` 默认 0=**不限**（仅受全局 LLM 信号量与每会话 swarm_max_parallel 约束），显式正值为运维 override 仍封顶 64；`OwnerFairExecutionGate` capacity<=0 走无限 permit 分支（waiter 队列有界 + owner round-robin 不变）；runtime_guards 三档画像全部置 0，docs/INSTALL.md 同步。
- **测试债清理（三处与本次改动交织的历史漂移）：** ① test_deployment_configuration 期望清单缺 `TOFU_MODEL_REPLAY_CACHE_MAX_MIB`（前序 WIP 加键未更新测试）——补齐；② test_swarm_resource_budget 断言 `execution.capacity >= 1` 与 0=不限语义冲突——改 `>= 0`；③ wire-parity 基线冻结 HTML 字节——tool-search-presentation.ts 里 `String(...)` 被删（setAttribute 语义等价但字节不同）导致 5/61 轮漂移，回退该文件恢复字节一致。
- **验证：** swarm 八套件 + deployment_configuration + turn_projection/tool_args/frontend 渲染套件共 114 针全绿；swarm_panel_authority.test.cjs 4/4；ruff 净；build:frontend + verify:frontend 通过，新 bundle 已发布（硬刷新生效）。

### 2026-09-06 (后台活动芯片：主 agent 已退出、子 agent/后台命令仍在跑时的常驻统计)

- **起因（用户裁定）：** 经常出现 sub-agent 还在执行而主 agent 已退出、只能等自动派发的情况——这本身没问题，但底部应有一处统计显示「仍在执行」的 swarm 子代理与后台 run_command。
- **改动：** ① 后端 `list_active_swarms()`（lib/swarm/integration/_state.py）——按 owner 列出未终止 swarm 会话（swarm_key+conv_id 供深链，刻意不用 task_id：派生轮通常已结算；per-agent 状态在注册表锁外采样，单会话异常只跳过不拖垮读者）；新路由 `GET /api/v1/activity/background`（routes/api_v1/activity.py）返回 `{swarms, backgroundCommands}`，命令侧只留 status=running，稳态两表皆空。② 前端新 section `background-activity.js`：convStatusStrip 内 #turnNav 前的芯片（稳态 display:none），单一链式 setTimeout 轮询（活跃 3s/空闲 10s/隐藏页跳过），点击弹上开浮层——分「Sub-agents / Background commands」两区，逐 agent 行显示 role/objective/round，swarm 行点击 loadConversation 深链，用户文本全走 textContent；数据签名不变不重绘浮层。③ `Api.activity.background`（onError:'null'+coalesce+background priority），契约落 docs/contracts/API_CONTRACT.md §2.19，i18n zh/en 各 8 键，CSS 进 17-compaction-context-rail.css。
- **排障实录（真实产品 bug，不止测试事）：** jsdom 测试首跑芯片永不出现——`_schedule()` 先 clearTimeout 再排新定时器，boot 路径（DOMContentLoaded → `_schedule(1200)`）会把测试（或真实调用方）在启动早期通过 `updateBackgroundActivity()` 排的 0ms tick 直接取消。修为 DCL 分支有 pending 定时器则跳过（`if (!_pollTimer) _schedule(1200)`）。context-bar.js 同款 clobber 模式未动（无即时刷新调用方）。
- **规格：** tests/background/test_background_activity_api.py 7 针（owner 过滤、terminated 过滤、路由稳态/种子快照）；tests/background/test_background_activity_chip.py（jsdom 驱动生成 bundle 里的 section 本体：挂载位置、空态隐藏、标签文案、浮层分区/行、objective 按文本渲染防 XSS、深链、快照清空复隐）+ 断链 neuter 针证明断言有牙。
- **验证：** 两新文件 9 针全绿；test_swarm_status_truthfulness + test_api_v1_integration 97 针、test_api_contract_drift 6 针全绿；ruff 净；build:frontend + verify:frontend 通过，新 bundle 已发布（硬刷新生效；纯 Python 侧重启 server.py 后路由生效）。
### 2026-09-06 (ask_human 批量表单：点选项报「功能模块加载失败」根修 + 表单去重美化)

- **起因（用户截图）：** 批量指导卡上点任意选项右下角弹「功能模块加载失败，请检查网络后重试」，且卡片顶部把「Multiple questions (batch form): 1.… 2.…」原文 dump 一遍、各分区又重复一遍问题，格式难看。
- **根因：** 上轮批量表单落地时把 `selectBatchChoice`/`submitBatchAnswers` 加进了 feature-bridge.js `_FEATURE_ENTRY_POINTS`（装了路由 stub）与 prelude/epilogue 真函数，却漏了 main.ts 的 `miscEntries`——点击时 stub 走 `_invokeFeatureOwner` → `TofuModules.canInvokeFeature(name)` 查 `routedFeatureEntries` 为 false → fail-closed 弹 `feature.loadFailed` toast。单问题路径正常是因为 `submitHumanGuidanceChoice/FreeText` 早在 miscEntries 里。jsdom 契约测试直接注入 resolver、绕过 bridge，故未暴露。
- **修法：** ① main.ts `miscEntries` 补两个名字（与其余 human-guidance 提交同域 misc）。② `renderAwaitingCard`：批量表单渲染出分区时不再显示顶层合成摘要与翻译指示器（表单为空时回落原文）。③ CSS：`.hg-batch-section` 由发际线分隔改为圆角卡片容器（flex+gap），tofu/light 两主题补 `.hg-batch-section`/`.hg-batch-qnum` 配色（原 `#a5b4fc` 浅紫在暖色浅底上看不清）。
- **规格：** 新建 tests/frontend/test_feature_bridge_routing.py——`_FEATURE_ENTRY_POINTS` 全集必须 ⊆ main.ts 路由集（缺谁列谁），并钉两个批量动作在 miscEntries；该类 bug（bridge stub 无 domain owner → 点击弹网络错误）自此有守卫。
- **验证：** 新规格 2 针 + human-guidance 邻套（actions/presentation/expired）4 针全绿；`generate:styles` 重组 + `npm run check:frontend` 全链绿；`verify:frontend` 通过，新 bundle 已发布（硬刷新生效）。
### 2026-09-06 (tofu-db 收尾轮：4KB 打包 A/B 实测后被否决回归 8KB + 临时 prof 埋点全拆 + 三引擎基线定稿)

- **目标（用户任务）：** 完成 tofu-db 测试与优化收尾，回答「能否上线、还缺什么」。
- **4KB 打包目标 A/B（被否决）：** PAGE_PACK_TARGET_BYTES 8KB→4KB 实测收益显著——turn_append 600 提交 1.76ms→1.09ms（-38%，store_kb 93.4→65.7），conversation_search 中位 3.68→2.10ms，mixed 1.25×→1.95×，而 sidebar/record.get 扫描零回退（该数据量级下页解码成本与字节数近似线性，每页固定开销未主导）。**但全量测试暴露 3 处失败，其中 2 处是真实破坏而非断言常量：** ① `range_mount_consolidation_enforces_its_byte_budget_before_row_budget`——3KB 大 key 在 4KB 页下一页仅容 1 行，分裂逻辑产出空/乱序 children（"entity children are empty or unsorted"）；② `tool_result_prune_executes_five_thousand_rows_across_entity_pages`——InvalidPayload。即 4KB 对大 key 负载的分裂不变量不安全，按「稳定性优先」裁定回退 8KB（527/527 复绿）。4KB 留作后续项：需先硬化分裂逻辑（单条超大 entry 页的 ≥1 entry 保证 + 内部节点 min-fanout 边界）。
- **临时埋点拆除：** prof_timer.rs 全模块删除，authority/engine/entity/semantic_executor/server/lib 六处调用点逐点移除（含 engine.rs 的 [prof-carry] 桶统计块），grep 零残留；cargo test 527 passed / 0 failed（6 target 全绿，含 fault-injection 恢复族），clippy 0 警告。
- **定稿基线（medium，/tmp/bench_final.json + bench_final_3engine.json）：** vs SQLite 8 胜 2 负——record_put 1.64×、record_get 4.79×、sidebar_list 1.33×、conversation_open 5.36×、search 2.63×、mixed 1.19× 胜；conversation_create 0.56×、turn_append 0.44× 负。vs PostgreSQL（一次性本地实例，unix socket，fsync=on）**8/8 全胜**：put 4.29×、get 15.53×、create 1.58×、sidebar 4.36×、open 23.29×、turn_append 1.25×、search 6.08×、mixed 6.39×。注意：3 引擎同进程运行时 sqlite 引擎报 database_unavail——是 bench harness 的 env 泄漏（postgres 引擎构造时写 TOFU_STORAGE_TEST_POSTGRES_DSN_FILE 进程全局变量），非产品回归；sqlite 对照取 2 引擎轮的 bench_final.json。
- **遗留结构项（即「全面超越」的缺口）：** ① turn_append 存在 O(N) 每提交增长——延迟随单会话 turn 数线性上升（600 提交 avg 1.09ms vs 1000 提交 p50 1.62ms @4KB），疑为随 turn 增长的每会话结构（turn directory / sync event 链）被整体重写，需立项做链式追加化；② conversation_create 的固定提交成本 ~0.6ms vs sqlite 0.59ms，差距在 per-commit fsync+semantic 开销；③ 4KB 打包需分裂硬化后重测。

### 2026-09-06 (tofu-db 提交热路径优化：WAL 算术准入 + 8KB 打包目标 + CommittedTransaction 缓存编码信封)

- **目标（用户任务）：** 对 tofu-db 做系统性测试与优化，在 agent harness 常见负载上全面超越 SQLite/PostgreSQL。本轮针对 turn_append_burst（600 次连续 turn 追加提交） profiling 出的三块热区。
- **Phase 1（WAL 算术准入 + 页打包目标）：** checkpoint 准入改为算术判定（不再逐提交重编码+哈希整条历史）；页打包拆分为 8KB 新写目标 / 16KB 解码存储硬上限（兼容既有大数据库）。600 提交客户端延迟 2.16ms → 1.36ms（checkpoint 613.7µs → 0.1µs，wal_append 352.6 → 274.2µs，tree_rewrite 543.1 → 409.0µs）；页数断言随 8KB 目标同步更新。
- **Phase 2（编码信封缓存）：** `CommittedTransaction` 新增 `encoded_envelope`（提交时落盘的精确字节，含 carried 载荷），删除 `carried`/`authority_state_carried` 载荷副本与 `encode_envelope()`——checkpoint/历史段构建不再二次编码，常驻内存零增长（字节换字节，少一份拷贝）。`TransactionEnvelope::decode_skipping_carried` 新增跳过载荷拷贝的解析档（边界校验全保留），批量提交 fold/push 与 history 段解码改走该档；recovery 保留 `decode_with_carried`（restore_loose 需要载荷局部量）。
- **验证：** cargo test 539 passed / 0 failed（6 个 target，含 fault-injection 恢复前缀族）；clippy 无新增警告（存量 is_multiple_of×2 在 prof 代码）。burst 复测：1.36 → 1.32ms（759 op/s），tree_rewrite 409.0 → 401.6µs，wal_append 275.7µs 持平，ckpt 0.1µs。
- **下一瓶颈：** prepare=407µs（块写入+carried 拷贝）与 tree_rewrite=401.6µs（写路径页重解码——decoded-page cache 按设计只在读路径校验后填充，fault-injection 完整性约束不动）。

### 2026-09-06 (deepseek-chat/reasoner 增删振荡根治：meituan 模板摘除陈旧供给 + 全局退休不变量)

- **起因（用户要求）：** 前一条日志留下「sync 过程中 deepseek-chat/deepseek-reasoner 反复增删」的观察，用户要求查明并一并解决。
- **排查实录：** ① 三次 boot sync 的 added 60/41/1 是本会话模板分批上线的正常增量，非删除重加。② 真正的振荡回路是模板打架：deepseek.json 的 `retired_model_ids`（官方 2026-07-24 下线，别名在官方 API 直接报错，slots/定价/thinking hints 已清）与 meituan.json 仍 authored 同两个 wire id 并存——UI 矩阵给它们挂「已下线/建议移除」芯片，用户删行后 PUT 全量覆盖，下次 boot sync 又从 meituan 模板插回，反复增删。③ provider_catalog_sync 逐行核实为 add-only（「nothing is ever deleted」），不是删除方；access.log 12:44 两次 PUT 200 + 13:36 三次 400（陈旧 revision 冲突）吻合 UI 编辑覆盖路径。④ 活探裁决失败：网关 /models 返回空列表；chat 探针三个模型一律 402「Credit已耗尽」（AIGC validation 阶段先拦，不区分模型有效性）——按官方下线公告 + 2026-09-04 镜像探针先例（400 invalid model name → 删行）处理。
- **修法：** ① meituan.json 摘除 deepseek-chat/deepseek-reasoner 两条陈旧供给（`context_window_source=meituan_plaza` 的残档）；离线验证二者变为 prunable（retired ∧ 不在目录），重启后随 pro 四行一并剪枝，feed 经 retired 过滤永不复活。② 钉全局退休不变量 `tests/model_routing/test_world_catalog_sync.py::test_no_template_authors_a_retired_identity`——任何 shipped 模板不得 authored 任何模板已退休的 canonical 身份；world_catalog.py 模块文档串与 PROVIDER_TEMPLATE_RECIPES.md 同步改写（目录 guard 保留，作部署侧 override 模板的兜底防御）。
- **验证：** test_world_catalog_sync + test_provider_template_feed + test_deepseek_retirement_peak 43 passed；ruff 净。
- **生效条件：** 模板热加载即生效（sync 不再插入）；注册表已有两行的剪枝随既有的 restart-deferred 一并生效。

### 2026-09-06 (模型目录：pro 占位行剪枝 retired_model_ids + Pareto 同坐标簇横向排开虚线框)

- **起因（用户截图）：** 模型目录里 gpt-6-astra 与 gpt-6-astra-pro 同挂 AA 54.7、GPT-5.6-Sol 与 sol-pro 同挂 51.3，且 base 行无价格；claude-fable-5-1、Gemini Flash 3.8 也无价格。用户质问 pro 与 base 为何同分、无价格是否热更新失败，并裁定：同坐标横向排开的点用虚线框分组，pro 行根本不该存在。
- **根因：** ① 早前一波 world catalog sync 把 astra-pro/sol-pro/terra-pro/luna-pro 当独立模型录进 registry，官方合同实际只有 base 四个；pro 行共享 base 的 benchmark 映射故同分，删除后 base 行价格照常显示（价格缺失是 catalog 源无 pricing 字段，fetcher 抓到什么显示什么，缺省渲染 "—"，非更新失败）。② pareto-chart.ts `spreadMarkers` 对同坐标堆叠点走螺旋散开，把同价同分的模型搬到不同 score 行。
- **修法：** ① `lib/model_info/data/openai.json` 增 `retired_model_ids`（4 个 pro id），`world_catalog` sync 支持 retired 剪枝——在册即删、未来 sync 不再收录；docs/PROVIDER_TEMPLATE_RECIPES.md 同步。② `spreadMarkers`：同坐标簇纯横向排开（Y 永不动，score 行不变），返回 cluster 元数据；`renderChart` 对簇画一个虚线 group frame（model-catalog.css），近邻孤立点保留螺旋兜底。
- **规格：** tests/model_routing/test_world_catalog_sync.py 钉剪枝行为；新建 tests/evaluations/test_pareto_spread_clusters.py（node 驱动 TS：簇只横排、簇内不碰撞、单帧几何、孤立点螺旋兜底不变）。
- **验证：** world catalog 邻套全绿；pareto 新旧测试绿；frontend typecheck 本改动零错误 + build + verify 绿，新 bundle 已发布。运行中服务基线实测 astra-pro/sol-pro/terra-pro/luna-pro 各 2 处在册（curl :15000/api/v1/model-routing），重启后应归零。
- **生效条件：** 前端硬刷新即得新 bundle；registry 剪枝需重启 server.py——restart-deferred 已排队（返回 already pending），静息后自动执行。本会话正由该实例服务，不强制重启。
- **后续观察：** sync 过程中 deepseek-chat / deepseek-reasoner 出现反复增删的振荡迹象，待立项排查稳定化。

### 2026-09-06 (Claude Code questions[] 真批量表单
### 2026-09-06 (Claude Code questions[] 真批量表单 + Codex 后台命令一等公民：两件前端面灵感落地；todo_list 报错溯源为旧进程)

- **起因（用户裁定）：** harness 横评六条灵感中留作独立迭代的两件本轮落地——① Claude Code `AskUserQuestion` 的 questions[] 真批量表单（一张表单一次交互，不退化成单卡顺序追问）；② Codex 后台命令一等公民（`run_command background=true` + `check_background_command`/`kill_background_command` 轮询与终止）。同轮用户贴出一条 `todo_list … tool_not_enabled` 报错要求溯源并评估自动重定向。
- **ask_human 批量：** schema 加 `questions[]`（2-8 项，每项 {question, response_type, options?}）；`_human.py` 新增 `_normalize_batch_questions`（丢非 dict/空问题项，options 逐条归一）与 `_handle_ask_human_batch`——多问题打包成**一次** guidance 阻塞等待，JSON 数组回复解析回逐项答案；<2 有效问题自动落回单问题路径；unattended 模式走哨兵回复不阻塞。前端 `renderBatchForm` 渲染多问题卡片，`HumanGuidanceActions` 加 `selectBatchChoice`/`submitBatch`，i18n zh/en 各补批量键，CSS 补 `.hg-batch-*`。
- **run_command 后台：** 新模块 `lib/tasks_pkg/background_commands.py`——进程内注册表（上限 32，先逐最老已完成项），`BackgroundCommandTask` 代理**轮询自己的 aborted 标志**（杀后台绝不影响前台轮次），daemon 线程跑 execute、contextvars 复制上下文；`command_id=bg_*` 立即返回。`check_background_command` 返回 status/result/elapsed，`kill_background_command` 置隔离 aborted 由 runner 轮询杀进程组。code_exec handler 接 background 参数分支，两新工具入 project.py schema + PROJECT_TOOLS。
- **排障修 bug（规格驱动）：** 规格跑出 `_worker` 异常分支只写 `state['error']` 漏写 `state['result']`——异常路径 result 永远 None；修为无条件落盘（异常时存格式化错误串，与成功路径同契约）。
- **todo_list 溯源：** 该名是 **Amazon Q Developer CLI** 的内置清单工具（官方文档 aws.github.io/amazon-q-developer-cli/built-in-tools.html，命令词汇 create/complete/load/add/remove/lookup），不是 Codex（Codex 是 `update_plan`）。**自动重定向早已落地**：`_classify.py:164` 别名 todo_list→todo_write（注释精确引用其 fuzzy 分 0.737 低于拼写门 0.90 的事实）、`_transform.py:330` 命令形注入 `todos:[]` 占位 + `lib.tools.todo.apply_todo_operation` 执行状态合并、gateway 咨询别名表先于 fuzzy 门；test_tool_input_repair 三针钉死（别名、命令形注入、原生载荷不触）。用户看到的报错来自**未加载工作树别名的旧服务进程**（别名至今未提交，HEAD 版 _classify.py 无此行；纯 Python 需重启生效）。网关级实证：`resolve_catalog_name('todo_list', catalog) → ('todo_write', None error)`。
- **规格：** 新建 tests/tasks_pkg/test_ask_human_batch.py 8 针（归一化×3、批量阻塞、unattended 哨兵、<2 落单问题、abort、JSON 回复解析）、tests/background/test_background_commands.py 13 针（id 返回、完成、异常捕获、未知 id、running/completed 状态、kill 语义、容量驱逐、列表快照）；events.py 声明 `batchQuestions` 字段。
- **验证：** 两新文件 + test_tool_input_repair 165 passed；run_command/gateway 邻套 37 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效（含既有的 todo_list 别名）；前端硬刷新。

### 2026-09-06 (本地 TeX 工具链落地：.texlive + lib/paper/latex_layout，offline 取代 overleaf-mcp 编译/版面感知)

- **起因（用户决策）：** 科研工作流需要论文写作能力；overleaf-mcp 的远端编辑走 git token 桥、要付费会员，而 read/edit 基元本地已够用——缺的只是「排版感知」（页数、边距、章节落页、末页充盈度）。用户拍板：本地装 TeX 编辑器进 tofu 环境，全部本地干。
- **改动：** ① `.texlive/` 仓库内 TinyTeX/TeX Live 2026（gitignored，$HOME 只读无处装），`syslib/` 存 conda 拷贝的 libfontconfig 补 xelatex 启动依赖。② 新模块 `lib/paper/latex_layout.py`（约 700 行，零第三方依赖）——`compile_project`（latexmk 封装，synctex 常开）、`page_count`（log→PDF 正则两档）、`section_page_map`（离线 SyncTeX 解码→章节落页+末页充盈度，移植自 overleaf-mcp 的 offline 子集，HTTP 管道全丢）、`compile_and_report` 一键、CLI `python3 -m lib.paper.latex_layout <dir>`。③ `_tex_env` 生成仓库内 fontconfig：include 调用方既有 FONTCONFIG_FILE（conda 激活会 export 一个）否则 /etc/fonts/fonts.conf，追加项目目录 + TL opentype 树（TeX Gyre/Fandol）+ `.texlive/fonts`（改名克隆体）+ 仓库内 cachedir——$HOME 只读下 fc-cache 无处可写的老坑与 uv 缓存同型。④ 验证场：`data/paper-smoke/emnlp`（ACL 模板，pdflatex 9 页 PDF + 全章节落页图）与 `data/paper-smoke/thesis`（neuthesis ctex 模板，xelatex 112 页 XDV）。
- **排障实录（全是环境怪，各有出处）：** ① 系统 perl 缺 `Digest::MD5` 致 latexmk 秒崩且**不写新 log**——错误循环读到陈 log 以为包没装好；conda 环境 perl 带 MD5，latexmk `#!/usr/bin/env perl` 随 PATH 得救。② latexmk 的 fdb 记录上次失败后**拒绝重试**，包补装也不重启编译→ `compile_project` 固定加 `-g` 强制至少一轮全量（顺带保证 log 新鲜）。③ 本地 tlpdb 被中断的代理安装写坏：zref stanza 内混入孤 SHA 行+空行，文件清单被 catalogue 字段劈成两半；按 TLPOBJ 语法重建 stanza（指令→runfiles size=N→文件行→catalogue）后 tlmgr 复活。④ ctex `fontset=windows` 按名找 SimSun——项目自带字体目录进 fonts.conf 即解。⑤ **XeTeX 拒绝 fontconfig 的 family 替换**（校对返回字体族名与请求一致）：`<match>` alias 对 `\setmainfont{Times New Roman}` 永远无效——用 fontTools 把 TeX Gyre Termes/Heros/Cursor 四面各 4 个 name 表项改名进 `.texlive/fonts/`（GUST 许可要求改名，合规），Times New Roman/Arial/Courier New 按名命中。⑥ 剩余唯一失败是模板自身 bug：artratex.sty 加载 ctable，其改写的 tabularx 内部与 TL2026 array 不兼容（7 个错全在 Backmatter.tex 演示表格）——112 页 XDV 照常产出，非环境问题，不修用户模板。
- **规格：** tests/paper/test_latex_layout.py 20 针（synctex 流解析/最近行 tie 取后/标签归一/geometry/MediaBox 含 FlateDecode 对象流解压/章节扫描跳过星号嵌套/find root 三态/无 synctex fail-closed/无工具链 fail-closed/fontconfig 生成+调用方 conf 被 include+XML 转义+回退别名针/全链路 e2e 编译小文档断言页数章节末页）。
- **验证：** 本文件 20 passed；邻套 test_research_draft_protocol 9 passed；ruff 净。EMNLP e2e：9 页 PDF，section_page_map 全量落页。thesis e2e：SimSun/Times New Roman 全解，112 页 XDV。
- **生效条件：** 纯 Python 库——即装即用；`.texlive/` 不入 git（环境供给物，tlmgr 可增量装包）。
- **后续（用户保留决策）：** 是否把 compile/layout 接进 lib/tools 注册表作聊天工具（涉及 schema/确认策略/资源预算设计，另行裁定）；overleaf-mcp 仅剩实时协作场景价值。

### 2026-09-06 (暂停被压实卡住根治：L2 摘要接入任务中止信号，活任务摘要改走流式看门狗)

- **起因（用户报告）：** 暂停对话的优先级和响应速度太低，有时按下停止会被「压缩」卡住。
- **根因（两层叠加）：** ① 轮循环的中止检查点在 `before_round`，随后 dispatch 钩子先跑 `run_round_message_hygiene` → L2 压实 → `_generate_query_aware_summary` 廉价模型摘要调用——这个调用**完全没接 abort_check**，而摘要输入接近满窗时生成要十秒到分钟级，停止信号在整个摘要期间不可见，要等摘要完成、进入本轮主 LLM 调用的传输层 0.5s 轮询才被消费。 proactive 经济压实上线后该窗口在任意轮都可能命中，不再只是接近满窗时。② 更深处：非流式 `dispatch_chat` 虽收 abort_check 但只用于重试循环/排队 sleep，底层 `chat()` 是裸阻塞 `http_post`，生成中途物理上无法打断——只有流式 transport 的看门狗（ABORT_POLL_INTERVAL=0.5s）能中途断流。
- **修法（`_layer2/_summary.py`）：** 仅当 task 字典带运行期 `aborted` 键（活任务，`handle_abort_at_round_start` 直接索引该键）时：构建 `AbortSignal.from_task_flag(task).is_set` 作 abort_check；入彑前预检已中止则跳过付费调用直接返回 None；**use_stream 追加 `abort_check is not None`**——活任务摘要即使没有 UI 回调也走流式，看门狗可中途断流；两个 dispatch 调用都传 abort_check；重试梯对 `AbortedError` 特判——中止绝不走 cheap→text 补枪（轮次已在解体，第二次付费调用是纯中止延迟），info 日志返回 None（消息原样保留，压实优雅拒绝，轮循环经既有中止门以 user_abort 收场）。合成 task 字典（手动 /compact、独立调用方）无 `aborted` 键→完全不接线，保留非流式 back-compat 路径（既有针 `test_summary_no_on_delta_uses_nonstreaming`/`test_non_codex_provider_pin_is_never_overridden` 原样通过）。
- **规格：** tests/llm_dispatch/test_summary_streaming.py +4 针——活任务无 on_delta 也走流式且 abort_check 随 `task['aborted']` 翻转实时生效、中途 AbortedError 不落 text 档（calls==['cheap']）、入彑前已中止零 dispatch 调用、合成字典保持非流式且不收 abort_check。
- **验证：** 本文件 17 passed；压缩邻套 239 passed；中止/轮循环套 100 中 1 红（test_conversation_command_abort 报 `lib.chat` 无 `build_user_msg_from_payload` 属性——该测试文件与 lib/chat 在工作树均干净，HEAD 上即红，兄弟在飞，与本改动无关）；ruff 净。
- **文档：** docs/CONTEXT_COMPACTION.md「Local L2 summary dispatch」段补中止语义。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (Pareto 前沿标签重叠根治：兑底分支放弃碰撞检查 → 爬升+侧滑直到避开所有障碍)

- **起因（用户截图）：** 成本×AA 图上 qwen3.5-9b 与 DeepSeek R1 Distill Qwen 7B 两张贴纸叠在一起。
- **根因：** `layoutFrontierLabels`（pareto-chart.ts）每张贴纸只在点位正上方试 8 行；前沿接近竖直时（同价位挤一串模型，红线+已放贴纸很快堵死 8 行）走兜底分支——兜底按公式硬钉位置、完全不做碰撞检查，后放的贴纸直接压在前放的上面。
- **修法：** 提取统一 `collides()` 闭包（已放贴纸含 CHIP_MIN_GAP_X 水平间隔 + 前沿线段 + 标记点）；8 行用完后继续向上爬行并逐行向两侧滑移（步长 CHIP_ROW_PITCH，范围覆盖整个绘图宽度），第一个无碰撞位置胜出；水平位移自动触发现有 `leader` 引线逻辑（|cx - anchor.x| > 2）。只有整条前沿上方被塞实时才退回旧的钉顶行为。
- **规格：** 新建 tests/evaluations/test_pareto_frontier_labels.py——竖直障碍墙逼出全部 8 行阻挡，断言两贴纸不重叠（含 10px 水平间隔）、不出界、移位贴纸带引线；无阻挡锚点保持居中 row 0 无引线（常见路径不变）。
- **验证：** 新旧 pareto 测试 2 passed；ruff 净；typecheck:modules 报错全部限于兄弟在飞的 human-guidance-actions.ts（本改动文件零错误）；build:frontend + verify:frontend 绿，新 bundle main-O_n4YvYA.js；bundle_manifest_freshness 6 passed。
- **生效条件：** 浏览器硬刷新生效。

### 2026-09-06 (harness 工具设计灵感落地四件：timeout_s 单位后缀、schema 瘦身、write_file append、fetch_url extract)

- **起因（用户裁定）：** 各家 harness 工具设计横评后，用户拍板六条灵感全部采纳。本轮落地其中四件自含项；两件涉及前端面的（Claude Code questions[] 真批量表单、Codex 后台命令一等公民）裁定留作独立迭代——questions[] 的价值在「一张表单一次交互」，退化成单卡顺序追问等于没有收编，不做半成品。
- **#4 run_command timeout→timeout_s：** 单位歧义是真实伤口——Claude Code Bash 毫秒（默认 120000）直通曾被当秒变成 33 小时，此前 repair 管线只能靠 `>86400` 启发式猜。schema 主推 `timeout_s`（单位进参数名，Codex `yield_time_ms` 同款约定），旧拼写 `timeout` 进 `_PARAM_ALIASES` 作别名兼容；毫秒换算守卫只挂在歧义拼写上——显式声明秒的 `timeout_s: 120000` 不被误换算。消费端四处双键读取（project_mod/tools ×2、远端桥 ×2），远端桌面协议前归一 timeout_s→timeout（桌面只认老键）。
- **#3 schema 瘦身：** 五个最重描述原地压缩（行为契约一字不丢）：apply_diff 859→559、apply_diffs 728→506、insert_content 1093→688、insert_contents 799→585、fetch_url 1171→1052。read-before-edit 门禁六处重复散文收成统一短句；insert_content 的例子枚举删除（路由语义保留）。test_tool_audit_tranche1 钉死的 run_command 散文（'NO default timeout'/'avoid `cd`'/'no persistent'）不动。
- **#5 write_file append=true：** Amazon Q 灵感。ops 层 final=old+new 一次原子写；**关键针是 freshness token 必须哈希最终字节**（old+new），否则同会话下一次写被共享 HEAD 守卫误判过期拒写（test_second_write_same_conv_after_append_not_refused 专钉）。成功消息保 'File created:/updated:' 前缀（meta.py 与 loop-breaker 模式匹配）+ `[appended +SIZE]`。远端路由 fail-closed：老版本桌面 agent 会把 append 静默忽略变覆盖——宁可报错不可丢数据。**刻意不映射 Amazon Q fs_write**：其 command 词汇（create/str_replace/insert/append）横跨多个原生工具，既有收编纪律不变。
- **#1 fetch_url extract 模式：** Gemini web_fetch 灵感。新模块 `lib/search/search_extract.py`——单次 temperature=0、max_tokens≤1500、thinking 关闭、页面截断 48k 的有界问答（复用 require_verified_provider_stream_result 终态校验 + facade dispatch_stream 测试缝）。fail-soft 契约在调用侧：提取失败回落原始页面（已付费的抓取永不丢），只有空问题/空页面/空回复才 raise。schema 标注单 URL 模式限定（批模式忽略，与 reason 同纪律）。
- **规格：** 新建 test_write_file_append.py 5 针、test_fetch_url_extract.py 8 针；test_tool_input_repair.py timeout 三针改写 + 新增两针（timeout_s 显式秒不换算、双键 timeout_s 胜）；test_no_backend_timeouts.py 断言键改 timeout_s。
- **验证：** repair+gateway+timeouts+append 229 passed；extract+fetch 邻套 15 passed；写工具邻套 70/71（test_tracked_text_files_stay_under_hash_threshold 红=兄弟 cargo build 产物超阈值，与本改动无关）；run_command 邻套 158 passed；audit/project_tools/gate 152 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (Modelplaza 新上批次核对：8 卡 7 已在册，补 glm-5.3-flash；镜像提示经人审推翻)

- **起因（用户截图）：** Modelplaza「新上」8 卡（Kimi-0902-preview / Gemini-3.8-Flash / Claude-Fable-5.1 / Hy4-preview / GLM-5.3-Flash / DeepSeek-V4-Flash-Vision-Exp / Qwen3.8-27B / GLM-5.3），要求更新美团服务商模板。
- **核对结论：** 7 个早已在 meituan.json（上一波 plaza 同步产物，cost 公式=(输入¥+输出¥)/2÷7.2÷1000 逐一复核吻合）；唯一缺 **glm-5.3-flash**——它 09-05 已随阵容更新进了 glm.json 厂商模板、_tables.py 静态定价行（¥0.8/¥2.8 厂商价）与 _release.py，但美团网关侧一直没补。
- **同步工具行为（设计内的人工决策点）：** `scripts/meituan_template_sync.py` 实弹探测 126 wire（glm-5.3-flash 判定 rate_limited=served，429 报文带模型名），但 `_is_mirror('glm-5.3-flash','glm-5.3')` 包含式命中 → 只给 mirror hint 不加条目。hint 在本例是**误报**：flash 是独立模型（多模态、广场价 ¥0.4/¥1.4 vs 旗舰 ¥8/¥28），折进 glm-5.3 的 request_ids 会错价错能力——按工具语义人工推翻，手工按 build_recipe 约定补条目。
- **改动：** ① meituan.json plaza 区块按字母序插 glm-5.3-flash：caps [text, vision, video, thinking, cheap]（名字派生 thinking/cheap + 卡片标签 图像理解/视频理解）、rpm 100（_infer_rpm flash 档）、cost 0.000125（广场价折算）、ctx 1000000 meituan_plaza 非精确。② test_meituan_served_catalog_sync.py：_EXPECTED_NEW +glm-5.3-flash 钉能力集、_EXPECTED_PRICED +glm-5.3-flash。③ 顺手修既有红：_EXPECTED_NEW['grok-4.6'] 补 'cheap'——0da131e83 配方+定价行已落地 cheap 但漏更针（git show HEAD 实证先于本改动存在）。
- **不碰：** _tables.py 静态行（厂商价，与广场价双轨并存，kimi-0902-preview 同例：recipe cost 走广场价、静态行可缺省）；_context.py（glm-5.3 家族规则已解析 1M vendor_official）；_aliases（非镜像，不建别名组）。
- **验证：** served_catalog+template_sync+recipes+world_catalog 36 passed；pricing/release 邻套 51 passed；context_profile 契约 12 passed。
- **生效条件：** 经 supervisor `/restart-deferred` 重启 worker 生效（startup 世界目录同步重跑）；选择器热重载即时可见。

### 2026-09-06 (官方模型缺价格根治：public_projection 读边界注入 official_pricing + pro 同分机制澄清)

- **起因（用户截图追问）：** 模型页 gpt-6-astra / claude-fable-5-1 / gemini-3.8-flash 无价格，pro 与非 pro 同 AA 分——「不是自动抓取、热更新的吗」。
- **根因（三条独立机制叠加，管道本身没坏，feed 缓存里这些模型都有价）：** ① 目录同步 **insert-only**——世界目录首次同步把官方模型插进注册表时不带 list_pricing，之后同步只插新行、永不更新已有行，价格冻结为空；② Gemini 3.8 Flash 在 OpenRouter feed 是占位行，feed 合并 **authored 恒赢**，官方价进不来；③ AA 分数按**前缀匹配**继承，pro 变体与基础模型命中同一 AA id，所以 54.7/51.3 成对出现——这是分数机制的设计行为，不是 bug。
- **设计判断（投影 vs 改写入策略）：** 不改 insert-only 同步、不改 authored 恒赢（owner 状态权威不被自动覆盖），改在**读边界投影**补价——与 `release_date` 同一层。字段命名 `official_pricing` 区别于持久化 `list_pricing`，客户端回显也不会把投影值固化成 owner 状态。附带实证 `lib.pricing` 覆盖：gpt-6-astra($10/$50)、gpt-5.6-sol($5/$30)、claude-fable-5-1($10/$50, cacheRead 0.025)、claude-opus-5($5/$25)、gemini-3.8-flash($1.5/$7.5)；`-pro` 变体无官方行但有各自 list_pricing，**刻意不做基价回落**（不同 SKU，回落=虚报价格）。
- **改动：** ① `lib/model_routing/domain.py` `public_projection` 输出 `official_pricing`（查 `lib.pricing` 官方价目表）。② 前端 `model-catalog/types.ts` 加字段类型；`model_catalog_tab.js` `catalogDocument` 投影转发；展示层 `official_pricing ?? list_pricing`；`pareto-chart.ts` 同步处理新字段。
- **规格：** test_model_routing_contract.py 加投影针；test_model_catalog_tab.py 加 official_pricing 转发针。
- **验证：** 两文件 31 passed；邻套 5 件 119 passed；typecheck:modules 净；check:runtime 绿（settings-presenters.generated.js 需重新 compose——改了 runtime section 后 `--check` 会报 stale，先跑 `node scripts/compose_frontend_runtime.mjs`）；build:frontend + verify:frontend 绿，新 bundle main-KoxU-npa.js；ruff 净；bundle_manifest_freshness 6 passed。
- **生效条件：** 两段式——Python 侧已触发 supervisor `/restart-deferred`（等静默后换装）；前端浏览器硬刷新。

### 2026-09-06 (Claude Code / Codex 基础工具收编：Task/Agent 子代理、Bash 毫秒超时守卫、description caption 回填)

- **起因（用户追问）：** 各家 harness 基础工具收编后，用户点名问 Claude Code 和 Codex 还差什么。审计发现四个真实缺口：① Claude Code `Task`/OpenCode `task`/Kimi `Agent` 三家共享单子代理信封 `{description, prompt, subagent_type?}` 未收编；② Claude Code `Bash` 超时单位是**毫秒**（默认 120000），直通会被当秒变成 33 小时；③ Codex 旧版 `shell` 的 `command` 是 argv 数组，原生 run_command 只吃字符串；④ 最痛的——Claude Code `Write`/`Edit`/`MultiEdit` 全都没有 `description` 键，而 write_file/apply_diff/edit_file 顶层、apply_diffs 条目均 required，外来编辑调用此前一律 fail-closed 白跑一轮。
- **设计判断（caption 回填 vs 形状守卫）：** description 是展示性 UI 元数据不是执行语义，缺它硬拒=为最常见的外来工具调用付出一整轮重试。回填从目标路径机械派生（`Edit a.py`/`Write a.py`/`Edit N files`），与 Amazon Q 命令形注入 `todos:[]` 占位同一先例。但**只在载荷带外来键拼写时点火**（`_FOREIGN_EDIT_KEY_SPELLINGS`）——原生 schema 模型看得到 required description，缺它是模型错误，依然诚实报错；「原生载荷永不被改」不变量对破损原生调用同样成立。
- **改动（复用三层架构前两层）：** ① `_classify.py` 别名 +2：`task`/`agent`→spawn_agents。② 参数别名：load_skill +`skill`→skill_id（Claude Code Skill 工具，`args` 惰性）。③ `_transform.py` 新变换 +4：`_transform_spawn_agents`（prompt→单条 agents[0].objective；subagent_type 各家自由串无可靠 role 映射，惰性透传）；`_transform_run_command`（command/cmd argv 数组 join + `timeout>86400` 判毫秒换算——无 harness 会设 >24h 工具超时）；`_transform_grep_search`（output_mode count/count_matches→count_only，content/files_with_matches 惰性超集）；`_backfill_caption` + write_file/apply_diff/apply_diffs/edit_file 四个 composer。注册表每工具单函数、多步 composer 串联；apply_diff 的 foreign 判定排除 `edit` 键改用 `changed or 外来拼写`（保住「原生+杂散 edit 键不触」既有针）。
- **既有针更新 6 处（全部外来键精确相等断言，caption 是预期新产物）：** write_file body keys、apply_diff Claude/OpenCode 键、MultiEdit 两条目、gateway 假目录（补 description 属性+期望值）、swarm 直连路径。
- **规格：** test_tool_input_repair.py +16 针（task/agent 别名、Task 信封 unwrap、无 prompt 不捏、skill 参数别名、argv join×2、毫秒换算、原生 timeout 不触、output_mode 两向、caption 回填×3、foreign 门负向×2、已有 description 不覆盖）。
- **验证：** repair+gateway+compat+dispatch 236 passed；邻套签名组 116 passed 中 2 红（test_conv_ref_raw_default）经 stash 实证与本改动无关（兄弟在飞）；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (Auto Research 收尾步表单瘦身：协议一键从证据起草 + 高级字段折叠 + 建议绑定一键采纳)

- **起因（用户截图+要求）：** Auto Research 最后一步「立项与冻结实验协议」有十余个空白字段（主指标/基线/数据集/证伪器/种子/环境/评测/停止条件/预算），用户要求简化体验并提升最终产出质量。
- **设计判断：** 管线此前已为证据付过费（survey、open-gap map、accepted ideas 带机制级新颖性论证）——空白表单等于让 owner 凭记忆重填证据里已有的内容。修法不是删字段，而是**让草稿从证据来**：一次有界、无工具的模型调用把持久化的 survey/gap/idea 折成完整协议草稿，owner 审阅修改后经既有版本化 workspace PUT 冻结（写权限威不变，草稿永不自持久化）。
- **改动：** ① 新模块 `lib/research/draft.py`（179 行）——`draft_protocol(direction, lang, idea_id)`：加载持久化证据（无持久化工件/未知 idea → ValueError fail-closed），单次 temperature=0、max_tokens≤4000、thinking 关闭的结构化合成，复用 `require_verified_provider_stream_result` 终态校验与 `normalize_program_fields` 归一（与 PUT 同一契约）；hypothesis 缺省回落 idea 的 falsifiable_prediction→novelty_claim；模型回复无 JSON/全空 → ValueError；audit_log 记录。② 路由 `POST /api/v1/research/workspace/draft-protocol`（sync handler，阻塞模型调用让出事件循环；@api_meta 文档化；docs/contracts/API_CONTRACT.md §10 补行）。③ 前端 `research-workspace.ts`：01 区核心网格收敛为候选/假设/主指标/基线/数据集/证伪器六格 + 「从候选生成协议草稿」按钮（drafting 态禁用），种子/环境/预算/评测/停止条件折进 `<details class="rsw-advanced">`；02 区新增「采纳建议绑定」——按 catalog 建议分数为每个未绑定 capability 选最高分合格工具（write capability 跳过只读工具）填入绑定行，保存才授权（hint 语义不变）；新增非阻塞 notice 条（draftApplied/autobindDone/autobindNone）。④ api.js 加 `draftProtocol`，locales zh/en 各 +8 键，CSS +`.rsw-draft-bar`/`.rsw-advanced`。
- **踩坑：** edit_file 的 insert_after 锚在函数签名行会把新函数插进该函数体（TS1184），锚应选签名行之前的空隙或改用 insert_before。
- **规格：** 新建 tests/research/test_research_draft_protocol.py 9 针（归一化+证据入 prompt+有界参数、位置 id 回落、hypothesis 三级回落、无工件/未知 idea/缺参 fail-closed、垃圾回复与全空回复拒收、禁写持久化 tripwire、真 HTTP 栈 200/400×2）；test_research_production_workspace.py +1 针静态契约（draft 按钮/api 路由/advanced details/autobind）。
- **验证：** 新规格 9 passed；research 邻套+i18n 共 55 passed（一轮 8 红为兄弟在飞 mid-write 的 tool_input_repair 导入瞬断，复跑全绿）；typecheck:modules 净；ruff 净；build:frontend + verify:frontend 绿，新 bundle main-Dg8gkm6F.js。
- **生效条件：** Python 侧重启 server.py 生效；前端浏览器硬刷新生效。

### 2026-09-06 (延期重启真正等静默：supervisor 轮询 worker /health/quiescence，任务清零才换 worker)

- **起因（用户裁定）：** 「deferred restart 为什么每次都直接重启？」——用户要求 defer 必须等所有任务跑完才允许重启。
- **根因：** `supervisor.py schedule_deferred_restart` 的 defer 只保证 202 先送达（ack 窗口 0.35s），随后无条件 `manager.restart()`——「延期」延的只是响应回包，不是任务静默。更新链路的 running-task 守卫（update/restart 409）只挡交互式立即重启，走 supervisor `/restart-deferred` 的通道完全不看任务表——#2199-#2203 五条「Execution was interrupted by server restart」均由此产生。
- **改动：** ① worker 新增 `GET /health/quiescence`（routes/common.py）——进程级在跑任务数（与 update 重启守卫同一个判定 `list_running_tasks()`），注册表异常 fail-busy 报 `quiet:false, runningTasks:null`（宁可等，不可杀看不见的任务）；路径入 `_QuietPollFilter` 前缀名单，2s 轮询不刷访问日志。② supervisor `_await_worker_quiescence`：ack 窗口后轮询 worker 探针（scheme 随 `.last_serve_mode`，复用 `server_manager._read_probe_json`），quiet 才 restart；默认最长等 3600s、2s 间隔（`TOFU_SUPERVISOR_DEFER_QUIESCE_MAX_WAIT_S`/`_POLL_S`）。fail-open 保持向后兼容：老 worker 无探针（404）、worker 不可达、载荷畸形、探针自身异常 → 'unknown'/'error' 记 warning 后照旧重启——重启永不被静默机制卡死；timeout 同样记 warning 续行（defer 的语义是「等」，不是「取消」）。③ 202 应答带 `waitsForQuiescence:true`。
- **规格：** test_supervisor.py +3（busy×2→quiet 集成穿 schedule、三态 outcomes、探针 404/quiet 语义）；test_orchestrator_health_probes.py +2（计数往返、注册表异常 fail-busy）。
- **验证：** supervisor+probes+update_guard 55 passed；logging+legacy_api 11 passed；ruff 净。
- **生效条件：** 两段式——supervisor 经 `refresh_supervisor` 热 reload 即装载等待逻辑（worker 不动）；但探针端点要下次 worker 重启才存在，**下一次**重启仍是旧行为（unknown→立即，部署本改动不可避免的一次），再往后所有 deferred restart 都会等任务清零。

### 2026-09-06 (科研铸造台落地页重构：删营销式 hero/运行范式侧栏，改为单列任务首屏)

- **起因（用户截图+要求）：** 用户打叉右侧「研究运行范式」五步面板、「01 — 有检查点·可检查·可恢复」条，指出首页冗余元素过多、巨大标题无信息量，要求整体重新设计。
- **改动：** ① `research-view.ts` `showResearchLanding()` 重写——删除 `landingCapability()` 及整个 `.rs-capabilities` 侧栏、`.rs-hero-kicker`、`.rs-hero-proof`；hero 只剩品牌行 + 功能陈述标题 + 一段流程说明 + 方向输入框 + 模板 chips，下方直联科研档案。② 文案：heroTitle 从营销口号改为输入→产出契约（zh「从研究方向，到经评审的实验简报。」/ en 同步）；heroBody 把被删侧栏的独立评审步骤折进流程描述。③ i18n：zh/en 各删 8 个废弃键（heroKicker/heroProof/capabilitiesLabel/capability×5），契约 4814→4806 键。④ `03-paper-reader.css`：`.rs-landing` 收窄为 920px 居中，`.rs-hero` 去双列 grid/min-height 570px 改内容自适应单卡片，标题 clamp(61px)→clamp(32px)，grid 纹理 mask 改为向下淡出，删 capabilities/hero-proof/kicker 全部规则及两个媒体查询中的对应条目。
- **验证：** check:i18n 封闭世界 4806 键过；check:styles 32 sections 过；check:runtime 165 sections + lazy bindings + reachability + TDZ 全绿；typecheck:modules 仅 research-workspace.ts 2 红（`_draftResearchProtocol`/`_autobindResearchCapabilities`——兄弟在飞的未提交 WIP，本改动未触该文件）；build:frontend + verify:frontend 绿，新 bundle main-p6zISEb-.js；pytest bundle_manifest_freshness + visual_surfaces 13 passed。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-06 (各家 harness 基础工具词汇收编：编辑/读取/搜索/抓取/提问/技能六族进重定向管线)

- **起因（用户要求）：** 清单工具收编后，用户要求横向把各家 harness 的编辑、读取、执行命令、网络搜索等基础工具全部过一遍，能 1:1 映射的都进鲁棒性修复管线。
- **调研结论（一手源码实证）：** ① Kimi CLI：编辑=`StrReplaceFile`（`{path, edit:{old,new,replace_all?}|[...]}`，src/kimi_cli/tools/file/replace.py），读取=`ReadFile`（`line_offset`/`n_lines`），搜索=`Grep`（`glob` 文件过滤 + `head_limit`），网工具=`SearchWeb`/`FetchURL`。② OpenCode：编辑=`edit`（`filePath`/`oldString`/`newString`/`replaceAll`），读取=`read`（`offset`/`limit`），提问=`question`（与 Claude Code AskUserQuestion 同 `questions[]` 信封），技能=`skill`（`{name}`）。③ Gemini CLI：抓取=`web_fetch`（主模式 `{prompt}` 不可映射，直连 `{url}` 可收）。④ 刻意不收编并文档化：Codex `apply_patch`（lark 自由文法）、Amazon Q `fs_read`/`fs_write`（mode 标签命令词汇跨多个原生工具）、Gemini `web_fetch` prompt-only 载荷（fail-closed 报 required key）。
- **改动（复用三层架构前两层）：** ① `_classify.py` 别名 +6：`strreplacefile`→apply_diff（edit_file 会话走 convergence 集合）、`searchweb`→web_search、`fetchurl`→fetch_url、`web_fetch`→fetch_url、`question`→ask_human（questions[] 信封与既有 ask_human 变换同构）、`skill`→load_skill。② 参数别名：apply_diff/write_file/read_files +`filePath`→path，apply_diff +`oldString`/`newString`/`replaceAll`，grep_search +`glob`→include、`head_limit`→max_results（Kimi `ignore_case` 无对应键惰性透传——本工具默认大小写不敏感），find_files +`directory`→path，load_skill +`name`→skill_id。③ 结构变换 +2：`_transform_str_replace_file_to_apply_diff`（Kimi 单 edit 信封拆包，list 形透传 fail-closed——单编辑工具不可表达批量）；`_transform_foreign_read_range`（Claude/OpenCode `offset`/`limit` 与 Kimi `line_offset`/`n_lines` 统一映射到 start_line/end_line，正整数守卫排除 bool，原生键在场永不点火）。④ edit_file 统一变换链扩展：路径链 +`filePath`、search/replace 链尾 +`old`/`new`、单候选触发元组 +`oldString`、新增 Kimi `edit` 信封分支（list 形逐条归一）、`replaceAll`→`replace_all` 落位。
- **规格：** test_tool_input_repair.py +16 针（名称别名×2 组含 edit_file convergence、Kimi 信封→apply_diff/edit_file、list 形透传不半映射、原生+杂散 edit 键不触、OpenCode 键→edit_file/apply_diff、offset/limit 与 line_offset/n_lines 两族映射、原生范围键优先、question 信封 unwrap、grep glob/head_limit、find directory、skill name）。
- **验证：** repair+gateway 187 passed；邻套六件 108 passed 中 2 红签名逐字=已归因兄弟在飞（todo schema 329>325 token 预算、`_repaired` tool_start 事件违约——本改动对 TODO_WRITE_TOOL/events +0 字节）；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (Muse Spark 1.3 不入注册表：meta 模板 contributor SKU 撞 identity，fail-closed 编译跳过整个模板)

- **起因（用户追问）：** Muse Spark 1.3 已发布，模型配置页却一直没有——用户问模型列表为何不自动更新。
- **根因（三层更新机制全部落空）：** ① 世界目录同步只编译包内模板——meta.json（09-05 authored）虽带了 muse-spark-1.3，但同文件把 `-contributor` SKU 写成第二个 recipe；`_RELAY_CHANNEL_SUFFIX`（_creator_identity.py:108，09-02 引入）把 `-contributor` 归类为接入渠道后缀（同一模型的接入产品，非独立模型），两个 recipe 折叠成同一 identity `meta/muse-spark-1.3`，migration 报 duplicate offering identity，compile fail-closed → **整个 meta 模板被跳过**（app.log 两次启动均有同名 warning），1.3 与 contributor 都不入注册表。② OpenRouter 动态 feed 里 meta 刻意不映射（Muse id ≠ meta-llama 前缀）。③ provider catalog sync 只覆盖已配置服务商。附带发现：选择器虽能热重载显示该模板，但两个模型全选接入会在 compile 时报同样的错——模板本身也不可达。
- **改动：** meta.json 按契约（provider_template_recipes.py:60「一个逻辑模型一条 offering，wire 拼写进 request_ids」）把 contributor 折进 `request_ids: ["muse-spark-1.3", "muse-spark-1.3-contributor"]`（meituan.json glm-5.1/glm-5.1-huawei 同例）；编译产物=1 个 offering + 2 条 wire deployment。守卫缺口：`test_every_shared_builtin_compiles_an_onboarding_bundle` 此前只选每个模板第一条 recipe 编译，永远触不到跨 recipe identity 冲突——改为全量编译（世界同步同款形状，改动前对 meta 必挂）；test_world_catalog_sync 补 muse-spark-1.3 + creator meta 断言。
- **验证：** recipes+world_catalog 15 passed；邻近套件（feed/pricing/access_matrix/model_add/convergence/intelligence×2）137 passed；routing 契约+model_info 96 passed 中 1 红为 test_capability_taxonomy_parity 前端源码断言（provider_render.js 兄弟在飞，本改动未触前端）；ruff 净。重启后日志确认 muse-spark-1.3 入注册表。
- **生效条件：** 经 supervisor `/restart-deferred` 重启 worker 生效（startup 世界同步重跑）；选择器热重载即时可见单条 muse-spark-1.3（contributor 作为 wire 拼写随 offering 携带）。

### 2026-09-06 (模型页/服务商页协调机制复审：激活重渲把防陈旧从约定变成结构 + 守卫测试)

- **起因（用户要求）：** 拆分落地后复审两页协调机制，防止未来再犯「改了模型只渲服务商页」这类错误。
- **审计结论：** 共享权威 `_stgModelRouting` + 单飞 `_loadModelRoutingAuthority()` 无竞争；三个改 creators/models 的流（openSettings、订阅同步、模板 bundle 接入）全部走 `_renderModelRoutingTabs()` 双渲；纯服务商编辑（凭证/矩阵/卡片）单渲语义正确；模型页投影是数据边界（typed owner 类型上拿不到 Provider 字段）。
- **缝隙 1（承重约定）：** `switchSettingsTab` 只切 CSS 类不重渲——「模型页不陈旧」此前完全靠每个流记得调双渲的人肉约定。修法：模型 tab 激活时重渲（core_panel.js switchSettingsTab），与 preferences/speech/devices 既有 populate-on-switch 惯例同构；模型页只读（搜索框在静态 HTML 外），重渲永安全。服务商页刻意不加——卡片内有未保存的半截 API key 输入，激活重渲会丢输入。
- **缝隙 2：** core_panel.js openSettings 回调里 `_renderModelRoutingTabs()` 裸调，与 `_refreshSubscriptionModelCatalog` 的 typeof 守卫不一致——已对齐。
- **守卫：** 新建 tests/model_routing/test_model_catalog_tab.py 6 针静态契约——模型页不碰 stgProviderList/不定义服务商渲染器、服务商页不碰 stgModelCatalog/不定义模型页渲染器、三个渲染入口全 sections 唯一定义、switchSettingsTab 激活重渲模型页且不渲服务商页、catalogDocument 投影排除 providers/connections/credentials/offerings/deployments/aliases/routes。
- **验证：** 新守卫 6 passed；邻近套件 109 passed；check:runtime 165 sections 全绿（settings-presenters 已重组）；build:frontend + verify:frontend 绿，新 bundle main-D4tX3ud4.js。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-06 (服务商模板动态 feed：OpenRouter 公开目录热更新进选择器，退役模型永不复活)

- **起因（用户截图+要求）：** 设置 → 接入矩阵里 DeepSeek 模板只有 deepseek-chat/reasoner/v4-flash/v4-pro 四行（其中前两个官方已下线），用户问为何模板还停在老模型，要求所有服务商模板支持自动热更新、直接用动态数据。
- **根因：** 模板目录（`lib/model_info/data/*.json` + `static/provider_templates/*.json`）是随发布打包的手工维护 JSON——热重载只解决「改了不用重启」，没有任何机制自动写入新内容。配置后另有 `provider_catalog_sync` 用已配置凭证 re-list `/models`，但首次接入前的选择器目录只能等发版。
- **改动（新模块 `lib/provider/provider_template_feed.py`，362 行）：** 后台刷新把 **OpenRouter 公开模型目录**（无需凭证）折叠进选择器——`VENDOR_PREFIX_BY_TEMPLATE` 映射 16 家自有品牌（deepseek→deepseek、kimi→moonshotai、glm→z-ai、gemini→google…），目录行转成候选 offering recipe（context_window、per-token 定价 ×1e6 转 per-1M USD、架构/参数推 capabilities 并 union 名称模式注册表、cheap 档由既有定价权威重估）。`load_provider_templates()` 每次读合并缓存并在过期/缺失时踢单飞 daemon 刷新（TTL 默认 24h，`TOFU_TEMPLATE_FEED_TTL_S`）；缓存落 `data/config/provider_template_feed.json`（原子写、无秘密、随 data/ 导出排除）。安全轨：authored 同 model_id 恒赢；模板可 author `retired_model_ids` 封杀（deepseek.json 钉死 chat/reasoner）；日期 checkpoint（-0813/-20250805/-2026-04-01）与计费变体（:free/:batch）不导入；抓取/解析/写盘任何失败保留 authored 目录原样，选择器永不阻塞网络；`TOFU_TEMPLATE_FEED=0` 整体关闭（conftest 默认关，与 TOFU_MODEL_CATALOG_SYNC 同例）。serving 云/聚合器/前缀对不上的品牌（groq、meta、doubao、nvidia、meituan…）刻意不映射，保持纯 authored。
- **踩坑：** ① `write_text_atomic` 用同目录 mkstemp——`config_path` 不建目录，首写必 FileNotFoundError，`refresh_now` 需先 `ensure_config_dir()`；② 测试首版把 kimi 目录行写成 `kimi/` 前缀，OpenRouter 实际拼写是 `moonshotai/`。
- **规格：** 新建 tests/providers/test_provider_template_feed.py 15 针（目录映射/日期+计费变体丢弃/无价不贴 cheap/authored 赢+退役不复活/未映射模板不动/禁用绕过/缓存逐行重校验/refresh 成功回写往返/三失败 fail-closed/禁用不抓/单飞调度+异常复位/loader 端到端合并+禁用忽略缓存）；契约 `provider_offering_recipe_v1.schema.json` 补 `retired_model_ids` 属性；docs/PROVIDER_TEMPLATE_RECIPES.md 新增 Dynamic feed 章节（与 provider catalog sync 的职责划清：接入前公开数据 vs 接入后认证 `/models`）。
- **验证：** 新规格 15 passed；模板家族 recipes+retirement+brand_icons+model_add+access_matrix 55 passed；消费者组 143 passed 中 4 红全部归因兄弟在飞/历史遗留（tofu-db target-differential 构建产物被 track 3 针——与 09-05 期刊签名逐字一致；meituan.json 部署名 model_id 1 针——该文件本改动未触）；ruff 净。真机冒烟：refresh_now 抓得 16 模板 214 模型，deepseek 选择器出现 v3.2/r1/terminus 等行且 chat/reasoner 保持死亡。
- **生效条件：** 纯 Python——重启 server.py 生效；选择器下次打开即见合并目录，缓存在后台按 TTL 自新。

### 2026-09-06 (模型页/服务商页根治拆分：模型页独立 owner + 双向注释/文档清淤)

- **起因（用户裁定+要求）：** LLM 改配置时长期分不清「模型」页与「服务商」页。用户裁定语义边界：模型页=全局只读 Creator/Model 注册表（世界上存在即在列，按开发厂商分组，世界目录同步+订阅同步保鲜）；服务商页=API 供给，一家公司供多模型，单向绑定到模型。要求彻底捋清、根治、删掉过时注释。
- **现状核查：** docs/MODEL_REGISTRATION.md 的 Browser projection 一节早已写下同一契约（边界本就是设计意图），但浏览器侧实现未跟上：`provider_render.js` 同时渲两页，模型页是服务商渲染函数的副作用；`access_matrix.js` 头部注释自述「不存在独立模型页」；`lib/model_catalog/__init__.py` docstring 谎称「只剩 _creator_identity」（aa/openrouter enrichment 仍在 model_intelligence 路由服役）；模型 tab 按钮是 settings 里唯一硬编码中文、不走 i18n 的 tab；`settings.providersTitle` 是死键。
- **改动：** ① 新建 `settings/model_catalog_tab.js`（manifest 注册，165 sections）——模型页唯一 owner：`_renderModelCatalogTab()` 渲 `#settingsTab_models`，交予 typed feature（`features/model-catalog/panel.ts`，owner 钩子早已注册在 settings.ts）的是 Creator/Model-only 投影，数据边界而非视图约定；`_renderModelRoutingTabs()` 双渲助手按名重绘两页。② `provider_render.js` 删除模型页渲染（含 fallback 渲染器），只留服务商供给；头部注释重写为镜像边界说明。③ 调用点分类四处走双渲（打开设置、订阅同步刷新、模板 bundle 接入、删除服务商——都会重载权威或改 creators/models），纯服务商编辑（凭证/连接/offering/矩阵授权）保持 `_renderProvidersTab()` 单渲。④ index.html 模型 tab 接 `data-i18n="settings.tabModels"`，zh/en 加键；删死键 `settings.providersTitle`。⑤ access_matrix.js 头部注释改为指向模型页（矩阵=provider 域内供给管理，刻意 provider-scoped）。⑥ `lib/model_catalog/__init__.py` docstring 如实描述（身份/匹配辅助 + AA/OpenRouter enrichment；注册表状态权威在 lib/model_routing）。⑦ FRONTEND_ARCHITECTURE.md 表格：服务商行加「provider-scoped supply only/never re-renders 模型 tab」，新增模型页契约行。
- **验证：** i18n 契约 4814 键 + usage 封闭世界；check:runtime 165 sections + lazy bindings + reachability + TDZ 全绿；typecheck:modules 净；access_matrix+provider_model_add+style_composition+world_catalog_sync+creator_identity 105 passed，model_intelligence aa/openrouter+model_group_convergence 17 passed；build:frontend + verify:frontend 绿，新 bundle main-Z1VL-5vK.js。
- **生效条件：** 纯前端+注释——浏览器硬刷新生效。

### 2026-09-06 (Claude Code / OpenCode / Gemini / Codex 清单工具全部纳入 todo_write 重定向)

- **起因（用户要求）：** 横向调研 Codex、Claude Code 及各家 harness 的工具列表，把需要进鲁棒性修复管线的都收编。
- **调研结论（全部一手源码/官方文档实证）：** ① Codex CLI 计划工具是 **`update_plan`**——openai/codex `codex-rs/core/src/tools/handlers/plan_spec.rs`：`{plan: [{step, status: pending|in_progress|completed}], explanation?}`，required `["plan"]`。② Claude Code 清单工具是 **`TodoWrite`**——`{todos: [{content, status, activeForm}]}`，无 id，形状与原生仅差 id。③ OpenCode 是 **`todowrite`**（dev 分支已删 `todoread`）——`{todos: [{content, status, priority}]}`，状态多一个 `cancelled`。④ Gemini CLI 是 **`write_todos`**——packages/core/src/tools/write-todos.ts：`{todos: [{description, status}]}`，校验器另收 `blocked`（原生合法）。Cline 无清单工具（plan_mode_respond）、Aider 无 function-calling 默认、Cursor/Windsurf 仅有泄露版不可考——均无可收编目标。
- **改动（复用既有三层架构前两层）：** ① `_classify.py` 别名 +7：`todowrite`（Claude Code 与 OpenCode casefold 同键）、`write_todos`、`update_plan` 指向 todo_write；顺手收编 Gemini `run_shell_command`→run_command、`google_web_search`→web_search、`read_many_files`→read_files（paths→reads 参数别名既有）、Codex `view_image`→inspect_image（path 1:1）。② `_transform.py` 新增两个变换串入 `_transform_todo_write` 链：`_transform_update_plan_to_todo_write`（envelope `plan`→`todos`，`explanation` 惰性透传）+ `_transform_harness_todo_item_vocabulary`（逐项 `step`/`description`→`content` 有真 content 不覆盖；`cancelled`/`canceled` 条目直接删除——Gemini/OpenCode 语义=离开追踪列表，与全量 sync 同构，映射 completed 会谎报完成；外来标记集合 {step, description, priority, activeForm} 任一命中即按位补 id `str(i+1)`）。关键事实：未知键在本管线是惰性透传不是硬拒（apply_diff file_path 事件实证），故 `priority`/`explanation`/`activeForm` 无需剥离；条目 required `[id, content, status]` 才是 fail-closed 点。Claude Code 无 activeForm 的残缺调用无外来标记，仍落诚实契约报错——形状守卫原则不变。
- **规格：** test_tool_input_repair.py +8（四 harness 别名/Gemini+Codex 四别名/update_plan envelope 变形/契约存活/Gemini 条目映射含 blocked 透传/cancelled 删除/Claude Code activeForm 标记补 id/原生与歧义载荷不触含 blend/修复→状态机端到端）；test_unified_tool_gateway.py +2（`update_plan`、`write_todos` 经 normalize_execute_request 端到端：双修复记录、错误为空、条目形状正确）。
- **踩坑：** item 变换首版先 pop 标记键再检测标记，`foreign` 永不置真导致静默 no-op——标记检测必须先于 rename（与 Kimi 变换同构的 foreign 置位纪律）。
- **验证：** repair+gateway+todo_list_compat 192 passed；tool_search_evaluation+todo_continuation+todo_tool_schema+direct_dispatch_alias+tool_hallucination 86 passed；ruff 净。两红仍为前两日已归因的兄弟在飞（schema token 预算 329>325、`_repaired` tool_start 事件违约），签名逐字一致，本改动对 TODO_WRITE_TOOL +0 字节。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (模型配置=世界目录：启动同步三个新模型入注册表 + codex client_version 自动跟踪 npm latest)

- **起因（用户追问）：** gpt-6-astra / claude-fable-5-1 / gemini-3.8-flash 已进模板 JSON 却始终不在「模型配置」页；codex 订阅缺 astra 的根因是目录请求 `client_version` 静态钉在 0.146.0，上游按版本门控新模型。用户裁定两条原则：① 模型配置页是世界模型注册表——「世界上存在这个模型，配置里就得有」，与服务商/订阅无关；② client_version 必须自动跟踪最新版，禁止硬编码。
- **改动：** ① 新建 `lib/model_routing/world_catalog.py`——启动时（routes/__init__.py owner authority 就绪后）编译全部包内服务商模板，把缺失的 Creator/Model 行插进 owner 注册表；insert-only 幂等，不碰 provider supply、不覆盖既有行（含用户编辑），`merge_keys` 去重防 model_id/display_name 双写。② 新建 `lib/oauth/codex_version.py`——`codex_client_version()` 从 npm `@openai/codex` dist-tags.latest 解析（有界缓存 + 失败回退），地板为 outbound.py 钉版；`codex_catalog.py` 拉目录时动态求值，缓存按版本分键不变。③ docs/MODEL_REGISTRATION.md 新增「World catalogue sync」小节。
- **规格：** tests/model_routing/test_world_catalog_sync.py（插入/幂等/不覆盖/跳过 local 模板）；tests/oauth/test_codex_version.py（npm 解析/缓存/回退地板）；test_codex_catalog.py 夹具钉住版本解析保确定性，URL 断言改导入常量。
- **验证：** 10 个邻近套件 113 passed；重启后日志 `world catalogue sync: added=60, revision 14`，codex refresher 以 client_version=0.153.4 重拉上游 9 模型含 gpt-6-astra；离线只读 sidecar 权威库确认注册表 471 模型，三个目标模型全部在列。
- **生效条件：** 已重启生效；模型配置页硬刷新即可见。

### 2026-09-06 (SetTodoList → todo_write：Kimi 自家 harness 词汇同样纳入重定向)

- **起因（用户追问）：** 昨天把 `todo_list` 归因 Amazon Q Developer CLI 后，用户质疑「报错模型是 Kimi，会不会是 Kimi 家 harness 的习惯」。
- **调研结论：** 不是。Kimi 自家 harness（Kimi CLI / Kimi Code CLI）的内建清单工具是 **`SetTodoList`**——MoonshotAI/kimi-cli `src/kimi_cli/tools/todo/__init__.py` 源码实证：`Todo{title, status: pending|in_progress|done}`、`Params{todos: list[Todo]|None}`（省略即查询、空数组即清空），官方文档 kimi-cli.com 工具清单一致。`todo_list` 命令词汇仍确证为 Amazon Q（tool_index.json + todo.rs）。Kimi 模型发 `todo_list` 是训练数据里 Amazon Q 语料的残留，非自家 harness 习惯。
- **顺修（生产模型是 Kimi，其原生词汇同样有逃逸风险）：** 两层适配，复用昨日三层架构的前两层（SetTodoList 写模式=全量替换，与原生 sync 同构，无需第三层解释器）：① `_classify.py` 别名 `'settodolist': 'todo_write'`；② `_transform.py` 新增 `_transform_kimi_todo_item_vocabulary` 串入 `_transform_todo_write` 链——逐项 `title`→`content`（有真 content 不覆盖）、`done`→`completed`（非原生合法状态，混合调用也修，避免下游静默降级 pending）、列表一经判定外来即按位补 id（`str(i+1)`，与 `_normalize_todos` 同约定；Kimi 无 id 概念，替换语义按位置对齐，连续两次全量替换能稳定 sync 合并）。查询模式（无 todos 键）刻意不动，落诚实契约报错；空列表（Kimi 清空模式）留给状态机如实回执。
- **规格：** test_tool_input_repair.py +6（别名/条目映射/契约存活/原生与歧义载荷不触/混合调用 done 映射/修复→状态机端到端两轮替换稳定合并）；test_unified_tool_gateway.py +1（`SetTodoList` 经 normalize_execute_request 端到端：双修复记录、错误为空、条目形状正确）。
- **验证：** repair+gateway+todo_list_compat 181 passed；search_evaluation+todo_continuation+todo_tool_schema 54 passed；direct_dispatch_tool_alias+tool_hallucination 32 passed；ruff 净。两红均为昨日已归因的兄弟在飞（schema token 预算 329>325、`_repaired` tool_start 事件违约），签名逐字一致，与本改动无关。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (接入矩阵请求名完整显示：去掉 150px 省略号截断)

- **起因（用户截图+要求）：** 设置 → 服务商接入矩阵里请求名被缩略——根行模型名（`Qwen3.5-397B-A17B-Ins…`）与别名列 wire id（`deepseek-ai/DeepSeek-V…`）都带省略号，用户要求「请求名不要缩略」。
- **根因：** `frontend/src/styles/settings/07-access-matrix.css` 的 `.stg-mx-mid` 设了 `max-width:150px + overflow:hidden + text-overflow:ellipsis`（注释自述「长 id 省略」）。该设计当初是为了配合标签列 shrink-to-fit（td `width:1%; min-width:170px`），把表格富余宽度让给凭证列。
- **改动：** 删除 `.stg-mx-mid` 的 max-width/overflow/ellipsis 三件套，保留 `white-space:nowrap`——长请求名把标签列撑过 170px 地板，矩阵自身的水平滚动容器（`.stg-matrix-scroll` + 宽屏 `stg-matrix-wide` 面板加宽机制）吸收溢出；`.stg-mx-corner` 注释同步改写。凭证列的 key hint 省略（`.stg-mx-credkind`）不是请求名，不动。
- **验证：** test_style_composition + test_provider_model_add + test_access_matrix 9 passed；compose_frontend_styles 重合成 static/settings.css 并 grep 确认新规则落盘；build:frontend + verify:frontend 绿。
- **生效条件：** 纯 CSS——浏览器硬刷新生效。

### 2026-09-06 (服务商模板品牌图标补齐：10 家聚合/推理云厂商不再回退灰色笑脸)

- **起因（用户截图+要求）：** 设置 → 服务商模板列表里 Cerebras / DeepInfra / Fireworks AI / Hugging Face / ModelScope / Nebius AI Studio / Novita AI / SambaNova / SiliconFlow / Together 共 10 家渲染成灰色笑脸占位图标，要求补齐。
- **根因：** `brandIconHtml()`（frontend/src/core/model-brand-icons.ts）对 `MODEL_BRAND_ICONS` 注册表里没有的键静默回退 `generic` 图标 + `#888`；模板目录（lib/model_info/data/*.json + static/provider_templates/*.json）声明的 35 个品牌键中恰有这 10 个无图标、也无 `MODEL_BRAND_COLORS` 品牌色。此前无测试钉住「模板品牌必须解析」这条契约（docs/FRONTEND_ARCHITECTURE.md 早有文字表述）。
- **改动：** ① model-brand-icons.ts 新增 10 个 mono SVG（`@lobehub/icons-static-svg` 官方 mono 变体逐字，MIT，与现有图标同源；剥离 title/height/width/style/xmlns，保留 `fill="currentColor" fill-rule="evenodd"` 与 path 级 clip-rule，遵循 stepfun 既有形制）+ 10 个品牌色（取自各图标官方 -color 变体主色：cerebras #F15A29、deepinfra #2A3275、fireworks #5019C5、huggingface #FFD21E、modelscope #624AFF、novita #23D57C、sambanova #EE7624、siliconflow #6E29F6、together #FC4C02；nebius 上游无 color 变体，品牌标志在 nebius.com 为近黑，取 #1A1A1A 并在注释留痕）；② docs/FRONTEND_ARCHITECTURE.md 契约行补测试引用。
- **规格：** 新建 tests/providers/test_provider_template_brand_icons.py——解析两个模板目录全部 brand 键，钉住每个键在 `MODEL_BRAND_ICONS` / `MODEL_BRAND_COLORS` 都有真条目（first run 复现 10 缺；颜色正则初版只认 6 位 hex，漏了 generic #888/grok #aaa/kimi #ccc 三位写法，改为 3|6 位）；`custom` 内建模板是表单占位非品牌，刻意排除。
- **验证：** 新规格 + audio_chat + model_group_convergence 6 passed；typecheck:modules / check:runtime（164 sections）全绿；build:frontend + verify:frontend 绿，新 bundle 已发布（main-EAKENer4.js，grep 确认新图标进包）；ruff 净。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (todo_list → todo_write 跨 harness 重定向：Amazon Q Developer CLI 命令词汇三层适配)

- **起因（用户报错+要求）：** 模型回合 `todo_list` 调用被拒——`tool_not_enabled`，候选 `todo_write` 模糊分 0.737 低于 typo 闸。用户要求查清这是哪个 harness 的工具并后端自动重定向。
- **调研结论：** `todo_list` 是 **Amazon Q Developer CLI** 的内建清单工具（aws/amazon-q-developer-cli `crates/chat-cli/src/cli/chat/tools/tool_index.json` + `todo.rs`），命令词汇：`{command: create|complete|load|add|remove|lookup, tasks[], completed_indices[], new_tasks[], insert_indices[], remove_indices[], todo_list_description?}`——与我们 todo_write 的全量 sync/push/replan 协议不同构，不是单纯改名能解决的。
- **改动（三层，各司其职）：** ① `lib/tool_input_repair/_classify.py` 名称别名 `'todo_list': 'todo_write'`（精确名优先原则不变）；② `_transform.py` 新增 `_transform_todo_list_command_to_todo_write`——command 是已知动词且无真 todos 时注入 `todos: []` 占位，使契约校验（required:['todos']）放行；与既有 nested-envelope 变换串成 `_transform_todo_write` 链；③ `lib/tools/todo.py` `apply_todo_operation` 拆为包装器+`_apply_canonical_todo_operation`——包装器把命令翻译成 canonical 操作后走原状态机：create→空态 sync-create / 同单 no-op / 异单 replan（reason 取 todo_list_description 或合成）；complete/add→sync 合并（0-based 索引，新 id 避撞，root 完成后 add 自动改 replan 重开）；remove→replan（带审计 reason）；load/lookup→no-op 只读（revision 不膨胀，回执 note 经 `_result_text` 透出）。todos 非空时一律原生优先；未知 command 落原生协议如实回执。
- **规格：** 新建 tests/tools/test_todo_list_compat.py 22 针（含 NC 中和针：patch 掉 `_todo_list_command_name` 后同 payload 必须建不出清单）；test_tool_input_repair.py +4（别名/占位注入/原生与未知 command 不触/链上 nested-envelope 仍解包）；test_unified_tool_gateway.py +1（`todo_list` 经 normalize_execute_request 端到端：alias_tool_name + structural_transform 双修复记录、错误列表为空）——复现用户原始失败形状。
- **兄弟在飞归因（纪律执行）：** 邻套两红均非本改动——test_todo_tool_schema 的 token 预算 329>325 系兄弟的 bounds 描述文案（HEAD 317，载荷 diff 恰为两句描述，本改动 +0 字节）；test_tool_hallucination 的 `_repaired` tool_start 事件违约系兄弟的 clamp 特性（_ingest.py/_repair.py/gateway.py 未提交 diff），events.py tool_start spec 待其补。
- **验证：** 新规格 23+4 全绿；tool_input_repair 全量 + gateway 全量 + search_evaluation + todo_continuation + direct_dispatch_alias + todo_tool_schema 共 244 passed（另 2 红归因兄弟）；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-05 (export 推送凭证卫生根修：PAT 不再落入 .git/config，清洗 6 仓明文 remote URL)

- **起因（用户报告+裁定）：** tofu-open/.git/config 两个 remote（origin/niutrans）URL 内嵌明文 PAT，要求立即撤下，并问导出侧脱敏状况。
- **根因：** `export_pkg/_publish.py::_git_push` 把 `_resolve_authenticated_remotes` 拼出的 `https://<token>@...` 直接 `git remote add` 进导出仓 config——每次 `export --push` 重写一次，手工清掉也会被下回推送复活。同类残留另有 4 仓（tofu-private、chatui_export_1dgihog9、liantong、keyan，keyan 为 `rangehow:<PAT>` 形式），已全部 set-url 为干净 URL。
- **改动（_publish.py）：** ① 凭证 URL 改挂 `auth_url`（仅内存），`url` 恒干净；② remote add 前 fail-closed 拒绝含 userinfo 的 URL；③ push/fetch/tag 以 auth_url 为目标按 URL 直推；④ `_push_branch` 对 URL 目标跳过 `-u`（实测 `git push -u <url>` 会把 URL 写进 branch.<name>.remote）；⑤ 新增 `_redact_url_credentials` 统一脱敏控制台/日志/异常文本；顺带堵掉更狠一面：ours-merge 提交信息原先插 `rname`，URL 目标会把 token 提交进公开历史，改插 display。
- **规格：** test_export_import_safety 新增 5 针（resolver url/auth_url 分离、redact、URL 目标禁 -u、具名 remote 保留 -u、端到端推送失败路径 config 与控制台均无 token）；publish_gate 调用针 rname→target。
- **验证：** 直接三套件 39 passed；export 家族+credentials_vault+desktop_build 等 191 passed；红针全部归因兄弟在飞（`packages/tofu-db/target-differential/debug` 构建产物被 track，3+1 针）与 NiuTrans 镜像发布滞后（3 针，本地文件干净）。公开镜像 93 提交全历史扫描无真实 token；主仓历史与镜像双向不相交（cat-file 验证）；旧 export.py 历史中的 PAT 与现用 token 哈希不同（已轮换过）。
- **结论：** 现用 PAT 从未进入任何 git 历史或远端仓库；暴露面仅限本机 FUSE 明文（已清）。是否轮换由用户裁定。
- **生效条件：** 纯发布工具侧——export.py 下次运行即新行为，无需重启服务。

### 2026-09-05 (扩展免手动更新：同路径覆盖解压 + keepAlive 看门狗自动重载)

- **起因（用户要求）：** 每次扩展更新都要解压后到浏览器里手动选择文件夹重新加载，要求更方便的方式。
- **关键事实：** ① 未打包扩展的 ID 由安装目录绝对路径派生——覆盖解压到同一文件夹，扩展保持已装状态，只剩 reload 一步；② Chrome 在 `chrome.runtime.reload()` 时从磁盘重读未打包扩展全部文件（live-reload 类开发工具的通行机制）；③ zip 是下载时从 `browser_extension/` 即时打包的（routes/browser.py），代码侧改版本即生效，无需重启。
- **改动（background.js）：** 新增 `_maybeReloadForSelfUpdate()` 挂在既有 keepAlive 闹钟（24s）上——带 cache-buster fetch 包内 `manifest.json`，磁盘版本与运行中 `EXT_VERSION` 不一致即 `chrome.runtime.reload()`；有排队结果/在飞命令时推迟一拍，不丢内存态结果；解压中途的撕裂读抛出则下拍重试。闹钟与轮询状态无关，401/426 泊停态的扩展同样能被新文件唤醒。版本 5.5.1→5.5.2（manifest.json + manifest.store.json + 商店 README 两处引用）——版本差是看门狗的唯一触发器，不 bump 就不会重载。
- **文案（local-control.js + zh/en locales）：** 四处升级引导（browserExtOutdated/ProtocolMismatch/CapabilityMismatch/Stranded）从「覆盖加载」改为「覆盖解压到当初加载的文件夹，扩展自动重载」，i18n 契约重生成（4814 键）。
- **规格：** 契约探针 harness 的 chrome.runtime mock 补 `reload()` 计数，新增 selfUpdate 模式（同版本不重载 / 磁盘新版重载一次 / 忙时推迟），test_browser_tab_outcome.py 新增对应针。
- **验证：** tab_outcome 25 passed（含新针）；chrome_store_manifest_parity + preseed + bridge_auth_backoff + devtools + network_evidence 74 passed；check:i18n/check:runtime（164 sections + reachability + TDZ）/typecheck:modules 全绿；build:frontend + verify:frontend 绿，新 bundle 已发布（main-D663G2wd.js）。
- **生效条件：** zip 即时打包 + 前端 bundle——硬刷新即生效，无需重启。但看门狗在 5.5.2 里首发：老扩展（≤5.5.1）升这次仍需手动重载一次，此后更新才是全自动。

### 2026-09-05 (运行中命令卡片移除「运行中...」大字标签：spinner+计时+中断已足够表达)

- **起因（用户截图+要求）：** 运行中命令卡片头部「运行中...」字样突兀、字明显比旁边「命令」芯片和「剩余 13m37s」大，用户裁定不要了。
- **大字根因：** 标签本体 `.ptool-cmd-label` 11px/600，`23-turn-reading-system.css:420` 的阅读系统字号提升规则 `:is(.ptool-text, .ptool-cmd-label){font-size:12px}` 本意是 prose，把命令标签扫进去变成 12px/600，旁边芯片都是 10-10.5px。
- **改动（tool-command-execution-presentation.ts）：** 纯 running 状态不再渲染状态标签（spinner + 剩余时间 + 中断按钮 + 脉冲点已充分表达进行中）；`progressStatusLabel` 非空才发 `<span class="ptool-cmd-label">`——cancelling/spooling/cancelled-partial/overflow-loss 这些携带额外信息的瞬时状态标签保留。i18n 键 `toolCommandExecution.running` 从 zh/en 移除并重生成契约（4812 键）。CSS 未动——stdin「Waiting for input...」与浏览器卡片 query 标签仍合法使用 `.ptool-cmd-label`。
- **规格：** test_tool_command_execution_presentation.py 更新两针并新增两针（纯 running 无 `ptool-cmd-label`、cancelling 保留 `>Cancelling…</span>`；retained dispatch 路径钉无标签），PASS 计数针 26→28；wire parity 基线重建。
- **兄弟在飞连带：** `_tool_rounds_wire_parity_rounds.json` 被并发写入者加了 3 轮（rejected-hallucinated/rejected-contract-invalid/error-failed-tool-long-multiline，58→61），基线重建一并纳入，否则数量针红。另：全量 `-k` 收集时 4 个无关文件（watch_follow_up/watch_goal_north_star/human_gate_http_owner/sync_digest_ghost_eviction）import 期断言红，同为兄弟在飞产物，与本改动无关。
- **验证：** 呈现器+parity 4 passed；邻近八套件 35 passed；check:i18n/typecheck:modules/check:runtime 全绿；build:frontend + verify:frontend 绿，新 bundle 已发布（main-EMNr4Ix_.js）。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (重启续跑渲染根修：投影 fallback 复活旧 contentPrefix，大段文字压在对话底部)

- **起因（用户截图+要求）：** conv mto8xq9we7xc6o 重启续跑后，被中断 attempt 的 1636 字过程叙述作为一整块「最终回答」压在对话最底部，贯穿整个续跑过程。
- **根因链：** ① attempt 1 中断时 settled projection.content = 累计叙述 blob；② continue 操作把 projection.content 原样拷进 `cfg['contentPrefix']`（command_service.py:1421）；③ `apply_resume_state` 一次性 seed `task['content']=contentPrefix`（设计：display bookkeeping only，_resume_state.py:424）；④ 续跑每个工具轮 `_discard_pretool_prose` 归零 `task['content']`；⑤ `turn_lifecycle._task_projection` / `_delta_text_fields` 的 `task.get('content') or cfg.get('contentPrefix')` fallback 在累加器为空的整个工具执行期间反复复活旧 blob；⑥ `_synchronize_terminal_blocks` 每次重算都把它盖进 `text:terminal` 段。fallback 是 v1 continue 的残留（v1 前端气泡直接拼 contentPrefix），与逐轮 discard 设计直接冲突。
- **改动（lib/turn_lifecycle.py）：** 两处投影路径删除 `or cfg.get('contentPrefix')` fallback——seed 只在 resume hydration 生效一次，之后累加器为唯一权威，与新 turn 行为一致。prefill 通道（resumePrefill）不受影响：fallback 只在累加器为空时触发，finalize 从不经过它。
- **考古：** attempt 1 的 durable rounds 无 assistantContent 并非 stamp 缺失——`compact_tool_rounds_for_frame_budget` 为帧预算剥离了旧轮重载荷，非第二 bug。
- **规格：** test_turn_lifecycle.py 新增 `test_projection_never_resurrects_content_prefix_after_resume_discard`（空累加器+contentPrefix 必须投 ''；有新 prose 必须投新 prose，_task_projection 与 _delta_text_fields 双针）。
- **验证：** test_turn_lifecycle 35 passed；邻近七套件（round_usage/inbox_inject/mcp_images/resume_executor_config/resume_state_wire_parity/todo_continuation/commit_round_daemon）109 passed；turn_projection_segments + continue_lossless + edit_realigns 121 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。在跑的 turn 下次续跑时前缀仍会作为 seed 显示一次，首个工具轮 discard 后即消失，不再粘滞。

### 2026-09-05 (块级复制按钮：中间段文字块可独立复制，不再只能复制最终回答)

- **起因（用户要求）：** 多轮工具调用穿插的对话里，turn 级 Copy 只持有 `projection.content`（最终回答），中间段 prose 没有任何复制路径；用户裁定方案 A——所有文字块统一加 hover 复制按钮。
- **设计：** 事件管道零改动——surface 的 `emitIntent` 本就从 `[data-block-id]` 祖先自动带上 `blockId + turnId`；按钮只发 `copy-block` intent，内容解析全部落在宿主。turn 级 Copy 语义不变（只复制最终回答），最终回答块同时有块级和 turn 级两个入口（内容相同）。
- **改动（四面）：** ① `classic-conversation-renderers.ts` `renderTextBlock` 末尾追加 ghost 复制按钮（`data-conversation-action="copy-block"`，复用 `actionIcon('copy')` 与 `msg-action-btn` 体系，幂等——重渲染不重复添加）；② `conversation-surface-controller.ts` copy 改写分支覆盖 `copy-block`（翻译显示模式下 operation 重写为 copy-original/copy-translated，与 turn 级一致）；③ `conversation_turn_store.js` 新增 `copy-block` 分支——按 blockId 在 `projection.segments` 里找 `type==='text'` 的段，terminal/deliverable 段以 `projection.content`/`translatedContent` 为权威（与 turn 级口径一致），中间段用 `segment.text`/`translatedText`；`_flashTurnCopyFeedback(turnId, blockId)` 支持块级作用域——blockId 含冒号（`text:llm-1`），用 dataset 直接匹配而非 CSS 属性选择器（无 CSS.escape 环境的降级正则会剥冒号导致失配）；④ `23-turn-reading-system.css` 块按钮右上角绝对定位、hover/focus-within 显示，窄屏（≤720px）常显，与 turn 动作架同一披露契约。
- **规格：** 新建 `test_frontend_conversation_surface_vite.py::test_text_block_copy_button_addresses_exactly_that_block`（渲染+controller 改写联合针：每文本块恰一枚按钮、thinking 块无、块级 intent 带 blockId、翻译模式重写 operation、重渲染不重复）+ CSS 静态针（hover 披露契约）；`test_frontend_turn_copy_feedback.py` 新增 jsdom 7 针（中间段原文/译文、terminal 段权威 content/translatedContent、未知 blockId 与 thinking 段拒写剪贴板、块级 flash）。
- **连带 pin 更新：** `test_branch_lane_is_keyed_collapsible_and_routes_stable_lane_intents` 的 `childActions` 期望 `['copy']` → `['copy-block', 'copy']`（块级按钮在 DOM 序上先于 turn 动作架）——唯一受影响的既有针。
- **验证：** 新增 2+2+7 针全绿；vite surface 全量 72 + 邻近五套件共 93 passed；check:runtime（compose 164 sections + lazy bindings + reachability + TDZ）/check:actions/check:i18n/typecheck:modules 全绿；build:frontend + canary + verify:frontend 绿，新 bundle 已发布（main-C0WRElhS.js）。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (run_command 工具记录双折叠控件：隐形分区点击区 → 显式「命令/输出」芯片)

- **起因（用户反馈）：** 工具记录标题行是隐形分区点击区——左半展开命令体、右半展开工具结果，无任何视觉提示，用户「随便点了几下感觉怪怪的」，要求两个控件都显式展示且设计得当。
- **设计：** 标题行 meta 区放两枚折叠芯片（`ptool-cmd-fold-cmd` / `ptool-cmd-fold-output`）——chevron SVG + 中文标签「命令」/「输出」（i18n 键 `toolCmd.foldCommand`/`toolCmd.foldOutput`），`aria-expanded` 同步；chevron 旋转与展开色调纯 CSS 由 block 的 `cmd-open`/`output-open` 类驱动，重渲染无需 JS 换标。标题行本体不再可点（输出唯一入口是输出芯片，故 `_cmdOutputOpenToggle` 不需要嵌套控件守卫）。
- **改动：** ① `tool-command-execution-presentation.ts` `foldChipHtml()` 发两枚芯片；② `tool_rounds.js` 新增 `_cmdBodyToggle`/`_cmdOutputOpenToggle` 处理器 + `_cmdBodyExpanded`/`_cmdOutputExpanded` 两个按 toolCallId 持久化的 Set（截断 512 码位与呈现器 `INTERACTION_KEY_UNITS` 对齐，否则长 id 展开态静默丢失），退役 `_cmdHeaderToggle`；③ 09-tool-terminal-details.css 芯片样式；④ zh/en locales。
- **静态扫描器陷阱（根修）：** `generate:actions` 后两个新动作没进 `runtimeActions` 表——芯片模板写成 `data-tofu-action="${action}"` 参数化插值，`runtime_action_analysis.mjs` 的 `actionAttributeValues` 只看到未闭合前缀 `data-tofu-action="`，接收器名永远收集不到（旧代码全属性内联单字面量所以没事）。修复：foldChipHtml 按 kind 分支输出两条**完整字面量**（属性+接收器名同串），渲染 HTML 逐字节不变、扫描器可见。规则：data-tofu-action 的接收器名必须与属性名在同一字符串字面量里。
- **验证：** check:runtime（164 sections + 13 lazy bundles + reachability 1643 调用点 + TDZ）/check:actions（74 动作）/check:i18n（4813 键）全绿；typecheck:modules 净；pytest 9 passed（cmd_collapse/tool_command_execution_presentation/wire_parity + 邻近 tool_round 三套件），wire parity 基线按新设计重建；build:frontend + verify:frontend 绿，新 bundle 已发布。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (订阅模板自动同步：启动即调和 OAuth ProviderAccess，不再等打开页面/重新登录)

- **起因（用户要求）：** fable 5.1 / astra 已进模板与规格，但「模型配置」与订阅服务商的模型列表都不见；用户裁定——以后不希望点「同步模板」，自动同步。
- **根因：** 订阅（OAuth）服务商写入模型路由权威的 `reconcile_oauth_providers()` 只有两个触发点——OAuth 设置页状态轮询、登录回执；代码侧发货的模板/规格更新（如 claude 规格新增 fable-5.1）自身从不传播，必须人进一次页面才落库。叠加「Python 改动须重启才生效」，用户看到的就是两处都没更新。
- **改动（routes/__init__.py）：** `start_registered_background_services` 在 REQUEST_SERVICES 能力块新增启动调和——personal 模式调用 `reconcile_oauth_providers()`（幂等：投影无变化不写库；codex 侧只读本地目录缓存 `_read_cache`，无网络）；distributed 模式沿用 TODO(enterprise) 跳过日志。放在 codex 目录 refresher 块之前、各自独立 try/except 故障隔离。重启即自动同步覆盖了全部场景——模板/规格是 Python 代码，本就须重启生效；codex 远端目录漂移由既有周期 refresher 兜底。OAuth 页轮询的调和保留（双写幂等）。
- **规格：** test_process_roles 两针更新——api 角色调用序列 `['oauth-reconcile', 'codex']`（started 仍计 1，调和非驻留服务）、distributed 模式钉死不调用；test_server_import_worker_isolation 的 per-app 幂等针补打桩，调用序列 `['scheduler', 'oauth-reconcile', 'codex-catalog', 'plugin']`。
- **验证：** process_roles + import_worker_isolation + server_background_services 16 passed；oauth_outbound + codex_catalog 33 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效（fable/astra 代码改动本就待同一次重启）。

### 2026-09-05 (全败 swarm 仍自动续跑 + 子智能体 OAuth owner 崩溃：三处根因修复)

- **起因（用户截图+要求）：** 4 个子智能体 2 败 2 取消，平台仍自动派发「Auto-continued · Continue by integrating the completed swarm results」；用户裁定——全败无需续跑，至少一个成功才续跑并告知大模型部分失败；其余大小问题全部寻根修复。
- **根因一（自动续跑）：** `_maybe_autocontinue`（lib/swarm/integration/_autocontinue.py）只查 `has_pending`，任何未读更新（含纯失败负载）都触发续跑。修复：新增全败闸——`agent_inbox.pending_items(swarm_key, modes=['swarm-update'])` 只读快照（drain 仍是唯一消费方），无任一 `status == 'completed'` 即跳过续跑，失败负载留给下一个人工轮次（与 chain ceiling 同姿态）；未盖章状态 fail-closed。master 两处 enqueue（lib/swarm/master.py:909/:1299）补 `extra={'status': result.status}`（extra 由 enqueue 平铺到条目顶层），rehydration 重入队同样盖章。
- **根因二（子智能体失败）：** 截图 `LLM call failed at round 1: local request preparation failed: LLM desktop egress owner requires a numeric user_id` —— `transport_owner_scope('')` 在 require_user_id 处崩溃（8/31 桌面出口提交引入），OAuth 槽位从无 owner 调用方拿到 `''`；且 swarm 子智能体从不传父任务 owner_user_id，每个子智能体 LLM 调用都无 owner。修复：① `lib/llm/_transport.py` 对 `''` 幂等（同时覆盖 chat.py/astream.py 双重归一）；② `lib/swarm/agent.py` 新增 `_llm_owner_user_id()` 并在两处 dispatch 调用传 `owner_user_id=`，OAuth 槽位按父租户解析令牌。
- **连带修复（兄弟在飞暴露）：** ① tests/presence/test_presence_subagent_integration.py 的显式签名 mock 被兄弟新增的 `max_429_attempts` kwarg（及本次 owner_user_id）打破，补 `**_dispatch_kwargs`；② 兄弟新增 routes/api_v1/local_serve.py 把 lib/local_serve/_register.py 拉上启动路径，其顶层 `from lib.llm_dispatch.discovery import discover_models` 破坏启动休眠边界（SERVER-DISPATCH 五旗），改为函数级懒导入（对齐同文件 `_rebuild_slots` 模式）。
- **验证：** 终扫 132 passed（swarm_async/agent_loop_chassis/llm_transport_connection_reuse/presence/startup_boundary/agent_inbox/billing_settle/inbox_wire_purity）；local_serve 六套件 115 passed；startup_boundary 4/4；autocontinue 邻近 75 passed；ruff 净。失败归因纪律：先 `git status`/`git diff HEAD` 区分自己与兄弟改动再认领测试红。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-05 (更正：服务商模板扩充实际生效条件——loader 重写属 Python 变更，须重启 server.py)

- **症状（用户两次截图）：** 白天扩充模板（8→35）后，设置面板「从模板添加」仍只显示 8+1 家，硬刷新无效。
- **根因：** 运行中的 server.py 于 14:09 启动，早于 16:39 的 `load_provider_templates()` 重写（旧实现=`_BUILTIN_PROVIDER_TEMPLATES`+硬编码 `OPENAI_TEMPLATE`+static 目录合并；新实现=每次调用 glob `lib/model_info/data/*.json`+static）。旧 loader 根本不读 data/ 目录，所以 19 个新 JSON 对它不可见；OpenAI 显示的 15 模型来自旧硬编码常量而非 data/openai.json。
- **结论与规则：** 「模板 JSON 热读」只对**已运行新 loader 的进程**成立——改 JSON 即时生效；改 loader/路由等 Python 代码仍须重启。19:10 服务重启（pid 73884→147806）后实测 `GET /api/v1/providers/templates` 返回 35 家（official 22 + relay 12 + local 1），用户重开弹层即见。
- **教训：** 交付条目写「生效条件」时区分数据热载与代码重启两个层级，不能把热读结论泛化到同批次的 Python 改动上。

### 2026-09-05 (OpenRouter 定价自动化集成：公开目录 API + 策略守卫写入，假 $0 类问题机制性闭环)

- **起因（用户要求）：** Inkling 假 $0 帕累托点修复后，用户要求「智能集成 OpenRouter 定价、以后自动解决这类问题」，先调研公开 API/开源库再全面推进。
- **调研结论：** ① 公开目录 `GET https://openrouter.ai/api/v1/models` 完全公开、无需密钥，返回全量模型含 `pricing.prompt/completion/input_cache_read`（字符串，USD/token），含 `:free` 变体与 `~` 前缀 alias 条目（`alias_target.slug` 指回 canonical）；② 每模型端点 `/api/v1/models/{author}/{slug}/endpoints` 可查各推理商分列价（Inkling 实价查证即走此接口）；③ 官方 Python SDK（openrouter-python）是聊天客户端，目录抓取不需要它——单个 keyless GET 足够，不引依赖。
- **模块（lib/model_catalog/openrouter.py）：** 抓取/解析/归一（×1e6 转每 1M）→ 24h 磁盘缓存（data_root，TTL 可用 `TOFU_OPENROUTER_CACHE_TTL_SECONDS` 覆盖）→ 普通读取不阻塞：过期/缺失只调度一次有界后台刷新（最小间隔 60s），仅显式 refresh 内联抓取；行数预算 2048。alias 条目折叠到 canonical slug，`:free`/全零行标记 `positive=False` 永不作写源。
- **写入策略（用户钉死，与「全零价根修」一致）：** 已定价模型的官方价权威永不被 OR 改写；未定价模型分两路——creator 在 `lib.pricing` 表有官方直连覆盖（任一兄弟模型可解析）→ `official_covered` 只报告等人工查官价；无官方覆盖 → `fillable`，refresh 时 CAS 写入 `list_pricing`（带来源标注）；已定价但与 OR 混合价差 >25% → `drift` 交叉校验报告（如 deepseek 谷时官方价 vs OR 最廉端点价差，供人工审，绝不自动改）。
- **路由（routes/api_v1/model_intelligence.py）：** `GET /api/v1/model-intelligence/openrouter-pricing` 只读报告（六桶+summary）；`POST .../refresh` 强制拉取 + 对 fillable 集 CAS 写 authority。
- **规格：** tests/model_routing/test_model_intelligence_openrouter.py 8 针（归一换算/alias 折叠/全零不写入/三桶策略/drift 阈值/缓存 TTL 与后台单飞/GET 形状/POST CAS 写+revision 递增）。
- **验证：** 8 针 + AA 13 + api_contract_drift 6 全绿；ruff 净。实网冒烟：330 行解析，`thinkingmachines/inkling` $1.00/$4.05/$0.17 与 rev 10 手工登记完全一致（交叉验证）；官方表 lookup 接线确认（deepseek-v4-flash 命中→covered，inkling 未命中→fillable）。API_CONTRACT.md 补策略段。
- **生效条件：** 重启 server.py 后新路由可用；首次 GET 触发后台拉取，POST refresh 立即填充 fillable。
- **后续（明示）：** ① 设置面板可加「同步 OpenRouter 定价」按钮调 POST（暂未做 UI）；② drift 报告可接通知；③ 定时自动 refresh 未开——写操作保持显式触发。

### 2026-09-05 (通用设置跟进：发送方式标签与说明合并为一行)

- **起因（用户要求+截图批注）：** 「输入框发送方式」标签与下方灰色说明「选择触发"发送消息"的按键。Shift+Enter 始终插入换行。」两句直接合并——前半句与标签纯重复，有信息量的只有 Shift+Enter 提示。
- **改动：** general.html 标签改「输入框发送方式（Shift+Enter 始终插入换行）」并删说明行；en 作 "Input Send Behavior (Shift+Enter always inserts a newline)"；孤儿键 settings.inputSendModeDesc 从 zh/en 删除，contract 重新生成（4813 键，含兄弟并行新增的 2 键）。
- **验证：** check:i18n/check:actions 绿；settings_panels_parity 14 passed；build + canary + verify:frontend 绿，bundle 已发布。两次构建被兄弟并行写打断（authoring inputs changed / canary 20s 超时），重试即过；check:runtime 的 reachability 报 _cmdBodyToggle/_cmdOutputOpenToggle 不可达——tool_rounds.js 与 tool-command-execution-presentation.ts 于 18:49–18:50 被兄弟会话改写中（mtime 实证），与本改无关，待兄弟收尾后复跑。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (Muse Spark 1.3 入库：Meta 模板换线 + 定价/发布日/上下文/effort 四面同步)

- **起因（用户要求）：** 「muse spark 1.3也出了，更新吧」。查证：Meta 于 2026-09-02 在 Meta Model API 目录上线 Muse Spark 1.3（无博客发布，双 SKU 同日上架），仓内 `meta.json` 还是旧 Llama 线。
- **事实（codersera + felloai 双源核实一致，dev.meta.ai 官方页被内容门拦下）：** ID `muse-spark-1.3` / `muse-spark-1.3-contributor`；上下文 1,048,576、最大输出 943,718（恰为窗口 90%）；标准档 $1.25/$4.25、缓存读 $0.15（readMul 0.12），contributor $0.10/$0.20、缓存 $0.002（readMul 0.02）——代价是 Meta 可用数据训练（厂商明示）；reasoning 强制，effort 档位 minimal/low/medium/high/xhigh（默认 medium），无 none 档，max 仅合作方有限预览（GA 顶档 xhigh）。
- **改动（十面）：** ① `lib/model_info/data/meta.json` 换 Muse Spark 线（text/vision/video/thinking/cheap ×2 SKU，旧 Llama 条目随兄弟模板扩充条目同步出白名单）；② `_tables.py` 双 SKU 定价行；③ `_release.py` `('muse-spark-1-3', '2026-09-02')`（子串覆盖 contributor）；④ `_context.py` 1,048,576 vendor_official；⑤ `_family.is_muse_spark`（不吃 `gpt-5.3-codex-spark`，无 'muse-spark' 子串）；⑥ `_capabilities.muse_spark_reasoning_effort` + `_MUSE_SPARK_EFFORT_MAP`——off/minimal/none→minimal，max/ultra→xhigh，thinking 关→minimal（K3/GLM-5.3 强制推理先例：给最廉合法档，不吃 400）；⑦ `_max_output` 943,718；⑧ `build_body` 新分支——只发 `reasoning_effort`，sampling 参数未核实一律不发（K3/GPT-5 先例，避免首调 400）；⑨ `_readjust_thinking_params` 同口径 failover 分支 + 从旧家族带来的 temperature/top_p 剥除；⑩ `lib.llm` / `lib.model_info` 导出面。
- **规格：** 新建 tests/llm/test_muse_spark_reasoning_effort.py（谓词/ladder/钳档/build_body/failover 11 针）；release/context 合同各加 contributor 针。cheap 双 SKU 由定价档位推导确认（<\$3/<\$15 双轴严格），非手工标签。
- **验证：** 阶梯 35+78+45+58 passed + 端到端冒烟（模板热载→定价行→发布日折叠→上下文→build_body max→xhigh）；ruff 净。基线红 1 条与本改无关：test_meituan_served_catalog_sync 的 grok-4.6 caps 期望没跟上兄弟已提交的 0da131e83（九月阵容同步给美团 grok-4.6 补 cheap）。
- **生效条件：** 模板 JSON 热读即时；_tables/_release/_context/_family/_capabilities/build_body/_api_stream_state 纯 Python 需重启 server.py。
- **未核实项（明示）：** Meta 是否接受 temperature/top_p（未发即安全，若官方文档证实可接受再回填）；音频输入 1.3 官方标注退化（模板未标 audio 能力）；1.1/1.2 两代未收录（1.3 同价同窗，官方推荐默认 1.3）。

### 2026-09-05 (服务商模板大扩充：8→35，新增 19 家官方厂 + 中转商，cheap 审计白名单同步)

- **起因（用户要求+截图）：** 「从模板添加」弹层只有 8 家服务商，用户指出所有模型商至少都该是可添加的服务商、中转商更应收录，要求立即更新。
- **根因认知：** 模板选择器数据源是两目录合并——`lib/model_info/data/*.json`（包内目录）+ `static/provider_templates/*.json`（部署覆盖），`load_provider_templates()` 每次调用热读（lib/provider/provider_template_recipes.py:134）。截图里的 8 家全在 static 侧，包内 data/ 原有 8 家，两目录按 key 并集展示——本质是可添加的模板太少。
- **新增 19 个模板（全部写包内 `lib/model_info/data/`，热读即时生效）：** 官方 8 家——baidu(千帆 ERNIE)、stepfun(阶跃)、cohere、perplexity(Sonar)、ai21(Jamba)、upstage(Solar)、nvidia(NIM)、meta；中转/聚合 11 家（`category: "relay"`，排序落在 official 之后）——siliconflow、groq、together、fireworks、cerebras、sambanova、deepinfra、novita、nebius、modelscope、huggingface。模型 ID 取自仓内证据（AA 榜单缓存 data/aa_index、定价表 _tables.py 162 行、美团网关目录），capabilities 仅用已建立取值（text/thinking/vision/cheap）。
- **合同联动（test_pricing_tier_sync.py）：** `cheap` 标签的所有权在定价权威，无定价证据的手工 cheap 必须登记 `UNPRICED_CHEAP_ALLOWLIST`（显式可审、等待定价行）。19 家新模板带来 62 条未定价 cheap（中转 wire id 如 `zai-org/GLM-5.3` 与官方定价行 `glm-5.3` 键不匹配，openrouter 既有条目同此先例），白名单 6→66。不为凑绿伪造定价行——「全零价根修」教训：未知价 ≠ 免费，实价核实后应回填 MODEL_PRICING 并从白名单移除。
- **协作插曲：** 兄弟会话在飞同改 `lib/model_info/data/meta.json`（换 Muse Spark 线）——重读后同步删掉白名单里两条过时 meta Llama 条目；pareto 测试红（muse-spark/free-llm 夹具）是兄弟 pareto-chart.ts 在飞改动，与本改无关。
- **验证：** JSON 语法 27 文件净；加载器实出 35 模板（official 22 + relay 12 + local 1）；compile_provider_template_bundle 冒烟过（siliconflow/GLM-5.3，strip_unpriced=False 保留手工 cheap）；模板/定价/onboarding 邻近 9 套件 74 passed；ruff 净。
- **生效条件：** 模板 JSON 热读——设置面板下次打开即见，无需重启、无需硬刷新。
- **遗留（明示）：** ① 19 个新 brand key 在前端 model-brand-icons 无配色/图标，回落 generic（纯外观）；② 62 条白名单条目待实价回填；③ AA 榜尚未覆盖的小厂（iFlytek Spark、01.AI、Inflection 等）未收录，证据不足不猜 base_url。

### 2026-09-05 (通用设置精简：模型参数区退役，发送方式提上主区)

- **起因（用户要求+截图批注）：** 通用页「模型参数」区（温度滑杆 / 最大 Token 数 / 图片最大宽度）用户从不设置，属多余条目；「输入框发送方式」藏在「功能模块」折叠区里太深，要求提上来；思维深度下的灰色小字「新对话的默认思维深度级别」与标签重复，一并删。
- **改动：** `static/settings_panels/general.html` 删整个模型参数 section（标题+三控件），保留的「默认思维深度」与上移的「输入框发送方式」组成一个 two-col 行（发送方式保留自己的 Shift+Enter 说明小字）；功能模块折叠区内的原发送方式组删除。JS 接线同步拆：`core_panel.js` openSettings 删 settingTemp/tempVal/settingMaxTokens/settingImageMaxWidth 四段回填，`save_export.js` 删对应三行 config 写入。i18n 清 6 个孤儿键（settings.modelParams/temperature/maxTokens/imageMaxWidth/imageMaxWidthPh/defaultThinkingDesc，zh+en），contract 重新生成（4812 键）。
- **保留边界：** `config.temperature`/`maxTokens`/`imageMaxWidth` 的默认值（core.js：1 / 128000 / 0=跟随服务器）与消费方（main_conv_lifecycle 聊天载荷、upload.js 压图、cost.js 1024→0 迁移）不动——老配置里存过的值继续随整配置持久化，行为完全不变；`_syncRangeOutput` 仍被 experiments.html 两个滑杆使用，保留。
- **验证：** check:runtime（compose 164 sections + lazy bindings + reachability + TDZ）/check:i18n/check:actions 全绿；typecheck:modules 净；vite build 428 modules + verify:frontend 过，新 bundle 已发布（settings-B77FzZ_m.js）；pytest settings_panels_parity + brand_mascot_single 25 passed。全仓 grep 无 settingTemp/tempVal/settingMaxTokens/settingImageMaxWidth/defaultThinkingDesc 残留。
- **生效条件：** 纯前端——浏览器硬刷新生效。

### 2026-09-05 (模型管理并入访问矩阵：矩阵常驻为唯一供给管理面，manager 弹层全家退役)

- **起因（用户要求）：** 「模型管理」弹层与「访问矩阵」两套界面并存太冗余，用户明确偏好矩阵风格，要求以矩阵为主、合并为一并彻底美化，界面上不留多余元素。
- **结构（access_matrix.js 重写为供给管理面）：** ① 管理行——子串搜索（canonical + 请求名，`_setMatrixQuery`）+ 互斥状态筛选芯片（全部/已启用/已停用/待确认，带实时计数，`_setMatrixFilter`）+「+ 添加模型」（开目录 picker，实现留 provider_render.js）；② root 行——状态芯片（待确认=斜纹格+悬停说明、已下线=目录 lifecycle retired + 删除线只留给它）、元信息行（中文能力芯片、K/M 缩写上下文 `_modelRoutingContextLabel`、入/出结构化定价，目录价带「目录价」tag）、行动作（探测行、添加请求名 `_addV2Alias`、垃圾桶移除供给 `_removeV2Offering`）；③ 请求名行——树形 └ 连接符 + 每别名轮转色相，禁用的请求名（新加未过探测的固有形态）保留可见带「未启用」芯片与自己的删除 ×（`_removeV2Alias`）；④ 排序：启用优先再按 id 字母序，停用只暗化不用删除线冒充已删除；⑤ 零供给也渲染矩阵（「该服务商还没有模型供给」+ 添加模型入口）——否则新服务商永远加不了第一个模型。
- **provider_render.js 退役面：** `_stgMatrixOpen` 开关与「访问矩阵/收起矩阵」按钮、`_stgProviderManagerLimit`、`_renderProviderManagerModelRows`、stg-mcard 卡片 chrome、`stg-v2-model-identity` 全部删除，矩阵常驻内嵌于卡片；`_stageV2AddModels` 语义钉死——confirmed 供给继承目录事实（capabilities/context）并为每条现存凭证铸授权（dedupe），自定义 wire id 走 pending_identity 不伪装官方身份、不铸授权；新 deployment 出厂 enabled + probe_status passed + identity_confidence high/pending（domain.py 校验要求，否则保存必败）。密钥卡 hint 输入框改 `size` 属性按内容定宽。
- **样式：** 07-access-matrix.css 新增管理行/搜索/状态芯片/meta/行动作/pending 行/空态规则；06-model-routing-v2.css 保留共享件（.stg-v2-filter-chip、.stg-v2-price-tag/io、能力芯片）并清死样式（stg-mcard、stg-provider-v2-foot、stg-v2-model-row 等）。i18n 57 键 zh/en 齐备（matrixReleaseLabel 等 release 文案从 manager 硬编码移入 locales）。
- **排障：** `_resumeMatrixProbe` 补 `typeof Api === 'undefined'` 护栏（对齐 `_loadV2KeyStats` 既有模式）——两个旧 node 台子不打桩 Api 即挂；重写中 `_mxDedupe` 失去全部调用点被 reachability 门拦下，删除。协作插曲：基线对比 stash 时 `git stash pop` 被兄弟 git 进程的 index.lock 挡住，锁消后原样 pop 恢复，无损失。
- **规格：** tests/providers/test_provider_model_add.py 全文重写（6 针——confirmed/pending staging 形状+授权+dedupe、目录价回落+「目录价」tag+「未设置价格」占位永别、矩阵常驻+零供给空态+密钥输入定宽、状态芯片/排序/筛选/搜索/行动作/K-M 上下文、静态合同含死 chrome 否定针与 CSS 针）；tests/test_frontend_model_routing_v2.py 两测试改钉常驻矩阵（行内 wire id、掩码 hint 作列头、offerings 行内 toggle），manager 测试改钉凭证操作+矩阵 meta 面。
- **验证：** 阶梯 38 passed（provider_model_add/access_matrix/brand_resolver/frontend_model_routing_v2/speech_persist/adapter_upgrade_ui/runtime_composition）；check:frontend 全链绿（compose 164 sections + 32 styles、contracts/i18n/actions/tsc、vite build 428 modules）；ruff 净。基线红 3 条均为兄弟在飞域与本改无关：pareto-chart.ts 点集漂移（+177/−18 兄弟改）、i18n scanner 的 projectBrain.actBlock（project-brain 域）、runtime_composition ×2（project-brain-status.js/conv_state_reducer.js 被兄弟删）。
- **生效条件：** 纯前端——新 bundle 已发布，浏览器硬刷新生效。

### 2026-09-05 (九月模型阵容大同步：Fable 5.1/Astra 订阅面 + 九家模板/定价/发布日期)

- **起因（用户提问）：** 「为什么 fable 5.1 和 astra 既没进模型配置也不在订阅更新里？」查证：两者当日已进工作树（astra 16:02 进 openai.json/outbound codex 层，fable-5-1 已在 anthropic/bedrock 模板与 _tables），但 ① server.py 自 14:09 运行未重启，静态文件改动未生效；② 订阅层（ProviderAccess）要 UI「同步模板」或重登才拉新模板。属生效路径问题而非配置缺失，但同时发现其余九家模板/定价整体过期，遂做全量同步。
- **模板阵容（7 文件）：** xai 换 grok-4.6/4.20/4.1-fast（4.1-mini 退役）；qwen 全系换代 qwen3.8-max/3.7-plus/3.8-flash（qwen3-max/3.6-plus/3.5-flash/coder/vl 官方已进 Legacy 区）；doubao pro 换 seed-2-1-pro-260628（Seed 2.1 现役旗舰，lite/mini 2.0 仍在服保留）；mistral codestral-latest 换 mistral-medium-latest（Codestral 25.01 官方弃用，编码位被 Medium 3.5 吸收）；glm 加 glm-5.3-flash（¥0.8/¥2.8）与 glm-4.7-flash（免费档）；openrouter 修幻觉条目 openai/gpt-5.6→gpt-5.6-sol、加 claude-fable-5-1 与 gpt-6-astra；bedrock 加 us.anthropic.claude-opus-5/sonnet-5-v1:0；tencent 下架 kimi-k2.5（上游 8/31 退役 404）；meituan grok-4.6 补 cheap。
- **定价表（_tables.py）：** DeepSeek V4 8/16 调价落地——峰谷块从 INERT 转 LIVE（effective_from=1786809600），谷时基价 Pro $0.66/$1.98（hit $0.022→readMul 0.0333）、Flash $0.22/$0.66（hit $0.007→0.0318），美团网关镜像同步、无 peak 块（官方直连专属）；新增 qwen3.8 三行、doubao-seed-2-1-pro（¥8/¥20→$1.10/$2.76）、glm-5.3-flash（$0.11/$0.39、hit ¥0.23→0.29）、grok-4.6 实价 $2/$6（cached $0.50→0.25）/4.20/4.1-fast、mistral-large-latest→Large 3 $0.50/$1.50、small→Small 4 $0.15/$0.60；MiniMax-M3 半价促销转正 $0.30/$1.20（>512K 档 $0.60/$2.40）；codestral 行删除留注（Do NOT re-add 模式）。qwen3.7-plus 用 >256K 档价（≤256K 档未核实，注释明示）。
- **发布日期针（_release.py）：** qwen3-8-max/3-8-flash→2026-08、qwen3-7-plus→2026-05、grok-4-6→2026-08、glm-5-3-flash→2026-08（置于 glm-5-3 前，子串更长者优先）、doubao-seed-2-1→2026-06。
- **订阅/ outbound：** oauth/outbound.py 删 claude-opus-4-1-20250805（8/5 退役）；codex 层 16:02 已含 gpt-5.6-sol/terra/luna + gpt-6-astra，无遗留 gpt-5.6 裸引用。slots 默认 codestral-latest→mistral-medium-latest（死 SKU 交换）。
- **规格同步：** test_pricing_tier_sync 白名单删三条已定价项（doubao-2-0-pro、grok-4.20、grok-4.1-mini），onboarding bundle 针改 doubao lite；test_pricing_dated_folding 三条针改新基价、lookup 集成针改固定谷时时间戳（峰谷生效后 wall-clock 断言不再确定）；test_deepseek_retirement_peak 的 INERT 针改写 LIVE 版（effective_from/谷时基价/峰时翻倍/镜像无块）；_peak.py 文档串 TBA→生效日期。
- **验证：** 定价/模板/发布日期阶梯 86 passed + outbound/catalog_sync/dispatcher 邻近 82 passed；ruff 净。
- **生效条件：** 模板 JSON 热读即时；_tables/_release/outbound/slots 纯 Python 需重启 server.py；订阅层需 UI 同步模板或重登。
- **未核实项（明示）：** qwen3.7-plus ≤256K 档价、doubao seed-2.1 lite/mini 是否存在、mistral-medium-latest（Medium 3.5）价格卡、glm-5.3-flash cache 命中价（¥0.23 为二手源）。

### 2026-09-05 (Inkling 帕累托 $0：OpenRouter 实价登记进 authority rev 10)

- **起因（用户报告+截图）：** 帕累托图上 Inkling · Thinking Machines 落在最左 $0 列（tooltip `AA/质量 32.2 · $0/1M`、路径 doubling 成 `thinkingmachines/thinkingmachines/inkling`）成假前沿点；用户指出 OpenRouter 明码标价（https://openrouter.ai/thinkingmachines/inkling），免费无稽。
- **与「全零价根修」条的衔接：** 该条三面修复（迁移 `_list_pricing` 丢全零 + 图表 `blendedModelCost` 全零守卫 + CAS rev 8→9 剥掉全部 80 条全零 list_pricing）已让 inkling 变「未挂牌」并随 16:52 bundle（settings-Cv3rFlei.js → panel-BECbSdqR.js）排除出图。但 inkling 不是「价格未知」——OpenRouter 有实价，剥零只是止损，本条补真价。
- **查证：** OpenRouter endpoints API（/api/v1/models/thinkingmachines/inkling/endpoints）：Together/BaseTen $1.00 输入 / $4.05 输出 / $0.17 cache-read（每 1M），DeepInfra $0.95/$4.05/$0.16 略低但近一日 uptime 不稳；无官方自营推理 API，登记多数端点价。AA 分 32.2 与 data/aa_index 缓存一致。`model_id` 残留 creator 前缀（409 行中仅 5 行此形态）且被 cred_2009fa… / cred_ec6300… 两条凭证 authorization.models 引用——**不能改名**（授权会悬空），doubling 路径暂存留。
- **修复（纯在线数据）：** `PUT /api/v1/model-routing` CAS `expected_revision` 9→10，inkling 登记 `list_pricing {input:1.0, output:4.05, cache_read:0.17, USD, per_million_tokens}`，3:1 混合 $1.7625/1M；复查 409 模型/23 creator 计数不变，该行其余字段未动。
- **生效条件：** 在线数据即时生效（图表下次拉取即以 ~$1.76 正常落点，非前沿点）；「$0 不入曲线」语义需浏览器硬刷新加载 16:52 bundle。

### 2026-09-05 (密钥卡渲染信封 JSON 根修：提示/揭示/表头三表面把存储编码当成了密钥本身)

- **起因（用户报告+截图）：** 美团服务商卡片两把密钥异常——#1 密钥框里是 `{"api_key":"22080658572602085409","extra_he…` 原文 JSON，#2 掩码是 `{"ap********…`（JSON 外壳的掩码）；同时自定义请求头区空白，请求头「显示在密钥字段」而不是出现在请求头该在的地方。
- **根因（三层同根）：** 凭证明文按设计是 `tofu.credential-secret/v1` 信封（api_key+oauth+extra_headers 单串加密存储，调度在路由物化时解码）。但①`put_secret` 的 `key_hint` 直接对整串明文取 `secret_hint`，信封凭证的掩码变成 JSON 外壳掩码（截图 #2）；②早期 v2 写入方把 legacy `extra_headers` 全部折进信封，Connection 的自定义请求头编辑器自然空白，信封内容又随密钥字段一起显示（截图 #1 与「请求头错位」）；③前端 reveal 把信封 JSON 原样塞进密钥输入框。
- **修复：** 新建 `lib/model_routing/credential_envelope.py`（`decode_credential_envelope` / `credential_secret_display_hint`——提示只取可用密钥本身的位数，header-only 凭证标 `headers`）；repository.py 三处 hint 派生收口到该助手；`lib/provider/provider_headers.py` 新增 `split_header_secrecy` 分类器（auth/key/token/secret/password/cookie/credential 命名及传输保留名→信封，其余→Connection）；migration.py 迁移拆分 legacy extra_headers，普通路由头落到可检视可编辑的 `connection.extra_headers`；bootstrap.py 新增 `_repair_credential_secrets`——authority 已激活时幂等收敛存量：普通头搬回授权连接、重封信封、重派生提示，冲突头（连接上已有不同值）保留加密以维持「信封覆盖连接」的调度优先级；前端 provider_render.js 新增 `_v2KeyDisplaySecret`（reveal 只显示纯 api_key），模板暂存把 `credential_extra_headers` 合并到 connections 而不再折进信封（信封 `extra_headers: {}`）。
- **规格：** contract 3 条（迁移拆分/提示派生/分类器）、bootstrap 2 条（修复+幂等重放、冲突保留）、frontend 2 条（reveal 解码 node 台子、模板暂存静态钉）。
- **验证：** contract+bootstrap+frontend+dispatcher_v2+secret_envelope 边界 30 passed；邻近 model_routing 六套件 35 passed；ruff 绿。frontend 文件 4 红均为兄弟在飞域：`_stgMatrixOpen` 定义被兄弟从 access_matrix.js 删除而 provider_render.js 引用仍在（lazy-binding audit 正式拦截，非本改引入），pareto-chart.ts 兄弟改动致点集断言偏移；compose 已核 164 sections，check:frontend 全链被该兄弟改动挡在 binding audit，待其收尾后复跑发布。
- **生效条件：** 服务端修复重启 server.py 生效，bootstrap 修复在 authority 激活路径自动收敛存量数据（无需手工数据迁移）；前端 reveal/暂存修复随下次 check:frontend 全绿发布 bundle 后浏览器硬刷新生效。

### 2026-09-05 (Inkling 帕累托 $0：OpenRouter 实价登记进 authority rev 10)

- **起因（用户报告+截图）：** 帕累托图上 Inkling · Thinking Machines 落在最左 $0 列（tooltip `AA/质量 32.2 · $0/1M`、路径 doubling 成 `thinkingmachines/thinkingmachines/inkling`）成假前沿点；用户指出 OpenRouter 明码标价（https://openrouter.ai/thinkingmachines/inkling），免费无稽。
- **与「全零价根修」条的衔接：** 该条三面修复（迁移 `_list_pricing` 丢全零 + 图表 `blendedModelCost` 全零守卫 + CAS rev 8→9 剥掉全部 80 条全零 list_pricing）已让 inkling 变「未挂牌」并随 16:52 bundle（settings-Cv3rFlei.js → panel-BECbSdqR.js）排除出图。但 inkling 不是「价格未知」——OpenRouter 有实价，剥零只是止损，本条补真价。
- **查证：** OpenRouter endpoints API（/api/v1/models/thinkingmachines/inkling/endpoints）：Together/BaseTen $1.00 输入 / $4.05 输出 / $0.17 cache-read（每 1M），DeepInfra $0.95/$4.05/$0.16 略低但近一日 uptime 不稳；无官方自营推理 API，登记多数端点价。AA 分 32.2 与 data/aa_index 缓存一致。`model_id` 残留 creator 前缀（409 行中仅 5 行此形态）且被 cred_2009fa… / cred_ec6300… 两条凭证 authorization.models 引用——**不能改名**（授权会悬空），doubling 路径暂存留。
- **修复（纯在线数据）：** `PUT /api/v1/model-routing` CAS `expected_revision` 9→10，inkling 登记 `list_pricing {input:1.0, output:4.05, cache_read:0.17, USD, per_million_tokens}`，3:1 混合 $1.7625/1M；复查 409 模型/23 creator 计数不变，该行其余字段未动。
- **生效条件：** 在线数据即时生效（图表下次拉取即以 ~$1.76 正常落点，非前沿点）；「$0 不入曲线」语义需浏览器硬刷新加载 16:52 bundle。
- **协作注记：** 期间兄弟会话在飞同改 migration.py、test_model_routing_contract.py、access_matrix.js、provider_render.js，已重读确认本改八块互不踩踏。

### 2026-09-05 (全零价根修：未知价≠免费——迁移、图表、在线数据三面同改)

- **起因（用户要求根修）：** 上一轮只修了 DeepSeek V4 Pro 0813 一个点（CAS 折别名价 + 迁移折叠），用户要求「fundamental fixes」——同类假 $0 不能再出现。
- **根因链（比上一轮更深）：** 系统数据模型无法区分「价格未知」与「官方免费」。目录扫描在没有价格卡时物化显式 `{input: 0, output: 0}`，迁移 `_pricing` 照单全收为 Model.list_pricing，图表 `blendedModelCost` 把 0/0 算成 $0 画进免费车道——占位零一路伪装成官方价。全库共 80 条 Model 行带全零 list_pricing（上一轮的 6 条日期针只是有 AA 分因而显眼的部分）。生产端逐一排除后确认：现行 discovery 只写正价、模板 recipes 无 pricing、meituan 同步脚本价块为空——零全部是历史扫描遗留，缝合处只剩迁移入口与图表消费端。
- **修复（三面）：**
  1. **迁移入口（lib/model_routing/migration.py）：** 新增 `_list_pricing`——全零（input≤0 且 output≤0）官方价一律丢弃（返回 None），Model 保持未挂牌（目录显示「目录未挂牌」）；两处 list_pricing 调用点切换。`offering.actual_pricing` 故意保留零语义：那是 provider 实际成交价，免费网关（内部 LongCat）的 $0 是真的，路由 cost/价格上限/计费照常。半零促销（input=0、output>0）不受影响。
  2. **图表消费端（model-catalog/model.ts `blendedModelCost`）：** 全零价返回 null → 模型进既有「有质量分但缺官方价格，未入图」排除注记，不再画 $0 假前沿点。已持久化的历史零无需数据迁移即被渲染层兜住。
  3. **在线数据：** 经 `PUT /api/v1/model-routing` CAS（revision 8→9）剥掉全部 80 条全零 list_pricing，复查无残留，deepseek-v4-pro-0813 保持已折叠正价。
- **取舍（明示）：** 官方真免费模型（如智谱 glm-4.5-flash 免费档）从此也无法用全零表达官方价——图表无法区分「真免费」与「占位零」，一律按未知处理、排除出图；真实免费网关的零成本由 actual_pricing/计费层表达，不受影响。
- **规格：** tests/model_routing/test_model_routing_contract.py 原折叠针改钉——无别名针与 non-pin 零价模型（glm-9.9）均无 list_pricing；新建 tests/model_routing/test_model_catalog_pareto.py（vite bundle harness）钉图表语义——全零模型不入点集且无任何 $0 点、半零促销照常绘制（0.5 入前沿）、未挂牌与占位零按质量分降序进排除列表。
- **验证：** 阶梯 80 passed（model_routing_contract/bootstrap_model_catalog/model_routing_bootstrap/pricing ×3/dispatcher_v2/api_v1 合同/pareto/brand_resolver）+ 消费方 15 passed（provider_model_add/intelligence_aa/access_matrix）；ruff 净；typecheck:modules（tsc）净；vite build 重发布 main-Br5fNYR0.js。
- **生效条件：** 在线数据即时生效；图表修复随新 bundle 浏览器硬刷新生效；迁移代码对将来重跑迁移/修复/模板编译生效（server.py 无需重启——迁移路径每次现算）。
- **协作注记：** 本改期间兄弟会话在飞同改 migration.py 与 test_model_routing_contract.py（provider_catalog_sync 域），已重读确认互不踩踏。

### 2026-09-05 (模板热更新 + 远程目录自动同步：新模型发布不再需要改代码发版)

- **起因（用户报告）：** 「Gemini 3.8 Flash 已经发布了，为什么我们还没有？」并要求「从今以后模板支持热更新、必须绝对智能」。根因是结构性的：模板模型清单硬编码在 Python 常量里，供应商每发一个新模型都要改代码、重启、发版。
- **Part 1（模板抽文件 + 每请求热读）：** `_BUILTIN_PROVIDER_TEMPLATES` 各供应商抽成 `lib/model_info/data/<key>.json`（v1 offering_recipes 形状，含 gemini-3.8-flash），常量只留 anthropic 协议桩 + custom 占位；`load_provider_templates()` 每次调用重读文件系统——`lib/model_info/data/*.json`（包目录）→ `static/provider_templates/*.json`（部署覆盖，按 key 后者胜），单文件损坏/编辑中瞬态无效 JSON 只告警跳过不拖垮整目录；bootstrap 加载器同序合并；tofu.spec 打包 data 目录。title_gen 的 `_THINKING_MODELS_TO_EXCLUDE` 从静态名单改为运行时由模板 capabilities 派生 + 静态地板。
- **Part 2（provider_catalog_sync.py，远程目录自动同步）：** 新建 `lib/llm_dispatch/provider_catalog_sync.py`——周期性用仓库里存的凭证对每个已配置远程 provider 重新 `GET /models`，diff 后把新 wire id 以 `pending_identity` offering + `identity_confidence=pending` deployment CAS 追加进 owner 作用域 v2 authority（复用 health_local 共享监控线程，启动 600s 后首跑、之后固定 6h，无第二个常驻线程）。安全栏：严格只增（既有行不改不删，用户禁用的 offering 永远不动）；dismissed 台账——用户删掉的同步模型永不复活，而 pending→confirmed 身份确认不算删除（per-owner 状态 `data/config/model_catalog_sync.json`）；>200 条的聚合目录（OpenRouter 类）整体跳过；单 pass 每 provider 至多导入 32 条；单 provider 失败不株连（首 WARNING 后续 DEBUG）；新增后重建 dispatcher slots。
- **env 对齐既有文档合同（重要决定）：** 调查发现 `.env.example`/RELIABILITY_RUNBOOK 早在 347d8be54/026042a24 就承诺了 `TOFU_MODEL_CATALOG_SYNC` 机制（含 REMOVE_AFTER=2 退役语义），但全库无任何实现——陈旧 doc-first 漂移。落地采用已文档化的 env 名（`TOFU_MODEL_CATALOG_SYNC`/`_DELAY`/`_INTERVAL`，免费获得 conftest 的套件级禁用），语义按更安全的只增设计落地，.env.example 与 runbook 行改写为实际语义、删除未实现的 REMOVE_AFTER 承诺。新 provider 在 Settings 保存时本就走模板编译的认证探测拿全量目录，6h 同步只补「供应商后来发布的模型」，故不在保存路由挂 trigger（`trigger_provider_catalog_sync()` 留作显式 seam）。
- **规格（tests/providers/test_provider_catalog_sync.py，15 针）：** pending 追加形状（csync- 稳定 id、schema 枚举核对 identity_state/pending + identity_confidence/pending）、任何形式已知 id 不重複、删除→dismissed 不复活、身份确认≠删除、禁用行不被改、聚合规模跳过、32 条/趟封顶下趟补齐、loopback 归本地 sweep 不碰、oauth 凭证不用于列表、缺 secret 跳过不算失败、单家失败不株连、env 退出、poll 间隔+trigger 强制、health_local 协作调度。
- **连带修：** `test_current_provider_templates_omit_unsupported_ids` 原扫描生成的 app-runtime.js——bundle 里唯一退役字面量是 cost.js 的 canonicalization 映射 `'gpt-5.6-pro'→'gpt-5.6'`（合法修复面，镜像 conv_config/_legacy.py），改为只扫权威 `lib/model_info/data/openai.json` 并注释缘由。
- **验证：** 新套件 15 passed；阶梯 74 passed（catalog_sync/local_autodiscover/model_routing_contract/bootstrap/discovered/local_provider/dispatcher_v2）；Part1 面 31 passed（template_recipes/title_gen/deprecated_sku）；ruff 三文件净。glm-9.9 一度红为兄弟在飞写 `provider_template_recipes.py` 半途的瞬态，复跑即绿。
- **生效条件：** 纯 Python——重启 server.py 生效；模板 JSON 热改无需重启（每请求重读）；tofu-db/前端无涉。
- **遗留 seam：** 退役语义（REMOVE_AFTER 式自动下架）有意未实现——只增 + dismissed 已覆盖僵尸问题，自动删行风险大于收益；如未来要做，先解决「供应商临时把模型从 /models 摘掉」与「用户手动加的自定义 id」两类误删面。

### 2026-09-05 (设置保存必现「model-routing v2 authority is not ready」根修：feature 侧读错了作用域)

- **起因（用户报告+截图）：** 设置弹窗点「保存」必现红条 `保存失败: model-routing v2 authority is not ready`（v0.17.0）；服务商卡片、访问矩阵渲染一切正常，用户多次排查无果。
- **根因（双作用域错配）：** `_stgModelRouting`/`_stgModelRoutingRevision` 的所有者是 settings 懒加载包（settings-presenters.generated.js，`featureRegistry as runtimeScope`），经 `Object.defineProperties` 以 live 存取器暴露在 registry target 上；app-runtime 主包的 runtimeScope 从无此名（生成物 grep 零命中）。而 speech.ts 的 `persistSttProvider`（save_export.js 对每次保存无条件调用）用 `readLiveRuntimeBinding` 读——该口只查 app-runtime 作用域 → 恒 undefined → 抛 not ready。服务商 UI 正常恰因设置包内部读自己的词法变量，不走此口。同根连带两处：`readLiveRuntimeBinding('_stgModelRoutingRevision')` 恒得 0（即使文档读到，STT 持久化也必 CAS 冲突）；`reloadRoutingAuthority` 用 `writeLiveRuntimeBinding` 写错作用域，设置包暂存修订号不刷新。引入点：v2 迁移检查点 382d09d51 把旧的 `globals()._stgProviders` proxy 直读改成了 live-binding 口。
- **修复：** speech.ts 三处改走 `featureRegistry`（`globals()`）——读文档/修订号、写回 reload 后的新文档与修订号；proxy set 命中 settings 包存取器 setter，与 `_saveServerConfig` 随后 `Api.modelRouting.replace(_stgModelRouting, _stgModelRoutingRevision)` 读写同一份状态。feature-registry.ts 注释澄清 read/writeLiveRuntimeBinding 只覆盖 app-runtime 保留作用域，懒包状态须走 registry。
- **规格（新建 tests/providers/test_speech_provider_persist.py）：** node+jsdom 台子精确复刻生产拓扑——状态仅经 registry 存取器存在，window（对演 app-runtime 作用域）全程无此名并钉死断言。create 路径：CAS 修订号 7 传递、reload 后新文档与修订号 8 落回 settings 作用域；delete 路径：`deleteProvider('stt', 11)` 与落点。负面对照：同台子跑 HEAD 旧版 speech.ts 精确复现 `model-routing v2 authority is not ready`。
- **验证：** 新规格 1 passed；邻近契约（model_brand_resolver/provider_model_add/browser_adapter_upgrade_ui 等）33 passed；runtime_composition/bundle_manifest_parity 4 红为兄弟在飞域存量（stash 本修改后复跑同 4 红，指向 routes/common.py 标记与 conv_state_reducer 等未触文件）；ruff 绿；typecheck:modules（tsc）净；check:frontend 全链绿，新 bundle（settings-hsQXL3Z-.js 等 117 assets）已发布。
- **生效条件：** 纯前端——浏览器硬刷新生效。
- **邻近观察（未动）：** paper 域 qa.ts/report-runtime.ts 对 `_paperHash` 也用 read/writeLiveRuntimeBinding，而 paper 其余模块走 `globals()` proxy；若 `_paperHash` 所有者同属懒包则存在同类潜伏错配，留 paper 域 owner 核实。

### 2026-09-05 (访问矩阵圆点 toggle 点了不生效根修：per-access 列序号错索全局 credentials 数组)

- **起因（用户报告+截图）：** 访问矩阵里点 gemini-3.5-flash 某格的空心圆点想开通，毫无反应；且「有的格子点了有反应、有的没有」。
- **根因：** 矩阵列号用的是 provider 作用域序号（`context.credentials` = 全局数组按 `provider_access_id` 过滤后的位置），而 `_toggleMatrixAccess` 拿这个序号直索引未过滤的全局 `_stgModelRouting.credentials`。该 provider 的两条凭证在全局数组位于下标 1、2（前面还有一个 provider 的凭证占下标 0），于是点列 0 实际改的是别家 provider 的凭证——被点的格子的授权行纹丝不动，圆点永不翻转；凭证恰从全局下标 0 开始的 provider 则完全正常，这正解释了「有的灵有的不灵」。同文件的 `_applyMatrixRecommendations` 走的是 `context.credentials[i].row` 正确路径，独漏 toggle。
- **修复（access_matrix.js `_toggleMatrixAccess`）：** 凭证解析改经 provider 作用域 `context.credentials[credIdx].row`（`.row` 即全局数组同一对象引用，保存流照旧持久化）；offering 侧 `offeringIndex` 本就是全局下标，不动。
- **规格（新建 tests/auth/test_access_matrix.py）：** `test_toggle_targets_provider_scoped_credential`——node harness 双 provider 三凭证（p1 的凭证占全局下标 1/2），点 p1 矩阵列 0/1 授权落在 cred-p1a/cred-p1b、别家 cred-p0 始终为空，再点翻转撤销。修复前该测试按预期转红（授权错落到 cred-p0）。
- **验证：** 新测试 + 相邻 test_model_brand_resolver.py 共 6 passed；check:frontend 全链绿（compose 164 sections + vite build 重发布 main-DgrEftcc.js）。
- **生效条件：** 纯前端——新 bundle 已发布，浏览器硬刷新生效。

### 2026-09-05 (DeepSeek V4 Pro 0813 帕累托图 $0 根修：迁移折叠日期针归零价 + 在线 CAS 修复)

- **起因（用户报告+截图）：** 性价比帕累托图上 DeepSeek V4 Pro 0813 落在最左 $0 列（tooltip `AA/质量 53.2 · $0/1M`），成为假前沿点。
- **根因：** 目录扫描对日期针 `deepseek-v4-pro-0813` 没有价格卡，却物化了显式 `{input: 0.0, output: 0.0}` 的 list_pricing 进模型路由聚合；厂商不会对针重新定价，这个 $0 不是官方免费档。计费缝（lib.pricing.pricing_for_model）早就有 dated-pin→rolling-alias 折叠，但迁移面 `plan_legacy_migration` 没有对应规则，$0 一路进了 authority 并被图表当成官方价。
- **修复（两面）：** ① 在线修复——经 `PUT /api/v1/model-routing` CAS（revision 7→8）把 `deepseek-v4-pro-0813` 的 list_pricing 折到滚动别名 `deepseek-v4-pro` 的价（$1.32/$3.96，含 cache_read $0.044），3:1 混合成本 = $1.98/1M；② 根修——`lib/model_routing/migration.py` 新增 `_fold_dated_pin_list_pricing`：normalize 前扫 document.models，非正价的单段 `-MMDD`/`-YYYYMMDD` 后缀针继承同 creator 下滚动别名的正价 list_pricing（整体深拷贝含 cache_read）；无正价别名时保留显式 $0 不动。
- **规格（tests/model_routing/test_model_routing_contract.py）：** `test_migration_folds_dated_pin_list_pricing_to_rolling_alias`——零价针折到别名价（含 cache_read 透传）、别名自身价不变、无别名针（kimi-k2-instruct-0905）保留显式零。
- **验证：** 阶梯 68 passed（model_routing_contract 20 + bootstrap 4 + pricing dated_folding/refresh/tier_sync + dispatcher_v2 + api_v1 合同 44）；ruff 绿。在线复查：authority revision 8 上针价已正，剩余零价日期针（LongCat ×2、mistral ×3、kimi-k2-instruct-0905）均为文档内无正价滚动别名，属数据缺口而非折叠漏网。
- **生效条件：** 在线数据即时生效（图表下次拉取即正确）；迁移代码改动对将来重跑迁移/新导入生效，server.py 无需重启（纯迁移路径）。

### 2026-09-05 (访问矩阵三连修：凭证列头显示 key_hint + 探测按钮只转闪电 + 模型列收缩适配)

- **起因（用户报告+截图）：** ① 矩阵凭证列头只显示「凭证 N / api_key」，两个同 kind 密钥无法区分；② 探测中整个工具栏按钮连文字「正在后台探测」一起 360° 旋转（截图红框里斜挂的文本框）；③ 模型名列占掉一半以上宽度——名字根本没那么长。
- **根因：** ① `_renderAccessMatrix` 列头只渲染 `cred.kind`，而 `key_hint`（secret_hint 掩码，sk-…•••…wxyz）同行数据本就在 `_stgModelRouting.credentials` 上；② `.stg-mx-probe.running { animation: stg-mx-spin }` 作用于整个按钮元素而非内部 svg（`.stg-mx-zap.probing svg` 早已是正确模式）；③ 表格 `width:max-content; min-width:100%` 时 auto layout 把多余宽度按内容比例分发给最宽的模型列——td 的 max-width 在 auto layout 下不生效，列宽失控。
- **修复：** 列头第二行改渲染 `key_hint`（无 hint 的 oauth/local_identity 回退 kind），title 带 `kind · hint` 全文；`.stg-mx-credkind` 加 `max-width:150px` + ellipsis 防长 hint 撑宽凭证列；`.stg-mx-probe.running svg` 只转闪电；`.stg-mx-corner`/`.stg-mx-model` 用 `width:1%`（min-width:170px 保底，`.stg-mx-mid` 150px ellipsis 封顶）实现 shrink-to-fit，多余宽度归凭证列均分。
- **规格：** tests/model_routing/test_model_brand_resolver.py 新增 `test_access_matrix_header_shows_masked_key_hint`——node harness 双凭证（api_key 带 hint / oauth 无 hint）渲染 `_renderAccessMatrix`，钉住 hint 入 HTML、title 含 `api_key · hint`、无 hint 回退 kind。
- **验证：** 该套件 5 passed；check:styles 绿；check:frontend 全链绿（compose 164 sections + vite build 重发布）。tests/meta/test_runtime_composition.py 有 2 条基线红（project-brain-status.js / core/conv_state_reducer.js 被兄弟在飞提交删除，与本改无关）。
- **生效条件：** 纯前端——新 bundle 已发布，浏览器硬刷新生效。

### 2026-09-05 (GPT-6 Astra 入表：复用 openai.json 单合同五处自动派生，只补三处静态表 + Codex tier)

- **起因（用户报告）：** GPT-6 Astra（OpenAI，GA 2026-09-03，API id `gpt-6-astra`）发布后配置与订阅更新里都没有；同问 Fable 5.1（昨日已入表未提交，今日 14:09 server.py 重启后工作树六表已在进程内生效——supervisor 只负责 spawn/kill server.py，自身进程年龄与配置无关）。
- **与 Fable 5.1 入表的结构差异：** OpenAI 旗舰系走 `lib/model_info/data/openai.json` 单一声明合同（gpt56_pricing_rows/gpt56_slot_configs/is_official_gpt56_model/OPENAI_TEMPLATE 派生定价、slots、compaction、1.05M ctx/128K 输出、两个 picker 模板），所以 Astra 只需：①openai.json contract.models + offering_recipes 各加一行；②bootstrap_pkg/providers.py builtin openai 兜底模板加一行；③_release.py 发布日期针（`gpt-6-astra` → 2026-09-03）；④oauth/outbound.py `_CODEX_MODEL_TIERS` team/plus/pro 三档加 `gpt-6-astra`（发布节奏：Plus/Pro/Business/Enterprise「未来数日」陆续开放，free 档不加；Business 走 team 别名）。无需 aliases 组（单一拼写）、无需 _slots/_tables 手行（合同派生）。
- **数据（双源核对：yottalabs + aiwiki 引 OpenAI 9-03 定价页）：** $10/$50 每 1M，cached input $1（read mul 0.10）、cache write $12.50（write mul 1.25）、>272K 长上下文 $20/$75——与合同既有 tier 公式（input×2/output×1.5）精确吻合，故家族级常量零改动。role=flagship，slot cost=0.050。
- **已知小差（遗留）：** 合同 reasoning_efforts 是家族共享枚举含 `none`，Astra 实际不支持 `none`（仅 low/medium/high/xhigh/max）；默认 medium 不受影响，仅用户显式选 off 才会被上游拒绝——如需 per-model effort 枚举是另一条独立改动。
- **规格（tests/llm/test_openai_gpt56_contract.py）：** expected 模型集补 `gpt-6-astra`；新增 astra 双档定价针（272K 边界 $10/$50 vs $20/$75、cache 双 mul、tier id）。
- **验证：** gpt56 合同针过；bootstrap_model_catalog/pricing_tier_sync/pricing_dated_folding/capability_taxonomy_parity 53 passed（test_multi_agent_wire_contract_includes_beta_header 1 红为兄弟在飞域——_round_request_prep.py multiAgentStage latch 重写，与本改动无关）；context_profile_contract/billing_context_tiers/compaction_gate_tokens/claude_line_version 93 passed；oauth 合同 76 passed；ruff 绿。
- **生效条件：** 纯 Python/JSON 表改动——重启 server.py 生效；订阅侧 Codex tier 在用户打开 OAuth 设置页（/api/v1/oauth/status 触发 reconcile_oauth_providers upsert）或重新登录时刷新 ProviderAccess；既有 OpenAI provider 行的模型清单不自动回填，Settings 里重加/同步模板后 picker 可见。
### 2026-09-05 (Codex 订阅 404 根修：共享调度池统一 Responses 协议词汇)

- **根因：** v2 Connection 的 `openai_responses` 原样进入共享 Slot，传输层只识别 `responses`，故向 Codex 错发 `/backend-api/codex/chat/completions` 和 Chat Completions body。临时 Slot 原有局部转换掩盖了入口差异；同路重试无法修复错误路径。
- **修复：** 协议转换收口到 `Slot.__post_init__`，删除 ephemeral 的重复转换；持久化 Connection / RouteSnapshot 保留 v2 词汇。Codex 与普通 Responses 服务商共用该边界。
- **验证：** 新增共享池 Codex / keyed Responses 回归先双红（实际 URL 为 `/chat/completions`），修复后共享池与临时入口 19 passed；Responses、OAuth、404 重试、槽位适配、压缩策略和缓存邻域 162 passed。离线只读当前路由权威，再由真实共享池构造目标槽位，以合成最小提示实测 `gpt-5.6-sol`：sync 3.18s / async 3.41s，均返回 `OK` / `stop`；未读取或改写 live sidecar 凭证。
- **生效：** 代码已验证；现有 worker 需按运行规范经 UI 批准重启后加载。其他在飞修改未回退，未以并发工作树的全量套件作为本次验证结论。

### 2026-09-05 (长会话切换卡顿数秒根修：Turn 内 block 级窗口——单 Turn 上千块不再全量挂 DOM)

- **起因（用户报告）：** 切到很长的对话卡顿几秒。浏览器实测基线（conv `mtn1xf31zzaqnv`）：3,544 个 block、约 134.7k DOM 节点，切换主线程最长长任务 ~5.8s。80-Turn 窗口（CONVERSATION_DOM_WINDOW_MAX_TURNS）挡不住「单个 Turn 内工具轮次堆出上千块」的形状。
- **修复（conversation-surface.ts，镜像 Turn 窗口模式）：** 新增 block 级窗口 `CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS=200` / `BATCH=100`（硬顶 500，windowing.maxBlocksPerTurn/blockBatchSize 可小调）。`blockWindows: Map<presentationId, startIndex>`——超窗 Turn 初始只挂尾部 200 块，Turn 头部插 `turn-blocks-earlier` 披露控件（pill 样式），每次点击向前扩 100；展开态同会话重渲染保留、跨会话 render 清空；流式追加自然留在可见窗口内。emitIntent 增加 `data-conversation-block-window-action` 分支；i18n 键 `chat.loadEarlierBlocks`（zh/en，{n} 计数）由 conversation_turn_store.js 的 windowing.blockLabel 注入；常量经 conversation/index.ts 导出。另修 placeManagedAt 游标在陈旧重复节点被移除后脱落的边界（直接 keyed-children 遍历先存 next 再删）。
- **规格（tests/frontend/test_frontend_conversation_surface_vite.py）：** `test_surface_windows_blocks_within_oversized_turn`——450 块单 Turn：初始 200 块/首块 block-250/renderBlock 恰好调 200 次、控件文案与 DOM 顺序（header→blocks-earlier→blocks）、三次点击 200→300→400→450 后控件隐藏、同会话重渲染保持展开、跨会话重置回窗、100 块小 Turn 不触发窗口。调试插曲：fixture 首版把所有 segment 标 terminal=true 且 content=''——视图模型对 terminal text 块取 `projection.content ?? segment.text`（conversation-view-model.ts:619），空串不触发 ?? 回退，全部块 displayMarkdown 为空被丢（同文件 1420 行过滤），改为仅末块 terminal + content 同步。
- **验证：** 该套件 70 passed；check:frontend 全链绿（runtime/契约/i18n/TDZ/canary）；浏览器实测同 conv 两次访问：DOM 节点 134.7k→17.6k（-87%），初始挂块 3,544→404，最长长任务 5.8s→375ms（warm）/75ms（cold），页面出现 4 个块窗口披露控件。用户感知的主线程冻结约 15 倍收敛；click→settle 墙钟仍 ~4s，残量主要是会话数据拉取与 200 块/turn 的 markdown 渲染，如需再收可下调 maxBlocksPerTurn。
- **生效条件：** 纯前端——新 Vite bundle（main-jBrHQREG.js）已发布，浏览器硬刷新生效。

### 2026-09-05 (带图排队消息永卡 + 排队角标打字狂闪双根修：queue activate/cancel 摘除 receipt + composer markup 签名记忆)

- **起因（用户报告，conv mtnutburlq1uc7）：** ① 带粘贴图（196KB）的消息发进排队后永远不进生产队列，日志每 drain tick 重复 `Command response is too large for a receipt`；② 打字时输入框右下角排队数字角标疯狂变大变小闪。
- **根因（前后端各一）：** ① `turn.queue.activate`/`turn.queue.cancel` 的响应含完整 Turn publics—— queued input Turn 的 projection 内联 base64 图片（随机字节不可压缩），超 64 KiB stored-receipt 上限（receipt_codec.py MAX_STORED_RECEIPT_BYTES，zlib level-1 一次尝试后仍超即抛 StorageError）；receiptRequired=true 使整个命令事务回滚，drain tick 每次重试同样回滚，队列行永久搁浅。② composer 的 draft listener 每次击键触发 requestRefresh → `updateComposerSendControls` → `showStop` 无条件 `button.innerHTML = STOP_ICON + queueBadge(...)` 重写——`.queue-badge` span 被重建，`queueBadgePop 0.3s` CSS 动画每击键重放一次。
- **修复：** ① 两个 queue 生命周期命令摘除 receiptRequired（OperationSpec turns.py + contracts/storage_operations_v1.json + contracts/tofudb_ir_v1.json 三面同步，gen 脚本重生成 generated_storage_operations.rs 含 sha；generated_tofudb_ir.rs 不发射 receipt 字段无 diff）。幂等性不依赖 receipt：activate 提交后队列行已删，重放答 not_found；cancel 重放答 `cancelled=False`——对齐 attempt 生命周期命令的 receipt-free 模式。② composer-send-controls.ts 新增 `renderedMarkups` WeakMap 签名记忆：markup 串未变跳过 innerHTML 写入（onSend/onStop/className/aria 仍每次刷新），仅状态翻转或 queueCount 变化才真重写。另：`lib/message_queue.py` 预览键白名单补 `steerFallback`，队列条可解释「insert now 降级为排队」的行。
- **规格：** tests/turn_lifecycle/test_turn_lifecycle.py 新增 `test_queued_pair_with_inline_image_activates_and_cancels`——128 KiB 随机 base64 dataUrl 的 queued pair 激活+取消均提交，并断言响应确实令 `encode_receipt_response` 抛 too large（防投影外置化造成假阴性）；tests/frontend/test_send_button_stop_feedback.py 测试台给按钮加 `markupWrites` 计数器，钉住打字三连击 `writesWhileTyping=0`、状态翻转才 +1（cleared=3 / idle=4 精确钉）。
- **验证：** 阶梯 60 passed（turn_lifecycle/receipt_codec/message_queue/pre_authority/send_button）；sidecar 合同 138 passed/1 skipped；差分 141 passed 仍全绿；check:frontend 全链绿（compose 164 sections 重生成）；ruff 绿；cargo check --lib 净。
- **生效条件：** sidecar 重启（Python OperationSpec）+ tofu-db daemon 重启重编译（generated Rust）；前端已重新 compose，浏览器硬刷新生效。

### 2026-09-05 (Tofu-DB 差分套件首次全绿：4 条基线红根修——update_status 入合同 + tool_result 响应键对齐 legacy)

- **起因：** 差分套件长期 4 红（baseline 记为兄弟在飞域，兄弟提交落地后红仍在）：orchestration events 终态栅栏脚本 step 9 `orchestration.run.update_status` 被 tofu-db 答 `operation_not_implemented`（step 10/11 连锁失败）；tool_result_artifact ×3 脚本 read/search 响应多出 `expiresAtMs`/`mediaType`/`visibleBytes` 三键。
- **根因（两个独立缺陷）：** ① `orchestration.run.update_status` 在 legacy OperationSpec 注册表（workflows.py:23）与 generated_storage_operations.rs 都在册，唯独 contracts/tofudb_ir_v1.json 的 executableOperations 漏登记——daemon admission 按此名单默认拒绝，Rust 域逻辑 `run_update`（orchestration.rs:928）早已就绪却从未可达；② artifact.rs 的 tool_read/tool_search 响应比 legacy `_artifacts.py` 的逐字键集多三个信息键，差分门是逐键全等比较，多键即发散。
- **修复：** 合同 executableOperations 补 `orchestration.run.update_status`（command/receiptRequired=true，对齐 legacy OperationSpec）+ gen 脚本重生成 generated_tofudb_ir.rs；artifact.rs read/search 响应删三键与 legacy 键集逐字一致（put 响应保留 expiresAtMs/contentSha256——legacy put 同形，差分本就走绿）；pre_authority 钉住的操作名单同步补钉。
- **验证：** 差分套件 141 passed（+4，首全绿、_PORT_BACKLOG 与 KNOWN_DIVERGENCES 均未为此开口）；pre_authority 5 passed；cargo test --lib 520 passed；v2_client+project_recent_relink 16 passed；ruff 绿。
- **生效条件：** tofu-db daemon 重启生效。

### 2026-09-05 (五路并行架构遗留审计：存量债收敛，真债集中在 tofu-db 迁移面与文档口径)

- **方法：** 5 个 read-only reviewer agent 并行（存储遗留 / tofu-db 迁移域 / 死代码 / 文档时效 / 前端双栈）+ 本地侦察。工作树 855 文件在飞（666 删除全部在 packages/tofu-db Rust 重构），审计避开在飞 diff，只报既有债。
- **存储遗留（净）：** routes/、lib/、lib/tasks_pkg/、bootstrap_pkg/ 零 legacy `conversations`/`conversation_messages` 引用，全部走 lib.storage 客户端 → sidecar；残余 legacy 表 SQL 仅存在于运维脚本（storage_deep_clean / migrate_pg_to_sqlite）、debug/inspect_conversation.py 与测试。`TOFU_STORAGE_MODE` 在 runtime_guards/Helm/install.sh/契约测试四面强制拒绝。松散端：3 个测试 fixture 仍播种 legacy conversations 表（tests/test_turns_v2_api.py:18,217、test_conv_state_ssot_drift.py:44、test_conv_open_no_restamp.py:7 docstring）；storage_deep_clean 的 legacy-retirement pass（约 600 行）待人工定退役时间线。
- **tofu-db（关键发现）：** `scripts/tofudb_migrate.py` 不在 master——50d2b9cf9 只存在于 tofudb/integrate 与 tofudb/par-mig 分支，master 仅有 tofudb_v2_client.py。因此 docs/STORAGE.md:59 引用了 master 上不存在的文件；STORAGE.md:58「278 可执行 / 53 拒绝」已过时，实际 315/331 可执行、16 默认拒绝（contracts/tofudb_ir_v1.json 准确）。迁移注册表只有 credential/billing/record 三域，其余 32 域 IR 执行面在推进（paper/research/raw_archive/artifact 等 Activate 提交）但迁移器无 scan/replay 计划。
- **前端双栈（迁移中，文档诚实）：** 121 个 retained JS sections vs 30+ TS feature 目录；未迁移面最大两块：orchestration canvas/palette/shell 约 70 个 sections、settings 10 个面板全 JS；translation.js 页生命周期轮询是文档点名遗留。`static/js/` 已不存在（static/ 纯 generated 输出）。FRONTEND_ARCHITECTURE.md 如实描述 ratchet-to-zero 中间态，无虚报。
- **文档：** catalog.json 漏 3 文件（README_EXTENSION.md、TOFUDB_RECORD_LIST_LIKE_PROPOSAL.md、TOFUDB_SEARCH_SNIPPET_PROPOSAL.md）；两个 proposal 均为「DECIDED 2026-09-04 且已实现」状态，按 no-archive 规则应删并把 record.list 字面前缀语义/snippet 折叠语义折进 STORAGE.md；ENTERPRISE_READINESS_AUDIT.md 内容已是 current-state 契约但名字像过时审计，建议改名；全库抽样仅 1 处 cosmetic dead ref（docs/modules/scheduling_ops.md:49 模块符号被反引号渲染成路径）。注意：两个 proposal 与在飞的 STORAGE.md/TOOL_INVENTORY.md 修改相邻，WIP owner 可能已计划处理。
- **死代码（净）：** 真 FIXME=0（唯一命中是术语 stoplist 字符串）；deprecated 40 处全为有意机制（契约 flag、openapi 机制、lib/llm/_transport.py 带测试的 env 别名 shim）；源码树 TODO≈55 且绝大多数是 todo_write 功能词误命中；TODO(enterprise) 11 处为制度性架构债标记（by design）；孤儿模块 2026-08-30 已清一轮（5 模块 585 LOC，853e6b429）；scripts/find_dead_functions.py 全量扫描 >240s 超时，宜在稳定树上跑。

### 2026-09-05 (Tofu-DB project.relink 跨面移植：recent+brain+会话设置三族一事务，端口棘轮转绿)

- **边界：** 激活 `project.relink`（1 op）——2026-09-04 daily-cost 条目明确「暂缓拒绝」的最后一op，当时 Project Brain 族未迁移；knowledge 域落地后障碍清除。可执行且差分覆盖目录面 283→284。
- **语义对齐（对 legacy `_project_relink`）：** `recent_project::relink` 重键 recent 条目——目标已存在时 count 相加、last_used 取 max、owner 精确计数减一；`project_brain::relink_scope` 整体迁移投影+事件流（双 scope 均有时走 `merge_relinked_projections`：不可变行按身份去重、冲突报 identity conflict、上限报 limit reached）；`conversation_header::relink_project_settings` 覆盖活跃+回收站两表——LIKE 候选扫描（上限 10000 与 legacy `PROJECT_RELINK_CONVERSATION_LIMIT` 一致，+1 探针超限报 `database_conflict`），仅在声明的 project 设置面精确值重写（`projectPath` 单值 + `projectPaths`/`readOnlyPaths` 列表替换后去重），不推进 rev。错误对齐：old==new → `database_protocol_error`，old 不在 recents → `database_not_found`。
- **IR：** `TransactionIrStep::ProjectRelink{old_path,new_path,updated_at_ms,slot}` + `compile_project_relink`（owner scope 校验、4096 字符帽与 legacy 一致）+ dispatch 臂——三族写共享一个事务，响应 `{project, projectBrainMoved, conversationsMoved, trashedConversationsMoved}` 与 legacy 逐键一致。
- **规格（3 脚本 9 步组 + 棘轮）：** test_tofudb_differential 新增 `_project_relink_scripts()`——① 迁移 recent+brain+活跃/回收站 pins（含 `readOnlyPaths` 列表去重写、relink 后 `project_brain.get`/`active.list` 新键可查、扫尾 finish+delete 不污染模块级 legacy authority）；② 已存在 recent 条目合并（count 2+1=3、last_used max）；③ old==new 与未知 old 双错误分类。端口棘轮同步转绿（newly_executable 清零）。docstring 覆盖数 283→284。
- **落地排障：** insert_after 锚点残留重复 `pub(crate) fn clear(` 签名行致 unclosed delimiter（删之）；`transaction_ir.rs` 无 `invalid_data` helper——按文件既有风格内联 `io::Error::new(InvalidData,…)`。
- **验证：** relink 3 脚本+ratchet 4 green；全量差分 133 passed，仅剩 4 条基线红（orchestration events 终态栅栏 + tool result artifact ×3，均兄弟在飞域，baseline 已记）；Rust 触改模块单测 160 passed（recent_project/project_brain/conversation_header/transaction_ir/semantic_executor）；pre_authority+v2_client+project_recent_relink 21 passed；ruff 绿。
- **生效条件：** tofu-db daemon 重启生效。

- **合入（与兄弟 paper 两提交三方并）：** 兄弟在 HEAD 连续落 `paper_artifact`(521d48630)+`paper_podcast`(37a6e1644)，本地 relink+knowledge+plugin 基于 HEAD~2，12 文件冲突——contract JSON 按名字排序位 union（paper.* 前、plugin.* 后），generated IR 由 gen 脚本重生成消解，semantic_executor dispatch 双臂并留（knowledge 与 paper.podcast/report/translation 均须先于 paper.* 通配），differential/pre_authority 清单 union。合并后全量重验：cargo check 净、cargo test --lib 520 passed、差分 137 passed（仅剩同样 4 条兄弟在飞基线红）、pre_authority+v2_client+relink 21 passed、ruff 绿。提交含 knowledge.rs/plugin.rs 两新文件（lib.rs mod 声明依赖，缺之树不可编译）。JOURNAL 积压多条兄弟条目未随本提交走（append-only，留兄弟自行提交）。
### 2026-09-05 (工具搜索-披露-压缩零结果死循环根修：ledger 参数签名 + composer disclosed_tools 尾块 + 重复查询破循环)

- **起因（用户报告，conv mtmoc0cjzapyq4）：** 会话密集工具失败——搜索「experiment rerun fork batch launch trials canvas · mcp__llm」零结果，随后 `mcp__llm__experiment_run`/`_fork`/`_rerun`/`_batch_fork`/`_get_node_config` 连续 6 次「未执行：参数校验未通过」（缺 ids/base_id/name/experiment_id）或 tool_not_enabled，模型在多词宽检索与猜测参数名之间死循环。
- **根因（三层叠加）：** ① deferred 工具 schema 是一次性披露——承载它的搜索页随轮次老化被压缩摘要吞掉，而模型不知道哪些名字已披露过，于是换关键词重跑宽检索；② 宽检索只返回未披露工具，已披露名字被省略后查询零结果，零结果又被读成「继续搜」；③ 参数名无从得知（搜索页已丢），模型凭猜调 execute_tools，连撞 contract 拒绝。与 mtgzs6bnmglpfa（task_id 猜成 epic_id）同族但更广：那次是单工具参数猜测，这次是整片工具族不可见。
- **修复（四处联动）：** `lib/tools/disclosure_state.py` ledger 条目新增有界参数签名（required/optional 名，各 ≤16、单名 ≤64 字符，不含值），新增 `disclosed_entries()` 归一化读取；`lib/tasks_pkg/context_composer/_providers.py` 新增 `disclosed_tools` 尾块（`tools.disclosure`，placement=tail，≤700 tokens，≤16 条 × ≤8 参数名）——每轮从 ledger 恢复「已披露但不在 wire」的 deferred 名单+签名，指示走 execute_tools 精确全名调用、禁宽检索，ledger 不可用静默省略；`lib/tasks_pkg/handlers/tool_gateway.py` 对同任务逐字节相同搜索计数，`repeated_query≥2` 时宽检索 notice 替换为破循环指令，但精确名恢复（already_disclosed/already_visible/resolved_name）保留原恢复提示不覆盖；`lib/tools/gateway.py` 精确名未中时 notice 明确「目录固定，重搜同名不会出现」并给出候选/放弃二选一。
- **规格（4 文件 121 针）：** test_tool_disclosure_state（签名归一化/record 合并/disclosed_names_for_catalog 指纹对齐）、test_context_composer（尾块渲染/跳过 wire 可见与已出目录名/disabled_blocks/state_unavailable 静默）、test_tool_search_loop_breaker（重复计数/破循环 notice/精确恢复豁免）、test_unified_tool_gateway（missing_name 破循环 notice/execute 路径修复）。
- **验证：** 目标 4 文件 121 passed；邻接契约环（input_repair/gateway_sanitize/strict_no_fallback/errors_accounting/programmatic_serial/outage_cap/contract_assembly）266 passed；压缩+检索环（compaction_* 20 文件/mcp_tool_search/tool_search_evaluation/orchestration_composer/manual_compaction/storage_projection_compaction）474 passed；ruff 绿。全量 19k 套件 30 分钟未跑完被弃（并发写者移动靶）；collection 期两处兄弟 WIP 红（test_frontend_watch_follow_up 缺 project-brain-status.js section、test_human_gate_http_owner asyncio marker）均 HEAD 即在，与本 diff 无因果。
- **生效条件：** 纯 Python——重启 worker 生效。

### 2026-09-05 (幻灯片任务「只有 started 没有下文」根修：WorkUnit 跨面投影 + 前端追踪卡——产品轴降级优先、状态词表唯一)

- **起因（用户截图）：** 幻灯片工具卡片显示视频徽标 + `started`，之后永远没有任何完成/失败回执。用户问「为什么显示视频、有没有跑完没人说」。
- **根因（两层）：** ① 徽标语义错位——motion_video handler 把 slides-deck 等生产任务一律打上「视频」徽标，且 queued/running 混在 started 一词里；② 状态断链——后端有完整 stage 事件流（stage_started/done/retry/…，lib/production/stages.py），但前端没有任何渲染方消费它，任务终态（含 artifact_quality.degraded 产品轴）到不了用户界面。
- **契约先行：** docs/contracts/EVENTS.md §1 补 stage 事件文法修订（stage_started/done/retry/skipped/failed 五注册类型，checkpoint_invalidated 与 per-capability extras 保持私有）；docs/contracts/API_CONTRACT.md §2.18 新增 workUnit schema——唯一跨面生命周期枚举 `queued|running|retrying|done|degraded|failed|aborted`，浏览器逐字渲染投影、永不自行推导状态。
- **后端：** `lib/production/work_unit.py`（新，纯投影器 (task, events)→workUnit，runtime 状态轴全映射、未知状态投影为 failed 而非静默重标，done+degraded→degraded 产品轴压过交付轴）；`routes/api_v1/tasks.py` GET detail 带全量 workUnit、list 带 compact；`lib/tasks_pkg/handlers/motion_video.py` 徽标归一化（queued/running 分词）+ extra.workUnitRef。
- **前端：** `tool_rounds_rich.js` 新增 WorkUnit 卡富渲染（produce_slides 正文、per-tool 来源 chip、状态 chip、data-wu-task 属性）；`workunit_tracker.js`（新 section）——扫描 data-wu-task 注册表，非终态轮询 GET detail，指纹比对刷新卡片，终态停轮询；探针 `_wuTracked/_wuTrackedCount` 由真实内部调用保持可达（reachability 大门）；`09-tool-terminal-details.css` stepper + degraded 修饰 + 完成行；i18n zh/en 键 + 生成 json 重建。
- **规格：** tests/production/test_work_unit_projection.py（新 174 行：状态映射全参数化、degraded 优先、非 workUnit kind→None）；test_motion_video_p4.py、test_frontend_motion_tool_render.py 更新。
- **验证：** 五套件 72 passed（work_unit_projection/motion_video_p4/frontend_motion_tool_render/motion_video_display/api_contract_drift）；ruff 绿；check:frontend 全链绿（runtime 164 sections、reachability 1650 callables、TDZ、styles 32 sections、双契约 current、i18n 4804 keys、bundle canary）。
- **生效条件：** 后端纯 Python 重启 worker 生效；前端已重新 compose runtime+styles，浏览器硬刷新生效。

### 2026-09-05 (工具参数校验「太严格」三层根修：报错带边界值 + 描述写边界 + 修复层数值 clamp 收敛)

- **起因（用户观察）：** MCP 工具调用反复死于参数校验（`mcp__xuecheng__read_doc`：`Value below minimum at $.max_chars` → invalid_argument_value 整轮作废），质疑校验是否太严格。
- **分析（三层缺陷叠加）：** ① contracts.py 的 min/max 分支报错只说「below minimum」不给边界值（length 分支早就带，min/max 漏对齐）；② 高频参数描述不写边界（edit_file/write_file description、todo id/content、search_tools/artifact limit、xuecheng max_chars），模型只能靠猜，猜错即拒；③ 修复层对 server-dynamic (MCP) schema 整体豁免，越界数值（limit/max_chars 这类 sizing wish）直接 fail-closed 拒绝，白烧一整轮。
- **修复：** P0 contracts.py min/max 报错对齐 length 分支带边界值；P1 上述高频参数描述全部补边界（含 xuecheng-mcp server.py 的 max_chars「(1000–500000)」）；P2 新增 `clamp_arguments_to_schema` / `clamp_tool_arguments_from_documents`（contracts.py）——数值越界 clamp 到声明边界、纯展示字段（truncatable 白名单）按 maxLength truncate、`NEVER_CLAMP_PARAMS` 语义例外（get_conversation.before 导航游标永不可静默挪动）；ingest 接线为 Stage 4.5（静态修复索引豁免 MCP 之后、Stage 5b 契约拒绝之前），直达与 gateway 两条路径同覆盖。clamp 永不抛异常，任何内部错误原样返回保持 fail-closed 行为不变；no-op 保持对象同一性。
- **规格（tests/tools/test_tool_input_repair.py）：** clamp 单元针（min/max/bool 穿透/no-op 同一性/缺文档穿透/未知契约穿透）+ 端到端针 `test_ingest_clamps_mcp_numeric_bounds_before_contract_rejection`——完全复刻用户失败形状（max_chars=10 < minimum=1000），断言 Stage 4.5 clamp 后 parse_error/contract_error 均为 None、fn_args 落到边界值、repair_log 记 `clamp_minimum`/`clamp_maximum`。
- **验证：** test_tool_input_repair 80 passed 全绿；消费方套件（paper_tool_args_repair/edit_file_unified/read_gate_unified/tool_name_slot_collision/refactored_utils/api_contract_mcp_parity/paper_full_tools）151 passed；ruff 绿。xuecheng-mcp 侧 `import xuecheng_mcp.server` ok；该仓 pytest 收集被 vispy「GL ES 2.0 library not found」环境缺失阻断（git stash 对照 HEAD 即红，与本 diff 无因果）。
- **生效条件：** 纯 Python——重启 worker 生效；MCP 描述变更随下次 schema 下发/重连生效。

### 2026-09-05 (task_start_failed「Storage writer acquisition timed out」根修
### 2026-09-05 (task_start_failed「Storage writer acquisition timed out」根修：raw_archive.put 全表 SUM+回读挤出单写者 → storage_meta 增量计数)

- **起因（用户报告，conv mtnkmj4ffxbckc）：** 前端弹 task_start_failed（source=tasks.spawn，raw="Storage writer acquisition timed out"），任务在 spawn 首步写存储时 2s 超时安全终止。
- **根因：** 失控任务 706b5e7c（kimi-k3，560+ 轮、23k+ 事件，每轮归档 ~1MB 请求/响应 blob）持续洪峰——raw_archive.put 在 execute 阶段各占单写者 2s+（blob 解压+大插入 + **每次 put 全表 `SUM(stored_bytes)` 扫描** + **插入后回读整行含至多 16 MiB blob**），turn.event.record commit 2.7–4s；user 优先级获取帽（2s）被持续饿死。与 2026-08-20 fastpath 根修（commit/fsync 慢）是不同变体：本次慢在 execute 阶段大 blob 作业。诊断坑：写者占用 held_by 行落在 logs/server-console.log，不在 app.log。
- **修复：** `_raw_archives.py`——活用量改 `storage_meta.raw_archive_stored_bytes` 增量计数（预算锁下从 SUM 一次性引导，插入后 _charge，partial 降级行 stored=0 也正确记账）；删 post-insert 回读，元数据由在手值直接装配；`_conversations.py` conversation.delete 与 `_turns_lifecycle.py` 两处行集删除在同事务 _debit，计数不再随删除漂移。
- **规格（tests/storage/test_raw_archive.py +201，9 针）：** put 不再回读 / 计数器一次性引导+charge / 会话删除与 turn 行集删除借记 / 预算降级 partial 语义不变。
- **验证：** test_raw_archive 9 passed；storage_sidecar_contract 138 passed 1 skipped；integration_control+storage_maintenance_circuit 36 passed；conversation_commands_v3 19 passed；ruff 绿。test_conversation_change_storage 1 红=兄弟 WIP（ImportError sqlite_conversation_change_references_available，HEAD 即缺该符号，与本 diff 无因果）。
- **生效条件：** 纯 sidecar Python——重启 worker 生效。
- **未做（开放方向）：** 单任务事件洪峰背压与长任务失控保护仍未实施（本次肇事任务由用户手动停止）。
### 2026-09-04 (ModelPlaza 同步器落地：meituan.json 自动适配 aigc.sankuai.com 目录，50→115 recipes)

- **起因（用户指令）：** 美团模板里很多 id 网关并不在售（历史污染），要求以 `https://aigc.sankuai.com/ml/modelPlaza/modelInfo` 为目录权威让模板自动适配。
- **权威链：** ModelPlaza（SSO listV2，目录权威）× 活网关探针（API key，在售证明）→ 交集。plaza 上架 ≠ 在售（死 id 滞留），探针看不到未上架 id；网关 OpenAI `/models` 返回空列表是污染累积根源。
- **交付（scripts/meituan_template_sync.py，新）：** 快照/SSO 抓 plaza → 面分类（chat/image_gen/embedding/skip）→ chat id 走 `lib.provider.provider_probe.build_probe_work` 按 dispatcher 路由面探针 → merge：保 50 / 删 0 / 增 65。存活 recipe 手工字段全保留（request_ids 池/rpm/cost/ctx）；EXCLUDED（退役 GPT-5 家族 + Bailing Ring/Ling）政策压探针；瞬时探针失败保留并标 unverified，只有 not_found/bad_request/unauthorized 才删；镜像归一化包含启发（min-len 5 + 数字边界守卫，glm-52-meituan→glm-5.2 命中、glm-5.3-flash≠glm-5）只提示不自动并；非 chat 候选只报告（默认模型归属是 owner 决策）。--write 幂等（sha256 三次写入一致），定价块整体再生。
- **纠偏（plaza 标签语义）：** vision 权威关键词是「图像理解」（此前只有 图片理解 漏 GLM-4V/glm-4.5v 等），「视频理解」→video（只对 tag 派生放行，名称派生的 video 仍视为生成面剔除）。
- **结果：** meituan.json 50→115 recipes（+824/−122），lib/pricing/_tables.py 同步 +42；hy3-preview/gemini-3.8-flash 探针实证 ok，移出 _NEVER_TEMPLATE（守护测试钉 dated 注记）。
- **验证：** 新规格 tests/test_meituan_template_sync.py 13 针 + 守护 test_meituan_served_catalog_sync.py + recipes + pricing 三套共 64 针全绿；ruff 绿。
- **生效条件：** 纯模板/定价静态数据——重启 worker 后新会话生效。
- **未做（报用户人工定）：** 非 chat 面 plaza 候选 40+ 个只报不加；8 条 mirror hint（glm-52/53-meituan、kimi-k2.6-baidu、minimax-m2.5-tencent、vertex.claude-opus-4.7/4.8 等）待人工决定是否并入 request_ids 池；grok-4.6 保留但本轮探针瞬时超时（unverified）。

### 2026-09-04 (浏览器扩展重配对孤儿标签根修：MV3 worker 被杀丢 tab id、空白页永驻——chrome.storage.session 台账 + init 清扫)

- **起因（用户报告）：** 浏览器自动开的 tofu 标签不会自动关，永远挂成空白页。
- **根因：** 该标签是 `attemptAutoRepair` 阶梯第二档（无 Tofu 页时自开隐藏后台标签 mint 新 key，background.js:479 一带）。服务器不可达时标签停在空白/chrome-error，梯子挂在 `waitForTabLoad` 20s 里；MV3 service worker 此刻被杀，整个 async 函数连同清理路径死掉，tab id 全在内存丢失；worker 经 keepAlive 重启后 `init()` 无任何对账逻辑——泄漏永久化。
- **修正（background.js）：** ① 隐藏修复标签在第一个长 await 之前把 tab id 写进 `chrome.storage.session`（`REPAIR_TAB_SESSION_KEY`）——session 存储跨 worker 重启存活、浏览器重启清空，正是该台账语义；② 梯子 `finally` 清台账；③ 新增 `_reapOrphanedRepairTab()`，`init()` 每次 worker (重)启动即跑：无记录/标签已消失→返回；已完整加载成自家 Tofu 页→保留（下一档梯子可隐形 mint）；空白/about:/chrome-error:/自家地址未完成→关闭；用户已导航他处→撒手。own-server 判定从 `chrome.storage.local` 读 serverUrl，不依赖尚未恢复的模块态 SERVER_URL。
- **规格（test_browser_bridge_auto_repair.py）：** 头部契约补第 6 条；新增三针——台账写入必须先于 `waitForTabLoad` 且带 `!opts.forceTab` 护栏（前台用户手势标签永不进台账）、清扫器形状+init 挂钩、NEUTER 去钩变红针。
- **验证：** 本文件 21 针 20 绿，唯一红 `test_manifests_moved_to_470_together` 是兄弟未提交 WIP 把 dev manifest 顶到 5.5.0 而 store 仍 5.4.4（HEAD 双 5.4.4，与本 diff 无因果）；邻近 auth_backoff/preseed/fleet_registry/popup_design 39 passed；ruff 绿；node --check 绿。
- **生效条件：** 纯扩展改动——chrome://extensions 重新加载即生效；服务器侧无改动。

### 2026-09-04 (模板下拉二修：第二列被裁根因=grid min-content 抵住 460px 帽；logo chip 被主人否掉改回裸标)

- **起因（用户反馈上轮修复后）：** 名字「都显示不全」，右边第二列被截断；logo 的奶油背景框「很丑，不要」。
- **根因（截断）：** 菜单挂在 `.stg-section-actions`（只有 3 个按钮宽）里、自身 `max-width:460px`，而 `.stg-template-grid` 是 `1fr 1fr`=`minmax(auto,1fr)`——nowrap 名字的 min-content（最长 Amazon Bedrock (OpenAI-compat)）抵住轨道，总需求 ~530px > 帽值，第二列溢出被裁。上轮加的 chip 又加宽了 min-content，雪上加霜。
- **修正：** `.stg-template-menu` 改 `width:max-content`（保持 `min-width:360px`，上限提到 `min(600px, calc(100vw - 48px))`），让菜单按两列全名取宽而不是被容器帽住；grid 改 `repeat(2, minmax(0, 1fr))` 允许收缩（名字保留 nowrap+ellipsis 作究极网）；chip 两条规则全删——logo 回裸标。移动端媒体查询（≤760px 单列）原有覆盖属性不变，级联不受影响。
- **复现升级：** repro 改为生产挂载（菜单 absolute/right:0 挂在真实三按钮 `.stg-section-actions` 里、880px 面板），并加可执行指标：menuWidth 539、grid scrollWidth==clientWidth（无横向溢出）、16 个名字零截断；截图确认裸标+全名+hover 奶油行。
- **规格：** 上轮 chip 针改写为 `test_template_item_resets_button_ua_leak_and_grid_may_shrink`——UA 重置四属性保留 + 菜单不得再有 `.stg-template-menu … .stg-brand-icon` 规则 + `width:max-content` + `minmax(0, 1fr)`。
- **验证：** local_deploy+turn_serving_route 15 passed；ruff 绿；check:styles 绿（32 sections，直接取退出码）；check:frontend 大门仍红在 check:runtime（兄弟 runtime WIP 拖陈生成文件，与纯 CSS diff 无因果）。
- **生效条件：** 纯前端样式——已重新 compose，浏览器硬刷新即生效。

### 2026-09-04 (tofu-db 迁移门 + 文件系统认证矩阵/GC 溢出规模证据合入 master)

- **背景：** 并行渗透批 6 代理中 par-paper/par-ops/par-knowledge/par-integ 死于上游限流（`Upstream rate-limit attempt budget exhausted (120/120)`，已记忆 tofudb-swarm-rate-limit-120-budget），par-perf 取消；par-mig/par-cert 完成并带 commit。收割路径：独立 integrate worktree（tofudb/integrate）合并+跑门，再回主 worktree `git merge --ff-only`——脏树直合被拒的模式与文件工具只能写主 worktree 的坑均已记忆（tofu-merge-into-dirty-worktree-ff-only-pattern）。
- **迁移门（50d2b9cf9）：** `scripts/tofudb_migrate.py`（806 行，注册表驱动 + depends_on 拓扑序）——legacy SQLite 只读有界扫描（`mode=ro`+`query_only`，keyset ≤1,000 行/8 MiB），经 `scripts/tofudb_v2_client.py` 走生产 storage.v2 线协议 replay 进真 `tofu-db serve`；credential/billing(wallet+ledger)/record 三域端到端；持久 per-domain checkpoint（原子 rename 发布，源行数+SHA-256 钉死）；`TOFUDB_MIGRATE_FAULT_AFTER_OPS=7` 故障注入杀进程后重跑，收敛到与不间断参照迁移完全相同的域 SHA-256；二次重跑幂等（replayed_ops=0）；legacy .db 前后字节一致（rollback 证据）；机器可读报告 `tofudb-migration-report.v1`，任何 digest 失配非零退出。resume 粒度为 per-domain（域内 replay 天然幂等收敛），剩余域随 op 稳定插入注册表。
- **顺手收口：** `scripts/tofudb_v2_client.py` 入库——scripts/* 默认被 .gitignore 第 113 行整目录忽略，此前快照全漏了它（已记忆 tofu-scripts-gitignore-allowlist），干净 checkout 的差分套件因此缺 import；本次连 `!/scripts/tofudb_migrate.py` 一起补白名单。
- **认证矩阵（b0b68f5b8）：** `certify-filesystem-matrix` 操作员命令 6 cell 全过——local.native；mount.opt-in（真实 FUSE，`TOFU_DB_CERT_MATRIX_MOUNT_DIR` 显式开启才跑，非空目标拒绝）；vfs-fault syscall-error interrupted/ENOSPC 各 56 例、short-write 6 例、lost-sync 29 例，每个确定性用例崩溃后重开恢复恰好一个已提交前缀（durable/checkpoint 序列 + 载荷/块逐字节校验）；每 cell 一行机器可读结果，保留 `measurements_are_observations_not_release_certification=true` 诚实观测框架。GC 溢出规模 5 测试：65,536-mark 阈值精确触发溢出且 spill ≤64 MiB lean 预算；4×65,536 异构 mark 满额运行、第 5 个超预算报错；33.4M marks/1 GiB 预算算术钉死契约常量；删除帽缩比 6 blocks/round 4 轮收 20 orphan 无饥饿（CONTROL cursor 推进）；20M loose 目录扫描界缩比验证越界 fail-closed。
- **验证（本会话在 integrate worktree 亲自复跑，不信代理自报）：** cargo test 482 lib + 5 authority_gc_process + 4 daemon_process + 6 filesystem_certification 全绿 0 failed；cargo clippy --all-targets -D warnings 绿（修掉 par-cert 测试里 2 处 `assertions_on_constants`，par-cert 当时没跑 --all-targets）；cargo fmt --check 仅 integration_workspace.rs:828 红（兄弟在途 WIP，非本 diff，不动）；IR 契约 --check 绿；ruff 绿；pytest test_tofudb_migration.py 3 passed；差分全套 119 passed。STORAGE.md 缺失清单删除 migration/GC spill scale/认证矩阵三项并补两段交付描述。
- **生效条件：** 纯 tofu-db 预权威资产 + 文档/脚本——不影响运行中的 Python sidecar，无需重启。
### 2026-09-04 (tofu-db log_aggregate 域上线 + LIKE 的 ICU 简单折叠奇案)

- **域落地（commit af243f399）：** log_aggregate flush/query 双 op 进 tofu-db——fingerprint UTF-8 字节作物理键，(big-endian last_seen, fingerprint) 扫描索引逐字节对齐 legacy SQLite 索引序，保留插入序复刻 rowid 平局；合并语义（level/logger/template/first_seen 留旧，count/last_seen/sample 取新）、扫描/响应双重上限、2 个差分剧本（合并/清扫/排序/LIKE 元字符/全校验拒绝）进 117 门。
- **根修 LIKE 分歧：** 差分抓到 `q='Α'` 在 legacy 匹配 `α` 而 tofu-db 不匹配——legacy Python sqlite3 是 **ENABLE_ICU** 构建，LIKE 对双操作数逐码点做 Unicode **简单**大小写折叠（ς~σ~Σ、K(Kelvin)~k、ẞ~ß，但 ß↛ss、ﬀ↛ff）。修复=冻结 ICU 折叠表进 `generated_unicode_simple_fold.rs`（1512 映射，二分查找），谓词按折叠后码点窗口比较。
- **坑（已记忆 tofudb-like-icu-simple-fold-and-wrong-icu-trap）：** `find_library("icuuc")` 在本机解到 OS 的 libicuuc.so.50（Unicode 6.2，仅 1055 映射）——从它重生成表会静默缩水破奇偶；生成器必须经 `ldd _sqlite3.so` 追到 conda 的 libicuuc.so.78（Unicode 17）。UCD-17 C+S 经 sqlite3 LIKE 双向逐条校验与 ICU-78 完全一致（1512 映射 0 剔除），双源互证。
- **勿混：** `generated_unicode_casefold.rs` 是 Python `str.casefold()` 全折叠（ß→ss），服务 artifact 域 Python 奇偶，另一份契约。
- **验证：** 域内 5 单测 + 2 差分剧本绿；全量 117 门 114 绿，余 3 红全为兄弟在途 artifact 域（响应多出 expiresAtMs），非本 diff 因果。
- **并发注：** 本域由双会话在同一 worktree 接力完成（折叠表/生成器/消费方互相吸收收敛，sibling 落锤提交 af243f399），全程靠 edit_file 新鲜度闸门防撞。
### 2026-09-04 (本地部署页重设计：硬件探测 → 智能推荐目录 → Agent 部署接力 + DeepSeek V4 Flash 云端兑底 + 失败诊断修复闭环)

- **起因（用户指令）：** 本地部署入口只是个引擎预设选择器，太糙。要求：用 llm+agent 按用户硬件环境推荐最合适的开源大模型并配置环境；安装要健壮，出问题靠 agent 修；跑不起来也要 agent 诊断修复；硬件没戏时建议接入便宜的 deepseek v4 flash。
- **后端（lib/local_serve/）：**
  - `_catalog.py`（新）：手工维护的开源模型目录（1.7B–32B，chat/coding/reasoning/vision/multilingual 标签，每模型 HF BF16 + GGUF Q4_K_M 双变体，权重字节为参数推导的估值——真实下载后由 _probe/_plan 重算权威值）；`evaluate_hardware` 把一次硬件快照映射到目录：可行变体取最优（GPU 上 HF 优先，GPU 档永远压 CPU 档），comfortable→tight→extreme→cpu 档序 + 同档参数降序；`cloud_alternative` 在本机跑不动 ≥7B 舒适模型时给出 deepseek-v4-flash（定价取 lib/pricing 活值 + DeepSeek 模板 key 供页面深链）。
  - `_diagnose.py`（新）：failed 实例的 last_error + 有界日志尾部 → 已知失败类（disk_budget/disk_full/installer_missing/platform/network/oom_exhausted/cuda_driver/arch_unsupported/port_conflict/ollama_setup/start_timeout/interrupted/unknown），每类带人话摘要 + 具体下一步 + retryable 裁决（直接回答 agent 的第一个问题「现在重调 deploy 有没有意义」）；永不抛异常。
  - `api.py`：`recommend(purpose)` 只读（探测+排名+云端兑底）；`deployment_status` 对 failed 行嵌 `diagnosis`——聊天工具与设置页中继同一份指引。
  - 工具面：+`local_serve_recommend`（只读可搜族）；`_fmt_recommend` 输出硬件行/排名候选/下载命令/云端兑底；`_fmt_status` 渲染诊断块。
  - 路由（routes/api_v1/local_serve.py，新）：三个只读 GET（hardware / recommendations?purpose= / deployments 活态对账），providers scope；一切写仍走审批门控的工具族。
- **前端（settings/local_deploy.js 重写 + 08-local-deploy.css 新）：** 页面四段——① 硬件条（GPU 显存/内存/磁盘实时片）；② 智能推荐（用途 chips 过滤，卡片含适配档芯片/参数/变体/引擎/权重估值/下载命令复制，「让 Agent 部署」预填接力进新对话）；③ 云端兑底卡（深链 DeepSeek 模板向导）；④ 托管实例台账（状态芯片 + failed 行诊断摘要 + 「让 Agent 诊断修复」接力，stopped/planned 行给「让 Agent 启动」）；手动接入区（四引擎预设 + 托管路径 + 自定义）原样保留。既有测试针（modal 类/preset 序/分组/staging 权威）全部兼容。
- **顺手修：** zh/en 的 managedDeployPrompt 还引用旧工具名 local_serve_inspect → 改 local_serve_prepare。
- **规格：** tests/local_serve/test_local_serve_recommend.py（新 26 针：排名/用途过滤/未知用途兑底/CPU 档/云端兑底触发与字段/诊断十分类参数化/从不抛/api 形状/handler 格式化/路由三只读针）；test_local_deploy_settings.py 扩展（页面段针 + 27 个新 locale 键）。
- **验证：** 新 26 针 passed；local_serve 全家 + local_deploy + provider_model_add + turn_serving_route 137 passed；ruff 绿；check:frontend 全绿（重组 163 sections/32 styles + i18n 契约 4786 键 + tsc + Vite 117 资产 + canary）。
- **生效条件：** 前后端都有改动——需重启 worker + 浏览器硬刷新。
- **未做：** 未跑真实 playwright 截图（本机无 GPU/实例可渲有意义数据）；目录权重字节为估值，首次真实下载后 _probe/_plan 重算权威值（设计如此）。

### 2026-09-04 (「从模板添加」下拉根修：.stg-template-item 是 <button> 但从未重置 UA 样式——灰瓷贴/居中文/错误字体全漏出)

- **起因（用户报告，截图）：** 设置页「从模板添加」下拉渲成一串灰底瓷贴、名称与模型数居中、字体不对——「样式好丑」。
- **根因：** `_showTemplateMenu` 的项是 `<button class="stg-template-item">`，而基类规则（06-file-changes-settings-shell.css）只给了 flex/padding/border，没给 `background`/`text-align`/`font-family`——UA 默认 buttonface 灰底（rgb(239,239,239)）、`text-align:center`、UA 字体全部漏出。兄弟类 `.stg-local-deploy .stg-local-preset` 当年自己补了 `text-align:left`+背景，基类漏了，所以该菜单自 2026-08-24 引入起就从未以设计形渲染过（截图模型计数证明用户跑的是旧后端，但 CSS 双版本一致，排除「旧样式」）。
- **playwright 实证（独立页 + static/styles.css）：** 修复前 computed 复现 `bg rgb(239,239,239)`/`textAlign center`/`fontFamily Arial`，截图与用户图一致；修复后 `transparent`/`left`/Plus Jakarta Sans。
- **修复（基类 4 行 + 顺带瓷贴化打磨）：** `.stg-template-item` 补 `background:transparent;text-align:left;font-family:inherit;color:inherit`；行几何 7px→8px/10px、radius 4px→9px、grid gap 2px→4px；品牌图标进奶油 chip（`.stg-template-menu .stg-template-item .stg-brand-icon`——`brandIconHtml` 内联了宽高，chip 用 `box-sizing:content-box`+padding 撑大而不覆盖尺寸；选择器作用域限定菜单，local-preset 自带 chip 不会套娃），hover 时 chip 染金辉、行奶油底——与 `.stg-local-preset-icon` 同一语汇。
- **复现期坑：** 设计 token（--s-cream/--s-skin/--s-white）定义在 `.settings-panel` 作用域下，独立页不复刻该作用域时 `var(--s-skin)` 无兜企→border-color 退 currentColor 出一圈深边——是 harness 假象非设计问题；local-preset 渲染验证也不能给容器乱套 `.stg-modal`（自带高度/flex 语义会压扭内容）。
- **规格（test_local_deploy_settings.py）：** `test_template_item_resets_button_ua_leak_and_chip_stays_menu_scoped`——基类四属性针 + chip 选择器必须带 `.stg-template-menu` 前缀、无裸 `.stg-template-item .stg-brand-icon` 规则。
- **验证：** local_deploy+turn_serving_route 14 passed（新增 1 针）；check:styles 绿（32 sections，真实退出码 0——注意 `npm ... | tail` 的管道退出码是 tail 的，先前两次「Node.js v24.14.1」尾行实为 settings.css 兄弟 WIP 陈旧报错被管道掩盖）；check:frontend 大门当前红在 check:runtime（settings-presenters.generated.js 被兄弟 runtime sections WIP 拖陈，api.js/provider_render.js/local_deploy.js/tool_rounds.js/main/* 均兄弟未提交改动）——与本次纯 CSS diff 无因果，共享树移动靶按例上报。playwright 三轮截图：修复前复现用户图→修复后干净菜单→hover 奶油行+金辉 chip、local-preset 瓷贴无回归。
- **生效条件：** 纯前端样式——已重新 compose，浏览器硬刷新即生效；无 Python 改动。

### 2026-09-04 (Meituan 模板去污染：63→50 食谱，全量对齐探针权威；v3.2 镜像族四面清除)

- **起因（用户报告）：** Meituan 模板里很多模型端点实际不服务——早期设计模型/服务商时可能有污染，要求清理干净以便测试。
- **权威比对（probe_cache/95a225612ce5cf84.json，1122 cells，ok|rate_limited=在售）：** 63 食谱中 11 个全部 wire id 探针证死、2 个从未被探——① deepseek-v3.2 + 4 个美团镜像拼写（-tencent/-baidu/-huawei/-doubao）chat 面全 400「invalid model name」，家族已被网关下线；② claude-sonnet-4.6（含 aws. 拼写）400「Access to Anthropic models is not allowed for this account」；③ 8 个非 chat 模态食谱（gpt-image-1.5/2、gemini-2.5-flash-image、gemini-3-pro-image-preview、gemini-3.1-flash-image-preview、text-embedding-v4/3-small/3-large、Doubao-Seed-ASR-2.0）在各自 image/embedding/transcription 探面同样 404；④ hy3-preview（混元，tencent.json 才有正当居所）与 gemini-3.8-flash（未提交的新增 hunk）从未在该网关被探，无法证实即清除。
- **四面同步（记忆 meituan-served-catalog-sync-rules）：** meituan.json 删 13 食谱（纯 -122 行）；_aliases.py 删 v3.2 美团镜像组；_slots.py 删 4 镜像槽位 + 留 RETIRED 日期注（plain deepseek-v3.2 保留——tencent.json TokenHub 模板仍在售）；_tables.py 两处定价表各删 4 镜像行。共享 id（hy3-preview、gemini-3.8-flash、claude-sonnet-4.6 家族、图像/嵌入模态 id）的全局槽位/定价/taxonomy 不动——其他模板与 builtin 仍在用。
- **守卫测试（test_meituan_served_catalog_sync.py）：** _NEVER_TEMPLATE 追加整块 2026-09-04 剔除清单（含「探针证死」与「从未被探」两分类注释）+ docstring 记录本次清理依据。
- **验证：** 守卫 + 邻近 8 套 148 中 147 passed——独挂 test_frontend_model_fold::test_consumers_route_through_the_typed_fold_owner（_renderProviderModelList 符号在 frontend/src 全树不存在，兄弟会话前端 WIP 预存，本次 diff 不含任何前端文件）；ruff 绿。清理后 50 食谱逐一对探针复核 0 未证实。
- **生效条件：** 纯 Python 表 + 静态 JSON——需重启 worker 生效；无前端改动。
- **运行时重置（主人已拍板，同日完成）：** server_config.json 备份为 `.bak-20260904-meituan-cleanup` 后四处清理——① sankuai 供应商 models 454→50（模板食谱形状 + aliases/enabled 运行时默认，probe 留下的 key_access disabled 态一并归零，下次探测全新推导）；② 旧版 `model_catalog` 选择器目录同步剔除：sankuai offerings 404 个死证全删、routes 悬空即删、models 无 offering 即删（460/467/460→58/63/58，oauth_codex/deepseek builtin 不动，gpt-5.4/5.5 等共享 id 靠 codex offering 续存）；③ presets.gemini 死拼写 gemini-3.1-flash-lite-preview→GA gemini-3.1-flash-lite；④ model_limits 删 2 条 v3.2 镜像残留。catalog revision 与 provider _catalog_revision 同步 5→6。复核：50 模型全部 wire id 探针在售 0 例外，另两个供应商未动。
- **未动（已向主人报告）：** `models` 段的 IMAGE_GEN_MODEL=gemini-3.1-flash-image-preview 与 EMBEDDING_MODELS=text-embedding-*——美团端点三个模态全探死，配置内无任何替代后端，属功能级默认而非测试面，留待主人决定。

### 2026-09-04 (preset 选择器 v2 行图标根修：Creator id 被当 glyph key 直渲，除 openai 巧合命中外全落灰盒)

- **起因（用户报告，截图）：** 模型选择器里 GPT-5.6 Sol 有真 OpenAI logo，但 Qwen/Claude 各行全是灰色通用图标——而设置页同一家来源的分类都显示正确品牌标。
- **根因（命名空间混淆）：** preset `_row`（main_toolbar_ui.js）对 `m.brand` 非空且非 oauth/adapter 的行把这个字符串**逐字**当 `MODEL_BRAND_ICONS` 的 glyph key 用。legacy 行的 brand 来自 provider 模板、本来就是 glyph key（'openai'/'qwen'），没问题；但 v2 confirmed 行在 `_modelRoutingDropdownModels` 里塞的是 `model.creator_id`（'anthropic'/'alibaba'）——Creator 命名空间，icons 表里没有这些键，全部落 generic。'openai' 恰好两个命名空间同名，所以截图里只有它有真 logo。设置页（provider_render.js 模型选择器/目录行）一直走共享 `_modelBrand(model_id, creator_id)` 解析（CREATOR_TO_BRAND：anthropic→claude、alibaba→qwen），所以那边正确。
- **改动（main_toolbar_ui.js `_row`）：** v2 confirmed 行（`routing_v2 && !pending_identity`）改走 `_modelBrand(m.model_id, m.creator_id)`——与所有 Settings 模型界面同一个解析接口；pending 行保持 provider.brand 原样（其 brand 本来就是 glyph key），legacy/oauth/adapter 路径不动（与分组表头一致性契约不变）。
- **规格（test_model_group_convergence.py 新函 6 针）：** 真实 `createModelBrandResolver` native 模块 + 四 offering 合成 v2 文档（anthropic/alibaba/openai confirmed + openrouter pending）——anthropic 行出 claude、alibaba 行出 qwen、openai 行保持 openai、pending 行保持 openrouter、Creator 命名空间永不直渲。
- **验证：** 本文件 2 passed；邻近（model_brand_resolver/agent_mode/chat_mode_parity/project_bar_binding/turn_serving_route/proxy_background_budget）45 passed；check:frontend 全绿（compose + 契约 + i18n + tsc + Vite build + canary，117 assets）；ruff 绿。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。
### 2026-09-04 (自动科研断线续接：进入科研模式经 tasks?kind=research 重挂在跑任务，不再落回初始页)

- **起因（用户报告）：** 输入题目后显示「爬取文档」，返回对话再回来又落初始首页——科研态只在内存 `_researchStream`，`exitResearchMode` 调 `_destroyResearchRuntime` 拆干净后无任何恢复路径，而服务端 job 明明还在跑。
- **根因：** `enterResearchMode` 只查内存流，注释自称「server-side job remains resumable」但 resume 路径从未实现；`GET /api/v1/tasks?kind=research&status=running`（owner 域，meta 带 direction/lang）一直具备续接所需的全部信息。
- **改动（纯前端）：** research-runtime.ts 新增 `resumeInflightResearch`（list running→pending 取最新 → get 快照校验未蒸发 → `researchApplySnapshot` 重放轨迹 → 在跑则重挂 push+轮询，已完结则投影终态并 hydrate 持久档案；list/get 间隙被 TTL 清扫则置空流返回 false 不留半恢复态）；push 挂载逻辑从 startResearchJob 抽为 `attachStreamPush` 两处复用。research-session.ts enter 无本地流时先同步落 landing 再异步续接（沿用「chrome 同步、hydrate 异步」惯例），命中即重绘控制台；页面刷新后点进科研同样生效。
- **规格（tests/research/test_research_resume.py，2 针）：** jsdom 三场景——在跑任务续接（方向/taskId/phase/toolRounds 重放 + push 订阅 [research, taskId]）、离开期间完结（不持订阅、终态投影 + lookup 持久档案）、无在跑（landing 不动、流保持空、list 恰好 running+pending 两问）；源码针钉死 enter 必须调 `_resumeInflightResearch`（防回归落回 landing）。harness 注意：模块运行时状态写在 featureRegistry overrides 而非 globalThis，须经 `TofuNativeTest_*` iife 全局取 registry 读流。
- **验证：** 新 2 针 passed；agent_reuse+lookup_route 16 passed；typecheck:modules 绿；Vite 构建 117 资产、resume 代码确认入 panel-owners chunk；ruff 绿。check:frontend 首门 compose --check 红系兄弟 WIP 改了 runtime sections 未重组（api.js/provider_render.js 等，本 diff 未触碰任何 section），不代为重组。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (模型管理行二轮改版：删「模型商/模型名」子行、别名→请求名并放大、行布局通栏化、上下文 K/M 缩写)

- **起因（用户报告，截图）：** ① 模型名下方的 `anthropic/claude-sonnet-5` 子行与粗体 ID 冗余；② 「别名」命名不直观，且区域在行内占比太小；③ 三列网格（identity | detail | actions）信息密度组织混乱；④ 上下文裸数字（1000000/128000）应按惯例用 K/M。
- **行结构（provider_render.js）：** 删除 `.stg-v2-model-identity` 列与子行——行改为「信息区通栏 + 右侧操作列」两列网格，信息区自上而下：模型名 + 状态芯片 → 能力/上下文/发布 → 价格 → 请求名。待确认行的说明从子行移到「待确认」芯片的 title（`该模型 ID 未匹配到模型目录，仅此服务商内可用`），不再占一行。
- **别名→请求名（纯用户可见文案，标识符 `_addV2Alias`/`stg-alias-chip` 不动）：** 搜索占位、行内标签、添加按钮、showPrompt 标题与正文、重复提示、toast、删除 title、服务商卡片摘要共 8 处。
- **请求名放大（06-model-routing-v2.css）：** `.stg-v2-model-aliases` 10px→11px + gap 布局；manager 内 alias 芯片 padding 1px 6px→2px 8px（服务商卡片的紧凑芯片不动）。
- **上下文 K/M：** 新增 `_modelRoutingContextLabel`——整除优先、否则一位小数（1000000→1M、128000→128K、8192→8.2K、8000→8K、16000→16K、500→500）。
- **CSS 清理：** `.stg-v2-model-row` 网格 `minmax(170px,1fr) minmax(0,1.8fr) auto`→`minmax(0,1fr) auto`；停用暗化目标从 identity+detail 并为 detail；retired 删除线选择器改 `.stg-v2-model-idline strong`；共享选择器中的 `.stg-v2-model-identity` 全部摘除（目录 tab 的 `.stg-v2-catalog-identity` 不动），idline strong 补上省略号规则。
- **规格（test_provider_model_add.py）：** 子行消失针（`stg-v2-model-identity`/`meta/esm` 不在行 HTML）、待确认 title 针、K/M 针（行内 1K/500/8 + 助手五例精确数组）、请求名针（`+ 请求名` 有 / `+ 别名` 无 / 源码文案针）、CSS 两列网格针；文件本轮新增未跟踪，顺手 `ruff format` 规整。
- **验证：** provider_model_add+panels_parity+local_deploy 30 passed；check:frontend 全绿（compose 163 sections/31 styles + Vite build，117 assets）；ruff 绿。playwright 截图复核：通栏行、请求名芯片醒目、上下文 1M/8.2K/128K 正确——注意本机无 libatk，截图需 `LD_LIBRARY_PATH=$HOME/tofu-browser-libs/lib` + `~/.cache/ms-playwright/chromium_headless_shell-1234`。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (Gemini 3.8 Flash 入表：六处知识表 + 三模板刷新，3.6-flash 出 picker)

- **起因（用户报告，截图）：** Gemini 已发布 3.8 Flash，「从模板添加」picker 里 Google 模板还停在 3.7 代际。
- **知识表（新模型落地标准六处中的五处，gemini 无 aliases/oauth）：**
  - `lib/pricing/_tables.py`：+`gemini-3.8-flash` $1.50/$7.50（沿袭 3.7 标准价档），Gemini 家族缓存乘数 1.00/0.25；严格小于边界内推导为 cheap。
  - `lib/llm_dispatch/config/_slots.py`：+槽位（caps text/vision/thinking/cheap，marketplace 默认 20 RPM，cost 0.005，与 3.6/3.7 同形）。
  - `lib/model_info/_context.py`：+`(gemini-3.8-flash, 1M, vendor_official)`；未知护栏从 3.5–3.7 扩到 3.5–3.8（已验证 flash 变体仍走精确规则，gemini-3.8-pro 等未验证变体继续拒猜）。
  - `lib/model_info/_release.py`：+`('gemini-3-8-flash', '2026-09')`。
- **模板：** builtin gemini 模板 3.8-flash 入、3.6-flash 出（仍 6 模型；3.7 保留）——3.6 比 3.7 贵且旧两代，picker 默认单不再推；openrouter +`google/gemini-3.8-flash`（5→6）；meituan.json +同 id 行（62→63，沿袭「每月 marketplace 批次」惯例）。3.6-flash 仍留在 slots/pricing/context/release 知识表——存量安装与发现富化不受影响。
- **联动钉：** title_gen 排除集 -gemini-3.6-flash +gemini-3.8-flash +google/gemini-3.8-flash；pricing_tier_sync allowlist +openrouter 3.8（前缀 id 无价，沿袭 3.7 例）；context 契约测试 +3.8 精确窗口钉与 gemini-3.8-pro 未知钉；release 测试 +2026-09 钉。
- **验证：** 窄六套（release/context/pricing_tier_sync/title_gen/meituan/recipes）58 passed；相邻六套（bootstrap_catalog/provider_model_add/model_folds/pricing_dated_folding/pricing_refresh/discovered_provider）79 passed；ruff 绿。编译级核对：gemini 6 / openrouter 6 / meituan 63，3.8-flash 均在内。
- **生效条件：** 纯 Python 表 + 静态 JSON——需重启 worker 生效；无前端改动。截图里只有 12 家是因为上次 16 家扩容也未重启，重启后 Anthropic/MiniMax/Mistral 一并出现。

### 2026-09-05 (中段检查点段落倒序根修：活体尾批窗口内累积散文前置到工具卡前 + parse 后镜像即时刷新，消除「叙述消失」窗)

- **起因（用户报告，三张截图）：** 一轮 3 工具调用中——① 思考/内容渲染在工具行下方（顺序反）；② 等到部分工具结果后内容直接消失；③ 全部工具结束后渲染才正确。
- **根因（同一条静态镜像缝，两处症状）：** 工具卡早通告（工具轮状态 `executing`）时散文印章尚未落盘（它在 dispatch-parse 时才盖到批次首轮 `assistantContent` 上），此窗口内叙述仍在 `task['content']`/`task['thinking']` 累积器里。① 倒序——配额授予后的结构性写触发 `checkpoint_task_partial` → `assemble_segments` 把累积器散文当 terminal 段追加到尾（工具卡之后），sync 上屏即反。② 消失——进入 dispatch 时 prelude 的 `_discard_pretool_prose` 清零累积器（DELTA_RESET 同步把前端散文抹空），而印章落在内存轮条目上、静态镜像仍是 parse 前的旧装配（terminal 散文已被 reset 抹掉），直到 round_end 检查点重装配才恢复——中间整段窗口叙述不可见。
- **修复一（segments/_assemble.py，倒序）：** 装配循环跟踪尾随批次（首个工具段对象 / 是否活体 / 是否已盖章）；terminal 散文先建成 `terminal_segments` 列表，仅当「尾随批次活体（`LIVE_ROUND_STATUSES`）且未盖章且累积器非空」时插到该批次首个工具卡前——与 wire 序及活体修复 `_append_missing_live_tool_segments` 同形。已完成的尾随批（prefetch 类，从不盖章）保持尾部落位（散文是即将到来的 deliverable，旧行为不动）。被移动的段保留 `terminal`/`deliverable` 旗标——所有 wire 投影按旗标而非位置排除 terminal 段（`_rounds_view_from_segments` pre-scan 跳过），wire 纯净性不变。
- **修复二（tool_dispatch/_parse.py，消失窗）：** `parse_tool_calls` 循环末尾、accumulator 已空（discard 已跑）时惰性 `assemble_segments(task)` 刷新 `task['segments']`——此时印章已落批次首轮，镜像立刻以「散文在工具卡前」的正确形落盘，不再等 round_end；accumulator 仍有散文（防御态，discard 未跑）则跳过，活体 terminal 块继续拥有叙述。
- **词汇单一来源（segments/_types.py）：** 新增 `LIVE_ROUND_STATUSES` frozenset（searching/executing/pending_approval/awaiting_human/awaiting_stdin），`manager/_sync._has_inflight_round` 的本地集合切换引用。
- **规格（test_segment_model.py，7 针）：** 活体未盖章尾批拉前累积散文（thinking+text 序 + 旗标保持）、移动后 wire 视图纯净（无 assistantContent）、已完成尾批保持尾部、已盖章活体批不重复/不重排（防御）、无累积散文不变；parse 刷新后叙述先于工具卡且无 terminal 残留、accumulator 非空时镜像不动（identity 针）。
- **验证：** 新 7 针 passed；segment_model/turn_projection/edit_realigns/prelude+round wire parity/dedup_cache/repair_patch 151 passed；checkpoint/inflight/parity 邻居 65 中 test_interrupted_turn_metadata 一针在批量跑时挂——stash 我的 5 文件后 HEAD 同批量复现同挂，单跑/单针均绿，证实为预存跨文件日志捕获干扰（兄弟 _turn_prelude/_budget WIP 期），与本次 diff 无因果；ruff 绿。docs/modules/task_engine.md Task carrier 段补窗口排序契约。
- **生效条件：** 纯 Python 改动——需重启 worker 生效；无前端改动。

### 2026-09-04 (tofu-db 差分簿收敛 8→4：record.list 通配 + search 三项全走提案路径①修 legacy，两份提案结项)

- **起因：** KNOWN_DIVERGENCES 剩 8 条，其中 5 条「待人拍板」的提案两份 docs 均推荐路径①（修 legacy）且消费者影响已核实为零/纯增量；主人指令「全速推进上线」即按推荐路径落地。另外 3 条 tofu 缺口（sync.changes 信封/migration_receipt/secret.get）已被兄弟提交关闭。
- **改动（commit 60c6ebff2 + 被兄弟 2184d726f 席卷的测试侧）：**
  - `record.list`（_records.py）：`_record_list` 转义 LIKE 元字符并声明 `ESCAPE '\'`——prefix 双权威均为字面字节前缀（后端中立 SQL，无已知通配消费者）。
  - `conversation.search`（_conversations.py）：`_conversation_search_op` 重写——命中选择从 SQLite `lower()/LIKE`（仅 ASCII 折叠）改为 Python `str.lower`（全 Unicode，与 Rust `to_lowercase` 精确同构）按排名序扫会话（50/块、命中即停、每阶段 5000 会话扫描上限）；快照窗口起点重钳（`start=max(0,pos-radius)` 后从 start 取全宽）；窗口宽度按实际命中词（多词 AND 兜底时是 words[0] 而非整查询）。提案消费者核查：仓内唯一生产调用方 conv_ref 只读 hit id 不读 snippet，①②零影响、③纯增量召回。
  - 差分脚本摘除 4 个 `expect_divergence` 标记 + KNOWN_DIVERGENCES 删 4 条（棘轮强制）；探针保留转断收敛（通配键对/起点钳位/多词宽度/CAFÉ-café 四场景）；case-folding 脚本改名 `search case folding ascii and unicode converge`。两份提案 docs Status 改 DECIDED。
- **剩余簿（4 条）：** 2 条固有后端格式差异（project brain cutover 状态、system.reclaim 物理指标，永久 rationale）；2 条真正需要人语义裁决（append_settled 重复 identity 错误类 database_integrity vs database_conflict；turn.list 跨 lane 平局序契约未定）——非 agent 可决，留给主人。
- **验证：** legacy 搜索单测 38 passed；差分 record/search 子集绿；HEAD 全量（兄弟「原子 Turn 对创建」落地后 109 脚本）106 passed / 3 failed——3 挂全是 tool_result_artifact.*，系兄弟未提交的 _artifacts.py 删 expiresAtMs 回显所致（其 Rust 侧仍回显，其收尾后自愈）；ruff 绿。
- **协作事件：** 兄弟 2184d726f 整文件提交把我的差分测试未暂存改动席卷进其提交（内容无损，归属混入）；剩余 4 文件单独成 60c6ebff2。并发期部分暂存（patch 拆 hunk + `git apply --cached --recount -C1`）要防 hunk 漂移——兄弟活跃编辑时旧补丁秒级失效。

### 2026-09-04 (服务商模板扩容 + 列表换血：Anthropic 原生协议模板入市，新增 MiniMax/Mistral，五家 builtin 刷新到目录代际)

- **起因（用户报告，截图）：** 设置页「从模板添加」只有 12 家，且模型列表过时——Qwen 还停在 qwen-plus/qwen-flash，OpenRouter 还是 gpt-5.4/deepseek-chat，官方 Anthropic 根本不在 picker 里。
- **Anthropic 入市（static/provider_templates/anthropic.json，新）：** builtin 里一直有个 anthropic stub，但无 `protocol` 字段、会被 `load_provider_templates` 显式过滤（否则会编译出 OpenAI 形状的假连接去打 Messages API），因此 Settings picker 从来看不到 Anthropic。新静态配方带 `protocol: 'anthropic'`（migration 一等协议），按 key 覆盖 builtin stub——bootstrap 与 Settings 两个 picker 同时获得。8 模型对齐 OAuth 官方清单顺序：claude-opus-5 / sonnet-5 / fable-5-1 / fable-5 / opus-4-8 / opus-4-7 / sonnet-4-6 / haiku-4-5；全部 MODEL_PRICING 有价，haiku 唯一 cheap（$1/$5），sonnet $3/$15 卡在严格小于边界外不标 cheap。`thinking_format` 字段验证后删除——v2 连接不携带该键，protocol 已隐含。
- **MiniMax / Mistral 入市（minimax.json / mistral.json，新）：** 定价与 slots 表早已覆盖这两家（MiniMax-M3/M2.7/M2.5、mistral-large/small/codestral），模板层补齐官方入口；全部模型按定价推导为 cheap 并如实标注。
- **builtin 换血（bootstrap_pkg/providers.py）：** qwen 3→5（qwen3-max/qwen3.6-plus/qwen3.5-flash/qwen3-coder-plus/qwen3-vl-plus，替下 qwen-plus/qwen-flash）；gemini 5→6（+gemini-3.6-flash，lite-preview 换 GA 拼写 gemini-3.1-flash-lite）；doubao +seed-2-0-mini-260215；bedrock +us.anthropic.fable-5-1-v1:0 / claude-opus-4-8-v1:0；openrouter 换 anthropic/claude-opus-5、openai/gpt-5.6、deepseek/deepseek-v4-pro（替 claude-sonnet-4.6/gpt-5.4/deepseek-chat）。全部新条目优先选 MODEL_PRICING 有价的拼写，cheap 标签与定价括号推导逐一对齐。
- **联动钉：** title_gen 排除集同步派生（+qwen 四新/+gemini-3.6-flash/+deepseek/deepseek-v4-pro，-qwen-plus）；pricing_tier_sync allowlist（-deepseek-chat，+deepseek-v4-pro，+doubao mini）；test_provider_template_recipes 的 anthropic 针从「不在 picker」翻为「静态配方 + 原生 protocol + 首推 claude-opus-5」。
- **验证：** 配方/价格分层/title_gen/bootstrap 目录 37 passed；相邻 17 套 213 passed——5 个失败均为并行会话预存（provider_templates.js 前端段文件不存在，HEAD 测试同样引用；sku 针扫描的 app-runtime.js 在 HEAD 即含 'gpt-5.6-pro' 折叠映射字面量），与本次 diff 无因果。ruff 绿。
- **生效条件：** 纯 Python 表 + 静态 JSON——需重启 worker 生效；无前端改动。
- **picker 现状（16 家）：** Anthropic 8 / Bedrock 5 / DeepSeek 2 / Doubao 3 / GLM 7 / Gemini 6 / Meituan 62 / MiniMax 4 / Mistral 3 / Moonshot 4 / OpenAI 15 / Qwen 5 / Tencent 20 / xAI 2 / OpenRouter 5。

### 2026-09-04 (tofu-db 差分门 HEAD 红根修：turn.recover 计数被跨脚本遗留污染——脚本头泄洪 + 自身时间钉)

- **起因（门红）：** 全套件 106/107，独挂「turn recovery is chunked guarded and replay visible」——`turn.recover` 首轮返回 recoveredAttempts 在 tofu-db 侧为 0、legacy 侧多出 3 个外来 attempt；单跑该脚本却绿。
- **根因：** legacy 权威是模块级、被同 pytest 进程全部脚本共享，而 tofu-db 守护进程每脚本全新——unscoped `turn.recover` 把前面脚本遗留的可恢复 attempt 一并捞回，计数取决于全套件执行历史，双权威天然不等。属 harness 脚本设计缺陷，非 tofu-db 产品 bug。
- **修法（tests/tofudb/test_tofudb_differential.py，commit 738ffab52）：** 恢复脚本头部插 3 个 `compare=False` 的默认 chunk `turn.recover` 泄洪外来遗留（其属主脚本均已完结），后续 `created_before_ms` 钉便只覆盖本脚本自己的 attempt；`_Ref` 下标随 3 个新步骤平移。三套默认 chunk 远大于当前遗留总量，兄弟新增的 dispatch_worker 类脚本再留几个遗留也兜得住。
- **验证：** 干净 worktree（HEAD）全套件 107/107 绿（42s）；主仓（含兄弟 WIP）用 `TOFUDB_DIFFERENTIAL_BIN` 指外部 HEAD 二进制单跑恢复双脚本绿；ruff/语法绿。部分暂存提交——同文件兄弟未提交的 dispatch_worker 脚本留在工作区未动。
- **教训（已入记忆 tofudb-semantic-differential-harness）：** 给差分 harness 加任何做 unscoped 计数断言的脚本前，先想「前面脚本会不会留下可被本脚本捞到的状态」；会产生可恢复遗留的新脚本落地后，后续计数类脚本需同款泄洪。

### 2026-09-04 (模型管理面板重设计：状态芯片取代删除线 / 筛选 chips + 启用优先 / 紧凑开关 / 垃圾桶图标 / 入出价签)

- **起因（用户报告，截图）：** ① 行内开关 52×30 巨大——根因是设置页 hero 规格 `.stg-toggle` 基准几何被密集行原样复用，无语义；② 停用供给用删除线（语义=已删除）；③ 行尾 ✕ 与弹窗关闭 × 同形；④ 价格 `USD 2 / 10` 不分输入输出；⑤ 供给多但只有搜索框且字母序把停用行排在可用行前面；⑥「待确认身份，仅限当前服务商」内部行话；⑦ 能力芯片英文大写与全中文界面不一致。
- **改动（provider_render.js）：** `_stgProviderManagerFilter`（all/enabled/disabled/pending）+ 工具栏下方 `_stgManagerFilterChip` 计数 chips（`_setProviderManagerFilter` 只刷新 chips 选中态与行列表，不重建工具栏避免抢搜索框焦点）；行排序启用优先、组内字母序；停用改为信息区降透明度 + 「已停用」芯片，删除线只留给目录 `lifecycle:'retired'` 模型（context 新增 `modelLifecycles` 映射，行加 `is-retired` + 「已下线」芯片）；待确认行加「待确认」芯片 + 子行改「未匹配到目录模型 · 仅此服务商可用」；`_modelRoutingPriceHtml` 结构化价格（货币 + 入/出金色小标 + 「目录价」芯片后缀，title 说明输入/输出价）；能力芯片经 `_MODEL_CAP_LABELS` 译中文（原始 cap 名留 title）；移除按钮 ✕ 改羽毛垃圾桶 SVG + `aria-label="移除该模型供给"`（确认弹窗 `_removeV2Offering` 本就有）。
- **样式（06-model-routing-v2.css）：** `.stg-v2-model-row .stg-toggle` 变量覆盖到 36×21（密钥行同款紧凑几何，基准组件不动）；`.is-disabled` 整行 opacity 拆为只暗化 identity/detail（操作区保持可用）；新增 `.stg-v2-state-chip`（off/pending/retired 三色，双类选择器压过 `.stg-v2-model-identity span` 共享规则的 monospace 10px）、`.stg-v2-price-*`、`.stg-v2-filter-chip`（active 金边）、`.stg-v2-remove`。
- **命名避让：** `stg-v2-manager-tabs` / `_stgProviderManagerTab` 是 test_turn_serving_route 钉死的已移除标识符，新状态命名 `_stgProviderManagerFilter` / `stg-v2-filters` 不撞针。
- **规格（test_provider_model_add.py）：** 价格针改新格式（入/出片段 + 目录价芯片计数 1 + 未设置价格兜底）；新增 `test_manager_rows_state_chips_sort_filter_and_icons`（harness 注入 retired 模型/停用确认行/待确认行——启用优先序、三态芯片、删除线只在 retired、人话子行、中文能力芯片、垃圾桶图标无 ✕、disabled/pending 筛选结果）与 `test_manager_toolbar_exposes_status_filters_and_compact_toggle`（源码 + 样式源码针：紧凑几何变量、旧整行 opacity 与停用删除线规则不复存在）。
- **验证：** provider_model_add+panels_parity+local_deploy 30 passed，turn_serving_route 4 passed；check:frontend 全绿（compose 163 sections/31 styles + 契约 + i18n + tsc + Vite build + canary，117 assets）；ruff 绿。playwright + `~/tofu-browser-libs` 独立页渲染 9 供给合成数据截图核对：启用行在前、待确认/已停用/已下线芯片与价格结构正确——注意独立页需 `<html data-theme="light">`，否则 `:root` 默认深色令 `--text-primary` 近白、strong 文字不可见。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。
- **遗留：** 服务商卡片与模型目录 tab 的能力芯片仍是英文原始名（本次只统一模型管理弹层）；如需全局中文化是另一条独立改动。

### 2026-09-04 (Pareto 图前沿标签重设计：标注芯片 + 实测字宽 + 错层避让红线/标记 + 引线)

- **起因（用户报告，截图）：** 成本 × AA Index 图右上前沿簇（kimi-k3 / claude-opus-5 / GPT-5.6-Sol）标签连成一串「kimi-k3claude-opus-5」，GPT 标签直接压红线。旧摆放是 5.2px/字符估算宽度 + 1px 内边距 + 只避让其他标签（不避让前沿折线和标记），估算偏小即视觉粘连。
- **改动（pareto-chart.ts）：** 导出纯函数 `layoutFrontierLabels`——芯片（奶油底圆角 rect + 居中文字）按 x 序贪心分配点上方行层（行距 17px，最多 8 层，兜底钳在图内），碰撞检测同时覆盖：已放芯片（横向 10px 间距）、前沿折线线段（精确的线段-矩形相交）、全部标记（圆-矩形）；宽度改用 canvas `measureText` 实测（与 CSS 同字体规格），测量不可用回退原估算。被顶高或边缘钳位移位的芯片画细引线回指标记。关键依据：前沿折线上方区域按定义无标记（否则该点支配前沿），向上错层永远安全。
- **样式（model-catalog.css）：** 新增 `.stg-mc-pareto-callout-chip/-leader`；`.stg-mc-pareto-point-label` 去掉白色描边光晕（芯片即背景）；callout 组 pointer-events:none 不挡标记 hover。
- **规格（test_frontend_pareto_chart.py，新）：** node harness 钉截图回归场景——芯片两两不触（8px 横向间距）、不压红线、不碰标记、拥挤对纵向分离、位移芯片带 leader 旗标、确定性、输入序对齐、10 点同位堆叠兜底仍在图内。
- **验证：** 新套件 1 passed；check:frontend 全绿（compose 31 styles + 契约 + i18n + tsc + Vite build + canary，117 assets）。playwright + chromium_env 独立页渲染 43 模型合成数据截图核对：右上簇 kimi 行 0 / claude 抬一层带引线 / GPT 再抬一层，左缘 DeepSeek 芯片钳位带引线，页面实测芯片 rect 零重叠。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (Fable 5.1 入表 + DeepSeek V4 Pro 0813 无价根修：定价查找日期后缀折叠缝，杜绝「新 checkpoint 手工补行」)

- **起因（用户报告）：** Fable 5.1（GA 2026-09-01）发布了配置里却没有；DeepSeek V4 Pro 0813 一直没有价格。用户裁定：模型配置自动更新机制要优化，不能每次靠手工补五张表。
- **根因（两层）：** ①新旗舰——fable-5.1 需要同时落 MODEL_PRICING/slots/aliases/release/bootstrap/oauth 六处静态表，此前没人手工加就不存在；在线刷新 `_fetch_model_pricing_online` 只给当前活跃模型定价，不管目录。②日期 pin——`deepseek-v4-pro-0813`（GA 2026-08-12，滚动别名 deepseek-v4-pro 背后的同一 checkpoint）是精确键查表，表里只有滚动别名 → 计费/价格分层/发现富化全部按零计。
- **机制修复（lib/pricing/_tables.py `pricing_for_model`）：** 新查找缝——精确命中优先，未命中折叠一个尾部 `-MMDD`/`-YYYYMMDD` 日期戳重试滚动别名的行。四处原直接 `MODEL_PRICING.get(model_id)` 消费点全部切换（_provider.lookup_pricing、config/_pricing._resolve_prices、discovery/_discover、discovery/_capabilities）；`build_rate_card` 导出表自身键，豁免。非日期后缀（-tencent/-huawei 云镜像）不折叠；LongCat -2601 类迭代戳折叠到无行 id 仍是 miss——不会给错行。deepseek-v4-pro-0813 因此无需自己的价格行即获 $0.435/$0.87（含 peak 日程）。docs/modules/auth_providers_billing.md 定价段记录该缝契约。
- **Fable 5.1 数据（六处）：** MODEL_PRICING 四 id（$10/$50，cacheReadMul 0.025=读价降 75%）；_slots 四 id（cost 0.030 ≈ 2x Fable 5）；_aliases 新互替组；_release 针用 `fable-5-1`（裸/aws/Bedrock/claude- 四拼写全覆盖，置于 claude-fable-5 前）；bootstrap_pkg anthropic 模板与 oauth/outbound claude 列表各加一首位。1M ctx 与 128K max_output 靠既有 family 规则自动命中（fable≥(5,0)、is_claude），compaction/discovery/brand 正则不动的已覆盖。
- **DeepSeek 0813 元数据：** 入 V4 Pro 别名组（与滚动别名/腾讯镜像互替）、slots 行（同 pro）、_release `2026-08-12`（置于 pro 规则前）。
- **规格（tests/pricing/test_pricing_dated_folding.py，20 针）：** 折叠（0813/0731/8 位 Claude 快照）、精确优先、非日期后缀不折叠、迭代戳不错配、lookup_pricing 集成+返回拷贝、消费点不绕缝源码钉；Fable 5.1 价格/slot/别名/发布日期全拼写；0813 别名组/slot/日期序。test_claude_line_version.py 参数化补 fable-5.1 三拼写解析针。
- **验证：** 新套件 20 passed；claude_line_version/pricing_tier_sync/pricing_refresh_lifecycle/audio_chat 96 passed；相邻 bootstrap_model_catalog/capability_taxonomy_parity/context_profile_contract/billing_context_tiers/dispatcher_model_routing_v2/oauth 双契约/context_limits_namespace_fold 62 passed；ruff 绿。
- **生效条件：** 纯 Python 表与查找缝改动——需重启 worker 生效；无前端改动。
- **遗留：** 「全新模型发现」仍无自动化——在线刷新只服务当前活跃模型的设置页展示，不把 OpenRouter 全量目录合入 MODEL_PRICING/slots；如需目录级自动补价是另一条独立改动（涉及调度成本权威表的运行时变更策略）。

### 2026-09-04 (本地部署类型选择面板重设计：分组卡片 + 图标贴 + hover 金边雪佛龙，不影响服务商模板下拉)

- **起因（用户报告，截图）：** 「选择本地部署类型」二级面板（设置 → 服务商 → 本地部署）观感差——六个选项挤成无分组的扁平行，图标与文字排版松散。
- **改动（local_deploy.js）：** 预设选择器弹层新增作用域类 `stg-local-deploy`；六个预设拆为两组渲染——「推理引擎」（vLLM/SGLang/Ollama/llama.cpp，`data-group="engines"`）与「托管与自定义」（模型路径托管/自定义，`data-group="other"`，保持 owner-ratified 顺序：managed 在 custom 前、custom 最后，分组只是视觉层）。瓦片抽出 `_localDeployPresetTile`：图标贴（40px 圆角奶油底方块）+ 名称/描述（描述允许换行，原 nowrap 长文案被切）+ 右侧 hover 才显现的雪佛龙箭头。
- **样式（06-model-routing-v2.css）：** 全部规则以 `.stg-local-deploy` 前缀作用域化（0-3-0 优先级压过 `.stg-template-item` 基础紧凑样式，与样式表加载顺序无关），服务商模板下拉菜单（provider_render.js 共用 stg-template-grid/item）零影响。卡片：白底 + `--s-line-strong` 边 + 12px 圆角 + xs 阴影；hover 金边 + 金色光晕 + 上移 1px + 图标贴变金色底；`:focus-visible` 金环；组标签 10px 大写弱化。
- **i18n：** 新增 `settings.localPresetGroupEngines` / `settings.localPresetGroupOther`（zh/en），generate:i18n 再生成（4748 keys）。
- **规格（test_local_deploy_settings.py）：** 新增一针 `test_preset_chooser_renders_grouped_card_tiles`（作用域类/瓦片结构/双分组/i18n 键源码钉）；locale 针的 required 集合补两个组标签键。
- **验证：** local_deploy+provider_model_add+panels_parity 28 passed；相邻 brand_resolver/capability_taxonomy_parity 31 passed；check:frontend 全绿（compose 163 sections/31 styles + 契约 + i18n + tsc + Vite build + canary，117 assets）；ruff 绿。playwright + `~/tofu-browser-libs`（LD_LIBRARY_PATH，本机缺 libatk/X 库）独立 harness 渲染真实静态样式表截图核对常态与 hover 态。
- **生效条件：** 纯前端改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (工具失败双重展示根修二号：错误行接管完整 toolContent（不截断可换行），timeline failed 卡片按轮剔除)

- **起因（用户报告，截图）：** `mcp__llm__experiment_stop` 失败同一渲染两处——上面工具行（徽标「失败」+ 首行 300 字符截断的 MCP Error，恰好截在 `"target": "{…`）与下面 activity 卡片（「执行失败」+ log 策略截断 628 字符的 detail + reasonCode）。与昨日拦截行同一模式，用户裁定照修；硬刷新无效是因为上次只修了 `rejected` 路径。
- **截断链（两层不同机制）：** 工具行 `_renderErrorRow` 是 JS 层 `.split("\n")[0]` + `slice(0,300)+"…"` + CSS `-webkit-line-clamp: 4`；activity 卡片 detail 是后端 `_diagnostic_text`（`_DETAIL_MAX_CHARS=400`）经 `log_redaction.bound_text` 插入 `… <log policy omitted N chars> …`——数据层截断，卡片永远拿不到全文，全文只在 `round.toolContent`。
- **修复一（去重，conversation-view-model.ts）：** `visibleActivityEntries` 扩展——收集 `status==='error'` 工具轮的 `toolCallId` 集合，命中集合的 `activity.tool.failed` 条目不再上屏；轮被修剪/缺失时条目保留（孤儿兜底）。`execute_tools` 网关行豁免——它走 `legacyGatewayValidationEntries` 重放为子校验行，不能被当成重复剔除。后端 timeline 契约不动。
- **修复二（完整显示，tool_rounds.js + 09-tool-terminal-details.css）：** `_renderErrorRow` 删掉首行+300 字符截断，全量渲染 `round.toolContent`（`\n`→`<br>`），tooltip 保留首行；CSS 对 `.ptool-line.ptool-error .ptool-error-reason` 解除 4 行 clamp（与 rejected 共用同一条规则）。
- **规格：** surface vite 新增 `test_activity_tool_failure_row_yields_to_the_inline_tool_error`（命中轮剔除 + 孤儿 failed 行保留 + 网关行豁免三断言）；wire-parity 电池新增 `error-failed-tool-long-multiline` 轮（~600 字符多行 MCP Error，钉死全文 + `<br>` 换行），baseline 重生成（61 轮，既有 60 轮输出零漂移）。
- **验证：** wire-parity 2 passed；surface 套件 69 passed；check:styles（31 sections）+ check:frontend 全绿（compose + tsc + Vite build + canary，117 assets，新 hash）。docs/modules/task_engine.md 失败规则段补失败行去重契约。
- **生效条件：** 纯前端展示层改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (自动科研 429 致死根修：discovery 翻译按 agent 策略等窗口退避 + tofu 自带搜索兜底 + 中断运行不再冒充「诚实零」)

- **起因（用户报告，截图「本次运行未能完成」）：** 自动科研又失败——discovery 翻译（中文方向→英文检索词）遭 429 预算耗尽即死，原始中文方向原样喂给只索引英文元数据的 arXiv vertical（构造上零命中）→ 语料 0 篇 → 闸门无候选 → 前端空面板显示「所有候选项均被闸门淘汰……这是诚实的结果，不是故障」——把流水线故障冒充成闸门评审意见。用户裁定：① 不要一遇 429 就失败，要稳定；② tofu 自带搜索为什么不用；③ 翻译失败逻辑应对齐 tofu-agent 会话轮的做法（等窗口退避重试）；④ 其余照修。
- **根因链：** UI 向翻译故意 fail-fast（TOFU_TRANSLATE_MAX_429_ATTEMPTS 4–8，见前条 conv-affinity 修复）——后台生产阶段照搬了这套策略是错的，没有人盯着转圈，429 窗口（分钟级）相对于整段流水线预算是可等的。
- **修复一（lib/research/recipe.py，429 韧性）：** `_discover_search_query` 包装——`DispatchRateLimitBudgetExceeded` 不再致死，按 tofu-agent 会话轮同一张退避表（`turn_retry.auto_turn_backoff_seconds`，3s→6s→12s→24s→30s 封顶 + jitter）睡眠等窗口重试，直到阶段期限 `TOFU_RESEARCH_DISCOVERY_TRANSLATE_DEADLINE_S`（默认 600s，clamp 30–3600）；`_discovery_sleep` 以 1s 切片睡眠保证 owner abort 及时落地；非 429 异常照旧立即传播（不重试确定性故障）。
- **修复二（禁止中文原句进 vertical + 自带搜索兜底）：** 翻译未成功时跳过 arXiv vertical——`_search_arxiv_via_web` 新 seam 走 tofu 自带 web 搜索（`lib.search.search_bridge.install_search_bridge` → `tofu_search.search.orchestrator.perform_web_search`，「方向 + arxiv」查询），从结果 URL 归一化提取 arXiv id 收成种子列表；vertical 健康但结果不足 `_MIN_HARVEST_PAPERS`（3）时同样 web top-up。原语言检索正是 web 引擎相对英文 vertical 的结构性优势。
- **修复三（前端误导文案）：** `research-view.ts::researchIdeasHtml` 空面板分支按 `stream.status` 区分——`error`/`aborted` 运行显示新 i18n 键「运行中断，没有评审结论 / 流水线在完成前中断——空列表是故障的结果，不是闸门的评审意见」，只有真正完成的运行才保留「宁缺毋滥诚实零」文案。
- **规格：** test_research_request_contract 重写旧 fallback 针（钉死「中文原句永不进 vertical」+ web 兜底 + 下一轮重试翻译）+ 新增两针（429 退避重试直到成功、超期限后 web 接管而非死亡）；test_research_discoverability 渲染 harness 新增 error 场景（events 返回 status:'error' 空结果 + lookup found:false）+ 一针断言中断运行不显示闸门淘汰文案。
- **验证：** 五套件 50 passed（request_contract 9 / recipe 14 / action 8 / agent_reuse 7 / discoverability 12）；generate:i18n 再生成（4746 keys，+2）；tsc/ruff 绿。
- **生效条件：** 需重启 worker（recipe.py 在运行进程内）；前端硬刷新。
- **遗留：** discovery 之后的 survey/ideas/evaluate 各阶段的 LLM 调用仍是单次预算致死策略——本次只修了「第一篇论文都搜不到」的最短路径；如需全阶段 429 韧性是另一条独立改动（阶段级 crash-resume 已存在，重跑会重做该阶段）。

### 2026-09-04 (服务商卡片三改：密钥区单行紧凑化 / 模型管理可新增模型（目录多选+自定义）/ 价格缺失回退目录价)

- **起因（用户报告，截图）：** ① 密钥区空间浪费——30+ 字符宽度的密钥输入框只放 9 字符的首尾识别位；② 模型管理不能给服务商新增模型（要求可从模型配置里直接选、也允许自定义）；③ 模型管理里价格有缺失——服务商无特价时应默认沿用模型目录的挂牌价。
- **密钥卡片（06-file-changes-settings-shell.css + provider_render.js）：** 卡片从两行改单行 flex（#N 徽标、密钥文本、眼睛/删除钮、当日统计 margin-left:auto 推右，窄屏自动换行）；masked 输入框以 `size` 属性按内容定宽（reveal/hide 同步重算 size）、去边框读作纯文本，hover/focus 才现框线；新建密钥草稿由 `.stg-key-card--new` 覆盖保持全宽盒式编辑器。
- **添加模型 picker（provider_render.js，+290 行）：** 模型管理工具栏新增「+ 添加模型」二级弹层（z-index 12010，复用 manager-overlay 骨架）——自定义模型 ID 输入（Enter/按钮）+ 目录多选（搜索、勾选、批量添加，排除已供给与 retired 模型，行内显示目录价）；staging 进 `_stgModelRouting` 随全局保存（save_export.js 的 Api.modelRouting.replace）持久化。
- **staging 状态决策：** 新 deployment 直接写 `enabled + probe_status:'passed'`（目录模型 confidence 'high'、自定义 'pending'）——domain 校验（lib/model_routing/domain.py）拒绝 enabled 而未 passed 的 deployment，写 unprobed 会让保存直接失败且 UI 没有任何翻正流程；与 speech.ts / discovered_provider.py 既有先例一致，访问矩阵探测仍是事后验证真实可达性的手段。confirmed 供给按 `_credential_authorized` 规则把模型 ref 追加到该接入全部既有凭证（去重），可在访问矩阵撤销；自定义模型为 pending_identity（仅本服务商显式调用，不参与官方路由），不发授权。wire id（含别名占用）冲突整体去重跳过。
- **价格回退：** `_modelRoutingProviderContext` 新增 modelListPricing 映射；模型管理行取 `actual_pricing || list_pricing`，回退时标注「目录价」，两者皆无显示「未设置价格」（原一律「未设置成交价」）。注意：服务端计费侧（dispatch_adapter.py 的 actual_pricing 读取）未动，如需计费也沿用目录价是另一条独立改动。
- **卡片入口：** 模型管理按钮去掉 offerings.length 门（零供给服务商也能进管理加第一个模型），空态文案改「尚无模型供给 — 打开「模型管理」添加。」；访问矩阵按钮仍按有供给才显。
- **规格（tests/providers/test_provider_model_add.py，5 针）：** node harness 行为测试——目录 staging 产出 confirmed+enabled+passed+high、目录事实继承与默认值、凭证授权追加去重、重复 staging 幂等；自定义 staging 产出 pending_identity+pending confidence 且不发授权；价格三态（actual 无标注、目录价标注、双缺空态）；零供给卡片仍渲染模型管理 + 密钥输入 size="10"；工具栏/弹层 data-tofu-action 源码钉。
- **验证：** 新套件 5 passed；相邻五套件（brand_resolver/local_deploy/panels_parity/capability_taxonomy）44 passed；服务端三套件（agent_provider_setup/headless_agent_server/public_agent_runtime）35 passed；check:frontend 全绿（compose 163 sections/31 styles + 契约 + i18n + tsc + Vite build + canary，117 assets）；ruff 绿。check:i18n 预存过期为并行会话 WIP（paper/inject 键），generate:i18n 顺带再生成。
- **生效条件：** 纯前端展示/staging 改动——已重新构建，浏览器硬刷新即生效。

### 2026-09-04 (My Context 学习机制双失灵根修：429 预算 1→3 恢复召回 + 证据消息须带持久信号杜绝垃圾偏好)

- **起因（用户报告）：** 「我的上下文」完全没用——回答偏好学了一堆乱七八糟的，关于我/工作规则什么都学不到。
- **数据实证（data/memories/profiles/1_*/.tofu_user_context_changes.json + logs）：** 自 08-28 起仅 5 条助理学习记录，其中 4 条是一次性任务噪音（"Yes, you can"、「给复制的会话加 copy 标签」、「像资深研究者一样简练审阅一下这篇论文」、一条任务型 work_rule），用户已手动删剩 1 条；09-01 以来 19/19 次 dispatch 全部死于 `Upstream rate-limit attempt budget exhausted (1/1)` 与廉价池无路由模型（gpt-5.4-nano/kimi-k2-0711 400/404），零新增。
- **根因（两层）：** ①召回死亡——`_MAX_429_ATTEMPTS=1`，个人网关廉价池常态饱和，一次 429 即整轮放弃；且 `_DURABLE_SIGNAL_PATTERNS` 漏 `来自`/`不要`，UI 自己的示例「我来自美团」作短消息发送时连评审门都过不了。②精度失控——确定性校验只查逐字证据/形状/去重，语义价值全押 cheap 模型自觉；"Yes, you can" 这种引用噪音逐字 grounding 全过。
- **修复一（召回，profile_consolidate.py）：** `_MAX_429_ATTEMPTS` 1→3（429 轮换落不同槽位，advisory 预算仍有界，注释记录实测依据）；模式补 `我来自`、`不要`、`i'm from`。
- **修复二（精度）：** 新增 `_evidence_has_durable_signal`——`kind='new'` 动作的逐字证据必须落在一条本身命中持久信号模式的用户消息里（update/reinforce 豁免：纠正已有条目自带意图）；`_recent_surface` 拆出 `_recent_user_texts`（预算语义不变，前缀/截断逻辑同构）；系统提示词对齐该规则并补反例（任务级风格要求≠response_preference、引用/粘贴文本≠用户陈述）。实测 6 例：4 条真实垃圾全拒，「不要出现大模型表述风格」「我来自美团」「提交集群任务时→hope MCP」全过。
- **修复三（可观测）：** `_validated_actions` 按原因计数（shape/evidence/kind/no_durable_signal/unknown_item/type/fields/unsupported/duplicate），有拒绝时一条 debug 日志——此前「模型提了但全被拒」完全不可见。
- **规格：** test_user_profile.py 新增 4 针（一次性指令被拒且确认模型已被调用、短 identity 可学、裸「不要…」偏好可学、无信号 update 仍放行）；test_user_context.py 的 429 预算钉更新为 3 并记录理由。
- **验证：** test_user_profile 41 passed；test_commit_round_daemon + test_llm_json_consolidation + test_user_context + test_context_composer 合计全绿（89+81 passed）；ruff 绿。
- **生效条件：** 需重启 worker（profile_consolidate 在运行进程内）。

### 2026-09-04 (执行前拦截双重展示根修：工具行接管完整原因（不截断可换行），timeline 重复卡片按轮剔除)

- **起因（用户报告，截图）：** 同一次 `search_tool_artifact` 契约拦截渲染了两处——上面工具面板行（徽标「执行前已拦截」+ 300 字符截断的原因，「cursor(str…」正好截在要害处）与下面琥珀色 activity 卡片（全文 + kind 码）。用户裁定：去下留上，但上面必须显示完整。
- **重复链：** 后端 `lib/turn_activity_timeline.py` 对带 rejection 的 skipped 轮发 `activity.tool.blocked`/`unavailable` 条目（时间线保持完整，不动）；前端 `visibleActivityEntries` 原样放行，锚定在同一工具块下方，而工具行 `_renderRejectedToolLine` 本就渲染同一 rejection——两处同源同文。截断链：`firstLine = reason.split("\n")[0]` + `slice(0,300)+"…"`（实测报错全文 ~340 字符，恰在 300 处被切）+ CSS `-webkit-line-clamp: 4`。
- **修复一（去重，conversation-view-model.ts）：** `visibleActivityEntries` 新增前置过滤——收集带 rejection 的工具轮 `toolCallId` 集合，命中集合的 blocked/unavailable 条目不再上屏；轮被修剪/缺失（历史边缘）时条目保留，信息不丢。原始时间线与后端契约不变，Android/调试消费不受影响。
- **修复二（完整显示，tool_rounds.js + 09-tool-terminal-details.css）：** `_renderRejectedToolLine` 删掉首行+300 字符截断，全量渲染（`\n`→`<br>`），reason 缺 kind 码时追加一行（与被移除卡片的信息持平，如 `tool_contract_invalid`）；CSS 对 `.ptool-line.ptool-rejected .ptool-error-reason` 解除 4 行 clamp（普通失败工具输出保留 clamp 不动）。
- **规格：** wire-parity 电池新增两针（契约拦截长文 + 幻觉建议分支），baseline 重生成（60 轮，既有 58 轮输出零漂移）；`test_battery_covers_every_dispatch_family` 守卫补两个 rejected 家族标记；conversation_surface_vite 新增 `test_activity_tool_rejection_row_yields_to_the_inline_tool_rejection`（命中轮剔除 + 孤儿 unavailable 行保留）。
- **验证：** wire-parity 2 passed；surface 套件 68 passed；turn_activity_timeline 31 passed（1 失败为并行会话 WIP 的 model_fallback 契约漂移，与本次无关——本改动零 lib/ 文件）；check:styles + check:frontend 全绿（compose 163 sections/31 styles + tsc + Vite build + canary，117 assets）；ruff 绿。docs/modules/task_engine.md 失败规则段补展示层去重契约。
- **生效条件：** 纯前端展示层改动，浏览器硬刷新即生效。

### 2026-09-04 (本机构建 APK 挂上服务端 LAN 分发：/api/v1/mobile/*——扫码即装，彻底不走 GitHub)

- **起因（用户裁定）：** Android/iOS 安装包下载源挂 GitHub 不合理——与服务端同仓代码可能不一致；既然 android/ 本机构建链已打通（见前条），应直接从服务端分发 assembleRelease 产物。
- **实现（routes/api_v1/mobile.py，新 blueprint api_v1_mobile_bp）：** 三个端点全部走常规 auth 门——① `GET /api/v1/mobile/android/apk` 以 `application/vnd.android.package-archive` attachment 服务 APK（send_file_conditional + no-store + nosniff），未构建时 404 并提示先跑 assembleRelease；② `GET /api/v1/mobile/android/link` 返回 {available, url, tokenEmbedded, qr, sizeBytes, modifiedMs}，url 按请求 Host 拼绝对地址（桌面浏览器用 LAN IP 访问时手机扫码同网段可达）；③ `GET /api/v1/mobile/android` 零前端构建依赖的自包含扫码页（QR data URI + 状态 + 直链）。APK 路径默认仓内 Gradle 输出，`TOFU_ANDROID_APK_PATH` 可覆盖。
- **手机鉴权设计：** 复用既有 `?token=` first-link 传输——会话 cookie 存的就是原始 token，`link`/扫码页把当前请求出示的凭证嵌进 QR URL（`_extract_bearer_or_cookie` 重提取），手机首次命中经标准 query-token 重定向装上自己的会话 cookie，之后下载走正常认证；无凭证时 tokenEmbedded=false 如实上报。不发新 key、不开公共路径。
- **规格（tests/api/test_api_v1_mobile.py，6 针）：** 未构建 404；mime/Content-Disposition/字节逐等；link 的 token 嵌入 + Host 头绝对 URL + QR data URI + sizeBytes；无 token 时 tokenEmbedded=false 且无尺寸字段；扫码页渲染 QR；三端点无 auth 全 401。
- **验证：** 新套件 6 passed；相邻三套件（capabilities_agents_drift/tool_inventory/api_v1_integration_control）26 passed；整树 server 装配冒烟（55 个 v1 blueprint 无重名）；ruff 绿。docs/contracts/HEADLESS_API.md 新增 §3.10。
- **生效条件：** 新路由模块在运行进程内——需重启 server；之后桌面浏览器开 /api/v1/mobile/android 扫码即装。
- **遗留：** iOS 无本机构建链（Linux 机），不在本次范围；Android 客户端内建的 GitHub 更新检查源仍指向 remote release，如需「客户端自更新也走服务端」是另一条独立改动。

### 2026-09-04 (feature.loadFailed 根修：Vite 多代资产保留 + idle 全量预取，终结「点按钮报模块加载失败、强刷就好」)

- **起因（用户报告）：** 存活标签页点击按钮偶发 toast「功能模块加载失败，请检查网络后重试」，强制刷新必好。
- **根因（两层，均与构建代际有关）：** ① 构建只保留「当前 + 上一代」资产（build_frontend.mjs retained 集合），开发机一天多次重建前端，超过两代仍开着的标签页其懒加载 chunk 已被物理删除，点击即 404；浏览器模块表会把失败的 dynamic import 缓存整个文档生命周期，之后每次点击都失败，只有刷新能清。② 除少数域外所有 feature chunk 都等首次点击才拉取，瞬时网络抖动即中毒同一张模块表。已有 build-watch（push pong 携带 build id）与 feature-load-recovery（60s 有界重载 + sessionStorage 接力）只是缓解：busy 时重载最长推迟 30 分钟，且代际 >2 时重载前的点击已必先失败一次。
- **修复一（scripts/vite_asset_retention.mjs + build_frontend.mjs）：** 新增纯函数保留规划器——当前/上一代图钉死不动，更老的已掉代资产按 mtime 从新到旧保留，默认 7 天 / 48 MiB 双上限（env TOFU_VITE_ASSET_RETENTION_MAX_AGE_MS / TOFU_VITE_ASSET_RETENTION_MAX_BYTES，clamp 0..30 天 / 0..56 MiB，56 MiB 上限保证 static/vite 全树仍在本地镜像默认 64 MiB 预算内，AGENTS.md 原则 3）；每次构建 utimes 重盖当前图全部资产（atomicCopy 对已有文件短路，不盖则掉代文件的 7 天宽限从首次发布时间起算被吞掉）；候选 stat 容忍并发构建 ENOENT。vite.config emptyOutDir 置 false：裸 vite build 不再清空 live 目录。
- **修复二（frontend/src/main.ts）：** idle 全量预取——boot 后每个 idle 切片（requestIdleCallback，timeout 2s）经 loadDomain 拉 4 个路由域，健康会话里点击时模块表已全热、零网络取件；失败仍走既有 60s 有界重载 seam（预取与点击语义一致），openUpdateDialog 保留 prepareFeature 通告路径不进扫掠。
- **规格：** tests/test_frontend_vite_asset_retention.py 新增（接线钉 + 13 针 planner 行为 harness：年龄窗/字节顶/新者优先/同名决胜/未来 mtime/NaN 元数据/env 解析与钳制）；test_bundle_concurrency 清理钉更新；test_frontend_feature_load_recovery 补 prefetch 接线三钉。
- **验证：** 受影响四套件 22 passed；check:frontend 全绿（含 Vite build + Chromium canary，117 assets）；真实构建两轮实证——「kept 22 older-generation files, pruned 0」，前两代 main-D8j8CAHT.js 与当前 main-ByvMZ9DP.js 并存可解析；ruff 绿。**预存失败（与本次无关，stash 后 HEAD 复现）：** test_bundle_manifest_parity 两败（routes/common.py 资产槽钉，系并行会话 WIP 漂移）。
- **生效条件：** 纯构建期/前端改动——已重新构建，浏览器硬刷新即享预取；旧标签页里已中毒的模块表项仍需一次刷新清理（浏览器语义，无法原位消除）。

### 2026-09-04 (android/ 本机构建链打通 + 按钮圆角/卡片描边样式统一)

- **环境（用户裁定本地搭链）：** tofu 开发机用户态装好 Android SDK（~/Android/Sdk：cmdline-tools + platform-34 + build-tools 34）+ 复用共享盘 JDK17；`./gradlew test` + assembleDebug 全绿（21 套件 236 测试），assembleRelease 签名与 GitHub 已发布版一致、可覆盖升级——本机构建产物与 GitHub remote 版本同源，回答「下载源挂 GitHub 是否奇怪」：现在本机即可从服务端同仓代码直接构建，不再依赖 GitHub artifact。
- **测试基建（commit f1d9d4ef2）：** ReauthWebViewClientFailureTest 从手摸 fake 升级到 Robolectric shadow 框架（WebResourceError 包私有构造无法 fake，传输错误用例直接驱动抽成 static 的 routeMainFrameTransportError）；fetch-test-deps.sh 钉版本抓取全部 SDK-free 测试 jar（含 @Config(sdk=33) 的 android-all-instrumented），新克隆可离线跑 ./test-local.sh。
- **样式（commit 44133a4ea）：** 审计发现两处真实不一致——① 按钮圆角：M3 默认体育场形（stadium）与 12dp 输入框/16dp 卡片同屏打架（仅「Save & connect」手写 13dp），新增 TofuButtonShape(12dp)（对齐 web --radius）统一全部 Button/FilledTonalButton/OutlinedButton/TextButton，FAB 保留体育场形（Material 惯例）；② ServerCard 零阴影 + surface 贴 background 暗色下仅 ~4% 对比度，补 1dp outlineVariant 发丝线（web 卡片 --border 的同角色）。Theme.kt 新增 TofuButtonShape token，ServerCard 改用既有的 CardShape 别名。验证：`:app:compileDebugKotlin` 绿。
- **环境坑（已存 memory git-commit-sigbus-shared-fuse-concurrent-repack）：** git commit 反复 SIGBUS——并发写入方在别的主机重写 pack，FUSE mmap 不一致；等 pack mtime 稳定窗口再提交即成功。
- **生效条件：** 纯客户端改动，随下次 assembleRelease 发布生效。

### 2026-09-04 (翻译 429 换不动根修：翻译 dispatch 解除会话粘连——无状态调用不等热 key，纯分数选槽 429 即换)

- **起因（用户裁定）：** 翻译太不稳定；翻译不需要长时间记忆，429 了就快速换、换到啥是啥。
- **根因（conv affinity 粘连链）：** 翻译 dispatch（`smart_chat`/`dispatch_stream`，capability='cheap'）在 worker 线程上继承会话粘连——`dispatcher._pick` 优先会话粘 key（route_key `cap:cheap`），429 时 `_api_stream` 的 warm-key hold 为保 prompt cache 等热 key 最多 1.5s（争用升级 8s）再重选；粘住的 provider 饱和时整轮烧掉翻译的 429 预算（TOFU_TRANSLATE_MAX_429_ATTEMPTS，个人探针 4–8）后以 DispatchRateLimitBudgetExceeded 失败。翻译每次请求都是新载荷，没有可复用的缓存前缀，这套「记忆」纯属延迟与失败放大。
- **修复：** `lib/llm_dispatch/conv_affinity.py` 新增 `conv_affinity_cleared()` 上下文管理器（暂存→解绑→退出恢复）；`lib/translate/engine/_engine.py::_dispatch_translation_candidate` 的非流式（smart_chat/dispatch_chat）与流式（dispatch_stream）两条路径均在其内 dispatch。效果：picker 纯分数选槽（429 轮换直接落到最健康可用槽）、warm-key hold 永不触发（`_conv` 为 None）、翻译不再 record/rebind 会话粘 key、cache-settle 门（`_cache_conv_id=''`）跳过；退出后恢复线程绑定，后续聊天轮次的粘连不受影响。一处包裹覆盖全部翻译调用方（server auto-translate、/api/v1/translate、chat send-path、message_queue、paper 翻译——均汇聚 `_translate_one_chunk`/`_translate_freetext`）。
- **规格（tests/translate/test_translate_dispatch_deadline.py）：** 两针——非流式与流式 dispatch 期间 `get_conv_affinity()` 为 None 且返回后恢复原绑定。
- **验证：** 本套件 + test_conv_affinity 19 passed；翻译引擎相邻五套件（model_gate/incremental/refusal_cache/worker/send_lane）61 passed；sticky/dispatch 相邻四套件（warm_key_hold/epic_c_sticky/dispatch_stream/durable_exclusions）60 passed；ruff 绿。docs/modules/llm_io.md 重试段前补契约句。
- **生效条件：** 需重启 worker（engine/conv_affinity 均在运行进程内）。

### 2026-09-04 (spill envelope cursor 从 0 重读根修
### 2026-09-04 (spill envelope cursor 从 0 重读根修：cursor 指向 summary 预览末尾真实字节偏移 + read/search 响应删三个死字段)

- **起因（用户报告）：** mtmoc0cjzapyq4 会话读 xuecheng 文档（29,285 字节），spill 后轮轮从 cursor=0 重读 artifact，preview 已展示的内容被重复消耗 token；并指出 read 响应里 expiresAtMs/mediaType/visibleBytes 对模型无决策价值。
- **根因（lib/tasks_pkg/compaction/_budget.py 的 _materialize_envelope）：** spill 时 `cursor="0"` 硬编码——纯文本结果的 summary 本就是原文的 byte prefix（二分截断在 _fit_summary/inline 双循环验证），模型已看 bytes 0..N 却被告知从头读，每次 spill 浪费约等于 preview 全部 token（实测 93KB 文档浪费 ~7.5k tokens/轮）。
- **修复一（cursor 续读）：** `_materialize_envelope` 新增 `artifact_content` 参数（仅 budget_tool_result_v2 主调用点传入原文）+ `_resume_cursor`——当候选 summary（剥掉 marker 后）是 artifact 原文的真实前缀时，cursor 指向 preview 末尾的 UTF-8 字节偏移；合成 summary（结构化 JSON 结果、聚合缩减）保持 "0"，因为 preview 不对应原文偏移。marker 拆两种：前缀可续读的用新 `_ARTIFACT_RESUME_MARKER`（明说「preview 已覆盖开头，cursor 指向其后」），其余保持原文案。
- **修复二（死字段）：** `_tool_result_artifact_read` 响应删 expiresAtMs/mediaType/visibleBytes（SELECT 顺带不再取 media_type 列），`_tool_result_artifact_search` 删 expiresAtMs；模型只需要 content/offset/nextCursor/truncated/sizeBytes/artifactRef。put 响应的 expiresAtMs 保留（CAS 幂等钉测试）。
- **规格（tests/tasks_pkg/test_long_agent_v2_contracts.py）：** `test_tool_result_v2_plain_text_spill_cursor_resumes_after_preview`（中文文档 spill：cursor>0 且 stored[:cursor] 逐字节等于 preview 去 marker）；`test_tool_result_v2_structured_spill_keeps_zero_cursor`（JSON 数组 spill 保持 "0"）；owner-scoped 套件补三字段不存在断言（read + search）。
- **验证：** test_long_agent_v2_contracts + test_tool_result_artifact_batch 57 passed；artifact 续读/loop-breaker/投影四套件 191 passed；sidecar 契约 -k artifact 4 passed；test_paper_full_tools 32 passed；ruff 绿。端到端干跑（93KB 文档）：cursor=18534 与 preview 零重叠拼接无损，envelope 7563 tokens 对比旧行为重读浪费 ~7459 tokens。
- **生效条件：** 需重启 worker（_budget/_artifacts 均在运行进程内）。

### 2026-09-04 (steer 选了「立即插入」却静默排队根修：turn prelude 复活 conv inbox 墓碑槽位 + steerFallback 全链路透出)

- **起因（用户报告，截图「排队中 #1」）：** 用户明确选了「立即插入」，过了很多工具轮后消息仍显示排队——界面只说「排队中」不解释为什么。
- **根因（两层）：** ① 上一 turn 的 swarm teardown/abort 把会话键控的 inbox 槽位墓碑化，语义是「不会再有 orchestrator drain 它」；但新 turn 的 orchestrator 每轮都在 drain 同一个键，`run_turn_prelude` 却没有复活槽位——steer 快通道整轮关闭，`enqueue_after_durable_commit` 返回 False，`_deliver_while_running` 静默降级 durable queue。② 降级路径只有一个不分原因的 `steer slot is closing` 日志，ACK/队列投影/前端渲染全无任何「你选了插入但窗口关了」的信号。
- **修复一（根修，lib/tasks_pkg/orchestrator/_turn_prelude.py）：** 新增 prelude 第 1 步——任务启动时若 `swarm_key_for(task)` 对应槽位被墓碑化则 `untombstone` 复活（fail-soft，复活成功 info 日志）。墓碑的「安静期守卫」语义由 finalize 在 turn 结束 teardown/abort 时重新立碑保留。
- **修复二（可观测，lib/conversation_sync/command_service.py）：** steer 降级分原因日志（`no_live_attempt`/`inbox_tombstoned`/`steer_window_closed`/`attempt_gone`/`commit_not_persisted`）；queue payload 打 `_steerFallback` 标记；ACK 回 `steerFallback: true`。
- **修复三（契约+投影）：** `contracts/conversation_sync_v3.yaml` 的 `ConversationQueueItem`/`TurnCommandResponse` 加 `steerFallback`（重新生成 py/ts 契约）；`_queue_item`（queue.list）与 conversation.get 投影（_turns_read）透出；`lib/message_queue.py` 的 `_QUEUE_PREVIEW_KEYS` 加 `steerFallback`（queue-bar 轮询契约）。
- **修复四（前端）：** 选择弹窗文案明确化——「立即插入当前回复」（副标题注明窗口已关会自动转排队）vs「排队，下一个 turn 再发送」；ACK toast 分支：fallback 时提示「插入窗口已关闭——已转为排队 (#n)，将在下一轮自动发送」；queue-status 块与原生队列项渲染器显示「插入窗口已关闭 · 排队 #n」+ 琥珀色 + title 解释；view model `QueueStatusBlockViewModel.value.steerFallback`、`sameQueueItem`/`sameBlock` 差异链覆盖。
- **规格：** test_conversation_command_inject_mode 扩展（ACK/queueItem/get_queue/sync 快照四处 steerFallback 断言 + 普通 queue 行不带标记）；test_lib_orchestrator_turn_prelude_wire_parity 两针（墓碑复活 + fail-soft）。
- **验证：** 两文件 16 passed；相邻契约/队列/快照五套件 46 passed；message_queue 三套件 23 passed；前端会话/队列三套件 72 passed；ruff 改动文件绿（_queue.py 有一处 HEAD 预存 F401 未动）；check:frontend 全绿（含 compose 重新生成 163 sections + i18n 4744 keys）。docs/modules/task_engine.md 注入契约段已补 steerFallback/prelude 复活。
- **生效条件：** 后端（prelude/command_service/投影/message_queue）需重启 worker；前端硬刷新。

### 2026-09-04 (设置弹窗全面去 brutalist：弹窗外壳/矩阵/开关/状态点/页面残留清零 + --s-border 误用修复)

- **起因（用户裁定）：** 设置弹窗整体样式重设计（A+C 方案：柔金浅底选中 + 安静分组标题 + 白卡片 + 发丝线 + C 风层级），明确拒绝扁平/neo-brutalist。按钮立体化已先入库（见前条），本条是外壳与页面残留收尾。
- **弹窗外壳根修（application/07）：** `.settings-panel.modal` 残留 3px 墨框 + 直角 + `8px 8px 0` 硬偏移阴影——特异性 (0,2,0) 压过 06 的 `.settings-panel` 软壳，整个弹窗实际渲染粗黑框。改为与 06 对齐：1px 细边 + `--s-radius-lg` + 双层柔和深投影。**权威地图教训：外壳视觉以 07 的 `.settings-panel.modal` 为准（已存 memory settings-shell-authority-map-and-brutalist-scan）。**
- **token 误用修复（settings/07 权限矩阵）：** `border-bottom: 1px solid var(--s-border)`——`--s-border` 是完整简写 `1px solid var(--s-line-strong)`，嵌套后展开成无效声明，表格横线静默丢失。矩阵全面发丝线化（`--s-line`/`--s-line-strong`），外框 2px 墨线 → 1px。
- **页面残留清零：** settings/04 的 auth-src/priv-host 主按钮立体金渐变、状态点 2px 方框→圆形细边、`auth-src-switch` 从方形硬轨换为与 `.stg-toggle` 同语言的内凹胶囊轨道+弹簧滑块、tool-discovery 卡片三处硬阴影→软阴影（图标改墨渐变+金辉环）；settings/05 remote-root-add、settings/01 mt-provider-card、settings/02 skills-drop-zone 三处对角位移 hover → translateY 垂直浮起。全树三正则复扫（`[2-9]px [2-9]px 0` 硬阴影 / `[2-9]px solid` 粗线 / 对角位移）确认无漏网；豁免 ctx-* 索引卡片页与 tofu 像素主题（均为有意设计）。
- **顺手：** general.html 两处 toggle 行 inline `margin-top:8px` 移除（交回 .settings-group gap）；06 的 `.stg-table` 过时 brutalist 注释订正。
- **验证：** compose（31 sections）+ check:styles + check:frontend（Vite build + canary OK，117 assets）全绿；test_settings_panels_parity 14/14；browser_preview_page 渲染 after.html / after-reveal.html 双页 0 console 错 0 页面错误；browser 子代理真实浏览器实测（结果见当轮会话）。
- **生效条件：** 纯静态 CSS/HTML，浏览器硬刷新即生效。

### 2026-09-04 (kimi 全系无 AA 分数根修：creator 品牌别名桥接 + Pareto 前沿标签左上避让)

- **起因（用户报告）：** 成本 × AA Index 图里 kimi 全系没有质量分；前沿模型名称写在点右侧挡住其他散点。
- **根因（lib/model_catalog/_score_matching.py）：** `match_model` 的 creator 过滤只做裸字符串前缀比较——目录 canonical creator_id 是 `moonshot`，AA 数据集 creator 是 `Kimi`，互不为前缀，12 个 kimi 模型在名称比较前就被整行滤掉。别名知识（`FAMILY_BRAND_KEYS['moonshot']=('moonshot','kimi')`）就在 `_creator_families.py` 但匹配器没引用。
- **修复：** 新增 `_creator_identities`——用 `FAMILY_BRAND_KEYS` 把 canonical creator 扩展成家族品牌身份集（moonshot↔kimi、alibaba↔qwen/tongyi、meta↔llama、zhipu↔glm/zai 等全部生效），creator 过滤对身份集做前缀相容；`kimi/kimi-k2.5` 这类命名空间 model_id 增加叶名（`rsplit('/',1)`）参与匹配。保守语义不变：精确匹配唯一才收，等长前缀歧义仍放弃。
- **标签（frontend/src/features/model-catalog/pareto-chart.ts）：** 前沿点标签从固定右侧（pointX+15）改为候选位链——优先左上（text-anchor end，pointX-15，逐 13px 上移避让他标签，8 档），出画/重叠才回退右侧原位链。左上矩形按 Pareto 定义必然无散点（否则该点被支配），数学上不遮点；最左 $0 车道的点左侧没空间自动回退右侧。
- **规格：** tests/model_routing/test_model_intelligence_aa.py 两针（Kimi 品牌行匹配 moonshot::kimi-k3=59.7；kimi/kimi-k2.5 叶名匹配 36.0）；tests/test_frontend_pareto_chart.py 一针（付费前沿标签 anchor=end 在左上、$0 车道长名回退 start、标签盒两两不重叠）。
- **验证：** AA 套件 7 passed；creator/路由相邻四套件 119 passed；真实数据离线干跑：11 个 kimi 模型全部命中（kimi-k3→59.7 Kimi K3 max、k2.6→45.1、k2.7-code→43.0、k2-thinking→33.5 等），总匹配 231/409；check:frontend 全绿（含 typecheck + canary）。
- **预存失败（非本次引入）：** test_frontend_pareto_chart.py 旧针的 `stg-mc-pareto-badge` 断言（HEAD 代码就是 pareto-glyph，另一会话 WIP 漂移）；test_frontend_model_routing_v2.py 三败同为 WIP 漂移。
- **生效条件：** 后端 _score_matching.py 需重启 worker；前端硬刷新。

### 2026-09-04 (swarm 流并发槽拉满
### 2026-09-04 (swarm 流并发槽拉满：probe 公式 t/2封顶4 → t*2封顶64；sub-agent 429 重试预算 120 次有界释放槽位)

- **起因（用户裁定）：** 会话 mtmeigdi90y5kx 七个 sub agent 只有四个在跑（三个 round 0 排队 100 分钟），用户裁定「流并发槽拉满，不许限制，太耗资源就想办法优化」。诊断另发现 par-knowledge 在 429 重试循环里空转 200+ cycle/2h+、par-ops 单次调用挂起 2.6h——两者占着全局槽位不放，看门狗被重试循环的 checkpoint 心跳喂住（quiet_for 反复清零，stall 永不触发）。
- **改动一（拉满，runtime_guards.py + lib/swarm/resource_policy.py）：** personal probe 公式 `swarm_global_workers` 从 `min(4,(t+1)//2)` 改为 `min(64, t*2)`，wave=workers*2（封顶64），session=wave*3（封顶256），session_capacity=t*2（封顶128）；resource_policy 硬顶 32/16/32/128/64 → 64/64/64/256/128。本机实测新值：workers=64、parallel=64、wave=64、session=192、capacity=96（t=48）。显式高顶保留（resolve_resource_budget 架构要求有界值），不等于无界。
- **改动二（优化，lib/swarm/agent.py）：** 新增 `SWARM_MAX_429_ATTEMPTS`（env `TOFU_SWARM_MAX_429_ATTEMPTS`，默认 120，clamp 1..4096），两处 `dispatch_stream` 调用（主循环 + wrapup）传入 `max_429_attempts`——detach 的 sub agent 没有用户替它取消 429 墙，120 次（≈30 分钟）后本轮失败并释放执行许可，不再空转占槽。_api_stream.py 本就预留了该 opt-in 参数（「blanket cap 故意去掉」是前台轮次语义，有用户取消兜底，不适用后台 agent）。
- **规格：** test_deployment_configuration 五组快照期望值全量更新（默认快照 8/8/16/48/8、小机 4/4/8/24/4、大机 36/36/64/192/36、probe 失败 2/2/4/12/2）；test_swarm_resource_budget 硬顶断言更新；test_swarm_agent_loop_chassis 新增 `test_dispatch_receives_bounded_429_budget` pin。
- **验证：** test_deployment_configuration 42 passed；swarm 相邻五套件 79 passed；ruff 绿。**预存失败（与本次无关，HEAD 上复现）：** test_swarm_resource_budget.py::test_read_only_projection_cannot_advertise_above_resource_wave_limit（maxItems 得 8 期望 2，stash 后同败）。
- **生效条件：** 需重启 worker（runtime_guards 在进程启动时物化 env；resource_policy/agent 常量进程内）。文档 docs/INSTALL.md 三行预算表 + 新增 TOFU_SWARM_MAX_429_ATTEMPTS 行已同步。

### 2026-09-04 (tofu-search 全部标签页 about:blank 根修
### 2026-09-04 (tofu-search 全部标签页 about:blank 根修：capture-before-navigation 卡死链——上限 4→32 + idle reaper + capture 降级 best-effort + 导航失败收尸 + CDP 等待 5s 上限)

- **起因（用户报告）：** tofu - search 打开的标签页全部停在 about:blank。实测 logs/app.log 当天 190 次 `Network capture capacity reached (4 active tasks)`，create_tab/research_url/fetch_url 全灭。
- **根因（两层，browser_extension/background.js）：** ① 85f8ba6cd 引入 capture-before-navigation：`cmdCreateTab` 先建 inert about:blank tab → `_startNetworkCapture` → 才导航；capture 启动抛错时空白 tab 无人导航无人关闭。② `_networkCaptures` 上限 4 且无 idle reaper，`_releaseCdp` 的 `chrome.debugger.detach` await 无界悬置则 stop 路径永远走不完——槽位只进不出；keep-alive alarm 让 MV3 service worker 不死，map 永不重置。
- **修复：** `NETWORK_CAPTURE_MAX_ACTIVE` 4→32（保留硬顶=个人机内存预算，AGENTS.md 原则 3——「不要限制拉满」会放过泄漏，用户已确认）；新增 `_reapIdleNetworkCaptures`（idle>5min 或 age>15min 强制 stop），挂在 keep-alive alarm（每 ~24s）+ `_startNetworkCapture` 容量检查前——容量自愈；`cmdCreateTab`/`cmdNavigate` 的 capture 降级 best-effort（启动失败照导航），导航本身失败关掉 inert tab 再 rethrow——about:blank 残骸路径消除；`_releaseCdp`/`_acquireCdp` 的 detach/closing 等待加 `CDP_SETTLE_TIMEOUT_MS`=5s 上限掐断悬置楔死链。manifest 5.4.4→5.5.0。
- **规格：** tests/_browser_extension_contract_probe.js 新增 `createTabDegraded` 模式（塞满容量钉降级导航 + 导航失败收尸）；tests/browser/test_browser_tab_outcome.py `test_extension_create_tab_never_strands_blank_tabs`；test_browser_research.py / test_browser_network_evidence.py 的 active pin 4→32。
- **验证：** 扩展契约 probe 四套件 54 passed + 相邻七套件 157 passed；docs/modules/browser_automation.md 流程 5/12 已同步。
- **生效条件：** 扩展层不自动更新——**用户必须在 chrome://extensions 手动重载扩展**（旧 SW 里卡死的 4 槽位同时清掉）。memory `browser-blank-tabs-network-capture-capacity` 已更新。

### 2026-09-04 (设置弹窗按钮全面立体化：渐变+内高光+软阴影取代扁平填充；清除残留硬偏移阴影；手机客户端卡片软化)

- **起因（用户裁定）：** 设置弹窗按钮扁平风太丑（金块 保存/检查更新/编辑系统提示词 等），要求全部重设计、明确不要扁平。
- **设计语言：** 「柔软触感」立体按钮——180° 纵向渐变（金 #e3ba66→#cfa14a→#bd8f39 / 米白 #fff→#f6f1e3 / 红 #f26d6d→#dc2626→#c11f1f / 墨 #3d362c→#1a1814）、inset 0 1px 顶部高光 + 底部内阴影、分层软投影（金色按钮带 rgba(201,153,63) 辉光）、金色按钮 text-shadow 微高光；hover translateY(-1px) 浮起+投影加深，active translateY(1px) 按压+内阴影。彻底清除 translate(±1px)+Npx Npx 0 var(--s-ink) 硬偏移阴影残留。
- **权威层级提醒：** `.settings-panel .btn-primary/.btn-secondary`（含 footer）的有效权威是 12-tofu-theme-project-controls.css 尾部的 ALL-themes 覆写块（specificity 打平 + 文件序靠后），07 的 `.settings-footer .btn` 只是几何/兜底——两处处已同步为同一语言；tofu 像素主题在设置面板内本就走该块（无 per-theme token 重定义），不受影响。
- **覆盖面：** 12 覆写块（footer 取消/保存 + 面板内全部 btn-primary/secondary）；06 的 .stg-btn/.stg-btn-danger/.stg-btn-balance/.stg-update-btn/.stg-mobile-row（+ -soon 惰性覆写补 background 冻结）；07 的 .stg-btn-primary/-secondary/.settings-panel .btn-danger/footer 族；settings/01 的 OAuth .btn-small 族（ink 主按钮渐变，删除被基线取代的 :not 规则）。幽灵/虚线/图标/链接类（.stg-btn-add/.stg-btn-icon/.stg-keys-btn/.stg-btn-link）保持安静风不动；ctx「索引卡片」页硬阴影是该页独立设计语言，不动。
- **顺手修复：** .stg-mobile-card 从 1.5px 墨线直角（漏网的 brutalist 残留，即用户截图里手机客户端卡片那圈黑框）改为 1px var(--s-line) + 14px 圆角 + --s-shadow-sm。
- **验证：** compose（31 sections）+ check:styles + check:frontend（Vite build + canary OK，117 assets）全绿；debug/stg_redesign_preview/buttons-after.html 经 browser_preview_page 渲染 0 console 错 0 资源缺失；browser 子代理在真实浏览器新开标签页实测 computed style 与截图（结果见当轮会话）。
- **生效条件：** 纯静态 CSS，浏览器硬刷新即生效，无需重启 worker。

### 2026-09-04 (订阅适配器模型目录：额度快照时机自动触发节流重同步)

- **起因（用户裁定）：** codex 订阅的模型列表此前只在 OAuth 完成/删账号/ensure/provider 不 ready 四个事件点刷新；健康服务商永不重同步，CLIProxyAPI 升级后新增的上游模型不会自动出现。用户要求「有额度刷新机会的时候也自动触发」。
- **机制：** 每条 adapter codex 响应本就携带 x-codex-* 额度头（`record_codex_quota` 以 `adapter:<agent_id>` scope 落快照）——这是零额外成本的同步时机。三处调用点（lib/llm/stream.py、chat.py、astream.py 记录额度处）在快照新鲜时调 `note_adapter_quota_observed`（lib/desktop/adapter.py）。
- **节流与安全：** 每 (owner, agent) 1 小时最多一次尝试（`_quota_sync_marks` TTLCache，先标记后执行防并发踩踏）；后台 daemon 线程 `_quota_sync_worker` 先 fetch `/v1/models` 与路由文档中现有 offering 集合（`model_id or pending_model_id`）比较，**集合不变直接返回**（不重置 dispatcher、不覆写用户手动停用）；需要重 provision 时从现有 provider 行取回显示名后缀，避免名字回退成 agent_id。失败仅 debug 日志，等下一个 TTL 窗口；不做 deprovision（瞬时故障不拆服务商）。
- **规格（tests/desktop/test_desktop_adapter.py TestQuotaOpportunitySync 六针）：** 集合变化重 provision 且保留显示名；集合不变跳过；未 provision 过的 agent 首次观察即建；worker 异常吞掉不传播；hook 每 TTL 只放一次线程、无 policy 不放线程。
- **验证：** test_desktop_adapter 31/31；相邻 quota/adapter/transport 六套件 78 passed；llm stream/chat 传输五套件 72 passed；ruff 绿。
- **生效条件：** 需重启 worker（lib/llm/* 已在运行进程内）。


### 2026-09-06 (目录同步去除 access 启用门槛：停用=关路由，不冻结目录保鲜)

- **起因（用户裁定）：** 用户质疑「为什么还要启用才更新」——模板/服务商卡片有更新就该直接更新。核查发现 `_eligible_targets`（provider_catalog_sync.py）以 access 级 `enabled` 过滤，停用的 DeepSeek 服务商永远进不了认证 `/models` 同步。
- **附带实证（真机只读验证）：** 该用户存储的 DeepSeek key 对官方 `/models` 与 `/chat/completions` 均返回 401 invalid——截图矩阵绿点是缓存探测结果（页面上有「清除探测结果」按钮即证）；用户需重贴有效 key，否则任何同步都取不到目录。
- **改动：** `_eligible_targets` 不再按 access `enabled` 过滤（连接/凭证级 enabled 保留——那是逐组件吊销语义）；模块 docstring 安全轨 + docs/PROVIDER_TEMPLATE_RECIPES.md Lifecycle step 6 同步语义。
- **规格：** test_provider_catalog_sync.py +`test_disabled_access_still_synced`（停用 access 仍 add-only 追加且不回翻 enabled）。
- **验证：** catalog_sync+template_feed 31 passed；ruff 净。
- **生效条件：** 纯 Python——重启 server.py 生效。

### 2026-09-06 (静态服务商模板按 feed 数据刷新：6 家 35 个新模型入库 + canonical 拼写变体去重)

- **起因（用户裁定）：** 动态 feed 层上线后用户仍不满——「直接修改这些过时的静态模板」，要求打开服务商卡片就能看到最新模型，与 key/探测开关无关。
- **改动：** ① 以 `data/config/provider_template_feed.json`（OpenRouter 公共目录缓存）为数据源，把 6 家映射厂商的 feed 新增模型写回静态模板：deepseek +7（v3.1-terminus/v3.2/v3.2-exp/chat-v3.1/r1/r1-distill-llama-70b/v4-flash-vision-exp）、anthropic +7、glm +6、minimax +5、mistral +9、tencent +5；行形状沿用 recipe 契约（model_id/capabilities/rpm/context_window/pricing），capabilities 按 text→vision→thinking→cheap 排序。② 根修 `merge_feed_recipes` 去重：精确 model_id 字符串改为 `canonical_key`——否则 authored `MiniMax-M3` 与 feed `minimax-m3`、`claude-opus-4-8` 与 `claude-opus-4.8` 这类拼写变体会同时进选择器，且在世界同步 migration 折叠成同一 offering identity 时整模板 fail-closed（meta.json 事故同型）。③ 静态合并同规则去重，anthropic 撤 5 条、minimax 撤 3 条拼写重复。
- **边界（guardrail）：** canonical_key 比 migration identity 更严（剥日期），meituan 历史 authored 的 gpt-4o-2024-08-06/11-20 两个日期检查点互撞属刻意共存，**不要**用 canonical 规则重写历史 curated 文件——只对新增 feed 行去重。
- **规格：** test_provider_template_feed.py +1 针（minimax 拼写变体去重）；disabled 针 authored 清单期望更新为 9 条。
- **验证：** feed+recipes+catalog_sync 43 passed；world_catalog_sync 4 passed；feed 开/关两路径 34 个模板全量编译通过；ruff 净。
- **生效条件：** 纯数据+Python——重启 server.py 后世界同步把新模型灌入注册表，卡片「+ 添加模型」选择器即可见；已配置服务商的矩阵增量仍归 provider_catalog_sync（401 key 是当前阻塞）。

### 2026-09-06 (已安装服务商卡片向模板供给收敛：template_supply 同步上线，gemini-3.8-flash 自动进老卡)

- **起因（用户裁定）：** 「我的美团网关卡片是早就添加的，模型里还是没 gemini-3.8 flash，我要求可以自动更新」。模板只播种新安装——老卡永远看不到后来上架的模型；而 aigc.sankuai.com 的 /models 列表为空，provider_catalog_sync 对它无目录可发现。
- **设计（三层闸门，全部为既有语义复用）：** ① **指纹匹配**——模板全部 base_url（顶层+协议面）都在该 access 的 connections 里，且已存在关系（provider brand 相同或 ≥1 个共享配方身份），手工建的同 host 最小 access 绝不灌入；② **探测门控**——缺失配方必须用该 access 自己的凭证实弹探活（ok/rate_limited 与模板策展同一 served 口径），经 build_probe_work 按配方路由（Claude 候选探 Anthropic 面，与调度同路），探死的 id 永不复活、瞬时失败下次启动重试；③ **纯插入**——既有 offerings/deployments/凭证/用户编辑一律不动不删；新行原样复用编译产物（身份/能力/上下文窗），id 按目标 access 重派生，deployment 重指向 access 自有 connection，确认模型的引用追加进凭证 authorization（调度立即可路由）。ok 部署 enabled+passed；rate_limited 部署 disabled+unprobed（域不变量：enabled 必须 passed）——与现网 49 条 unprobed+disabled 同形态。
- **关键验证细节：** gpt-4o-2024-{08-06,11-20} 两个日期快照与模板 gpt-4o 身份 merge-key 相同，同轮内被去重（一身份一 offering）——收敛结果 116 配方 −1 种子 −2 同身份 = 113 新 offering、123 部署。
- **改动：** ① 新模块 lib/model_routing/template_supply.py——sync_template_supply（边界/仓库/探针全可注入）+ 共享监视器调度三件套（poll_if_due/trigger/start/stop，TOFU_TEMPLATE_SUPPLY_SYNC=0 退出，BOOT_DELAY 240s、间隔 6h）；② health_local 监视循环挂 supply_poll（与 catalog_poll 并列，失败不拖累本地健康）；③ 移植成功后 audit_log + _rebuild_dispatcher_slots 即时生效。
- **规格：** tests/model_routing/test_template_supply_sync.py 8 针——全量收敛（ Anthropic/OpenAI 面归属、凭证授权、注册表行、gpt-4o 去重）、全死不加、只加活的、rate_limited 保持可重探、二次运行零操作、用户禁用不复活、无模板 URL 不探测、部分 URL 覆盖不匹配。
- **验证：** 新规格 8 passed；catalog_sync+world_catalog+recipes+routing 契约+meituan 目录+health_local 70 passed；local_autodiscover 18 passed；ruff 净。
- **文档：** docs/PROVIDER_TEMPLATE_RECIPES.md 生命周期补第 7 条。
- **生效条件：** 已触发 supervisor /restart-deferred（等静默换装）；首轮在 worker 启动后约 240s 执行，日志看 'template supply sync'。

### 2026-09-06 (日志扫雷三修：gpt 根组合子 400 / ToolScript 活对象毁持久化 / action DSL 嵌套调用解析)

- **起因（用户要求）：** 扫日志找还没修的 bug。命中三条真实缺陷，另有两条噪音（tika `Couldn't find 'cf'` 检测告警、codex MCP 撞名后 server 已禁用）判定非 bug。
- **Bug 1 — gpt-* 全系工具调用 400：** OpenAI 工具参数校验器拒绝任何根组合子（"schema must have type 'object' and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'const'/'not' at the top level"），write_file 的 canonical 根 `oneOf`（content | content_ref）硬 400 每个 gpt slot。修法：`_normalize_provider_tool_schema`（lib/tools/gateway.py）对 `is_gpt` 模型应用与 Kimi 相同的**健全根松弛**（属性并集 + 每个分支都成立的要求）；MFJS 方言重写仍 Kimi 专属。canonical schema 仍是执行校验权威，线投影只是放宽。
- **Bug 2 — ToolScript 活对象毁终端持久化：** `return tools.call` 这类程序返回活的 `_Bound`，原样塞进 `programRuns` 后 `persist_task_result` 的终端 `json.dumps(meta)` 抛 TypeError——任务终端元数据从未落盘、run 记录丢失。修法（lib/tasks_pkg/handlers/tool_gateway.py `_execute_program`）：`run['result'] = json.loads(_json(value))`，与模型可见载荷同一份 `default=str` JSON 安全投影。
- **Bug 3 — ptool 头展开器静默死：** action DSL 方法调用分支用**贪心**接收者正则，`this.setAttribute('aria-expanded', this.parentElement.classList.contains('expanded'))` 被读成「不可解析接收者上的 `.contains(...)`」抛 "Unknown action method"，点击纯 console 报错。修法（frontend/src/action-registry.ts）：括号平衡感知 `splitMethodCall`——从尾部扫描 `.name(` 边界，取第一个参数文本到串尾括号平衡的切分。**非贪心同样错**（会把 `this.closest('.x').remove()` 的调用形接收者切碎），两方向各一针钉死。
- **规格：** test_wire_tools_sanitize.py +TestGptRootCombinatorProjection 3 针（真实 write_file 投影、非投影 provider 身份透传、gpt 净 schema 身份）；test_programmatic_local_backend.py +1 针（活对象 run 记录 JSON 安全 + 严格 dumps 存活）；test_action_registry_nested_calls.py jsdom 合约 +5 检查（锚点守卫、aria 翻转×2、父类切换、closest 接收者回归）+ 源级针（splitMethodCall/balancedParens 在场）。
- **顺带修两针既有红：** ① kimi 投影日志已按设计降 DEBUG（"bounded debug diagnostic"），测试还在 `caplog.at_level(WARNING)` 下断言——改 DEBUG 捕获 + 新文案断言（HEAD 即红，temp worktree 实证）；② `test_non_kimi_keeps_strict_root_union_by_identity` 用 gpt-5.6 钉「不投影」——gpt 加入投影后语义过期，改 `test_non_projected_provider_…` 用 deepseek-v4-pro 钉身份透传。
- **非本改动之红（均实证）：** test_kimi_500_token_compaction_preserves_description_parameter 在 pristine HEAD(v0.17.1) 即红；邻套另 4 红（ask_human 542>325、write_file 400>360、todo 329>325、ask_human required 漂移）全是兄弟在飞 WIP——write_file 加 `append` 参数、todo.py +204 行、human_guidance questions[] 批量，属主测试尚未跟进。
- **验证：** 三主套 104 passed（仅 HEAD 既有 1 红）；ruff 净；frontend typecheck 本改动零错误（baseline 3 个在别文件）；build + verify 绿，新 bundle 已发布。
- **生效条件：** Bug 1/2 纯 Python——重启 server.py 生效；Bug 3 前端硬刷新即得新 bundle。

### 2026-09-06 (incremental translation: atomic consumer retirement)

- Reproduced a lost-finalization race: after the accumulator queue timed out, but before registry cleanup, `put_terminal(replace=True)` still reported acceptance although its consumer was exiting. The terminal coordinator therefore skipped whole-turn fallback. Terminal consumption also permitted replacement work that would never run.
- `_operation_buffer.py` now closes producer admission under the same condition lock as idle timeout/terminal consumption. Late handoffs return -1; existing `finalize_incremental` returns False and the existing coordinator fallback remains the sole recovery path. Pending terminal replacement remains supported; no new worker, timer, queue, or durable state.
- Regression: two tests failed before the fix; four retirement/admission tests now pass, and 30 existing incremental/visible-turn tests pass. Scoped Ruff passes. Existing broad gates have unrelated retired-route/decorative-marker and documentation line-budget failures. Python service has not been restarted.

### 2026-09-06 (v0.17.2 发布：release_open.sh gate3 路径修复 + VERSION/pyproject 双版本权威对齐)

- **发布：** `scripts/release_open.sh dbece9028 --bump patch --push --harbor-repo …` 全绿——tofu-open 4cd257c8（dbece9028 导出树，10781 文件/脱敏 90/密钥扫描净）+ 2bf26edd（pyproject 对齐）已推 origin/niutrans 双 remote，tag **v0.17.2** 双端落位；harbor `DEFAULT_TOFU_INSTALL_SPEC` pin v0.17.2（cacecba，test_agentix_tofu 10/10）。
- **排障实录：** ① gate 3 指 `$TOFU_OPEN_DIR/lib/trajectory/` 目录，实际代码在单文件 `lib/trajectory.py`——grep 对不存在目录 rc=2 + `set -euo pipefail` 静默杀脚本（连死两轮发布，错误还被 `2>/dev/null` 吞掉）；修为单文件路径并给 gate 3/5 补 `|| true`（gate 2 早有）。② `export.py --bump` 只写主仓 `VERSION` 并提交（0.17.0→0.17.1，108524773），**不碰 pyproject**——导出树 pyproject 继承源 pyproject（0.17.0），tag v0.17.1 与远端既有 tag 撞车被保留（NOT moving without --force 救了一命）；release_open.sh 的 tag 探测读导出树 pyproject，反手把 harbor pin 从 v0.17.1 回退成 v0.17.0。修法：主仓 VERSION+pyproject 双对齐 0.17.2（c248f8299），tofu-open pyproject 对齐（2bf26edd），脚本 tag 探测改读主仓 VERSION（49932abde）。
- **主仓 push 伪命题：** 主仓 origin 就是公开镜像 rangehow/ToFu，但 master **从不 push**（历史与镜像不相交，export.py 单向导出）——JOURNAL 七十二遗留的「push 前需 owner 确认」作废，committed 即完成。
- **提交：** 主仓 49932abde（脚本修复）+ c248f8299（v0.17.2）；tofu-open 4cd257c8 + 2bf26edd；harbor cacecba（ruanjunhao/tofu-kernel 已 push，git.sankuai 走保管库 dev_sankuai_token Bearer extraHeader）。

### 2026-09-06 (检测矩阵：429 默认建议禁用)

- 按用户要求删除短窗口 429（每分钟/每小时）的建议禁用豁免；最终 rate_limited 一律 recommend_disable=true。原多次探测任一次成功优先规则不变，普通 dispatch 的临时限流健康策略不变。
- normalize_probe_snapshot 读时升级存量 429 的 recommendation 并重算 summary，不改写原始缓存对象；前端沿用后端建议，无需新 bundle。
- 验证：test_probe_rate_limit_defaults + test_probe_cells + test_probe_persistence 共 38 passed；ruff 通过。Python 服务重启后生效。

### 2026-09-06 (每日检测矩阵自动应用)

- 新增个人 scheduler 内置 Daily Provider Detection：每日服务器本地 08:00，默认启用，重启 reconciliation 保留用户禁用。仅按持久任务 owner 重建 providers principal，异步执行不阻塞 tick。
- 抽取 HTTP 原有 provider bundle / probe plan 为 lib/model_routing/probe_plan.py 共用；probe_automation.py 通过既有公平工作队列运行全新探测，包含未授权格子。全部成功才按原 revision CAS 一次性移除推荐 grants，不自动开启健康格子；不完整、超时、取消、配置冲突均不提交。
- 单轮总量 4096 cells / 1800 秒，复用原有最多 8 cell workers；取消后排队 cell 不再开始网络请求，已在途请求按原 HTTP timeout 结束。检测可能产生供应商费用（包括图像探测）。服务需在 08:00 运行，不补跑错过的 cron。
- 验证：47 个 automation/API/OAuth/face/resource/scheduler 测试通过；增加取消/预算两针后 automation+nonchat+429 30 passed；ruff 通过。文档与架构 gate 仅报告现有无关超长文档、retired routes/turns_v2.py 与 research-workspace 装饰标记。
- 生效：需重新启动 Python 服务，启动时自动注册任务；本轮未发起生产探测或强制重启。

### 2026-09-06 (研究闭环：复评修订、扩展语料、有界证据与实验主张失效)

- 复评从只附报告改为最多两轮 gated revision/re-review，稳定 idea_id/version、24 条问题、科学字段差异判断、付费阶段检查点与恢复、原始/修订历史和累计费用落盘。高/中优先级且带依据的 ideate 意见自动修订；检索/综述类意见仍明确待处理，不伪装解决。
- 默认检索目标60、最大200，最多6类查询/每次40条，重复饱和停止，入库沿用40篇批次。综述从200篇存量前缀选最多20篇工作集，每篇最多2×700字符原文快照片段，初始证据包40k字符；中文bigram与稳定排序。引用存在不再升级科学置信度，覆盖明确区分索引/选中/未读。
- 创新元数据保守修复、显式噪声最近邻排除、溢出审计有界。实验执行状态与科学结果分离，错误/排队回执不能充当完成测量；假设/协议/证据变化使旧主张失效，稿件另绑定科学快照，旧编译不能重新证明新结论。
- 动作上下文去掉历史回执正文并保留引用，逐请求模型窗口+工具+输出预检（输入48k、输出8k上限，未知窗口32k）；修复 action 读取 survey_md/open_gaps 键名错误。前端显示修订/待处理问题与历史，空白卡字段兼容，缺分不再0.00。
- 验证：完整研究邻域278 passed；本轮后端 ruff通过；前端 build+verify通过。仓库全门禁仍被其他并行改动阻塞：event-contract.generated过期、human-guidance-actions重复destroy/缺批量方法、retired routes/turns_v2，以及其他文档行数。研究契约文档已独立登记 docs/RESEARCH_CYCLE.md，自身文档预算通过。
- 边界：未启动付费模型/计算实验、未重启或提交发布。当前仍为有界arXiv发现和单工作集综述，不是全文分层综合/引用图搜索；证据卡只验证快照匹配，不证明entailment。真实研究质量提升需后续等预算实测。

### 2026-09-06（桌面连接与执行收敛：共用生命周期、轮询独立、配对路由安全、分发完整性）

- 完整版/受控版共用 desktop/agent_connection.py：状态、已授权候选线路恢复、远端浏览器中继、单轮询线程与五秒停止等待；超时保留句柄拒绝重叠启动。两份 spec 显式纳入模块；本地完整版继续使用进程凭证和默认拒绝权限。
- lib/desktop_agent/_runtime.py + _run.py：普通执行和 egress 流离开轮询线程；运行/完成 ID 在进程内去重（65536 硬上限、满时拒绝新执行）；共享启动探测决定 1–4 并发，结果 512项/16MiB、流 256项/2MiB，上报前缀仅在完整有效响应后确认。重试带抖动、60秒上限、停止可打断。非持久 exactly-once；停止轮询不保证已执行子进程被取消。
- _probe.py/_pair.py：识别真实 flat bridge_auth_required；探测和轮询遵循 Requests 代理/NO_PROXY/CA 配置，不跟随重定向，不带 netrc 身份。已有 bearer 不再转移给未经授权的 LAN/SSH 发现地址；允许显式 attach 候选。修改两条旧测试，不再把不安全发现当恢复要求。
- mirror/platforms：保留摘要/非法大小到校验边界，比较发布大小和 SHA-256 再替换；同大小本地损坏重新下载；记录 release_sha256 与 integrity_source（github-api-digest/computed-only）。流下载 2GiB/30分钟上限，失败保留旧包。兼容无摘要旧发布，不声称签名更新。
- 验证分组：运行/分发 112 passed；流式/项目/权限寻址/配对/摘要后续 160 passed；最终启动器/角色/安装构建 148 passed；平台选择及构建先前 153 passed（这些组有重复，不累计）。ruff 通过。一次整合超时定位为两处旧 HTTP fake 不接收 allow_redirects，修桩增加安全断言后复测通过。架构总门禁被非本轮 routes/turns_v2.py 退休接口挡住。
- 未重启服务、未发布安装包、未执行实机签名/安装升级/回退。浏览器看门狗另经审查确认 pending poll 结果与非事务式覆盖风险，本轮未改；不能作为正式无人值守更新承诺。SSO 网页转发仍依赖活跃登录页面。当前文档 docs/modules/remote_execution.md 同步边界。

### 2026-09-06 (Swarm 面板终态优先与跨执行隔离)

- 复现：面板非空 `_swarmAgents` 遮住已落盘的 2/2 结果，标题却采用 settled snapshot，出现 Complete + 0/2。渲染改用终态 snapshot，overlay 不覆盖终态；中止计入结束但不计成功，未确认/静默状态不冒充完成。临时面板与实际 spawn 绑定，wait/result 不作为新执行。
- 持久化：receipt 新增 `swarm_run_id` 保留原 session task，snapshot `swarm_id` 使用该稳定身份；live stamp、CAS、fast-completion compensation 都检查身份，复用 agent ID 不再污染其他执行。缺身份的旧记录保留可读，拒绝含混回写。follow-up 持久化不改原 task id。
- Push：正常与重启恢复适配器在 push envelope 添加 `swarmId`，不修改 task event；前端匹配执行与 roster，终态不被迟到事件重新激活。
- 验证：最终集成 Python 160 passed；Node 面板回归 4 passed；精确故障及状态/snapshot 小套 22 passed。runtime/contract 检查及 build:frontend、verify:frontend 通过。全局 typecheck 仍受既有 human-guidance-actions 重复实现/缺方法阻塞；另有既有资源 schema、事件 fixture、前端来源断言与文档行预算失败，未绕过或扩大修改范围。
- 生效：前端已生成发布，刷新加载；Python 新身份与 push 适配器需服务正常重启。未强停运行任务、未改写历史会话、未重跑已完成子任务。

### 2026-09-06 (研究闭环：引用图、分层证据、跨阶段修订与实验决策)

- 接入 arXiv/Semantic Scholar/CrossRef 元数据及双向引用关系；DOI-only 标记 metadata_only。默认60/上限200，修订补证遵守总量上限。
- 综述改为全部入选原始 parsed_text 摘录的有界 map/reduce；位置/版本/hash 校验后独立语义评估，反证可关闭空白。覆盖指摘录而非全文，语义评估不是证明。检查点串行准入并恢复调用账本。
- 复评触发检索、综述重建、候选依赖重验及淘汰，发布修订后的证据包。完整性审查修复空ID误淘汰、跨空白问题覆盖、陈旧检查点复用等8项回归。
- 实验回执驱动 retain/refine/refute/inconclusive 与需确认的后续实验，负结果可支持限定范围的负结论。稿件要求结构化公平基线、不确定性、种子、验证集及主张溯源审计。前端展示决策/审计；质量对照拒绝自评分和不匹配预算。
- 验证：研究/综述/创新点/harvest/hierarchy 318 passed；研究ruff通过；前端build与verify通过。模块类型检查被无关 human-guidance-actions.ts 重复destroy和缺批量方法阻断；文档门禁被API/STORAGE及三个其他domain map超行阻断，未覆盖并行任务。
- 未运行真实模型等预算对照、真实实验复现或重启服务。历史价格恢复与崩溃后准确账单有边界；测试证明软件行为，不证明顶会质量。

### 2026-09-06 (tofu-db launch verification: bounded cache and isolated benchmark)

- Fixed decoded-cache hit-only LRU growth by compacting stale records above twice resident entries; zero capacity releases the recency allocation. New 100,000-hit regression preserves eviction order. Verified-read population and 8/16 KiB packing/hard limits remain unchanged.
- Benchmark drivers now own independent runtimes/clients, restore startup environment on failures, and clean disposable roots/DSN files. Seven regressions cover backend orderings and lifecycle failures. Git ignore exceptions expose benchmark/client sources for future publication.
- Settled-append footprint regression: 1/32/128/256 turns stage 7,815/92,730/112,422/136,186 bytes and 1/23/35/47 blocks, with identity checked after reopen. Eightfold growth from 32 to 256 produces 1.47x bytes: whole-transcript O(N) rewriting is not established; scattered COW fanout remains the optimization target. No speculative append rewrite shipped.
- Fresh gates: Rust 529 unit + 12 integration passed; strict clippy and release build succeeded (FUSE incremental-cache hardlink warnings only). Release-binary differential + benchmark isolation + v2 client: 149 passed. Separate pre-authority/v2: 6 passed; agent export/runtime CLI: 22 passed (overlapping suites must not be summed as unique tests).
- Fresh medium three-engine report `/tmp/tofudb-verified-medium-y_jgeo63/report.json`: disposable local PG fsync/synchronous_commit on; all workloads ran without errors. Mean speedups vs SQLite in put/get/create/sidebar/open/append/search/mixed order: 1.65/5.53/0.56/1.28/5.34/0.39/1.02/1.88x; vs PG: 4.56/14.94/1.67/4.07/23.37/1.22/5.34/6.15x. Search p95 loses to SQLite (7.035 vs 5.576ms). Creation/append maxima 319/337ms need pause/rotation investigation. One local run is not comprehensive superiority or stability certification.
- Corrected stale README operation/chunk counts and unsupported `certify-filesystem-matrix` availability claim; current CLI exposes only `certify-filesystem`. Selection stays prohibited: 329/331 operations, cutover certification, extended/correlated fault evidence and 30-day shadow remain gates.
- Documentation gate still fails existing line budgets in STORAGE, API_CONTRACT and three domain maps. Existing fsync_stats instrumentation was preserved as unrelated work; absence of all instrumentation is not certified. No deployment, publication, live DB access or service restart performed.

### 2026-09-06（跨轮上下文爆炸：持久续接、请求计量隔离与出站防护）

- 事故追踪：上一轮末次 input 68,037，最终答复约 1,497 字符；下一轮 cold restore 后重建约 3,360 条消息，其中 1,823 个工具结果来自前两轮（1,626 + 197），不是末轮突然新增 2M。超大请求被服务商拒绝；2.18M 是本地估算，不冒充服务商精确 tokenizer 计量。
- 修复：完成任务工作上下文改为 Sidecar 所有权隔离的持久 continuation，每 owner/conversation/lane 一份，8 MiB / 16,384 messages / 4,096 witnesses 上限，无 TTL；完整审计历史不删。请求 usage 复用校验完整前缀、模型和 system/tools，移除历史 anchor 对新请求的下压；selected-slot 出站增加本地硬窗口检查。
- 验证：聚焦 9 文件 87 tests passed（11.77s）。独立审查进一步找到同模型准入整数未绑定内容、done 事件触发队列早于 continuation 发布两个缺口，正在补修，不将该结果等同上线完成。
- 门禁：architecture 仍报 routes/turns_v2.py 退休路径；documentation 存多份文档超行预算，本轮已压紧成本与上下文模块新增说明。共享工作树存在大量其他任务改动，不整体提交或回滚。

- **审查补修已验证：** final selected-slot guard 无条件使用本地计数（复用内容摘要 tokenizer cache，不复用未绑定内容的准入整数）；continuation 发布移到 terminal authoritative settlement 之后、queue drain 与 done 返回之前，失败不撤销结算。新增消息替换/system 漂移/嵌套 tools 修改与阻塞发布竞态规格。最终合并回归 **95 passed / 12.49s**；独立邻套另有 wire 47、生命周期 73 项通过（有重叠，不相加计总数）。Python 与 Sidecar 变更仍需部署/重启后观察真实下一轮 restore 与发布状态，本轮未强制重启服务。

### 2026-09-06 (tofu-db: remove fixed shadow wait; reference-copy reduction and honest benchmark exits)

- User removed the mandatory 30-day shadow waiting period. STORAGE.md and package README retain correctness, durability, migration/rollback and performance/resource gates; production selection was not enabled.
- Reference commits now share pending page/root Arc payload handles until WAL encoding, removing temporary deep copies without changing encoded bytes, 8 KiB packing, verified-read cache policy or durability barriers. Two new exact-envelope/handle-release/crash-fault tests pass. Unavailable Turn maintenance now has an owner/payload fail-closed, zero-authority-I/O regression; no fake maintenance success or executable-registry promotion.
- Benchmark reports now include p99 and return nonzero for skipped engines or failed workloads. Focused Python harness: 10 passed; startup/catalog Python guards: 5 passed; release differential plus harness: 151 passed. Strict clippy and release build pass (FUSE incremental hard-link fallback warnings). Full Rust run: 531 passed, one catalog-classification assertion failed because concurrent catalog work changed totals from [139,188,4] to [140,190,4]; do not count the full suite as green.
- Fresh medium release comparison, /tmp/tofudb-shadow-policy-medium.json: SQLite mean-latency wins 6/8 (put 1.80x, get 4.25x, sidebar 1.39x, open 5.53x, search 2.30x, mixed 1.88x); create 0.56x and append 0.42x remain slower. Create max 319.488 ms confirms write-tail work remains. No fresh PostgreSQL measurement or isolated pre/post attribution in this pass.
- Remaining work: cleanup eligibility/age/reference/quota maintenance; resumable search-rebuild scheduling/consumer and concurrent-mutation fencing; write amplification and checkpoint tails; remaining release evidence. Documentation check reports six existing line-budget failures. Shared worktree has concurrent contract/document writers; no changes were reset or committed.

### 2026-09-06 (tofu-db 架构续推：根页 WAL 去重、时序量具、回填持久游标)

- reference commit 的 root 若已列于 block_ids，WAL 不再在 authority-root slot 重复携带；独立 root 仍保留载荷。8KiB 根页规格精确少8,192字节，崩溃/重开/checkpoint后重开一致。第一波全量 Rust 536+12 passed，strict clippy/release通过。
- 内部 search backfill checkpoint 持久保留 owner/tenant generation、≤1024-byte forward cursor；dirty tokens与cursor同事务，旧观察及并发提交均被OCC拒绝。调度器/全局scope枚举尚未接入，两个maintenance RPC仍unavailable，不算上线能力。
- 基准增加≤10个时序窗口、二进制SHA256/环境/engine-order元数据、console p99；拒绝空/重复engine和无效显式binary；Rust readiness限制4KiB/120s，初始化失败清理。24项Python测试及ruff通过。明确当前open测空transcript，mix实为90%reads，PostgreSQL必须显式一次性实例。
- medium三引擎全成功 /tmp/tofudb-architecture-three-medium.json，PostgreSQL18.6独占private socket/fsync+synchronous_commit+full_page_writes/checksums开启，结束已停止；证据目录/tmp/tofudb-pgbench.vcIuZVaa保留。SQLite6/8 mean胜、PG7/8；create1.526ms max344.95ms，append2.962ms max339.37ms。时序窗口揭示周期性303–345ms暂停，不能将WAL字节减少宣称端到端全面提升；继续checkpoint路径优化。
- 新鲜release differential+harness 161 passed/3 failed：并发legacy continuation契约在turn.event.record新增model_continuation_status及terminal replay字段，Rust未对齐。继续实现，不删差分字段。文档gate仍5项line-budget失败。
- 优化前release留存/tmp/tofudb-architecture-baseline-ke0y948p/tofu-db，SHA256 8f4aa9287e08f9737b1f22fffa1828299d32f9f568e1a2d08db0d7cddbf5e261，用于后续同条件对照。未切换生产、未提交、未恢复其他会话改动。

### 2026-09-06 (tofu-db 第二轮：segment 写合并、历史流式 checkpoint、continuation 边界对齐)

- payload segment新增≤256KiB合并写缓冲，大块旁路；128×8KiB规格132→8次write，编码字节/哈希/fsync/rename不变。history逐段构建持久化，临时编码只保留≤4MiB一段，小suffix精确容量；故障矩阵覆盖buffer flush和大块旁路。
- turn.event.record对齐无candidate时的continuation status及terminal replay字段；candidate进入有界验证，不伪装stored。原生continuation get/put/delete及持久化仍unavailable，未宣称完整支持。
- 第二轮最终验证：Rust540+12 passed，strict clippy/release通过；Python differential+harness165 passed，前述3项漂移失败已解决。回填checkpoint新增真实并发commit冲突规格。
- 最终新鲜三引擎medium /tmp/tofudb-architecture-final-three-medium.json：SQLite6/8 mean胜，PG8/8 mean胜。SQLite ratios put1.74/get5.19/create0.41/sidebar1.33/open5.19/append0.42/search1.05/mixed1.28；PG4.67/14.11/1.20/3.94/21.52/1.23/4.96/4.04。Rust create mean1.558/max377.814ms，append2.787/max308.351ms，mixed0.343/max285.465ms；search p99 12.26ms仍差于SQLite9.81ms。两轮间非隔离A/B，不归因墙钟收益；集中checkpoint尾停顿仍未消除，不能声称架构全面超越。
- 最终binary SHA256 75eafe52aed829e61a19f8e90e3e89362e7d2827de0bdd8866e1c115fbac3639。一次性PG已停止，证据/tmp/tofudb-pgbench-final.h2hbuewg保留。未进行生产cutover。后续必须继续拆分checkpoint前台工作、跨namespace写放大、回填consumer/cleanup eligibility及原生continuation持久化。

### 2026-09-06 (tofu-db 第三轮：显式owner回填消费者闭环)

- `schedule_owner_search_backfill` 现在持久调度显式tenant/owner scope；既有search worker在dirty队列排空后每轮扫描≤8个会话头，dirty标记与游标原子提交，投影继续走既有MVCC重建和exact-token ACK。无自动startup调度/全局owner扫描/新线程。checkpoint完成仅表示已入队，不表示投影完成。全局legacy RPC仍unavailable。
- 5项backfill测试+真实投影清空后恢复检索集成测试通过；continuation新增UTF8实际字节/16384消息/4096 witness边界规格通过。当前完整Rust541+12 passed、strict clippy通过，未把上一轮release性能报告冒充此轮新测。
- cleanup前置核查：legacy必须superseded_at过期且无current_attempt引用、无sync attempt_sequence引用，再处理raw archive/event/quota删除。Rust没有superseded_at持久索引，不得拿updatedAt替代。continuation持久化仍需与Python canonical witness一致、权威ordinal CAS及所有生命周期原子清理。两项未完成。

### 2026-09-06（停止生成：await/swarm 取消断链修复）

- 现场主任务在 await_agents(60s) 内收到用户 Stop 后仍等待剩余约56秒；普通swarm handler漏传取消谓词，await循环不识别取消，工具池仅在future产出后检查Stop。
- 统一v3/标准cancel_task，原子保留首次取消来源与时间、owner及task identity fence、terminalizing拒绝；停止入口直接通知同owner会话swarm，覆盖后续turn未await情形。正常父turn完成仍允许后台swarm存活。
- await取消独立于超时/driver drain，100ms检查，快速返回分支也优先取消；pending取消下传。工具池100ms FIRST_COMPLETED观察Stop，保留done-but-uncollected真实receipt，未完成调用正确结算aborted。子provider抛异常和返回client_aborted两种形态均归取消，保留usage/部分回答，不进入失败重试。
- 复审拒绝将原有pool timeout改成无界drain：恢复timeout有限返回；显式Stop保留协作worker的资源所有权直到退出。非协作Python handler或阻塞callback仍无强制秒停保证；timeout遗留线程仍能触及共享状态，这是保留的既有隔离缺口，不能把wait=False解释为线程已退出。
- 验证：组合9文件94 passed/35.24s；补跨turn/owner及await快速返回后41 passed/7.84s；补返回式provider中断后16 passed/2.76s；相关ruff通过。结算邻套3个过时事件样本改为现行合法字段，未放宽生产校验。
- 文档：TURN_SETTLEMENT.md增加明确取消合同，task_engine.md引用。全仓文档门禁仍有5份其他owner文件行数超限；swarm邻套另有既有stub task_id/schema预算失败。共享工作区其他修改均保留，不宣称全仓绿。
- 生效：本轮纯Python变更，未重启承载中的服务、未提交；尚无上线后点击Stop端到端复测。

### 2026-09-06（后台命令：真实分发覆盖、pending 误纠偏与显式入口断链修复）

- 现场：会话 `mtptl924v307i4` 的推送于 22:21:37 因 operator steer 转后台（`bg_23f221871873`）；22:21:51 intent-stall 把 `exitCode='background'` 当失败，随后查询命中 `Unknown project tool`。本次未重跑推送，读取范围未发现该命令完成入队记录。
- 分发：project schema 分组不再充当全部执行 owner；通用 handler 排除 check/kill，保留 builtin/plugin 注册覆盖契约。新增真实 executor 分发回归，不再只验证 schema 存在。
- 状态：精确后台哨兵不算退出失败，真实失败兄弟仍触发；纠偏文案不再把执行失败说成从未执行，提示检查副作用后恢复。命令卡片不再将后台回执显示为失败或已结束。
- 生命周期：显式后台复用 steer 既有 durable queue + inbox completion lane，接通 project/standalone 两入口；远程 worktree 不支持时明确拒绝。查询/终止按 owner 隔离；轻量执行 task 不保留父 PID/消息图，完成释放 task/异常帧，线程启动失败归还槽位，容量仅淘汰已完成项。进程内 registry 不提供跨重启运行恢复；durable enqueue 失败沿用日志降级，显式结果仍可查询。
- 验证：组合 148 passed；ruff、scoped diff check、npm run check:frontend（含类型/构建）、verify:frontend 全通过。全库文档门禁仍有其他并行文档超行预算，本轮 task_engine 已压至预算内。代码未提交，未强制重启后端；新前端 bundle 已构建发布。

### 2026-09-06（重启后近期待验收项回归：批量问答递归修复、工具预算修复、上线目录核验）

- 本轮范围：近期 restart-deferred 的目录退休、批量问答、后台命令/工具别名、压实与 Stop、append/extract、TeX/研究恢复及 tofu-db 正确性；保留共享工作树，不重启服务、不提交、不调用付费模型、不修改生产数据。
- 修复：ask_human 原始 questions≥2 但归一后不足2项时，回落单问仍携带 questions，反复重入导致 RecursionError；先复制参数并移除批量判别字段，再投影首个有效问题。新增一项/零项有效问题两针先红后绿，14项问答测试全绿。todo_write schema 329>325 token预算失败，删除重复 max24 散文，maxItems=24 权威约束不动，schema/兼容31项复绿。infra_runtime 文档失效 test_app_lifecycle 路径改为真实 quart 生命周期套，并保持全部不变量压回行预算。
- 已确认生效：只读 live /api/v1/model-routing 中 astra-pro、sol-pro、terra-pro、luna-pro、deepseek-chat、deepseek-reasoner 六个精确身份均为0。前端69项聚焦测试、check:frontend、全量 typecheck、verify:frontend通过；生产构建按owner脚本派生。
- 验证（不同命令有重叠，不相加作独立总数）：根侧59项压实摘要/abort/append/extract/timeout、31项TeX/研究协议与恢复（含真实本地编译）、256项压实/中止传输/安全抓取邻套、36项restart/生命周期、46项Stop/swarm取消均通过。工具侧初始170项通过；邻套249通过/1预算失败，预算修后专套31通过；相关ruff通过。
- 存储：基准环境泄漏修复已在共享工作树中，未重复改写；24项benchmark +10项进程边界通过，内存负控恢复旧非隔离实现使4/4隔离针失败，确认规格有效。Rust经CARGO_HOME定位工具链，2 build jobs/2 test threads，541单元+12集成=553通过，strict clippy退出0（FUSE增量缓存hardlink回退警告）；8KB页打包不动，未重跑三引擎性能对照。data_tier补基准启动隔离及临时Postgres规则。
- 仍未全仓绿：docs gate有API_CONTRACT 1232>1200、STORAGE 1245>1200、auth_providers_billing 230>220、remote_execution 259>220；architecture gate拒绝仍存在的routes/turns_v2.py退休HTTP面；cargo fmt --check报告已有Rust格式差异。没有以删并行工作、放宽门禁或格式化无关文件掩盖这些失败。pytest NC guard持续提示已有message_queue/scheduler/styles脏改，不作为本轮功能正确性的额外证据。
- 限制：没有真实浏览器提交批量表单/点击Stop端到端验收；本轮新Python修复尚未重新启动加载，不把用户之前的重启当作本轮修复已上线。后台注册表不具备跨进程恢复；存储结构性性能项目与4KB分裂硬化仍不在本轮范围。没有会话rollout路径，未运行session audit。

### 2026-09-06（后台命令结果交付：等待让出、持久证据展示、ACK 安全与批处理）

- 决策：保留 durable queue 的恢复/租约/owner/幂等能力，执行结束、交付未确认、模型消费分开；不新增命令状态权威或改写历史工具回执。
- await_agents 对 user-steer/background-command 在既有约100ms检查片内让出，pending_input 不消费 inbox/子结果、不取消子任务，Stop优先；其他阻塞兄弟工具/在途模型不强制抢占。
- BackgroundCommandCompletion 同源透传 queue list/snapshot、fresh-turn input、mid-turn receipt；原卡显示 Moved to background，完成卡显示 finished/delivery unconfirmed、Cancel delivery、转义可展开2000字预览与截断提示。修复 _bgCommandInjects 归一化丢失、legacy截断XML和未使用wrapper造成的假截断提示；模型原始内容不变。
- 安全：fallback错误字典不再触发ACK删除未消费结果，必须显式verified provider completion；失败/abort保留恢复证据。consume_peer对尚未到达twin加queueId fence，clear/revive保留，最多16384 slot/id，FIFO/重启后保护结束。
- 发布：owner/conv/command派生稳定queue UUID；冻结payload的瞬态存储失败最多3次尝试、总sleep≤2s；真实sidecar lost-ACK验证只留1行。显式后台查询新增delivery_status；首个durable commit前仍无崩溃恢复保证，steer-handoff重试耗尽仍以日志暴露。
- ACK：queue.injection.ack单RPC、零queue.list，≤256 IDs，一事务删除合格未绑定peer/bg行、renumber一次、发布removedQueueIds；保护人类/无关/linked/cross-owner行。registry40及目录生成同步；SQLite验证，未跑PG，实验Rust仅元数据非实现。
- 验证：集成136 passed；最终queue/lease/presentation邻套38 passed；fixture lint修正后batch6 passed；scoped ruff/diff-check、storage catalog freshness、npm check:frontend（生成/类型/构建）、verify:frontend通过。架构门禁仍报退休routes/turns_v2.py；文档门禁仍4份既有超限，task_engine保持220行。未声称全库绿，未做真实浏览器/线上重启后验收。
- 生效：前端bundle已构建发布；Python与新增sidecar操作需协调重启后端/sidecar，未强制重启、未提交。消费与持久ACK并非跨模型事务，崩溃不确定窗口可重投，删除不实exactly-once承诺。无rollout路径，未运行session audit。


### 2026-09-06 (ToolScript：单次模型往返依赖链与搜索参数校验)

- `catalog.search` 支持对象参数并保留位置参数，复用 search_tools schema 的类型/边界；非法参数在回调前返回 invalid_search_arguments + retry_hint，不再把对象字符串化后用默认 8 条搜索。
- 网关 schema 与稳定上下文说明以结果依赖的串行调用链为主要用途，明确 status/output/error、JSON 文本解析、失败及截断前置结果处理；独立调用用 batch，链内独立分支可 parallel。移除本地 handler 未被调用、且与实际权限契约相矛盾的 hosted eligibility 检查死代码；普通权限及审批不变。
- 新增参数边界规格 82 项及真实网关/派发/回执依赖链规格 16 项（仅替代叶子 I/O）。并行聚合返回数字的测试发现 program_output.result 原先拒绝数值/数组/布尔值：修正事件权威类型为合法 JSON 类型并重新生成 TS 契约，覆盖 0/false/null/数组等收尾。
- 验证：参数/依赖链/事件/agent-core 114 passed；此前网关/本地执行/搜索/上下文 196 项通过（分轮验证）；ruff、补丁空白检查及事件生成 freshness 通过。文档全局检查受未涉及的 API_CONTRACT、STORAGE、auth_providers_billing、remote_execution 行数预算阻塞。未重启服务、未提交或覆盖其他工作树改动。
- 最终合并回归（上述九组规格）：310 passed in 10.85s，无失败。

### 2026-09-06（模型生成阶段与思考内容文案分离）

- `llm_thinking` 不再统一覆盖为“推理中”：保留 detailKey/detailArgs 的生成回复、分析工具结果及轮次，兼容 raw detail/label，缺省使用“正在生成回复…”。该阶段不再使用思考专属样式或字符计数；`thinking_active` 复用思考块的“思考中…”文案。
- 渲染契约同步；补生成阶段本地化、轮次、旧文案与空阶段回退、思考样式/计数边界测试。阶段 i18n 与会话 surface 91 项通过；npm run check:frontend 通过并构建发布静态资源。

### 2026-09-06（科研工作台分段迁移：独立前端与事实进度初段）

- 科研四个旧 Paper owner 迁入 `frontend/src/features/research/`，独立 lazy entry，复用既有任务传输、阅读器与工作区 CAS。新增稳定 DOM patch，移除虚构百分比，显示提交、重连和停止请求错误；终态推送不被迟到 running 快照回退，final 成果边界不再直接解绑任务订阅。
- 新增 `research_progress_v1` 机器契约、TS 生成器和后端事实进度初段，接通发现/逐篇采集；仍未完成带 run/sequence/业务进展时间的完整投影、全阶段即时成果与证据版本历史。这是分段落地，不能视为已接受完整计划验收完成。
- 引擎在停止竞态后以任务权威结算写恢复清单，避免将迟到成功结果写成 done。三个无模型停止规格通过。工作区动作离开时清计时器，保留句柄供同页面返回恢复，迟到响应受页面代际隔离；刷新后动作重接仍待完成。
- 主验证：27 工作区/历史权限 + 60 科学有效性/确认 + 42 发现/修订 + 31 提交/显示/恢复 + 3 引擎停止测试通过。真实 Chromium 390/1280 宽、200 文献、30 次更新两例通过，保留焦点/选区/展开/滚动；仅为组件浏览器验收，非完整长任务端到端验收。科研前端独立检查 3 项及 TS 检查通过。
- 仓库基线仍有非科研失败：四份文档超行数、退休 v2 路由遗留；frontend_vite_domains 两项源扫描断言失败（admin 资产标签与 sessionStorage 误识别）。未回滚其他工作树改动、未触发真实实验/发布、未重启服务。

### 2026-09-07 (空 live phase 不再误报 workerStarting)

- **根因：** conversation-view-model 的 running + 空 livePhase 回退写死 workerStarting；重载丢失瞬态阶段或 delta 正常清空阶段均被误读为刚拿到执行名额。运行状态只证明已进入执行，不证明仍处于启动阶段。
- **修复：** 空阶段改用 waitingWorkerStatus；真实 worker-entry phase 原样保留；输出中的 text/thinking 尾块继续折叠空占位。未放宽资源限制、未重启服务。
- **验证：** conversation Surface 72 passed；check:frontend 全链（合同/类型/构建）通过，verify:frontend 通过，新 bundle 已发布。文档检查仍有 API_CONTRACT、STORAGE、auth_providers_billing、remote_execution 四个无关文件超行数预算，未越界修改。浏览器需刷新加载新 bundle。

### 2026-09-06 (跨轮上下文重放与双压缩根修)

- 普通 Turn command 的 `excludeLast=True` 仅排除空输出目标，不再被 continuation eligibility 当成 unsupported mode；非空恢复目标、真实 resume 载荷、策略/历史 witness 校验仍保留。重放规格改用真实 command-service 配置。
- 硬窗口溢出在 L2 入口及调用处绕开经济否决；自动收敛和应急截断共用最终 dispatch admission 预算源，保留工具/合成消息余量，不再按百万窗口的 60% 留下 600K。
- 经济与 retry witness 改读有 task/model/provider 作用域的当轮缓存观测；历史 cache-read 不再授权当前经济决策。成功压缩清除旧前缀观测。日志用 supplied count，receipt 区分经济否决、无可折叠内容及摘要失败。
- 不修改 durable transcript、不重启服务；Python 改动须受控重启加载。全局 gate 的四份文档行预算及 retired turns_v2 路由阻塞属于其他 worktree 改动，未覆盖。

### 2026-09-06（run_command 显式工作目录错误）

- `mtpzgflo9k1orh` 前两次调用显式 `working_dir` 带错误的 ` common/root?` 后缀，第三次改为真实项目根目录；不是相同环境下的瞬时故障。原始参数已含错误，更深层生成原因尚无证据。
- 修复边界：无效目录须明确拒绝，不能猜路径、自动创建或悄悄回退执行；复用 not-run 状态。既有 sticky cwd 已校验存在性，不另造状态机。命令卡片展示传入目录，不把请求参数当成实际运行证据。

### 2026-09-06 (swarm 稳定性根修：准入等待心跳化 + AbortedError 落 CANCELLED 不重试 + 面板计时器容忍时钟偏差)

- **起因（用户任务）：** swarm 代理频繁 aborted/报错，要求对照 Codex/Claude Code/OpenCode 的 harness 方案把 swarm 推向完善。排障期间本会话派出的 3 个研究子代理全部被 `AbortedError: swarm execution cancelled before admission` 取消——病灶自证。
- **根因（两处）：** ① 准入背压被误判为停滞——agent 在线程池拿到执行权后先过 RateLimiter 信号量再过进程级 OwnerFairExecutionGate，排队期间零 LLM 轮次零工具调用，liveness beacon 逐渐陈旧，900s 停滞墙把整个等待中的 wave 误杀（日志 ABORT agent=logs 族）。② AbortedError 从准入等待抛出后落入 `_run_one` 的 generic `except Exception`：打 AGENT_RETRY 日志、带重试撞同一个 gate、最终把取消错标为 FAILED 而非 CANCELLED。
- **修法（后端三处 + 前端一处）：** ① `scheduler.py::_run_one` 用 `thread_progress_sink(self._beacon, spec.id)` 包住整个「rate_limiter.run_agent / agent.run」调用段——排队等待循环据此心跳 beacon，queued ≠ silent；`except Exception` 分支首判 `isinstance(exc, AbortedError)` → 置 CANCELLED + break，绝不进 on_retry（abort flag 是粘性的，重试必死在同一 gate）。② `rate_limiter.py` 信号量等待循环 + `execution_gate.py` 条件变量等待循环各加 `notify_tool_progress('admission_wait'/'gate_admission_wait')`（无 sink 时 no-op）。③ 前端 `streaming_swarm_panel.js::_recoverSwarmAgents`：startedAt 由服务端时钟铸造，客户端时钟落后（VM 漂移/休眠恢复）时被旧 `<= Date.now()` 校验整条丢弃、运行中 agent 计时器消失——改为 5 分钟偏差窗内容忍并 clamp 到本地 now（计时器从 ~0s 起走），真错误量级（epoch 秒/双重转换）照丢。
- **规格：** 新建 tests/swarm/test_swarm_admission_liveness.py 3 针（gate 排队心跳保 beacon 新鲜且 swarm 级 is_making_progress 为真；AbortedError→CANCELLED 且 on_retry 零调用、max_retries=2 下不重试；真实 RateLimiter 准入等待下 beacon 沉默 <1s）；test_frontend_swarm_timer_reattach.py 重指 NEUTER 锚点 + 新增偏差窗 clamp/超窗丢弃两案。
- **验证：** 新规格 3/3；test_swarm_liveness/agent_start_clock/stop_chain/resource_budget/timer_reattach 71 passed；test_swarm_async 65 passed；ruff 净；check:frontend 全链绿（runtime 166 sections + vite build + verify，新 bundle main-BEpJiw6z.js 已发布）。两处失败（blank_ids task_id stub 漂移、resource_wave_limit maxItems 8≠2）经 stash 隔离证实为工作树其他 WIP 的既有失败，与本轮无关。
- **生效条件：** 纯 Python 需重启 server.py；前端硬刷新。
- **横评对照（立项候选）：** Codex 的 spawn slot 预留/SQLite job 崩溃恢复/子代理审批上浮，tofu 已有对应物（resource_policy 配额、_rehydrate、ask_human）；OpenCode 的 `retry-after` 头解析与 SessionRetry 错误分类退避在 lib/llm 缺席——候选跟进项；doom-loop 检测 lib/swarm/agent.py 已有。

### 2026-09-07 (后台活动统计限定当前对话)

- 后台活动芯片、标题提示和浮层统一按 activeConvId 与记录 conv_id 精确匹配；newChat/loadConversation 在更新活动对话后立即重投影缓存并关闭旧浮层，不等待轮询。迟到快照按当前对话过滤，无归属/headless 记录不显示。
- background_commands 注册表保留最小 conv_id 并在快照输出，进程结束释放 task 后归属仍可查询；API 保持 owner 作用域，展示层不再跨对话汇总。契约更新 docs/contracts/API_CONTRACT.md §2.19。
- 验证：后台活动 UI/API 9 passed，覆盖同步切换、新对话隐藏、迟到响应隔离和命令归属；npm run check:frontend 与 verify:frontend 通过。前端已构建；Python 新快照字段需服务重启后用于新启动命令，未在本次操作中重启运行中的服务。

### 2026-09-07（系统干预统一展示与自动提示收敛）

- 删除连续六轮单工具效率提示、连续三轮只读 PTC adoption 提示及普通循环中的 serial gateway 激活；成功串行调用不能证明可并行或无进展。保留历史诊断读取和显式实验生命周期兼容接口。
- 保留真实重复失败、空操作、覆盖读取与未消费 artifact 保护；提示和停止统一经 `record_injected_note` → `system_note` → turn block。重复成功验证保护改为 `incomplete`，不再假装用户任务完成；后台任务接受的正常交接不变。
- 扩展 canonical TurnSystemNoteSegment：兼容旧 kind，新增通用 kind 与 prompt/continue/tool-policy/stop 动作、reason、稳定身份。原文/原因有界，默认折叠并按纯文本安全渲染，不声称 context appended 等于模型已消费；修复投影重建丢 note、重复 ID 和历史无 ID note 相互覆盖。展示记录不进入 assistant/tool replay。
- TODO 续跑改为核对未解决状态、尊重结构化人类交接与审批；意图停滞保持一次上限，修正缺右括号的结束标记解析。预算提示只报估算、不催促结束；预算、流中断恢复、明确 commentary 续跑也接入统一记录。
- 验证：后端循环/搜索保护/流恢复/相位/预算 192 passed；TODO/意图/预算 76 passed；前端会话与 segment 相邻组最初 198 passed/2 旧标题断言失败，改为校验生产者原文后展示组 20 passed；投影/预算/流恢复最终 83 passed。新增 DOM 测试覆盖折叠、动作差异、恶意原文与重复重绘。ruff、conversation-sync/i18n 生成校验、TS typecheck、build:frontend 和 verify:frontend 通过。
- 文档同步 task/context domain、RENDER_CONTRACT、TURN_SETTLEMENT、LLM_COST_OPTIMIZATION。make docs-check 仍被本次未改的 API_CONTRACT、STORAGE、auth_providers_billing、remote_execution 四文档原有行数超限阻挡；不擅自压缩其他工作。未重启服务、未提交 Git；Python 运行中进程需重新加载才能采用新行为。

### 2026-09-07（LongCat Flash harness：显式兼容配置与运行时异常修复，未发布）

- 新增 `--harness-profile off|longcat_flash`（默认 off）及请求配置 `harnessProfile`；直接工具暴露复用既有 registry/full wire 配置，不复制 schema、不改变权限。LongCat 泄漏包只检测并要求原生工具重发，不猜测 XML 参数或直接执行。
- 终止恢复接入真实 analyser：空 stop / 无 payload 工具终止 / 明确残包共享任务级最多两次恢复，继承已耗异常预算，保留操作回执，耗尽写 typed abnormal_stop 并禁止整轮重放。流中断、语义停滞与人工等待保留原有优先级。独立审查发现的优先级回归已由相邻测试复现后修正。
- 路径误拒根因：任务作用域注册根与绝对路径全局校验不一致。项目 dispatch 绑定可信 registry scope，未知及显式缺身份 scope 均不借全局根；保留 realpath containment 与越界拒绝。
- transient/disableModelFallback 压缩采用任务累计默认 60s、上限 120s 预算，严格原模型，429 次数有界，失败锁存并保留历史；放不进上下文时显式 ContextCompactionError。独立审查仍发现桌面 relay 等待 headers 不检查取消、可等约 60s 的既有传输缺口，未修，不能宣称所有路由硬超时。
- 验证：八套组合 133 passed / 7.70s；末次残包引用边界调整后 profile+terminal 39 passed / 3.26s。定向 diff --check 通过。文档门禁仍因 API_CONTRACT/STORAGE/auth_providers_billing/remote_execution 四份既有超行预算失败。
- 未发布、未提交、未重启、未发车。工作区大量并行改动尚未隔离成干净发布源；画布 359059 实际指向 tofu-kernel-v3，而 v0.17.2 pin 更新在 tofu-kernel，发车必须复核实际远端/安装 commit。13 题候选清单保存于 autoharness/work/tofu-harness-canary.json；精确题集选择尚未接通，EDS=13 仅取前13题不能替代。未产生新分数。

### 2026-09-07 (后台结果取消投递：v3 queue-only 取消支持)

- 用户报告后台结果待投递时，立即插入文本报 Internal error、取消投递报 Queue removal failed。取消根因已确认：v3 `turn.queue.cancel` 拒绝没有 Turn/Attempt 关联的后台结果行。
- 修复 Python Sidecar 取消权威：仅 pending 后台结果可取消，保留 owner/lease 围栏，原子删行、重排与 revision/replay；提交后清理并 fence inbox twin。前端仅在返回 inputTurn 时恢复草稿，后台结果取消不清空用户输入。
- 验证：取消与 inject-mode API 回归 11 passed；后台完成投影/ACK/injection 邻套 16 passed。新增实际 API 场景验证 steer 不丢待投递后台结果。
- 限制：立即插入的历史 500 仅记录 database_protocol_error，当前隔离 Sidecar 场景未复现，未宣称修复；需要失败请求的具体存储错误证据继续定位。

### 2026-09-07 (长回复立即插入 500：整条 Turn 撑爆命令回执)

- 补查 server-console.log:119512/119517，两次失败均为 `turn.steer.commit: Command response is too large for a receipt`，与 10:44:47 的请求 2ffae0-4237 对齐。故障回复 57 工具轮只读重建回执 1,222,866B、zlib level1 563,354B，超过 64KiB，真实数据复现同一异常；后台结果排队不是触发根因。
- steer 命令仍需 receipt，只返回有界 injection/turn identity 与提交 revision；同步捕获同事务写有界 conversation.activity snapshot invalidation，不把全文或增长的 injection patch 放进 receipt。完整回复在 inbox wakeup 后按 owner 查询；查询失败仍 ACK steered、不带 latestTurn，由已提交同步通知恢复，避免已接受文本被误报失败。
- 新实际 Sidecar/API 测试用 1.28MB 不可压缩历史验证成功、小回执、重放不重复写、变更 payload/owner 拒绝；故障注入验证后提交查询失败仍接受并送入 inbox。回执上限未改，未动真实会话数据/未重启服务。
- 验证：API 插入与取消 13 passed；存储契约/进程边界 148 passed,1 skipped；插入消费与 exactly-once 20 passed；receipt codec 7 passed；生成契约更新，定向 diff --check 通过。PostgreSQL 未启用；既有文档行预算超限仍在。

### 2026-09-07 (conversation-scoped project restoration)

- Project restoration now has one typed owner (`conversation-project.ts`) shared by navigation and boot. Empty composers follow saved primary/extras/read-only policy; drafts retain their selected context. Generation/selection checks reject stale hydration and restore responses, and full matching bindings avoid redundant setPaths.
- Navigation and generic toolbar capture no longer persist global project state. Restore failures retain saved bindings; server responses cannot substitute another primary. Send captures project context before hydration, preserves intentionally projectless drafts, and remote restoration retains read-only roots.
- Verification: typed owner test passed; retained integration suite 27 passed; neighboring project/dial suites 39 passed before the last added edge-case tests. `npm run check:frontend` and `verify:frontend` passed. `make test-frontend` completed build/checks but pytest collected zero tests (exit 5), so it is not a passing broad-suite result. Documentation gate reports preexisting line-budget violations in API_CONTRACT, STORAGE, auth_providers_billing and remote_execution.

### 2026-09-07 (conversation-scoped models and presets)

- Model/preset, provider reference and thinking depth restore only with an empty composer. The typed model controller fences delayed hydration against newer selections/navigation. Navigation/general settings capture never overwrite model bindings; explicit model/depth picks and send commit identity.
- Catalog refresh no longer substitutes random/default models. Legacy selection clears stale v2 references; authoritative null modelRef clears cached identity. Send captures identity before hydration and uses its committed model in resolver overrides.
- Verification: 53 model/project adapter, cold-hydration and authoritative composer tests passed; earlier model dropdown/project baseline 75 passed. Frontend gate rerun after final navigation fence.

### 2026-09-07 (Swarm completion publication, terminal panel fencing, and monitoring)

- Canonical child results precede fallible snapshot/UI observers; observer errors no longer lose completion, duplicate callbacks are idempotent, and inbox insertion is fenced against the bounded delivered-result ledger under the session lock, including recovered notifications. Await/get delivery before enqueue cannot leave a late twin. This is an in-process guarantee, not crash-safe exactly-once provider delivery.
- Frontend rejects telemetry naming a durably settled execution before provisional-launch fallback; new execution IDs still support pre-settlement and completed-parent launches. Repeated tool-call IDs bind rich segment data by ordered occurrence inside attempt/task scope without mutating durable inputs.
- Background activity owns one single-flight poll/timeout, zero hidden-page polling timers, composition teardown and inert late responses. Unchanged popovers update elapsed leaves without replacing rows; tests execute authored source.
- Structured `swarm.driver_done` outcomes/counts/reason codes and terminal presentation distinguish cancellation/stall from success. Fixed stale test fixture identities, retaining production ownership guarantees. Updated task-engine/render/logging/frontend contracts.
- Validation: full Python swarm suite 273 passed; combined activity API/chip/completion regressions 15 passed; Node authority 7 passed; neighboring source suites 28 passed (overlap, not additive); scoped ruff/whitespace passed. `check:frontend`, including production Chromium boot canary, and `verify:frontend` passed (`main-Dkn41daP.js` at verification). Existing large-chunk warning remains.
- Boundaries: shared checkout contains concurrent WIP. Documentation gate retains the same four pre-existing over-budget files (API_CONTRACT, STORAGE, auth_providers_billing, remote_execution), no new failures from these docs. Live logs confirm all three repair agents completed but the resident Python worker still emitted the old summary. Backend restart/adoption and real production end-to-end replay remain unverified. No restart, resource-limit change, commit or live-log deletion performed.

### 2026-09-07 (科研记录数据库持久化 + 实时论文工作台)

- 科研启动前及每篇采集/阶段业务回执前写入现有 Sidecar `record.*`，owner+task 隔离，快照 200 篇/8 MiB 上限、无 TTL；不持久化 token 心跳。保存论文元信息、进度及已确认 pipeline checkpoint，数据库失败不能 clean success。
- 新增 records 列表/详情/显式 resume API；内存任务失效后显示 interrupted 而非假运行，恢复使用既有 production recipe；任务级 result 冻结，避免相同方向多轮产物混淆。索引与快照两次提交非原子；不保证仅凭数据库自动重启执行，revision scratch 不在备份范围。
- 前端新增 live-library.ts：论文卡片为主区域，标题/状态/原文入口实时出现，计数降级辅助、移除 paper_done 裸字段；科研首页从数据库记录重开，支持显式继续中断任务。响应式主题变量、200 卡上限、用户标题转义。
- 验证：科研域并行快照 253 项通过；最终记录/接口/真实 Chromium 长标题窄屏测试 18 项通过；任务结果冻结和终态 8 项通过，ruff 净。文档门禁存在四处既有超长文档，不扩大本次任务修改。
- 最终前端新增行为规格 4/4，`generate:runtime`/`generate:styles`、`check:frontend` 全链、`verify:frontend` 全部通过，已生成并验证新的静态构建；后台 Python API 需服务重启后生效。

### 2026-09-07 (Skills 检索：词边界、有界补召回、验证故障可见)

- **根因：** 本地已安装奖励可让无关候选进入结果，名称前缀把 Frida 命中 Friday；在线完全信任上游顺序，整句空结果无恢复；验证失败丢失结构化错误后被过滤成普通零结果，卡片统一显示 ready。
- **修复：** discovery 增加实际词项交集准入及分隔符分词；在线在验证前抑制单词前缀噪声，保留无词面重叠的语义召回；四词以上空查询最多用两个原查询锚点回退，首次非空即停止，最多三次搜索、十二次验证，搜索错误不回退。成功验证缓存五分钟，失败进入三十秒有界缓存并保留错误及 Retry-After。模型结果展示来源/候选/过滤/验证/回退信息，handler badge 区分命中、零结果、降级。安装确认、验证与 owner 边界不变。
- **验证：** skills 全域及权限/隔离邻套 155 passed；追加预算与短失败缓存规格、Skills/Memory 前端边界 15 passed（其中9项与前套重叠，共161项不同规格）；本次文件 Ruff、git diff --check 通过。全仓 documentation gate 因 API_CONTRACT/STORAGE/auth_providers_billing/remote_execution 既有超行失败，architecture gate 因已退役 turns_v2 路径等其他范围问题失败，未改动它们。未重启正在运行的服务；无可用会话审计路径。

## 2026-09-07 — Goal phases share normal Turn streaming

Goal now admits each sequential visible phase before dispatch and settles the
same child attempt. Child writes remain fenced by the root executor; stop from
a child resolves to the root, and cancellation preserves partial phase output.
Completed phase sync sends an incremental suffix. Canonical Goal typed inputs
carry the original request, latest full worker handoff and latest full review,
without duplicating accumulated history. Terminal cleanup releases the one live
projection baseline.


A rendering follow-up fixed the completion handoff for those phases. Bounded
visible messages had only preview rounds, so durable sync now fill-restores
canonical arguments, result content, timing and task/attempt identity from the
live phase by stable call ID before assembling segments. In the browser, a
terminal VU payload that omitted `toolRounds` previously discarded that rich
state; retaining it initially introduced a second bug by rebuilding and
therefore overwriting the backend's terminal segment chronology with stale live
blocks. Settlement now keeps backend segments authoritative while independently
settling/backfilling rich rounds. This restores real tool names, result cards
and Request Inspector links instead of metadata fallbacks such as Project,
Checklist and `sync`. Focused reducer, Vite Surface, Sidecar Goal-phase,
inspector/canonical-round and rich-outcome suites cover both paths.

Browser replay exposed a second fault: normalizing a projection before deriving
its wire patch could omit newly introduced segment placeholders. Patches now
start at the actual preceding wire document; first nonempty text/thinking emits
a structural block. The captured Sidecar stream renders live reasoning and text
through the existing reducer and Surface without pending events or DOM identity
replacement. Owning contracts: `docs/contracts/CONVERSATION_SYNC_V3.md`,
`docs/TASK_EXECUTION_MODES.md`, `docs/ORCHESTRATION_PRODUCT_SURFACES.md`.

Font-enabled Chromium profiling also found that advancing the 80-Turn window
moved all 79 retained subtrees. Retiring outgoing nodes before cursor placement
now costs only one removal and insertion; a MutationObserver regression pins
that budget. The synthetic 128-Turn / 512-commit long-text fixture, with full
application CSS, fonts and the live tail visible, has 7.5 ms render-commit P95
both before and after (maximum 12.8 to 10.7 ms), with 80 mounted turns and stable
active nodes. Existing CSS content-visibility masks much of the layout cost;
the much larger improvement observed without global CSS is not an application
latency claim. The 64-iteration context fixture reduces the final worker input
from 936,576 to 62,093 characters and cumulative visible-sync body transport
from 66,048,000 to 1,024,000 characters; durable history remains complete.

Verification: 97 focused backend tests, 73 Surface tests and four isolated
PostgreSQL parity cases pass; `npm run check:frontend` including production
build passes. Broader gates retain unrelated failures in legacy route removal,
schema/adoption stubs, the Goal command busy stub and existing documentation
line budgets. No live model run or running-server restart was performed.

### 2026-09-07 (tofu-db 故障规模矩阵补齐：correlated 多 sync 丢失 + extended rotation 认证通过)

- **完成（全部验证绿）：** ① `DeterministicVfs` 支持多故障规则（`arm_faults`，`faults: Vec<FaultRule>`，单规则 API 不变）；② `engine.rs` 新增 `every_correlated_pair_of_lost_commit_syncs_recovers_a_complete_prefix`——对 commit+checkpoint 全程 37 个 sync 点枚举全部 666 对 DropSync 组合：653 对完整恢复（事务+块+历史快照齐全，恢复后可继续 commit/checkpoint），13 对 fail-closed（open 拒开或 read 拒服务），0 撕裂投影；期间发现并钉死两条语义——快照长度不符仍是硬断言（撕裂=违规），manifest/块引用缺失的 read-time 拒读归类 fail-closed（与 open-time 拒开同教义）。③ `extended_rotation_chain_and_window_faults_recover_complete_prefixes`——12 轮 commit+checkpoint 链 + 每窗口单 sync 丢失恢复完整前缀。④ `backup_gc.rs` spill 全预算规模测试（1 GiB 排序 mark run 路径，ignored 为既定大规模认证）。
- **性能实测（medium 规模 e2e vs sqlite，`scripts/tofudb_perf_compare.py`）：** 8 负载 6 快——search 2.85×、open 6.11×、get 5.00×、put 1.94×、sidebar 1.56×、mixed 2.09×；2 个写突发负载更慢——conversation_create 0.60×、turn_append 0.55×，且 p99 尖峰 ~18ms vs sqlite 1.1–2.5ms，指向 checkpoint/rotation 屏障延迟（正是 barrier-latency gate 的证据目标）。注意 perf 脚本自带纪律：仅测量工具，CI 不得断言墙钟阈值。
- **全量回归：** `cargo test --lib` 564 passed / 0 failed / 1 ignored。
- **文档同步：** `docs/STORAGE.md` 与 `packages/tofu-db/README.md` 认证状态已更新——correlated 多 sync 丢失与 extended rotation 从「缺失」改为「已认证」。
- **上线判定（按 STORAGE.md/AGENTS.md 自身 gate 清单，仍未达标，不可接选择开关）：** ① 块加密缺失（STORAGE.md 明示）；② barrier-latency 优化仍是认证 gate（上述两个写突发负载 + p99 尖峰即其证据目标）；③ 迁移/回滚 gate——migrator 仍在 `tofudb/integrate` + `tofudb/par-mig` 分支未合入 master；④ AGENTS.md 明令：全部 gate 通过前任何启动路径选择 tofu-db 即契约违规。

### 2026-09-07 (研究工作台设计原型：文献列表去卡片化 + 年份/引用排序与会议溯源)

- **交付范围：** 仅更新 `designs/research-workbench/` 独立设计研究，未改生产 UI、API、存储、任务或真实文献。合成数据始终显式标记，不伪造外部核验链接。
- **交互：** 证据页默认展开横向空间并收起上下文；文献列表按行展示标题/研究关系、年份、会议与录用、引用数、阅读状态；共享同一来源选择的证据比较视图保留预算、覆盖与限制。原生表头按钮与排序下拉均可按年/引用双向数值排序，空值置底，0 保留，同值按标题/ID 稳定排序。会议/角色/搜索组合过滤；顶会范围可在本次演示调整，Workshop 不继承主会标签；溯源面板保留示例提供方与核验日期。筛选/排序不改纳入范围，改来源仍触发原有下游复核。
- **架构检查（只读）：** `AcademicDiscovery.semantic_record` 已读 year，但 canonical corpus merge、harvest 与 `records.persist_record` 的公开 paper projection 未承接；record schema 无 year/citations/venue。现有词面相关性排序没有入库阈值。未来应走现有适配器→身份合并→采集事件→耐久 projection→typed UI，不另造前端元数据权威；规格已记录此生产缺口。
- **验证：** `node --check` + hermetic Chromium `verify.py` 全绿：6 视图 × 5 宽度、既有状态/审批/来源复核、新增数值排序/缺省/键盘 aria-sort/组合筛选/顶会配置/溯源/手机横滚；0 page errors、0 网络请求。1440px 列表实测容器与 scrollWidth 均 1156px，四行高 108–109px，圆角 0。`package.py` 重建 104723-byte portable HTML 与 15 图画廊；生成 HTML `#evidence` 直接加载和引用降序 smoke 通过，无远程请求。截图读取仍仅返回编码负载，未声称完成视觉或屏幕阅读器人工签收。

### 2026-09-07 (edit_file 去掉纯插入形状拒绝：精确替换优先，语法提示按最终批次)

- **裁定与根因：** 用户授权移除 `replace(anchor, anchor + new)` 的 `pure insertion rejected` 写法门槛；该形状不等于重复落盘错误，转为 line insert 会补换行，行内 `1→10`/`x→x + 1` 不等价。参数字符效率不再冒充写入正确性。
- **实现：** 本地 `_ops.py`、桌面 `_project.py` 删除 wrap gate、检测器及旧开关入口；replace 保留完整内容与操作身份，支持前缀/后缀/双边锚点及有意复制。精确匹配保留 CRLF 和无末尾换行；insert 保留既有 universal-newline 及插入专属回显修复。schema 前置合法替换语义，使搜索截短仍可读；现有 kimi-k3 计数 381→378，未抬预算、未新增逐次教学提示。
- **语法反馈：** unified edit 每文件比较原始文本与最后成功提交结果一次；中间坏、末态好不警告，末态坏只报一次，原本坏不新归咎，失败提交不报成功提示。本地仍内存顺序计算后单次原子写；桌面仍逐 edit 快照/写入/重盖 freshness，告警只保留首/末有界 Python 内容。parser 前后文本均限 2,000,000 字符，未新增硬语法拒绝。
- **规格与结果：** 最终本地58、桌面项目73、schema相关4项绿（直接合并131绿后桌面新增4针另验）；写入安全邻套99/100，注册/派发/settlement/隔离/inventory188/188，桌面/命令类型/远程路由/dispatch146/147。覆盖精确字节、replace_all、依赖编辑、匹配与freshness失败、插入回显、末态告警、原子失败跨文件隔离、撤销顺序和解析预算。ruff与diff-check净；生成清单--check通过。离线评测补“合法冗长替换仍正确、仅操作偏好不同”，并纠正“字符减少=token减少”。未跑付费模型、未重启服务或桌面进程，不宣称账单或端到端延迟降幅。
- **未触碰的独立失败：** 改前已有write_file两项schema失败（400>360；120-token fit为空）；哈希规格误计入已跟踪大型Rust产物1红；远程scaffold引用不存在的PROJECT_TOOL_CREATE_PROJECT 1红。docs-check另有API_CONTRACT/FRONTEND_ARCHITECTURE/STORAGE/auth_providers_billing/remote_execution五文档超限；architecture-check指向未触碰的retired routes/turns_v2.py。未降低预算、扩大hash阈值或删除并行产物。一次并行meta.py临时语法错误阻断收集，后续正常通过，未覆盖该文件。桌面子任务超时无产出，主任务接手完成；日志追加遇到真实stale-read拒绝后重读合并，保留并行日志。

### 2026-09-07 (受控端安装包缺失：发布前置修复 + 不可用状态诚实呈现)

- **实证根因：** GitHub Build Desktop run `34097356650` 在 `Require a CHANGELOG entry for this version` 失败，所有平台构建与 release 被跳过；Latest `v0.16.0` 只有四个完整桌面安装包与 SHA256SUMS，没有 `TofuAgent-*`。Linux 服务器不能代替原生 macOS CI 生成 agent DMG，空 `agent_downloads` 不能解释成“后台准备中”。
- **修法：** `_lcInstallerUnavailableHtml` + en/zh `local.installerUnavailable` 明确无兼容包并保留发布页出口；Windows ready 仍走个性化 EXE，macOS/Linux 仍只用 agent component 直链。公开源在隔离 worktree 修为 `0.17.4`，补 changelog/SDK/uv 元数据，不重指旧 tag。export allow-list 恢复 TDZ 检查器、browser canary、v1 model-routing 与 event contract 生成器；frontend-dist 配 Python/PyYAML/Playwright/Chromium，保留所有 gate。release_assets 尺寸门槛覆盖 agent；macOS agent 新增冻结运行时 smoke 后才打 DMG。
- **验证：** 公共隔离源 renderer/platform 71 passed；workflow/prerequisite 85 passed、2 skipped；完整 `check:frontend` + Chromium canary + manifest 校验通过；canonical release/version 10 passed、export/prerequisite 5 passed，ruff 净。已创建 scoped commit `a255c0848e805ebd2e2894e41636694436c63f17`；原生 CI 与镜像产物验证尚待完成，不能把源码通过等同于包已可下载。

- **续验：** `a255c0848` 已正常 fast-forward 推到 GitHub main；run `34145296114` 的 Determine version 与 Build frontend artifact 均 success，四平台原生构建启动。canonical `generate:runtime`/`generate:i18n` 后 `check:frontend`、Chromium canary、manifest 验证也通过（119 assets，main `Bq5VDIA_`）；刷新页面可见诚实的缺包状态。受控端真正可下载仍以原生 CI 完成与镜像验收为准。


### 2026-09-07 (受控端安装包缺失：发布资产与冻结运行时边界修复)

- **事实：** GitHub Latest 仍为 v0.16.0，仅四个完整桌面包，无 `TofuAgent`；空 `agent_downloads` 不能推导为「后台正在准备」。本机控制面改为如实提示兼容受控端包尚不可用，保留 Release 入口，不改变 Windows 个性化安装与 Mac/Linux 平台选择。
- **发布前置：** 新版本 v0.17.4 补齐 changelog、公共导出遗漏的前端生成器/浏览器 canary，以及 frontend CI 的 Python/PyYAML/Playwright/Chromium 依赖；轻量包尺寸门禁与 macOS agent 冻结启动验证纳入发布契约。已有公开版本不重写。
- **原生 CI 暴露：** Linux 启动缺 `lib/model_info/data/openai.json`；Windows 在 `ensure_private_log_directory` 用 CRT `os.open` 打开目录而失败。修复后本地真实 PyInstaller 再发现 `frontend/authoring-inputs.json` 缺失；源码布局检验又发现 HTML shell fragments 缺失会使首页 503。spec 补充这些运行时资源与 `scripts/release_assets.py` 平台策略；冻结环境只包含 fragments，不误当完整前端源码重算 authoring digest，构建前源码新鲜度与运行时 manifest/资源完整性验证仍保持。
- **Windows 安全边界：** `lib/log_windows_paths.py` 使用 CreateFileW no-follow/backup-semantics 原生句柄，拒绝最终路径 reparse points，访问错误不吞；文件句柄显式转交 CRT 并保留 binary/append 语义。Windows 使用既有/父目录继承 ACL，不声称 chmod 等价于私有 ACL；POSIX 0700/0600、no-follow 与身份竞争检查未放松。
- **验证：** 本地冻结 Linux 重建 128.4s、启动 1.9s、exit 0、有 `TOFU_SMOKE_OK`、无 traceback；新冻结资源规格 8 绿、Vite 新鲜度/损坏邻套 24 绿、发布工作流/尺寸/安装器一致性 109 绿 1 跳过；Windows 边界规格 14 绿 1 原生跳过、日志邻套 55 绿。两路独立审阅无发布阻断项。旧本地 venv 不等同当前干净 CI 依赖，Windows/macOS 最终结论仍由新一轮原生 CI 给出，不能把本地绿声称为已发布。

### 2026-09-07 (重复 todo 根修：最终请求恢复完整权威清单，拒绝更新不再伪装为未变化)

- **问题与根因：** 长轮次下，运行时持有 `_todoState`/`_todos` 不等于下一次模型请求可见。旧清单附件与文件提示共享可裁剪的 1,600-token 块，且附件先于最终 admission/响应式压缩执行；摘要或字节恢复可以再次删除 ID。旧可见性检查还会把保留的指纹或模型尝试过的调用当作完整已接受状态。此时模型复建/补发计划容易触发严格 sync 拒绝，浏览器又把拒绝标成“清单未变化”，造成错误诊断与重复修复循环。
- **修法：** 新 `context_composer/checklist.py` 是单一无存储写入的投影 owner；初始 composer、旧附件入口、最终 admission 两次计数之前和 reactive retry 的同模型 body 构建前均委托它。完整活动清单、精确 ID/status、frame/revision/祖先身份走 required `active_checklist` 尾载体，与可选提醒预算隔离；不靠截断 ID 塞窗口，不恢复工具权限，不可约溢出沿用 typed refusal。当前载体必须完整匹配；未变化复用原字节，变化只追加新版本，不改历史缓存前缀，不复制 replan 审计历史。
- **展示：** rich checklist 由经典与 turn-native 两条入口共用，以 `todoRejected`/`todoNoop` 显式元数据分别显示“Update rejected”/“Checklist unchanged”；拒绝详情完整转义，已接受清单不被拒绝参数覆盖。保留每次审计调用，修订计数只算有效变更。本改不是删除重复卡片或放松清单完成约束，也未改变空回复的异常恢复策略。
- **规格与预算：** 新 `tests/goals/test_checklist_context.py` 27 针覆盖最终压缩 true/false、Continue、prompt replace/可选指导关闭/无工具权限、内容损伤但指纹残留、嵌套路径、缓存不变、响应式 token/内存恢复及失败守门、GPT-5.6-Sol 的 default/codex Responses 实际序列化入参。最大合法 24 项×512 字符/64 字符 ID/六层 Unicode 清单投影实测 68,162 UTF-8 bytes，回归上限 80 KiB；本地 GPT token 估值 50,881，仍完整参与最终预算而非假装免费。
- **验证：** 新规格已通过；邻套 263 针初跑 262 绿、1 红（旧测试将“todo enforcer 不触发”误写成“空回复必须 break”），修为检查独立异常恢复计数、todo 计数未增加、消息未注入后该针通过；未改运行时重试策略。`check:frontend` 全链/构建、`verify:frontend`、架构检查、定向 Ruff 与 diff whitespace 均通过。独立后端复核无新增问题；前端复核确认经典和 turn-native 共用的生产 rich renderer 覆盖三态。core-only fixture 故意降级 generic，不据此新增第二份清单渲染 owner。
- **未宣称完成的外部证据：** 未跑生产模型 A/B 或重启运行服务，不能保证模型绝不再发 no-op；前端 bundle 已构建，后端生效需正常重启/重载，页面需刷新。全仓文档门仍红在本改之外的五个超行数文件（API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、modules/auth_providers_billing、modules/remote_execution）；context_engineering 本次归属文档已压回预算内。共享工作区有并行作者，未回滚其变更；无可用 rollout.jsonl，未运行 session audit。

### 2026-09-08 (残留 bug 清查：坏 checkpoint a1f60772e 的 stash 合并残留根修)

- **起因（用户任务）：** 全项目排查是否仍有需修复的 bug，要求彻底谨慎地调查并给出长期根本解。
- **调查方法：** 后台跑 `make test-unit` 全量（185 failed / 19734 passed / 8 errors），结合 JOURNAL 既有「未触碰的独立失败」记录逐一溯源；用 `git log -S`、删除提交对照、符号存在性核对区分「孤儿残留」与「兄弟并行 WIP」。
- **根因（同一事件）：** checkpoint `a1f60772e`（9-03）把一次 stash 合并冲突的两侧都留了下来（提交内可见 `>>>>>>> Stashed changes` 标记），与已裁决的退役决定冲突，产生三类残留：
  1. **死代码引用已删符号：** `lib/tools/registry/_build.py::_build_project_scaffold` 导入已被 a05c621a6（create_project 退役）删除的 `PROJECT_TOOL_CREATE_PROJECT`；其 ToolSpec 注册在合并中被丢弃，函数成死代码，但 `tests/tools/test_remote_worktree_routing.py::test_remote_hint_applies_to_scaffold_spec` 仍断言 stash 侧方案 → 常驻 1 红（ImportError）。
  2. **已退役 v2 HTTP 面复活：** `routes/turns_v2.py` + `tests/test_turns_v2_api.py` 被 4f5939dcc 明确删除后由该 checkpoint 复活（未注册任何 blueprint），违反 architecture-check 的 retired-path 门（8 条违规）及 `test_conversation_sync_v3.py` 的「文件不存在」ratchet。
  3. **孤儿测试复活：** `tests/test_conv_state_ssot_drift.py`（12 红）与 `tests/conversation_sync/test_sync_digest_ghost_eviction.py`（4 collection error）所测功能（sync-digest 路由、`core/conv_state_reducer.js` 段）在整个代码库中不存在——4f5939dcc 删除了整个 17 文件 conv_state/sync-digest 测试族，该 checkpoint 只复活了其中 2 个而没有实现。
- **修法（完成退役，而非恢复功能）：** ① 删除 `_build_project_scaffold` 及 `_build_project_or_code_exec` 中已过时的 create_project 过滤注释（`project_tools_for_runtime()` 本就不再含它）；② 删除断言废弃方案的 scaffold 测试；③ 工作树删除 `routes/turns_v2.py`、`tests/test_turns_v2_api.py`、`tests/test_conv_state_ssot_drift.py`、`tests/conversation_sync/test_sync_digest_ghost_eviction.py`（与 4f5939dcc 的删除裁决一致；`scripts/migrate_pg_to_sqlite.py` 虽同批被复活但系 live `migrate_sqlite_to_postgres.py` 的硬依赖且有 347d8be54 等后续提交正当化，保留）。
- **系统性核查：** 对 4f5939dcc 删除的全部 384 个路径逐一验证存活状态，确认复活仅上述 3 个（+正当保留的迁移脚本）；全仓扫描确认无残留 `>>>>>>>`/`<<<<<<<` 冲突标记。
- **验证：** 受影响 11 套（remote_worktree_routing/tool_registry/conversation_sync_v3/project_tools/mcp_tool_links/write_approval_gate/temp_write_and_root_signal/readonly_roots/tool_input_repair/command_analysis_extraction/frontend_tool_round_icons）391 passed；`scripts/check_architecture.py` 恢复 `OK (2281 authored files)`；失败文件集前后对比零回归（无「修前绿修后红」文件），first-run 中 4 个因树中途变动而红的邻套在稳定复跑中复绿；ruff check 净、`git diff --check` 净（ruff format 为全仓既有的建议性告警，未扩大 diff 噪音）。
- **边界（明确不修）：** 全量复跑仍存 167 failed + 4 errors，全部分布在有活跃兄弟 WIP 的域——lib/storage_sidecar（10+ M 文件，含 test_conversation_change_storage 的 `sqlite_conversation_change_references_available` 符号被 9cc925e25 删除而测试未同步）、.gitignore/export 漂移（兄弟的 .gitignore diff 正在补 exact-keeper）、frontend 契约等；按共享树纪律不擅自接管。docs-check 五文档超行预算为既有状态，未压缩他人文档。未重启服务、未提交 git（.git/index.lock 存在陈旧锁，未强删）。

### 2026-09-08 补记 (test_human_gate_http_owner 违例标记根修)

- **起因：** 复跑尾部信号 `tests/api_keys/test_human_gate_http_owner.py` 4 collection error（`'asyncio' not found in markers`）。
- **根因：** 该文件对 2 个测试裸用 `@pytest.mark.asyncio`，但本仓 addopts 带 `--strict-markers` 且按既定约定不加载 pytest-asyncio（`test_server_async.py:72`/`test_research_discoverability.py:84` 均以私有 event loop 驱动协程）——标记未注册即收集期报错，**这些测试从未真正运行过**（checkpoint 7c0385b45/2145cc4e0 引入，工作树干净，非兄弟 WIP）。
- **修法：** 按仓内既有 `_run_async` 约定改造——同步测试壳 + `asyncio.new_event_loop().run_until_complete` 驱动原协程体；parametrize 移到同步壳上。
- **验证：** 5 passed（含此前从未执行的 2 个所有权边界用例 + 4 参数化校验用例），首次真实运行即全绿；ruff check 净（format 告警为 HEAD 既有的全文件引号风格漂移，make lint 仅警告不卡门，保持最小 diff 不动）。
### 2026-09-08 (Opus 流异常：首轮心跳断流恢复、统一退避与原生 SSE 错误分类)

- **取证：** 当次应用日志样本有 120 条 Opus 5 正常流记录、21 条 `premature_close` 记录。异常事件环中多次只见 2–3 个 `ping`，约 34–36 秒后 EOF，没有正文、工具或 provider finish；另见 HTTP 502 和零事件长等待。独立 requests 直连同一网关、使用现有两把 key 的两轮共四次短提示探针得到两次 HTTP 429、两次响应头等待超时（65 秒诊断上限）；这些失败发生在 Tofu SSE 解析之前。没有证据证明这批正文被解析器吞掉，也不能把上游当前不可用宣称为已修复。
- **代码：** `_analyse.py` 去掉空异常流必须已有前轮的限制，心跳或短思考前缀后 EOF 在首轮也使用既有有界恢复；所有该类重试复用带抖动、可取消的指数退避和同模型换槽提示，取消零退避分支。原生 Anthropic translator 将无 HTTP 状态的 `rate_limit_error` / `api_error` / `overloaded_error` 映射为 429/500/529，交由共享分类器处理，保留显式网关状态；未提高重试上限、未将断流伪装成成功。
- **验证：** 新回归最初 11 失败/3 通过，修复并增加取消、退避上界和正常完成边界后 17 项通过。相邻解析/传输/任务用例 248 项、调度/预算用例 102 项经分层验证和定向复测通过；其中旧首轮立即失败断言改为预算耗尽断言，旧断流 fixture 去除与“正常空终止”冲突的标记。共覆盖 367 项；ruff 与修改范围 `git diff --check` 通过。全仓 docs-check 仍被另外 5 份未改文档的既有行数超限阻挡，本次 LLM domain map 在预算内。
- **边界：** 保留其他会话的在途改动；本次未重启正在运行的应用，代码需进程重新加载后生效。并发工作区的检查只代表本次分层取样，不宣称全仓 release gate 全绿。

### 2026-09-08 (跨协议流审计：终止容错、Responses 正文补齐与错误分类一致性)

- **范围与证据：** 继续覆盖 OpenAI Chat Completions、Anthropic Messages、Responses 的同步/异步 HTTP SSE、Responses WebSocket 及非流式 HTTP 200 错误信封。最初跨协议复现矩阵 70 项中 66 项失败。对照 WHATWG SSE 解释规则和 OpenAI 官方 Responses streaming-events：数据之外的 EOF 控制字段不应废弃已完成响应；Responses 的顶层 `type:error` 也必须处理；终态完整 output 可以承载增量未到达的正文。没有把独立上游故障归因于这些本地缺陷。
- **终止与正文：** SSE framer 丢弃未结束的纯注释/id/event/retry 尾部，仍拒绝未结束的 data、无效 UTF-8/JSON 和越界事件；retry 提示只接受有界 ASCII 数字，避免 Unicode 数字或巨大整数导致解析崩溃。provider finish 已到但没有 `[DONE]` 不再误记 premature close。Responses 完整可见正文按已输出前缀的长度和 SHA-256 校验后只追加缺失后缀；冲突报错，缺省/null 正文元数据不充当空正文证据，隐藏 agent 内容不外泄。新增 8 MiB 增量实验确认追踪器额外常驻内存小于 256 KiB，不另存一份完整增量文本。
- **分类：** `llm_errors.provider_error_http_status` 统一结构化 status/code/type，显式状态优先，复用原 HTTP 分类器处理限流、配额/重置、权限、请求错误、上下文超限、内容过滤及网关故障。各协议保留原始错误字段；Responses 识别顶层/嵌套 error，非流式适配器不再丢失 HTTP 200 内的错误信封。WebSocket 不再把已分类错误重新包为可重试网络异常；HTTP 同步/异步把明确 provider 应用错误与网络路由故障区分，保留凭证已发送的布尔证据以识别网关“未收到 key”矛盾。未知 SSE 错误保留为显式 ProviderStreamError，`error:null` 不视为失败，异常 message 类型也不能覆盖结构化分类。
- **验证：** 新定向测试从 105 项扩充至 121 项（补充部分缺省正文边界后最终复测 121 passed），覆盖三协议双传输、HTTP 200、WebSocket、尾部控制字段、真实 data 截断、配额/权限/参数错误、网络健康、正文冲突/缺省/Unicode/隐藏内容及常驻内存。第一批既有解析/物理 HTTP 交付/连接复用/活动续期/错误和账务邻套 411 passed；定向、Responses 邻套和任务恢复/预算/调度/终态门禁合批 424 passed（两批存在 Responses 重叠，非 835 个独立用例）。未放宽真实数据损坏判定，未增加重试上限或改变连续传输空闲超时；心跳、思考与 WS 活动续期及取消合同保持通过。ruff 与修改范围 diff 检查通过。
- **文档与运行边界：** LLM domain map 仍为 220 行并更新协议、错误和验证入口；docs-check 仍仅报告另 5 份未改文档既有超行预算。未提交、未重启活动服务；变更需进程重新加载后生效。本工作区存在其他在途修改，分层通过不代表全仓发布门禁通过。

### 2026-09-08 (Tofu harness：空停传输取证、终态一致性与 ATIF/诊断修复)

- **线上事实：** 画布362094/flow863800的v0.17.3精确13题canary已DONE；25 trials中首试6/13、最多三试12/13，9/25异常（8 empty_stop、1未执行工具标记）却被旧CLI/agentix全判SUCCESS。历史证据见相邻autoharness的 `docs/tofu-canary-863800-diagnosis-20260908.md`。奖励与运行健康独立，不把异常前已通过的修改归零；本轮没有足够逐请求原始终帧，不能确认旧空响应的全部上游成因。
- **复现与修复：** requests.iter_content(64KiB)对非chunked短SSE会等待填满或EOF，已flush的心跳/内容对上层不可见；改为urllib3.read1并保留解压和requests异常映射，旧urllib3走一字节fallback。close/Content-Length/chunked×plain/gzip六种真实socket规格通过。这个传输缺陷真实存在，但不能据此认定8次旧空停均由它造成。OpenCode按finish与无待执行工具结束，不额外要求最终正文非空，缺少empty_stop标签也不代表没有空回答。
- **终态与恢复：** SDK AgentResult.ok与CLI复用canonical verdict，缺finish不默认stop；异常、截断、取消、工具交接不能成为完整任务成功，保留部分正文、用量与错误。空停在原上下文追加明确纠正指令，仍最多两次，禁止整轮重放副作用；导出最后16份白名单parser receipt。独立agentix工作树 `work/agentix-tofu-stream-fix` 防御校验ok/status/finish/error/返回码并兼容token别名，不再建模型恢复循环。
- **ATIF与诊断：** 导出原生toolName/toolCallId/toolArgs/toolContent，保留角色及调用-回执顺序；运行开/收尾明确messages来源，历史工具不能遮蔽本次执行。独立审查发现历史遮蔽及JSON编码/跨帧工具参数漏脱敏，11个样例先10红1绿再修复。诊断改为结构化有界脱敏，工具输入/参数分片与畸形、超大、过深值即使debug也省略；常驻ring400行/256KiB、单行64Ki字符、512节点/10层及文件轮转均有规格。此诊断不是byte-exact replay，默认不落provider正文。
- **验证（集合重叠，不累加）：** 前序SDK/CLI/runtime143项、agentix23项、流/恢复邻套236项；审查修复相关39项；随后来源/解析/终态150项与architecture检查通过；额外日志深度/节点/事件预算1项通过。修改范围ruff/diff-check通过。干净wheel在仓库外隔离安装，doctor、数据库排除、诊断脱敏及真实HTTP恢复/耗尽链路均通过：分别3/4个请求、都只执行1个工具，终态分别stop/provider_finished与abnormal_stop/empty_response；包边界1554 members/0泄漏。
- **验证快照而非发布：** `.eval-runs/tofu-stream-fix-final-dist/tofu_agent-0.17.4-py3-none-any.whl`，SHA256=`2874a3dd7c6c3233ad0207d7fae6a87edf6f8299b0a3afb9f591f664d2c1f252`。构建后共享树的_sse_core.py另有SSE error.message类型保护并行改动，所以该包是上述时点快照，不宣称等于持续变化的全部工作树。未提交/push/发布/改画布/重启应用。全仓docs-check仍被另外5份未改文档超限阻挡；NC警告对应的共享树修改未擅自回滚。Harbor非零退出不抛错及token context接入未改，平台绿灯不能验收runtime健康。下一步需固定修复提交、独立evaluateName，在同模型13题/预算/重试配置下复跑，才可报告线上空停率变化。

### 2026-09-08 (思考强度滑杆精简与顺滑拖动；Claude effort 最终传输修复)

- **交互：** 桌面/移动共用 `thinking-depth-slider.ts`，删除重复当前档位和底部解释，刻度统一常规字重；28px 胶囊槽包住 22px 球，保留 44/48px 操作区。拖动保持连续位置，松手吸附六档（off/low/medium/high/xhigh/max），只有换档才调用原 `selectThinkingDepth` 持久化一次。
- **实测根因：** 单改原生 range 的 step=any 能消除桌面跳档，却仍有 Chromium touch slop，小幅触摸 2→2.1 不动。改为 Pointer Capture 驱动连续位置，每手势只读一次几何、保留抓取偏移；键盘仍使用原生 step=1。取消/失焦/模型切换释放 capture，取消后无 pointerup 也可立刻键盘操作；其他 pointer 的取消不能干扰当前手势。
- **Claude：** canonical/OpenAI-compatible 保留顶层 effort，原生 Messages 映射到 output_config.effort 并保留格式；显式 low 不再误作旧 preset，压缩后同模型重建保留已解析强度；深度单项变化的 continuation witness 保留工作视图，同时 guard v2 model/provider/routing intent。上游 gateway 消费字段/缓存命中与 route identity 尚未以本轮活测证明，未启用 per-message effort 等 beta。官方高默认值与旧-family omit-medium 兼容差距已明确写入文档。
- **验证：** 最新 15 项 Node 滑杆规格通过；新增 `tests/application_shell_fragments/test_thinking_depth_slider_browser.py` 两项隔离 Chromium 151 规格通过，覆盖鼠标/真实 CDP touch、小幅连续拖动、同档零保存、跨档单提交、同步 sync、取消后键盘恢复，以及 1100/320/390 × 三主题 × 中英布局。截图 `/tmp/tofu-depth-browser-dg4et462/`；工具图片接口返回 base64 文本，未宣称人工视觉审图。此前 effort/replay/adapter/compaction 318 项通过，ruff 净。最终 build:frontend + verify:frontend 通过，bundle `main-BXhJekuj.js`；未重启后端或刷新用户页面。
- **边界：** 浏览器规格是实际 markup/CSS/owner 的隔离集成，非生产对话持久化 E2E。check:frontend 在本轮初期通过；最终 typecheck 被并行 WIP `swarm-agent-presentation.ts` 四项 TS2339 阻断，未越界修复。documentation-check 仍有共享文档超预算/未登记项；本轮 llm_io 与 LLM_COST_OPTIMIZATION 的新增内容已压回预算内。独立 Settings 邻测还有旧「模型管理」标签断言失败，未据此声称全仓全绿。


### 2026-09-08 (压缩连续性：历史用户证据不冒充当前请求，跨 turn 工具热尾与原请求镜像核验)

- **根因与证据边界：** 旧 L2 单独保护 opening anchor，再把其余旧用户原文放入 retained wrapper；wire 相邻 user 合并后，开场问题/历史/最新继续指令/运行时附件变为同一条 user，自动 compact 调用与摘要却在其后。保留文本不等于保留当前请求位置。原 preflight `messages_snapshot(kind=request)` 可经 `snapshot_delta.rebuild_snapshots` 从持久化 delta 恢复；必须核查 degraded，不要把压缩前 archive、post-tool state 或离线模拟称为原网络包。
- **实现：** `_history.py` 统一人类源判断及有界原文历史载体，provider 剥离私有字段后仍有可见历史归属；opening 是历史证据，不自动恢复为当前问句。自动压缩把 receipt 放在最新真实 human 之前，跨 user turn 保住最新完整真实执行 batch，synthetic compact 不占执行热尾，incomplete batch 不冒称 complete。head truncation 保护 irreducible history/最新 human/真实 batch，耗尽可删内容则安全停止。
- **摘要与手动持久化：** 仅丢弃摘要输入中的非人类散文，保留用户文本及边界空白；缺失 Objective 不回退开场问句。摘要不得凭空制造确认步骤、权限或把 assistant proposal 升格成用户约束。manual 使用原 human Turn 身份，摘要前冻结 read-set，CAS 接受并发尾部但新 human stop/edit 阻止旧摘要 repin。历史原文仅在投影时封装；连续摘要前历史共用说明，完整近期对话不逐条重复包裹。
- **预算规格：** 新增并发原文保留测试暴露重复说明开销；修复后合成样本由 21 次 guidance 降至 1 次，估算 after 从 16,000 降至 12,659 tokens，同期未压缩含新增尾部为 12,665。并发 token/msg 断言比较同一含新尾部基准，不宣称比开始时快照必然更小。
- **验证：** 最新 manual/history/provider 三套 65 绿；builder/async/manual-route/storage-projection/Responses-phase 邻套 98 绿；anchor/summary-authority/intra-turn/invariants/improvements/gate-token/receipt/usage 八套 314 绿，合计 477；此前其余压缩邻套 87 绿。目标 ruff 与 diff whitespace 检查净。离线回放 `python -B -m tests.compaction.test_compaction_history_wire` 使用真实 compactor + build_body、仅摘要是声明的 deterministic stub，31→7 messages，最新继续指令独立保留；不等于真实模型行为验证。
- **文档门禁：** `CONTEXT_COMPACTION.md` 更新历史/当前请求、摘要 authority、CAS、provider 与 replay 契约。本轮最后 `make docs-check` 仍被其他并发文档行预算阻挡（API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、auth_providers_billing、remote_execution、task_engine），没有提高预算或改写它们求绿；不宣称全仓 gate 通过。未重启运行服务、未提交/推送发布，当前离线结果不代表旧进程已加载新逻辑。

### 2026-09-08 (Tofu harness修复快照发布并发车；线上终态验收仍等GPU)

- **隔离发布：** 用户授权先更新远端供实验拉取。产品源码冻结到`.eval-runs/harness-source-20260908` @574a67d6ddbf423c6944021336adec43e0e86987，公开导出树`.eval-runs/harness-public-20260908`经过滤/脱敏/secret scan/语法完整性检查，推rangehow/ToFu新分支`harness/streamfix-20260908` @**3c5d378f30dcecebad02bd6966bd0fcfaa0b7e0b**并读回。版本0.17.4，稳定main/tag未移动，未上传PyPI；不宣称等于继续变化的整个共享工作树。
- **验收：** 导出树终态/SSE/真实HTTP/ATIF关键69项通过。`.eval-runs/harness-dist-20260908/tofu_agent-0.17.4-py3-none-any.whl` SHA256=**1233d39940470bad1d2bf9053a53b8bc6fa8ef6aa1964ac74fbacf96af6b374f**，1562成员/0排除项泄漏，storage模块未打包。仓库外干净安装以`-I`跑doctor及真实HTTP恢复成功/耗尽链通过（3/4请求、各1工具，ok=true/stop与ok=false/abnormal_stop）。再从远端以画布相同git完整SHA spec安装，PEP610 resolved commit精确验证。没有把此前不同内容的同名0.17.4本地wheel混入。
- **集成发布与发车：** Agentix独立`ruanjunhao/tofu-streamfix-20260908` @0923594475f588235b9df3b0704fc3f234a5fc53（23测试通过）；Harbor同名分支@325e33e88de9bdf07bab806463129c6bedb494b8（10测试通过），固定Tofu新完整SHA。新[画布362610](https://model.sankuai.com/user/modelManage/experimentalTaskDetail/362610)/node2055217/task1473327/taskDetail4509604/**flow870408**已启动；同Flash-v588/原精确13输入、独立evaluateName。17:11 Scenario实际13题集合与旧输入索引精确相等；Job来源agent/Harbor/input/timeout读回正确。
- **最新状态与边界：** 17:12:52 flow RUNNING，CPU Agent13/Group1均SERVING，GPU psxk7cjxrr7ko5yn Worker和AM Pending、无分配节点，模型尚未启动；17:16无回传trial，不能确认容器安装或线上空停已消失。平台间歇504是观测API错误，不当Tofu失败。新内外预算按要求7d，旧单题28801秒，非严格同预算消融；旧6/13首试、12/13三试、9/25异常仍只作历史。Harbor非零退出策略与token context接入未改，平台绿灯不替代runtime验收，103题门槛未过。
- **交接：** autoharness `work/tofu-streamfix-870408.json`登记三层SHA/配置/待验；诊断报告§5与AGENTS已同步。`work/tofu-streamfix-870408-watch.py`通过观察夹具后启动只读bg_820afedbbdf9（30分钟上限、60秒间隔，首个完成trial/错SHA/平台终态/连续3次API错误即停），不会自动改配置或重启；首个trial回传后仍需核验安装、终态、parser receipt与ATIF。未重启活动Tofu服务。


### 2026-09-08（检索能力与浏览器读取根重构：Web／原生站点隔离、统一目标验收）

- **根因：** 可连接的站内 search 曾自动加入 Web 引擎广播，导致小红书和 ModelPlaza 收到 `site:` 官方文档问题。网络提取又按业务 JSON 形状而非当前任务相关性选取响应，可能把账号/聊天等旁路数据当正文。
- **架构：** 保留现有 agent 选择、浏览器 owner/device/lease/policy；新增一个 `browser_site` 能力入口，发现→读 schema→执行只读命令，不引入路由模型/新浏览器进程/新知识库。默认 Web 搜索零站点发现与调用；显式选择失败不扩源。`tofu-search` 0.10.4 暴露 `SiteSearchTerminalError`，使原生参数、升级、限速/内容失败无法降级绕过到兼容抓取或 cookie replay，宿主最低依赖同步。
- **唯一验收：** `content_contract.py` 定义 target/receipt；`content.py` 消费 DOM、snapshot、hydration、捕获 API，投影目标记录并给出 ok/partial/empty/blocked/unavailable 与 coverage/evidence。删除旧 HTML/raw-read/初始态全量渲染分支。空页不是无结果；查询不匹配不能靠标题出现在 DOM 中通过；missing fields、媒体未读与预算截断如实报告，未完成字段/截断不学习成功观察。端点形状来自实测清洗后的 body，不编造业务记录。
- **扩展：** 5.5.3 additive contentSnapshot，无新命令/权限；去 CSS/导航/隐藏内容、open shadow/虚拟列表累积，5k nodes/80 depth/80ms/40 shadow roots/40 records。修复 selector root 遍历越过 nextSibling 的作用域漏洞。限速落在 adapter 执行边界（小红书 search/detail 12 秒间隔、300 秒退避；256 owner/device/site 路由、15 分钟 TTL、无内容缓存/睡眠线程）。
- **边界：** 只保留观察到的可用 URL；签名敏感链接不输出损坏的脱敏可导航 URL，而是明确缺 URL/partial。自动 signed search→detail 仍不保证；图片 OCR、视频、完整评论、closed shadow 不宣称已读。只读真实 Friday 当前详情返回了正文，但活服务仍输出旧读取格式，新实现需重启服务/重载扩展后另做线上 E2E；没有执行页面中的沙箱/cookie 操作说明，也没有新增小红书风控请求。
- **验证：** 搜索包最终 632 绿/6 skips；浏览器与宿主首轮 503 绿/2 红（新工具缺 timeline label/badge 已补，并补类型化错误显示），最终相邻回归另记下方。静态检查、Node 语法、diff check、monorepo 和 uv lock check 通过。工具 schema 14,989 chars，保持原 15k 上限；浏览器权威文档 219 行。全库 architecture 仍被域外 providers.py route→dispatcher 依赖阻塞，documentation 仍有其他五份文档超行限；未改动这些并行工作，也未提交/发布/重启。
- **最终补记：** 新增显示测试实测到 `tool_result_error` 漏识别原生 `ToolResultEnvelopeV2`（只支持 dict/str），导致限速轮 error 内容却以 done 收尾；修正唯一解析器并补成功/partial/终止/可重试对象规格。最后浏览器/宿主 511 项与 terminal breaker 16 项全绿；536 项扩展套仅清单审批元数据缺 `browser_site` 一红。补站点/操作/有界脱敏参数 enricher（不改权限/串行门），重生成 TOOL_INVENTORY；最终站点/清单/真实审批 renderer 30 项全绿，ruff、Node、diff、inventory-check 净。未无差别重跑未变浏览器全套。长代理邻套 72 绿/2 红均为已有 gateway schema 564>522 与 lean URL 文案断言；未放宽。五份域外文档超限及架构违规仍阻塞全仓 gate；没有重启/发布，新线上 E2E 和端到端性能比较未完成。

### 2026-09-08 (tofu-db：历史段分步准备、后台发布预留修复与三引擎复测)

- **写路径：** 临近 WAL 轮转的四个历史段窗口内，每次单条/批量提交最多准备一个 4 MiB 历史段；只缓存最多 33 个引用，checkpoint 才发布 manifest 并退役 WAL。短 generation 不提前生成历史文件；GC 保留未发布引用，重开从既有有界 WAL 恢复，无新线程或载荷缓存。新增逐 syscall 错误、短写、丢失同步矩阵，验证完整已确认事务、块内容、重开及继续提交。
- **配件可靠性：** 全量测试暴露真实后台打包竞态：take-completed 曾在前台发布/退役前释放 reservation，后台可能重打同批块。新增 Publishing 状态与 RAII guard，成功/失败都释放，不复制第二份 ID 集；补齐确定性独占、失败重试和逐发布故障测试。另修复 outbox 截止时间用例的无限忙等：旧 2 ms deadline 可在 sink 开始前到期，测试却无限等待 sink 调用；改为有界 channel 同步，先确认执行再验证调用方超时与重试。发现挂死后主动终止该次测试进程，未把它算作通过。
- **验证：** 最终 cargo test（4 线程）591 passed / 1 ignored（原有 1 GiB spill 规模项），strict clippy、release build、IR generator check、monorepo check 通过；最终语义差分 145 passed，协议/基准/pre-authority 邻套 30 passed。共享树其他修改保留；最终 docs-check 仍有 5 份文档超行预算，包括 STORAGE，发布门禁尚未全绿（共享树另一份超限在本轮期间已由其他修改消除）。
- **性能及代价：** 1000 次原生 Turn 追加、before/after 各三次交错实测，三次最大延迟的中位数 130.480→22.140 ms（约 -83%），p50 0.775→0.769 ms；平均耗时的中位数仍 1.0278→1.1113 ms（约 +8.1%），不能声称整体吞吐提升。最初从 4 MiB 即准备的方案平均回退约 13%，已收窄为轮转前窗口，但剩余回退仍需消除。完整数据 `/tmp/tofudb-goal-append-near-interleaved.json`，配套逐次日志和 before 二进制保留。
- **三引擎：** `/tmp/tofudb-goal-three-final-medium.json` 的本机 medium 客户端合同负载全部执行成功，无跳过。对 PostgreSQL 8/8 平均延迟胜（1.57–20.59×）；对 SQLite 5 项明显胜、search 基本持平、create/append 分别慢约 1.29×/1.60×，写尾仍显著落后。Tofu-DB release SHA256 `a6ac232c14092882989c3d6ce3202af29d1b296dbe0112b97ac2219ae460586a`；PG 18.6 为私有 Unix socket 临时实例，fsync/synchronous_commit/full_page_writes/data_checksums 全开，完成后 pg_ctl fast stop 成功，证据目录 `/tmp/tofudb-goal-pg-caepm893`。首次 PG 因 Conda 环境缺 psycopg 跳过，随后使用已有 .venv 驱动重跑；首次失败报告不算三引擎证据。
- **上线仍未达成：** `tofudb/integrate` 中迁移工具与真实文件系统认证矩阵仍未合入当前树；多域迁移/回滚、规模认证、TofuSQL、静态块加密、签名打包与 Supervisor 接线等门槛尚未全部完成。本轮未切换生产存储或发布，后续先消除剩余吞吐回退和 SQLite 写负载差距，并推进未合入的迁移/认证能力。

### 2026-09-08 (Swarm 模型标识与工具结果：工作器来源、波次归属和有界证据合并)

- 截图对应的耐久记录已有模型/工具证据；缺失来自 start 先于 factory、展示重新路由、共享 session ID 的旧波次围栏、会话级 status 跨 roster 回补，以及稀疏 live 卡片压住完整 snapshot。factory 后再 start；已构造 worker 的模型是唯一展示来源，status 带物理 start clock。
- 新 `swarm-agent-presentation.ts` 统一裸/full-v2/sparse handle、波次 roster 和有界合并；当前波次优先于旧终态围栏，status 不跨 roster。live/persisted tool 共用 occurrence callId，terminal snapshot 最终裁决；30 行/2,000 字符 detail/32 KiB 与省略说明保留。wire toolCalls 别名在 normalize 消费一次，迟到 start 不得降级已 done 调用；两项新增回归先红后绿。
- 新 provenance 8 绿；Node authority 18 绿并接入 pytest；结果/时钟/overlay 邻套 18 绿，最终 authority + Chromium rich-panel 2 绿。runtime/TypeScript/scoped ruff 通过。build/canary 成功；此前 build+verify 通过，最终 verify 因构建期间并行 authoring input 改变报 stale，不宣称最终全树发布 gate 通过。全库另有样式生成漂移、5 份文档既有超限及 provider route→dispatcher 越层依赖；保留并行改动，未重启活动服务。

- **最终门禁补记：** 最后一次 `npm run check:frontend && npm run verify:frontend` 的 runtime/styles/全部生成契约/i18n/actions/TypeScript 均通过；Vite 变换与打包完成后，publication guard 检测到并行 source 变化，按设计拒绝发布（`frontend authoring inputs changed during the frontend build`）。不再追逐共享树反复构建；本任务代码/定向验证完成，最终发布需稳定输入窗口，backend 未重启。

### 2026-09-08 (后台命令投递根修：Turn 回执与队列移除合为同一权威提交)

- **根因：** 后台命令完成不等于结果投递已确认；旧 deferred flush 先发 injection 事件/累积 `_bgCommandInjects`，再独立删除队列行，因此能同时显示「已进入上下文」与「结果送达未确认」。不是少做一次 DOM 刷新，也不能靠 command 文本或执行 ID 隐藏队列。
- **权威：** preview 带精确 `queueId`，`turn.event.record` 在一个 owner-scoped 事务里提交 receipt、合法队列删除/重编号、task replay carrier 和 `AttemptEvent.payload.removedQueueIds`。删除验证 conversation/owner、workflow kind、command identity 及三个空 Turn/Attempt 链接；失败回滚两半。浏览器通过相同 attempt/sequence/revision/patch 校验后，一次 reducer 通知安装回执并移除精确队列项；同命令的其他投递保留，剩余队列 rank 与存储重编号一致。
- **恢复与预算：** 仅 verified provider completion 后 ACK；冻结一份回执，最多两次 commit 尝试，不再调用模型，stable blockId 防 lost-ACK rebase 重复；成功后才更新 executor mirror/清 pending，并 fence late inbox twin。未提交回执不能通过其他 checkpoint 泄漏。沿用 256 项有界 inbox，超额不 drain。provider 消费与数据库非跨系统事务，提交前 crash 仍可能重投，旧无 queueId 回执不授权猜测删除。
- **验证：** focused receipt/UI/injection/ack 24 项通过（23 项首跑通过，UI 修正为真实 version=1 patch 后单跑通过）；邻套 turn persistence/sync/cancel/recovery/dispatch/storage 137 项通过；provider acknowledgement 前序 17 项通过。最终队列 rank/cold snapshot UI 两项通过、目标 ruff/schema/event 生成检查/TypeScript/diff-check 通过。check:frontend 前段全部通过，首次 build 因并行 pet 迁移在扫描后删除 tofu-pet.d.ts 失败；确认源码变动后重跑，最终 build/canary/verify 成功（main-BNRdihQb.js，120 assets）。这是该构建时点的共享树结果。
- **边界：** architecture-check 仍报告域外 providers.py:100 → lib.llm_dispatch.health_local；documentation-check 仍有另外五份文档超限，本领域契约未超限。NC 警告涉及的 queue/scheduler 修改已核对为语义 WIP，styles 属源生成图并行改动，未擅自回滚。未重启后端、未提交/push；前端构建通过不代表旧服务已加载新事务。当前契约见 CONVERSATION_SYNC_V3、RENDER_CONTRACT、task_engine。

### 2026-09-08 (项目栏口袋剧场：重做构图与角色交互，替换旧宠物 FSM / 程序化画布)

- **方向：** 不再把宠物、满幅纹理、工作控件挤在同一条窄栏。上方 112px（窄屏 104px）为纸雕绘本风景，下方保持原项目控件；三幕「三叶草邮局 / 写给月亮的信 / 风开始的地方」，共用一只奶油色豆腐角色与同套几何、阴影、表情。图像生成服务本轮无健康部署，改直接手绘 SVG 轮廓；未引入新依赖/图片请求。
- **交互：** 种草 / 放纸船 / 放风筝会让角色前往并留下可见小纪念（最多五件、会话内）；点击问候/显示 My Day 后端摘要，拖动有抬起/落地，方向键移动，键盘按钮等价操作。保留原 scene-switch 动作及 meadow/pool/sky/off 设置键；静止模式持久化，reduced-motion 下角色不自动位移但保留操作结果。场景关掉时恢复紧凑栏高，非 tofu 主题无剧场。
- **实现：** `features/pocket-theatre/{controller,art}.ts` 为唯一 owner，`main.ts` 就绪后懒载、私有 `TofuPet` 服务接既有入口；删除旧 `runtime/scene/tofu-{pet,scene}.js`、公开场景桥与废弃画布/帧测试，保留后端 My Day 摘要/发射接缝。`25-pocket-theatre.css` 统一新布局，12/13 样式删除旧背景层与宠物规则、简化工作控件与静态输入托盘。控件继续显式 allowlist，覆盖层绝不挤入 flex 流。
- **预算：** 一条有限 rAF 链（最多 30 次几何更新/秒）、一个可替换消息 timer、五件纪念；静置零 rAF/周期 timer。隐藏、离屏、原生后台、主题切换、关闭和静止模式全部取消动态工作；完整销毁释放监听器/观察器/节点。新生产 chunk 17.79 KB / gzip 7.12 KB（包含三幕 SVG），不再预加载 22 个角色图。
- **验证：** 新 Node 行为/资源规格 9 绿；真 Chromium 五针覆盖 1440/800/390/320、三场景结果可见、控件命中、不溢出、off 降栏高、双语与静止模式；聚焦接缝/翻译 12 绿；ruff/diff-check 净；`check:frontend` 全链及 `verify:frontend` 绿（120 assets）。实服 :15000 新页面探针 200、一个 `.pt-theatre`、零旧画布、零 JS 错误；无用户任务/设置修改。截图在 `designs/pocket-theatre-review/`。未跑全库测试：广域 Vite 邻测另有 admin serving 字面串和 sessionStorage allowlist 两处域外断言失败；documentation gate 五处既有文件超行限（含并行追加的 FRONTEND_ARCHITECTURE），未重写其他会话文档。页面硬刷新生效，无需后端重启。


### 2026-09-08 (工具卡头部单一展示所有者：历史去重、拒绝优先与聚焦验证入口)

- **根因与边界：** 一个头部改动原需追踪后端标题、结算 metadata、retained 徽标分支和历史记录。现由纯 TS `conversation/presentation/tool-header-presentation.ts::createToolHeaderPresentation` 统一 `queryText` 与 `renderBadgeHtml`；后端仍是新标题/执行事实权威，retained dispatcher 只委托，删除 `_computeToolBadgeHtml` 及无消费者的 refusal 别名。架构 ratchet 防止旧策略函数回流。
- **行为：** 新批量编辑及可验证的历史标题去掉 `(N edits)` / `(N insertions)`，右侧计数不再以 token/chars chip 替代；单写和其他工具保留既有兜底。历史匹配需路径、批次数与完整旧标题签名相符，歧义/超限原文不改，不迁移存储。部分失败详情、缺详情的 failed 状态及 structured refusal 优先保留，error 分支也复用同一策略。
- **有界与安全：** 标题归一化 8,192 units / 参数 120,000 units / 128 edits；徽标 2,000 units。既有 refusal owner 将路径投影限定 32 × 512 units、未知 kind 160 units，省略号显式披露；修复 prototype 字段名被误当 legacy refusal 的查表缺陷。冻结输入与 HTML 转义有行为针。
- **维护路径：** `frontend/src/conversation/README.md#tool-card-header` 为唯一紧凑所有权图，docs map/architecture 只指向该处；新增 `npm run check:tool-header`。复用 backend builders、materialized dispatcher 与真实 Vite owner，不另建测试运行系统。
- **验证：** 初始新矩阵复现 20 红；review 新针复现 terminal refusal/资源边界缺口。最终 `check:tool-header` 93 绿（含 61 轮 wire-parity）；基线重生成逐项校验仅两处历史计数 badge HTML 删除，其余字段不变。独立 Goal/rejection/outside-workspace Vite 针全绿；邻套 121 绿/3 红，红项为并行 Overleaf hook 缺失绑定及 manifest 相邻位置断言，未改其源或放宽针。ruff、typecheck、完整 `check:frontend`、`verify:frontend`、scoped diff-check 均绿（120 assets）。
- **全库未清零：** architecture 仍报 providers.py:100 → dispatcher 内部依赖；documentation 仍报 API/FRONTEND_ARCHITECTURE/STORAGE 及两个 domain map 原有超行限，本次 map 行仅改指针未增行。共享工作树其他改动全部保留；未重启运行服务，不能声称线上已生效。


### 2026-09-08（Tofu-DB：自适应历史准备、可复现认证矩阵与 GC 重试预算）

- **写入尾延迟：** 历史段准备从固定提前窗口改为按即将到来的 WAL 字节数计算，至少留一个 4 MiB 段、最多一个轮转窗口，单次仍只准备一段；先通过 WAL 入场校验再准备，原 WAL 保持恢复 authority。三组交错 1,000 次持久化追加的最大延迟中位数 130.125→21.105 ms，平均耗时中位数 1.0667→1.0790 ms（约 1.15% 差距；此前固定窗口约 8%），不宣称统计等价。微测量和各二进制哈希保存在 `/tmp/tofudb-goal-append-adaptive-interleaved.json`。
- **真实 GC 缺陷：** spill 写满或 I/O 失败后保留满缓冲区，旧 `len == IDS_PER_RUN` 检查允许下一次重试越过阈值并无限增长。小回归先复现红，再修为满缓冲先重试 flush、成功后才允许增加引用；连续 ENOSPC、恢复写入及崩溃读回规格通过。修正被默认跳过的全规模测试错误溢出断言后，release 测试实际处理 33,488,896 引用/511 runs/1,071,671,244 bytes（约 1,022 MiB），6.81 秒、峰值 RSS 20,132 KiB，越界拒绝且清理临时文件。独立测试二进制 SHA256 `28050a91d52faa70a87311c12ca8cbf81a75cbeeab1dc93f84ce2a5a1a2a217c`；资源证据 `/tmp/tofudb-gc-scale-resources.json`。
- **认证配件：** 引入并强化独立 `certify-filesystem-matrix` CLI，格式/命令/环境/上限/必选 cells 进入 Schema IR 并生成 Rust 常量。绝对空路径预检，非空目标保留；可选挂载缺失报 partial，实际失败报 fail 且非零退出。恢复必须保住全部已确认事务及精确 payload，未确认 group 只接受完整前缀；空 fault evidence 和恢复错误均不能报通过。全量测试发现随机 segment ID 改变 manifest hash，目录分片碰撞导致轨迹 87/88 步漂移；有界预建 256 个空 shard，复用同一采样轨迹并验证故障已命中/类型匹配，64 次随机身份一致性规格通过。fresh-shard 仍由 block 故障规格与真实路径覆盖。另将 12 轮轮转规格加强为已确认序列与每条 payload 精确恢复；双 barrier 同时被设备丢弃的关联故障仍只是负向能力边界。
- **最终验证：** 常规 Rust 603 绿（另一个默认 ignored 的 GC 全规模测试已显式运行通过），严格 Clippy/release 构建通过；同 release SHA256 `1e4e5e3a5dddae7f8fc8a9c61666d44a87185655b05585d44fa744395e86511f` 的 XFS 与 `fuse.bgfuse` 矩阵 6/6、227 个确定性故障案例通过，两个真实路径各保留约 2.01 MiB 的跨进程证据。Python SQLite 差分 145、合同 5、性能工具 24 绿；91 个 package/contract 源文件摘要在构建后核对未漂移。原始挂载报告 `/tmp/tofudb-matrix-real-mounts.json`。
- **性能与上线边界：** 最终三引擎 medium、相同 `/tmp` XFS、顺序客户端全链路无 skip：Tofu 均值胜 PostgreSQL 8/8（1.80–23.16x）；对 SQLite 6/8 更低，创建 0.932 vs 0.661 ms、追加 1.927 vs 1.210 ms 仍落后，搜索约 9% 优势不当统计显著结论。私有 PG 18.6 的 fsync/synchronous_commit/full_page_writes/data_checksums 均开启，测试后已停止；完整报告 `/tmp/tofudb-matrix-three-medium.json`，可读结果 `/tmp/tofudb-matrix-results.md`。迁移分支的全域物化、未 fsync 进度、源计数冒充目标计数和缺目标 identity 绑定尚未修复，未把该原型接入当前树。文档门禁仍有 API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、auth_providers_billing、remote_execution 五份既有超行限；未宣称全仓发布门禁通过，未切换应用 authority、未提交/推送/发布。


### 2026-09-08（Tofu-DB：保真导入原语、有界源读取与耐久迁移断点）

- **原生入口：** 新增离线库 `migration.rs::restore_document`，导入前核对实际 CONTROL UUID，保留正数版本号、时间戳和原始 JSON 字节，限制单文档 8 MiB；已存在且完全一致可重放，冲突拒绝覆盖。内联/owner-bound blob 共用原生文档编码，调用方可将索引和进度放进同一个 AuthorityTransaction。领域索引、全部数据映射及可运行的端到端迁移 CLI 尚未实现，不能把这些原语当作完整迁移认证。
- **源与进度：** SQLite 源通过只读 pinned snapshot、精确主键分页及 SQLite 驱动入场限制避免先物化全域/大行，最多 512 表/256 列/8 MiB 行，拒绝临时排序并检测并发提交。进度绑定源摘要/schema、目标 UUID、映射计划摘要，最多 64 领域/64 KiB，只有独立源/目标计数及摘要匹配才持久发布。独占锁、校验和、file fsync→replace→directory fsync；不确定发布后必须重开。完成领域释放游标，保留计数/摘要；恢复仍须重新枚举目标，UUID 和旧进度不能证明目标未被回滚。daemon readiness 从已打开 CONTROL 返回真实 authorityUuid，资源上限和流程进入 Schema IR 及生成的 Python 常量。
- **故障和资源证据：** 新原生 4 项规格覆盖元数据/重放、错目标/owner、非法值零 I/O 入场，以及逐 I/O ENOSPC、短写、丢失同步后的文档/同事务标记一致性与 ACK 恢复。Python 当前 26 项边界规格覆盖各发布阶段故障及真实子进程 os._exit、身份/锁/预算拒绝、源变动；24 MiB 合成源按 256 KiB 行流式读取的 Python tracemalloc 峰值低于 4 MiB，2 MiB 超限单行在 64 KiB 配置下拒绝且 Python 峰值低于 1 MiB。这不是进程 RSS 或物理断电认证。
- **验证与共享树漂移：** 原生实现全套 607 passed / 1 ignored（GC 规模项上一轮已显式通过），strict Clippy 和 release build 通过；相邻 Python 首轮 179 passed / 1 skipped，随后最新迁移 26 + pre-authority 5 全绿。skip 为未启用的真实 PostgreSQL kill/reconnect lane。本轮并行 schema 58→59 后，同步操作目录生成产物（335 操作哈希未变），针对版本和 daemon 进程再验 1+4 绿并重构建；发现 Python 客户端仍写死 schema 58，改由同一目录生成轻量版本常量，客户端/pre-authority 6 绿，生成检查和 scoped Ruff 通过。
- **未通过的门禁：** 最终 release SHA256 `25cd6845bb6e9dd6c47f4df6422ec3e1df8ef25e489b23b2ad52b45cbe2aa54c` 差分 140 passed / 5 failed；失败均为并行新增 conversation_search_v2 的 match_field/role 及 snippet 窗口语义，未删断言或记为已接受差异，分页/标题/分块新契约的完整原生适配仍待完成。共享树文档门禁仍有五份超行预算及新 conversation_search_v2 未登记；没有宣称最终全库全绿。没有重新测性能，旧三引擎结果不能代指此构建。证据 `/tmp/tofudb-migration-results.md`；原生搜索新契约对齐、完整领域迁移/目标独立校验/回滚和剩余发布门槛继续推进，本轮未切换应用 authority、提交、推送或发布。

### 2026-09-08（项目栏口袋伙伴：撤掉常驻风景，把趣味藏进可主动打开的小物件）

- **设计取舍：** 上一版常驻横幅被否定为抢眼、占地。参考 Calm Technology 的低注意力原则与 Playdate 的手动操控，比较页边住客、口袋机关和饰物盒后，改为现有按钮槽内的 32px Tofu；正常项目行约 44px，不另占高度。主动打开 268px 纸口袋，三种手绘 SVG 小物件为线轴花园、茶杯海和信封天空；不是缩小版风景横幅。每件初态加四次操作构成五个固定画面，末态停止，可重置，三个进度整数仅页面会话内保存。
- **所有者与预算：** `features/pocket-theatre/{controller,art}.ts` 与 `25-pocket-theatre.css`，私有 `TofuPet` 复用原项目栏动作位。所有保存场景都默认关闭；day/activity 不打开、不推进、不发气泡。body 上的浮层选锚点空间较大的一侧，短视口只内部滚动，开合/隐藏不移动项目栏或输入框。关闭释放插画 DOM；owner 内零 JS timer/rAF（含玩耍），无图像下载/随机 painter/transport，quiet 与 reduced-motion 保留手动结果但禁 CSS 动效。
- **真实浏览器修复：** 初期 full-shell fixture 用标签正则删 head 内 favicon 时被 data URI 中的原始 SVG 破坏，改整段替换 head、保留真实 body，而非放松布局断言。独立 code review 指出短屏焦点可见性/被动关闭回焦问题；Chromium 新针实际复现：disabled 会立即 blur，必须在 render 前保存是否拥有焦点；完成/重置只滚 pocket，重新打开清 scrollTop。外部 pointerdown 后原生 mousedown 还会清焦，click 后有条件恢复；新外部焦点始终优先。关闭/隐藏不自动重开。
- **最终规格：** Node 行为/生命周期/预算 11 绿；Chromium 7 绿（1440/800/390/320、480×360 CSS reflow、固定几何、三玩具、双语/reduced-motion、短屏键盘完成/重置、真实外点与输入框焦点）；邻接场景加载/day/translator 9 绿；scoped ruff 与 diff-check 通过。`check:frontend` runtime 阶段通过，后两次因并行 stylesheet 源变化导致 stale 拒绝；确认原因后重生成，剩余全部 gate 阶段及最终 build/canary/verify 通过，未改其他特性源绕过检查。
- **证据边界：** `designs/pocket-companion-review/` 保存整壳桌面/移动/三物件及先前 live-app 截图；先前实服验证闭合→打开→推进→关闭零 JS 错误。最终 native Chromium 测试验证布局/交互，不等同审美签收：本轮 root 与独立视觉 reviewer 的 read/inspect 均只收到 base64 文本而非可见图，未谎称完成独立视觉评价。广域文档预算与两项旧 Vite 断言/其他模块 bare-t 检测失败未纳入本次修复；共享树非全绿。当前契约在 `docs/FRONTEND_ARCHITECTURE.md`。仅前端修改，无后端重启、Git 提交或推送。


### 2026-09-08（Tofu-DB：搜索 v2 对齐、删除重建 ABA 修复与真实三后端合同）

- **原生搜索：** 同一 conversation_search_v2 契约生成 Rust 上限；新增有界分页、标题/ID、短语优先与跨字段/分块全部词匹配、用户 originalContent 优先、Unicode lowercase 和原文窗口。每逻辑块 4,096 字符/重叠 512，公开文本字段分别抽取，原生 actor 为 turn 角色权威，不序列化 image/tool 私有对象、不再截掉 10 KB 后尾文。游标绑定源/投影 UUID、租户、owner、查询，最多 8 KiB；每页最多 256 headers/512 chunks，支持巨型单会话中断续查及 generation 变更。压缩归档与标题纳入原生 source worker；8 行/2 MB 入场及 worker 总页数上限保留，超大源明确 partial；巨型源 worker 错误分类等仍待补齐。
- **耐久与资源：** format/source identity 变化只清理对应 owner 的可重建投影，pending backfill 与 ACK 持久化；owner 预算纳入空 header、标题和替换成本。真实归档规格发现 dirty marker 被 ACK 删除后计数重置的 ABA：新修改误复用已发布 generation，旧 ACK 也可能删除新修改。改用每次新鲜 128-bit 持久 token，保留旧 8-byte 读取，精确 ACK/同事务 source mutation 不变。覆盖 partial 替换、归档创建/裁剪、ACK 故障及其他 owner 保留。600 chunks、7,342,254 源字节、512-byte turn ID 的 debug 全测试进程 4.017 s，峰值 RSS 56,108 KiB（54.8 MiB），低于 128 MiB 验证预算；不是 release 稳态 RSS。
- **相邻真实缺陷：** SQLite 搜索校验/取消异常现在先回滚再归还连接；portable 查询扫描完有界候选并在游标携带最佳来源，避免先遇 assistant 就提前返回。PostgreSQL 测试改用逐用例 UUID schema，消除共享数据串扰。真实后端仍复现插件 manifest/row 和 Project Brain binary event 的 JSONB 错绑，以及成本实验把已解码 JSONB dict 当 bytes 导致有效记录丢失；分别保留二进制绑定和共享精确过滤。新增重建规格又复现两后端项目路径迁移只更新 event、遗漏 checkpoint 内部 projectKey；现同事务转换且拒绝损坏/foreign checkpoint，8 项正常/回滚规格通过。备份测试显式验证既有 PostgreSQL 平台托管契约，不假定应用生成本地文件。
- **最终验证：** 原生常规 Rust 615 passed（full library 后修正 server 旧结果字段断言并聚焦补验，含 17 process/filesystem）；strict Clippy/release 通过。默认 ignored 的 GC scale 上轮已显式验证且本轮未改其实现。最终 release SHA256 `0945fc85fa6789d68856c55c3ed9c28de734377aacec977107ac4d27545fbdce` SQLite 真进程差分 146 passed，另真实 PostgreSQL 分页差分通过。稳定代码 SQLite sidecar/cutover 146 passed / 1 PG opt-in skip；真实 PG sidecar/plugin/portable-search/checkpoint 107 passed / 2 SQLite-only fault/seed skip，相应案例在另一后端通道执行。Python 查询 32 passed / 1 portable-only skip，性能工具 26 passed，adapter/cutover/字段 codec 邻测 29 passed；三份生成检查、scoped Ruff/diff-check 通过。最终私有 PG 已停止，该测试库零残留 schema。
- **性能口径修正：** benchmark 现在测完整逻辑分页请求，空页 continuation 也计时；各负载开始前重新握手，避免其他引擎测量超过 30 秒后复用已过期 native socket，不对未知写入结果盲重试。修正后 medium、同 /tmp XFS 三引擎 8 项全完成、无 skip：Tofu 均值胜 PG 8/8（1.52–21.21x），胜 SQLite 5/8。创建 1.037 vs 0.669 ms、追加 1.826 vs 1.238 ms、完整分页搜索 8.904 vs 3.750 ms 仍落后；追加 p99 10.257 vs 1.969 ms、mixed max 40.874 vs 2.972 ms 也需继续处理。PG 18.6 fsync/synchronous_commit/full_page_writes/data_checksums 全开；单次顺序客户端测量不冒充统计显著或并发吞吐。
- **未完门槛：** 完整领域迁移、独立目标校验、回滚/切换、剩余发布与安全门槛、SQLite 搜索与写入/尾延迟差距仍未解决。共享树 documentation gate 六份文件超行预算（API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、auth_providers_billing、llm_io、remote_execution），不宣称全仓发布通过。没有切换应用 authority、提交、推送、部署或重启生产。报告 `/tmp/tofudb-search-v2-results.md`，原始基准 `/tmp/tofudb-search-v2-three-medium.json`；session audit 通过、0 avoidable exec/wait rounds。本轮是进展，目标保持 active。


### 2026-09-08（Tofu-DB：有界搜索加速，同数据交错验证与完整基准反超 SQLite 搜索）

- **定位与实现：** 保留符号的优化构建用 cpu-clock 采样定位重复树遍历、页缓存查找和分配；硬件 cycles 没有样本，未当作证据。查询一次读取最多 16 个 header（从 conversation_search_v2 契约生成），每个查询词仅准备一次 Bloom 位；每页仍最多 256 headers/512 chunks。空 generation 由耐久 generation_bytes 减去同一函数计算的 header 保留量精确判定，确认无文本块才跳过范围查询。新增严格的负大小拒绝；标题跨词、Unicode 大小写展开、空→有文本→空替换及游标完整性保留。没有新增索引、常驻缓存、worker 或磁盘格式；旧 v2 数据直接可读。
- **可信测量：** 基准准备除了等待全部 seed hits，还要求空查询返回 ready coverage，避免无关会话仍未索引时测出虚低耗时；不放宽任何扫描/查询上限。使用同一份已完成索引且进程正常退出的 medium 数据快照，每次复制到独立临时 authority，A-B-B-A-A-B 各 50 warmups/1,000 次完整查询，所有轮次逐项比较同一组 20 hits/snippets。最终三轮均值的中位数 10.450→2.830 ms（减少 72.9%，3.69x）；p99 中位数 15.228→3.144 ms。仅 batch/Bloom 阶段曾实现 10.608→6.985 ms，但两阶段 seed corpus 不同，不混比其绝对值。长且非空的语料仍走有界分块扫描，不声称已拥有全域倒排索引。
- **验证：** 常规 Rust 全套 618 passed，另一个默认 ignored 的 GC 规模项上一轮已显式验证且本轮实现未动；strict Clippy/release 通过。搜索邻套 18 passed，包含原发布故障矩阵及新增边界；性能工具 29 passed，覆盖 ready/partial/indexing 准备门禁。最终 release SHA256 `972b64c026151cb86a20fefa16e8e724c1e4e7897fbb47f6ce18f218ad7100b9` 真进程 SQLite 差分 146 passed，真实 PostgreSQL 分页差分也通过。600 chunks/7,342,254 源字节/512-byte turn ID 的 debug 整测试进程峰值 59,024 KiB（57.6 MiB），低于 128 MiB 验证预算；这不是 release 稳态 RSS。三份生成检查、scoped Ruff/diff-check 通过，94 个原生源/契约文件摘要在测量结束后复核未变。
- **三引擎与剩余差距：** 同 /tmp XFS、medium 顺序客户端八项全完成、无 skip/error，PG 18.6 的 fsync/synchronous_commit/full_page_writes/data_checksums 全开。Tofu 均值胜 PG 8/8（1.57–50.27x）、胜 SQLite 6/8；搜索 3.022 vs SQLite 3.716 vs PG 151.931 ms，搜索 p99 3.329 vs SQLite 4.135 ms。创建 0.999 vs SQLite 0.687 ms、追加 1.840 vs 1.235 ms 仍落后；对应 p99 9.038/9.208 vs SQLite 1.129/2.879 ms。mixed max 50.423 vs SQLite 2.124 ms、sidebar max 34.845 vs 4.005 ms 仍是明确的尾延迟问题。均值领先不能代替这些门槛；完整迁移、独立目标校验、回滚切换、安全与发布认证仍未完成，目标保持 active。
- **收尾：** 私有 PG 已停止，合成数据库与各轮复制数据清理，保留日志/样本/报告。共享树 documentation gate 七份文件超预算（API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、auth_providers_billing、llm_io、remote_execution、task_engine）；包 README 修正了旧的“创建/追加无劣势”说法，未宣称全仓发布门禁通过。未切换生产 authority、提交或推送。session audit 已运行，累计报告 2 个可避免 exec/wait 回合。结果 `/tmp/tofudb-search-acceleration-results.md`，配对原始测量 `/tmp/tofudb-search-same-corpus.json`，三引擎 `/tmp/tofudb-search-batch-three-medium.json`。


### 2026-09-08（Tofu-DB：复用后台打包降低写入 p99，保留 p95 回退为未解决门槛）

- **边界与实现：** 已同步 active WAL 是新历史段的恢复源；最多暂存一个 ≤4 MiB 历史段，复用现有 512 KiB 栈/≤8 MiB 批次打包器，检查点先耐久化全部 staged blocks，再发布历史目录并删除源 WAL。线程持有 RAII 注册，禁止重复 worker，退出/展开/启动失败释放注册，stop/drop 收束线程；带版本条件变量保留“先通知、后等待”的历史到达、目录发布和停止信号。无新增线程、队列或格式版本，无 worker 时保留原同步准备和排空。此前仅把近轮转准备转到后台曾形成 135.9 ms 积压，提前准备但仅轮询仍有 85.4 ms；最终主动唤醒消除这条 25 ms 交接等待。
- **真实链路配对：** 旧 release `972b64c026151cb86a20fefa16e8e724c1e4e7897fbb47f6ce18f218ad7100b9` 与最终 `de206501f0cc3da380c9f12c58aa6a2dc1d1ba08dcd507f12ef1a9ee5589d5d1`，全新临时 authority、medium 创建/追加各 1,000 次、A-B-B-A-A-B。三轮中位数：创建 mean 0.715→0.693 ms、p99 7.810→5.598 ms（-28.3%）；追加 mean 1.590→1.559 ms、p99 9.750→7.652 ms（-21.5%）。但创建 p95 0.828→1.051 ms、max 12.229→15.387 ms；追加 p95 2.586→2.747 ms。保留这些回退为明确待办，不把 p99 改善当作全面胜出。
- **完整三引擎：** 八项无 skip/error，PG 18.6 四项耐久设置全开；Tofu mean 胜 PG 8/8、SQLite 6/8。创建 0.851 vs SQLite 0.645 ms、追加 1.788 vs 1.209 ms 仍落后；搜索 3.063 vs SQLite 3.676 vs PG 147.369 ms。mixed max 87.511 ms、创建 max 20.679 ms 仍需定位，单次最大值本身不证明新根因。没有切换生产 authority；迁移全适配、独立目标校验、回滚切换、安全/发布认证及一致领先仍未达成，完整目标保持 active。
- **验证与预算：** Rust 624 passed（607 lib + 17 process/filesystem），已有 1 GiB GC 项默认 ignored 且实现未改；strict all-target Clippy/release、SQLite 真进程差分 146 passed、真实 PG 分页差分通过。逐 I/O 错误/短写/丢同步矩阵覆盖两条历史发布路径并核对 ACK 事务和载荷；新增单段预算、暂存不写盘、worker 退出 fallback、先发布再准备下一段、注册生命周期及通知竞态规格。微基准补齐 daemon 默认 packer、1..=10,000 参数边界和固定八个慢样本；非法输入在建库前拒绝，嵌套阶段与后台计时不可相加。2,000 次微基准三轮 RSS 范围旧 144,796–164,828 KiB、新 145,072–174,436 KiB，不能用其波动孤立归因 4 MiB 暂存成本；单段上限由确定性断言验证。
- **收尾与证据：** 95 个 native 源/构建输入摘要最终未变。私有 PG 确认停止，回收 112.7 MiB 合成数据，日志保留。共享树 documentation gate 仍五份文档超预算（API_CONTRACT、FRONTEND_ARCHITECTURE、STORAGE、auth_providers_billing、remote_execution），并行修改使全仓结果持续变化。session audit 已执行，累计 496 次工具调用、3 次可避免 exec/wait。报告 `/tmp/tofudb-write-history-results.md`，配对 `/tmp/tofudb-write-wire-ab.json`，全量 `/tmp/tofudb-write-history-three-medium.json`。未提交、推送或部署。


### 2026-09-08（Tofu-DB：去除历史缓冲区拷贝，冷列表首读减少约九成）

- **写入分配边界：** 历史编码 Vec 的所有权直接移交 pending store；借用的普通页面仍走同一套去重/暂存逻辑。WAL 校验用共享的 56-byte header 增量计算 CRC32C/BLAKE3，不再为校验分配完整 framed record；保留 8 MiB 上限及原帧哈希。新增最大历史缓冲区指针身份/重复暂存预算规格，以及跨哈希分块边界、4/8 MiB 载荷的帧哈希对照和崩溃后逐字节恢复检查。中间版 `539837765c44bb08b6b46525a06c502e552272f6283969edc1b30ba71abf3256` 对上一版 `de206501...` 六轮创建 p99 5.669→3.723 ms、追加 6.745→5.835 ms；均值未可靠改善，不把分配减少宣称为已赢得写入均值。
- **冷读取根因与修正：** native-only 慢请求记录把 mixed 的 48.7/44.5 ms 长尾定位到 iteration 9 的首次 conversation.list。无筛选的 limit=50 仍提前读取 1,000 条索引。新增冷 I/O 规格先以 small=40、full=41 次读取失败；普通列表改为只读剩余结果数量，目录分页多读一条证明 has_more，筛选与文件夹 total_count 保留原扫描额度。修正后 list50=4、catalog50=5、list1001=41；同时间戳游标、跨页精确结果、末尾筛选命中、总数和只读行为全部验证。
- **同数据配对：** 最终 release `29481f5becc9fc73f94e0d0171a18dac6bcf4fc4710671d82ab41e9eb318977d` 对中间版 `539837...`，1,001 会话且索引就绪的关闭快照，每种响应形状每轮新复制/新进程，A-B-B-A-A-B，首读单独计时后再测 100 次热读并逐项比对返回值。普通列表首读中位数 19.257→1.782 ms（-90.7%），目录页 19.837→1.379 ms（-93.0%）；热读均值中位数 0.848→0.606、0.641→0.416 ms。语料 560,008,123 bytes，低于 1 GiB 复制语料预算；这是进程缓存冷读，不是 OS 磁盘冷缓存实验。
- **完整验证和仍有的差距：** Rust 626 passed（609 lib + 17 process/filesystem），默认 ignored 的既有 1 GiB GC 项未改；strict Clippy/release、真实 SQLite 差分 146 passed、真实 PG 分页差分通过。95 个 native 源/构建输入摘要最终未变。正式 medium 三引擎八项无 skip/error，PG 18.6 四项耐久设置全开；均值胜 PG 8/8、SQLite 6/8。列表 0.660 vs SQLite 1.443 ms，搜索 3.021 vs 3.859 ms；创建 0.863 vs 0.649、追加 1.785 vs 1.284 ms 仍落后，mixed max 35.645 ms。另一次成功 native-only 诊断 mixed max 10.042 ms 仍是首次列表，不把它当通用尾延迟上界。
- **失败实验与上线边界：** 首次完整语料准备遇到搜索就绪超时；加观测重试时索引已就绪，但 1,725,833,894-byte 语料触发 1 GiB 复制上限，均清理且未计入配对。后续补充 native-only profile 再次在搜索准备超时；带覆盖状态/有界失败快照采集的下一轮通过，因此没有失败快照留下，就绪间歇性仍未解释。上述 560 MB/1.73 GB 存储规模也要求持续负载增长与回收收敛验证，不单凭大小宣称泄漏。复制语料清理，私有 PG 确认停止并回收 112.6 MiB；共享树文档门禁六份文件超行数预算且有并行修改。迁移、独立校验、回滚、安全/发布认证、稳定就绪、资源收敛和全面性能领先仍不完整，整体目标保持 active。未切生产、提交、推送或部署。
- **证据：** `/tmp/tofudb-cold-list-write-copy-results.md`、`/tmp/tofudb-cold-list-same-corpus.json`、`/tmp/tofudb-write-copy-final-three-medium.json`、`/tmp/tofudb-mixed-profile-diagnostic.json`；完整失败日志保留。session audit 已执行，累计 568 次工具调用、3 次可避免 exec/wait 回合。


## 2026-09-09 — Native search backfill coalesces existing dirty work

- A fresh projection over an already populated source previously drained dirty tokens before starting backfill, then rebuilt the entire corpus again. The deterministic 33-conversation regression failed with 66 publications. Backfill now enqueues a bounded header page first and defers dirty keys beyond its pending durable cursor. Explicit subsequent backfills still rebuild every header; new/deleted conversations on either side of the cursor, busy foreground admission, archive coverage, exact-token retries, and owner isolation remain covered.
- `contracts/tofudb_ir_v1.json` now generates the 16-header backfill, 16-dirty-conversation, and existing 1,024-source-page bounds. One metadata page retains at most 128 KiB of inline values, verified at the maximum inline value size across crash/reopen; Turn/archive text remains 8 rows/2,000,000 bytes. No additional worker, queue, retained inter-round snapshot, disk format, or harness timeout extension. Opt-in fsync diagnostics report the existing fixed maintenance counters at daemon exit.
- Fixed 2,069-conversation source, fresh projection, unchanged 250 ms interval: initial ready coverage improved 105.268 → 36.813 seconds; rebuilds 4,138 → 2,069 and source pages 8,276 → 4,138. Neither run had foreground contention or background failure. The candidate's complete paged search returned all 2,069 IDs exactly once. This establishes one deterministic timeout path; the two earlier uncaptured intermittent failures are not conclusively attributed to it.
- Final binary `f50e3937a190d8d61e8b1fb13e0980cd30d4a055c944b28c0618d47bb8b22b80`; 96 sealed build inputs. Focused red/green regression, 43 search neighbors, strict all-target release Clippy, 628 Rust tests (one unchanged large GC certification remains default-ignored), 5 pre-authority contracts, generator check, 146 SQLite differentials, and real PostgreSQL paged differential passed. Full medium comparison completed all eight workloads for all three engines without skips/errors, with PostgreSQL durability/checksums enabled.
- Medium means still beat PostgreSQL 8/8 and SQLite 6/8. Creation/append are 0.887/1.964 ms versus SQLite 0.679/1.207 ms; native p99 is 3.967/6.876 ms. Mixed mean is 0.232 ms, p99 1.094 ms, max 33.707 ms. No foreground write-performance improvement is claimed from these unpaired runs. Readiness progress is not production certification or universal performance parity.
- Private PostgreSQL was verified stopped (pg_ctl status 3); its data/socket and the bounded synthetic source fixture were removed (488,913,358 bytes). Evidence/report: `/tmp/tofudb-backfill-coalescing-results.md`, `/tmp/tofudb-backfill-readiness-{before,after}.json`, `/tmp/tofudb-backfill-final-three-medium.json`. Shared-tree documentation checks still report seven line-budget failures; unrelated concurrent edits were preserved. Search source-page limit behavior, sustained growth/GC convergence, write tails, and migration/cutover certification remain follow-up work.


### 2026-09-09（Tofu-DB：独占 GC 会话复用标记并准确报告回收完成）

- **根因与边界：** `system.reclaim` 原来为计划和执行各生成一份相同标记；确定性回归先以 3 次写入对直接回收的 2 次失败。现在两个阶段共用独占 Engine 借用的会话，最多 4,096 次调用，不跨会话缓存；业务提交、检查点和历史裁剪在此期间不可并发，活跃快照在 I/O 前拒绝。只从稳定数据标记中排除物理目录的元数据引用，每轮额外保护当前目录根，语义引用仍保留。任一轮错误禁止继续复用，显式 finish 检查清理错误，drop 尽力清理。
- **资源和完成语义：** 标记与分段重写共用原临时空间预算；同时容不下时先检查清理，再重写，下一轮重建。CLI 显式 execute 批次最多 4,096 轮/600,000 ms，默认时间 60,000 ms，只在轮间检查时间并报告预算耗尽；未指定批次仍执行一轮。空窗口覆盖保留于会话、目录变化重置、plan 不推进覆盖。新测试还复现最后一次分段重写过早返回完成，已改为继续清理旧目录并检查新目录后才完成。
- **验证：** 26 项 GC、3 项 RPC、7 项跨进程定向验证通过；跨进程夹具曾报告目录租约占用；当时调整释放顺序后通过，但后续确认 BlockStore 本身不持有 LOCK，不能把该失败归因于双重目录租约（见后续 VFS 描述符生命周期修正）。逐 I/O 错误、短写、丢同步、标记损坏、快照、历史裁剪、临时预算不足和崩溃后精确载荷恢复均覆盖。最终 release `8ffad5a6741eabc15269e68fae57c85532e834fc20c4145409d19f62e6cdde8f`：657 Rust passed（638 lib + 19 process/filesystem），既有 1 GiB 项默认 ignored；146 SQLite 真进程差分、5 项预上线契约、生成器 check、严格 all-target release Clippy 和 fmt 通过。97 个构建/测试输入摘要最终未变。
- **生命周期：** 同一万条追加语料，裁剪 192 个历史段并保留 64 个，随后显式会话 1,440 轮仅构建一次标记，90.678 秒从 2,038,960,951 降至 549,475,913 bytes；GC 峰值 RSS 125,148 KiB。原 600 秒/1 GiB/512 MiB 预算全部通过，重开后 552,138,138 bytes，全部 10,000 条响应、WAL 和 authority UUID 一致。此前每轮冷启动模式 600 秒未完成；调用模式不同，不伪称同路径速度配对。重开仍新增 2,662,225 bytes 可重建文件，尚未解释，不以一次语料收敛代表任意持续负载上界。
- **配对与三引擎：** 同千条关闭快照、六轮 RPC A-B-B-A-A-B，关闭两组后台打包以固定语料，单次 reclaim 中位数 309.099→170.221 ms（-44.9%）；整个 native 进程峰值 RSS 中位数 116,688→82,848 KiB，全部业务响应和 GC 指标一致。正常后台写入六轮配对追加 mean 1.199→1.242、p99 4.752→5.432 ms，保留回退并继续定位，不能把 GC 收益归于前台写入。完整 medium 八项无 skip/error，本批 mean 胜 PG 8/8、SQLite 7/8；创建 0.622 vs SQLite 0.644 ms，追加 1.466 vs 1.194 ms，创建/追加 p99 仍同时落后两者。不从单批次推断稳定全面领先。
- **收尾：** 私有 PG 18.6 四项耐久设置全开，真实分页差分通过；确认 pg_ctl status=3 后清理 118,135,834 bytes，两个合成生命周期/配对语料也清理，日志保留。共享树文档门禁仍 10 份既有文件超行数预算，scoped diff check 通过；完整目标仍 active，未切生产、提交、推送或部署。报告 `/tmp/tofudb-gc-session-results.md`，相关原始结果/复现脚本同前缀；重复重开增长、持续调度、前台写入长尾、迁移/独立校验/回滚/安全发布认证仍待完成。


### 2026-09-09（Tofu-DB：消除只读重开分段积累，显式释放实际文件租约）

- **增长根因与实现：** 相同千条关闭快照，旧 daemon 每次只读启动新增 2,640,864 bytes 的未发布 `.pseg`；plan 和关闭打包器的启动均零增长。已完成打包原来只在后续提交/检查点发布。现有空闲维护轮次改为最多发布一个完成分段，非阻塞获取 authority，仅在目录有容量时执行，不合并、不检查点 active WAL；工作线程正常退出在释放注册前清理仍未交接的完成分段，保护已交给前台的 publishing 状态和暂存/WAL 源，显式 stop 返回清理错误。新增回归先分别以目录计数 0≠1、孤立文件 1≠0 失败后通过。
- **租约边界：** 并行 daemon 组出现关闭后 WouldBlock，单独运行通过。dup 回归确认 close 无法在副本存在时结束 flock 生命周期；真实 VFS 现在像模拟 VFS 一样记录独占锁所有者并在 drop 显式 unlock。复制描述符与实际子进程持有副本两项规格通过，后来新租约不受旧副本关闭影响；并行组通过，无重试。未捕获原失败的具体继承进程，因此不声称逐进程归因完成；已纠正前一条 JOURNAL 把 BlockStore/Engine 同时打开当作双重租约的推断。
- **增长与预算验证：** 本版千条语料首次发布新增 2,713,089 bytes，之后两次无新增/删除/变长文件；仓库内三进程规格核对文件内容摘要也保持一致。万次追加裁剪 192/保留 64 历史段并先发布一个完成分段，GC 1,645 轮/一次标记，114.976 秒从 2,036,594,751 降至 552,139,090 bytes；之后三次重开均为 552,139,090 bytes，全部响应、WAL 和 authority UUID 一致，GC RSS 127,932 KiB，原 600 秒/1 GiB/512 MiB 预算通过。物理布局与上一轮不同，不作同语料 GC 速度归因；突发退出孤立文件仍依赖显式 GC，不宣称任意持续负载上界或自动调度已完成。
- **性能与认证：** 最终 `a40da8609486d24ce6eb6267d9523e5c8e889e5d8786c9b570fde7074981a84f`；102 源/构建/测试输入摘要未变。正常后台六轮 A-B-B-A-A-B：创建 mean 0.587→0.566、p99 3.654→3.690 ms；追加 mean 1.248→1.253、p95 1.972→2.223、p99 5.363→5.427、max 中位数 14.513→17.603 ms，保留尾部回退。完整 medium 八项无 skip/error，本批均值胜 PG 8/8、SQLite 7/8，追加 1.327 vs SQLite 1.211 ms，创建/追加 p99 仍同时落后两者，未达全面领先。
- **验证与清理：** 665 Rust（645 lib + 20 process/filesystem）、146 SQLite 真进程差分、真实 PG 分页差分、5 预上线契约、生成器 check、严格 all-target release Clippy、fmt 通过；既有 1 GiB 项默认 ignored。发布/清理逐 I/O 错误、短写/丢同步、载荷恢复与后续检查点均覆盖。私有 PG 18.6 四项耐久设置开启，确认停止后清理 118,086,703 bytes，合成语料也清理；日志保留。共享树仍 10 份文档超行数预算。报告 `/tmp/tofudb-reopen-growth-results.md` 及同前缀原始数据；未切生产、提交、推送或部署，完整目标仍 active。


### 2026-09-09 — Tofu-DB: bounded soft rotation and mandatory hard-cap handoff

- Fixed two red regressions: soft rotation defers checkpoint while background payloads remain unpublished; actual single/group writes crossing 64 MiB complete the drain before append instead of failing after one 8 MiB step. Full drain requires an empty staged-payload set, and condition waiting rechecks Creating under its mutex to retain completion/failure before sleep.
- Canonical activeWalRotation owns the unchanged physical/wait bounds and generates native constants. Four fixed stages distinguish checkpoint drain/wait/clear/swap. No new worker, queue, format or production authority selection.
- Validation: 669 Rust tests (649 library + 20 process), 146 SQLite differential cases, five pre-authority checks, real PostgreSQL paged parity, strict release all-target Clippy/fmt/generator. Existing 1 GiB mark-spill case remains ignored. Deterministic single/group soft and forced handoffs cover per-I/O errors, short writes, lost syncs, crash recovery and continued writes; actual hard-bound tests reopen every payload.
- Six interleaved A/B runs retain all results: native 2,000-append mean median 822.4→775.7 µs (-5.7%), maximum median 17.250→12.486 ms (-27.6%). Peak RSS median 119,028→127,244 KiB (+6.9%); all runs stay within 512 MiB RSS / 2 GiB disk budgets and fixtures are removed. A 24.994 ms WAL outlier remains. Client append maximum median 14.551→11.740 ms improves, but mean 1.215→1.234 ms and P99 4.870→5.272 ms regress; no general write-speed win is claimed.
- The eight-workload medium batch beats PostgreSQL on all eight means and SQLite on seven; append mean remains 1.276 vs SQLite 1.203 ms, and native create/append P99 remains worse than both peers. Six candidate checkpoints have no background wait and near-empty full drains; old-WAL unlink/directory sync still costs about 5–6 ms per rotation. Counters overlap and are not additive.
- All 103 inputs remain sealed. Existing documentation line-budget failures remain; private PostgreSQL is verified stopped and synthetic data removed. Complete evidence: /tmp/tofudb-append-tail-results.md. Production remains pre-authority; release gates and universal harness superiority are incomplete.


### 2026-09-09 — Tofu-DB: bounded background WAL retirement and release-link integrity

- Engine publishes CONTROL before handing one retired WAL to the existing pack worker. The private retirement mutex keeps deletion/directory sync outside the authority mutex. One slot retains at most 64 MiB; the next checkpoint drains it before another rotation, and failed cleanup retains backpressure while preserving an already published checkpoint's result. Worker absence is synchronous; shutdown drains/unregisters atomically and exposes cleanup errors/panics. Reopen derives only the immediate predecessor without scanning; older pre-existing orphans remain outside this handoff.
- New tests pause real unlink while an ordinary commit succeeds, inject per-I/O errors/short writes/lost syncs across publication and deletion, recover exact payloads after crash, and exercise shutdown failure/panic/reopen. Actual single/group 64 MiB admission measures two WALs ≤64 MiB each and ≤128 MiB combined while retirement is held.
- The initial release artifact failed six GC process tests before CLI output. GDB found first-allocation SIGFPE; 12 KiB of initialized .data was zero. Same-layout relinks have identical .text/.rodata/.init_array: link-time stripping reproduced exact broken SHA 4dea0725…, while no-strip linking and subsequent stripping of a separate copy worked. Cargo release strip=false prevents the reproduced linker failure; allocator dependencies are unchanged and about 3 MiB of non-loaded symbols remain. Diagnostic byte-patched copies were never used for gates/benchmarks.
- Final native SHA 19088becccf73b30dbe59de72598f23c16663a2b06ec432eab388c06d5e008cd passed startup, 675 Rust tests (655 library + 20 process), six focused checks, 146 SQLite differential cases, five pre-authority checks, real PostgreSQL paged parity, strict release Clippy/fmt/generator. Existing 1 GiB mark-spill case remains ignored; all 104 inputs and final binaries remain sealed.
- Six interleaved runs: per-checkpoint mean median 8.022→2.675 ms (-66.6%); in-process append mean median 882.1→784.2 µs (-11.1%), P95 1.602→1.340 ms (-16.4%). RSS median 121,548→123,868 KiB; all 512 MiB RSS / 2 GiB fixture budgets pass and data is removed. Client append mean 1.341→1.344 ms and maximum 23.978→23.983 ms are essentially unchanged; P99 5.629→6.108 ms regresses 8.5%. All outliers remain.
- One medium batch beats PostgreSQL on 8/8 means and SQLite on 7/8; append remains 1.410 vs SQLite 1.179 ms, and create/append P99 remains worse than both peers. WAL and non-rotation publication/history-preparation tails remain measured bottlenecks. Private PostgreSQL is verified stopped and synthetic data removed; ten existing documentation line-budget failures remain. Report: /tmp/tofudb-wal-retirement-results.md. Production remains pre-authority and the full release/superiority goal is incomplete.
### 2026-09-09 — Tofu-DB: bounded incremental history preparation

- History preparation uses one ≤4 MiB encoder or pending block and hashes/copies whole WAL records toward 256 KiB per foreground call. The existing segment bytes and hash/sequence checks remain; the shared encoder rejects a bad record before consuming it. No worker or queue was added; activeWalRotation owns the target and lifecycle.
- Soft rotation lets partial encoding and its live worker finish. Hard 64 MiB admission and explicit checkpoint finish the partial buffer and materialize all pending blocks before WAL retirement. Worker exit resumes synchronous preparation; crash reconstructs from the active WAL without a new scan.
- Five new tests cover per-step capacity, corruption/retry, crash recovery, single/group soft rotation, worker-exit fallback, and per-I/O errors/short writes/lost syncs across partial-history checkpoint publication. Final 680 Rust tests (660 library + 20 process), 146 SQLite differential cases, five pre-authority checks, real PostgreSQL paged parity, strict release Clippy/fmt and generator write/check paths pass. The existing 1 GiB mark-spill case remains ignored.
- All three tuning variants and a separately selected final validation batch are retained. Final client append mean 1.384→1.212 ms (-12.4%), P99 5.699→2.902 ms (-49.1%), maximum 11.876→14.090 ms. Synchronous checkpoint tails and filter/WAL stalls remain; no general throughput or worst-case claim follows from P99 alone.
- One medium batch has lower means than SQLite in 7/8 and PostgreSQL in 8/8 workloads. All micro runs pass 512 MiB RSS / 2 GiB fixture ceilings; private PostgreSQL is stopped and synthetic data removed. All 104 inputs and final binaries stay sealed; native SHA 83cead23db0ff303c40afcb716b2367a9d50eb5329198fa6f25dcd6968dfadc8. Ten pre-existing documentation line-budget failures remain. Report: /tmp/tofudb-publication-tail-results.md. Production remains pre-authority and the full goal is incomplete.

### 2026-09-09 — Tofu-DB: reject regressive short-history scheduling

Checkpoint instrumentation confirmed roughly 2.6–3.2 MiB maximum synchronous
history tails. Three one-prefix preparation candidates reduced that local work,
but all three regressed complete-client append mean/P99. The final rejected trial
reduced checkpoint-body mean 7.158→3.194 ms while append mean increased
1.330→1.417 ms and P99 2.891→3.332 ms. All six interleaved samples per trial are
retained; diagnostic reruns do not replace these comparisons. Restored the prior
verified WAL rotation, worker batching and history preparation semantics.

Retained only total/maximum remaining history-byte counters: two fixed AtomicU64
values, no new samples, workers or persistent growth. They measure the suffix
after partial preparation finishes, excluding segment headers, and count attempts
whose later publication may fail. A separate daemon diagnostic found no history
compaction and one search-rebuild run per build; it does not establish a single
cause of the client regression. This change claims observability, not a speedup.

Validation: 680 Rust tests, 146 SQLite differential cases, five pre-authority
checks, strict release Clippy, rustfmt and generator write/check passed; one
existing 1 GiB spill test stays ignored. Final diagnostic smoke met 512 MiB RSS /
2 GiB fixture ceilings and removed its data. All 104 inputs and both artifacts
are sealed. The documentation gate retains ten existing line-budget failures.
Production remains pre-authority. Final native SHA256:
`5b5c51a7c86557c25fe2f15b994aed9399a1ad93f142e13b3db3d7598a3fca60`.
Evidence: `/tmp/tofudb-history-tail-results.md`, rejected `-v1/-v2/-v3` binaries,
source/build seals and A/B reports, `-wire-profile.json`, and final validation logs.

### 2026-09-09 — Tofu-DB: transfer exactly sized entity page allocations

Entity pages allocate their exact encoded length within the existing 16 KiB
format bound and transfer that buffer to pending storage. The owned entry point
removes spare capacity and retains the verified deduplication, WAL-carried payload
and checkpoint barriers; no new worker, retained cache or format is introduced.
Five regressions cover fixed v1 page bytes, maximum page/capacity bounds, allocation
identity/deduplication, exact WAL/reopen state, and every commit fault boundary.

Final interleaved client append mean 1.197→1.180 ms,
P99 2.668→2.453 ms; all samples and regressions are retained.
Micro mean 770.5→747.9 µs, while maximum 11.739→14.122 ms. Median aggregate
pending insertion time 38.7→19.7 ms supports the allocation finding, but overlapping
stage counters are not disjoint request costs. Every micro run met 512 MiB RSS /
2 GiB fixture ceilings. One final medium batch had lower native means in
7/8 SQLite and 8/8 PostgreSQL comparisons; this is not
general superiority or release certification.

Validation: 685 Rust tests, 146 SQLite differential cases, five pre-authority
checks, PostgreSQL paged parity, strict Clippy, format and generator write/check
passed; one existing 1 GiB spill test remains ignored. A concurrent generated
catalog update initially mixed new source metadata with the old binary and failed
the coverage ratchet after 145 passes; the current frozen build passes all 146.
Initial samples/source/hash remain separate. An isolated same-catalog control
then verified the allocation change; its candidate hashes match the tested final
build, and its baseline micro matches the original baseline. Copied timestamps
initially caused a stale Cargo cache hit; identical hashes rejected those artifacts
before timing, and a real rebuild fixed the control. Working Cargo outputs were
rebuilt afterward. The unchanged micro binary reused its complete sample batch.
All 104 inputs and final artifacts are sealed; private PostgreSQL was verified
stopped and synthetic fixtures removed. Documentation retains 10 existing
line-budget failures. Production remains pre-authority.
Native SHA256: `62320abe85c370dfd015b2ab6ffa1a8aa822f8d5ce648a2b4f60550e404308d7`.
Evidence: `/tmp/tofudb-page-staging-results.md`, source/build seals, A/B samples,
`-before-catalog-*`, and final correctness/three-engine logs.

### 2026-09-09 — Tofu-DB: retained artifacts and atomic Research publication

The eight `paper.artifact.*` operations now execute natively with generated
receipt validation, bounded chunks and leases, a tenant-wide reserved-byte/count
index, and owner-filtered cleanup of at most 32 chunks per call. Generic
record/blob documents own the bytes. Research workspace CAS, retiring the prior
build and pinning the verified replacement share one authority transaction;
discard cannot undo a committed pin after a lost acknowledgement. The native
launch probe owns the retained budget and readiness exposes its resolved value.
Python chunk planning now converts schema-valid integral float sizes at its
arithmetic boundary; strict schema-version validation remains unchanged.

Verification: 694 Rust tests pass (one existing ignored), strict Clippy/format
and generators pass, all 151 SQLite differential tests pass, and PostgreSQL 18.6
passes nine real-process tests plus five retained differential scripts with all
durability settings enabled. Sixteen retained Python tests pass; a concurrently
expanded replay test was rechecked separately. Per-I/O failures, short writes,
and dropped syncs cover upload, seal, cleanup and coupled publication. Native
GC/backup/restore preserves ten chunks, a blob-backed receipt, the workspace/pin
and shared capacity; a lower restored budget rejects new work without deleting
the pinned PDF. The docs gate retains ten existing line-budget failures.

Warm 8 MiB artifact uploads over three rotated fresh-authority runs achieve
40.19 / 56.24 / 27.78 MiB/s on Tofu-DB / SQLite / PostgreSQL. Native append mean
is 5.348 ms per 256 KiB versus 3.120 / 6.854 ms; seal, read and workspace publish
lead both references. Cold results retain Python's lazy schema startup cost as
separate evidence. The current eight-workload medium core batch leads SQLite
on six means and PostgreSQL on eight; create (0.677 vs 0.639 ms) and turn append
(1.342 vs 1.173 ms) remain SQLite gaps. These shared-host samples do not establish
statistical dominance or replace the previous matched allocation experiment.

Evidence: `/tmp/tofudb-retained-results.md` and the matching source, build,
verification, drift, raw-sample and cleanup receipts. Frozen native SHA-256:
`d68b5b5400a018b8d3a7a490ee0afb74b39ae1e3f0a51eb00b037a68feda6bed`.
141 source inputs are sealed. Concurrent unrelated build-schema/comment edits
left generated native inputs unchanged. Private clusters and backup fixtures
were verified stopped and removed; logs and final native/source artifacts remain.
The registry now has 351 operations, of which 343 execute natively; the eight
new `paper.document.*` operations and complete migration/replay/release gates
remain work. Production selection is still prohibited by the pre-authority guard.

### 2026-09-09 — Tofu-DB: validate retained chunks once and bound client encoding

Strict Base64 admission now creates immutable canonical chunk bytes and their
digest once. Request validation charges bounded fields without reserializing the
chunk; replay hashing streams the exact prior canonical JSON. Consuming payload
parsing is scoped to the retained-artifact compiler. Persisted bytes, hashes,
owner fencing, quota, fault rollback and secure zeroization remain unchanged.
The storage.v2 Python client optionally accelerates compatible large-text JSON;
its eligibility walk is bounded to 4096 values and depth 32, with exact stdlib
fallback for floats, unsupported shapes and unavailable encoders. Benchmark
metadata now identifies the client source and codec configuration.

The final six-run full-stack control lowers 256 KiB append mean from 5.185 to
3.080 ms (40.6%). Three rotated fresh-authority warm uploads achieve 64.23 /
57.31 / 27.19 MiB/s on Tofu-DB / SQLite / PostgreSQL, including prepare and seal.
Native daemon VmHWM is recorded separately from client/application memory.
An earlier global parser change was rejected after create regressed from
0.528 to 0.577 ms; its binary, source and negative results remain preserved.
The selected scoped version's matched core means are create 0.543→0.523 ms and
append 1.224→1.224 ms; append P99 is 2.730→2.263 ms. These shared-host samples
do not establish statistical dominance. The final medium three-engine batch
has six lower means and one tie against SQLite, eight lower means against
PostgreSQL; turn append still trails SQLite, 1.289 versus 1.197 ms.

Verification: 696 Rust tests pass (one existing ignored), strict Clippy and
format/generator checks pass, 151 SQLite differential tests and five PostgreSQL
retained differential scripts pass. Four wire/encoding tests, five pre-authority
checks and 29 performance-tool tests pass; the stronger metadata assertion was
rechecked separately. Native GC/backup/restore preserves ten chunks, a
blob-backed receipt, the workspace/pin and tenant capacity; a lower restored
budget rejects new uploads while preserving pinned reads. The docs gate retains
ten existing line-budget failures. All 144 sealed source inputs remained stable.
Four stopped private fixture directories were removed, reclaiming 572,938,361
bytes; raw samples, profiles, logs and source/binary controls remain available.

Selected native SHA-256:
`982063486d8272cc94707c2392a18d165853ac79083ebc062ddd421ffc19c61b`.
Evidence: `/tmp/tofudb-chunk-results.md`, selected `-scoped-*` receipts,
`-combined-ab.json`, component controls and rejected global-parser evidence.
Coverage remains 343/351; document operations, SQLite write gaps and full
migration/replay/release certification remain active work. Production authority
selection was not changed.

### 2026-09-09 — Tofu-DB: saved document identity, publication and reading CAS

All eight `paper.document.*` operations now execute through native bounded IR,
bringing the current executable inventory to 351/351. The generator checks the
declared count against the actual inventory. Generated public schema validation
also owns local/cloud source and reading shapes. Canonical ASCII JSON preserves
reference source-binding hashes and completed-receipt retry digests. Generic
owner-bound records hold at most 64 KiB; a tenant-shared 16-byte count refuses
document 129 and conversation association 513 without deleting durable state.
Only transactions containing document IR admit its new capacity namespace.

The requested generation's first terminal receipt, retry result and retained
PDF pin commit together. Expired, abandoned and superseded generations cannot
publish; an unverified replacement cannot displace a verified manuscript.
Reading revisions remain independent and preserve writer/base-revision/sequence
ordering, including older saves arriving after a final save. No source-path
lookup, catalog scan, resident worker or cache is added.

Final verification: 705 Rust tests pass (one existing ignored), including nine
new document tests and the adjacent retained tests. Registration/publication
fault matrices cover every I/O error, short write and dropped sync; capacity,
maximum Unicode source, query mutation rejection and owner fences are checked.
Strict Clippy/format, generator checks and five pre-authority tests pass. All
156 SQLite differential tests and nine PostgreSQL retained/document scripts
pass. Seven reference repository tests pass. Native GC/backup/restore preserves
ten chunks, a blob-backed receipt, document/source bindings, reading state, the
Research workspace/pin and both full catalog counts; restored excess admission
is refused and pinned bytes remain readable under a lower artifact budget.

Three rotated warm document runs have mean request/finish/reading/current
latencies of 0.222/0.341/0.304/0.137 ms natively, versus
0.562/0.968/0.930/0.571 ms on SQLite and 1.361/2.292/2.214/1.495 ms on PostgreSQL.
Warm 8 MiB uploads achieve 66.15/53.16/27.19 MiB/s respectively. The final core
batch has lower native means in six of eight SQLite and all eight PostgreSQL
comparisons; create 0.736 versus 0.733 ms and append 1.518 versus 1.424 ms remain
SQLite gaps. These shared-host results do not establish general superiority.

The initial globally admitted capacity namespace variant measured paired append
mean 1.212→1.274 ms (+5.1%), with improved P99. Its source/binary and complete
negative samples remain archived. The selected scoped version's separate paired
control has create mean 0.569→0.556 ms and append 1.197→1.213 ms (+1.3%);
append P99 2.896→3.026 ms still increases. This is not a no-regression claim or
proof that the namespace grant alone caused the initial timing difference.

An early adjacent test overlapped a concurrent retired-reader policy change;
the final Python, Rust and updated test were verified together. The first wire
document scripts also used an owner different from their authenticated fixture;
that test setup was corrected while separate authorized-owner tests remained.
All 150 final sealed inputs match the selected source. Four stopped fixtures
across both candidates were removed, reclaiming 854,437,124 bytes; raw samples,
profiles, logs and both native/source controls remain. Documentation still has
ten existing line-budget failures. Production selection was not changed.

Selected SHA-256:
`34991bf1b619b5b256fe5d0cdcc73cd6c3d947838228fd2e1d95142f9cf2dad2`.
Evidence: `/tmp/tofudb-document-scoped-results.md`; the initial variant and
its regression remain under `/tmp/tofudb-document`. Full-domain migration,
logical replay, cutover/rollback, other release gates and SQLite write
performance remain active work despite complete registered-operation coverage.

## 2026-09-09 — Exclusive record migration and PostgreSQL JSON scalar parity

Added an explicit `tofu-db migration-session` operator child with one exclusive
existing-authority lease, private inherited pipes, fixed tenant/owner, UUID
checks on every request, deadlines and generated frame/page limits. The Python
coordinator uses snapshot insert-or-match instead of `record.put`: raw JSON,
version and timestamp survive import and exact retry. A 128-key owner catalog,
indexed source lookups and independent target point reads replace materialized
record catalogs and the 1,000-row verification ceiling. Incremental verified
prefixes permit matching commits ahead of the durable checkpoint; supplied
source bindings are always recomputed. Old live TCP invocation and mapping
checkpoints fail closed. Managed task-result, continuation and paper namespaces
require their domain adapters. Wallet verification now includes alert threshold
and timestamp, so unrepresentable snapshots cannot receive a complete receipt.

The real process test imports 2,005 records, kills the child immediately after
commit 257 and resumes from the earlier checkpoint. Version 1042, timestamp
123456789 and original JSON spacing/number spelling survive. Python traced peak
is 437,904 bytes and native VmHWM is 37,628 KiB, below the 24/512 MiB test bounds.
These measurements cover streaming records, not every domain's import buffers.

PostgreSQL parity testing exposed JSON strings being parsed twice: `"v2"`
raised an internal error, while JSON-shaped strings could change type. Pooled
loaders now mark decoded strings explicitly and the shared codec unwraps them
once. Structured JSONB results keep their prior representation. Returning all
JSONB results as raw bytes was rejected by the existing checkpoint byte-evidence
test because PostgreSQL normalizes JSON text; the final marker preserves that
integrity check. Both wire formats and connection-local loader isolation pass.
The revoked-credential fixture was corrected to include disabled=true, matching
both engines' revoke operation; wallet timestamp mismatch remains a rejection
case instead of being omitted from verification.

Validation: 689 Rust library and 20 process tests passed, one existing ignored;
all four new native tests passed again after a cursor-only Clippy API update.
Strict Clippy, formatting and generator guards passed. Migration/source/
checkpoint tests cover 21/26 cases and five pre-authority guards. SQLite native
differential passed 156 cases; the final binary passed 14 PostgreSQL record,
credential and billing scripts. PostgreSQL Sidecar contracts passed 98 cases
with two skips. SQLite Sidecar/process tests passed 152 with one skip, plus the
new scalar round-trip contract; final scalar loader tests passed all 13 cases.
Five owned PostgreSQL fixtures were stopped and removed, reclaiming
1,612,648,551 bytes. Session audit completed. Documentation still exceeds eleven
repository line budgets; no limit was relaxed. The shared worktree also contains
unrelated concurrent edits, so evidence is tied to sealed inputs.

Final binary SHA-256:
`5a4f287c4dcea5b0fc9d8a3973d3304e90017db58eef48646ae76642328703f0`.
Evidence: `/tmp/tofudb-import-results.md`, final binary, 162-file source manifest
and source archive under `/tmp/tofudb-import-*`. Full-domain migration, complete
credential/billing catalog verification, exact wallet adapters, cutover/rollback,
logical replay, release selection and SQLite write performance remain active
work. No production authority or startup selection was changed.


## 2026-09-09 — Engine write-path burst optimization: fresh staging removes the dedup probe

- Profiled the settled Turn append hot path (`turn_append_bench`, stage attribution
  plus `perf` symbols on a local-disk authority): mimalloc allocation ~13%, libc
  memcpy ~7.5%, blake3 ~4.3%, serde_json ~3-4%, lock/Weak overhead ~2%. The
  dominant per-commit CPU costs were put_deferred (~138µs across 14-32 pages),
  load_node (~70µs), and WAL double-fdatasync (~150µs; DropSync contract, kept).
- Four changes landed in the engine write path. (1) LeafFrame frame-based leaf
  packing packs modified adjacent leaf runs into the encoded page buffer directly,
  eliminating ~450 entry clones and a re-encode per commit. (2) put_deferred keeps
  one pending-set probe and a cache read-lock peek instead of the write-lock miss
  path. (3) append_settled settlement moves projections instead of cloning. (4)
  Fresh-write staging: copy-on-write pages and rebuilt root directories stage
  through `put_deferred_fresh(_owned)`/`write_block_fresh(_owned)`, skipping the
  durable dedup probe for content the engine just encoded. Staging stays
  idempotent through the pending-set entry API, the WAL envelope remains the
  durability point, and a coincidental durable duplicate costs WAL bandwidth
  only. The owned entry points drop spare capacity so pending residency stays
  bounded by encoded bytes; exact-allocation encoder buffers are retained
  untouched.
- Correctness ladder: 726 Rust lib tests passed, 0 failed (the two earlier
  InvalidPayload failures from concurrent schema work cleared once that work
  reconciled). Fault matrices unchanged: per-I/O ErrorBefore/ShortWrite/DropSync,
  reference-payload survival, allocation-retention, and exact-capacity bound
  regressions all green. The WAL barrier keeps its double sync_data (DropSync
  contract). One real regression was caught by the ladder and fixed before any
  measurement: the fresh owned path initially retained excess Vec capacity,
  violating the pending-residency bound asserted by
  `owned_staging_reuses_exact_allocations_and_bounds_retained_capacity`.
- Paired A/B, same machine, 400-iteration turn_append_bench, back-to-back runs.
  Control build (this session's engine edits reverted, concurrent work kept) vs
  current: /dev/shm mean 456.9/477.4µs -> 399.3/378.8µs (~13-19%); per-window
  put_deferred 70 -> 38µs, write-path block_get/get_unc_miss probes 13-32 -> 0.
  beegfs FUSE authority under identical cluster load: control mean 17,143µs
  (get_unc_miss alone 10-21ms per window, ~28-30 FUSE lookups at ~700µs each)
  vs current 2,485.8/2,669.3µs — 6.4-6.9x. The FUSE win is the durable dedup
  probe elimination; WAL fdatasync pairs are paid equally by both binaries.
  Absolute FUSE latency is cluster-drift-bound today (wal ~1.5ms vs ~188µs in
  calmer sessions); only the back-to-back pairing is claimed.
- End-to-end SQLite comparison (medium scale, paired, fresh release binary
  SHA-256 `c16fb682f2f0651e7082abcd1acb6086cff06c532f13ae6f0e8a2705e4c4f344`):
  turn_append_burst p50 0.76ms / 1213 op/s vs SQLite p50 1.24ms / 826 op/s
  (1.47x faster mean); conversation_create_burst p50 0.46ms / 1930 op/s vs
  0.59ms / 1599 op/s (1.21x faster mean). Creation p99 1.53ms still trails
  SQLite's 0.93ms — tail parity is not claimed. One comparison run skipped the
  tofudb engine with `storage.v2 peer closed mid-frame`; an immediate rerun
  with the same binary passed twice (tofudb-only and paired), attributed to a
  transient daemon teardown race between back-to-back harness launches, not
  observed again.
- Docs updated: `packages/tofu-db/README.md` (fresh staging contract, current
  SQLite comparison state) and `docs/STORAGE.md` (entity staging sentence).
  Raw evidence under `/var/tmp/tabperf/`: p4a/p4b (tmpfs), p4fuse/p4fuse2,
  ctlfuse, ctl1/ctl2, opt6, p4_vs_sqlite.json, p4_vs_sqlite_create2.json.
  No production authority, startup selection, or remote was changed. Follow-up:
  create/append p99 tails (history_stage spikes ~850µs and pack rotation),
  sustained-load growth, and the remaining six workloads were not re-measured
  this session.


## 2026-09-12 — Startup recovery and native Supervisor implementation

The live SQLite model-routing authority had row revision 83 but document
revision 82. Its document passed current normalization with revision as the
only changed field. An explicit operator repair saved a private, fsynced
preimage in the runtime data directory and committed revision 84 through the
existing semantic CAS; repository reread matched the expected document. The
integrity check remains intact. This establishes the recovery, not the cause
of the earlier inconsistent write. The managed application completed all nine
startup phases in 58.7 s; live/ready/startup probes returned HTTP 200.

The selectable-backend documentation had outrun the actual Supervisor: native
launch helpers were missing and the native test module could not collect.
Supervisor now resolves the binary and explicit scope, negotiates v2 and
propagates launch budgets through the existing client adapter. A child-only
path resolver acquires the shared project lease and preserves it across exec
into tofu-db, preventing simultaneous native/SQLite authorities. It publishes
a credential-free locator; missing native data fails without init or fallback.
Native shutdown uses stdin EOF and a bounded wait before kill, permitting
normal exit status 0. No production backend selection was changed.

Verification: 53 model-routing contract tests; 15 initial native/client tests;
122 migration/process-boundary tests; 167 native/SQLite differential tests;
156 final supervision/SQLite contracts (one opt-in PostgreSQL skip); final
10 process-boundary tests, Ruff, scoped diff check and architecture check pass.
The initial pytest invocation hit unrelated auto-loaded napari/OpenGL plugins;
subsequent runs disabled plugin autoload. Documentation gate remains red for
two uncataloged artifacts and nine line-budget overruns in the shared tree.
Session audit ran with zero avoidable exec/wait rounds.

Production cutover remains incomplete. The discovered source is 116,560,273,408
bytes, and the declared v9 importer omits populated compaction/raw archives,
trash graphs, attempts and sync state, among other tables. Passing fixtures
therefore do not establish complete production migration. Preserve the source
and implement/verify those domains before switching. Evidence:
`/tmp/tofu-native-source-coverage.json`, `/tmp/tofu-native-differential.log`,
`/tmp/tofu-native-migration-tests.log`,
`/tmp/tofu-native-supervision-regression.log`, and
`/tmp/tofu-startup-docs-check.log`. The full user goal remains active.


## 2026-09-12 — Root compaction archive migration and bounded resume verification

The v10 offline plan adds root compaction archive restore/get/scan, generated
from the IR contract. The native adapter publishes content, global identity,
chronological index and count atomically, preserves pre/post messages, summary,
receipt, original timestamp and source payload byte count, and rejects
divergent replay. Legacy compressed messages hydrate under an 8 MiB transcript
ceiling. Detached archives remain retained but live access still requires
the active owner-scoped parent. Child scopes explicitly remain unsupported.
Live source inspection found 2265 root archives, maximum 50 per conversation,
including 12 without an active parent; none were deleted or migrated in place.

Shared document-domain resume verification retained every decoded source row
in an unused list. The regression reproduced 17,927,524 bytes of peak retained
Python allocation for 4096 small documents; removing the list passes the
2 MiB verification ceiling, including independent catalog enumeration. A
completed checkpoint now re-enumerates the target as well as verifying source
point reads, so an extra target row cannot evade completed-resume verification.

Verification: 115 final migration/resume/source tests, 5 final archive semantic
differential cases, and 4 final native archive cases pass against the rebuilt
debug binary. Native library tests passed 844 with one existing ignored before
the final detached-parent increment; the final four archive cases and five
differential cases cover that increment. Generator drift, Ruff, scoped diff
and architecture checks pass. Documentation retains two uncataloged artifacts
and nine line-budget failures; no gate was waived. Evidence and binary SHA:
`/tmp/tofu-archive-migration-results.json`,
`/tmp/tofu-archive-final-migration.log`,
`/tmp/tofu-archive-final-differential.log`,
`/tmp/tofu-archive-native-final.log`.

Service remains ready on SQLite, PID 181331; production cutover is still
incomplete. Next concrete data dependencies: 58,856 raw archives, 3,682
attempts, 1,303,312 attempt events, 22 tombstones and trash/sync graphs. The
current Turn importer also refuses 3247 already-terminal Turns solely because
current_attempt_id is non-null (2952 completed, 64 failed, 193 interrupted,
38 truncated); preserving those pointers requires the Attempt adapter, not
clearing historical identities. Source and production selection are unchanged.


## 2026-09-12 — Terminal Attempt import and production-schema preflight

Offline import plan v11 adds terminal Attempt snapshots before Turn restore.
The private native adapter preserves command/config/error/timing/queue facts,
rebuilds identity, command, per-Turn directory/count and retention indexes in
one commit, independently scans owner catalogs, and retains detached history.
Turn restore now preserves current_attempt_id only after verifying the imported
terminal Attempt belongs to that Turn. Live attempts remain refused. Python
source ownership requires conversation, Turn, trash or tombstone evidence.

Read-only production preflight verified all 3682 Attempt owners and snapshots.
It exposed two fixture blind spots: storage_conversations really has the
(id,user_id) primary key, and one historical failed Attempt has a JSON string
error. The importer/fixtures now use the composite key and preserve the exact
JSON error value. Declared-table schema comparison still finds missing
hinted_visits/hint_match_visits in storage_browser_site_observations; no source
repair or backend switch was performed. Event/raw/trash/sync migration remains
unfinished. serverctl reports PID 181331 live and ready on port 15000.

Evidence: /tmp/tofu-attempt-rust-full.log records 848 passed/1 ignored before
the final JSON-error compatibility edit; /tmp/tofu-attempt-native-final.log
records all 3 native adapter tests passing afterward. The neighboring migration
and differential run passed 288 tests before the composite-key fix; the full
import module then passed 91 tests with the corrected composite-key fixture.
Final executable integration is recorded separately after the rebuild.
Source preflight: /tmp/tofu-migration-schema-preflight.json. Documentation check
remains red on existing uncataloged artifacts and line budgets, including
STORAGE.md; no gate was waived. Session audit was run for the active rollout.

Final rebuilt executable verification: all 91 importer tests passed in
/tmp/tofu-attempt-final-integration.log, including string-error round trips
and detached history. Binary SHA-256:
8a835995c3ec196019908f9393b56113b9180c87a01b9b62116cc7a3d6ca0881.
Generated contract drift and Ruff checks pass; architecture check passes over
2435 authored files. No production cutover has occurred.


## 2026-09-12 — Repair published browser-observation schema evolution

Production-schema comparison found hinted_visits and hint_match_visits absent
from storage_browser_site_observations: the published version-58 CREATE statement
had grown fields without an additive upgrade for existing authorities. Schema
62 adds each missing counter with a zero default, preserves any existing value,
and leaves prior observations untouched. It never infers hint usage from success.
The storage operation catalog was regenerated and the native binary rebuilt.
Schema tests now compare against the authoritative version rather than stale 60.

Validation: 16 schema tests pass, including both missing columns and one already
present column, retained row values and repeated migration. Neighboring storage,
process-boundary, browser and deferred-startup contracts: 178 passed, 1 PostgreSQL
environment skip. New native browser/schema differential checks: 2 passed.
The first filtered native invocation selected no tests (exit 5); corrected node
selection is the two-test result in /tmp/tofu-schema62-native.log.

Applied via the authorized lifecycle restart. serverctl reports PID 85757 on
port 15000, live and ready; production source reports schema 62 and zero declared
migration-plan schema mismatches (/tmp/tofu-schema62-source-preflight.json).
The browser observation table is empty. No tofu-db authority cutover occurred;
attempt events, raw archives, trash/sync and other source coverage still require
adapters and verification. Existing documentation inventory/line-budget failures
remain; no release gate was waived. Session audit was run.


## 2026-09-12 — Attempt event migration and production size discovery

Plan v12 restores Attempt events through a private native adapter. Original
UTF-8 event JSON text/BLOB bytes and created_at are retained; envelope identity
must match source columns and the imported terminal Attempt. Event publication
and maximum-sequence head updates are atomic. Retention gaps remain gaps, and
independent 128-row catalogs verify target identity/head consistency. Native
payload accounting derives from actual bytes rather than source bookkeeping.
Live reads still require active owner-visible conversation and Turn state.

Verification: 124 migration/boundary/residency tests passed, two real-child
active/detached event resume cases passed, and final Rust library suite passed
849 with 1 ignored. Native fault/reopen test covers retained sequence gaps,
ordinary read visibility, foreign-owner isolation and invalid head rejection.
Binary SHA-256: 5dd05953a0145cbc0fb4c2b2297926873c6f5720724a65b6947f0fde8866350e.
Ruff and generated contract checks pass. Architecture passes; pre-existing
documentation catalog/line-budget failures remain unwaived.

Production sample: first 4096 events passed after adding bounded UTF-8 BLOB
decoding. Full source format traversal exceeded 1.1 million rows, then correctly
stopped at SQLite's 8 MiB row admission bound. Therefore full event coverage is
NOT certified: oversized historical events need bounded complete import/read
support, and total-volume throughput must be measured before cutover. A live
source parent scan also rejected certification because the source changed;
final source/target binding requires a stopped-write snapshot. Runtime remains
SQLite, PID 85757, live and ready. No production tofu-db cutover occurred.
Evidence: /tmp/tofu-event-{integration,regression,native-unit,rust-full}.log,
/tmp/tofu-event-full-preflight.log and /tmp/tofu-event-source-sample.json.


## 2026-09-12 — Bounded source streaming for the oversized Attempt event

The live read-only event-size diagnostic found 1,282,064 retained events totaling
15,857,047,653 bytes, with one oversized event of 25,294,288 bytes. These are
live diagnostic observations, not a stopped-source migration binding; background
retention explains changed counts across probes. Results are in
/tmp/tofu-event-size-diagnostic.json.

Added SQLiteMigrationSource.blob_chunks: exact primary-key selection, read-only
incremental TEXT/BLOB handle, generated maximum 256 KiB chunks, explicit caller
size ceiling, and generator/connection lifecycle cleanup. It does not raise the
ordinary 8 MiB row admission limit. Tests cover exact hashes for large text and
binary values, total/chunk limits, wrong keys/owner, missing rows, early iterator
close, and Python allocation peak below 1 MiB. All 28 source-boundary tests pass;
Ruff and generated-contract checks pass.

The production oversized event streamed all 25,294,288 bytes with Python peak
527,801 bytes and SHA-256
4ee565e498c58236ea2f68ebaf64e9fc6c99b4c2e84be16f352a5a764d7ed225
(/tmp/tofu-large-event-hash.json). No payload dump or source mutation occurred.

Remaining work: connect this source primitive to native offline chunk
publication and bounded oversized-event reads. The existing storage.v2 protocol
already supports 64 MiB streamed requests/responses, so ordinary 8 MiB frames
need not grow. Do not certify the event domain or perform production cutover
until complete large-event round trips and fixed-source verification succeed.
Architecture/documentation gates and session audit were run; existing doc
inventory/line-budget failures remain unwaived.


## 2026-09-12 — Complete oversized Attempt event round trips

Plan v13 hashes event metadata plus source-byte manifests, with event bodies
read through the bounded SQLite BLOB iterator. Small events use normal restore;
larger ones use begin/append/finish/abort over the private operator pipe. One
volatile buffer per child is capped at 32 MiB, decoded chunks at 256 KiB; offsets
permit only contiguous append or identical replay. Length and SHA-256 are
verified before atomic event/head publication. EOF, abort and failed finish
release pending state. Ordinary oversized operator gets fail with a bounded
chunk-read instruction rather than killing the child.

Versioned-document helpers now accept domain-supplied read bounds while existing
callers retain 8 MiB defaults. Historical Attempt events have a generated 32 MiB
cap; event pages stop before 64 MiB minus protocol headroom. The existing v2
response-chunk transport delivers complete large events without enlarging
ordinary frames. Active-parent and owner read fences remain intact.

Final validation: 127 migration/boundary/residency tests pass; the 25 MiB test
includes incomplete-child EOF followed by successful reimport, exact hash and
metadata reads, child survival after an oversized normal get, and complete
ordinary application-protocol response streaming. Its Python peak stays below
8 MiB and native Linux RSS below 512 MiB. Neighboring event/attempt/recovery
differential cases: 20 pass. Final Rust library: 849 pass, 1 ignored. Ruff,
generated contract and architecture checks pass. Documentation inventory and
line-budget failures remain unwaived. An earlier wire-test launch raced a
binary rebuild (ETXTBSY); the final stable-build run passed.

Actual production event: 25,294,288 bytes were streamed into an isolated temporary
native authority, reopened, and independently matched by SHA-256 and metadata.
Hash: 4ee565e498c58236ea2f68ebaf64e9fc6c99b4c2e84be16f352a5a764d7ed225.
Upload 5.177 s; Python peak 2,428,631 bytes; native RSS high-water 97,030,144 bytes.
The scratch authority was removed afterward; production source was read-only.
Evidence: /tmp/tofu-production-large-event-result.json,
/tmp/tofu-large-event-regression-final.log, /tmp/tofu-large-event-budget.log,
/tmp/tofu-large-event-rust-final.log. Binary SHA-256:
4b5ae2329725470325e968985eb7fa22634ca76de021b2edd4d1bcdf52aab2d9.

This resolves the observed oversized-event path, not full-authority cutover.
Remaining work includes full event throughput/fixed-snapshot verification and
raw archive, trash, child archive, sync and other source-table coverage. Service
remains SQLite, PID 85757, live and ready; no production backend switch occurred.


## 2026-09-12 — Raw archive decoder and conversation-capacity compatibility

Read-only production inventory: 58,856 raw archives, 4,294,965,581 stored bytes
(just below the unchanged 4 GiB tenant ceiling), largest compressed archive
2,062,761 bytes, largest request 7,622,250 bytes and response 8,663,155 bytes.
The largest conversation has 1,734 archives; largest Attempt usage is 16,777,119
bytes, within the unchanged 16 MiB cap. Evidence: /tmp/tofu-raw-source-budget.json
and /tmp/tofu-raw-distribution.json. Raw archive import remains pending.

Fixed native strict inflation to pass only unconsumed compressed bytes on each
output window and reject no-progress loops. Existing EOF, trailing-data and
64 MiB decoded-limit checks remain. Added a multi-MiB preservation regression
and expanded the real SQLite/native differential read to a byte window beyond
2 MiB in a multi-MiB provider response.

Raised the generated per-conversation archive cap from 512 to 1,792, covering
observed data while keeping the raw-archive deletion component within existing
point-witness and write budgets with reserved room for lifecycle work. Boundary
verification creates the maximum archive set, rejects one more, atomically
deletes the set and confirms zero counts. Tenant/Attempt bytes and owner count
caps are unchanged; no new pruning or deletion policy was introduced.

Validation: all 11 raw module tests pass (capacity test 105.55 s for the complete
filtered run), one semantic parent/quota/owner test passes, both exhaustive raw
write/delete fault-recovery tests pass, and both final native differential cases
pass. Generated drift, Ruff and architecture checks pass; existing documentation
catalog/line-budget failures remain unwaived. The first pytest selector contained
an invalid space expression; the corrected final selection passed. Session audit
was run. Final binary SHA-256:
b1e15d7ebadd194aa5a7c7e12684c791059f07eb0cd27740aeb4b58edf9a933e.
Logs: /tmp/tofu-raw-{capacity-tests,semantic-tests,fault-tests,differential}.log.
Production remains on the recovered SQLite authority; no native cutover occurred.


### 2026-09-12 — Exact raw archive offline migration

Plan v14 now includes storage_raw_archives. The native private adapter preserves
compressed parts, missing-part metadata and detached owner history; source quota
limits refuse import rather than applying the live writer's content scrubbing.
Full snapshot replay is exact, all covering indexes and counters publish in the
same transaction, and independent catalog plus source-grouped counters gate the
completion receipt even on completed-checkpoint resume. Source aggregates stream
with file-backed temporary work; ordinary frame/source-row limits remain strict.

Validation: three native migration tests plus thirteen neighboring raw archive
and fault tests passed; all 129 migration Python tests passed, including native
crash/resume, extra target rows and counter mismatch. Read-only production
conversion covered 58,856 rows without errors. Its largest 2,062,761-byte archive
restored in an isolated native authority and matched exactly after reopen, with
Python peak 8,255,291 bytes and native RSS 45,748,224 bytes. Evidence is in
/tmp/tofu-production-raw-result.json and /tmp/tofu-raw-source-preflight.json;
build SHA256 6453701bd42bc2e3a131b784c3170f575d2e192fc779c6aaff277f5573f2b1b9.
Production remains on SQLite pending remaining durable-domain coverage and full
source/target verification; no cutover or full migration is claimed here.


### 2026-09-12 — Frozen transcript coverage and bounded source decoding

Refreshed v14 source coverage found 59 populated unmapped tables, including 12
trash headers and 66 trash Turns. All 2,265 compaction archives have root scope;
there are no populated child-scoped archives in this production snapshot.
More critically, 4,539 active conversations have no main Turns and rely on their
frozen messages_json. Total frozen source bytes are 5,697,010,886, with maximum
62,997,304 bytes. Header-only migration cannot certify those conversations.

Added exact-PK blob_size metadata without materializing oversized SQLite TEXT,
sharing the read-only handle lifecycle with blob_chunks. Added strict bounded
JSON-array streaming and authoritative archive/projection hydration. Byte-level
structure scanning avoids quadratic prefix reparsing; a bounded stdlib object
decoder avoids the alternative decoder's large temporary arena. Largest-source
content chains match exactly; final observed Python peak is 443,834,254 bytes and
decode/encode verification time 10.087 seconds. Native preservation and frozen
history reads remain the next required implementation; no cutover occurred.
Evidence: /tmp/tofu-frozen-transcript-budget.json and
/tmp/tofu-largest-frozen-transcript-decode-final.json. Source boundaries and
existing migration regressions passed 28 and 101 tests respectively; dedicated
streaming tests cover grammar, UTF-8, compression, owner fencing and memory.

### 2026-09-12 — Native frozen transcript pages and exact JSON numbers

Plan v15 binds original frozen transcript bytes and imports decoded public
messages into immutable owner-scoped pages. A completed manifest requires
independent page enumeration, content hashes and counts; resume revalidates that
physical catalog. Chunk uploads remain bounded and mutually exclusive with event
uploads. Main Turns retain precedence; frozen histories support message windows,
recoverable delete/restore and clones sharing existing body blobs. Message-bearing
conversation lists use the same bounded large-history response ceiling.

Production preflight decoded 5,439 conversations into 5,105 pages without errors;
4,539 conversations without main Turns require this path. A largest-history
application comparison exposed fractional timestamps being rounded by the default
Rust JSON parser. Enabling serde_json float_roundtrip preserves those binary float
values and is now covered by an exact-value regression.

The history checkpoint fault campaign also exposed an unconsumed fault leaking
into recovery. Trials now fork the same durable seed and target an occurrence of
an I/O kind, since randomized segment paths can change directory I/O interleaving.
Only unexercised targets may retry; every fired error, short write and dropped sync
must pass the unchanged acknowledged-transaction and payload recovery checks.
Recovery explicitly disarms injection without removing the simulated lost-sync
state. The targeted campaign and all six frozen transcript native tests pass.
Production remains on SQLite; remaining durable-domain migration and full
source/target verification are still required before cutover.

Final-build evidence for this slice: the 62,997,304-byte production source restored
into two pages in 25.759 seconds; native manifest/page hashes matched after reopen,
and both application detail and message-bearing list responses matched source
content. Native upload/serve peaks were 187,543,552 / 330,207,232 bytes; Python
upload peak was 443,849,263 bytes. The final-binary crash/resume integration passed.
Evidence: /tmp/tofu-production-frozen-result.json; binary SHA256
ad156dfe3bd3fa863d640968631f915db9928ab271a4b644c30f1711a15986b9.
Contract generation, Ruff and architecture checks passed. Documentation checks
still report existing uncataloged files and line-budget violations, including the
expanded storage authority; those remain open and are not waived.
Full native library regression on the stable slice: 860 passed, 0 failed,
1 ignored in 123.66 seconds. Production application and manager health endpoints
both returned HTTP 200 after validation; storage authority was not switched.
Next-domain read-only inventory: recoverable trash contains 12 headers and 66
main Turns (maximum 22 per conversation), all terminal; largest projection is
1,202,002 bytes. Every trash header has a nonzero revision, so migration must
preserve revision as well as deletion time and content. All current frozen trash
arrays are empty. Evidence: /tmp/tofu-trash-preflight-result.json. The adapter
must publish recoverable capsules without exposing staged rows through active
conversation/Turn catalogs or breaking their independent resume verification.

### 2026-09-12 — Preserve source conversation revisions during offline import

Trash preflight exposed a shared header fidelity defect: header import omitted
source rev, and Turn restore used the live event path to advance revision and
updated_at. Plan v16 binds rev and imports the header through private
migration.conversation.restore, atomically preserving its owner claim, exact
logical revision, settings, timestamps and covering index. Replay rejects changed
source fields. Turn/page imports reconstruct derived message counts without
inventing new header events. This also makes resume verification stable when a
Turn timestamp is newer than its historical parent header clock.

New native tests cover fault-atomic header/index publication, source revision and
clock retention through Turn import, reopen, replay, owner mismatch and changed
revision rejection. Python source-binding regression rejects a revision-only
source change. Final-binary Python migration/streaming/boundary checks: 153 passed.
A live-source read-only sample at revision 1662 matched its exact header and Turn
after isolated native import and reopen. Evidence:
/tmp/tofu-production-header-revision-result.json; binary SHA256
1acf3bddb8f19ec01d3a2c5c4b760dccd2da83f6de32bdb90000048bf8cf899e.
Production remains on SQLite and trash import is still required.

Trash adapter constraint: existing persistent range pins extract the transaction's
committed snapshot root, not its pending writes. Creating header/Turns and deleting
them in one transaction would therefore pin the wrong snapshot. The adapter needs
an explicit recoverable staging lifecycle before capsule publication; never claim
an atomic capsule merely because create and delete shared one transaction.
Final native library regression for v16: 862 passed, 0 failed, 1 ignored in
113.41 seconds. Contract generation, Ruff and architecture checks passed;
application and manager health endpoints both returned HTTP 200. Existing
documentation catalog/line-budget failures remain open; no backend cutover or
full durable-domain migration is claimed by this result.

### 2026-09-12 — Recoverable trash migration with committed capsule publication

Plan v17 imports trash before active-domain verification. Header and one
owner-scoped pending marker publish atomically; exact historical Turns and frozen
pages are replayed and checked before a durable seal. A subsequent transaction
pins that committed seal, retires active ranges and releases the marker together.
This preserves source revision, deletion time and display count and avoids pinning
a pre-import root. Pending targets refuse application readiness; catalogs expose
pending identities so missing-source stages cannot certify an empty trash domain.
Independent verification reads pinned headers, physical Turn catalogs, counts,
retention indexes and frozen page content. Composite-PK source prefix scans reject
cross-scope resume, and retained projection codecs hydrate before comparison.

Native tests cover source divergence, owner fences, incomplete/extra Turns,
frozen restore, metadata/index corruption and errors/short writes/dropped syncs
across begin, seal and publication. Each injected fault must execute and recover
without exposing a partial application authority. Real-process Python tests resume
staging and verified-prefix interruptions, reject pending readiness and changed
pinned content, exercise oversized frozen pages and bound fixture process memory.
A complete initial native suite passed 866 tests with one ignored; subsequent
pending/catalog verification changes receive targeted native and process checks.
The Python suite passed 155 tests initially; its one sparse legacy-trash fixture
was corrected and the failed test passed separately. Additional pending-source and
invalid-codec regressions passed. Existing documentation catalog/line limits remain
open, and production is still on SQLite pending remaining durable domains.

Final v17 trash evidence: the native build SHA256 is
3006fecd8247f45adf86aaac4a1ecd04a18c5024a995c763b5934933f4e4ea6b.
All 12 live-source trash headers and 66 Turns matched pinned native content
and catalogs after reopen in an isolated target. Application restore then
preserved the selected source Turn snapshots after another reopen. Import
peak RSS was 60,166,144 bytes; Python peak allocation was 17,149,651 bytes.
Evidence: /tmp/tofu-production-trash-result.json. Final targeted native checks
passed 5 tests; final native-process Python trash checks passed 4 tests.

### 2026-09-12 — Require source table coverage at native operator admission

The domain importer can certify its declared mappings without noticing an
unmapped nonempty source table. The operator CLI now refuses such a source
before opening its target and repeats the check inside the actual import
snapshot. The report uses the plan as its mapping inventory and bounded
SELECT-constant presence probes, so oversized bodies are never materialized.
Empty unknown tables remain reported; no legacy/cache-looking name creates
an exemption. The partial domain API remains available for isolated adapter
certification and makes no full-source cutover claim.

Live query-only snapshot evidence /tmp/tofu-native-source-coverage-v17.json
contains 141 tables and 56 unmapped populated tables. These include legacy
state, indexes and replay metadata requiring explicit disposition or migration;
this count is not a count of independent missing product domains. Production
remains SQLite. The next coverage work must prove each disposition and cover
managed record namespaces as well as table identity; table coverage alone is
not a field-fidelity or cutover certificate.

Regression evidence: 122 tests passed in the source/import/boundary selection;
two failures were resolved: the missing-binary fixture now isolates file
admission from the earlier coverage gate, and the real-child regression was
rerun against the current debug binary instead of the stale default release
binary. The final 6 focused checks passed, including no-target-mutation on
unknown data, bounded oversized-body presence, and in-snapshot revalidation.

### 2026-09-12 — Preserve Turn deletion markers in native import v18

The source retains 22 Turn tombstones within its seven-day retention window.
Discarding them at cutover would omit deletions from incremental Turn reads.
The offline adapter now preserves exact owner, conversation/Turn identity and
deletion timestamp, atomically publishing the tenant-wide Turn identity claim
with the delta and retention keys. Already-deleted source attempts contribute
an empty native attempt-pin list. The native get/catalog independently verify
both indexes, the claim and absence of an active Turn. Changed source clocks,
occupied identities, owner mismatch and partial index corruption fail closed.
No parent revision or historical timestamp is advanced by import.

Native tests verify reopen/idempotent replay, ordinary delta visibility and
owner isolation, index corruption and every observed fault occurrence across
reads/writes/sync (including short writes and dropped barriers). Six neighboring
Turn migration tests passed. Python source/import checks passed 122 tests;
real-process tests cover 130-row pagination, interrupted-prefix reopen/resume,
changed-source rejection, adjacent trash and child-process import.

All 22 live-source tombstones were imported into an isolated target and matched
exactly after reopening on binary SHA256
3e98a1a7b9e739a8120021a4b3a82bb2999cf31bfc2aaa58ba841da759264296.
Evidence: /tmp/tofu-production-tombstone-result.json and
/tmp/tofu-tombstone-python-native.log. The current source coverage inventory is
/tmp/tofu-native-source-coverage-v18.json; this does not certify omitted domains,
full-source migration, or cutover. Production remains SQLite. Architecture,
Ruff and generated-contract checks passed; existing documentation gate failures
remain open.

### 2026-09-12 — Materialized public Turn projections in native import v19

Active Turn import previously parsed stored projection JSON without decoding
its private deduplication references; trash had a separate decoder. The shared
Turn conversion now uses the canonical storage codec for both paths, preserving
tool arguments/results in the public snapshot instead of importing references
as user-visible content. Malformed codec data fails closed. The mapping plan
version changes so old verified prefixes cannot certify the new conversion.

Source binding now admits only materialized inline Turn projections. It checks
checkpoint-table occupancy and scans bounded head metadata without loading
projection bodies. External checkpoints, active patch chains and malformed
residual counters require source settlement/recovery before offline migration;
they cannot be silently treated as empty inline projections. Empty/default head
metadata remains admissible. The importer does not mutate the running source.

The native reopen/resume regression preserves complete Unicode tool results and
arguments from encoded source references. Four bad head/checkpoint variants
refuse before target writes/checkpoint creation; a materialized control imports.
Migration/source/transcript/coverage suite: 170 passed. Codec suite: 8 passed.
Ruff and architecture checks passed. Native engine bytes are unchanged from
3e98a1a7b9e739a8120021a4b3a82bb2999cf31bfc2aaa58ba841da759264296.

Live source admission currently observes projection checkpoints. A separate
bounded projection-content inventory also hit the existing source row ceiling;
that unsuccessful scan is not production-content verification. Large Turn
projections require a bounded chunk adapter before full-source import. The
metadata-only size inventory is /tmp/tofu-turn-projection-size-inventory.json.
Production still uses SQLite, and full-source migration/cutover remains open.

### 2026-09-12 — Bounded large Turn snapshots in native import v20

The live source contains 26 projections above ordinary 8 MiB row admission.
All decoded projections total 497,715,494 bytes; the largest is 41,428,255 bytes.
Turn source reads now scan metadata and stream exact-PK projection/settlement
BLOBs in 256 KiB chunks under a shared 64 MiB source ceiling. Source binding
hashes original chunks. Inline and trashed Turn conversion share this reader.

A private native session owns one bounded Turn upload/read buffer, mutually
exclusive with event/frozen uploads. Uploads support exact-offset/identical
replay, digest verification before atomic Turn publication, and explicit
abort/failed-finish/EOF release. Readback caches one immutable serialized
snapshot, returns length/hash receipts, and releases the buffer at its final
chunk. Snapshot size reserves 64 KiB beneath 64 MiB; ordinary IR/write admission
remains unchanged. Turn materialization and get/list/delta responses use
separate declared read limits. Oversized aggregates still fail instead of
truncating history. These limits are generated from turnSnapshots in the IR
contract. The remaining lifecycle/large-write release surface is not certified
by a successful import/read proof alone.

Real-process tests cover a 9 MiB Unicode projection, verified-prefix interruption,
reopen/resume, full public turn.get/turn.list responses, and a native peak RSS
below 512 MiB. A second test covers a large pinned trash Turn across reopen,
without exposing an active conversation. Transfer receipt-corruption and partial
buffer-resume tests passed. Python migration/source/transcript/coverage/transfer
suite: 176 passed, plus the added large-trash test passed separately. Initial
native full suite: 878 passed, one old 8 MiB response-budget assertion failed,
one ignored. Its corrected test crosses the declared streamed-response ceiling
and passed separately; it no longer prints a huge successful payload on failure.
All seven Turn migration tests and nine release-signature tests passed.

The release-signature module concurrently introduced compilation and parser
failures: ReleaseVerification needed Debug for its negative tests, and whitespace
splitting incorrectly discarded valid filenames containing the signed packaging
annotation. The parser now retains the filename remainder and requires the
packaged tofu-db basename. This turn's native runtime proof uses SHA256
f6a9dfa9c5ee3137154f87a38cf36b6cfb6ab2f8dc6159afd1f320f4b1b27b70.

All 26 oversized source Turns, their headers and terminal attempt pins imported
into one isolated authority and matched exact snapshots after reopen; every
ordinary turn.get returned the exact projection. Largest snapshot: 41,450,680
bytes. Native import peak RSS: 388,452,352 bytes; serving peak: 370,470,912 bytes;
total isolated proof: 632.62 seconds. Evidence:
/tmp/tofu-all-large-turn-proof.json. This is not complete-conversation or
full-source cutover certification; production remains SQLite and healthy.

Storage documentation is now split into STORAGE.md, STORAGE_TOFUDB.md and
STORAGE_OPERATIONS.md, all within their catalog budgets and linked from the
first-hop map. The TofuSQL contract is cataloged; its generator emits rustfmt-
canonical arrays deterministically, fixing format-only generated drift without
changing catalog semantics. Write/check, rustfmt check and a new generated drift
test passed. Ruff/architecture checks passed; unrelated documentation catalog
and line-budget failures remain open.

### 2026-09-12 — Native import v21 verifies retired trading mirrors

The remaining source-table gate included retired physical trading tables even
though the extracted plugin now writes only the mapped generic plugin store.
Read-only inspection confirmed all 36 declared legacy tables (12,811 rows;
18 populated tables) match both the historical migration receipt and current
plugin document rows. Receipt equality alone is not admission evidence because
current rows may subsequently diverge.

Plan v21 now declares a bounded current-row mirror proof: mapped plugin manifest
and rows are prerequisites; registered legacy-scan declarations provide exact
columns and keys; every source column and full row multiset must equal current
plugin row payloads. Unknown/extra columns, missing or duplicate rows, malformed
data and exhausted budgets remain blocked. No old table is deleted. Verification
runs before native startup and again in the actual pinned import snapshot.
Limits are 256 declarations and 50,000 rows per side; only row hashes are retained
beyond the ordinary bounded source row. This avoids treating a table name or a
historical marker as proof of preservation.

Evidence: 13 mirror/coverage tests passed; 113 complete import tests passed
against the existing native binary, including large active and trashed Turn
transfers and reopen checks. Ruff and architecture passed. The live read-only
coverage report `/tmp/tofu-native-source-coverage-v21.json` verifies 36 mirrors
and leaves 39 populated tables unresolved. This is not stopped-source or full
cutover certification; production remains SQLite. Documentation checks retain
the previously observed uncataloged routing handoff and unrelated document
line-budget failures. Session audit completed.

### 2026-09-12 — Native import v22 retains retired domain history

Read-only comparison found that retired scheduler/timer rows are not current
mirrors: all 337 old timers and 8 old scheduled tasks are absent from current
repositories, and old poll rows are missing or different. Replaying those rows
as live state would revive historical tasks. Plan v22 therefore archives an
explicit ten-table inventory (errors, project charter/status/watch history,
recent projects, scheduler/timers and their poll histories) as inactive typed
records in `migration.retired_sqlite.v1`. The operator owner is an explicit
archive custodian, not an invented original owner. No production rows were
changed or removed.

Every source column and primary key is retained, with tagged NULL/integer/
real/TEXT/BLOB cells preserving byte/type distinctions. Envelope version and
time are format sentinels; historical clocks remain untouched in cells. Native
record insert-or-match and independent owner-scoped catalog/get verification
supply durability and divergence rejection. Generic record verification delegates
only this reserved namespace to the archive verifier; source occupation of that
namespace is refused. Missing verified rows and malformed target documents fail
instead of being repaired on resume. Complete and zero-row checkpoint cases
have separate regression coverage.

Source admission precedes binding and writes: 50,000 rows, 2 MiB encoded record,
512 MiB total archive. Checkpoint capacity increases from 64 to 128 domains in
the canonical IR/generator, retaining the 64 KiB file ceiling. The 29 checkpoint
boundary tests passed. Full neighboring import/migration/mirror/coverage run:
132 passed; seven archive-specific cases passed after the empty-prefix test
fixture correction. Ruff, generated-contract check and architecture passed.
Documentation checks still report the prior unrelated catalog/line-budget
failures. Session audit completed.

Real-source isolated proof archived all 13,382 rows across the ten tables and
verified all exact snapshots again after native reopen; no live timer was
created. Copied source size was 11,067,392 bytes; proof completed in 89.6 seconds
against binary f6a9dfa9c5ee3137154f87a38cf36b6cfb6ab2f8dc6159afd1f320f4b1b27b70
(the authoritative full hash is in `/tmp/tofu-retired-source-proof.json`).
Scratch authorities were removed after verification. This is not stopped-source
or full cutover certification. `/tmp/tofu-native-source-coverage-v22.json` leaves
29 populated tables unresolved. Production remains SQLite and was observed
Ready/liveness-passing during this work.

### 2026-09-12 — Receipt source admission for native cutover

Receipt migration cannot use the ordinary text/integer primary-key reader:
`storage_command_receipts_v2` is WITHOUT ROWID with a 32-byte BLOB command key.
Added bounded `digest_key_rows` and `digest_key_point` to the storage-owned
source reader. They seek/order raw bytes using the declared primary index and
expose lowercase hex only as a cursor/identity. Width/type/cursor violations,
non-indexed sorting and source row/hex-expansion overflow fail closed. Iterators
close their owned cursors; snapshot consistency and committed-change detection
remain the same source lifecycle. The importer plan is still v22: reader support
alone does not claim those receipt tables are migrated.

Evidence: 41 digest-key/snapshot/checkpoint boundary tests and 14 neighboring
residency/coverage/mirror tests passed. The large-row rejection test measures
Python allocation below 512 KiB while SQLite rejects a 1 MB source body under a
1 KiB row budget. The live, pinned read-only receipt audit independently compared
source counts with full iterator traversal and decoded every stored response:
380,354 legacy receipts (27,014,720 stored bytes, maximum command ID 191 bytes)
and 49,835 v2 receipts (224,591,550 stored bytes, maximum response 65,535 bytes).
All 430,189 decoded successfully. Python peak was 3,292,142 bytes; 37.01 seconds.
Report: `/tmp/tofu-receipt-source-audit.json`. No receipt was modified or deleted.
Ruff and architecture passed; docs-check retains the earlier unrelated catalog
and line-budget failures. Session audit completed.

Remaining semantic seam: Python and Rust use the same domain-separated command
key and compatible receipt response codec. Both request digests bind operation
and canonical payload, but the native client injects `user_id` when a v1 caller
omits it. Source receipts do not independently store owner/tenant identity;
`tenant_users` currently contains ten distinct owner values. Do not infer receipt
ownership from an archive custodian or blindly remove owner fields to make a
hash match. Native restoration/replay must prove ownership and preserve conflict
fencing before these tables can clear the gate. Production remains SQLite; the
29-table coverage deficit has not been reduced by this reader-only change.

### 2026-09-12 — Scoped native command-receipt snapshots

Added private `migration.command.receipt.restore/get/scan` operations through
`receipt_migration.rs`. Tenant and owner are explicit payload fields and must
match the exclusive operator session. The existing authority receipt publisher
now accepts a validated snapshot keyed by the already-hashed command identity;
normal commands and imports share atomic entity/blob/family publication. Exact
encoded response bytes, operation, request digest and commit time survive
reopening. Identical restoration does not commit; changed snapshots conflict.
No source ownership inference or historical request-digest normalization occurs.
The existing 64 KiB stored / 4 MiB decoded / 7 KiB inline receipt budgets remain.

Native tests cover inline, blob and compressed snapshots, cross-owner/tenant
isolation, paged catalogs, malformed/bounded input, ordinary receipt lookup and
all observed read/write/sync failure points with deterministic crash recovery.
The actual rebuilt daemon also passed six public-client tests: matching source
payload digests return the original response without creating the target
record; changed requests and a v1 digest lacking injected `user_id` conflict
before any mutation. This closes the target replay prerequisite without claiming
the unresolved global SQLite receipts have proven owners.

Verification: 36 focused native receipt tests passed; full native library suite
882 passed, 1 ignored (114.13 seconds). Six live-client process tests and 41
source/checkpoint boundary tests passed. Generated IR check, Ruff and architecture
passed. Existing unrelated docs catalog/line-budget failures remain. Session
audit completed. Rebuilt binary SHA-256:
`d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274`.

Production remains SQLite; source import plan remains v22 and still does not
admit receipt tables. Establishing source receipt scope and preserving replay
across the implicit-owner digest seam remains required before full cutover.

### 2026-09-12 — Import v23 archives retired local-account metadata

The source logical outbox and replay checkpoints contain zero rows, so they do
not provide the missing receipt ownership witnesses. SQLite command receipts
are globally keyed within their source authority; scoped native restoration
alone is not a proof of equivalent replay semantics. A future migration path
must preserve that legacy identity/conflict contract while respecting current
authorization and tenant isolation, rather than guessing receipt owners.

Separately verified that the old `users` schema (numeric id, username, display
name, password hash, clock) is not the active account authority. Current auth
uses the API credential gate/local-admin context and `tenant_users`; there are
no retained runtime SQL reads of the old users/schema_meta/scoped_sequences
inventories. Plan v23 adds those three explicit historical tables to the typed
inactive archive. Their hash, original numeric identity and counters are
retained exactly, without creating login principals or replacing active schema
versions/sequence state. Existing archive and checkpoint resource bounds remain.

The new native regression preserves a retired account, old schema version and
maximum signed 64-bit sequence across reopen while the native tenant-user
catalog stays empty. Nine focused archive/rollback tests and the full 134-case
import/coverage/mirror suite passed. Ruff and architecture passed; docs-check
retains the existing unrelated catalog and line-budget failures. Session audit
completed. Service status remains Ready and liveness-passing (PID 126983,
SQLite authority, approximately 0.40 GiB RSS).

Real-source isolated proof now covers 13 tables / 13,828 rows, exact after
reopen, with no live timers or login accounts created. Copied source size is
11,214,848 bytes; 99.6 seconds against native binary
`d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274`.
Evidence: `/tmp/tofu-retired-source-proof-v23.json`; scratch stores were removed.
`/tmp/tofu-native-source-coverage-v23.json` has 25 uncovered populated tables:
three mapping gaps were resolved here, while projection checkpoints became
empty independently in the running source. Neither that transient emptiness nor
this isolated archive proof certifies stopped-source migration or cutover.

### 2026-09-12 — Native migration v24: bounded retired large-row archives

Plan v24 preserves seven explicitly retired conversation/task/history tables as
inactive typed manifests and raw byte parts. Source metadata and incremental
slices avoid full-cell Python allocation. Parts precede manifests; published
prefixes and independent target catalogs reject missing or extra parts instead
of repairing corruption. Generated limits bound rows, manifest size, raw row
bytes, target record count and conservative encoded size. This preserves old
history without recreating executors, queued work or current conversation state.

Validation: 143 importer/coverage/mirror/cell tests passed; contract generation,
Ruff and architecture checks passed. Three real source rows were copied read-only
into an isolated source and imported/reopened with exact all-cell verification:
conversation messages 62,997,304 bytes, transcript messages 17,893,174 bytes,
and one task-result row selected by largest content field (126,781 bytes).
The task-result selection does not claim the largest total task-result row.
Python traced peaks were 4,456,714 and 3,470,051 bytes; 155.0 seconds total.
Native binary SHA-256: d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274.
Disposable data was removed after proof; production source was not modified.

The current live-source coverage observation has 18 unmapped populated tables,
down from 25 in v23. This is coverage evidence, not a full stopped-source
migration certificate. Global command-receipt scope and replay/projection/search
state still require explicit admission. Production remains SQLite; no cutover
or worker restart occurred. Documentation check still reports the existing
uncataloged capability-routing handoff and oversized authority/domain documents.

### 2026-09-12 — Native migration v25: retired search provenance

The plan archives all retired FTS5 content rows (including original integer
rowids and text absent from current conversations) and its complete typed
configuration. The source coverage gate accepts only the exact known
unicode61 FTS5 DDL and SQLite virtual/shadow classification. The virtual view
and three physical index structures are separately reported as reconstructible
from those archived inputs. Ordinary same-name tables, changed tokenizer/DDL,
missing archive mappings and unreadable provenance fail closed.

Validation: 148 importer, coverage, mirror, cell and search tests passed,
including actual native restore/reopen preserving unique search text, NULs,
rowids and configuration. Ruff, generated IR check and architecture check passed.
The documentation gate retains the previously observed uncataloged handoff and
line-budget failures. Production still uses SQLite; no restart or switch occurred.

Live-source coverage currently reports 13 unmapped populated tables; the six
retired FTS tables are accounted for, while a live projection checkpoint table
has become populated again. A full real-data proof of 4,542 content rows plus
one config row (259,047,424-byte isolated SQLite copy) is still running; it is
not yet a completed reopen certificate. The initial diagnostic copy correctly
hit the ordinary 8 MiB row ceiling, so the disposable copy now uses SQLite's
internal INSERT SELECT and the unchanged bounded large-row importer.

Read-only maintenance analysis hit its 60-second budget before any complete
payload/retention measurement. Zero candidate values are not absence evidence.
It found five missing deferred retention indexes and recommends stopped-server
index maintenance. No transport or durable data was deleted.

### 2026-09-12 — Retired search real-data proof completed; migration v26 caches

The pending v25 search proof completed successfully: all 4,542 original FTS
content rows and one configuration row passed native import and exact reopen
verification. The isolated copy was 259,047,424 bytes; first pass took 553.1s,
full two-pass proof 747.7s with the debug native binary. This establishes
fidelity, not production migration throughput. The script exited successfully
and removed its temporary source/target. Proof receipt:
`/tmp/tofu-v25-search-proof.json`; binary SHA-256 remains
`d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274`.

Plan v26 also archives retired pricing_cache and rate_limit_events values,
identities and timestamps without activating prices or counters. Current
pricing remains in generic records and current limits have their separately
mapped storage_rate_limit_events authority. The real five legacy rows passed
isolated native import/reopen in 1.44s with no active pricing/limit records
created. Focused native regression and all 149 neighboring migration tests passed
(137.27s). Ruff, IR generation and architecture checks passed; the existing
uncataloged handoff and documentation line-budget failures remain.

Current live coverage has 11 unmapped populated tables, including an active
Turn projection checkpoint. Source storage_meta keys are authority_uuid,
project_brain_cutover_v1, raw_archive_stored_bytes and schema_version; their
native identity/marker/counter semantics need explicit admission rather than
blind active configuration restore. No production restart or authority switch
was performed; full stopped-source migration and live certification remain.

### 2026-09-12 — Native migration v27: source metadata provenance

The complete storage_meta table is now preserved in the typed inactive archive,
with per-key native semantics in tofudb_migration_metadata.POLICY included in
the plan digest. Only the known authority UUID, required source schema version,
u64 raw-archive byte counter, and completed Project Brain cutover marker are
admitted. Unknown keys may own replay/projection lifecycle and fail before
writes. Native identity/schema remain independent; native raw-archive accounting
is derived from imported data rather than the historical source counter.

Seven focused tests passed, including malformed/unknown metadata refusal and
native reopen with an independent authority UUID and unchanged actual byte
counter. All four real source metadata rows passed isolated native import and
reopen in 0.26s, preserving exact source values. The target contained no raw
archives, and its actual byte count remained zero despite the archived source
counter. This is provenance proof, not full source-accounting certification.
The scratch source/target were removed. Binary SHA-256 remains
 d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274.

The 155-test migration ladder initially had 150 passes and five expected-set
failures caused by the newly preserved schema_version record. Updated those
assertions to include its exact key, preserving strict extra-record detection;
all five failed cases passed on rerun. Production implementation did not change
after the full ladder. Ruff, generated IR and architecture checks passed.
Documentation still has the previously recorded uncataloged handoff and
line-budget failures. Live source coverage now has ten unmapped nonempty tables:
legacy attempt/task events, both command-receipt stores, conversation replay
changes/heads, storage events, projection outbox/checkpoints and turn-search
projection. No production restart or native authority switch occurred.

### 2026-09-12 — Global receipt source identity audit

Added a bounded source-only receipt audit that preserves the live dual-format
resolver's integrity rule: a command identity present in both legacy and v2
stores is rejected even when response/digest/clock values agree. This prevents
a future importer from silently coalescing ambiguous source state. The helper
preserves exact encoded response bytes, validates the existing codec/digest/
clock fields and emits separate complete hash chains without assigning owners.
It uses indexed v2 digest lookups, no all-receipt identity set, one million rows
maximum, and a 60s default/300s hard audit deadline. Exhaustion does not return
successful partial admission.

Validation: six source-audit tests passed, including duplicate identity,
invalid metadata, deadline cleanup and extra-column refusal; twelve neighboring
BLOB-key source tests passed. Ruff and architecture checks passed. Full real
source audit with a 120s budget found 380,354 legacy and 49,961 v2 receipts
(430,315 total), zero duplicate identities, valid codecs/digests/clocks,
72.34s elapsed, and 3,303,526-byte Python allocation peak. The legacy chain
matches the prior complete audit. Report: /tmp/tofu-receipt-identity-audit.json.
No production/source mutation occurred. Server remains Ready/liveness passing
on PID 126983, SQLite; startup fixes are still not loaded by a restart.

Receipt table coverage remains blocked until native scope/replay is implemented
and verified: source identity is global while existing native snapshots are
owner/tenant scoped. The source audit does not infer past ownership, rewrite
request digests, authorize foreign replay or constitute a cutover certificate.

### 2026-09-12 — Native legacy-global receipt core and owner witness

Added legacy_receipt.rs primitives for one source authority per target tenant.
The generated legacyReceiptImport profile reserves owner 18446744073709551614,
separate from the existing tenant-global owner, and bounds history to one
million receipts / 1 GiB encoded responses. The source UUID and exact row/byte
accounting commit atomically with the existing receipt/blob/family publisher.
Source swaps, divergent restores and quota overflow fail; matching restores
revalidate the codec without advancing counters. Tagged metadata prevents UUID
bytes from being confused with another entity codec.

The semantic compiler records whether the exact hashed request payload contains
an explicit numeric user_id matching the authenticated owner. IR execution
checks legacy global identity before ordinary receipts or mutations, including
receipt-free commands. Missing ownership, reserved callers, changed operation
or digest fail with conflict without returning the historical response. Existing
user and tenant-global receipt scopes remain separate. Bench examples now
explicitly mark synthetic, unowned request digests as lacking this witness.

Validation: five new native tests passed, covering inline/blob/compressed
reopen, real semantic owner isolation/no-write behavior, row/byte limits,
existing tenant-global receipt separation, and every exercised deterministic
error/short-write/dropped-sync point with exact metadata+blob recovery. Three
neighboring IR tests passed. Full native library gate: 887 passed, one existing
ignored test, 113.21s. cargo check --examples passed; six Python source audit
tests, generated IR check, Ruff and architecture checks passed. Documentation
still fails on existing budgets/handoff and now also an uncataloged
API_V4_MIGRATION.md from concurrent workspace work; full-worktree results remain
a moving target. No unrelated files were reverted.

These primitives are not exposed in the offline operator allowlist and source
receipt domains remain unmapped. Independent native catalogs, operator scope
admission, full receipt import and actual-client replay proof remain required.
The standalone native binary was not rebuilt in this increment; earlier real
migration proofs still refer to d1551600af378c5e3a67847ba8d935586c600df3b69a2358844cb6a253e0d274.
Production remains SQLite. Quiescence reported three running tasks, so no
worker restart or authority switch was performed.

### 2026-09-18 — Native control operations, engine log structure, busy-retry budget, medium-scale certified matrix

Roadshow-readiness increment on branch `codex/tofudb-roadshow-20260917`
(worktree `/tmp/tofu-roadshow-20260917`). Three observability gaps closed and
one write-path failure mode eliminated, then a full medium-scale evidence
matrix against the pinned engine.

`system.health`/`system.metrics` are now authenticated storage.v2 control
operations (`src/control_ops.rs`): they dispatch ahead of semantic admission
— staying out of the 355-operation catalog and the differential gate — and
answer from listener/maintenance/packer observers, lock-free durability
counters, nonzero commit-stage totals, and the launch resourceBudget without
ever taking the authority mutex. Contract recorded in
`contracts/storage_v2.json` `controlOperations`. The Python v2 client prefers
these ops and degrades to the old synthetic bridge on `unknown_operation`;
that wire code now survives client sanitization as a first-class taxonomy
member. The same client's RPC histogram now feeds
`tofu_storage_rpc_seconds` exactly like the v1 path. Engine stderr is single-line
JSON (`src/engine_log.rs`) registered in `docs/LOGGING.md`; the FSYNC_STATS
dump stays human-oriented and opt-in.

Root-caused the medium-scale write failure: WAL-rotation backpressure
(`database_busy`, always pre-mutation) surfaced to callers because command
retries were fail-fast without a not-dispatched proof the wire cannot carry.
The v2 client now retries busy commands with the same command id under a
bounded 16-attempt exponential-backoff budget (idempotent by the engine's
receipt discipline); all other command failures keep the shallow budget.
Final matrix: all twelve workloads complete on local and FUSE with zero
harness-visible retries.

Validation: 991 native lib tests, 24 process/cert integration tests, 177/177
semantic differential probes against the pinned binary
(`922a32331e2e8bc0dc9dc442d88a217a3099832ab38459c94c79e9949a55571b`), 21 v2-client
and 30 supervisor-tofudb Python tests, storage.v2 generator `--check` clean.
Certified medium matrix (≥1,000 write-path samples) in
`docs/STORAGE_PERFORMANCE_CHARTER.md`: read family wins p50 and p99 locally
(1.2–1.6×) and concurrent throughput 2.8×; FUSE write p50 wins 3.9–6.1× and
large-turn p99 7.9×; disposable default-config PG 18 reference trails p50 on
all eleven comparable workloads (1.6–6.3×). Honest tails remain: create/append
p99 trail SQLite locally (10.3/8.8 ms vs 1.2/2.6 ms), swarm writer p99
208–706 ms, sidebar/concurrent p50 trail on FUSE; legacy search projection
stabilized at medium scale in no run on either volume, so no search A/B
exists. Soak under the 8 GiB budget is still uncovered. Production remains
SQLite; the native backend is a certified selectable opt-in.

### 2026-09-18 (second increment) — Rotation-finish atomicity fix + foreground pacing cap

Two engine changes, one correctness, one latency, driven by the medium-scale
matrix and stage attribution:

1. `finish_rotation_checkpoint` now runs the deferred-block drain BEFORE the
   covered snapshot is destructured. A background pack publication owning the
   pending set is a transient defer (`WouldBlock`), but the phase-3 drain used
   to propagate it after the covered prefix had moved out — dropping the live
   authority's in-memory history and surfacing `database_integrity` to every
   subsequent writer. The drain now restores the whole snapshot on the
   transient path (the rotation lane re-prepares after publication completes)
   and fails closed with `restart_required` on any other drain error, matching
   phase-2 semantics. Reproduced deterministically by holding a completed pack
   in its publication handoff: new test
   `rotation_finish_defers_whole_snapshot_while_pack_publication_owns_materialization`.
2. `PREPARATION_MAX_STEP_BYTES` 1 MiB → 256 KiB. Stage attribution showed
   up-to-1-MiB foreground history-pacing steps costing ~3.7 ms and dominating
   write p99; a 256 KiB step costs ~1 ms and a 64 MiB deficit still drains in
   ~250 commits, with any remainder always finished by the checkpoint itself.

Validation: 992 native lib tests (fault matrix included), 24 process/cert
integration tests, 177/177 differential probes. Medium local A/B vs the
previous certified binary: create p99 10.26 → 3.28 ms, put p99 2.42 → 1.49 ms,
mixed p99 1.62 → 1.45 ms, no p50 regressions; large-turn and swarm phases now
complete where the integrity failure previously aborted them. swarm writer
p99 remains the open tail (181–208 ms; rotation pacing under writer
convoys), and append p99 shows run-to-run variance around rotation events.

### 2026-09-18 (third increment) — Convoy starvation fix: swarm p99 halved

Two small, mechanistically targeted changes against the swarm p99 tail:

1. The rotation and pack-adoption drivers' probe locks were
   `try_lock`+sleep polls, which starve under writer convoys — freshly
   arriving writers always beat the sleeping driver, so rotation starts were
   delayed until the hard ceiling converted into client-visible backpressure.
   Both probes now use blocking `lock()` (the probe body is a quick in-memory
   take; no mutex holder ever waits on these drivers, verified against the
   engine's wait graph).
2. The v2 client's busy-command backoff now starts at 2 ms (was 10 ms) and
   caps at 64 ms (was 250 ms): rotation install windows are millisecond-scale
   (measured ~4 ms locally), so a coarse first retry turned an invisible wait
   into a p99 event.

Validation: 992 lib + 24 integration + 177 differential on binary
`39e145755b66f00bcb6c721ba643febecd8661c07cc0ece1ecaa6e0629c63368`. Medium
local A/B: swarm writer p99 208 → 107 ms, put/mixed p99 improved, p50s
unchanged. Remaining tails are structural and documented in the charter:
rotation-event latency on append (install window ~4 ms plus mutex convoy),
FUSE read-side latency, and CPU-starved write tails (gate 7).

### 2026-09-18 (fourth increment) — Server-side rotation parking

The last client-visible effect of a rotation is gone. A command rejected
with rotation backpressure now parks inside the daemon on the new
`RotationBarrier` (a generation-counter condvar the rotation driver signals
after every finish) and replays in place up to sixteen 8 ms waits inside its
own request deadline; the parking predicate (`rotation_blocks_appends`) also
covers the transient-defer gap via a 1 MiB WAL headroom margin. Budget
exhaustion returns the same retryable `database_busy` the client already
absorbs, so the worst case is exactly the pre-parking semantics. The wire
contract is unchanged. A deterministic server test fills an authority to the
hard ceiling, reserves the rotation lane, and proves a framed command is
held through the install and succeeds (an instant busy reject fails it).
Measured A/B: swarm writer p99 124 → 104 ms. A 2-segment preparation-lead
experiment was measured and reverted (p50 cost, no p99 gain) — recorded in
the code comment so it is not re-litigated.

Validation: 996 lib + 24 integration + 177 differential on binary
`7183645658a8d18cecad339fd368cb0edc794bea432b0d18840aa9be855ead8b`.

### 2026-09-18 (fifth increment) — Component stage attribution, 30-minute soak, FUSE root-cause map

`fsync_stats` stage counters are now attributed per thread component
(foreground connection workers, rotation/pack-adoption drivers, background
packer, maintenance) so a FUSE report can separate cost that lands on
foreground commits from background progress. The dump prints a per-component
breakdown alongside the flat stage report.

The attribution answered the open questions and disproved two levers:

* FUSE write p99 (25–35 ms band) is 1.8 % of commits paying a 25.8 ms
  foreground `drain_step` (a full segment pack inline when the background
  packer falls behind) plus the packer's 8.7 s I/O storm competing for mount
  bandwidth — not the rotation install (~74 ms × 7, a minor share). Pacing
  the packer with the existing operator knob
  (`TOFU_DB_PACK_BYTES_PER_SECOND=32MiB/s`) cut create p99 24.9 → 4.6 ms
  with flat p50; documented as an operator knob for FUSE/network mounts
  (STORAGE_TOFUDB.md), not a default, since the right rate is mount-class
  knowledge. The structural fix (a background drain lane / batched block
  syncs) is certified-work for a later increment.
* FUSE read gap is NOT cache capacity: the read benchmark showed a 0.5 %
  hit rate with compulsory misses (COW churn + one-shot paging), so cache
  enlargement was measured-then-rejected before writing any code.

30-minute bounded soak (`scripts/tofudb_soak.py`, medium corpus, ~2.9 M
operations): p50 flat at 0.207–0.226 ms in every per-minute window; engine
peak RSS 523 MB (499 MiB, inside the 512 MiB gate); 14 classified errors
(5 ppm) clustered in a ~2-minute window whose signature — throughput
collapse with zero engine error lines, unaffected p50, spontaneous recovery
without restart — matches host-level contention on this shared box (other
tenants' engines were live on it), not an engine fault. The attribution
instrumentation now exists to catch such an episode in-process next time.

Validation: 997 lib + 24 integration + 177 differential on binary
`ea37753110b0f111a6e2468a597c8b655b51367ef9de354fedc449ad150222ea`. The v5
clean matrix (local/FUSE/PG against the deployed pin) is recorded in
`STORAGE_PERFORMANCE_CHARTER.md`: local put p99 now also wins (1.60 vs
2.75 ms), maintenance 3.7–4.2×, PG p50 11/11 (1.9–5.8×); process note —
cargo's incremental cache can re-hardlink byte-different variants of one
source, so the certified artifact is always the deployed copy.


## 2026-10-07 — JOURNAL 追加被恢复预算拒绝：拆分展示与恢复预算

触发操作：向 JOURNAL.md 追加本轮中断诊断、105 回归、四源 fresh 封包和
strict 发布证据，要求保留全部历史。工具返回
`Cannot read original file safely; no write was performed: Original file exceeds recovery byte budget`。
失败发生在写入前，原文没有被修改。

根因：项目写工具拿读取展示的 `MAX_FILE_SIZE = 512 KiB` 限制完整原文捕获；
prepared-write intent 又把原文、post-image、base64 schema 固定在同一小上限。
原 JOURNAL 已有 699,612 字节，追加短段落也必然触发拒绝。仅放开原文读取
还会在 intent 或撤销指纹校验处再次失败。

修复：新增资源契约中的 `TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB`，复用一次
启动时的容量、内存余量、磁盘余量探测，个人默认由三者共同决定，未知内存
回退 4 MiB，分布式回退 16 MiB；明确覆盖值需重启生效。完整原文读取、
UTF-8 字节 admission、stage、intent 分类、base64 封包、逆补丁及撤销指纹
统一使用恢复预算。读取展示和命令快照保留各自预算。撤销批次允许至少验证
一个可写入的大文件，包含 EOF 探测字节。超过真实预算仍在修改前拒绝；
缩小预算不会删除待恢复证据，后续扩大预算可继续恢复。保持完整原文和
post-image 证明、原子替换、fsync、并发编辑拒绝及撤销日志确认。

验证：145 项专项测试通过，覆盖真实子进程中断恢复、日志失败、原子批处理、
原文字节完整性、命令撤销和资源契约。最终新增 17 项大文件/预算回归全部通过，
连同批次撤销测试共 23 项通过。项目域完整执行得到 1,340 通过、4 跳过，
24 项失败，失败集中于既有浏览器源码装配、工具 schema 投影、SWE-bench
环境和 C 编译环境。在修复前基线复验失败所在测试文件，得到 24 失败、
35 通过；失败测试标识集合与修复后的 24 项完全相同，没有新增失败。
文档/架构/配置全仓检查仍报告既有超限、链接或登记问题；本次资源生成器
check 和 diff whitespace check 通过。

实际 JOURNAL 副本验证：在 699,612 字节原文后追加 39 个 UTF-8 字节，
逐字节验证原文前缀及新增内容，并成功撤销。撤销后 SHA-256 仍为
`cd7dd7edb81f6f0854196aae88635fd909dd84258165c6139b413e25b0a5c4e0`。
冷启动单次追加的 tracemalloc 峰值 14,071,847 字节，包含首次校验器导入；
带分配跟踪的操作耗时 3.8769 秒。测量数据、测试输出和合入前原文备份保存在
`/tmp/tofu-write-recovery-backup/`。本条追加保留全部既有 JOURNAL 字节。

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
