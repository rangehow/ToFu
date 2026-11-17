# Does Tofu get better the more it is used? Memory and lead-knowledge report (2026-11-17)

This is a one-time report written for the owner's question. Delete it once the owner has decided on the gap list.
Sections 1–5 changed no code. Every live measurement in them was a read: memory files read in place, plus loopback `GET`s
through `debug/inspect_conversation.py`'s `_DiagnosticAPI`. Section 6 adds the replay benchmark that the gap list proposed
(`evaluations/owner_memory/replay.py`) and its first measurement. Section 7 diagnoses its retrieval misses, replays from
the defect point, adds a cross-request arm (`evaluations/owner_memory/midtask.py`) and measures one candidate mechanism. Section 8
measures and ships lesson triggers (`evaluations/owner_memory/triggers.py`). Section 9 scales the defect replay to 126 cases
with power and diagnoses why lessons in context still fail (`evaluations/owner_memory/scale.py`). Private owner content is
reported only as counts and topics.

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

**第二次测量（2026-11-17，见第 7 节）：还是没有证明"越用越好"。**

- **为什么 11 个案例没取到自己的新经验。** 不是角色过滤，也基本不是置信门槛（只有 1 例）。
  - 5 个案例的经验写的是"干活途中才遇到"的问题，开头的请求里根本没提到。
  - 3 个是一句话的追问，主题在更早的对话里。
  - 3 个被同主题的其他经验挤出了前两名。
- **试了一个结构性修复，但没有上线。** 做法是工具调用失败时，用失败信息再选一次经验。
  - 新经验出现在自己 Turn 里的比例从 12/23 升到 16/23。
  - 但它额外注入的经验只有 23% 相关（开头选的是 68%）。
  - 结果上也没有可测的提升，所以作为候选留在评测代码里。
  - 986c9ef4 的 held-out 选择 93/93 不变。
- **从踩坑前一步开始重放（11 个有明确踩坑步骤的案例）。** 现在 36% 的回答真正走到了缺陷那一步，第一次只有 4%。
  - 有经验减没经验，避开率变化 −3.0 个百分点，95% 区间 [−21.2, +15.2]。
- **跨请求（7 个后来的不同请求又踩了同一个坑）。**
  - 这 7 次发生时，库里其实已经有那条旧经验。
  - 重放时有没有那条旧经验，避开率都是 0。

**第三次测量（2026-11-17，见第 8 节）：经验现在会记录"在什么情况下用得上"，并且已经上线。"越用越好"在结果上仍然没有证明。**

- **改动。** 每条经验除了"怎么修好的"，还记录触发情境（trigger）：什么样的请求或工作、涉及的文件/命令/工具、会看到的症状或报错。新写和改写经验时必须填写；已有的 2,615 条经验用一次回填补上（模型只看经验本身）。选择仍走原来的打分、门槛和每次 2 条，没有新增任何参数。
- **回填质量（盲测）。** 抽 40 条，混入别的经验的触发情境：自己的 40/40 判为对得上，别人的 0/40；有 9/40 顺带写了一部分修法。
- **取得到。** 23 个案例里，新经验被自己的请求选中从 12 条到 13 条；踩坑前一步 4/11 到 5/11；跨请求 3/7 到 4/7。提升很小，有进有出。
- **更相关（主要收益）。** 986c9ef4 的 held-out 93 个 Turn，盲评：相关注入从 91 条到 106 条（+15，95% 区间 [+6, +25]），无关的少 12 条 [−22, −3]，相关率 60.3% → 68.8%（+8.6 个百分点 [+3.0, +14.8]）。23 个案例和 20 个干扰任务开头注入的相关率 67.9% → 72.6%。预先登记的上线条件全部满足。
- **结果。** 踩坑前一步重放：避开率 27.3% → 30.3%（+3.0 个百分点 [−15.2, +24.2]）；跨请求：0/21 → 1/21。都不显著。

**第四次测量（2026-11-17，见第 9 节）：样本够大了，结论清楚——经验本身有用，但生产环境还没有把它用上，所以"越用越好"仍然没有证明。**

- **样本。** 线上能找到的全部 401 个 Worker Turn，覆盖 tofu、parallel、knowledge 三个项目和全局库。得到 127 个真实缺陷案例（126 个有完整评分）：28 个是"这个 Turn 自己写的经验"，99 个是"别的对话早先写过、这次又踩了"的复发。每组 6 次重放，盲评。
- **把经验放到模型眼前，有明确效果。** 避开率从 17.3% 升到 34.8%，+17.4 个百分点 [+10.9, +24.6]。
- **但生产检索只在 25/126 个案例里把对的经验放进去。** 按生产实际运行，有没有这条经验：+1.8 个百分点 [−1.5, +5.3]。这个区间对 10 个百分点的效果有足够把握（功效约 1.0），可以说生产环境里没有 10 个百分点量级的"越用越好"。复发案例中，库里早有经验，生产只在 9/99 次把它放进去。
- **经验在眼前却仍然踩坑，原因主要是"没理会"。** 25 次踩坑里，17 次经验明明适用、写清了怎么做，回答却既没提也没照做（只有 3 次提到经验）；5 次用错，3 次经验太笼统，0 次经验本身错误。
- **试了一个结构性修复：在下一步动作前，把已选中的经验改写成一句"遇到 X 时，做 Y"的检查。** 预先登记的三条上线条件全部满足：避开率 34.8% → 56.0%（+21.3 个百分点 [+14.3, +28.3]），选中的经验完全不变，80 个无关任务的质量不降（99.7% 对 99.7%）。
- **但没有上线。** 生产的上下文组装器不能做到"只在动作前出现一次"。能做到的方式是每轮工具调用后重发一份，实测效果归零（+0.3 个百分点 [−4.6, +5.5]），模型把每轮都出现的话当成背景。下一步是只在"即将执行的工具调用和经验的触发情境匹配时"出现一次检查，需要找到不靠阈值的匹配规则。

