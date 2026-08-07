# STORAGE_REDESIGN.md — 存储架构重设计（PG 退役 · SQLite 单引擎 · 写放大根治）

> 状态：owner 已裁决（2026-08-07）。本文是唯一的执行依据。
> 裁决原文要点：①下线 PostgreSQL 批准，但 row-equal 逐表验证 + 跨重启验证 +
> owner 签字前 `data/pgdata` 只归档不删除；②`data/pg_backups` 343GB 只保留最新 1 份；
> ③events 类数据批准 ~0.3s 落盘窗口（它是回放缓存，内存 SSE 流才是真源）。

---

## 1. 为什么重做（负担清单，全部实证）

| 负担 | 证据 |
|---|---|
| FUSE 上的 live pgdata 25GB | `du -sh data/pgdata`（2026-08-07） |
| PG 备份/归档合计 **~411GB**（pg_backups 343GB/8 份 + pg_backup.sql 67GB + chatui_pre_rename_backup.sql 5.7GB + pg_emergency_backup 396MB + *.dump 738MB + claude_dialogue 残留 543MB） | du 实测（2026-08-07） |
| 只为「养 PG + PG↔SQLite 双语桥」的代码 ~8-9k 行 | `_sql_translate.py` 324 + `_wrappers.py` 456 + `_pg_seed.py` 619（已撤回）+ `_schema_pg/`+`_schema_sqlite/`+`_core_schema/`+`_pg_ownership/`+`_pg_backup/` 共 6,092 |
| PG-on-FUSE 事故史 | IP 漂移裂脑、postmaster 假死、跨机接管心跳（`.tofu/memories/pg-*`）、`pg_subtrans` 读损坏 |
| **在服生产慢查询（今天，2026-08-07 13:51-14:20，logs/error.log）** | `DELETE FROM task_events`（TTL 清理）**2.0-12.7 秒**；`UPDATE conversations SET messages=…`（整段 JSONB 重写）**10.5-11.9 秒** |
| 静默回落分叉隐患（现役） | `data/tofu.db` 6GB、mtime 停在 2026-07-09——它是指定 SQLite 回落库（`_core.py:319`）。PG 自举失败时 fail-loud 守卫只**警告仍照常服务**（`_core.py:2495-2536`）：重启即可能把新写灌进过期一个月的分叉库 |

**根因定性**：不是「存储技术不够强」，是武器类型选错——客户端/服务器式数据库（常驻
postmaster、共享内存、跨机认主）架在网络文件系统上，伴生病占了 ~9k 行护航代码。

**owner 永久约束（不可谈判）**：数据库与一切持久化数据永不置于项目目录之外
（/tmp 会被清、且有用户无权限；项目路径权限最充足）。性能问题只能在项目目录内解决。

## 2. 实测基线（本机 BeeGFS FUSE，项目目录内，2026-08-07）

| 测量 | 结果 | 含义 |
|---|---|---|
| 单次 fsync（4KB 写） | **0.9 ms** | 持久化一次不贵 |
| SQLite WAL 逐行提交 | **1.19 ms/次** | 热路径够用 |
| SQLite DELETE 日志逐行提交 | 12.75 ms/次 | 元数据操作多——FUSE 按**操作次数**收税 |
| WAL 500 行一次事务提交 | 29.3 ms（0.06 ms/行） | **攒批 = 20 倍收益** |
| SQLite 版本 | 3.53.1（Python 标准库自带） | 零新增依赖 |
| 独立复测（第二套基准，同日） | fsync p50 0.23 ms；WAL 0.79 ms vs DELETE 9 ms；单事务 2.3-3 万行/s；**WAL 关闭后无 -wal/-shm 残留**（本挂载 WAL 安全）；逐块 fsync 吞吐掉 37 倍 | 双测互证，结论稳定 |

结论：**FUSE 按 IO 次数收费而非字节数**。正确策略是把「很多次小操作」收敛成
「很少次大操作」，不是搬数据（也不许搬）。

## 3. 目标架构

一个引擎、一个文件、两条写入车道、按丢失容忍度付钱。

```
data/
  tofu.db            ← 唯一权威库（SQLite，WAL，synchronous=NORMAL，mmap 关闭）
  .tofu_db_owner     ← 跨机双开哨兵（见 §5）
  db_snapshots/      ← VACUUM INTO 每日快照，保留最近 N 份（替代 343GB dump 囤）
  config/*.json      ← 保持原子 JSON 不动（现状已正确）
  （blobs 文件继续只进文件系统，DB 只存元数据——现状已正确）
```

