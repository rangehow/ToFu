# Does Tofu get better the more it is used? Memory and lead-knowledge report (2026-11-17)

This is a one-time report written for the owner's question. Delete it once the owner has decided on the gap list.
Sections 1–5 changed no code. Every live measurement in them was a read: memory files read in place, plus loopback `GET`s
through `debug/inspect_conversation.py`'s `_DiagnosticAPI`. Section 6 adds the replay benchmark that the gap list proposed
(`evaluations/owner_memory/replay.py`) and its first measurement. Private owner content is reported only as counts and
topics.

## 给主人的一页摘要

**结论：没有证明"越用越好"。** 现有实验和线上数据都没有测过"随使用时间变好"这件事。

- **实验只测了某一个时间点。** 五个实验（基线 MEMORY-EVAL、实验 1/1b/1c、FM-2x2）都冻结了语料，在录好的请求上回答一个问题：*此刻*加上记忆或画像有没有帮助。FM-2x2 甚至故意冻结了语料。没有任何实验比较"早期语料"和"后期语料"。
- **记忆在单个时间点上有一点帮助。** 基线里偏好应用率是 31.7%，关掉预取后降到 28.3%。Coordinator 全上下文从 56.2% 降到 43.8%。
- **线上数据显示"存得多"，没有显示"用得好"。**
  - tofu 项目的经验条数一直在涨，增长还在加速：11 月第二周新增 318 条，现在共 2,230 条。
  - 但每个 Turn 只预取 2 条。11-15 到 11-17 的 312 个 Turn 里，预取了 477 条，被主动打开的只有 16 条（3.4%），在最终回答里被点名的只有 1 条。
  - 抽 60 条人工看，约一半和任务相关。Project Router 收到的 14 条全部无关。
  - My Context 里没有生效条目：0 条 active，17 条都是 inactive。学习器是关着的。312 个 Turn 中，`preferencesApplied` 出现 0 次。
  - 2,230 条经验全部是"推断（inferred）"级别：没有一条是主人陈述或已确认的，被标记"推翻"的也是 0 条。纠错靠原地改写（537 条被改过），所以生命周期机制从没用上。
- **重复犯错率测不出来。** 系统不记录"同一个问题在已有经验之后又发生"。经验正文里有 2 条自己写了"又发生了"。
- **结果趋势只能看 3 天。** 线上库从 11-15 搬机后才开始。11-15 到 11-17 共审核 142 次：135 次接受，3 次返工，4 次拒绝。更早的历史在旧机器的 TofuDB 里，只读打不开，因为要起引擎才能读。

**负责人之间怎么沉淀知识：真正在复利的是"跨项目消息 → 共享手册 → 全局 skill"这条路。**

- 4 个负责人的信箱里有 73 条跨项目消息，集中在 910C、Hope 和 GPU 队列。
- knowledge 和 parallel 互相发验证过的配方，Global Router 转发和汇总。最后由 Worker 发布成全主人可见的 skill `hope-910c`。
- 这个 skill 一天内被三个项目加载了 10 次。
- 负责人自己不能写经验，也不能改文件。他们的知识存在信箱和对话里，要靠 Worker 落地。

**第一次纵向测量（2026-11-16，见第 6 节）：仍然没有证明"越用越好"。**

- 拿 23 个真实缺陷经验，在"这条经验写之前"和"写之后"两个语料快照上重放同一个录制请求，盲评回答是否避开缺陷。
- 两组避开率都是 4.3%（69 次里各 3 次），差值 0.0 个百分点，95% 区间 [−8.3, +7.3]。
- 主要原因有两个：
  - 现有预取只在 12/23 个案例里把这条新经验注入回去，其余 11 个案例的请求逐字节相同。
  - 第一轮回答几乎都还在探查阶段，没走到缺陷那一步（138 次里"踩坑"0 次）。
- 干扰组：18 个无关任务在小语料和大语料上都是 100% 在正轨，没有退化。
- 这套基准可以一条命令重跑，用来测并行 Worker 改的记忆机制。

## 1. Verdict: not shown