## 1. Verdict: not shown

| Claim part | Evidence | Status |
|---|---|---|
| Memory helps at one point in time | MEMORY-EVAL baseline: application 31.7% with prefetch, 28.3% without; Coordinator full context 56.2% → 43.8% without prefetch (`docs/modules/context_engineering.md`, "Owner understanding: design record", Decision 8; `docs/OLD_REPO_RECOVERY_MEMORY.md`, "Experiment findings"; raw `.eval-runs/owner-memory/private/baseline-report.txt`, coordinator/full `appliedRate` 0.5625) | partially shown (small effect, one snapshot) |
| An always-present owner profile helps at one point in time | 1b K1 +20.0 pp; 1c K1 +16.1 pp [+5.4, +28.6] but 1c NOT PASSED (K3) (`k3-1c/REPORT-k3.md`); FM-2x2 PASS (`fm2x2/private/analysis.json` `verdict: {profile|L1: PASS, profile|L0: PASS}`) | shown in the lab; **not in production**: Phase 1 is unmerged (`docs/OLD_REPO_RECOVERY_MEMORY.md` §A) and My Context has 0 active items |
| The system improves as it is used (compounding) | Earlier experiments never varied corpus age. The before/after-lesson replay (§6, 2026-11-16) now does: on 23 real defects, avoidance is 4.3% before and 4.3% after the lesson, difference 0.0 pp, 95% CI [−8.3, +7.3]. From the defect point (§7.3, 11 cases): −3.0 pp [−21.2, +15.2]. In later, different requests that met the same defect (§7.4, 7 requests): 0 of 21 avoided with or without the earlier lesson. With lesson triggers (§8): defect point +3.0 pp [−15.2, +24.2], later requests 0/21 → 1/21. At scale (§9, 126 cases, 6 trials): production +1.8 pp [−1.5, +5.3] (power ≈ 1.0 for 10 pp); with the lesson in front of the model +17.4 pp [+10.9, +24.6] | **not shown in production, now with power: a lesson the model sees helps clearly, but production shows the right lesson in 25 of 126 cases, and a model with the lesson in context mostly ignores it (17 of 25 hits). A pre-action check fixes that (+21.3 pp) but has no deliverable form yet (§9.4)** |
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

## 7. Second measurement: retrieval diagnosis, defect-point replay, cross-request arm (2026-11-17)

Code: merged `eval/opencode-glm53-tb21` at `c83f91e43b` (986c9ef4's memory mechanisms and ee07449f's revert included) plus
this change. Harness: `evaluations/owner_memory/midtask.py` (stages in `evaluations/owner_memory/README.md`); same private
root, frozen corpus and 23 frozen cases as §6. Private evidence: `.eval-runs/memory-replay/private/diagnosis/`, `points.json`,
`recurrences.json`, `runs/midtask-v1/`.

**Plain answer: "better with use" is still not shown.** At the defect point, a lesson changes how often the defect is
avoided by −3.0 pp [−21.2, +15.2]. A later, different request that met an earlier lesson's defect again avoided it 0 times in
21 replays with the earlier lesson and 0 in 21 without it.

### 7.1 Why the new lesson is not selected for its own request (11 of 23)

Each case's after-arm selection was replayed stage by stage (`diagnosis/retrieval-before-fix.json`): the query production
extracts, the lesson's BM25 position in the metadata ranking (1 = first; two are injected), the confidence gate and role fit.

- **Role filter:** never the cause. All 23 lessons fit the Worker's tools.
- **Confidence gate:** the cause only for C18, which has no overlapping token at all.
- **Query cap:** the cause for C21. In the recorded envelope its subject ("SkillHub") lies beyond the 2,000-character cap.
- **Legacy envelope:** not a cause. 15 recorded inputs use the pre-`6a66c62932` "Original human request … Assigned objective"
  envelope. Re-rendered in today's format (as 986c9ef4 did), the same 11 lessons are missed
  (`diagnosis/retrieval-modern-envelope.json`).

