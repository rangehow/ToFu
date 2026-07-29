<!-- pt_a4c9d33e CLOSED 2026-07-27: board flipped to done from a dispatch that DID carry project_board_* tools. The implementation was in HEAD (fbda6d98 + d12cd17f, CAS 5/5) the whole time — only the flip was missing, because the closing tool was absent from the autonomous toolset. That silent dead end is now a visible `tool_not_available` envelope (9abdcb22, epic pt_88791cb08cb2495c), so a task blocked this way reports the reason instead of settling as a success. -->

### 2026-07-29(续·幽灵 Stop 键) — 自查抓出**我自己守卫里的散文承诺**:docstring 写着「must not resurrect a dot」而断言里根本没有这一半;它保护的正是本 objective 的**反面**——「看得见却没在生成」(commit `48aa84e4`,test-only 零产品码;套件 **10/10**,**NEUTER-F 精确咬 1 条**,相邻环 **56/56**)

- **★ 这不是「测试不严谨」,它守的是我自己引进的风险面。** VU 可见性四批里**每一个修复都在让会话读作忙**,所以镜像风险是我造的:VU 轮次若在 push 断线期间结束,客户端最后的认知停在「carrier 在跑」⇒ **停止键永远亮着、什么都不生成**。与 owner 最初报的「在生成却看不见」是**同一种卡死状态,只是反过来**。
- **★ 空守卫的形态(与本条线上一次的 docstring 陷阱同族,换了个方向):** `test_poll_fallback_idle_projection_attaches_nothing` 的 docstring 明写「**and must not resurrect a dot**」,而断言只有 `connectToTask == []`;**harness 甚至没有暴露忙态**,那半句是纯散文。上一次是「文本扫描被自己的 docstring 满足」(守卫读到了不该算数的东西),这次是「docstring 承诺了断言里没有的那一半」(守卫少读了该算数的东西)——**同一家族的两个方向,我在同一条工作线上各犯一次。**
- **判据(已并入记忆家族):写在守卫里的承诺会被后人当成已验证。** 复核守卫时要**逐条把 docstring 的每句「must X」勾到对应的 assert**;没有对应断言的,要么补断言,要么删掉那句话——留着比没有更糟,因为它让人不再去看断言列表。
- **★ 实测 → 断言,顺序不能反(本轮最关键的一步):** 先驱动**真** reducer 量这条路:carrier 到达 `busy=true / carrier='t'` → 轮询投影不含该会话 `busy=false / carrier=null`。**行为本来就是对的,缺的只是守卫**,所以钉的是**已验证的事实**;顺序若反过来就成了「写个断言然后祈祷它成立」。
- **落点(不手写等价判断):** 把**真** `computeConvBusy` 提升进 harness——手写的忙态判断可能与 shipped 谓词分歧而**放行回归**;先验忙态用**真** `applyRunningTaskIdsFrame` 经 `__seedWire` 播种,不手搓集合。三层(wire → reducer → 观察谓词)全部走生产路径。
- **NEUTER-F:** 摘掉 `applyConvStateSnapshot` 的 CLEAR 分支 → **恰好 1 条红**,报 `{'busy': True, 'carrier': 'da0717c8'}` —— 幽灵 Stop 键当场现形;逐字节还原。
- **★ 一处过程自纠:** 首版把 `computeConvBusy` 漏出提升列表 ⇒ **6 条 ReferenceError 红**,而看起来像产品坏了。**判据:harness 报错要先分清「被测代码错」与「我没把被测代码装进来」**——前者改产品,后者改 harness,方向相反。
- **验收边界:** test-only,**无产品行为变更**;前四批仍需重启 + bundle 重建才到用户面前。

### 2026-07-29(续·派单滞留根修) — 「排队中却什么都不生成」的另一半:**epic 的 30 分钟租约一过期,那条 kickoff 就同时对两条路不可见**;实测扫描集 **0 vs 4**——board 上每一条真滞留都是旧扫描看不见的(`pt_b46ad973a7ba4621` DONE;commit `6d5a7e56`,2 文件;新增 3 条(2 条失败先行),**NEUTER 咬**;套件 **9/9**,相邻环 **68/68**;活体扫描集实测)

- **★ 这是 VU 可见性那三批的**第二个症状**,不是无关缺陷:** owner 最初报的三张截图里,第二张就是「排队中,但什么都没在生成」。前三批修的是「在生成却看不见」,这一批修的是「真的没在生成」。同一个 objective 的两半。
- **★ 根因是两扇门同时关在同一行上,而每扇门单独看都合理:**
  - `_effective_status` 在**读取时**回收过期软租约 ⇒ 租约一过,epic 读作 `open`、`owner_conv_id` 被抹空;而 reconcile 扫描集是「拥有 **claimed** epic 的会话」⇒ **该会话整个从扫描集里消失**;
  - 正常派单循环**确实**会重新选中这个 open epic,但 `_epic_already_queued` 看到那条还在的 kickoff,拒绝再入队。
  
  **两条路各自都在正确地执行自己的规则,而交集是空的** —— 这就是为什么它不是「慢」而是「永远」。
- **★ 活体实测把「理论缺陷」变成「board 上正在发生的事」(本轮最值钱的一步):** 对真实 board 跑两种扫描集——
  | 扫描集 | 结果 |
  |---|---|
  | claimed-only(旧) | **0 个会话** |
  | 队列行驱动(新) | **4 个会话** |
  | 新增可达 | **4 个,即全部** |
  
  也就是说**旧扫描在这块 board 上此刻的召回率是 0**。另跑一次无关项目路径 → 空集,跨项目隔离成立。
- **落点(锚在耐久事实上,不是锚在会过期的那个):** `_convs_holding_undrained_kickoffs` = board 的 claimed owner **∪** 「持有 workflow_step 队列行且 config.projectPath 指向本项目」的会话。队列行是**耐久**的,租约是**会过期**的——判据要挂在前者上。探针失败时退回旧的 board 派生集(即老行为),绝不把异常抛进 sweep。
- **★ 配套守卫钉的是「加宽扫描集」的反向风险,不只是正向修复:** 一个读作 `open` 的 epic **同时**是正常派单循环的合法目标 ⇒ 天真的加宽会让同一次 sweep 既排空旧 kickoff、又入队一条新的 = **同一个 epic 两个计费任务**。故补 `test_expired_lease_strand_does_not_double_dispatch`,断言恰好 1 次 spawn 且队列**没有被重新填上**。
- **既有守卫为何看不见:** `test_sweep_reconciles_stranded_kickoff` 覆盖的是 **claimed** 那一种滞留,它在租约有效期内成立;**过期这一格从来没有夹具**。新用例显式先断言前置条件(`status=='open'` 且 `owner_conv_id` 为空)——把「为什么旧扫描找不到它」写成断言,而不是留给读者推断。
- **验收边界:** 纯后端,**运行中进程不带**,需重启后 30s heartbeat 才按新扫描集跑;本批未回填历史滞留行(重启后首次 sweep 会自然排空它们)。

### 2026-07-29(续·P0 费率票逐条复验关闭) — 实现早在 `818b3f8`+`d4eeb25`+`da64614` 落地,本轮是**按票面验收标准逐条实测复核后关票**(零新产品码);**一处刻意偏离票面并说明理由**:票面主张 `110022` 应归 fund,实测它是交易所可转债,按 fund 收 1.5% 赎回费对债券是 **60 倍错**(`pt_86e9ea617e1f47e8` DONE)

- **逐条复验(全部在干净 committed worktree 上跑):**

  | 票面验收项 | 实测结果 |
  |---|---|
  | grep 不到硬编码费率字面量 | **offenders: 0**(6 文件剥注释扫 `0.0015/0.005/0.015/short_sell_penalty/buy_fee_rate/sell_fee_rate`) |
  | `metrics.final_value` == DB `total_pnl` | **9 次跨标的跨种子全部逐分一致** |
  | 1 年期真实 10% 必须报 ≈10% | **+10.00% → 年化 +10.67%**(原 61.67%);-10% → **-10.61%**(原 -41.2%) |
  | 零阿尔法 3000 次纯机制损耗 | 宽基 **-1.89%→+0.04%**、蓝筹 **-3.76%→-0.35%**、成长 **-5.50%→-0.24%**,均优于或接近票面目标(-0.24/-0.49/-1.21) |
  | ①prompt 与账本同源 | system prompt 已无写死费率(`万2.5`/`0.05%印花税` 均为 False),两处 prompt 均由 `_describe_fees(config.fee_book)` 渲染 |
  | 悬空 import 变响 | `meta_strategy.py:329/346` 与 `intel_backtest.py:77` 均已分离 `ModuleNotFoundError` |

- **★ 一处刻意偏离票面(必须记,否则下一个人会以为是漏改):** 票面③写「`110022`→bond(**应为 fund**)」。实测 `110022` 是**交易所可转债**(11xxxx 段),在**场内按佣金交易**,不是场外基金。若按票面归 fund,三日卖出会被收 **1.5% 赎回费**,而真实是 0.025% —— **60 倍错**。故保持 bond 归类,并给 bond 单独配了「佣金 only、无印花税」的交易所档;真正的场外基金(`003003`)仍走分档赎回费且标 `confidence=default`。**判据:票面给的「正确答案」也要实测,它可能只是把一个错的方向换成另一个错的方向。**
- **`161725` 判 etf 亦非缺陷:** 它是**场内 LOF**,确实按场内佣金交易,归 etf 正确。
- **票面「禁止混入」三条已严格遵守:** 同根 K 线执行(`cfb6bd8`)、止损自适应(`c1ac232`)、`max_positions` 对齐(`c1ac232`)均在**独立提交**中,未混入本票范围。
- **验收边界:** 本轮**零新产品码**,纯复验 + 关票。干净 committed worktree 全量 **257 passed**,余 2 条既有红(`test_simulator_migration_parity`,缺 `server.py`,宿主路径依赖,与本票无关)。

### 2026-07-29(续·工具注解与消费侧拼写) — owner 指出我把「25 个工具零注解」当可选优化放走了,而它**正在生效地**让 16 个读操作串行+逐个弹审批;追下去发现**真缺陷在消费侧不在供给侧**:Tofu 的提取器是 v1-only,v2 上会让全机队每个 server 的注解静默归零(commit chatui `d5b010cb`;overleaf 0.3.0 注解;守卫 **11 + 48**,**NEUTER×4 全咬**;相邻环 **154 passed**;wheel 级验收)

- **★ owner 的判据比我原先写的强一档,而且可实测:** 我第一轮只写了「一个注解都没声明」并归进「不并入本批」。实测 `read_only_hint` **不是装饰字段**——唯一消费者 `lib/tasks_pkg/tool_dispatch/_flags.py:130` 拿它做**串行/并行分区 + 写审批闸**,注释明写「默认把每个 MCP 工具当写工具(serial + approval-eligible),只有 `readOnlyHint` 显式 True 才留在并行池」。而 `annotations=None → read_only_hint=False`,overleaf-mcp **25 个工具 0 个带 annotations** ⇒ 读一篇论文的每个动作都被当写操作。
- **★ 而 owner 给的 16 条读清单里有 7 条实测不是读,这正是他自己警告的危险方向:** 规范原文是「the tool does not modify its **ENVIRONMENT**」——不是「不改远端项目」。逐个读 dispatch 分支:
  - `compile_project` / `download_log` / `get_page_count` / `locate_in_pdf` / `section_page_map` **都真的在 Overleaf 上跑一次编译**(`layout._compile_with_editor_id` 镜像 `compile.compile_project`)。**「它返回信息」不等于它是读**——判据是它为了拿到信息做了什么。
  - `download_pdf` / `download_source_zip` / `download_source` **写用户文件系统**到调用方给的路径,且 `download_source(overwrite=True)` 实测会**清掉非空目录**(`compile.py:235`)。本地磁盘也是 environment。
  
  这 7 条若标成 read-only,会**同时**进并行池并跳过审批——`delete_file` 那一类的隔壁。**判据:票面给的分类也要逐条按实现复核,「听起来像读」是最不可靠的信号。**
- **★ 我自己的自动化启发式两个方向都错,已弃用:** 我写了个「不 compile 且无 output_path ⇒ 读」的脚本,它把 **`delete_file` 判成 READ-ONLY**、把 `list_projects` 判成 WRITE。分类只能逐工具按行为定。最终 **9 读 / 16 写**,写侧再按规范补 `destructiveHint`(仅在 `readOnlyHint=false` 时有意义)。
- **★ 真正的根缺陷在消费侧,而它比供给侧的空缺严重得多:** `_extract_read_only_hint` 只读一个属性名 `readOnlyHint`。wire 名恒为 camelCase 所以看着安全,但**Python 属性名不是**:
  | SDK | `getattr(ann,'readOnlyHint')` | `getattr(ann,'read_only_hint')` |
  |---|---|---|
  | v1 | **True** | ABSENT |
  | v2 | ABSENT | **True** |
  
  v2 把模型字段全转 snake_case、camelCase 只留作序列化别名。于是单拼写查找在 v2 上**不报错,只是对每个 server 的每个工具都返回 False**;而 False 同时也是「这个 server 没声明注解」的诚实答案 ⇒ **坏状态与正常状态不可区分**。净效果:升级那天全机队所有 read-only 工具悄悄退出并行池、开始逐个要审批,日志里什么都没有。**与 `434bde89` 修的 `McpError`→`MCPError` 完全同型:判据锚在上游拥有的名字上,改名时不会 fail-loud。**
- **端到端实测(这一步才是验收):** 在真实 mcp==2.0.0 venv 里用 **Tofu 自己的提取器**跑 overleaf-mcp 的 25 个工具——修前 read-only **0/9**,修后 **9/9**。我中途写过一个「复刻提取器」的替身,它报 9 而真提取器报 0;**替身正好掩盖了这个缺陷**,已弃用改为 import 真函数。
- **落点:** 供给侧一张 `_READ_ONLY_TOOLS` / `_WRITE_TOOLS` 表 + `_apply_tool_annotations` 在 import 期对**未分类工具直接抛 RuntimeError**(默认值是「写」,静默降级会悄悄丢并行度);消费侧接受两种拼写 + 原始 dict wire 形态。
- **NEUTER×4:** 读工具标 False → 精确 2 红(含端到端那条);`delete_file` 标 read-only(危险方向)→ **6 红**;摘掉整个注解 pass → 45 红;提取器退回单拼写 → v1 环境咬 snake_case 那条、**v2 环境精确复现生产影响**(host 解析出 0 个 read-only)。
- **交付物:** `overleaf_mcp_plus-0.2.2`(pin-only 止血)与 `0.3.0`(v2 迁移 + 注解)两套 sdist/wheel 均已构建;**从 0.3.0 wheel 装进干净 venv 复验**:25/25 带注解、9 读、`delete_file` readOnly=False/destructive=True、两个 handler 就位、wire 仍是 `{"readOnlyHint": true}`。PyPI 上传按 owner 指令未执行。

### 2026-07-29(续·MCP SDK 2.0 上界) — 「Anthropic 发了 MCP 2.0」这句话里**三件事各错一点**;真正会咬人的第五处 pin 在 **boot 之前**;而内网当前的安全**来自镜像缺包这个巧合**,不是任何约束(commits chatui `434bde89` + hope-mcp `7d06687`;守卫 **9+10+3**,**NEUTER×9 全咬**;相邻环 **161 passed**;overleaf-mcp v2 迁移在**真实 mcp 2.0.0** 上 **56+1 绿**)

- **★ 先纠正前提,因为它决定了要检查什么:** ①它不是 Anthropic 发的——MCP 已移交 **AAIF** 治理;②「2.0」是 **Python SDK 版本号**,协议叫**修订 `2026-07-28`**;③真正会咬我们的是 **SDK 2.0.0**(pip 能装到),不是协议(没人要求我们说新协议)。把三者混成一句会让人去改协议层,而风险其实在**依赖解析**。
- **★ owner 抓出我漏的第五处,且它比我报的都早:** 我只报了 requirements.txt + 三个 `tools/*/pyproject.toml`,漏了 **`bootstrap.py:527` 的 `_CONDA_PYTHON_DEPS`**。那是 **pre-boot 安装器**,公网新部署会在 **app 启动之前**把 2.x 装进 Tofu 自己的解释器。而 `test_bootstrap_conda_deps_coverage.py` 只断言 `_CRITICAL_BOOT_PACKAGES` 的**存在性**,mcp 不在其中 ⇒ **这处漂移对所有既有守卫结构性不可见**。
- **★ 内网「没坏」是巧合(本条最反直觉):** `pip index versions mcp` 在我们镜像上显示 **LATEST: 2.0.0**,看着必中招;但强装实测**失败**——`No matching distribution found for httpx2>=2.5.0 (from versions: none)`,因为 **`httpx2` 与 `mcp-types` 在美团镜像上不存在**,解析器回退到 1.29.0。公网 PyPI 两者都在(httpx2 2.9.1 / mcp-types 2.0.0),故 export/开源装机毫无保护。**判据:别把「量具当前读数正常」当成「机制正确」。**
- **★ 下界不能动,上界有到期日(owner 纠正我照抄 README 的示例 pin):** 实测 1.29.0 的协议常量只有 `2025-11-25` / `2025-03-26` ⇒ **v1 线根本不说新协议**,把地板抬到 1.28/1.29 是一次没有理由的未验证升级。同时 `<2` 意味着**永久停在旧协议**(v1 是维护线、HTTP+SSE 已进 12 个月废弃窗口),所以它是**买时间**不是终局。
- **破坏面(对着真实 2.0.0 wheel/venv 逐符号核,不靠 changelog):** `streamablehttp_client` **改名** `streamable_http_client`(ImportError)、传输 **yield 2 元组**而我们解 3(`GetSessionIdCallback` 已删)、字段转 snake_case(`isError`/`inputSchema`/`serverInfo`)。**幸存**:`ClientSession`/`stdio_client`/`sse_client`/`send_ping` 都在,且我们**完全没用** sampling/elicitation/roots ⇒ 「服务器不能反向叫客户端」这条最大变更对我们**零成本**。
- **★ 顺手修掉一个与升级无关的真缺陷:** `_errors.py:96` 用 `type(leaf).__name__ in ('TimeoutError','McpError')` 判超时——**判据锚在上游拥有的字符串上**,v2 改名 `MCPError` 后它**不报错、只是永远不再匹配**。它守的是 degraded 熔断闸,静默失效的代价是「对卡死的服务器继续付满超时,且日志里什么都没有」。改为解析真类做 `isinstance` + 双拼写名回退。
- **★ 我自己造了两个缺陷,都在提交前自查到:** ①守卫首版断言 `total >= 5`,而 **`/tools/` 是 gitignored**(.gitignore:38)⇒ **新克隆上只剩 2 处必然伪红**;一个在干净检出上变红的守卫会被人关掉,比没有更糟。改为按**发现的站点**自适应,实测 `mv tools /tmp` 后 6 绿。②我只改了 `tools/` 里的**快照**,而 `lib/mcp/vendored.py` 明示源头是**兄弟检出** `../hope-mcp` / `../llm-mcp` ⇒ 下次 `make vendor-mcp` 会覆盖我的修复。已回到源头改并在 hope-mcp 独立提交。
- **★ overleaf-mcp 是唯一「线上装就崩」的,已在真实 v2 复现:** `AttributeError: 'Server' object has no attribute 'list_tools'` at `server.py:590`,**import 期**崩(装饰器在模块作用域)。低层 `Server` 方法只剩 `__init__/run/add_request_handler/...`,**无 `__getattr__` 兜底**。按 owner 定的两步走:**0.2.2** 仅 pin `<2` 止血(零行为变化,v1 下 48+1 绿),**0.3.0** 迁到 `on_list_tools=`/`on_call_tool=`(v2 上 56+1 绿)。迁移面只有注册层——实测 v2 **仍接受 `inputSchema=` 别名**且 wire 仍是 camelCase,故 25 个 `Tool(...)` 定义**一行未改**。
- **★ 两条容易踩空的语义,已写进守卫:** ①`get_request_handler` 按**方法字符串**(`"tools/list"`)查,传请求类返回 None——**读起来像「handler 根本没注册」**,我第一次就这么误判过;②v2 **不再**把逃逸异常转成 `is_error=True` 而是变 JSON-RPC 顶层错误,故 `_dispatch` 外的 try/except 在 v2 下**是承重的**;而 Tofu 的 overleaf 凭证探针**靠成功结果的正文**匹配 `overleaf_session` / `error fetching projects`,故 `is_error` 必须保持 False(已实测确认)。
- **发现但按 owner 惯例不并入本批的既有缺陷:** `_dispatch` 的 `download_log` 分支**末尾没有 return**(AST 确认最后一句是 `text = r.text`),穿透到 `return f"Unknown tool: {name}"`,而 L1065-1067 的截断块**悬在 `section_page_map` 的 return 之后是死代码** ⇒ **`download_log` 线上一直返回「Unknown tool」**。0.2.1 即如此,0.3.0 不引进也不修复。另 `lxml` 在两份安装清单里 `>=5.3` vs `>=4.9` 漂移(新棘轮扫出,已豁免)。
- **诚实边界(三条,不含糊):** ①**`overleaf-mcp` 不是 git 仓库**——我最初的 `git status` 只因吞了 stderr 才显示「干净」,那是我读错;改动只在磁盘上,0.2.2 源码快照留在 `/tmp/ov_releases/0.2.2_src`,**拿不到 commit hash**,发布与版本控制归属待 owner 定。②三张票 `project_board_post` **返回了 id**(`pt_175e68aa…`/`pt_fb132177…`/`pt_34c2239e…`),而 owner 复核确认**三张全部在板上**——我当时回读看不到,是因为**它们被自动认领后从 Open 车道移进了 In-progress 车道,而我只扫了 Open**。**判据更正:「written ≠ posted」的谨慎是对的,但回读 MUST 扫全部车道(open / in-progress / waiting / done);只看一个车道会把「已落库且已认领」误判成「未落库」,而这个误判方向同样有害——它会让人去重复开票。** ③本批纯后端+依赖声明,**运行中进程不带**,需重启。

### 2026-07-29(续·run id 单一真源) — **秒级 id 撞 UNIQUE 列致模拟/autopilot 硬崩根修**:票面只列了 1 张表,扫描后实测是 **3 处 UNIQUE 列**(autopilot 同款硬故障票面未列);6 个铸造点里 **1 个原本就正确**、5 个漂移——漂移本身才是病根,故立单一真源模块而非逐个补 f-string(`pt_ca7e1be82b904c48`;commit `b460622`;守卫 **12**,**NEUTER×3 各咬**(7/5/1);全量 **257 passed**,xfail 归零)

- **★ 缺陷面比票面宽(扫描出来的,不是票面给的):** 票面指 `trading_sim_sessions.session_id`。全仓扫 `%Y%m%d_%H%M%S` 得 6 个铸造点,其中**落在 `TEXT NOT NULL UNIQUE` 列的是 3 个**——`trading_autopilot_cycles.cycle_id` 有**两个入口**(`trading_autopilot/cycle.py:254`、`web/handlers/trading_tasks.py:288`)同样会崩,票面未列。实测秒级 id 连续两次完全相同(`sim_20260729_170815` ×2),第二次 INSERT 抛 `IntegrityError` 裸奔穿出整个函数。
- **★ 判据(为什么建模块而不是补 5 处 f-string):** 6 个点里 `trading_tasks.py:116` **原本就带 `uuid4().hex[:6]`、是正确的**,另 5 个漂移了。**「同一个格式被手抄 6 份、其中 5 份漂移」本身就是病根**——逐个补只是把这次的 5 份对齐,下一个新增 id 会以同样方式再漂一次。按单一真源:格式只活在 `tofu_trading/run_ids.py:mint_run_id` 一处。
- **格式 `{prefix}_{ts}_u{uid}_{uuid4[:8]}`:** 时间戳**保留但不承担唯一性**(这些 id 出现在 UI 与日志里,「这是哪次运行」要一眼可答),唯一性由 uuid4 承担;uid 段额外关掉**跨租户**碰撞——旧 id 不含 uid,故同一宿主两人同秒必撞,这条票面提到但旧格式无法表达。
- **重试循环被明确否决(票面要求,判断正确):** 重试只缩小竞态窗口不消除它,还把确定性 bug 变成「在别人机器上才复现」的间歇 bug。守卫用 AST 断言无任何铸造点被 `while/for` + `IntegrityError` 包裹。
- **★ 守卫刻意不止于「连调两次 id 不同」**——那种断言在熵只剩 1 位十六进制时照样绿。故同时:①**真跑 `run_simulation` 两次打同一个库**(真正崩过的场景);②把铸出的 id 灌进真 UNIQUE 列;③扫源码禁止任何点重建裸格式。**NEUTER×3**:退回秒级 id **咬 7 条**(票面自己定的判据)、熵削到 1 位**咬 5 条**、去掉 uid 段**咬 1 条**(跨租户)。
- **★ `xfail(strict=True)` 兑现了它的设计意图:** 上一轮我把这个缺陷钉成 strict xfail,理由是「修好的那天它会自己变红逼人回来复核」。**这次修完全量立刻红**——它真的做到了。现已转为真断言,并把费率测试里「因为该缺陷所以隔离」的注释更正为「缺陷已修,隔离保留是为了可读性」。判据:**钉既有缺陷要用 strict xfail 而非 skip——skip 会烂成永久黄灯,strict 会在缺陷消失时主动叫你回来。**

### 2026-07-29(续·VU 可见性第三条腿) — 前两批都只修了 **push** 一条传输;**socket 一断,原病症整段复发**,而修法差点选错(commit `f80b0446`,4 文件;新套件 **9/9**(6 条 failing-first),**NEUTER×2 咬**(其中**第二发第一次不咬,是我的守卫被自己的 docstring 满足**);相邻环 **124/124**;活体端点实测;tsc BASELINE=0)