| Claim part | Evidence | Status |
|---|---|---|
| Memory helps at one point in time | MEMORY-EVAL baseline: application 31.7% with prefetch, 28.3% without; Coordinator full context 56.2% → 43.8% without prefetch (`docs/modules/context_engineering.md`, "Owner understanding: design record", Decision 8; `docs/OLD_REPO_RECOVERY_MEMORY.md`, "Experiment findings"; raw `.eval-runs/owner-memory/private/baseline-report.txt`, coordinator/full `appliedRate` 0.5625) | partially shown (small effect, one snapshot) |
| An always-present owner profile helps at one point in time | 1b K1 +20.0 pp; 1c K1 +16.1 pp [+5.4, +28.6] but 1c NOT PASSED (K3) (`k3-1c/REPORT-k3.md`); FM-2x2 PASS (`fm2x2/private/analysis.json` `verdict: {profile|L1: PASS, profile|L0: PASS}`) | shown in the lab; **not in production**: Phase 1 is unmerged (`docs/OLD_REPO_RECOVERY_MEMORY.md` §A) and My Context has 0 active items |
| The system improves as it is used (compounding) | Earlier experiments never varied corpus age. The before/after-lesson replay (§6, 2026-11-16) now does: on 23 real defects, avoidance is 4.3% before and 4.3% after the lesson, difference 0.0 pp, 95% CI [−8.3, +7.3] | **not shown: measured once, no detectable effect** |
| Live use makes memory better | §2: the corpus grows, but injection is fixed at 2 items per Turn, 3.4% of injected items are opened, 1 of 477 is cited, owner items are 0, and lifecycle states are 0 | **not shown**; inputs accumulate, use does not visibly improve |

So "the more it's used, the better it gets" is **not demonstrated**. What is demonstrated: memory and the profile each help a little
at a fixed point in time, and the stored corpus grows every week.

## 2. Longitudinal measurements on real data (read-only)

### 2.1 Window and its limits