| Case | Position (recorded / today's envelope) | Cause | Evidence |
|---|---|---|---|
| C02 | 593 / 384 of 2,503 | query text: the lesson's subject appears only mid-task | the request is a studio-UI art task; the Chromium/fontconfig hang happened while running visual tests |
| C14 | 1,335 / 1,914 of 2,512 | query text: mid-task subject | a multi-goal optimisation request; the swarm stall on unconfirmed tool effects happened during the work |
| C21 | 7 / 7 of 293 | query text: mid-task subject, cut by the query cap | the SkillHub login failure was found while executing a replacement brief; untruncated, the lesson ranks 1st |
| C22 | 4 / 4 of 2,518 | ranking: a sibling lesson from the same investigation ranks first | the get_metadata parameter-bug lesson outranks the stale-package lesson |
| C23 | 76 / 76 of 2,519 | query text: mid-task subject | the request is about engine certification; the fixture-size trap appeared at the gate run |
| C18 | not ranked (0 shared tokens) | query text + gate: a 36-character follow-up | "根源性修复吧…"; the subject lives in earlier Turns |
| C19 | 49 / 49 of 2,515 | query text: a follow-up | "still seems stuck"; the subject is in earlier Turns |
| C10 | 4 / 4 of 305 | query text + ranking: a Coordinator follow-up instruction | two 910C/Hope lessons outrank it |
| C07 | 9 / 15 of 304 | ranking: a crowded topic | 8–14 other Hope/910C lessons outrank it |
| C13 | 3 / 4 of 306 | ranking: a crowded topic | two 910C queue lessons outrank it |
| C16 | 7 / 7 of 292 | ranking: sibling Overleaf MCP lessons | the output-URL and citation-verifier lessons outrank it |

Five of the eleven lessons (C02, C14, C21, C22, C23) record something met **during** the work, which the opening request never
names. Three more are follow-ups whose subject is in earlier Turns. The other three lose to lessons on the same topic.

### 7.2 The structural fix tried: selection at a failed tool round (not shipped)

- **Mechanism (no new knob).** After a tool round whose result is a failure (the compaction owner's `error` evidence class:
  `isError`, a non-zero `run_command` exit, or an error word opening the result), the turn-start selection runs again. It uses
  the same candidate pool, confidence gate, role fit and two-item cap, over the failed calls and their results. Lessons already
  visible in the Turn are left out, and nothing ranked lower takes their place.
  - The block is appended once per round through the production composer.
  - Turn-start selection is unchanged.
- **Selection, before → after** (`runs/midtask-v1/precision-pairs.json`, dry runs):

  | Measure | Turn start only (production) | With failure-time selection |
  |---|---:|---:|
  | Case lesson selected anywhere in its own Turn | 12 / 23 | 16 / 23 (adds C02, C13, C18, C23) |
  | Case lesson in front of the model at the defect point (§7.3) | 4 / 11 | 6 / 11 (adds C02, C18) |
  | Earlier same-defect lesson selected in the later request (§7.4) | 3 / 7 | 3 / 7 |
  | 986c9ef4 held-out split (93 Turns), selections identical to 986c9ef4 | 93 / 93 | 93 / 93 (relevance 82/151, unchanged) |

- **Relevance of what each trigger injects.** Over the 23 case Turns and the 20 interference Turns, the same judge
  (`claude-opus-5.5`, fixed prompt, blind to arm) labelled every injected lesson.

  | Trigger | Injected | Relevant | Precision [Wilson 95%] | Per Turn |
  |---|---:|---:|---|---:|
  | Turn start | 84 | 57 | 67.9% [57.3, 76.9] | 1.95 |
  | Failed tool round (case Turns) | 102 | 25 | 24.5% [17.2, 33.7] | 4.43 |
  | Failed tool round (interference Turns) | 58 | 12 | 20.7% [12.3, 32.8] | 2.90 |
  | Failed tool round (all) | 160 | 37 | 23.1% [17.3, 30.2] | 3.72 |

- **Outcome at the defect point** (§7.3, `after_turnstart` → `after`):
  - avoided +3.0 pp [−9.1, +15.2];
  - hit −6.1 pp [−18.2, 0.0].
- **Decision: not shipped.** It raises selection, but three of four lessons it adds are irrelevant: about 3.7 extra lessons per
  failing Turn, with no detectable gain. Shipping it would lower the relevance of what the owner's roles read, which the brief
  forbids.
  - It stays as an evaluated candidate in `evaluations/owner_memory/failure_selection.py`.
  - It reproduces all 51 measured request digests byte for byte.
  - `lib/` is unchanged by this work.
- **Alternatives.**
  - A score floor, a top-1 cap or a stricter overlap count at failures: rejected. They are tuned knobs (AGENTS.md principle 5).
  - Exact-identifier matches only: not better (21 of 83 relevant, against 16 of 77 for token overlap).
  - Also requiring the lesson to pass the gate against the Turn's request: exploratory, on the same labels. 150 of 160 pass
    anyway, with the same precision.
  - Recent-history queries for follow-ups (C18, C19, C10): not tried. 986c9ef4 kept selection on the latest authored words,
    and changing that moves the held-out split, which would need new blind labels.

### 7.3 Mid-task replay: resume just before the defective action

- **Defect points (pre-registered, `points.json`).**
  - A fixed model read each case's frozen rubric and the numbered tool steps of its source Turn, and named the defective step.
  - The replay starts from the Inspector-recorded request of the round that issued that step. It is checked to hold every
    earlier real tool result and not the step itself.
  - **11 of 23 cases have a defect point.** In 11 others, the source Turn's agent avoided or fixed the defect itself: the
    request asked it to fix exactly that defect, and the lesson records the fix. C09's Turn issued no tool call.
- **Arms.** `before` is the store before the lesson. `after_turnstart` adds the lesson, with production selection.
  `after` adds the lesson and the §7.2 candidate.
  - Each arm recomputes the Turn's turn-start carrier, and in the failure arms the failure-time carriers too, over its snapshot.
  - 3 trials per arm.
  - Blind judging: the judge sees the request, the latest steps, the original agent's next action, the rubric and the
    response, with lesson names masked and no arm.
- **The defect point is reached.** 36% of rows are `avoided` or `hit`, against 4% in §6's first-round replay (132 of 138
  were `not_reached` there).