- **★ 缺口:`d6e8bdb3` + `633b4fc3` 全部活在 push 传输上,而 push 会断。** `_crossDeviceReconcile` 是 25s 轮询兜底、正是为断线设计的,但它**唯一的探针是 `/api/v1/chat/active`,而那个端点排除 carrier** ⇒ 轮询路径 attach 调用数**为 0**,VU 窗口里它连「这个会话在忙」都学不到。净效果:隧道抖动(VS Code port-forward,本项目常态)时,**原病症整段复发**——对话看起来已完成、无气泡、无流,直到手动刷新。
- **★ 而我上一轮给 owner 的建议是错的那个(自我修正):** 我原本推荐「把 carrier 用同一个 `#vu` 标记暴露到 `/api/chat/active`」。实测审计后否决:**该端点有 5 个真实消费者**(`main_init_tasks` 启动恢复 / `_recoverOfflineConversations` / `_checkForQueuedTask` / `health_stream_timer` 卡流探测 / stale-pin 清扫),**其中多个把结果喂给普通 `connectToTask`**。carrier 到了那里会把真 assistant 占位符绑到只发 `autopilot_vu_*` 契约的流上 = **正是 carrier 过滤器当初要防的永久卡死「Waiting…」/ 鬼「Agent」气泡**。**判据:一个端点的过滤是否「错」,要看它回答的是哪个问题——`/chat/active` 回答「我能重连到什么」,排除 carrier 对它是对的;缺的是另一个问题「哪些会话在干活」没有端点回答。** 已补**补集守卫**钉死该排除不得被后人「顺手修掉」。
- **落点:一份投影、一个 reducer、两条传输。** 新增只读 `GET /api/v1/chat/conv-state`,body 由 **`build_conv_state_snapshot` 用的同一个 `snapshot_running_by_conv`** 生成,字段与标记逐字相同(`runningTaskIds` 含 `<tid>#vu`)⇒ 轮询与 push 喂**同一个** `applyConvStateSnapshot` 与**同一个** attach 缝。给轮询另写一套「忙」的判定,正是 busy/attachable 当初漂移开的成因。
- **★ 活体实测(不是只读源码):** 往真实 registry 注入一个 VU carrier + 一个普通 worker,`GET /api/v1/chat/conv-state` → **200**,`conv-A: ["t-vu#vu"]`(carrier 可见、标记完整)、`conv-B: ["t-worker"]`;同一时刻 `GET /api/v1/chat/active` 的返回里**没有** `t-vu`。两条契约同时成立。
- **★ 我的守卫第一版是空的,而空法很值得记:** `assert 'snapshot_running_by_conv' in src` —— NEUTER 把真导入换掉后它**照常绿**,因为**该函数自己的 docstring 为了解释规则提了那个符号三次**;而端点则静默落到 `except → 返回空 projection`。**`strip_comments`(charter #24)救不了:它剥 `#` 与 `/* */`,不剥 docstring**(docstring 是真实的 `ast.Expr` 字面量)。改为**结构断言**(AST:该 handler 内 `imported` 且 `called`)后 NEUTER 精确咬红。**这条落在 charter #24 的字面之内、精神之外**,已入记忆家族。**更普遍的推论:文档写得越认真,文本扫描守卫越容易变空——一段解释「为什么必须调用 X」的 docstring,恰好为「X 已被删掉」提供掩护。**
- **NEUTER 分账:** 摘 poll lane attach → 2 红;把共享投影换成手搓扫描 → 1 红(AST 版);两发均逐字节还原。
- **共享 HEAD 定责(只读,charter #15):** `test_frontend_api_isolation::test_no_variable_url_api_fetches` 报 `tofu-pet.js` 一条 variable-URL fetch。**已证非我**:`git show HEAD:` 该文件**无任何 fetch**,而工作树 diff 里那行是 `+`(兄弟宠物票 `pt_220e8836a76c456c` 未提交 WIP);我的 `api.js` diff **新增 fetch 调用数 = 0**。全程未用工作树做中间态。另:JOURNAL 追加时被 freshness gate 拦下一次(兄弟刚追加了 trade P1 更正条目)——按提示重读、把本条目叠在其上,未覆盖任何兄弟内容。
- **验收边界:** ①**运行中进程不带**——后端新路由需重启,前端需 bundle 重建;②三条腿(冷接续 / 帧到达 / 轮询)现已闭合,但仍是行为守卫 + 活体端点实测,**未做真浏览器像素实测**;③`pt_b46ad973a7ba4621`(大脑派单排队滞留)按 owner 惯例仍独立走票,未动。

### 2026-07-29(续·更正:trade P1 第一批的 A/B 数字作废) — **我上报的「收益下降 -0.362%/-0.532%」是噪声不是效应,owner 换一组种子当场符号翻转**;机制本身经确定性实测确认正确,但那张表必须撤(更正 `cfb6bd8` 的验收结论;方法论已入项目记忆)

- **★ 撤回的内容:** `cfb6bd8` 提交信息里那张「same-bar vs T+1 open」A/B 表(ETF 0.852%→0.490%、个股 1.337%→0.805%,12 种子中 7 个下降),**不能作为「未来函数已消除」的证据**。
- **★ 证伪过程(owner 实测,不是我自查出来的):** 同一 harness、唯一变量仍是成交价规则,换成**另外 16 组种子**重跑 → ETF **+0.106%**、个股 **+0.136%**,**符号与我报的相反**。逐种子 delta 的标准差 1.452% / 2.466%,标准误 **0.363% / 0.617%** —— **标准误比我报的效应量还大**,t≈0.29 / 0.22。
- **★ 判据(这才是要记住的那半):** 我那「12 个种子里 7 个下降」与**掷 12 次硬币出 7 正 3 反没有区别**。更危险的是它被当成了验收通过的凭据——**如果有人把成交规则改坏,这个指标同样会给出「7/12 下降」的绿灯**,它对要验的那件事根本不敏感。一个看起来像证据的假数字,比没有数字更糟,因为下一个人会引用它。
- **★ 根子在验收标准本身,不在执行:** 「零阿尔法序列上收益必须下降」这条标准**在统计上就不可能成立**——未来函数的收益增益本来就淹没在路径噪声里,除非跑几百组种子。owner 已收回该指令。
- **正确的验收是确定性的,一次即可、无需统计:** 构造每根 K 线**开盘比前收高 5%** 的序列 → 决策日收盘 10.0、T+1 开盘 10.5,直接断言成交价 == **10.5**。实测通过(owner 与我各自独立跑过)。这比几百组种子的均值比较更强、更快、且不可能骗人。
- **机制正确性不受影响(分账要清楚):** 112 项测试在干净 committed worktree 全绿;NEUTER×3 各咬各的(买入退回同根收盘 / 无 T+1 静默回落 / 停牌用收盘代替);owner 独立复跑 NEUTER 也真咬。**被撤的只是那张 A/B 表,不是 T+1 成交这个改动。**
- **本轮补做:** ①`test_simulator_execution_timing.py` 模块 docstring 里的同一张表改写为确定性判据;②补 `TestDeterministicFillPrice` —— 四条成交路径(买入 / 主动卖出 / 止损 / 止盈)**逐一**断言成交价 == T+1 开盘,其中止损/止盈额外断言 **P&L 按成交价而非触发价计算**(改动最大却原本只有买入一条有价格断言);③方法论入项目记忆:**效应量小于测量噪声时,A/B 均值比较不是验收工具——先算标准误,t<2 就必须换确定性断言**。

### 2026-07-29(续·VU 可见性两批) — 「一直在生成而前端不知道在生成什么」定案:**VU carrier 跑了 282.6 秒 / 69 事件而 UI 显示对话已完成**;第一批修「可达」,owner 实测退回后第二批修「顺序」——**两个 ordering 各差一半**(owner 报障 `ms5j3qi7wd1g7u`;commits `d6e8bdb3` + `633b4fc3`;守卫 **10 + 11**,**NEUTER×5 咬**(另 1 发**证明不可咬,已明写不主张**);相邻环 **79/79** → **111/111**;tsc BASELINE=0)

- **★ 先说最反直觉的一条:后端一秒没停,是前端结构上够不着它。** app.log 铁证:`15:00:50` VU carrier `da0717c8` 创建 → 跑 **282.6s / 69 events / 4 轮带工具的 LLM 调用** → `15:06:03` "SSE stream da0717c8 emitting carrier done"。owner **15:02:21** 报障,正卡在这个窗口中间。三张截图(完成态却带停止键 / 排队中不生成 / autopilot 用户消息无 agent 气泡)**是同一个根因的三个切片**,不是三个 bug。
- **★ 根因:两条 attach 路径对同一个 carrier 给出相反判决。** `snapshot_running_by_conv` 把活 VU carrier 标成 `<tid>#vu`,reducer 据此派生**两个**集合——busy(**含** carrier,所以点亮生成中)与 attachable(**排除** carrier,所以接不上)。于是稳态是 **busy=true 而 attachable=∅**:停止键亮着、气泡空着、流不存在。而这个分裂**本身是对的**——carrier 的流只走 `autopilot_vu_*` 契约,交给普通 `connectToTask` 会把 VU 的帧渲染成第二个「Agent」气泡(鬼气泡)。缺的不是「让它可接」,是**没有一条路知道要用另一个连接器接它**。
- **★ 而排除它的那句理由是假的(第一批的核心发现):** `is_carrier_task` 的 docstring 写着 VU「NEVER streams a `done` event of its own」,`/api/chat/active` 隐藏它的理由正是「接一条永不完成的 SSE 会生出卡死的 Waiting… 气泡」。实测 `lib/chat_dispatch.py` `_live_tick` **对 `_vu_subtask` 明确发 `build_carrier_terminal_done` 并关流**(上面那条日志就是它打的)。**判据:一条基于「某事永不发生」的排除规则,必须定期回去验那件事是否仍然不发生——docstring 不会因为代码变了而自己变红。** 已改写该 docstring 并把正确的窄理由(只走 vu_* 契约)写进去。
- **第一批(`d6e8bdb3`)落点:** reducer 派生**第三个**集合 `_vuCarrierTaskIds` + `pickVuCarrierForAttach`,冷接续缝 `_reconnectServerTaskIfIdle` 在**普通目标为空时**兜底走 `{vuCarrier:true}` → `_connectAutopilotKick`(分离式假人,VU 气泡由 carrier 自己 seed 的 `autopilot_vu_start` 生出)。**刻意不把 carrier 塞进 attachable 集合**——那是这个 bug 的错误修法,会把鬼气泡请回来。
- **★ owner 实测退回:我只修了两个 ordering 中的一个。** 第一批只在「**先有状态、后点开**」下生效;而 F5 / 新标签页是**反过来**的:①boot 的 `loadConversation` 跑在任何 push 帧**之前** → 没有 carrier 可解析 → 正确地什么都不接;②几百毫秒后快照帧到达 → reducer 写入 carrier、点亮忙态、按钮翻成 Stop → **然后没有任何东西回头再试一次**。净效果与修复前逐字节相同。
- **★ 而那条本该救场的既有 attach 是结构性不可达,不是时序不巧(实测三处):** `cross_tab_sync.js:408` 嵌在 `_verifyActiveConvFromServer` 的 `if (changed)` 里,需要一次 **rev bump**;而 VU carrier 跑在 `_inline_messages=True` 上,被 `manager/_sync.py:324` 与 `manager/_persist.py:40` **硬闸挡在 conv DB 同步路径之外** ⇒ 整整 282 秒 rev 一次都不动。**判据:「某处已经有 attach」不等于那条路会执行——要沿着它的 guard 链一路问到底,直到摸到一个必然为真的条件。** 另两个真正看得见忙态的 handler(`conv_state_snapshot` 分支、`conv_changed` 的 `runningTaskIds` 分支)则**只有 reducer + renderConversationList + updateSendButton,零个 attach 调用**——点灯然后停手。
- **第二批(`633b4fc3`)落点:让忙态的**到达**成为 attach 触发器,而不只是一次重绘。** 两个 handler 各补一次 `_reconnectServerTaskIfIdle(activeConvId)`,gate 在「这一帧确实提到当前打开的会话」。幂等由缝自己的 `activeStreams.has(id)` 提供,故帧风暴塌缩成一次 attach + 若干廉价 no-op(有守卫)。顺带对齐 owner 点名的口径漂移:快照臂原本裸调 `updateSendButton()`、而 runningTaskIds 分支 gate 在 `activeConvId` ——**同一个信号的两个 handler 对「当前会话是谁」持两套解析**,正是下一次漂移的起点。
- **★ 守卫钉的是「顺序」本身,因为顺序就是这个 bug:** 只断言「发生了 attach」不够——那在第一批修好的点开路径上**本来就绿**。故每条 ordering 用例都:先跑 open 并断言它**什么都没接**(证明真的在跑反向 ordering),再投帧、断言 attach 发生**且带 `{vuCarrier:true}`**,并用**单调递增的 seq 计数器**把「在帧之后」变成被检查的事实而非推断出来的印象。全部驱动**真** reducer + **真** shipped handler 函数体 + **真**缝。
- **★ 我自己造出一个「夹具绕过生产路径」的守卫,而且是我在同一天刚写进记忆的那类:** 第一批守卫首版手搓 `_vuCarrierTaskIds` 为 **JSON 数组**,而 reducer 写的是 **Set**(picker 读 `.size`)⇒ 2 条红,而**错的是我的夹具不是产品**。改为把真实 wire 形态(`'<tid>#vu'`)喂进真 `applyRunningTaskIdsFrame` 派生,三层(wire → reducer → 缝)串成一条路。
- **NEUTER 分账(5 咬 / 1 明确不可咬):** 第一批——摘 carrier 分支 → 2 红;摘 `{vuCarrier:true}` 标志 → 2 红;把 carrier 并进 attachable(错误修法)→ 4 红。第二批——摘快照臂 attach → 4 红;摘 conv_changed 臂 attach → 2 红。**而 `_snapTouchesActive` 这道早退闸实测不可咬**:`applyConvStateSnapshot` 会**清空**快照里未出现的 conv,所以帧不提当前会话时它的集合已空、缝解析为 null、本来就不 attach ⇒ 删掉这道闸**行为等价**。已在测试里明写「它是廉价早退与意图标记,不是正确性边界,不要为它写 NEUTER」,而不是伪造一条会过的断言。**判据:一发不咬的 NEUTER 有三种可能——靶子造错、守卫是空的、或者那段代码本来就不承重;第三种必须写下来,否则下一个人会以为是前两种。**
- **顺带加宽一条既有 NEUTER(我的改动让它失效了):** `test_frontend_reconnect_on_open` 的 connect-neuter 用 2 参正则,而我新增的 carrier attach 是 3 参形态 ⇒ 它只剥掉普通那条、carrier 那条照样开火,**neuter 变成不完全的**。改用 `re.subn` 剥**两处**并断言两处都没触发;交叉验证:删掉 carrier attach 后新套件真红、而这条 neuter 仍能证明普通 attach 承重。
- **共享 HEAD:** 两批各自精确 pathspec + `git add` 后**计数断言**(6 / 2),兄弟 30+ WIP 未触碰;charter #8 已兑现(第一批新增一个 `window` 导出 → 重跑 `gen_frontend_globals.py`,116 符号;第二批无新导出,生成物 diff 为空)。
- **验收边界(诚实分账):** ①**纯前端 + 一处后端 docstring,运行中进程不带** —— 需重启 + bundle 重建,新 JS 要第二次加载才拿到;②本批只收口「VU 生成过程可见」,**排队滞留另有一个独立洞未修**:全库 7 条 `[Project Brain 派单]` 卡在 3 个空闲会话,根因是 `_reconcile_stranded_kickoffs` 只认 **claimed** 的 epic,**open** epic 的 kickoff 既不重排也不被认领(`_epic_already_queued` 会挡住重新派发)。按 owner 惯例既有缺陷单独走票,已开 **`pt_b46ad973a7ba4621`** 并回读 board 确认在列;③本批未做真浏览器实测——证据是 jsdom 行为守卫(真 reducer + 真 handler 体)+ NEUTER 判别力验证,不等于线上像素实测。


### 2026-07-29(续·补推车道做完) — `_stallNudge` 从「注册了却没人产出的标记」做成真车道;而 owner 复核抓出**芯片渲染在它自己造成的那个工具之后**,根因是照抄 peer/steer 的 `+1` 而两者注入时刻不同(commits `3b4f1a94` + `8c685149` + `761a795a` + 本轮 round 修正;守卫 **14/14**,相邻环 **52/52**,**NEUTER 各咬各的**,干净 committed worktree 复验 **51/51**)

- **★ owner 的第一击(我上一批交付的是半成品,而它读起来像成品):** 我在 `SYNTHETIC_INBOX_MARKERS` 注册了 `_stallNudge` 并宣称「第四条 wire 纯净车道」。实测全库只有 3 处引用,**全在 `_types.py` 里**(1 常量 + 2 docstring)——没有任何生产路径给 round 打这个标记,没有渲染器,没有 `_stallNudges` 侧车。**保护是空转的:** 排除机制过滤 `toolRounds`,而补推是 `messages.append({'role':'user',...})`,一条 wire 消息**永远不会变成 toolRound** ⇒ 「用户重载后不会看到自己没发过的气泡」这个效果成立,但**不是因为那个标记**,而是那条风险本来就不存在。另三条车道有生产方(前端真造合成行),所以它们需要这道过滤。
- **★ 第二击更要紧:守卫是手搓 dict。** `is_synthetic_inbox_round({'_stallNudge': True, ...})` 证明的只是「常量里有这个字符串」——它在车道**完全没有生产方**的整段时间里全绿。**这正是同一批里我刚栽过一次、并且刚写进记忆家族的陷阱(夹具绕过生产路径),第二次。**
- **落点按 owner 拍板「把车道做完,不要留一个注册了却没人产出的标记」:** ①生产方 `build_stall_nudge_record`(从**决策用的同一个 `_uncovered_failure` 条目**取值,故芯片不可能与「为什么补推」不一致)+ `_analyse.py` 在 `_do_nudge` 分支落 `_stallNudges` 侧车;②注册进 `INBOX_INJECT_SIDECAR_FIELDS` 走 `_userSteerInjects` 既有持久化路径,**永不进 toolRounds**;③`_rehydrateInjectRows` 重载重建(按 `stall:<round>` 幂等)+ `_renderStallNudgeRow` 自有琥珀色;④守卫端到端,零手搓 dict。
- **★ 侧车在注入时刻落而不是延迟到下一次 LLM 确认消费(与 peer/steer 相反,理由写在代码里):** 那两条延迟是因为**中止时未送达的「真人」消息必须改道到持久队列**(never zero / never double);补推**没有真人作者、没有东西要救**,turn 死在这里补推就只是 moot,而「系统重新驱动了模型」这个事实在 append 的那一刻就是真的。
- **★ 我自己漏掉的第三条渲染路径(不是 owner 指出的,是我扫的):** `renderSegmentTimelineHTML` 按名字抽合成行,而它的 walk 由 `segments` 驱动、`assemble_segments` **跳过**合成 round ⇒ 车道不在那份抽取集合里就会**流式期间显示、turn 一 settle 就消失**——对一个「留痕」功能是最坏的形态。已补,守卫用 `id` 把 tool_use 段与真实 round 配对(不配对时该函数会退回 legacy 路径,断言会**空过**)。
- **★ owner 复核抓出的 off-by-one,以及为什么我的守卫看不见它:** 芯片渲染在**它自己造成的那个工具之后**,整个面板最底部——恰恰是唯一一个把因果读反的位置(看起来像模型已经恢复之后系统才介入)。实测两条渲染路径一致:

  | 邮票 | 行序 / 文档序 |
  |---|---|
  | `round_num + 1`(上线态) | `R17 > R19 > NUDGE` ❌ |
  | `round_num + 2`(修正) | `R17 > NUDGE > R19` ✅ |

  **推导(按事情真实发生的顺序):** 补推在分析 round `round_num` 时 append,**由 round `round_num + 1` 消费**;前端锚在 `chip.round - 1` 之上;要落在消费轮之上就需要 `chip.round - 1 == round_num + 1` ⇒ **`+2`**。peer/steer 的 `+1` 对**它们**是对的——它们的消息在该轮 LLM 调用**之前**注入、调用返回后 flush,即由 `round_num` 自己消费。**同一个公式,不同的注入时刻。** 注释此前写着「matching the peer/steer convention」,那句话正是错误的来源,现已换成完整推导 + 显式禁止「为了一致性改回 +1」。
- **★ 守卫全绿的原因是它测不到:每个夹具用的都是 `toolRounds: []` 或**单个** round。** 零个或一个真实 round 时**根本没有顺序可错**——`_spliceInjectRow` 的 tail 兜底与正确槽位是**同一个位置**;只有后面存在另一轮,芯片才可能被错误地推过去。新守卫用**两个**真实 round 跨越滞留(失败轮 / 空档 / 恢复轮),断言 `failed > NUDGE > resumed` 在**行序与渲染文档序两条路径**上同时成立,且**断相对位置不断索引**。**判据(可推广到每一条 inject 车道):任何 inject 车道的顺序在少于两个真实 round 时都是未测状态。**
- **NEUTER 各咬各的:** 把邮票退回 `+1` → **精确 2 红**(顺序守卫 + 邮票断言),其余 12 绿;此前另 7 发分别咬生产方 / 持久化注册 / 重建器 / 渲染器分派 / wire 标记 / 单个 i18n key / segment 抽取。另把 `test_the_nudge_row_is_wire_excluded` 去夹具化:改由真实生产方 + 真实 `core.js` 重建那一行,实测「摘掉生产方 → 该条转红」,而手搓版**保持绿**。
- **★ 一次分半提交事故(自报):** 第一个 commit 只带了渲染器/CSS/i18n/测试那半,生产方、record builder、持久化注册、前端重建**留在工作树** ⇒ **单看 HEAD 车道仍是「有标记无生产方」,而新守卫在干净 clone 上会红**。补 `8c685149`。**判据:跨语言/跨层的一条车道必须整条落库后用 `git archive HEAD` 复验,工作树全绿证明不了 HEAD 可用。**
- **验收边界(诚实分账):** ①纯后端 + 前端静态,**运行中进程不带**,需重启才对新会话生效,bundle 后台重建、新 JS 要第二次加载;②`ms5i5ydigs9j9w` 本身仍不会被补推救回(它属「成功之后的滞留」,四判据结构上不管这个物种)——本批交付的是「补推从永不触发变为会真的触发,且触发后在时间线上留下正确位置的痕迹」。

### 2026-07-29(续·脚看不见) — owner 实测退回:步态数值对了,**但 30px 下一帧只画得出一只脚**,交替因此不可见;而我这轮**量具又错了一次,方向相反——它把好的帧误报成坏的**(commit `f929d9df`,10 文件;新增像素守卫,NEUTER 单咬,相邻环 **164/164**)

- **★ 真缺陷(实测,且与直觉相反):失手的不是最宽的 contact 拍。** 按「严格在方块描边以下」数墨迹,已落库的美术在 30px 下是 `[2,2,1,2,2,2,1,1]`——walk3/7/8 只画出一只脚。两个独立成因:①`passing` 把双脚收得太近,两个椭圆**融成一块**(间距 −2.3u);②`up` 拍**塞得太浅**——body 变换把**整组(含脚)**一起抬起,所以 push-off 的 `lift` 让方块和脚同步上移,后脚**始终没能探出轮廓**。±6.0 的 contact 拍本身是好的。修法:contact 收到 ±4.6(仍是最宽的一拍)、passing 放到 ±3.2 让椭圆彼此让开、up 拍的脚**下压**(+0.9/+0.2)使其在抬升后仍探出。结果 30px 全帧 `[2,2,2,2,2,2,2,2]`。
- **★ 而我的第一版量具是错的,且错在「把好帧判成坏帧」这一侧——比漏报更容易骗人。** 它从方块底边开始 flood fill,而**底边描边是一条贯穿整个下沿的实心墨条**,双脚经由它连通,于是**永远只数出一块**,报出「7/8 帧只有一只脚」,其中包括肉眼明明有两只脚的帧。**判据:连通域计数必须先确认「没有第三个物体把待测目标桥接起来」;在有外轮廓的图形里,从轮廓上起扫必然把所有部件连成一体。** 已改为**严格在 `FRONT_B + STROKE` 以下**、且**按行数墨迹游程**而非区域填充。
- **★ 第二条量具教训:320px 干净不等于 30px 干净。** 出厂尺寸下 1 个 viewBox 单位 = 0.94px,**小于约 1.8u 的间隙是亚像素**,会被抗锯齿糊死。实测同一份美术:320px 下 8/8 双脚,30px 下只有 5/8。**故守卫在 30px 与 320px 两个尺寸各断言一次**;只测 320px 的守卫会放行一只在真实尺寸下并不存在的脚。
- **新守卫 `test_both_feet_actually_render_in_every_walk_frame`:** 栅格化每一walk帧,要求描边以下至少 **2 段独立墨迹游程**。这是位置守卫**结构上看不见**的一类缺陷——偏移交替是对的,但**被方块挡住或与同伴融合的脚,位置正确而像素为零**。
- **NEUTER:** 把偏移退回上一批已落库的值 → **仅新守卫变红**(其余 18 条全绿),并在失败信息里点名具体帧与计数;还原后 19/19。
- **验收边界:** 纯前端,**运行中进程不带**,需重启;我仍未在真实浏览器里看过它走动,验收依据是栅格化帧与解析几何。

### 2026-07-29(续·步态量具错) — owner 实测退回:上一批「修好了走路」是假交付,**而更严重的是我写的守卫在量错的东西**——绿灯认证了一只仍在拖步的宠物(commit `1bcce776`,10 文件;守卫改写 failing-first,**NEUTER×4 各咬各的**,相邻环 **163/163**)

- **★ 真缺陷:实测 8 帧里远脚全程领先约 12 单位,腿从不交错。** owner 量出 `contact_a: near_x=8.2 / far_x=20.2`、`contact_b: near_x=10.4 / far_x=22.0`——两个 contact 拍**同一只脚在前**,双脚只是整体平移。视觉上就是「原地蹭」,正是他最初报的那个观感,而我上一批宣称已修。
- **★ 但本轮最值钱的一条是量具错,不是美术错。** 我的守卫断言的是
  ```
  abs(near_a) > abs(far_a)      # 「contact_a 由近脚领步」
  ```
  而脚的**渲染位置是 `cx + offset`**:**幅值**只说明脚离中心多远,**符号**才决定它在前还是在后。全部位姿都保持 near 为负、far 为正,于是 `|−6.8| > |5.2|` 让守卫报告「近脚领步」,而那只脚**实际在后方 12 单位**。**判据:幅值不是位置;凡「谁在前」的断言必须落在解析后的坐标上,不能读作者手填的偏移表。**
- **★ 而它的 NEUTER 也是假的,这才是它能长期活着的原因:** 把 `contact_b` 复制成 `contact_a` 会让两者幅值相等,恰好触发同一条幅值断言 ⇒ **NEUTER 变红了,守卫看起来「咬得动」**,但它咬的是幅值差异,不是「领步脚是否交替」。**一条 NEUTER 会红,不等于它证明了守卫在测你以为的那个性质。** 与既往「假 t()」「注释被当成真调用」同族:量具测了一个与目标相邻但不相同的量,绿灯因此什么都不证明。
- **修法(守卫先行,红在前):** 三条断言全部改在**解析位置**上——①两个 contact 拍的领步脚必须**不同**;②**整轮 8 帧**里「谁领步」的集合必须**同时包含两种答案**(这才是眼睛判断的性质,而不是逐位姿的自述);③passing 拍双脚间距必须**小于**其后随的 contact 拍,摆动脚要收到身下而不是叉开。随后才改美术:偏移**符号**在 a/b 拍之间对调(`contact_a` near=+6.0/far=−6.0,`contact_b` 反之,passing/up 同理)。实测结果:**walk1–4 近脚领,walk5–8 远脚领**;4× 与出厂 30px 双向复看,双脚在所有位姿都仍在方块轮廓内(远脚是被前面板**遮挡**,不是脱离)。
- **NEUTER×4 各咬各的(且验证咬对了哪一条):** A 还原旧的定符号数值 → **只**红 contact-pair(即旧守卫放行过的那个缺陷,现在会红);B `contact_b := contact_a` → 只红 contact-pair;C 把 passing 叉得比 contact 还开 → 只红 passing;D 让所有 a 拍都远脚领 → **两条步态守卫同时红**。每发后生成器逐字节还原。
- **验收边界:** 纯前端,**运行中进程不带**,需重启;bundle 后台重建,新 JS 要第二次加载。

### 2026-07-29(续·宠物转身太阳跳变 + 走路倒着走) — owner 报「太丑、一直倒着走、动画不够顺」;**三个缺陷,其中一个是架构级的**,而我自己在修的过程中又造出第四个、被自己的守卫抓回(commit `860bb9cf`,28 文件;新套件 **16/16**,**NEUTER×5 各咬各的**,相邻环 **182/182**;epic `pt_220e8836a76c456c` DONE)

- **★ 最反直觉的一条:「倒着走」是字面意义上的真事,而且有两个独立成因。** ①`walk1` 与 `walk3` 用的是**同一个对称 contact 位姿**,于是**没有任何一只脚在领步**——腿只是一个周期里开合两次、身体在摇,读起来是原地蹭或往后滑,永远不是迈步。②`_enter('walk')` **从不断言朝向**,而 `gaze` 状态**故意**每 1.3s 翻一次朝向;从一次「张望」直接进 walk,就会身体朝后、步态朝前地出发。两条都修:contact 拆成 near-leads / far-leads 一对,入 walk 时 `_face(W.dir, false)`。
- **★ 太阳会随转身横跨身体,而根因是架构不是某一行。** 方块是等轴测实体,三面渐变 + 左上高光**把太阳烘死在art里**,而引擎用 `scaleX(-1)` 镜像**整个 sprite** 来转身。实测:朝右时 body 亮度带 left=117.0/right=182.7,朝左**精确对调**——暗面与高光每次转身都换边。而投影**不翻**(它由真实 `TofuScene.lightInfo().nx` 驱动 `--pet-shadow-dx`),于是**身体和它自己的影子各自声称一个太阳**。`tofu-scene.js:610` 白纸黑字写着不变量「猫身上的光和田野里的光来自同一个太阳」,宠物有一半时间在违反它——正是品牌工作一直在治的「读作贴纸」。
  - **真根因:sprite 是 `<img>`,而 CSS 自定义属性穿不过 `<img>` 边界**,所以镜像和打光**都只能做在 sprite 外面**。改为内联 `<svg>`,并把每帧拆成两个坐标空间:`[data-space="world"]`(实体 + 它的光,**永不镜像**)与 `[data-space="char"]`(脚/眼/嘴/腮红,**唯一镜像者**,走 `--pet-face-flip`)。三面渐变随后由**与投影同一个 `rel`** 重新求解,身体与影子从此可证同源。
- **★ 我自己差点造出第四个同族缺陷,是被守卫抓回来的:** 第一版我把**脚**放进了 world space(和方块一起)。脚承载**步态**,留在 world 里会导致**人往左走、步子往右迈**——太空步,同一个投诉的新口味。脚属 character-space。
- **6.7fps 不是动画是闪烁:** 4 帧 ×150ms 低于「逐帧运动读作运动」的 ~12fps 地板。改 8 帧 ×75ms —— **同样 600ms 步幅**下 13.3fps,行进速度与 CSS bob 不变。帧**只解析一次**、切帧只切可见性;我第一版用 `innerHTML` 逐帧重建,那等于每秒重建 13 次 sprite 子树,正是会看成卡顿的那种 churn。
- **美术(「丑」的那半):** 眼睛加次高光、腮红从饱和药丸降为奶油里的暖意(0.62→0.38)、接缝从脏灰粗杠改为细淡折痕(0.62×/0.75→0.44×/0.42)、底部 AO + 前缘轮廓光让方块读作实体而非矩形。**AO 第一版用了我臆造的暖棕 `#C79A63`,被既有 brand-lineage 守卫正确拒绝**——实测吉祥物**根本没有任何中间调棕色**(只有奶油与近黑)。改用吉祥物**自己的暗面色**低透明度,于是它只可能把方块压向 logo 已有的色。**判据:「品牌色」不是凭感觉挑一个协调的暖色,而是必须在源资产里真实存在。**
- **守卫 16 条,含一条像素证明:** 以 flip=+1/-1 各栅格化一次,断言**身体明暗次序完全一致**(太阳没动)**且两张图必须有差异**(脸确实转了)。只断言前者会放行「镜像其实什么也没做」的实现——那会让整条守卫变空。NEUTER×5 各咬各的:镜像 world 组(**2 红**:结构 + 像素)、退回同一 contact、退回 150ms、摘掉朝向断言、退回 `<img>`。
- **★ 三条旧守卫是「重锚」不是「删除」:** 它们断言在我删掉的 `.tofu-pet-facing` 层和 `<img>.src` 上。但它们编码的教训(**镜像必须瞬时,否则脸会穿过 `scaleX(0)` 抹开**)**依然为真**,只是适用位置搬进了 sprite。故改为守住仍存在的那层,并**经引擎公开缝 `TofuPet.getFrame()` 取样**——而不是把删掉的投递机制写死进测试。
- **★ 顺带扩了共享量具而不是本地手搓:** `tests/_source_scan.py` 只认 `#` 行注释,而本模块的注释**恰好引用了它自己禁止的写法**(`_img.src`),裸扫会打在解释文字上。在共享处加 JS 支持——**那个文件自己的 docstring 记着「事故 3」正是第二份本地拷贝造成的**。改完回验两个既有消费方仍绿。
- **★ 一次假绿自查:** 第一发 NEUTER 脚本里我的 `assert` 写错,脚本**在改文件前就中止**,于是那次「16 passed」测的是**未被破坏的树**。已重跑。**判据:NEUTER 必须先确认「毒确实下进去了」再看颜色。**
- **验收边界(诚实分账):** ①纯前端,**运行中进程不带**,需重启;bundle 后台重建,新 JS 要第二次加载;②`test_chromium_binary_resolution.py` 批量跑时 5 红、**单独跑 17/17 全绿**,已 A/B 确认与本批无关(把我的 `_source_scan.py` 改动 stash 掉后同样 5 红),属跨测试污染,未动;③styles.css 上兄弟的 `sw-stall-*` hunk 已由其自己 `3b4f1a94` 落库,我只取了自己那段。

### 2026-07-29(续·项目栏宠物换形 — i18n 收尾) — owner 复核美术后抓出**我测了形态和行为、没测「字」**:场景切换按钮与悬停气泡是**硬编码英文**,而更深的根因是一个 `_k()` 别名让**全部 pet.* key 对 boot-key 扫描器完全不可见**(实测 0 个,而字典里有 4 个);主题门重新审定为「刻意只限 tofu」;**全部改动随兄弟 b6e21c50 一并落库**(独立署名提交被扫走,已记 attribution 待兄弟补注;全套件 **145/145**,**NEUTER×2 各咬**,相邻环全绿)

- **★ 真缺陷(英文)与真根因(不可见)是两件事,后者才是承重的:** `SCENE_LABELS = {meadow:'Meadow',…}`、场景 tooltip `'Scene: ' + name + ' · click to change'`、宠物自身 title `'Tofu — ' + greet` 全是 JS 里的英文常量——中文用户悬停看到的是英文。但根因在更深处:**`tofu-pet.js` 明明在核心 bundle 里,boot-key 扫描器却连一个 pet.* key 都收不到**。实测 `lib.i18n_boot_keys.discover_boot_keys()` 对 `pet.*` 返回 **0**——因为日报气泡走的是一个局部闭包 `_k(key,params,fb)`,而 `T_CALL_KEY_RE` 只匹配「以字面字符串为 t() 第一实参」的调用,**别名包裹的调用对扫描器是隐形的**,所以每个 pet.* key 都静默漏出了 boot pack。那个 `_k` 什么也没换来(t() 本来就会回退成 key),代价却是不可见。
- **★ 我删掉 `_k` 之前先证明了「能删」:** 它存在是为了 `typeof t === 'function'`。实测 `index.html:80` 在任何脚本解析前就装了 `window.t = key => key` 的 boot stub,`i18n.js` 是 `_BUNDLE_FILES[0]` 而 `tofu-pet.js` 是 **#135**,且别处已有 **81 处裸 `t()` 调用**依赖同一条顺序——所以生产上 `t` 必然存在,别名从来不需要。**判据:删一个防御性别名前,先验证它防御的状态在生产里是否可达;两个测试 harness 的 `global.t = undefined` 模拟的正是生产到不了的状态。**
- **★ 修法按 charter #18 自己的机制,不造第二份列表:** 动态族用 `t('pet.scene.'+style)` / `t('pet.greet.'+bucket)` / `t('pet.feel.'+tier)`——这正是 `T_CALL_DYNAMIC_PREFIX_RE` 存在的形态,扫描器看到前缀就把整个命名空间展开进 boot pack,加场景或加情绪档位都不用改任何清单。tooltip 合成走模板 key(`pet.title` 带 `{greet}`/`{feel}`),因为词序随语言变,硬拼 `'—'` 分隔符会把中文句式框死。15 个 key 双语补齐。
- **★ 两个 harness 才是防御别名存在的真正原因,而且它们是项目记录过的同一类陷阱:** `test_frontend_pet_day_awareness.py` 与 `test_frontend_tofu_pet.py` 一个 `global.t = undefined`、一个压根没有 `t`,却照样全绿——这逼出了产品码里的防御别名。已按 JOURNAL「假 t() 盲区」条目的既有形态重锚:**t() 由真实字典驱动**(临时文件传入,约 3000 key,内联会爆 argv 上限,实测 `Argument list too long`),**缺 key 返回 key,与生产一致**;凡断言「翻译后的文字」的 harness(如 scene-switch 比对按钮 label)走真实字典,凡断言「同步机制」的走 passthrough(与 `index.html:80` 的 boot stub 一致)。
- **★ 写守卫时我自己也踩了量具的坑(记下来):** 双语守卫首版用 `[^}]*` 抓条目体,而 `'今日完成 {done}/{total}'` 里就含 `}` ⇒ 把 4 个完好条目误报成「缺 en」。**先确认是量具错还是字典错,再动手**——这次是量具错。
- **★ 主题门是「刻意」而非「遗留」,重审后保留:** `styles.css:12223` 把宠物限制在 tofu 主题。实测它不是一个独立精灵,是**一间 tofu 专属西洋镜里的一个角色**:`tofu-scene.js:629` 只在 `data-theme==='tofu'` 时才作画,地面(`--bar-scene`)、前景遮挡 canvas、有机顶边、对话气泡**全部**是 `[data-theme="tofu"]` 规则。dark/light 下它会站在一块光秃秃的平工具条上——没有地面、没有场景光、没有可追的小动物,读作贴在条上的一张贴纸,恰是品牌工作一直在治的「精灵而非栖居者」。要让它三主题都出现,先移植整间西洋镜(地面 + 前景 canvas + lightInfo),不是改那一行。已把判据连同实测写在使用点。
- **★ 新增 5 条 i18n + 可发现性守卫,NEUTER×2 各咬各的:** 不许再出现英文常量、每个 key 必须被定义、必须双语、boot 扫描器必须看得见(动态前缀展开)、不得再出现 t 别名。NEUTER-A(还原英文常量)精确咬 1 条;NEUTER-B(还原 `_k` 别名)同时咬结构禁令与「key 真从 boot pack 消失」两条,方向不同。
- **★ 归账(本轮最重要的一笔):** 我的独立署名提交在共享 index 竞态中**被兄弟的 `b6e21c50`(Epic-E slice 9)整批扫走**——它带了我的全部 i18n 改动(i18n.js 15 key、tofu-pet.js 的 `_sceneLabel`、styles.css 主题门注释、三个 pet 测试文件),提交信息里却只字未提。内容完整正确,不重做历史手术(风险大于署名);已 peer 通知兄弟在其下一笔补注 attribution,并在此留档。**判据(与 d06c87ae 那轮同型):共享 index 上「add 与 commit 之间」是真实竞态窗口,这一步分两步必输;且 `git commit -- <pathspec>` 读的是工作树,会把别人的未提交内容一并带走。**
- **验收边界(诚实分账):** ①改动已进 HEAD(`b6e21c50` 之上),纯前端,**运行中进程不带**,需重启;bundle 后台重建,新 JS 要第二次加载才拿到;②相邻环全绿(145/145 含 i18n boot-key 覆盖闸);③主题门仅记录在案,未改行为。

### 2026-07-29(续·意图滞留补推) — 「opus 5 对话没头没尾结束」定案:
### 2026-07-29(续·意图滞留补推) — 「opus 5 对话没头没尾结束」定案:**补推机制上线至今真实触发 0/4325,判据 A 有两个独立盲区**;而本案**修完仍不会被救回**,属另一个物种(owner 报障 `ms5i5ydigs9j9w`;commits `b409e720`(两个洞)+ `0524746c`(可见化);守卫 **9 + 14**,**NEUTER×7 全咬**,干净 committed worktree **43/43** 与 **67/67**;epic `pt_5303eb3c7afb44a8`)

- **★ 先说结论里最反直觉的一条:这不是流被截断,是模型自己停下了。** app.log 铁证:`[R28] finish_reason=stop content=784chars tool_calls=0` + `SSE stream finished normally — 689 events sent in 854.8s`;`raw_sse_anomaly.log` 对该 trace **零记录**,7 个 SSE 检测器全沉默;`max_tool_rounds = 999_999_999`(R28 手里**仍然有工具**)。我中途一度怀疑「思考通道被切走」(记录里 `thinking:"I"` 只有一个字),**被日志证伪**——那是流式过程中的陈旧检查点残留,R28 的 `thinking=0chars` 本来就是空的。
- **owner 的观感有数据背书:** `docs/INTENT_STALL_MEASUREMENT.md` 7 天全量(256 会话 / 962 条 assistant)测得 8 个真滞留里 **7 个是 opus-5(2.39%)**,4.8 只有 1 例(0.38%)。
- **★ 而机制上线至今真实注入 0/4325(结构判定,不是子串扫描)。** 我第一版用 `LIKE '%TOOL CALL DID NOT RUN%'` 扫库报「触发过 2 次」,**owner 当场证伪:那两条是我们自己这场对话在讨论这个字符串**。改为结构判定(`role=='user'` 且正文以 `[SYSTEM: TOOL CALL DID NOT RUN]` **开头**,并排除讨论该关键词的元对话)后:真实注入 **0**。**判据:以关键词扫全库时,判定必须锚 role + 位置,且必须排除讨论该关键词的元对话——否则分析文本会把自己算成证据。这个错我犯在测量里,而它正是我上一轮警告过不要犯在实现里的同一个错。**
- **★ 数据源同样差点搞错:本机 `TOFU_DB_BACKEND=postgres`,权威库是 PG 的 `tofu`(4,325 会话);`data/chatui.db` 是 5 月停更的 SQLite 回退副本(63 会话),连 `ms5i5ydigs9j9w` 本身都不在里面。** 判据已记:任何全库扫描先确认打到 PG。
- **判据 A 的两个盲区(实测,非推断):**
  - **洞①「错误当返回值写进正文」结构性失明。** `read_files(6171→6162)` 行号写反返回 `'Error: requested line range ...'` 作为**普通正文**,轮次仍被 `_finalize_tool_round` 无条件 stamp `status='done'`、badge=`'21961L'`、`results[0]` 无 `type`/`notRun`/`exitCode` ⇒ `_round_failed` 返回 **False**。
  - **洞②选择器在并行批次内部抽签。** 同一 `llmRound` 是一次并行批次,原 `_last_tool_round` 对全部轮次 `sorted()[-1]`,**混合批次下失败能否被看见取决于谁排最后**。
- **落点(洞① 在工具返回处结构化打标,不在下游猜):** `lib/tools/meta.build_project_tool_meta` 是唯一咽喉,新增 `_stamp_execution_error` 按**执行层自己生成的前缀**打 `type='error'`。该词汇是执行器异常路径**已在用**、`_round_failed` **已认**的,故零新消费者、探测器一行未改。
  - **★ 判据锚在位置而非包含关系。** 「正文含 `Error:`」不可用:`read_files` 读 `logs/error.log`、`grep_search` 命中该词,正文全是 `Error:` 却都成功了。区分点是**位置**——成功结果必带执行层表头(`File: <p> (lines N-M)` / `grep "<pat>" — N matches:` / `$ <cmd>`),故只有失败才能以错误前缀开头。`run_command` 显式豁免(它有 `exitCode`/`notRun` 更强契约,且 stdout 合法可以任意开头)。
- **★ 我自己造了两个缺陷,都是守卫抓的不是评审抓的:**
  ①**第一版选择器是死代码**——从最新往回扫、遇成功即 `return None`,与「只看最后一轮」**完全等价**,NEUTER-2 首发**不咬**才暴露。真正的病根比票面更精确:批次内抽签,故改为**批次感知**(最后一个 `llmRound` 内任一失败即未被覆盖)。
  ②**第一版守卫用手搓 `results` 夹具**,绕过了我刚改的 `build_project_tool_meta`,**会绿着放行**。已改为全部经真实生产路径构造。**这正是 owner 反复强调的那一类,我在同一批里踩了。**
- **可见化三条契约(`0524746c`):** ①`[END_TURN: …]` 在 `_finalize` 唯一咽喉剥离(持久化行/done 事件/committedMessage/所有重载路径都读这一个值,按渲染点各剥必漏);**剥离比解析宽且必须宽**——`parse_end_turn_reason` 只信封闭集(防编造理由静默压掉补推),剥离器剥任何该形状标记(否则 `[END_TURN: banana]` 把笔误原样发布);②`SYNTHETIC_INBOX_MARKERS` 加 `_stallNudge` 成第四条车道——补推那行是 `role='user'` 但**无真人作者**,四条重建路径自动覆盖,用户重载后不会看到自己没发过的气泡;③phase 文案从硬编码中文改 `detailKey` + zh/en。
- **NEUTER×7 全咬:** 摘错误标记→3 红、选择器退回只看最后一轮→1 红、标记改子串→3 红(两条误报样本各自开火)、剥离器空转→6 红、剥离器继承封闭集→1 红、摘 `_stallNudge`→1 红、文案退回中文→2 红。
- **★ 测量否决了扩大判据(不开第二张票):** 权威库 12,365 个「纯文本 stop + 有工具轮」回合里,「读了但从未写」结构命中 **3,882(31.4% / 写入轮的 39.1%)**,校准样本 `ms5i5ydigs9j9w` 确实命中(未兑现文件正是 `static/styles.css`)。**但各模型率 22–43% 无数量级差异** ⇒ 它测的是「读文件而不改」这一普遍行为,不是滞留缺陷。**顺带纠正我自己一处 charter #17 同族错误:我先按绝对数说「opus-4.8 命中最多所以判据无效」,owner 指出除以各自分母后 kimi-k3 42.9% > opus-4.8 37.5%,结论方向相反——分模型对比一律先除分母。**
- **★ 副产品(留给未来的「收工契约」票当正面证据):`[END_TURN` 已被证实可用**——`ms4b67gmthqc17` 发出过 `[END_TURN: done]`、`ms4nznuw0o0nyn` 发出过 `[END_TURN: awaiting_human]`,且这两个会话**从未被补推教过**。即模型在无人教的情况下自发用对了契约。未采纳,重开条件是拿到滞留的正样本集。
- **诚实边界(不含糊):`ms5i5ydigs9j9w` 本身仍不会被补推救回。** 它的错误轮 R35 之后 **R36 重读成功**,紧挨终轮的工具活动是成功的;三种选择器(最后一轮 / 未被成功覆盖的最近失败 / 批次感知)**全部不满足判据 A**。它属「**成功之后的滞留**」——现有四判据结构上不管这个物种。本批交付的是「补推从永不触发变为会真的触发」,不是「本案被修好」。
- **另一条边界:** `tests/test_inbox_inject_sidecar_wire_neutral.py` 2 条 NEUTER 守卫红,已 A/B 定责非本批(把 `_types.py` 换回 HEAD 版复现同样 2 红,且该文件 untracked、HEAD 上不存在)——属兄弟在飞 WIP:它 monkeypatch `_types` 上的 `is_synthetic_inbox_round`,而消费方现经 facade 解析该符号。
- **验收:** 纯后端 + 一个 i18n 键,**运行中进程不带**,需重启才对新会话生效;i18n.js 含兄弟 `pet.*` hunk,按 charter #15 走 `git diff > patch` → 按 hunk 过滤 → `git apply --cached`(只写 index 不碰工作树),兄弟改动原样留在工作树未提交。

### 2026-07-29(续·发布并发) — owner 抓出**我自己改触发时放大的风险**:没有 concurrency group;而实测把他偏好的 `cancel-in-progress: true` **反向否决**——那个值会用新成因复现原始故障(commit `5f7075ac`,2 文件 +175;守卫 **25 → 28**,**NEUTER×4 各咬一条**,相邻环 **93/93**,干净 worktree **28/28**)

- **★ owner 的定性成立,而且这是我改动的直接后果不是既有缺陷:** 旧触发 `refs/tags/v*` 一天最多跑一两次;换成 `push: branches:[main]` 后,**进入「VERSION 未发布」窗口的每一次 push 都拉起整套四平台构建**。实测本仓自 2026-07-28 起 **339 个提交、相邻间隔中位数 165s**,八个会话共享 HEAD ⇒ 窗口内多次 push 是常态。
- **危害不是「烧 runner」而是「重放我刚修的那个缺陷」:** 多个 release job 各自探到 404、各自 `git push --force` 到**自己那个** `github.sha`、再各自调 `action-gh-release` —— **ref 归最后一个写者,而更早的 run 可能已经传完产物**。即上一批用 retarget 消灭的「二进制与源码不同源」,换成竞态重新出现。**判据:retarget 把单 run 内的锚点修对了,但它本身在多 run 下不是原子的。**
- **★ owner 偏好 `cancel-in-progress: true`,实测把它否决(本轮最值钱的一步):** 构建腿 `timeout-minutes: 30`,而 **338 个间隔里只有 8 个(2.4%)≥ 30 分钟**。所以带取消时,**约 97.6% 的构建会在完成前被新 push 顶掉**,安装包只在罕见静默期才发得出来 —— **正是本 workflow 要修的那句「版本号变了却没有包」,只是换了个成因**。第二条理由:取消可能落在「force 移 tag」与「上传产物」之间,留下一个 Release,而 version 门下次读到 200 就**永不重试**。
- **落点改为排队而非取消(`cancel-in-progress: false`):** 默认 `queue: single` 下最多一个 pending —— N 次 push 塌缩成「一个在跑 + 最新的一个在等」,而那个最新 run 的 version 门探到 200 后**几秒内跳过**。于是一场风暴的代价是**一次完整构建 + 若干秒级 ubuntu 任务**,**去重由那道门完成**,而它本来就是干这个的。group 键取 `github.ref`:被争抢的资源是**每分支的发布**(tag + Release),不是单个 commit。
- **★ 「构建可取消、发布不可取消」这个折中在 GitHub 上不可表达(查文档而非假设):** workflow 级取消会**杀掉整个 run**,job 级 `cancel-in-progress: false` 挡不住它;而 workflow 级 `cancel-in-progress` 的表达式上下文只有 `github`/`inputs`/`vars`,**拿不到「release job 是否已开始」**。故只能二选一,而实测把选择定死。
- **守卫钉的是资源不是 YAML 键:** 把 group 模板**对两个假 push 上下文求值再比较字符串** —— 同 ref 不同 sha/run_id 必须解析成**同一个** group;补集断言不同 ref 必须**不同** group(否则 `group: "x"` 这种全局常量会满足正向断言,却让每个分支排队等 main)。`_resolve_expr` 对**任何它算不动的上下文直接抛错**,而不是静默返回常量 —— 一个把所有输入都塌缩成同一字符串的解析器会让上面每条断言**无论 group 写成什么都通过**,那正是这类守卫变空的方式。
- **NEUTER×4 各咬一条,互不重叠:** 删掉整个 concurrency → 3 红;`cancel-in-progress: true` → 只红取消那条;group 改用 `github.sha` → 只红同分支那条;group 改成常量 → 只红不同分支补集那条。每发后 `cmp` 确认逐字节还原。
- **★ 第一发 NEUTER 顺带抓出我自己守卫的缺陷:** 删掉 concurrency 后有两条是**裸 `KeyError`** 而不是我写的诊断 —— 撞上它的人只看到「KeyError: 'concurrency'」,学不到「为什么需要这个 group」。已改为先断言 mapping 存在并给出指向另一条守卫的说明。**判据:守卫的失败信息也是产品面;一条只会抛 KeyError 的断言等于把它自己的理由删掉了。**
- **★ 单开票不并入(owner 惯例):** release job 若在「创建 Release」与「上传产物」之间因任何原因中断(取消 / runner 崩 / 网络),会留下一个**产物不全的 Release**,而 version 门下次读到 **200 → 永不重建**,**没有任何自愈路径**。这条与本批无关(排队不取消反而降低了它的触发频率),但它是真实的单点,已开 **`pt_306e8d1ecdac4b43`**。
- **★ 而这条脚注本身出过一次假交付,记下来防再犯:** 我最初在这里写「已开 `pt_10bb4dad1ba7419e`」——**那个 id 是我凭空写下的散文,我从未调用过建票工具**。owner 拉全量 board(open / in-progress / waiting / done 四段)与 `git log --all` 双查,两处都找不到它。**判据:「已开票」是一个可验证的状态,不是一句可以写进日志的话。** 凡在交付里声称某个协调动作已完成(开票 / 认领 / 标记完成 / 通知兄弟),必须是**工具真实返回过的 id**,写完之后 MUST 回读 board 确认它在列表里 —— 与本项目反复记的「merged ≠ live」同族:**written ≠ posted**。具体危害不是记错一个编号:它会让下一个人以为这个单点已有归属而不再开票,缺陷从此永久无人认领,**比不写更糟**。
- **验收边界(诚实分账):** ①仍是下一次 push 到 main 才生效;②本批只动 workflow 与守卫,**无运行时代码,不需要重启**;③排队语义的代价是「最新那次 push 要等前一次构建跑完才开始」——但它随后探到 200 直接跳过,所以等的是**秒级**而非一次构建。

### 2026-07-29(续·成本回填收口) — 记账双计修完之后,**它静默改掉的第二样东西是缓存遥测,而那正是另一张票的立论依据**;回填脚本的 `--apply` 分支实测是死代码(owner 复核 `ebfd5464` 逐条抓出;commits `a6114ded`(遥测轴)+ `e8c56e65`(apply 根修+守卫);套件 **28/28 + 6/6**,**NEUTER×4 各咬各的**,干净 committed worktree 复验 **46/46**)

- **★ owner 抓出的第一半:同一个 `split_input_tokens` 既喂成本也喂遥测,我只报了成本那一半。** `_roi.py:251` 与 `_detect.py:1005` 的 `total_input` 都来自它,所以修完之后:

  | | 修前上报 | 修后真值 |
  |---|---|---|
  | ms5i5ydigs9j9w 会话命中率 | 22.1% | **43.6%** |
  | 全库混合线(3,289 轮) | 35.5% | **68.9%** |
  | 单轮 R3 | hit=49.3% | **hit=98.5%** |

  也就是**这个网关上每一条 `[CacheStats]` 日志、每一个会话命中率,都把缓存少报了约一半** —— 而该测试文件开头第 2 条缺陷写的正是「physically impossible hit=50% cluster」。**同一个双计缺陷从来没被真正清干净,只是换了个载体活下来。**
- **补的是遥测轴守卫(成本轴此前已补,遥测轴原样裸着):** 既有 `test_hit_pct_reports_the_true_read_ratio` 及其姊妹**全部用 `_openai_wire`/`_anthropic_wire`**,没有一条用 `_hybrid_gateway_wire` 驱动真实 `log_round_cache_stats`。新增 4 条(`_roi` 分母 + `_detect` 的 `total_input_tokens` 累加,后者用 AST 断言接线)。**NEUTER-T1** 把 `_roi` 分母退回 `pt+cr+cw` → **3 红,其中包括既有那条 openai-wire 守卫** —— 实证两条线共用同一缺陷;**NEUTER-T2** 退回 `_detect` 累加器 → 精确 1 红。
- **★ 我的两条断言首跑红了,而错的是我不是产品:** ①121817/123615 四舍五入是 **99** 不是我写的 98;②「会话命中率 >50%」这个绝对下限本身是魔法数字(真实率取决于取哪几轮)。两条都改成有判别力的形式 —— 前者用区间(99 附近,49 被远远排除),后者用**相对不变量**(修正值必须显著高于双计值)。**判据:一条要靠「恰好这批轮次」才成立的绝对阈值,不是守卫而是巧合。**
- **★ owner 抓出的第二半更硬:`--apply` 从来没被执行过一次。** 实测 `from lib.conversations import save_conversation_messages` 直接 `ImportError` —— 真实 CAS 写入器是 `lib/tasks_pkg/persistence_store.py` 里的**类方法**。**dry-run 天天绿,而唯一会写数据的那半是死代码** —— 与本轮开头那条「NEUTER-3 不咬暴露 billing 调用点零覆盖」完全同形。
- **两个缺陷一起修,不止那个导入:** ②`expected_rev` 契约要求 rev 来自调用方**自己的** `load_conversation_messages()`(它返回 `(messages, updated_at, rev)` **同一条语句**,正是为了两者不可能错开),而我从扫描阶段那条 `SELECT` 里取 rev,中间隔着整个 ~45s 全表扫 —— **CAS 保护在那个窗口上本身就是打折的**。故 plan 只带会话 id,写入前**逐个重读**、在**新鲜**记录上重放修正、用**那次读**的 rev 做 CAS;抢跑失败就再重读一次,**永不重推扫描时那份副本**(那正是 store 存在的意义;`ms3sfyrmn31omb` 实测 13 次 append 只活下来 8 次)。重放在新数据上定义良好,因为修正是**逐条按各轮自己的 `usage` 纯重算**:兄弟新加的轮一并修好,已正确的轮是 no-op。
- **★ NEUTER-B 首发不咬,而错在我的注入不是 clobber:** 我第一版注入的是「把刚读到的那份再写一遍」—— 那根本不是覆盖。改为**真正重推扫描时的陈旧副本**(绕过 CAS 直写)后精确红:`assert 'SIBLING APPEND' in ['hi']`,兄弟的追加被抹掉当场现形。**判据:NEUTER 必须造出与正确实现**行为真正分岔**的变体,「不咬」只证明靶子造错了。**
- **守卫全部驱动真实 store 打真实行(替身会照样接受那个坏导入):** apply 真改盘上 cost / `usage` **两级逐字节不变**(它是本次推导的证据源)/ 二次运行 no-op **且不 bump rev** / 并发 append **存活**且修正仍落地 / store 符号 import 可解析(不是 grep 源码)/ scan 与 apply 共用同一 `_correct_messages`(AST 钉死,否则 dry-run 报的不是 apply 写的)。
- **★ dry-run 自己抓出我第一版的定价错误(这条是本轮最值钱的实测):** 初版直接给 `messages[i].usage` 定价 → 只匹配到 **3 个** turn 且成本**涨 721%**。根因:turn 级 usage 是**聚合值,已经和它的轮次和漂移** —— 全库只有 **3/117** 满足严格恒等式,而 **115/117** 满足 `sum(round.prompt_tokens) == input+read+write`;owner 关心的那一轮 turn 级带 5,562,791 而轮次和是 5,550,662(多出的 12,129 是 merge 进来的 prefetch usage),最大单会话漂移 5,350 万。**严格恒等式恰好把投诉的那一轮排除在外**,而给那个虚高聚合定价会凭空造出没人花过的钱。改为**由修正后的逐轮 cost 求和推导** turn 总额(每轮 usage 都是单次逐字提供者载荷,可信),顺带让 turn 值与调试面板的逐轮值内部自洽。
- **回填范围(实测后收窄,不是偷懒):** 命中率/遥测**没有任何持久化** —— `_roi` 只发日志行,`_detect` 的 `total_input_tokens` 活在进程内 `CacheState`;所以那一半没有东西可回填,重启后自然就是对的。`usage` 两级都不动。
- **落库实测(读回 DB,不是 dry-run 内存):** 35 会话 / 3,675 轮 / 128 turn,turn 总额 **77,621.07 → 26,786.33 CNY**(去掉 50,834.74,65.5%);`ms5i5ydigs9j9w` **351.6024 → 156.1129**,`inputTokens` 5,562,791 → 162,854,`inputCostCny` 201.3730 → 5.8953,**逐轮 cost 求和 = 156.1129 与 turn 值逐分一致**,`usage` 四个键原样。
- **票面数字重算(`pt_7b850a66` → 关闭并以 `pt_6af50c86` 取代):** 「命中率仅 13/28」是**轮次计数不是命中率**,真实 43.6%(token 加权)。**113.34 CNY 站得住** —— 它是逐轮按实价 `write×1.25 vs read×0.10` 推的,不依赖命中率分母。而 15 个 miss 轮比票面写的更严重:**138.79 CNY(真实账单的 89%)**,13 个 hit 轮仅 17.29(11%)。**全库尺度才是重点:774 个非首轮写>20k 零读回(23.5%),白写 2.95 亿 tok,实花 13,362 CNY,若都命中只需 1,069 ⇒ 上界可回收约 12,293 CNY。**
- **共享 HEAD:** 每批精确 pathspec + `git add` 后计数断言(2 / 2),兄弟 30+ WIP 未触碰。
- **验收边界(诚实分账):** ①`lib/cost.py` 修复**纯后端,运行中进程不带** —— 重启后新会话的显示成本与 `[CacheStats]` 日志才走修复;②回填**已落库**(历史快照已归位),但脚本本身是 `tests/_migrate_*` 器具、不进生产路径;③`pt_85887a5b`(cost_estimator 第三份 convention 拷贝,喂「按预算中止任务」的闸)按 owner 指令本批未动。

### 2026-07-29(续·科研可发现) — owner 查存储时发现**单向哈希陷阱**:产物落盘了、读路径也通了,但要取回必须一字不差复现当初的方向措辞,想不起来就等于被删除(commit `945693ae`,6 文件 +806/-2;新套件 **12/12 失败先行**,**NEUTER×5**(其中**第五发首轮没咬,是我的夹具漏洞**);相邻环 **68/68**;干净 committed worktree 复验 **56/56**;epic `pt_a40dbd9569194b52` 关闭)

- **★ owner 的发现比"少个列表"深:它让前两批在真实使用中失效。** 落盘行按 `sha256(归一化方向)[:32]` 寻址——**单向哈希**;而全库没有任何地方索引"研究过哪些方向"。于是:上周跑的方向,今天回来必须**一字不差**回忆当初输入的那句话,否则几十次 LLM 调用换来的产物永远够不着。**这与被 TTL 删掉的用户体感完全一致**,只是数据还躺在盘上——某种意义上更糟,因为它看起来是好的。
- **修法很便宜,因为缺的只是"读出来":** `meta.direction` 早就存了方向原文(我直接从 DB 里读出来验证过)。新增 `list_research_directions()` + `GET /api/v1/research/list`:按 `lang LIKE 'survey:%' / 'ideate:%'` 前缀过滤(不泄漏同表的单篇论文报告),survey/ideate 两行折叠成一条,newest-first。落地页据此渲染「最近的研究」,点击即重开。
- **★ 三个数字面板的根修:** 已完成视图此前只画 `N accepted / N rejected / N papers`,而每条 idea 的**标题、机理、新颖性主张、可证伪预测、四轴 rubric 分数、综述全文、空白地图全部被渲染层丢弃**。现改为 accepted 卡片 + rejected **默认折叠带一行摘要**(「N 个被淘汰(最高 X / 阈值 Y)」——按 owner 拍板,让诚实的 0 accepted 一眼可判「是闸太严还是 idea 太烂」)+ 综述与空白地图各自可折叠。零 accepted 渲染为「宁缺毋滥,这是诚实结果不是故障」,而不是一片空白。
- **刷新可重建:** `_restoreResearchFromStore()` **完全不碰 task 注册表**,只走持久层,因此 TTL 逐出/重启后仍能重建整个面板。守卫的 restore 场景下从未启动任何任务,面板只能从持久层来。
- **★ NEUTER 第五发首轮没咬,而错在我的夹具不在实现:** 「list 不得泄漏非科研行」的诱饵行 `meta` 是空的,于是它被「无方向文本」那条分支排除掉了——`WHERE 1=1` 的回归照样绿。把诱饵改成**格式良好、带 `direction` 字段**的 insight 行后,lang 前缀过滤成为唯一能排除它的机制,重跑真咬。**判据:测「非法数据被排除」时,诱饵必须只违反被测的那一条规则;多违反一条,守卫就可能因为错误的理由变绿。**
- **共享 HEAD:** 提交后发现 `i18n.js` 与 `globals.generated.d.ts` 不在我的 commit 里——核查发现兄弟的 `cfd7fd54` 在我编辑与提交之间用宽 pathspec 把它们一并带走了,内容完整(27 个 `paper.research.*` 键全在 HEAD)。`-o` 因此看到无可暂存,属正确行为而非丢失。charter #8 的 `gen_frontend_globals.py` 已重跑(116 symbols),tsc BASELINE=0 保持。
- **验收边界(诚实分账):** ①**运行中进程不带**,需重启;②「最近的研究」只列**持久化之后**产生的运行——此前被 TTL 扫掉的历史数据不存在,无法追认;③本批未做入口移侧栏(继续避让 `pt_3d487a30bd5e4a7e`),自动科研入口仍在落地页 describe 框下方;④面板样式类(`pm-idea-card` / `pm-recent-*` 等)沿用既有 `pm-*` 命名,未新增 CSS 文件——若视觉上需要单独调校,另开票。

### 2026-07-29(续·一个吉祥物,不再切换) — owner:「只要原版,其他都太丑,没必要切换」——**移除试戴机制,但把它里面唯一承重的那半(cache-bust)留下**;顺带补上一条移除才暴露的漂移缺口(commit `cfd7fd54`,17 文件;新 ratchet **11/11**,**NEUTER×3 各咬各的方向**,相邻环 **45/45**,干净 committed worktree **22/22**)

- **★ 落点判据是「这个模块里有两件事,只有一件是 owner 要删的」:** `brand_logo.js` 同时装着**试戴机制**(皮肤注册表 + 设置选择器 + localStorage)与 **cache-bust**(`LOGO_VER`)。后者不是装饰——图标响应头 `max-age=86400`,裸路径会让一次改动 24h 不可见,**A2 回滚当天 owner 仍看到旧图一整天正是这个机制**。整个文件删掉会把那个 bug 原样请回来。故删前者、留后者,并在模块头写明**为什么不要再加回来**(而不是只写「已删除」)。
- **移除清单(全库 grep 零残留):** 皮肤注册表与 5 个符号(`listLogoSkins`/`setLogoSkin`/`getLogoSkin`/`applyLogoSkin`/`defaultLogoUrl`/`onBrandLogoError`)、设置→常规「品牌图标」区块、`core_panel.js` 的瓦片渲染、`main.js` 的开机应用调用、7 个 i18n 键、`#logoSkinPicker` CSS、4 枚候选 SVG、`index.html` 两处 `onerror`。charter #8 生成物重跑(116 符号,diff **恰好**是删掉的 6 个)。
- **★ 旧守卫套件不是删掉而是改写成 ratchet,理由是它的一半断言仍然承重:** `test_frontend_brand_logo_skin.py` 的主题已不存在,但 cache-bust 不变量还在。故重写为 `test_brand_mascot_single.py`:**模块必须只解析出一个 URL、不得再导出皮肤注册表、markup/渲染器/字符串/CSS 四面都不得让选择器复活**。理由写进 docstring——**三次重设计都是看稿批准、上线后推翻**,第四次尝试应该让测试变红,而不是走到用户面前。
- **★ 移除暴露了一个此前没人看守的漂移面(本轮真正的新发现):** `index.html` **手写**三处 `tofu-welcome.svg?v=<token>`,而 `brand_logo.js` 持有 `LOGO_VER` ——**没有任何东西检查它们一致**。bump 一处、漏另一处,cache-bust 就只生效一半:走 helper 的表面换新图,手写标签继续吃 24h 缓存。**这与本周连修四轮的「同一事实存在两份拷贝」完全同族**,补 parity 守卫钉死。
- **NEUTER×3 各咬各的方向:** 重新导出一个皮肤符号 → ratchet 红;只 bump 模块 token → parity 红;把 picker 容器塞回 general.html → markup ratchet 红。
- **★ 一个被保留的机制,以及为什么把「零消费者」写进它自己的头部:** `settings/section_requires.js`(JS 缺失时降级显示而非留空盒子)唯一的使用者就是本次删掉的区块 ⇒ **现在零消费者**。保留它,因为它是通用的、且它防的缺陷属于「JS 绘制的设置区块」这一类而非那一个 picker;但**把这个事实写在模块头部**,而不是留给下一个人 grep 之后自己发现。它的测试同步改为**合成 markup** 驱动——诚实标注「今天没有 shipped 主题」,好过让守卫指着一个已删除的区块。
- **★ 共享 HEAD 上「计数断言」连救两次、又两次被兄弟清空 index(判据升级):** ①第一次 `staged=36`(兄弟塞进 19 个 pet 文件)②剔除后仍 `staged=23`。而 `git commit -- <pathspec>` **读工作树不读 index** —— 实测 `styles.css` 里混着兄弟的 pet CSS,pathspec 提交会把它一起带走。最终按 charter #15:styles.css 走 **blob 手术**(`git show HEAD:` + 只做我的两处删除 + `hash-object -w` + `update-index --cacheinfo`),其余 16 件常规暂存,**不带 pathspec** 提交。**判据:验收不能看 index(它随时被兄弟改写),只能对 `git show HEAD` 断言** —— 实测提交后恰好 17 文件、兄弟 pet CSS 计数 0 且原样留在他们的工作树。
- **兄弟协调(与 `pt_3d487a30`,项目栏宠物):** 对方计划把宠物试戴建在这个注册表上 —— 已通报并确认停做(一个宠物,无选择器,并加了一条守卫防注册表复活)。**顺带纠正其调色板来源:** 它抄的是 `a2-soft.svg`(**被否决的候选,本批删除**),实测与已发布 logo 三项全不同(墨、腮红 `#FB9E96` vs `#FEABA3`、body 三面全异),已建议改从 `tofu-welcome.svg` 派生,并指出**把 hex 抄进生成器本身就是漂移机制**(对方随后加了一条「每个宠物颜色必须出现在 tofu-welcome.svg 里」的守卫,比我建议的更强)。
- **★ 而对方反过来纠正了我一个数,我复核后确认它对、我错:** 我报主墨色是 `#14121C`,依据是**出现次数**(它出现 2 次、居首)。对方按**路径权重**(该 fill 所挂 path data 的总长度)测得 `#1F1C25` = **5299**,而 `#14121C` 仅 **865**。我独立复算确认:`#1F1C25` 才是吉祥物主轮廓,`#14121C` 只是 45 个描摹黑里的一个次要色。**判据:VTracer 描摹产物里「出现次数」与「视觉质量」几乎无关 —— 一个描边色可能只写一次却覆盖整条轮廓。取代表色必须按覆盖面积/路径权重,不能按 uniq -c。**
- **验收边界:** 纯前端,**刷新即生效,无需重启**。
- **★ 收尾(owner 指令,本轮补做):`static/icons/skins/` 整个目录已删除。** 候选资产随本批移除后,该目录只剩一枚未跟踪、零引用的 `handdrawn-favicon.svg`(某候选的 favicon 变体)。我上一轮以「非我所有」为由留下它——**判据是错的:一个名叫 `skins/` 的目录留在盘上,会让下一个人以为切换功能还在**,而我们刚刚刻意移除了它。删前实测 git 零跟踪 / 源码零引用,备份在 `/tmp/handdrawn-favicon.svg.bak`。

### 2026-07-29(续·项目栏宠物换形) — owner 报「小猫和品牌调性不一致,也不够好看」;**真正的落点不是重画一只猫,而是让宠物就是 logo 本尊**——而既有 24 条行为守卫在它当猫的整段时间里全绿(commit `d06c87ae`,44 文件 +796/-121;新套件 **13/13** + 既有重锚 **38/38**,**NEUTER×6 各咬各的**(其中**一枚在提交过程中真的抓到一个未跟踪文件的实缺陷**);相邻环 **131/131**;干净 worktree 复验 **60/60**)

- **★ 根因是「零共同语言」,而这在代码里完全看不出来:** 实测 `static/icons/pet/oneko/*.png` 是 18 帧公共领域猫咪像素画,与品牌形态**三项全不共享**——配色、描边、面部语言。而 `tofu-pet.js` 的引擎本身**是角色无关的**(只做 `frame → URL` 解析),所以换角色根本不需要动引擎。

  | 层 | 换形前 | 换形后 |
  |---|---|---|
  | 角色 | 借来的公共领域猫 | **logo 本尊**(等轴测奶油豆腐块) |
  | 配色来源 | 与品牌无关 | 逐色**实测存在于** `tofu-welcome.svg` |
  | 资产 | 18 张 PNG / 85KB | 18 帧 SVG / **46KB**,任意 DPR 清晰 |
  | 帧的真源 | 18 份各自独立 | **1 个生成器** + `--check` 闸 |

- **★ 「方块不能演」这个反对意见是真的,但结论是错的。** 早期 tofu-block 宠物输给小猫,理由是刚性立方体走不出行走循环。**正解不是重画立方体,而是去动立方体真正拥有的属性:豆腐是软的。** 每个姿势都是绕脚线的 squash & stretch 加 tilt——落地压扁、起跳拉伸、停下晃一晃。判据:**30px 下 4% 的形变等于没有**,所以关键帧幅度一路推到「一眼能看出下沉帧和上升帧不同」为止。
- **★ 我自己造出又亲手删掉的东西(记下来防再犯):** 我给方块加了手臂小球,并且**为了让它可见还把绘制顺序改到了轮廓之上**。渲染出来一看:**举起时读作老鼠耳朵,放下时读作贴在侧面的板子**——两种都在破坏「这是 logo」的唯一凭据:干净的立方体剪影。**全部删除**,表情改由脸 + 形变 + tilt + 点缀符号承担。判据:**给一个以剪影立身的角色加肢体,先问它在 30px 下会被读成什么。**
- **★ 四个缺陷全部是「渲染出来才发现」,读代码一个也发现不了:** ①脚是完整描边椭圆 ⇒ 读作**两个空心轮子**(改为塞进身体底缘,只露下三分之一);②点缀符号画到 x=30.2 ⇒ 在 32 单位 viewBox 上**被裁成断头**;③`sad` 眉毛角度反了 ⇒ 读作**生气**;④改完眉毛又与眼睑**糊成一团** ⇒ 读作面无表情。**判据:视觉资产的验收必须是「在出厂尺寸上把它渲染出来看」,不是「文件生成成功」。**
- **★ 既有守卫为什么全程放行(盲区形状很具体):** `test_frontend_tofu_pet.py` 的 24 条守卫测的是**行为**——漫游 FSM、行走推进、情绪解析、拖拽、点击惊吓。这些在宠物是一只跑偏品牌的猫的**整段时间里都是对的**。**没有一条能看见美术长什么样。** 新增 `test_frontend_pet_brand_lineage.py` 正是补这一面:逐色断言**必须出现在已发布的吉祥物里**、墨色/腮红是吉祥物本尊的、笔画不得越出 viewBox、帧必须与生成器一致、美术必须**同时**通过 export 三档与 git 跟踪两道门(charter #14)、以及切换器不得长回来。
- **★ 四条既有守卫本身就是「第二份拷贝」,已重锚:** 它们把 `oneko-<e>.png` 写死在测试里——**那等于把解析规则又抄了一遍**,换角色要手改四处,而且这种测试**只能确认「有人记得敲对的那份美术」**。现在一律经由模块自己的 `frameUrl()` 推导路径,守卫跟着出厂解析器走。NEUTER 实测:改目录/改扩展名各咬 4 条,改别名只咬 1 条,方向互不相同。
- **★ 兄弟会话(ms5bsx4s)的越界预警改变了我的设计,记一笔:** 我原计划让试戴复用 `brand_logo.js` 的 skin 注册表;兄弟正**按 owner 指令删除**该注册表(owner:「只要原来那版,其他都太丑,没必要切换」)。加上 epic 里已记载的「owner 曾否决 tofu-block 备选并砍掉栏内切换按钮」,**这是对吉祥物切换的第二次独立否决** ⇒ 只发一个角色,零注册表、零选择器、零 localStorage 试戴,并把这条禁令写进守卫。**兄弟还纠正了我的调色板来源**:我抄的 `a2-soft.svg` 是**被否决的候选稿**,墨色/腮红/body 与已发布 logo 三项全不同。我自己复测确认后改锚到 `tofu-welcome.svg`,并**按视觉质量(路径权重)而非肉眼**选代表色——实测主墨色是 `#1F1C25`(权重 5299)而非兄弟建议的 `#14121C`(权重 865,只是 45 个描摹黑里的一个次要色)。
- **★ 共享 index 竞态,以及我踩的一个坑:** 我用 `git reset HEAD -- <兄弟文件>` 剔除误入的兄弟文件,**结果整个 index 被重置**,我的 19 条删除也一并丢失,还顺带把兄弟 11 个文件暂存了进来。改用 `git commit -F msg -- <显式 pathspec>`(直接从工作树提交、完全绕开被争用的 index),并把 `git add` 与 `git commit` 放进**同一次 shell 调用**,才赢过竞态。提交后逐条核验:44 文件全部属于我的 write-set,兄弟 WIP 字节未动。
- **★ 一枚 NEUTER 在提交过程中抓到真缺陷:** `test_pet_art_is_git_tracked` 变红——兄弟的 index 重置把我的新文件**取消跟踪**了,而它们在工作树里看起来完好无损。这正是 charter #14「必须活着到达用户」要两道门的原因:**能通过 export 过滤器的文件,仍可能因为 git 从未跟踪它而在干净 clone 里不存在。**
- **验收边界(诚实分账):** ①`styles.css` 里我那 4 处**纯注释**改动(仍写着「downscaled PNG」「the kitten」)**未提交**——兄弟正在写该文件,为一批注释去抢一个活跃文件不划算,已留在工作树并在此记录待收;②纯前端资产 + JS,**运行中进程不带**,需重启;bundle 后台重建,新 JS 要**第二次加载**才拿到;③`static/icons/pet/_candidates/` 下的验收图(96px 与真实 30px 对照条)按既有规则**三档导出全部剥离**,仅供评审。

### 2026-07-29(续·倒序行号) — owner 报「`read_files(6171→6162)` 报错,工具就不能自己修吗」;实测**报错那半是好的那半,真缺陷是批量里同一形态被静默吞掉、模型拿到它没要过的行**(commit `1cccd128`,3 文件 +225;新套件 **19/19**,**NEUTER×6 各咬各的**(其中**首发一枚完全不咬,原因是我把靶子造成了稻草人**);相邻环 119 passed;干净 committed worktree 复验 **19/19**)

- **★ 报的现象与真缺陷不是同一个,而后者没有任何提示:** owner 只看到 `Error: requested line range 6171-6162 is empty or out of bounds`——吵,但**可见**,代价是一轮往返。实测同一形态进批量后完全变样:

  | 形态 | 修前结果 |
  |---|---|
  | 单个倒序 `{6171→6162}` | 报错(可见,浪费一轮) |
  | 倒序 + 同文件另一区间 `{100→50}` + `{60,70}` | **静默返回 60-70**,零提示 |

  成因在 `_merge_same_file_ranges`:按 `(start, end)` 排序后 `(100,50)` 落到 `(60,70)` 之后,`100 <= 70+40`(GAP_THRESHOLD)于是被并进去、`end=max(70,50)=70`。**倒序那条整个蒸发,模型收到一份干净的、它没要过的行。** 这比报错糟——报错至少让模型知道要重试。
- **落点(单一真源 + 顺序是承重的):** 新增 `_normalize_line_range()`,在 `tool_read_files` 漏斗上、**`_merge_same_file_ranges` 之前**调用。放在合并之后只能修可见那半(NEUTER-2 精确咬 1 条证明)。展示层 `project_tool_display` 复用**同一个** helper,避免第二份手写判定(charter #24 同族),否则工具行会显示 `L6171-6162` 而实际读的是 `6162-6171`。
- **★ 刻意只修倒序,不碰越界——这是取舍不是遗漏:** 倒序**无歧义**(只有一个区间可解),越界(9000 行文件要 20000-20100)**是真错**,swap 救不了它,clamp 进合法范围等于把一个错误请求伪装成成功。NEUTER-3b 正是照着「过度修复」造的:把越界 start clamp 到 1 → 返回整个文件 9000 行、**2 条补集守卫开火**。
- **★ 首发 NEUTER-3 完全不咬,而错在我的靶子:** 我把「naive 修法」写成 `sorted((start,end))`,但 `sorted` 对已正序区间是**恒等**——它与我的实现行为完全一致,等于拿稻草人当靶子跑了一发空转。**判据:NEUTER 必须造出与正确实现行为真正分岔的变体,否则「不咬」证明不了任何事,只证明我造错了靶子。** 重打的 3b/4b 才分别咬到过度修复与半修两个方向。
- **NEUTER×6 各咬各的:** ①helper 退化为恒等 → **8 红**;②归一化挪到 merge 之后 → 1 红(静默那半);③b 越界 clamp 进范围 → 2 红(补集);④删掉修正提示 → 2 红;⑤b 只修单条、批量放行 → 1 红(实测输出 `6171-6185`,**正是修前的原病灶**);⑥展示层退回原始区间 → 1 红。
- **修正保持可见:** 结果头部加一行 `[Note] read_files: reversed line range(s) auto-corrected — …`,并 `logger.info` 记一条。**不做静默改写**——本项目反复吃过「功能可用但没有痕迹」的亏;一行提示让模型知道自己的调用是畸形的,又不打断它拿结果。
- **共享 HEAD 事故一起(已恢复,无工作丢失):** `git commit -- <pathspec>` 连报两次 `did not match any file(s) known to git`——查证是兄弟会话在我 `add` 与 `commit` 之间**反复重置 index**(暂存区从我的 3 项变成兄弟的 17 项、再变 23 项),新建文件的 index 条目每次被清掉。工作树内容始终完好(AST + grep 逐文件核实)。**判据:共享 HEAD 上 `add` 与 `commit` 必须在同一次 shell 调用内完成**,把竞态窗口关掉;分两次调用必然输给活跃的兄弟。提交后复核:本次只含我的 3 文件,兄弟 82 项改动原样未动。
- **相邻噪声一条已定责非我方:** `test_project_tools.py::TestCleanCommandOutput::test_multi_device_startup_collapsed` 红——在**干净 HEAD worktree 上同样红**,属 `clean_command_output` 折叠逻辑的既有失败,与本批无关。收集门 **12,474 tests / 0 err**。
- **验收边界(诚实分账):** 纯后端工具层,**运行中进程不带**——重启后新会话的 `read_files` 才走修复。

### 2026-07-29(续·科研产物读路径) — owner 抓出我**刚修完孤儿又造了一个孤儿**:产物落盘了,但 `load_research_artifacts` 全库零调用方;修法不是补一个函数,是把「按方向查」做成 tasks 端点结构上做不到的那条路(commit `ef09c6a5`,4 文件 +366/-1;新套件 **9/9 失败先行**,**NEUTER×4 各咬各的**;相邻环 **42/42**;干净 committed worktree 复验 **42/42**)

- **★ 同一形态第三次出现,而这次是我造的:** 上一批我写进 JOURNAL 的判据是「函数被 export 进 facade 不等于它被用了,要数调用方」。然后我自己交付了 `load_research_artifacts`——写好、export、**零调用方**。owner 的话是对的:产物安全地躺在 `paper_reports` 里,而产品里没有一条路径能走到它,等于把东西换个地方埋起来。**判据升级:每加一个函数,配套守卫必须断言它有真实调用方且链路端到端通,而不是断言符号存在。**
- **为什么不能复用 `GET /api/v1/tasks/<id>`(这是本批的设计核心,不是新造轮子):** ①它按 **task id** 寻址,而落盘行按**方向哈希**寻址、根本不带 task id;②它解析的是**进程内** TaskRuntime 注册表,`cleanup_stale()` 一扫或进程一重启就 404——**正是持久化要覆盖的那个窗口**。所以两者互补:活任务看进度/中止走 tasks,已完成的工作任何时候回看走 `/api/v1/research/lookup`。
- **`found:false` 定为正常 200 而非 404:** 重贴附路径每次打开都会调它,「这个方向没研究过」是**信息不是错误**;若返回 404,前端就得把「真故障」和「没研究过」在同一个错误分支里猜。
- **★ 守卫的决定性用例先清空 task 注册表再发 HTTP:** 因此只有产物**真从磁盘服务**才可能通过——mock 引擎或裸符号 import 都满足不了。另加两条反孤儿钉:路由模块必须真调 `load_research_artifacts`(走 `tests/_source_scan.strip_comments(lang='python')` 剥注释,charter #24,免得注释里出现该名字就算数)、`api.js` 必须真暴露该路径。NEUTER×4:摘蓝图注册 / 改成读内存注册表 / 丢 degraded 标志 / 摘 api.js 方法,四发分别咬中四条不同断言。
- **★ 一个会让守卫「绿着空转」的坑,自己踩到并修掉:** 我最初用 `@pytest.mark.asyncio` 写 7 条异步用例。本套件跑在 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 下,**pytest-asyncio 根本没加载**,该标记只会让用例静默跳过——`PytestUnknownMarkWarning` 淹在警告堆里,而摘要显示「passed」。改用 `asyncio.new_event_loop()` 显式驱动(照 `tests/test_agent_poll_routes.py:46` 既有惯例)。**判据:在禁用插件自动加载的套件里,`@pytest.mark.asyncio` 等于把测试关掉;异步路由守卫一律显式起 loop。**
- **共享 HEAD 两起(均只动 index,兄弟工作树零影响):** ①`git add` 后计数断言报 9 个文件——兄弟把 4 个 `static/icons/skins/*.svg` + 1 个品牌守卫预暂存在共享 index;②执行 `git reset HEAD <他们的文件>` 后**再次断言仍漂移**,这次多出 5 个 `lib/motion_video/*`——**兄弟在我两条命令之间又写了 index**。结论:共享 index 上「reset 再 commit」本身也是竞态。改用 `git commit -F msg -o <我的4个文件>`(`-o/--only`)**绕过 index 直接按 pathspec 提交**,一步落地;实测 HEAD 恰好 4 文件。**判据:共享 HEAD 上提交已跟踪+新增混合文件集,`-o` 比「add + 计数 + commit」更安全,因为它不依赖 index 在两条命令之间保持不变。**
- **相邻噪声一条已定责非我:** `lib/project_mod/read_tools.py` 工作树版有兄弟在飞的 WIP 语法错(重复 `def` 行 + 裸中文破折号行),使 `import server` 在工作树里整树失败。核实 `git show HEAD:` 版本 `ast.parse` 干净 ⇒ 属未提交 WIP,我未碰该文件,验证全部在干净 worktree 上做。
- **验收边界(诚实分账):** ①**读路径通了,但前端仍然只显示三个数字**——结果面板(下一步)未做,这批只保证「数据可达」;②`Api.research.lookup` 已就位但**尚无 JS 调用方**,下一批画面板时接上(本批守卫只钉住 api.js 暴露了该路径,不谎称已被使用);③**运行中进程不带**,需重启;④入口移侧栏继续避让 `pt_3d487a30bd5e4a7e`。

### 2026-07-29(续·科研产物落盘) — owner 顶回我的排序:我打算先修前端渲染,但**产物根本没有持久层**,那样修出来的 re-attach 会忠实恢复出一个 404;根因是设计稿规定的持久化「建了一半、从未接线」(commit `749f3464`,3 文件 +619/-3;新套件 **18/18 失败先行**,**NEUTER×4 各咬各的**;相邻环 **40/40**;干净 committed worktree 复验 **28/28**)

- **★ 我的入口诊断是对的,但优先级是错的,而 owner 的反驳可实测:** 我查出「入口不是没有,是入口后面那块屏幕只显示三个数字」——`_public_task` 是整字典透传(只剔 locks/messages),`result.accepted[]`、带四轴分数的 `rejected[]`、`survey_md`、`open_gaps` **早就在线上**,是前端 `_researchApplySnapshot` 只取 `.length` 扔掉的;`research.js` 里 `localStorage` **0 次**。四条属实。但 owner 指出第五条:**这些产物压根没落盘。**
- **实测根因(比「前端少画卡片」深一层):** `lib/research/runtime.py:22` 是 `ProductionRuntime('research', ttl=7200)`,产物只在进程内 `_tasks` 字典;`cleanup_stale()` 按 `updated_at` 扫终态任务直接 `pop` ⇒ **跑完约 2 小时后产物永久消失,服务器一重启立刻全没**。而同一 paper mode 的报告/评审/播客/insight 全部落 `paper_reports` 永久可取。
- **★ 最刺眼的一处:持久化被规定了、建了一半、从未接线。** 设计稿 §5 明写综述/空白地图/创新点走 `paper_reports` 复合 lang 键、「一行新 schema 都不用加」。实测 `survey_lang_key`(survey.py:78)与 `ideate_lang_key`(ideate.py:294)**已写好、已 export、已进 `lib/paper/__init__.py` facade**,docstring 甚至写着「lets a survey persist」——但 `grep` 全库仅 8 处命中,**全是定义与再导出,零调用方**;而同族 `insight_lang_key` 有 **19 处**含真实 `_persist_insight` 写入路径。**判据:一个函数被 export 进 facade 不等于它被用了;要防这类「半成品」,得数调用方,不能数定义。**
- **落点(复用唯一写路径,不造第二个 writer):** 新增 `lib/research/persistence.py`,走 insight 同款 `upsert(db, PAPER_REPORTS, …)`。三处设计判断:①**方向身份要归一**——方向是人手打的自由文本,`  KV Cache ` 与 `kv cache` 必须命中同一行,否则一次 miss = 整条 harvest→survey→ideate 按全价重跑;②**要命名空间前缀**——方向不是论文,不加前缀则一个文本恰好等于某论文正文时会写到那篇论文的报告行上;③**结构化真源放 `meta`**(survey 的 `open_gaps` 是 R3 机读契约、ideate 的 rejection audit 是 `IDEATE_GATE_THRESHOLD` 的校准数据),`report` 只存人读正文,**digest 明确标注不被任何 reader 反解析**,避免第二真源。
- **★ 失败姿态是刻意的:** 一次 ideate 花掉多次 LLM 调用,所以 DB 写失败只记 ERROR + 返回 `False`,**产物照常回到调用方**;守卫里专门有一条 patch `_upsert_row` 抛异常、断言 stage 仍返回完整 artifact。存储问题不许毁掉已经做完的昂贵工作。
- **★ 守卫判据按 owner 要求写成「TTL 逐出 + 重启后仍可取」,而不是「写入函数被调用过」:** 后者 mock 也能满足。每个用例写**真 SQLite**,然后 `_wipe_runtime()` **清空整个 `_research_runtime._tasks`**(与 `cleanup_stale` 同效、与重启同效)再读回。NEUTER×4 各咬各的:摘 ideate 接线 → `test_ideate_stage_persists` 红;摘 survey 接线 → `test_survey_stage_persists` 红;去命名空间 → 碰撞守卫红;去归一化 → 身份守卫红。四发方向互不相同。
- **共享 HEAD 事故一起(已恢复,无损失):** `git add` 成功、计数断言通过后,下一条命令里 index **变空**——兄弟会话的 `git reset` 清了共享 index。按 charter #15 先只读定责(`git log` / `git ls-files` / `ls`),确认**三个文件在磁盘上原封不动、recipe.py 接线仍在(grep 计数 4)**,仅 index 被清,遂重新 `add`+计数断言+提交合并为一条命令减小窗口。**判据:共享 index 上「add 与 commit 之间」是一个真实竞态窗口,两步之间不要插入任何其他命令。** 另:新文件无法用 `git commit -- <pathspec>` 形式(pathspec 只认已跟踪文件),必须提交 index。
- **验收边界(诚实分账):** ①本批**纯后端**,前端仍然只显示三个数字——结果面板(第 2 步)与 re-attach(第 3 步)未做,但它们现在有了可依赖的地基;②**运行中进程不带**,需重启才生效;③持久化**从本批起的新任务生效**,此前已被 TTL 扫掉的历史产物**无法追回**(数据已不存在,不是接口问题);④入口移侧栏(第 4 条)按 owner 指令**本批不动**,`pt_b5cb0e2dff634bf7` 正在改侧栏,避让。

### 2026-07-29(续·美团双模板合一) — owner 问「为什么这么多美团模板,合成一个」;实测**它们不是重复而是同网关的两条协议线**,但 owner 推翻了兄弟会话 ms4lpqzs9nd6xb 的「不合并」结论并要求根修。落点是 **account/face 分离**:`protocol`+`base_url` 从 provider 层下沉为按模型解析(六提交 `67a9bfb7`/`c3657d73`/`22e6003a`/`f1df3e61`/`9cbcc8f1`/`3f8e1510`;新套件 **6 套 47 条**,**NEUTER×11 全咬**;干净 HEAD **149 passed**;真实配置端到端实证)

- **★ 根因命名(这决定了修法):** 旧 schema 把两件事塞进一个 `provider`——**账户**(api_keys/extra_headers/计费/配额,基数 1)与**接口面**(base_url+protocol,基数**可以是 N**)。美团网关是 1 账户 + 2 面(实测两卡 3 把密钥前缀与 `extra_headers` **完全相同**),而 schema 强迫 N=1,于是只能复制整个账户。**两张卡不是设计选择,是唯一能写出来的形态。**
- **★ 已在偿还利息(实测,非推演):** 干净 HEAD 上 `test_meituan_marketplace_models.py` **是红的**——`claude-fable-5: missing from template`。`git log` 定位到正是今天的拆分提交 `90202d96`:它把 Claude 搬出 `meituan.json` 时带走了 fable-5,而另一套守卫要求 fable-5 必须在里面。**两套守卫互相矛盾。** 合并后 11/11 自然转绿——这是合并有效性最好的证明,不是靠改断言。
- **落点:** provider 声明 `faces{}`,`resolve_face()` 三条规则解析(①显式 `face` 逃生舱 → ②`is_claude` 且有 anthropic 面 **自动选它** → ③默认面)。第 2 条是价值所在:**签名丢失从「靠守卫拦手滑」变成结构上不可能**,未来 opus-6/fable-6 加进来当天就是对的,不需要任何人记得改配置。
- **★ owner 抓出我方案的洞(比我自查强一档):** 实测 `_syncFromTemplate` 只写 `model_id/request_ids/capabilities/cost/aliases`,**从不写 provider 级字段**;而它的 `added++` 分支会把模板里有、卡片里没有的模型直接加进去。⇒ 合并模板带 6 个 Claude + 老卡片(无 faces)点一次同步 = 6 个 Claude 落到 openai 面**静默丢签名**,与 `90202d96` 刚修的同形,只是换了扇门。故规则 2 **不回落而是 fail-loud**(不建 slot + ERROR + 卡片告警),且 sync/apply 两条模板路径都补带 `faces`。
- **★ 迁移必须是代码不是手改(owner 否掉我的备份改配置方案):** 手改只修一台;别人的 `sankuai_anthropic` 在模板删除后 `_findMatchingTemplate`(按 base_url 精确匹配)再也匹配不到,那张卡变孤儿、"从模板同步"静默失效。照 `dispatcher.py:211 _migrate_provider_extra_headers` 先例写加载时迁移 + `update_json_atomic` 持久化。合并判据是**同 host **且** 同密钥集合**——同网关两个租户绝不能合(会串계费/配额/密钥健康史);密钥**顺序**不是身份,**真子集**是不同账户。
- **★ 我自己造出一条结构性瞎守卫,被 NEUTER 而非评审抓出:** 探测那批的首版测试**自己重新实现了一遍 work-list 循环**,于是 NEUTER I(路由层不再按 cell 解析)**仍然全绿**——它断言的是自己那份副本,不是线上路径。这正是本项目反复踩的「守卫看不见它要防的缺陷」形态。根修=把构建提取成 `build_probe_work()` 真缝,路由与测试共用;改完 NEUTER I 才真红(2 条)。另自纠一处:首版断言探测看到 wire id(`yuju-…`),实测 work list 走 `[model_id]+aliases` 即**逻辑 id**——是我的预期错,改期望而非改产品。
- **★ CSS 断言不是装饰(NEUTER K 证明):** 告警条只断言 markup 会在**零样式**下照样绿——「守卫绿/用户看不见」。故直接断言 shipped 样式表里选择器存在**且**有 border+background;删掉 CSS 块 → 真红。
- **验收(owner 五条,逐条实测):** ①`static/provider_templates/` 只剩 `meituan.json`(43 模型),`brand` 从 `claude` 修回 `meituan`(旧值会让整卡掉进 Claude 分组,破坏 `model_group.js` 的收敛);②真解析器跑合并模板:**6 Claude → anthropic 面 / 37 → openai 面 / 0 拒绝**;③无 faces + Claude → fail-loud,NEUTER 真咬;④真实 `server_config.json` 经**加载时迁移代码**:`['sankuai','sankuai_anthropic']` → `['sankuai']`,同一 provider_id 产出 **11 个 anthropic slot + 57 个 openai slot**、共用同 3 把密钥、零拒绝;⑤干净 HEAD **149 passed / 5 skipped**,含既有红转绿。
- **共享 HEAD 事故两起(均已恢复,无工作丢失):** ①`git add` 后计数对但**内容**错——兄弟的 `EDGE_ADDONS.md` 挤进来而我的模板删除掉出,故改为**集合精确断言**而非计数;②提交瞬间兄弟 reset 清空 index,commit 报「no changes added」。实测工作树内容完好(grep 逐文件确认),重新暂存并改用 `git commit -F 文件 -- <显式 pathspec>` 一次落地。**判据:计数断言不够,要断言集合;提交要带 pathspec。**
- **验收边界(诚实分账):** ①后端半**需重启**才对新会话生效;②前端需**刷新**,且新模块进 bundle 需重建;③迁移在**下次加载时**才写盘——本轮只做了内存演练与备份(`/tmp/sc_pre_migration.json`),没有手改 owner 的线上配置;④合并后 Claude 的 `provider_id` 由 `sankuai_anthropic` 变 `sankuai`,**按 provider_id 存的密钥健康史与探测缓存会重置一次**(owner 已知悉并接受——两边密钥物理相同,合并后统计反而更准)。
### 2026-07-29(续·孤儿 tag 锚错 commit) — 判据修对了,但**它救回来的那个 release 会把新二进制挂到旧源码上**:`target_commitish` 对已存在的 tag 是死参数(owner 复核 `5114cbca` 抓出;守卫 **17 → 25**,**NEUTER×4 各咬各的**,相邻环 **90/90**)

- **★ owner 的判据(我把一件危险的事当成了好消息):** 我在上一批写「orphan tag 会被 action-gh-release 采纳并补发」,并据此认为无需特殊处理。三环拼起来结论正好相反:①`export.py:3059 _push_tag` 明写 "never silently MOVES a published tag",不带 `--force` 时**保留远端旧 tag**;②`findTagFromReleases` 扫的是**已有 Release 列表**,orphan tag 找不到匹配 → 走**创建**分支;③而 GitHub REST 文档对 `target_commitish` 的原话是 **"Unused if the Git tag already exists."** ⇒ Release 挂到旧 tag 所指的那个 commit 上。
- **净效果(比不发版更难查):** 下一次 push 发出来的 v0.15.2,**安装包来自 `github.sha`(新树),而 Source code (zip/tar.gz) 与 `generate_release_notes` 来自旧 commit**,再被 `make_latest: true` 推给所有用户。**全程零报错。** 实测确证该状态就是下一次 push 会落进的状态:远端 v0.15.0 / v0.15.2 有 tag、`GET /releases/tags/` 双 404。
- **落点(在创建 Release 之前把 tag 移到真正构建的那个 commit):** `release` job 新增一步,位置在完整性闸之后、`action-gh-release` 之前 —— 全部平台产物齐备才动 tag。四种判决镜像 `_tag_push_action`:**不存在** → 交给 action 创建(此时 `target_commitish` 才真正生效);**已指向 `GITHUB_SHA`** → 什么都不做;**指向别处且证实无 Release** → force 移动;**其余** → 不动并告警。
- **★ owner 给的安全理由我实测发现不够,必须再收一道(这条是本批最关键的自查):** 票面写「`version` job 刚证明 404,所以是孤儿」。但 **`workflow_dispatch` 把 `should_release` 置 true 时根本不探测**(那是故意的重发逃生门)—— 于是**在已发布版本上手动重跑会走到这一步**,按票面写法就会 force 移动一个**已发布**的 tag,破坏每一个下游 pin。故该步**自己重探一次 Release API**,只移动**证实 404** 的 tag。**判据:「上游某个 job 已经检查过」不构成本步骤的授权,除非那条检查在本步骤所有可达路径上都必然发生。**
- **失败方向与 version 门相反,也是刻意的:** 那道门对不确定**倾向于构建**(多烧四台 runner 而已);这一步对不确定**倾向于不动 tag** —— 移动一个可能已发布的 tag 是破坏性且难撤销的。探测拿不到明确 404 就只告警。
- **★ 我第一版的 harness 自己在撒谎,而它报的是「产品有 bug」:** `test_a_tag_already_on_the_built_commit_is_left_alone` 首跑红,说本该静默的路径 force push 了。查明是 stub 用 Python `repr` 输出 ls-remote 夹具,**制表符被写成字面 `\t`**,`awk '{print $1}'` 看到的是**一个不可切分的字段** ⇒ 步骤里每一次 SHA 比较都无法匹配、直接掉进 push 分支。**判据:harness 里的夹具必须是真字节(写文件再 `cat`),用 `repr` 内联会把转义序列喂给被测 shell,坏的是量具而不是产品。**
- **顺带钉住一个只在真实数据上才会犯的错(修 harness 时才可测):** `export.py` 建的是**带注释的 tag**,它的裸 ref 指向 **tag 对象**而非 commit —— 不先用 `^{}` 剥,`--bump` 路径上**每一次运行都会 force 移动一个本来就正确的 tag**。守卫 `test_an_annotated_tag_is_compared_by_its_peeled_commit` 用「peeled=已构建 commit、tag 对象 SHA 不同」的夹具钉死;实测远端 v0.15.2 正是这种形态。
- **NEUTER×4 各咬各的方向:** ①删掉整步 → **8 红**;②授权换成 `should_release`(照票面写法)→ **精确 2 红**(已发布 + 探测不可读两条补集);③摘掉 `^{}` 剥离 → **精确 1 红**(注释 tag);④把该步移到 Release 之后 → **精确 1 红**(顺序)。
- **守卫形态:** 断言**该步真正发出的 git 命令**(stub `git`/`curl`、跑真 shell、读调用日志),不是断言 YAML 文本 —— 「移动孤儿 tag」与「移动任何 tag」在文本上几乎一样,而两者的差别就是一次被改写的已发布 tag。
- **两处措辞更正(owner 点名):** 远端**没有 v0.15.1**(实测 `git ls-remote --tags` 只有 v0.15.0 与 v0.15.2),工作流头部注释、`version` 门注释、守卫 docstring 与上一条日志里的同一句全部改准。
- **验收边界(诚实分账):** ①仍是下一次 push 到 main 才生效;②本批只动 workflow 与守卫,**无运行时代码,不需要重启**;③`workflow_dispatch` 在**已发布**版本上重跑仍会重建产物并更新那个 Release(既有逃生门语义),但**不会**再移动它的 tag。

### 2026-07-29(续·发版判据选错) — owner 用线上真实状态证伪我上一批的修复:**tag 是发版的产物,不是发版的证据**;而这个代理在两个方向上都错,其中一个让「不带 --bump 也能出包」这个卖点在最正式的那条路上恰好失效(commit `5114cbca`,2 文件 +240/-24;守卫 **17/17**,**NEUTER 咬 7 条**,相邻环 **82/82**,干净 worktree 复验 **17/17**)

- **★ owner 的证伪(线上实测,不是推断):** 我上一批用 `git ls-remote --tags` 回答「这个版本发过没有」。owner 指出这两件事在本仓**此刻正好是分裂的**——**v0.15.0 与 v0.15.2 都在远端有 tag,而两个都没有 Release**(实测 `GET /releases/tags/` 双 404;最后一个有 Release 的是 v0.14.2)。于是我的门对当前 `VERSION=0.15.2` 判定 `should_release=false`,**一个包都不会建**:修完之后,那些饿死的版本仍然永远发不出来,而这恰恰是 owner 最初报的那个现象。**判据:「修复」必须拿它要修的那个故障态当输入验一遍,否则它可能在自己的靶子上恰好无效。**
  - **★ 更正一句我没核过的测量(owner 复核抓出):** 我先前写「v0.15.0 / v0.15.1 / v0.15.2 三个 tag 都在远端」。实测 `git ls-remote --tags` **远端根本没有 v0.15.1**(它连 tag 都没走到),只有 v0.15.0 与 v0.15.2。工作流注释、守卫 docstring 与本条目里的同一句已一并改掉。**判据:引用具体清单前必须真的跑那条命令,「三个」这种数字最容易顺手写错并被后人当事实继承。**
- **★ 第二个方向更要命,它让本批的卖点在最正式的路径上反向失效:** `export.py::_git_push` 里 `_push_branch` 与 `_push_tag` 在**同一个循环体内背靠背执行**,中间没有任何等待。所以 `--bump` 那条路上,tag 与分支几乎同时到达远端——workflow 被分支推送唤起、`version` job 跑 `ls-remote` 时 tag **大概率已经在了**,于是它判自己「已发布」直接躺下。而不带 `--bump` 时没有 tag、反而能建。**方向正好反了:越正式的发布路径越建不出来。** 这不是理论竞态,是 `--bump` 路径的默认结果。
- **落点(问真正关心的那件事,不用等待或重试去躲竞态):** 判据换成 `GET /repos/{owner}/{repo}/releases/tags/v$VER`——200 已发布跳过,404 未发布就建。tag 早到、tag 由 `--bump` 先推、tag 是历史遗留,**三种情形全部不影响判定**,那两个 orphan tag 会在下一次 push 时自动补发。
- **★ 为什么不用 `gh release view`(这条决定了实现形态):** 它对「没有这个 release」与「API 调用失败」**都返回 exit 1**,且该映射在 cli/cli#6024 里被明确记为**未文档化行为**。用它做判据**在结构上无法表达 fail-open 规则**——限流会被读成「已发布」而静默跳过发版。故走 REST 拿显式 HTTP 码。
- **失败方向刻意不对称:** 任何不是明确 200 的结果都建(限流 / 403 / 5xx / 传输失败)。多建一次的代价是四台 runner;漏建一次是又一次静默不发版,**正是本轮在修的那个故障**。curl 上的 `|| true` 是必需的——`set -e` 会让一次网络抖动直接杀掉整个 step,连带杀掉本该发生的构建。
- **orphan tag 在发布侧无需特殊处理(查证而非假设):** `action-gh-release` 解析目标时扫的是**已有 Release 列表**里的 `tag_name`(`findTagFromReleases`),不是 git tag——所以它会**采纳**已存在的 tag 并在其上创建 Release,而不是报错。
- **★ 守卫从「断言文本」改成「执行真实 shell」,因为旧那条正是在给这个 bug 站岗:** 原 `test_an_already_released_version_does_not_rebuild` 断言 step body 里出现 `ls-remote` 与 `refs/tags/v` 两个字面量——**按 tag 判会让它永远绿**。现在拿 stub 的 `curl` 挂进 PATH、真跑那段 shell、读它真正写进 `$GITHUB_OUTPUT` 的 `should_release`。
- **★ 第一发 NEUTER 没咬,而原因暴露了我 harness 的一个洞:** 退回 tag 判据后,headline 的 orphan-tag 测试**照样绿**——因为临时目录里没有 origin,`ls-remote` 失败,坏判据**恰好掉进了「建」这个正确结果**。**判据:harness 必须把故障态的前提也建模出来**(此处 = tag 真的存在),否则坏实现会因为一个不相干的失败而偶然通过,整发 NEUTER 变成空转。补上 `git` stub 后重跑,**7 条红**含 headline 那条。
- **两条实证(owner 点名要的):** ①`VERSION=0.15.2` + tag 已存在 + 无 Release(线上实测 404)→ `should_release=true`;200 对照组 → `false`;403/429/500/502/传输失败 → 全部 `true`。②NEUTER 退回 tag 判据 → 7 条红,还原后逐字节一致、17/17 绿。
- **★ 一处自伤(同族第二次):** 新写的 shape 守卫禁止 step body 出现 `ls-remote` / `gh release view`,而**该 step 自己的注释正是在讲「为什么不用这两个」**——守卫当场把这段解释判红。改为**先剥注释再断言**。如果反过来去删注释,就等于让守卫吃掉那份防止复发的文档。
- **验收边界(诚实分账):** ①仍是**下一次 push 到 main 才生效**;但与上一批不同,这次 `VERSION=0.15.2` **不需要先 bump**——orphan tag 会被采纳并补发。②`macos-15-intel` 到 2027-08 的边界不变。③本批只动 workflow 与守卫,**不含运行时代码,不需要重启**。

### 2026-07-29(续·桌面版不出包 + 安装提示空盒子) — owner 报两条:「版本号变了不自动 build」与「不管装没装都该直接显示安装提示」。第一条**不是没触发,是触发了然后饿死**:GitHub 已退役的 runner 标签**不报错、只是永不调度**,排队满 24h 被自动取消,`release` 因 `needs` 被 skip(commit `d604418a`,8 文件 +881/-19;新套件 **9 + 15**,**NEUTER×3 + 物理 NEUTER 咬 8 条**;相邻环 **90/90**,既有 local-control **67/67**,干净 worktree 复验通过)

- **★ 第一条的根因是两个独立缺陷叠在同一个现象上,只修一个会以为没效果:**

  | 层 | 实测证据 | 性质 |
  |---|---|---|
  | ①`--push` 不带 `--bump` **根本不产生 tag** | `export.py` 里 `is_release=bool(args.bump)`,而 workflow 只认 `refs/tags/v*` | 自动化依赖「人记得加参数」 |
  | ②有 tag 时**构建饿死** | 真实 run **29632725079 / 29927622183 / 30001088220**(v0.15.0/1/2):`macOS DMG (x86_64)` 的 `runner=""`,排队**整整 24h** 后被 GitHub 自动取消 | 退役标签不报错,只是永不被调度 |

- **★ owner 的表述与实测略有偏差,而偏差处正是关键:** 他说「不会自动触发 build」,实测**触发了**(`ev=push ref=v0.15.2`),但三个版本各自只构建 3/4 平台、**一个包都没发**,Releases 页一直停在 v0.14.2。原因是 `release` 有 `needs: build-macos`,矩阵里一条腿被取消 ⇒ 整个发布 **skipped**。**判据:`timeout-minutes: 30` 对此完全无效——它量的是执行时间,不是排队时间。**
- **`macos-13` 已于 2025-12-08 退役**(actions/runner-images#13046)。为何 v0.14.2(2026-07-16)还能成功?因为**引入 x86_64 矩阵的那个提交 `57de6f1d` 就在同一天 15:05**,赶在容量彻底枯竭前跑过一次;07-18 起再没成功过。顺带发现 **arm64 腿用的 `macos-14` 是同一颗定时炸弹**(2026-07-06 起进入弃用,2026-11-02 停止支持),故一并前移到 `macos-15`。
- **落点(按「让版本号本身成为信号」而非补参数):** 触发改为 `push: branches: [main]`,由 `VERSION` 决定要不要发——`v<VERSION>` 在远端还没有 tag ⇒ 构建并发布(手动 dispatch 永远构建,作为重建逃生门)。这样 `export.py --push` 无论有没有 `--bump` 都会出包,且**普通内容推送不会白烧四台 runner**。tag 由 `action-gh-release` 的 `tag_name` 自己建,两条入口(bump 推 tag / 分支推)收敛到同一个「每版本一次发布」。
- **★ 新守卫的设计判据(这才是防复发的那一半):** 「标签是否还有效」只有 GitHub 调度器知道,离线测不出;但**退役日期是提前几个月公布的**。所以守卫把日期作为数据钉住,并在 **EOL 前 90 天就变红**——把「等到某次发布悄悄没发生才发现」变成「还有时间轮换时的一条红测试」。已知死标签(macos-13/14)也留在表里,这样有人回退到它们时,报错会直接说出原因。
- **NEUTER×3 各咬各的:** 退回 `macos-13` → 退役守卫红;退回 tag-only 触发 → 分支触发守卫红;删掉 `tag_name` → 发布定位守卫红。
- **★ 第二条是本项目明令禁止的那一类:功能可用,但界面先给你一个空盒子。** 与 07-29「死控件根修」同一形态。实测三个失效面,而**只有第一个是 owner 看见的那个**:
  ①`openLocalControlModal` 先画界面再发两个异步请求,期间状态是 `local.checking`「正在检查…」、`#lcBrowserSetup` / `#lcDesktopSetup` **都是空 div**;
  ②两个 renderer 的错误分支都写 `setup.innerHTML = ''`——**后端一抖就把唯一可执行的步骤擦掉**,恰恰在最需要它的时候;
  ③`_lcRefresh` 开头 `if (typeof Api === 'undefined' …) return;` **静默早返回、完全不重绘**,于是「正在检查…」+ 空盒子会**永久停在那里**。
- **落点=「检测只负责升级,不负责让指引出现」:** 下载扩展 / 安装桌面版这两条**不需要任何后端知识**,所以直接写进 `index.html`(它在任何 JS 解析前就已送达,这是「第一帧」能成立的前提),再由 `_lcPaintFloor()` 在打开时同步重绘;错误分支改为回落到 floor 而非清空。下载那段 markup **只写一份**(`_lcBrowserDownload`)——floor 与检测出的 `download` 态本就是同一条指令,写两份必然漂移,而漂移的 floor 就是「第一眼给出错指引」。
- **★ 桌面版 floor 故意不带下载链接(这是取舍不是遗漏):** URL 来自后端 `UPDATE_REPO`(可用环境变量覆盖),在静态 HTML 里硬编码会让 fork 指向上游的 releases 页——**一个自信的错链接比「一句没有快捷方式的完整指令」更糟**。链接由 `_lcRenderDesktop` 稍后补上,并有守卫钉死 HTML 里不得出现 `href=`。
- **物理 NEUTER(比断言级更强):** 直接改真文件——摘掉 `_lcPaintFloor()` 调用 + 把两个 setup div 清空 → **8 条真守卫变红**(含「Api 缺失仍可用」「错误不清空」「按钮真能点」);还原一律用 `cp` 备份回写,**绝不用 `git checkout`**(charter #15:后者不区分「我要撤销的实验」与「我还没提交的成果」)。
- **★ 一处我自己造成的红,以及它暴露的 harness 契约:** 既有 `test_frontend_local_control_merge.py` 是**按函数名从生产源码里切片**的,不是整文件加载;我新增的 `_lcBrowserDownload` 不在 `_SHIPPED_SYMBOLS` 里,于是 24 条测试全炸在 `ReferenceError`。**这是我的漏改,不是既有失败**——把两个新符号加入切片表后 67/67 恢复。判据:**往这个文件加会被 renderer 调用的新函数,必须同步 `_SHIPPED_SYMBOLS`。**
- **charter #8 已兑现:** 新增两个 `window.*` 导出 ⇒ 重跑 `gen_frontend_globals.py` 并提交(顺带把兄弟已提交的 `applySectionRequirements` 补进这份陈旧的生成物——已核实该文件在 HEAD 中受跟踪,故非误收)。typecheck ratchet 3/3、i18n key 覆盖 8/8、三档导出存活全绿。
- **验收边界(诚实分账):** ①workflow 改动**下一次 push 到 main 才生效,无法追溯补发 v0.15.x**;`VERSION` 现为 0.15.2 且该 tag 已存在,故下次发版需**先 bump VERSION**(或手动 dispatch 用可用 runner 重建 0.15.2)。②`macos-15-intel` 是**最后一代 Intel 镜像(到 2027-08)**,之后 x86_64 macOS 必须放弃或迁出 GitHub 托管 runner。③前端半边是静态 HTML + JS:**刷新即可**,但 bundle 后台重建,新 JS 要**第二次加载**才拿到。④相邻噪声一条已定责非我方:`ci.yml` 的 CI 在每次 main 推送上都是红的(测试层面,与本批无关),本次**未**让发布依赖 CI,故不阻塞出包。

### 2026-07-29(续·成本彻查修复) — 记账双计根修落地,**而 owner 抓出的第二半比第一半更重:两个成本面早已漂移 2.246 倍,两个模块的 docstring 各自宣称「can NEVER drift」**(commit `ebfd5464`,3 文件 +295/-3;新守卫 **9 条**,**NEUTER×3 各咬各的**(其中**一发首版完全不咬,暴露出真实调用点零覆盖**);干净 committed worktree **104/104**,收集门 **12,234 / 0 err**)

- **★ owner 用同一组数字分别驱动两条真路,证伪了我报告里「钱包与显示共用同一引擎」那句话:**

  | 面 | 结果 | 推出的未缓存 input |
  |---|---|---|
  | 显示 `compute_cost`(原始 dict) | **$48.56** | 5,562,791 |
  | 钱包 `compute_request_cost`(标量) | **$21.62** | 174,983 |
  | 真值(网关自己给的 `input_tokens`) | ~$21.6 | **162,854** |

  也就是说**钱包那条路基本是对的,显示这条才是错的** —— 而我把两者当成同一个引擎汇报了。成因:`compute_request_cost` 走 `synthesize_usage`,它用算术规则 `cr+cw > inp` **重新判定** convention,`5,387,808 < 5,562,791` 于是正确读成 OpenAI 总量;`compute_cost` 拿原始 dict 时被 Anthropic 键抢先判成 residual。**两处 docstring 都写着「can NEVER drift」,实测在生产载荷上是假的** —— 与 charter 已记的「注释里的假保证」同族。
- **落点(charter「单一真源」):修在 `split_input_tokens`,它的注释早已自称是 "the SINGLE source of this decision"。** 新增 `_anthropic_residual_input()`:判为 anthropic 时残差**必须**取 `input_tokens`,`prompt_tokens` 只作为「无原生键」那种反向混合形态的兜底。**刻意不动 `normalize_usage` 的全局别名顺序** —— 实测它有 **~28 个消费者**,而该顺序对每一种非混合载荷都是对的;改它等于用一个更大的洞换一个小洞。
- **第二处落点是让钱包停止二次猜测:** `request_flow.py` 改为在缝上解析好残差再交标量,而不是把富 dict 拆成标量让 `synthesize_usage` 重猜一遍。这是根因层修法 —— 只要「convention 判定」还发生在两个地方,它们就会再次漂移。
- **实测结果(单会话 + 全库):**

  | 量 | 修前 | 修后 |
  |---|---|---|
  | `inputTokens` | 5,562,791 | **162,854** |
  | `totalInputTokens` | 10,950,599 | **5,550,662**(缓存不再计两遍) |
  | `costCny` | 351.60 | **156.12** |
  | `inputCostCny` | 201.37 | **5.90** |
  | 显示 vs 钱包 | $48.56 vs $21.62(**2.246×**) | **$21.5642 vs $21.5642** |
  | 全库(2,868 轮 / 29 会话) | 51,308 CNY | **15,620 CNY**(−35,688) |

- **★ NEUTER-3 首版完全不咬,而它暴露的洞比我修的那个更隐蔽:** 把 `request_flow` 的调用点退回 `_nu['input']`(即生产缺陷原样),`tests/test_billing.py` **22/22 全绿**。原因:**既有每一条 billing 守卫都用手写标量驱动 `compute_request_cost`,没有任何一条走真实调用点** —— 这正是本项目反复吃过的「测了 helper 不等于测了接线」。补 AST 接线守卫(断言真实 `Call` 节点的 `input_tokens` 实参不得是 `normalize_usage()['input']`,且模块内真的调用 `split_input_tokens`)后,NEUTER-3b **精确咬 1 条**。**判据:一发不咬的 NEUTER 不是「守卫多余」,而是「这条路径根本没人测」。**
- **★ 夹具盲区的形状很具体,已写进测试文件头防复发:** `_anthropic_wire()` **刻意不含 `prompt_tokens`**,所以本文件里每一条 Anthropic 断言都跑在**纯**载荷上,真实网关形态从未进过任何守卫的扫描面 —— 这就是 72% 全库虚高能一路绿着的原因。既有 `test_hybrid_payload_with_impossible_cache_reads_as_residual` 只覆盖**反向**算术形态(cache > prompt_tokens),**结构上到不了**本缺陷。新 `_hybrid_gateway_wire()` 同时带两套拼写,并配一条「守卫的守卫」断言它**真的**是混合且满足实测恒等式 —— 否则有人把夹具「简化」成纯 Anthropic dict,底下每条断言仍会全绿而什么都没测。
- **PARITY 守卫必须驱动两条真路,且不能只断言「一致」:** 一致可以被「两边一起错」满足,故同时断言**两者都等于按网关自己的残差算出的真值**;另加一条绝对地板(该轮必须 <200 CNY)。
- **NEUTER×3 各咬各的方向:** ①退回 convention 解析 → 3 红;②**naive 修法**(无条件优先 `input_tokens`)→ **5 红**,其中我的 OpenAI 补集精确开火 —— `_openai_wire()` 带着 `input_tokens: 0` 这个残留字段,naive 修法会把每一个 OpenAI 轮的未缓存 input 解析成 0 并**少收费**,**比现状更糟**;③见上。
- **★ 顺带实测出第三份 convention 拷贝,按 owner 惯例单开票不并入(`pt_85887a5b2f2d416f`):** `lib/cost_estimator.py:63` 的 `_split_tokens` 是**独立第三份**实现,用的正是 `usage_cache_convention` docstring 点名为「latent 10x BILLING BUG」的幅度启发式。实测:①全命中(prompt=82843/cached=82843)返回 uncached=**82843 应为 0** —— **10.4x 悬崖在这里原样存活**;②混合线返回 174,983 应为 162,854;③margin-of-one 正确(只在边界一侧错)。它喂 `check_budget` → `orchestrator/_run.py:872` 每轮预算闸,**即一个从未发生的成本可以中止任务**。既有 `TestSplitTokens` 只有 4 例且**从不触碰相等边界与混合形态**,所以悬崖绿着。
- **验收边界(诚实分账):** ①**纯后端,运行中进程不带**(merged ≠ live)—— 重启后新会话的显示成本才走修复;**已落库的历史 `cost` 字段是当时算错的快照,本批未回填**(回填需单独决策:它会改写用户看过的数字);②`pt_7b850a669a074cec`(缓存真洗空,113 CNY)未动 —— 其中 12 轮需网关方带 `trace_id` 确认,不属我方可修;③共享 HEAD 纪律:精确 3 文件 pathspec + `git add` 后计数断言(=3),18 项兄弟 WIP 全部未被触碰(提交后逐项复核 `M` 列表原样)。

### 2026-07-29(续·成本彻查) — owner 报「5M tokens 花了 350 元」:**其中 195 元根本没花出去,是显示口径把「缓存含总量」当「未缓存残差」又计了一遍**;真实 156 元里又有 113 元是缓存真被洗空(纯诊断,零产品码;开票 `pt_28375442baa9487b` + `pt_7b850a669a074cec`)

- **★ 两个独立缺陷叠在同一个数字上,必须分账 —— 否则修完一个会以为没效果:**

  | 层 | 金额 | 性质 |
  |---|---|---|
  | 界面显示 | 351.60 CNY | 记录里的原值 |
  | 真实值 | 156.08 CNY | 按本项目价表重算 |
  | **虚高** | **195.52 CNY(55.6%)** | **纯记账缺陷,钱没花出去** |
  | 其中理想值 | 42.74 CNY | 若缓存正常命中 |
  | **真实浪费** | **113.34 CNY(占真实账单 73%)** | **缓存真被洗空,钱真花了** |

- **★ 缺陷一(记账双计)根因:「取值」与「判定」不同源,而 `normalize_usage` 的 XOR 假设被这个网关证伪。** `sankuai_anthropic` 发的是**混合载荷** —— 同时带 OpenAI 拼写 `prompt_tokens`(缓存**含**总量)与 Anthropic 拼写 `cache_*_input_tokens`(残差语义)。`lib/cost.py` 两半各自为政:
  - `_USAGE_KEY_ALIASES['input'] = ('prompt_tokens','input_tokens')` —— **先取到总量** 5,562,791;
  - `usage_cache_convention` 因 Anthropic 键在场,第一个分支就返回 `'anthropic'`(= input 是未缓存残差);
  - 于是 `split_input_tokens` 返回 `(总量, 总量+read+write)`,**把整个前缀按未缓存价再计一遍**,`totalInputTokens` 也虚报成 10,950,599。

  而 `normalize_usage` 的 docstring 明写「carries ONE convention only (OpenAI keys **XOR** Anthropic keys)… **the fallback order is immaterial**」——**这个 XOR 前提对该网关不成立,别名顺序因此从「无关」变成「决定性」。**
- **实测(不是推断):** 真实未缓存只有 **162,854 tok**,却按 **5,562,791 tok** 计价 —— input 单项应 5.90 CNY 变成 **201.37 CNY**;把该 usage dict 原样喂 `compute_cost` **精确复现记录里的 351.6024**。逐轮取证:**28/28 轮**满足 `input_tokens + cache_read + cache_write == prompt_tokens`,即 `prompt_tokens` 就是缓存含总量,无一例外。
- **★ 全库血径(比单会话严重):** 扫 470 个会话 → **27 个含该形态、2,740 轮命中、非混合形态 0 轮** —— 也就是**该网关每一轮 Anthropic 原生缓存请求都中**。显示总额 **51,308 CNY** vs 真实 **14,356 CNY**,**虚高 36,952 CNY = 界面所报的 72%**。
- **既有守卫为何放行(盲区形状很具体):** `tests/test_cache_accounting_convention.py` 有一条 `test_hybrid_payload_with_impossible_cache_reads_as_residual`,但它只覆盖 `cache > prompt_tokens`(算术不可能)那个**反向**形态;而本例是 `cache < prompt_tokens` 的**正向**混合。根子在夹具:`_anthropic_wire()` 构造的是**纯** Anthropic dict、**不含 `prompt_tokens`** —— 真实网关形态从未进过测试。**判据:夹具漏掉一个键,就等于把一整类载荷排除在扫描面之外,而扫描面不会自己报错。**
- **缺陷二(缓存真被洗空)是真花掉的那 113 元:** 15/28 轮 `cache_read=0` 且 `cache_write>20k`,累计**白写 2,732,435 tok**;按命中轮实测的 delta 写入比(均值 4.7% / 中位 2.6%)反事实计算,理想只需 42.74 CNY。写入按 1.25× 计价、读取 0.10×,**写比读贵 12.5 倍**,所以「写多于读」(2.97M > 2.42M)本身就是病征。
- **★ 成因分账(两条独立,禁止混在一个验收里):**
  - **3 轮**判 `cache_write_unsettled`(R2/R27/R28,间隔 11.8s / 11.2s / 14.8s)。`lib/llm_dispatch/cache_settle.py` 的 `settle_cold_enabled()` **默认 OFF**(实测本机无任何 `TOFU_CACHE_*` 环境变量),冷写后下一轮只等 **1500ms** 短窗,而冷条目可见性需 **15–20s** —— **短窗比所需窗口小一个数量级,必然穿透**。该默认值当初的理由写在 docstring 里:「a cold tool loop wastes only ~14k rewrite tokens (a fraction of a cent)」——**本例单会话白写 273 万 tok,这个前提已被证伪**。
  - **12 轮**判 `server_side` / `no_cache_reuse`,而它们的间隔达 **12s–76s**,远超任何可见性窗口 ⇒ 属**网关侧未复用**,`cache_settle` 修不了。
- **★ 我的「两条前缀交替」假说被自己的探针证伪(记下来防再犯):** 命中轮的 read 来源在 R14↔R19 之间跳了 5 轮,看起来像两条前缀交替互相踩。但实测 28 轮的 `_wire_region.system` 与 `tools` 哈希**全部相同**、`_wire_bytes` 块数**单调 1→56** ⇒ 前缀严格 append-only,只有一条谱系。**判据:「读回来的不是上一轮写的」不足以推出「有两条前缀」——先看 system/tools 哈希与块数单调性,那两个量直接否掉整类假说。**
- **本部署是显示口径失真,不是真扣款:** `compute_request_cost`(钱包)与 `compute_cost`(显示)共用同一引擎,但 relay 计费由 `billing_enabled()` 闸住、只在多用户中继生效。**故本机 owner 看到的 350 是估算被放大,不是余额被多扣;但开了 relay 计费的部署会真的多扣。**
- **验收边界(诚实分账):** 本批**纯诊断,零产品码** —— 两个缺陷的修法都涉及成本引擎/退避默认值的设计取舍,已按 owner 惯例分别开票(`pt_28375442baa9487b` 记账双计、`pt_7b850a669a074cec` 缓存洗空),等拍板后再动手。另:12 轮 `server_side` 需带 `trace_id` 找网关方确认是 per-request miss 还是 TTL 边界,**不属我方可修范围**。

### 2026-07-29(续·同一形态第三次) — owner 在**干净 committed 树**上抓出我这批引进的回归:我上一轮刚修好「注释被当成真调用」,却只修了两份实现中的一份(commit `69c727f8`,3 文件;三套 **56/56**,**NEUTER×2 双向**;干净 worktree **56/56**;方法论已进 charter)

- **★ owner 的判据(比「又一个 bug」重):** 这是**同一形态第三次复发**——①QR 批次的假 `t()` harness;②上一批 f4112121 的 ratchet 误报;③本次。前两次我都当成单点修掉了,没把它变成写这类守卫的**固定前置**。
- **回归形态:** 我在 `install.sh` 新写的两行注释里放了恢复命令 `python -m playwright install chromium`(**全量构建,故意的**),而 `test_chromium_binary_resolution.py` 的扫描器**不剔注释**,于是把它当成「安装器还在拉 175MB」告警。**工作树是绿的,干净 committed 树才红**——我只跑了 owner 上一轮点名的两套,恰好漏掉唯一会红的那套;而我改的是 `install.sh`,它正是第三套明确扫描的文件。
- **★ 根因不是注释,是判定逻辑有两份手写实现:** 我上一轮在 `test_install_uv_fastpath.py` 里修好了「先剔注释」,却把 `test_chromium_binary_resolution.py` 里的**第二份正则**原样留着。**两份拷贝必然漂移**——改一处、另一处继续漏,这正是 owner 点名的根因。
- **落点(去重而非再补一次):** 新建 `tests/_source_scan.py` 持有唯一定义——`strip_comments(text, lang)`(整行注释置空、**保留行数**以免打断调用方的行号运算)+ `playwright_install_invocations()`(剔注释 → 匹配真子命令形态,排除 `install-deps` 这个**不同的**子命令)。两条 ratchet 全部委派,均不保留本地正则。
- **边界是实测决定的,不是偷懒:** 只处理**整行注释**,不处理块注释/行尾注释——shell 的 `#` 可出现在引号内,**半吊子的解析器比不解析更糟**(会静默吞掉真代码,把守卫变成空转)。需要忽略行尾注释的守卫应改为匹配更具体的命令形态。
- **★ NEUTER 双向(去重最容易悄悄削弱守卫,必须两个方向都验):** **L** 去掉一处**真**调用的 `--only-shell` → **两条**守卫都红(证明去重没削弱任何一条);**M** 摘掉 `strip_comments` 调用 → 注释误报**精确复现**(证明是这一步在起作用)。扫描面同时从 **6 → 4**:两条注释里的恢复命令不再计入,4 条真调用全带 flag。
- **★ 方法论已固化(charter #24):** 「任何扫描源码文本的守卫 MUST 先剥注释」+「同一条判定禁止第二份手写实现」。双向判据写清:注释**不得满足**守卫(否则假绿空转),也**不得违反**守卫(否则假红,训练所有人忽略这个面)。
- **验收边界:** 纯测试基础设施改动,**无生产行为变更**;`--only-shell` 决策与三处论证均未再动。

### 2026-07-29(续·被证伪的前提与悬空引用) — 「结论对、论证错」的 ratchet 是危险的那一种:它永久强制一个决策,却拿一句实测为假的话当依据(owner 点名我漏做的第 4 项;commit `f4112121`,2 文件;两套 **39/39**,**NEUTER×2(咬/不恒红)**;干净 committed worktree **39/39**)

- **★ owner 的判据(比「注释过时」重一档):** `--only-shell` 的三处理由都写着「no headless=False / record_video / channel= call site exists」,而其中一处正是 `test_playwright_downloads_only_the_headless_shell` 这条**强制 `--only-shell` 的 ratchet 的论证依据**。下一个人想搞清「为什么只装 shell、代价是什么」,读到的是「没有任何 headed 调用点」——于是永远不会知道登录墙功能是这个决策的**已知代价**。论证错的 ratchet 迟早会被人用错误的理由推翻或扩大。
- **三处落点(全部改成实测事实 + 完整权衡):** `install.sh:427`(uv 路径)、`install.sh:1885`(conda 路径)、ratchet 的 docstring。现在都写明:全仓**恰好一个** headed 调用点(`tofu_search/fetch/interactive_login.py`,登录墙 cookie 捕获);`chrome-headless-shell` **架构上无 headed 模式**(独立二进制,非 flag);所以 `--only-shell` 是**「省 60% 下载,代价是该功能不可用」的交易而非免费的胜利**;该功能现由 `headed_chromium_executable()` 判定并以 `reason='headed_unavailable'` 诚实降级,需要它的用户跑 `python -m playwright install chromium`。
- **★ owner 另抓出的悬空引用(与前者同型):** 同一段 docstring 引用 `lib/motion_video/_env.py::_playwright_chrome_candidates` 作为「已接受 shell 二进制」的证据,**而那个函数在委派重构中已被我删除**(它成了死代码)。断言仍然全绿,而论证链指向一个不存在的符号——**结论可能成立,但陈述的理由无法被审计**,和陈旧前提是同一种失效。已重锚到活路径(`chromium_env.chromium_binaries`,`chrome_bin` 委派它)。
- **把这一类钉住而非只修这一处:** 新增 `test_docstrings_do_not_cite_deleted_symbols`——本文件任何 `module.py::symbol` 形式的引用都必须能**经 AST 解析到真实符号**。**NEUTER×2 双向验证:** 植入一条指向已删除 helper 的引用 → 精确报 `(symbol deleted)` 变红;植入一条**仍然有效**的引用 → 保持绿。既不空转,也不恒红。
- **★ 我在修的过程中自己造出一个同型缺陷(且是我上一轮修过的同一类):** 新写的 install.sh 注释里含恢复命令 `python -m playwright install chromium`(**全量构建,故意的**),`--only-shell` 的 ratchet 立刻把它当成真调用告警。根修不是改注释措辞,而是**让守卫先剔除注释行再扫真调用**——注释永远不该满足**或违反**守卫。这条原则本文件别处已在用,这次补齐。
- **★ 第二个自伤(守卫的自指陷阱):** 新守卫的 docstring 为了记录历史,**自己写下了那条已删除符号的引用**,于是它把自己判红了。改为**散文形式**提及(不构成可解析的 `module::symbol`),既保住历史记录又不违反自身规则。另:示例占位符 `path/to/mod.py::symbol` 也需排除,否则同样自指为红。
- **验收边界:** 纯注释/守卫改动,**无行为变更**;`--only-shell` 决策本身**未改**(owner 定调保留),改的只是它的论证与代价披露。

### 2026-07-29(续·第四份拷贝与 headed 前提) — owner 实测抓出两个洞:①我上一轮收编了 chromium_env docstring **点名在案的四份拷贝中的三份**,独漏用户最常撞的那份;②`--only-shell` 的决策依据「zero headless=False call sites」**是假的**(owner 复核;commits tofu-search `c61fa7d` + chatui `cb461f9d`;守卫 **17/17**,**NEUTER×4 各咬各的**(3/1/1/2);全环 **129/129**;干净 committed worktree **34/34**)

- **★ owner 的第一击(我的漏项,不是新缺陷):** `tofu_search/fetch/playwright_pool.py` 的自愈仍只认 `$CONDA_PREFIX`——本机实测该变量**为空**。洗净 env 走生产池:`LD_LIBRARY_PATH` 保持 `''`、launch 抛 `TargetClosedError`。而 `chromium_env.py` 自己的 docstring **早就写着**它是四份漂移拷贝之一、且注明「measured: with it unset, its self-heal is a no-op」。我上一轮收编三份、留下这份,**而它恰是 `fetch_url`/JS 渲染抓取的唯一入口**。
- **★ 守卫结构上看不见它:** `test_chromium_env.py` 的 ratchet 名单是**仓库相对路径**,而 tofu_search 是 out-of-tree 独立仓库——名单再长也覆盖不到。这个盲区正是它能活下来的原因。新守卫改为**按已安装包定位**(`import tofu_search.fetch` 取 `__file__`),缺失时诚实 skip。
- **★ 我自己在修的过程中发现的第三个洞(比前两个更隐蔽):** 该池**完全没有 fontconfig 半边**(grep `FONTCONFIG` = 0 处)。本机无 `/etc/fonts`,实测 `measureText('tofu')` = **0**。也就是说:只修 `LD_LIBRARY_PATH` 会把「崩溃」换成「**静默抓回空白页**」——后者读起来像「网站没加载」,比崩溃难查得多。修完实测 **119.4**。判据已进守卫:池的字形守卫与 launch 守卫**必须并存**。
- **★ owner 的第二击(前提被证伪):** 三处 `--only-shell` 的决策注释写着「zero headless=False call sites」——实测 `interactive_login.py:91` 就是 `headless=False`。带完整 env 实跑:`Executable doesn't exist at .../chromium-1223/chrome-linux64/chrome`。**headless shell 在架构上没有 headed 模式**(它是独立二进制,不是 flag)。更糟的是报错附带 Playwright 的「刚安装或更新过,请运行 playwright install」横幅——**而这条命令正是造成 shell-only 状态的那条**。
- **落点(owner 定调:保住 -60% 收益,只让唯一需要 headed 的功能诚实降级):** 把「能否开窗」变成**可判定的事实**而非各调用方自猜——`chromium_env` 新增 `is_headless_shell()` / `headed_chromium_executable()`,`describe` 报告 `headed_executable`。**不改回装完整构建。**
- **★ 根因比「文案不好」深一层:** `is_interactive_login_available()` 原本只查 `HAS_PLAYWRIGHT`——**对「只有 shell」这个真实失败原因构造上不可能失败**。所以它报告功能可用,然后在 launch 处死。现改为查 headed-capable 二进制,失败带 `reason='headed_unavailable'` + 正确命令。
- **跨仓 seam 的形态(owner 要求不硬 import):** 池与登录墙都是**先委派**(`from chromium_env import ...`),**import 失败才回退**到内建实现;回退锚 `sys.prefix`、`$CONDA_PREFIX` 降为末位候选,且候选目录必须 **isfile 到 sentinel 库**才收(charter #13)。两条路径**分别实测**:委派路径与回退路径都是 `LD_LIBRARY_PATH` 非空 + launch OK + 字形 119.4。
- **★ 我的回退守卫首版红了,而它是对的:** 报 `chromium_env WAS importable — fallback not exercised`。原因:`_run_probe` 用 `cwd=ROOT`,`python -c` 会把 cwd 放进 `sys.path`,所以清掉 `PYTHONPATH` 根本不够。改用中立 tmpdir 作 cwd 后才真正测到回退。**守卫拒绝在没真跑到被测路径时变绿——这正是它该有的行为。**
- **★ NEUTER 第一轮结果不可信,已重做:** 我的 `run()` 里带了 `cd`,导致后续相对路径全部失效——只有 F 真正生效,G/H/I 的「3 failed」其实都是 F 残留。**判据:NEUTER 脚本里的路径一律用绝对路径,helper 内禁止 `cd`。** 重做后 F/G/H/I 各咬 3/1/1/2,方向互不相同,还原后 17/17。
- **兄弟协作:** `scripts/install_on_server.sh` 的 `--only-shell` 修复被兄弟(ms5bbwg8)的白名单提交 `07f979d6` 一并带走,已核 `git diff` 为空、未重复提交;其 vertical/travel WIP 未被我卷入。
- **验收边界:** ①两仓分别提交,**运行中进程不带**,需重启;②登录墙在 shell-only 装机上**仍不可用**——这是设计选择(保住 -60%),但现在它**诚实说明并给出正确命令**,而不是死在一条误导性报错上。

### 2026-07-29(续·headless-shell 通道) — 「装了吗」这个问题有三个互相矛盾的答案;而**桌面端那个答案在检查一条安装器故意永不创建的路径**(owner 质疑「不是装过吗」;commit `92af42fc`,7 文件 +610/-58;新套件 **17/17**,**NEUTER×5 各咬各的**(其中**一发首版空转,因为我把被测函数改成了死代码**);相邻环 **119/119** + 真浏览器视觉环 **18/18**;干净 committed worktree **49/49**)

- **★ owner 的质疑成立,而「缺 libatk」是条件性的不是缺失:** 裸跑 headless-shell 确实死在 `libatk-1.0.so.0`,但 10 个库**全部就在** conda env prefix 里(`describe_chromium_env()['issues']` 为空)。走 `ensure_chromium_env()` 后 `rc=0`、真出 DOM、Playwright 真截图 **8598 个墨迹像素 / 249 灰阶**。**缺的不是库,是 `LD_LIBRARY_PATH` 导出**——`chromium_env.py` 正为此存在。
- **★ 真缺陷在「哪个二进制算数」这半边,而它从没被下沉:** `chromium_env.py` 管住了 env,但二进制解析仍是手抄三份且互相矛盾。实测本机只有 `chromium_headless_shell-1223`(完整 `chromium-1223` **不存在**):
  - `desktop/post_install.py:88` 问 Playwright 要 `chromium.executable_path` 再 `isfile`。**该属性恒指完整构建**——实测 `exists=False` 而同一解释器里 `launch()` 成功(`ps` 抓到父进程就是 shell)。于是桌面端永远显示「浏览器引擎未安装 ~150MB」并劝用户重复下载**一个本来就能用的浏览器**。
  - **这不是暂态偏差:** `install.sh:430` 用 `--only-shell` 是深思熟虑的省 60% 下载决定,**那条路径产品永远不会创建**。
  - `lib/motion_video/_env.py` 探测形态正确,但写死 `~/.cache/ms-playwright`,对 `install.sh:336` 导出的 `PLAYWRIGHT_BROWSERS_PATH` 全盲。
- **落点(按 charter #13「二进制解析必须 isfile 到目标」):** 二进制解析下沉进 `chromium_env.py` 成 `browsers_root/chromium_binaries/chromium_executable`,跨平台形态齐全(Linux/macOS/Windows × 完整/shell),排序按**(能力档, 版本)双键**——同版本时完整构建胜出。两个探测器改为委派;`describe_chromium_env` 报告已解析二进制并把「没有浏览器」列为可执行 issue。
- **★ 写守卫时抓出的第二个真缺陷(我原实现也错了):** `PLAYWRIGHT_BROWSERS_PATH` 是 **OVERRIDE 不是追加**——实测把它指向一个**空目录**,Playwright 仍解析到该目录内且**绝不回退** `~/.cache`。我第一版按「追加」写,会报告一个 Playwright 根本不会从那儿启动的浏览器。是我的测试先红把它抓出来的。
- **★ NEUTER B 首版空转,原因是我自己造成的:** 我把 `chrome_bin()` 改成直接委派后,`_playwright_chrome_candidates()` **成了死代码**——NEUTER 改了个没人调的函数,自然不咬。**判据:委派式重构后要问「原来那个 helper 现在还有调用方吗」**;留着一个「看起来权威实则无人调用」的函数是给下一个人挖坑。已删除它并把守卫重锚到活路径 `chrome_bin`,重做后真咬 2 条。
- **★ 既有 9 条浏览器守卫为何全绿地放行:** 它们**只驱动「已安装的那个通道」**,断言真浏览器能出像素——这在整个缺陷期间都是对的。**没有一条断言「探测器怎么说」或「三个探测器是否互相同意」**。新套件正是补这一面:用合成磁盘布局(仅完整/仅 shell/两者/都无)驱动三个探测器并断言**verdict 一致**。
- **顺带修掉的两处安装漂移:** `desktop/launcher.py:648`(frozen 路径,**打包版用户真正走的那条**)与 `post_install.py` 都在拉 175MB 完整构建;`scripts/install_on_server.sh` 同样漂移。三处补齐 `--only-shell`。
- **更正一条已被证伪的陈旧断言:** `test_frontend_tofu_scene_pixeldiff.py:25` 的模块 docstring 写着「Chromium 缺 ~12 个系统库所以不可用」——实测为假。该文件继续用 cairo 是因为**重放确定性**,与可用性无关,已改写清楚。
- **共享 HEAD 纪律(本批两次):** ①`git add` 后计数断言读到 **staged=8 而非 7**,查出兄弟把 `scripts/package_extension.sh`(它自己 epic `pt_d64220b406e841b2` 的产物)预暂存在共享 index 里,按 charter #15 用 `git reset HEAD` 只写 index 剔除,复核其工作树 2618 字节原封不动;②`git commit -F /tmp/...` 的消息文件被兄弟活动清掉导致提交失败(JOURNAL 记过的同型竞态),改用 `.git/` 内文件成功。
- **验收边界(诚实分账):** ①`scripts/install_on_server.sh` 的修复**在工作树里但没进提交**——`.gitignore:64` 的 `/scripts/*` 使它**未被 git 跟踪**,干净 clone 里根本没有这个文件。这与兄弟正在做的 `pt_d64220b406e841b2` 是同一个仓库策略缺口,已在提交信息与板上注明,不越界改 `.gitignore`;②纯后端 + 桌面端,**运行中进程不带**,需重启才对新会话生效;③相邻红一条已定责非我:`tests/test_brand_shadow_pixels.py` 有重复 `def _chromium_available` 的 IndentationError,属兄弟在飞 WIP,HEAD 版本语法正常,我未碰该文件。

### 2026-07-29(续·Continue 回滚出口) — owner 实测退回:上一轮只把**入口**换成身份优先,**出口仍吃数组下标**——漂移×失败叠加时,要续接的那条回答从屏幕上消失,而 toast 还说「可重试」(改动随兄弟 `271916c2` 一并落库;守卫 **15 条(+2),failing-first 5 红**,**NEUTER×2 各咬 5+1**,干净 committed 树 **25/25** + 端到端双路径实证 FIXED)

- **★ owner 的复现(两个条件叠加才暴露):** DOM 停 3 条态(`msg-0..msg-2`,中断气泡 `data-msg-id=a-tail` 在 `msg-2`)+ `conv.messages` 已变短 + POST 抛错 → `tail node present=false`(本该 true)、`msg count=2`(本该 3)。**用户点 Continue、网络一抖,要续接的那条回答就没了**;比上一轮「抬错节点」更严重——这次是把目标弄丢,而 toast 还在说可重试。
- **机制(同一病根的另一半):** `finalizeStreaming` 用 `_idxOf` 拿**数组下标**(漂移下=1),`renderMessage(msg,1)` 渲出 `id=msg-1` 的静态节点换上去;可 DOM 里**已经有一个 `msg-1`**(历史气泡 `a-old`)⇒ 同 id 两份;紧接的 `_evictByMsgId(inner,'a-tail',keep=getElementById('msg-1'))` 里 `getElementById` **返回先出现的 `a-old`**,keep 指向错节点,刚恢复出的 `a-tail` 被当孪生体清掉。实测坐实:注入后 `[id=msg-1]` 计数=2、`getElementById` 取到 `a-old`。
- **★ 真因不是「下标算错」,而是 `sm.outerHTML = html` 会 ORPHAN 掉 `sm`:** 交换后**没有任何句柄**指向新写入的节点——这正是当初只能用 `getElementById` 回捞的原因,而回捞在 id 撞车时必然取错。改为解析到游离容器后 `sm.replaceWith(_finalEl)`,**把新节点按引用握住**,清扫直接传 `_finalEl`,keep 从此与下标是否新鲜无关。**判据:凡是「写完再回头找自己刚写的东西」的代码,都要问一句——那个查找键在这一瞬间唯一吗。**
- **按 owner 指令未走捷径:** 没有在 `_rollbackContinueShell` 里绕过公共缝另写第四份查找;修在 `finalizeStreaming` 本身,5 个调用文件(比票面写的 4 个多一处:`main_regen_continue` / `main_send_pipeline` / `sse_pipeline`×4 / `stream_lifecycle`×2 / `streaming_render`×2)一并受益。
- **★ 守卫差点又假绿(harness 自纠):** Guard 6 首版仍在用 harness 的 ConvView **stub**,而缺陷在**真实** `conv_view.js` 的清扫里 ⇒ 全绿却什么都没测到。改为把 `conv_view.js` 作为 `argv[4]` **先于** target 加载、并**显式不安装 stub**,再加一条 `real_convview_in_use` 断言钉住「跑的确实是真缝」。**判据:守卫里凡 stub 了某个协作者,必须有一条断言证明被测路径没有走 stub。**
- **NEUTER×2 各咬 5+1:** I 把 keep 改回 `getElementById('msg-'+idx)`;J 退回 `outerHTML` + 回捞。两发都精确复现 owner 报的形态。
- **★ 顺带重锚一条兄弟脆守卫(而且它把缺陷编码进了断言):** `test_source_carries_identity_keyed_render_seam` 把局部变量名**逐字写死**成 `_evictByMsgId(_inner, msg._msgId, _keep)`,因本批改名而红——**行为没变**(清扫仍在且更正确)。更值得记的是:那串字面量里的 `_keep` **就是 `getElementById` 回捞本身**,等于把缺陷钉成了「必须保持」的契约。改为钉行为不变量(清扫按 `msg._msgId` 跑 + keep 不得由位置 id 回捞),并实测该断言**仍真咬**(不是靠放宽换绿)。
- **端到端双路径实证(干净 committed 树):** 漂移×成功 → shell 落在 `a-tail`、历史气泡完好、节点数 3;漂移×失败 → 中断气泡存活且非 shell 形态、历史气泡完好、节点数 3、toast 在 ⇒ **BOTH PATHS FIXED**。
- **共享 HEAD 事故(第三次,本轮两连):** 提交窗口内兄弟先塞进 16 个 `static/icons/pet/*` + 迁移脚本进共享 index(计数断言两次 abort 拦下),随后又清空 index;最终我的三个文件被兄弟的 `271916c2` **连同他自己的改动一起提交**。逐项核对:`replaceWith(_finalEl)` / `_evictByMsgId(..., _finalEl)` / Guard 6 / 兄弟守卫重锚 **四处全部完整在 HEAD 上**,旧的 `const _keep = getElementById` 计数为 0,工作树无残留。**判据:共享 HEAD 上「我的改动进没进去」不能看自己的 commit 是否成功,要按符号逐项 `git show HEAD:` 核对——它可能搭在别人的车上进去了。**
- **验收边界:** 纯前端静态改动,刷新即生效;**未做真浏览器实测**——owner 明确表示待本条修完自行点验。`Continuing…` 硬编码英文仍按既有缺陷另开工单。

### 2026-07-29(续·Continue 外壳落错气泡) — owner 实测退回:**我上一批自己引进的回归,比它修的原 bug 更难看**——一条历史回答被就地改写成 `Continuing…` 脉冲,而真正的中断气泡毫无反应(commit `9295e7b9`,3 文件 +238/-1;守卫 **13 条(+3),failing-first 5 红**,**NEUTER×2 精确咬**,干净 committed worktree **21/21** + owner 复现脚本转 FIXED)

- **★ 根因是「前置保证被拆掉而查找方式没跟着换」(不是简单写错):** `_raiseContinueShell` 用 `document.getElementById('msg-' + lastIdx)` 找节点。**改造前同样一行位置 id 是安全的**——它跑在 `ConvView.replaceAll(...)` **之后**,那次重绘刚把所有 `msg-N` 重新盖过,位置 id 必然新鲜。上一批(`b17ecd2e`)删掉 `replaceAll`(因塌缩已提前到点击帧,事后重绘不再需要)、又把查找提到**任何渲染之前**,于是「id 是新鲜的」这个隐式前置条件消失,而查找方式原样保留。**判据:删掉一个调用时,要问它除了表面职责还顺带保证了什么。**
- **owner 的实测复现(用真实 shipped 函数):** DOM 停在 3 条消息态(`msg-0..msg-2`,中断气泡 `data-msg-id=a-tail` 挂在 `msg-2`),`conv.messages` 因一次 poll/merge/peer 对账掉到 2 条且尚未重绘 → 点 Continue 抬到 `msg-1`(`a-old`)身上,**函数返回 true**。后果三重:①历史回答的正文被 zone innerHTML 覆盖成脉冲;②真正的中断气泡毫无反应;③`_shellUp=true` 使成功路径**跳过 else 里的「重绘后再抬」兜底**,`updateStreamingUI` 往错节点的 zones 灌流。即项目反复在防的「功能不可用而界面呈现为正在工作」。
- **★ 违反的是项目自己早写下的禁令,不是新规则:** `chat_render.js::_reconcileFindEl` 明文「有 `_msgId` 就**只**按它匹配,MUST NOT fall back to positional `msg-${i}`,after an index shift that slot belongs to a DIFFERENT message」;`conv_view.js::_findMsgEl` 同为 `data-msg-id → msg-${idx}` 优先级。`_raiseContinueShell` 是全库唯一反着来的。
- **★ 修法落在根因层而非调用点:** 真因是 **`_findMsgEl` 被关在 `conv_view.js` 的 IIFE 里没有导出**——规则写了两遍却**没有可复用的缝**,所以外部调用方只能手抄 `getElementById`(我上一批正是这么干的)。故把它提为 **`ConvView.findMessageEl` 公共缝**,`_raiseContinueShell` 委派过去。**MISS 返回 false 是 load-bearing 的**:既有的「重绘后再抬」兜底正是为 lazy-window evict 掉 tail 的情形准备的,之前因为总能拿到(错的)节点而**永远不会触发**。
- **守卫为何测不出(owner 点名):** 原 harness 用 `staticAssistantHtml(1,'a1')` 配 2 条消息,**位置 id 恒等于 `length-1`**,两种查找在该夹具下行为完全一致。补 Guard 5 索引漂移用例(DOM 停旧位置 + messages 变短),断言外壳落在 `data-msg-id` 匹配的节点,**并加补集断言**:历史气泡存活 / 正文未被改写 / 不是 shell / 无脉冲——防「抬对了但顺手砸了另一个」。
- **守卫的守卫(本轮关键):** Guard 5 **stub 了 `ConvView.findMessageEl`**,若 shipped 的缝是位置优先,产品坏了而 harness 仍绿。故直接断言 `conv_view.js` 里 `_findMsgEl` 的 `data-msg-id` 匹配**必须先于** positional fallback;harness 的 ConvView stub 也按真实缝逐字改写(原先根本没有 `findMessageEl`,照旧会测不到分派)。NEUTER H 摘掉它 → 真红。
- **★ 过程自纠(charter #8 差点被我破):** 新写的 JSDoc `@param` 用了 em-dash 分隔,TS 扫描器报 **TS1127 Invalid character**,使 `test_frontend_typecheck` 从 HEAD 的 3/3 绿变 1 红(BASELINE=0)。改为 ASCII 连字符后回绿。**判据:JSDoc `@param` 的分隔符走 ASCII,注释正文才可用排版破折号。**
- **相邻环定责(41 套分两批,余红全部非本批):** `conv_model_identity` 2 条在**干净 HEAD worktree 上同样红**(该树 `grep findMessageEl` 计数 0,证明确无我改动)且我未碰 `conversations.js`;`rendermessage_segment_gate` / `stale_cache_paint_gate` / `warm_open_adopts_reconciled_list` 三条 HEAD 与主树逐文件对比一致;`model_fallback_banner` 是兄弟未跟踪的新文件;`conv_verify_failure_reheal` **单跑 3/3 绿**(批量里的红是顺序污染)。**判据:批量红必须逐文件回干净 HEAD 比对才能定责,批量结果本身不足以归因。**
- **验收边界:** 纯前端静态改动,刷新即生效;**未做真浏览器实测**——owner 明确表示待本条修完自行点验。`Continuing…` 硬编码英文仍按既有缺陷另开工单。

### 2026-07-29(续·Continue 点击链路) — owner 顶回我的首版处方:照抄 regenerate 的占位补丁会造**孪生气泡**,因为 Continue 不截断 tail;真正落点是**原地提前抬外壳**(commit `b17ecd2e`,5 文件 +781/-97;守卫 **10 条 failing-first 6 红**,**NEUTER×6 全咬**(含一发复现我自己实作中犯的错),干净 committed worktree **23/23**)

- **★ owner 的证伪(我的修法一本身会引进新缺陷):** 我提议用 `_renderTranslatingBubble` 补 Continue 的死区。owner 指出它是**tail 追加**——regenerate 能用是因为它**先截断了 assistant 消息**,tail 空出来了;Continue **不截断**,被中断的气泡仍在 tail 上,追加会让整个 POST 窗口出现**两个 assistant 气泡**(中断的 + 假占位)。这正是 `_applyContinueCheckpoint` 里 ms43foj3 注释在讲的孪生气泡缺陷。**判据:补「慢」不能用会造「幻影」的手段;一条数据 ⇒ 一个 DOM 节点是硬不变量。**
- **★ 正确落点让两个症状同时消失(而不是各打一个补丁):** `_raiseContinueShell()` 在**点击帧**就把同一个节点原地转成 streaming 外壳(`msg-N` → `#streaming-msg` + elapsed timer + zones)。①反馈变零延迟,不依赖任何往返;②**高度塌缩也随之提前到点击帧**,此时 `_forceScrollToBottom(null,true)` 贴底一次,之后回填只在视口**下方**生长——向下生长不移动读者正在看的东西,所以**「事后补救滚动」这个动作整个不再需要**,成功路径的 `replaceAll` + `scrollToBottom` 一并删除。
- **跳历史的两层根因(实测坐实):** `replaceAll(forceScroll:false)` 走外科手术路径,而该路径的锚点捕获/复位被 `if (_bgRepaint && ...)` 包着,`_bgRepaint` **只有 `_bgRefreshChat` 会设** → 这次重绘**零滚动保全**;而回滚恰是全项目高度变化最剧烈的一种(丢弃未完成轮次 + 清 thinking + 尾部正文降级为折叠 `priorContent`)。兜底的裸 `scrollToBottom()` 又在 `core.js:351` `!force && !isNearBottom(200)` 时**直接 return**,且单 rAF 对着 `content-visibility:auto` 估算高度会「lands mid-history」——**这句是 `main_translating_bubble.js:59` 的原话**,说明同一个坑项目里已有人踩过并写下结论,其他气泡插入点全部换成了真实高度原语,**只有 Continue 还在用裸的**。
- **★ owner 抓出我漏掉的第三、第四条(比我报的两条更严重):** ③POST 失败 `catch` 只有 `debugLog`(**仅进调试面板**),用户点了 Continue 等半天什么都不会发生——这本身就是「点了没反应」的最坏形态;而外壳提前之后,静默失败会把用户**卡死在 `Continuing…` 脉冲**前。故 `_rollbackContinueShell()`(`finalizeStreaming` 回渲静态气泡;因该路径**从未改动消息文档**,`finishReason='interrupted'` 与 Continue 按钮原样返回,可重试)+ `showToast('continue.failed')`。顺带 `data = await res.json()` 原在 `res.ok` **之前**,代理 502 的 HTML body 会在 `json()` 里抛进同一个静默 catch → 改为 ok 先行。④`conv.activeTaskId` 到函数最后才赋值,整个死区窗口重入敞开 → 加 `conv._continueInFlight`,第一个 await 之前设、`finally` 清。
- **★ 我自己在实作中犯了闸失效的错,并被行为守卫抓出:** 改造后 `await _buildConvConfig(conv)` 有一份**残留在闸之前**,于是双击两次都进得来(`single_post` 红)。修完后**补了一条结构不变量**:`continueAssistant` 里**闸之前不得出现任何 `await`**(剥注释 + 剔除会提前 return 的 empty-turn 分支后扫),并把它做成 NEUTER F ——把 await 放回闸前 → 2 条红。**判据:行为守卫抓到的坑,能钉成结构不变量就要钉,否则下一次重排还会踩。**
- **另一处自纠(harness 假红):** 4 条 jsdom 守卫首跑是 **0 PASS**,看着像真红,实际是我把 `report()` 写在 async IIFE **外面**同步执行,`out` 还是空的。改为 `.then(report).catch(...)` 并让抛错显式打 `FAIL harness_threw`。**判据:「0 PASS」与「N 条 FAIL」必须可区分,否则 harness 坏了会伪装成被测代码坏了。**
- **顺带重锚一条兄弟脆守卫:** `test_empty_guard_includes_toolrounds` 用 `src[fn:fn+1200]` **固定字节窗口**取 empty-guard,我新增的闸注释把它推到 1146+66 > 1200 之外而报红——**行为没变**(`!_hasRounds` 仍在)。改为锚在 guard 语句本身。这是 JOURNAL 里「在被切片守卫的文件里插东西,要先问切片边界在哪」的又一个实例。
- **★ 共享 HEAD 事故自报(本轮第二次,且这次是我造成的):** `git commit -- <pathspec>` **直读工作树**,把兄弟未提交的 3 处 WIP(`local.checking` 删除 / `local.desktopFloor` / `applySectionRequirements`+`_lcBrowserDownload`+`_lcPaintFloor`)一起提交了。发现后按 charter #15 剥离,但**剥离脚本又把兄弟内容从工作树也抹掉了**,只得再手工还回未提交态。过程中还遇到:兄弟清空共享 index 导致 `staged=0` 被计数断言拦下;`/tmp` 被清理导致 `-F /tmp/msg` 找不到文件(改用 `.git/` 内);兄弟往共享 index 塞 `scripts/*`。**沉淀判据:共享 HEAD 上对「工作树含兄弟 WIP 的文件」禁用 `git commit -- <path>`,唯一正解是 `git diff` → 按 hunk 过滤 → `git apply --cached` → `git commit`(无 pathspec,提交前计数断言),因为前者会把工作树里不属于本批的行一起带走。**
- **验收边界(诚实分账):** ①纯前端静态改动,**刷新即生效**,但新守卫文件与 i18n 新键需 bundle 重建才对已加载页面生效;②**未做真浏览器实测**——本批证据是 jsdom 行为守卫(快照取在 stub 的 await 内部,严格早于网络回复)+ NEUTER×6 判别力验证,不等于线上像素实测;③外壳里的 `Continuing…` 仍是硬编码英文(改造前就是),按「重构中发现的既有缺陷另开工单」惯例未在本批处理。

### 2026-07-29(续·死控件根修) — owner 去戴皮肤时发现**开关不在他手里**:标题+说明是静态 HTML 照常渲染,格子由缺失的 JS 填充,于是他看到「标题 + 说明 + 一个空盒子」;而我把「需重启」写在验收边界脚注里却让他现在去点——**两句话冲突,他按后者行动了**(commit `4546356f`,9 文件;新套件 **6/6 含 NEUTER 真咬**,相邻环 **44/44**;真浏览器实测降级形态)

- **★ 缺陷形态(项目明令禁止的那一种):** 功能不可用,界面却把它呈现为可用。根因不是代码错——`brand_logo.js` 的 bundler 注册与 dev-fallback 标签**都在 HEAD 里**;而是**运行中的进程持有它启动时的打包清单**,新模块要等重启才进 bundle。于是「代码正确」与「界面诚实」是两件事:前者绿,后者是个假装可点的空盒子。
- **★ owner 要的是根因层修法,不是 logo 一处特例:** 落点为**通用降级契约**——任何设置区块用 `data-requires="符号名"` 声明它的 JS 依赖(支持多符号空格分隔),`settings/section_requires.js` 在设置打开时逐一检查:全在 → 正常渲染;**任一缺失 → 加 `.degraded`,CSS 隐藏控件并显示「此功能需重启服务后生效。」**。新增一个 JS 依赖型区块从此是**一个属性**,不是一段新代码。
- **★ 我的 9 条既有守卫为何全瞎(owner 的判据):** 它们**全部在测模块自身的行为**,而这个失效只在模块**不存在**时出现——没有一条测「模块不在时 UI 长什么样」。新套件正是补这一面:用**真实 shipped 面板切片**驱动(缺失→必须 degraded、存在→必须正常)、多符号形态、以及一条**CSS 生效断言**(class 翻了但样式表不响应,守卫会假绿而用户仍看到空盒子)。NEUTER:摘掉符号检查 → 缺模块的区块重新变成「看起来可用」,精确复现 owner 撞到的形态。
- **真浏览器实测降级态(生产 CSS + 真实面板 markup):** `degraded=1`、**pickerVisible=False**(空盒子真的消失)、noticeVisible=True、文案「此功能需重启服务后生效。」
- **验收边界:** 修复纯前端;`section_requires.js` 自身也是新模块,**同样要等重启才进 bundle**——但这次不再有欺骗性:重启前该区块整体降级并明说原因,而不是留个空盒子。重启后皮肤选择器与三枚候选即可点。

### 2026-07-29(续·皮肤资产迁出 _gen) — owner 纠正:我把**用户能点到的产品资产**提交进了生成器工作台目录;而我的守卫只测「文件存在」,**测不出「文件放错地方」**(commit `da5742d0`,6 文件(4 个 R100 纯改名);守卫 **9/9**,新增位置不变量 + 导出存活守卫,**NEUTER 真咬**;新路径五枚实测全绿)

- **★ owner 的判据(比我的守卫强一档):** `static/icons/_gen/` 名字就在说「这是临时产物」。将来任何人清理它、或把它加进导出剥离规则,五枚皮肤里的四枚会**静默回落成原版**——用户点了没反应,看起来像开关坏了;而「文件存在」这条断言在开发树上永远是绿的,看不见这类失效。
- **实测坐实风险不是假设:** `export.py` 里 `_gen` 的同级兄弟 **`_candidates` 已被三级导出全部剥离**(`PERSONAL_EXCLUDE_DIRS` 与 `ALWAYS_EXCLUDE_DIRS` 都有它,注释写着「raw AI-gen asset candidates + proof strips (review-only, multi-MB)」)。`_gen` 今天恰好不在名单上——**是侥幸,不是安全**:它与已被剥离的目录同属一个语义家族。
- **落点:** 四枚资产 `git mv` 到 `static/icons/skins/`(保留改名历史,diff 显示 **R100 内容零变化**),文件名去掉 `candidate-` 前缀(它们不再是候选稿,而是可选皮肤);注册表五个 `path:` 全部指向产品路径,`_gen` 只留生成器与草稿。模块头写明这是**不变量**而非偏好。
- **两条新守卫(owner 点名那条 + 我自查补的一条):** ①**位置不变量**——任何注册皮肤的路径不得含 `/_gen/` 或 `/_candidates/`;②**导出存活**——直接 import `export.py` 的 `_should_exclude`,对每枚皮肤 × 三个导出档位逐一断言不被剥掉(charter #13/#14:「必须活着到达用户」的文件要有导出存活守卫,不能靠假设)。NEUTER 实测:把 `minimal` 的路径改回 `_gen` → 位置守卫立即红。
- **★ 顺带修掉我自己一条脆断言(改名暴露出来的):** 原「切换后三个面同步」的守卫写死了文件名 `candidate-a2-soft`,改名后它红了。重锚为**从注册表取该皮肤的实际 path 再断言**——比原来强:以后任何改名都不会让这条守卫因为找不到字符串而假绿或假红。
- **迁移后实测(真浏览器 + curl):** 新路径四枚全部 **200**(2218/8449/949/4794B),旧 `_gen` 路径 **404**(证明是迁移而非复制);五枚 URL 两两不同、`img[data-brand-logo]` 与 favicon 同步、**无一在 `_gen` 下**、缺失资产回落仍生效。
- **验收边界:** 纯前端 + 资产改名,刷新即生效;新模块进页面仍需 bundle 重建;三枚候选仍是**待试戴状态**,`default` 永远是原版且排注册表首位。

### 2026-07-29(续·假 t() 盲区) — owner 实测抓出我上线的 QR 标签是**字面量 `project.qrScan`**;根因不止「忘了加 key」,而是**测试 harness 伪造的 `t()` 语义与真实现相反**,11 条渲染守卫全绿地放行了它(commit `bd8c63e0`,3 文件;守卫 **15/15**,**NEUTER×2 各咬 4 条**;干净 HEAD worktree:i18n 守卫 **8/8**(此前红)+ QR 全套 **66/66**)

- **★ owner 的证伪(用真 i18n.js 实测):** `t("project.qrScan","Scannable QR code")` 返回的是 **`"project.qrScan"`**,不是那句英文。对照组 `t("common.cancel")` → `"取消"`。也就是用户在二维码上方看到的是一行**变量名**。
- **★ 根因是两层叠加,第二层才是真问题:**
  ①`t()` 的签名是 `t(key, params)`,**第二参是 `{placeholder}` 替换表,不是缺省文案**——我按「fallback」写,它**语法上合法、语义上什么也没做**;而两个 key 从来没在 `static/js/i18n.js` 里定义过,于是 `t()` 走 `!entry → text = key` 分支吐出裸 key。
  ②**我的 harness 把 `t` 伪造成 `(k, d) => (d || k)`**——这与真实现**语义相反**:它把第二参当兜底文案返回,凭空**制造出真 UI 永远不会产生的字符串**。所以「key 缺失」这个缺陷在测试里长得和「key 正常」一模一样,**11 条渲染守卫测的是一个不存在的函数**。这才是它能上线的原因。
- **落点:** ①补 `project.qrScan` / `project.qrScanMulti` 两 key 的 zh+en;②调用点去掉那个假 fallback 参数,并留注释钉死「第二参是 params 不是缺省值」;③**harness 改为抓真字典**——Python 侧正则解析 `static/js/i18n.js` 成 `{key:{zh,en}}` 注入 node,`t()` 对缺失 key **返回 key,与生产一致**。
- **★ 项目里本来就有一条守卫在喊,我没跑它:** `test_frontend_i18n_key_coverage.py::test_every_referenced_key_is_defined_all_namespaces` 在**干净 HEAD 上就是红的**,点名的正是 `ui/tool_rounds.js: project.qrScan` 与 `project.qrScanMulti`;我实测它在我第一次 QR 提交之前(`6a5dec8d~1`)是**绿的**,所以这是我引进的真回归。已按 owner 要求列入常跑项:**任何往 JS 加 `t('...')` 的批次都必须过它**。
- **NEUTER×2 各咬 4 条:** 删掉两个 key(复现上线态)→ 4 红;把假 `t()` 装回去 → 同样 4 红(证明盲区确已消除,而非靠新加断言绕过)。另有一条「守卫的守卫」断言字典抓取真的解析出 >500 条且含这两个 key——否则「key 缺失」与「字典没解析」会同样通过,断言就是空转的。
- **★ 我自己造成一次工作丢失(记下来防再犯):** NEUTER 还原时我用了 `git checkout -- tests/test_frontend_qr_render.py`,**把自己未提交的 harness 改造整个回滚**(charter #15 禁止「以工作树为中间态」正是这个)。i18n key 与调用点修复因不在该 pathspec 内幸存,harness 重做一遍。**判据:NEUTER 的备份/还原一律用 `cp` 到 /tmp,永不使用 `git checkout`——后者不区分「我要撤销的实验」与「我还没提交的成果」。**
- **一处噪声已定责(非我的):** 该守卫现在只剩 `feature-92a75489.js` / `feature-dfd9ee59.js` 报 `paper.qaThinking`。这两个是 **gitignored 的本地陈旧 bundle**(`.gitignore:133`),源码里 grep 不到该 key,干净 HEAD worktree 里也不存在这两个文件——属兄弟在飞的 paper 改动的构建残留,不在本批范围。
- **最终形态(真 t() 实测):** `project.qrScan → "可扫描的二维码"`,`project.qrScanMulti → "个可扫描的二维码"`。
- **验收边界(仍未做的三条不变):** ①纯前端静态改动,**刷新即生效**(不需重启);②「扫完自动继续」仍只是积木;③二维码时效与清理未做。

### 2026-07-29(续·三候选入表) — 试戴位从「一枚我已否过的稿」变成**三条真正分岔的路**;像素这一枚**跟生成器搏斗三轮后诚实改为手写**(owner 指令;commit `50db04ed`,9 文件;守卫 **7/7**(新增「注册皮肤的文件必须真存在」),五格布局实测 **2 行/最小宽 94px/无溢出**)

- **owner 的判据(记下来):** 开关做完不等于目标达成——「让我戴一个我已经否过的东西,等于零进展」。机制层验收通过后,下一步是**填充候选而不是打磨开关**。
- **三条路,方向真分岔(不是三个微调版):** ①**像素精修**——保留现款像素气质与手作感,只修 VTracer 的脏边/歪棱,造型语言不动;②**极简**——减法赌小尺寸辨识度,平色块+两眼+一条小嘴,砍掉渐变/高光/腮红/内部棱线,「只在 64px 才读得到的细节一律不要」;③**手绘感**(我自己的判断)——六轮否决里每一版几何重绘都得到同一句「干净但冷」,而 owner 反复回到的原版,其魅力恰恰是精确性会杀死的东西,所以这枚**故意把不完美放回去**:角不等长、描边按受光面/背光面分粗细、五官左右不对称、腮红溢出一点。赌的是**工艺的温度而非工艺的精度**。
- **★ 像素这一枚:三轮改坐标全部失败,最后诚实改路子(过程自纠):** 复用 `gen_candidate_c.py` 的平面方程放五官,第一版脸贴左棱;按「列窗口」重排→仍偏;按实测 interior(x 4..14/每列 11 行)再排→**还是偏,且腮红穿到轮廓外**。判据浮现:**平面方程对「画立方体」是对的工具,对「摆一张脸」是错的工具**——「居中」在这里是对可见菱形的视觉判断,不是列集合的算术中点。故新起 `gen_pixel_refined.py`:立方体仍解析生成(阶梯规整正是这枚的卖点),**五官改为手写像素图**,并加一道 `cells.get(...)=='left'` 的落点校验,保证任何一笔都画不到轮廓或别的面上。
- **五格排布(owner 预警的坑,实测确认会塌):** 主题选择器是 `repeat(3,1fr)` 固定三列——**主题永远是 3 个,而皮肤表会长**,5 项塞进 3 列会挤扁。给 `#logoSkinPicker` 自己的 `auto-fit + minmax(88px,1fr)`,不动主题选择器。420px 面板实测:5 格 → **2 行,最小宽 94px,scrollWidth 无溢出**。
- **新增守卫一条(本轮最可能静默坏的地方):** 「注册表里每一枚皮肤的文件必须真实存在且非空」。缺了它,一个写错路径的候选会静默走 onerror 回落成原版——用户点了没反应,看起来像开关坏了,而不是像资产丢了。
- **实测五枚全绿:** URL 两两不同(5/5 distinct),每一枚都同步应用到 `img[data-brand-logo]` **与 favicon**(applied_everywhere=True ×5)。
- **验收边界:** ①候选资产**这次进了提交**(必须进,否则界面上点不到),但 `default` 仍是原版、且注册表 `default` 永远在首位;②纯前端,新模块进页面仍需 bundle 重建;③三枚都是**待试戴的候选,不是终稿**——由 owner 在岗试用后定夺。

### 2026-07-29(续·章程并发) — owner 顶回我的处方并给出反证:**append 在这个存储形态下会吞掉兄弟的决策且返回 ok=True**;我的「不需要锁」结论来自**串行探针**这个假形态(commits `6b0715fa` 后端 + `587e1d86` 前端;并发套件 **8/8 失败在前**,charter+attention **99 passed**,前端三套 **38 passed**,干净 committed 态 **85/85**;**共享 HEAD 上本批被兄弟抹掉一次,全量重做**)

- **★ owner 证伪我上一轮的处方(我当时要摘掉 `expected_version`):** 我用**串行**探针(兄弟提交完 → 我再提交)看到 `[D0, SIBLING, MINE]` 三条都在,据此断言「append 天然幂等、锁是多余的」。owner 顶回来后我改用**交错**形态实测:在 `commit_charter` 的读写窗口里插入一次兄弟 append → 最终 `['D0','MINE']`,**兄弟那条被吞,而我的返回值是 `{'ok': True}`**。**判据:并发性质必须用交错探针验,串行探针在坏代码上也会过——它根本进不了窗口。** 这条已写进新套件的文件头,防止后人把交错「简化」回串行。
- **★ 根因不是锁多锁少,是 read-modify-write 整列覆写这个原语:** `project_charter.decisions` 是单行里的一个 JSON 数组,commit 读出整列 → 内存 append → 无条件 upsert 写回整列。两个提交只要交错就互相覆盖。章程决策是要注入给每个兄弟会话的共享意图,**静默丢一条比报错严重得多**。
- **落点按操作语义分锁(不按调用点):** append 与其它 append **可交换** ⇒ CAS 打在 `version` 上、miss 则重读并只重放**自己那一条**(有界 6 次),`expected_version` 降为建议值;overwrite(`content`)**不可交换** ⇒ 保持硬闸 409,且写入同样走 CAS 使检查不被读后竞态击穿。
- **★ owner 抓出我方案里的真坑(不堵就会用新洞换旧洞):** `content` 与 `add_decision` **可以同传**(路由的门是 `content is None and not add_decision`,实测一次调用两者同时落库)。也就是说「这次是不是纯 append」**不是签名保证的,是调用方习惯**——CAS 重放会拿陈旧 `content` 覆盖兄弟刚改的北极星。现二者**互斥**(库+路由两道 `invalid_combination`),让纯 append **从参数即可判定**,重放安全才从「通常没事」变成「不可能出错」。补集守卫钉住:兄弟在窗口内改北极星 → 我 append 决策 → **北极星必须是兄弟的新值**。
- **同一缺陷的第二个实例(owner 指出,我上一批只修了一个页签):** 章程页签把渲染时的 `data-ver` 钉进请求,兄弟自提交后必 **409 且提议仍卡在待办**;且 `project-brain.js` 的 **13 个 mutation catch 全部只有 `console.warn`**(整文件 `showToast` 出现 0 次)。`_reportFailure` 因此提为**唯一失败面**并导出、attention 页签改为委派,13 处全部接线;带 `quiet` 档只给用户没请求过的后台增益(悬停预览)——对那些弹 toast 同样会训练用户忽略这个面。
- **守卫按后果不按行号:** 面板级不变量对**真实 catch 集合**(花括号配平解析全部 `.catch`)断言,新 handler 自动被覆盖;并断言解析器至少找到 10 个 handler,防止解析退化后对空集合断言而**假绿**。
- **过程自纠三条:** ①我的长文本夹具以空格结尾而 `propose_amendment` 会 `strip()`,两条守卫首跑因**我的夹具**而红;②新助手函数插在 `commit_charter` **之前**,打断了 `test_propose_source_never_touches_charter_table` 的**源码切片锚点**——移到其后即复原(判据:在被切片守卫的文件里插函数,要先问「切片边界在哪」);③`test_charter_tab_commit_sends_no_expected_version` 首版匹配原文,被**修复自身的说明注释**绊倒(注释里就有这个词),改为剥注释后再断言。
- **★ 共享 HEAD 事故(本批第二次):** 未提交的全批工作(CAS、路由互斥、13 处接线、前端守卫)被兄弟会话一次性抹掉,`git show HEAD` 核对后全量重做。教训已兑现为动作:**改为分段落地即提交**(后端 `6b0715fa` 先落,前端 `587e1d86` 随后),而不是攒一大批。另:i18n 五个新键被兄弟宽 `add` 卷进 `3772eeff`(逐字核对与我写入一致,故未重复提交);`git commit -F -` 的 heredoc 曾与兄弟提交竞态导致 index 被清空,改用 `-F 文件` 后成功。
- **验收边界(诚实分账):** 后端半**需重启**才对新会话生效;JS 半边**免重启但需两次加载**——bundle 过期时先发旧包、后台重建,下一次加载才拿到新包。相邻红两条已查明归属:`paper.qaThinking` / `project.qrScan*` 缺键属兄弟会话(HEAD 上即缺,我未碰 `ui/tool_rounds.js`)。

### 2026-07-29(续·扫码时序) — owner 实测证伪我的「已完成」:**只在 finalize 出图对扫码登录等于没有**;二维码必须在命令**仍阻塞等扫**时就成像(commit `3b407958`,6 文件;套件 **49 + 11**,**NEUTER×3 咬**(其中**去重那发首版空转,原因是协程合并而非去重在起作用**),干净 HEAD worktree **62/62** + 端到端真解码;wire-parity **2/2 逐字节不变**)

- **★ owner 的证伪(我上一轮交付的是「事后凭证展示」不是「扫码登录」):** `gh auth login` / device-code 这类流程的时序是**先打印二维码,然后阻塞等你扫**——命令此刻**没有结束**。而 `_attach_terminal_qr` 挂在 `_finalize_tool_round`,只在命令**跑完**才执行。所以在「用户真正需要扫」的那整个窗口里,二维码根本不是图片;等它出现时授权窗口早已关闭,或命令因等不到扫码而超时退出。**净效果 = 功能不存在。**
- **实测基线(owner 给的三条断点,我逐条复现):** ①`_renderSearchingRow` 的 run_command 分支**没有** `_renderQrStrip` 调用;②`static/styles.css:7644-7645` 的 `.ptool-cmd-output-live` **同样是 `pre-wrap` + `word-break: break-all`**——我上一轮定位的那个「结构上不可扫」根因,在**实时面板里原封不动**,我只修了已完成面板;③`terminal_qr_images` 在 `lib/` 里除 `lib/qr.py` 外**只被 `_finalize.py` 引用一处**,流式路径全程不扫。渲染器实跑:`qr strip=false / <img>=false / live pane=true`。
- **落点(挂唯一咽喉,不写第二份检测):** `LiveQrScanner` 扫 `_make_run_command_progress_cb._flush_locked` 里**已累积的 `_partialOutput`**(buffer 增长的唯一地点),命中就把 `qrImages` 写上 `round_entry` 并随 `tool_progress` 带出;前端 `_renderSearchingRow` 复用**同一个** `_renderQrStrip`,位置在 live `<pre>` **之前**;`sse_handlers_io.js` 的 `_handleToolProgress` 落 `qrImages` 到 round(否则后端做完也到不了 DOM)。`_renderQrStrip` 现在接受 round(运行中,还没有 `results`)或 meta(已完成)两种载体。
- **两条护栏(owner 点名要):** ①**幂等**——按渲染图字节签名去重,一个码只推一次(chunk 每 ~200ms 来一次,art 在 buffer 里待整个等待期,不去重会用多 KB 图刷爆 SSE);②**成本**——art 字符预检 + 行数下限 + `_MIN_GROWTH` 增量节流 + 尾窗 96KB,避免每 chunk 全量重扫整个 buffer 的 O(n²)。
- **★ 端到端真解码(唯一验收标准):** 按真实 CLI 时序喂流(提示行 → 二维码 → "Waiting for you to scan..." → 继续吐点),**finalize 从未调用**,从**浏览器真会加载的那个 `img src`** 里解出 `https://github.com/login/device?user_code=WDJB-MJHT`。7 个 progress 事件里**只有 1 个**带 QR(去重生效)。干净 HEAD worktree 复跑同样通过。
- **★ 我的去重守卫首版是空转的(自纠,且原因反直觉):** 摘掉 `_seen` 记忆后测试**仍绿**。诊断发现:回调层的 `_COALESCE_BYTES`/`_COALESCE_MS` 把 40 个尾部 chunk **合并成 2 次 flush**,`scan()` 几乎没被调用——**在起作用的是协程合并,不是去重**,我测的是错的东西。改为直接驱动 `LiveQrScanner` 且每轮增长都超过 `_MIN_GROWTH`,让**唯一能压住重复的只剩签名记忆**;重做后清空 `_seen` → 真红。另留一条 wire 层补集测「一个码一个事件」。
- **NEUTER×3 各咬各的方向:** 摘流式扫描 → 3 条红(复现 owner 报的原缺陷);清 `_seen` → 去重守卫红;前端运行中分支不拼 `${liveQrHtml}` → 3 条红。**NEUTER B2(去掉 `_MIN_GROWTH` 节流)仍绿是正确的**——节流是纯性能优化,去掉它行为不变只是变慢,不该让行为守卫变红。
- **共享 HEAD 纪律再次生效(第三次):** `git add` 后计数断言读到 **staged=14 而非 6**——兄弟把 8 个文件(brand_logo/i18n/main/settings 等)预暂存在共享 index 里。按 charter #15 逐个 `git reset HEAD -- <path>` **只写 index 不碰工作树**剔除,复核那 8 个文件工作树内容仍是 ` M`/`??` 未被动过,staged 精确回到 6 才提交。
- **验收边界(诚实分账,与上一轮相同的三条仍未做):** ①**纯后端 + 前端静态**,运行中进程不带,需**重启**才对新作业生效,前端需**刷新**;②「扫完自动继续」仍只是积木(`resolve_human_guidance` 可被后台线程调用),**未内建任何站点的登录轮询**;③二维码**时效与清理未做**,落盘 PNG 无过期回收,登录 URL 带 token 等同凭证而 `uuid4` 文件名只是「难猜」不是「有鉴权」。

### 2026-07-29(续·logo 试戴开关) — 两次「批准后又推翻」的根因不是设计而是**验收方式**:把决定权从「看稿拍板」改成**在岗试戴的运行时开关**;顺带补上主 logo 从未有过的 cache-bust(owner 拍板;commit `4308a2df`,10 文件;新套件 **6/6 含 NEUTER×2 各咬各的方向**,相邻环 **35/35**;纯前端,**刷新即生效**)

- **★ 根因判据(owner 的定性,我实测坐实):** A(+40% 版)与 A2 都是**先批准、上线后在真实欢迎屏被推翻**——A2 甚至过了 in-situ 无头截图闸。截图过目与「每天看着它工作」是两种暴露强度,所以**品牌资产的终审只能是在岗试用**,交稿-拍板这个回路本身有缺陷。落点因此不是再交一版稿,而是**把决定权做成开关**:候选是可穿戴皮肤,默认永远是原版,用户自己戴半天再决定,任何新设计都不必赌一次全量上线。
- **cache-bust 是前提,不是附赠:** 图标响应头 `max-age=86400`,而主 logo 三处引用(favicon/欢迎屏/侧栏)**全是裸路径**——这正是昨天 A2 回滚后 owner 仍看到 A2 一整天、以为「怎么还没回滚」的机制。编排面图标早有 `?v=` 机制(`_ORCH_ICON_VER`),主 logo 反而没有。新模块 `core/brand_logo.js` 持有 `LOGO_VER` 作为唯一真源。
- **落点(单一咽喉,六处引用全部改走它):** `logoUrl()` / `brandLogoImgAttrs(size)` 解析「当前皮肤 + 版本号」;`applyLogoSkin()` 重指所有 `img[data-brand-logo]` **与 favicon**;`setLogoSkin()` 落 localStorage 并即时应用。消费点:index.html 三处静态标记 + `ui/chat_render.js` / `main/main_conv_lifecycle.js` 两个欢迎屏渲染 + `settings/core_panel.js` 移动端入口 + `main.js` boot 应用。设置 UI 在「常规」页,选项**从注册表渲染而非手抄**(加一枚候选 = 注册表加一行)。
- **两条不变量按 owner 要求钉死:** ①默认必须是原版——无存储值、**以及存了一个已被删除的皮肤 id**,都必须解析回 `tofu-welcome.svg`(后者是真实风险:候选下架后 localStorage 里的旧 id 会变成死路径);②候选文件缺失必须回落不能白屏——`onBrandLogoError` 换回默认 URL 且幂等(防错误循环)。NEUTER×2 各咬各的方向:摘掉注册表校验 → 未知 id 被原样采纳;摘掉回落赋值 → 坏图留在原地(白屏)。
- **★ 实测差点误判成产品缺陷(记下来):** 真浏览器里换上候选后 `naturalWidth=0`、截图上两个状态看起来一样,我一度认为切换没真生效。**补测原版图片同样 TIMEOUT** —— 是本机无头环境静态资源加载慢的既有现象(`curl` 同 URL 稳定 200/2218B),与皮肤机制无关。**判据:判定「新功能坏了」之前,必须让对照组(原有功能)跑同一条探针**;否则环境噪声会被记成产品缺陷。切换逻辑本身实测成立:`skin/url/img src/favicon` 四者随切换同步改变。
- **★ 共享 HEAD 三连(本批全程被兄弟写覆盖三次,全部由守卫/断言抓出,零静默):** ①`js_bundler.py` 的注册行被覆盖 → `bundle_manifest_parity` 报 `orphaned: core/brand_logo.js`(**守卫抓的是真事故不是笔误**);②`settings/core_panel.js` 我的改动被回退 → 提交前 grep 复核发现;③`chat_render.js` / `main_conv_lifecycle.js` 两处渲染改动被覆盖丢失 → **计数断言 `staged=8 ≠ 12` 当场 abort**(这正是上一批事故后落的硬纪律第一次真拦住东西)。另:`js_bundler.py` 与 `index.html` 的我方改动被兄弟连带提交进 HEAD(反向卷带),内容核对完整正确,不再为归属花提交。
- **相邻套件一处红是被我有意反转的前提:** `test_frontend_mobile_client_entry` 断言 `tofu-welcome.svg` 字面量,而该处已改走 helper。重锚到「必须带 `data-brand-logo`(即经过共享 helper)」——比原断言更强:手写路径会同时丢掉 cache-bust 与皮肤,新断言两者都覆盖。
- **验收边界(诚实分账):** ①纯前端,**刷新即生效,无需重启**;但运行中的服务器缓存着旧 bundle,新模块要等 bundle 重建或走 dev-fallback 才进页面(实测服务的 HTML 已带 `?v=20260729a`,函数需 bundle 更新);②候选目前只挂 A2 一枚,**它是被试戴的候选不是终稿**;③`_gen/logo-redesign/` 素材仍未提交,家族像素稿留盘待命。

### 2026-07-29(续·测试互毒) — owner 抓出:我上一批的守卫**自己就是我刚修完的那个缺陷**——快照/还原对把陈旧事实盖回去,毒死同进程后续每个测试(CI 真断;commit `e740887d`,4 文件;**结构守卫连错三版,每版错法不同,全部由 NEUTER 而非评审抓出**;验收单次调用含 `pack_serving` **111 passed / 0 failed**,干净 committed worktree 复验同数)

- **★ 最该记住的一条:我犯了自己刚修完的同型错误。** `4b3398bf`/`05b55fbe` 修的正是「快照/还原对静默重置陈旧事实」;而我为它写的守卫 `test_i18n_pack_boot_floor.py` 里,恰好用 save-mutate-restore 去管 `js_bundler._pack_filenames` / `_bundle_includes_i18n`。
- **实测复现(owner 给出、我逐行确认):**
  ```
  before:                  {} | includes_i18n = True     <- 被当作 saved 捕获
  after get_i18n_pack_tag: {'zh': 'i18n-zh-…'} | False    <- 真实已发布状态
  after "restore":         {} | includes_i18n = True     <- 陈旧值盖回
  >>> 下一个文件拿到的 get_i18n_pack_tag('zh'): None
  ```
  根因:这两个全局是**构建的副作用产物**,而快照拍在「测试自己触发的那次构建」**之前**,所以 restore 不是复原,是把前构建态盖到真实态上;模块从此永久自称「pack 未启用」。
- **★ 为什么单文件跑不出来(这决定了守卫形态):** `test_i18n_pack_boot_floor.py` 与 `test_i18n_pack_serving.py` **各自单独跑都全绿**,组合才 7 红;两者都是 `@pytest.mark.unit`,`make test-unit` / `ci` 一定同时收集 ⇒ **这是当天就断的 CI,不是 flake**。故守卫必须**按文件对、在同一进程内、按毒化顺序**跑,per-file 绿不能替代。
- **落点:** ①`js_bundler.reset_manifest_for_tests()` —— 撤销 manifest 变更的唯一受支持方式是**作废**(并且必须一起清 `_bundle_mtime`,否则陈旧闸会让读者直接服务被清空的 manifest),让下一个读者重建,永不重放快照;②boot_floor 改为断言**真实构建态**产生的 tag,dual-bundle 回退时诚实 skip;③`_Pinned` 只还原被 monkeypatch 的**函数**(那不是构建副作用),**数据**走 reset 缝。
- **★ 结构守卫连错三版,三种错法各不相同,全部由 NEUTER 抓出(不是评审):**
  | 版本 | 错法 | 后果 |
  |---|---|---|
  | v1 | 锚在 `saved` 这个**名字**上 | 漏掉 `self._saved` 形式,植入后守卫照绿 |
  | v2 | 只要右值不是字面量就报 | **误报合法 setup**(`_pack_filenames = self._packs`)——逼人去改正确代码,比没有守卫更坏 |
  | v3 | 数括号拼接续行 | 被**字符串字面量里的不平衡括号**打败(本文件自己的正则就有),把后续整个文件吞进一行,藏住已植入的违规 |

  终版改用 **`ast` 解析**:识别「capture(`x = mod._pack_filenames`)」与「replay(`mod._pack_filenames = x`)」的**配对**,只报配对成立者。**判据:扫 Python 源的守卫不要用行正则拼续行——解析器不会被字符串内容骗。**
- **NEUTER×4 各咬各的方向:** 把毒化放回去 → 顺序守卫红并列出组合失败;两种 replay 形式(裸名 / `self._saved` 元组)→ 结构守卫红且带行号;去掉 reset 里的 mtime 清零 → reset 缝守卫红。合法 setup 全程不误报。
- **验收边界:** ①owner 判据「单次 pytest 调用且必须含 `test_i18n_pack_serving.py`」= **111 passed / 0 failed**,干净 committed worktree 复验同数;②`private_hosts.js` 与兄弟的 `project.qrScan*` 两处红按 owner 指示未碰;③本批纯测试基础设施 + 一个测试专用缝,**不改任何运行时行为**,但前两批仍需重启才生效。

### 2026-07-29(续·检视面板) — 工具行调试面板收敛为单视图 + **对比度缺陷不在这个面板,在它复用的共享渲染器**(owner「字号偏小、对比度偏低;请求按钮多余,直接只显示结果状态才对」;commit `4fa28c5c`,6 文件 +750/-142;新套件 **13 + 23 探针**,**NEUTER×3 + ×4 各咬各的**,干净 committed worktree **48/48**,真浏览器三主题像素验收 PASS)

- **★ owner 的第二半判断在数据层就是对的,我先去验证了它才动手:** `tool_dispatch/_pipeline.py:857` 的 post-tool 镜像是在**工具结果已追加进同一条 messages 列表之后**拍的(同 roundNum 轴),所以「请求」载荷是「结果状态」的**严格前缀**。两个 tab = 点两次看同一批消息、第二次少了结果。收敛为单视图后**信息零损失**(守卫 `state_view_carries_request_content` 钉死)。
- **★ 但「只显示结果状态」有一个会静默变成死面板的陷阱,是我查 swarm 侧才发现的:** `lib/swarm/agent.py:50` 的 `_emit_request_snapshot` **只发 kind='request'**,swarm 子代理从不产出 state 镜像。纯 state 面板会让**每一条子代理工具行**都显示「镜像已过期或不存在」。故请求轴保留为 **fallback 而非 tab**:`_riFetchRoundView` 先取镜像、取不到降级取请求,**并把取到的是哪根轴回报给调用方**,由新的 kind chip 在屏幕上写明——降级渲染绝不能被读成镜像。NEUTER-3 专咬这条(摘掉 fallback → 无镜像轮渲染空面板)。
- **顺带修好一个被 tab 结构掩盖的交互:** 旧的 toggle 判据带 `&& !tab`,而 state 入口**永远传 tab**,所以同轮再点从不收起。单按钮后 toggle 语义唯一(`same_round_toggles_closed` + `other_round_replaces_panel` 两条各钉一个方向)。
- **★ 对比度的真实根因不在这个面板的任何一条规则里,而在它复用的 `.debug-msg-*` 共享渲染器——角色标签**压根没有** light/tofu 覆盖。** 按各主题真实 `--bg-secondary` 实测:

  | token | light | tofu |
  |---|---|---|
  | role-user / -assistant / -tool | 1.67–1.99 | 1.75–2.08 |
  | debug-str / -num / -null | 3.69–4.26 | 2.75–3.40 |
  | `.debug-msg-summary`(--text-tertiary) | 3.05 | (dark 3.55) |

  即 tofu 纸底上的 `TOOL` 是 **2.08:1**——画出来了,但不可读。**这与 charter 已记的「同一份数据两套口径」同族,只是这次分叉的是「主题覆盖」:语义色/文字层历次都改过,角色标签从未进入任何一轮,因为它不在任何一张 token 表里。** 三主题按各自色系重挑至 ≥4.5:1(保色相),summary 移出 --text-tertiary,面板正文 10.5 → 11.5px/1.55(**它是被逐字读的 JSON,不是被扫的行列表**)。
- **★ 本轮最值钱的一条:静态守卫全绿之后,真浏览器抓出了它结构上看不见的缺陷。** 我给面板新加的 kind chip 用了 `var(--accent)`——那是**填充色**(按钮底、1-2px 边框),当文字实测 **3.49 / 3.71 / 3.00:1**。静态守卫为什么没咬:**它只给「已存在的 token」评级,而一个新加元素继承了一个填充色,恰好落在它的输入集合之外**。改 `--ri-kind-ink` 分主题后真浏览器复测 **10.20 / 8.26 / 6.56:1**。随后把守卫扩到面板自身 chrome(含 fallback 态必须与镜像态视觉可分)。**判据:contrast 类守卫的失效点在「扫描面」,而扫描面不会自己报错——加了新的着色元素,必须同时把它加进评级集合。**
- **量具本身两处缺陷,都是它自己抓出来的:** ①静态守卫的 dark 查找用裸选择器,会**子串命中**同名的 `[data-theme=...]` 规则,于是拿 tofu 的色去压 dark 底报假红——加固定宽度 lookbehind;②真浏览器探针必须**向上找第一个非透明底**再算,直接读元素自身 `backgroundColor` 会拿到 `rgba(0,0,0,0)`。
- **NEUTER 共 7 发全咬:** 静态 4 发(摘 tofu 角色覆盖 / 还原 2.75:1 的 tofu num / summary 退回 tertiary / 摘面板字号)+ 行为 3 发(摘 kind='state' / 摘 round-scoping / 摘请求 fallback)。每发均确认落盘后再跑,回合结束 `cmp` 复原。
- **★ 事故自报(两半,第一半是我自己):`cp` 恢复用的备份早于我最后一次编辑,把刚落地的 chip 修复一起冲掉了。** 备份取于 09:06:30,chip ink 改于 ~09:05 之后——`cmp` 报「IDENTICAL」是**相对那个陈旧备份**的一致,不是相对我的工作成果。**判据:NEUTER 的备份必须在每一发之前重新取,不能全程复用一份;`cmp` 通过只证明「回到了备份那一刻」,不证明「没丢东西」。** 第二半:同一时间窗内 `request_inspector.js` / `i18n.js` / p7 测试被回退到 HEAD(我从未 `cp` 过这三个),机制未查明——`git stash` 语义吻合(tracked 回退、我两个 untracked 新测试幸存)但 `stash show` 里没有我的内容,**故只记现象不下结论**。我一度按「兄弟 reset」通报,经对方举证(其 09:12:22 那次带 pathspec、只清 index)**确认我归因错误并已更正**。**判据:通报他人造成损失前,先把 reflog 条目的完整命令读完——带 pathspec 的 reset 不碰工作树。**
- **归账(charter #15):** styles.css 4 hunk 中 3 个是我的(第 4 个是兄弟 local-control 排版)、i18n.js 4 hunk 中 1 个是我的(其余 11 个键属兄弟)——两文件均走 `git diff > patch` → 按内容标记过滤 → `git apply --cached`;`tool_rounds.js` **完全排除**(工作树里只剩兄弟的 QR 实现,我那条注释已随回退消失,不值得再争)。提交前逐文件核对暂存内容,确认零兄弟内容混入。
- **验收边界(诚实分账):** ①纯前端,bundle 后台重建自动带,**刷新即生效,无需重启**;②真浏览器验收用的是**复刻 `_riMountToolPanel` 的静态 HTML**(生产 CSS + 生产 markup 形状),不是驱动真实 SSE 流——像素与级联是真的,数据链路未在浏览器里端到端跑;③`tests/_ri_panel_visual_check.py` 是**验收器具不是 pytest 用例**(需浏览器),已在 docstring 写明用法。

### 2026-07-29(续·扫码显示) — 后端二维码在前端可见:`run_command` 终端二维码「不是丑,是结构上不可扫」根因落地 + `ask_human` 扫码登录闸(新增 `lib/qr.py` 460L;新套件 **41 + 7**,**NEUTER×7 全咬**(其中**一发首版空转,是我自己的断言假**),干净 HEAD worktree **50/50**;wire-parity **2/2 无需重生基线**)

- **需求:** owner「后端生成二维码,前端怎么看见?比如要我扫码登录某个东西」+ 追加「希望 `run command` 的结果里也能显示二维码」。
- **★ 根因不是美观问题(这条决定了整个方案形态):** 终端二维码画在 `.ptool-cmd-output` 里,而该面板 `static/styles.css:7562` 是 `white-space: pre-wrap` + **`word-break: break-all`** —— 模块行会在任意列被重新折行,二维码的二维栅格**当场被摧毁**;且该面板默认 `display:none`(要点「Show output」才展开)。**所以再怎么调样式都救不了一个「必须用手机对着扫」的东西,它必须变成真位图。** 落点因此是「重建 bitmap」,不是「把字符画渲染得好看点」。
- **★ 偏极性不能假设,必须由 QR 标准自己裁决(实测两种工具方向相反):** `qrcode.print_ascii` 用 cp437 `[255=NBSP, 223=▀, 220=▄, 219=█]`、`pos = top + (bottom<<1)`,**块字符=暗模块**;而 `print_ascii(tty=True)/invert=True` 与多数 Go/Node QR CLI 是**反色**(块字符=亮模块)。猜错方向产出的是**照片负片,任何扫描器都读不出**。故实现把每种可能读法都构造出来,再用**三个 7×7 finder 角标**验证,只有真过标准的读法才输出——自纠而非「按第一个测过的 CLI 调参」。
- **★ 验收唯一标准是「手机能读」,所以断言必须真解码:** 用 `cv2.QRCodeDetector`(本机 cv2 4.13 实测可用)把交付的 PNG **解码回原始字符串比对**。**11 种终端风格 × 3 种尺寸(21/33/49 模块)= 33 组全部解码成功**;负样本 8 类(普通日志/box-drawing/进度条/块表格/markdown `###`/阴影块/spinner/空)**全部 0 误报**。
- **过程自纠三处,全是我自己的假设被实测推翻:**
  ① 我为支持 `##` 风格把 art-glyph 闸放宽后,**反而弄坏了原本可用的 `print_ascii` 与反色**——因为我给 `_trim` 加了句「finder 外沿是均匀的,所以剥完要退回一格」,**这是错的**:QR 最外行横跨两个角标 + 中间的浅色分隔带,**永不均匀**;退一格把 29 模块撑成 31 直接判废。
  ② `_candidate_blocks` 原按「art 字符出现的列」裁窗口,但 `print_ascii` 里**暗模块恰恰是那个空白字符(NBSP)**,于是裁掉了真模块(29 模块的码只剩 25 列)。改为不裁列、交给 `_trim` 剥任意颜色的均匀边框——缩进与日志前缀顺带一并解决。
  ③ 我第一版「顺序守卫」**是空转的**:`event['results']` 与 round 持有**同一个 list 对象**,所以把挂载挪到 `append_event` 之后断言照样过(实测 NEUTER 4 首发全绿)。改为在 emit 时刻**深拷贝快照**再断言——因为真正会坏的消费者是**会复制/序列化**的 SSE sink。重写后该发真咬。
- **落点(两条通道 + 一个咽喉):**
  | 通道 | 机制 | 为什么这样 |
  |---|---|---|
  | `run_command` 结果显示 | `_finalize_tool_round` 唯一咽喉扫 `meta.output` → 挂 `qrImages` | 三个 run_command 建 meta 处(本地/远程/project)**一次覆盖**,不写三份会漂的实现 |
  | `ask_human` 扫码登录 | `qr_login_question()` 落盘 PNG + 返回 `![](/api/images/x.png)` markdown | 问题文本走 `renderMarkdown`(`tool_rounds.js:347`),阻塞等人期间图就在卡片里 |
- **★ 扫码登录**不能**内联 base64(实测两条硬理由):** ①`autoTranslate` 开启时 `_autoTranslateHumanGuidance` 会把**整段问题文本**发去翻译再替换显示,1.5k 字符 base64 会被当正文送进 LLM 嚼一遍并改坏 ⇒ 扫不出;②`renderMarkdown` 会给 `/api/...` 图片自动补 `BASE_PATH`,反代/云 IDE 前缀下才加载得到,手写 `<img>` 没这个待遇。
- **体积实测(这条支持「QR 可以内联」而截图不行):** QR PNG data URI **0.8–2.2 KB**,**比它替换掉的 ASCII 字符画还小**(1710→770 / 6554→2238);SVG 形态大 13 倍故只出 PNG。因此不属于 `test_binary_blob_text_stream_guard` 防的多 MB 那一类,`idb-cache.js` 的 `imageDataUris[].uri` 剥离也不会碰 `qrImages`(独立字段),刷新后仍可扫。
- **依赖真缺口(顺手补):** `qrcode` **本机装着但 requirements.txt 从未声明**——干净环境会**静默降级**(生成返回 `''`),而对一个登录提示来说那看起来像功能坏了而不是缺可选件。已声明 `qrcode>=7.4` 并标注:检测半边只需 Pillow(已声明),生成半边缺包时 log + 返回 `''` 不崩(实测)。
- **wire-parity 零回归:** 无 QR 时 markdown 逐字节不变(`${qrStripHtml}` 为空串),`test_frontend_tool_rounds_wire_parity` **2/2 绿,无需重生基线**。
- **★ 共享 HEAD 事故(第三方视角实录,两起):** 我的 8 个文件被**两个不同的兄弟提交**连带带走——`4c3ad19a`(字标)吃掉我的 CSS,`6a5dec8d`(JOURNAL)吃掉其余 7 个。**我的计数断言正确 abort 了**(staged=7≠8)并因此定位到事故;兄弟随后自报「计数读到 8 仍继续」。内容 HEAD 上完整且干净 worktree 50/50 绿,**不做 history rewrite**(风险更大),仅在此留档。**另纠正兄弟一处误判:`tests/_qr_login_capture.py` 不是我的**(先前提交 `31f3519f`,当前正被第三个会话编辑),若按其建议「归我提交」会连带第三方在改的文件。
- **验收边界(诚实分账):** ①**纯后端 + 前端静态**,运行中进程不带,`run_command` 通道需**重启**才对新作业生效;前端改动需**刷新**。②`ask_human` 扫码闸给的是 `qr_login_question()` 这块**积木**,「扫完自动继续」需调用方起后台线程轮询上游状态后调 `resolve_human_guidance()`——本批未内建任何具体站点的登录轮询。③二维码**时效与清理未做**:落盘 PNG 无过期回收,`uploads/images` 会累积;登录 URL 带 token 等同凭证,`uuid4` 文件名只是「难猜」不是「有鉴权」,高敏感场景应另走带会话校验的路由。这三点均**未实现**,不是已完成项。

### 2026-07-29(续·logo 收官) — 家族 epic 以「前提蒸发」关闭:主 logo 回滚原版后 VTracer 家族与主 logo 工艺**重新自洽**,统一不再必要;像素精修版 11 枚留盘可一言重启;另实测主 logo 三处引用无 cache-bust 是真缺口

- **收官逻辑:** 家族 epic(`pt_651bd5a3078e450d`)的前提是「主 logo 已精确几何化 → 两套工艺不齐」。owner 对家族票答「退回修改」后我按**像素精修**路线改完(保留现行血统与道具、严格 32 网格、22px 道具可辨,生成器 `gen_family_pixel.py`,in-situ 闸图 `_gen/logo-redesign/family-pixel-gate.png`)——但复盘确认:A2 回滚(`c69c7aec`)后,家族与主 logo **同为 VTracer 工艺,重新一致**,epic 的统一目标由「回滚主 logo」这一事件达成,上线新家族反而再次引入两套工艺。故**关闭而非上线**;11 枚像素精修稿 + 闸图全部留在 `_gen/logo-redesign/family-pixel/`,owner 一言可重启。
- **暴露的真缺口(已报 owner 待决):** `tofu-welcome.svg` 三处引用(favicon/欢迎屏/侧栏)均无版本号,`max-age=86400` 让 owner 在 A2 回滚后仍看到缓存的 A2 一整天(「为什么还没回滚」的乌龙根因);编排面图标早有 `?v=` 机制,主 logo 反而没有。
- **边界:** 字标 CSS 全程未碰(peer `pt_91be4876d7c64bbe` 已收口字标统一,见其条目);本批零线上文件变更。
- **★ 自报共享 HEAD 卷带事故 + 一处误归属(两个错叠在一起):** 收官那条 journal 提交 `6a5dec8d` 的计数断言读数 **8 而非 1**,我**打印了却没有 abort**——`git add -- JOURNAL.md && echo "staged: $(...)" && git commit` 这个写法里计数只是回显,`&&` 链照常走到 commit。于是 QR 兄弟(conv ms5bjrsk)已暂存的 7 个文件(`lib/qr.py` 460L 新建 / `tasks_pkg/executor/_finalize.py` / `tools/human_guidance.py` / `ui/tool_rounds.js` / `requirements.txt` / 两个测试)被卷进一条 `docs(journal)` 提交。内容完好(对方独立复核:干净 detached worktree **50/50 绿**),不重置共享 HEAD(历史改写风险更大),双方一致同意**不为归属再花一次提交**——工作码在 HEAD 上比提交信息整齐更重要。
- **★ 第二个错在我的事故通报里(对方纠正 + 我实测确认):** 我把工作树里 ` M` 的 `tests/_qr_login_capture.py` 说成「你剩下的未暂存文件,自己提交」。实测证伪:该文件 `git log` 溯源到 `31f3519f`(早已在库),当前是**第三方会话正在编辑**,QR 兄弟从未碰过它。若对方照我的话去提交,就会把第三方的活儿**再卷一次**。**判据:通知受害方时「哪些文件属于谁」也必须实测溯源(git log),不能按「同批出现在 git status 里」推断——否则事故通报本身会制造第二起事故。**
- **★ 落成硬纪律(已存 project memory `shared-head-commit-count-assert-must-abort`):** 计数断言必须是**能让 shell 非零退出的真判断**(`[ "$n" -eq N ] || { git diff --cached --stat; exit 1; }`),不是 echo 一行给人眼看;任何无 pathspec 提交前 `git diff --cached --stat` 是强制预检——共享 index 里随时可能躺着别人的暂存。

### 2026-07-29(续·尺度同源·家族收官) — owner 实测第四刀:形态/配色/印章/落影/动效统一了五项,**唯独「尺度」从未进过任何一轮**——dark/light 的品牌区是 64px 图标配 24px 普通标题;而守卫看不见它是**因为一条豁免注释把两个轴一起豁免了**(epic `pt_b5cb0e2dff634bf7`;commits `cf26f48b`/`ccdf48ba`/`8a4995c1`;parity 套件 **46/46**,相邻环 **131/131**,干净 committed worktree **74/74**,**NEUTER×2 各咬各的方向**)

- **★ owner 用真实 index.html markup 量出的第四处漂移:**

  | | tofu | dark | light |
  |---|---|---|---|
  | 吉祥物 | **100px** | 64px | 64px |
  | 主标 | **42px** | 24px | 24px |
  | 印章 | 12.6px | 10.5px | 10.5px |
  | 图标下间距 | **24px** | 6px | 6px |

  即 dark/light 下这块区域是「一个小图标 + 一行和页面上任何 h2 无区别的标题」,**品牌时刻只存在于一个主题**。根因与前三轮同型:基础层 `styles.css:400` 是早年**通用**小尺寸,放大值写在 `[data-theme="tofu"]` 的 11333/11360;移动端阶梯(84/68px)同样只有 tofu 有,另两个主题**在手机上连缩放曲线都没有**。
- **★ 守卫为何结构性看不见,而且是一条注释造成的(本轮最值钱):** `_LETTERFORM_PROPS` 的注释只写「font-size is deliberately excluded」。该豁免在**双表面轴**(侧栏 15/18px vs 欢迎屏 24/42px)上完全正确,却**顺带把跨主题轴一起豁免了**;两套品牌套件里 `mascotPx`/`brandFontPx` 的断言数是 **0**。**判据:一条豁免必须写明它豁免的是哪个轴——只写「某属性被排除」会在另一个轴上留下无人看守的缺口,而缺口不会自己报错。** 注释已改准,并新增第 6 节按三主题 × 三断点断言尺度相等。
- **落点:** 尺度提到主题无关基础层,取 tofu 现值(100/42/24)——那是本轮设计(印章几何、落影椭圆、字距补偿)**全部围绕它求解**的比例,反过来把 tofu 拉小会让前三轮的实测结论失效。放大值锚在 `.welcome-icon` 与 `.welcome h2.tofu-brand`(0,2,1,严格压过基础层 `.welcome h2` 0,1,1),**不动通用 `.welcome h2`** ——它仍要服务非品牌的欢迎标题。
- **★ 我第一版的移动阶梯是死代码,是自己的守卫抓出来的:** 我把 84/68px 写在桌面尺度旁边,而**预存在的全局** `@media(max-width:768px){.welcome-icon{52px}}` 与横屏那条同为 (0,1,0) **且位置更靠后** ⇒ 浏览器解析出的仍是 **52px**。阶梯必须放在**文件尾部**才真正生效。两条全局规则原样保留(非品牌调用方行为不变)。**判据:同特异性下源序决定胜负,新写的「更具体意图」如果位置更早,就是一段读起来正确的死代码。**
- **NEUTER×2 分向咬:** 尺度退回 `[data-theme="tofu"]` 专属 → **9 红**(精确复刻 owner 报的形态,tofu 正确保持通过);移动阶梯搬回基础层 → **精确 2 红**(只有两条阶梯活性断言,其余不动)。另有一条 `test_the_breakpoint_flattener_actually_selects_blocks` 守住展平器本身——一个「全丢或全留」的展平器会让第 6 节整节空转。
- **★ 量具设计:`_cascade_at(css, width)` 按视口展平样式表,把命中的 @media 体**保留在原位置**内联(所以后置规则仍然获胜)。** 不能复用 `_desktop()`——它把 @media 整个剥掉,阶梯就不可见。`_media_applies` 刻意窄:任何提到它不建模的特性(横屏/reduced-motion/pointer/print)的条件一律判**不适用**,免得一个未建模的块静默满足尺度断言。
- **★ 我弄红了一条兄弟守卫,修法是把魔数派生化而不是放宽它:** `test_nc_reverting_landscape_to_500_only_regresses` 写死 `reverted.count(new_header) == 1`,而品牌区的横屏阶梯**合法地**新增了第三个共享该条件的块 ⇒ 假红。它的**意图**(NC 只许还原 layout 块,不许碰品牌块)是对的且已保留,改为**先取 header 计数、再断言这次还原恰好消耗一处**——比魔数**更强**,且不关心将来有多少品牌块。实测仍会咬:把 layout 块退回 500-only → 2 红。
- **验收(三主题 × 三断点,九格逐格相等):** 桌面 100px/42px/24px · ≤768px 84px/20px/18px · ≤480px 68px/20px/12px;九格落影全部不压 pills。交付图重出(`theme-{tofu,dark,light}.png`,含侧栏 lockup),**三张图品牌区一样大**。纯 CSS + 测试,**刷新即生效,无需重启**。
- **家族收官:** 字标(`4c3ad19a`/`a054eac0`)→ 印章+落影(`cd5111b3`)→ 动效(`9427e24a`)→ 尺度(`cf26f48b`),四轮同一根因的四个属性面全部收敛到主题无关基础层。dark 落影浓度仍按 owner 指示等实物观感。

### 2026-07-29(续·影子行为学) — owner 实测:落影**跟着吉祥物一起飘**,地面线摆幅 3.48px ⇒ 仍读作贴纸;根因是浮动挂在落影宿主上,而**冻结动画的守卫对这一整类缺陷结构性免疫**(epic `pt_9cb68659e06a465a`;commits `9427e24a`/`116163d6`;新套件 **17/17**,相邻环 **119/119**,干净 committed worktree **59/59**,**NEUTER×3 各咬各的方向**)

- **★ owner 的实测(这次机制和结论都对,与上一轮相反):** `animation:tofuFloat` 挂在 `.welcome-icon`(styles.css:11303),而落影是同一元素的 `::before` ⇒ 父元素 `translateY` 时影子**锁死同步移动**。9 帧采样 icon bottom `160.03→156.55`,**地面线自己在飘**。这恰好复活了引入落影本要解决的贴纸感——**真实接触影的行为是:地面线不动,物体升高时影子变小变淡。**
- **落点:** 浮动移到 `.welcome-icon img`(宿主静止 ⇒ 地面线钉死)+ 落影配**同周期同缓动的反相动画**(4s ease-in-out 同时起播,相位天然对齐;吉祥物到最高点时影子 `scale(.82)` / `opacity(.62)`)。
- **★ 浮动必须用独立变换属性 `translate` 而不是 `transform`(不做这一步两个动效互相掐死):** hover 态已占用 `transform:scale(1.06) rotate(-3deg)`,把浮动也写在 `transform` 上两者会**互相覆盖**——悬停掐掉浮动或浮动掐掉悬停。独立变换属性与 `transform` **各自成分复合**,可以共存。实测 hover 后:`transform` 带 scale/rotate 矩阵、`translate` 仍在 `-2.28px` 上继续浮动。同理落影的呼吸占用 `transform`,故它的水平居中从 `translateX(-50%)` 改为几何量 `left:11%`(=(100-78)/2)。
- **动效同属品牌形态,一并提到主题无关基础层:** `tofuFloat` 此前只在 `[data-theme="tofu"]` 块内,dark/light 下吉祥物**完全静止**——与前两轮「形态挂在 tofu 上」是同一类缺陷,只是这次落在**运动**上。另补 `prefers-reduced-motion`,且关掉动效后影子**停在贴地静止态**(scale 1/opacity 1)而不是动画中途某个收缩帧。
- **三主题实测:** 地面线位移 **0.00px**(要求 <1)、吉祥物顶边位移 **3.63~3.66px**(要求 >0)、影子 opacity 呼吸 **0.62..0.97**。
- **★ 守卫的结构性盲区(owner 点名,本轮方法论核心):** 既有像素守卫用 `*{animation:none}` **冻结动画**换取差分稳定,于是它**永远看不见运动缺陷**——影子被带飞的整段时间里,所有冻结帧断言都是绿的。新增三条按三主题参数化的运动不变量:①地面线位移 <1px 而吉祥物必须位移(**这条才是「影子 vs 贴纸」的判据**);②影子在最高点必须**同时**更淡更小(只是静止不动仍读作贴花,静止是必要不充分);③吉祥物在每个主题都必须真的动。相位用 **Web Animations API `pause()` + seek `currentTime`** 精确定位(0ms 静止 / 2000ms 最高点),**不靠 sleep 赌相位**,测试因此确定性。
- **★ 顺手修掉一个我自己埋的 harness 陷阱(值得记):** `*` **不匹配伪元素**,所以 `*{animation:none}` 从来没冻住影子自己的呼吸动画——两张冻结帧各自捕到呼吸的不同相位,差分 bbox 因此比图标下缘多伸出 36px,把 `test_ground_shadow_sits_under_the_mascot` 打红。改为 `*,*::before,*::after`。**这次报红是诚实的**:几何断言抓到的是它自己输入不自洽,而不是在噪声上蒙混过关——正好反证了上一轮补的「位置断言」是承重的。
- **NEUTER×3 分向咬:** 浮动挂回 `.welcome-icon` → **4 红**(三条地面线 + NC 自身);删影子呼吸 → **3 红**;浮动退回 tofu 专属 → **2 红**(仅 dark/light,tofu 正确保持通过)。
- **验收边界:** 纯 CSS + 测试,**刷新即生效,无需重启**。dark 落影浓度仍按 owner 指示等实物观感再定。

### 2026-07-29(续·落影像素闭环) — owner 报「落影三主题零可见像素」;实测**落影一直在画**,零像素来自**我和 owner 各自的探针假阴性**——但 owner 的结论「守卫证明不了它出现过」完全成立,已补像素级守卫并把落影真正移到脚下(epic `pt_f3a1704a6fd646ab` 续;commits `cd8da7af`/`2d7abd45`;新套件 **7/7**,相邻环 **73/73**,**NEUTER 删落影 → 6 条真红**)

- **★ 归因更正(我必须先说清楚,因为 owner 给的机制不成立而结论成立):** owner 判断是「图片盒与图标盒重合,落影被不透明贴图压住」。但 CSS 绘制顺序上,**定位过的 `::before` 画在非定位 `<img>` 之上**,所以这个机制说不通。逐层实测:`overflow:visible`、`opacity:1`、`content:""`、渐变真实存在——一切正常。把落影换成**纯红**仍是 0 像素,这在规则生效时不可能发生 ⇒ 问题在**探针**,不在产品码。
- **★ 两个假阴性成因(都会让探针对着一个正在绘制的东西说"没画"):**
  ① **`add_style_tag` 抢在首次布局/绘制之前**——注入的「关闭落影」样式和基线截图撞在同一帧,两张图逐字节相同,于是报 0。同一张 parity.html 加上等待后立刻测出 **1338 像素**。
  ② **file:// 外链样式表**——`document.styleSheets` 读不到(跨源受限),`rule present in sheets = 0` 而 `content` 却是 `""`,探针与屏幕互相矛盾。改为**把 CSS 内联**进 `<style>`,把加载器整个移出实验。
  实测确证:内联 + 等待后三主题分别 **949 / 452 / 908** 像素,且差异 bbox **水平落在图标范围内、贴近图标下缘**——就是脚下那片影子。
- **★ owner 的结论仍然成立,而且是本轮最有价值的一条:** 旧守卫断言的是「规则解析出 `content:""` 且有 gradient」——**这在落影完全不可见时同样成立**,它证明不了任何像素。这正是烧字幕那批的同型教训。新建 `tests/test_brand_shadow_pixels.py`:截「有影/无影」两帧做差分,**差异即落影**,断言不可能空转;并**同时断言位置**(水平在图标盒内、纵向在下缘),否则一片跑到别处的影子也能靠像素数蒙混过关——那恰是 `position:relative` 要防的失效。
- **探针自守卫(必须有):** `test_the_pixel_probe_can_actually_fail` 让两帧**都**关掉落影,断言差异**恰好为 0**。没有这条,`px>0` 可能被字体抖动/动画相位满足,套件在落影从未绘制时也会全绿。
- **落影确实该下移(owner 的产品判断对):** 图片盒与图标盒实测完全重合(同为 84×84、`imgTop=0`),落影留在盒内就**贴在贴图下沿**、读不出「托住」;`bottom` 取负探出盒外才像地面。同时三主题加深:
  | | tofu | dark | light |
  |---|---|---|---|
  | 旧(bottom:1px/.24) | 1893 | 461 | 970 |
  | 新(bottom:-5px/.34) | **2644** | **607** | **1247** |
- **NEUTER 分级咬得干净:** 删掉基础层落影规则 → **6 条真红**(三主题 × 可见性/位置);而只把 `bottom` 退回 +1px **仍然通过**——这是**正确**的,因为那时落影本来就可见、只是弱。守卫因此把「可见性」与「位置」分开断言,而不是揉成一个综合分。
- **交付图重出时又抓到一处 harness 缺陷:** 侧栏生产上是 `position:fixed`,把它和欢迎屏放进同一文档会**盖住欢迎屏**——之前那版交付图其实是把侧栏画在了吉祥物上面,这也是「落影看起来不见了」的又一个来源。改为**分别渲染再拼图**。
- **验收边界:** 纯 CSS + 测试,**刷新即生效,无需重启**。dark 落影浓度按 owner 指示留待实物观感再定。**方法论补记(owner 点名):** 「形态提到基础层」只保证规则同源,**不保证它出现在屏幕上**;落款印当时有计算样式+实拍双证据,落影只有 CSS 层证据——这一步差别就是本轮的全部教训。

### 2026-07-29(续·品牌形态同源) — owner 二次退回:字标统一了但**品牌形态还挂在 tofu 上**,切到 dark/light 品牌区整个退回改造前;顺带**测试骗了我一次而浏览器纠正了它**(epic `pt_f3a1704a6fd646ab`;commits `cd5111b3`/`6800a0ec`;守卫 **36/36**,相邻环 **71/71**,干净 committed worktree **55/55**)

- **★ owner 实测二次退回(我又只做了一半):** 上一批把「字标」三主题同源了,但**落款印与吉祥物落影**仍是 `[data-theme="tofu"]` 专属。owner 切主题实测 `.tofu-brand small`:

  | | tofu | dark | light |
  |---|---|---|---|
  | 字号/字重 | 12.6px / 700 | **11px / 400** | **11px / 400** |
  | 颜色 | 奶油 `#FCF8EE`(陶土印底) | **灰 `#6a6a7a`** | **灰 `#868490`** |
  | 形态 | 陶土落款印 | 裸字 | 裸字 |

  也就是说「在 42px 主标旁读作漏排说明文字」这个**本次要修的原始缺陷,在三分之二的主题里原样存活**;`.welcome-icon::before` 同理,另两个主题下吉祥物仍悬空。**判据:「统一」不能只统一我当时正在看的那个属性——形态、色彩、几何都要各自问一遍「它在三个主题下都成立吗」。**
- **落点与上一批同构:** 形态(印章几何 / clamp 下限 / 字距补偿 / 左右不等 padding / 倾角 / 落影椭圆)提到**主题无关基础层**一次性供给,主题层只准覆盖**印底色**与**落影浓度**。`position:relative` 也必须一并下沉——基础层 `.welcome-icon` 本来没有定位,一直在**借用 tofu 层的**,不补的话落影会锚到错误的祖先上。
- **★ 实测抓到两件读代码看不出来的事:**
  ① `[data-theme="tofu"] .welcome h2 small`(通用说明小字)与基础层印章规则**同为 (0,2,2) 且位置在后**,把印面字拽回 Jakarta 500/13px。改为 `:not(.tofu-brand)` 排除——它本来就只该管非品牌小字。
  ② 印章**自己的奶油字对旧印底只有 2.95:1**——这是本批之前就存在的可读性缺陷,被「只有 tofu 有印章」掩盖着。三套印底重新推导:印面字全部 ≥4.5:1、印章对各自页底 ≥3.0:1(tofu 4.82、dark 4.82、light 5.28)。
- **dark 修完后不需要任何印底覆盖**(基础层值在深底上就达标),于是它只调落影浓度——暖棕落影在深底上读作**脏污**,换成更淡的中性阴影。**少一层覆盖就少一处会漂移的地方**,这与本 epic 一路的判据一致。
- **★ 测试骗了我一次,浏览器纠正了它(最值得记的一条):** 新守卫报 tofu 下印面字重是 **500**,看起来像我刚修的 `:not()` 没生效。**先去 Chrome 实测——真实值是 700,CSS 没问题。** 根因在共享特异性引擎:它**不检查祖先复合选择器上的标签名**,于是 `.welcome h2:not(.tofu-brand) small` 里的 `h2:not(.tofu-brand)` 被 `.welcome` 这个祖先满足了(它的类集合确实没有 `tofu-brand`),而印章真正的父节点恰恰**就是**那个 `h2.tofu-brand`。加 `_seal_css()` 在解析印章前丢掉带 `:not(.tofu-brand)` 的规则(它们按定义就是不该到达印章的),**不动共享引擎**(兄弟套件依赖它)。**判据:守卫报红时先问「浏览器同意吗」——若不同意,要修的是 harness,不是产品码;信了它我就会去"修"一个不存在的缺陷。**
- **顺带删掉两个变空的 tofu 块:** 基础层印底修正后,tofu 的印章与落影覆盖变成**与基础层逐字节相同的副本**。同值副本正是会漂移的东西——本 epic 从头到尾就是在处理这个。
- **NEUTER 决定性验证:** 把印章与落影**退回 tofu 专属**(复刻上线前形态)→ **9 条真红,精确落在 dark/light**,而 tofu 正确保持通过(该主题本就该保住印章,这条不对称被显式断言,免得被误读成漏洞)。
- **验收(三主题实拍重出 `theme-{tofu,dark,light}.png`):** 三个主题下「豆腐」都是陶土落款印(700 字重、奶油印面字、微斜)、吉祥物都有接地投影。**纯 CSS + 测试,刷新即生效,无需重启。**

### 2026-07-29(续·三主题同源) — owner 实测退回上一批:**统一只做在 tofu 一层,dark/light 下仍是两套**;而真因不止配色——`.tofu-brand` 的字重/字距是被 `.welcome h2` 压死的**死代码**,同一份声明在两处得出相反结果(epic `pt_22f8e1009cfa4cce`;commits `a054eac0`/`5bd32d65`/`a14eded0`;守卫 **26/26**,相邻环 **61/61**,干净 committed worktree **45/45**)

- **★ owner 实测退回(我上一批的边界判断错了):** 我把共享真源挂在 `[data-theme="tofu"]` 前缀上,于是另外两个主题一点没覆盖到。owner 切主题实测:

  | | 侧栏 `.sidebar-brand` | 欢迎屏 `.tofu-brand` |
  |---|---|---|
  | font-weight | **700** | **600** |
  | letter-spacing | **+0.6px** | **-0.72px**(方向相反) |
  | 字母配色 | 紫渐变 `#8b6cf6→#a78bfa` | 金渐变 `#f5d5a0→#e8b86d` |

  dark 下就是**紫色侧栏 Tofu + 金色欢迎屏 Tofu**,同一品牌两个颜色。
- **★ 真因比「配色没统一」深一层,也解释了漂移为何长期隐形:** `.tofu-brand`(0,1,0) 自己写了 `font-weight:700` + `letter-spacing:0.08em`,但通用 `.welcome h2`(**0,1,1**)特异性更高,把它们压成 600/-0.03em;而**侧栏那份一模一样的意图却生效了**。同一份声明在两处得出相反结果,源码看上去两边都写了 700/0.08em——**死代码伪装成配置**。所以修法不是「把值抄一致」,是把真源上提到**主题无关基础层**,并让选择器带上下文(`.sidebar-header h1 .sidebar-brand`=0,2,2 / `.welcome h2.tofu-brand`=0,2,1)严格压过 `.welcome h2`,让「写下的就是生效的」;主题层此后**只准覆盖颜色**。
- **★ 修复本身又造出一次新漂移,靠重测三主题抓到(不是靠读代码):** 基础层真源建立后,tofu 块原来的 `[data-theme="tofu"] .sidebar-brand`(0,2,0)**反被新基础层压过**,实测变成「侧栏像素体 + 欢迎屏 Inter」。tofu 块的选择器必须同步带上下文。**判据:新增一层「更强的公共层」后,必须回测每一个原本依赖旧层级的主题,不能只测新加的那两个。**
- **字重 700 不是随手取的:** 字体包实测 Pixelify Sans **只随包提供 Bold 一个字面**(`static/vendor/google-fonts-local.css` 仅 `PixelifySans-Bold.woff2`),600 只会让浏览器**合成假粗体**,且两个字号下粗细还不一致。守卫直接读字体 CSS,将来多出字面会红,提示重新评估。
- **★ 守卫两处结构性缺陷,都是 owner 指出的:**
  ① **构造性失明**——每个 `_Elem` 硬编码 `theme='tofu'`,所以上面三行分叉**一条都测不到**,套件报 11/11 全绿而分叉正在线上。现按 `_THEMES=(tofu,dark,light)` 参数化;字母配色断言补上 `background-image`——字母是**渐变裁切文本**,渐变才是颜色,只比 `color` 是空断言。
  ② **守卫写线上样式表**——三个 NEUTER 用 `open(CSS,'w')` 改真文件再 `finally` 还原。共享工作树上两个方向都危险且**都已实际发生**:兄弟并发读到半写文件让套件**伪造失败**(`''.count` 读到空文件),更糟的是兄弟若在还原窗口内提交,会把 **neuter 过的 CSS 提交上线**。现全部改为**内存字符串**变异,并加 `test_suite_never_writes_the_stylesheet` 守住守卫本身。
- **★ NEUTER 必须冒充「该主题下真正获胜的那一层」:** 不带前缀的 neuter 在 tofu 下**完全无力**(主题块压过基础层)而**静默空转**。这个坑在本套件出现了**两次**(加了 light 成对覆盖后又复现一次),参数化让它按主题暴露,而不是藏在聚合的绿色里。另实测一条**不是缺陷**的现象:把同样的分叉注入到真源**之前**不会红,而 Chrome 也认为两处仍然一致——同特异性下源序决定,修复合法地吸收了它。
- **★ 统一暴露出一个被分裂掩盖的老缺陷(拍图时发现,不是评审发现):** 两处统一到同一套金系后,才看见这套色是**为深底调的**,落到 light 的近白侧栏(#f4f2ed)与白底欢迎屏上**每个字母都是 1.00–2.35:1**,其中「o」的起点 `#fdf2d7` 在白底上是 **1.00:1——字面意义上看不见**。只改明度不改色相(仍是琥珀/陶土),最差 3.26:1、多数 >4.5:1。**必须成对写两面**——只盖侧栏的单边覆盖正是被删掉那四条 light 靛蓝渐变的形态,也正是漂移的来路。
- **验收(三主题实拍,`static/icons/_gen/wordmark-preview/theme-{tofu,dark,light}.png`):** tofu = Inter 800 / -0.03em / brand-ink 两面一致;dark = Pixelify 700 / 0.06em / #e8e8ed 两面一致;light = Pixelify 700 / 0.06em / 深琥珀两面一致。**纯 CSS + 测试,刷新即生效,无需重启。**
- **过程事故(自报):** 本轮中途**兄弟会话的写入抹掉了我未提交的三处编辑**(freshness gate 先拦了一次,第二次文件已被整体替换),另有一次**兄弟 `git reset` 清空了我的 index** 导致 commit 落空。两次都未丢已提交内容,重做后改为**编辑完立刻原子提交**。与今日早些那条纪律同源:共享 HEAD 上未提交的工作随时可能蒸发,**窗口越短越安全**。

### 2026-07-29(续·字标统一) — 「两处统一 + 品牌区创意重设计」:侧栏字标停在旧工艺**而代码里看不出任何异常**,因为两处各写一套完整声明、漂移天然不可见;顺带**自报一起共享 HEAD 误提交**(owner 截图圈出两处;epic `pt_91be4876d7c64bbe`;commit `4c3ad19a`,12 文件 +888/-87;新套件 **11/11 含 NEUTER×3 各咬各的方向**,相邻环 **46/46**,干净 committed worktree 复验 **30/30**)

- **★ 根因不是「有人漏改了一个值」,是没有任何东西把两处绑在一起。** 侧栏与欢迎屏**各自持有一份完整的** font-family / font-weight / letter-spacing / color 声明,所以 owner 2026-07-28 拍板的方案 A 只落在欢迎屏、侧栏留在旧工艺,而两个 block 各自都是自洽的、**读代码看不出问题**。修法是提炼**单一字形真源**(`[data-theme="tofu"] .sidebar-brand, .tofu-brand` + 其 `>span` 子规则),两面共同消费,各面只许再声明自己的**字号**与悬停编排——不是把值手抄齐。
- **像素实证(生产 CSS + 真实 markup + 无头 Chrome,不是读源码):**
  | | 侧栏 `.sidebar-brand-o` | 欢迎屏 `.tofu-brand-o1` |
  |---|---|---|
  | 计算色 | `rgba(0,0,0,0)` 透明 | `rgb(169,101,54)` |
  | `o` 字形 | 隐藏,由 `::after` 渐变方块代画 | 真实字母 |
  | letter-spacing | `-0.01em` | `-0.03em` |

  `tofu` 是**默认主题**(index.html:33 兜底),所以两处都是每个用户的首屏。18px 下那块惨白方块读起来像**渲染故障**,不像字母。
- **创意重设计:3 候选 in-situ 实拍 → A 三稿精修 → A3 落地。** B(竖排豆腐)42px 下逼仄、细规线不可见;C(暖光纸笺卡)**把 pills 挤成两行**——布局回归,直接否决。A 落地为**落款印**:「豆腐」从 `.34em` 稀疏小字(在 42px 主标旁读作漏排的说明文字)变成微斜陶土印章;吉祥物补一片接地投影(此前只有 drop-shadow、没有落影,读作贴图而非放在纸上的物件)。
- **★ 印章字号必须是 `clamp(10.5px,.30em,13px)`,两个纯方案都被实拍否决(反直觉,记下来):**
  | 方案 | 桌面 42px | 移动 20px | 结论 |
  |---|---|---|---|
  | 纯 `px`(12.5) | 正常 | 印章几乎与主标同高,比例失衡 | ❌ |
  | 纯 `em`(.30) | 正常 | 解析为 **~6px**,中文两字糊成一团 | ❌ |
  | `clamp` 三值 | 取 12.6px | 取 10.5px 下限,可读 | ✅ |

  **判据:em 缩放看似「天然自适应」,但中文有绝对可读下限,纯比例缩放必然在小字号翻车。** 印章 padding 左右不等(.64em/.50em)同样是实拍定的——`letter-spacing` 会在末字「腐」后留一份字距,左右等距看起来右边更空。
- **★ 守卫第一版「通过了,但理由是错的」(我自己 neuter 抓出来的,不是评审):** 共享特异性引擎 `_selector_matches` **只按空白切分选择器**,于是 `.tofu-brand>span` 被当成一个无法解析的复合选择器、**永远不匹配**;两边都解析出 `None`,而 `None == None` 让 parity 断言绿灯空转。修法:本套件内 `_desktop()` 把 `>` 归一化为后代组合器(`_specificity` 本来就把 `[>+~]` 折叠成空格,特异性不变),**不动共享引擎**——兄弟套件依赖它。
- **★ 第一发 NEUTER 没咬,而原因是产品码比我以为的更稳:** 注入 `.sidebar-brand-o{color:transparent}` **(0,2,0)** 竟没让 o 变透明——共享 `>span` 规则是 **(0,2,1)**,多一个类型选择器压过它。这个差值正是「一条散落的单字母覆盖无法悄悄重开漂移」的承重结构,故补一条守卫钉住它,而不是让它继续当巧合。改用**忠实复刻上线前形态**的 NEUTER 1b(共享规则排除 o + 旧的透明 o & `::after` 方块)后,3 条守卫精确开火。
- **顺带清掉一处矛盾对:** 旧的 `.welcome-icon::before{content:none}` 与新落影规则同特异性且位置在前,虽不影响结果,但留着就是下一次漂移的种子——删掉而非叠加。
- **★ 自报事故:`git commit -- <pathspec>` 把两个兄弟会话的未提交 styles.css 工作扫进了我的 commit。** 我完整执行了 charter #15 的正解(diff → 按行号过滤 hunk → `git apply --cached` → 断言 index 中兄弟标记计数为 0 → 文件数计数 12,全部通过),然后在收尾 `git commit -F - -- static/styles.css ...` 上把它全部撤销——**pathspec 让 git 重新从磁盘读文件**,`.fallback-banner` 簇(~6281)与 `.ptool-qr-*` 簇(~7567)连同 ~592-719 若干 hunk 一起进了我的提交。
  - **不 revert**:`git show HEAD:static/styles.css | grep -c` 核实各关键字 9/5/4/2、**内容完好无损**,这是**署名错误而非数据丢失**,revert 会真的删掉兄弟的活工作。
  - **发现纯属偶然**:是 peer(`ms5bbwg8`)来问 `.lc-*` 边界、我顺手复查才看见的。**`git diff --cached` 全绿与 commit 是否干净没有因果关系**——前者读 index,pathspec-commit 读工作树,不同源;唯一权威的收尾断言是对 `git show HEAD` 而非对 index。已按此更正纪律,并把本项目**六条各自碎片化的同主题记忆合并成一条**(正因为碎成六份,它们谁都没在关键时刻浮出来)。
- **验收边界:** 纯前端 CSS + 测试,**刷新即生效**,无需重启。字标 markup 与 i18n 未动;`tofu-welcome.svg` 未动(owner 已两次现场否决 logo 改动)。**未做 owner 在岗试用**——按 2026-07-28 的方法论修正,品牌资产终审只能是在岗长期观感,本批预设可回滚窗口(单 commit,`git revert 4c3ad19a` 前需先剔除误提交的兄弟 hunk)。

### 2026-07-29(续·全库体检) — 「最近改动太多,还有没有残留 bug」的一次系统体检:**11,878 用例 / 3 条 CRITICAL / ~40 个失败文件里,只有 2 条是真缺陷,而两条同属我自己上一批漏的收尾**(commit `7d034057`,2 文件 +5;目标守卫 **20/20**,相邻环 **40/41**,于共享 HEAD 显式 pathspec 提交)

- **方法:三条独立取证线并行,而不是只跑测试。** ①活体运行时日志(error.log + 8.7GB 的 app.log)②干净 HEAD worktree 全量测试(必须隔离——主树有 49→63 项兄弟会话 WIP,脏树跑测试全是误判)③对每条真红做三态定性(真 bug / 守卫腐烂 / 环境)。**结论:测试红得多≠缺陷多,~40 个失败文件里真缺陷只有 2 条。**
- **★ 三条 `CRITICAL Uncaught exception — process is terminating` 全是噪声,而它们各自骗人的方式不同(这条最值得记):**
  | 时间 | traceback 顶帧 | 真相 |
  |---|---|---|
  | 07-29 07:54 | `File "<string>", line 5` + `TypeError: 'AppContext' object does not support the context manager protocol` | **agent 自己跑的 `python -c` 子进程**报错,被日志系统记成"进程终止"。主库 `app_context` 只有 `server.py:2691` 一处且已是 `async with` |
  | 07-28 14:46 | `server.py:3438 asyncio.run(_serve())` → `OSError: [Errno 98] Address already in use` | 重启时**旧进程未退新进程先起**,端口占用,非产品 bug |
  | 07-27 21:52 | `File "<stdin>", line 2` | 同 ①,agent 的探测子进程 |

  **判据:`<string>` / `<stdin>` 顶帧的 CRITICAL 是 agent 自己的子进程,不是服务崩溃——定责前先看 traceback 的第一帧是不是真文件。** 顺带证伪两条我一开始怀疑的:`POST /api/v1/browser/open-extensions` 线上 404 不是缺路由(`routes/api_v1/browser.py:190` 有),是**运行中进程比代码旧**;`app.log.2026-07-27` 8.7GB 也不是死循环,是 8 个 agent 满负荷一天的正常量(采样确认无超长行、无高频重复)。
- **★ 两条真缺陷是同一个根因的两半,且都是 charter 明文规定的收尾步骤:** `5f9564fe`(allow_private_hosts)把 `settings/private_hosts.js` 加进了 `js_bundler._BUNDLE_FILES`,但漏了:①`index.html` 的 dev fallback `<script>` 标签——20 多个 settings 兄弟脚本都有,**只有它没有**;该文件导出 4 个 `window._privateHost*`(被 HTML `onclick` 直接调用),打包失败走逐文件回退时全部 `undefined`,私网白名单面板按钮**静默失效**。这与 2026-07-28「侧边栏会话全消失」是**同型坑**(清单与 fallback 不同步)。②`globals.generated.d.ts` 未重跑,直接违反 **charter #8**。修法就是那两步本该做的事;`gen_frontend_globals.py` 在主树产出的 diff **恰好只有我的 4 个符号**,兄弟的 `renderModelFallbackBannerHtml` 未混入。
- **守卫腐烂 vs 真 bug 的分界(逐条实测,不按印象):** `test_code_quality` 报的 6 处"静默 catch"**全是误报**——守卫只按 AST 形状(`except` 后跟 `return`/`pass`)判定,看不懂返回值本身就是错误传达:`private_hosts.py:122` 是 `except ValueError: pass / else: raise` 的标准"探测 IP 字面量"惯用法(`pass` 是成功路径)、`_concat.py:381` 返回结构化 `{'ok':False,'category':'io','detail':…}`、`auth_sources.py:328` 的 `0.0` 是 docstring 明写的契约。同类:`test_events_use_single_round_key` 匹配到的是 EventSpec **docstring 里的示例** `{"round": 3}`(同块 `'roundNum'` 字段正确存在);`test_meituan_marketplace_models` 断言的是昨天 `90202d96` **故意移走**的 Claude 清单;`test_probe_nonchat_skip` 3 条是假函数签名少了新增的 `oauth` 形参;`test_export_js_sanitize_syntax` 自己的报错就写着「known offenders 不见了,请更新 sweep」。
- **★ 顺带暴露两条测试基建缺陷(未修,属独立工作项):** 全量跑**两次被卡死打断**——`tests/test_paper_terminology_audit.py` 真的去打线上 LLM(栈停在 `dispatch_chat` → `time.sleep(0.3)` 重试循环)、`tests/test_recovery_merge_guards.py` + `test_recipe_sources_card_spoken.py` 真的连 DB 阻塞在 `_core.py:852 cursor.execute`。conftest 本已为 2026-06-28 删 2300 会话的事故强制 sqlite,但它自己的注释承认「Several tests deliberately write to the REAL database」——**这类测试在生产库被 8 个 agent 打满时必然挂起,使全量跑不可能一次跑完**。
- **验收边界(诚实分账):** ①收集门 **11,878 用例 0 收集错误**,说明无 import 层断裂;②全量跑因上述卡死未能一次跑完,已分段覆盖 a-z(跳过 3 个卡死文件),失败清单完整;③两条修复的目标守卫 20/20 绿,相邻环唯一那条红(`test_export_js_sanitize_syntax`)**已在干净 HEAD 实测同样红**,非我引入;④纯前端静态资产改动,**刷新即生效,无需重启**。