- **Live conversation store.** `GET /api/v4/conversations` lists 171 conversations (150 Workers, 16 chats, 5 Coordinators), all created
  2026-11-15 … 11-17. The store starts at the host move. Earlier history is in the old host's TofuDB
  (`/path/to/your/project last write 11-14). Opening it means
  starting an engine on it, which writes `LOCK`/WAL, so it was not opened. **No conversation-level measure covers more than 3 days.**
- **Memory files.** These carry their own `created`/`updated` stamps back to 2026-W12, so corpus growth is longitudinal.
- **Logs.** `logs/` rotates and holds 11-15 … 11-17 only.
- **Conversations scanned.** All 171, excluding this report's own conversation: 312 assistant Turns. 13 conversations were larger than the
  inspector's 8 MiB client budget; they were read with a raised client budget in the scratch script, with no server change.
- **Possible load.** The server manager logged readiness-probe timeouts at 01:35 and 01:36, during the last whole-conversation scan. The same
  log has 163 such events from before the scan, so the scan is not known to have caused them. It may have added load.

### 2.2 Memory and lesson counts and growth

**Method.** Frontmatter of every `*.md` in each store was parsed line by line (YAML parsing failed on about 1,200 files with unquoted colons),
then bucketed by ISO week of `created`.

| Store | Lessons | Growth |
|---|---:|---|
| tofu project (`<tofu>/.tofu/memories`) | 2,230 | W12 18 → W20 438 → W30 917 → W40 1,372 → W46 2,228. Per week, W43–W46: 146, 132, 205, 318 (accelerating) |
| global (`data/memories/global`) | 295 | bulk 82 + 54 + 35 in W28–W30, then 1–27 per week |
| parallel project | 34 | — |
| knowledge project | 15 | — |
| Global Router home (`data/router/1`) | 0 | — |

**Only a small part of the corpus is reached.** In the 3-day window, prefetch injected 189 distinct lessons, which is 8.5% of the tofu corpus
plus globals.

### 2.3 Are prefetched memories used or cited per Turn?

**Method.** For each assistant Turn, `provenance.memoryPrefetch.memories[]` (the id and name of each injected item) was compared with:

- **opened:** the item id or name appears in a `tool_use` input of that Turn (`search_memories memory_id`, `read_files` path);
- **in answer:** it appears in the final `content`;
- **in text:** it appears in model `text` segments.

The context-injection `system_note` is excluded, because it contains every injected item by construction. An earlier pass that did not
exclude it reported 97% "cited". That figure was an artifact.

| Role | Turns | Turns with injection | Items injected | Opened | In answer | `search_memories` calls | `create_memory` calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| Worker | 170 | 152 | 304 | 14 | 1 | 20 | 31 |
| Project Routers (tofu, knowledge, parallel, Watchdog home) | 51 | 20 | 40 | 1 | 0 | 4 | 0 |
| Global Router | 58 | 44 | 85 | 1 | 0 | 2 (both `mode=owner`) | 0 |
| Chat | 33 | 26 | 48 | 0 | 0 | 1 | 2 |
| **Total** | **312** | **242** | **477** | **16 (3.4%)** | **1 (0.2%)** | **27** | **33** |

- **Fixed selection.** Every completed prefetch selected exactly 2 items, except 7 Turns with 1. Strategy was `local_high_confidence`,
  with a median of 20 candidates.
- **No owner preference was applied.** `preferencesApplied` appears on 0 of 312 Turns, because My Context has no active item.
- **Relevance.** One unblinded labeller (this worker) judged a random sample of 30 injected Turns (58 items, seed 7), from the request head
  and the item ids only.
  - Plausibly relevant: 27 of 58 (47%).
  - Workers and chats: 26 of 38. Global Router: 1 of 6. Project Routers: 0 of 14.
  - For comparison, MEMORY-EVAL measured precision at 33.3% (7 of 21).
- **Repetition.** The most-injected item went into 26 Turns, mostly to the tofu Project Router for the same long owner brief, where it is
  unrelated.
- **Limit.** Using a lesson without naming it cannot be detected. The 3.4% is a floor on use, not a measure of it.

### 2.4 Trust-class mix

**Method.** `memory_trust_class` is derived from the stored `status` and `provenance.source_class` (`lib/memory/contracts.py`).

| Store | owner-stated | verified | inferred | disproved |
|---|---:|---:|---:|---:|
| Lessons, tofu (2,230) | 0 | 0 | 2,230 (2,095 unrecorded source, 135 `agent_report`) | 0 |
| Lessons, global (295) | 0 | 0 | 295 (280 unrecorded, 15 `agent_report`) | 0 |
| My Context (owner scope `1_…`) | 0 | 0 | 4 `needs_confirmation` (`legacy_import`) | 13 superseded (`stale` 11, `duplicate` 1, `conflicts_with_owner_decision` 1) |

- **Lifecycle fields.** No lesson has `status`, `superseded_by` or `sensitive`; 9 have `enabled: false`.
- **Silent corrections.** 537 tofu lessons were edited in place (`updated` ≠ `created`), so corrections overwrite and never use the lifecycle.
  - Example: the stale SSH-key lesson that caused MEMORY-EVAL's and 1c's false memories was corrected in place on 11-12.
  - Its id is still `verify_internal_tofu_release_over_devexample_corpcom_rest_no_ssh_key_on_this_host`. Prefetch injected it 6 times in the window.
- **My Context has not changed since the audit.** The file was last written 2026-11-10, with 0 active items. `features.json` has
  `profile_consolidate: false`.

### 2.5 Repeat-mistake rate

**Not measurable as a rate.** Nothing records that a defect recurred after a lesson about it existed: no field, event or link.

Proxies from lesson text:

- **Recurrence language.** 281 of 2,230 lessons contain loose recurrence words ("again", "repeat", "follow-up to" …). 13 use strong phrasing
  ("hit again", "recurred", "regressed", "re-introduced").
- **Explicit recurrence after a lesson existed.** Two lessons document it:
  - `find_filessearch_files_omit_live_log_files_in_gitignored_logs` ("verified 2026-11-07, hit again");
  - `storage_fenced_for_an_hour_…` ("recurred 2026-11-12" after the 11-11 lesson).
- **Instructions without detection.** Two more tell the next agent "if hit again, fix it"; nothing detects whether that happened.
- **Near-duplicate lessons.** Jaccard ≥ 0.5 on name and description tokens finds 14 pairs, 9 of them written at least a day apart.
  - Example: `codelab-image-provisioning-via-mlapi` and `…_1`.
  - A suffixed duplicate means a writer did not find, or did not reuse, an existing lesson.

### 2.6 Outcome trend

**Method.** Every `review_project_work` call in the 5 Coordinator conversations was read for its `decision`.

| Day | accept | rework | reject |
|---|---:|---:|---:|
| 11-15 | 58 | 0 | 1 |
| 11-16 | 60 | 1 | 3 |
| 11-17 (to 01:30) | 17 | 2 | 0 |
| **Total** | **135** | **3** | **4** |

That is 1.05 reviews per accepted result. **There is no weekly trend:** the store holds 3 days, and the old-host history could not be read
(§2.1). Git history is not a substitute: commit counts measure throughput, not rework.

## 3. How project leads capture, retain and share knowledge today

The leads are the Global Router, the Project Routers and project Coordinators. Their tool ceiling is `COORDINATOR_TOOLS`
(`lib/tasks_pkg/coordinator_policy.py`) plus `ROUTER_ONLY_TOOLS` (`lib/router/policy.py`).

- **No durable write.** They have no file tools and no memory write tools; `search_memories` is their only memory tool.
- **Knowledge lands through others.** Anything durable a lead learns reaches storage through a Worker it delegates to, or stays in its
  mailbox and transcript.

Usage counts are from 11-15 … 11-17.

| Channel | Writes it | Reads it | Crosses projects? | Observed use |
|---|---|---|---|---|
| Project lessons (`<project>/.tofu/memories`) | Workers and chats (`create_memory`); leads cannot | round-0 prefetch for every role; `search_memories` | no (canonical project only) | 33 writes (parallel 13, tofu 13, router home 4, knowledge 3); 27 searches; 477 injections |
| Global lessons (`data/memories/global`) | the same tools with global scope | prefetch and search in every project | yes | 146 of 477 injections were global |
| My Context | owner UI; the learner (off); Phase 0 audit | `user_context` block, every role | yes (owner scope) | 0 active items; 0 `preferencesApplied` |
| Owner lookup (`search_memories mode=owner`) | — (read-only) | Router, Coordinators, Watchdog, Workers | yes | 2 calls (Global Router) |
| Project journal (`JOURNAL.md`) | any role with file tools (`lib/project_mod/journal.py`) | injected as project history | no | tofu: last write 2026-10-23 (716 KB, effectively retired); knowledge: written 11-16; parallel: none |
| `AGENTS.md` / docs | Workers through reviewed delivery | every role of that project as project rules | no | 782 commits touching `AGENTS.md` files in W44–W47 (tofu repo) |
| Owner-global skills (`data/skills/users/1/`, 24 skills) | Workers in the Router home, through reviewed publication | `load_skill`, every project | yes | 70 `load_skill` calls (`yourprovider-hope-platform` 31, `hope-910c` 10, `tofu-frontend` 9 …) |
| Cross-project lead messages (R13, `send_project_message`) | leads only | the recipient lead's inbox, `consider_project_message` | yes | 73 cross-project originals (Global Router received 22, tofu 4, knowledge 26, parallel 21); all accepted. Leads: 204 sends, 156 considers, 3 `list_project_leads` calls |
| Same-project reports | Workers to their lead | the lead | no | 71 (tofu 6, knowledge 48, parallel 17) |
| Standing instructions (R4) | Global Router, in a human Turn | Router forwards | yes | 0 records |
| Resource registry `RESOURCES.md` (R13) | Router-home Workers | `list_project_leads include_resources` | yes | **absent**: the file does not exist in `data/router/1/` |
| Deliverables and reports | `report_to_user` | the owner (lead office) | — | 32 `report_to_user` calls; the `deliverables` route returned 404 on the running build, which predates `280e472392` |

**The one observed compounding loop:**

1. The knowledge and parallel leads exchanged verified 910C, Hope and GPU-queue recipes: mount fixes, preflight and node health, queue
   capacity.
2. The Global Router relayed and corrected them.
3. A knowledge Worker published `NPU_PLAYBOOK.md`.
4. A Router-home Worker turned it into the owner-global skill `hope-910c` on 11-16.
5. Later corrections flowed back as messages ("all four shared 910C package fixes are done and verified").
6. Within a day `hope-910c` was loaded 10 times: knowledge 4, router home 4, parallel 2.

This loop is lead-driven and manual. No memory mechanism produced it.

## 4. Gaps and the smallest proof or fix (no knobs)

1. **Nothing measured compounding.** Done as §6; the first result is null, and §6.6 names what limits it.
   - **Smallest experiment, a time-sliced replay.** Take lessons written for a real defect that carry `provenance.conversation_id`/`turn_id`.
     135 lessons have this, all created 11-13 … 11-16. Only the 12 from 11-15 onward have source Turns in the live store; the other 123
     need the old store (gap 7), or new lessons collected as use continues.
   - **Arms.** For each source Turn's recorded round-0 request, two arms change only the lesson corpus: the snapshot just before the lesson's
     `created` time, and the snapshot just after.
   - **Measure.** Is the lesson retrieved, and does the response avoid or handle the defect? Judge plus blind hand labels, as in FM-2x2.
   - **Dilution control.** Run the same unrelated requests on both snapshots.
   - **Decision.** The pre-registered 95% bootstrap interval of the after − before difference. The rule is a statistical convention, not a
     tuned value.
   - **Reuse.** The MEMORY-EVAL harness (`evaluations/owner_memory/`) and the `k4-1c/tools/parity.py` frozen-corpus method do this today.
2. **Repeat mistakes are invisible.**
   - **Mechanism.** `create_memory` returns the ordinary `search_memories` result for its own description.
   - The writer then either updates or supersedes an existing lesson, recording `recurrence_of: <id>` through the existing `superseded_by`
     lifecycle, or writes a new one.
   - The repeat-defect rate per week then becomes a count. No threshold is involved: the writer decides from the normal search result.
3. **Corrections bypass the lifecycle.**
   - 537 in-place edits, 0 superseded or disproved, and stale ids keep matching.
   - **Fix.** An edit that changes a lesson's claim writes a successor plus `superseded_by` (the vocabulary already exists, see
     `context_engineering.md` "Owner memory").
   - Prefetch already excludes superseded lessons.
4. **Owner preferences do not accumulate.** My Context has 0 active items, the learner is off and Phase 1 is unmerged.
   - Smallest step, owner-only: confirm or reject the 4 `needs_confirmation` items.
   - Then the Phase 1 port decision in `docs/OLD_REPO_RECOVERY_MEMORY.md`, recommendation 1.
5. **Use is not recorded.** Turns record what was injected, not what was used; 1 of 477 items is named although `memory_guidance` asks for
   ids.
   - Measure first with experiment 1 (above) before adding anything.
   - A per-Turn "used ids" receipt would need the model to report it, and that is the same unreliable signal.
6. **Leads have no durable write of their own.**
   - Cross-project knowledge survives only when a lead gets a Worker to publish it (`hope-910c`).
   - The R13 registry `RESOURCES.md` that `list_project_leads` points to does not exist. This is an adjacent defect: report it to the Router
     home, or create the file through a reviewed Router-home Worker.
7. **History before 11-15 is unreadable read-only.**
   - A longitudinal outcome trend (rework rate and Turns per accepted result by week) needs either a read-only open of the old TofuDB
     authority, or an owner-approved import.

## 5. Acceptance-check evidence

1. **Verdict (§1):** not shown. It cites the design record (`docs/modules/context_engineering.md`), the recovery report, the raw files in the
   old `.eval-runs/owner-memory/` (`private/baseline-report.txt`, `k3-1c/REPORT-k3.md`, `fm2x2/private/analysis.json`) and the live
   measurements in §2.
2. **Longitudinal measures (§2):** counts and weekly growth; use and citations per Turn; trust mix; repeat-mistake proxies; the outcome table.
   Each states its method and limit. A weekly outcome trend could not be measured (3-day store).
3. **Lead channels (§3):** each row gives writer, reader, cross-project scope and observed count.
4. **Gaps (§4):** gaps with the smallest experiment or mechanism; none adds a knob. No code was changed; this file is the only change.

## 6. First longitudinal measurement: before/after-lesson replay (2026-11-16)

This section records the baseline on the current code (`eval/opencode-glm53-tb21` at `4d4f42e353`). A parallel Worker is changing
the memory mechanisms; rerun the same command under a new run name to measure them against the same frozen cases (§6.7).

### 6.1 Design (frozen before any replay)

Harness: `python -m evaluations.owner_memory replay` (`evaluations/owner_memory/replay.py`; stages and contracts in
`evaluations/owner_memory/README.md`). It builds on the owner-memory benchmark:

- recorded provider requests (`bases.capture_task_request`);
- production prefetch and composer (`prompts.build_request`);
- the frozen-store format (`corpus.write_slice`, `FrozenCorpus`);
- the transport and its stop rules.

Its pieces:

- **Corpus.** One copy of every lesson store was frozen at 2026-11-16T18:08Z: 4 stores (tofu, parallel, knowledge, global),
  2,606 files, digest `c8d22181…`. No lesson in any store was edited after 2026-11-15T08:00, so slicing by each file's
  `created` reproduces the store exactly at any instant in the window. Lessons deleted before the freeze cannot be reconstructed;
  §6.5 found none among the recorded selections.
- **Cases.** Every lesson whose `provenance` names a source Turn still in the live store: 31 candidates.
  - A fixed model (`claude-opus-5.5`, rubric prompt digest in `cases.json`) read each lesson and its request. It decided whether
    the lesson records a real defect, and wrote avoid and hit signs that a grader can apply without seeing the lesson.
  - **23 cases were included.** 1 was excluded as a design record. 7 were excluded because the gateway refused every rubric
    call: the parallel project's 910C/TAUD training cluster.
  - Frozen at 2026-11-16T19:07Z, digest `bb02e6c8…`.
- **Arms.** Each case replays the source attempt's recorded first-round request (real system blocks, tools, parameters and
  history). Only `relevant_memories` is recomputed, by production prefetch over:
  - **before:** every lesson created strictly before the lesson;
  - **after:** the same set plus that lesson only.
- **Interference arm.** A seeded sample (seed 20261116) of 20 other Worker Turns whose conversations wrote no lesson, replayed
  over:
  - **smaller:** the experiment's earliest store, before its first case lesson (299–2,536 files per project);
  - **larger:** the store at selection (303–2,553 files).
- **Replay.** Model `kunlunbedrock.claude-opus-5.5-m17` with each request's own parameters; 3 trials per case arm and 2 per
  interference arm; arms interleaved in a seeded order.
  - No tool executes.
  - A read of lessons the arm's snapshot holds returns their content, as production would.
  - Any other tool call ends the replay. What is scored is therefore the start of the work: text, reasoning summary and first
    tool calls.
- **Blind scoring.** The judge (`claude-opus-5.5`) grades every row in a seeded shuffled order. Its input never contains the
  arm, and every lesson name either arm injected is masked in the response.
  - Case outcomes: `avoided`, `hit` or `not_reached`.
  - Interference outcome: on-task `yes` / `partial` / `no`, scored 1 / 0.5 / 0.
- **Statistics.** Per-case difference after − before, averaged over cases. 95% interval from a two-stage bootstrap (10,000
  resamples) over source requests, then trials, because several lessons share one request. Pooled rates carry Wilson intervals.
  The interference arm passes if the lower bound of larger − smaller exceeds −0.10, FM-2x2's non-inferiority margin.

### 6.2 Per-case results (judge-scored)

"Injected" means production prefetch put the new lesson into the after-arm request. Where it did not, the two arms sent
byte-identical requests.

| Case | Store | Defect (topic) | Injected | Before: avoided/hit/n | After: avoided/hit/n |
|---|---|---|---|---|---|
| C01 | parallel | read-only storage misreported as quota exceeded | yes | 0/0/3 | 0/0/3 |
| C02 | tofu | headless Chromium hang from unreachable fontconfig path | no | 0/0/3 | 0/0/3 |
| C03 | tofu | Rust toolchain relocation off frozen volume | yes | 0/0/3 | 0/0/3 |
| C04 | tofu | wrong delta base causing false delivery conflicts | yes | 0/0/3 | 0/0/3 |
| C05 | tofu | task delta diffed from baseline, not merge-base | yes | 0/0/3 | 0/0/3 |
| C06 | tofu | Playwright browser path hidden by cache redirect | yes | 0/0/3 | 0/0/3 |
| C07 | knowledge | read-only tree migration and Hope payload packaging | no | 0/0/3 | 0/0/3 |
| C08 | tofu | git checkout timeout permanently blocks delivery | yes | 0/0/3 | 0/0/3 |
| C09 | tofu | order-dependent test via module-level manifest cache | yes | 0/0/3 | 0/0/3 |
| C10 | knowledge | Hope job stop failures and slow-loading liveness | no | 0/0/3 | 0/0/3 |
| C11 | parallel | missing model credential for live pilot | yes | 0/0/3 | 1/0/3 |
| C12 | tofu | restoring ruff, clippy and fmt lint gates | yes | 0/0/3 | 0/0/3 |
| C13 | knowledge | Hope CUDA queue probe config pitfalls | no | 0/0/3 | 0/0/3 |
| C14 | tofu | parallel sub-agent stalls on unconfirmed tool effects | no | 1/0/3 | 0/0/3 |
| C15 | parallel | E2 harness runtime path and rundir traps | yes | 0/0/3 | 0/0/3 |
| C16 | global | vendored MCP launcher pin and uv cutoff | no | 0/0/3 | 0/0/3 |
| C17 | parallel | runtime commit detection for post-commit verifier | yes | 0/0/3 | 0/0/3 |
| C18 | tofu | cross-host process exit evidence for recovery | no | 0/0/3 | 0/0/3 |
| C19 | tofu | stuck attempts after host handoff | no | 2/0/3 | 1/0/3 |
| C20 | global | hope metadata error misread as missing code version | yes | 0/0/3 | 1/0/3 |
| C21 | global | mtskills SkillHub headless SSO login via CIBA | no | 0/0/3 | 0/0/3 |
| C22 | global | MCP server silently loads stale installed package | no | 0/0/3 | 0/0/3 |
| C23 | tofu | engine gate crash probe fixture size | no | 0/0/3 | 0/0/3 |

### 6.3 Aggregate

| Subset | Cases | Avoided before [Wilson 95%] | Avoided after [Wilson 95%] | After − before [bootstrap 95%] | Hit before / after |
|---|---:|---|---|---|---|
| All | 23 | 4.3% (3/69) [1.5, 12.0] | 4.3% (3/69) [1.5, 12.0] | **+0.0 pp [−8.3, +7.3]**; 2 cases up, 2 down, 19 same | 0 / 0 |
| Lesson injected | 12 | 0.0% (0/36) [0.0, 9.6] | 5.6% (2/36) [1.5, 18.1] | +5.6 pp [0.0, +16.7] | 0 / 0 |
| Lesson not injected (identical requests) | 11 | 9.1% (3/33) | 3.0% (1/33) | −6.1 pp [−18.2, +3.0]: sampling noise | 0 / 0 |

**Verdict: compounding is not shown.** A lesson written for a real defect does not measurably change how the same request is
started.

### 6.4 Interference arm (no regression)

The judge refused every row of 2 of the 20 sampled tasks (both from the 910C cluster). That leaves 18 tasks with 36 rows per arm;
for 16 of them the larger store changed the injected lessons.

| Subset | Tasks | On-task, smaller [Wilson 95%] | On-task, larger [Wilson 95%] | Larger − smaller [bootstrap 95%] | Verdict (margin −0.10) |
|---|---:|---|---|---|---|
| All | 18 | 100% [90.4, 100] | 100% [90.4, 100] | 0.0 [0.0, 0.0] | no regression |
| Injection changed | 16 | 100% [89.3, 100] | 100% [89.3, 100] | 0.0 [0.0, 0.0] | no regression |

The bootstrap interval is degenerate (every row is on task), so the Wilson bounds are the informative ones. The store grew only
4–17 lessons per project over the window, so this tests the window's growth, not a corpus twice the size.

### 6.5 Validity checks

- **Snapshot parity.** Prefetch over the store as of each source attempt's start reproduced the memories the recorded request
  actually carried in 14 of 23 cases.
  - The 9 mismatches differ in one of the two slots. All the lessons the recorded requests carried still exist.
  - The mismatches are consistent with prefetch-query code that changed after those Turns (`6a66c62932`, 2026-11-15 18:21Z, in
    `lib/memory/prefetch/_query.py`). The replay uses current code in both arms on purpose.
- **Judge agreement.** I hand-labelled blind (no arm, names masked) a seeded sample of 30 judged case rows.
  - Agreement was 29/30 (96.7%). Cohen's κ is 0.0, uninformative here because the judge called all 30 rows `not_reached`.
  - I also checked the 6 rows the judge called `avoided`, blind to arm though not to the judge's verdict. I agree with 4.
  - Substituting all 36 hand labels gives +1.4 pp [−3.0, +6.9] overall and +2.8 pp [0.0, +11.1] where the lesson was injected,
    the same conclusion.
- **Gateway refusals.** 3 replay rows were refused and succeeded when retried; all 218 rows are usable. 8 interference rows
  could not be judged (§6.4).
- **Cost.** Replays $53.05 (218 rows, $0.24 each). Judge $2.71. Rubrics $0.91. Price card: USD per million tokens, input 4,
  cached 0.4, cache write 5, output 20.

### 6.6 What limits this measurement

1. **Retrieval is the first bottleneck.** In 11 of 23 cases, production prefetch did not select the lesson for the very request
   that produced it. Prefetch picks 2 lessons by metadata BM25 on the request text, and long multi-goal envelopes rank other
   lessons first. No downstream mechanism can help these cases.
2. **First-response scope.** The model's first round is almost always discovery: git status, grep, reading docs. 132 of 138 case
   rows were `not_reached` and none was `hit`, so the measure sits near its floor.
   - Defects that occur mid-task (a hang during visual tests, a conflict at delivery) cannot appear in the first round, even when
     the rubric author expected them to (`observableEarly` was true for 22 of 23 cases).
   - A mid-turn replay from the recorded round just before the defective action would be more sensitive. It needs per-case
     decision points, and per-round handling of the memory carriers.
3. **The lesson is the outcome of the replayed request.** Where the lesson was injected, the model often said that the memory
   suggests the work "may already be done" and checked that first. This arm partly measures "the same task asked again", not
   "a later, different request that meets the same defect". Truly recurring defects need the later requests that met them, and
   nothing records those links (§4 gap 2).
4. **Coverage.** 3 days of live history and 23 cases. The 910C/TAUD cluster is missing because the gateway refuses its content.

### 6.7 Reproduce and re-measure

```bash
# private root and cases are already frozen; every stage resumes
.venv/bin/python3 -m evaluations.owner_memory replay \
  --private <canonical>/.eval-runs/memory-replay/private \
  --key-hints 6672 --stop-on-429 --jobs 2 --run-name <name>
# blind hand-label check of the judge: --stage sheet, fill handlabels.json, --stage agree
```

- **Where the data is.** The baseline lives in `<canonical>/.eval-runs/memory-replay/private/runs/baseline/` (`report.json`,
  `report.md`, `calibration.json`, `parity.json`). Nothing under the private root is committed. The committed tests
  (`tests/evaluations/test_owner_memory_replay.py`) use synthetic lessons only.
- **Measuring a mechanism change.** Run the command under a new `--run-name`. A run directory refuses rows whose request digests
  or replay semantics differ from its own, so the comparison stays on the same frozen cases.
- **After the parallel mechanism changes landed** (merged at `d5844809df`, touching `lib/memory/prefetch/_inject.py`, `_query.py` and
  `_run.py`): a dry run with no model calls builds every request and gives the same retrieval (12 of 23 lessons injected) and
  parity (14 of 23). Whether they change outcomes needs a model rerun under a new run name.
- **AppIds.** At the time of this run, AppId `0733` (used by FM-2x2) answered 401. A 401 or 403 now stops a run at once
  (`82155085f9`).