| Case | Topic | Defect step | Lesson in front of the model (`after`) | before av/hit/n | after_turnstart | after |
|---|---|---|---|---|---|---|
| C01 | read-only storage misreported as quota | 54 of 84 | turn start | 0/0/3 | 0/0/3 | 0/0/3 |
| C02 | Chromium hang from fontconfig path | 24 of 117 | failure only | 0/0/3 | 0/0/3 | 0/0/3 |
| C10 | Hope job stop failures | 4 of 10 | no | 2/0/3 | 1/0/3 | 1/0/3 |
| C12 | restoring lint gates | 38 of 249 | turn start | 1/1/3 | 2/0/3 | 2/0/3 |
| C13 | Hope CUDA probe config | 21 of 129 | no | 1/0/3 | 0/1/3 | 0/0/3 |
| C14 | sub-agent stalls on unconfirmed effects | 110 of 380 | no | 0/0/3 | 0/0/3 | 0/0/3 |
| C15 | E2 runtime path traps | 236 of 396 | turn start | 1/0/3 | 0/0/3 | 0/0/3 |
| C17 | commit detection for verifier | 46 of 116 | turn start | 3/0/3 | 3/0/3 | 3/0/3 |
| C18 | cross-host exit evidence | 27 of 57 | failure only | 2/0/3 | 2/1/3 | 3/0/3 |
| C21 | SkillHub headless login | 31 of 88 | no | 0/0/3 | 0/0/3 | 0/0/3 |
| C23 | engine gate fixture size | 21 of 41 | no | 0/1/3 | 0/3/3 | 0/3/3 |

| Subset | Cases | Avoided: before / after_turnstart / after [Wilson 95%] | before → after [bootstrap 95%] | Hit: before / after |
|---|---:|---|---|---|
| All | 11 | 30.3% [17.4, 47.3] / 24.2% [12.8, 41.0] / 27.3% [15.1, 44.2] | **−3.0 pp [−21.2, +15.2]** (2 up, 3 down) | 6.1% / 9.1% |
| Lesson in front of the model in `after` | 6 | 38.9% / 38.9% / 44.4% (n = 18 each) | +5.6 pp [−22.2, +33.3] | 5.6% / 0.0% |
| Lesson reached only at a failure | 2 | 33.3% / 33.3% / 50.0% (n = 6) | +16.7 pp [0.0, +66.7] | 0.0% / 0.0% |
| Lesson not in front of the model | 5 | 20.0% / 6.7% / 6.7% (n = 15) | −13.3 pp [−40.0, +6.7] (identical-request noise) | 6.7% / 20.0% |

- **Noise floor.** C13 and C23 send byte-identical requests in all three arms. C23 still moves from 1 hit to 3 hits in
  3 trials.
- **Without the lesson, the defect is often avoided anyway.** 30% of `before` rows avoid it (C17: 3 of 3), so a replayed
  model does not reliably repeat the original agent's mistake.
- **"Same task asked again" persists.** One `after` response in C17 read the injected lesson as "a sibling worker already
  built this" before checking.
- **Judge check.** I blind-labelled a seeded sample of 16 judged mid-task rows (no arm, names masked; `calibration.json`).
  - First pass: 12 of 16 agreed. I had read responses truncated to 1,300 characters.
  - The four disagreements, re-read on the full response the judge saw: all four go the judge's way (one borderline, H13).

### 7.4 Cross-request arm: a later, different request that met the same defect

- **Recurrences (`recurrences.json`).** No lesson carries 986c9ef4's `recurrences` links yet: they started on 11-16. They were
  reconstructed with the same repeat check `create_memory` now runs.
  - For each case lesson, the ordinary lesson search for its name and description was run over the store just before it:
    688 older lessons from other conversations.
  - A fixed model judged whether the case's defect is the older lesson's defect happening again.
  - **14 pairs over 7 later requests** (C04, C05, C06, C08, C09, C14, C23). One judgment of 910C content was refused by the
    gateway and counts as "no verdict".
  - In all 7, **the store already held a lesson about the defect when the later request ran, and the defect recurred anyway.**
  - The 13 lessons whose text says "hit again" or "recurred" (§2.5) recurred before the live window, so their later
    requests cannot be replayed.
- **Arms.** `with` is the store as the later request found it. `without` is the same store minus its earlier same-defect
  lessons.
  - Replayed from the first round (`XS`, 7 requests) and, where a defect point exists, from the defect point (`XM`: C14, C23).
  - 3 trials per arm.

| Replay | Requests | Earlier lesson injected (`with`) | Avoided without / with | Hit without / with | with − without |
|---|---:|---:|---|---|---|
| First round | 7 | 3 (C05, C06, C09) | 0/21 / 0/21 | 0/21 / 0/21 | 0.0 pp [0.0, 0.0] |
| Defect point | 2 | 0 | 0/6 / 0/6 | 3/6 / 2/6 | hit −16.7 pp [−50.0, 0.0] (identical requests) |

**The cross-request arm does not show "better with use" either.**

- Where the earlier lesson reached the later request (3 of 7), the first round never reached the defect.
- Where the replay reached the defect (C14, C23), retrieval never brought the earlier lesson.

### 7.5 What this establishes and what is next

- **Measurement.** The defect point is now reached, and repeats are now detectable.
  - The 11-case mid-task arm and the 7-request cross-request arm are both small. A ±20 pp interval is what 33 rows per arm buy.
  - Every new lesson written with `repeat_of` adds a cross-request case automatically. Rerun under a new `--run-name` as they
    accumulate.
- **Mechanism.**
  - The binding constraint is unchanged: lessons are keyed to what the agent met, while selection reads what the owner asked.
  - Selecting on failures reaches those lessons, but it brings mostly noise with them.
  - A trigger that is both reachable and precise has not been found without a tuned threshold (§8 then moved the situation to the write side: lesson triggers).
- **Reproduce.**

```bash
.venv/bin/python3 -m evaluations.owner_memory midtask \
  --private <canonical>/.eval-runs/memory-replay/private \
  --key-hints 6672 --stop-on-429 --jobs 4 --run-name <name>
```

- **Run notes.**
  - C02's request carried a screenshot the Inspector keeps only as `[base64 image, N chars]`, so its 9 first rows failed with
    HTTP 400.
  - The harness now sends a text note for such images. All arms are equal, so the model cannot see that screenshot.
  - C02's three request digests were re-pinned in the run manifest (`repinned`), because no usable C02 row existed.