| 决策 | 理由 |
|---|---|
| **SQLite 单引擎，PostgreSQL 整体退役** | 审计确认零阻碍：运行时默认本来就是 SQLite（`_core.py:216`）；FTS5 替 tsvector（`search_index.py` 已有 `conversations_fts`）；单进程线程池模型不需要 PG 级并发；最重 SQL 是 TTL 批量 DELETE，双方言都支持。删除 ~9k 行护航代码，裂脑/假死/认主/端口管理整族消亡 |
| **单一库文件，不拆 events.db** | TTL 清理 SQL 要 JOIN `task_events`×`task_results`（见在服慢查询），拆库就得 ATTACH；攒批后 sync=NORMAL 的 fsync 成本已可忽略，第二文件买不到任何东西 |
| **两条写入车道** | ①**同步车道**：珍贵数据（会话/用户/账单/配置）语义不变，调用方同步等 commit；②**攒批车道**：events 类（task_events / poll_log / rate_limit_events）进内存队列，单写者线程按「满 300ms 或满 500 行」落盘——owner 批准的 0.3s 窗口 |
| **单写者** | 所有写汇聚一条连接，进程内「database is locked」整族绝迹；busy_timeout 可从 30s 降到亚秒。P1 先为 events 车道建写者线程，P3 拆桥后完成全量收编 |
| **消息行存转正** | `conversation_messages`（现镜像表）升为主存储：每消息一行，追加/单行更新；`conversations` 退化为元数据行（id/title/rev/msg_count/search_text…）。**整段 JSONB 全量重写（在服 10-12 秒）直接消亡**。CAS rev 语义保留在元数据行上 |
| **task_events 攒批 + 既有 TTL** | 每增量一行的提交（`event_log.py:168` commit=True）进攒批车道；6h/30d TTL 保留。SSE 实时推送走内存，不依赖落盘时序 |
| **备份 = VACUUM INTO 日快照** | 单文件、一致、可直接改名恢复；体积比 pg_dumpall 小约 3 个数量级 |

### 被否决的候选
- **LMDB / RocksDB**：C 扩展新依赖；mmap 走 FUSE 是慢车道；无 SQL。
- **DuckDB**：分析型引擎，不匹配 OLTP 负载。
- **保留 PG**：正是要卸的担子（§1）。
- **双文件拆分**：JOIN 需求 + ATTACH 复杂度，攒批后无收益（见上）。

## 4. 写放大根治（P1，引擎无关，PG 在服期间立即受益）

当前每轮对话的提交放大（实证 + 读码）：
1. `task_events`：**每流式增量 1 次提交**（一轮几十~几百次）——最大写入源。
2. `conversations`：每轮 ≥1 次整段 JSONB CAS 重写（+rev 触发器 +FTS 同步）——在服实测 10-12 秒。
3. `conversation_messages`：delete+re-upsert 镜像（TOFU_MESSAGES_ROWS 开关下）。
4. `task_results`：起止 + 状态更新，数次。

改造后每轮：events 攒批车道 ≤(时长/300ms) 次且按行数提前冲刷；消息行追加 K 行
（K=本轮新增消息数，可并入同一事务）；元数据行 1 次 CAS；task_results 2 次。

**验收（owner 钉死）**：在 `_core.py` 提交点加计数器，用合成一轮对话的探针实测
「每轮提交次数」前后对比，**降幅 ≥10 倍**；报告随 P1 交付。

### P1.1 落地记录（2026-08-07，events 车道）

- `event_log.py` 写后批车道：单写者线程 + 票号回执；终帧（done/error/aborted/
  interrupted）同步等回执（durable-before-visible 对一切重连锚点成立）；
  `TOFU_EVENT_BATCH=0` 退回旧逐行提交；队列满降级同步写不丢行。
- **组提交窗口**：首个 get 返回后按窗口聚合至 300ms/500 行——首版「拿到即冲」
  在稀疏流下退化为逐行提交（实测 30 事件 26 次提交），窗口化后同一突发一次提交。
- **车道感知读**：`read_events` 与 Request Inspector 的 `_read_events_uncached`
  都会并生产者影子（`pending_event_rows`），且**影子快照先于 DB 读**——调试实证：
  双读跨越写者的 commit→影子注销间隙会让行两边都不可见（fold 落后一个突发）。
