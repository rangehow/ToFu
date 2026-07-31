### 2026-07-31(续·tsc 棘轮归零:票面的一半是对的,另一半被实测证伪) — 自主派单接自己开的 `pt_65df12e6278b4dda`;**修的那 1 个错误只值 12 行,而本批真正的产出是发现「谁来守住放宽本身」从来没人管**(`pt_65df12e6278b4dda` DONE;commit `50466ec9`,2 文件 +56;探针×2 双向、**NEUTER×2 双向**;相邻 8 套 **31/31**;A/B 对拍 base **1 红** → mine **0 红**)

- **★ 票面第二半被实测证伪,所以我没实现它 —— 这比照做更省事也更正确:** 它建议「加一条守卫让 `test_frontend_globals_generated` 成为 CI 必过闸」。实测该文件**已经双重入闸**:既带 `@pytest.mark.unit`(`ci.yml:46` 的 unit job),文件名又匹配 `tests/test_frontend_*.py`(`ci.yml:115` 的 frontend job)。**它一直在守,只是没人去看它的红灯。** 新加机制只会多一份重复。
- **票面第一半成立且已修:** `local-control.js:128` 的 `navigator.userAgentData` 是 Chromium-only 的 UA-CH API,不在 tsconfig 的 `lib` 里 ⇒ TS2551。**产品代码是对的**(已有 `typeof navigator !== 'undefined'`,紧接着又查 `getHighEntropyValues` 是否为函数),且它读的是**唯一**能拿到 CPU 架构的来源 —— Apple Silicon Mac 在 UA 串里自称 "Intel Mac OS X"。故按该闸自己的提示改类型声明,不动产品码。
- **★ 声明刻意写成可选(`userAgentData?: any`),理由不是风格:** 那个调用点的全部意义就是这个 API **可能不存在**;非可选声明会把它正在处理的「缺失」这一情形**类型掉**,于是诱导后来者删掉那个守卫。可选声明让类型系统继续承认这件事会发生。
- **★ 两发探针,而第一发是空转 —— 记下来是因为我差点据此下错结论:** 我先把 `navigator.userAgentData;` 换成 `userAgentDataX;`,tsc **0 错误**,看起来像「新声明把真实拼写错误一起消音了」。查 `grep -c` 实测该串在文件里出现 **0 次**(真实行尾是 ` : null;`)⇒ **变异是 no-op,文件被原样写回**。换成真实存在的形态后精确报 `TS2551` 且提示正确拼写;补发一条跨文件全局探针(`_lcUpdateBadge` → `_lcUpdateBadgeXYZ`)报 `TS2552`。**判据(与 7-30 那条同源):NEUTER/探针空转时,先 `cmp` 证明字节真的变了,再谈守卫是否承重。**
- **★ 本批真正的发现:被放宽的接口本身没有任何守卫。** `globals.d.ts` 合法地放宽了 Element/EventTarget/Navigator 等几个 lib.dom 接口(tsc 无法把 `getElementById()` 收窄到具体子类型),但这**只在它保持显式属性清单时**才站得住。一行 `[key: string]: any` 就能让棘轮当场停止做本职 —— 每个被放宽接口上的**任意**属性拼写错误都会类型通过,而文件读起来仍像一份严谨声明。这与该模块 docstring 已记录的「手写 `declare var` 把契约降级成注释」是**同一失效、低一层**。故新增 `test_ambient_dom_widening_stays_scoped_not_blanket`,守的是**形状不是内容**:新增一个命名属性属日常维护、无需改测试;伸手写通吃签名则必须先说服这条守卫。
- **NEUTER×2 双向:** ①注入 `[key: string]: any` → 精确只咬新守卫那一条;②把**同样的字面量写进注释** → 保持沉默(按 charter #24 用共享 `_source_scan.strip_comments` 先剥注释 —— 这个文件本身就在用散文解释这条规则,一个能被注释触发的守卫是会训练人忽略它的假警报)。
- **★ 我自己引进并当场修掉的一个缺陷:** `insert_content` 的锚点同时匹配了 decorator + `def`,把原函数的头和体劈开,留下一个**没有函数体的孤儿 `def`** ⇒ `IndentationError`,整个文件 collection error。是跑测试抓的,不是自查。**教训:往测试文件里插函数时锚点要选在函数体末尾的语句上,不要选在下一个函数的 `@decorator`/`def` 行上。**
- **回归判据:** 工作树 tsc **0 错误**;相邻 8 套 frontend 守卫 **31/31**;纯净 HEAD vs 纯净 HEAD+我的 2 个文件同组对拍 —— base **1 红**(正是本票要修的那条)、mine **0 红**,零新坏。共享树 65 个兄弟 WIP,按 pathspec + 计数门(`-eq 2`)提交。

### 2026-07-31(续·「本对话如何被项目大脑影响」这个面板本身在说谎) — owner 报「目标那条根本没显示、而且是不是被截断了」;**两条都成立,而查下去是同一个类的四个实例**,其中一个是**注入面与工具面两个渲染器被搞混、而布尔值恰好一致所以一直绿**(commit `994b1f66`,7 文件 +638/-62;后端 **10 条失败先行**(旧后端 10 红/新后端全绿)+ 前端 **13 项 jsdom 检查**,**NEUTER×6 各咬各的**;相邻环 **399 passed**,2 红均在纯净 HEAD 复现)

- **★ owner 的两句话各命中一个真缺陷,而我先取证再动手,结果发现是四个而不是两个。** 面板标题写着「本对话如何被项目大脑影响」——一个可证伪的声明,于是我逐条去证:
  | # | 缺陷 | 实测证据 |
  |---|---|---|
  | ① | **Goals 泳道压根不存在** | `build_conv_influence` 返回的 key 只有 `['board','charter','convId','pendingDecisions','projectPath']`,**没有 `goals`**;而 `_inject.py` ★4.455 每一轮都在发 **308 字符**的 `[PROJECT GOALS]` |
  | ② | **`board.injected` 读的是错的渲染器** | 它调 `render_board_block`(**工具**面,18,178 字符),而提示词用的是 `render_board_injection_block`(**8,845** 字符,缩略成 id+标题) |
  | ③ | **「已注入」与「需调工具才可见」混成一张列表** | 全部平铺;一个意为「模型已经读过」的界面同时在展示模型必须花一轮工具才拿得到的东西 |
  | ④ | **epic 标题裸渲染**(owner 说的「截断」) | 线上 epic 标题实测最长 **1,628** 字符,而容器是 `max-height:40vh` ⇒ 一条就把后面所有行顶出面板 |
- **★ ② 是本批最该记的一条:两个渲染器都非空,所以布尔值一直一致,错的调用点因此活了下来。** `bool(18178字符)` 和 `bool(8845字符)` 都是 `True` —— **一个布尔断言在结构上不可能发现这个 bug**。所以新守卫比的是**尺寸**:`inf['board']['chars'] == len(prompt_block)` 且 `!= len(tool_block)`,并且先断言 fixture 本身让两者真的不同(否则测试是惰性的)。**判据入库:当两个候选来源都通过同一个真值测试时,守卫必须钉住能区分它们的那个量,而不是那个真值。**
- **★ ③ 的形状:这是本轮反复出现的同一族——一条断言着不再/并非为真的事情的消息。** 面板把「模型已读」和「模型可去读」画成一样,等于把后者静默升格成前者。修法是两个通道 + **每条注入泳道带实测字符数**(`goals.chars` / `charter.chars` / `board.chars`,全部由**调用 `_inject.py` 调的那一个函数**得出),再加一条 `abridgedInPrompt`:面板显示完整正文而提示词只发了约 200 字符的标题,这个差额必须由后端说出来,而不是留给人自己发现。
- **★ 一发前端 NEUTER 打空,而打空的原因值得单记:我把 tools 容器的 class 改成 injected 的,守卫仍绿。** 因为 `querySelector('.pb-inf-channel-injected')` 命中的还是**真正那个**注入通道,而它里面**本来就没有** tool 行 ⇒ 检查结构上碰不到我的变异。**这是「形式上扰动一个符号」而不是「模拟真缺陷」的又一例(同族第 N 次)。** 改成把 tool 行真的拼进注入通道的 body,精确咬红。
- **★ 同一类问题也出在测试桩上,而它会让一条永远打不响的断言长期为绿:** harness 的 `t = (k) => k` 返回裸 key,于是 `'{n} chars'.replace('{n}', 308)` 这一步**在测试里完全不可观测** —— 一个从不打印数字的成本徽章会通过。把桩改成对被测 key 返回真实模板后,`goals_cost_is_backend_number` 立刻转红,修好才绿。
- **顺带修掉一条既有红(非本批引入,纯净 HEAD 复现):** `_seed` 调 `commit_charter(content=…, add_decision=…)`,而该 API **明确拒绝这个组合**(`invalid_combination`,注释写着「partial application is worse than none」)⇒ `test_influence_split_from_conv_a` 一直红。拆成两次 commit。
- **刻意不做:** `pendingDecisions` **不放进任何一个通道**——提案只有人类提交后才到达智能体(章程 #0),把它画成「影响」会是第五个谎。它单独一栏,标题直接写明「未提交前不会到达任何智能体」。
- **共享树纪律(章程 #15):** 提交前 `git diff --cached --numstat` 断言恰好 **7 行**,兄弟会话大量 WIP 未被带走。
- **验收边界:** 纯读路径 + 渲染改动,**不改任何注入行为**(模型收到的字节完全不变);需重启 + 重建 bundle 才可见;真浏览器未实测。

### 2026-07-31(续·被拖拽的宠物没有「被拎起来」的感觉:纵向通道**结构上不存在**) — owner 报「there is no vertical change」;实测不是手感调参问题,而是**整个模块把指针的 Y 分量丢掉了**;而本批最有价值的一条是**一发 NEUTER「没咬」,追下去发现真 bug 确实存在、只是我的场景够不到**(`pt_eb55025ac4684cda` DONE;commit `2da9ea1f`;新守卫 **10 条**,对未修复 HEAD **7 红失败先行**;**NEUTER×4 各咬各的**;宠物环+场景环 **215/215**)

- **★ 先量再修,而测量把「手感不好」变成了「通道不存在」:** `_place()` 只写 `transform = 'translateX(' + W.x + 'px)'`,**没有 `W.y`**;全文件 `clientY`/`movementY`/`offY`/`startY` 命中数 **0**。把鼠标抬到屏幕顶端,精灵**一个像素都不动**。drag 态改变的全部只有 4 项:`cursor:grabbing`、`z-index:5`、落影缩到 `12x3` / `opacity .55`。
- **★ 而第 4 项正是它读起来「不对」而不仅是「没动」的原因:落影在演一场身体没有参演的提起。** 一个贴地不动的精灵配上正在缩小的落影,比「落影完全不变」**更强烈地**暗示「贴着地面滑行」——因为两个通道互相矛盾。**判据入库:两个通道讲不同的故事,比一个通道沉默更糟。**
- **修法是补齐三拍(举起→携带→落下),而不是加一个 CSS hover 动画:**
  | 拍 | 机制 | 为什么必须这样 |
  |---|---|---|
  | 举起 | `LIFT_GRAB_PX` 在**状态进入时**断言 | 纯靠指针位移推导的话,**横向拖拽永不产生高度**——正是 owner 报的那个现象 |
  | 携带 | 跟随指针自身升降,自 press 起点度量,钳 `LIFT_MAX_PX` | bar 是 `overflow:visible`,可以升出栏外;不钳则一次快甩把精灵扔到页面上方 |
  | 落下 | `fall` 态在 rAF 里积分重力 + `data-landing` 压扁 | 从高处**瞬间归零**永远读不出「曾被握住」,与「抓起无升起」是同一个单帧跳变问题 |
- **★ 一个 transform 写者不变式:** `transform` 是**一个属性不是两个通道**,所以 X/Y 必须在**同一条声明**里写。这也是落下为什么在 JS 里积分而不是用 CSS 动画——位置层上的动画会**永远压过**内联 transform,把每帧的 translateY 打掉。落地压扁因此挂在**帧层**(与 pivot 同理)。
- **落影改为由 `--pet-lift` 连续驱动而非 `[data-state="drag"]`:** 状态选择器在**松手瞬间就失效**,而那时宠物**仍在空中(正在下落)**,于是落影会在精灵还没着地时**弹回全尺寸**。高度驱动同时解决了「两档跳变」与「下落期失效」两件事;太阳方向通道 `--pet-shadow-dx/scale` **刻意不覆盖**,否则提起会顺手废掉场景光耦合。
- **★ 本批最有价值的一条:NEUTER「没咬」不等于机制多余,追下去它救了一个真 bug。** 摘掉通用状态超时里的 `fall` 排除项后,快速拖拽的测试**全绿**——按这个证据它像是多余的防御。**不是。** 拖拽通常从 `walk` 进入,而 `walk` 的 `until` 在 **1400–3000ms 之后**,所以短拖拽期间那条分支**无论怎么写都够不到**。把宠物**握住几秒**(完全正常的操作)后陈旧 `until` 过期 ⇒ 去掉排除项后**第一个空中帧**就调用 `_pickNext()`,宠物被捕获进休息态。实测 6 秒长按:停在 **`translateY=-12` 且 state=`sleep`**——**永久悬空,再没有任何一条腿能把它放下来**。已单独立测。**判据:NEUTER 打空时先问「我的场景够得到那条路吗」,再下「机制多余」的结论。**
- **守卫是失败先行的,且是对**未修复的 HEAD** 证明的:** 10 条里 **7 条红**,第一条的报错逐字是「the engine writes no translateY at all — the sprite has no vertical channel, which is exactly the reported defect」。它们用**真指针序列**驱动**出厂模块**,配手动泵的 rAF,再**读回引擎自己写的 transform**;测试里**不重写一份物理**。**刻意不钉**具体高度与缓动曲线——那是口味,会重调,钉了就变成变更探测器。
- **★ 陈旧兄弟守卫就地反转:** `test_frontend_tofu_scene::test_css_pet_has_contact_shadow_that_detaches_on_drag` 要求存在字面的 `[data-state="drag"]::after` ——**正是本批取代掉的机制**,所以旧断言只能靠「把缺陷放回去」满足。改为断言**性质**(提起必须同时缩小**并**变淡),两种机制都能满足。**它的 NEUTER 也得一起修**:原来用 `count=1` 剔除 `.tofu-pet::after`,而落影现在是**两块**,于是剔掉一块还剩一块 ⇒ **误报通过**。
- **★ 共享树纪律,以及我自己犯的一次:** `styles.css` 里有兄弟**未提交**的 17 行 `.pb-inf-*`,用 `git add -p` 只暂存我的两个 hunk 把它排除了(事后核验:提交里 `+/-` 行含 `pb-inf` 的为 **0**,兄弟改动仍完好留在工作区)。**但最后一次提交我漏写了 pathspec**,把兄弟一条 12 行的 JOURNAL 条目(`pt_58a88295`)卷进了我的提交。内容**完好未丢失**(已核验在 HEAD),但那是别人的工作挂在我的提交信息下。**教训:`git add -p` 护住了一个文件,却因为最后一步省掉 pathspec 而在另一个文件上翻车——纪律必须覆盖每一次提交,不是覆盖「危险的那次」。**
- **验收边界:** 纯前端,**需重启 + bundle 重建**;真浏览器未实测(证据为 node 驱动真模块的指针序列 + rAF 泵)。

### 2026-07-31(续·验收清单第①条本身就是我在修的那个缺陷) — 自主派单接 `pt_58a88295d4024055`;票面要求「首次发版人工盯 run 日志三条」,而**第①条「确认这步是 success 不是 skipped」正是一条活在人的记忆里的不变量——与本 epic 修的东西同型**(commit `1b0b8a5f`,2 文件;新守卫 **5/5 失败先行**,**NEUTER×4 各咬各的**(4/1/1/1);相邻环 **87 passed**)

- **★ 先确认真做不到再谈别的:** `gh` 未安装、无 `~/.git-credentials`、无 `credential.helper`、`GITHUB_TOKEN`/`GH_TOKEN` 均未设;直接 `POST /actions/workflows/.../dispatches` 实测 **401**。所以「触发一次真实 run」在本环境结构上不可达,这一条只能开票。
- **★ 但票面把三条并列是错的分类,拆开后第①条根本不需要人:** ②③(闸对 0.16.0 输出 DOCUMENTED、传 0.99.0 能红)已由上一批的干净 checkout 端到端测试覆盖——那些测试**真的执行**了 shipped step body。剩下的第①条「这步是 success 不是 skipped」**是 workflow 可以自己回答的**,把它留给人眼等于给这个 epic 留了一个它自己在批判的东西。
- **★ 为什么 skipped 是最坏的一格(这决定了修法必须是断言而不是文档):** GitHub 把 skipped 步骤渲染成**灰色**排在绿色旁边,**job 依然成功**。于是一个 `if:` 不再匹配的闸(输出被改名、`version` job 被重构、条件被写反)会**无声退化成零**,没有任何红点,而发版照旧发出去、run 日志**看起来完全正常**。静态可达性套件求值 `if:` 表达式,能抓住「今天写错」;抓不住「语法没问题但运行时不匹配」。
- **落点:** 闸加 `id: changelog_gate`,后置一步比对 `steps.changelog_gate.outcome` 是否为 `skipped`(以及空值),命中则 `::error` 注解 + `exit 1` ⇒ `version` job 红 ⇒ 所有 build job 因 `needs: version` 结构上无法启动。
- **★ 自检必须与被检者同条件,这一条是 NEUTER-C 逼出来的:** 若自检的 `if:` 比闸更窄(比如只在 `workflow_dispatch` 时跑),就会存在**闸可以无人观察地 skip 的发版运行**——一个作用域小于被检对象的检查器。守卫直接断言两者条件字符串相等。
- **NEUTER×4 各咬各的:** 删掉自检(复现原始缺口)→ **4 红**;`exit 1` 改成 echo 警告 → 1 红;自检条件收窄成 `event_name` → 1 红;摘掉闸的 `id`(outcome 变得不可观测)→ 1 红。四发后 workflow 均 `cmp` 逐字节还原。
- **一个自己抓回来的 harness 缺陷:** 排序守卫用 `s is _selfcheck_step()` 比对象身份,而两次 `yaml.safe_load` 产出的是**不同对象** ⇒ 它因与产品无关的原因失败。改为按内容定位。**又一次:先确认守卫是因为真缺陷而红。**
- **仍然缺口(如实留票):** runner 侧那一次真实执行。票面已更新为**只剩第③条需要人**(传 `version_override=0.99.0` 确认 build job 不启动),①已机器化、②本地已端到端验过。
- **验收边界:** 纯 CI 配置 + 测试,**零产品码、零前端改动**,无需重启。GitHub Actions 未实跑。

### 2026-07-31(MCP 供应链可复现 + 我自己引进的 ECOMPROMISED 回归) — owner 报「overleaf `'Server' object has no attribute 'list_tools'`,而且不只这一个」;**真因不是 pin 漏了一处,是 MCP server 的依赖树从来没有任何锁定**;而我的第一版修复**修好 uv 侧的同时打挂了 npx 侧**,是 owner 实测抓回来的(commits `0c93162d` + `dae51563`;守卫 9→**14**,**NEUTER×10 各咬各的**;相邻环 **182 passed**;真实 bridge **7 OK / 0 FAIL**)

- **★ 先证伪自己的第一诊断,它决定了整个方案形状。** 我起初打算给 `mcp_servers.json` 的 overleaf 规格加 `--with 'mcp<2'`。构造 v1/v2 各一个真 server + 版本无关 client 跑 **2×2 真进程互通矩阵**:**四格全绿**,v1 客户端↔v2 服务器**双向** `tools/list` + `tools/call` 均通,协议一律协商到 `2025-11-25`。⇒ **SDK major 不是 wire 兼容边界**,`mcp<2` 保护的东西在协议层根本不存在。
- **★ 真根因是「没有锁」,证据来自磁盘而不是推理。** 扫 `data/mcp-cache/uv/archive-v0/` 178 个环境,挑出装了 overleaf 的 30 个:**同一个规格字符串、同一个包版本(0.2.1 从没动过),解析出 5 个不同 mcp 版本**——1.28.1×16 / 1.28.0×5 / 1.27.2×5 / **2.0.0×3** / 1.29.0×1。**漂移 100% 在传递依赖上**。⇒ 钉规格没用、隔离环境也没用(那只是把一个不确定的大环境换成 N 个不确定的小环境),每次冷解析都是一次独立抽奖。今天是 mcp,明天可以是 pydantic/httpx/anyio。
- **★ 否掉了「每个规格钉死版本」这个直觉方案,用数据。** `registry.py` 目录项**浮动 50 / 钉死 0**;而 `mcp_servers.json` 是 **gitignored 用户数据**(`.gitignore:31`),守卫扫它在新克隆上必然空转通过。两种形状同一个毛病:**管声明,而不管真正发生解析的那一处**。
- **落点:时间截断,注入在 `_ensure_writable_caches`** —— 每个 launcher 子进程都过的唯一那条缝(`_bridge.py:748` 就作用在交给 `StdioServerParameters` 的那个 env 上),故 uv/uvx 与 npm/npx **一并覆盖,含尚不存在的 server**。实测:同 cutoff 两次**冷缓存**解析 → 依赖树**逐字节相同**(911B);npm `--before` 同样钉住传递树(`zod@3.24.1` vs 无约束的 `3.25.76 + 4.4.3`)。端到端:`mcp 2.0.0 + AttributeError` → `mcp 1.28.1 + import OK`。**是地板不是笼子**:operator 自设的 `UV_EXCLUDE_NEWER` 优先,`TOFU_MCP_SUPPLY_CUTOFF=''` 可完全关闭,两向都钉了守卫。
- **顺带补的真缺陷:`install.sh:1059` 的 `"mcp>=1.0"` 无上界**,第 1239 行被 `conda install -c conda-forge` 真实消费,而 conda-forge **已有 2.0.0**。pin 守卫**从没扫过这个文件**——把它加进站点的瞬间就红(1 failed / 9 passed),**红灯抓出的是真缺陷,不是我造的**。
- **★★ 而我这批的第一版自己引进了一个更难看的回归,是 owner 实测抓回来的,不是我自查出来的。** cutoff 让 npm 开始拿 `--before` 去对照 `_npx/<hash>/` 里**既有的** `package-lock.json`;那份 lock 是**没有 cutoff 时解析的**,能命名 cutoff 之后发布的版本 ⇒ npm 判定 lock 不可信,直接 `npm error code ECOMPROMISED / Lock compromised`。同缓存 A/B:**cutoff ON → 3/3 全挂;cutoff OFF → 正常启动**。
- **★ 它的分布是最坏的那一种:从没跑过 Tofu 的机器完全没事(没有 slot),而每一个既有部署在 cutoff 落地那一刻,所有 npx 类 server 立刻全挂。** 新用户看不到、老用户全崩。**教训写进守卫 docstring:一个改变解析结果的变更,必须同时负责迁移「按旧规则已经解析出来的东西」,否则「可复现」只对空盘成立。**
- **`npm cache verify` 修不好**(实测清了 86 个坏条目照样挂)——slot 不是内容寻址缓存,是**已物化的安装树**。
- **判据只能自己造:npm 不把 `before` 写进 lock 的任何位置**(grep 验证),所以陈旧性无法从 npm 自己的元数据读回。故 `_reconcile_npx_cache` **在 lock 旁边打自己的标记**:缺失/不一致即驱逐,一致即跳过。这让判定**精确而非启发式**(不解析版本、不比较发布时间),且**每个 slot 只付一次**——实测第二遍 github 44.0s→**4.2s**、12306 27.1s→**7.9s**,而全量 npx 冷装实测 55.4s,「每次连接都清」本身就是一场故障。
- **失败先行是对着「已发布的那个 commit 状态」做的:** 摘掉接线(= `0c93162d` 逐字状态)→ `test_reconcile_is_wired_into_the_launcher_env` **精确变红**。**NEUTER×4 各咬各的**:永不调用(tested-but-not-wired)1 红 / 去掉标记检查导致每次全清 1 红 / 驱逐但不打标记 2 红 / 连无 lock 的 slot 也清 1 红。
- **验证一律走真实 bridge,不是合成 env dict:** 重建坏状态(slot 无标记)后 `MCPBridge.connect_server` 逐个连——**7 OK / 0 FAIL**,原始那条 `AttributeError` 消失,overleaf 发现 21 个工具。
- **rollinggo-flight 置为 `enabled:false`:** 厂商端点 `/mcp/flight` 实测 **404**(`"No static resource mcp/flight"`),`/mcp` 现在只提供**酒店**(serverInfo=`MCP Hotel Server`,3 个酒店工具)。**这是外部变更,代码修不了**,原因写进 description。逐字段对拍确认只改 `enabled` + `description`,**凭证未动**。
- **★ 共享工作树教训(值得单独记):我的第一版改动被兄弟会话的并发 checkout 整个回退掉了**——`_vendor.py` 与测试文件双双回到 HEAD,而我当时用来做 NEUTER 的 `/tmp` 备份**是在回退之后拷的**,所以备份也是干净 HEAD、救不回来。重做后**立即提交**。写入侧的 freshness gate 起了作用(拒绝了一次基于陈旧内容的插入)。**判据:在共享树上做多轮 NEUTER 时,备份必须在第一次编辑之前拷,且改动落地后尽快提交,不要攒批。**
- **未修 / 已开票:** ①`pt_0df1c87a8439405c` —— **npx 冷启动结构性超预算**:`MCP_CONNECT_TIMEOUT=30`(就绪上限 65s)而冷启动实测 55.4s / 27.1s / 44.0s,热启动仅 4-8s;**不要简单调大全局值**(会让真崩溃的 server 也拖几分钟才报错,`types.py:18-25` 已明确区分「崩溃」与「等待」)。②`pt_4f0e176578cb44a9` 的 L2(解析域隔离:hope/xuecheng/llm 经 `_install.py:99` 装进 `sys.executable`)与 L3(客户端迁 v2,incompat 面实测仅 5 处;v1 宿主 `LATEST=2025-11-25` **结构上说不了** `2026-07-28`,而 v2 能向下协商 ⇒ 迁移是净扩容)仍未动。
- **验收边界:** 后端已对活库生效,**无需重启即可复现**(我是直接驱动真实 bridge 验的)。`data/config/mcp_servers.json` 是 gitignored 用户数据,不在提交内。

### 2026-07-31(续·「第四个时钟」被证伪,但同一段代码里藏着更坏的东西:并行工具结果按一个恒为 `''` 的 id 寻址) — owner 指出 `future.result(timeout=300)` 是第四个墙钟;**实测证伪了这个诊断,而顺着它查下去撞上了真正制造「无结果」的那个 bug**(`pt_12f37f7fbf9c4d83`;commit `b1386039`,4 文件 +353/-32;守卫 16→**24**,**NEUTER×4 各咬各的**(3/1/1/1);相邻环 **116/116**)

- **★ 先说证伪,因为它是本批最省钱的一步:`as_completed` 只产出「已经完成」的 future,所以那个 300s 永远不可能开火。** 实测三档延迟(0.05/0.4/2.0s)经 `as_completed` 产出时 `done=True` **全为真**,拿 `timeout=0.01` 去读 2.0s 的任务照样拿到结果;而**同一个 future 不经 `as_completed` 直读**,0.2s 就抛 `TimeoutError`。⇒ 它是死重量,不是活的上限。**仍然删掉**:一个暗示了并不存在的边界的数字,对下一个读者就是陷阱。
- **★ 真 bug 在同一段代码,而它的成因是「兜底永远不生效」这种最难看见的形状:** 结果按 `tc.get('id', str(uuid.uuid4())[:8])` 做键,而 `lib/llm/_sse_core.py:854/922` 建槽位时写的是 `'id': ''` —— **键一直存在,所以 uuid 兜底永远不触发,也就永远无法去重**。两个后果都实测复现:
  | 形态 | 实测结果 |
  |---|---|
  | 两个 `id=''` 的并行调用 | 塌成**同一个 dict 键**,`{"command":"pytest"}` 收到的是 **git status 的输出** —— 静默,无报错无日志 |
  | 兜底万一触发 | 执行时与查表时**各自 mint 不同 uuid** ⇒ 查不到 ⇒ 模型收到字面量 **`(no result)`**,正是屏幕上那四个字 |
- **而这正是 `coder-tests` 真实走过的分支:** 它 round 1 派了**两个**并行 `run_command`(线程 `agent-coder-tests-tools_0` / `_1`)。owner 关于「这条分支就在事故路径上」的判断完全正确,只是致命的不是那个 300。
- **修法选位置索引而不是「补一个 id」:** 位置是批次的内在属性,**结构上不可能碰撞**,映射天然是全的;而「给空 id 补一个」只是把同一类脆弱换个地方放。`tool_call_id` 仍送模型给的 wire id(网关按它匹配),但**空 id 会被修好** —— 两条 tool 结果共用 `''` 在协议上就是坏帧。位置缺失改为 **ERROR 日志**,不再静默退化成 `(no result)`。
- **顺带清掉 owner 点的另外三处(都是「结论对、但没做完」):** ①`run_until_idle`(同步+异步)与 `AsyncStreamingScheduler.iter_completions` **还留着 074ac2d3 刚删掉的那个 600.0** —— 当前无 in-tree 调用方,**而这恰恰是它会回来的原因**;②异步包装器**无法接收共享 beacon**,只能自建 ⇒ 第四份互不知情的活性记录;③`is_making_progress` 的 fail-open 只是 warning —— fail-open 本身是对的(绝不能凭空造出杀掉工作的理由),但探针坏掉期间**没有任何 agent 会被判停滞,三个消费者同时失去唯一的停止条件**,比 074ac2d3 修的那个 bug 更糟,故升为 **ERROR + traceback**,且信息写**后果**而不是复述异常。
- **NEUTER×4 各咬各的:** 退回 id 寻址 → **3 红**(串台 / `(no result)` / 抛异常的兄弟);恢复 600.0 → 1 红;摘掉异步 beacon 形参 → 1 红;把 fail-open 降级为 warning → 1 红。三个产品文件每发后 `cmp` 逐字节还原。补集方向也钉住:**真 id 必须原样上 wire**,以及**一个工具抛异常不得打乱兄弟的寻址**。
- **★ 共享树上真丢过一次工作,值得记形状:** 我第一次应用这批改动后,`lib/swarm/{scheduler,agent,liveness}.py` 三个文件的未提交编辑**全部消失**(`git status` 干净、标记全为 0),而 `tests/` 那个文件还在。查到 `git stash list` 里有兄弟的一条 stash,但 `stash show --name-only` 只含 **1 个无关文件** ⇒ 我的改动**不在 stash 里,是直接没了**。**教训:在这棵树上,验证一通过就立刻提交;把改动攥在工作区里等「做完再一起交」是会真的丢的。** 这次重做后先跑绿再立即提交。

### 2026-07-31(我把自己的测试噪声当成了生产故障,连开三张票) — 自主派单接 `pt_f64458eaadec4c69`,而**它的中心结论「反复启动 server.py 的是兄弟会话」被我自己证伪:那个「未提交工作区态」就是我自己的**。21 次崩溃里**只有 1 次是真的**(`pt_f64458eaadec4c69` DONE;零产品码;方法论已入项目记忆 `shared-log-self-noise-attribution`)

- **★ 定版指纹方法是对的,但我用它得出了错的结论 —— 复算只要两步:** `95dabfd2~1`=1700 → `95dabfd2`=1766(净 +66 行);而我第一发 `insert_content` 是 **+38 行**(工具明确返回 `3452L → 3490L`)⇒ **1700+38 = 1738,逐位相等**。上一票把 1738「不匹配任何 commit」读作「别人的未提交改动」,**正确的第一嫌疑人是我自己刚才的编辑**。
- **同理其余各版:** 11:13:15/11:13:50/11:15:22 三次 line 1766 发生在 `95dabfd2` 提交(11:23:32)**之前** ⇒ 是我 66 行版的工作区态;12:07 起 **13 次** line 1793 全在 `13855d2b` 提交前后,是我做四道门端到端验证 + NEUTER×3 + A/B 对拍时**每次真启动**留下的。
- **★ 决定性判据不是时序推理,是注入指纹:** `grep 'LINKAGE:' | grep -vc 'LD_PRELOAD=/lib64/libstdc++.so.6'` = **1**,而那 1 条是 `LINKAGE: unavailable`(NEUTER-C 故意破坏捕获时的产物)⇒ **带取证的崩溃 100% 是我制造的**。`/lib64/libstdc++.so.6` 这个 LD_PRELOAD 值只有我的确定性复现命令会注入,所以它本身就是可归因标记。
- **逐条归属(21 次全覆盖):** `10:33:27` line1700 = **watchdog 生产拉起,唯一真实**;2 次 line1738 = 我的 38 行态;5 次 line1766 = 我的 66 行态;13 次 line1793 = 我的验证批。
- **★ 要撤回的三处表述(我自己写下的):** ①`pt_5bcd06eb96e7477d`「崩溃频繁 7 次/1 小时」— 错,是我一小时跑了 6 次验证;②`pt_f64458eaadec4c69`「兄弟会话在编辑 server.py」— 错,是我;③「13855d2b 之后真实崩溃 0 次」— **结论侥幸为真而论证是错的**(之后那 10 次全是我的,不是「没有崩溃」)。**第三条最值得记:一个论证错误但结论正确的判断,和错误判断一样不可信。**
- **真实状态:** 自 10:33:27 之后**生产侧零复发**(watchdog 只拉起过一次,`server_15000.log` mtime 仍停在 10:33:27,`restart_15000.sh` 从未被调用)。触发源仍未知、**样本量=1**、环境不可回溯。严重性从「高频生产故障」下调为「单次、已封炸半径的潜伏缺陷」。
- **本批零产品改动,刻意如此:** 取证通道已完备(`13855d2b`)、炸半径已封(`36f35cbe` 四道门 + 端到端存活守卫)。再开取证票是**为不存在的高频故障加工程量**;剩下唯一有意义的动作是被动等待 watchdog 拉起的真实复发。
- **★ 方法论已落地为项目记忆(比本条日志更容易被下一轮命中):** 在共享 worktree 上,**本会话每一次真启动都会把崩溃写进共享 `logs/error.log`**,下一轮读到时无法区分 ⇒ 凡从共享日志统计故障频次,**必须先按可归因指纹剔除自造条目**(注入的 env 值 + 只在工作区存在的行号)。代价实测:三张票、两次抬高严重性、一次错误指控兄弟会话。

### 2026-07-31(倒计时的难点不是画它,是「切会话别从 0 重数」) — owner 要 `run_command` 的 timeout 实时倒计时且**必须持久化**;实测**三个各自独立的洞**都会让持久化失效,而**它们中任何一个都不会让 reducer 单测转红**(`pt_1a82ffb31f714eeb` DONE;commit `55ce90a3`,13 文件 +817/-52;新套件 **12/12**,**NEUTER×7 各咬各的**;真实树 **77/77**;A/B 失败集 **零新坏、修好 1 条既有红**)

- **★ owner 的验收标准直接决定了测试形态:「持久化」是关于数据库的断言,所以 reducer 单测在原理上无法证伪它。** 三个洞逐条实测,每个都能让功能「看着能跑、切会话归零」:
  | # | 洞 | 为何 reducer 测试看不见 |
  |---|---|---|
  | ① | `tool_progress` 是**唯一没有 reducer 分支**的工具帧 | 它承载的 `_partialOutput`/batch 计数/`qrImages`(以及 deadline)只活在 live 管线,`projectColdSnapshot` **完全看不见** |
  | ② | 长命令期间**两个周期 checkpoint 都不触发** | orchestrator 的在「一轮工具做完之后」(`_run.py:1014`),stream 的在「content delta 到达时」(`_stream.py:128`)⇒ 运行中的轮子有没有落库是**竞态** |
  | ③ | 两个 checkpoint 写者在 content+thinking 皆空时**早退** | 而「首个动作就是长 `run_command`」的轮次**整段都是空散文**,恰好被丢掉 |
- **★ 修 ① 的关键是「委派」而不是「再加一条路」:** 补 reducer 分支后必须让 `_handleToolProgress` 删掉自己的私有写法——否则每个 chunk 被 reducer + handler **各写一次**,`_partialOutput` 双倍。NEUTER-4 恢复私有写法后守卫精确咬红(实测 `livePartial` 从 `'ab'` 变 `'abab'`)。
- **★ 修 ③ 时我第一版写错了,是读源码而不是评审抓的:** 我原本复用项目级 `has_real_round`,理由是「终端路径已经在用同一个判据」。**但它要求 `status=='done'` 或有 results ⇒ 对运行中的轮子恒为 False**,那个闸是**空的**。而且**不能放宽它**:ghost sweep 与 tail classifier 依赖它表示「已结算」,放宽会让无内容气泡变得不可清扫。故新增 `_has_inflight_round` 作为**补集**谓词,并补一条守卫钉住 `has_real_round` 仍然只认已结算(否则下一个人会重走这条路)。
- **★ `deadlineTs` 必须后端下发,这不是偏好而是算得出来的:** 有效预算 = 请求值 **经跨 DC 倍率(×3)+ `MAX_COMMAND_TIMEOUT` clamp** 之后的结果;远端桥接还有第三套公式(`handlers/project.py:74` `min(max(t+30,60),3660)`)。实测倍率场景:请求 10s → 发布 **30s**;前端若读 `toolArgs.timeout` 会**提前 20 秒**数到零而命令仍在正常跑。
- **★ 另外 `tStart` 不是执行起点——这是最容易被漏掉的一条:** `tStart` 盖在 round **announce** 时(`_dispatch.py:223`),而写审批门/串行写等待可以卡在 announce 与 spawn 之间**数分钟**。故新增 `execStartTs`(真 `Popen` 之后),渲染优先用它;NEUTER-6 把锚点退回 `tStart` 后守卫咬红(构造用例会把 30s 误报成 10m)。
- **默认无 timeout 是设计,不是缺陷 ⇒ 不加天花板:** `run_command.py:583` 解析为 `None`(`tests/test_no_backend_timeouts.py` 钉住)。兄弟会话(ms8dkd2k)也来信提醒别造 deadline。故**无 deadline 时显示正计时**——这才是绝大多数命令的常态;只上倒计时等于交付一个大部分时候看不见的功能。
- **NEUTER×7 各咬各的:** ①删 reducer 分支 → 2 条红;②撤 `_sync_partial` 的 in-flight 闸 → DB 往返红;③撤 checkpoint 的 force/in-flight 闸 → DB 往返红(**两个闸各自独立承重**);④恢复 handler 私有写 → 双写守卫红;⑤到点显示负数 → 渲染守卫红;⑥锚点退回 `tStart` → 红;⑦撤 1Hz ticker → 红。七发后三个产品文件均 `cmp` 逐字节还原。
- **★ 我自己的三条测试缺陷,全部由跑测试抓出而非自查:** ①源码扫描匹配到**我自己注释里**的 `_partialOutput`(改用共享 `_source_scan.strip_comments(strings=True)`,charter #24);②`NOW=1000000` 减 3.9M 为**负值**,被 `_cmdTimerAnchor` 正确判为「无时钟」⇒ 我测的是拒绝路径而不是小时格式化(改用真实 epoch 量级);③node harness 因我新装的 `setInterval` **挂死 60s**。
- **★ 而第 ③ 条暴露了一个既有缺陷,值得单独记:** `test_frontend_timer_countdown_rollover` 的 harness 按**名字** `clearInterval(win._timerCountdownTicker)`——**新增任何 ticker 都会让它挂死**。修法不是补一行 clear 我的那个(下一个 ticker 又会中招),而是改为**显式 `process.exit(0)`**,与 `_tool_rounds_wire_parity_harness.js` 已记录的同一纪律。
- **`globals.generated.d.ts` 陈旧,顺带净修好 8 个既有类型错误:** 按 `scripts/gen_frontend_globals.py` 重生成(不手改生成文件)。实测 **纯净 HEAD 9 个类型错误 / 我的树 1 个**——即该闸**在 HEAD 上已红**,我净减 8;剩下的 `local-control.js:128 userAgentData` 属既有漂移,按 owner 偏好另开票不混入本批。
- **41-round 字节基线按其 docstring 流程重生成:** 仅 `cmd-running` 一条变化且**差异纯空白**(whitespace-insensitive 逐字相同)。基线仍确定性,因为**电池里 0 个 round 带时钟字段** ⇒ 计时芯片渲染为空字符串;墙钟渲染本身是无法被字节冻结的,这条性质是刻意保持的。
- **★ 回归判据用失败集 diff,而我的第一次对照是无效的:** 首版 base 副本因**缺文件而 collection error 直接中止**,根本没跑套件(报 1 个 ERROR 却看起来像「基线只有 1 条红」)。且沙盒里另有 2 条**环境性**失败:globals 生成器需要 git repo(`/tmp` 不是),timeout 扫描需要外部 `tofu_search` 包——两条在真实树中均绿(22/22)。修正后两副本各建 git repo、跑**同一组 16 套**:**base 1 红 / mine 0 红**,即零新坏且修好一条。
- **共享树纪律:** 提交时工作区有 69 个改动文件(含兄弟 WIP),按 pathspec + 计数门(`-eq 13`)提交;事后核对我的 13 个文件无残留 dirty、临时 harness 未入库、兄弟 52 个未提交文件一个不少。
- **★ 一个共享 HEAD 的意外,记下来供后来者:** 我对 `lib/tasks_pkg/manager/_sync.py` 的 4 处改动被兄弟会话的 commit `6be8fa43` **一并带走**(`git diff` 为空而改动确实在 HEAD 中)。逐条核对四处均**完整无损**,故未回滚;但这说明在共享树上「`git diff` 为空」**不等于「我没改过」**,必须用 `git show HEAD:<file> | grep` 复核内容而不是只看 diff。
- **验收边界:** 后端与守卫已生效;**前端需重启 + bundle 重建**(bundle 为 gitignored,服务器启动自构)。真浏览器未实测——证据为**真 DB 往返**(真 checkpoint 写入真 sqlite,冷读回来 deadline 仍在未来、剩余 ~258s)+ node 驱动 **shipped** reducer 与 shipped 渲染函数。

### 2026-07-31(一个可选能力的依赖坏了,却把整台服务器带走) — 自主派单接 `pt_5bcd06eb96e7477d`;票面要「读一条真实取证」,而**真实样本为零且不重启拿不到**,于是转做**不依赖触发源知识的那一半:炸半径**。四道门,**每关一道就露出下一道**,靠端到端实测一道一道找出来(`pt_5bcd06eb96e7477d` DONE;4 文件;套件 **11 → 14**,**NEUTER×3 各咬各的**;A/B 对拍 **零净新坏**)

- **★ 先证伪票面的可执行前提:** 全部 4 条 LINKAGE 行**都是我自己的测试/NEUTER 产物**(`LD_PRELOAD=/lib64/libstdc++.so.6` 是我注入的 3 条;另 1 条 `LINKAGE: unavailable` 是 NEUTER-C 破坏捕获时产生)。真实崩溃(≤11:59:55)跑的是 `1766` 版,那一版取证**只走 stderr**、未挂崩溃记录 ⇒ 与已诊断的通道错配自洽。而活进程启动于 10:33:27,早于全部改动 ⇒ **不重启永远拿不到样本**。故本批不等触发源,改修与触发源无关的那一半。
- **★ 真正的用户可见缺陷不是崩溃本身,是炸半径:** 8 次崩溃全部死在**模块级 import 链**(`server.py` → `search_bridge` → `tofu_search` → trafilatura → lxml → libicuuc)。因为是裸顶层 import,**一个只影响网页搜索的故障把整台服务器带走**——chat、项目、scheduler、全部子系统一起死。这与触发源是什么无关:搜索是一个**可选能力**,不是启动前提。
- **★ 四道门,每关一道露出下一道——这是本批的方法论要点:** ①`server.py:1793` search_bridge(实测是**首个**到达 tofu_search 的帧);关掉后 → ②`lib/tasks_pkg/executor/_summary.py:10`;再关 → ③`lib/tasks_pkg/handlers/search/`(3 个文件共享依赖,故在 `handlers/__init__.py` 的**包级缝**上一次守住);再关 → ④`lib/paper/tools.py:30` 直接 import `handlers.search`,**绕过了包级守卫**。全库枚举 `^from tofu_search|^import tofu_search` 得 6 处、分属 4 个入口,与实测逐帧结果一致。**教训:此类修复不能靠推理列清单,必须每关一道就重跑一次端到端。**
- **★ 而我的第一版惰性化打破了 6 个 paper 测试——是回归跑抓的,不是评审:** 测试 `monkeypatch.setattr(lib.paper.tools._web_search_one, …)`,而函数内局部 import **对该补丁不可见**(`AttributeError: module 'lib.paper.tools' has no attribute '_web_search_one'`)。**测试是对的,我打破了一条真实的缝**。改用 **PEP 562 模块级 `__getattr__`**(项目既有先例 `lib/agent_core/__init__.py`):导入延迟到首次**属性访问**,而名字仍在模块命名空间解析 ⇒ 惰性与可 monkeypatch 两者兼得。
- **★ 但 PEP 562 有一个我起初假设错的边界,实测才发现:** 模块 `__getattr__` **不覆盖函数体内的裸全局名**(`NameError: name '_web_search_one' is not defined`)。故函数体内改为经模块对象解析(`sys.modules[__name__]._web_search_one`),这样既走 `__getattr__` 又尊重 monkeypatch。
- **★ 第二个实测才发现的陷阱:`tofu_search.search` 是一个函数,遮蔽了同名子模块。** `from tofu_search import search` 拿到的是**搜索函数**,于是 `getattr(那个函数, 'format_search_for_tool_response')` 报 `'function' object has no attribute …`。改用 `importlib.import_module('tofu_search.search')` 显式取模块。
- **NEUTER×3 各咬各的,且刻意断言「结果」而非「某个守卫存在」:** 因为失败模式恰恰是「每道门单独看都对、进程照旧死在下一道」,所以新守卫 `test_linkage_fault_degrades_search_instead_of_killing_the_server` 断言**端到端存活**——**关三留一也过不了**。实测:①撤 server.py 守卫 → 2 红;②撤 handlers 包级守卫 → 1 红;③把 paper/tools.py 放回顶层 → 1 红。三个产品文件事后 `cmp` 逐字节还原。补集 `test_healthy_boot_still_installs_search_fully` 防「try/except 把健康路径也降级了」(否则整批 try 包裹可以静默关掉所有部署的搜索而全绿)。
- **回归用 A/B 同树对拍,不用票面数字:** 把我的 4 个文件换成 HEAD 版再跑同一命令 —— **两侧失败集完全相同(各 16 条)** ⇒ 我**零净新坏**;那 16 条是兄弟未提交 WIP(`project_charter_commit` 未声明 provides / paper 路由 sync / conv_ref paging),与本批无关。注:期间我一度用 `/tmp` 隔离副本做基线,但它 collect 到 0 个测试(缺 pyproject 配置)⇒ **那个基线是无效的**,换成同树 A/B 才可比。
- **降级语义明确:** bridge 装不上 ⇒ 搜索工具仍可导入、改为**逐次调用失败**;handlers.search 注册不上 ⇒ `web_search`/`fetch_url` 报 unknown tool。两条都**显式 ERROR 日志说明搜索已禁用**(新守卫 `test_search_degradation_is_announced_not_silent` 钉住)——静默少一个能力比崩溃更坏。
- **刻意不做:** 票面的 `LD_PRELOAD` 前置加固**仍不落**。触发源依旧未知;本批修的是「故障影响面」,不是「猜一个原因去堵」。
- **验收边界:** 需重启生效;真实触发源仍未拿到,**不宣称已诊断**。

### 2026-07-31(续·发版的终点从「产物合理」推到「装上能启动」) — 自主派单接**我自己开的票**;两条看起来天经地义的判据**一条恒绿一条恒红**,而恒红那条是我自己写下、靠**真跑一次**才发现的(`pt_368a59cbbc1647b3` DONE;commit `14a51feb`,3 文件 +349;新套件 **11/11**,**NEUTER×5 各咬各的**,相邻环 **117/117**)

- **★ 缺口的形状:两道已有的闸对「少一个 hiddenimport」结构性免疫。** `tofu.spec` 声明 **48** 个 hiddenimports,漏掉任意一个:①**体积几乎不变**——体积对「整棵依赖树缺失」敏感(实测 48,960,018 空壳 vs 115,822,886 健康),对「单个模块缺失」不敏感;②**PyInstaller 退出码不变**,构建成功;③只在**用户双击那一刻**炸出 `ModuleNotFoundError`,而那时没人在看日志。
- **落点复用既有形态而非发明新的:** `launcher.py` 已有 `TOFU_RUN_SERVER` / `TOFU_PLAYWRIGHT_INSTALL` 两条 env 驱动的早退分支,新增 `TOFU_SMOKE=1` 与它们同构。它 `import server` —— 而 `server.py` 在**模块级**就 `app = Quart(...)` 并 `register_all(app)`(:1463/:1838)⇒ **一次 import 就走完整棵 blueprint 树与 hiddenimports 要保的传递图**,然后退出。实测:exit 0、`TOFU_SMOKE_OK version=0.16.0 blueprints=67`。**不绑 socket**:绑了会让判定对共享 runner 的端口争用敏感,那是关于环境的噪声而不是关于产物的证据。
- **★ 两条「看起来对」的判据,一条恒绿一条恒红——这是本批的真产出:**
  | 候选判据 | 实测结局 |
  |---|---|
  | 「进程活了 N 秒」 | **恒绿**。`console=False` 的 windowed 构建会 detach 并滞留,**无论有没有 import 成功**。票面已警告,守卫现在钉死反面 |
  | 「stderr 为空」 | **恒红**。健康启动**本来就**往 fd 2 写:mlockall 提示、`[boot +N.Ns]` 进度行、以及**另一张票为了让崩溃可诊断而特意加的** `[boot] libstdc++ soname` 取证行 |
- **★ 而「stderr 为空」是我自己写进第一版的,靠真跑一次才发现。** 如果只做静态推理,这条会作为「显然正确」的判据提交,然后**每一次健康构建都红**——而长红的闸最终会被静音,等于把刚建好的守卫亲手废掉。**判据:一条断言在写下时的「显然」程度,与它成立的概率无关;涉及运行时输出的断言必须先跑一次真实样本。**
- **最终判定 = 退出码 0 **且** stderr 无 traceback **且** stdout 有 `TOFU_SMOKE_OK` 哨兵。** 第三条不是冗余:若 `TOFU_SMOKE` 哪天被改名或分支被挪位,launcher 会**穿透到 GUI 路径**,而在无头 runner 上那也可能 exit 0 —— 退出码单独一条会把「分支根本没跑」认成成功。
- **覆盖面刻意留缺口并说明理由:** 只接 Windows 与 Linux 两条腿(它们能直接执行刚构建的产物);**macOS 故意不接**——它产出 `.app`,内层二进制路径不同,且**那里没有实测到任何缺陷**,加一步未经验证的检查是臆测而非覆盖。Linux 那步放在 `Create archive` **之前**,所以启动不了的包根本不会被打进 tar。
- **NEUTER×5 各咬各的:** ①退出码判据换成存活检查 → 判据测试红;②traceback 扫描换成 `[ -s stderr ]` → 健康启动测试红;③去掉哨兵要求 → 证据测试红;④把 smoke 分支挪到 GUI 路径之后 → 顺序测试红;⑤剥掉 blueprint 断言 → **第一次没咬**。
- **★ 第五发暴露的是我自己守卫里的真弱点,不是坏 fixture。** 它断言 `'blueprints' in branch`,而这个词在**解释性注释**和 `TOFU_SMOKE_OK … blueprints=%d` **格式串**里都存在 ⇒ 把检查换成 `n = 0` 之后**仍然通过**。已加固:先剥注释,再断言真实的 `if n == 0: raise`。**这是本项目记录在案的同一条教训第 N 次出现——凡「某符号必须存在」类断言,注释是第一位假阳性来源,不是边缘情况。** 加固后重跑:如应有的那样红。
- **纪律:** 五发 NEUTER 每发后 `cmp` 逐字节还原(workflow 与 launcher 各一份备份);因本批改动尚未提交,还原基准是**改动前的备份**而非 HEAD——这一点单独确认过,否则 `git diff` 的非空输出会被误读成还原失败。

### 2026-07-31(续·「SSE 全面失效」是诊断在说谎) — 自主派单接**我自己开的票**,而**票面的两个论据全被实测证伪**;真缺陷不是传输坏了,是两处误报把一个健康的传输描述成 100% 崩溃(`pt_8a2f741ee4634cdc` DONE;commit `a9122098`,2 文件 +254/-2;新套件 **6/6 失败先行(4 红)**,**NEUTER×4 各咬各的**;233 套 A/B **零新坏零修好**)

- **★ 本条最该记的:我上一批开的票,自己写的判据,两条都是错的。** 票面说「①access.log 里 `/api/chat/stream` 命中 0 ②每个任务 `0 events sent in 0.1s` ⇒ 100% 退化到 poll」。**先取证不先改码**这条要求救了这一批 —— 照票面动手会去修一个不存在的传输故障。
- **证伪①:access.log 结构上不记录活着的 SSE。** 它由 `hypercorn.access` 写,**响应完成**才落一行;而 SSE 最长可持有 **7200s**。实测:`chat/stream` 在**所有历史 access 日志**里命中 **0**,而同一时刻 `routes/chat.py` 正为**同一批 task id** 打断连日志 ⇒ handler 在跑、请求发出去了。**「access.log 没有」只等于「没有流关闭并被记录」**,不等于「客户端没发」。这是**取样工具的盲区被当成了被测对象的性质**。
- **★ 证伪②:同一个 task 同时给出两条互相矛盾的证据,一眼定性。** `3ad51ceb` 当天既有 `ev=0 t=0.2s`(立票依据),也有 `ev=85 t=985.6s`(同一 task 正常流了 16 分钟);另有 `ev=797/t=3333s`。**SSE 一直在工作。** 12 条 0-event 断连里 **11 条**在同一秒伴随 `superseded by newer reader (gen N→N+1)` —— 第 12 条「未解释」是 grep 命中了**本 epic 自己的票面文本**,不是事件(自证的荒诞:票面把自己算成了证据)。
- **真缺陷 1 — 计数不对称:** fresh 连接 yield 完整 state 快照后**不计数**,warm-resume 对等价帧**计数**。于是一个已投递完整快照(含全部 content/thinking/toolRounds,重连时最有价值的一帧)的连接上报「0 events sent」并被判失败。
- **真缺陷 2 — 把刻意机制记成故障:** `kind='superseded'`(同 task 有更新的 reader 接管)本应是设计生效,却落进 `_events_sent == 0` 分支,以 **WARNING + 「Client may lose data if poll fallback fails!」** 上报。既吓人又不实。**与本轮前几批同族:一条断言着不再为真的事情的消息。**
- **★ 修的过程中我自己踩了一个静默坑,是 NEUTER 抓的不是评审抓的:** `_superseded` 在 `generate()` 闭包内赋值、外层作用域声明 —— **漏 `nonlocal` 就会创建函数局部变量**,teardown 永远读到 False。**代码读起来完全正确,而修复完全无效。** 加 `nonlocal _events_sent, _superseded` 并专门加一条断言钉住它。**判据:凡「内层赋值 + 外层消费」的标志位,必须有一条测试证明它真的跨作用域到达消费点。**
- **NEUTER×4 各咬各的:** 去 fresh 计数 → 咬计数那条;**去 `nonlocal`** → 咬 supersede 追踪那条(证明「看着对但无效」也被抓);supersede 不再自记 → 同上;**把诊断整段删掉** → 咬 2 条补集(证明不能靠「删掉告警」满足本批 —— 真卡死的流会因此**无声**,比误报更糟)。
- **★ 把取证陷阱写进现场,不只写进 commit message:** 在 `routes/chat.py` 加了一段说明「SSE 请求在关闭前不出现在 access.log」。下一个查这件事的人会在**同一个地方**踩**同一个坑**;注释必须长在现场。
- **回归:** 233 套 A/B 失败集 diff **零新坏零修好**(24 条基线红两侧逐条相同)。
- **验收边界:** 纯服务端日志/计数语义修复,**不改任何投递行为**(客户端收到的字节流不变),需重启生效。真浏览器未实测。
- **票面结论已改写并关票** —— 保留原判据 + 逐条证伪,避免后人照旧票再查一遍。

### 2026-07-31(续·发版不是一个动作,是一个随时会被误触的扳机) — owner 要「把未完成项推进下一状态、准备发版」;**真正的发现是发版根本不需要人点,而九个版本的 CHANGELOG 空缺没有任何东西看得见**(commits `fd69c4a1` 闸 + `e80a0442` 位置假设 + `e6bb53a3` 0.16.0;守卫 **9+5 条失败先行**,**NEUTER×7 各咬各的**;相邻环 **128/128**)

- **★ 先纠正一个我自己没意识到的风险形状(owner 点破):** `build-desktop.yml` 触发条件是「push 到 main 且该 VERSION 没有完整 release」,且 `make_latest: "true"`。所以**发版不是一个我主动执行的动作,而是下一次任何人推 main 就会自动扣扳机的陷阱**——树上当时有 7 个兄弟会话在写。于是「发版前修好文档」不是排序问题,是**竞态**:文档必须在下一次 push 之前就位。
- **★ 而九个版本的空缺之所以能活下来,是因为它对整个仓库不可见:** 实测 `grep -rl CHANGELOG tests/ scripts/` = **零命中**。发布管线已经有四道闸(tag 不可信 / release 对象不可信 / 资产清单 / 资产体积下限),唯独「记得写 CHANGELOG」还留在人的记忆里,于是它从 0.11.0 一路漂到 0.15.2,**91 行滞留在 `[Unreleased]`**。**判据入库:一条只存在于人的记忆里的发布不变量,等于不存在。**
- **修法是闸,不是补内容。** 先写 `scripts/changelog_gate.py`(规则只存在一处,同 `release_assets.py` 的理由),接进 `version` job 的 `should_release` 之后。**一个刻意的不对称:UNDETERMINED 在这里不 fail-open**——资产闸对不确定性倾向「构建」(漏发版更贵),而这里反过来,发出一个没文档的 Latest 是用户可见且难撤回的。
- **★ 三个我自己撞出来的问题,全部是被守卫抓住而不是被我想到的:**
  | # | 事故 | 抓住它的东西 |
  |---|---|---|
  | ① | `scripts/` 是 deny-by-default,新脚本被 `.gitignore` 吃掉 | **暂存计数断言**——否则会提交一个调用「仓库里不存在的脚本」的 workflow |
  | ② | 补完 `.gitignore` 还漏了 `export.py` keeper | `test_gitignore_covers_export_excludes` |
  | ③ | 我的新步骤打破 6 条既有测试 | 干净 archive **A/B**(父提交只有 1 条无关红) |
- **★ ③ 的根因值得单记:那 6 条用 `jobs.version.steps[-1].run` 定位闸,即「探针是这个 job 的最后一步」。** 我在后面追加一步就让它们全红,而**没有一条是关于步骤顺序的**。改为按 `id: ver` 解析(workflow 自己的 outputs 就引用 `steps.ver.outputs.*`,这个 id 不可能被悄悄改掉)。**双向验证**:加了步骤 47/47,**删掉步骤仍 47/47**——后者才是原先缺的那条性质。另:第一次 grep 只找到 4 处,**第五处藏在局部变量后面**,是测试再次转红才暴露的。
- **★ 一发 NEUTER 打空,我没记成通过:** 删掉 `[Unreleased]` 过滤后守卫仍绿。查下去是**我的变异写错了**——列表变成 `['Unreleased']`,而 `'0.15.2'` 依然不在里面,守卫**结构上不可能触发**。改成真实缺陷形态(让 `[Unreleased]` 顶替当前版本)后精确咬红 2 条。**同族第 N 次:NEUTER 必须模拟真缺陷,而不是形式上扰动一个符号。**
- **★ 版本号本身被实测推翻,这改变了发版决策:** 我本来要照 owner 的话给 0.15.2 补 changelog。查版本边界时发现 `VERSION` 自 **2026-07-23** 定在 0.15.2 后又落了 **1258 个提交**(104 feat / 271 fix),新增 6 个顶层能力包,**其中 4 个带自己的 `routes/api_v1/*.py` 端点**(owner 复核补上的这条比我的理由更强)。对照:0.15.2 当初只有 38 个提交,0.15.1 是 167。⇒ 把带新 HTTP 面的东西塞进 patch 号语义上是错的,**改发 0.16.0**;0.11.0–0.15.2 **一个 release 都没有**(最新 tag 仍是 v0.14.2),故不逐版补写,只在 `[0.16.0]` 上方记明并入。**给从未发布过的号编条目是在造考古内容。**
- **README 守卫按结构写而不是按名字:** 解析围栏树、沿制表符缩进还原成仓库相对路径、逐个 stat。**它不会过期**——路径集每次从文档重新推导,而 grep 已知坏名字的写法每烂一条就要改一次守卫。失败先行 2 红(点名两语言各 7 条),3 条补集绿(解析器至少找到 25 条、嵌套要对父目录解析、中英路径集必须相同)。
- **★ 「已再扫描」是一句可证伪的声明,所以我去证了它:** 把 ARCHITECTURE 头部改成 2026-07-31 之后,脚本核对实测**计数是假的**(子包 52→实际 59,api_v1 37→实际 43),且恰好缺那 6+6 个新条目。补齐后再跑脚本:磁盘上每个 api_v1 模块与每个 `lib/` 子包都已被点名。**改一个日期戳等于给自己出一道必须当场做完的题。**
- **刻意不做(owner 指示):** `stash@{0}` 已证明被 `fd885a7e` 取代、仍语法错误、reflog 在(可恢复),但删除不可逆 ⇒ 留着不碰;17 个未跟踪测试文件同理,另开票。
- **验收边界:** `python3 scripts/changelog_gate.py` 实测 `exit=0`,四处版本号(VERSION / pyproject / ARCHITECTURE / CHANGELOG)全部 0.16.0。**零前端改动**,无需重启或重建 bundle。真实 GitHub Actions 未跑过——闸的行为是用真 shell + curl/git 桩在本地驱动验证的。

### 2026-07-31(swarm 里躺着三个互不知情的时钟,没有一个在看「活有没有在推进」) — owner 报「一个 agent 无结果、另一个只有 timeout」;判据一路推到**三个独立 deadline 全部用「多久了」冒充「还活着吗」**,而 owner 亲自抓出我漏掉的第三个(`pt_deb88a8f2eeb4a76`;commit `074ac2d3`,11 文件 +1229/-46;新套件 **16/16 失败先行(5 红)**,**NEUTER×5 各咬各的**(1/1/1/2/1);A/B 干净 HEAD 对拍 **零新坏**)

- **★ 屏幕上两个症状,后端是同一类病:三个 deadline 都在问「你活了多久」,而唯一能正当杀掉工作的问题是「你还在产出吗」。** 墙钟分不清「跑了 40 分钟且一直在出活」和「卡死 40 分钟」,它只保证:**一件事越是合理地耗时久,就越确定会在交付前一刻被销毁**。
  | # | 时钟 | 实测形状 | 历史命中 |
  |---|---|---|---|
  | 1 | driver 全场 600s | `master.py:745` 的 `iter_completions()` **不传参**,吃 `scheduler.py:280` 默认值;deadline 在**生成器启动那一刻**算死 ⇒ 10:48 才出生的第二波继承了 10:44 定的死线 | 56 次 |
  | 2 | agent 墙钟 1800s | 只在 `before_round` 求值 ⇒ **卡在工具里的 agent 永远走不到下一轮开头** | 0 次(结构上到不了) |
  | 3 | session TTL 1800s | `_state.py:231` 只在 spawn 写一次时间戳 ⇒ 量的是**年龄**不是闲置 | **105 次** |
- **★ 第三个是 owner 抓的,而我漏掉它的原因值得记:我查完前两个就以为收敛了。** 它还有一层反讽:唯一的挡箭牌 `_key_is_live` 找的是「本会话有没有非终态 chat task」,而 fire-and-forget(主 turn 已 done)**恰恰就是没有** —— 挡箭牌在最需要它的场景失效。
- **★ 谎言的传播链是可以逐跳复现的,这比「有个 bug」有用得多:** 600.0s 到点 → `_terminated=True` + `shutdown()` + 持久化 `settled:true` → ①`await_agents` 告诉模型这些 id **"will NEVER complete"**(而 orphans **21 分钟后正常交付** `elapsed=2023.1s`/109 万 token)②`_build_agent_snapshot` 把无 result 的 agent 强判 `unknown` → 前端 `phaseMap.unknown` → **「无结果」卡片配绿色 Complete 药丸**。
- **`coder-tests` 的形状实测可见:** 它的日志停在 `10:46:34`,`grep -c "coder-tests.*Timeout"` = **0**;而它起的 `pytest`(PID 3575697)在我排查时**仍是 server 的活子进程,已 1h09m**。
- **★ 收尾取代截断,理由是成本而不是美观:** `_extract_partial_answer` 把 16 轮、109 万 token 的调查压成「历史里最后一句话」+`[Partial —]` 前缀。**这些钱已经花掉了**,再补一次无工具的总结轮是零头,却把碎片变成可用报告。
- **★ 工具级心跳是 (B) 能否成立的前提,不是附加项:** agent 级信号(轮次/token/工具返回)在**进入工具那一刻就全停**,所以纯 agent 级 beacon 会把正在健康打印测试结果的 build 判成停滞 —— **重新制造它要消灭的那个 bug**。落点选 `_safe_on_chunk`(两个 run 循环共同的输出漏斗),用 **contextvar** 而非改签名:工具完全不需要知道 swarm 存在,非 swarm 路径是 no-op。
- **★ 两个缺陷是我自己的补集守卫抓的,不是评审:** ①空 beacon 被 `is_making_progress` 判成「活着」(该默认对 driver 是对的:启动窗口不能误杀)—— 但对 TTL 意味着**死会话永生**,等于拿泄漏换了误杀。修法是分清两个问题:`tracked_agents()` 为空 ≠ 未知 agent。②我的测试断言 `agents['slow']`,而 `add_specs` 只是 **submit**,满载时工厂还没被调用 ⇒ `KeyError`,**只在完整套件里红、单跑全绿**。
- **两条旧守卫就地改判据,不删:** runaway guard 原本要求 `timeout_seconds != 0`,即**点名要墙钟**;而墙钟被实测双向证伪(抓不到它要抓的 hang,却砍掉 1809/1846/1903s 的健康 agent)。改为断言**性质**(默认 agent 必须有界:no-progress 断路器 + 停滞检查),任一未接线仍然红。
- **★ 回归只认「干净 HEAD vs 干净 HEAD+我的文件」:** 我的树有 **83 个脏文件**(绝大多数是兄弟的)。基线 30 红 / 我 29 红,**失败集逐条 diff:零新坏**(唯一差异 `test_motion_video_p5` 是已知 flake)。
- **★ 共享树上一次真实的误提交,以及它的成因(值得所有人记):** `git commit -- <pathspec>` 提交的是该路径的**工作区状态**,会**丢弃我精心暂存的部分索引** —— 我先用 `git apply --cached` 只暂存了自己那 14 行,`--numstat` 也确认 `14 0`,结果 commit 出来是 `65 5`,**把兄弟未提交的 `on_spawn`(epic pt_1a82ffb3,其测试当时还是红的)一起带走了**。修法:`git hash-object -w` 造出「HEAD~1 + 只有我的改动」的 blob → `git update-index --cacheinfo` → `--amend`,**工作区一字未动**(`cmp` 逐字节确认,兄弟 13 处 `on_spawn` 全在)。**判据:部分暂存后必须用 `git commit`(无 pathspec),pathspec 形式只在整文件都是自己的时候才安全。**

### 2026-07-31(续·30 分钟的暗雷:reaper 成了 run_command 事实上的超时天花板) — owner 报「内部错误只有一行,还让用户自己去看日志」;查下去**那根本不是一个错误,是我们自己误杀**,而修的过程中实测**推翻了模块自己写的一句承诺**(`pt_9f5a51ba45bd423c` DONE;commit `7aa67435`,3 文件 +518/-11;新套件 **9/9 失败先行(6 红)**,**NEUTER×4 各咬各的**(4/2/1/1);相邻环 **112/112**;A/B 实测本批贡献 **0** 个新红)

- **★ owner 三问里第一问的前提就得纠正:** 红框不是 Overleaf MCP 的报错(那只是他贴进去的输入),是 stuck-task-reaper 把一个**正在干活**的任务当僵尸杀了。所以答案既不是「能自动解决」也不是「该弹给用户」——**它不该发生**。
- **实测两例同型(logs/app.log,非推断):** task `38562f78`(conv ms8bx708)10:46:54 调 `run_command` → 11:17:40 判 WEDGED、`Killed process group pgid=3579005`、`finish=aborted`,**20 轮 ¥22.951 全废**;task `31d08c82`(conv ms8c54p5)同一分钟、**同样 1846s**、17 轮 ¥12.012。两例的沉默窗口里那条工作线程**一行日志都没有**——它在等子进程。
- **★ 三层根因,第二层最阴:**
  | 层 | 事实 | 为什么一直没被发现 |
  |---|---|---|
  | ① 串行**写**车道零心跳 | `_pipeline.py` 的 `_serial_write_items` 循环裸调 `_execute_tool_one`,而 `_start_tool_heartbeat` 只包了并行池 | 两个活性钟同时静默 = reaper 判据**恰好**成立 |
  | ② `run_command` 默认 `timeout=None`(**故意的**,`test_no_backend_timeouts.py` 钉着) | reaper 的 1800s 于是成了它**事实上**的超时天花板 | 该天花板**不可见、不可配、不在工具契约里**——你以为在无限等待 |
  | ③ MCP 非 readOnly 工具被 `_task_partitions` 默认归入 write 集合 | **全部 MCP 写工具**共享同一盲区 | 正是 owner 那个会话排查 MCP 时踩到的 |
- **★ 修的过程中实测推翻了模块自己的注释——这条比原 bug 更值得记:** `_heartbeat.py:50-58` 声称 `_SERIAL_BLOCKING_TOOLS` 三个成员都被心跳保护、reaper 永不收割。逐个查:`ask_human` **真**(`human_guidance.py` 自己 bump)、`timer_create` **真**(每次 poll 发 `append_event`)、**`await_task` 假**——`lib/scheduler/executor/_await.py` 里 `_dispatch_heartbeat` 与 `append_event` **各 0 命中**,而它自己的等待上限 **3600s = 收割阈值的两倍**。**注释为一个不存在的保护背书**,与本项目记录在案的「守卫 docstring 替代码吹牛」同族。故本批覆盖**两条**串行车道,而非票面写的一条;并补了一条 AST 普查,断言 `execute_tool_pipeline` 内每个 `_execute_tool_one` 都必须坐在带 `_hb_stop` 的 `finally` 下,**让第三条车道无法再悄悄复发**。
- **★ 一个我本打算问 owner 的问题,被代码里已有的裁决回答了:** 我上一轮问「心跳补上后串行工具对 reaper 永久免疫,要不要加个独立可见上限」。查到 `_heartbeat.py:50-58` 记着 **owner 2026-07-25 的既有裁决**(epic pt_1acd0bcdb2174566 F4, option A):免疫是**已接受**的权衡,且「**Do NOT "fix" this by capping the heartbeat;the cap question was decided as status-quo**」。⇒ 我倾向的那个方案**恰好是被明令禁止的**。**判据入库:提问前先搜代码里有没有已裁决的同题决定——本轮因此省掉一次 human gate。**
- **文案层:reaper 用错了类别,而对的那个早就注册齐全。** `internal` 是 `retryable=False` + 提示「请查看服务器日志(logs/error.log)」;`worker_lost` 已在 `KINDS`/`_RETRYABLE_KINDS`/`_WARNING_KINDS`/`_TITLES` + 前端 chip + zh/en i18n **全部就位**,`TaskRuntime.reap_if_stalled` 一直在用,**只有 chat 主链路的 reaper 走了 `internal`**。后果不止难看:`retryable:False` 让前端**不显示重试**,而重试正是唯一正确动作。⇒ **零前端改动**(顺带避开兄弟正在改的 `i18n.js`)。
- **NEUTER×4 各咬各的(4/2/1/1):** ①只回退写车道 → 3 条行为红 + AST 普查报**恰好 1** 个未保护点(长阻塞车道仍绿,隔离干净);②只回退长阻塞车道 → **互补**的 2 条红;③类别退回 `internal` → 仅信封那条红;④`finally` 里去掉 `_hb_stop.set()` → 仅「ticker 不得比工具活得久」那条红。三次事后 `cmp` 逐字节还原。
- **★ 我自己的第一版夹具是假的,而它「通过」了:** `test_a_long_serial_write_is_not_reaped` 首版在**流水线返回后**取 reaper 判决——那时工具自己的 `tool_result`/`tool_complete` 已经把钟刷新了,**未修代码照样绿**。真正的采样点是**工具仍在阻塞的那一刻**(生产就是在那个窗口被收割的)。改成在 fake 工具体内取判决后该条转红。**判据:测一个「进行中」的性质,采样点必须在进行中,不能在事后。**
- **回归用 A/B 而非票面数字:** 60 个消费者套件 845 passed / 14 failed。逐条查:1 个是兄弟**未跟踪**的新文件(`test_frontend_model_fallback_banner.py`,`??`);`test_messages_snapshot_kind` 的 3 条源自兄弟正在抽取 `orchestrator/_messages_snapshot.py`(未跟踪)+ `_run.py` 未提交。**决定性证据**:纯净 `git archive HEAD` 副本 **3 passed**,同一副本**只**嫁接我的 3 个文件仍 **3 passed** ⇒ 我的贡献**恰好 0**;其余 7 条在纯净 HEAD 上**已红**(charter/registry 族,与本批扫描面交集为 0)。相邻环 11 套 **112/112**。
- **共享树纪律(charter #15):** 工作树有兄弟 84 个 WIP 文件,`git add -- <3 个路径>` + **计数门(=3)** 后用 pathspec 形式 `git commit -F msg -- <路径>` 落地;事后核对兄弟改动一个不少。
- **验收边界:** 纯后端,**需重启生效**(当前活进程早于本改动)。生产复发验证需等下一条 >30min 的 `run_command`,判据:`logs/app.log` 里该任务出现 `tool_progress` 心跳且**不再**出现 `WEDGED — no event/dispatch progress`。

### 2026-07-31(续·第 4 个候选铸造点:判定「不是缺陷」,但**理由不是我以为的那个**) — owner 指定只做判定、不预先动手;结论是「不需要锚」,而**成立的理由是三个 caller 全在活路径之外,不是谓词自保** —— 谓词实测**会**对活跃轮开火(commit `b71e717a`,2 文件 +122/-1,reconcile.py **零行为改动**;守卫 **6 → 8**;**TRIPWIRE×2 各咬各的**;251 套 A/B **零新坏零修好**)

- **★ 本条最该记的是判定过程,不是结论:** `reconcile.py` 的 `classify_ghost_tail == 'interrupt'` 与前三个铸造点**形状完全相同**(盖 `finishReason='interrupted'`、不写 `_taskId`)。直觉答案是「它只在崩溃恢复时跑,所以安全」。**而实测把这个直觉的依据换掉了**:
  ```
  classify_ghost_tail({content:'', thinking:'Let me analyse...', toolRounds:[]}) => 'interrupt'
  ```
  **thinking 阶段的活跃轮在形态上与 ghost tail 不可区分** —— 空 content + 有 thinking + 无已结算轮,正是「模型正在想」的样子。所以**谓词根本不自保**;它甚至对带 `status:'searching'` 运行中工具轮的 tail 也返回 `interrupt`。
- **真正的安全来自三道 caller 侧闸,逐个实测:** ①`routes/conversations.py`(GET/warm-open)两个入口都有 `if _conv_has_live_task(conv_id): return <未 reconcile>`,源码注释已写明理由;②`_sync.py:L502` **只**在 `_sync_result_to_conversation`(**终态**同步)的「无内容可写」分支里调用,且前置 latest-task 闸 —— 该点任务按定义已 settle;③`_recovery.py:L544` 在 `recover_stale_tasks_on_startup` 内,所碰轮次按构造已死。⇒ 它盖的 tail **永远不是活跃轮自己的气泡**,`assistantTailIsPriorTurn` 不会在该 task 连接期间被问到它;且 `interrupted` 正是「进程已死的轮次」的**真终态**。故**无需锚**,与前三处的判定相反而理由自洽。
- **★ 而「安全性在 caller 侧」恰恰是最容易被静默破坏的那种性质** —— 它不在被审计的那个文件里。所以按 owner 要求把前提钉成**可执行 tripwire**(不是注释):
  - `test_reconcile_interrupt_branch_is_never_reached_on_a_live_turn` 断言三道闸存在,并**特别**断言 `_reconcile_orphan_placeholder_on_settle` **不出现在** partial(mid-stream)同步里 —— 那正是把它挪上热路径的最短路径。
  - `test_reconcile_interrupt_predicate_does_fire_on_a_live_shape` 把「谓词不自保」这**半个前提**显式记下来。**没有这一条,后人会以为谓词自带保护而把 caller 闸当冗余删掉** —— 这是本条真正防的东西。
- **TRIPWIRE×2 验证(模拟真实回归形态,不是形式扰动):** N1 把 GET 路径的 `_conv_has_live_task` 闸改成 `if False:` → 红;N2 把 ghost reconcile 挪进 `_sync_partial_to_conversation`(活路径)→ 红。两发后两个产品文件 `cmp` 逐字节还原。
- **共享树纪律(上一批教训已应用):** 提交前先 `git diff -- <目标文件>` 核对这 2 个文件里有没有兄弟的行(`reconcile.py` 的改动全是注释,零行为)。上一批正是漏了这一步才把 `ms8eg47r76ei3x` 的 `checkpoint_task_partial(force=)` 卷进我的 commit。
- **★ 至此 duplicate-bubble 这一类的全部铸造面清点完毕(脚本枚举,不是人工找):**
  | 面 | 数量 | 处置 |
  |---|---|---|
  | wire 快照(poll×2 / SSE state×2) | 4 | 全部经 `terminal_gate` |
  | DB 写(partial checkpoint P1a) | 1 | 经 `terminal_gate` + 原子 `_taskId` |
  | `done` 事件 | 2 | **刻意豁免**(它本就是终态信号) |
  | reconcile interrupt 分支 | 1 | 判定无需锚,前提由 tripwire 钉住 |
- **验收边界:** 本批零行为改动(注释 + 测试),无需重启。真浏览器未实测。

### 2026-07-31(续·守卫看不见打包在一行里的键) — 自主派单接自己开的票 `pt_715f5283c34d4294`,而**我自己的票面诊断是错的**;真正的缺陷比票面描述的那个严重,且**修的过程中我第一版又造了第三个洞,是 NEUTER「没咬」抓出来的**(commit `f781094a`,1 文件 +125/-14;本文件 5 passed/1 **FAILED** → **6 passed/1 skipped**;导出环 **48/49**;**NEUTER×3 各咬各的**)

- **★ 先证伪自己的票:** 票面写「`branding.js` 与 `visibility_defaults.js` **已不存在于工作树**(被兄弟移走)」。实测:**两个都在**(316/548 行),而且那两条参数化测试**都是 PASSED**。我上一批开票时把断言的**报错文案**当成了事实——那句 "the known offenders went missing" 是**守卫的猜测**,不是证据。**判据入库:开票前必须验证报错文案里的事实主张,报错消息是嫌疑而非结论。**
- **真因 A(票面那一半,成立):下界锚在文件布局上。** `visibility_defaults.js` **确实**已完全不含该 token(HEAD 上 `grep -c` = 0)——`0d3293da` 的品牌分组改造把逐 provider 可见性表换成了不以 provider 作裸键的形状。于是 `assert swept >= 2` 为一次**源码改进**而变红,而类不变量本身完好。改为**活性断言** `assert swept`:不论哪些文件带该键,每一个都必须 sanitize 成可解析 JS——这条性质**与文件个数无关**;零载体仍然红,因为空跑的 sweep 正是「一整类悄悄失去覆盖」的方式。
- **★ 真因 B(票面未列,更严重):正则 `^\s*meituan\s*:` 锚行首 ⇒ 看不见**打包在一行里**的键。** `static/js/core/model_group.js:54` 一行塞多个键:`mistral: 'Mistral', glm: 'GLM', meituan: 'Meituan', kimi: 'Kimi',`——**这正是本守卫存在的那个形状**,而「逗号落在哪」决定了守卫看不看得见它。覆盖实测 1 个,应为 2 个。
- **★ 而这不是理论上的整洁问题,有决定性实测:** 把替换 token 退化回 `your-provider`,`model_group.js` sanitize 后**是坏 JS**(`node --check` 非零),而**旧的锚定正则连候选都不认它**。一个真会白屏的载体在结构上是隐形的。
- **★ 第三个洞是我第一版自己造的,而抓它的是「NEUTER 没咬」:** 我最初把「`model_group.js` 必须在 swept 集合里」写在 sweep **内部**,并用 `if key_re.search(src)` 开门。这是**自我否证**的:一旦正则退回锚行首,那个门变成 false、断言被跳过、**整套照样 5 passed/1 skipped 全绿**。**sweep 结构上无法验证自己的量具**——它用同一个正则既挑文件又决定是否检查,所以量具变弱只会让它「少看几个文件」然后继续报绿。故把能力断言拆成**独立测试** `test_the_key_pattern_sees_a_midline_key`,钉在**固定字符串**上(不会被重排、不会搬家):四种危险形状必须命中(独占一行/行中打包/无空格/冒号前有空格),引号键必须不命中。**同族判据第 N 次:凡「扫描 + 断言」同用一个 pattern,必须另有一条测试直接钉 pattern 的能力。**
- **真因 D(顺手补):参数化测试的前置是空洞的。** `assert 'meituan' not in sanitized` 同时被「sanitizer 干了活」和「这里本来就没这个 token」满足。既然该文件已不含它,这一条一直在**绿着却对 sanitizer 什么都没断言**。改为先查**源码**,无可改写时**明确 skip 并写明理由**。
- **★ 一处我自己写错又当场改掉的话:** 我在注释里写「引号键是被 negative lookbehind 排除的」。**不对。** `'meituan':` 里**闭合引号**夹在名字与冒号之间,所以 `meituan\s*:` **根本匹配不上**,有没有 lookbehind 都一样;实测 lookbehind 只在**一种**输入上改变判定(有前引号无后引号的畸形 JS)。注释已改为陈述真实机制,以免下一个读者把「保险带」当成承重件。
- **NEUTER×3 各咬各的:** ①正则退回锚行首 → **只有**能力测试红(**正是这一发最初「没咬」,暴露了洞 C**) ②去掉 lookbehind → 同一条红 ③token 退回 `your-provider` → **3 条红且含 sweep**,即 sweep 现在能抓到它过去漏掉的真实产品回归。第三发后 `export.py` 经 `git status` 确认与 HEAD 一致。
- **验收:** 本文件 5 passed/1 **FAILED** → **6 passed/1 skipped**;导出环 + runtime_layout **48 passed/1 skipped** 零红。**产品面无需改动**:当前 token 是合法标识符 `yourprovider`,`node --check` rc=0,不存在现场泄露——本批修的是**守卫的视野**,不是产品。

### 2026-07-31(发版链条:同一个缺陷类的**六个实例**,而每一个都曾经全绿) — owner 问「exe 是不是最新版、发布流程自动吗」;线上 latest 停在 v0.14.2 而 VERSION=0.15.2,追下去不是一个 bug 而是**一条链**,且**其中四个是被前一个修复的成功暴露出来的**(commits `a4320287` `965436cf` `14e444a4` `80a917f4` `351770c2` `4c56b954` `6dc16036` + tofu-search `a6dadf2`;新守卫 **7 套**,**NEUTER×18**;发版仍**待 owner 上传 0.5.3**)

- **★ 贯穿全批的一句话:「我们校验的东西不是会发出去的东西」。** 六个实例形状高度同构,发现顺序就是被上一个修复逼出来的顺序:
  | # | 校验的 | 实际跑/装的 | 后果 |
  |---|---|---|---|
  | ① | 本地 `build-desktop.yml` | GitHub 上 7-23 的旧副本 | 三次发版饿死在退役 `macos-13`,**32 条守卫全绿** |
  | ② | 字节一致的 `requirements.txt` | 公共 PyPI 解不出 `tofu-search>=0.5.3` | 三条腿装依赖失败 |
  | ③ | 资产**文件名** | 49MB 空壳 installer | 名字对、体积错,闸看不见 |
  | ④ | 工作树里的 tofu-search | 干净检出 import 就炸 | 已坏 3 天,连自己的套件都 collect 不了 |
  | ⑤ | 本地构建的 wheel | PyPI 将来实际服务的字节 | 半个上传/传错副本都静默 |
  | ⑥ | **什么都没校验** | `install.sh`(真实用户的第一步) | **零守卫,且实测本机也是硬失败** |
- **★ ⑥ 与前五个不同族,值得单独提炼——而我第一版对它的诊断是错的,被 owner 顶回后实测证伪了自己:** 我原判「装了 0.5.2 的环境会命中 skip 探针 ⇒ 本机永远绿」。实测那段**逐字节真实**的探针:它**不只查符号,还比版本**(`_v(ts.__version__) >= _v(_TS_FLOOR)`),本机 0.5.2 < floor 0.5.3 ⇒ 退出码 **2** ⇒ 不跳过 ⇒ 落进安装分支 ⇒ 无 `vendor/` ⇒ PyPI 实测 `No matching distribution found` ⇒ **`fail`**。所以**本机与干净机器一样是硬失败**,不是假绿。
  **真实形状因此不是「本机绿」而是「没人跑过」**:前五个是「测试环境 ≠ 发布环境」,⑥ 是**「从没运行」冒充「运行得通」**——更隐蔽,因为**连一次失败观测都不存在**。**判据:零观测不是零缺陷。** 一个从未被任何自动化执行过的产品路径,它的「没有已知问题」不携带任何信息。
- **★ ⑥ 的守卫必须钉与外部状态无关的性质,这是 owner 批准我偏离指令的理由:** 票面原本要求「隔离环境能否跑通 installer」,但那条会把**发布状态**当作**代码正确性**断言——0.5.3 一上架自动转绿,从此**永久无法区分「installer 逻辑对」与「PyPI 恰好有货」**,且与 `test_requirements_public_resolvable` 同源重复。改钉三条与 PyPI 库存无关的:①floor 解析器(两边都从文件读、不硬编码 ⇒ 抬下限不会假红;真正防的是**静默降级到 fallback `0.4.0`** 从而装一个**低于真实需求**的版本);②`vendor/` wheel digest(install.sh **优先** vendor 于 PyPI,失败才回落 ⇒ 一个未验证的本地 wheel 能绕过整批建立的「已发布字节 = 已验证字节」不变量;**空是合法状态**并在 docstring 写明理由,否则将来有人往 vendor 放别的东西会把守卫改松);③**`fail` 不得降级为 `warn`**(server.py 把 tofu_search 列为 CRITICAL import ⇒ warn 会给用户一个「安装成功、启动崩溃」的体验,比硬失败糟)。
- **★ 而这条守卫的第 17 发 NEUTER 抓出的是**我自己守卫里的真 bug**,不是坏 fixture:** 把**真实已验证**的 wheel 放进 `vendor/` 本该绿,却红了。查下去是 `from test_published_dependency_identity import _EXPECTED_DIGESTS` 在 `pytest tests/<file>` 形态下抛 `ModuleNotFoundError`(`tests/` 不在 `sys.path`)。**这个失败的外观是「真 wheel 被拒绝」——正是最容易诱人去放松断言的那种形状。** 改为从姊妹文件**文本解析** digest(保持单一 owner),而不是插 `sys.path`——后者会让守卫的可达性取决于 pytest 的调用方式。
- **★ ① 的根因不是标签过期,是我们自己强推覆盖掉了修复。** `128ad422`(macos-13→macos-15-intel)是**直接在下游仓库**写的,让 v0.14.2 成功发版,但从未回流。两条独立证据:`git merge-base --is-ancestor` 判否;`git fetch` 逐字打印 `+ 128ad422...59bc8254 main -> origin/main (forced update)`。`export.py` 是 re-`git init` + 非 ff 即 `--force` ⇒ **下游独有的修改不是「丢失」而是「被回滚」,零日志零告警**。分叉清点 66 个远端独有文件,**零需回流**(7 个测试逐个查到上游有意删除的 commit;`write_tools.py` 已重构为包;`openai.json` 被 `bootstrap.py:176` 取代)。
- **★ ③ 比 owner 报的更深一层,而它是「运气救了我们」:** Windows 腿 `Install dependencies` **报告 success 而 pip 已经失败**——Windows 默认 shell 是 pwsh,多行 `run:` 里原生命令失败**不中止**,步骤取**最后一条**命令的退出码。于是 PyInstaller 打了一个依赖全缺的包:**48,960,018 vs 健康 115,822,886 字节**。三条腿一起挂才没发出去;若那三条是好的,完整性闸会数到四个资产然后 `make_latest` 把空壳钉成 Latest。**同一个不可满足的 pin,在三个平台响亮报错、在第四个平台静默产出损坏产物**——这个不对称才是缺陷。
- **★ 我自己在这批里造了一个缺陷,是跑套件发现的,不是看 diff:** 给 `PLATFORM_ASSETS` 加 `min_bytes` 把行从 4 字段加宽到 5,**打断三个位置解包的消费者**。最坏的是 `routes/api_v1/desktop.py`——它的表加载包在 `except Exception` 里、降级到 releases 页,所以生产症状是「直连下载链接悄悄消失」,**而所有发布闸照常绿**。教训:**单一真源只有在每个消费者都同意它的形状时才是单一的**,位置解包让「形状」成为承重且不可见的契约。arity 现已对全部消费者钉住。
- **守卫的自我约束(本批新形成的纪律):** 每条守卫都要回答「我测的是不是真正会生效的那个对象」——漂移守卫**同时覆盖两个发布远端**(`rangehow` + `NiuTrans`,实测都停在同一陈旧 sha);resolvability 守卫**硬编码 pypi.org、绝不走 pip**(本机 pip 指向内网镜像,走 pip 等于答另一个问题);import 守卫**子进程 + `PYTHONNOUSERSITE=1` + 断言 `__file__` 在导出目录内**(否则绿灯可能来自 site-packages 里那份 0.5.2);身份守卫读 PyPI 自己的 `digests.sha256`(pip 校验下载用的就是它)。**离线一律 skip 不 pass**——「没检查」报告成「一致」正是要消灭的那种谎言。
- **★ 前提守卫必须建模完整变换链,不是一环:** 漂移守卫用「逐字节相等」的前提是导出不改写这些文件,而 opensource 导出跑的是**三段链**(`export.py:2488-2490`:restore keep-files → `ruff --fix --unsafe-fixes` → verify)。只建模 sanitizer 会让守卫**因正当理由长红 → 被静音 → 失明**,正是它自己 docstring 警告的那件事。另补白名单守卫:`scripts/` 是 opensource 排除目录,`release_assets.py` 只靠**手工白名单**一条进公共树——加第二个脚本忘了登记,发布出去的 workflow 就调用一个不存在的文件。
- **★ NEUTER 的两次空转,都是变异无效而不是守卫失明:** ①把 `PYTHON_VERSION` 换成 `PYTHON_VERSIOX`,而 `ci.yml` 里该 token **出现 0 次** ⇒ 文件原样写回;②手打一个 hash 想验证正向路径,hash 本身是错的。**判据:NEUTER 不咬时先比 sha/证明变异真的改了行为,再谈守卫是否承重。** 14 发里其余 12 发各咬各的。
- **④ 的处方被 owner 纠正,而纠正是对的:** 我原打算「只摘 `DOMAIN_META` 出来修好 HEAD,travel 留到 0.5.4」——**摘不出来**。缺的三个符号(`DOMAIN_META`/`available_types`/`describe_domains`)是同一次重写的产物:`DOMAIN_META` 字典体里直接写着 `travel` 条目,后两个围绕「按凭据可用性过滤域」的新语义。且「0.5.3 的干净版本」在 git 历史里**从来没存在过**(`ddbd504` 起 8 个提交全带这个洞,含 0.5.3 feature 提交本身),所以回滚是构造新东西而非恢复旧的。按 A 整体提交,全量套件 exit 0。
- **验收边界(诚实记录):** **发版尚未完成**。0.5.3 产物已构建 + `twine check` PASSED + 隔离环境 import 实测通过,校验和 `0b840779…`(whl)/`e4d1120a…`(tar.gz),tag `v0.5.3` 已打,**上传需 owner 凭据**。上传后判据:resolvability **35/35** + 身份守卫 hash 匹配 → 再发版 → `/releases/latest` = v0.15.2 + 四平台 + SHA256SUMS + Windows 包过 81MB 闸。另开票 `pt_368a59cbbc1647b3`:**体积合理 ≠ 装上能启动**,hiddenimports 漏一项不影响体积也不影响退出码,只在用户双击那刻炸——按 owner 指令不塞进本批。
- **Chrome 那一问已定性关闭(`pt_d30b63f98bfb4f70`):** Windows/macOS 外部安装的 `update_url` **必须指向应用商店**、本地 CRX **仅 Linux**;非商店 force_install **要求 AD 域**;`--load-extension` 在 **Chrome 137 已移除** ⇒ **要管理员权限也绕不过**。顺带修了真缺陷:`tofu.spec` 的 `datas` 缺 `browser_extension`,桌面版点「下载扩展 ZIP」必 404——**恰恰是那个最拿不到扩展的发行版**。

### 2026-07-31(续·闸修好了,但产品的入口够不着它) — 自主派单接**已 DONE** 的 `pt_d689f2016ecf4311`;票面前提「修完了」为真,而**实测发现那个修复在 HTTP 入口结构上不可达,同一缺陷在守卫落地后又复发了 2 次**(commit `920f83ae`,2 文件;新守卫 **4 条(2 条失败先行 + 2 条一开始就绿的补集)**,**NEUTER×3 各咬各的**(2/1/1);相邻环 **339 passed**;活库闭环 `needsYou 0→2`)

- **★ 派单给我的 epic 已经是 DONE,但活库数据当场推翻了「已修复」:** 一致性闸 `9e2a0481` 确实在 HEAD 里(`question_required` 拒绝 + 短语表 + 响亮截断日志),可 `pt_3879f00e` 的 `block_count` 在**闸落地之后**从 4 涨到 **6** —— 仍然 `block_question=None`、`reason` 仍然**恰好 2000 字符**、散文里仍然逐字写着「STILL AWAITING owner one-click on the 4-option question card」。**一个已经上线的守卫,眼睁睁看着它要防的缺陷又发生了两次。**
- **★ 根因是分层可达性,不是判据错:** `_claims_a_question_card()` 对那段**已存储**的散文实测返回 `True`(短语 `'question card'` 落在第 1829 字符,截断前就在),所以闸的判据完全正确。真正的洞在 `routes/api_v1/project.py:806`——`block_task(project_path, conv_id, task_id, reason)` **四个位置参数**,`question`/`options` 既不从 body 读、也不往下传。⇒ 对**每一个走 HTTP 的调用方**,能构造出来的 block **只可能是无问题的那一种**,闸永远不可能被触发。**判据入库:一道闸的可达性等于能够到它的最薄的那个调用方;只在库边界设闸而不在路由设对应守卫,等于在产品真正的入口处没有闸。**
- **★ 而这个洞为什么能活过写闸的那一批:实测 `grep -rn "board/block" tests/` = 0 命中 —— 整个仓库没有任何一条测试碰过这个路由层。** 上一批把闸和它的 7 条守卫都写在 `block_task` 这一层,于是「库函数对了」被误读成「功能对了」。这与本项目记录在案的同族错误一致:**代码对了、没人证明产品路径上对**。
- **失败先行的形状值得单记:4 条守卫里只有 2 条该红。** 另 2 条(拒绝必须以 400 上抛、无问题的 `[sibling]` block 必须继续合法)**一开始就是绿的**——这正是它们作为**补集**的价值:如果「透传 question」可以靠「让每个无问题的 block 都失败」来满足,那修复就会打断正常的 `[sibling] path=…` 与基建签字类 block。补集绿着才说明我没有用过度触发换绿灯。
- **★ 第一版守卫红得不是地方,差点验错东西:** 3 条挂在 `@require_auth` 的 `RuntimeError: Not within an app context` 上 —— 那是我的 harness 缺陷,不是被测缺陷。**一条因为错误原因而红的失败先行守卫,和没有守卫等价。** 改为沿 `__wrapped__` 剥掉装饰器直接驱动 handler 本体后,才拿到「2 红 2 绿」这个**真实**的先行状态。
- **NEUTER×3 各咬各的:** ①退回四位置参数(复现原始缺陷)→ **2 红**;②传 question 但丢 options → **1 红**;③吞掉拒绝(不再返回 400)→ **1 红**。三发后 `routes/api_v1/project.py` 均 `cmp` 逐字节还原。
- **回归判据用干净 HEAD A/B,不用票面数字:** 相邻环 4 个红灯(`test_project_board_autonomy_rule` ×2、`test_project_brain_influence`、`test_project_brain_live_paths`)**全部先于本批存在** —— `git archive HEAD` 隔离副本跑同一组得到**逐条同名的 4 个失败**。按 owner 偏好不在本批顺手修。
- **活库闭环(本批的另一半):** 用**刚刚修好的那条结构化通道**把两个 epic 真正问给 owner。`pt_3879f00e` 的原始 4 个选项已随 2000 字符截断**永久丢失**(实测幸存段止于 "…with te"),故按幸存散文重建为 4 个可执行选项。实测 `needsYou 0→2`、两条都带真卡片(4 / 3 个选项),且**均已从 `select_dispatchable` 中排除**——不再每次心跳烧一个 billed turn 去重新发现同一个没人能答的门。
- **验收边界:** 后端已对活库生效;**零前端改动**,无需重启或重建 bundle。前端 `api.js` 的 `boardBlock` 仍只发 reason,这是**刻意的**——那是人类在面板上手动阻塞的生命周期控件,按定义没有结构化问题;缺陷在于路由**丢弃**调用方确实传了的字段,不在于人类控件没传。

### 2026-07-31(续·导出树里两个套件根本跑不起来,其中一个是我上批弄坏的) — 自主派单接自己开的票 `pt_045b37a1b05349b7`;票面只列了 1 个缺陷,实测是 **3 个**,而**其中一个是我自己上一批引进的回归,是我审计自己而不是别人报的**(commit `c1e986a4`,3 文件 +187/-11;守卫 3→5;**NEUTER×4 各咬各的且诊断可区分**;本仓 **123/123**,导出树 22 passed/4 skipped)

- **★ 先证伪票面的一半:那两个 `scripts/` 文件根本不是「漂移」。** `.gitignore` 里**早已**写着 `!/scripts/cache_waste_report.py` 与 `!/scripts/cache_ab_probe.py`,各带多行理由 ⇒ 它们是**有意跟踪**的,只是从没在 keeper 一侧登记过。票面把「未登记」读成了「两张表不同步」,而实际是**一张表说了话、另一张表没记账**。
- **★ 语义必须先分清,否则会修反:** `export._OPENSOURCE_KEEP_FILES` 的含义是**「照样发布出去」**,而这 18 个文件要的是**「留在库里、永不发布」**——两者相反。故全部进测试侧的 `_EXTRA_KEEPERS`,一个都不进 `_OPENSOURCE_KEEP_FILES`。登记前实测两脚本无密钥、无绝对内网路径(`cache_ab_probe` 点名了自家网关与一份内部报告,**这正是它必须不发布的理由**)。
- **母稿用前缀登记而不是抄 16 条路径:** 加一个姿态/重渲一帧都不该要求改守卫,且前缀下每条路径的理由**逐字相同**。但前缀是**一张空白许可**,所以给它配了自己的陈旧纪律 `test_keeper_prefixes_are_live_and_narrow`:①必须以 `/` 结尾(不然会顺带匹配 `a/bc` 这种兄弟路径)②必须仍覆盖到至少一个 tracked 文件(不留死许可)③覆盖到的**每个**文件都必须真的被导出排除(不许越界罩住可发布的东西)。
- **★ 票面没列的缺陷 A:导出树连**收集**都做不到。** `tests/test_cache_waste_report.py` 在**模块作用域** exec `scripts/cache_waste_report.py`(`cwr = _load()`),而 `tests/` 随导出、那个脚本不随 ⇒ 公开树里 `FileNotFoundError` 发生在 collection 阶段:`Interrupted: 1 error during collection`,**整文件贡献 0 个测试**。改为模块级 skip 并写明原因;**双向验证**:本仓 24 passed(skip 没有泄漏进私有库)、导出树 1 skipped。
- **★ 缺陷 B 是我自己上一批(`5c26fdb0`)引进的,用 A/B 定量:** 我新加的 3 条尺寸守卫从**母稿**重新推导全局 scale,而导出树没有母稿。在模拟导出树上对拍:**改动前 1 failed/13 passed,改动后 4 failed/22 passed** ⇒ 那 4 条里**恰好 3 条是我的**。也就是说,**修好宠物抖动的那一批,同时让公开树的宠物套件硬红**。
- **修法选「按主体可用性拆分」而不是四个 try/except:** 一个共享前置 `_require_masters()` 只检查那**一个**前提并说明理由。判据是:主体缺失时**降级为有理由的 skip**,而**不是**硬红,更**不是**静默通过——「在主体不存在的树上照样报绿」是三者里最坏的,那等于让绿灯不再有含义。只读**出厂帧**的断言在所有树上都跑;依赖母稿的断言在**有母稿的地方**跑,而那正是每一个私有 checkout,也就是**管线唯一能被重跑、因而不变量唯一可能被打破的地方**。`test_shipped_frames_match_the_pipeline` 一并接入——它就是改动前那 1 条红灯,是同一问题的**原始实例**。
- **★ NEUTER 不但要咬,还要**互相可区分**:** 前两发都让 3 条测试变红,如果只看数字会以为守卫不分辨。故让 prefix 那条测试在第二发时**点名漂移的确切路径**(`the exact-path keepers have drifted: ['scripts/cache_ab_probe.py', ...]`),于是「前缀丢了」与「exact-path 丢了」给出**不同诊断**而非同一句话。四发:①删母稿前缀(复现票面原始缺陷)→3 红,offender 恰为 16 张母稿 ②删两个 scripts 登记 →3 红且点名 ③摘掉 `_require_masters` →导出树回到 4 硬红 ④摘掉 cache_waste 的 skip 闸 →导出树回到 collection ERROR。事后所有文件 `cmp` 逐字节还原。
- **顺手补的一处静默漏跑:** 该文件的 standalone `main()` 只列了 3 个测试,新加的 2 条**不在其中**——用 `python3 tests/...py` 跑会静默漏掉它们。已接入。
- **回归与归属:** 本仓 **123/123**(宠物环+缓存审计+漂移守卫);导出环 42/43,**唯一红灯经纯净 HEAD A/B 判为先于本批存在且与本批零交集**——`test_export_js_sanitize_syntax` 断言 `swept >= 2` 并点名 `static/js/branding.js` 与 `visibility_defaults.js`,而这两个文件**当前已不在工作树**(兄弟会话移走),即一个**锚在会移动的文件名上的计数下界**。按 owner 偏好开票 `pt_715f5283c34d4294` 而不在本批顺手修。

### 2026-07-31(续·第三个铸造点:判决被**写进数据库**) — owner 抓出前两批只堵了两条**快照传输**,而第三处**写库**,所以刷新后从 DB 复现,比内存态顽固;**而它同时绕过我那两层**(commit `6be8fa43`,3 文件 +435/-20;新守卫 **6 条**(失败先行 5 红),**NEUTER×3 各咬各的**;262 套 A/B **零新坏零修好**)

- **★ 为什么这一处比前两处严重:** `_sync_partial_to_conversation` 不是快照传输 —— 它**写 `conversations.messages`**。前两批的 `terminal_gate` 管的是「发出去什么」,管不到「存下来什么」。存下来的那一行会被下一次加载读回,所以**刷新不但不能自愈,反而是复现路径**。
- **实测缺陷:** P1a 块的触发条件是 `if task.get('finishReason'):` —— 纯存在性。而 `_finalize.py` L843 打 finishReason、L954 才 `status='done'`,中间 **110 行**含阻塞的 `_generate_tool_summary`;**5 秒**检查点定时器常态落在窗口内 ⇒ 把「已落定」写进一条**还在生成**的消息行。
- **★ 复合缺陷是 owner 抓出的那一半,也是真正致命的一半:这条路从不写 `_taskId`。** 全库只有 `_sync_result_to_conversation`(终态同步)写它。于是落库的是 `{finishReason:'stop', _taskId: 缺失}`。实测把这行喂给 `assistantTailIsPriorTurn`,**即使问的是它自己的 task 也返回 true** —— 身份臂缺锚不生效,reload-safe 臂照常开火。**我上一批新加的身份臂在这条路上结构性失效**,因为锚根本没被写下来。
- **修法两半,各自独立承重:** ①触发条件改为 `is_terminal_status(status) or task['_finalize_started_at']`,复用 `terminal_gate`(**第三个消费者**),不再手写第三份时序假设。`_finalize_started_at` 在 **L953** 打 —— 正好在 110 行窗口**之后**、终态翻转**前一行** —— 精确接纳 P1a 真正要服务的场景,排除制造矛盾的那段。②携带判决时**原子写入 `_taskId`**。
- **★ 我的首版守卫又是空的,而这次的错法比上次隐蔽:** 窗口内那条我写成「要么不带 finishReason,**要么**与 `_taskId` 同时出现」。NEUTER-2(退回 presence-only)**依然全绿** —— 因为 `_taskId` 那一半单独就能满足这个析取式,**时序闸看起来是装饰**。已拆成两条**独立命题**:窗口内**一律不落判决**(钉时序闸)+ 真 finalize 时**判决与锚同时落**(钉原子性)。**判据:一个 `A or B` 形式的断言,永远无法证明 A 和 B 各自承重 —— 两半修复必须两条测试。**
- **NEUTER×3 各咬各的:** 去掉 `_taskId` 原子写 → 咬 2 条 carried_with_identity;退回 presence-only → **只**咬 midwindow;过宽(永不携带)→ 咬 2 条 complement(证明「永远不发」不能满足本批,否则 P1a 的存在意义被删)。
- **陈旧套件就地反转 —— 这是第三个把缺陷当契约认证的:** `test_interrupted_turn_metadata.py::test_partial_sync_writes_finish_metadata` 的 fixture 正是 `status='running'` + `finishReason='stop'` + 无 latch,原断言**要求**判决被写进消息。三批下来同一形状出现三次(wire_parity / prior_turn_reducer / 本条),说明**「测试固化了当时的行为」在本项目是一类系统性风险,不是偶发**。
- **★ 共享树纪律翻车一次,如实记录:** pathspec 是对的(3 个文件、计数门通过),但 `_sync.py` 在工作树里**已经带着兄弟会话 `ms8eg47r76ei3x` 的未提交改动**(`checkpoint_task_partial(force=)` + `_has_inflight_round`,属 `pt_1a82ffb31f714eeb`),于是它们**搭我的 commit message 上了车**。**pathspec 只能限定「哪些文件」,不能限定「这些文件里的哪些行」** —— 这是本项目纪律的一个真实缺口。事后处理:①不回滚(回滚等于删同事的活),②跑他们的相邻套件确认未损(22/22 全过),③`project_message` 告知对方代码已在 HEAD、勿重复应用,并说明我在同一函数里加的新约束。**下次:改共享热点文件前先 `git diff -- <file>` 看有没有别人的行。**
- **回归:** 262 套 A/B 失败集 diff(纯净 HEAD vs 纯净 HEAD+我的 3 个文件)**零新坏零修好**,27 条基线红两侧逐条相同。
- **顺带把 docstring 改成真话:** 原文写「Terminal-only fields are withheld while the turn is mid-stream」,而代码按存在性一律携带 —— 与 `finish_info.js` 那条同族的假注释,**本批是这个形状的第三次**。
- **验收边界:** 后端已生效,数据库写路径**无需 bundle 重建**即刻生效。真浏览器未实测。

### 2026-07-31(取证行写对了,但写去了没人看的地方) — 自主派单接 `pt_f97a21c02bda4ab9`;票面说「等复发」,**实测复发已经发生 6 次**,而我上一批的取证**一次都没捕获到**——不是没生效,是**写进了一条会被丢弃的通道**(`pt_f97a21c02bda4ab9` DONE;2 文件;套件 **5 → 11**,**NEUTER×3 各咬各的**(2/1/1);相邻环 **58/58**)

- **★ 第一步就把票面前提推翻了:** 票面写「等待下一次复发以确定触发源」。实测 `logs/error.log` 的 `Uncaught exception` 时间戳:**10:33:27 / 11:10:23 / 11:10:35 / 11:13:15 / 11:13:50 / 11:15:22 / 11:26:32** —— **7 次**,其中 6 次发生在原始那次之后,且 11:26:32 那次在取证 commit `95dabfd2`(11:23:32)**之后**。所以触发条件早就满足了,epic 却在等一个已经来过的事件。
- **★ 而真正的发现是:取证行一次都没捕获到,原因是通道错配。** `grep -c 'libstdc++ soname' server_15000.log` = **0**。追下去:`server_15000.log` 的 mtime 停在 **10:33:27**,而崩溃发生在 11:10–11:26 ⇒ **那些 boot 根本不是 watchdog 拉起的**。取证行走 `os.write(2)`(stderr),而 watchdog 是唯一把 stderr 重定向进 `server_15000.log` 的角色;非 watchdog 启动的进程,stderr 去了终端或管道,**没人留存**。与此同时崩溃记录本身走的是 **logging → `logs/error.log`**(7 条 GLIBCXX 都在那里)。**两条通道从不相交:操作员会去读的那份报告里,恰恰没有取证。**
- **判据澄清(避免下一个人误判):** 取证行**本身是对的**,顺序也是对的(实测崩溃形态下取证在第 3 行、ImportError 在第 28 行)。坏的只是**耐久性**——`stderr` 不是耐久通道。所以修法不是重写取证,是让链接状态**搭上崩溃记录本身**。
- **落点:** ①把取证结果留在模块级 `_TOFU_LINKAGE_FORENSICS`;②`_crash_excepthook` 对**链接类** ImportError(消息含 `GLIBCXX`/`libstdc++`/`symbol`)追加 ` | LINKAGE: …`。端到端实测:崩溃形态跑真 `server.py`,`logs/error.log` 里出现 `Uncaught exception — process is terminating | LINKAGE: libstdc++ soname -> mapped=/usr/lib64/libstdc++.so.6 | LD_PRELOAD=… | LD_LIBRARY_PATH=…`。**选择性**也实测:`ValueError` 与 `ImportError('No module named foo')` 均**不**附加。
- **★ 本批第二个自我发现:我写的第一版守卫有盲点,是 NEUTER 打空暴露的。** NEUTER-C(把 `_TOFU_LINKAGE_FORENSICS` 改名 = 破坏捕获)**10/10 全绿**。查下去:钩子用 `globals().get(..., 'unavailable')` 兜底,于是崩溃记录退化成一句 **`LINKAGE: unavailable`**——注解还在、信息没了,而静态断言(grep 到 `LINKAGE`/`_TOFU_LINKAGE_FORENSICS` 字样)与 hook-shape 测试**照旧全绿**。**这与本 epic 修的是同一族缺陷:一个存在但不携带信息的仪器。** 补 `test_real_crash_writes_a_usable_binding_into_the_log`——真跑崩溃形态、读 error.log、断言不得含 `unavailable` 且必须点名 `lib64`。重跑 NEUTER-C:**精确只咬这一条**。
- **★ 而这条新守卫第一次跑是 SKIP,差点变成一条永远不执行的装饰:** 它去读 `<repo>/logs/error.log`,而 `tests/conftest.py` 为日志隔离把 `TOFU_DATA_DIR` 指到临时目录(那段注释正是 2026-07-27「app.log 一天涨到 9.1GB」后加的)⇒ 子进程写的是隔离副本,repo 的 error.log 零新增 ⇒ 判据落到 `pytest.skip`。**2.16s 就跳过是线索:真崩溃要 1.4s 启动**。改为按 `TOFU_DATA_DIR` 定位日志后 **11/11 无跳过**。**判据:一条 skip 掉的守卫和一条不存在的守卫等价,必须确认它真的执行过。**
- **NEUTER×3 各咬各的:** ①崩溃钩子退回原样(复现本轮缺口)→ 2 红;②去掉选择性、无条件附加 → 1 红(`test_linkage_attachment_is_selective`);③破坏捕获 → 1 红(新端到端守卫)。三发后 `server.py` 均 `cmp` 逐字节还原。
- **刻意不做:** 票面列的防御性加固(`LD_PRELOAD` 前置 conda libstdc++ + 新 re-exec)**仍不落**。触发源依旧未知——本批修的是「下次能不能拿到证据」,不是「猜一个原因去堵」。加固处方与其边界条件已在票面存档,等真实取证到手再判。
- **验收边界:** 需重启生效;当前活进程(10:33:27 启动)早于两批改动。

### 2026-07-31(续·终态信号闸收口) — owner 顶回上一批:**「你把 SSE 那条写进 commit message 当作第二层的理由,但它是源头」**;而「第二层挡得住」这个前提**本身就是假的**,有第二个消费者根本不过 reducer(commit `c6989082`,6 文件 +416/-18;守卫 **9→12 全行为断言**,**NEUTER×4 各咬各的**;200 套 A/B **零新坏零修好**)

- **★ owner 的纠正比「还有一处漏了」重一档:** 我上一批**知道** `lib/chat_dispatch.py` 也会把 `finishReason` 拷进快照 —— 我把它写进了 commit message,当作「所以要做第二层防御」的**论据**。这是本末倒置:它不是「下游需要挡住的风险」,它是**和 poll 并列的第三、第四个铸造点**。按项目目标(根因,不打补丁)它必须一起修。**手里已有的证据被用来论证「不修」,是我这两批里最该记的那个形状。**
- **★ 而「第二层能挡住」这个前提实测为假:** `static/js/ui/finish_info.js:720` 的 `const _terminal = msg.finishReason || msg.usage;` **不经过** `assistantTailIsPriorTurn` ⇒ SSE 路径上一个**还在生成**的轮次会被画上已落定的 finish bar。所以前端 reducer 那一臂**结构上覆盖不到**这个消费者。更值得记的是它上一行的注释:「the mid-stream checkpoint … **WITHHOLDS** finishReason/usage until completion」—— 前端**已经写下了对后端的假定**,而后端从未兑现。上一批只让它在 poll 上成真,SSE 上仍是假话。**同族假注释第 N 次。**
- **四个快照面,脚本枚举而不是靠读:** 写脚本扫 3 个文件里所有「把 finishReason 拷进快照 dict」的循环并判定是否过闸 —— 修前 4 个 state/poll 面**全部 ungated**;修后 **4/4 GATED**,仅剩 2 个 `done` 面 ungated(**正确**,done 本就是终态)。这比逐个人工找可靠。
- **落点:新增 `lib/chat/terminal_gate.py`,规则只存在一次。** `filtered_snapshot_meta(meta, status)` 按**该快照自己上报的 status** 判定。poll 那两个本地常量**删除**改为 import —— 否则就是第四份手抄的字段策略,而 `extract_task_meta` 的 docstring 正好记录了这种不对称已经让本项目付过什么代价。
- **★ 刻意 NOT 下沉到 `extract_task_meta`(这是本批最需要论证的取舍):** 它的输出**同时**喂给真终态 `done` 事件(chat_dispatch late/synth done、routes/chat.py late done)。在那里设闸会把终态字段从「专门用来送达它们」的事件里剥掉。闸属于**快照边界**。补 `test_terminal_gate_does_not_strip_terminal_done_events` 钉死,NEUTER(把闸塞进 extract_task_meta)精确咬红 —— 这条 NEUTER 存在的意义就是防止后人「顺手收敛一下」。
- **闸的范围按实测收窄,不图省事全扣:** 只扣 `finishReason/usage/preset`(preset 与前两者在 `_finalize.py` **同一赋值块**,时序相同)。`model/thinkingDepth/provider_id` 出生即有、live 气泡靠它渲染模型标签,且 finish_info.js **明确**视 model-only 为非终态;`toolRounds/content/thinking/phase/apiRounds/error` 是进度不是落定声明 —— 全部照发。守卫对「model 必须存活」「apiRounds 必须存活」各有一条正面断言。
- **★ 首版 SSE 守卫用真 HTTP,实测挂死 299.3s —— 而这个失败本身是有信息量的:** `running` 任务的 `/api/chat/stream/<id>` 按设计保持连接 **7200s**(`_MAX_SSE_DURATION`),test client 读不到 body;而把任务注册成 `done` 就摧毁了被测场景本身。改为在**函数边界**驱动真实构造器 `build_fresh_state_snapshot`,并把「为什么这里不能用 HTTP 往返」写进 docstring —— 否则下一个人会以为我偷懒,再撞一次 300s。
- **NEUTER×4 各咬各的:** 共享闸 pass-through(咬**2 条** SSE 守卫)/ 内存 state 面 / DB-row state 面 / 错误缝。**第三发第一次打空**:DB-row 面当时**已过闸但没有任何测试驱动它** —— 这正是让原 bug 活下来的那类盲区(代码对了、没人证明)。补 `test_sse_db_row_state_branch_...` 后复跑,共享闸 NEUTER 现在**同时**咬两条 SSE。三个产品文件事后 `cmp` 逐字节还原。
- **陈旧套件就地反转:** `test_routes_chat_wire_parity.py::test_build_fresh_state_snapshot_carries_full_task_state` 的 fixture 是 `status='running'` **且** `finishReason='stop'`,原断言要求三个终态字段**出现在** state 里 —— **它把这个缺陷当作「byte-parity 正确」认证了**。改为断言不出现 + 补终态 complement(done 时必须照发,否则闸可以靠「永远不发」满足)。非终态字段的 byte-parity 一字未动。
- **回归:200 套 A/B 失败集 diff(纯净 HEAD vs 纯净 HEAD + 我的 8 个文件)—— 零新坏零修好**,18 条基线红两侧逐条相同。
- **另开票不顺手改(owner 指示):** `pt_8a2f741ee4634cdc` —— SSE 每次 0.1s / 0 事件断连,100% 流量退化到 1Hz poll(925 次 poll vs 5 次 send,`/api/chat/stream` 命中 **0**)。票面要求**先取证再改码**:先分清「前端根本没发 stream 请求」与「发了但被代理吃掉」。
- **验收边界:** 后端已生效;前端注释与 reducer 需重启 + bundle 重建。真浏览器未实测。

### 2026-07-31(发版体检 → 「什么都不需要你」是假的) — 为 0.15.2 发版做全面体检时,实测抓出 board 上**两个 epic 静默停摆 ~19h**,而注意力面正报告 `needsYou:0`;根因不是数据脏,是 `block_task` 的 API **允许「有问题但没登记问题」这个状态**(`pt_d689f2016ecf4311` DONE;commit `9e2a0481`,2 文件;守卫 **7/7 失败先行**,**NEUTER×5 各咬各的**(3/2/1/2/1);相邻环 **148 + 614**;活库闭环 `needsYou 0→1`)

- **★ 病症的判据比「有个 bug」重一档:面板逐字告诉 owner「没有任何事需要你」,而两条工作流已经停了 19 小时。** 而 `pt_3879f00e` 的 `block_reason` 里**逐字写着**「STILL AWAITING owner one-click on the 4-option question card」——**它点名的那张卡从来不存在**(`block_question` 为 `None`)。
- **★ 双向失明,这才是它能活 19 小时的原因:**
  | 面 | 判据 | 后果 |
  |---|---|---|
  | 对人 | `project_attention._board_questions` 按 **`block_question` 列**建 blocking 项;散文里的 `[human-gated]` 前缀**全库从不匹配**(该模块 docstring 自己写明了) | 不进「Needs you」 |
  | 对机器 | `select_dispatchable` **只**跳过 `block_question` 已设的行 | cooldown 一过就重新可派单 |
  ⇒ 这个 epic **既没到达人,也没真的停下**:每次心跳都可能烧一个 billed turn 去重新发现同一个没人能答的门。实测 `pt_03f4cdf1` 已 **blocked 10 次**、累计休眠到 ~23.6h。
- **★ 第二半:两条 `block_reason` 长度**恰好都是 2000**(=`_TITLE_MAX_CHARS`),被截断在词中间("no early ex" / "(exten")——作者枚举的选项正落在被砍掉的那一段里,而**没有任何日志记录发生过截断**。静默截断把「我写了选项」变成「选项不存在」,且不留任何可诊断的痕迹。
- **根因是 API 形状,不是这一条数据:** `reason` 是自由文本、`question` 是可选 kwarg,**二者无一致性约束**。修法沿用 `update_decision` 的 `summary_required` 先例:散文声称有卡而未登记 ⇒ **在任何 mutation 之前**拒绝(`question_required`)。拒绝优于静默接受——把工作停在一个不存在的控件后面,是唯一不可留的行为。
- **★ 而我第一版的短语表被自己的「过度触发」补集当场咬红,这是本轮最有价值的一条:** 我把 `awaiting owner` / `awaiting the owner` 放进了声称短语表,结果它咬中**合法**的 `[sibling] path=lib/x.py awaiting the owner of that file`——那里的 "owner" 指的是**文件的属主**,不是要做决定的人。**一个同时描述普通兄弟协作的短语,不能承载这条拒绝**。已删除该短语,并给 `[sibling]` 加了构造性豁免(它按定义不可能有人类问题)。判据:凡新增「禁止某种措辞」的守卫,必须先写补集,否则你只是在惩罚合法用法。
- **NEUTER×5 各咬各的(3/2/1/2/1):** 摘掉拒绝(复现原始缺陷)→ 3 红;先 mutate 再拒绝(半应用)→ 2 红;静默截断 → 1 红;**过度触发**(拒绝每一个无问题的阻塞)→ 2 红;删掉 `[sibling]` 豁免 → 1 红。五发后产品文件 `cmp` 逐字节还原。
- **回归判据用干净 HEAD A/B,不用票面数字:** 相邻环 5 个红灯**全部先于本批存在**——`git archive HEAD` 隔离副本上跑同一组套件得到**逐条同名的 5 个失败**,嫁接我的 2 个文件后**仍是同一份名单、+7 passed**。其中 `test_project_board_autonomy_rule` 那 2 条是 charter #0(agent 不再持有 `project_charter_commit`)留下的**陈旧守卫**,按 owner 偏好不在本批顺手修,已如实留在票上。
- **活库闭环(本 epic 的另一半):** 用**结构化通道**(即本次修复强制的那条路)把那个从未被问出口的问题真正问给 owner——`pt_03f4cdf1` 现在带 3 个选项出现在「Needs you」,实测 `needsYou 0→1`、`blocking=1`,且它**已从 `select_dispatchable` 中排除**(不再空转派单)。`pt_3879f00e` 已由兄弟会话 `mrxinirv0t6n6v` 认领推进,其问题自然消解,故未一并处理。
- **发版体检的其余结论(未动手,已开票/留证):** VERSION=0.15.2 而最新 release 仍是 v0.14.2 ⇒ 该版从未发布;`CHANGELOG.md` 最新条目是 `[0.10.0]`,**0.11.0–0.15.2 共 9 个版本零条目**、91 行滞留在 `[Unreleased]`;`docs/ARCHITECTURE.md` 自述「Last re-scanned 2026-07-18 / VERSION 0.13.0」;README 的 Project Structure **37 条路径里 7 条已不存在**(`lib/fetch`、`lib/search` 已外迁,`endpoint.py`/`executor.py`/`image_gen.py`/`mt_provider.py` 已 .py→包);`pyproject.toml:7` 仍是 0.13.0 但**运行时无人读**(`lib/version.py` 读 VERSION 文件)⇒ 仅整洁问题。发布工作流实测健康:VERSION 驱动而非 tag 驱动,runner 标签 `ubuntu/windows-latest` + `macos-15`/`macos-15-intel` 全部在役,下载直链已钉 tag(88 tests green)。
- **★ 一条方法论自纠:** 我曾用 `grep 'lib/fetch' README.md` 得到 0 命中,据此判定「README 没有这个陈旧路径」——**错了**。README 用制表符画树(`├── fetch/`),路径里根本不含 `lib/` 前缀。**负向 grep 结果是弱证据**;后来用 `ls -d` 逐条实测才拿到真实的 7 条。
- **`git stash@{0}` 判定为可安全丢弃(未执行,留给 owner):** 内容是 `lib/llm_sanitize/_gateway.py` 的**半完成删除**(19 行,删掉了 `_GATEWAY_BLOCKED_TERMS = {` 开括号),实测 `py_compile` **仍然 IndentationError**;其功能已由 `fd885a7e` 的 ZWSP 机制取代且更强;`.git/logs` 与 stash reflog **均存在**(此前一份只读结论说「无 reflog、丢了不可恢复」,已被实测证伪)⇒ 丢弃可恢复。
- **验收边界:** 后端已对活库生效;本批**零前端改动**,无需重启或重建 bundle。全量 13182 测试**收集零错误**,但整套跑被中断,故**未**给出全绿断言。

### 2026-07-31(宠物「抖动」的根因在管线不在美术) — owner 报「宠物动起来像在抖,而且有点丑」;**实测证伪了「丑」这个前提:美术是无辜的,是管线把它抖坏的**,而 owner 复核又补上我漏掉的第二个成因(`pt_4857e21b2ac6447d` DONE;commit `5c26fdb0`,26 文件 +771/-64;宠物守卫 **82 → 94**,**NEUTER×4 各咬各的**(3/1/2/2);场景环 **111/111**)

- **★ 三个缺陷,同一个根:`process_ai_frames.py` 对**每帧独立** trim 到自己的 alpha bbox、再缩放到自己的最长边 = MAX_SIDE ⇒ **缩放系数是姿态的函数**(0.1837–0.2319,26% 跨度)。于是:
  | 缺陷 | 实测 |
  |---|---|
  | 尺寸抖动 | 腮红间距(**刚性**面部特征,不可能合法变化)在**同姿态**帧间漂移 **16.5%**、walk1..4 内 **9.7%** ⇒ 走路时以 13fps 呼吸自身宽度的十分之一(口径见下方更正条:全 22 帧那一档含两个**故意**的极限姿态,衡量的是姿态幅度而非抖动) |
  | 横向传送 | 按**墨迹** bbox 居中 ⇒ 不对称特效(思考气泡/星火)把**身体**推偏最多 **2.27px**(30px 精灵的 8%),仅**切换心情**即触发,没有任何东西在移动 |
  | 特效用身体尺寸买单 | 星火更大的姿态被缩得更小,身体为特效腾地方 |
- **★ 母稿是无辜的,而这一条决定了「不重画」:** 走路帧身体宽度在**母稿**里只差 **1.2%**,出厂却 6.4% ⇒ 缺陷 **100% 在下游**。owner 明确否决重画:「我已经签过这个角色,测量说它无辜就别重开美术」。**判据入库:先量母稿再决定是不是美术问题**,否则会把一个已批准的角色拿去冒无谓的风险。
- **修法是「量角色,不量墨迹」:** `_anchors()` 取**最大不透明连通域**(即豆腐块本体,天然排除游离特效)得身体中心 x + 脚线 y;`_layout()` 从全帧极值推**一个** scale 与**一块**画布;每帧按「身体中心对画布中线、脚线对画布底行」合成。**尺寸恒定与配准从此是结构性质**,不是「直到有人加一个星火更大的姿态之前成立的巧合」。画布对身体中心**对称**——因为 CSS 朝向翻转是整帧 scaleX,非对称画布会绕画布中线翻转,于是**每次转身都把角色平移一段**。
  `身体偏移 2.27px → 0.19px` · `画布 12 种 → 1 种(160x149)`
- **★ 更正(owner 复核抓出):我上报的「腮红跨度 34.5% → 6.5%」是**两个不同总体**之间的比较,不成立。** after 那个数我漏采了 `scratch1/scratch2`(恰好正是尺寸守卫按名字排除的两帧),而 before 那个数的总体里含 `celebrating`/`sleeping`。**改口径前后不可比**——这与本轮我自己抓到的另两次(层叠验证器被注释咬、NEUTER 打空)是同一族错误:量具本身没被审。已从 git 取出 `5c26fdb0^` 的 22 张旧帧,用**同一指标、同一批帧**重测:
  | 总体 | n | BEFORE | AFTER |
  |---|---|---|---|
  | 全部 22 帧 | 21 | 65–113 = **73.8%** | 60–90 = **50.0%** |
  | 同姿态子集(排除两个极限姿态) | 14 | 97–113 = **16.5%** | 77–82 = **6.5%** |
  | 走路循环 | 4 | 103–113 = **9.7%** | 77–82 = **6.5%** |
  (旧的「34.5%」两头都不对应,作废。)
- **★ 而「全部帧仍有 50%」不是残留缺陷,是被忠实保留的美术,这一条必须证明而不是声称:** ①**尺度忠实**——`shipped_bodyW / master_bodyW` 对全部 22 帧都必须等于同一个 S=0.171674:实测 **0.17113..0.17288**,最大偏差 **0.70%**;而 `scratch1 +0.10%` / `scratch2 -0.31%` **比普通帧偏差还小**(普通帧 -0.32%..+0.70%)⇒ 锚点逻辑对它们 0.56 与 1.56 的**非常规长宽比**处理正确,不存在「极限帧被管线搞坏」。②**差异源自母稿**——母稿腮红 idle 479 / scratch1 352(73%)/ scratch2 527(110%),乘 S 后预测 82.2/60.4/90.5,实测出厂 82/60/90(误差 ≤0.5px)。画师就是把 scratch1 画成侧转压缩、scratch2 画成横向压扁的。**50% 这个数衡量的是姿态幅度,不是抖动**;抖动只可能发生在同姿态帧之间,那一档是 16.5% → 6.5%。
- **★ 残余 6.5% 必须证明是美术而不是新管线的锅,否则等于换一个谎:** 全局 scale 下「出厂尺寸 = 母稿尺寸 × 常数」必须成立 —— 实测比值 **0.1711..0.1729**(1.02%,纯整数取整),最大偏差 **0.151 渲染 px**。所以残余就是画师自己的线宽变化,**被忠实保留**。这直接改掉了我第一版守卫的**错误前提**:它断言各帧尺寸**全等**,而那是在断言一件假的事(母稿本身 651..674)。故承重守卫改为**尺度忠实**(`_SCALE_FIDELITY_PX=0.35`)而非尺寸相等,尺寸阈值放宽到覆盖 0.74px 的诚实美术差异。**同族第 N 次:先问「自然样本长什么样」,再写阈值。**
- **★ owner 复核补上的第二个成因(我漏了):脚滑 1.62×。** `WALK_FRAME_MS` 是字面量 ⇒ 步频与**实际位移**解耦,两者静默不一致:美术声称每步 12.28 渲染 px,而 34px/s × 300ms 只走 10.20 px ⇒ 腿比身体快 1.62 倍(「滑行的身体上装了蜂鸟的腿」)。**且一个常量不可能同时服务三种速度**(walk/chase/flee)。改为**推导**:`ms = STRIDE_PX/speed*1000/WALK_DISTINCT`,由**唯一**的 `_advanceGait(dt, speed)` 承载(替掉三份复制粘贴的推进循环);`W.speed` 34 → **41px/s** 让推导值落在 ~75ms(13.3fps,清过 12fps 闪烁地板)。三条腿滑移比现均 **≤1.00×**。
- **★ 转身 pivot 是死代码,而这一条只有查层叠才看得见:** `:12085` 的 squash-hop 在 walk 动画规则**之前**且**同特异性**(各 0,2,0)⇒ 后置的 walk 规则胜出;而朝向翻转**只**发生在 walk/turn/chase 期间 ⇒ 「立定旋身」在**任何可达状态下都从未播放过**。移到 walk 之后并用 `:not(:root)` 提到 (0,4,0)。**刻意不用 data 属性提特异性**:`mount()` 不设初始 `data-state`,而 `_startle()` 可在首次 `_enter()` 之前就 pivot ⇒ 属性式提升会**恰好漏掉用户刚戳的那一次翻转**。无 `!important`。用真实特异性+源序解析验证。
- **★ 我第一版验证器自己被注释咬了:** 用正则直接扫 CSS,把**注释文字**当选择器算进特异性,于是报告「PIVOT STILL DEAD」。改为先走共享 `strip_comments(lang='css')` 后正确。**本项目记录在案的同型错误又一次:凡扫源码,先剥注释。**
- **这一整类此前零守卫:82 个宠物守卫全绿地放行了它** —— 没有任何一条断言「角色在每帧尺寸相同」,而 `--check` 报 OK 是因为它拿帧去比**造成缺陷的那个管线**。新增 8 条 + 4 发 NEUTER(各咬各的:每帧归一化→3 红;墨迹居中→1 红;固定 75ms 配旧 34px/s→2 红;pivot 削弱并前置→2 红),三个产品文件事后 `cmp` 逐字节还原。
- **★ NEUTER 必须模拟真缺陷而不是形式上扰动一个数:** 第一版「只把间隔打回 75ms」**打空**——因为本批正是把速度重钉到 41px/s 使推导值≈75ms,所以 75ms 在当前速度下**是对的**。改为复原**出厂那一对**(75ms + 34px/s)才咬。
- **陈旧套件就地反转:** `test_walk_cycle_clears_the_twelve_fps_floor` 读的是字面量 `WALK_FRAME_MS`——**那个字面量就是 bug**,所以旧断言只能靠「把缺陷放回去」来满足。改为对**推导值**断言同一条性质(不得读作闪烁),严格更强。
- **`walk5..8` 保持诚实的 4 帧重放**,不假装 8 个不同姿态;真中间帧是另一张美术票,由 owner 决定是否开。
- **验收边界:** 纯前端 + 静态资产,**需重启 + bundle 重建**(bundle 为 gitignored,启动自构)。真浏览器未实测;证据为 PIL 逐像素测量 + 真实特异性/源序层叠解析。**相邻红灯经证伪不属本批:** `test_gitignore_covers_export_excludes` 在纯净 HEAD 已红(18 个被报文件与本批改动集**交集为 0**;16 张 raw 母稿来自 `70161357`),按 owner 偏好开票 `pt_045b37a1b05349b7` 而不在本批顺手修 —— 注意母稿**必须留在库**:它们是管线唯一输入,删掉则尺寸恒定性无法重跑。

### 2026-07-31(量具在撒谎:relief 报告释放 10.7GB 而实际释放 0) — 自主派单接 `pt_36e7854ac6094079`;修完「报告值」之后,**我自己的第一版告警又犯了同一族错误——它在真机上结构性打不响,是端到端实测抓的,不是评审抓的**(`pt_36e7854ac6094079` DONE;2 文件;套件 **26 → 34**,**NEUTER×6 各咬各的**(0/4/1/1/2/1);相邻环 **75/75**)

- **★ 先把票面数字复核一遍,而复核把严重性又抬高了一档:** 全量扫 `logs/error.log` 的 406 次 relief —— **usage 下降 38 次、持平 367 次、上升 0 次**;而累计**报告**释放 **4272.1 GB**,累计**实测** usage 下降仅 **18.3 GiB** ⇒ **夸大 234 倍**。`log_pages=57 files/10775.6MB` 那个数字是 fadvise **建议过的文件表观大小之和**(可回收量的上界),被当成「刚释放了这么多」印在日志里。
- **★ 而最关键的一条是:陈旧测试**认证了这个缺陷本身**。** `test_relieve_includes_log_page_drop` 伪造 `drop_logs_cache` 返回 `bytes=12_000_000` 然后断言 `stats['log_pages_bytes'] == 12_000_000` —— 它精确地把「报告表观大小」钉成了契约。**26 条测试全绿地放行了一个每 30 秒撒一次谎的量具**。故按惯例**就地反转**而不是删除:改为 `test_relieve_reports_measured_reclaim_not_apparent_size`(usage 前后相同 ⇒ 报告必须是 0,无论 advise 了多少字节),并补**补集** `..._when_usage_actually_drops`(否则「永远报 0」也能过,等于换一个谎)。
- **三处落点:** ①`relieve_memory` 报告 `RECLAIMED = before.usage - after.usage`(实测差值),日志措辞从 `log_pages=58 files/10774.3MB` 改为 `advised 58 files (0 unchanged/skipped), RECLAIMED 4.8MB`;②`drop_files_cache` 按 `(mtime_ns, size)` 去重,同一文件未变则跳过——实测第二次 relief **58 个文件全部 skipped、0 次 syscall**(此前每 30s 白跑 57 次,且每次都把它们的表观大小重新计入报告);③连续 N 次无效 relief 升级为**一次性** CRITICAL,点明「压力来自 cgroup 外部,本进程无法缓解」。
- **★ 然后是本批真正值得记的一条:我自己的告警在真机上永远打不响,而单元测试全绿。** 判据写的是 `reclaimed <= 0`。端到端跑真 cgroup:回收序列是 `39641088, 344064, 0, 0, 180224, 0, 0, 0` —— **共享 cgroup 的 usage 计数器一直在抖**,每隔几次就有一个 180KB 级的碎屑,严格 `>0` 判据把它当成「relief 有效」**重置计数**,streak 序列 `0,0,1,2,0,1,2,3` 永远够不到阈值 5,`escalated=False`。**这与我刚修完的缺陷是同一族:一个无法报告它所监控的状况的仪器。** 改为**显著性**判据(`TOFU_CGROUP_MATERIAL_PCT` 默认 0.1% ≈ 220GiB 上的 225MB,刚好高于 `%.1f%%` 会四舍五入抹掉的 0.05%);复验后 streak `0,1,2,...,7`、**escalated=True**。补 `test_noise_sized_reclaim_does_not_rearm_the_escalation` 钉住它,NEUTER-6 把判据退回 `<=0` → **精确只咬这一条**。
- **NEUTER×6 各咬各的:** ①票面指定的「fadvise 改 no-op」→ **0 红**(单元测试 mock 掉了 pressure,这一发对单元层结构上不敏感,故改用②直击性质);②`reclaimed` 退回 apparent size(复现原始缺陷)→ **4 红**;③关闭去重 → 1 红;④升级去掉一次性闩锁(每次都发=噪声化)→ 1 红;⑤永不累计(复现「无限重复同一 WARNING」)→ 2 红;⑥判据退回严格 `<=0` → 1 红。每发之后 `cmp` 逐字节还原。
- **消费者兼容性:** `lib/motion_video/engine.py:_drop_page_cache` 用 `stats['files']/stats['bytes']`,两键均保留(新增 `skipped` 是加法);且它 advise 的是**刚渲染完的新文件**,mtime 必变,永不被去重跳过。
- **能力边界写进 docstring 存档,避免下一个人重新论证:** relief 只能释放**本进程拥有的**字节(堆缓存 + 自己日志的 page cache);12037 个 journal 采样显示真实构成是 kmem 45–126 GiB + cache 55–156 GiB,而 tofu 自己 RSS 仅 0.16–9.9 GiB ⇒ 大头是兄弟进程与 FUSE slab,**结构上够不着**。所以 relief 是尽力而为的礼节、**不是缓解手段**,日志不得暗示相反。
- **验收边界:** 后端改动,**需重启生效**(当前活进程加载的是旧模块);已用真 cgroup + 真日志文件端到端验证两次 relief 与 CRITICAL 升级。

### 2026-07-31(一轮生成两个 Agent 气泡) — owner 报「第一个气泡生成中途停住,又新开一个继续」;定案落在**「非终态快照广播终态字段」**,而**判据第一步就把「后端重复写入」整类排除掉了**(commit `f9d3ef84`,5 文件 +565/-20;新套件 **7/7 失败先行**,**NEUTER×4 各咬各的、各一条**;A/B 失败集 diff **零新坏零修好**)

- **★ 第一刀先证伪最省事的那个假设,后面所有取舍都靠它:** 拉 conv `ms8c0645hwl327` 的 DB 原始记录 —— assistant 消息**恰好 1 条**(`msg_count=2`,1 user + 1 assistant),而屏幕上**两个**气泡 ⇒ **这是渲染身份违约,不是后端重复写入**。这一条直接判掉了「去持久层加去重」这类修法:数据本来就只有一条。
- **★ 而这台部署上「偶发」其实是 100%,量出来才知道:** 当天 3 个任务**全部**记录 `SSE ... DISCONNECTED PREMATURELY — 0 events sent in 0.1s`,即**全部流量跑在 1Hz poll 兜底上**(access.log:**925 次 poll vs 5 次 send**,`/chat/stream` 命中 **0**)。所以这个 bug 的触发面不是「网络抖动时偶尔」,而是**这台机器的常态路径**。
- **★ 根因是一对互相矛盾的字段,而它的窗口宽度是可以数出来的:** `_finalize.py:843` 打 `task['finishReason']`,`status='done'` 要等到 `:954` —— **中间 111 行**,且含 `_finalize_dangling_tool_rounds` / 压缩用量折叠 / **`_generate_tool_summary`(一次阻塞 LLM 调用)**。而 `chat_poll` 的字段拷贝只有 `if task.get(key)`、**没有 status 闸** ⇒ 落在窗口内的 poll 返回 **`{status:'running', finishReason:'stop'}`**。窗口是**秒级**而非微秒级,这正是 1Hz poll 能反复采到它的原因 —— 「有没有 race」和「race 有多宽」是两个问题,只答前者会低估。
- **前端那一环:陈旧终态字段压过身份。** `assistantTailIsPriorTurn` 把 `!!finishReason` 一律判成「上一轮」,**即使该 tail 正绑在当前 task 上**。`_pollFallback` 把矛盾字段拷到活跃消息 → 下一次 `connectToTask` 把**本任务自己的活跃气泡**判成别人的完成轮 → push 新 `_msgId` 占位符 → delta 全部转投新气泡,**原气泡再也没有写者(冻结在半句话)**,下一次重绘同时画出两个。
- **★ 旧真理表不只是过期,它和自己的调用点互相矛盾 —— 这比「断言写错」重一档:** `sse_pipeline.js:258` 先按身份解析槽位,注释明写「Matching by identity … makes a second bubble for the same task **structurally impossible**」;而旧 reducer 紧接着**把这个解析丢掉**。也就是说**代码读起来是安全的**(和 7-30 那条假注释同族),测试还在为矛盾的那一半背书(`finishreason_same_task_is_prior === true`)。
- **双层修法是刻意的,不是保险起见:** ①源头不再铸造矛盾(终态字段按**REPORTED status**设闸 —— 用真正发出去的那个值,不用另读一次 `task['status']`,否则两者仍可能不一致);②即使别的写者再铸造一次也不再承重(身份优先)。第二层有具体理由:`sse_pipeline.js:884` 的 `state` 处理器**同样**会把 `ev.finishReason` 拷到活跃消息 —— 只修 poll 会留下同形第二条路。
- **★ 保留 `!!finishReason` 那一臂,是因为「更彻底」在这里恰好是错的:** 想让第一条测试变绿,最省事的是把该臂整条删掉。但 `_taskId` **不入库**,DB 载入的完成轮没有它 ⇒ 删掉会让「刷新后新任务把上一轮重播进新气泡」复活(Scenario D)。故收敛为**narrowing 而非 removal**,并补一发反方向 NEUTER 专钉这一点。
- **★ 自查抓出我自己第一版守卫是空的:** poll 那两条最初写成**源码文本断言**(`'_TERMINAL_ONLY_KEYS' in src`)。NEUTER 时发现:**把真网关整段删掉,测试依然全绿** —— 因为常量还在。改为**行为断言**:真起 server、真打 `/api/v1/chat/poll/<id>`、断言响应体。改完后 NEUTER 才咬。**判据:凡断言「后端不会发某字段」,唯一可信的量具是响应体本身,常量名和注释都能满足源码断言。**
- **NEUTER×4 各咬各的、各一条:** ①摘掉 reducer 身份臂(复现原缺陷)→ 只有 reducer 那条红;②删内存分支闸 → 只有 `withholds_finish_reason_while_running` 红;③删 DB 分支闸 → 只有 DB 那条红;④**过宽闸**(`_terminal_ok = False`,永远不发终态字段)→ 只有**补集**那条红(done 任务必须照发 finishReason/usage,否则轮次永不落定、finish bar 永久空白 —— 那会比原 bug 更糟)。四发后两个产品文件 `cmp` 逐字节还原。
- **★ 陈旧套件就地反转,不删;而其中一条的旧 docstring 自己承认了问题:** `test_frontend_connecttotask_taskid_dedupe.py` Scenario A 旧契约断言「push 一个空占位符」,docstring 辩解它「stays EMPTY」所以无害。**并非无害** —— 完成回答后追加一个空 assistant 气泡,本身就是屏幕上多出来的气泡。新契约:**复用本 task 自己的槽位、不追加**(实测 A/B/D:`false / true / true`)。
- **★ 回归判据用失败集 diff,不用票面数字 —— 这次尤其必要:** 首轮 131 套跑出 **21 红**,而纯净 HEAD 副本上同样 9 套是 **0 红**,看起来像我砸了一片。实测原因:**工作树有 60 个改动文件,其中 55 个是兄弟会话的**,且部分失败套件本身就是兄弟**未提交的新文件**(`??`,纯净 HEAD 里根本不存在,所以基线为 0 是取样假象)。于是把**我的 5 个文件嫁接到 `git archive HEAD` 副本**,与基线跑**同一组 127 套**:**零新坏、零修好**,失败集逐条相同(12 条基线红全部来自兄弟 WIP 与环境)。**「我的树 vs 纯净 HEAD」不是有效对照,唯一有效的是「纯净 HEAD vs 纯净 HEAD+我的文件」。**
- **共享树纪律:** 提交前索引里已有兄弟的东西,故 `git add -- <5 个路径>` + 计数门(`-eq 5`)+ `git commit -F msg -- <5 个路径>`(pathspec 形式);事后核对我的 5 个文件无残留 dirty、兄弟 53 个未提交文件一个不少。
- **验收边界:** 后端已生效;**前端需重启 + bundle 重建**(bundle 为 gitignored,服务器启动自构)。真浏览器未实测 —— 证据为**真 HTTP 往返**(poll 端点)+ node 驱动 **shipped** reducer。

### 2026-07-31(GLIBCXX 启动崩溃:三张票、两次诊断被实测证伪,最后交付的是**取证**而不是修复) — owner 连续两轮用实测顶回我的因果断言;真正的产出是「把不可回溯变成可诊断」的一行,以及一份**排除清单**(`pt_cc918d3e44554489` 认领中;server.py +66;新套件 **5/5**,**NEUTER×3 各咬各的**(5/1/2);相邻环 **29/29**)

- **★ 本批最该记的不是结论,是我错了两次的形状 —— 两次都是「手里已有证伪证据却照旧写进票」:**
  | 票 | 我的诊断 | 证伪它的实测 |
  |---|---|---|
  | `pt_6f61d968a438476f` | server 继承 `LD_LIBRARY_PATH=/lib64:...` 遮蔽 conda libstdc++ | 我自己的复现 **3/3 全过**。`readelf -d` 实测 `lxml/etree*.so` 与 `bin/python3.12` 都带 **RPATH**(非 RUNPATH),而搜索序 **RPATH > LD_LIBRARY_PATH** ⇒ `/lib64` 排首位也永远赢不了。**处方(前置 conda lib 进 LD_LIBRARY_PATH + re-exec)是空操作**:改一个 RPATH 已压制的变量,会关票而 bug 还活着 |
  | `pt_68afc413b948401b` | soname 抢占,触发源=平台注入的 dolphinfs preload | 该 preload 配真实 LD_LIBRARY_PATH **10/10 全过零崩溃**,soname 赢家仍是 conda 6.0.34 |
- **★ 「谁先映射 soname 谁占有」这句话本身就过强,而这是 owner 顶回来的第二刀:** 实测 `ctypes.CDLL('/lib64/libstdc++.so.6', RTLD_GLOBAL)` 之后再 import trafilatura → **OK**,maps 里 conda 6.0.34 与 /usr/lib64 版**同时存在**;经 NEEDED 链拖入(`CDLL(libjvm.so)`,其 ldd 明确指向 /lib64)→ 同样 **OK**。**ld.so 允许两份 libstdc++ 并存**;该性质**只对显式 LD_PRELOAD 成立**。所以「加载顺序」这个说法对 dlopen/NEEDED 是假的。
- **已实测排除的候选触发源(这是本批最贵的产出,防止下一个人重走):** ①平台 preload(`/etc/profile.d/pc_env.sh:15` 无条件 export)10/10 干净 ②RTLD_GLOBAL dlopen ③NEEDED 链(libjvm) ④`/etc/ld.so.preload` **不存在** ⑤`restart_15000.sh` 里 `grep -c LD_PRELOAD` = **0** ⑥模拟 libicuuc 的 RPATH 命中失败(复制到无 libstdc++ 的目录)确实产生 GLIBCXX 错误,但**报错主语不同**(`./libicuuc.so.75.1:` vs 生产的 `/lib64/libstdc++.so.6:`)⇒ 排除。
- **唯一能复现的形状:** `LD_PRELOAD=/lib64/libstdc++.so.6 python -c "import trafilatura"` —— **10/10 崩,对照组 10/10 过**;且其错误串与生产**逐字节相同**(`cmp` 两份 219 字节文件 IDENTICAL)。据此可定形态:报错主语是 `/lib64/libstdc++.so.6` 而 `required by` 是 conda 下的 libicuuc ⇒ **libicuuc 自身 RPATH 解析正常**,坏的只是 libstdc++ 这一个 soname 的归属。
- **★ 而当前环境里没有任何东西这么做 ⇒ 触发源至今未知。** 崩溃进程 3459833 已消失,`ImportError` 属正常退出**不产 core**(`core_pattern` 虽配置也取不到)⇒ **环境不可回溯**。故 owner 拍板:**先取证,不修复**——re-exec 与 preload 加固都押后,等拿到真实触发源的证据再谈。
- **交付物就是一行 stderr**(与既有 `[boot]` 行同通道,watchdog 重定向进 `server_15000.log`):记录 `/proc/self/maps` 里 libstdc++ 的解析路径 + `LD_PRELOAD` + `LD_LIBRARY_PATH`。实测现状 `grep -c 'LD_PRELOAD|LD_LIBRARY_PATH' server_15000.log` = **0**,即下次复发仍将从同一个起跑线重查。
- **★ 而我写的第一版诊断行有一个真缺陷,是新守卫抓的、不是评审抓的:** 无任何 preload 时(裸部署)libstdc++ **此刻尚未映射**,那一版会记录 `not-yet-mapped` —— **在最需要它的那类部署上恰好是空的**(生产恒有平台 preload 所以看不出来,我最初只测了生产形态)。修法不是放宽测试,而是让未绑定时**主动用 ctypes 探测 loader 会选哪一份**,输出区分 `mapped=` / `would-resolve=` 两态(「还没人认领」与「这一份拥有它」是两个不同的事实,不能压平)。三形态实测均有值:生产=`mapped=<conda>`、裸部署=`would-resolve=<conda>`、崩溃形态=`mapped=/usr/lib64`。
- **顺序是这条诊断的全部意义,故单列一条守卫:** 实测崩溃形态下诊断行在**第 3 行**、ImportError 在**第 28 行**。NEUTER-2 把诊断块移到 `search_bridge` import 之后 ⇒ **精确只咬 ordering 那条**(其余 3 条照绿),这正是「重构把它挪下去而其他断言全绿」的失明场景。
- **NEUTER×3 各咬各的:** ①删掉整个诊断块 → **5 条全红**(复现原始盲点);②挪到重依赖 import 之后 → ordering + 判别 2 条红;③撤销探测回退退回 `not-yet-mapped` → 判别 + 新增的「不得退化为占位符」2 条红。三发后 `server.py` 均 `cmp` 逐字节还原。
- **顺带更正两条被我夸大的事实:** `server_15000.log` 里 2 条 GLIBCXX 是**同一次崩溃打印两遍**(stderr + CRITICAL handler,时间戳去重后仅 `2026-07-31 10:33:27` **一个**);2026-07-28 14:39–14:44 的 4 次连续 DIED **与本 bug 无关**(该窗口 `grep -c GLIBCXX`=**0**,全是 98–99.5% 的 cgroup OOM 挤压)⇒ 真实影响是**单次**启动崩溃,不是重启风暴。
- **同批日志普查的其余结论(各自开票,不混入本批):** ①`pt_36e7854ac6094079` cgroup relief **量具在撒谎**——341 次触发、usage 92.1%→99.9% 从未下降,而日志每次都报 `log_pages=57 files/10775.6MB`(那是 **apparent size 之和**,不是回收量);干净对照证明 fadvise 机制本身是好的(读进 0.06GiB → cache +0.06,fadvise → -0.06),只是瞄错目标:真正大头是 kmem 45–126GiB + 共享 cgroup 的兄弟进程,tofu 自己 RSS 仅 0.16–9.9GiB。②9.1GB 的 `app.log.2026-07-27` 是一个失控 swarm agent(`Round 12301341/∞`,0.0s/轮),**53.36M/53.79M 行 = 99.2%**;断路器已在事后落地。③SSE `DISCONNECTED PREMATURELY` 实测 3/3 都是 `0 events in 0.1s` 且都有 `Falling back to polling`、任务最终 `closed after done` ⇒ 真降级但无数据丢失,归既有传输 epic。
- **共享 HEAD 纪律:** 提交前 `git status` 显示 7 个兄弟会话的未提交文件;`git diff --stat -- server.py` 确认该文件**只有我的 66 行插入、单一 hunk**,故按 pathspec 提交 `server.py` + 新测试 + JOURNAL 三个路径。写 JOURNAL 时被新鲜度闸拦下(兄弟刚提交了发布批次),重读后把本条插在其上方。
- **验收边界:** 诊断行**需重启才生效**(当前活进程 3459968 启动于修改之前);真实触发源仍未知,本批**不宣称已诊断**。

### 2026-07-31(守卫校验的文件不是真正在跑的那个文件) — owner 问「exe 是不是最新版、发布流程自动吗」;三次发版全失败而 **32 条发布守卫全绿**,因为它们读的是本地文件、GitHub 跑的是另一份;而那份差异**是我们自己的导出流程造成的**(commit `a4320287`,3 文件 +391;新守卫 **9 条**(4 条失败先行),**NEUTER×5 各咬各的**,相邻环 **122/122**;**未推送**——分叉清点待 owner 拍板)

- **★ owner 点出的那一层比我诊断的更深,而它解释了「为什么没人发现」:** `tests/test_desktop_build_workflow.py` 的 `_WORKFLOW = _ROOT / '.github/workflows/build-desktop.yml'` 读**本地**文件。本地从 7-29 起就是对的 ⇒ 30+ 条守卫**结构上永远绿**;而 GitHub 实际执行的是 7-23 的旧副本(`on: push tags: v*` + 已退役的 `macos-13`)。**根因类别不是「标签过期」,是「本地真源与已部署产物之间没有任何一致性检查」。**
- **★ 而那份旧副本不是「忘了推」,是被我们**强推覆盖**的——这是本批最值得记的一条:** commit `128ad422`(「fix Intel leg — macos-13 → macos-15-intel」)是**直接在下游仓库**写的,它让 v0.14.2 成功发版,但从未回流。实测两条独立证据:①`git merge-base --is-ancestor 128ad422 HEAD` 判否;②`git fetch` 逐字打印 `+ 128ad422...59bc8254 main -> origin/main (forced update)`。`export.py` 以 re-`git init` + 非 ff 即 `--force` 的方式发布 ⇒ **下游独有的修改不是「丢失」而是「被回滚」,且全程零日志零告警**。三次发版(v0.15.0/0.15.1/0.15.2)因此各自饿死在同一个退役标签上:x86_64 腿排队**恰好 24h** 被自动取消,`release` 因 `needs:` 被 skip。
- **★ 第二个发布远端此前没人提过:** `_GIT_REPOS['opensource']` 配了**两个** remote——`rangehow/ToFu` 与 `NiuTrans/ToFu`,实测**两者都停在同一个陈旧 sha `59bc8254`**。只覆盖第一个的守卫会在半个机队陈旧时报告「干净」,故新守卫对两个远端各跑一遍。
- **落点 `tests/test_published_pipeline_drift.py`:** 拉已发布副本与本地**逐字节**对拍(`.github/workflows/{build-desktop,ci}.yml` + `scripts/release_assets.py`)。
  - **为什么敢用「逐字节」而不是解析比字段:** 因为**实测**过前提——用 export.py 真实的 `_sanitize_source_opensource` 跑这三个文件,输出与输入**完全相同**(三个 True)。故这是真不变量而非近似。且更弱的比较会重新招回同一个 bug:**某个没人想到要比的字段**。
  - **配套钉住这个前提**(`test_guarded_paths_survive_the_export_sanitizer`):若将来某个受保护文件开始被净化改写,漂移守卫就会因**正当理由**长红 ⇒ 被静音 ⇒ 套件失明。所以把前提做成独立断言,让失败信息指名真因,而不是让人去猜。
  - **离线 SKIP 而非 PASS:** 证据在网络上,网络不可达意味着**没检查**;把「没检查」报告成「一致」正是本模块要消灭的那种谎言。
- **NEUTER×5 各咬各的:** ①对**当前绿**的 `ci.yml` 改一个字符 → 它那两条(两个远端)红,还原后 sha 回到 `3bbef34f`;②注入一个会被净化的字面量 → **只有**前提测试红;③`TOFU_SKIP_NETWORK_TESTS=1` 与真死代理(`127.0.0.1:9`)两种方式 → **6 skipped,零 pass**;④撤销 spec 那一行 → 2 红;⑤dest 改成 `'.'` → **只有**落点测试红。
- **★ 第一发 NEUTER 打空,而正确反应是先查变异有效性再下结论:** 我最初把 `PYTHON_VERSION` 换成 `PYTHON_VERSIOX`,守卫不咬。查 `grep -c` 实测 `ci.yml` 里该 token **出现 0 次** ⇒ 文件被原样写回,**变异本身是 no-op**,不是守卫失明。换成真实存在的 `name: CI` 后精确咬红。**判据:NEUTER 空转时,先证明变异真的改了字节(比 sha),再谈守卫是否承重。**
- **`tofu.spec` 的真缺陷(随本版发):** `datas` 里 `browser_extension` 零命中,而 `routes/browser.py:171` 是**请求时**现场 walk `BASE_DIR/browser_extension` 打包、目录不存在即 404;`routes/api_v1/browser.py:91` 同源读出 `extensionPath`。⇒ **桌面版恰恰是那个「用户拿不到扩展」的发行版**,而它的用户最没有「clone 仓库加载文件夹」这条退路。守卫断言的是**行为**不是字面量:执行 spec 真实的 datas 构造逻辑(含它自己的 `os.path.exists` 过滤——正是这个过滤让遗漏变静默)并检查存活条目,同时钉住两个 handler 的路径推导,因为**包对了而 handler 算出别的路径同样是 404**。
- **分叉清点(owner 指定的发版前置,66 个远端独有文件,零未分类):** promo/ 构建产物 26、`static/icons/pet/oneko/` 19(已被 7-30 宠物重设计取代)、egg-info 5、uploads fixtures 4、**tests/ 7 个(逐个查到上游有意删除的 commit:`aa6f7ea6`/`38ec59b6`×2/`364a7f4a`/`62f685b7`×2/`5892fdb3`)**、`write_tools.py`(本地已重构为**包**:`__init__/_ops/_paths/_text`)、`scheduler.js`(被 `lib/scheduler/*` 取代)、两个 provider 模板(`meituan_claude_code.json` 由 `22e6003a` 合并;`openai.json` 由 `bootstrap.py:176` 的代码内目录取代)、`data/config/.gitkeep`。**结论:没有一个是需要回流的下游独有修改** —— 唯一那个(`128ad422`)的内容**已被本地 7-29 那批以更完整的形式覆盖**(实测远端独有的 27 行非注释内容全部被本地版取代)。
- **Chrome 那一问已定性关闭(不是「难」是「不允许」),开票 `pt_d30b63f98bfb4f70`:** Windows/macOS 外部安装的 `update_url` **必须指向应用商店**、本地 CRX **仅 Linux**;非商店 force_install **要求 AD 域**;`--load-extension` 在 **Chrome 137 已移除**(139 又移除 `--disable-extensions-except`)⇒ **要管理员权限也绕不过**。唯一出路是上架,前置两项本仓已记录的真缺陷:权限 17→10、`manifest.store.json` 漏 `downloads`(而 `background.js:1966` 真在调用)。
- **验收边界:** 漂移守卫的 4 条红是**当前真实状态**,推送后自动转绿——它们现在就是那份「待发布」的量具。**本批未推送**,按 owner 指令等分叉清单签字。共享 HEAD 有兄弟 7 个文件 WIP,已用精确 pathspec + 计数门(=3)提交,事后核对兄弟改动一个不少。

### 2026-07-30(续·收口收出两条从来没人钉过的性质) — charter #24 最后 5 个手写剥离器迁完;但**本批真正的产出不是迁移,而是两发 NEUTER 打空**——它们打空的那一刻证明了这两个守卫赖以成立的性质**根本没有测试**(`pt_6d03c27e2feb4777` DONE;commit `1a759db4`,5 文件 +171/-148;5 套 **27/27**;**NEUTER×4 各咬各的、各一条**;全消费者环 **585/585**)

- **★ 票面把 6 个文件按「为什么不能盲迁」分了三组,而三组的答案都得靠实测,不能靠推理:**
  | 组 | 票面担心 | 实测结论 |
  |---|---|---|
  | ① `strings=True` 新能力 | 共享模块没有该能力 | 前置已在 `1ee9879f` 落地;迁后真文件**存活标识符集完全相同**(两向各 0 个独有) |
  | ② 行号语义承重 | 只看行数相等不够 | 共享版是**纠错**不是平迁:`chat_render.js` 本地 2093/2097 行、**64 处错位** → 共享 **0**;`main.js` **934 处** → 0;`conversations.js` **813 处** → 0 |
  | ③ 本地 tokenizer 可能更强 | 更弱则应保留本地 | **不更弱**:`api.js` 两边同为 1680 行、标识符集全等;而 `boot_early_restore` 的本地正则**反而更差**——它**删行**(1519→1162) |
- **★ 本批的真发现:两发 NEUTER 对**未改动**的套件打空。** 打空不是「没问题」,是「这条性质没人钉」:
  - **`strings=True` 是死重量。** 实测 `stream_reducer.js` 里 13 个 FORBIDDEN 符号,**即使保留字面量内容也一个都不出现**(全 0)⇒ 生产扫描两半都没走到。而既有那条 NEUTER 注入的 `twUpdate` 是**代码**,任何剥离器都抓得到,**区分不了**字面量剥离器与纯注释剥离器。补 `test_NC_forbidden_word_inside_a_string_is_not_a_violation`:把禁用词放在**合法**位置(注释 / 报错文案 / 模板串)必须保持沉默;同时钉补集——同样的词**作为代码**必须照抓,否则等于奖励「把一切都删掉」的剥离器。
  - **行号上报没人钉。** 补 `test_reported_line_number_points_at_the_real_source_line`。
- **★ 而写这条测试时我顺手改掉了自己上一版 docstring 里的一句错话——值得单独记:** 我原先写「行号在这里 load-bearing」。**不对。** `_enclosing_span` 是**从剥离后的文本自身**重新推导函数跨度的,所以剥离器删行时它**依然自洽,判定不变**。坏掉的是**上报位置**:构造用例里 bare read 在源码第 9 行,删行剥离器报**第 5 行**并引用**错误的源码文本**——把读者送到无关代码上。**"判定错"和"定位错"是两种损失,不能混为一谈**,守卫的 docstring 尤其不能替代码吹牛。
- **NEUTER×4 各咬各的、各一条:** 强制 `strings=False` → **只有**新的字面量 NEUTER 红;把「空白行」改成「删行」→ **只有**新的行号测试红;`api_contract` 退回朴素双正则(即它 docstring 记录的那次事故:`// … /api/paper/*` 里的 `/*` 吃掉 paper 域的 `upload:`)→ `test_api_call_sites_are_defined_paired` 红。
- **★ 第四发第一次打空,而正确反应是加强 NEUTER 而不是宣布「不承重」:** `boot_early_restore` 的剥离器改成 pass-through,**源码不动时不咬**。于是按**守卫自己声明的前提**加压:删掉可执行语句 `_target = conversations[0].id;`、只留一句提到它的注释——**剥离器开着 → 红(正确),剥离器废掉 → 绿(失明)**。这才是那条 docstring 承诺的性质。事后 `static/js/main.js` 与 HEAD **逐字节一致**。
- **回归:** 54 个 `_source_scan` 消费者 **585/585**(上午那 21 个宠物红灯已随兄弟会话提交消失)。共享树:`test_events_round_key_unified.py` 是兄弟未提交的 WIP(未领的 `pt_174f89ef`),按 pathspec 提交,提交后确认它**仍在工作区未动**。

### 2026-07-30(宠物机制整层翻新:气泡跟随 + 正面朝向 + AI 形象) — owner 三连报「气泡不跟着宠物走 / 太丑 / 倒着走(脸朝左身往右)」。三件各自的根因都不是表面那一层,而「丑」的处方推翻了五个月前的架构判据(`pt_0149983835484650` DONE;四套宠物守卫 **82/82** + 相邻环 **111/111**(api_isolation+scene×3 / i18n_boot+scene_perf+p0);pixel 级 NEUTER×5 各咬各的)

- **★ 气泡不跟随:位置在弹出瞬间定格一次,然后宠物被惊跑。** `_showBubble` 只在 show 时算一次 `left = W.x + petW/2`;而同一个 `interact()` 会 `_startle()` 让宠物以 120px/s 逃窜 ⇒ 气泡滞留在空草地上 4.2s。修法定点:`_place()` 每帧 `_positionBubble()`。新守卫驱动真模块 rAF 泵 14 tick 断言 `|bubbleLeft-(x+15)| < 0.6`,NEUTER 摘掉重锚定调用 → drift > 5px 精确咬红。
- **★ 「倒着走」是一太阳不变量自己造成的,且结构上不可修。** 旧等距立方体的三面烙了一个太阳,所以转身只允许镜 char 子组(脸+脚),身体永远在 world 空间——**身体的 3/4 朝向因此被冻死**:朝固定一个方向,半时间与移动方向相反。「一太阳」与「朝向一致」对 3/4 身体**互斥**。五轮前批都在 char 空间内修,修不动这个根。处方:换成**正面朝向**角色(柔和对称光照、无烙太阳)⇒ 整帧镜像既安全(没有太阳可拖)又完整(身体/视线/步态永远同行)。镜像落在 `.tofu-pet-img > img`(**子元素**而非动画层——animation 永远压过同元素上的静态 transform,这是旧 SVG 把 flip 藏进 sprite 的同一理由)。
- **★ 形象是图像模型产的,但一致性来自管线不是来自运气:** hero 一张 + 15 张 img2img 派生(锁定构图/调色/描边),`process_ai_frames.py` 统一 chroma-key(greenness 软坡 + despill)→ 裁 alpha bbox(零 padding,CSS `object-fit:contain` + `object-position:bottom` 让每帧脚底共线而高矮对比保留)→ ≤160px。22 个引擎槽位:walk5..8 重放 walk1..4,groom1/3 是 groom2 的 ±6° 旋转。**raw 1024² 母稿住进 `_candidates/ai/`——复用既有导出豁免段,零 export.py 改动**(顺带在导出守卫里把这条断言钉死)。
- **★ 像素守卫按实测几何设计,不是照抄旧阈值:** 足部分析(底部 <55% 行宽区):contact 帧双脚分开(span 134)而 passing 帧收拢(span 74/84);walk1→walk3 脚对质心右移 6px ⇒ 交替领先可在像素上断言(>3px 阈值)。品牌血统从「hex 集合 ⊆ logo」换成**颜色族占比**(奶油 84-91% / 墨线 5-11% / 腮红 2.4-3.3% / 绿残留 0.00%),阈值取实测值一半以内;NEUTER 把 idle 的 R/B 通道互换(奶油→蓝)→ cream 占比崩穿阈值精确咬红。
- **★ 我自己写的第一版裁切守卫前提是错的:** bbox 裁剪后描边**必然**触边(头顶/脚底各几点)——「触边即裁切」对裁剪后美术恒真,守卫恒红。真正的判据是**亮色填充触边**(被拦腰切断的形状在切口处露出无描边覆盖的填充色):实测全集仅 celebrating 顶部有 5px 星火尖,阈值 8px。同族判据第 N 次:**先问「我测的形状在自然样本上长什么样」,再写阈值**。
- **★ 漂移闸改为解码后像素比较,NEUTER 抓出追加字节对它无效:** 旧闸比文本字节;PNG 闸比解码像素(对重压缩免误报)。于是 NEUTER 第一版(文件尾追加垃圾字节)**不咬**——PIL 解码忽略尾随字节。NEUTER 改成真涂一个像素 → 咬。**判据:NEUTER 必须模拟它要防的那类编辑(手改像素),而不是形式上最省事的那种扰动。**
- **★ 豁免 ratchet 顺手收紧一格:** 帧加载从 `fetch(svg 文本)` 改为 `new Image()` ⇒ tofu-pet.js 的 variable-fetch 豁免条目变陈旧,按「只降不升」删除——今后该文件任何 variable fetch 直接红。
- **陈旧套件就地反转(先例:Goals 批):** light_direction 14 条里 11 条钉的是 world/char 两空间机制 ⇒ 整文件按新契约重写(整帧镜像 / 像素步态 / 12fps / 管线闸 + NEUTER×3);brand_lineage 的 SVG 专用断言(wellformed/viewbox/hex 血统)换成 PIL 版,导出/git-tracked/i18n/switcher 各条保留。**那批「兄弟未提交 pet 资产导致 21 红」的旧红随套件重写整体消解**——前提(旧 SVG 机制)已不存在。
- **验收边界:** 纯前端 + 静态资产,**运行进程不带,需重启 + bundle 已重建**(bundle 为 gitignored,服务器启动自构)。真浏览器未实测(node 驱动真模块 rAF 泵 + playwright 30px 实尺寸渲染验证翻转/object-fit/脚线)。

### 2026-07-30(续·守卫瞎了眼却一直报绿) — `test_frontend_api_isolation` 那条红灯的 ①② 早已在 `b35e939a` 落地,本轮补的是**它赖以成立的那条性质从来没有守卫**:共享剥离器只钉过字符串里的 `/*`,**从没钉过 `//`**——而真正咬人的是后者(`pt_996d4a9a31e24af7` DONE;commit `1ee9879f`,2 文件 +186/-14;primitives **22/22**,**NEUTER 精确单咬**;45 个消费者失败集 diff **零新坏、2 项修好**)

- **★ 先证伪票面,再动手:** 票面说该守卫「在纯净 HEAD 上已红」。实测 `git archive HEAD` 隔离副本:**5 passed**。查 log,①② 已由 `b35e939a` 修掉——红灯根因既不是 `api.js` 真变更也不是基线过期,而是**新文件 `tofu-pet.js` 落地时没登记自己的静态雪碧图豁免**;同一 commit 顺手把本地那份坏 `_strip_comments` 折进共享模块。我独立重跑 NEUTER×4,四条全复现。
- **★ 于是本轮的真问题变成:修好了,但没有任何测试钉住「修好」这件事。** primitives 套件的 face 6 只探了字符串里的 `/*`,**`//` 那一半是空的**——而 `//` 才是实际造成损失的那个 marker。`static/js/api.js` 这行真代码:
  `if (path.startsWith('http://') || path.startsWith('https://')) return path;`
  经旧正则后变成 `if (path.startsWith('http:` ——**从第一个 `//` 起整行被删,在扫描开始之前**。
- **★ 为什么值得单独一条测试而不是塞进参数化表:** 这个失败**静默且单向**。代码被提前吃掉**不可能**让「必须不存在」类断言变红,所以表面上什么都没坏——守卫只是从此看不见文件的一部分,并**继续报绿**。全库实测 171 个 JS 文件里 **27 个**在两种剥离器下不同,且**每一个都是共享版保住了正则版吃掉的代码**(零例反向),所以这 27 个文件的守卫全部**正是**压在这条性质上。
- **补的是双向,不是「永不剥 `//`」:** 同时钉住补集——同一行里**既有** `'https://x'` 字面量**又有**真尾注释时,真注释必须照剥。只钉一半会把守卫从「瞎」换成「聋」。
- **NEUTER 精确单咬:** 把共享 js 路径退回 `re.sub(r'//[^\n]*','',...)` → **只有新测试**变红(22→21);还原 → 22/22。另在隔离副本上复跑 ①② 的四条:carve-out 文件多一个**真** variable fetch → 红;`fetch(` 只出现在注释里 → 绿;删掉 carve-out 条目 → 红;**N4a/N4b 是决定性的一对**——同一行 `'https://x'; fetch(u);`,共享剥离器**红**(代码存活、被看见),坏正则**绿**(代码被吃掉、守卫失明)。
- **回归判据用失败集 diff,不用票面数字:** 把这 2 个文件嫁接到纯净 HEAD 副本,与基线跑**同一组** 45 个 `_source_scan` 消费者:**零新坏**,**2 项修好**(`strings=True` 与 trailing-newline 两条测试正由这一对补齐)。62 个 collection error **两棵树完全相同**——是我只抽取 `tests/static/lib/routes`(缺 `chromium_env`)的**取样假象**,不是回归。
- **★ 21 个宠物守卫红灯不是我的,用 A/B 证的:** 工作树里 `test_frontend_pet_light_direction`(15)+`test_frontend_pet_brand_lineage`(6)全红。纯净 HEAD 副本**加我的改动**与**不加**:**5 failed / 32 passed,逐条同一份名单**⇒ 我的改动贡献**恰好 0** 个。余下 16 个来自兄弟会话未提交的 pet SVG / i18n;那 5 个属于未领的 `pt_0149983835484650`。**按 owner 的偏好,不在本批里顺手修**。
- **共享树纪律(索引里已有别人的东西):** 提交前 `git diff --cached` 显示兄弟会话**已暂存 40 个** pet 资产文件——直接 `git commit` 会把它们一起卷走。故 `git add -- <2 个路径>` + `test $(... | wc -l) -eq 2` 计数门,再用 **`git commit -F msg -- <路径>`**(pathspec 形式,绕过索引)落地;事后核对兄弟的 40 个**仍在暂存区、一个不少**。

### 2026-07-30(续·人类改不动 agent 真正读的那一行) — owner 抓出章程仍在广播被推翻的规则:**正文是对的,而摘要——注入唯一渲染的那一行——永远停在旧设计上**;根因不是笔误,是 `update_decision` **压根没有 `summary` 参数**(commit `f8e8e7d2`,7 文件 +338/-15;守卫 **50/50**(3 条失败先行),**NEUTER×6 各咬各的**(4/3/2/2/1/1);相邻环 **160/160**;活库已改正)

- **★ owner 的判据(比「摘要写错了」重一档):** 决策 #0 的**正文**描述的是已上线的 A 设计,**摘要**却还在说「goal 提升进 charter.content」——正是那个设计取代掉的东西。而 `_decision_headline` **优先取 summary**,每轮注入**只渲染这一行**(正文要多一次 `project_charter_read`)⇒ **每个兄弟会话读到的仍是被推翻的规则**,包括我自己这一轮的上下文。
- **★ 真因是人类改正路径上的结构缺口,不是这一条数据脏了:** `update_decision(project_path, index, text, *, expected_version, updated_by_conv)` —— **没有 summary 形参**。它重写 `text`、返回 `ok=True`、bump 版本,而 `summary` **永远不动**。所以面板上看起来改好了、提示里完全无效。**与今早那枚「断言一件已经不成立的事」的药丸同形**:UI 为一个不再为真的状态背书。
- **省略语义(本轮真正要拍的那个决定):**
  | 情形 | 行为 | 理由 |
  |---|---|---|
  | 条目**有** summary 而调用方省略 | **拒绝** `summary_required`,且在**任何 mutation 之前**返回(text/summary/version 全不动) | 静默沿用旧行是唯一不可留的行为 |
  | `summary=''` | 清除,headline 回落到新正文 | 这是明确指令,不是省略 |
  | 条目**没有** summary 而省略 | 照常编辑 | 遗留条目本就渲染缩略首行,强制要求会把它变成**每次遗留编辑的税**,而不是对陈旧摘要的陷阱防护 |
  **拒绝优于清除**:清除会把一条精心写的规则行**静默降级**成缩略首行——用户没要求过的损失;而拒绝是一个他能回答的问题。`_SUMMARY_UNSET` 哨兵承载「省略 vs 显式清空」,`None` 表达不了这个区别。
- **★ 必须一路穿到底,否则单元测试会绿而产品面原样坏着:** 路由透传 summary 并**保住 absent-vs-empty**(把两者合并 = 把拒绝变成静默清除,NEUTER-5 专咬这条);`api.js` 写明省略会被拒;**面板内联编辑器改为同时编辑两个字段**——它此前**渲染** summary 却只编辑正文,即「用户看得见 agent 消费的那一行,却没有任何控件能改它」;后端开始拒绝纯正文编辑后,只改正文的编辑器会把「静默错规则」升级成**死的保存按钮**。新字段配自己的 CSS + zh/en 标签(「这是每个会话都会读到的一行」)——**没有样式规则的字段就是隐形控件**,本项目记录在案。
- **NEUTER×6 各咬各的方向(4/3/2/2/1/1):** 复原原始缺陷(完全忽略 summary)→ 4 红;**静默沿用旧摘要**(被明令禁止的那个行为)→ 3 红;**拒绝前先 mutate** → 2 红;路由丢掉 summary(修复止步于库边界)→ 2 红;路由把 `''` 折叠成省略 → 1 红;面板退回只编辑正文 → 1 红。三个产品文件事后 `cmp` 逐字节还原。
- **★ 陈旧套件就地反转:** `test_routes_charter_edit_delete_roundtrip` 原本对一个**带 summary 的条目**只发 `text` 并断言 200 —— **它认证了这个陷阱**(编辑落库,而注入的 headline 继续渲染旧的 `'D0'`)。现改为先断言拒绝,再做正确的双字段编辑。删掉它等于让下一个人重新引进同一条路。
- **活库闭环 + 一处我怀疑过的风险已证伪:** 决策 #0 的摘要经**人类 REST 路径**改写为 A 设计的规则;随后驱动**真** `_inject_system_contexts` 复核:新摘要在真提示里、旧摘要 0 次命中。另实测 `_system_text` **只扫 `messages[0]`**,所以 JOURNAL.md 正文里出现的 `[PROJECT CHARTER]` / `[PROJECT GOALS]` 字样(各 1/3 处)**不可能**满足幂等守卫、不会抑制真块——这条与今早的 marker 冲突同族,故实测而非假设。
- **写者清点(防第二条漏网路径):** 全库只有 `update_decision` 与 `delete_decision` 两个决策写者、一条 REST 路由;`delete_decision` 整条移除条目,**不存在 summary 不对称**,无需改动。
- **验收边界:** 后端已对活库生效;**前端与 i18n 需重启 + bundle 重建**。真浏览器未实测(证据为直接驱动真注入路径 + 真 REST 路由 + node 驱动 shipped 面板逻辑)。

### 2026-07-30(Goals 独立注入·方案 A) — owner 纠正我上午那批:「**目标就是目标,不用非得进章程才生效**」+「**不该让 LLM 直接改章程,章程永远需要人工审核**」。选 A 之后**删掉的机制比加上的多**——净 -2 行(`pt_b4b24a9df465426c` DONE;commit `6c28925c`,14 文件 +1039/-1041;守卫 **27/27 + 7/7**,**NEUTER×6 各咬各的**;相邻环 **215/216**;活库闭环:`charter.content` **清空**而目标仍在真提示里)

- **★ 上午那批是过度设计,而 owner 的一句话把它整段删掉。** 三态药丸、diverged 判定、替换预览卡、CAS 版本门、`promoted_text`/`promoted_at` 两列——**全部只为一件事存在:目标被复制进了 `charter.content`**。一句话有两份拷贝就需要「哪边动了」的对账;**一份拷贝一个都不需要**。这是本轮最值得记的判断:选 A 不是「再加一条注入通道」,是**把上午引进的复杂度连根拔掉**。
- **落点:** `render_goals_injection_block()` 把每条 **open 的 kind=goal** 渲成自己的 `[PROJECT GOALS]` 块,由 `_inject.py` 挂在**与章程/看板同一条** cache-stable 尾块缝上。目标的状态模型就是 `(kind, status)` 两个字段:**它存在、且未解决**。resolve/delete 即撤回,没有第二个「取消提升」步骤可忘。
- **多目标是合法的,单目标性本来就是那一列的产物:** 上午之所以「至多一条 active」,是因为 `content` 只有一格。没有那格之后,几条目标全部注入,不需要唯一约束、不需要位移、不产生 diverged 同侪。
- **`promote_watch_item` 对 goal 改为**明确拒绝**(`goal_not_promotable`)而不是静默 no-op:** 还在调它的调用方是照旧契约在跑,必须被告知,而不是让它以为自己设了北极星。
- **章程回归人类专属:** `project_charter_commit` 从 `CHARTER_TOOLS` 与 registry 的 `provides`/`write_tools` 移除,agent 只剩 read + propose。**而这件事能活到今天的原因是一句假注释**:`registry/_build.py:196` 写着 commit「is NEVER exposed as an agent tool」,而 `CHARTER_TOOLS` **三个名字全在发**——代码**读起来**是安全的,于是今早我(一个 agent)就这么无审核地写进了 owner 的章程。那条注释现在是真的了。
- **★ 工具名保留在 `CHARTER_TOOL_NAMES` 里,是为了让拒绝**指名道姓**:** 从旧 transcript 学到这个工具的模型仍会发起调用,它应该被告知「去用 `project_charter_propose`」,而不是收到一个不透明的 unknown-tool 错误。顺带清掉 **~96 行**因此不可达的 commit 分支体。
- **★ 而删除那段分支体让 `_route_lesson_to_memory` 变成孤儿——如实记录而不是假装没事:** 它唯一的调用者就是被删的 `kind=lesson` 分支。agent 仍可用 `create_memory` 记教训,所以**能力没丢**;但那套「同主题折叠成一份记忆」的三通道去重**不再运行**。我在它的 docstring 顶部标注了不可达 + 为什么保留(那套去重的测量成本是贵的部分:真实同族教训对只有 ~0.10 词汇包含度,这正是显式 `into_memory` 通道存在的理由),并按 owner 惯例**不塞进本批**修。
- **★ 本轮最有价值的一条:一个我自己引进的缺陷,是新守卫抓的,不是评审抓的。** `_refresh_tail_block` 靠「剥掉所有含该 marker 的块」实现幂等。而我给章程的空北极星提示里**逐字写下了 `[PROJECT GOALS]`** ⇒ 注入目标块时把**整个章程块连同决策一起删掉**,而日志照旧报告两个块都构建成功(`charter:656,goals:303`)。**这是一整类缺陷不是一个实例**,故守卫改为对**全部 marker** 断言「任何块的正文不得出现别的块的 marker」。
- **NEUTER×6 各咬各的:** 摘掉 `_inject` 里的 goals 调用(复现原始缺陷)→ 2 红;goals 块连 concern 一起发 → 3 红;resolved 的目标继续注入 → 2 红;把 commit 工具放回 `CHARTER_TOOLS` → 1 红;恢复 marker 冲突 → 2 红;让 goal 又能提升进章程 → 1 红。六发后四个产品文件 `cmp` 逐字节还原。
- **★ 陈旧套件一律**就地反转**,不删:** ①`test_project_charter.py` 8 条 agent-commit 测的是**存储性质**(版本递增 / 提议出队 / 滚动截断 / append 可交换),那些性质仍然成立、只是写入者变成人 ⇒ retarget 到 `commit_charter`,而「agent 不能写 content」那条**加强**成「agent 连 commit 工具都没有」;②kind-routing 的**策略层**(缺 kind 拒绝 / report 打回 JOURNAL / lesson 进记忆)随工具一起消失 ⇒ 11 条收敛成一条,把它们断言过什么**写在 docstring 里存档**;③前端三条替换预览测试**反转为断言那套机制不存在**,这样将来谁想重建 goal→charter 写入,必须先把这些符号加回来、而这条守卫会红。
- **★ 而那条「断言机制已消失」的守卫第一版就犯了本项目记录在案的同型错误:** 它被**我自己写的解释性注释**咬红——那段注释必然要提到被禁的符号名。已改为先走共享 `strip_comments(lang='js')`。**判据:凡断言「某符号不存在」,注释是第一个假阳性来源,不是边缘情况。**
- **活库闭环(owner 指定的验收):** 把 `charter.content` **清空**、并把今早那条 LLM 自写的 invariant #0 改写为 A 设计的规则(经 `update_decision`,人类 REST 路径);随后驱动**真** `_inject_system_contexts`:①目标原文在真提示里 ✅ ②`[PROJECT GOALS]` 块在 ✅ ③`[PROJECT CHARTER]` 块**也还在**(证明目标不是搭它的车进去的)✅。
- **共享 HEAD:** `tool_rounds.js` 的 `_CONV_META_TOOLS` 条目与两个 `projectBrain.watchGoalLive*` i18n 键被兄弟提交(`433a07c3`/`461cd0e0`)顺带带进 HEAD——已逐字核验在库,故正确地不在本批 pathspec 内;计数门因此从 15 改为 14 并在提交信息里注明。另清掉一个 gitignored 的孤儿 bundle(`feature-3a97842b.js`,10:00 构建、已不在 manifest),它是 i18n 覆盖守卫唯一的红来源。
- **验收边界:** 后端已对活库生效;前端与 i18n **需重启 + bundle 重建**。真浏览器未实测(node 驱动 shipped `buildWatchItem` + 直接驱动真注入路径)。唯一相邻红是 `tofu-pet.js` 的 variable-URL fetch——兄弟 epic 的既有失败,本批三个 JS 文件 `fetch(` 计数均为 0。

### 2026-07-30(续·直链钉 tag) — owner 实测抓出我上一批留下的 404 定时炸弹:URL 与文件名**生命周期不同**却被拼在一起,而**我那批 32 条守卫一条都看不见它**(`pt_ded078e2fe3642ed` DONE;commit `b7652c95`,2 文件;守卫 **+9 失败先行**,**NEUTER×3 各咬各的**;相邻环 **128/128**;真实网络 4 条链 HTTP 200)

- **★ owner 的判据比我的实现更准,我承认得干脆:** 我把 URL 拼成 `/releases/latest/download/<缓存的文件名>` —— 两半**此刻自洽**,但生命周期不同:文件名被我缓存 **900s**,而 `latest` 由 GitHub 在**用户点击那一刻**解析。期间一发新版,`latest` 指向新 tag,而新 release 里**没有那个旧文件名**。
- **★ 真实请求实测(同一仓库):**

  | URL 形式 | 结果 |
  |---|---|
  | `latest/download/Tofu-Setup-0.14.2-win64.exe`(在当前 latest 内) | **200** |
  | `latest/download/Tofu-Setup-0.15.2-win64.exe`(不在) | **404** |
  | `latest/download/Tofu-Setup-0.13.0-win64.exe`(旧版本名) | **404** |
  | `releases/download/v0.14.2/Tofu-Setup-0.14.2-win64.exe`(钉 tag) | **200** |

- **★ 窗口当时就是敞开的:** `VERSION`=0.15.2 而最新 **release**=v0.14.2 ⇒ 下一次发版**必然穿过**,届时 15 分钟内点下载的人拿到 404 —— **恰是发版后下载最多的时刻**。
- **★ 修法不是缩 TTL(缩到多小都有窗口),而是让「不一致」在结构上不可表达:** 名字与 URL 现在**同一条记录、同一份 payload** 出来,`_match_platform_assets` **逐字拷贝两者**而不再从名字重建。优先级:①`browser_download_url`(GitHub 自己给的钉 tag 链,**payload 里一直都有,是我上一批把它丢了**);②同一 payload 的 `tag_name` 自拼(**仍钉 tag**,回退去摸 `latest` 就等于把缺陷再犯一次);③两者都无 ⇒ **丢掉该资产**(编一个 URL 会 404 却看起来很权威,调用方退回 releases 页)。
- **★ 守卫的关键一条不是「不许是 latest」而是「URL 的 tag 版本 == 文件名版本」:** 只断言「不是 latest」会被**钉错 tag** 的 URL 满足。owner 点名的那发 NEUTER(文件名 0.15.2 / URL 改写成 0.13.0)**恰好只咬版本一致那 3 条**,证明这条断言测的是真命题。
- **★ 结构守卫踩到我自己写的文档:** 断言「executable 代码里不得出现 `latest/download`」时,该模块的 **docstring 正是在解释这个构造为什么错**,于是原始子串扫描把**防止回归的那段说明**判成违规——而且可以靠**删掉解释**来消红,方向完全反了。改为 **AST 摘除所有 docstring 后再扫**(注释也剥)。同族判据第二次:**负断言必须先问清「我扫的是代码还是散文」**。
- **★ 漂移免疫性是直接演示的,不是推理的:** 仓库上 `v0.15.2` / `v0.15.0` 两个 tag **都存在且都比 v0.14.2 新**,而 4 条 v0.14.2 钉 tag 链**全部 200** —— 这正是旧构造会变 404 的那一格。
- **NEUTER×3 各咬各的:** 版本错位 → 恰 3 红(版本一致);退回 latest 构造 → 7 红(钉 tag + 版本一致 + 结构);回退去摸 latest → 3 红(结构 + 两条回退)。
- **测试 fixture 同步改成 API 的真实形状**(name + 它自己的钉 tag URL,两者由同一版本串派生)—— **那本身就是被测的「同一快照」性质**,fixture 若允许两者不同就没在测这个契约。
- **架构判定未动:** owner 实测确认那部分正确(7 种客户端全对),本批零改动。
- **验收边界:** 纯后端,**需重启**(进程内 TTL 缓存仍持旧形状)。

### 2026-07-30(Devices 设置页) — owner 问「这个设备设置面板是不是很丑」:实测**不是设计丑,是这个面板从来没有过样式**——9 个类在两张样式表里各 0 条规则;顺带证伪了「服务器打包再分发」的一半,只做对的那一半(`pt_ef4a5ac948824298` DONE;commit `433a07c3`,10 文件 +1437/-52;新套件 **32/32**(**23 条失败先行**),**NEUTER×6 各咬各的**;相邻环 **92/92**)

- **★ 判据不是「丑」而是「不存在」:** `devices.html` + `settings/devices.js` 用了 `.stg-section`/`.stg-desc`/`.stg-row`/`.stg-table`/`.stg-dim`、**裸 `.stg-btn`** 和 4 个 `.devices-*`,实测 `styles.css` 与 `settings.css` **各 0 条**。于是浏览器兜底:裸 `<table>`(无内边距)、系统 `<button>`、只有默认 margin 的 `<p>`、两个 section 完全不分隔。**改法只能是补真规则,给现有规则「调样式」在结构上无从下手**。
- **★ 裸 `.stg-btn` 是本批最隐蔽的一格:** `.stg-btn-danger`/`-add`/`-icon`/`-balance`/`-link` **全都存在**,所以这个名字**看起来是有定义的**,任何评审都不会怀疑;而 Devices 页两个主按钮用的恰是**裸类**。同理 `.stg-section` **只有这一个消费者**——**单一调用方的类没有第二个页面可以「看起来不对」**,这就是它躺了这么久没人发现的原因。
- **★ owner 只报了设置页,实测同一 feature 还有第二个未着色面:** 项目文件夹浏览器的「远程设备」分组(`#remoteDevicesSection`,由 `project.js::_renderRemoteDevicesSection` 绘制)的 `remote-agent-*`/`remote-root-*` 同样 0 规则。只修设置页会让「devices UI 没样式」这句话**只修好一半**,且剩下那半正是用户挂远程工作树时会遇到的。已一并补齐。
- **★ 「正在加载…」永久停留的真因不是慢,是 `.catch` 结构上不可达:** `Api.desktop.devices()` 声明 `{onError:'null'}`,而 api.js 自己的文档写着「rejections become null」⇒ 后端死掉时 **resolve 成 null 进 `.then`**,那条画 ⚠ 的 `.catch` **永远进不去**。更糟的是 `(d && d.agents) || []` 把 null 直接喂给**空态渲染器**,于是**agent 明明在跑的用户被告知「暂无设备连接」**——他会合理地以为自己的 agent 死了。**「没能问到」和「你没有设备」是两个事实,现在渲染成两个样子**(`.devices-load-failed` vs `.stg-empty`)。
- **★ 两条车道同屏两个世代:** `_populateDevicesTab` 只把 agents 置为 loading、**从不重置 tokens** ⇒ 上半「正在加载…」下半已答「还没有 bridge 令牌」。一次请求两个答案。现在同帧同世代。
- **★ 服务器打包再分发:错的那半与真的那半。** 错:**Linux 服务器编不出 Windows `.exe` / macOS `.dmg`**,PyInstaller 不交叉编译(psycopg2-binary/pymupdf/lxml/cryptography 都是当前平台的 C 扩展),`build-desktop.yml` 四条腿 + 完整性闸是对的路,没动。真:**下载链接是瞎的**——`_desktop_download_url` 只回 `releases/latest`,挂着 5 个资产让用户自己猜。落点是**按平台直链**。
- **★ 资产名必须从单一真源推导,而「按 glob/label 正则反推平台」是同一份映射的伪装:** 给 `scripts/release_assets.py` 加 `PLATFORM_ASSETS`(os, arch, label, glob)四元组,`REQUIRED_PLATFORM_ASSETS` **改为从它派生**——两份手维护的列表会漂移,且**各自在自己还认识的平台上继续绿**。路由读这份表,零手写文件名(`test_desktop_build_workflow` 断言 glob 不得出现在别处)。
- **★ macOS 那一格:UA 里 Apple Silicon 也自称 `Intel Mac OS X`**(Chrome/Safari 都是)⇒ 两个 DMG 从 UA **不可区分**。所以架构未知时**同时给两个并说明怎么选**;猜一个会让**约一半 Mac 用户下到打不开的包**,比让他选更糟。架构走 `navigator.userAgentData.getHighEntropyValues`(**不是** `Sec-CH-UA-Arch` 请求头——那个要服务器先回一次 `Accept-CH`,于是**渲染下载按钮的首帧必然 arch-盲**)。
- **★ 我自己写的代码里抓出一个 404 制造机,而我的守卫当时看不见它:** glob 的 `*` 是版本号,而 `/releases/latest/download/<name>` **只解析 release 不解析文件名** ⇒ 带 `*` 的 URL 必 404。改为拿 GitHub latest-release 的**真实资产名** fnmatch 匹配(TTL 缓存 + 不可达则降级回 releases 页)。**NEUTER 第 7 发(把 glob 直接塞进 URL)第一次没咬**——因为 glob **也以 `.exe` 结尾**,只查扩展名的断言分不出 `Tofu-Setup-0.15.2-win64.exe` 与 `Tofu-Setup-*-win64.exe`。已补「文件名不得含 `*?[`」。**判据:断言「形状对」时要问它能不能把坏形状也算对。**
- **★ 我的平台测试一开始静默依赖 `api.github.com`:** 加 `published=` 注入缝,6 个调用点全部确定性;顺带把因此变成死代码的 `pytest.skip` **删掉而非留着**——留着会让整条断言在无出网的机器上安静蒸发。
- **★ 前端可达性单独立守卫(本批最该记的一条):** 后端选对了安装包,但**只要 UI 不连线,产品路径上完全不可达而 Python 守卫全绿**。补了 6 条 node 驱动**真 `_lcRenderDesktop`** 的端到端守卫(单链接/双 DMG 带解释/releases 页兜底存活/local_source 分支同款/客户端确实问了 arch/新字符串已翻译)。**修完自己又当场造了一个同型缺陷**:新加的 `.lc-dl-row`/`.lc-dl-direct` 一开始 0 规则——和我正在修的病一模一样,已补。
- **NEUTER×6 各咬各的:** 摘裸 `.stg-btn` → 2 红;退回单车道重置 → 恰 1 红;退回 null 空态 → 恰 1 红;reject 时清空 tokens → 2 红;macOS 猜一个 → 恰 1 红;手写资产名 → 6 红。
- **★ 共享 HEAD 三次摩擦,一次是真事故:** ①**兄弟把 Python 源码写进了 `static/styles.css`**(22291 → 1048 行,内容是 `tool_dispatch/_pipeline.py` 的逐字节副本)。先确认**那个 .py 本体完好**(1048 行、`ast.parse` 通过、与污染内容 identical)⇒ 是重复写入而非搬移,零兄弟工作丢失,再 `git checkout HEAD --` 恢复并重贴我的规则。②我的 `styles.css` 规则最终由**兄弟的恢复提交 `6c09656c` 逐字节带进 HEAD**(已核验 8 个规则全在),故本批只提交剩余 10 文件。③循环里的 `git reset -q` 每轮把上一轮的暂存清空 ⇒ 改为**同一 shell 链内 stage+断言+commit,不再 reset**。
- **★ 自报一起误提交:** `i18n.js` 夹带了兄弟 Goals 批的 `projectBrain.watchGoalLive{,Hint}` 两个键。**已核验两键逐字提交且在盘上完好(i18n.js:3577-3578),零丢失**,已 `project_message` 通知 ms6qb35tjl9y46 不要重复添加。**选择上报而非回退——回退等于为了我的提交边界整洁去删他的工作。**
- **既有无关红灯如实记录:** `test_frontend_api_isolation` 报 `tofu-pet.js` 1 处 variable-URL fetch。该文件与 HEAD **逐字节相同**且我从未碰过,属兄弟 epic,未在本批修。
- **验收边界:** 纯前端 + 后端路由,**运行中进程不带**,需重启 + bundle 重建。真浏览器未实测(jsdom 驱动 shipped 渲染器 + 直接驱动路由 helper)。

<!-- pt_a4c9d33e CLOSED 2026-07-27: board flipped to done from a dispatch that DID carry project_board_* tools. The implementation was in HEAD (fbda6d98 + d12cd17f, CAS 5/5) the whole time — only the flip was missing, because the closing tool was absent from the autonomous toolset. That silent dead end is now a visible `tool_not_available` envelope (9abdcb22, epic pt_88791cb08cb2495c), so a task blocked this way reports the reason instead of settling as a success. -->

### 2026-07-30(文件里装的不是它自己的语言) — 接自己开的 CRITICAL 票,而**污染已被兄弟修掉**;真正剩下的缺口是票面那句「值得顺带查一下有没有守卫能防住这类路径错配」——**答案是没有,而且现成那条最接近的只管尺寸**(`pt_b62849184ede40d8` DONE;commit `a66fa6ea`,1 文件 +151;**NEUTER×5 双向全咬**;相邻环 **176 全绿**;产品文件零改动)

- **★ 先确认票面前提是否还成立:** `static/styles.css` 现在 22430 行 / `data-theme` 1682 / 不再 `ast.parse` 通过 ⇒ **已恢复**。票面点名的 4 套复现器 **40/40 绿**。追溯到兄弟的 commit `6c09656c`「restore the CSS a concurrent rewrite dropped + guard the loss」——他恢复了自己那 16 行并为**自己的**类加了守卫。**所以恢复动作不需要我做,我也没有去覆写任何东西。**
- **★ 剩下的真缺口,以及我差点误判它已被覆盖:** 全库搜到 `test_write_freshness_gate.py:308` 有 `assert getsize('static/styles.css') > 512*1024`。它**确实会咬住这次事故**(坏文件约 42KB),一度让我以为票面问题已解决。但读它自己的注释:*"Sanity the guard itself reads the real tree"* ——**它是 freshness 阈值的 fixture 自检,不是污染绊线**;而且只管尺寸,**「尺寸对、语言错」那一半直接穿过去**。实测:我造了一个 **1046627 字节**的 Python 文件,尺寸地板放行,只有新的形态断言咬住。**判据:一条恰好能咬住某次事故的断言,不等于针对那类事故的守卫——要读它的意图(注释/docstring),否则会把巧合当覆盖。**
- **★ 事故的代价不在坏,而在「错误信息指向错误的地方」:** 87 个套件全部报「no `[data-theme=tofu] .folder-badge` rule found」——指向**缺了一条选择器**,于是排查从「谁删了 CSS 规则」开始。**没有任何一处说出更朴素的事实:这个文件已经不是 CSS 了。** 两个会话为此绕路,而且它还**静默作废了一次等价性对拍**(对拍双方都在读 Python 源码,却得出「仅差一个尾部换行」的真结论,见 `37c6a04e`)。
- **落点(刻意窄:形态检查,不是 linter):** `.css` 必须含声明块且**不得解析为 Python 模块**;`styles.css` 必须**整体**保有主题层(>100 个 `data-theme`,比现存 ~1680 低一个数量级,合法重构几十条规则永不误报);两个最大 JS 入口同样查 Python 模块。**不做 CSS 语法校验**——`node --check` 与 bundler 守卫已覆盖 JS,而在这里手写半个 CSS parser 正是 charter #24 要防的「第二份实现」。
- **★ `ast.parse` 成功不能单独当判据:** 它太宽松,很多 CSS 片段也能过。真正区分「被覆盖成模块」的是**顶层有 import 或 def/class**,所以判据是「解析成功 **且** 是个真模块」。
- **NEUTER×5 双向:** 复现原始污染(Python pipeline 模块)→ 红;**同尺寸异语言**(专门打尺寸地板的盲区)→ 红;空/截断样式表 → 红;主题层整体丢失但仍是 CSS 形状 → 红;**CSS 注释里写满 Python 样子的散文** → **保持绿**(charter #24:注释不得违反守卫)。
- **失败信息带恢复命令**,这是本条守卫存在的意义:「parses as a PYTHON MODULE … overwritten with source from another file … Recover with: `git checkout HEAD -- <path>`」。
- **验收边界:** 纯测试基础设施,**产品文件零改动**,不需要重启。全部 NEUTER 在 `/tmp/shapeneuter` 隔离副本里做,真实 `static/styles.css` 逐字节未动。


### 2026-07-30(等价性证明验的是垃圾输入) — 票面要求「逐字节对拍、完全一致才替换」,我照做了、结论「仅差一个尾部换行」——**而我对拍的那份 styles.css 已经被覆盖成 Python 源码**;修正输入后真实答案是 3104 行差异(`pt_df2a951a07324e47`;commits `37c6a04e` 11 文件 + `a8470440` 2 文件;**NEUTER 双向全咬**;相邻环 **108 全绿**;产品文件零改动)

- **★ 本轮最值得记的一条:等价性对拍必须先校验「输入是不是它声称的那个形态」。** 我第一轮拿 `static/styles.css` 对拍,得出「仅差一个尾部换行、零行级差异」——**结论为真且毫无价值**:它证明的是两个函数**对 Python 源码的处理一致**,不是对 CSS 的处理一致。因为那一刻该文件已被兄弟会话整体覆盖成 tool-execution pipeline 模块(22291 → 1048 行,`data-theme` 1680 → 0,`ast.parse` 通过)。迁移后 53 红。
- **★ 定责必须只读、且必须排除「共享树被兄弟改坏」这一项:** ① `git archive HEAD` 纯净副本跑同样套件 → **40/40 绿**;②工作树 → 36 红;③**把我 11 个文件全部 `git checkout --` 还原后仍然 36 红** ⇒ 与我无关;④`git diff --stat` 显示 1048 insertions / 22291 deletions。随后在 `/tmp/mig_proof`(HEAD 内容 + 同一套迁移)复跑 **43/43 绿**,反证迁移正确。**判据:还原自己的改动后仍红 ⇒ 不是我;这是只读手段,而覆写别人的文件即使为了定责也被禁。**
- **★ 被外部污染阻塞时不「先落地再说」:** 11 个待迁守卫全读那个坏文件 ⇒ 落库等于提交一批**工作树里无法验证**的改动。当时的正确动作是 block + 单独开票(`pt_b62849184ede40d8`),而不是覆写兄弟未提交的文件——他的改动没有第二份拷贝。兄弟随后自行恢复,我立刻复跑落地。
- **修正输入后的真实等价性(CSS 半场,11 个):** 差异是 **3104 行**,但性质是**行号语义**:共享版把注释行**留空**以保持行数(22400 行),本地版**删除**它们(20295 行)。判据落在更高的抽象层:**选择器集合完全相同(各 6466 条、零独有)+ 空白无关内容签名逐字节相同**,仅 25 条规则体差在空白;而这些守卫的断言全是空白无关的(子串/正则)。迁移前后**同为 66/66**。
- **★ JS 半场(2 个)不是等价而是 upgrade,必须说清:** 本地 `re.sub(r'//[^\n]*','',s)` 把**字符串字面量里的 `//`** 当注释——`api.js` 的 `path.startsWith('http://')` 从 `//` 起被整行删除,**真实代码在扫描前被吃掉**。171 个 JS 文件里 **27 个**因此不同,且**每一个都是共享版保住了本地版吃掉的代码**(零例反向丢失)⇒ 严格减少假阴性:扫描不再会被一个 URL 弄瞎。
- **★ NEUTER 里我自己的探针错了一次:** 第一发「注释不得破坏守卫」显示 1 红,看起来像守卫缺陷。实测是**探针无效**——`test_NC_bevel_is_flagged` 本身是**自我 neuter**(注入坏规则、断言守卫咬),而我插入的注释复制了 `.folder-badge{` 这个锚点字符串,它的 `replace(...,1)` 于是命中了注释而非真规则。换成不与自我 neuter 锚点冲突的选择器后**保持绿**;另补一发「注释里含不配对大括号」也保持绿;删真规则 → **3 红**。
- **刻意不迁的 6 个,各有实测理由(已开 `pt_6d03c27e2feb4777`):** `reducer_purity` 需要共享模块新增 `strings=True`;`lazy_sentinel_anchor` + `i18n_pack_boot_floor` 报告源码行号,行号语义承重需逐文件实测;`api_contract` + `boot_early_restore` + `convview_apply_guards` 是语义更强的单遍 tokenizer(后者 docstring 记录了 `streamSessions.get` 被吞的真实事故)。`api_isolation` **在纯净 HEAD 上已红**,单独开票 `pt_996d4a9a31e24af7`。
- **验收边界:** 纯测试基础设施,**产品文件零改动**,不需要重启。


### 2026-07-30(守卫认定的机制已被删除) — 自主派单接自己开的票,判据落在 **(a) 守卫过期** 而非 (b) 产品缺陷;而定案靠的不是代码注释,是**四条独立证据**(`pt_e3809ce36b544975` DONE;commit `2c3eb6e0`,1 文件 +111/-25;**NEUTER×7 双向全咬**;相邻环 **21 + 59 全绿**;产品文件零改动)

- **★ 票面要我在 (a) 守卫过期 / (b) 产品缺陷 之间判定,倾向「先查 `_igAbortController` 的所有 abort() 调用点」。照做,四条腿都指向 (a):**

  | 证据 | 实测 |
  |---|---|
  | 单图路径上的 `.abort()` 调用点 | **只有 1 个**,在 `_igCancelGeneration`(取消按钮)内 |
  | 请求传入的 signal | 只有 `_igAbortController.signal`;**全文件无** `AbortSignal.timeout`、无 `setTimeout(...abort...)` |
  | `Api.images.generate` | 显式钉 **`timeout: 0`** ⇒ 共享请求层也不产生 abort |
  | git 考古 | `eb1ddee5`「Remove the browser-side timeout ceilings too」**把 150s 看门狗和标志位一并删除** |

  标志位当初存在的**唯一**理由,就是把用户取消与那个看门狗区分开(两者都表现为 `AbortError`)。看门狗没了 ⇒ 没有第二个 AbortError 生产者可区分 ⇒ 标志位是死状态。**删得对。**
- **★ 决定性佐证,也是「这是守卫缺陷而非产品缺陷」的最强证据:** `tests/test_frontend_no_client_timeouts.py:220` **早就断言 `_igUserCancelled` 必须不存在**——那是删看门狗的兄弟会话写的、钉住「已删除」的守卫。也就是说**两条守卫在直接互相矛盾**:一条要求这个标志位存在,一条禁止它存在。本轮之后两者一致。**判据:发现一条守卫红时,先搜同一符号的其他守卫——互相矛盾的一对里,必有一条的前提已经蒸发。**
- **重写落点:钉「重构后仍然成立的用户可见属性」+「推断赖以成立的结构性前提」。** 前者双向(取消不得被标成网络错误 / 非 abort 失败不得被标成取消——没有后半句,「永远说取消」也能通过);后者是**绊线**:一旦有人重新引入客户端看门狗,超时就会以 AbortError 到达并被标成「用户已取消」(用户可见的谎),该测试立刻红并指名 `eb1ddee5`——那正是标志位必须回来的时刻。
- **顺带拆掉固定字节切片:** 旧的 `src[catch_start:catch_start + 1800]` **溢出真实 820B catch 块约 1KB**、读进了邻居代码——这正是它在被断言的 token 早已从文件里删除之后**还能「通过」**的原因。改用 `tests/_source_scan.brace_block`(4bc60817 新增)。
- **★ NEUTER 抓出我自己第一版断言的漏洞(本轮最值得记的一条):** 我写的 `'.abort()' in cancel_fn` 在**删掉单图 abort 之后依然为真**——因为同一函数里紧跟着的批量循环有 `ac.abort()`。**子串检查被同函数内的另一处调用满足**,NEUTER 不咬。已收窄到具体控制器(`_igAbortController...abort(`),并为批量那条补了互补断言。**判据:负/正断言都要问「有没有别的东西恰好也能满足它」,同一函数内的相似调用是最容易被忽略的那个。**
- **NEUTER×7 双向全咬:** 注释里提 `AbortSignal.timeout`/150s 看门狗 → **保持绿**;真加 `setTimeout(...abort)` → 红;`AbortSignal.timeout` 变体 → 红;取消被悄悄标成网络错误 → 红;删单图 abort → 红;删批量 abort → 红;只在注释里留 abort 调用 → 红。
- **验收边界:** 纯测试守卫,**`static/js/image-gen.js` 与 HEAD 逐字节相同**,不需要重启。


### 2026-07-30(共享扫描器补齐语言) — 自主派单接自己开的票,而**票面指令对 20 个文件里的 17 个是不安全的**:共享模块**根本没有 css 语言**,盲目迁移会静默削弱一打守卫(`pt_b95c6d396edd467d` DONE;commit `4bc60817`,5 文件 +683/-31;守卫 **0 → 19**(该模块此前零测试而有 28 个消费者);**NEUTER×5 各咬各的**;相邻环 **374 全绿**)

- **★ 票面(我自己开的)说「20 个文件各有一份手写 `_strip_comments`,逐个换成共享版」。实测先证伪:**

  | 事实 | 数字 |
  |---|---|
  | 消费 **CSS** 的文件 | **12/20** —— 而共享模块的 lang 表里**根本没有 css** |
  | `lang='css'` 静默退化到 shell 默认后,真 styles.css 残留 `/*` | **1495 个**(等于没剥) |
  | 需要剥**行内**块注释的 | **17/20** —— 共享版刻意只剥整行 |
  | 同一文件上:本地实现残留 vs 共享版残留 | **0 个 vs 20 个** |

  **判据:盲目迁移会「看起来成功」地拆掉 17 个守卫的牙齿——比它要消除的重复更糟。** 正确动作是**让共享模块能表达调用方真正需要的东西**,而不是把调用方按到一个更弱的原语上。
- **★ 顺带发现一个真·静默 bug:** `test_chat_active_consumer_census.py` 两处传 `lang='py'`——**未注册**,一直靠「退化到 shell 而 shell 恰好也用 `#`」的巧合工作。已登记为显式别名,把巧合变成声明。
- **落点(纯能力增量,零既有行为变更):** css 升为一等语言;`py`/`sh`/`bash`/`javascript`/`mjs` 注册别名;新增 `inline=True`(尊重字符串/模板字面量)而**默认行为逐字不变**;**未知 lang 改为硬报错**——正是这份静默让 css 空转藏了这么久。
- **★ 切片站点重新实测,票面的定性也是错的:** 票面说这 4 处「同时具备指控邻居和漏扫尾部」。实测**没有一处溢出**,三处 Python 全是**截断**(真实构造 6424B/16684B/3692B vs 800/600/800 窗口),且**三处都是正向断言**——正向断言配截断窗口是最危险的组合:今天通过只因为 token 恰好靠前,代码一挪就**安静变绿**。已迁 3 处;第 4 处不迁,因为精确提取暴露它的前提已经是假的(见下)。
- **★ 我自己的两个提取器各带一个 bug,都是新套件当场咬出来的,而且都属于「静默返回错误区域」——与固定切片同一失效类:**
  - `python_block` 对 `fill_form_sequential` 返回 **164B**(真实体 ~6.4KB):**换行签名的右括号与 `def` 同列**,纯缩进规则在那里就判定块结束了。**一个「精确」提取器比它替换掉的 2000 字节窗口更错**——它把一条本来通过的守卫变红,是**修复造成的假警报**。
  - `brace_block` 向**前**找 `{`,结果咬住内层对象字面量 `handle({ nested: true })`,返回的片段停在被断言 token 之前。改为向后回溯到**外层**块。
  
  两个都补了专门的回归守卫。
- **NEUTER×5 各咬各的:** css 退回 shell(3 红)/ 未知 lang 静默退化(1 红)/ 关掉 inline(2 红)/ 复原换行签名 bug(1 红)/ brace_block 复原前向扫描(1 红)。
- **★ 两条红灯经只读定责后确认不是我的,已各自开票不吞进本批:**
  - `test_paper_report_push_transport.py` —— **未跟踪的兄弟 WIP**(为尚未实现的 `pushSubscribe` 写的失败先行)。用 HEAD 版与本批版 `_source_scan` 分别跑,结论**完全相同** ⇒ 与本批无关。
  - `test_frontend_p0_small_batch.py` —— **干净树上已红**:`_igUserCancelled` 在 `image-gen.js` 里**一次都不出现**,`git archive HEAD` 取出的 HEAD 版同样不含。该文件 `_source_scan` 引用计数 **0**。已开 `pt_e3809ce36b544975`,并在票面写明关键判断:若超时路径也调 `AbortController.abort()`,则这是**产品缺陷**(超时被误标成「用户取消」)而非守卫过期。
- **后续开票(依赖已解除):** `pt_df2a951a07324e47` —— 12 个 CSS + 5 个 JS 守卫现在**可以**安全迁移了。票面写明迁移纪律:**每个文件先做「本地 strip vs 共享 strip 逐字节对拍」,完全一致才替换**;`reducer_purity` 还额外中和字符串字面量(共享模块尚无该能力),`api_contract`/`boot_early_restore` 用的是 tokenizer 单遍扫描(语义比正则版强),这三个都要单独判断而不是照搬。
- **验收边界:** 纯测试基础设施,**零产品文件改动**,不需要重启。


### 2026-07-30(Goals↔北极星单一概念) — owner 问「我在 Status & Focus 用 Goals 设的目标会进 LLM 上下文吗」——**问题的两个前提都被实测证伪**:不会进(零字节),而那枚「已进章程」药丸当时正在撒谎(`pt_bd42aad10a744014` DONE;commit `af297eb8`,10 文件 +1049/-61;守卫 **18/18 + 7/7**,**NEUTER×8 各咬各的**(后端 3/1/1/1、前端 2/3/1/1);相邻环 **103/103**;活库闭环:注入块 **0 → 244 字节**)

- **★ 先证伪两个前提,再动手。** ①`lib/tasks_pkg/system_context/` 全包 grep `project_watch`/`project_status`/`status_line`:**零匹配**——而且不是漏做,`test_watch_not_in_system_context_source` 明确断言这些符号**不许出现**在注入路径里。②同一时刻实测:watch item `promoted=1`,而 `read_charter()` 返回 `exists=False`、注入块 **0 字节**。UI 正在为一件已经不成立的事背书。
- **★ 真病根比「没接线」更糟:接了,但接到了会被淘汰的那一列。** `promote_watch_item` 无论 kind 一律走 `add_decision` ⇒ 目标落进 decision 列表,受 `_INJECTION_DECISION_WINDOW=20`(尾部窗口)+ `_MAX_DECISIONS=100`(FIFO)双重淘汰。这**正是 `_NO_GOAL_NOTICE` 自己的注释记录在案的旧事故**:「a goal committed as a decision instead is subject to both, which is how one previously went invisible」。实测佐证:线上 20 条 decision 里**零条**带 `[Goal` 前缀——它从来没活下来过。
- **★ 单一北极星语义是推导出来的,不是维护出来的(本批最值得记的设计判断)。** 判据是 `norm(text)==norm(content)`,而 `content` 只有**一份** ⇒ 文本相等的数学性质本身保证**至多一条** goal 处于 active。所以:不需要 DB 唯一约束、不需要提升时反写同侪、**不存在可以不一致的东西**。owner 给的三个候选里,这是唯一没有可漂移状态的。
- **★ 第三态 diverged 是正确性要求,不是打磨。** 「从未提升」与「提升后有一边被改」在文本上**完全同形**(都是 `text != content`),只有持久回执(`promoted_text`+`promoted_at`)能区分。把 diverged 显示成「未提升」= 递给用户一个按钮,一点就静默覆写他刚在另一边改的字。故新增两列不是为了好看,是为了让分歧**可诊断**(谁动的),而不只是可检测。
- **★ 我自己的守卫第一版就犯了本项目记录在案的同型错误。** `test_goal_reaches_agents_only_via_commit_charter` 首版做子串扫描,结果被**它自己 docstring 里的「injection」一词**咬红——干净的树报红。判据:**docstring 不是注释**,`strip_comments` 剥不掉它;要断言「有没有 CALL x」就得走 AST 看真实调用名集合,子串扫描对这个命题结构性不适用。
- **★ 就地反转而非删除既有守卫(第二次用同一纪律)。** `test_promote_calls_charter_commit_not_agent_prompt` 断言 `add_decision` 以 `[Goal` 开头——它**认证了错误路由**。拆成 goal/concern 两条并在 docstring 写明旧前提为何失效;删掉等于让下一个人重新引进同一条路。
- **★ 两个上限描述同一样东西却不一致,当场对齐而非开票。** `_ITEM_TEXT_MAX` 2000 → 8000 与 `_CONTENT_MAX_CHARS` 相等。不等 ⇒ 分歧态「以章程为准」回写会静默截断;相等 ⇒ 那个分支**根本不存在**(owner 指令:这是本设计自己引入的路径,不是既有债务)。
- **★ 只测 B 段的守卫对「A 段发的 ≠ B 段读的」结构性免疫——本批预先补了那条腿。** 缺陷完全落在前端分支逻辑上(`!item.promoted` 让 diverged 的 goal 把按钮拿回来),任何 Python 断言都看不见。新套件用 node 驱动**真** `project-brain-status.js` 的 `buildWatchItem`,4 发前端 NEUTER 全咬。
- **零新增注入通道(明确声明):** `system_context/` 一个字节未改,human-facing-only 守卫一字未动;goal 到达 agent 的唯一路径仍是 `commit_charter` → 既有 `render_charter_injection_block`。
- **活库闭环(owner 指定的验收):** ALTER 落库 → 用新路径把 owner 那条 goal 设为项目目标 → 注入块 **0 → 244 字节**且目标原文逐字在内;随后模拟在章程页改字,卡片正确翻成 `diverged(side=charter)`,还原后翻回 `active`。charter 承接本批 invariant 后注入块 533 字节,**目标位于 decisions 之上**——这正是 `content` 列存在的结构性理由。
- **验收边界:** 后端已对活库生效(ALTER 幂等);前端与 i18n **需重启 + bundle 重建**。真浏览器未实测(node 驱动 shipped 函数 + 直接驱动后端)。

### 2026-07-30(续·散文让结构守卫转红) — 自主派单接前两批顺手开的票:`test_events_round_key_unified` 在干净 HEAD 上已红,而**产品完全正确**——54 个 EventSpec 里裸 `round` 字段键**实测 0 个**,守卫报的那 1 条是 PHASE 描述文本里的示例(`pt_174f89ef93ac41be` DONE;commit `59e2cef6`,1 文件 test-only;守卫 **3 → 5**,**NEUTER×3 各咬各的**(3/1/1,含 1 发反向);隔离副本 **5/5**;相邻环 **94/94**)

- **★ charter #24 的反方向,同一个病:** #24 说「负断言不能被**散文满足**」;这次是「结构断言不能被**散文推翻**」。旧守卫用正则扫 `fields={...}` 的**原文**:`re.search(r"['\"]round['\"]\s*:", block)`。而 dict 字面量的原文**同时包含键和值**,PHASE 的 `detailArgs` 值恰好用示例说明自己的形状——`'(e.g. {"round": 3, "model": "claude-4"})'`。于是**一段完全正确的文档**让守卫红了,并且是**在干净 HEAD 上红**,指控一个早已统一好的契约。
- **★ 判据:关于「字段名」的结构断言,必须对着字段名求值。** 改为扫 `fields` 的 **KEY**。
- **★★ 但「用 AST 取 keys」这个直觉修法有个盲区,是我实测才发现的:** 本 epic 前两批我给 4 个工具 spec 加了 `**_TOOL_CLOCK_FIELDS` / `**_TOOL_END_CLOCK_FIELD` 展开。`ast.Dict` 里展开项的 key 是 `None`,**朴素的 AST key 遍历会静默跳过恰好这 4 个 spec 的全部键** ⇒ 守卫靠「没看见」而变绿。所以主真源用**运行时注册表** `all_event_specs()`(展开已解析),AST 只作**独立交叉校验**并把展开项显式标记为 `'**'`(而不是当成干净)。**NEUTER 2 就是把 `round` 藏进 spread 里 ⇒ 注册表那条精确咬,证明这个盲区真的被堵上了。**
- **★ 反向守卫(本批的关键一条):** 新增 `test_NC_prose_mentioning_round_is_NOT_flagged`,并**锚定到真实的 PHASE spec**——断言它不声明裸 `round` 字段、**同时**断言它的描述里仍然含 `round` 字样。这样将来有人把守卫退回原文扫描,这条会立刻红(NEUTER 3 实测:退回旧正则 ⇒ 1 红)。**只做正向的话,一个「重新扫原文」的回归会完全静默。**
- **顺带把过期的 docstring 与 standalone runner 一起修了:** 文件头原本写着「TESTS-FIRST: RED on HEAD by design」——那句话在漂移修完之后就变成了**给假红打掩护的说明**,任何人看到红灯都会以为「本来就该红」。runner 也同步为 5 个 face,不再打印「RED (expected)」。
- **验收边界:** 纯测试改动,**产品文件零改动**(`lib/agent_core/events.py` 逐字节未动),不需要重启。

### 2026-07-30(续·Reading Mode 不止 report 一个引擎) — owner 指出目标句里的「paper reading mode report」覆盖的是**整个 Reading Mode**;脚本普查 5 个配了 push_channel 的 runtime,实测**另有 2 个面复刻了 report 修复前的同一张图**。本批把契约收敛成**一份共享实现**,并被既有守卫抓出**我自己的 2 处真回归**(`pt_f6aec3ad0efb40de` DONE;commit `da7ca59a`,6 文件 +636/-14;新套件 **7/7**(3 条失败先行),**NEUTER×6 各咬各的**;相邻环 **78/78**;`git archive HEAD` 隔离副本 **57/57**)

- **★ 普查表(脚本枚举 `lib/paper/*runtime*.py` 的 push_channel × 前端消费者,不按票面列名):**

  | runtime | channel | per-tool 事件 | 前端 | 判定 |
  |---|---|---|---|---|
  | report_runtime | paper | 有 | report.js | 上一批已修 |
  | **qa_runtime** | paper | 有 | qa.js | **poll 700ms,pushSubscribe=0** |
  | **recommend_runtime** | paper | 有(经 `recommend_task._on_tool_event` 转发) | arxiv.js | **poll 600ms,pushSubscribe=0** |
  | podcast_runtime | paper | **无**(全部 `podcast_engine/*.py` 的 tool_start 计数 0) | podcast.js | 豁免 |
  | translate_runtime | paper-translate | **无**(tool_start=0) | babel.js | 豁免 |

  `video` 压根没有 push_channel,按构造不在范围内。**两条豁免是实测的、不是假定的**——它们只发阶段进度,订阅了也不承载本 epic 关心的东西。
- **★ 判据升级:豁免声明必须会「过期」。** 普查守卫不只断言「谁没接」,还**反向断言豁免仍然为真**:一旦 podcast/translate 将来长出 `tool_start`,豁免自动失效变红。NEUTER 6 就是给 podcast 塞一个假的 `tool_start` ⇒ 精确咬普查那条。**一句「按设计不需要」如果没有会过期的机制,它就是一个没人再验的豁免**(上一批 screenshot 的教训直接搬过来)。
- **★ 契约收敛成一份:`static/js/paper/push_transport.js`。** 抄 report.js 的内联助手去修第二、第三个消费者,会留下**三份近乎相同的同一契约**。共享模块只暴露三个函数:`paperIngestEvent`(seq 有序 exactly-once 闸)/ `paperAttachPush`(按 (state,taskId) 幂等 + `isCurrent` 弃用守卫 + 终态自动释放)/ `paperDetachPush`。
- **★★ 加第二条传输的代价仍然是 exactly-once,而这两个面比 report 更脆:** `qa.js`/`arxiv.js` 原来**只有 cursor、没有 seq 闸**,裸接会把每个 delta 应用两遍(答案渲染两次)、并让 recommend 的 `candidate` 事件**重复持久化卡片**。所有 apply(push 与 poll 两侧)统一走共享 seq 闸。
- **★ 释放必须在 `finally`,不能只靠终态帧:** abort 分支与 404/expired 分支**永远看不到终态帧**;而 **Q&A 每问一个问题铸一个新 task**,漏释放就是「每问一句泄漏一个常驻 handler」。两个消费者都改成 finally 释放。
- **★★ 既有守卫抓出我自己的 2 处真回归(比新功能更值得记):** 新增一个进 bundle 的文件却**没在 `index.html` 补 `<script>` 标签** ⇒ **dev fallback(打包失败时改用逐个标签的那条路)会静默丢掉它**。`test_artifacts_bundle_registration` + `test_bundle_manifest_parity` 两条同时转红。用干净 HEAD archive 归属:14 条 bundle 失败里**恰好 2 条是我的**,其余 12 条在干净 HEAD 上同样红。**判据:新增 bundle 文件是「两处登记」——`_BUNDLE_FILES` 与 `index.html`,只做一处会在打包降级路径上无声失效。**
- **验收边界:** 本 commit 纯前端,**需重启 + bundle 重建**;真浏览器未实测(node 端到端驱动 shipped `push_transport.js` + `qa.js`,12 项检查)。

### 2026-07-30(续·第 8 条 lane 与 round 自描述) — owner 复核抓出**我自己给 screenshot lane 写的「按设计留在 post-phase」理由是假的**;以及一条更普遍的:**skip lane 的 round 上没有 `tEnd`,而 poll/刷新读的正是 round**。本批最值得记的是 **NEUTER 连揭我三次守卫太弱**(commit `d2c165f5`,2 文件;守卫 **13 → 15**,**NEUTER×4 各咬各的**;相邻环 **175/175**;`git archive HEAD` 隔离副本 **50/50**)

- **★ 我上一轮定了「用脚本枚举、不按票面条目修」,然后自己在最后一条上违反了它:** 8 条 `continue` 里我把 screenshot 归为「终态取决于模型视觉能力,要等 post-phase」。owner 实测证伪:`model` 是 `execute_tool_pipeline` 的**入参**,`model_supports_vision(model) -> bool` 是**纯函数**,派发时就能算;AST 检查那个分支**根本不碰 `_round_results_for_budget`**(唯一真需要屏障的东西)。真实事件序确实是 `tool_result(shot) → tool_result(slow) → tool_complete(slow) → tool_complete(shot)`。**判据:「按设计」这三个字必须能被实测支持,否则它就是一个没人再验的豁免。**
- **★ 抽 `_screenshot_display_content(model, tool_content)`,并把 post-phase 的 `continue` 删掉:** 只有**保序的 `role:'tool'` 消息 append** 真正属于 post-phase;终态帧改由共享 settle 幂等发出。副产品:census 从 8 降到 7,并补一条断言「screenshot 不得再长出 `continue`」——**分类表与代码必须一致,否则守卫会替一个已消失的理由站岗**。
- **★ 两个调用点都改读 `model` 形参而非 `task['model']`:** 编排器传的是 `rs.model`、之后才**镜像**到 task 上,所以镜像在中途 fallback 时可能是旧值。用形参直接消掉这一整类分叉。
- **★★ round 不自描述:`_settle_tool_result` 只把 `tEnd` 算给事件,从不写回 round。** 实测被拒绝的 round:`status=rejected tStart=Y tEnd=N`。这不是装饰问题——**poll 车道(`/api/v1/chat/poll`)和每次刷新都是发整个 `toolRounds` 对象、从不重放事件**,所以对没有 SSE 的客户端、以及任何刷新过页面的用户,「执行段」永久缺失,而那正是三段里的第一段、也正是用户排查慢 turn 时会走的路。写回后 `tStart`/`tEnd` 都在 round 上;`or now_ms()` 在**反方向**承重:已经过 `_finalize_tool_round` 的 round 保住它的**真实**结束时刻。
- **★★★ 本批最重要的一条:NEUTER 连揭我三次守卫太弱,三次形状各不相同。**

  | NEUTER | 首版守卫为何没咬 | 修法 |
  |---|---|---|
  | 覆写真实 `tEnd` | 用了 450–3000ms **容差带**,而 600ms 工具被抹成 **718ms**,轻松穿过 | 捕获 finalize 记下的**精确时刻**并要求**相等** |
  | screenshot 退回屏障后 | round-shape 守卫**对顺序结构性免疫**(post-phase settle 幂等,退回后 round 依然形状完好、只是晚) | 单独加一条**顺序**守卫 |
  | post-phase 判决写死 | dispatch 的 settle **幂等地赢了**,14 条其余守卫全绿 | 断言**模型真正收到的那条 `role:'tool'` 消息**与 UI 判决一致 |

  共同教训:**「绿」只说明我断言的那个命题成立,不说明缺陷不存在**。容差带、幂等覆盖、以及「只测事件不测 round」都能让一个真缺陷合法通过。
- **★ 我自己的 fixture 又错一次(同族第三次,方向仍是「没把被测代码装好」):** `model_supports_vision` 对**未知**模型名默认返回 **True**(宽松),所以我编的 `'text-only-model'` 静默走了 vision 分支,**no-vision 那一路等于什么都没断言**。换成 `deepseek-chat`(能力表实测报告为 text-only)。判据:**造 fixture 用的常量也要实测,不能假定它落在我想要的那一格。**
- **验收边界:** 本 commit 纯后端,**需重启**;真浏览器未实测。

### 2026-07-30(续·被漏掉的 6 条 skip lane) — owner 复核抓出**上一批只接了 dispatch 路,pre-phase 的 skip lane 全没接**;而其中「流式预取命中」是**零耗时**工具,反而成了全产品唯一仍被慢兄弟拖住的一类。**更要紧的是:天真地接上会把「被拒绝」画成「已完成」**(`pt_ac380e3dde2c4c69` DONE;commit `34cffd94`,3 文件 +717/-2;新套件 **11/11**(8 条失败先行),**NEUTER×6 各咬各的**(2/2/1/1/1/1,含 1 发反向);相邻环 **171/171**;`git archive HEAD` 隔离副本 **46/46**)

- **★ 票面自己不全,判据是「用脚本枚举」而不是「按条目修」:** owner 点名 3 条 lane,AST 枚举 `execute_tool_pipeline` 的**全部** `continue` 实测 **8 条**——parse_err/幻觉工具、dedup+prefetch 命中、审批拒绝、abort 短路、pre-hook block、串行写 abort 跳过(6 条**都没接**)+ 长阻塞串行(已接)+ screenshot(自己发)。**这 6 条的共同点:工具根本没跑,耗时为零** ⇒ 恰恰是最该秒亮的那批,却等本轮最慢兄弟。已把「枚举 + 逐条分类」做成 census 守卫钉在 8,新增一条未分类的 `continue` 就红。
- **★ 最刺眼的一格是流式预取:** `StreamingToolExecutor` 的存在意义就是**模型还在吐 token 时就把工具跑掉**(`inject_into_cache` 在 `orchestrator/_run.py` 真实调用),所以派发时答案早就在手。实测事件序 `tool_result(cached) → tool_result(slow) → tool_complete(slow) → tool_complete(cached)`——**零成本的那个最后结算**。修后 cached 在 slow 报告之前就全部收口。
- **★★ 真正危险的是第二半,天真接线会引进比延迟严重得多的缺陷:** `stream_reducer.js` 的 tool_complete 分支原文是 `if (r.status !== 'rejected') r.status = 'done'`——**只保护了一个判决**。而 `aborted`/`error`/`unanswerable` 都是本库真实在用的 round 状态(实测赋值点 9/15/1 处),任何一个后面跟一帧 tool_complete 都会被**静默提升为 done** ⇒ **用户明确拒绝的写入渲染成「已应用」、被 Stop 打断的工具渲染成「已完成」**。两侧同时修:后端 `_settle_tool_result` 增 `terminal_status`(盖到 round 且随事件上线),前端改为「帧上显式 status 优先 → 否则已持终态判决者不许被提升 → 只有真在飞的才落 done」。`pending_approval`/`awaiting_human`/`awaiting_stdin` **刻意不算终态**——对它们来说一次真完成意味着「等待已解决、工具随后跑了」。
- **★ 失败先行的输出还额外暴露了第三个缺陷(票面和我都没预料):** 被拒绝的写入 round 停在 `status='pending_approval'`、abort 跳过的停在 `'searching'`,一直到 `_finalize_dangling_tool_rounds` 在**任务末尾**才清扫 ⇒ **一次 Stop 或一次 Reject 会让转圈动画转完这一整轮剩余时间**。现在两者在被决定的当刻就带上终态判决。
- **★ NEUTER 必须含反向那一发:** 摘 cache lane → 2 红;摘 reject lane → 2 红;去掉 aborted 判决 → 1 红;恢复旧的 rejected-only 客户端守卫 → 1 红;终态集合置空 → 1 红;**反向:把 status 完全冻结 → 1 红**(证明「不许覆盖终态」没有过度到「正常 round 也到不了 done」)。只做正向的话,一个「什么都不写 status」的实现会全绿。
- **★ fixture 判据延续上一批的教训并前置执行:** 本套件的 round **一律用真实 `_build_tool_round_entry` 构造**——手工造 dict 会漏 `tStart`、让时长恒 0ms,那是 fixture 缺陷伪装成产品缺陷,上一批已被咬过一次。
- **刻意不动的一处:** L630 screenshot lane 留在 post-phase——它的终态取决于**模型有没有视觉能力**,而那要等 post-phase 才决议;它本来就自己发 `tool_complete`,不是遗漏。
- **顺带证伪一条既有红灯并单独开票(不塞进本批):** `test_events_round_key_unified` 在干净 HEAD 上就红,实测把两批改动全部回滚后仍红。根因是**守卫误报**:PHASE 的 `detailArgs` **描述文本**里含示例 `{"round": 3, ...}`,守卫在 fields 块里搜 `'round'` 命中了**文档字符串的值**而非字段名(PHASE 真字段是 `roundNum`,早已统一)。已开 `pt_174f89ef93ac41be`,倾向改守卫为只断言 fields 的 **key**——值里出现任何单词都不该让结构守卫转红,与 charter #24「负断言不能被散文满足」同源。
- **验收边界:** 后端 + 前端 bundle,**运行中进程不带**,需重启 + bundle 重建;真浏览器未实测(node 端到端驱动 shipped reducer)。

### 2026-07-30(工具完成→前端停转) — owner 报「搜索完了转圈还在转,要等下一个工具的第一个 token 才更新,没法排查卡在哪」;实测**不是等下一个工具,而是等本轮全部工具**,而且是**三道互不相同的屏障** + 一个让任何修复都不可证伪的缺失量具(`pt_67ffc2b700094ce9` DONE;commits `bcaad758` 17 文件 +2795/-133、自审补救 `6c09656c`;新 4 套 **35 测试全部失败先行**(19 红:3 排序/4 批量/4 push/8 时钟),**NEUTER×7 各咬各的**(3/4/1/4/1/1/1);相邻环 **173/173**)

- **★ owner 的推断偏早了一格,真相更宽:** 票面猜「等下一个工具的第一个 token」。实测 `tool_result`(停转圈那半)**本来就是即时的**——handler 在 worker 线程里就发了;真正被推迟的是 `tool_complete`(内容/token/预览芯片),它在 `pool.shutdown(wait=True)` **之后**的 post-phase 才发 ⇒ 一轮里 0.05s 的 `read_files` 要等 40s 的 `web_search`。**判据:先分清「哪一半慢」再动手,否则会去修本来就对的那半。**
- **★ 三道屏障形状不同,修一道不动另外两道:**

  | 屏障 | 症状 | 落点 |
  |---|---|---|
  | 轮级线程池 | 快工具等最慢兄弟 | 抽 `_settle_tool_result`,在 `as_completed` 内即时结算(串行写/长阻塞两条道同样处理) |
  | **批量内部** | `web_search(queries=[3])` 是**一个** round,N 次网络请求只有**一个**可观测跃迁(截图正是这格) | `run_batch_concurrent` 加 `on_item`,每条查询落地即发 per-item `tool_progress` |
  | paper 传输 | 后端**早已**在 push 上广播,前端 `pushSubscribe` **零调用** | 补 `_attachReportPush`,**三处** attach 点全接(刷新后最常见的是 lookup 那条) |

- **★ 「后端根因再同步前端」在 paper 这条恰好反过来:** `report_runtime` 早配了 `push_channel='paper'`、`tool_done` 工具一返回就 append——后端零缺陷,缺的是前端那条腿。`research.js:167` 是现成先例。**判据:先证伪「后端有病」这个前提,否则会去修一个健康的后端。**
- **★ 加第二条传输的代价是 exactly-once,不是「更快」:** push 与 poll 送**同一批**事件,裸接会把每个 delta 应用两遍 ⇒ 报告正文渲染两次(该文件的 `delta_reset` 注释本身就是这个事故的疤)。落点:`_applyReportEvent` 升级为**按 `seq` 的有序 ingest 闸**(每个事件都带 `TaskRuntime.append_event` 分配的单调 seq),去重是**精确**而非启发式。
- **★ 第 4 件事才是让前三件可验证的东西:** 四个工具 EventSpec **零时钟**,所以「慢在上游/慢在屏障/慢在浏览器丢渲染(`twFlush-skip` 真会丢)」三者同形,「我修好了延迟」不可证伪。补 `tStart`/`tEnd`/`emittedAt`(epoch **毫秒**,单一 `now_ms`;`emittedAt` 只在 `build_event` 一处盖、**只盖工具类型**,delta 热路径不加字节)+ ingress 盖 `receivedAt`。三段:执行=`tEnd-tStart`,传输=`receivedAt-emittedAt`,渲染=`painted-receivedAt`。
- **★ `receivedAt` 为什么必须在 ingress 盖而不能在 reducer 里盖:** reducer 是**纯函数**,其 live fold 要与同一 turn 的 cold projection **逐字节相同**(parity 契约)。在里面写 `Date.now()` 会破坏该契约。所以 reducer 只**透传**时钟,`receivedAt` 作为**客户端本地遥测**从 parity canonicalizer 里排除(排除的是对比,不是生产)。**我第一版守卫断言「reducer 必须盖章」——命题本身与既有契约冲突,已就地反转并写明理由。**
- **★ 我的 fixture 错了两次,方向都是「没把被测代码装好」:** ①手工构造 `round_entry` 不带 `tStart` ⇒ 时长恒 0ms,看起来像产品缺陷,实为 fixture 绕过了真实构造器(改用 `_build_tool_round_entry`);②harness 只加载 `report.js`,而 `_reportView` 住在 `paper-reader.js` ⇒ 报「符号缺失」。同族第 N 次,判据不变:**红灯先分清「被测代码错」与「我没装好它」。**
- **★ 我自己引进的一处真回归,由基线守卫抓回:** 空插值 `${_renderBatchProgress(round)}` 给非批量行留下多余空白 ⇒ 冻结的工具行**逐字节基线** 2/41 不符。**选择收紧插值而不是重生成基线**——基线的价值就在于不被随手刷新。
- **★★ 自审抓出比上面都严重的一件:提交后 `styles.css` 带了兄弟 +123 行 `.stg-*`、而我的 `.ptool-batch-progress` 一行没进去。** 兄弟在我插入与提交之间**并发重写**了该文件,保留了他的、静默丢了我的。**而全链路都「工作」:后端发进度、handler 存计数、渲染器吐出 span,只有让它可见的那条规则没了 ⇒ 像素上什么都没有,且没有一条 Python 守卫会红(它们都不读样式表)。** 与 `.conv-state-unconfirmed` 同型:**DOM 里有语义 ≠ 用户看得见。** 已补两条守卫(CSS 存在 + 驱动 shipped `_renderBatchProgress` 的正/反向),NEUTER 复刻那次覆盖 → 精确咬新守卫。
- **刻意不做的两件(有实测理由):** ①轮**聚合**预算留在屏障后——它按定义需要全部结果才能定轮尺寸,改用 `tool_compacted` **增量纠正**已宣告的结果,而不是让首发事件等它;②单条(非批量)search **不发** per-item 进度——它本来就只有一次网络调用,`tool_result` 已是 per-item 信号,补一条只会给产品里最高频的工具凭空加一倍事件量。
- **验收边界:** 后端 + 前端 bundle,**运行中进程不带**,需重启 + bundle 重建;真浏览器未实测(node 端到端驱动 shipped `dispatchSSEEvent` / `report.js`)。

### 2026-07-29(续·context_limits 折叠) — 同族第三张牌:`model_context_limits` 以 slot.provider_id 为键,账户/面合并后 `sankuai_anthropic::*` 学习条目全部孤儿化——含今晚 opus-5 刚从一条 1.1M 成功提示学到的 expand,重启即丢(`pt_998336d4ec734207` DONE;commit `815f2cf1`,3 文件;新套件 **10/10 失败先行**,**NEUTER×3 各咬各的**(6/1/2);相邻环 **75/75**)

- **★ 与 key_stats 折叠同型但有一处关键不同:这个文件被前端裸读。** `routes/config.py` 把 `model_context_limits` 原样发给 Settings UI,所以只做内存态折叠挡不住 UI 显示孤儿——`_load` 折叠后**立即持久化**;且持久化的 mutate 折的是**写时**的文件内容而非读时快照(跨进程并发 learn 不丢更新,复用 update_json_atomic 的 flock)。
- **冲突规则:meta.ts 新者胜,平手账户赢。** 学习即证据,新证据描述的是当前上游窗口;账户是存活命名空间(新学习写向它)。
- **★ 真实数据校准的三个形状(肉眼查过真文件才定的判据):** ①`ephemeral:local::glm5.1-FP8`——provider 段自带单冒号,**只能按第一个 `::` 切**;②裸模型键(`glm5.1`)不动;③dangling meta(无对应 limit 值的)不复活(与 `_persist` 同契约)。
- **映射复用同一真源** `provider_face.account_namespace_map`(charter #24 无第二份面规则),结构守卫钉住「consume 而非 reimplement」。
- **NEUTER×3 各咬各的:** 摘折叠调用 → 6 红;折叠但不持久化 → 恰 1 红(文件断言那条);ts 冲突规则反转 → 恰 2 红(两条冲突测试)。还原 `diff -q` 逐字节一致。
- **真实冒烟(收敛性写盘,如实记录):** 真 config 加载后 `sankuai::claude-opus-5=1110553`、`sankuai::aws.claude-opus-4.7` 已折叠落盘,面键清零。边界:运行中进程(23:14 boot)仍持旧内存态,下次 `_persist` 会暂时写回孤儿,**重启永久收敛**;且当前进程对 opus-5 的窗口查找在重启前 miss 这条学习(退回静态预设)——与今晚所有批次同一「需重启」边界。
- **验收边界:** 纯后端,**需重启**;重启当刻 opus-5 恢复使用 1.1M 学习窗口。

### 2026-07-29(续·face 药丸清点过滤面) — owner 抓出我上一批**新增了 resolve_face 的第二个消费者却没清点 dispatcher 的前置过滤面** ⇒ 用户自己关掉的模型被画成醒目琥珀「未注册(协议面缺失)」;**而我第一版只修后端,前端那条腿让整个修复在生产路径上不可达、后端套件却全绿**(`pt_db4730d10104498e` DONE;commit `dc1a73b3`,3 文件;守卫 **24/24**(7 条失败先行),**NEUTER×7 各咬各的**(3/1/1/1/1/1/1,含 2 发反向);相邻环 **88/88**;HEAD archive 独立复跑 **24/24**)

- **★ owner 点名 2 个过滤面,实测是 4 个,我先清点再动手才发现第 3 个:** `_build_slots_from_providers` 在调 `resolve_face` **之前**有 4 道语义早退闸——L334 `provider.enabled=False` 整卡跳过 / **L399 `effective_keys` 为空整卡跳过(owner 与我第一轮都漏了)** / L408 `model_id` 空(端点已过滤,无缺口) / L414 `model.enabled=False`。三格实测均为「端点 `ok=False` 而 dispatcher 连 resolve 都不做」⇒ 三处假告警。**判据:清点过滤面要用脚本枚举 resolve 之前的全部 `continue`,不能按票面列的条目逐个修——票面本身可能不全。**
- **★ L399 的判据不是「有无密钥」而是 `effective_keys`,这个区别会咬掉整类用户:** `brand=='local'` 且无密钥时 dispatcher **放行**(拿空串当单个密钥,vLLM/SGLang 无鉴权)。若按密钥数判 `no_keys`,**每个自建部署的药丸会全部消失**。已补反向守卫 `test_keyless_LOCAL_provider_is_NOT_skipped` 钉死这个补集。
- **★ 本批最重要的一条:我的 Python 守卫全绿,而修复在真实产品路径上完全不可达。** 后端 skip 逻辑读 `provider.enabled`/`model.enabled`/密钥数,但 `_refreshFaceResolutions` **自己构造 payload 且三个字段一个都不带** ⇒ 分支永不触发。**根因是守卫形状**:5 条 Python 守卫直接 POST 完整 provider dict,**结构上看不见「payload 构造」这一步**。补两条 node 驱动真 shipped JS 的跨腿守卫后立刻转红(`KeyError: 'providerEnabled'`)。**判据:当缺陷可能落在「A 段发的 ≠ B 段读的」时,只测 B 段的守卫是结构性免疫的;必须有一条端到端驱动 A 段真代码。**
- **★ 凭证承诺与新需求的张力,解法是传计数而非明文:** 端点 docstring 明确承诺「凭证永不到达此处」,而 skip 需要知道「有没有可用密钥」。直接把 `api_keys` 塞进 payload 会破坏该承诺(且那是 UI→后端最敏感的一跳)。改传 `api_key_count` + `brand`——**回答问题所需的最小诚实事实**;并补反向 NEUTER(把真密钥塞进 payload → 泄露断言咬)。端点同时接受两种形状,因为直接传入的已存 provider dict 仍带 `api_keys`。
- **★ skipped 选择「标记」而非「静默跳过」,理由是可观测性不是行为:** 跳过会让客户端缓存 miss,而 miss 按设计**不渲染**——像素恰好是对的,但**原因是错的**,且未来想显示「已禁用」态时没有数据。标记后 `_faceChipHTML` 对 `r.skipped` 早退返回空串:**不建 slot 的条目没有 wire face 可标**,贴任何药丸都是前端无权做的路由声明(与琥珀假告警同类,只是更安静)。
- **顺带实测到一个真实事实:** 线上 `gpt-5.6-sol` 本来就是关闭状态,修复前它会被渲染成普通 `openai` 药丸,现在正确地不渲染。
- **★ 同型失误第三次(已入记忆):** `insert_content` 的 `content` 末尾又带上了锚点行 ⇒ 函数定义重复 ⇒ `IndentationError` 收集期崩。**两次都不是断言红而是文件坏,而 pytest 的收集期错误在 `-q` 下被日志噪声完全淹没**(grep `passed|failed` 得到空输出,看起来像「没跑」)。判据:`-q` 输出为空时先 `grep '^E '` 看收集期,不要重试跑法。
- **验收边界:** 纯后端端点 + 前端,**运行中进程不带**,需重启 + bundle 重建。真浏览器未实测(node 驱动 shipped 函数 + Quart test_client 端到端)。

### 2026-07-29(续·429 事件收尾:覆写清回自动) — owner 指令把今晚的手动覆写**提前**清回自动模式(原条目写的是「明天若健康再清」);**owner 同时抓出 fold 的一个复活陷阱**:清除必须打两个命名空间——只清账户那两行的话,面命名空间的死覆写会在下次重启被 fold「账户无值则迁移」规则原样复活(运行时状态操作,零产品码)

- **执行:** `key-override enabled=null` × 4(`sankuai::key_0/1` + `sankuai_anthropic::key_0/1`)。读回:磁盘 `overrides: {}`,fold 复活不可能;自动判定 key_0 **89%** / key_1 **94%** 成功率、exhausted=False,无需覆写自然启用。
- **key_2 按 owner 预言自然重停:** 23:47 task f0182d8c 在 opus-5 上撞 402,运行中的 c8e0ff98 把 `sankuai::sankuai_key_2` 记为 **key-wide** 熔断——这次落在了**账户命名空间**(UI 可见),代价恰是设计值:一次 live 402。
- **清除后 opus-5 仍在出流:** 23:53 两个任务在 `sankuai_key_1` 上 R9/R11 正常轮换,自动模式接管无感。
- **事件至此全链路闭环:** 诊断(三 key 死因各异)→ 解锁(手动开 key)→ 政策(429 永不禁,c8e0ff98 已在线)→ 显示(账户命名空间记账在线 + fold 待下次重启收敛)→ 卫生(覆写清回自动,key_2 自然停)。