- **Cost.**
  - Replays: $117.65 (153 rows, $0.77 each; requests of 110 KB–1.2 MB).
  - Judge: $4.95.
  - Points, recurrence and precision labels: about 955 short calls of the judge model, not itemized (the stage logs carry no
    usage).

## 8. Third measurement: lesson triggers, shipped (2026-11-17)

Code: this change, merged with `eval/opencode-glm53-tb21` at `b79e8f0062` (986c9ef4's mechanisms included). Harness:
`evaluations/owner_memory/triggers.py` plus `--corpus` on `replay` and `midtask`. Private evidence:
`.eval-runs/memory-replay/private/triggers/` (`PREREGISTRATION.txt`, `cache.json`, `spotcheck*.json`,
`heldout-labels.json`, `heldout-report.json`, `live-backfill.json`), `corpus-triggers/`, `runs/dryrun-triggers`,
`runs/midtask-triggers-v1/`, and `memory-compounding-20261116/heldout-trigger.jsonl`.

**Plain answer: what is injected is now measurably more relevant, and slightly more often the right lesson; "better with
use" is still not shown on outcomes.**

### 8.1 Mechanism

- §7.5 named the binding constraint: lessons are keyed to what the agent met, selection reads what was asked. The fix is on
  the write side. Each lesson records its **trigger**, the situation in which it applies (the kind of request or work,
  files, commands or tools, the symptom or error text), beside its outcome. `create_memory` and `merge_memories` require it;
  `update_memory` and a repeat write it into the new version (`docs/modules/context_engineering.md`, "Lesson triggers").
- Selection is unchanged except that the trigger joins the existing BM25 document and the confidence gate. No cap,
  threshold or top-k changed.
- **Backfill.** A fixed model (`claude-opus-5.5`, the judge model) read each existing lesson, and nothing else, with
  `TRIGGER_BACKFILL_SYSTEM`. 2,553 of the frozen corpus's 2,575 lessons got a trigger; the gateway refused 22 (the 910C/TAUD
  content of §6.7). The replay corpus and 986c9ef4's held-out snapshot were copied with those triggers; the frozen sources
  were not written.
- **Backfill quality, blind** (`spotcheck.json`). 40 seeded lessons, each shown once with its own trigger and once with
  another sampled lesson's, shuffled, origin hidden. Own trigger fits: 40/40 [91.2, 100]; foreign: 0/40. Own triggers
  that also state part of the fix: 9/40 (22.5%) — they still describe the situation, with some of the answer.

### 8.2 Pre-registered ship rule and result

The rule was written before any trigger-arm result (`PREREGISTRATION.txt`): ship only if (1) the case lesson's turn-start
retrieval on the 23 cases rises, (2) on 986c9ef4's held-out split the precision of what is injected does not fall and the
relevant count does not fall, and (3) turn-start precision over the 43 replay Turns does not fall.

| Measure | Production before | With triggers |
|---|---:|---:|
| Case lesson selected for its own request (23 cases, `replay --dry-run`) | 12 / 23 | **13 / 23** (gains C07, C10, C13, C22; loses C11, C12, C20) |
| Same, at the mid-task Turn start (11 cases) | 4 / 11 | 5 / 11 |
| Earlier same-defect lesson in the later request (7) | 3 / 7 | 4 / 7 |
| Held-out (93 Turns): relevant / injected, fresh blind labels | 91 / 151 (60.3%) | **106 / 154 (68.8%)** |
| Held-out difference, Turn-bootstrap 95% CI | | relevant +15 [+6, +25]; irrelevant −12 [−22, −3]; precision +8.6 pp [+3.0, +14.8] |
| Turn start, 23 case + 20 interference Turns (§7.2 judge) | 57 / 84 (67.9%) | 61 / 84 (72.6%) |

- **Held-out labels.** Every (Turn, lesson) pair either arm injects (211) was labelled once by one fresh labeller
  (`claude-opus-5.5`, fixed prompt, shuffled, no arm), over what 986c9ef4's labeller saw: role, request, lesson title and
  description. It agreed with 986c9ef4's labels on 132 of the 151 pairs both labelled (87%).
- By role (relevant / injected, before → after): Workers 79/112 → 91/112, Global Router 2/11 → 4/14, Project Routers
  4/6 → 5/6, chat 6/16 → 6/16, Watchdog 0/6 → 0/6. 50 of 93 Turns changed selection.
- **Decision: shipped.** All three conditions hold. The retrieval gain is small and churns (4 gained, 3 lost); the
  measured benefit is mostly relevance: the same 2-item budget now carries more lessons that should change what the
  role does.
- **Live backfill.** `scripts/backfill_memory_triggers.py` wrote the measured trigger into every live lesson (2,615 in the
  tofu, knowledge, parallel, cpt_data, hope-mcp and llm-mcp stores and the global store); every one was in the measured
  cache, no model call was needed, and a second dry run found 2,615 already done. Selection uses them once the server runs
  this code (nothing was restarted).
- The failure-time candidate of §7.2 also improves with triggers (precision 23.1% → 27.6%, 47 of 170), still far below turn
  start; it stays unshipped.

### 8.3 Outcomes: mid-task and cross-request replay with triggers

Same frozen cases, points and recurrences as §7, over `corpus-triggers`; 3 trials per arm; the §7.3 blind judge (masked
lesson names, no arm). `after_turnstart` is production with triggers; `after` adds the unshipped failure-time candidate.

