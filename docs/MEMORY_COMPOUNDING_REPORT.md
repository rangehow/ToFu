# Does Tofu get better the more it is used? Memory and lead-knowledge report (2026-11-17)

This is a one-time report written for the owner's question. Delete it once the owner has decided on the gap list.
No code was changed. Every live measurement was a read: memory files read in place, plus loopback `GET`s through
`debug/inspect_conversation.py`'s `_DiagnosticAPI`. Private owner content is reported only as counts.

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

**最小的验证办法：** 按时间回放。拿真实缺陷写下的经验，在"这条经验写之前"和"之后"两个语料快照上重跑同一个请求，看回答是否避开了这个缺陷。详见第 4 节。

## 1. Verdict: not shown

| Claim part | Evidence | Status |
|---|---|---|
| Memory helps at one point in time | MEMORY-EVAL baseline: application 31.7% with prefetch, 28.3% without; Coordinator full context 56.2% → 43.8% without prefetch (`docs/modules/context_engineering.md`, "Owner understanding: design record", Decision 8; `docs/OLD_REPO_RECOVERY_MEMORY.md`, "Experiment findings"; raw `.eval-runs/owner-memory/private/baseline-report.txt`, coordinator/full `appliedRate` 0.5625) | partially shown (small effect, one snapshot) |
| An always-present owner profile helps at one point in time | 1b K1 +20.0 pp; 1c K1 +16.1 pp [+5.4, +28.6] but 1c NOT PASSED (K3) (`k3-1c/REPORT-k3.md`); FM-2x2 PASS (`fm2x2/private/analysis.json` `verdict: {profile|L1: PASS, profile|L0: PASS}`) | shown in the lab; **not in production**: Phase 1 is unmerged (`docs/OLD_REPO_RECOVERY_MEMORY.md` §A) and My Context has 0 active items |
| The system improves as it is used (compounding) | No experiment varies corpus age or usage. Every arm adds one carrier to recorded round-0 requests on a frozen or drifted corpus. 1c and FM-2x2 froze the lesson corpus on purpose (`context_engineering.md`, 1c "Corpus", FM-2x2 "Design"; frozen corpora in `k4-1c/corpus-*`). The benchmark measures application "in a later, unrelated turn" (`docs/evaluations/OWNER_MEMORY_BENCHMARK.md`), which tests recall of a stated fact, not growth. | **not shown: nothing measures it** |
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

1. **Nothing measures compounding.**
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