- 实测验收：120 次追加 ≤2 次提交（**≥60 倍**，目标 ≥10 倍）；邻接环 24 套件
  196 针全绿；collect-only 16,322 零错。

## 5. 跨机双开哨兵（~100 行，替代 PG 那 1,100 行认主机制）

风险面：两台机器挂载同一项目目录、同时以写模式打开同一 SQLite 文件——BeeGFS 的
POSIX 锁跨机语义不能赌。机制（语义镜像已验证的 PG 心跳方案）：

- 写模式打开前检查 `data/.tofu_db_owner`（JSON：host/pid/ts，owner 每 30s 刷新）。
- 无文件或 mtime 超 TTL（120s）→ 接管（原子 os.replace 写入自己）。
- 他机持有且新鲜 → **拒绝写模式**（fail-loud：critical 日志 + 启动拒绝或只读降级），
  绝不静默双写。
- 同机多进程：WAL 正常承接（本机 BeeGFS fcntl 可用，基准实测 WAL 正常）。

## 6. 迁移路线（每步可逆）

- **P0 减负**：删除清单先经 owner 过目——pg_backups 留最新 1 份删 7 份；死残留
  （`chat.db` 0B / `chatui.db` 16MB / `*.corrupted_*` 314MB / 恢复脚本与 dump）；
  已撤回的播种机代码（`_pg_seed.py` 619 行 + 相关测试/文档横幅）。
  `data/tofu.db`（6GB 过期回落库）**改名归档**为 `tofu.db.pre-pg-archive`，
  P3 签字后删除。
- **P1 写放大根治**（§4）：攒批车道 + 消息行存转正。引擎无关，先在 PG 在服期间上线。
- **P2 导出器 + 灰度**：PG→SQLite 逐表导出（schema 双方言本就同源 `_core_schema`），
  **逐表 row-count + 校验和相等才放行**；导出为一个全新库文件，旧 tofu.db 归档位不动；
  `TOFU_DB_BACKEND=sqlite` 灰度，PG 转只读观察。回滚 = 摘掉 env 变量（PG 全程在位）。
- **P3 下线拆桥**（owner 签字后）：pgdata 归档保留策略另行确认；删除方言桥
  （`_sql_translate`）、双建表（`_schema_pg`）、双封装（`_wrappers` PG 半）、
  认主机制（`_pg_ownership`）、PG 备份（`_pg_backup`）、TOAST 自愈等 ~9k 行；
  `messages` JSONB 列退役；单写者全量收编；回落分叉隐患随第二引擎消亡而归零。

## 7. 数据类分层（丢失容忍度）

| 类 | 内容 | 车道/介质 | 容忍 |
|---|---|---|---|
| 珍贵 | conversations 元数据、消息行、users、billing_*、chat_artifacts、message_queue、config JSON | 同步车道 / 原子文件 | 零丢失 |
| 半珍贵 | task_results、swarm/orchestration、project_*、transcript_archive | 同步车道 | 事故审计级 |
| 可再生 | task_events、poll 日志、rate_limit、log_aggregates | 攒批车道（0.3s 窗口） | 可从对话/日志重建 |
| 纯缓存 | pricing_cache、daily_cost_cache、paper_* | 同步车道（量小） | 可重建 |
| 文件 | PDF/播客/图片 blobs | 文件系统，DB 只存元数据 | 珍贵（原样保留） |

## 8. 验收口径（owner 2026-08-07 钉死）

1. P1 前后实测「每轮对话提交次数」对比，降幅 ≥10 倍。
2. 全量测试绿 + `--collect-only` 零错（PYTEST_DISABLE_PLUGIN_AUTOLOAD=1）。
3. P2 迁移出具逐表 row-equal（行数 + 校验和）报告。
4. owner 签字前 `data/pgdata` 只归档不删除；P0 删除清单先过目。

## 9. 对既有守护的关系

- `no-external-paths-for-db` 记忆/裁定：**本方案完全遵守**——所有数据留在项目目录。
- PG 心跳/认主记忆（pg-cross-host-heartbeat-takeover 等）：P3 随机制删除整体退役，
  其语义由 §5 哨兵继承（同一个 TTL/refresh 数字，同一套接管判定）。
- `TOFU_DB_STRICT_PG` / `TOFU_REQUIRE_PG`：P3 删除；P2 灰度期间语义不变。