| Case | before av/hit/n | after_turnstart | after |
|---|---|---|---|
| C01 | 0/0/3 | 0/0/3 (lesson shown) | 0/0/3 (shown) |
| C02 | 0/0/3 | 0/0/3 | 0/0/3 (shown) |
| C10 | 1/0/3 | 0/0/3 (shown) | 0/0/3 (shown) |
| C12 | 1/0/3 | 2/0/3 | 0/0/3 |
| C13 | 1/1/3 | 3/0/3 (shown) | 3/0/3 (shown) |
| C14 | 1/0/3 | 0/0/3 | 0/0/3 |
| C15 | 0/0/3 | 0/0/3 (shown) | 0/0/3 (shown) |
| C17 | 3/0/3 | 3/0/3 (shown) | 3/0/3 (shown) |
| C18 | 2/0/3 | 2/0/3 | 2/1/3 |
| C21 | 0/0/3 | 0/0/3 | 0/0/3 |
| C23 | 0/1/3 | 0/2/3 | 0/2/3 |

| Arm comparison | Avoided | Difference [cluster bootstrap 95%] |
|---|---|---|
| Mid-task, all 11: before → after_turnstart | 27.3% → 30.3% | +3.0 pp [−15.2, +24.2] (2 up, 2 down) |
| Mid-task, before → after | 27.3% → 24.2% | −3.0 pp [−21.2, +18.2] |
| Mid-task, lesson shown in `after` (6) | 27.8% → 33.3% | +5.6 pp [−16.7, +33.3] |
| Cross-request, first round (7): without → with | 0/21 → 1/21 | +4.8 pp [0.0, +19.0] |
| Cross-request, defect point (C14, C23) | 1/6 → 1/6 | 0.0 pp; hit 1/6 → 2/6 |

- **"Better with use" is not shown.** Every outcome interval spans zero. The one case where triggers newly bring the
  lesson and the defect is reached (C13) goes from 1 avoided / 1 hit to 3 avoided, but one case of three trials is noise
  level (§7.3: C23 moved by 2 hits between identical requests).
- **Limits.** 11 mid-task cases and 7 later requests buy ±20 pp intervals. The C02 image note and C23 noise floor of §7.3
  apply unchanged. Cost: replays $118.53, judge $4.97, plus the backfill (≈2,600 short calls), held-out labels (211) and
  spot-check (80), which the stage logs record without usage.

### 8.4 Reproduce

```bash
P=<canonical>/.eval-runs/memory-replay/private; H=<canonical>/.eval-runs/memory-compounding-20261116
python -m evaluations.owner_memory triggers --stage backfill --private $P --heldout-dir $H --key-hints 6672 --stop-on-429 --jobs 4
python -m evaluations.owner_memory triggers --stage copy --private $P --heldout-dir $H
python -m evaluations.owner_memory triggers --stage spotcheck --private $P --key-hints 6672 --stop-on-429
python -m evaluations.owner_memory replay --stage run --dry-run --private $P --corpus corpus-triggers --run-name <name>
python -m evaluations.owner_memory midtask --private $P --corpus corpus-triggers --key-hints 6672 --stop-on-429 --jobs 3 --run-name <name>
python -m evaluations.owner_memory triggers --stage label --private $P --heldout-dir $H --arms baseline=heldout-v3-revert.jsonl,candidate=heldout-trigger.jsonl --key-hints 6672 --stop-on-429
python -m evaluations.owner_memory triggers --stage heldout --private $P --heldout-dir $H --arms baseline=heldout-v3-revert.jsonl,candidate=heldout-trigger.jsonl
```

`heldout-trigger.jsonl` is 986c9ef4's `replay.py` run with its `SNAP` pointed at `corpus-snapshot-triggers` and
`TOFU_DATA_DIR` at that copy's `data`.

## 9. Fourth measurement: 126 cases with power, and the "retrieved but not acted on" gap (2026-11-17)

Code: this change (`evaluations/owner_memory/scale.py`, stages in `evaluations/owner_memory/README.md`), merged with
`eval/opencode-glm53-tb21`. Private evidence: `.eval-runs/memory-replay/private/corpus-v2/`, `scale/` (`pool.json`,
`links.json`, `rubrics.json`, `points.json`, `cases.json`, `interference.json`, `PREREGISTRATION-fix.txt`), `runs/scale-v1/`
and `runs/scale-fix-unrelated/`. `lib/` is unchanged.

**Plain answer: "better with use" is still not shown in production, and now we know why with statistical power.**

- A lesson that is **in front of the model** at the defect point makes it avoid the defect **+17.4 pp** more often
  [+10.9, +24.6]. When it is also restated as a one-line check right before the next action, avoidance rises from
  17.3% (no lesson) to 56.0%.
- But production retrieval puts the right lesson there in only **25 of 126** cases. So the store with the lesson,
  under production selection, changes avoidance by **+1.8 pp [−1.5, +5.3]**. This interval rules out a 10-point
  production effect (power ≈ 1.0).
- The check that works cannot be delivered as measured. Sent the way the composer can deliver it (a superseding copy
  after every tool round), it gives +0.3 pp [−4.6, +5.5]. **It is not shipped.**

### 9.1 Case set (frozen before any replay)

- **Corpus.** A new frozen copy of every store, with the shipped triggers (`corpus-v2`, 2,621 files, digest `b346bc42…`,
  2026-11-17T04:09Z). Compared with `corpus-triggers`, 2,557 files are identical and 15 are new. 22 differ, but only in
  the 910C trigger lines the earlier backfill could not write. No lesson created in the window was edited later, so
  slicing by `created` is exact, as in §6.
- **Pool.** Every Worker attempt the live store still holds with recorded requests: **401 Turns** in the tofu,
  parallel and knowledge projects (11-15 → 11-17). cpt_data, hope-mcp and llm-mcp have lesson stores but no live Turns.
  Turns before 11-15 return 404 (old host), so the pool cannot grow further.
- **Two kinds of case.**
  - **own:** the lesson this Turn wrote (provenance), as in §6–8.
  - **recurrence:** a lesson written earlier in another conversation whose defect occurred again in this Turn.
    - The candidate lessons for each Turn are the ones production selection reaches through their triggers (turn
      start, and the failure-time candidate at every failed tool round), plus the `create_memory` repeat check for
      the lessons the Turn wrote: 3,956 candidates in total.
    - A fixed model (`claude-opus-5.5`, `LINK_SYSTEM`) read the Turn's numbered tool steps and decided which defects
      occurred: **130 links**. 17 Turns' link calls were refused by the gateway (910C content).
- **Rubric and defect point.** Each case got the frozen §6 rubric prompt and the frozen §7 point prompt. Of 169
  candidates:
  - 40 have no locatable defect step;
  - 1 is not a defect;
  - 1 rubric was refused.
- **Frozen cases: 127** (28 own, 99 recurrence) over 83 Turns, 65 distinct lessons, in all three project stores plus
  the global store (`cases.json`, digest in `runs/scale-v1/manifest.json`). 126 cases have judged rows in every arm.
- **Recurrence link quality.** I read 10 seeded recurrence cases. 8 are clearly the same mechanism; 2 are loose (a
  lint-gate lesson matched to a venv choice, an export-gate lesson to an unfinished test). Typical clear repeats:
  - the read-before-edit gate refusing `edit_file`;
  - `run_command` folding output lines;
  - recursive `grep` on the FUSE mount;
  - a heredoc commit message lost to the git rewriter.

### 9.2 Arms and judging

- **Replay point.** Every case replays from the Inspector-recorded request of the round that issued its defective step
  (§7.3). The turn-start carrier is recomputed by production selection over a time slice:
  - `without`: the store as the Turn found it (own: just before the lesson), minus every lesson linked to that Turn's
    defects;
  - `production`: the same store plus the case lesson;
  - `shown`: as `production`, with the case lesson placed in the first carrier slot when selection did not pick it;
  - `shown_fix`, `production_fix`, `shown_every`: the candidates of §9.4.
- **Byte-identical arms share rows.** Where production does not select the lesson, `production` equals `without`.
- **Trials and judge.** 6 trials per arm. Blind judging with the §7 mid-task judge: lesson names masked, no arm, seeded
  order. 2,940 of 3,048 rows were judged; the rest were empty gateway refusals.
- **Variance and power.** Intervals are a two-stage cluster bootstrap (10,000 resamples; clusters = source Turns).
  "Power" is two-sided 5% power for a true 10-point effect, from the interval's implied standard error.

### 9.3 Results

| Comparison (avoided) | Cases | Rates | Difference [95% CI] | Up / down | Power for 10 pp |
|---|---:|---|---|---|---:|
| `without` → `production` (production as it runs) | 126 | 17.3% → 19.3% | **+1.8 pp [−1.5, +5.3]** | 11 / 5 | 1.00 |
| `without` → `shown` (the lesson in front of the model) | 126 | 17.3% → 34.8% | **+17.4 pp [+10.9, +24.6]** | 50 / 14 | 0.81 |
| `production` → `shown` (what retrieval misses) | 126 | 19.3% → 34.8% | +15.6 pp [+9.4, +22.6] | 39 / 10 | 0.84 |
| own cases: `without` → `shown` | 27 | 30.1% → 62.2% | +32.1 pp [+14.1, +50.6] | 15 / 4 | 0.19 |
| recurrence cases: `without` → `shown` | 99 | 14.0% → 27.5% | +13.5 pp [+7.3, +20.4] | 35 / 10 | 0.85 |
| recurrence cases: `without` → `production` | 99 | 14.0% → 14.3% | +0.3 pp [−2.5, +3.0] | 3 / 1 | 1.00 |

- **Hit rate.** `without` 19.6%, `production` 17.6%, `shown` 10.5%. `without` → `shown`: −8.9 pp [−13.9, −4.7].
- **Retrieval is now the first bottleneck.**
  - Production renders the case lesson in 25 of 126 cases (16 own, 9 recurrence).
  - Where it does, `without` → `production` is +10.0 pp [−2.0, +23.9] (21 Turns).
  - In the other 101 cases the two requests are identical by construction.
- **Later requests meeting old defects** (the 99 recurrence cases) are the "better with use" question itself. The
  store already held a lesson about the defect, and production showed it in 9 of 99 Turns: +0.3 pp.

### 9.4 Why a lesson in front of the model still fails, and the fix tested

- **Diagnosis (blind labels, `runs/scale-v1/gap.json`).** The labeller (`claude-opus-5.5`, `GAP_SYSTEM`, no arm) saw:
  - the request and recent steps;
  - the defect;
  - the lesson that was in context;
  - the response.

  It classified every first-run row (2 trials) where the case lesson was rendered and the defect was not avoided:
  153 rows over 86 cases.

  | Outcome | Rows | ignored | misapplied | too vague | wrong | not yet (reasonable earlier step) | Mentions the lesson |
  |---|---:|---:|---:|---:|---:|---:|---:|
  | hit (walked into the defect) | 25 | **17** | 5 | 3 | 0 | — | 3 |
  | not reached | 128 | 5 | 1 | 0 | 0 | 122 | 5 |

  - **The dominant failure is "ignored".** In 17 of 25 hits, the lesson applied and said what to do, and the response
    neither mentioned it nor followed it. It mentioned the lesson in only 3 of 25.
  - No lesson was judged wrong.
  - Hand check, 12 seeded hit rows (`gap-handlabels.json`): 9 of 12 agree. 2 of the 3 disagreements are
    ignored-vs-misapplied/too-vague borderlines.
  - The lesson sits in the Turn's opening message, a median of 37 tool steps before the defective action (10th–90th
    percentile 8–123).
- **Structural fix tested (no knobs).** `trailing_check` restates the lessons the turn-start carrier already rendered:
  same selection, same order, nothing added. It writes them as one-line imperative checks, `When <trigger>:
  <description>`, built from each lesson's own fields, in one carrier right before the next action. The rule was
  pre-registered before any fix arm was built (`PREREGISTRATION-fix.txt`, digest `7d225bac…`). Amendment 1 raised the
  trials from 2 to 6 and the unrelated tasks from 40 to 80, before any fix row was judged.

  | Ship-rule condition | Measured | Holds? |
  |---|---|---|
  | 1. Avoidance up: `shown` → `shown_fix` CI lower bound > 0 | 34.8% → 56.0%, **+21.3 pp [+14.3, +28.3]**, 61 up / 13 down; hit −4.5 pp [−8.4, −1.0] | yes |
  | 2. Relevance not down: identical selection in every arm pair | 152 case and 80 unrelated arm pairs inject identical lesson sets (dry run) | yes |
  | 3. Unrelated-task quality not down: on-task (fix − production) CI lower bound > −0.10 | 80 unrelated Turns from their last round, 2 trials, blind mid-task on-task judge: 99.7% vs 99.7%, 0.0 [−0.01, +0.01] | yes |

  Where production retrieves the lesson (25 cases), `production` → `production_fix` is +27.3 pp [+11.1, +42.0]. With
  the check, production avoids the defect 69.3% of the time against 32.0% without the lesson.
- **Why it is still not shipped.**
  - Production cannot deliver "exactly one check, right before the action". The composer keeps sent history immutable
    and reuses an unchanged block, so the only deliverable form re-sends a superseding copy after each tool round.
    The check would otherwise sit wherever it was first appended.
  - Amendment 2 pre-registered that deliverable form, `trailing_check_every`, with the same rule, before it was built.
    Earlier copies were kept and compaction was not modelled: the most diluted case.

  | Deliverable form | Measured | Holds? |
  |---|---|---|
  | 1. `shown` → `shown_every` | 34.8% → 33.3%, **+0.3 pp [−4.6, +5.5]**, 26 up / 30 down | **no** |
  | 2. Identical selection | yes | yes |
  | 3. Unrelated on-task | 100% vs 99.7%, +0.4 pp [0.0, +1.5] | yes |

  - Repeating the same check after every round erases its effect entirely: `shown_fix` → `shown_every` −21.0 pp
    [−28.1, −13.9]. The model treats a line that recurs every round as background, which is the same "ignored"
    failure the diagnosis found.
- **Decision: not shipped.** The measured form passes but cannot be built in the composer as measured. The buildable
  form fails condition 1.
- **What the result does establish.**
  - Placing the lesson next to the action, once, is what works. Its value scales with whether retrieval reached the
    lesson at all.
  - The natural deliverable is a check emitted once, at the tool call its trigger matches: the brief's "explicit
    pre-action check". It needs a match rule between the pending call and the trigger. Every rule tried so far (§7.2)
    either needed a threshold or brought mostly noise. It is the next experiment, not part of this change.
- **Alternatives.** Rewording the opening carrier as imperatives was not run separately: 17 of 25 hits ignored a
  lesson that already stated its action, so the wording is not the binding problem. Rewriting lessons with a model
  ("authoring fix") was not tried: no lesson was judged wrong, and only 3 of 25 were too vague.

### 9.5 Limits and run notes

- The judge is the same model family as the replayed agent. Agreement with my blind labels was 29/30 in §6 and 12/16
  in §7. The gap labeller agreed 9/12.
- 9 own cases have only 6–12 rows per arm after refusals. The own subset (27 cases) has power 0.19 for 10 points; the
  pooled 126 cases carry the conclusions.
- **Harness defects found.**
  - The Inspector cuts long tool-call arguments to a non-JSON prefix, which the provider wire refuses. They now become
    a JSON note (`midtask._restore_elided_arguments`).
  - A forced third carrier entry could fall outside the renderer's byte budget. A forced lesson now takes the first
    slot, and arms record `targetRendered`.
  - A server restart during a judge pass made the credential reveal answer HTTP 503. The process exited; rerunning the
    same command resumed it with no lost rows. This is unchanged (`transport.py` treats it like any provider error at
    process start).
- **Cost.** Case replays $3,001.75 (3,048 rows), judge $109.61. Unrelated arm $553.13 + $14.79. Links, rubrics,
  points and gap labels (≈ 1,000 short calls) are not itemized.

### 9.6 Reproduce

```bash
P=<canonical>/.eval-runs/memory-replay/private
python -m evaluations.owner_memory scale --private $P --stage cases   # frozen; prints the counts
python -m evaluations.owner_memory scale --private $P --key-hints 6672 --stop-on-429 --jobs 16 --run-name <name> \
  --stage run --trials 6 --fix trailing_check,trailing_check_every
python -m evaluations.owner_memory scale --private $P --key-hints 6672 --stop-on-429 --jobs 16 --run-name <name> --stage judge
python -m evaluations.owner_memory scale --private $P --run-name <name> --stage report
python -m evaluations.owner_memory scale --private $P --key-hints 6672 --stop-on-429 --jobs 8 --run-name <other> \
  --stage interference --fix trailing_check,trailing_check_every
```
