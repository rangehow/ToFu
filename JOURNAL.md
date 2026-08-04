### 2026-08-04(测试体系 P0-3 落地:浏览器主干道巡检 12 条——中止/会话恢复/侧栏/多轮/键盘/新会话/主题持久化/设置弹窗/上传 chip 九旅程入 e2e hermetic 车道,Makefile+CI 双缝收编) — epic `pt_2f2c847ff8524e5e` P0 全量收口;commit 见下(4 文件);旅程 **9+3 全绿**;collect-only **15642 零错**

- **形态(对齐业界 10-50 条关键旅程守闸惯例):** 骑 test_e2e_smoke 母版的 hermetic 合约(真 app+真 Chromium+stub LLM,session fixture 直接 import 复用零重复),每条=一个真实用户旅程打在 live DOM 上;全部 LLM 路径断言 stub 哨兵递进(patch-miss 即红,绝不拿真模型输出充数)。
- **abort 旅程的支撑件:** stub 新增 `__e2e_slow__` 慢流分支(60 词×50ms,逐词查 task['aborted'])——中止点击确定性落在流中段;断言按钮弹回发送态+已流出部分保留+流不回生长(failing-first 实证:无分支时 wait slow03 超时红)。
- **事故自纠(多轮等待误配):** 首版 `_send_and_wait_done` 用 `.some()` 等「任一 stub 回复」,第二轮被第一轮的回复瞬间满足→哨兵断言抢跑(`assert 1 > 1` 被擒);改 `expect_assistant=N` 按第 N 条助手消息等,跨轮误配结构性消除。
- **附带发现(不捎修):** 主题选择器是无渲染元素的死 UI——`.theme-option` 只有 CSS 与两处 sync 代码,无任何元素创建点,cycleTheme 仅挂在 window 白名单;主题旅程改走真实持久化环(cycleTheme→localStorage→reload 启动还原)。
- **P0 全量收口,epic 分解关票:** P0-1 哨兵/P0-2 结构化断言地板(8f3204f7)/P0-3 本批全部落地;P1(字段级契约 top-N)/P2(棘轮殡葬+flake SLA+按路径选择运行——unit 层 20min 实测已立项有据)拆为独立子票,母票 DONE。

### 2026-08-04(向导现代化最后一公里:商店工件换新——受控端 45,171,747B built d9b34c71 / 桌面端 120,152,343B built 959fd1c9(mirrored→built 翻转),选择器/盘面/哈希三点核对全过) — owner 复核指令;纯工件操作+JOURNAL,零产品代码

- **owner 复核擒获:** 模板现代化≠安装体验现代化——面板下载的商店工件仍是旧 MUI 向导(agent built 53,166,091B;full 甚至是 mirrored 的 CI Inno 117,946,479B)。用缓存 payload 直 wrap 入店:agent 走 payload-d9b34c71(=验收 epic `pt_59b62951aad2463e` 验证过的那份,30s),full 走 git 最新 payload-959fd1c9(116s)。**验收链注意:P4 现在下载到的是同一 d9b34c71 payload 的新向导版(45.17MB),验收步骤不变。**
- **三点核对(owner 指定,全实测):** ①manifest 两行 size/sha256/git_sha/source 已更新(agent built d9b34c71、full 由 mirrored 翻转为 built 959fd1c9);②`store.find_for_platform('windows','x86_64',kind=agent|full)` 双双解析到新 built 工件,面板下载信息路由(routes/api_v1/desktop.py:167)走的正是这个选择器;③盘上 exe size+sha256 与 manifest 逐字节一致。
- **大小注记:** 入店 full 120,152,343B 比 bench 版(d887b685 payload)大 46KB——payload 不同(959fd1c9 更新),预期内;agent 与 bench 差 296B(solid 块边界),无实质意义。

### 2026-08-04(测试体系 P0 补记:unit 层全量墙钟实测 **20 分钟**
### 2026-08-04(测试体系 P0 补记:unit 层全量墙钟实测 **20 分钟**(16 worker/负载 95 共享机)——超 15min 业界阈值,「按路径选择运行」从「先实测」升级为「实测支持」;96-worker auto 在本机被内存压力收割卡死;189 红分诊零本批指纹) — epic `pt_2f2c847ff8524e5e` 补记;commit `8f3204f7`(13 文件 +543/−25)

- **测量账:** `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -u -m pytest -p xdist -m unit -n 16 --dist worksteal` → **1198.5s(19m58s),15136 passed / 189 failed / 20 skipped**。结论:超 Google 惯例 ~15min 阈值 → P2「按路径映射的选择运行」实测有据(不上 ML);CI 干净 runner 应更快,但本地全量已影响迭代节奏。
- **96-worker 卡死教训:** `-n auto`(96 worker)在这台 uptime 8 天、load 95 的共享机上 40 分钟零输出——worker 群被内存压力静默收割(历史 OOM 判例的弱化重演)。本机测量/排障一律 `-n 16`;Makefile `auto` 在超载共享机上的这个失效形态值得记牢。
- **189 红三态分诊(运行期间兄弟落地 ≥4 批提交,树在动):** 53 前端红全是 Epic-E 拆分 churn——锚点抽取类套件钉的函数被搬文件(`_renderPeerDelivery not found`、`_restoreConvProject is not defined`);`expect_pass`/`__JSDOM_RESULT__`/`PASS assertions` 指纹全日志 **零命中**;我改门卫的 4 个 scene 套件(weather/time_of_day/perf/pixeldiff)全绿;契约套件红=已挂票 `pt_d42f32511279432a` 的预存红。**本批测试基建与全部 189 红无关。**
- **P0 收口状态:** P0-1 哨兵 + P0-2 结构化断言地板已提交生效(下批前端套件自动受 expect_pass 棘轮约束);P0-3 浏览器主干道巡检(10-20 条关键旅程,test_e2e_smoke 母版)为下一批开工点。

### 2026-08-04(安装向导现代化:经典 MUI2 灰向导退役——自绘 nsDialogs 扁平向导(整页烘焙位图+真标签坐 3DFACE 卡片) + /SOLID lzma;受控端 53.2→45.2MB、桌面端 ~153→120MB) — owner 指令「受控端和桌面端安装风格像 2000s,速度也要优化」;epic `pt_0a8543c795e34698` **DONE**;commit 见下(8 文件);新套件×2 + 重写 parity=**31 针全绿**;NEUTER 精确;环 **58+31 绿**;collect-only **15633 零错**

- **风格根修(架构决策,实测驱动):** 2000s 观感本体=经典 MUI2 向导(灰侧栏+MS Shell Dlg 8+逐文件日志面板)。conda nsDialogs **无控件着色能力**(无 SetCtlColors 插件/无 WM_CTLCOLOR 回调,strings 实证),32 位应用在本机 wine 必挂(SIGSYS 实测 probe.exe core dump)——运行时验证不可行,故选**零运行时机关**方案:每页=一张 wrap 期烘焙的整页位图(PIL,`lib/desktop_dist/installer_art.py`,承载产品名+版本,文字仅品牌短词)+ 真 LangString 标签坐在位图里 #F0F0F0(=COLOR_3DFACE 精确值)的卡片上,du 坐标两侧同源——DPI/CJK 对话框字制度量差异下位图拉伸与控件摆放**同比例**,永不错位(CJK 拉伸陷阱的结构性免疫)。标签字体 Segoe UI/雅黑按 $LANGUAGE 切换+DPI MulDiv;ManifestDPIAware。
- **速度两果(全部实测):** ①`SetCompressor /SOLID lzma` dict32——受控端 53.17→**45.17MB**(−15%)、桌面端 ~153→**120.1MB**(−21%),solid 单流解压安装也快;构建 8s→29s/119s(构建侧代价);②3316 文件逐条刷日志面板废除→marquee 进度条+状态短句。
- **顺手补两枚契约缺口:** ARP 注册表(Inno 白送、NSIS 旧模板从没写过——「应用和功能」里查无 Tofu)+ 运行中守卫第三插入点(DirPageLeave,用户改指到另一个在跑安装)。**/S 静默安装/卸载保住**(安装体抽 DoInstall,页面跳过时可直调);agent 自启契约平移:目录页默认勾选项(UI)+ silent 空句柄=ON(旧默认 ON section 语义),值名与托盘共享 parity 钉。
- **事故自记×2:** ①IntOp 单运算语义(`* 13 / 72` 编译炸)——makensis 编译闸当场擒获;②「无 Section 不给编译」——隐藏桩区段 `Section -hidden`/`-un.hidden`(warning 8000 即设计本身)。
- **测试账:** parity 重写 22 针(语义契约演进:MUI_FINISHPAGE_RUN→finish 页勾选+Exec;组件页→默认勾选+silent ON;美术契约改「Inno 经典位图 vs NSIS wrap 期整页图」)+ art/几何套件 7 针(**卡片-控件包含棘轮**:模板 du 矩形必须落在卡片内,NEUTER 移出即红)+ makensis 编译闸 2 针(双 target 真美术真渲染真编译,无 makensis 则 skip);collect-only 15633 零错。
- **验证边界(诚实账):** 像素级真机首验挂在 owner 验收链(pt_59b62951aad2463e 同闸)——本机 wine 32 位必挂,已尽编译闸+美术像素断言+PIL 布局仿真(四页合成预览)三层;商店工件**未动**(owner 面板点重建即吃新模板,payload 缓存使命中时 wrap ~30s)。CI Inno 侧保持经典向导=**有意的工具分叉**,parity 文档化。

### 2026-08-04(测试体系战略落地 P0-1+P0-2
### 2026-08-04(测试体系战略落地 P0-1+P0-2:skip 响亮哨兵 + jsdom 结构化断言地板——owner 拍板 docs/TESTING_STRATEGY.md;TOFU_REQUIRE_FRONTEND 三缝接线;expect_pass 棘轮基线 78 只降不升) — epic `pt_2f2c847ff8524e5e`;owner 复核拍板并两处修正;commit 见下(13 文件);新套件×2 **33 针**;哨兵 e2e 双路实证;回归抽样 **56 绿** + collect-only **15616 零错**

- **owner 两处修正(已纳入设计稿):** ①CI 已有 node 专用车道(ci.yml setup-node 注释早已自知静默 skip)——P0-1 从「CI 装 node」改为「skip 必须响亮」哨兵制;②审计挖出却漏计划的洞——run_harness 默认 min_pass=1 且 `output.count('PASS')` 子串计数(BYPASS/PASSword 都算 PASS),102 文件只 60 个显式申报,断言地板是虚的——列入 P0-2 结构化改造。
- **P0-1 skip 响亮:** `tests/_jsdom.py` 新增 frontend_required(TOFU_REQUIRE_FRONTEND=1/true/yes/on)/skip_or_fail/frontend_module_guard(收编 4 个 scene 文件的 module-level 手写门卫);conftest 新增 pytest_runtest_logreport+pytest_sessionfinish 哨兵——前端套件以 node/jsdom/npm/tsc 理由 skip 时,REQUIRE 车道全场 exitstatus=1(分类器 is_frontend_dep_skip 纯逻辑,数据条件 skip 不误伤);Makefile test-frontend 与 ci.yml frontend job 双缝注入环境变量。e2e 实证:合成探针 skip 在 REQUIRE=1 下「1 skipped」但 **EXIT=1**,无标记车道 EXIT=0 遗留行为不变。
- **P0-2 结构化断言地板:** `_jsdom_harness.js` report() 追加权威尾行 `__JSDOM_RESULT__ {"pass":N,"fail":M}`;run_harness 改 parse_harness_result(尾行优先,无尾行走 PASS 行锚定计数,子串计数废除)+ 新 kwarg `expect_pass` 精确语义(多报少报都红);棘轮 `test_frontend_harness_expect_ratchet` AST 扫描调用点,**基线 78(53 文件)只降不升**,新套件必须申报。failing-first 实证:新针首跑 collection error 红→实现后 33 绿;NEUTER×4(虚报多/虚报少/抹 FAIL 行/子串膨胀)全精确红。
- **本机怪癖(预存,非本批):** 无 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 时 pytest 启动即崩——pyproject addopts `-p pytest_timeout` 与 entrypoint 重注册(ValueError: Plugin already registered: timeout),Makefile 的 `-p no:napari` 路径在本机已失效;计时测量改走 DISABLE + `-p xdist` 显式加载。
- **预存红×2 挂票 `pt_d42f32511279432a`(均实证与本批无关):** ①test_frontend_backend_contract——api.js:1251 live-session 路径含两个插值,提取器在第二个 `${` 内的 `?` 截断留下 `live-session${refresh` 半插值,normaliser 不识→假 MISS(HEAD 三文件全干净实证);②ruff E401 test_frontend_scene_time_of_day.py:342 `import colorsys, statistics`(HEAD 预存,本地 ruff 0.15.15 擒获)。
- **共享树判例:** 我的战略调研 JOURNAL 条目未提交期间被兄弟 E4 提交(2383ae9a)顺走——内容无损,但「写完条目尽快提交」再添一例。
- **待办:** P0-3 浏览器主干道巡检(10-20 条关键旅程,test_e2e_smoke 为母版,接 release 闸);make test-unit 全量墙钟时间测量中(决定选择运行与否);P1 字段级契约;P2 棘轮殡葬/flake SLA。

### 2026-08-04(预存红闭环:test_frontend_brain_tool_render 17 红——方向对齐 Epic-E 拆分(测试侧):harness 改 eval core+rich 双真源(拼接单次 eval 复现浏览器共享全局词法作用域),_nc 双 neuter 自动定位锚点所在文件) — 脑派发接我自票 `pt_9ef7b2ff38d24fea` **DONE**;commit `bc20e0d3`(1 文件 +56/−23,纯测试侧);套件 **17/17 绿** + 邻接环 7 绿

- **定案(测试漂移,与 restart_smoke/health_endpoint 同族):** 套件钉的是 2026-08-01 前的单文件布局——fcddc420(Epic-E sub-4)把全部 conv-meta 结构化渲染器迁至 DEFERRED `tool_rounds_rich.js`,core 只剩 dispatch + `_CONV_META_TOOLS` + `_webToolSvg`,NC 锚点全数落空报「anchor not found」。stash 实证纯净 HEAD 同 17 红,与 peer-row 批无关。
- **修法(零产品代码):** ①harness argv[2] 改 JSON 源列表,core 先 rich 后(bundle 序)拼接成**单次 eval**——关键坑:逐文件 eval 会把 core 的顶层 `const _CONV_META_TOOLS` 困在随 eval 结束即弃的词法环境里,rich 调用期读不到;拼接单次 eval 精确复现浏览器两 script 共享全局词法作用域的语义;②`_nc` 自动定位锚点所在文件(NC 锚点横跨 core/rich 两侧——glyph/_CONV_META_TOOLS 在 core,渲染器/折叠策略在 rich),patch 副本替换运行,断言两份船载文件字节不变。
- **测试账:** 主测试(103 针 PASS 断言)+ 16 NC 全绿(64s);邻接环 peer_inject_row_layout + conv_reducers_extracted 7/7;collect 21 枚零错。

### 2026-08-04(E4 订阅适配器 sidecar 全链落地:CLIProxyAPI 看护器+loopback 中继+adapter provider+传输链+设置页卡片;真机冒烟 v7.2.116 下载→启动→/v1/models 200、错 key 401;epic `pt_728134281fb841c3` **DONE**) — 脑派发接我自票;commit 见下(19 文件);环 **102 绿** + failing-first 干净 HEAD 实证 17 红 2 err

- **三分工:** 安全主干自做(agent 看护器/egress loopback 管线/服务器适配器层/路由),传输链与前端卡片两子代理并行(接口契约先冻结,零文件交叠)。
- **agent 侧 `lib/desktop_agent/_adapter.py`(519 行):** 首启从 GitHub Releases 下载(版本钉/`checksums.txt` SHA-256 校验,不符绝不换二进制)+ `host:127.0.0.1`+随机 api-key+管理端点 secret 的 config.yaml+崩溃看护(指数退避)+周检更新(防边缘重演漂移,owner 硬要件)+agent 重启 resume(policy.json);`target='loopback'` 白名单类=**agent 自身策略端口**(被攻陷的服务器也无法把中继指向任意本地服务)。**真机冒烟擒获真 bug**:tar 内二进制名是 `cli-proxy-api`(连字符),startswith('cliproxyapi') 提取失败——修复并钉针(实证优先于文件快照判例再添一枚)。冒烟全绿:下载校验→spawn→health→对 key 200/错 key **401**(攻击面要求活体验证)。
- **服务器侧:** `lib/desktop/adapter.py`(策略库——api-key/mgmt secret **服务器铸**(provider 调用必须持有,满足「随机/按 agent 存/不裸奔」)、relay_http/relay_stream(pin 单 agent,**绝不走 fallback 链**——别机即别钥)、ensure 后台编排 600s TTL、managed provider `adapter_<id>` 供应/撤销)+ 路由 `/api/v1/adapter/{status,ensure,stop}`(charter#0 信封,BadRequest 先捕防 500 吞 400);egress.py 双端 `_check_target` 管线。
- **传输链(子代理):** adapter 标记镜像 oauth 全链——dispatcher:356/612 读卡→Slot.adapter→api.py 四处传递→stream/astream/chat 分支(astream 全支委托 asyncio.to_thread,不冻事件循环;EgressUnavailable→EndpointUnreachableError 走模型回退)+ provider_probe 探测。14 新检。
- **前端卡片(子代理):** OAuth 区订阅适配器卡(状态徽章/启动停止/ensuring 加速轮询/ready 成功行/16 i18n 键);**i18n.js 被兄弟 a135e73a 扫入其提交**(共享 HEAD 惯例,内容逐字一致,证后收编)。62 检。
- **测试账:** 环=agent 17+server 15+transport 14+egress 34+前端卡 3+egress_line+i18n 覆盖+契约棘轮 6=**102 绿**;failing-first 干净 HEAD worktree 17 红 2 err(模块/缝缺席=正确理由)。
- **验收边界:** 端到端「登录→聊天」需真订阅账号在本机浏览器登录适配器(结构性人门);本批已实证到「适配器在线、API 鉴权正确、provider 可供应」。

### 2026-08-04(peer 注入行 UX 四修:来源气泡可点击跳转到来源对话 + 单发送者去重(头/体不再重复同枚气泡)+ 标题加宽 220→420px 全题可读 + 主体缩进 30→16px) — owner 截图四指令(来源重复/标题不全/不支持跳转/左边距大);commit `a135e73a`(5 文件 +134/−15);套件 **20/20 绿**(peer-inject 4 + conv_reducers/auto_translate/dup-bubble 邻接 16);预存红 17 枚实证无关并挂票 `pt_9ef7b2ff38d24fea`

- **定案(owner 截图四问):** ①来源对话重复——header 气泡与 body 卡片头是同枚气泡,单发送者注入(常态)重复渲染;②标题省略号——气泡 max-width 220px 且 tooltip 只有「conv id」毫无信息;③不支持跳转——气泡是纯展示 span;④主体左边距 30px 偏大。
- **修法:** `_peerFromBubble` 改 `<button data-conv-jump>`——委托点击处理器(tool_rounds.js 既有 ptool-turn-head 同档)经新增 `convFullIdById`(conv_reducers.js,与 convTitleById 共享 `_convFindById` 全等+唯一前缀匹配)解析全 id → `loadConversation` 跳转,解析不到给 toast;tooltip=完整标题+conv id+跳转提示,头部帽加宽至 420px,hover 态+↗ 跳转箭头传达可点。`_renderPeerInjectRow` 去重:distinct sender>1 才保留逐卡气泡。`.sw-inbox-row-body` 缩进 30→16px 对齐 header 图标列(swarm inbox/steer/stall 同族共享此规则,一并受益)。light/tofu 主题补气泡配色(原为 dark-only #82aaff)。i18n 三键(peer.jumpToConv/convNotFoundTitle/convNotFound)。
- **测试账:** peer_inject_row_layout 套件钉:跳转按钮形态(data-conv-jump+<button>)、tooltip 全标题+提示、单发送者 body 无重复气泡、多发送者 body 保留逐卡气泡、PASS 棘轮 13→15、source guard 扩三针;NEUTER 原三针全保绿。node --check×3 过;collect-only 15585+1 flake(test_jsdom_runner_structured 独跑 27 枚正常收集,判例内 flake)。browser_preview_page 双截图实证:tofu 主题下全题显示、body 无重复气泡、缩进收紧。
- **预存红挂票:** test_frontend_brain_tool_render 17 红——NC 锚点钉 tool_rounds.js,但 `_renderPeerDelivery` 已在 fcddc420(2026-08-01 Epic-E 拆分)迁至 tool_rounds_rich.js;stash 我全部文件后同 17 红实证与批无关,票 `pt_9ef7b2ff38d24fea`(方向对齐测试侧)。
- **生效面:** 前端走 bundle mtime 自愈重建,刷新即生效。
### 2026-08-04(侧栏事故根因层加固落地:语法二分降级——闸失败不再全包拒服,按文件归因后排除坏源重建;renderMessage 进能力断言名单,静默残废从此有红横幅;三枚静态棘轮进 CI) — owner 复核指令「不能只修实例,补上这一层」;commit `b280b157`(4 文件 +380/−102);环 **12 套件 86/86 绿** + 真实树构建冒烟过

- **定案(owner 擒获的结构性缺口):** 既有 `test_bundle_corruption_guard.py` 的设计承诺是「一个源文件损坏 → 该模块缺席,不是整页死;bundle 拒服后降级逐文件加载=更慢但能用的页面」——本次事故证明承诺对「语法级损坏」不成立:`_scan_source_corruption` 只认 git 冲突标记/NUL 字节,认不出中断编辑留下的括号失衡;于是整包闸拒服 → 全员跌入 dev-fallback → 同一个坏源文件被原始服务 → 两条路全死。
- **修法(js_bundler):** 新增 `_find_syntax_broken_sources`——按文件 `node --check` 归因,**只在整包闸失败时运行**(健康构建零成本);`_assemble_bundle` 尾部重写为两轮发布循环:闸失败 → 二分归因 → 排除坏的**非关键**源文件重组装重闸(降级契约与扫描器对冲突标记的既有契约同构);critical 文件语法坏/排除后仍失败(跨文件胶合 bug)维持拒服走 fallback。esbuild 失败路径自然收编(本就 fail-open 到 _minify_js,闸是唯一收口点)。
- **修法(index.html):** `_loadBearingCaps` 补 `renderMessage`——本次事故的「静默」半边根因:脚本全 load、main.js 正常 boot、四能力全在,守卫全程绿灯,而渲染函数缺席让每次点击抛错。此改动读盘即生效(无需重启)。
- **棘轮(新 tests/test_bundle_source_syntax_ratchet.py):** ①全部 ship 源文件(_BUNDLE_FILES+_DEFERRED_FILES ~160 枚)过 node --check(8 线程池 ~2s,node 缺席 skipif 与既有哲学一致);②index.html script src 唯一(忽略 query——经典脚本二次加载顶层 const 必死,fallback 专用地雷);③能力断言名单钉住 renderMessage(未来 defer/改名即红)。
- **既有套件方向对齐:** `test_node_gate_rejects_broken_bundle`(钉旧契约「语法坏→全包拒服」)按 owner 新契约改写为「非关键→排除重建」;新增 critical 坏→致命、NEUTER(二分被阉→退回致命,证明降级确实由二分带来)、排除后仍失败→拒服三针。temp-tree harness 复用,esbuild 强制关闭保持确定性。
- **生效边界(诚实账):** js_bundler.py 的加固需服务器重启才在线生效(当前生产 bundle 本已健康,无 urgency);index.html 能力断言即刷即生效;棘轮即进 CI。
- **验证账:** collect-only 18 枚零错;目标双套件 18/18;bundle 家族环(manifest parity/freshness/concurrency/nonblocking/stale-self-heal/loadguard-deferred/js-coverage 等 12 件)86/86;真实树 `build_bundle()` 冒烟产出 bundle-83e95b3b.js 且 node 闸过(esbuild 路径实证)。

### 2026-08-04(设置页模型卡「乱序」根修:排序键=卡片实际渲染的裸 model_id
### 2026-08-04(设置页模型卡「乱序」根修:排序键=卡片实际渲染的裸 model_id,不再是 pricing 注册表的隐形友好名——claude-fable-5 被 "Fable 5" 拖到 Doubao 与 gemini 之间,机器字母序、读者乱序) — owner 截图问「为什么模板没按模型名字母序排?claude-fable-5 为何在 d 下面?」;commit 见下(2 文件 +JOURNAL);套件 **11/11** + 守卫环 13 绿,failing-first 实证,api_contract 红实证预存

- **定案(证据链闭合):** 设置页 `_renderModelCard` 渲染的是裸 `m.model_id`,而 `_compareModelEntries`(core_panel.js)把冷排序/二分插入委托给了 `_compareModelsByDisplayName`——其排序键 `_modelShortName` 优先取 pricing 注册表名字:`lib/pricing/_tables.py:37` 给 `claude-fable-5` 起名 **"Fable 5"**($9.94/$49.72 与截图价格吻合)。排序结果 deepseek < doubao < "fable 5" < gemini——与截图逐像素吻合。列表其实排了序,只是按一个卡片上根本不显示的字符串排。
- **根修:** `_compareModelEntries` 改按裸 model_id 排序,复用 branding.js 共享的 `_MODEL_NAME_COLLATOR`(numeric 感知 + 大小写折叠,claude-opus-4.10 排在 4.6 后),branding 缺席(陈旧 bundle)回退小写比较。友好名比较器不动——它仍是工具栏 picker/预设页/默认模型下拉这些**渲染友好名的列表**的正确键。`_insertModelSorted`/`_coldSortModels` 自动跟随(discover/template-sync/手加模型同键)。
- **测试账:** test_frontend_model_picker_order 原第 6 检钉的正是 buggy 契约(「settings 冷排序与 picker 一致」)——方向对齐重钉:冷排序按裸 id 全序钉死 + 两枚事故针(`settings_pricing_name_does_not_move_card` claude-fable-5 必须坐 index 2 归 'c';`settings_gateway_id_sorts_where_shown` yuju-… 归 'y');静态守卫反转:core_panel 必须走共享 collator 且**禁止**再引友好名比较器/_modelShortName;消费者一律禁自建 Intl.Collator。failing-first:stash 产品文件后新针精确红,恢复全绿。事故自记:守卫断言 `'_modelShortName' not in core` 第一版被我自己注释里的字面量骗红——与 icon.run() 判例同族,注释措辞避开受检标识符。
- **环:** 本套件 4 + local_presets 4 + template_sync 3 = 11 绿;bundle manifest + access_matrix 13 绿;test_frontend_api_contract 红在纯净 HEAD 同签名复现=预存,与本批无关。
- **生效面:** 纯前端,设置页下次打开冷排序即按裸 id 重排;bundle mtime 自愈重建,无需重启。

### 2026-08-04(测试体系战略调研:业界怎么做×我们家底盘点×防负优化方案——三路 swarm 实证;结论=骨架已对齐业界,真缺口是「真浏览器主干道巡检+jsdom 静默 skip+棘轮治理 SLA」) — owner 战略问「项目已超大,前后端分开查 bug 和集成 bug 都难,大项目怎么做测试?防测试负优化」;纯调查,零产品代码

- **家底盘点(子代理实测):** 1,581 测试文件、**15,528** collect-only 实测(87.7s);单元~950/集成 API~40/守护棘轮~113(91 个 parity)/前端 jsdom 族 **467**/真浏览器 E2E 仅 4。前后端缝已有轻量契约守卫(test_frontend_backend_contract 静态抽 api.js 路径对 live url_map)——恰是 Fowler 认可的 monolith 场景 Pact 廉价替代。CI(.github/workflows/ci.yml)+ Makefile + conftest 自动打标兜底齐备。
- **业界结论(全部一手源):** Google 70/20/10 金字塔 + S/M/L hermetic 分级;Dodds 奖杯「mostly integration」;Google/Meta/微软测试选择(Taming/Meta arXiv 1810.05286/Herzig ICSE15:~50% 测试可跳过);flake 隔离+SLA+删除;覆盖率只作信号不作闸;Pact 在单消费者 monolith 反噬。
- **三个真缺口(按 ROI 排序):** ①CI 无 node 时 467 个前端套件**静默 skip**(skip-gated)=前端防线可整体消失而无人知晓;②真浏览器关键路径巡检太薄(4 vs 467,业界惯例 10-50 条关键旅程);③棘轮族无治理 SLA——日志「预存红闭环」系列反复出现「测试钉死旧契约→方向对齐」,正是 Dodds 警告的 implementation-detail 负优化形态,需定期殡葬审计。
- **明确不做(防负优化清单):** 不追全仓覆盖率数字、不上 Pact、不上 ML 测试选择(先量全量跑墙钟时间,>15min 才做路径映射选择)、不把 E2E 扩成主防线。
- **待 owner 拍板:** 方案若批准 → 落 docs/TESTING_STRATEGY.md + 挂 epic 分 P0(补 CI node 闸+10-20 条浏览器巡检)/P1(字段级契约钉 top-N 端点)/P2(棘轮殡葬审计+flake SLA 制度化)。

### 2026-08-04(强刷后侧栏点击全死:owner 报障定案——非慢同步,是兄弟中断编辑的语法残骸 + fallback 重复标签补刀;最小修复保兄弟功能,兄弟复工后自行提交闭环) — owner 报障「强刷后点侧边栏没反应」;commit `076d1183`(index.html);兄弟 `338b9d1e` 收编 chat_render.js 修复

- **诊断链(error.log 客户端三连错):** ①`chat_render.js:2263 Unexpected end of input`;②`stream_reducer.js:1 _TERMINAL_ROUND_STATUS already declared`;③`renderMessage is not defined ← showStreamingUIForConv ← loadConversation ← _handleConvClick`——③即「点侧边栏没反应」直接现场。SyncDrift STALLED 系背景噪音,排除。
- **根因双层:** 兄弟 mse9r2ir7ql0v4 一次中断编辑在 chat_render.js:1349-1350 留下两行 `function renderMessage(msg, idx) {`(多一开括号);esbuild 产出残包→打包器语法闸拒服(闸正常)→降级 dev-fallback→撞同源坏文件 + index.html 里 stream_reducer.js 重复 script 标签(classic script 顶层 const 二次执行必抛)。
- **处置:** 删重复签名行(兄弟卡片功能原样保留)→ bundle mtime 自愈重建实证(531d1ecc,200/1.07MB/语法 OK)→ 删 index.html 重复标签单独提交 → peer message 移交兄弟 → 兄弟复工先验证残骸已清再走完全程(47 针新套件+169 环)自行提交 `338b9d1e` 闭环。全量 static/js 语法扫描 + index.html 标签 uniq -d 扫描零残留。
- **判例沉淀:** 共享树未提交中断编辑=生产即坏;语法闸保得住 bundle 保不了 fallback——fallback 模式逐文件加载,源文件坏即全坏,且重复标签这类静默负债只在 fallback 才显形。

### 2026-08-04(大脑派发消息溯源卡片:_brainEpic 元数据贯通
### 2026-08-04(大脑派发消息溯源卡片:_brainEpic 元数据贯通——创建者可点标题/派发方式/路由原因三要素上屏,原文收进折叠 details;兄弟移交修掉我中断编辑的语法残骸) — owner 截图指令「项目大脑消息要显示哪个对话创建的 epic(可点击标题而非裸 id)、怎么派的、为什么派给我,样式再好看些」;commit `338b9d1e`(7 文件 +923/−7);新套件×2 **47 针**;回归环 **169 + bundle 29 + i18n 65 全绿**

- **数据链(三处贯通,display-only 不进模型):** `dispatch_epic` 队列载荷新增 `_brainEpic`(epicId/epicTitle 截 300/originatorConv+DB 解析标题/method/route/answered);`message_queue` 持久化缝传播到 user 消息(前端从 conv 行渲染,与 `_boardTaskId` 同缝)。**method** = 哪个缝派的:四事件缝各自 stamp `_via`(dependency_done/answered/posted/conv_idle),sweep 走 heartbeat 默认,未知 token 回落 heartbeat(防打错缝名裸泄)。**route** = 为什么是我:按板面自有字段推导(dispatch_target override ≠ created_by_conv→migrated;等于创建者→creator;否则 fallback),不新造数据源。签名零改动——`_via` 骑 epic dict 传入,既有 monkeypatch lambda(config=None) 全部不破。
- **前端卡片(_renderBrainDispatchCard,对齐 buildCompactionCardHtml 独立可测模式):** 脑紫渐变卡——epic 标题两行截断点击开大脑面板(openProjectBrain)、创建者标题 loadConversation 链接(**创建者=本对话时纯文本「本对话」不尬链自己**)、方式/原因双语标签、answered 绿徽章;未知 method token 原文兜底不崩。派发原文(英文指令墙)收进 collapsed `<details class="brain-kickoff-raw">`——**legacy 无 _brainEpic 不渲染卡片(不可伪造溯源)但原文仍折叠**(墙是同一堵墙),这是我测试首版断言写反而代码行为更优的定案(改测试不码)。
- **测试账:** 后端 `test_brain_dispatch_provenance` 13 针(route×3/method 五态/answered/标题截断模型侧仍全量/标题解析败不炸派发/真实 enqueue 载荷/真实 drain 持久化/四缝标记各一针/NEUTER 传播行精确红);前端 `test_frontend_brain_dispatch_card` 34 针(node harness 骑真实 chat_render——卡片全要素/自指无链/迁移+答复徽章/未知 token/legacy 无卡仍折叠/XSS 转义/未命名兜底/EN 语言/NEUTER 卡片消失)。事故自记:①post_task 内部即触发 on_epic_posted 即时派发,手动 dispatch 被占先——镜像集成测试 _mark_busy 技巧(发布时占住 conv 逼缝退让);②i18n 键族/bundle 族守卫首跑全绿无需对齐。
- **兄弟移交判例(共享树中断编辑残骸):** 我一次编辑被中断,chat_render.js 留了重复 `function renderMessage(msg, idx) {` 行(parse 全死,生产侧栏点击全灭);兄弟会话 msealdie 最小修复(删重复行,功能代码原样保留)+ 重建 bundle,并收编 index.html stream_reducer.js 重复 script 标签。我的复工第一动作=node --check + grep 验证残骸确已清,再继续测试——**共享树被移交后先验证再动工**的判例又一桩。
- **生效面:** 后端即重启即生效(下一条派发即带 _brainEpic);前端走 bundle mtime 自愈重建。存量历史派发消息无 _brainEpic,维持旧观(仅原文折叠生效)。

### 2026-08-04(订阅增强 E1+E2+E3 落地:漂移对拍守卫上线 + cloaking 常量同步 2.1.220/codex-tui/platform.claude.com + dispatcher 三果实 + 多 agent 有序 fallback;E4 适配器 epic 上板 pt_728134281fb841c3) — owner 复核设计稿后拍板实施(四点修订: S2 行陈腐更正/登录坍缩红利/适配器攻击面/更新通道);commit 见下(17 文件);环 **100 绿** + collect-only 15528 零错
### 2026-08-04(订阅增强 E1+E2+E3 落地:漂移对拍守卫上线 + cloaking 常量同步 2.1.220/codex-tui/platform.claude.com + dispatcher 三果实 + 多 agent 有序 fallback;E4 适配器 epic 上板 pt_728134281fb841c3) — owner 复核设计稿后拍板实施(四点修订: S2 行陈腐更正/登录坍缩红利/适配器攻击面/更新通道);commit 见下(17 文件);环 **100 绿** + collect-only 15528 零错

- **设计稿修订(owner 四点):** S2 OS 代理发现早已实现(_egress.py:72-92,我误标 TODO);§4.4 补「登录链整个坍缩」红利(适配器自带本机 OAuth 回调,三级登录兜底+刷新 singleflight 对托管订阅整体删除)与适配器攻击面(随机 api-key + management 端点锁)与更新通道(周检 release+哈希换二进制,否则漂移在边缘重演);O1 分发 owner 拍板=首启下载+钉版本+哈希+周期更新。
- **E1 漂移守卫(failing-first 实证 7 红→绿):** 新套件 `test_oauth_cloaking_drift.py` 10 检——直接解析 ../CLIProxyAPI 的 Go 常量(UA 版本/Stainless 四件/token_url/scope/client_id/originator/beta 线序/盐+cch 分支)与 tofu 对拍,漂移即红且报错带同步指引;参照仓库缺席 skip(CI/开源导出无碍);NEUTER 自检比较器必咬人。常量同步:2.1.63→**2.1.220**、codex_cli_rs→**codex-tui**(UA 换新格式)、token 端点 console.anthropic.com→**platform.claude.com**(双白名单+oauth 探测端点同步补)、beta 基线重排 2.1.220 线序(11 枚+has_tools 条件插 advanced-tool-use;淘汰 structured-outputs/fast-mode/token-efficient-tools 出基线)、补 Stainless 画像四件(0.94.0/v26.3.0/MacOS/arm64)。**dangerous-direct-browser-access 证据翻转**:CLIProxyAPI 2.1.220 活捉套件带此头,我旧注释「真 Claude Code 不发」是未测假设——按证据发改。
- **E2 三果实(子代理实施,21 检):** ①Codex `usage_limit_reached`/`at capacity` → 定时冷却(llm_errors 解析 resets_at/resets_in_seconds,钳 1s-24h;slot.record_error 收 cooldown_s;与 80431312 的 429 无限重试分通道不互撞);②oauth slot 401 → 强制刷新一次+重试一次才回退(_force_oauth_token_refresh,per-request 单次闸,非 oauth slot 不动);③404/422 晋请求级错误(RequestScopedError,release 不进冷却不耗回退)——400 本已请求级,钉守卫。
- **E3 多 agent fallback(6 新检):** route_request 拆出 route_candidates(pin 领先→最近成功→last_seen;未 pin 不再报错——旧「必须去设置页 pin」契约废除);egress_http 桥失败/agent 网络失败换下一台,**已送达 HTTP 错误(500 等)绝不换机**(防双计订阅配额);open_stream 仅零帧(pre-meta)才换机,有帧无 meta 宁报错不 retry(同防双计)。failing-first 在干净 HEAD worktree 实证 10 红 5 err→本批全绿。
- **测试账:** 七套件 **100 绿**(drift 10 + cloak 26 + outbound 10 + egress 33 + quota 10 + 401 4 + request-scoped 7);collect-only 15528 零错;e2 邻接环 306 绿(test_error_transparency_guard 红为预存,纯净树同签名)。事故自记:route_candidates 引 threading 早于中段 import→NameError 当场被套件擒获,提顶修复(教训重申:模块级常量引名先查 import 时序)。
- **未竟(结构不可达,列入 P4 真机验收):** 常量同步后「上游真收货」实测需经 egress agent(本服务器 geo-block 且无在线 agent);E4 适配器 epic `pt_728134281fb841c3` 已上板待实施。

### 2026-08-04(预存红闭环 + MCP per-tool 开关落地:update_search_settings 审批 enricher 补位(双红同族);MCP 工具从「全有或全无」到按工具勾选——桥过滤/热更新/调用拒绝三闸) — 脑派发接我自票×2 **DONE**;commits `8685385a`(enricher,3 文件)+ `75a56630`(MCP,8 文件 +641/−5);新套件×2 **22 针** + 环 **235+93 绿**

- **① 预存红 `pt_eb0251bb1cbe4a27`:** 搜索设置批(eb315d4b)把 `update_search_settings` 列入写分区却漏了审批 enricher——TestApprovalEnricherRatchet + approval CASES 覆盖双红(纯净 HEAD worktree 实证)。修法=一枚 enricher(逐 knob 列「设置项=新值」,无参纯读明示无变更)+ CASES 一行 + 台账重生成(gap 归 0)。97/97 绿。
- **② MCP per-tool 开关 `pt_53065dbe86bb4286`(browser v2 拆出的 Phase 2):** 本部署 ~190 个 MCP schema 全有或全无。现每服务器配置行携带 `disabled_tools`——禁用工具同时三闸:**藏**(get_openai_tool_defs/get_tool_safety 过滤,schema 瘦身即时生效)、**拒**(call_tool 对陈旧历史/在飞调用抛「disabled by user」)、**存**(JSON 行持久,config 迁移原样保留——钉了 overleaf 迁移场景)。PUT `/api/v1/mcp/servers/<name>/tools` 全量替换语义 + `set_disabled_tools` 热应用免重连 + 闩锁失效。设置页连接卡工具数徽章变展开按钮(勾选面板,乐观更新失败回滚),徽章/头部计数显示启用/总数。
- **刻意不做(定案记录):** MCP 长描述**不截断**——那些「小作文」装着承重使用契约(stop_job 的 runid-vs-runId 警告就在长文里),截断=静默劣化工具调用正确性;按工具勾选才是安全的瘦身(禁用工具 schema 整体出请求,省得最多)。
- **测试账:** test_mcp_tool_toggle 10 针(过滤/安全表同步/跨服同名隔离/默认全开/假桥无 _configs 兼容/调用拒绝/热更新往返/输入归一/配置回环/迁移保留)+ test_frontend_mcp_tool_toggle jsdom(徽章按钮形态/a-b 计数/面板行/全量名单 PUT——集合比较/重启用 + NEUTER 删面板渲染精确红);环=32 开关族 + 235 MCP 全家 + 93 registry/i18n/api-contract。
- **事故自记:** ①jsdom harness 断言把 PUT 名单当有序数组,实际行序语义——后端本就 sorted(set) 归一,断言改集合比较;②`import routes.api_v1.mcp` 直接导入撞已知 `@push_bp.websocket` AttributeError(环境预存,profile 明记可忽略),路由冒烟改走 py_compile+parity 套件。
- **生效面:** 后端即重启即生效;前端走 bundle mtime 自愈重建。用户在 设置→MCP→连接卡「N 个工具」按钮处按工具勾选。

### 2026-08-04(视频 epic 关票:P2 问题 owner 裁定「不做厂商专属」——视觉解说词接力落为模型无关答案:管线期一次批量视觉调用把帧条讲成 storyboard,纯文本聊天模型也能读视频) — epic `pt_6aca988757cb4019` **DONE**;commit `7384aa31`(11 文件 +304/−20);套件 **41/41** + 守卫环 **97 绿**

- **owner 答问定案:** P2 挂板提问「Gemini 原生直通怎么做」,owner 答「一定要用 gemini 吗?用户有什么模型用什么模型不可以么?」——裁定**不做厂商专属原生路径**。审视 P1 后擒获真缺口:帧通道只服务视觉聊天模型,**无视觉聊天模型丢整个视觉信道**(哪怕槽位池里有视觉模型)。
- **修法(VideoAgent 分解,零新依赖):** `lib/video_analysis/_caption.py`——管线期(非发送期)一次批量视觉调用 `dispatch_chat(capability='vision')` 把稀疏化帧条讲成「逐帧 `[MM:SS] 画面内容 + Overall 弧线」文本 storyboard,存进耐用净载;发送期 transform 二选一:视觉模型→原始帧(storyboard 抑制,NEUTER 针实证),纯文本模型→storyboard+转录。管线期生成=storyboard 是(视频×视觉池)的属性而非聊天模型的属性——多轮/预览/compaction 永不重复烧钱,发送零延迟零成本。
- **小重构:** `_thin_frames`/`_fmt_video_ts` 移入 `_frames.py` 单一定义(`_transform` 再导出保兼容);turn_builder 白名单收 storyboard/storyboard_model(status 属登记处元数据,针住不入净载);杀开关 `TOFU_VIDEO_STORYBOARD=0`。
- **测试账:** +8 检(env 杀开关/无帧/无视觉槽/消息形状[帧图+时间戳对、指令殿后、预算稀疏]/派发失败降级/transform 接力/NEUTER 抑制/净化键);e2e 管线更新(fake storyboard 防真网络);环=装配+契约+音频+server_async 97 绿。
### 2026-08-04(Opus 5 全链定案+探测假绿根修:AIGC 网关 400 说中文「不支持的模型类型」,`_classify_status` 的 200/400→ok 阶梯抢在体嗅探前面——vertex./aws. 死路由全显绿,诱导 owner 禁掉唯二能用的裸 ID+yuju) — owner 两连问(会话 mse7x3fr 为何 400 / 探测为何显绿);commit `04f1706a`(2 文件 +34/−2);探测环 **53 绿**;活网关复测实证死路由翻 not_found

- **事故全链(实测定案):** ①网关只认 `claude-opus-5` 裸 ID 与 `yuju-…-evaDaily`(探针 429=路由存在),`vertex./aws.claude-opus-5` 是确定性 400「不支持的模型类型」;②13:29 设置页探测把死路由全标 ok(detail 就是「HTTP 400」),yuju 反被 429 标 recommend_disable;③13:30:05 配置保存:3 key 全禁用裸 ID+yuju,只剩两条死路由;④13:30:23 会话 59e5a896 三 slot 全 400 → fallback kimi-k3(用户看到「我是 Kimi」)。
- **假绿根因(lib/provider_probe.py):** `_classify_status` 先判 `code==200|400→ok` 再嗅体,且 not_found 嗅探只认英文三词(model_not_found/does not exist/no such model)——AIGC 用 400+中文报死路由,双保险全落空。同病还有 `LongCat-Flash-Omni-2603`(key2,ok/HTTP 400)。
- **根修:** `_ROUTE_MISSING_MARKERS`(英文三词 + unsupported model/unsupported_model/model not supported + 不支持的模型类型)提到状态阶梯之前——任何状态码带死路由体即 not_found;plain 400 仍 ok(既有针保);顺带愈「200 带 model_not_found 体」潜例。probe_one_cell docstring 同步。
- **测试账:** test_probe_cells +2(事故原文体 400→not_found / 英文 unsupported model→not_found);环=probe_cells+scoped+nonchat_skip+oauth **53 绿**。活网关实证:vertex./aws.claude-opus-5 经修复分类器 → not_found。
- **遗留(owner 决策):** server_config.json 里 opus-5 的 key_access 禁用仍未翻转(等 owner 拍板是否有意);探测缓存盘上假绿格要下次设置页重探才刷新;fable-5 同款「裸 ID 全 key 禁用」形状待 owner 顺手核。
### 2026-08-04(收官抛光:最高频失败文案双语撞动词根修——`reason.unreachable` 改「服务器无响应 / no answer from the server」,shell/reason 缝不再复读;compose 断言钉成类防回潮) — owner 复核末项指令;commit 见下(3 文件);套件 **24/24**(ComposedCopy +3),NEUTER 一针精确,环 **213 绿**

- **定案(owner compose 验收擒获):** `unreachable` 是探测/配对失败的主力 token(服务器没开/端口错/隧道死),其拼合文案是产品里被读次数最多的错误句,而清扫批选的短语与外壳动词撞车——en「Cannot reach Tofu there: **the address cannot be reached**.」/ zh「连不上服务器:**无法连接到该地址**。」,verifyFailed 与 pair.failed 双壳双语四句全拗口。根修=一处:`desktop.reason.unreachable` 改不重复外壳动词的说法(en `no answer from the server` / zh「服务器无响应」)——拼完四句全通顺(Cannot reach Tofu there: no answer from the server. / 连不上服务器:服务器无响应。)。
- **同类排查:** timeout/error/not_tofu/bad_response/http_ 族 compose 无病(短语均不含外壳 reach/连/配对 动词)——钉成 `test_other_reason_tokens_compose_without_the_same_disease`,防的是「下一个 token 又犯同病」而非这一处。
- **测试账:** ComposedCopyTest +3——短语钉死(owner 亲定措辞)、四句 compose 禁含 `cannot be reached`/`无法连接` 且理由不空、兄弟 token 无同病;NEUTER 一针(还原文案→三针全红)。native_i18n 套件 21→24。
### 2026-08-04(原生面 i18n 系统性清扫:owner 复核驳回「i18n 全量落实」——lib 层全 token 化 + UI 边界唯一三映射(reason_text/connect_error_text/component_msg) + token 普查棘轮;顺手擒获 owner 清单外进度文案/终端兜底两族) — owner 复核指令五条;epic `pt_a28866376e614375`;commit 见下(7 文件);新套件 **21 针**,NEUTER×6 全精确;环 **256+196=452 绿**;zh 渲染实弹演示八连全中

- **定案(owner 抽查五漏,全部实证):** ①`parse_connect_line` 三条英文 ValueError 被对话框 `str(ve)` 原样上屏;②probe/pair 机读 token('unreachable'/'http_404'/'not_tofu'/'bad_response')被 `.replace('{reason}', val)` 裸渲进中文句;③`size_hint='~115 MB download'` 英文属性拼进组件卡;④`install()`/progress 全系英文消息('Chromium browser installed successfully.' 等)进度视图原样渲染;⑤托盘链路行 `str(detail or code)` 兜底可摆英文异常。我补查又擒两族同族漏网:progress_callback 文案('Downloading Chromium...')与无 tkinter 终端兜底交互。
- **根修纪律(一句话:lib 抛 token,UI 映射,键在主题):** lib 层一律改抛机读 token——`ConnectLineError(code, detail)`(missing_parts/too_many_parts/bad_url,str() 保持非空无 secret,connect_line 契约套件零改动保绿);probe/pair 本已 token 不动;`Component.install()`/progress_callback 改返回/发 token 或 `detail:<raw>`;`size_hint` 属性删除入键 `desktop.comp.<key>.size`。UI 边界(connect_ui/role_window/post_install/launcher 托盘缝)只经三个主题映射碰文案:`reason_text`(http_ 族填 {code})、`connect_error_text`、`component_msg`(token 查表;`detail:` 前缀本地化「安装失败」+ 原文尾巴=owner 动态细节规则);未知 token 开发态原文兜底,但普查棘轮要求 lib 可发的每个 token 必有键。托盘链路行 error 分支键化 `desktop.tray.stError`;终端兜底交互(installPrompt/alreadyInstalled/标题/进度/结果)双语,纯日志行保英文(诊断面)。
- **测试账:** 新 `tests/test_desktop_native_i18n.py` 21 针——三映射逐 token 双语≠+raw token 不当中文;http 填码;`detail:` 前后缀语义;parse 三码+secret 不回显;未知异常/token 透传;**普查棘轮**(扫 _probe/_pair 的 `return False, '...'` 字面量,每个 token 必在 _REASON_KEYS∪对话框专理∪http 族,新增未映射 token 即红——NEUTER 塞 `dns_poisoned` 实证);接线棘轮(err.config 走映射/component_msg 双消费点/size 键/stError/无 install 散文残留);新键族 zh 非假翻译+占位符双语对齐。NEUTER×6:映射丢 token/映射丢码/detail 分支断/lib 发新 token/parser 退回散文/对话框退回 str(ve)——全精确红。
- **zh 渲染实弹(TOFU_LANG=zh 直调映射链):** 无法连接到该地址/服务器返回 HTTP 401/服务器返回了无法理解的响应/服务器地址必须以 http:// 或 https:// 开头——当前为「ftp://x」/需下载约 115 MB/Chromium 浏览器安装成功/安装失败: make: gcc: error/出错——SSLError(...)——八连全中。
### 2026-08-04(预存红闭环:test_server_async::test_health_endpoint——断言钉迁移前裸数组契约,端点已按 charter#0 包 {ok,items} 信封;方向对齐,独立运行器同款断言同步修) — 脑派发接我自票 `pt_a1b4d3ec829c43c2` **DONE**;commit `3074918e`(1 文件 +8/−2,纯测试侧);套件 **13/13** + chat parity/drift 棘轮 **9/9**

- **定案(测试漂移,与已关票 restart_smoke 同族):** `/api/v1/chat/active` 返回 `{'items': [], 'ok': True}`(charter#0:数组负载必须包对象信封),断言还钉迁移前的 `isinstance(data, list)`。修法=钉信封契约(`ok is True` + `items` 为 list),`__main__` 独立运行器里的同款断言一并对齐。failing-first 证据天然俱在(改前红改后绿);零产品代码。
### 2026-08-04(视频上传+分析 P1 落地:抽帧+转录骑既有图片通道——模型感知帧预算/聚合图像账/本地盘 scratch/上传时异步处理;owner 四决策+四补充全纳入) — epic `pt_6aca988757cb4019`;commit 见下(18 文件);新套件 **33 检全绿**(真 ffmpeg 合成片集成)+ 守卫环 **141 绿**(2 预存红各挂票,均实证与本批无关)

- **形态(owner 定案 P1):** 视频上传即后台处理(ffprobe→原件持久化→均匀+场景检测抽帧→音轨骑 `lib/transcription` 既有 whisper 槽位链),产出自包含净载(耐用 `/api/images/` 帧 URL+转录+元数据)嵌进消息 `videos[]`——重载/断流恢复/多轮追问零额外机制。P2(Gemini 原生直通)留 epic 后半,依赖网关能力核查。
- **模型感知钳制(owner 决策①):** `lib/model_info/_video.py video_frame_budget` 四钳取最小——视觉门控(无视觉→0 帧仅转录)/家族帽(Claude 40,API 硬限 100 留余量)/上下文份额(30%×学习型 context_limit,实测 claude-sonnet-4-5→25 帧)/线字节帽(8MiB÷实测帧均字节,防网关 413)。**聚合账**在 `_transform_messages`:全请求图像块计数,Claude 90 上限跨视频记账(测试实证 40+40+10)。帧抽取 16/32/64 按时长档,发送时 `_thin_frames` 均匀稀疏化保首尾。
- **关键复用(全部实测):** ffmpeg/ffprobe 走 `motion_video._env`(imageio 内置 v7.0.2);probe 骑 `_gates.probe_video`;转录骑 `lib/transcription`(owner 擒获的错误假设纠正——**零新依赖**,禁 faster-whisper);帧压缩在抽取时一次到位(1568px/q4,对齐 `_CLAUDE_IMAGE_MAX_PX` 无 churn 哲学);原件存 `uploads/videos/` 走 `file_serving.send_file_conditional`(Range 安全)。
- **新闸(server.py):** 全局 MAX_CONTENT_LENGTH 50→520MiB(视频帽+slack),新增 before_request 分路 body 帽——仅 `/api/v1/videos/upload` 512MiB,其余全表面守 50MiB(owner 决策②,防抬高全局敞开所有路由)。
- **事故自纠:** ①`_append_video_blocks` header 引入 `if False` 死分支脏代码,同轮复核清除;②场景合并逻辑首版内联不可测且测试设计撞「均匀层±1s 去重」(短片上场景帧数学上必被吞)——抽 `_merge_scene_extras` 纯函数 + 探测器/单帧抽取分测,注释钉「场景层只在均匀间距>2s 的长视频上提供信息位」的设计边界。
- **预存红×2 挂票(均实证无关):** `pt_a1b4d3ec829c43c2`(test_frontend_api_contract 钉 auth-sources 模板串,HEAD 上单文件 stash 复现)+ test_server_async health_endpoint(envelope vs 裸 list,与已关票 restart_smoke 同族,GET 不过 body 帽构造上 exonerated)。
- **生效面:** 后端即重启即生效;前端走 bundle mtime 自愈重建;ffmpeg 零运维(缺则自动 pip imageio-ffmpeg)。
### 2026-08-04(角色窗三宗罪根修:tray-first tk host——最小化不再吞窗口(托盘先于窗口存在);detect_lang 中文 Windows 显示名 locale 根修(全英文→真双语);字体栈收编衬线宋体) — owner 截图三指令(「界面太丑重新设计」+「i18n 要全量落实」+「点最小化窗口直接消失、没进托盘」);epic `pt_1013577081ab4eeb`;commit 见下(14 文件);新套件 17 针 + 扩展 21 针,环 **84+311+76 绿**,NEUTER×7 全精确,agent 冒烟闸过(0.16.0/19 命令)

- **罪①定案(最小化吞窗=结构性,非补丁可修):** 旧编排 `show_role_window`(阻塞 mainloop)→ `icon.run()`——**窗口整个首生命周期内托盘根本不存在**,标题栏「_」iconify 到任务栏(通用 tk 羽毛图标无人识),「最小化到托盘」在结构上不可能兑现。根修=线程拓扑翻转:**主线程 pystray 从零秒占有;专职 tk host 线程(新 `desktop/_tk_host.py`)拥有全部窗口**(隐藏 root + queue + after 泵);托盘回调经 `post`(角色窗,fire-and-forget)/`call`(配对等返回值对话框,阻塞=今日行为)marshal;host 线程内调用短路内联防自死锁;`icon.update_menu` 留在托盘线程(pystray win32 `_update_menu` 毁建 HMENU,跨线程=与打开中菜单竞态)。标题栏「_」经 `<Unmap>`→state=='iconic'→withdraw 拦截,与窗内按钮同义进真托盘。非 win32(macOS 两框架都要主线程)host.start() 返回 False,全部入口内联=旧序列零改动。
- **罪②定案(i18n 从未生效于中文 Windows):** `locale.getlocale()` 在 Windows 返回**显示名** `'Chinese (Simplified)_China'`,不以 'zh' 开头,`if loc: return 'en'` 硬锁英文——owner 中文机器全英文界面即此。根修=归一化(小写+连字符/空格→下划线)后匹配 `zh`/`chinese` 双前缀。截图里全英文即实证。
- **罪③定案(丑=字体为主,布局为辅):** `('', 10)` 空族名在中文 Windows 落到**宋体**(衬线),拉丁字母全衬线渲染;另复选框贴卡片边、窗口不居中、任务栏 tk 羽毛图标、egress 档位天书。根修=`pick_font_family` 按平台栈运行时解析(win: Segoe UI/雅黑;zh 优先雅黑;mac: Helvetica Neue/苹方;linux: Noto/文泉驿),`center_on_screen`(光学中心 0.38),`set_window_icon`(iconphoto+iconbitmap 双轨),角色窗重排(eyebrow 卡标+档位一行说明×4+开机自启说明+发丝线+统一 12px 槽距),logo 锚定改列表(64px 图标照不再被 40px 标题照 GC)。
- **测试账:** 新 `test_desktop_tk_host.py` 17 针——平台闸/start 失败形/队列语义(stub root 驱动 `_drain_once`:结果落盒、异常捕获不炸泵、done 事件、after 重臂)/host 线程自调用短路(防自死锁,注毒即 AttributeError 红)/端到端 marshal(真实 drainer 线程对拍 fn 执行线程)/异常回抛调用方/超时不挂死/头less 导入棘轮(AST 顶层无 tkinter)/tray-first 接线棘轮(host 先于 icon.run——**被我自己注释里的字面 `icon.run()` 坑过一次**,改语句锚定正则)。tk_theme 套件 +21(Windows 显示名×4/字体栈×7/居中几何×2/字体解析棘轮——钉 `base_font = _f(10)` 且禁 `('', 10)` 回潮)。role_window 套件 +3(居中+图标锚/档位说明键双语全/双 App 档位键覆盖)。Unmap 棘轮首版子串检查被注释残留骗绿,升级 AST 级(bind 调用+withdraw 调用)——与 progress_callback 判例同族教训。
- **顺手擒获:** agent_launcher `_run_tray` 双重 def(合并残渣,前者死代码)收编;`test_both_launchers_import_role_window` 等旧棘轮全保绿。
- **验证边界(诚实账):** 本机无显示无 Xvfb,tk 渲染路径(字体实际落族/布局像素/托盘真实出现)未经真机实证——行为契约全部由 17 针队列语义+AST 棘轮钉住,像素面留待 owner 真机验收(受控端安装包需随 `pt_59b62951aad2463e` 链重建 payload 后到用户)。
### 2026-08-04(浏览器工具面 v2 认知减负:19→13 意图级合并——read_page 四合一/click+type 支持 text= 模糊定位/自动等待+动作回执/工作tab记忆;代码接管模型曾经的手工路由) — owner 指令「插件和浏览器控制工具要对齐大模型直觉,消除冗余,能用代码做的别让模型推理」;脑派发接我自票 `pt_869e5648403e4745` **DONE**(MCP per-tool 开关拆后续票);commit `55259a3c`(23 文件 +1517/−400);新套件 30 针 + 邻接环 **319 绿**;预存红×1 挂票 `pt_eb0251bb1cbe4a27`

- **审计定案(全部实测):** 内置 88 工具 + 本部署 ~190 MCP;仅浏览器族 schema=18,651 字符(~4.7k token/请求);台账自报 6 工具描述无法自区分(create_tab vs navigate 首句重合 0.57);感知层 4 工具互相在描述里教模型「何时别用我」=把渲染方式诊断外包给模型;wait/tab_id/CSS selector 是代码该管的细节全甩给模型。
- **v2 面(13 工具,纯表示层重构,扩展 27 命令零改动):** ①`browser_read_page` 四合一(read_tab/summarize/app_state/elements),auto 模式乐观读文本、稀疏(<400 字符)自动附结构摘要——渲染方式诊断收进代码;②`browser_click`/`browser_type` 支持 `text=`(服务端枚举元素+模糊排序:exact>prefix>substring,角色/标签加权,歧义回传前 5 候选让下轮必中);③keyboard 拆 `browser_type`(clear-first 打字)+`browser_press_key`(特殊键);④create_tab 并入 `browser_navigate(new_tab=true)` 且默认等加载(根治「读太早」);⑤hover_and_click+right_click_menu 并 `browser_menu_click`(via=hover|right_click);⑥`browser_wait` 从模型面退役(动作内部自动等待);⑦tab_id 全可选——服务端工作tab记忆(显式>记忆>活动tab播种,close 遗忘)。
- **速度账:** 动作回执(click/type/navigate 后一次轻量 list_tabs 对比标题/URL 增量)=验证不再花整轮 LLM;schema 18,651→13,589 字符(−27%);表单类任务从 ~6 轮到 1~2 轮。
- **legacy 连续性(关键设计):** 退役 10 名的 dispatch handler 全保留(直调 execute_browser_tool 可用)+ display 格式器全保留(历史工具卡照常渲染);tool_registry 注册面随 BROWSER_TOOL_NAMES 收缩自动摘除——无 provides/棘轮债(全覆 ratchet 无需豁免)。`LEGACY_BROWSER_TOOL_NAMES` 单源记录退役集,tool_display 分派与前端图标表据此保渲染。
- **新模块 `lib/browser/_resolve.py`:** 工作tab记忆/元素模糊解析/建议性自动等待(永不阻塞动作)/动作回执,全部 send 可注入——handlers 走门面代理(monkeypatch 契约不破),advanced 走模块级名,测试直注 fake。
- **测试账:** 新 `test_browser_v2_surface.py` 30 针(合并面钉死/schema<15000 棘轮/tab_id 可选/退役不 ship/legacy dispatch+display 连续/解析器四态/工作tab三态/回执三态/read_page auto 双态+模式委托/registry 声明/审批 enricher 覆盖/menu_click 双态);既有 4 套件对齐(tooling_fixes 两测试换名/facades 3→2/write-partition SSOT 期望表/approval CASES 去 4 加 3);台账重生成(88→84 内置,更新工具数+补 update_search_settings 行)。
- **顺手擒获(预存红挂票 `pt_eb0251bb1cbe4a27`):** `update_search_settings`(eb315d4b 搜索设置批)是写工具却无审批 enricher——TestApprovalEnricherRatchet + approval CASES 覆盖双红,纯净 HEAD worktree 同签名复现,与本批无关,按惯例另票(修法=一枚 enricher+一行 CASES)。
- **生效面:** 即重启即生效(模型下轮即见 13 工具新面);历史会话工具卡渲染不变;旧会话若从转录学来旧名,unknown-tool 错误即提示其恢复(新 schema 名单自明)。
- **MCP per-tool 开关:** epic 标题第三半拆为后续票(lib/mcp 无任何 allowlist 机制,需设置页 UI+桥过滤双半,独立成票)。

### 2026-08-04(CLIProxyAPI 深度对标 + 订阅中继场景增强设计稿:四路深读实测漂移×4——伪装层快照 4 天即过期;推荐「漂移警报 + sidecar 订阅适配器」分层方案;设计稿 docs/SUBSCRIPTION_RELAY_SCENARIOS_DESIGN.md) — owner 指令「克隆并仔细分析 CLIProxyAPI 如何包装订阅,增强我们的方案应对各种场景(服务器断网→经本地有网机路由)」;零产品代码(设计阶段,待 owner 拍板)

- **仓库**:../CLIProxyAPI 已在同级目录(7/31 克隆),git pull 至 a63da8a;四路并行深读(架构/Claude/Codex/凭证车队),全结论带 file:line,细节入库设计稿 §1-2。
- **解剖一句话**:CLIProxyAPI=「翻译官+化妆师+车队调度」——按各家 CLI 原生端点仿真(~20+ 协议翻译对)、伪装层跟 2026 军备竞赛、auths/ 多账号车队带 (auth×model) 冷却账本。
- **拓扑对反的实测结论**:它的 cluster(CLIProxyAPIHome)是「凭证集中+中心执行」,服务器断网场景帮不上;我们的 bridge(agent 长轮询、执行下沉边缘)是该场景唯一正确方向——**egress 已建成不是缺口,是反超**。owner 点名的场景(断网服务器→本地机路由)正是已建成的 egress 全链,待真机验收(pt_59b62951aad2463e P4)。
- **漂移实测(tofu vs 它,4 天快照已漂移 4 处)**:Claude 版本 2.1.63 vs 2.1.220(outbound.py:59);token 端点 console.anthropic.com vs platform.claude.com;Codex Originator codex_cli_rs vs codex-tui(outbound.py:78,UA 格式也变了);beta 列表缺新 flag。持平:计费头算法+cch=00000(新版仍在,另增 entrypoint/workload 变体)、PKCE、plan_type、singleflight。缺口:uTLS、多账号、resets_at 配额解析。
- **定案方向(设计稿 §4)**:①E1 漂移警报守卫——对拍 ../CLIProxyAPI 常量,把军备竞赛变成自动报警(便宜必做);②E4 sidecar「订阅适配器」provider——本地机跑 CLIProxyAPI:8317,bridge 新增 loopback 中继类型(不复用域名白名单),伪装层外包给上游社区、token 永不出本机、多账号车队与 Gemini/Kimi 等订阅源免费得;与内建 egress 是分层非替代;③顺手果实:usage_limit_reached→resets_at 定时冷却、401 先刷一次再判死、请求级错误不进冷却账本。开放问题:二进制分发渠道(倾向首启下载+钉版本+哈希)、多用户端口分配、uTLS 兜底实测定案。
- **记忆刷新**:`cliproxyapi_订阅机制与_tofu_oauth_失败根因` 已更新至 2026-08-04 版机制(platform.claude.com/codex-tui/2.1.220/cluster 拓扑)。

### 2026-08-04(绑定默认值收敛:0.0.0.0 全接口 + LAN 发现默认开——`python server.py` 是最后一个 loopback 孤岛;顺手根修批跑互染预存红 TestBridgeTTL) — owner 指令「BIND_HOST=0.0.0.0 TOFU_DESKTOP_LAN_DISCOVERY=1 以后设为默认,用户总是 python server.py 启动」;commit `6a70adb6`(14 文件 +224/−57);环 **79 绿**

- **定案(本就该统一的孤岛):** 排查发现默认值早已四分五裂——bootstrap.py(`os.environ.get('BIND_HOST','0.0.0.0')`)、Dockerfile、docker-compose、install.sh、`.env.example` 注释全是 0.0.0.0;只有 server.py argparse、restart_15000.sh、tofu_guard.sh、supervisor tofu.conf 还钉 127.0.0.1,而本部署恰好走这条路。owner 拍板全接口为默认,loopback 改为显式 opt-in(`--host 127.0.0.1`/`BIND_HOST=127.0.0.1`)。
- **安全面(不是裸奔):** ①server.py:3083 启动横幅早就有「open 认证+非 loopback 绑定」醒目告警(新默认下每次启动都亮,正好是踏绊线);②打包桌面版 `desktop/launcher.py` 自带 `BIND_HOST=127.0.0.1` 钉——笔记本 app 不继承 LAN 默认(守卫套件钉死);③bridge 端点恒要凭证,open 模式对非 loopback 拒发合成 admin(egress 设计稿 §11.5 已评估)。
- **LAN 发现默认开 + 诚实守卫:** `maybe_start_responder` 闸门从 `!= '1'` 翻成 `== '0'`(显式 0 才关);新增 bind_host 守卫——loopback 绑定时响应器沉默,否则广告一个不可达的 LAN 地址=把每个发现中的 agent 引向死地址。server.py 记录 `_TOFU_RUNTIME_HOST` 供此(与既有 `_TOFU_RUNTIME_PORT` 同缝)。三启动器 BIND_HOST 默认翻转,tofu_guard 那枚尤其要害:OOM 重生不能悄悄收窄绑定。
- **顺手根修(批跑互染预存红):** 扩环时 `test_desktop_egress::TestBridgeTTL::test_per_command_ttl_override` 单独绿、批跑红。逐类二分定位污染者=TestPairingLifecycle.test_2(预存,非本批):其 /api/desktop/poll 注册的 agent 在 15s 在线窗内使 `_online_ids_locked('')` 非空,v1 poller 的 `_deliverable` 恒 False→pending=[]。顺带查清一个文档化行为:无 TOFU_BRIDGE_SECRET 时 route 层 open_when_unset 短路与,per-user pairing key 的 user 绑定不进注册表(单租户无害;relay 部署设 global secret 后 per-user key 才生效——设计内,未动)。修法=TestBridgeTTL 密封化(快照-清空-恢复 bridge 注册表,87a54720 恢复式判例),测试侧零产品代码。
- **测试账:** TestMaybeStartResponder 按默认开重写 7 针(默认开/唯 0 关/非 0 值不关/loopback 沉默×3/全接口广告/无 LAN IP 沉默);新套件 `test_bind_lan_default.py` 6 针(argparse 默认正则钉/三启动器/桌面 loopback 钉/横幅存活钉);环=pairing 19+bind 6+parity 3+egress 30+agent_cli+contract_parity+lock_race=**79 绿**。
- **生效面:** 代码即重启即生效;从此 `./restart_15000.sh` 裸跑即全接口+LAN 发现,epic `pt_59b62951aad2463e` 的 P4 验收不再需要 env 前缀。文档面 CLAUDE.md/README×2/.env.example/两设计稿同步。

### 2026-08-04(受控端安装体验双修:运行中安装→双语提示自动关闭(不再裸撞文件锁);SSH 隧道黑窗全灭——CREATE_NO_WINDOW 收进唯一真实 spawn 缝) — owner 两指令(「已运行时安装被中断,应提示或给关闭按钮」+「装完反复弹黑壳窗,后台隧道要更优雅」);commit `a139dff6`(5 文件 +209/−3);环 **47/47 + 261 绿**;真 makensis 双组件编译实证

- **根因①(安装中断):** NSIS 模板对运行中的 app 零防护——`File /r` 撞上写锁定的 TofuAgent.exe 镜像,Windows 拒写,安装器裸抛 file-in-use Abort/Retry/Ignore。修法=前置探测而非事后报错:对 `$INSTDIR\${APP_EXE}` 做 append 模式 FileOpen(运行中镜像必拒写),锁则弹**双语**(英/中)Yes/No 提示——Yes 走 `nsExec::Exec taskkill /IM /T /F` 代关(nsExec 藏控制台,/T 连树杀故 ssh 隧道子进程同灭,/F 无文档可失),1.2s 后复探循环,No 则干净 Abort。钩子进 `.onInit`+`un.onInit` 双闸(卸载时运行同样半删目录),full/agent 共享模板一次全愈。CI Inno 侧补钉 `CloseApplications=yes`+`RestartApplications=no`(原是未断言的默认值,默认翻转会静默上线)。
- **根因②(黑窗连弹):** agent 是 windowed exe(tofu-agent.spec `console=False`),而 `_pair._tunnel_once` 裸 `subprocess.Popen(['ssh',...])`——Windows 给每个控制台子进程配新黑窗;发现阶梯最坏 3 主机×3 端口=9 连弹,且每次开机 resume 重跑。修法=`_spawn_tunnel` 唯一真实 spawn 缝,Windows 下带 `CREATE_NO_WINDOW`+隐藏 STARTUPINFO(全 getattr 守卫,Linux 可导入可测);注入 `_popen` 的测试面签名零改动。
- **测试账:** pair 套件 +4(posix 空 kwargs/win32 表面模拟/spawn 缝 kwargs 路由/无注入路径接线钉——回归裸 Popen 即红,且 Popen 补丁防破线时真 spawn ssh);parity +1(宏在/.onInit+un.onInit 双插/探针指 payload exe/nsExec 隐藏杀树/双语 LangString/Inno 双侧 count==2)。failing-first 对 HEAD 五针全红(NSIS 宏/onInit/Inno 钉/两 helper 皆无)。
- **验证(超测试的真闸):** 双组件渲染脚本过真 makensis 3.11 编译——nsExec.dll 链接进包、两条 LangString(1033/2052)入编、.onInit+un.onInit 双函数在列,agent.exe/full.exe 均产出。
- **生效面:** 模板修复属 wrap 半(NSIS),`_pair.py` 修复属 payload 半(冻结 exe)——在店 TofuAgent-Setup 需随 `pt_59b62951aad2463e` 验收链重建 payload+重 wrap 才到用户;CI 下次发版自然携带。真机验收清单应补两条:①agent 运行中双击新安装包→双语提示→Yes 后秒装;②装完首启+开机自启全程零黑窗。

### 2026-08-04(popup 重设计复核擒获根修:2s 轮询每 tick 无条件覆写 serverInput——用户输入中的 URL 每 2 秒被冲掉;focus/dirty 双闸 guardedField 收编全部自动刷新字段) — owner 复核指令;commit 见下(3 文件);设计套件 +2(环 **62 绿**);打字跨 tick 截图实证

- **定案(owner 复核擒获的继承性旧疾):** 重设计原样继承了旧行为——`setInterval(updateStatus, 2000)` 每 tick `serverInput.value = resp.serverUrl` 无条件覆写,用户正在键入的新地址每 2 秒被冲回服务器值;我的三状态截图天然不可见此病(canned 桩值==键入值),owner 一句点破。
- **修法(根修,可复用而非一次性内联):** `guardedField(input)` 双闸——focused(`document.activeElement`)或 dirty(用户首个 keystroke 置位 `dataset.dirty`)则跳过覆写;Save 提交时 `commit()` 清闩(popup 打开天然干净)。注释+守卫共同钉死:全部未来自动刷新字段必须骑同一闸,杜绝「下一个字段又内联裸写」。
- **测试账:** 设计套件 +2——①无条件赋值棘轮(正则 `serverInput.value =` 禁现)+ 闸体四钉(activeElement/dirty 闩/input 监听器/guardedField 本体);②NEUTER 第 3 针(回换直接赋值→棘轮精确红)。环 = 设计 11 + auto_repair 16 + preseed 10 + parity 25 = **62 绿**。
- **实测(打字跨 tick 截图):** preview harness 桩每次 getStatus 递增 commandsExecuted——3.73s 截图 Executed=130(证明 ≥2 次 tick 已跑过)而 Server 字段仍显示 `http://typed-by-user:9999`:统计在刷新、键入在存活,同帧两证,harness 用后即删。
- **事故自记:** apply_diff 连续两次 search==replace 空操作(工具仍报 "3 lines changed",实为同文本重写),且一次误替换把 `__main__` runner 吞进上一枚测试体内——read_files 复核 tail 后一次修对;教训重申:编辑文件尾部结构后必读回确认,空操作不报错。

### 2026-08-04(扩展 popup 全量重设计:暗黑紫孤岛退役——豆腐块语言(奶油头/墨边/金 CTA) + 状态英雄卡层级 + emoji 全族清零;新增 Paused 状态不再伪装 Disconnected) — owner 截图指令「信息层级不清、emoji 滥用,UI/UX 都要改」;commit 见下(3 文件);新套件 **9 检** + 扩展环 **60 绿**;三状态截图实证

- **定案(与侧栏/技能页同族判例):** popup 是最后一面暗黑紫(#1a1a2e/#a78bfa)孤岛——全 app 面板已豆腐化,扩展还停在旧时代;且层级扁平(罕见的 Pause 键与主操作同重)、emoji 充图标(✦🆔⏸▶✓✗📤⏳ 八枚)。重设计全部骑既有豆腐语言零新发明:奶油头+金锔钉线(对齐 settings-header)/墨底金字版本徽标(对齐 .mcp-app-status.on)/金左镶条状态英雄卡(inset 4px gold,对齐 .stg- 卡)/金 CTA(btn-gold)/墨边偏移影(3px/2px)/方角。
- **层级(DOM 序=阅读序,钉进守卫):** 状态英雄卡( Connected/原因双行,绿点带脉冲)→ 修复 callout(金框,唯一可见补救)→ Server 配置 → Advanced 折叠(手工 key 字段按 2026-08-04 敕令永居 collapsed details)→ 统计数字瓦片(大数字+小号大写标签,Failed>0 转红)→ 页脚(Pause/Resume 次级 ghost 键 + client ID 虚线 chip,点击复制全量带 Copied 反馈)→ tagline。
- **顺手擒真 UX 缺陷(Paused 伪装):** 旧代码 pollActive=false 时状态落 `lastError || 'Disconnected'`——用户自己暂停却读成故障「Disconnected」。新增第三状态:金点 + 「Paused / Polling is off」,Resume 键同帧呼应。三态(connected/repair/paused)截图全过:repair 态金 callout 在英雄卡正下方,层级读感正确。
- **契约守恒(三套件既有钉全绿):** versionBadge 派生锚/`{ type: 'repairNow' }` 字面/repairRow+needsRepair 接线/bridgeSecret 永居 details/'never needed in normal use' 话术——auto_repair 16 + preseed 10 + parity 25 零改动通过;消息协议(getStatus/setServer/setBridgeSecret/repairNow/toggle)与背景侧零接触。
- **测试账:** 新 `tests/test_browser_extension_popup_design.py` 9 检——豆腐四 token 正针(#f2ecda/#faf8f3/#1a1814/#c9993f)、暗黑五 hex 棘轮、**emoji 扫描器**(U+2300-23FF 媒体控制块含 ⏸⏳/U+25A0-27BF 含 ▶✓✦/U+2B00+/SMP 全平面;首版漏 U+23F8 自纠)、DOM 序层级钉、getElementById↔HTML id 双向接线钉、三状态 dot 类钉、统计瓦片钉;NEUTER×2(抹金 token→红;⏸/✓ 扫描器 sanity)。failing-first:`git show HEAD:` 旧 popup 实证 3 emoji+4 暗黑 hex+0 豆腐 token,全针必红。
- **事故自记:** ①run_command grep 大文件 static/styles.css 在 FUSE 坏窗口空跑 1835s 零输出(同日 grep 拦截闸 epic 的活样本)——dedicated grep_search 才是正路;②browser_preview_page 三连 45s 超时(FUSE 抖动),单加重试即过;③preview harness 三状态 + chrome.* stub 用后即删(debug/_popup_preview*)。
- **生效面:** 纯扩展静态资源——用户重载扩展/下次打包下载即得;`scripts/package_extension.sh` 拷贝清单不变(popup.html/popup.js 原位)。

### 2026-08-04(视频上传+分析调研定案:两路线全景 + Tofu 接入点地图——「连环画+台词本」走现有图片通道,LLM 层零改动;video cap 早已预留) — owner 指令「调研开源系统如何让大模型处理视频,让输入框支持视频上传分析」;三路 swarm(开源模型/商用API+开源前端/本库链路);零产品代码;记忆 `video-upload-analysis-design` 入库

- **业界两路线定案:** A. 抽帧+转录(模型无关)——ffmpeg 场景检测 `select='gt(scene,0.3)'`+均匀兜底、逐帧文本时间戳、音轨 Whisper 转录,产出 N 张 JPEG+台词本走图片通道(OpenAI cookbook 官方模式;短视频 8-16 帧、分钟级 32-64 帧封顶);B. 原生直通仅 Gemini(默认 1fps、~300 token/s 默认档、含音轨、File API 2GB/48h、1M 上下文≈1-3h)。**开源前端几乎无人自实现 A**——LobeChat 直接委托 Gemini Files API;Open WebUI/Dify/FastGPT 不支持视频。
- **本库接入点(实测):** ffmpeg 可用(`lib/motion_video/_env.py:109`,imageio_ffmpeg v7.0.2 可自动装);`capability_taxonomy.py:95` **已预留 'video' 能力位零消费者**;拒收点=upload.py 图片白名单+magic bytes+`_shrink_upload_image` 强制 Pillow+`server.py:1556` 50MB;图片真实注入链=`conv_message_builder/_transform.py:233` → `llm/body/_images.py`(attachments.py 是文本 reminder 不是图片)。
- **落地分相(待 owner 拍板):** P1 模型无关底盘——新端点 `/api/v1/videos/upload`(不动图片白名单)→ ffprobe → 场景检测抽帧 16-64 帧自适应 → 帧走 `_shrink` 同款 → faster-whisper 转录 → 复用图片注入链;P2 Gemini 原生通道(dispatch 层识别 slot 直传 Files API,token 省 70%+);P3 长视频 agent 化选帧。待拍板项:帧预算/时长上限/STT 选择/P2 是否做。
### 2026-08-04(预存红闭环:test_frontend_sse_assistantmsg_invariant——固定 1200 字符断言窗被 _ensureMsgId 加固挤爆;改守卫块作用域锚定,方向对齐而非代码迁就) — 脑派发接我自票 `pt_eab154ae989f456c` **DONE**;commit `c5d3977b`(1 文件 +7/−3,纯测试侧);套件 **2/2** + NEUTER 精确

- **定案(纯测试漂移,代码无罪):** `connectToTask` 的非空守卫(sse_pipeline.js:384)完整无缺——构造新 assistant 消息/`_ensureMsgId` 赋 ID/push 俱在,「dispatch 前非空」不变量成立。漂移源:某批给守卫块加 `_ensureMsgId` 硬化后块长 ~1370 字符,断言的固定 1200 字符窗把 `conv.messages.push(assistantMsg)` 挤出界外(失败输出正好截在 `conv.messages.pus`)。
- **修法(形状稳健化):** 断言窗口从固定字符数改为**守卫块自身作用域**——`src[idx:src.index('\n  }\n', idx)]`,钉到守卫的 2 空格闭括号(内层 if/对象字面量全在 4+ 空格闭合,不会误配)。块内再加日志/注释/硬化永远不会再挤爆窗口;真删 push/role 照样红。
- **NEUTER 实证:** 临时删守卫内 push 行 → 精确红(「no longer establishes a fresh assistant message」);`git checkout` 字节级还原(该文件工作树==HEAD 无兄弟占用)→ 2/2 绿。
- **同类判例再+1:** 与 segment_gate/open_conv_scroll_once/charter_tools 诸票同族——棘轮钉「字面窗口」而非「契约形状」时,任何块内正当增长都会引爆假红;块作用域锚定是该族的通解。
### 2026-08-04(run_command 文件系 grep 拦截闸:建议数月不敌一记实测——`grep -n … | head; grep -rn … | head` 在 FUSE 坏窗口空跑 17m04s 零输出;新闸拒执行并给 grep_search 翻译,流过滤/ rg / git grep / timeout 包裹全合法) — owner 截图指令「run command 仍频繁执行 grep,很慢,我们有专用 grep 工具,想办法拦截」;commit 见下(6 文件 +330/−7);新套件 **42 检**(failing 实证)+ 守卫环 **280 绿**(1 预存红见下)

- **定案(建议不是强制):** run_command 工具描述数月来一直写「Do NOT use run_command for grep——grep_search 5x+ 快」,模型照跑不误;截图实证 `grep -n 'cream\|…' static/styles.css | head -30; echo ---; grep -rn 'cream' static/*.css 2>/dev/null | head -20` 挂 RUNNING 17m04s(无 timeout 可终结)。修法案=骑既有闸层(catastrophic-delete / unbounded-scan 同款模式):分类器 `_grep_filesystem_segment` 进 command_analysis.py,tool_run_command 在 scan 闸后新增拒执点,杀开关 `TOFU_RUN_GREP_GUARD=0`。
- **规则(拒什么/放什么):** grep 族(grep/egrep/fgrep,sudo 穿透、env 前缀穿透)段满足其一即拒——带文件/目录操作数,或带 -r/-R(隐式 cwd 遍历);**流过滤合法**(`pytest 2>&1 | grep PASS`、`ps aux | grep python`——grep_search 替代不了);`rg`/`git grep` 本就是快速路合法;`timeout` 包裹不拆封(有界扫描合法,对齐 scan 闸哲学);exotic 形状(heredoc/xargs/命令替换)fail-OPEN,与模块内全部既有闸一致。
- **关键解析坑(全部钉针):** ①`2>/dev/null` 重定向 token 不剥会被误判成文件操作数(`ps aux | grep -v grep 2>/dev/null` 误伤)——`_strip_redirection_tokens` 分 fused(`2>&1`/`>out`)与 bare(`> out`)两形;②短旗簇参数消耗(`-m 5`/`-e PAT`/`-d skip`)按 GNU 规则走簇,漏走会把参数误当操作数;③heredoc/herestring(`<<`)整段 bail——操作数状的 token 是数据不是路径。
- **拒执信息(教学向):** 含被拒段原文 + 四行翻译(`grep -rn 'X' lib/`→`grep_search(pattern='X', path='lib')`;count→count_only;`| head -20`→max_results)+ 明示流过滤仍合法 + 杀开关。工具描述同步加「Enforced」句,模型不再被 surprise。
- **测试账:** 新套件 `test_run_command_grep_redirect.py` 42 检——拒 21 形(含截图原命令/sudo/env 前缀/链式/重定向包裹)/放 16 形(流过滤全族/rg/git grep/heredoc/xargs)/端到端 Popen 绊线(拒在子进程诞生前)/杀开关实证执行。既有 scan 闸套件 4 测试撞车(`grep -rn` 在工作区内合法执行的旧断言 vs 新闸)——该 4 针测的是 scan 闸本体,monkeypatch 关新闸隔离,注释记由。
- **预存红擒获(与本批无关):** `test_project_tools.py::test_multi_device_startup_collapsed` 在**纯净 HEAD `c5d3977b`** 同签名复现(temp worktree 实证)——fold 汇总行「[output folded: N of M lines omitted…]」新特性超出其测试断言(期望 ≤3 行,实际 4 行),fold 族待 sibling 认领;我的 command_analysis.py diff 零删行(纯增),exonerated。
- **事故自记:** insert_content 锚点含 `def` 行,内容插进函数定义与函数体之间(IndentationError 定时炸弹)——py_compile 冒烟当场擒获,删孤行修复;教训重申:插入锚点绝不含 def/函数头,插后先编译再测试。
- **生效面:** 纯后端,即重启即生效;描述变更随下次请求的工具 schema 自然下发。

### 2026-08-04(扩展「HTTP 405」定案+根修:preseed 烙的 serverUrl 丢 https 与 /proxy/15000 前缀——轮询打到网关默认路由 405,从未到 Tofu;根修=面板下载链自带 live base,后端钉 host 防伪) — owner 截图问「扩展为何连不上」;commit `ca4bccf1`(3 文件 +123/−4);套件 **10/10** + 桥接环 **103 绿**

- **定案(405 不是 Tofu 发的,三重实证):** ①access.log 全日 160 次 poll 全 401、**零 405**(含 14 天轮转日志 38915×200/39965×401,405 历史上也是零);②auth 闸门先于路由——无凭证连 GET 都只吃 401,Tofu 结构上不可能 405;③对根路径 POST 实测回的是 mlp 网关自己的 `{"error":"Unauthorized"}`(带 mlp CORS 头),证明**根主机请求根本不进 Tofu**。真凶链:`VSCODE_PROXY_URI=https://…mlp.sankuai.com/proxy/{{port}}/`——公共 URL 必须带 `/proxy/15000/` 前缀,而 `_build_bridge_preseed` 烙的 `request.host_url` 既丢前缀(边缘转发前剥掉)又降 scheme(TLS 在边缘终结、ProxyFix 有意不装)⇒ 12:17:57 owner 重下载的 zip(audit 实证铸 key k_7fb6b768)把扩展指到 `http://主机/` ⇒ 浏览器 SSO 有效穿过网关后落到默认应用(code-server 根服务)吃 405 ⇒ 扩展停在 HTTP 405,此后零 poll 到达。
- **时间线(全部日志实证):** 00:02–12:13 旧配置 URL 能到 Tofu 但 **X-Bridge-Secret 头全空**(audit `has_header:false`)→ 401 停摆(存量舰队 locked-out 族);12:17 重下载后 preseed 只填空槽——secret 槽空得以采纳新 key,serverUrl 槽被破值占住/或被破 preseed 填入 ⇒ 轮询转向根主机,405。
- **根修(对齐 downloads[].url 已有判例——前端握有 live prefix 知识,后端结构上不可见):** 前端 `downloadBrowserExtension` 携带 `?base=location.origin+BASE_PATH`;后端新增 `_external_base_url()` 三级——①base 参数(netloc 钉死 request.host,**防伪链接把新铸 bridge key 引向攻击者主机**)> ②`VSCODE_PROXY_URI` 模板填端口(Host 端口优先,ASGI scope server 兜底;Quart test client 无 base_url 参数,测试用 Host 头带端口)> ③host_url 旧行为。desktop 连接行不属本批——8/3 已判 SSO 墙下地址半必错、配对码取代之。
- **测试账:** `test_browser_extension_preseed.py` +4——base 采纳(https+前缀原样存活)/跨域 base 拒绝回退(安全针)/env 模板兜底/前端 wiring 静态针;套件 10/10;环=bridge_auth+connect_line+local_control_merge+auto_repair **103 绿**。
- **用户即时解锁(无须等上线):** 弹窗 Server URL 改 `https://5665bc99-279b-4edf-8553-c7b7804c6e02-vscode-zw05.mlp.sankuai.com/proxy/15000` 存——12:17 zip 的 secret 已在空槽采纳,下一轮 poll 即通;若仍 401 点「Re-pair now」(开着的 https Tofu 标签页此刻 startsWith 匹配,梯子借页铸新 key)。根修上线后新下载的 zip 自带正确 URL。
- **生效面:** 后端即重启即生效;前端走 bundle mtime 自愈重建。
### 2026-08-04(停滞横幅分制:工具执行中(心跳流动)不再弹「已停滞」——「仅心跳」在构造上≡「工具在执行」;横幅保留给连心跳都没有的真冻结) — owner 截图指令「执行命令时没必要提示,这不是正常的吗」;commit `906e721d`(6 文件 +272/−67);套件 **15/15** + 邻接环 **54 绿**(1 预存红挂票 `pt_eab154ae989f456c`)

- **定案(机制根因):** 后端心跳 ticker `_start_tool_heartbeat` 只在工具阻塞期间运行,所以「仅心跳自 tick、无新产出」在构造上就**等价于**「有工具正在执行」——旧的 300s 横幅只在健康命令执行中误报(FUSE 上 find/grep 动辄 5 分钟零输出,journal 实测 294s;工具行本有「Running command… (Ns)」活计时;真挂死由后端收割器 30 分钟无输出显式报错兜底,pt_8524e0ec)。7/31 建卡时的 2.5h 挂死事故如今已被收割器有界化,前端 5 分钟预警属重复告警。
- **修法(stall_watch.js):** watch 记录新增 `lastTick`,自 tick 到达即盖章;新增 `_ticksFlowing` 闸(窗口 60s=4× 心跳间隔,`window._STALL_WATCH_TICK_WINDOW_S` 测试缝,0=闸拆)——流动中不举旗;横幅保留给「连心跳帧都未到达」的真冻结(分发器死/流静断);心跳恢复即自愈,与真实输出同一路。i18n 文案同步改「期间连心跳帧都未到达,疑似卡死」(旧文案「仅有心跳」在新语义下是谎话)。零后端改动。
- **测试账:** W 套件分制重钉(执行中 500s 静默不报警/无帧冻结才报警/重放旧 tick 给流动宽限且不丢 emittedAt 静默底座);W2 NEUTER 闸(窗口=0 时同样喂料必须报警——证明抑制来自闸门而非探测器死亡,F5 底座针也迁此);R 渲染缝两制各一针;新增 `_flow_gate_present_in_detector` 棘轮防未来「简化」把闸拆掉。failing-first 论证:旧代码下 W 的 `tool_executing_no_banner` 与 R 的执行中针必红。
- **顺手擒获(预存红):** `globals.generated.d.ts` 相对 HEAD **已提交**源(tool_rounds_rich.js/paper-deepen 等)滞后——某批提交源文件漏重生成;重生成一并修正,守卫复绿(非兄弟 WIP,源已落库)。
- **预存红挂票 `pt_eab154ae989f456c`:** `test_frontend_sse_assistantmsg_invariant` 锚点窗口漂移(某批在 `role: "assistant"` 与 `conv.messages.push` 之间插了 `_ensureMsgId` 行,push 被挤出断言窗口);扫的 sse_pipeline.js 工作树==HEAD,同签名复现,与本批无关。
- **上线面:** 前端走 bundle mtime 自愈重建,最迟随下次重启上线。
- **事故自记:** write_file 新测试文件时把 C 风格 `/* */` 注释写进 Python 层 + 末尾游离 `"""`——7/31 同款事故再犯;py_compile 冒烟当场擒获。教训重申:**任何写文件后先语法冒烟再测试**。
### 2026-08-04(预存红闭环:census 套件本机恒红两枚——feature-* 构建产物跳过表收编 + 后端普查改 git 索引枚举(294s→0.2s);首版并行读误诊被实测推翻) — 脑派发接我自票 `pt_759f78bdafa2430a` **DONE**;commit `4fdef290`(1 文件 +80/−23,纯测试基建);套件 **4/4 in 1.46s**(原 302s 超时红)

- **① 前端普查假阳(命名漂移):** `_js_call_sites` 跳过表只认 `bundle-` 前缀,Epic E 后产物是 `feature-<8hex>.js`——扫描到产物里拼接的 `Api.chat.active(` 源码,误判「未申报消费者」。修法=跳过形状与 js_bundler 陈旧清理器同源的哈希锚定正则 `^(bundle|feature)-[0-9a-f]{8}\.js$`(源文件 `feature-loader.js` 不误伤),形状针四断言钉住。
- **② 后端普查超时(实测定案,首版误诊):** 第一版并行化**文件读取**无效仍 302s——探针实证瓶颈是 **os.walk 目录枚举本身**(294s FUSE readdir RPC;读内容仅 0.75s,并行 0.09s)。根修=枚举改走 **git 索引**(`git ls-files -co --exclude-standard`,实测 0.2s,覆盖已跟踪+未跟踪未忽略=共享树兄弟 WIP 也在普查内),线程池只负责读;git 缺席时回退线程化 walk(剪 __pycache__/.tofu_trash)。
- **意外之喜(git 语义更正确):** 集合 diff 实证裸 walk 多扫的 6 个文件全是 `.tofu_trash/` 恢复快照——已删除的消费者会被裸 walk 误计为活调用点,git 视图天然排除。
- **事故自记:** 首版「并行读」修法未先测量瓶颈即下手,302s 复跑仍红;教训=性能修复先 profile(本票靠 walk/read 分段计时 30 秒定位),勿凭假设开药。
### 2026-08-04(travel 续刀:vertical auto 路径富 items 直通前端紫卡——owner 复核擒获的集成缺口;孤儿测试文件收编) — owner 复验指令;commit `2619e3ef`(3 文件 +219/−9);环 **39+42=81 绿**

- **缺口(owner 擒获):** auto-detect 是用户命中 travel 的默认路径,而 `resolve_vertical` auto 分支返回 type-level 记录(有 items 无 sources)——`_vertical_to_sse_payload` 的直通条件是「items AND sources 同在」,type-level 落 legacy 分支被压成 content 首行合成的一行,航班 10 条可预订条目在前端紫卡全丢(LLM 文本不受影响)。explicit `vertical='travel'` 域路径无此病。
- **修法(根修):** legacy 分支优先直通记录自带 items(逐条 setdefault type/source,对齐 `registry._structured_items_from_record` 语义,复制不别名),无 items 才回退首行合成;新套件 `test_vertical_sse_payload.py` 5 针(直通/默认值/回退/域路径/空记录)。顺带按 0.8.0 新语义重写 `test_search_tool_schema.py` 两枚前提失效测试(免 key 全量→无警告;逐调用重建改由 FlyAI 锁存驱动),并重钉部分可用缺口警告机制(monkeypatch 单类型下线)。
- **事故自记(孤儿收编):** `test_search_tool_schema.py` 此前**零 git 历史**(7/31 travel 建造者留下的未跟踪 WIP,钉的是旧部分可用语义)——我 apply_diff 改写后随批提交,`git show --stat` 显示 create mode 才察觉。判:内容是现行正确语义且套件绿,收编优于留雷(孤儿钉旧语义=未来预存红);教训重申:提交前 `git status --porcelain`(不滤 ??)核对 create/modify 清单。工作树 JOURNAL.md 一度显示 M 但 diff 为空(racily clean),`git status` 刷新后净。
- **上线面:** chatui 本批(_display.py)+ tofu-search 0.8.0 同骑既有重启队列;`test_release_chain_tofu_search` 的 0.5.3 floor 钉行与本机 0.6.0 metadata 漂移是另一枚预存红(板上已有票),与本批无关。

### 2026-08-04(预存红×2 闭环:_adoptTailGrowthFromServer 抽取撞 drift 两套件——方向对齐 d3f9078e;顺手擒获 census 族第三红另票) — 脑派发接我自票 `pt_ef4ae7206b674e67` **DONE**;commit `c79e4f76`(2 文件 +23/−7,纯测试侧零产品代码);两套件 **9/9** + cross_tab 消费面环 35 绿

- **① merge_active_task NEUTER 锚点漂移:** 锚点钉的是抽取前内联形状(6 空格缩进、在 `_verifyActiveConvFromServer` Case 2 里);d3f9078e 把尾部采用抽成 `_adoptTailGrowthFromServer`(legacy Case 2 与 windowed 锚点对共享),调用本体缩进变 2 空格。重锚到函数内新形状,docstring 记「neuter 跟随 helper 而非调用点」。
- **② poll_open_conv_grow 拼接闭包缺员(隐蔽失败模式):** 独立抽取 `_verifyActiveConvFromServer` 的 harness 只拼了双 reducer;d3f9078e 新增 `_adoptVerifiedServerConv`/`_adoptTailGrowthFromServer` 调用,bare-name ReferenceError 被轮询路的韧性 try/catch **吞成静默 no-adopt**(adoptedGrow=False 而非崩溃)——drift 表面像产品回归,排查成本高。补齐拼接闭包 + 注释记此失败模式(`convWindowParam` 有 typeof 守卫,场景走 legacy 全量路,`_verifyAdoptWindowedTail` 永不求值故无需拼)。
- **顺手擒获(异族第三红,另票 `pt_759f78bdafa2430a`):** 扩环普查 cross_tab 全部 8 套件时 `test_chat_active_consumer_census` 两枚红——①前端普查把 `feature-*.js` **构建产物**(js_bundler 哈希命名)当未申报 `Api.chat.active()` 消费者,跳过表只有 `bundle-` 前缀(Epic E 命名漂移;本机有产物恒红、干净 checkout 绿);②后端普查 os.walk lib/+routes/ 在 FUSE 上逼近 300s pytest-timeout 偶发超时。与本票无关,按惯例另案。
- **验证:** 两修套件 9/9;cross_tab 消费面(deferrable/p6_verdict/boot_load_lease/conn_transient/apply_settings_extracted)35 绿无连带。
### 2026-08-04(travel vertical 复活:tofu-search 0.8.0 provider 链——RollingGo(带key) → FlyAI(飞猪内置试用凭证,免配置);酒店类型史上首次免 key 可用) — owner 指令「方案 A 绕过不接受,目标是 tofu-search 自己拥有这个 vertical」;tofu-search commit `23e648b`(11 文件 +919/−85);环 **536 绿**(2 枚 mcp-extra 收集错误=mcp 2.0 删 fastmcp 的环境预存,无关本批);**无 key 实测 flight/hotel 端到端双通**

- **起因复盘:** owner 问「当年想让 tofu-search 查机票酒店(类 RollingGo MCP、免 API 配置),现在是不是没成」。核查:travel vertical 早已建成(7/31 `a6dadf2`),但 ①RollingGo 航班端点 8/4 起对匿名调用稳定 401(error.log 实证),匿名时代终结;②酒店端点从来要 key;③进程内「需凭证」锁存翻转后 travel 域从 web_search 枚举自动摘除(诚实广告设计按预期工作)。结论:不是没建成,是上游关门+自我隐藏。
- **根治(owner 定案):** 逆向公共 npm 包 @fly-ai/flyai-cli 的 bundle——飞猪后端本身就是 streamable-http MCP 端点(flyai.open.fliggy.com/mcp),CLI 的「免 key 试用」=内置共享试用凭证 + HMAC-SHA256 请求签名。新增 `travel_flyai.py` 在 tofu-search 代理感知 HTTP 层复刻该线协议(零 Node 依赖、零 shell-out),两个 type 改 provider 链:有 ROLLINGGO_API_KEY 先 RollingGo,失败/无 key 落 FlyAI。
- **逆向三大坑(全部实测钉死):** ①node `digest('base64url')` **去 padding**,Python urlsafe_b64encode 带 '=' ——差一个字符就是 "Authorization verification failed";②线上工具名是 **snake_case**(search_flight/search_hotels),CLI 的 dashed 子命令名直接 401 "Tool not allowed";③签名覆盖**请求体原文字节**——requests 的 json= 重序列化(带空格+\u 转义)会静默破签,body 必须紧凑 ensure_ascii=False 一次序列化原文发送(base._post_json 新增 raw_body 路径)。黄金向量用 node 真 crypto 生成钉进测试,跨语言对拍。
- **收益:** 酒店类型首次免 key 可用;航班结果首次带**预订链接**(bookable:true,RollingGo 时代只能「仅查询」);试用档酒店价格被上游脱敏(¥2xx)在内容中诚实标注并指引 FLYAI_API_KEY 解锁;平台 systemMessage(体验模式提示)透传。
- **顺手根治(日历腐化):** travel_slots 设计意图是「时钟由调用方注入」,但 handler 层直接 date.today()——字面查询日期(2026-08-03)随日历过期,handler 测试**今天起集体转红**(与本批无关的预存雷,8/4 当天引爆)。时钟读点收进两个 handler 的 `_today()` 缝,测试 autouse 钉住;旧断言零改动复活。
- **chatui 侧零代码改动:** 工具枚举本就由 describe_domains() 动态生成;运行中服务器骑既有重启队列自然上线。flyai skill 的 SKILL.md 补 NODE_USE_ENV_PROXY=1(node undici fetch 不吃 env 代理,本机裸跑必 ENOTFOUND;该文件在 .tofu 数据区,gitignored 不入库)。

### 2026-08-04(状态镜像「刚提问就过期(>6h)」假案根修:检视器读取被流式噪音挤爆 10k 行上限——实测 51754 行任务只有 ≤6 轮可见;面板降级改尾部切片,不再倾倒 system prompt) — owner 截图两问(conv 搜索设置批 R87);commit `63be7fe0`(5 文件 +196/−14);环 **66 绿**;真实库实证 85 轮全解析

- **问 1 定案(根本没过期,是被截断):** 每条 SSE delta 都独立落 `task_events`(精确游标冷回放设计,event_log 模块头明记),长任务日志被流式噪音主导——实测真实库 12 个任务超 1 万行(最大 51754 行/170 快照);而检视器 `_read_events_uncached` 取**前 10000 行**,该任务仅 11 张快照(≤R6)幸存,R7 起 159 张全截断 ⇒ state/request 双轴 miss ⇒ 前端误报「该状态镜像已过期(>6h)或不存在」。且话术本身也是化石:分层保留(§10.4)后结构事件存活 **30 天**,6h 只是流式噪音层。
- **修复①(后端,根修):** 读取过滤为结构事件(`messages_snapshot`/`round_usage`/`round_start`/`round_end` + `endpoint_%`,复用 event_log.STRUCTURAL_EVENT_TYPES 防漂移)——检视器本来就只渲染这几类;同一 10k 上限从「撑不过 6 轮」变「覆盖数千轮」。真实库实证:51754 行任务 85 请求轮 + 85 状态行全折叠,R80 镜像 OK(R85 是最终答复轮,本就无 post-tool 镜像,mirror 在 roundNum='final',设计内走 request 轴兜底)。
- **修复②(前端,owner 指令「只显示工具调用和结果,不要 system prompt」):** 精确增量(diff 前轮同轴负载)不可得时——R1/前轮缺失/前缀分叉——旧行为=整桶倾倒(含 system prompt);改 `_riTailSlice`:state 镜像尾部即本轮(带 tool_calls 的 assistant + 其 tool 结果),切尾展示;空切片兜底非 system 消息,**绝不渲染空面板**(P7 孤 user 镜像场景擒获)。request 轴(子代理兜底)保持全量,本无 post-tool 尾可切。
- **修复③(话术):** `ri.stateEmpty` 改「该轮次没有可用的状态镜像(未生成或已被清理)」,双语同步 P7 harness 副本。
- **测试账:** test_request_inspector +2——10300 行噪音压在前、R88 结构行在 10k 窗口外仍双轴解析(旧代码必红)+ 结构过滤 NEUTER(WHERE 改恒真、参数表不动,delta 泄漏即红);P7 harness +5(R9 前轮缺失→尾部切片五针:state 轴/工具调用可见/结果可见/system 隐藏/历史隐藏);环 = inspector 17 + P7 5 + retention/compaction/snapshot_delta 22 + i18n_coverage/P4/P6 15 + stale_bundle_self_heal 7 = **66 绿**。
- **生效面:** 后端修复即重启即生效(或下次代码热载);前端两件走 bundle mtime 自愈重建(stale_bundle 套件实证),最迟随下次重启上线。
- **事故自记(严重):** 首批 apply_diffs 调用 JSON 畸形(尾部混入数千个 `{}` 元素),工具忠实把 41KB 垃圾**写进了** request_inspector.py 的 SQL 语句中间——pytest SyntaxError 当场擒获;按标记点对切除+ast.parse 复核修复,零净残留(diff 最终仅 12 行意内改动)。教训=**任何编辑后、测试前先语法冒烟**(python -m py_compile / node --check),畸形工具载荷不报错就落盘。
### 2026-08-04(预存红闭环:test_the_053_floor_carries_its_rationale——钉字面 0.5.3 pin 行,而 floor 已两迁至 0.7.3;守卫改 floor 无关,钉「纪律」不钉「版本」) — 脑派发接我自票 `pt_6b0e8573e26043ec` **DONE**;commit 见下(2 文件);套件 **8/8** + NEUTER×3 精确

- **定案(测试漂移的第二形态——钉了版本字面而非纪律):** requirements.txt 的 tofu-search floor 已两次上移(0.5.3 allow_private_hosts → 0.6.0 filter_mode → 0.7.3 replay no-op,每段理由注释俱在),旧守卫的 `^tofu-search>=0\.5\.3$` 字面正则失配——纪律本身从未失守,是守卫把「当前版本号」当成了「必须文档化」的代理。修法=floor 无关化:从 pin 行读出版本号,钉该版本必须配 `# >=<floor>:` 理由段 + 显式 HARD/SOFT 标记 + 非 stub(≥80 字符);守卫更名为 `test_the_floor_pin_carries_its_rationale`,模块头/节注释的 0.5.3 化石行同步改 floor 无关。
- **NEUTER×3 全精确:** 删理由段→「no rationale paragraph」红;删 HARD/SOFT 标记→「not marked HARD/SOFT」红;**升版无文档场景**(pin 改 0.9.9 无段)→红——最后一针证明守卫从今往后会在每次未文档化的 floor 上移时当场咬人,这正是它的存在意义。requirements.txt 全程零净改动(还原后字节一致)。
- **出票范围说明:** 同文件姊妹套件 `test_requirements_public_resolvable.py[tofu-search]` 亦红,但属**有意红**(模块头注明「RED until the publish」——tofu-search 上 PyPI 是 human-gated 在办事项),不在本票;其 docstring 里「本机装 0.5.2」亦为化石,同样不属本票。
### 2026-08-04(图片工具缩略图「重开即消失」全族根修:_serverHasImagesLocalLacks 第四析取——IDB 剥 uri 的缓存不再被误判 FRESH;附 browser_preview_page 白名单漏登记) — owner 截图两问(「这工具是新的吗?为何前端没有可展开预览?」)+ owner 复核擒获「历史轮也会补图」误称;commits `48448ce2`(2 文件)+ `9fce96b0`(4 文件);环 **25+17 绿**(2 预存红挂票 `pt_ef4ae7206b674e67`)

- **问 1 定案(是新工具):** `browser_preview_page` 前一日随 `69de1a5a`(浏览器桥 CDP 批)落地——服务端无头 Chromium 真渲染项目 HTML/URL,回传截图+console/报错报告。
- **问 2 定案(白名单漏登记,非设计):** 后端 `handlers/browser.py:131` 对一切 `__screenshot__` 结果挂 `meta.imageDataUris`(快递已到楼下),但前端 `_renderReadImagesBlock` 白名单只登记 read_files/inspect_image/browser_screenshot(门禁没录新住户)——preview 轮掉回通用单行,图数据静默丢弃。该批前端 +2 行只加了图标映射。修法=白名单 +`browser_preview_page` 一行(`48448ce2`),JSDOM 钉缩略图/全屏点击/徽章三针 + NEUTER 删名单项恰 2 红。
- **owner 复核擒获(我「历史轮也会补图」是误称):** IDB 写缓存时 `_stripToolRound` 剥 `imageDataUris[].uri`(OOM 防护,只留 format/filename),而 `cacheIsStale` 四析取(计数/updatedAt/segments/translation)对此盲——缓存判 FRESH,else 分支只 merge 翻译不 merge 图 ⇒ 同浏览器重开会话,**全族图片工具**(不止 preview)缩略图静默掉回徽章行;idb-cache 注释「needs them re-fetches from server」是无代码兑现的空头支票。
- **根修(owner 处方,segments/translation 同族):** `conv_persist_helpers.js` 新增 `_serverHasImagesLocalLacks`——位置对齐比较 toolRounds→results→imageDataUris,服务器轮有 uri 而本地对齐轮全无即 stale;**identity guard**(content 相等才比较,regen/编辑过的异回合不错判,与 translation 谓词同款);接进 `conversations.js` cacheIsStale 第四析取 ⇒ 判 stale 走既有整体采用服务器副本路径,全族历史缩略图一次回来。代价=含图会话重开即从服务器重载图字节(与 `!cacheHit` 路径同,单图 ≤500KB 上限),可接受。
- **测试账:** 新套件 `test_frontend_images_cache_recovery.py` 3 针——真谓词行为五态(剥 uri 检出/本地有 uri 不误报/双方无图/非 assistant 忽略/identity guard 跳异回合)+ 决策两态(同计数同 TS 下谓词在则 stale、NC 摘除则 FRESH=bug 复现)+ wiring 静态针(conversations.js 真调用,防「定义了没接线」);extracted 家族注册表收编第 7 助手(define+window 暴露+conversations.js 零重定义三断言自动覆盖)。
- **环:** images 3 + extracted 3 + segments 4 + bundle parity 15 + conv_model_identity/merge_active_task/boot_early_paint/poll_open_conv_grow 17 = 42 绿;**预存红×2 挂票 `pt_ef4ae7206b674e67`**(d3f9078e 抽 `_adoptTailGrowthFromServer` 撞 drift:cross_tab NEUTER 锚点钉旧内联形状 + poll_open_conv_grow harness 拼接集缺新函数——stash 本批 conversations.js 后纯净 HEAD 同签名复现,与本批无关,按惯例另票处理)。
- **事故自记:** ①write_file 新测试文件末尾游离 `"""`(SyntaxError 定时炸弹),read_files 复核当场擒获;②insert_contents 锚点带 ` *   ` 前缀不匹配文档行(实际 5 空格缩进),1/2 失败后读原文重锚。③提交前验树擒获兄弟 staged JOURNAL(搜索设置批)——按 `git-commit-pathspec-sweeps-partial-staging` 纪律,等兄弟批次落库、worktree diff==仅我条目后才 pathspec 提交本条目。
### 2026-08-04(Skills 设置页豆腐化重设计:暗黑残留全退役——头部对齐 MCP 商店头层级 + 作用域分段控件/计数徽章/已安装金语言/featured 金标) — owner 截图指令「界面太丑,对齐/风格/重点突出都不行,重新设计」;commit `42a51dd9`(5 文件 +333/−50);新套件 **4/4**(failing-first 对 HEAD 精确 4 红)+ 回归环 **82+81 绿**;桌面/移动/已安装三视口截图实证

- **定案(Skills 专属样式=旧暗黑时代孤岛):** 基础 mcp-app-card 早已豆腐化,但 settings.css 的 skills-* 家族全是暗黑残留——#1a1a1a 作用域 pill、紫色 rgba(108,100,230) 拖拽区、绿色 rgba(34,197,94) 已安装洗色、#333 卡片页脚线、#b38b5d 分页、暗黑 toast/文件列表;头部六元素(标题+双徽章+作用域+新建记忆+搜索)挤一条 flex-wrap 无层级,「新建记忆」还伪装成 scope tab(语义=记忆动作,不是视图切换)。与侧栏批(`8e4ac0d7`)同族判例:深色块在奶油面板里是异物。
- **重设计(全部骑既有豆腐语言,零新设计发明):** ①头部 DOM 序对齐 mcp.html(标题+徽章|spacer|作用域|动作|搜索)——两商店页从此一种头语言;计数徽章改豆腐印花 chip(`.mcp-store-header-title .stg-badge` 覆写靛/绿,MCP 页同 markup 一并受益);②作用域分段控件=奶油轨+墨块激活+金偏移影(呼应 `.mcp-cat-pill.active`,一页一种「激活」语言;MCP 页共享该类同愈);③「新建记忆」降级 `btn btn-secondary btn-xs`(与 MCP「+ 添加」同族);④已安装卡改 `.mcp-app-card.connected` 金语言(金渐变+金偏移影,告别绿洗),installed tag=墨底金字(呼应 `.mcp-app-status.on`);⑤official/warn 徽章统一金-琥珀族,卡片页脚/分页改 dashed 发丝线(对齐 `.mcp-app-footer`),toast/文件列表/分页族豆腐化;⑥**featured 金标**——目录按 featured 优先排序多年但排序原因不可见,名称行加实心金 stamp(i18n 新键 `skills.featured` 双语)。
- **测试账:** 新 `tests/test_frontend_skills_panel_design.py` 四层——jsdom 14 检查(featured stamp/official/warn/install_note/安装 CTA/已安装卡类+动作集)、CSS 暗黑残留棘轮(skill 规则体禁 24 个暗黑 token,真空闸 ≥25 规则)、豆腐锚点正针(激活金影/已安装金语言/拖拽金填充/墨金 tag,防「全删转绿」)、HTML 结构针(头部控件序/徽章在标题块/新建记忆非 scope tab/简介类/测试钉死的静态 onclick 存活)。failing-first 用 `git show HEAD:` 旧文件实证 4 针全红。
- **提交纪律(hunk 级,三脏文件各异):** settings.css 的兄弟 1974 hunk 期间已被 `eb315d4b` 落库,工作树 diff=纯我的 3 hunk → 直接 add;i18n.js 混兄弟 2 hunk(ri.stateEmpty + egress 两枚)→ awk 摘 `@@ -1077` 单 hunk `git apply --cached`,三重守卫(暂存文件数=5 / i18n 增行=1 且=skills.featured / css hunk 数=3)原子过关才 commit,零 pathspec。兄弟 msdzvqi5(搜索页)边界零交叠;ri.* hunk 留树归主。
- **验证:** 预览 harness(真 CSS+运行时抓真面板标记+真 skills.js 渲染器+15 条目模拟目录)三视口截图——桌面 1280×950(目录+featured/official/warn/已安装卡+金 CTA+toast)、移动 390×844(头部换行整齐、卡片单列)、桌面已安装作用域(激活 tab 金影/状态 pill/三按钮);harness 用后即删。

### 2026-08-04(预存红闭环:test_charter_tools_register_in_project_mode——断言钉的是被 2026-07-30 有意收回的 agent 自提交契约;方向对齐而非代码迁就) — 脑派发接我自票 `pt_0711f247210f45a5` **DONE**;commit 见下(2 文件);注册表族 **86/86** + charter/board 邻接 **93/93**

- **定案(纯测试漂移):** 旧断言 `assertIn('project_charter_commit', names)` 钉的是 2026-07-12 的自提交去闸契约——该契约 2026-07-30 被 owner 有意**反转**(charter 永远要人类复核,`CHARTER_TOOLS = [read, propose]`,见 lib/tools/conversation.py 头部注释与 `lib/conversations/project_charter.py:1007` WITHDRAWN 记录)。与 segment_gate/open_conv_scroll_once/api_integration 诸票同族:测试滞后于有意契约迁移。
- **重钉(两半契约都钉):** ①模型侧集合=read+propose,`assertNotIn('project_charter_commit')`;②**拒绝半**——`CHARTER_COMMIT_TOOL` schema 与名字刻意保留在模块符号/`CHARTER_TOOL_NAMES` 里,旧转录学来该工具的模型会得到「人类闸门在哪」的明确拒绝而非幽灵工具错;这半若被「清理」掉,拒绝路径就退化成 unknown-tool,故一并钉住。
- **NEUTER 精确:** 把 commit 临时加回 `CHARTER_TOOLS`(模拟契约倒退)→ 新 `assertNotIn` 当场精确红;还原 17/17 绿。
- **事故自记(上一批遗留收口):** 搜索设置批的 `git commit -- <paths>` 扫掠部分 staged 事故已存 memory `git-commit-pathspec-sweeps-partial-staging`;本批提交前先核「worktree diff == 我的全部」再走 pathspec,流程闭环。
### 2026-08-04(技能系统「装了却说没装」定案:chat 模式 × 项目作用域的设计缝——注入链路本体三重实证健康) — owner 截图指令「查技能系统现在有什么问题;我先在 chat 模式问,现在 studio」;零产品代码(纯调查);证据链=注入日志+DB 持久层+生产网关探针+离线全真复演;记忆 `skills-chat-mode-project-scope-invisibility` 入库

- **主根因(chat 模式不可见,100% 复现):** flyai/soyoung 两包均为**项目作用域**——目录安装器与 API 默认 `scope='project'`(routes/api_v1/skills.py:160),落 `<project>/.tofu/skills/<id>/`;chat 模式不挂项目 → `build_skills_index(project_path=None)` 只扫服务器全局库 `data/skills/global/`(**目录根本不存在**)→ 索引为空 → 模型如实答「没装任何技能」。实测:`build_skills_index('.')` 出两条,`build_skills_index(None)` 出 `''`;日志实证本对话 10:21/10:22 两轮 chat 模式 `proj_enabled=False`、10:33:37 studio 轮 `proj_enabled=True`。类比:技能装进了项目抽屉,chat 模式不带这把钥匙。
- **studio 链路三重实证健康(排除法逐一销案):** ①`[Context]` 日志 10:33:40 conv=mse17ie1 blocks 含 `skills_index:963`(注入确实发生);②生产同款 4 块 system 探针经 dispatch→sankuai→kimi-k3,模型 4/4 块全念出(网关不丢中间块);③真 `_inject_system_contexts` 复演(静态 12872+memory 1401+skills 963+peer 1430 全真内容)同路径下发,kimi-k3 逐字答出 `flyai`/`soyoung-clinic-tools`。代码链逐站排除:`_append_to_system_message`/anthropic_outbound hoist/llm_sanitize/add_cache_breakpoints/conv_message_builder 全不丢块;429 重试同 slot 同 body(cache.py 契约),无换路。本轮我自身未感知该块=长工具轮模型侧上下文视图折叠个例(我的可见上下文带折叠标记),非管线缺陷;新 studio 对话问「你有哪些技能」即可对公众验证。
- **邻接发现×2:** ①`.tofu/skills/global/` 混入 40 个平铺**记忆** .md(记忆/技能拆分迁移残留;包扫描器跳过 `global/` 子目录故无害),另有 split-migration collision 警告(git-diff-magnitude…md 在 .tofu/skills 与 .tofu/memories 双侧同存);②手写 `_parse_frontmatter` 只认单行 JSON 的 `metadata:` 块,flyai 真实嵌套 YAML(`openclaw.requires.bins=[node]`)解析成空串 → `requires_bins` 永远 `[]` → 运行时资格门对真实 OpenClaw 包失明(设置页「需要 node」徽标来自目录元数据,非运行时解析)——无 node 机器上会广告一个激活即败的技能。
- **修复方向(待 owner 定,属产品意图):** A=目录安装对项目无关技能默认/可选全局作用域(设置页是全局面板,默认装进当前项目反直觉);B=chat 模式补技能可见性(需权衡缓存与「无项目」语义);C=用户侧即时解:全局库建目录后 `mv .tofu/skills/flyai data/skills/global/`,全模式立即可用。

### 2026-08-04(零配置收口:owner 复核擒获两枚「连接行为主」残话 + 我顺手擒第三枚 CLI flag 教学——egress tooltip 改配对码主流程 / egress 未授权改托盘 Permissions 开关主推 / 托盘鉴权失败开药重配) — owner 复核指令两条;commit `c6ed3302`(2 文件 +5/−4);守卫环 **37 绿**(tray 棘轮+egress btn+onboarding+i18n 覆盖+缺键绊线)

- **三枚收口:** ①`settings.egressGetAgentTitle` 原教「下载受控端并用连接行连接」→ 改教配对码主流程(「配对这台电脑」+ 6 位码,无需地址无需隧道);②`settings.egressAgentNoCap` 原教「重启受控端加 --allow-egress 参数」→ 托盘「Permissions → 允许转发订阅 API 流量」勾选主推,CLI flag 降为源码运行路径(顺手擒获,同类残话);③`desktop.tray.stAuth` 原只报病「连接行里的密钥不对」→ 开药「密钥已失效;托盘「连接到另一个 Tofu…」重新配对即可」。
- **提交纪律:** i18n.js 工作树仍混兄弟 msdzvqi5 两个未提交 hunk(1010/1077 行区)——hunk 过滤 + `git apply --cached` 只摘本批 1413 行区两行,提交纯暂存区零沾染。
- **至此「零配置+零手工隧道」目标 agent 可达面全齐:** 新装零配置(preseed)/ 凭证失效自愈(auto-repair 梯子)/ 卡死存量可被发现并一键引导(locked-out 三态)/ 全表面话术一致(面板/托盘/对话框/egress)。剩余仅 P4 人工重启(epic `pt_59b62951aad2463e` 在门)。

### 2026-08-04(存量舰队恢复链:owner 复核擒获「自愈代码够不到已卡死的旧扩展」——extVersion 上线路 + locked-out 登记处 + 面板三态诚实;顺手根修扫描器族原子改名窗口误判) — owner 复核指令四步;commit `5c252962`(13 文件 +510/−21);新套件 **9 针**(failing-first 对 HEAD 精确 9 红)+ 扩展结构针 +1;环 **254/254**(18 套件)

- **盲区定案(owner 擒获):** 上一批(`faa9169f`)的自动重配全部装在新扩展文件里,而 load-unpacked 侧载扩展**没有更新通道**,401 卡死后连轮询都进不来——服务器对「门外徘徊的旧版扩展」与「从未安装」零区分,面板一律「尚未安装」,是误导性谎言;桌面 agent 侧早有 `_with_drift`,浏览器扩展侧是零。
- **链(四步):** ①扩展 poll body 带 `extVersion`(manifest 直读,杜绝硬编码孪生);②`mark_poll` 存 `ext_version` 且**成功轮询自动清除该 client 的 locked-out 记录**(重下载的新 zip 沿用同 storage 同 clientId,自愈零账面);③`browser_poll` 鉴权失败先解析 body 记 locked-out(TTL 15min 读时过滤——避开 Python 默认参数绑定陷阱;容量 32 上限逐最旧;匿名无 clientId 不记)——Tofu 自己的 401 只可能是凭证失效,这正是存量呼救信号;④status 端点暴露 `servedExtVersion`(读盘 manifest,TTLCache 60s)+ `lockedOutClients`。
- **面板三态:** 已连接但版本落后 → 绿点不动 + 升级提示(两个版本号都点名)+ 下载按钮;未连接但有 locked-out → 状态「已安装但凭证已失效」(不再是「尚未安装」)+ 救援话术 + 一键重下载(preseed 零配置);下载按钮单源化(`_lcExtDownloadAction`/`_lcWireExtDownload`,三处复用防漂移)。
- **顺手根修(环中自擒):** merge 套件符号定位器把打包器原子改名窗口里的 `.bundle-<hash>.<rand>.js` 半成品当成第二定义源报「SINGLE SOURCE COPIED」——四处 rglob/walk 扫描器(merge×2/connect_line_contract/no_client_timeouts)统一排除 dotfile,该类误判绝种。
- **提交纪律新器:** i18n.js 工作树混有兄弟 msdzvqi5 未提交键族——`git diff | 按 hunk 过滤 | git apply --cached` 只把本批 3 键摘进暂存区,`git commit` 不带 pathspec 提交纯暂存区;兄弟 WIP 零沾染。

### 2026-08-04(搜索设置页重设计 + LLM 直改设置工具:后端实况条 + 管线预览 + 主旋钮双枚 + 高级折叠 + MB 单位 + update_search_settings) — owner 截图三指令(①风格乱无重点、前后端关系看不懂、页面太长 ②最大下载大小为何是字节,改 MB ③给大模型一个直接改设置的方法);commit 见下(14 文件);新套件 **22+19 针全绿** + NEUTER×3 精确;环 **249+52 绿**(2 预存红挂票)

- **重设计(焦点+前后端架桥):** 页首新增「后端实况」徽章条——扩展在线态/tofu-search 版本/SearXNG 实例数/过滤模式与模型/整轮+单页限时,数据来自 `GET /api/v1/server-config` 新增 `search_status` 投影(`lib/search_settings.status_payload` 单源,前端从此显示的是后端真实状态而非仅回显保存值);「一次搜索实际发生什么」管线预览卡——当前旋钮值实时代入一句话(引擎→抓取前 N 页(字符/超时)→过滤→注入),过滤关闭整行灰显「跳过过滤(原文直送)」,input/change 事件联动;主旋钮只留两枚(抓取网页数+LLM 内容过滤),5 个高级参数(超时/MB/字符×3)折叠进 `<details>`;三条长说明(过滤/站点接入/内网主机)腰斩重写。最大下载大小字节→MB(显示 ÷1MiB、保存 ×1MiB、0.5MB 分数合法、垃圾输入回退 20MB)。
- **LLM 直改设置(指令③):** 新工具 `update_search_settings`——`lib/search_settings.apply_updates` 单源读写(校验→钳制→原子落盘→`reload_config` 热生效→audit_log;零参数=纯读零副作用;`max_download_mb` 别名;`block_domain`/`unblock_domain` 与 optimizer `block_search_domain` 共享 `normalise_domain`(已改委托);env 遮蔽(FETCH_TOP_N 等环境变量压过保存值)诚实写进 notes 不谎报生效)。注册进新 `ToolSpec('search_settings')`,write_tools=批准门+串行分区。
- **注册表教训(cache-stable 棘轮当场咬人):** 首版把工具塞进 search spec 返回串(position 2)→ `test_tool_registry` 排序针精确红——新内建工具**必须追加在 base 相末尾**(human_guidance 后、capability 分界前),provides/write_tools 声明进独立 spec;SSOT 套件随之钉住覆盖与写分区完整。
- **测试账:** 后端 `test_search_settings.py` 22 针(纯读零副作用/钳制/MB 换算/字符串数字 coercion/bool 拒非 bool+拒当 int/未知键拒写/部分有效部分报错/合并不毁他节/域名归一去重/seed 不缩内置屏蔽集/env 遮蔽笔记/handler 写读双态)+ NEUTER×2(去钳制→恰 1 红;去 MB→恰 2 红);前端 `test_frontend_search_settings_tab.py` jsdom 19 针(MB 双向换算/管线预览联动两态/实况条在线离线不可用三态/save 整数分数垃圾三态)+ NEUTER(save/display 双路摘除各红)。视觉:独立预览 harness 双态截图实证(过滤开+折叠、过滤关+展开;用后删)。真实 server.py 引导烟雾:`GET /api/v1/server-config` 200 且 search_status 全字段就位。
- **预存红×2 挂票:** `test_charter_tools_register_in_project_mode`(板 `pt_0711f247210f45a5`——断言 2026-07-30 已有意 human-only 化的 project_charter_commit,纯净 HEAD 复现)+ `test_the_053_floor_carries_its_rationale`(板 `pt_6b0e8573e26043ec`,同法复现)。
- **共享树提交纪律(三会话同文件 WIP):** settings.css 4 hunk 仅 `@@ -1974` 是我的(余 3=skills 页兄弟),i18n.js 6 hunk 中 4 个是我的(skills.featured 与 local.ext* 三键为异己)——`git add -p` 按 hunk 精确入 staged,`8e4ac0d7` 批的整文件卷入事故不复演。
### 2026-08-04(桥接零配置化:扩展 401 自愈——cookie 穿 SSO 代理 + 借 Tofu 标签页会话静默重铸 key;配对码全分支主推,手工隧道话术全家退役) — owner 双截图指令(「扩展不应要任何凭证/手工配置;不要让用户手动开 SSH 隧道,全部由我们装的软件处理」);commit `faa9169f`(13 文件 +663/−148;i18n.js 三 hunk 已随兄弟 `8e4ac0d7` 提前落库);环 **229/229**(16 套件)

- **截图 1 定案(扩展 401 ×279 是双病合一):** ①扩展轮询跨域 fetch 不带浏览器 cookie——SSO 代理(codelab)在边缘直接 401,请求从未到 Tofu,此时贴什么 secret 都是死路;②Tofu 自身 `bridge_auth_required` 401(key 被重铸/吊销)只能回面板手贴新 key。修法两条独立成链:轮询 `credentials:'include'`(host_permissions `<all_urls>` 使 Chrome 跨域也附带 cookie,SSO 会话直接穿代理);401 按响应体分类,bridge_auth_required → `attemptAutoRepair()` 阶梯——先找已打开的 Tofu 标签页,在页面 MAIN world 调 `Api.desktop.mintToken`(页面自带会话=面板铸钮的同一授权)静默铸新 key 收养;无标签页 → 30min 冷却的开隐藏标签页;popup 唯一可见救济=「Re-pair now」按钮(用户手势才允许前台标签页,SSO 重登在那里完成后下轮梯子自愈)。手贴 key 输入框降级 `<details>` 高级区;「set the Bridge Secret in the popup」话术灭绝。双 manifest 4.6.0→4.7.0(trusted-input 契约守卫钉同步)。
- **截图 2 定案(面板教人开隧道=过时药方):** `local.proxyWarn`(「请改用 ssh 隧道地址打开面板再生成连接行」)整键删除——配对码不携带地址,受控端阶梯(loopback→LAN→ssh config→自建隧道)自己找路,警告的存在理由消失;连接行在 local_source/remote 两分支统一降级 `<details>` 高级兜底,且 `server_url_reachability=='public'` 时**整体不渲染**(SSO 边缘下它是实测死路);`local.awaitingAgent` 改自愈语义(已配对会自己重找通路,迟迟不绿→托盘「连接到另一个 Tofu…」重配);托盘 `stProxy`/`stUnreachable` 与连接对话框 `verifyFailed`/`instructions` 四处同步去手工隧道化。
- **测试账:** 新套件 `test_browser_bridge_auto_repair.py` 15 针(cookie 搭载/401 体分类/梯子形状/MAIN world 铸 key/单飞+repairBusy/话术灭绝/popup 降级/双 manifest 4.7.0/NEUTER×2);merge 套件 splice 表换血(proxyWarn 出,配对三助手+_lcPairCode 进)+重钉「配对全分支主推+连接行只在 details+全态零手工隧道」;agent_download 套件 section3/4/6 重钉(lcMintBtnSrc→lcPairBtn、public 藏连接行、awaiting 自愈);邻接自擒两枚漂移:backoff 套件 1600 字符窗口被扩支撑破→改结构性锚定(`_scheduleNextPoll(delay);` 收尾),preseed 套件钉旧 popup 文案→重钉「never needed in normal use」意图。商店 kit README 版本钉随 4.7.0 对齐(parity 守卫擒获)。
- **生效面:** 扩展 zip 每次下载从 `browser_extension/` 现打包——**免重启即生效**;面板 JS(local-control.js)走 bundle 自愈重建,最迟随 P4 重启上线;托盘话术随下个 agent 安装包。与 epic `pt_59b62951aad2463e` 的 P4 验收链合流(重启一次全部上线)。

### 2026-08-04(设置侧栏「深色砧板」退役:奶油豆腐索引栏——chrome 同族奶油色 + 四组印花分隔标签 + 激活白块嵌金条;轻量重排导航) — owner 截图指令「侧栏差点意思,与整体设计不搭,重设计到完美」;commit `8e4ac0d7`(3 文件 +81/−68);环 **81 绿**(52+29);桌面/移动双视口截图实证

- **定案(深棕渐变侧栏=异物):** 旧「cutting board」深色木纹渐变(#38332c→#2b2620)+暖灰文字在奶油面板里像外挂的另一应用;13+ 项平铺无层级。新设计=**豆腐索引栏**:侧栏与 header 同 `var(--s-cream)`(L 形铬框包白色内容面,心智模型=奶油框+纸面);新增 `.settings-tab-group` 印花分隔标签(10px/900/0.14em 字距 + 右侧细规线,呼应 `.settings-section-title`);激活态=白豆腐块(2px 墨边 + `inset 4px 0 0` 金条 + `2px 2px 0` 硬阴影,与关闭按钮/内容卡同语言);hover=实底 `--s-skin`(告别 alpha 洗色);focus-visible 金描边(a11y 补齐);图标不透明度 0.45→0.7、激活描金 `--s-gold-dim`。
- **导航重排(轻量):** 显示提前贴通用;订阅登录上移与服务商组成「模型接入」;其余按 能力(搜索/翻译/语音)/连接(网络/设备/飞书/MCP/Skills)/系统(记忆与偏好/高级) 落组;i18n 四键双语(groupModels/Features/Connections/System)。`data-tab` 按钮结构不变,`switchSettingsTab` 零改动,测试按属性寻址不受影响。
- **两枚顺手擒获:** ①飞书 logo 是 fill 型 SVG,全局 `stroke:currentColor` 会给品牌 logo 描边染色——加 `tab-logo` 类豁免(旧代码就有此隐患,奶油底上更显);②移动端横条覆写钉的是旧激活态(金底边+`::after`  shimmer,均已删)——同步为白块硬阴影并隐藏分组标签。面板 88vh→90vh 为分组标签腾纵向空间。
- **验证:** 预览 harness(实时抓真 index.html 标记+真 CSS)双视口截图——桌面 1280×950 全 14 项+4 组免滚动、移动端 390×844 横条图标态正常;harness 用后即删。环:devices/mcp_oauth_deferred/onboarding/i18n_coverage/bundle_parity 52 + settings 三套件 29 = **81 绿**。与兄弟 msdzvqi5(搜索页内容重设计)边界互认:它碰 settings_panels/search.html + styles.css 搜索组件区,侧栏段零交叠。

- **事故自记(当日晚补):** 整文件 `git add i18n.js` 把兄弟 msdzs82c **三枚** local.* hunk(desktopRemote 改写 + proxyWarn 删除 + awaitingAgent 改写)卷入本批 `8e4ac0d7`——owner 复核先擒一枚,兄弟核验补全三枚(我首查只看 diff 前 40 行漏报)。处置=不回滚(内容正确)+通知兄弟勿重提交+memory `shared-head-staged-diff-review`。次生形态:兄弟把工作树里 msdzvqi5 的 settings.search* 键(20+/3−)误记在我名下要「留给我提交」——已 DECLINE 更正归属;异己 WIP 绝不代提交,i18n.js 留给 msdzvqi5 自己收。

### 2026-08-03(owner 复核三枚全修:①致命——首启隧道 atexit 必死而 saved loopback 永久跳过阶梯,「开机自启」机器第二天必死无自愈;②LAN 响应器纯死代码;③隧道死磕 15000;修复后安装包二次重建) — epic `pt_59b62951aad2463e`;commit `d9b34c71`(6 文件)+ 构建 state=ok(git_sha `d9b34c71`);套件 **30+16+邻接 42 = 88 绿**;agent 烟雾 OK;server.py 编译过

- **① 地雷定性(owner 擒获,我 P3 的假绿):** 首启 `try_ssh_tunnel` 赢的 ssh 子进程挂 `_ACTIVE_TUNNELS`,`_reap_tunnels` 注册在 **atexit**——进程一退隧道必死;但 `save_remote_server` 存的 `http://127.0.0.1:15000` 只有隧道活着才可达,而 `agent_launcher.main` 第二幕只见 url 非空就**整体跳过阶梯**——第二次开机轮询死端口,托盘 Connect 又不重跑阶梯,用户零自愈。安装器默认勾开机自启 ⇒ 验收当天绿、第二天必死。**根修(按 owner 处方):** `resume_attachment` 先探后信——`probe_server(saved)` 活则零动作;死才重跑阶梯;找到服务器**保留原 token**(bearer 凭证与可达地址无关),地址变了才写盘;阶梯也空→原样返回(服务器可能只是没开,轮询继续、托盘 unreachable 诚实呈现,绝不把用户弹回首启对话框)。
- **② 死代码闭环:** `LanDiscoveryResponder` 全仓只有测试实例化——新增 `lan_ip()`(UDP connect 选路 trick,零流量)+ `maybe_start_responder(port, environ, bind)`(env=1 才启,无 LAN IP 静默),接进 `server.py::_start_background_workers` 尾部(读 `_TOFU_RUNTIME_PORT`,与 motion/research 恢复同族 try/except);随 P4 重启自然上线,off-by-default 不变。
- **③ 端口候选:** `_tunnel_once` 单端口尝试拆出,`try_ssh_tunnel` 循环 (15000/15100/15200),占用端口先经免费 bind 探针**秒跳**(白嫖 ssh 8s 超时的时代结束)。
- **测试账:** agent 套件 +12(resume 五态——活=零动作且禁阶梯/死=重指向且 token 不动/同址不重写/阶梯空=附着保留/隧道重建登记;端口三态——忙口跳次候选/全忙净 miss 且不 spawn/早夭逐口杀干净——旧「早夭杀进程」前提被候选循环打破,重钉为「每次尝试必收尸」);server 套件 +4(默认 off/非 '1' 变体全 off/启用即播 LAN URL 且线程活/无 IP 静默)。事故自记:resume 测试草稿误 patch 不存在的 `_pair.save_remote_server`(懒导入在 config)——setattr 不存在的属性直接 AttributeError,当场擒获改 patch 宿主模块。
- **安装包二次重建(带外同法):** `3bbd60ee` 包带雷,`d9b34c71` 重包入店;PYZ 目录字节命中 `_pair` 同法可验。

### 2026-08-03(P4 前置就绪:受控端安装包重建入店——带 _pair.py 的 TofuAgent-Setup-0.16.0-win64.exe 53.16MB;API 被长轮询淹没改带外构建) — epic `pt_59b62951aad2463e`;构建 state=ok(git_sha `3bbd60ee`,cached:false,~3min)

- **带外构建(环境挤压的工程解):** `POST /api/v1/desktop/build` 再试仍 20s 超时(1357 条长轮询 ESTABLISHED 的老问题);winbuilder 本是进程内 daemon 线程,唯一常驻路径就是服务器进程——改 `nohup python -c build_installer('p3-pairing','','agent')` 独立进程跑:state 与产物全在共享盘(data/desktop_toolchain + data/desktop_dist),服务器 GET /build 与面板读的是同一份落盘状态,带外与带内等价。单飞风险自知:服务器侧 kick 此刻不可达,无双跑源。
- **产物核验:** 店单 entries[0] git_sha=3bbd60ee/source=built/sha256=5989e316(旧 0e1658b2 被替);`_pair.py` 入包证据链=spec `collect_submodules('lib.desktop_agent')` 自动收全包子模块(同目录 _probe.py 同款机制,旧包已实证)+ 构建 boot smoke 过。版本号沿用 0.16.0 不 bump(项目惯例=同号重建,旧 0.16.0 本身就是 1a2cca6b 的重包)。
- **P4 真机验收前还剩一枚人工:** 配对端点(P1 路由 b0b42ff9)与面板配对按钮(P2)在运行中服务器(11:16 启动)上**未上线**——需 owner 重启(服务器重启属人工闸门,agent 不自行);agent 安装包本身不依赖重启。

### 2026-08-03(P3 落地:agent 侧配对+阶梯发现——_pair.py(兑换+loopback/LAN/ssh 阶梯+BatchMode 自隧道保活)+ 配对对话框(地址预填可改+精确失败因)+ launcher 首启与托盘统一走 prompt_attachment_flow) — epic `pt_59b62951aad2463e`;新套件 **22 绿** + 邻接 **95+6 绿**;agent 源码烟雾 `TOFU_AGENT_SMOKE_OK commands=19`

- **_pair.py(单模块「找+证+留」):** `exchange_pair_code`(POST /api/desktop/pair,无 bearer——409 invalid_code/429 rate_limited/http_n/transport 分类,代理 401 绝不报成「码错」);`discover()` 阶梯:loopback(1.5s)→ LAN 广播(UDP 15001 magic,响应先过 /api/health 再信——响应器 HMAC 本无共享密钥可验,probe 才是真验证)→ ~/.ssh/config 候选(跳过通配,去重,上限 3 个防首启卡分钟级);`try_ssh_tunnel`(BatchMode+ExitOnForwardFailure,赢则保活在 _ACTIVE_TUNNELS 供轮询走、atexit 收割,输则立杀)。
- **配对对话框(connect_ui.prompt_attach):** 地址栏(阶梯答案预填、可改)+ 6 位码栏,失败给三种不同修法(invalid_code 回面板重铸/rate_limited 等几分钟/其他=地址错);「改用连接行…」哨兵 PREFER_CONNECT_LINE 交给 prompt_connect_line——`prompt_attachment_flow` 统一首启(agent_launcher.main 阶梯后)与托盘重连,两面永不漂移。i18n 键族 desktop.pair.* 双语 14 键。
- **设计稿切片账:** §11.5 P1(b0b42ff9)/P2(2043d23f)/P3(本批)标 LANDED;原规划 _tunnel.py/_discover.py 合入 _pair.py(一个模块拥有整条路径,免三方漂移),对话框策略从「全落空才出现」改为「有答案就预填」。**P4 前置:在店 0.16.0 无 _pair.py,需重建受控端安装包**。
- **测试:** 新套件 22 检查——兑换七态(成功/409/429/http_401 代理/unreachable/timeout/bad_response×2/坏输入)+ LAN 探针(magic 出、广播标志、短包丢弃、未过 probe 滤除)+ ssh 解析(通配/默认块/注释/重复)+ 隧道三态(成功保活/probe 败杀/早夭杀/spawn 失败净 miss)+ 阶梯四序(loopback 短路/LAN 压 SSH/首赢即停/全空返回)+ 候选上限 + 附着流哨兵两态(回退连接行/取消不传染)。邻接 95(pairing 服务端+preseed+probe+connect-line 契约+agent_winbuilder)+ 6(tray i18n AST 棘轮)全绿;烟雾实证 _pair 未把服务端栈拖入 agent 闭包。

### 2026-08-03(P2 落地:面板配对主推——remote 分支②改 6 位配对码(大字+复制+TTL 倒计时),连接行降级高级 details;板面旧三步验收闸门换新) — epic `pt_59b62951aad2463e`;commit `2043d23f`(4 文件 +115/−7);环 **113 绿**(merge+agent-download+devices+probe+pairing)

- **面板形态(§11.2.2 兑现):** remote 受控端流程②由「生成连接行」改「**配对这台电脑**」——`_lcPairCode()` 调 `Api.desktop.mintPairCode()`(新,POST /api/v1/desktop/pair-code),渲染大号等宽 6 位码(.lc-pair-digits 26px/.18em 字距)+复制按钮+每秒倒计时(过期灰显提示重铸,一次性/TTL 诚实不藏)。连接行(lcMintBtn/lcTokenBox)整体降级 `<details>`「高级:连接行(配对码不可用时兜底)」;autoConnect(烘焙安装包)形态不带该兜底(零触控流程保持干净)。
- **i18n 键账:** `local.agentStep2` 已死(全局唯一引用点被替换)→ 原位换配对键族 8 键双语(agentStepPair/pairBtn/pairHint/pairCopied/pairExpires/pairExpired/connectLineToggle/copied);merge harness 符号表自动纳入 `_lcPairCode`(splice 未漏,环绿实证)。
- **板面(owner 指令②):** 旧 human-gated 阻塞「owner 办公机三步手动隧道验收」与设计稿 v4 §11.6 直接矛盾(流程即将被消灭,owner 明言不做)→ 换新闸门:「P3 落地后 owner 真机装新受控端,看阶梯发现是否零提问连上」;P3 落地后本对话自带 timer 盯 agent 注册,不依赖心跳冷却。
- **事故自记:** ①apply_diff 批量调用把 edits 数组与散装 search 参数混传 → 工具报 "File not found:"(空 path),散装参数被静默忽略;教训:apply_diffs 只收 {description, edits[]},多余键不报错也不生效。②i18n.js 遭兄弟会话中途改动触发 freshness 拦截 → 重读冲突区确认目标行未被波及后再改;共享树高频文件(i18n/styles)改前必重读。

### 2026-08-03(P1 落地:配对码 UX——lib/desktop/pairing + 双路由 + 组播发现响应器;设计稿 v4 落盘) — epic `pt_59b62951aad2463e`;commit `b0b42ff9`(5 文件);套件 **9+108 绿**;NEUTER 实证

- **配对码 UX(§11 定案):** lib/desktop/pairing.py(6 位一次性码,300s TTL,单次消耗,3 次尝试锁)+ LanDiscoveryResponder(UDP 15001 广播响应器,opt-in);routes 双端点:`/api/v1/desktop/pair-code`(需认证,面板铸码)+ `/api/desktop/pair`(无需认证——码即凭证,与 `/api/desktop/poll` 同族);消费成功走 `create_key(agents:bridge)` 同铸造流。
- **设计稿 v4 形态(三硬要求落盘):** ①阶梯式自动发现=loopback→LAN 广播→~/.ssh/config 候选→(全落空才问一次);SSH 自隧道降为阶梯一环。②双层优先级=per-user token-baked installer 为主路径(Phase 2 提前转正)+ 配对码为跨机/未烘焙兜底——配对码不是终点。③扩展中继=实测事实收进对比表(SSH 隧道 always-on+零新协议 vs 扩展中继需浏览器常驻+新中继协议);v1 不押其为唯一路径,P5 实测原型后定。owner 手动三步隧道验收(旧阻塞)在 v1 落地后整体作废,§11.6 明告。
- **测试账:** 新套件 9 检查(铸造/消费/单次/锁-out/过期/用户域/鉴权/重复消费)+ LAN 响应器 3 检查;过期 NEUTER 钩出 Python 默认参数绑定陷阱(`mint_code(ttl=_CODE_TTL_S)` 在定义时绑定,monkeypatch 无效 → 改调用时读取)。邻接 108 绿。
- **事故自记:** ①路由错插到 `def desktop_token_mint():` 装饰器与 def 之间,挤出的函数体含模块级 `return` → 整蓝图注册失败 → 404。教训:apply_diff 锚点选 `def X():` 时新内容插在 def 前,永远不在装饰器与 def 之间。②`api_not_found(status=409)` 硬编码 404 吞 status → 改用 `api_conflict`。

### 2026-08-04(预存红闭环:test_restart_smoke::test_sync_route_runs_under_quart——断言钉的是被 api-contract 批 11 有意迁移的裸数组契约;方向对齐而非代码迁就) — 脑派发接我自票 `pt_225df9a89fb14131` **DONE**;commit 见下(2 文件);restart 族 **28/28**

- **定案(纯测试漂移):** 票面写的「测试 DB 缺 schema_meta/paper_library 表」是**伴随噪音**(测试 env 未建表,ERROR 日志但非致命)——真红的断言是 `isinstance(data, list)`;`/api/v1/chat/active` 在批 11(2026-08-01)已**有意**迁到 `api_ok({'items': …})` 信封(routes/chat.py 迁移注释 + static/js/api.js:921 前端配套解包俱在),返回 200 但载荷是信封,旧断言钉的是被取代的裸数组形状。与 segment_gate/open_conv_scroll_once 两票同族:测试滞后于有意契约迁移。
- **修法:** 断言重钉为 `ok is True + items 是 list`(docstring 记漂移史);NEUTER 自明——路由回退裸数组则 `data.get` 当场 AttributeError,针必然咬。信封作为正确形状由同文件 §6 的 404/405 套件交叉钉住。
- **环:** restart 族 28/28(smoke 24 + lock_race 1 + parity 3)。

### 2026-08-03(chromium-libs 双 launcher 不变量闭环:supervisord conf 补 CHROMIUM_EXTRA_LIB_DIRS/fontconfig + parity 守卫钉两边同步;owner 复核擒获的结构性漏洞) — owner 指令三条;commit 见下(3 文件);新套件 **3/3**(NEUTER 精确 2 红)+ restart 族环 **27/28**(1 红=预存,板票 `pt_225df9a89fb14131`)
### 2026-08-03(chromium-libs 双 launcher 不变量闭环:supervisord conf 补 CHROMIUM_EXTRA_LIB_DIRS/fontconfig + parity 守卫钉两边同步;owner 复核擒获的结构性漏洞) — owner 指令三条;commit 见下(3 文件);新套件 **3/3**(NEUTER 精确 2 红)+ restart 族环 **27/28**(1 红=预存,板票 `pt_225df9a89fb14131`)

- **漏洞(owner 擒获):** `deploy/supervisor/tofu.conf` 的 environment= 只有 PORT/BIND_HOST/HOME/LANG——而 restart_15000.sh 在 supervisord 接管时**拒绝运行**(mutex 守卫),一旦切到 supervisor 管理(documented durable 方向),CHROMIUM_EXTRA_LIB_DIRS 无任何路径进服务器,FUSE 修复静默蒸发且所有证据都指向「已修过」。
- **修法:** conf environment= 补 `CHROMIUM_EXTRA_LIB_DIRS` + `FONTCONFIG_PATH`/`FONTCONFIG_FILE`(注释写明根因与双 launcher 同步义务);新守卫 `test_restart_chromium_libs_parity.py` 三针——restart 脚本导出覆盖值+发现约定、conf environment= 携带三变量、**两边指向同一库目录**($HOME/tofu-browser-libs/lib 约定,防一边改路径一边掉队)。NEUTER:回滚 conf → 恰 conf 两针红、脚本针绿。
- **预存红(不属本批,已挂票):** `test_restart_smoke::test_sync_route_runs_under_quart`——测试 DB 缺 schema_meta/paper_library 表,stash 本批 conf 后同签名复现,板票 `pt_225df9a89fb14131`。

### 2026-08-03(libatk 真根因改判:FUSE 坏窗口杀 .so 读取,不是缺包——本地盘库目录 + CHROMIUM_EXTRA_LIB_DIRS 启动接线;我上一批「本机已修复」被 owner 证据链推翻) — owner 复核指令四条;commit 见下(chatui 3 文件 + tofu-search 1 文件);验证 **ldd 10/10 干净 + 净环境发射+字体 10/10**
### 2026-08-03(libatk 真根因改判:FUSE 坏窗口杀 .so 读取,不是缺包——本地盘库目录 + CHROMIUM_EXTRA_LIB_DIRS 启动接线;我上一批「本机已修复」被 owner 证据链推翻) — owner 复核指令四条;commit 见下(chatui 3 文件 + tofu-search 1 文件);验证 **ldd 10/10 干净 + 净环境发射+字体 10/10**

- **owner 擒获的误判:** 我上一批 conda「装进 env」在 conda-meta/history 里只 link 了 ca-certificates/certifi——atk 等 10 包**早已安装**(文件一直在 env/lib,May 10 是包构建 mtime)。三次「libatk cannot open」(12:41/18:44/22:47)与两次成功发射(12:36/19:03)在恒定进程环境下交替,唯一解释=env/lib 所在 **beegfs-fuse 间歇性读失败**(同 PG FUSE epic `pt_4d321fb8f1c2400c` 一族);我首次 ldd 撞上坏窗口(10 个 FUSE 库集体 not found,系统库全正常=签名)。当时唯一确定性来源是 /tmp 本地盘目录,被我当「清理」删了——本机实际与修复前同样脆弱。
- **根修(与 FUSE 天气彻底解耦):** ①`/home/hadoop-aipnlp/tofu-browser-libs`(ext4 本地盘)conda create 全套 chromium 库(含 fontconfig+字体,`etc/fonts/fonts.conf` 俱在);②`restart_15000.sh` 启动环境接线 `CHROMIUM_EXTRA_LIB_DIRS`(chromium_env 与 tofu-search standalone fallback 共同的第一优先免过滤钩子,零 Python 改动)+ `FONTCONFIG_PATH/FILE`(env/etc/fonts 同在 FUSE,坏窗口=全字符空渲染);③install.sh Step 8 扩 FUSE 分支(`df -T` 判 env prefix 在 fuse 上→本地盘建/更新库目录,逐包回退,哨兵实证,导出覆盖值供本脚本点燃验证用,指引其他 launcher);tofu-search install.sh 同步同构分支。
- **验证:** 仅本地目录 LD_LIBRARY_PATH 下 ldd 循环 10/10 零 not-found;`env -i` 净环境(只给 HOME/PATH/本地库/本地 fontconfig)playwright 发射+measureText 循环 **10/10**。install.sh×2 + restart 脚本 bash -n 全过;发现逻辑干跑正确(默认路径 `$HOME/tofu-browser-libs`,`TOFU_BROWSER_LIBS_DIR` 可覆盖)。
- **重启队列(四枚,重启一次全上线):** jsonify `d9f931b6`、429 无限重试 `80431312`、扩展 preseed `d9a0016b`、**CHROMIUM_EXTRA_LIB_DIRS 启动接线**(本批)。运行中服务器(PID 242280,11:16 启动)环境变量无法外挂,浏览器发射在重启前仍随 FUSE 天气波动——这是环境挤压,非代码缺口。

### 2026-08-03(Autopilot 气泡「推理中 0 字符」根修:VU 通道的 thinking_active 相位从未携带 _thinkingLen——worker 通道一直带;穿插的「已发送给 kimi-k3」=轮次切换的设计内相位,服务端零卡死) — owner 截图两问(conv `msdcksqymtglha`);commit `8b18cbc2`(2 文件 +340/−2);新套件 **7 针**(failing-first 对 HEAD 精确 3 红)+ NEUTER 精确;环 **32 + 48 全绿**
### 2026-08-03(Autopilot 气泡「推理中 0 字符」根修:VU 通道的 thinking_active 相位从未携带 _thinkingLen——worker 通道一直带;穿插的「已发送给 kimi-k3」=轮次切换的设计内相位,服务端零卡死) — owner 截图两问(conv `msdcksqymtglha`);commit `8b18cbc2`(2 文件 +340/−2);新套件 **7 针**(failing-first 对 HEAD 精确 3 红)+ NEUTER 精确;环 **32 + 48 全绿**

- **定案(纯前端计数器断线,服务端健康):** app.log 实证——VU 任务 88c1b9bd R1 63s(thinking=6415 chars → 2 工具调用,content=0)、R2 86s(thinking=5221 → 882 字正文),TTFT 11.7s、缓存命中 96-97%,无任何卡死/重试。「卡很久」的观感=两轮长推理 + R1 纯工具调用轮零可见正文,全程唯一可见物就是那行相位。
- **根因(一行缺字段):** worker 通道每个 thinking delta 上送 `{phase:"thinking_active", _thinkingLen}`(sse_pipeline.js:1032),VU 通道(streaming_render.js delta 分支)只上送 `{phase:"thinking_active"}`——相位行绘制读 `phase._thinkingLen || 0`(streaming_ui.js:533)→ Autopilot 气泡整轮焊死「0 字符」。修法=worker 同构:delta 分支累计 `vuMsg._roundThinkingLen` 并随相位上送;phase 分支每个相位事件清零(轮内计数,对齐 sse_pipeline.js:1137)。
- **「已发送给 kimi-k3」穿插=设计内:** 每轮 LLM 派发前 `_stream.py` 发一次 waiting_model 相位(R1→R2 边界一次),下一轮 thinking delta 抵达即被顶回「推理中」——用户看到的来回跳=轮次切换的诚实活体信号,非故障;待新计数器生效后跳变节奏不变但数字会持续爬升。
- **测试:** 新 `test_frontend_vu_thinking_counter.py`(jsdom 驾真 `_handleAutopilotVuEvent`+streaming_ui)——7 针:相位携带长度/绘制 3 字符/爬升 8 字符/轮切换清零重计;failing-first 用 `git show HEAD:` 旧文件实证 3 针精确红(注:绘制针在旧码下经 `msg.thinking.length` 兜底反而绿,session 字段针才是真咬);NEUTER 摘除 `, _thinkingLen: vuMsg._roundThinkingLen` → 双针红;外加源码针防回归。环:VU/相位族 9 套件 32 + i18n/线 parity/段族 48 全绿。

### 2026-08-03(活会话晋升一等凭证:拆除强迫粘贴 cookie 的三道旧闸门——owner「OpenCLI 免配置,为何我们还要用户配 cookie?」定案) — owner 指令;chatui 本批(10 文件)+ tofu-search(0.7.3,2 文件);环 chatui **139/139**、tofu-search **41/41**

- **定案(owner 的困惑是对的):** OpenCLI 的「免配置」=装扩展+浏览器里登录一次;tofu P0 换路后本就走同一条路,但**设置页与两道后端闸门还停留在回放时代**——①卡片开关没粘 cookie 禁拨;②`match_source` 无 cookie 不匹配;③面板主路径是「F12 逐个复制 cookie 值」。路通了,沿途闸门还在收旧票。
- **三闸拆除:** ①开关授权改 `has_cookies || strategy !== 'cookies_replay'`(browser_first/public 永远可拨;replay 无 cookie 仍禁拨——没有可回放的东西);②`match_source` 放宽:browser_first 无 cookie 也匹配、public 永不匹配(初版漏 public 分支,新测试当场擒获);③面板引导重排:主路径「在你的浏览器登录该站→检测到会话→启用」,cookie 粘贴降级「离线兜底(可选)」+ 提示块。
- **活会话探测(新端点):** `GET /api/v1/auth-sources/<domain>/live-session`——桥 `get_cookies` 域级探测,**cookie 值永不离开浏览器**,只比对目录声明的会话 cookie 名(required/recommended);20s TTL+`?refresh=1` 强制重探;卡片懒加载三态徽标(已检测 ✓/未检测到/扩展离线)。tofu-search `_replay()` 对无 cookie 行短路(匿名池加载登录墙=浪费+bot 特征);floor `>=0.7.3`。
- **测试账:** chatui 注册表套件 +8(match 三态/探测三态/TTL 缓存与 refresh);tofu-search +1(无凭据回放腿不发射)。红→绿实证:public 匹配分支首版缺失,`test_match_public_never_matches` 精确擒获。
- **体验对照(与 OpenCLI 完全等价):** 搜索小红书 = 装桥扩展(一次性)+ 浏览器里登录小红书(日常已在)+ 拨开关——零复制粘贴。cookie 粘贴仅在浏览器常离线时才需要。

### 2026-08-03(预存红闭环:test_bundle_manifest_parity——阅读体验 P2-P4 四文件入 deferred 清单但漏 dev-fallback 标签;修法=按清单同序补标签) — 脑派发接我自票 `pt_38989d40003948a2` **DONE**;commit 见下(2 文件);parity+bundle 族 **78/78**

- **定案(纯漏配,非设计):** `paper/reading_xp.js`/`deepen.js`/`notes.js`/`focus_mode.js` 自阅读体验 P2-P4(`fe270ce9`…`7865fa34`)入 `_DEFERRED_FILES` 起就没有 index.html script 标签——bundle 构建成功时无感(strip→feature bundle 重建),但 dev-fallback(bundle 构建失败→按原始标签逐个加载)会静默丢整个 xp 轨(速览/专注/批注/深化四按钮全死)。parity 断言遇首个缺失即报,实测 4 个全缺(脚本复核)。
- **修法:** 按 `_DEFERRED_FILES` 既有顺序把 4 个标签插回 `report.js` 与 `babel.js` 之间(reading_xp→deepen→notes→focus_mode,与清单同序),带 `onload/_onScriptError` 计数钩子与 `?v=20260803a`。反向边(stripped→必须 rebundled)因四文件本就在清单内天然保绿。
- **环:** parity 18/18(原红 `test_every_manifest_file_has_dev_fallback_tag` 转绿)+ bundle 族(freshness/coverage/self_heal/corruption/concurrency/nonblocking/artifacts/scan_surface/model_caps)+ `test_frontend_paper_reading_xp` 共 **78/78**。

### 2026-08-03(composer 左缘锁定:输入框对齐 avatar 线——margin 复演 chat-inner 居中数学 + rail 授权值经 RO 镜像 + drawer margin-right 同步;顺手修「发送键滑进固定抽屉底下」旧疾) — owner 截图指令「输入框左移对齐 avatar,turn ctx 列以后专门放别的」;commit 见下(4 文件);几何套件 **3/3**(432 行 19 计数全 0)+ 邻接环 **58/58**;NEUTER×2 各咬各的

- **定案:** composer 自出生就 `margin:0 auto` 居中于整面板,宽面板下左缘在 avatar 线右 ~(band−872)/2(failing-first 实测 372/432 态偏 26–104px)——输入框在会话下方「游泳」,右侧 turn-ctx 列下方空间永远无法利用。根修=让 composer 坐在阅读列上:左缘=avatar 线、右缘=文本列右缘,全态成立。
- **三件套:** ①`.input-inner` margin-left 直接复演 `.chat-inner` 自己的居中数学(同一 max-width 公式 measure+52+家具+48、同一 24px 内边),宽度=`min(max(52+toolbar-w, min(52+measure, 轨道)), 100%−48)`——两缘对齐、胖工具栏向右长进空 rail 区、永不过界;②rail 授权值(--rail-w/--rail-gap)由 main.js `_mirrorRailGeometry` 经 ResizeObserver 从 `.chat-inner` 计算样式镜像——chatpane 容器查询够不到 composer 子树,也**无法扩展过去**(container-type 的 layout containment 会把移动端 fixed 底栏下拉从 viewport 重挂到带内);零授权默认值优雅降级=旧居中。③`body.ri-open .input-area` 补上与 `.chat-container` 相同的 margin-right——修掉「composer 右半(含发送键)滑进固定抽屉底下」旧疾(1920 侧栏收拢+drawer:右 256px 被遮),并保证两条带任何瞬间同宽(同 transition 锁步)。
- **无望面板地板:** drawer 780+宽侧栏+小窗可把 pane 压到 40px,轨道数学会把 composer 挤成 14px 细条、strip 仪表盘/圆点溢出(修 margin-right 后实测擒获)——`min-width:300px` 地板保住可用文本框,右侧伸进抽屉下方(=旧行为的有界版);移动端 ≤768 复位 0。
- **测试:** 几何套件第三主体「左缘锁」——`|composer.left − message.left| ≤ 1` 432 态全钉;#8 宽度断言重钉为新右缘契约 `max(300, min(max(measure,tw)+52, pane−48))`(读 composer 自己的 inline --toolbar-w,防「reflow 死了断言空转」);_PLANT 注入 `transition:none`(动画是时变装饰,几何断言度量静态值);failing-first 372 红起步。NEUTER×2:N1 恢复 `margin:0 auto` → 精确 233 红且仅左缘断言咬;N2 摘除 input-area drawer margin → 抽屉态双断言 164+180 红。
- **环:** 几何 3/3(432 行:左缘 0/宽度 0/tw 缺失 0/strip/chrome/gauge/nav 全 0);邻接 58/58(composer_floor+kinship+断点协调+coarse 逃逸+scroll_to_bottom 34;request_inspector_p6+滚动锚定族 19;turn_nav 5);bundle 族 44/45——1 红=`test_bundle_manifest_parity`(reading_xp.js 入 bundle 缺 dev-fallback 标签),**预存**(index.html/js_bundler.py 均未被本批触碰,HEAD 定义级复现),板票 `pt_38989d40003948a2`。
- **真机验收:** 15999 临时服务器(TOFU_SKIP_LOCK)双宽度截图——1920 侧栏收拢:composer 左缘贴 avatar 线、右缘贴文本列右缘,rail 下方整列留白待用;1366 侧栏开(pane 1034<1056):rail 折叠进消息头,composer 左缘仍锁 avatar 线。

### 2026-08-03(扩展下载即配对:bridge_preseed 烙进 zip,用户零输入 + libatk 走 conda 实测可行并落进安装脚本) — owner 两指令(「别让用户贴只有后端能铸的 key,下载时直接打进扩展」+「libatk 能不能 conda?行就进安装脚本」);commit 见下(chatui 7 文件 + tofu-search 1 文件);新套件 **6/6**(NEUTER 实证 5 红)+ 邻接环 **69/69**(bridge backoff/async_poll/queue_ttl/chromium_env×2/api-contract parity)

- **扩展零输入配对(与桌面 agent preseed 同构):** `GET /api/browser/download` 打包时为**本次下载**现铸一把 `agents:bridge` key(secrets 哈希落盘,旧 key 永远无法再物化,故每次下载铸新 key)+ 以 `request.host_url` 为 serverUrl,注入 `browser_extension/bridge_preseed.json`;铸 key 失败 fail-open 退化为无 preseed 的 zip(弹窗字段留作修复路径,不挡下载)。`background.js` 新增 `adoptBridgePreseed`:**只填空槽**(bridgeSecret/serverUrl 已配置一律不动,重下载不砸好配置),先于 autoDetectServer 执行,无 preseed 文件(repo dev 载入)静默跳过。弹窗文案「required」改「pre-paired by the downloaded package」。
- **libatk 定案(conda 可行,已实测三层证据):** ldd 实测无头 shell 缺 10 个 .so → 按单抓药 conda 装 tofu env(事务=10 新包+ca-certificates 补丁级,零移除)→ `chromium_env.chromium_lib_dirs()` 哨兵自动发现 → **生产路径点燃**(ensure_chromium_env→launch→渲染)'POOL_PATH_LAUNCH_OK 148.0.7778.96'。**本机已修复**;运行中服务器池 60s 失败冷却(playwright_pool.py:709)后下次启动尝试自愈,**无需重启**。
- **历史坑形:** env 里 gbm-cos7/nss/部分字体是旧 build,atk/xorg/fontconfig 全缺——Step 8 组装原子失败或清单演化从未回流旧 env(装的机会只有首次 install)。修法(install.sh Step 8):组装失败→**逐包回退**(一个包下架不连坐全组)+ 哨兵证据验证(chromium_lib_dirs 同款探针,不信退出码)+ 下载后**点燃+字体渲染验证**(对齐 uv 路径既有检查);tofu-search install.sh 同步(conda-meta 门控,非 conda env 时指路 install-deps);chromium_env.py 修复提示对齐完整清单。全清单(含 mesa-libgbm-cos7)在本镜像 fresh-create 干跑可解。
- **部署注记:** preseed 在 routes/browser.py——**重启前**从服务器下载的 zip 仍无 preseed(11:16 启动的旧码)。等重启的修复累计三枚:jsonify `d9f931b6`、429 `80431312`、本批 preseed。

### 2026-08-03(error.log 全量审计 + 桥接 key 重铸:两枚修复卡在重启上,401 垃圾源消停) — owner 指令「查后端日志还有哪些要修」;纯审计批 + 1 枚 key(走服务器 API);curl 实测双绿
### 2026-08-03(error.log 全量审计 + 桥接 key 重铸:两枚修复卡在重启上,401 垃圾源消停) — owner 指令「查后端日志还有哪些要修」;纯审计批 + 1 枚 key(走服务器 API);curl 实测双绿

- **审计结论(6.5 万行 error.log 按签名归组):** 无新的未修代码 bug。两枚已修未上线——①`d9f931b6`(15:55,jsonify NameError,blocking 路径 PUT/PATCH conversations 500)②**owner 复核时擒获我漏报的 `80431312`(12:34,429 无限重试「永不打断对话」owner 指令)**——运行中服务器 11:16 启动,两枚都没吃到;13:02 与 15:33–15:50 两轮饱和风暴实测仍是旧行为(120s budget 升级 + TURN AUTO-RETRY 3/3 封顶,全天 58 次轮重试、239 次饱和升级,但 3/3 后均恢复,无硬错误信封打断)。已修已验证:Bad range(a76340a4 后 11:00 起零复发)。环境类不动:cgroup 挤压/PG FUSE 断连(板票 pt_4d321fb8f1c2400c)/Bing 软封改版(20 次)/pymupdf4llm `min() empty`(上游边界,有 raw fallback)。**磁盘卫生:logs/ 已 12G**(app.log.2026-07-27 单日 9.1G、postgresql.log 1.4G 未轮转),owner 暂缓清理。
- **桥接 401 闭环:** 每 ~5 分钟一次的 `/api/browser/poll` 401 = owner 的 Chrome 扩展(Windows Chrome 150,经隧道,peer=127.0.0.1)`has_header:false` 空手轮询——fail-closed 闸门按设计执法(桥接命令可执行 JS/读 cookie 罐,代理把来源全洗成 loopback,故桥端点永不看 IP)。重铸 `k_d1ea3188`(POST /api/v1/desktop/token,走服务器进程缓存+落盘同生,避开 8-02 侧进程裂脑坑);curl 实测:带 `X-Bridge-Secret` → **200 `{"commands":[]}`**,对照无头 → **401**。待 owner 把 key 贴进扩展弹窗 Bridge Secret 框。
- **Playwright 定案(不修):** `libatk-1.0.so.0` 缺失是 OS 共享库问题(pip 包与 chromium 二进制俱在),正经修法 `playwright install-deps` 需 sudo;池子自带 60s 失败冷却(playwright_pool.py:709)+ SPA 档降级 HTTP 抓取,搜索照常——接受降级。

### 2026-08-03(turn nav「消失」定案 + 状态条错位根修:条带搬进 .input-inner 共享 composer 宽度轨——仪表盘/圆点边缘从此就是输入框边缘) — owner 截图两问(「为什么 turn nav 消失了」+「它们没对齐输入框,难看」);commit 见下(4 文件);几何套件 **3/3**(432 行)+ 邻接环 **20/20**(P6/composer_floor/manual_compaction);NEUTER×2 各咬 432/432

- **Q1 定案(消失=搬家+设计阈值,非回归):** ①圆点自 `4dee9231`(7-28,conversation chrome converges into a status strip)从 `.chat-wrapper` 右缘绝对浮标(right:8px / top:50%)搬进 `#convStatusStrip`(输入框上方条带的右格)——owner 还在旧位置找它;②`buildTurnNav` 在 **<2 个 user 轮**时清空(turn_nav.js:105,阈值老到 `9ded44f5` 已在)——owner 工作流=一任务一会话(近期会话 msgCount=2 占大半,即 1 user 轮),圆点天然几乎不现身;截图会话本就是新会话(0 消息);③渲染无回归:几何套件对**生产构建器**种 5 轮,432/432 状态圆点皆在。
- **Q2 根修(错位):** 根因=条带与 composer **两条独立宽度源**——条带 `max-width:calc(--msg-measure + 52px)`=872px 定死,composer `.input-inner` 走 `--toolbar-w`(main.js 动态测量:max(工具栏自然宽,820 地板)+边框,钳 vw-48);实测 1365 视口:composer=822 vs 条带=872 → 左悬 **26px**/侧,工具栏更宽时错位任意漂。修法=**结构性共享轨道**(非调常数):条带搬进 `.input-inner` 作首子、删除自有 max-width——宽度只剩 `--toolbar-w` 一个源,任何视口/工具栏内容/主题下边缘天然齐。`_reflowToolbar` 的 9999px 测量 blowout 在同一 JS task 内同步收回,无闪烁(与 .input-inner 自身同理)。
- **守卫:** 几何套件新增 `stripInTrack`(父子结构)+ `stripMatchesComposer`(边缘 ±1px)双不变量,`stripAboveComposer` 改对可见的 `.input-box` 量(条带入内后 .input-inner 顶=条带顶,旧判据恒假)。**NEUTER×2 实证:** 独立 `max-width:600px` → 432/432 红;条带移出 .input-inner → 432/432 红(inTrack=False)。
- **真机实证:** :15000 同视口(1365,侧栏开)复测——仪表盘左缘 460→488px,与 composer 左缘精确重合(26px 外悬消除)。p6 静态 pin(strip 必须在 .input-area 内)零改动通过。

### 2026-08-03(预存红×2 闭环:test_frontend_open_conv_scroll_once——双层漂移:harness 缺符号只是表层,场景钉的「开卷 force-scroll 一次」契约已被 no-autoscroll owner 指令整体取代;套件按 widening 的现代职责重建) — 脑派发接我自票 `pt_7ba8b64906b54fd5` **DONE**;commit 见下(1 文件 +157/−77);本文件 2/2 + 邻接环 **18/18**(scroll/render 族 7 套件)

- **第一层(harness 漂移,票面诊断):** 渲染分解把 `_explicitBottomLatch` 挪进 streaming_render.js,本套件 harness 仍单 eval chat_render.js → ReferenceError。照 bg_refresh_scroll.py:306 的既有解药:`sources_defining` 按符号从 bundle 清单解依赖 + **合并为一次 eval**(let 不出自己的 eval 作用域,分次 eval 照样 ReferenceError)。
- **第二层(契约漂移,深挖才发现):** 修完符号仍全红——DBG 实测 render1 后 forceCalls=0/latch=null/scrollTop=0。实测定案:5286eada 时代「No-auto-scroll-on-OPEN」owner 指令已把开卷分支改为**永不 force-scroll、只 latch**(`!_sameConvDom || _initialSwitchLoad` → no-scroll + latch),本套件场景前提「render1 把读者送到底部」在产品上不再可能发生,其 NC(去 widening→render2 再吸底)永不可能咬——「开卷不自动滚」本身已由 test_frontend_open_conv_no_autoscroll.py 钉死(含分支 NC),本套件旧场景整体失效。
- **重建(disjoint 原则):** 本套件改钉 `_openAlreadyPositioned` widening 的**现代职责**——开卷进行中读者手动滚到近底部,同批后续 full render 的 innerHTML 清屏会把 scrollTop 归零(跳顶),唯有 widening 解除 `!_readerNearBottom` 否决让锚点被捕获、`_restoreScrollAnchor` 把读者钉回原位;开卷结束后 widening 自动脱离(近底部后台 render 恢复 force-scroll,钉「作用域=一次开卷,非永久抑制」);另钉 latch 每次开卷重新武装(切走再切回)。新场景 9 针:render1 三针(不滚/留顶/latch)+ render2/3 锚定复位+零 force + 开卷后后台恢复 force +  fresh open 两针。
- **harness 重建(对齐 no_autoscroll 参照模式):** 全局预置 stub 对 eval 内 let/function 词法绑定**静默失效**(实测:产品 latch 词法绑、断言读全局=null)——stub/状态访问器全部改经 `__H` 桥**追加进同一 eval 文本**闭包词法绑定(installStubs/openScroll get-set/lazyConv/lazyFrom/setRenderMessage)。此坑与 bg_refresh 注释记载同源:「a second eval reading _lazyConvId throws ReferenceError」。
- **NEUTER 实证:** widen 变体恰咬 `open_render2/3_anchor_restored` 两针红、其余 7 针全绿——widening 承重精确、无误伤;neuter 锚串缺失会 FAIL-fast(`neuter_widen_not_applied`)。
- **环:** 本文件 2/2;邻接 scroll/render 族 7 套件 18/18(open_conv_no_autoscroll/full_render_scroll_anchor/bg_refresh_scroll/scroll_bottom_latch/turn_nav_navigation/scroll_once/segment_gate)。

### 2026-08-03(预存红闭环:test_frontend_rendermessage_segment_gate——断言钉的是被 752927bd 有意废除的「抑制契约」;方向对齐而非代码迁就,我首版重钉亦踩懒加载现实) — 脑派发接我自票 `pt_d2e3c981493849cc` **DONE**;commit 见下(1 文件 +39/−8);本文件 1/1 + 邻接环 **60/60**(segment/thinking 族 12 套件)

- **定案(纯测试漂移,非产品 bug):** 红的 `on_no_duplicate_standalone_thinking`(segments 在场时 standalone thinking 计数必须=0)钉的是旧「timeline 渲染即抑制 msg.thinking」契约;产品 commit `752927bd`(2026-07-23「stop dropping the TERMINAL thinking」)已**有意拆闸**——timeline 刻意跳过一切 terminal 段(tool_rounds.js:3074 `if (s.terminal) continue`),`msg.thinking`=终端轮推理,批后无段,从不内联——旧闸一在,每个多工具回合的终端推理被静默丢弃(conv mrx3tv0ha8ffkc「reasoning_content missing」的复发根因)。新契约由 test_frontend_terminal_thinking_render.py 钉死(NC:回加旧闸→块消失),本套件只是滞后未跟。
- **修法(先例=「别搞开关,直接改测试」):** case-1 重钉为新契约——standalone 块恰一次(0=752927bd 丢弃回归,2+=真重复);块身份经标签字符元 `(24 chars)`(=msg.thinking.length)钉到 msg.thinking 字段;per-batch 推理在 timeline 内恰一次。「不重复思考」的原始意图以精确现代形态保留,docstring 补漂移史。
- **我首版重钉亦错(记档):** 先加了「终端文本恰一次」两针即红——实测块体 `.thinking-text` **按设计发空**,文本在 toggle 时经 `_toggleThinking` 懒注水(兄弟套件早写明「that is HOW the terminal reasoning text reaches the DOM on toggle」)——终端字符串无内联出现可计数,改钉标签字符元证块身份。教训:给懒加载渲染面写断言,先看兄弟套件怎么钉同一面。
- **NEUTER 实证(内存级,不动盘):** eval 源回加 `&& !_segTimelineRendered` → 恰两针新断言红、其余全绿、且旧断言在此产品 bug 下**反而会绿**——漂移实锤(旧断言把 bug 钉成正确)。
- **环:** 本文件 1/1;邻接 segment/thinking 族 12 套件 60/60(terminal_thinking/segment_timeline/streaming_interleave/failed_turn_actions/trimmed_tail/thinking_swap/thinking_zone/segments_not_echoed/segments_cache_recovery/edit_realigns/paper_review_segments)。

### 2026-08-03(流式相位文本「✏️ Applying changes」三连根修:emoji 全族退役 + apply_diff 正名 Patching files + tool_exec 相位行只在真有工具在飞时渲染 + 相位标签 i18n 结构化) — owner 截图两问(「这是什么工具,为何只看到 read_files」「工具记录同步延迟也要修;打补丁就写打补丁,别用 emoji」);commit 见下(13 文件 + i18n.js 经兄弟 `88eb3302` 卷入);新套件后端 **6** + 前端 harness **+7**(failing-first 精确 10 红起步);NEUTER×5 各咬各的,并顺手擒 ReferenceError 隐患 1 枚;环 **159/159 + i18n 族 105/105 + 终批 46/46**

- **病灶解剖(三个发射面共用一组标签):** ①`tool_dispatch/_labels.py::_TOOL_EXEC_LABELS` 全族带 emoji,apply_diff 叫「Applying changes」——打补丁不说打补丁;②`emit_tool_exec_phase` 的 tool_exec 相位在「工具全部落定→下一轮相位抵达」窗口期成为**陈旧相位**,前端 `!hasActiveSearch` 门放行渲染——工具已完工相位行仍称「正在修补」,用户对着已落定卡片找不存在的活动工具(「只看到 read_files」的真相);③`_emit_tool_round_phase` 的 toolContext 后缀把**上一轮**工具名以预拼接英文+emoji 字符串直出,中文 UI 里整条「正在分析结果…✏️ Applying changes」读起来像有工具正在跑。
- **修法(根因,非贴纸):** 后端——标签全族去 emoji、apply_diff(s) 正名 'Patching files'、MCP 兜底去 🔌;round-open 相位新增 `toolContextTools`(原始工具名数组,英文 toolContext 保留为无头兜底);poll 通道相位快照同步转发;events.py PHASE spec 文档化三字段。前端——i18n 新增 `stream.phase.toolExec{,Multi}`/`stream.phase.toolContext` + `tool.label.*` 17 键双语(修补文件/读取文件…);streaming_ui 新增 `_toolLabelText/_toolExecPhaseText/_toolContextPhaseText` 三助手;tool_exec 相位行**只在某轮真在飞**(searching/executing/submitted/pending_approval/awaiting_*)时渲染——陈旧窗口自然落空到诚实分支(等待中/推理中/none);llm_thinking 后缀改从 toolContextTools 组合(「上轮:修补文件」);sse_pipeline/streaming_render 白名单透传;health_stream_timer stall 横幅 tool_exec 分支同步本地化(typeof-guarded 降级英文 detail)。
- **N4 NEUTER 的意外擒获(记档):** 初版 `_toolExecPhaseText` 无 tools 兜底调 `_phaseDetailText`——该函数**嵌套在 updateStreamingUI 内**,模块级助手根本够不到;生产上 VU 转发帧(detail-only,无 tools 列表)+非 searching 在飞状态(submitted/pending_approval)即 ReferenceError 炸整帧。NEUTER 复演当场炸出,改自包含兜底 `(p&&p.detail)||''` 并新增 `phase_tool_exec_no_tools_fallback` 针钉死。**教训:harness 必须覆盖「无 tools 列表」帧;NEUTER 设计要连兜底路径一起变异。**
- **预存红处置(第二起 finish_info 漂移):** `test_i18n_boot_keys_discovery` 钉 `finishInfo.cbState.` 在启动关键动态前缀集——调用点已被兄弟 `48c1651f`(Epic-E sub-8 延迟化)搬到 DEFERRED `finish_info_rich.js`,正确缺席启动集;纯净 HEAD worktree 实证预存后**重钉**(删 stale 断言 + 增钉本批新前缀 `tool.label.`,契约意图不稀释)。另擒本批自引静默漏一枚:`t('tool.label.'+name)` 经中间变量调用=启动键静态扫描不可见(boot pack 丢全族),改内联字面量——`lib/i18n_boot_keys.py` 文档早写明此坑(动态调用必须内联)。
- **共享树卷入(判例第四起):** 本批 `static/js/i18n.js` 修改被兄弟 `88eb3302`(desktop proxy-URL 批)整段卷入其 commit——逐行复核与本批规范**逐字节一致**(3 相位键 + 17 工具标签 + 注释块),内容已回家;本 commit 只收其余 13 文件,JOURNAL 兄弟条目同期已由兄弟各自入库。

### 2026-08-03(代理地址连接行陷阱四层根修——owner「配好受控端仍无法启用」定案:codelab SSO 代理 401 墙,轮询零到达) — epic `pt_59b62951aad2463e`;commit `88eb3302`(12 文件 +644/−18);环 **12 新 + 48 jsdom + 106 邻接 + 117 agent/poll/parity + 56 bundle/i18n + 76 build-workflow 全绿**;agent 源码烟雾 `TOFU_AGENT_SMOKE_OK commands=19`;bundle-f2cee90b / i18n-zh-ee90459a 重建(gitignored,随重启自愈)

- **定案(实测,非推测):** owner 托盘截图 `Server: http://5665bc99-…mlp.sankuai.com`——连接行是**经 codelab 预览代理访问面板时**铸的,mint 取 `request.host_url`(浏览器可达≠agent 可达)。实测该代理对 `/api/desktop/poll` 与 `/api/health` 一律 **401 {"error":"Unauthorized"}**(SSO 边缘拦截,请求根本不到 Tofu);服务器 access.log 零 agent 轮询、注册表空——面板「未运行」+ 开关禁用是完全正确的显示,坏的是连接行的地址半。
- **四层各修一处沉默:** ①status 载荷新增 `server_url_reachability`(loopback/private/public 分类,`_host_reachability` 纯函数)+ `bridge_tokens_issued`——面板在**铸行前**对 public 主机亮警告(不挡铸行,公共主机未必有墙),并在「已铸行但零 agent 到达」时给出一句诊断(替代孤零零的死开关);②connect 对话框粘贴即探 `GET {url}/api/health`(唯一开放端点,`lib/desktop_agent/_probe.py`),失败给精确原因(http_401/not_tofu/timeout/unreachable…),第二次点击强制保存(服务器重启中不该死挡);③轮询 401 用 api_error 信封形状区分 **Tofu 的 401(密钥错)** 与 **网关的 401(地址错)**——旧日志把所有 401 都说成「bridge auth failed」,把 owner 引向错的那半行;④`run_agent(on_status=)` 转移回调(只在状态迁移时发,绝不每轮询发)喂托盘新「链路」行(已连上/鉴权失败/被代理拦截/连不上)——这条行本会五秒定案,而不是服务器侧翻日志。
- **测试:** 新 `test_agent_reachability_probe.py`(12:分类/探针/信封/on_status 迁移含「5 次轮询只 1 次回调」);agent-download harness §6(代理警告三态 + awaiting 提示三态 + 缺席字段零渲染);merge 套件 splice 补 `_lcProxyWarnHtml`/`_lcAwaitingAgentHtml`(splice 漏符号=全 ReferenceError 红,教训再记)。
- **环境插曲(不挂票,预存族):** ①pytest 插件自加载在本环境双坏——napari 链(GL ES 2.0 not found)+ addopts `-p pytest_timeout` 与 entry-point 'timeout' 重名注册;全仓库 Makefile 调用同坏;绕过=`-o addopts= -p no:napari`。②build 端点连试三次超时:服务器被 **1357 条 ESTABLISHED 长轮询** 淹没,TCP 接而不派;agent 安装包重建(含 probe/链路行)**顺延到即将到来的重启后**(现网 agent 工件=1a2cca6b 17:37,即时解锁不依赖新包)。③现时 manifest entries 为空但 owner 已下载 53MB 包——下载走 find_for_platform 旁路,留观。
- **owner 即时解锁(已回):** 办公机 `ssh -L 15000:127.0.0.1:15000 <codelab>` → 浏览器开 `http://127.0.0.1:15000` → 面板生成连接行(此时 host=loopback,无警告)→ 托盘「Connect to a different Tofu…」粘贴 → 注册到达,开关自动可拨。

### 2026-08-03(站点知识层 P2 落地:注册表泛化——设置页「站点接入」徽章化 + access_strategy 成为 tofu-search 路径序数据 + 登录 hints 单源化) — 脑派发 epic `pt_689b73b305fe4810` **DONE**;tofu-search `056a23b`(0.7.2,6 文件 +355/−54)+ chatui 本批(10 文件);环 tofu-search **40/40 + 全量绿**、chatui **55+53+28 全绿**

- **owner 之问的最终兑现:** 「美团/小红书 toggle 以后还要吗?」——设置段题「需要登录的来源」→**「站点接入」**(描述同步改为「每内化一个网站就追加一条」);每张卡片新增两徽章:**策略徽章**(browser_first 浏览器优先/cookies_replay 凭据回放/public 公开站)+**知识徽章**(医生钉过抽取器=「已内化 vN」金章,已连接未钉=「仅凭据」);描述里的 cross-reference(privateHostsDesc)同步改名。
- **策略是真数据不是装饰:** tofu-search 侧 xhs 引擎 + fetch/core 双消费 `access_strategy`——browser_first=现状(默认/缺省,零迁移),cookies_replay=旧序(池回放主/浏览器兜底,适合风控迟钝或浏览器常离线的站),public=身份路径全关(引擎 skip/fetch 当建议项走匿名管线)。fetch core 重构为 `_browser()`/`_replay()` 闭包共享登录墙判决,browser 腿诊断 reason 随策略命名(auth_source_browser_first|fallback)。登录墙契约测试按闭包形态重钉(意图不稀释:墙检查先于净文本返回 + 成功路必须消费判决闭包)。
- **域名清单数据化(票面另一半):** interactive_login 的「登录完成」cookie hints 改读注册表行 `fields` spec(与设置 UI 同一单源),模块表降为 standalone 兜底——登录流最后一个逐站硬编码消除;chatui `get_source/match_source` 全行合并目录 spec(login_url/fields/access_strategy),设计稿 §3.1 消费链兑现(library 不认识的键原样透传)。
- **chatui 注册表(lib/auth_sources.py):** DEFAULT_SOURCES 带 `access_strategy:'browser_first'`(老 auth_sources.json 零迁移);upsert 校验三值并落库;`_redact` 加策略投影 + 知识徽章(list_sources 一次性读 site_knowledge 注入,64 行不逐行读盘);路由 upsert 透传 + api_meta 同步;i18n 五新键双语;settings.css 徽章样式复用 field-badge 形制。
- **测试账:** tofu-search 新套件 10 针(三路策略×双消费面 + hints 注册表/兜底);chatui 新套件 8 针(默认/校验/持久化/老行缺省/spec 合入全行/别名匹配/徽章注入/redact 不漏 cookie 值)。事故自记(两起,均当场擒):①pytest fixture 里给**函数名**赋值属性而 yield 的是 list——fixture 命名空间对象化;②新套件调了不存在的 `fetch_core.fetch_url`(真名 fetch_page_content)——**记档:写跨包测试先核被测函数真名,grep 一遍胜过猜。**
- **余量(另起切片,未挂票):** `knowledge.detail` 结构化笔记提取、`stats`/RECORD 台账落盘——设计稿 §6 P1 行已记档。真机验收:设置页段位改名+徽章可见;切换某站 access_strategy 后搜索路径序即时改变(日志 via=pool/browser 可证)。

### 2026-08-03(turn-ctx 右栏「100% 缩放完全消失」双根修:授予阈值从「舒适+192px 奢侈缓冲」改为按 700px 文本地板推导 + fold 兜底行零宽出生即瞎) — owner 截图指令「80% 能看到,100% 完全消失,重新设计布局」;commit 见下(5 文件);几何视觉套件 **3/3**(216 状态×2=432 行全绿)+ 邻接 **28 套件 111/113**(2 红皆预存,HEAD worktree 实证挂票)

- **根因一(阈值失灵):** 右栏由 `@container chatpane (min-width:1368px)` 授予,1368=满配 1176px(820 文本+52 头像+24 缝+232 栏+48 边距)**外加 192px 奢侈缓冲**——owner 的面板在 100% 缩放恰为 ~1365 CSS px(2048 物理/1.5 DPR,侧栏收拢),**差 3px 够不到**;80% 缩放等效面板 1707px 才过线。修法不是调常数而是改推导:阈值=几何守卫已钉的 700px 文本地板+固定家具=1044px,授予点 1056px(+12px 滚动条/取整 slack)——1056–1175 面板得右栏+零外边距(文本 700–820px),更宽面板自动恢复外边距;CSS 注释与 `_RAIL_MIN_PANE` 携带同一笔账,两者必须同动。
- **根因二(fold 兜底从未工作,实测擒获):** 修完阈值后实测发现「低于阈值时 fold 一行摘要」**自出生(c03e0619 三轨网格重构)就不可见**——fold 作为 `.message` 直接子元素被网格自动摆进**零宽右栏轨**,`display:flex`、文本正确、渲染宽度 0(实测 {x:939,w:0,h:12});几何套件的 `lost` 断言只查 display+文本,对此结构性失明。owner 的「完全消失」=阈值失灵+兜底失明两症叠发。根修=`renderTurnCtxNote` 返回从拼接字符串改 `{fold, rail}` 两件(fold 缝进 `.message-content` 头部与正文之间——in-flow,`margin-bottom` 唯一有意义的DOM家;rail 保持 `.message` 网格子),chat_render 分拆两处 splice 并对 node 桩(返 '')防御;套件 `lost` 断言升级为**宽度感知**(foldW≥20),按面板≥200px 划界(200 以下抽屉缝中文本列自身已是竖条,设计自述「74px 时无可读物」)。
- **测试账:** 几何套件 3/3——432 行:0 裁剪/0 宽敞缺栏(192 roomy 行)/0 膨胀/0 上下文丢失/文本列 717–820(界 700–920)/0 composer 失同步;新增 vw=1100 边界宽度(侧栏收拢→面板 1100 刚过授予点)。failing-first=fold 修复前实测 fold w=0<20 必红;宽度感知断言首跑真咬 26 行(抽屉 0–62px 面板)+2 行(156px 面板 tofu 内馅后文本列仅 10px),划界 200px 后全绿——**断言咬合力三段实证,非调参消红**。邻接 28 套件 111/113:2 红皆预存(segment_gate 的 on_no_duplicate_standalone_thinking;open_conv_scroll_once×2=harness 单 eval chat_render 缺 streaming_render 符号 _explicitBottomLatch,bg_refresh_scroll.py:306 已记同解药 sources_defining),均纯净 HEAD(+node_modules 软链)worktree 复现,板票 `pt_d2e3c981493849cc`/`pt_7ba8b64906b54fd5`。
- **真机实证:** debug/_turn_ctx_preview.py 对线上 :15000 植物探针轮——1365px(=owner 100% 面板):右栏完整显示(Kimi K3+MAX+chips+workspace);1100px:右栏在、文本 756px;1000px:右栏折叠,fold 行「Kimi K3 · MAX · 4 tools · 1 ws」可见于消息头下。元素级截图 `/tmp/turn_ctx_el_{1365,1000}.png`。
- **方法记档:** ①owner 报「重新设计布局」,实测定案=架构(grid 轨+容器查询+fold 兜底)本身健全,失灵的是**标定**——按项目目标「分析根因后选最完美方案」,正解是把授予条件锚到几何地板而非发明第二套紧凑栏变体(1012–1080 的边际收益不抵第二视觉变体的维护成本)。②共享树又遇兄弟并写 styles.css(写闸 stale 拒写,重读核对己方 hunks 完好后再落)—— freshness gate 按设计工作。

### 2026-08-03(订阅登录页重设计:出口不可用改 action-first「安装受控端」callout + 三步流程条 + egress 全家话术改「受控端」;顺手修 parity 半迁移预存红) — owner 截图三指令(①按钮丑 ②只需装受控端就直接提示装 ③界面重设计突出重点);commit 见下(8 文件);环 **42+39 绿**(egress_line/agent_btn/onboarding/panels_parity/mcp_oauth_deferred + i18n 五套件)

- **定案:** ①「获取桌面代理」小按钮 inline 挤在红字旁 → 改为琥珀色 action-first callout:标题「需要安装受控端:服务器无法直接访问外网」+ 副行「在你的电脑上安装轻量受控端并连接后,登录请求就会经你的电脑发出」+ 右侧实心深色「安装受控端」按钮(仍深链 Local Control 单一安装面,点击后 3s 自愈轮询不变);②术语统一「受控端」(与本机控制面板一致)——egress 五状态文案从「出口/桌面代理」改「网络/受控端」;③面板顶部新增三步流程条(点击登录→完成授权→自动完成,Token 过期自动刷新生平第一次可见),中国提示从橙色长文收窄为琥珀横幅,底部「使用说明」四点(与流程条+卡内兜底提示信息重复)整节删除,键 `oauthInstructions`/`oauthNote1-4` 退役。
- **顺手修预存红:** `.oauth-egress-agent-btn` 规则误置 styles.css(egress epic 批引入),违反 settings_panels_parity 半迁移闸(`.oauth-` 只准住 settings.css)——HEAD 实证红(13 套件中唯一红);随本批归位 settings.css 并升级为 callout 内实心主按钮样式,闸转绿。
- **契约保留:** 按钮 id/`egressGetAgent(Title)`/`egressUnavailable` 键与 `oauth-egress-bad` 容器类全部保留 → egress_line 套件零改动通过;agent_btn 套件 NEUTER 锚点重钉新 callout markup 并加 sub/callout 双针;onboarding R2 键集补 `egressUnavailSub`。
- **视觉验收:** browser_preview_page 驱动真实 oauth.js 渲染五状态矩阵(unavailable 双卡/直连/经受控端+pin/未开 egress/检测中)+ 560px 窄视口换行验证,全绿后删预览文件。
- **共享树处置:** i18n.js/styles.css/JOURNAL.md 含兄弟未提交改动——本批走 blob 级暂存(HEAD+本批 hunks 铸 blob 入索引,staged 仅我 8 文件),与同日 `7c87f379` 记档的 pathspec 整文件吞事故正好对偶:blob 法保证只有自己的 hunks 进场。

### 2026-08-03(有损回声孪生收敛:无损子集 subsumption + sidecar 嫁接——裁剪窗口回声不再永久双气泡;owner 复验擒获的第三形态) — owner 复验指令;commit `2696f2c1`(2 文件 +338/−6);套件 +6(failing-first 实证契约红);NEUTER×1;邻接环 **20 套件 137/137**;真实数据 fold 8→7 + 生产缝克隆行 compute+persist 全通

- **owner 复验擒获(错误孪生修完后的第三形态):** `mscns5i0fcofgl` msgs 2&3 仍是同任务双气泡(task `b2d4edb9`,重试轮)——content/thinking/segments 逐字节相等,但**前端本地裁剪窗口**使 echo 只带最近 20 轮(keeper 30 轮的严格子集),apiRounds 仅差 keeper 侧 settle 补戳的 `cost` 键,字节相等判定永不收敛(实测 HEAD fold count=0)。三形态谱系自此完整:①字节级回 PUT 回声(原 collapse)、②错误孪生(终态分歧→fold-fill)、③有损回声(裁剪子集→subset+graft)。
- **修法:** ①无损子集 subsumption——toolRounds 按 `toolCallId` 匹配、apiRounds 按条目字段包含匹配,公共字段经 `_twin_round_canon` 规范化(瞬态键+null 脚手架+`_swarm:false`,与前端 `_canonRound` 同集)逐字节相等即判 subsumed;真分歧(未知轮次/公共字段不等/非白名单 twin 侧真值键)照旧保留,64 组实测防丢数据不稀释。②`_twin_graft_payload`——两条丢弃路径先把 twin 侧数据 fill-absent 嫁接回 keeper 再丢:round 级六键(实测只有 `receivedAt`/`emittedAt` 是真值,`approvalId`/`approvalMeta`/`guidanceId`/`_swarm` 是 null/false 脚手架,规范化同 `_canonRound` 丢弃)+ 消息级 `thinkingDepth`;永不覆盖 keeper 已有值、永不突变入参。
- **查根定案(非后端缝):** `receivedAt` 是客户端入口钟服务端本不可能有;`emittedAt` 在 wire 事件不在持久化轮;approval/guidance 在 tool_result 结算时被 reducer 置 null(设计);`_swarm:false` 是默认脚手架——keeper 缺这些键全属正当,嫁接是正解,后端无丢键 bug。`thinkingDepth` 消息级近渲染死键(branch_stream 写、无读者),fill-absent 嫁接零成本保全。
- **生产实证纪律:** 实行未动——在跑服务器还是旧码,提前治愈会被客户端 pushback 经旧码 PUT 缝再污染;收敛已用克隆行经生产缝 `_reconcile_conv_on_get_blocking` 实证(serve 7+persist 7+嫁接齐),重启后首个 GET/PUT 缝自然持久化。**事故自记(第三次同类):** 本 turn 又一次 apply_diff search/replace 写成相同文本(no-op edit),外加一次测试自证陷阱——把 receivedAt 改成 keeper/twin 不等值测「never overwrite」,恰触发公共字段分歧闸使 fold 整体不触发,改单元级钉嫁接 fill-absent 才真钉住语义。
### 2026-08-03(预存红闭环:test_live_retry_preserves_task_id——fake _Dispatcher 补 .slots;附:pathspec 提交卷入兄弟未暂存行事故复盘) — 脑派发接我自票 `pt_17117aca9b544702` **DONE**;commit `7c87f379`(1 文件 +4);本文件 **6/6** + 同缝(fake dispatcher 族)**50/50** 绿

- **定案:** 测试的 fake `_Dispatcher` 缺 `.slots` 属性,而 `dispatch_stream` 的 big-prefix gate 对每个请求读 `dispatcher.slots` 统计该模型 key 数(单 key → gate no-op)——AttributeError,HEAD 同签名可复现,纯测试侧滞后于 gate 引入。修法=补 `slots=[_Slot()]` 对齐真实 LLMDispatcher 表面(单 key 下 gate 按设计 no-op,测试仍驱动真实重试路径,契约意图不变)。扫描同缝全部 `get_dispatcher` monkeypatch 族(test_dispatch_stream/test_async/test_429_saturation/test_gateway_outage/test_premature_close/test_vendor_transient/test_shared_contention/test_gateway_errors_accounting):其余均已带 `.slots` 或不触 gate,无第二处漂移。
- **同批事故复盘(兄弟 hand-off + 我核实):** 我的 `790a1183` 用显式 pathspec 提交 `lib/llm_dispatch/dispatcher.py` 时,把兄弟(msd387xj,gateway_errors epic)**未暂存**的一行 get_slots_info 投影(`'gateway_errors': s.gateway_errors,`)顺带入库——pathspec 只限定**文件**,git 提交的是该文件的整个工作树内容,兄弟的同文件 WIP 无处可躲。790a1183..8e81fced 之间 HEAD 处于「投影在、Slot 字段未在」半批态(那时重启服务器会 AttributeError);兄弟发现后正确地在自己的 commit 里排除了 dispatcher.py 并留言,两半现已闭环(冒烟:Slot.gateway_errors 存在、投影可读、兄弟套件 12/12)。**教训:** 共享树提交前对 pathspec 内的每个文件 `git diff <path>` 逐 hunk 复核——自己的批只该有自己的 hunks,发现外来 hunk 要用 `git add -p` 或先与作者协调,不能整文件吞。此教训与「先验后修」判例同族(工作树不属于自己的改动,提交前必须归因)。
- **边界澄清:** 兄弟把我的会话误认为工作树里 i18n.js/styles.css/test_turn_ctx_rail_geometry.py 等 WIP 的属主——那些是 rail-geometry 兄弟(msd3agg6fc7hsn)的批次,我未动、也不会动。

### 2026-08-03(站点知识层 P3 落地:攻略老化 autofix 闭环——漂移信号→医生重侦察→只钉验证过的选择器,引擎下轮搜索即吃新知识) — 脑派发 epic `pt_7cee7904ffc143ca` **DONE**;tofu-search `ed9f7c2`(0.7.1,5 文件 +402/−9)+ chatui 本批(8 文件);环 tofu-search **30/30 + 全量绿**、chatui **102/102**(新套件 16 + 桥邻接/底盘/installer 86)

- **闭环全链:** ①tofu-search `SiteKnowledgeProvider` 缝(逐站抽取知识=数据:wait_selector/extractor_js/scrolls,未钉则引擎内置常量保底——重钉选择器从此不发版)+ 漂移信号 `register_site_drift_listener`:浏览器路径抽取器包探针 `{items,probe}`,**页面渲染出笔记锚点(anchors>0)却抽到 0 张卡片=选择器漂移**,与真空(anchors==0)/风控墙(不渲染锚点)三分;监听器异常吞掉绝不炸搜索;漂移仍喂 1800s 退避(敲漂移的选择器也是敲账号)。②chatui `lib/site_knowledge.py`(覆盖式存储,json_store 原子写,version 单调,空 extractor 拒钉)+ `lib/site_doctor.py`:**骑 run_agent_loop 底盘**(charter 铁律,scene_author 形制),窄工具四件(inspect_search_page→try_extractor→pin_knowledge→give_up),**pin 闸:只接受与上一次 try_extractor 验证通过的逐字相同的参数对**;登录墙=give_up(重钉救不了账号);触发四闸(env 杀开关/未知站无 brief/3h 冷却/全舰队单飞);全程 audit_log,永不 raise。③`search_bridge` 接线(软 floor:旧库无缝不注册不炸),requirements floor `>=0.7.1`。
- **刻意偏离设计稿记档(P2 半):** 「XHS 知识迁出为首个数据条目」不做——内置选择器留在引擎代码当**版本基线保底**,store 只放医生验证过的覆盖条目;P2 的「引擎读 knowledge 数据」缝由本批提前交付,剩余=注册表泛化 UI(板票 `pt_689b73b305fe4810` 仍开)。**缓办:** 扩展网络拦截+toString 伪装——autofix 环不需要(composite 判例,memory `bridge-composite-over-new-command`),真有页内 hook 需求再立项。
- **事故自记(本批两起):** ①`_search_via_browser` 归一化把 scrape 的 `None`(路径不可用)归成 `([],None)`——None-vs-[] 契约反转,池兜底永不触发,reroute 套件既有针当场擒获(契约测试的价值实演);修=None 先透传再归一化,测试补钉。②insert_contents 误把新文本写进 `replace` 字段(content 留空)——插入变空行,读现场发现后 apply_diff 归位;**记档:insert_contents 只认 anchor+content,写前先核字段名。**
- **测试账:** tofu-search 新套件 9 针(知识覆盖/内置保底/非法知识回落/漂移发射一次/真空不发/legacy 列表无探针不发/监听器炸不误搜索/漂移喂退避/池路径保列表形);chatui 新套件 16 针(存储 5 + 触发四闸 + 医生环 7:happy path 钉库/未验证拒钉/篡改参数拒钉/give_up 零写/环异常不 raise/未知站短路/shape 契约)。failing-first=tofu-search 新套件首跑即红(facade 未导新符号——修 `__init__.py`),两起事故均为红→绿实证。
- **真机冒烟属人域:** 办公机 Chrome 登录态下,若 XHS 改 DOM 致搜索连续空→app.log 现 `SELECTOR DRIFT suspected`→医生自动重侦察钉新知识(或 give_up 记账)——全程无需人工;`TOFU_SITE_DOCTOR=0` 可整体关停。

### 2026-08-03(401/403 全池兜底:fallback 模型也死不打断回合——错误仅在全池 key 不可用时才上报) — owner 截图指令「为什么 401 直接打断而不是轮换到有权限的 key?只有所有 key 都不可用才该报错;能自动处理的就别增加用户认知负担」;epic `pt_f16423431ee54979` **DONE**;commit `790a1183`(6 文件 +835/−29);新套件×2 **16 检查**(HEAD worktree 负对照精确 12 红,4 张对照天然绿证无过杀);回归环 **62+41+67+153 绿**

- **事故链(task 7ea8c25f,日志+配置取证):** 主模型失败 → 回退到配置的 fallback 模型 kimi-k3 → kimi-k3 被 `key_access` 钉死在**单把 key**(key_0/key_2 的 kimi-k3 wire id 显式 disabled_ids——「轮换到其他 key」在配置层面物理不存在)→ 唯一的 key_1 对 moonshot vendor 持久 401「无效的AppId」(per-(key,vendor) 权限,key_1 对 yuju-claude/glm/LongCat 同刻 err=0 健康)→ dispatch_stream 内 pair 排除后 `has_capable_slots` 不看 strict_model,池里陌生健康 slot 答 True 吊着循环,每 60s `maybe_reset_exclusions` 复活死 pair 白烧一次硬尝试,第 3 次后 exhausted(~2min)→ `_llm_call_with_fallback` 两跳用完直接打断——**而池内 yuju-claude-opus-5/LongCat-2.0/glm-5.1 等 slot 全部 err=0**。
- **三修:** **A(直接答案)** `_attempt_pool_rescue`:两个放弃分支(无 fallback/已在 fallback、fallback 也失败)先做全池兜底——`stream_llm_response(pool_wide=True)` 非 strict 无 prefer_model dispatch,`exclude_models` 跳过本链已证死模型(防重新扎进死模型的 429 墙);仅当 `has_capable_slots` 判池中再无健康 slot 才走原信封;尊重 `disableModelFallback` 显式 opt-out(头less 调用方要硬错误);badge 沿用 `_fallback_from/_fallback_kind` 机制(救援模型上徽章,i18n 键复用原失败 kind);usage/api_rounds/audit_log 与既有 fallback 成功路径同账。**B(机制卫生)** `_StreamRetryState`/`dispatch_chat` 新增 durable 排除集合:permission(401/403) pair/key 与 quota key 在 60s 429-cycling 重置中**存活**——重置本就是给 502/timeout 瞬态的第二机会(注释原意),认证拒绝不可能在一次调用内自愈,复活只会白烧硬尝试;下次 dispatch 调用全新状态,修好的 AppId 下回合自愈。**C(判空诚实化)** `has_capable_slots(prefer_model=…)`:strict_model 下判空只看 pinned 模型 alias group——kimi-k3 唯一 pair 被排除后**第一次 401 立即上抛**(省 ~2min 空转),429 冷却期仍正确答 True 继续循环(cooldown 不计入)。
- **记档的边界:** ①本批没动「key 级死亡→整日禁用」方向——实证 key_1 并非账号级死亡(同刻其他模型成功),「无效AppId」在此网关是 per-vendor 语义,整 key 隔离会误杀健康模型;②quota key 也入 durable(同类:「disabling for today」不该被 60s 重置 undo);③floor-retry 在 pool_wide 下禁用(相同 body 重发对漫游模型无定义);④kimi-k3 的单 key 钉死是 owner 自己的 key_access 配置——代码无法替其开通 vendor 权限,修复后该模型失败会被池级兜底**透明掩盖**(badge 可见),但要真正救活 kimi-k3 仍需 owner 在 Settings 给它开第二把 key 或修 AppId。
- **测试账:** 新套件 `test_dispatch_durable_exclusions.py`(durable 存活/瞬态被清/整 key 升级/60s 重置端到端不复活死 pair/has_capable strict 判空×2/strict 分支传参×2)+ `test_pool_rescue_fallback.py`(救援成功全断言:pool_wide=True+exclude_models 精确+rescue 模型上账+badge+信封缺席;池空走原信封;opt-out 不救援;救援失败走原信封;gate 探测失败仍尝试;manager 缝 pool_wide 转发×2)。负对照:`git worktree add /tmp/x HEAD` 跑新套件精确 12 红(B×5/C×3/A×4),4 张未变行为对照天然绿。事故自记(insert_content 陷阱第 9 起):锚点含函数签名时**插入内容末尾不得重复该签名**——本次重复致 def 行×2 SyntaxError,py_compile 当场擒获修复。
- **预存红挂账:** `test_cache_prefix_byte_identity_r4r5r6::test_live_retry_preserves_task_id`(fake `_Dispatcher` 缺 `.slots` 属性,big-prefix gate 读取处 AttributeError,HEAD 同签名逐字节可复现)——板票 `pt_17117aca9b544702`。

### 2026-08-03(裁剪尾部 Continue 判定同源双修:按钮「只有重新生成」是标签谎言 + empty-guard 捷径会真摧毁 DB checkpoint) — owner 截图问「点加载工具活动前为何只有重新生成」;commit `47f04fe3`(4 文件 +552/−3);新套件 **4 检查**(failing-first 精确红→绿,A1/A2+b1 全家族);NEUTER×2 各咬各的;回归环 **157/157**(等价性+continue 全族+chat_render 邻接+窗口化 PUT 守卫)

- **根因(一类两址,全是「本地判据扫描被裁副本」):** 窗口化首开把 `toolRounds` 传输裁剪(`_trimmed` + 服务端烙印 `_trimmedToolRoundCount`),**址一(标签谎言):** `chat_render.js` 闸门的 `computeTurnSettlement` 扫本地空副本 → keptRounds=0、error 不在可续写集合 → fail-closed 成 `regenerate` → 动作栏只画「重新生成」;而两种按钮的 onclick 同是 `continueAssistant()`,服务端 `/api/chat/continue` 会权威重扫 DB——**标签朝反方向撒谎**(说重新生成,实则走 checkpoint 续接)。**址二(数据丢失,owner 点名的更重隐患,实证形态与猜想不同):** 非 truncate 的 PUT 早有重字段守卫(`routes/conversations.py:1671-1756` 按 `_msgId` 回填,`test_put_refills_trimmed_heavy_fields_from_stored_blob` 钉住)——continueAssistant 的预 POST 同步**不**覆写;真正的洞在 empty-guard 捷径:裁剪的「空内容」尾部被本地判定 `_hasRounds=false` → 本地 pop + **`allowTruncate:true` 同步(该旗标整体跳过守卫)** → DB 里 9 轮可恢复 checkpoint 永久摧毁、全量重跑,服务端从未被询问。
- **修法(owner 定案):** 址一收口在**闸门**——`show:regenerate` 且 `_trimmed&&count>0` 升级为 checkpoint 类「继续生成」;`computeTurnSettlement` 本体不动(等价性套件 `test_frontend_turn_settlement_equivalence.py` 与 Python 端口字节锁定);烙印计数是**服务端事实而非不确定**,不违 fail-closed 哲学;清洁终态(`show:false`)永不升级;hydrate 后 `_trimmed` 清除,verdict 自算真实 keptRounds。址二=`_hasRounds` 承认 `_trimmedToolRoundCount`,落回 POST 由 `scan_continue_checkpoint` 重扫权威裁决(真空仍回 `fallback:'regenerate'` 再 pop——重新生成决定**服务端持有**而非客户端删除)。
- **测试账:** 新套件 `test_frontend_trimmed_tail_continue.py`——A 部 jsdom 驱动**真实 renderMessage**(裁剪空内容错误尾→checkpoint 继续×2、清洁裁剪尾→无按钮、未裁剪错误尾→诚实 regenerate、烙印计数缺如→不升级,双类按钮同为 `.msg-continue-btn` 故以 title 判别);B 部驱动**真实 continueAssistant**(裁剪尾→不 pop/无 truncate 同步/POST 必达/任务绑定;服务端 fallback→彼时方 pop;未裁剪真空→本地捷径照旧,防过杀)。NEUTER-A 删闸门升级块→A1 红;NEUTER-B `_hasRounds` 恒false→b1 红。回归修一枚:`test_empty_guard_includes_toolrounds` 固定 600 字节窗被注释推出 `getToolRoundsFromMsg` 定义行——按该文件自身判例改锚 `_hasRounds` 定义行(同类锚点漂移第二起:窗口必须锚语义定义,不锚字节距离)。
- **边界记档:** PUT 守卫只在 `_src` 命中时弹 `_trimmed` 标记——无 `_msgId`/`_taskId` 的遗产消息若带烙印落库,标记可能残留(后续 hydrate 找不到轮次,按钮经服务端复核回退 regenerate,fail-safe);不扩面。

### 2026-08-03(崩溃边缘错误孪生根修:G1 采认尾部补 stamp _taskId——crash 路径同收敛「一任务一行」) — 脑派发接我自票 `pt_75889ea726b84929` **DONE**;commit `0ebb766e`(3 文件 +164/−1);新测试 **+3**(failing-first 实证两契约红);NEUTER×1 精确;环 **76+95 绿**

- **缺口(54aa57a5 的崩溃边缘):** settle 路径的 `fold_duplicate_task_twins` 只覆盖「走到终态结算」的失败轮;任务**永不 settle**(崩溃/强杀)时,startup recovery 的 G1 合并采认无 `_taskId` 的尾部(GATE 4 只拒外部 id,采认合法),写了 content/thinking/`finishReason='interrupted'`/interruptedReason/toolRounds/model 却独独不补 `_taskId`——此后前端重连占位(同 `_taskId`、typed 429 信封)推回,keeper 无 id → keeper 搜索为空 → fold 永不配对,双气泡**永久**固化。
- **修法(与 epic 方向一致):** G1 采认分支在写 finishReason 同位调新缝 `_stamp_merge_home`(fill-absent,GATE 4 已保证无外部 id)——采认即成家:①下次 sweep 的 `_merge_home_index` 按 id 找家(ms1auj3n 跨轮缝合闸从此覆盖崩溃采认尾部);②settle 路径 `_find_own_assistant_slot`/provenance 闸可归属;③GET/PUT 缝 fold 可配对收敛。配套实测确认前端在 SSE 绑定时就会给自己的流式目标 stamp `_taskId`(sse_pipeline.js:418),故推回 PUT 的配对在写缝即可 fold,服务端 stamp 覆盖的是缓存丢失/迟到 twin 的兜底。
- **顺手修昨日自酿:** `54aa57a5` CAS 循环内 fold 的 prefix 探测 `except Exception:` 裸赋值命中 code_quality 棘轮(assignment-silent-catch),补 debug 日志出列;其余 42 站点全是兄弟近日 epic(browser-preview/desktop-agent/egress 等)引入的预存红,按 owner 偏好留专项清扫不入本批。
- **测试账:** `test_recovery_merge_guards` +3——采认尾部必带 `_taskId`(契约)、恢复合并→模拟推回占位→GET 缝收敛为一行且信封 fold 上 keeper(端到端)、NEUTER 缝后配对永不收敛(承重证明);failing-first 用 `git show HEAD:` 换文件实证两契约精确红(NEUTER 因缝不存在同红,属预期);事故自记:insert_content 保留锚点行致 def 行重复×2(_recovery.py 与测试文件各一),插入内容**不得重复锚点文本**。
### 2026-08-03(站点知识层设计+P0 落地:浏览器优先固定管线——XHS 换路弃 cookie 回放 + fetch 双缝 BROWSER-FIRST + scrape 结构化原语) — 脑派发 epic `pt_650dbfe189a0435e`(owner 三指令:固定流程内化/安全门后置/批准 tofu-search 优化);tofu-search `ffa67e7`(0.7.0,7 文件 +361/−46)+ chatui 本批(7 文件);设计稿 `docs/SITE_KNOWLEDGE_LAYER_DESIGN.md`;环 tofu-search **80/80 + 全量绿**(2 枚 mcp 预存红经纯净 HEAD worktree 实证)、chatui **21/21 + installer/dist 43 绿**

- **owner 三问答落稿:** ①「遇拦截能否固定流程自愈?」→ 分层固定管线:**第一分流「你有这个站的账号吗?」**(有=登录墙站点永远 BROWSER-FIRST,登录一次 cookie 永久免配置——浏览器自己签名;无=公共站点重试×3);②「美团/小红书 toggle 以后还要吗?」→ 变成**「内化站点注册表」的显示项**,设置里看得见的一个 toggle=一条注册表行,内化新站=追加一行,知识自动吃——策略、选择器、坑、验证结果全在那;③安全门后置,功能先行。
- **P0 根修(小红书止血):** tofu-search `xhs.py` 弃「服务器无头池+导出 cookie 回放」(风控根因:headless 指纹+异地 IP 重放),改 `BrowserProvider.scrape()` 优先(后台 tab 真实 Chrome:原生登录态/页面自签名/同 IP 同指纹——owner 曾被官方警告的根治),池回放降为兜底;可用性=启用 AND (cookies OR 在线浏览器);`None=路径不可用→fallback`/`[]=真空→喂 1800s 冷却`语义全程分明。fetch 面同根同治:`fetch/core.py` 对 auth-source 域名**先浏览器后回放**,原「回放失败后再升级浏览器」被吞并(消灭双跳)。
- **chatui 侧两件:** `_ChatuiBrowserProvider.scrape` 落地=**组合 5 个既有桥命令**(create_tab 后台→wait_for_element→scroll_page×N→execute_js→close_tab 必发),零扩展变更——owner 已装的扩展 v4.5.1 即插即用,无需 reload;requirements floor `>=0.6.1→>=0.7.0`(软 floor)。附:上一批遗留的 `docs/FETCH_IDENTITY_PATHS_DESIGN.md` 一并入库。**事故自记(共享树第三起):** 我先写的「扩展第 28 命令 cmd_scrape」实现(fetch.py/background.js/双测试)未提交即被兄弟清树动作整体抹掉(stash 无记录),兄弟随后按设计稿原意落地了 composite 版——**签名与 tofu-search 0.7.0 协议逐字兼容**(timeout/scrolls 全对齐),复核后收编更优解:composite 不需用户更新扩展,我的版本反而给真机冒烟加了 reload 门槛。教训钉死既有纪律「验证即提交」。
- **CLAUDE.md 同步:** lib/browser 域描述纠正为「扩展桥,服务器无运行时浏览器」(27 命令不变,scrape=provider 侧组合);新增登录墙 BROWSER-FIRST 纪律段(回放=风控触发器,auth_sources 进化为逐站注册表,指设计稿)。
- **测试账:** tofu-search 新套件 9 针(browser-first 路径/None vs [] 语义/池兜底/可用性矩阵/共享护栏/抽取器保真/无浏览器回退);fetch 排序针按新契约重钉(browser-first+subsumed);登录墙契约测试同向进化(假浏览器场景下不再断言二次升级)。chatui 侧收编兄弟套件 `test_browser_scrape_provider.py` 8 针(命令序列+后台 tab 不抢前台/close_tab 必发防泄漏/异常与传输错归零/扩展离线零命令/二进制不开 tab/选择器未确认仍尝试抽取)。failing-first 由 owner 原痛点天然满足;NEUTER 由路径选择断言天然覆盖。
- **预存挂账:** tofu-search `test_mcp_tool_calls.py`(收集 ModuleNotFoundError)+ `test_mcp_server_smoke.py`(7 红)纯净 HEAD worktree 实证与本批无关(mcp 环境缺失族),未挂票。
- **遗留切片(板票):** 设置页「内化站点」列表 UI(P2,docs §4.4);攻略老化 autofix(P3)。**真机冒烟属人域**(owner 在办公机 Chrome 登录态下跑一次 XHS 搜索即验收)。

### 2026-08-03(预存红×18 根修:迁移期「用而不导」三枚真产品 bug——全局闸门 500→401 恢复 + save_conv 409 缝 api_payload 化 + AST 棘轮钉住全 bug 类) — 脑派发接我自票 `pt_551fc875f3034f38` **DONE**;commit `d9f931b6`(7 文件 +354/−17);环 **125/125 + 终批 150/150**

- **定案(表面测试漂移,实测三真 bug + 一漂移):** 17 张 requires_auth 红的根因=`routes/api_v1/auth.py` 批 18(`67826416`)加 3 处 `api_error` 调用却未加导入——全局闸门的坏 token 401/无凭据 401/限流 429 全 NameError 成 500;`test_unknown_token_rejected` 同根。第二枚=`routes/conversations.py` `_Defer(jsonify,…)` 的 jsonify 从未导入——**生产实证 13:05:01 PUT /api/v1/conversations 500**(rev 冲突 409 变 500,前端 rebase 契约断裂;当日日志审计时亲见此 500 未识别)。第三枚=`routes/common.py` `_db_safe` 503 `database_busy` 路径 `api_error` 未导入(同类)。漂移=folders 生命周期测试两处裸数组断言(批 19 信封)。
- **批 9 tripwire 的正当拦截(记档):** 初版修法给 conversations.py 补 jsonify 导入,`test_shipped_source_converted`(批 9 棘轮:禁 jsonify 名)立刻红——按 tripwire 意图改走正路:四个 409 站点收口到 `_json` 缝,缝体改走 `api_payload` 透传原语(payload 键逐字节顶层保留、4xx 附加 request_id,契约干净且前端 rebase 契约不变);`_Defer` 的 status kwarg 是记账位,api_payload 的 HTTP status 须走位置参(否则 _finish 嵌套元组)。
- **守卫三层:** ①新套件 `test_routes_envelope_imports.py`——AST 棘轮:routes/** 任何 api_* 家族/jsonify 裸名引用必须可解析(模块级/函数级懒导入/本地定义皆可),钉住整个「用而不导」bug 类;含合成自证(vacuity guard:缺导入必咬、懒导入与本地绑定不误伤)——磁盘棘轮不能用 sys.modules NEUTER,自证即其 NEUTER。②husk 套件 +3 行为守卫(stale baseRev/空覆写/回归 PUT 各得 409 非 500)+`_defer_status` 物化语义对齐 `_finish`(helper 必须可调用)+NC-2(断 `_json` 缝→三 409 全红,200 对照绿)。③`test_db_safe_dual_mode.py` +503 信封行为测试(该套件此前只钉双模包装、从不触 `_handle` 路径——漏检根因)。
- **事故自记(陷阱族第 7/8 起,memory `editing-traps-insert-anchor-and-neuter-restore` 已更新):** ①NEUTER 锚点 4 空格缩进作为 8 空格行子串被 splice 进 try 体 SyntaxError——锚点必须带完整缩进;②NC 两度不咬合:测试顶层 import 钉死原函数对象(须调用时新鲜导入)+ `_nc_harness` 预种原模块全局使「删 import」NEUTER 静默失效(须改赋值 `name=None`)+harness 不物化 helper 结构性漏检(须断言 callable)——三坑同批记档。
- **预存挂账:** `test_code_quality` 4 红(silent-catch/raw-getLogger 族)经纯净 HEAD worktree 实证与本批无关,未挂票(长尾清扫面,待专项)。

### 2026-08-03(同任务错误孪生 fold:429 失败轮「双气泡」根修——一任务一 assistant 行,后端三件套) — owner 截图两问(conv `mscns5i0fcofgl`);commit `54aa57a5`(4 文件 +703/−32);新套件 **12 检查**(failing-first 精确红→绿,DB 驱动 settle 端到端);NEUTER×2 各咬各的;邻接环 **30 套件 206/206**

- **病灶(全链日志+DB 取证):** 一轮流式部分内容(35字+8642 thinking+12 工具轮)后死于 429 saturation 的回合,在 DB 落成两个气泡——①后端自有槽位(msg1):部分内容+`finishReason='error'` 但**无错误信封**(content-guard 分支只写 finishReason/usage/_taskId,从不写 error);②前端重连占位(msg2):11:36:13 刷新铸的 tmp 占位,SSE LATE done 盖上 365-cycle 信封,11:38:30 `loadConvMsgs KEEPING local` 全量 PUT 推回入库。既有 `is_duplicate_task_twin` 因 twin 携带 keeper 缺失的终态事实而刻意保留这对(防藏终局),两个 half-truth 行固化。
- **第二问同根:** Continue 判定(`_tsScanKeptRounds`)需要工具轮与错误尾在**同一行**上——错误气泡本地无轮次 → keptRounds=0 → 只给「重新生成」不给「继续生成」;node 实测五形态钉死(错误行带轮 → checkpoint/继续;无轮 → regenerate)。收敛为一行(内容+轮+信封)后判定即「继续」(checkpoint 12)。
- **根修(全部后端,零前端去重):** ①`_sync.py` content-guard 分支补写终态 `error`(set/pop 与正常路径镜像)——guard 保护更全的正文,从不保护终态判决;②`reconcile.py` 新增 `fold_duplicate_task_twins`:payload 字节包容且终态分歧的 twin → 终态字段 fill-absent 折到 keeper(**error 绝不折到清洁终态行**——瞬态矛盾即噪音,与正常路径 pop stale error 同判决)再丢弃;pass 0c 换用并前移到 fragment mark 前(fold 后 keeper 的 finishReason 不被误戳 'aborted');GET/PUT 双缝同愈;③`_sync.py` 终态 CAS 循环 settle 写里调用同一 fold(pre-loop 一次供 settings 尾事实,循环内 per-attempt 幂等覆盖 graft),fold 替换 keeper 后重解析 `last_msg` 引用——settle 时 DB 原子收敛。
- **账:** 契约和解一例——`test_twin_carrying_a_terminal_fact_the_keeper_lacks_is_never_collapsed` 重钉为新契约(意图不稀释:终态事实绝不丢,改折到幸存行;清洁终态行不吸收瞬态错误);keeper 搜索 earliest-walk 初版误把 user 界碑当否定答案(已找到的 keeper 被错杀),改 break 返 found;`_stallNudges` 注册进 `_TERMINAL_OWNED_FIELDS`(同族预存红,漂移闸 test_rev_cas_migration 顺手修);预存红外两枚非我域(api_integration×3=兄弟信封迁移,本批复核时兄弟已 `d77d69da` 闭环、db_guard 4 个兄弟新文件未挂 guard)。
- **纪律自记:** 本 turn 三度把 apply_diff 的 search/replace 写成相同文本(no-op edit),教训同前——replace 必须逐字节对照 search 再发;共享树又遇兄弟未决合并(UU _gateway.py)致 stash 不可用,预存红验证改走 HEAD 文件 grep 比对,不动兄弟状态分毫。
### 2026-08-03(预存红×3 闭环:test_api_integration 断言对齐 api_ok 信封——方向对齐而非代码迁就,api-contract 批 9/11 测试侧滞后清零) — 脑派发接我自票 `pt_4de2a0a67d694736` **DONE**;commit `d77d69da`(2 文件 +41/−24);全套件 **28/28 绿**(原 3 红);回归环 **19+34 绿**(契约 drift/parity/写缝)

- **定案:** 三处断言(`list_conversations_empty`/`save_and_load_conversation`/`chat_active_tasks`)仍钉裸数组形状,而 `GET /api/v1/conversations` 与 `GET /api/v1/chat/active` 已在 api-contract 批 9/11 协调迁移中把数组负载移入 `api_ok({'items': …})` 信封(charter #0 既定方向);同端点其余消费者(test_autopilot_summary_no_phantom、Api.chat.active 等)均已按信封读取,唯此三处滞后。
- **修法(先例=identity_gate_parity「别搞开关,直接改测试」):** 断言对齐信封(`ok is True` + `isinstance(items, list)`),顺手把 list_convs 的 api_meta 200 文档从「A JSON array」更正为 `{ok, items}` 信封——行为文档一致化。failing-first=三张原红天然满足。
- **预存挂账:** `test_api_v1_integration` 18 红(auth 域 requires_auth/unknown_token 族)经纯净 HEAD worktree 实证与本批无关,板票 `pt_551fc875f3034f38`。
- **方法复用:** 预存性判定全程 `git worktree add /tmp/x HEAD`(上批事故后固化的memory `dirty-shared-tree-never-stash-pop-use-worktree`),共享树零触碰。

### 2026-08-03(同角色相邻消息治理:合并日志分级——设计内缝静默/意外告警带定位 + 写缝一次性持久化去重;owner「源头不生成,生成后一次性绝对正确修复」) — epic `pt_99eeedbd40424fe6`;commit `be8c9b8a`(5 文件 +749/−4);新套件×2 **12 检查**(failing-first 精确 8 红,2 对照天然绿);NEUTER×3 各咬各的;环 **188+153+13 绿**

- **定案(实测驱动,非bug):** app.log 每日 ~2000 行 `[build_body] Merged 1 consecutive same-role`(当日起 1,825 行,1,988/1,990 恰为 1 对),离线重建 wire(conv mscog7yj 实证)+分布统计钉死 **99.9% 来自设计内合成上下文缝**——CLAUDE.md `_isMeta` 载体(index 1,A/B 实测省 18% 成本/+49% 缓存命中,`build_user_context_reminder`)/偏好档/附件提醒(`inject_attachments`)/swarm 收件箱(`drain_and_inject_inbox`)——这些缝**刻意**把 user 消息排在另一 user 旁,`_merge_consecutive_same_role` 的合并就是指定的最终装配步骤,模型所见 wire 逐字节即设计意图;INFO 行纯属噪音且形似「bug 被反复打补丁」(owner 正是据此观感发问)。剩余 0.1%(当日仅 2 行 Merged 2)=真生产者(发送竞态重复 user 行/错误幽灵邻接),此前混在噪音里不可见。
- **切片 A(lib/llm_sanitize/_messages.py):** `_is_synthetic_context_msg` 内容分类(头部 `<system-reminder>`/`<swarm-update>` 前缀或四枚已知标记);设计内对 → DEBUG 静默,意外对 → WARNING 带 `#idx/role`+60 字符预览(对齐 `_strip_empty_text_blocks` 定位先例);**累加器防洗白**——设计内对融合后即视为真实内容,后续真重复照报警(测试钉住)。合并结果字节不变=纯观察性改动,缓存零成本。
- **切片 B(写缝一次性持久化去重,owner 第二句的直译):** 发送竞态(乐观副本+server 副本同 `timestamp`)种下的重复 user 行,此前靠重建侧 `_dedup_duplicate_user_messages` 每次重建内存里重复愈合(同一 anti-pattern)。两条写缝接同一纯判定(`persist_conv_messages` + `_save_conv_blocking` PUT——后者正是可重种该对的逃逸缝,沿用 ghost-husk sweep 先例),同 timestamp 连续 user 行保 LAST(server 副本),**DB 里一次性愈合,重建侧从此零触发**;WARNING 带 conv + `audit_log('conv_user_dup_healed')`。不同 timestamp/非连续同行=出契约,不动(对照测试钉防过杀)。
- **未动载体(记档的取舍):** owner 第一句「源头不生成」若直译为「载体融合进首条 user 消息」,爆炸半径=注入幂等/ctx_freeze/MsgStore/autopilot 目标提取/token 分类/~10 测试文件,全部压在缓存敏感机上;实测该缝非缺陷(设计+A/B 证据),故不做。载体对从此 DEBUG 静默;WARNING 再响=真生产者,一条 grep 到位。
- **测试账:** failing-first 精确 8 红(6 分类+2 愈合;distinct-timestamp/非连续两对照天然绿证无过杀);NEUTER×3 精确——分类器恒 False→设计缝翻 WARNING 恰咬 3 静默针、PUT 缝短路→dup 落库恰咬其针、persist 缝短路同理;恢复 cmp 一致。**事故自记(第七/八次同类变体):** ①NEUTER 锚点 4 空格缩进作为 8 空格行的子串被 splice 进 try 体造成 SyntaxError——锚点必须带完整缩进;②NEUTER 不生效:测试顶层 `from x import f` 钉死原函数对象,须改调用时新鲜导入(harness 文档早写明,重犯)。
- **预存红挂账:** `test_api_integration`×3(断言裸数组 vs api_ok 信封,api-contract 批 9 测试侧滞后),纯净 HEAD worktree 实证与本批无关,板票 `pt_4de2a0a67d694736`。
- **共享树事故(自酿,已营救):** 验证预存性时 `git stash push` 因 untracked pathspec 报错短路,随后的 `git stash pop` 误弹兄弟代保管 stash `pt_871a26c7`(_gateway.py 冲突)——stash 条目保留=内容零丢失,`checkout --ours`+`reset` 复原。**教训入库(memory `dirty-shared-tree-never-stash-pop-use-worktree`):脏共享树预存红验证只用 `git worktree add /tmp/x HEAD`,永不 stash pop。**

### 2026-08-03(生命周期加固落地:stop.sh↔guard 互斥 + serve-mode 重放 + 心跳 wedge 双分支裁决 + [5/5](b) 探针修流;顺手擒 stop.sh 僵尸误报) — epic `pt_6f066c2ae2d64066` **DONE**(代码面);owner 按稿批准 A+B+C+D′+E+探针;commit 见下(5 文件 +335/−17 + 新套件 452 行);环 **16/16 新套件 + 22/22 邻接**(restart_lock_race+loop_stall_watchdog);NEUTER×4 全精确;部署=纯人门(重启审批)

- **A(stop.sh):** 杀前 `touch data/.tofu_guard_disabled` + 响亮提示 `--start` 恢复——堵「SIGKILL 后 9s 被看门狗抢跑占锁」;**顺手擒获顺带 bug**:harness 里 dummy 死后成僵尸,`kill -0` 永报活→「Failed to kill even with SIGKILL」假警——`_pid_gone()` 僵尸=已死(与 restart [2b/5] 同教训),生产真实可触发(父进程不回收时 stop.sh 必误报)。
- **B/C(guard):** `_serve_mode` 读 `data/.last_serve_mode`(server.py 在 TLS 决策**后**原子落盘 http/https,证书生成失败正确记 http);`relaunch` 据记录补 `TOFU_TLS=0/1`(无记录=空=不干预 auto-detect,绝不发明决策);`healthy()` 按记录协议先探再互补——cron 环境拉起 TLS 实例被 http-only 探针误报「60s 不答」与代理 plain-HTTP 撞 TLS 的「socket hang up」同根同治。
- **D′(双分支心跳裁决):** guard (c) 监听在但 HTTP 死、(b2) 无监听但活锁持有,两分支统一 `_wedge_proof_age`(心跳 pid 匹配+age>180s)裁决;首见只记档(`data/.tofu_guard_wedge`),连续 120s 才 SIGTERM→10s→SIGKILL→relaunch——**6.5h 冻结在此机制下 3 分半内自愈**;fresh 心跳=忙不杀(boot 无心跳不匹配=yield 如旧,两态各有 e2e 钉住)。server.py 第二层:心跳任务内 `_listener_death_decide`(纯函数矩阵钉住:未绑定期不起 watch、恢复清零、K=5 连续丢失→critical+audit+`os._exit(1)`)把「serve 死/循环活」变成 guard 能处理的干净死亡。
- **E+探针:** restart 日志 `>`→`>>`+每世横幅;连带效应用 LOG_MARK(行偏移)+LAUNCH_STAMP(时间戳串比较,字典序=时序)做启动域隔离——append 后前世的「instance lock held」「Loop blocking-guard」行永不误计;(b) 探针改查 app.log(INFO 级证据行实测 0 条进 stdout/990 条 WARNING+,旧探针必然假 FATAL——owner 实证的那次「FATAL」实为误报,242280 的修复确实载入)。
- **测试账:** failing-first 15 红起步(含 source 缝不存在时误 `--ensure` 拉起 guard——实测 flock 防线有效,无幽灵循环);行为 e2e=tmp 目录全真 harness(flock 持有者 `exec -a 'python server.py'`、探针命令缝 TOFU_GUARD_SS/CURL、心跳 TOFU_HEARTBEAT_DIR、/bin/true 快死 relaunch)。**事故自记(新类):ss stub 照抄教程加了 `tcp ` Netid 前缀,本机 ss 无 Netid 列($4=Local),stub 偏移一列悄悄滑进 (b2) 分支,NEUTER 2 假通过被当场擒获**——stub 必须对齐被测机器的真实输出格式,教训:探针类 stub 先 `ss|head` 实证列布局。NEUTER×4:N1 去 flag→e2e 红;N2a/N2b 分别短路 (c)/(b2) 的 `_wage`→各自 e2e 红;N3 append 还原 truncate→红;恢复 cmp 全一致(cp 备份法,不用 git checkout)。
- **真实冒烟:** healthy() 对线上 242280=OK;(b) 探针逻辑 STAMP=11:16:30 命中 242280 的证据行、STAMP=now 正确不命中;bash -n 三脚本。**部署账:** server.py 的 serve-mode 落盘与 listener 自裁要重启后才武装(人门审批);三脚本与 guard 是盘上新版本即刻生效(guard 循环下次 `--ensure` 换血即拾取,或等 cron)。
- **共享树事故营救(同日第二起同类):** 提交后复跑时全仓 `import server` 被 `lib/llm_sanitize/_gateway.py` 的 stash-pop 冲突标记炸断(13:42 某兄弟 pop 了代保管 stash `pt_871a26c7` 后留下未解冲突;`Updated upstream`=现行 owner 批准版,`Stashed changes`=被取代的旧草稿)。按同日先例处置:stash 条目仍在清单=内容零丢失,`git show HEAD:file > file` 还原 + `git reset HEAD -- <path>` 清索引 UU 态,38/38 复跑全绿,stash 分毫未动。谁 pop 的请自觉在干净窗口重 pop 并手工并两段 hunks。
- **看门狗换血(owner 补令,已闭环):** 旧循环 2007747 杀后,flock 被其继承 fd 9 的 `sleep` 子进程拖住(正是 server.py 注释里「孤儿子进程持 fd」的活样本,FUSE 上更显),首个手动 `--ensure` 静默退出;~45s 后 sleep 退出、cron `--ensure` 于 14:01:01 拉起新循环 pid 522185。验收:--status=running+listening、启动后 3 个轮次零日志(健康服务器=静默,无误杀)、无 wedge 残留、心跳 0.6s 新鲜。**D′ 心跳 wedge 裁决自此武装上线。** bash 在跑循环不拾磁盘新码的教训记档:生命周期脚本交付后必须确认「在跑进程」而非仅「盘上文件」。

### 2026-08-03(浏览器桥 CDP 化收编落地:owner 亲验全绿后令落地——可信输入 + browser_preview_page 双交付) — epic `pt_388be9f265fa44aa` **DONE**;原作者=会话 mscnzimu 后续工作(未提交弃置),本批=我逐 hunk 复核 + 补 requirements floor + 提交;tofu-search `d2ee3d9`(0.6.1,3 文件)+97;chatui `69de1a5a`(19 文件,+1304/−16);环 **109/109**(trusted_input+page_preview+parity+tooling_fixes+queue_ttl+async_poll+user_scope 七套件)+ tofu-search 池 **10/10**(owner 亲跑复核)

- **收编账(先验后修判例执行):** 逐 hunk 复核全部 361+97 行——扩展可信输入节(CDP-first + 合成兜底 + `trusted:false/fallbackReason` 注解,旧扩展无注解时 `_trusted_suffix` 静默不画蛇添足)、`browser_preview_page`(虚拟源 `tofu-preview.invalid` route-fulfill:相对资产/ES module 可跑、外网 abort 计数防 SSRF、`_safe_path`+realpath 双闸防逃逸、`__screenshot__`+`_text_fallback` 载 console/pageerror/missing/blocked 报告)、池注册表(内建 kind 保护 + 同 handler 冪等/异 handler 拒注册)、接线七件(dispatch/display/facade/registry 末位插 spec 保缓存前缀/tool_set 注入 _projectPath/图标/徽章)。
- **我补的唯一洞:** 兄弟升了 tofu-search 0.6.1 却没提 chatui floor——`requirements.txt` `>=0.6.0`→`>=0.6.1`(SOFT floor 注释:`_register_once` 对旧库降级为模型可见错误串,不炸启动)。
- **gate 定案(兄弟选择,复核认可):** 预览 spec 门=`project_ready` 而非 browser toggle——主路径是渲染项目文件,且不随扩展连接状态隐现;池不可用由 handler 在调用期报错串,schema 构建期绝不拉起 Chromium。

### 2026-08-03(浏览器桥 CDP 化 epic 挂起:共享树惊现同题完整 WIP——认领后先验树再动工的判例第三起) — owner 指令「可以用 CDP 优化 + agent 看自己写的页面 + 可信输入也优化」;epic `pt_388be9f265fa44aa` 已挂 [sibling];**零产品代码**

- **事件线:** owner 批了 CDP 优化两件(扩展内可信输入 + 服务端页面预览工具),我探查完代码、挂板认领、设计定稿——动工前最后一道 `git status` 发现工作树里已躺着**同题全覆盖的未提交实现**:background.js +224 行可信输入节(`_cdpRun/_cdpClick/_cdpHover/_cdpKeyDescriptor/_cdpKeyboard`,Ctrl+S 不产 text 的微妙处都处理了)、manifest 双清单 4.6.0、`lib/browser/preview.py` + `handlers/_preview.py`、双测试套件、tofu-search 池注册表三件套——且作者会话**此刻正在跑验证**(我的 run_command 串线收到其 tofu-search pytest 10 绿输出)。
- **处置(按断连环票判例):** 一行产品代码不写,原地让位。epic 挂 [sibling] + 全路径清单,附收编指南:若作者收官后树仍脏,按「先验后修」逐 hunk 复核(设计已核对一致:CDP-first+合成兜底+trusted 注解;预览走虚拟源 route-fulfill 而非 file://,与我定稿同构)→ 跑四个套件 → 显式 pathspec 提交关票。
- **设计沉淀(供收编者核对):** ①可信输入=chrome.debugger `Input.dispatch*`(isTrusted=true、真 CSS :hover),失败落回合成事件并注 `trusted:false+fallbackReason`;②预览工具 `browser_preview_page` 骑 tofu-search Playwright 池,池加 `register_task_kind` 注册表(比硬编码第 4 个分支更具扩展性);③文件模式**不用 file://**(ES module/相对 fetch 被 CORS 掐死),用 `http://tofu-preview.invalid/` 伪源 + route 从磁盘 fulfill,零网络出口防 SSRF,外网默认 abort;④结果走 `__screenshot__` 协议 + `_text_fallback` 载 console/pageerror 报告(文本模型降级仍有错误摘要)。
- **环境教训(已存记忆):** 共享 shell 通道会串线兄弟会话的命令输出——**每条 run_command 结果先核对 `$` 行命令回显再采信**;本次靠哨兵串 + md5sum + mtime 三角定位才识穿「文件在三个时间点的三个版本间跳变」不是 FUSE 缓存而是活作者在写。动工前最后一道 git status 写集检查,在脑派发时代是硬纪律。

### 2026-08-03(429 家族退避退役:项目级争用立即重试 + 日志节流——与无限重试默认同日闭环) — owner 指令「项目级 key 争用,跳过指数退避立即重试,防日志膨胀」;commit `92a2dbb1`(5 文件);环 **31/31 + 邻接 dispatch 72/72**

- **退役对象(唯一真·指数退避):** `note_shared_contention`(pt_1a72b708098d446f,2026-07-28)对项目级争用 429 把整个 (provider, model) 家族停进 2s→翻倍→60s 抖动窗口——strict_model 钉池 + 无限重试时代,这就是「请求每分钟干等、窗口还翻倍」的痛点。常规 429 本来就是 0.3s 快轮询+0.5s 槽位引导冷却,无指数成分。
- **修法:** 该钩子改**纯遥测**——streak 计数(30s 静默重置)+ 节流日志(strike 1-3 + 每 100 条 INFO,余 DEBUG),**永不触碰任何槽位冷却**,返回 0.0;`Slot.record_error` 的 0.5s 'rate_limit' 引导不动(那是立即重试本身的节奏;字面零延迟=热自旋打自家网关,不可取)。metrics 侧(分类器/contention_errors 记账/免 rpm 衰减/key_stats 双不喂)**全部保留**。
- **日志膨胀闸(点名的另一半):** dispatch_chat/dispatch_stream 的逐周期 429 INFO(旧「降噪」只摘 body 没摘行,~3 行/s、一任务一小时 ~1.1 万行)改前 3 周期+每 100 周期 INFO、其余 DEBUG;async 循环本就无逐周期日志。cooldown-wait 分支本就每 20 周期才一行,不动。
- **测试账:** backoff 套件整册重写为新契约——不停车 NEUTER 针(25 strike 永不改冷却/恒返回 0)、streak 静默重置、日志节流(100 strike 恰 4 INFO+96 DEBUG)、端到端立即重试、逐周期日志节流(6 周期恰 3 INFO+3 DEBUG,复用饱和套件 fakes);label/rpm 隔离针原样保留。`test_picker_not_steered_away` 防旧套件 flaky 坑(双槽等分抖动 ±5% 会抛硬币,给 other 垫 latency_ema=99999 钉死确定性)。
- **边界记档:** `retry_i18n` 的 'contention' label 分支与 i18n 键保留(函数契约+翻译键完整),只是 `cooling_cause_summary` 从此不再产出它——HUD 在争用墙期间显示「rate-limited」而非「shared project limit」,诚实(我们确实没在等停车窗口)。

### 2026-08-03(桌面图标「白圈」根修:ico/icns 改走抠底+裁剪画布——wizard/DMG 早已抠白底,app 图标漏走同一条路) — owner 截图指令「白圈太难看」;commit `d62ba6a4`(2 文件 +135/−2);守卫 3 枚 failing-first 实证 2 红;环 **76+13+40 绿**(build_workflow+installer_parity+desktop_agent)

- **定案(生成器漏缝,非素材问题):** 实测盘上 tofu.ico 256px 帧 **0% 透明**、四角 (254,255,255,255)——白画布 100% 不透明烘进每个帧,Windows 桌面把白板整块渲染出来。根因=`scripts/gen_desktop_icons.py` main() 把原始 logo.png(1024²、RGB 全不透明)直接喂 gen_ico/gen_icns;而同文件里 wizard-large.bmp 与 DMG 底图早已走 `_cut_out_background`(四角 flood-fill 抠外部白底)——app 图标是唯一漏走抠底路的产物,故无守卫(测试只钉了 wizard 抠底)。
- **修法(_icon_canvas 三件套):** ①复用 `_cut_out_background` 抠外部白底;②**透明像素 RGB 消品红**——Pillow 缩放 RGBA 不预乘 alpha,抠底后残留的 flood-fill sentinel 品红会沿 LANCZOS 核染进豆腐深色描边(实测 48px 帧验证 0 品红像素);③按 alpha bbox 裁剪+6% 边距取方——源画布 ~30% 白边,不裁则 16px 托盘帧豆腐过小。wizard/DMG 路径零改动(自带底色,继续全画布抠底)。
- **守卫(failing-first 2 红→3 绿):** .ico 最大帧四角全透明+中心不透明(NEUTER 靶=回退 raw src);立方体占帧 >45%(钉住裁剪,防「抠底不裁」回缩——裸抠底仅 ~34%);.icns 四角同钉(双平台同源)。`getdata()` 在该 Pillow 版本已弃用,测试改用 histogram。
- **上线路径:** tofu.ico/icns 是 **gitignore 构建产物**(.gitignore:252-253),真源=脚本+logo.png,CI 图标步骤(build-desktop.yml `gen_desktop_icons.py`)下次构建自动生效;owner 当前装机里的白板图标随下个安装包更新,Windows 图标缓存顽固时 `ie4uinit -show` 刷新。桌面蓝底/16px 托盘模拟图复核:立方体悬浮无白板,占比与可辨识性双升。
- **事故自记(第五次同类):** insert_content 又把锚 def 行写进 content 造成 `gen_wizard_images` 重复 def+悬空片段,按例当场修复——锚文本永不出现在 content 里。兄弟会话目击我 WIP 期共享树红×2,按「不夹修不动」正确处理,本 commit 即其消解。
### 2026-08-03(本机控制面板两连修:轮询签名闸根治「展开几秒自动收起」+ local_source 双角色可见矩阵——owner 截图两指令) — epic `pt_59b62951aad2463e`;commit `dc19252e`(6 文件);环 **48+95 绿**(merge 45+agent_download 3 / bundle-manifest+i18n 覆盖+devices+dist);NEUTER×2 各咬各的;bundle-d9da6a64 / i18n-zh-90501953 重建入包实测

- **病灶①(自动收起=轮询把整块 DOM 重写):** `_lcRefresh` 每 3s 跑 `_lcRenderDesktop` → 无条件 `setup.innerHTML = …` → 用户展开的 <details> 收起、刚生成的连接行消失。修法=**签名闸**(根因非补丁):渲染输入(setup_state/connected/server_url/双下载指纹/语言)未变则跳过重写,状态点/文字每拍照常更新;模态打开时重置签名。merge 套件 splice 表补 `_lcDesktopSignature`(否则 ReferenceError 全红——splice 按符号表抽函数,新模块级函数必须入册,教训记档)。
- **病灶②(折叠逃生口被无视+文字墙):** local_source 改**双角色可见矩阵**——受控端块在前(lc-role-primary 高亮:角色名+一句注解+下载+生成连接行),完整桌面版块在后,零折叠;文字从 5 段压到 1 标题+2 注解。stale-while-build:无 agent 资产时铸行钮挂在完整版块上(完整版自带 agent,托盘可 Connect to remote),隧道用户永不落空。
- **契约和解:** merge 套件 token 例外从「details 内」演进为「agent 角色块内」(owner 裁定覆盖 8-02 的折叠形态,测试明文记档);「每状态一个 lc-step」保持(role 头用 lc-role-head 非 lc-step);孤儿键 local.desktopSource 删除(tunnelToggle/tunnelHint 同批退役为 roleChoose/agentRole*/fullRole*);REQUIRED 派生钉补 AGENT 表(A2b 合流的 test_devices 滞后断言)。
- **兄弟现场避让:** 图标角红×2=test_desktop_build_workflow.py 兄弟未提交 WIP(gen_desktop_icons.py 同 M,HEAD worktree 实证其测试 HEAD 不存在)——不夹修不动;启动角色 UX epic(兄弟 f16fdbb1)动过 tofu-agent.spec/烟雾闸,与我的 web 面板侧零冲突。

### 2026-08-03(启动角色 UX S2-S4 全量落地:角色窗双 App 上线 + 本机控制出托盘 + 规格/烟雾/文档收口——epic 关票) — 脑派发回执 owner 答「按稿全量实施 S1-S4」;epic `pt_6956ccfb605e497b` **DONE**;commit `f16fdbb1`(11 文件 +747/−7);环 **12 桌面套件 251 绿** + agent 源码烟雾 `TOFU_AGENT_SMOKE_OK` exit 0

- **S2+S3 形态:** 新模块 `desktop/role_window.py`(connect_ui 单 authoring 模式)——纯构建器 `role_state_full`/`role_state_agent` 承载窗口每个事实(无头可测),门控 `should_show_at_startup` 读 config `show_role_window`(**缺席键=显示**:新装与「窗口前版本升级」长一个样,这两群人恰是窗口的受众;稿上「首启必弹」规则被此语义吸收,设计稿已注记),tk 渲染器懒导入+主题化+双语+单例重入(托盘「控制面板…」靠重入 lift)。**窗口不持状态**:每次 refresh 重拉 state_fn,每次变更委托给托盘同一批 handler(`_cc_state`/perms 活字典/autostart 缝)——是第二视图不是第二状态路,两面永不可能各说各话。完整版宣告「服务器」身份 + **dual_role 行**(同时受控于远程时明说——隧道事故盲区);受控端宣告「受控端」+所连服务器。两 launcher:icon.run() 前门控弹窗 + 托盘「控制面板…」回入口(agent 设为 default 项——它没有 web UI,面板即主面)。
- **规格/烟雾:** tofu-agent.spec 桌面缝行 +tofu.spec 显式登记 `desktop.role_window`(原先靠静态分析,保险登记);agent 烟雾闸新增「控制面板必须随包」断言,源码实测 `TOFU_AGENT_SMOKE_OK version=0.16.0 commands=19`。
- **S4 文档:** 双 README 组件表「无界面:托盘改连接」过时描述更正 + 角色窗说明段;desktop/README.md 首启体验章节;设计稿状态收尾。
- **测试账:** 新套件 16 检查(角色句双语/dual_role/门控默认·持久·不压他键/接线棘轮三张);failing-first 16 红;NEUTER×3 精确(dual_role→恰好咬 dual 针、门控翻默认→恰好咬 absent 针、launcher 摘回入口→恰好咬 reentry 针)。**批跑 2 红(test_app_icon_*_transparent_corners)实证为兄弟 logo-redesign 并发生成图标的批跑互染**——套件独立 76/76,图标文件 HEAD 零改动,与本批无关,同类已有「预存批跑污染」板票族。
- **事故自记(第六次同类):** insert_content 锚文本(quit 条目+闭合 })又写进 content → STRINGS 提前闭合 IndentationError,主题套件 28 红当场切除修复。**该陷阱已六次,记忆 `editing-traps-insert-anchor-and-neuter-restore` 在库仍犯——下次插入类编辑前先默念:content 里永远没有锚文本。**
- **边界:** 与兄弟 epic pt_59b62951aad2463e(web 本机控制面板/安装包分发)写集零交;板上其余悬案(PG 播种、agent 真机验收)仍纯人门,与本 epic 无关。

### 2026-08-03(调试面板「混入陌生工具」定案:面板无罪,卡片是 autopilot VU 的——_riTaskIdForRound 数值兜底把 VU 轮错配到 worker 任务) — owner 截图报 mscnr9uw32k9ak 调试面板与工具卡不符;commit `d19c0a19`(2 文件 +98/−8);failing-first 精确 2 红;NEUTER×2 各咬各的;环 **37/37**(P6 4 + inspector 前后端 33)

- **定案(三方各自无罪,配对错了):** 会话里有两个任务——worker `2f9e52ac`(11:17-11:21,5 轮 11 工具,已落库)与 **autopilot VU 后续任务** `8d1dddee`(11:21:57-11:36:19,「模拟用户/owner 用工具独立复核 assistant 主张」,8 工具含 web_search,后撞 kimi-k3 429 以 error 收场,气泡按 vu_cancel 设计无痕抹除)。工具卡=VU 的在飞轮(渲染正确);结果状态面板=worker 的真实镜像(数据正确);**错在锚点把 VU 的轮解析到了 worker 的 taskId**——逐字段核对(工具名/id 序号 0-8/输出字节数)实证面板内容与 worker 任务 wire 完全一致。
- **机制链:** ①VU 气泡 `role:'user'`+`_isVirtualUser`,其轮无 `_taskId`;②锚点 `_renderDebugEntry` 退到 `_riTaskIdForRound`——旧码先按 `role==='assistant'` 跳过 VU 气泡再做身份匹配,必然落空;③数值兜底 `(roundNum,llmRound)` 与 worker 同号轮**孪生碰撞**(双方都从 1/0 起编)→ 返回 worker taskId;④面板 faithfully 渲染 worker 镜像,看上去像「数据串了」。设计契约本是「不可解析→不渲染锚点」,数值兜底把天然不可解析的 VU 轮变成了「自信地错」。
- **修法(还原契约):** `_riTaskIdForRound` 身份匹配扩到**全角色**——命中即判属主:assistant → 其 `_taskId`;非 assistant(VU)→ `''`(owned-but-not-inspectable,VU 子任务本就不落 snapshot,无可检视);身份全落空才走数值兜底(仅 assistant)。单循环 tail-up,旧 tie-break 语义零漂移。
- **测试账:** P6 harness 造「尾部 VU 气泡+数值孪生轮」场景,+3 检查(VU 不渲染锚点/解析为空/worker 孪生轮不误伤);failing-first 精确 2 红(其余 15 检查全绿证无过杀);新 NEUTER(还原先跳过非 assistant 的旧行为→VU 检查必翻紅)+ 旧 NEUTER(roundNum 映射)各咬各的;邻接六套件 33/33。
- **上线账:** bundle **未重建**——兄弟在 tool_rounds.js/styles.css/i18n.js 的未提交改动会被一并打入,待其落地后由下次重建自然收编;修复只在调试面,线上影响=VU 轮暂时仍渲染锚点。**事故自记(第六次同类):** insert_content 又把锚 def 行写进 content 造成重复 def,按例当场切除——插入类编辑锚文本永不出现在 content 里。

### 2026-08-03(「stop 后为何起不来 + socket hang up」全链定案:stop.sh 与看门狗无互斥 + guard 环境缺代理标记拉起 TLS 实例;第一因=旧实例事件循环已冻结 6.5h) — owner 三连问调查;纯审计批,零产品代码;证据=logs/watchdog.log + server_15000.log 双横幅 + app.log 静默带 + pid_max 实测

- **时间线(全部有日志实证):** ①04:40:53 旧实例 2351494(8-01 09:08 由 guard 拉起)在 Range 下载 AssertionError(`a76340a4` 已修的那个 quart/werkzeug off-by-one)后**全进程静默 6.5 小时**(scheduler/DB-reaper/MCP-keepalive 全部消失,下次启动还 prune 到 1 份 faulthandler dump)——事件循环冻结,服务实际已死,这是用户来重启的第一因;②~11:14:0x FUSE 回神后 listener 消失(「Shutting down gracefully」行在 11:15:19 证据块里,归属被 restart 脚本 `>` 截断覆盖不可考);③11:14:41 用户首次 `python server.py` 撞活锁按设计拒绝;④11:14:58 stop.sh SIGTERM→12s→SIGKILL;⑤**11:15:19 `deploy/tofu_guard.sh` 看门狗(cron 保活,15s 间隔)按职责拉起新实例 240685 并占住单实例锁**——用户 11:15:22 第二次启动再撞锁(「LIVE local server pid=240685」),PID 数字反而变小是 pid_max=4194304 回绕的错觉;⑥**240685 启动横幅=HTTPS+自签证书**(guard 环境无 VSCODE_PROXY_URI/CODELAB 等标记,`_detect_reverse_proxy` 失判→auto-TLS),用户经 code-server 代理/plain-HTTP 访问命中 `server.py:2432` 注释逐字描述的坑:「proxy's plain-HTTP request hits our TLS listener and the connection is reset — socket hang up」;guard 自己的 `healthy()` 也讲 http,于是误报「60s 未应答」(实例 11:15:33 已 Ready);⑦11:16:31 `restart_15000.sh` 持 `.restart.lock`(watchdog.log 两次「standing down」实证互斥生效)→ 按 ss 实际监听者杀 240685 → 从用户终端(有 VSCODE_PROXY_URI)拉起 242280=**HTTP 横幅**,14s 健康。
- **三个「为什么」的答案:** stop 后起不来=**stop.sh 只杀锁文件 PID 且不碰看门狗**,guard 9 秒内抢跑占锁;hang up=**guard 拉起的实例是 TLS**,代理只会讲 plain HTTP;restart 行=它是唯一与 guard 有互斥协议(`.restart.lock`)且按端口实杀、从带代理标记的终端启动的入口。
- **环境漂移旁证:** 2351494(8-01 guard 拉起)当时是 HTTP(04:40 代理路径正常服务),240685(8-03 guard 拉起)变 TLS——guard 循环多次周转(日志里 3 次 loop started,本调查期间 398315→2007747 又换了一轮),某次从**无标记环境**(cron/旧终端)重启后,其 env 被每个 relaunch 继承。
- **修复建议(待 owner 拍板,生命周期面按惯例人门):** A. stop.sh 杀前 `touch data/.tofu_guard_disabled` 并响亮提示(或全程持 .restart.lock)——堵「杀完被抢跑」;B. guard relaunch 重放上次启动的协议决策(服务器 boot 时写 data/.last_serve_mode,guard 据此补 TOFU_TLS=0/1)——堵「环境失判→TLS」;C. guard healthy() 按记录协议 curl(-k https)——堵「TLS 实例被误报不答」;D. **serve-task 死亡即进程自杀**(LoopWatch 加 listener 活性检查,无监听且活着→CRITICAL+非零退出)——把「活但不服务」态变成 guard 能处理的干净死亡,6.5h 冻结变 15s 切换;E. restart 脚本 server_15000.log 改 `>>` 保留前世日志;F. guard (b2)「活锁持有者无监听=启动中」无限让路需 TTL/心跳裁决。

### 2026-08-03(启动角色 UX epic 立案 + S1 托盘 i18n 落地:托盘摘「最后英语面」帽子,AST 棘轮双向钉住) — owner 指令「桌面 App 启动即明身份(服务器端/受控端),客户端功能别全塞托盘,托盘连 i18n 都没有」;epic `pt_6956ccfb605e497b`(已认领);commit `dee57f38`(5 文件 +445/−21);环 **87/87**(tray_i18n+tk_theme+cc_persistence+smoke_gate+cmdtype_parity+install_paths+bundle_extension+agent_cli)

- **格局:** 先稿后码——设计稿 `docs/DESKTOP_STARTUP_ROLE_UX_DESIGN.md` v1 落盘(四切片:S1 托盘 i18n→S2 角色窗+受控端→S3 完整版角色窗+本机控制入窗→S4 文档),已挂问题卡待 owner 三选一(全量/只做 S1+S2/否原生窗)。**S1 不依赖答案**(owner 原话点名「tray doesn't even have i18n support」),先行落地。
- **S1 账(英文下行为零变化):** `_tk_theme.STRINGS` +16 个 `desktop.tray.*` 键(zh+en,占位符 {tag}/{url}/{port} 双语同构);`launcher.py`/`agent_launcher.py` 全部 pystray MenuItem 字面量经 `_tt()` 走 `t()`(动态项含 Server 标签/更新项一并);新套件 `test_desktop_tray_i18n.py` 三闸:AST 棘轮拒任何 MenuItem 字符串字面量(两 launcher 逐张钉)+ 引用键必存在双语 + zh≠en 防假 i18n + 占位符双语对齐。failing-first 精确 3 红(2 AST+1 引用计数);NEUTER×2 各咬各的(一侧还原文本→恰好该侧 AST 针红)。
- **事故自记(第五次同类):** insert_content 锚文本含了「chromium.desc 条目+闭合 }」并原样写进 content → STRINGS 提前闭合+条目重复,IndentationError 炸出主题套件 28 红,当场按重复块切除修复。**再记:插入类编辑锚文本永不出现在 content 里。**
- **事故自记(新类):** NEUTER 后用 `git checkout -- desktop/launcher.py` 恢复——在脏工作区上这是**整文件回滚到 HEAD**,把我未提交的 4 处接线一并抹掉;靠 apply_diffs 重放恢复。**记档:脏共享树上 NEUTER 恢复只用 cp 备份还原,永不 git checkout 单文件。**
- **剩余切片(待人答):** S2 角色窗 `desktop/role_window.py`(纯构建器+tk 渲染分离,connect_ui 单 authoring 模式)+ 受控端接线;S3 完整版窗口+本机控制面板入窗(复用 _cc_state 同缝,托盘留镜像+「控制面板…」回入口);S4 文档。与兄弟 epic pt_59b62951aad2463e(web 本机控制面板)写集不相交:那边是 local-control.js/api_v1/desktop.py,这边是 desktop/ 原生面。

### 2026-08-03(429 有界升级默认关停:无限重试成为默认行为——「429 永不打断对话,重试零成本」) — owner 截图报 kimi-k3 上「429 saturation (budget 120s, 364 cycles)」硬错误信封打断会话并下指令;commit `80431312`(3 文件);环 **7/7 + 邻接 dispatch 58/58**

- **定案:** pt_a21cd6eb(2026-08-01 事故)交付的「全 slot 连续 429 超 120s → RateLimitError(is_saturation=True) → llm_fallback 换模型」在**回退模型也饱和**时把硬错误信封(`⚠️ API 请求已达限频（429）`,context=on-fallback-model)拍给用户——owner 判这是「自己打断自己」,指令:429 面墙只许持续重试,永不打断。
- **修法(默认值翻转,机制留档):** `TOFU_429_SATURATION_SECS` 默认 `120`→`0`(=关停=逐字节回到升级机制引入前的无限轮转);设正数预算即恢复有界升级。升级机制本身、env 旋钮、gateway 5xx 独立 120s cap(`TOFU_GATEWAY_OUTAGE_BUDGET_S`,与 429 无关)**一律未动**;`is_saturation` 错误通道与 llm_fallback 换模型链路保留,只是默认永不再触发。
- **代价记档(知情的取舍):** 默认关停即重新接受 2026-08-01 事故形态——strict_model 钉死池在持久饱和墙上会无限循环(429 不计 hard_attempts,reaper 按设计不杀 429 循环),llm_fallback 永不收到饱和信号、不自动换模型。owner 明示「重试零成本」接受此形态;前端重试相位(「限流重试中 · 第 N 次」)持续可见,用户可随时手动停。若日后想要回升级:`TOFU_429_SATURATION_SECS=120`。
- **测试账:** 套件全部显式 pin env 故零红;新针 `test_default_budget_disables_escalation` 钉住「无 env → 预算 0」;docs/429_SATURATION_CONTROL_PLANE_DESIGN.md §1 默认值描述同步。

### 2026-08-03(「tofu 如何操作任意网站」设计讨论:三层解剖 + 家底盘点——运输/原语层已有,知识层是唯一缺口;XHS 风控根因实锤) — owner 发起设计讨论;三路 swarm 盘点(桥/服务端/tofu-search)完成;记忆 `tofu-browser-bridge-inventory` 入库;未写产品代码,待 owner 拍板

- **盘点惊喜(纠正 CLAUDE.md 过时描述):** `lib/browser/` 不是 playwright pool,是通往用户 Chrome 扩展的桥——扩展 27 命令(navigate/execute_js/click/type/scroll/wait_for_element…)+ `/api/browser/poll` 长轮询 + bridge token 鉴权全部生产在跑;16 个 browser_* agent 工具已接进 agent loop。**OpenCLI 三层模型对照:运输层✅、原语层✅(缺网络拦截)、知识层❌(站点攻略/策略分类/创作工作流——唯一绿地)。**
- **XHS 风控根因实锤:** tofu-search `xhs.py:263` 走服务器无头 Playwright 池+回放导出 cookie(服务器 IP 与 cookie 签发环境不一致=风控经典信号),且只有搜索卡片路径、无详情页路径(「抓不到正文」的架构原因)。接缝已存在:`search_bridge.py::_ChatuiBrowserProvider` 可路由到用户真实 Chrome——修法是换路不是新建。
- **三路线呈 owner:** A(桌面 agent 跑 opencli CLI,零建设但不沉淀)/B(现有桥上自建知识层,推荐)/C(服务端池,与 XHS 教训冲突,降级)。建议 B 为主 A 为探针;分期 P0(XHS 换路+详情抓取)→P1(扩展补网络拦截/toString 伪装/后台窗口租约)→P2(.tofu/sites/ 知识层复用 memory 预取+策略六分类)→P3(攻略创作 skill 骑 run_agent_loop)。安全:写操作过 approval.py,全程 audit_log。
- **待 owner 三答:** ①路线确认 ②P0 可否直接在 tofu-search 开工 ③P1 动 browser_extension 需与桌面域两兄弟 epic 划界。

### 2026-08-03(OpenCLI 仓库克隆与机制学习:小红书「免 token 免签名」方案定案——让浏览器自己签名,只读渲染结果) — owner 指令拷贝 jackwener/opencli 并学习;仓库落 `INS/ruanjunhao04/opencli`(chatui 同级);三路 swarm 侦察(架构/小红书适配器/adapter  authoring)完成;记忆 `opencli-study-xhs-browser-bridge` 入库

- **痛点对照:** 此前 tofu 做小红书=手动抠 cookie 直调 API,需自算 x-s/x-t 签名→触发风控被官方警告+笔记正文抓不到。OpenCLI 的答案:**永不自己签名**——CLI→daemon(localhost:19825)→WebSocket→MV3 扩展→chrome.debugger CDP→真实登录的 Chrome,页面自己的 JS 完成签名,适配器只读渲染后的 DOM/Pinia/`__INITIAL_STATE__`/拦截响应。
- **小红书三件套:** search=纯 DOM 抓 `section.note-item`+动态滚动凑 limit(note ID 是 ObjectID,前 8 hex 反推发布日期);note=必须带 xsec_token 的完整 URL(从 search/feed 链接透传,裸 ID 报错),goto 后随机等 2-5s 再抽 `#detail-title/#detail-desc`,识别 300017/300031 安全页抛 SECURITY_BLOCK;auth=只查浏览器 `web_session` cookie,页内 fetch credentials:'include' 自动带签名,全程零 cookie 导出。
- **可借鉴(tofu 已有自建 browser bridge):** 拦截器 `Function.prototype.toString` 伪装 `[native code]`(src/interceptor.ts)、双层租约仲裁+409 session_busy、WS 心跳保活 MV3 service worker、站点记忆缓存「没记忆→用 skill→产生记忆→下次 5 分钟」(~/.opencli/sites/)、adapter DSL 策略六选一按契约稳定性选(实测 PAGE_FETCH/INTERCEPT 修复频率是 PUBLIC_API 的 7-8 倍)。

### 2026-08-03(预存红×2 闭环:字体幽灵家族检查 fail-open 根修——2026-07-29 正则修复的引入性回归) — 脑派发接我自票 `pt_275e143a92374dec` **DONE**;commit `58e96506`(2 文件);NEUTER 精确(回滚正则→精确 3 红,还原 27/27);邻接环 **81/81**

- **定案(真 bug,非测试漂移):** `undeclared_font_families` 的 use-site 正则 `_USE_FAMILY_RE` 在 2026-07-29 修「内联属性闭合引号逃逸吞下文档尾」时把引号整类排除——**以引号开头的值(`font-family:'PingFang SC'`,CSS 最常见写法)从此一个字也捕不到**,幽灵家族检查对加引号的写法静默放行(fail-open)。两个红(ghost 报告缺失 + NEUTER 闸因找不到 finding 而先倒)同根。
- **修法(原子正则,双陷阱各钉一头):** 值=逗号连接的原子表,原子=成对引号段或非分隔符串——未成对引号永不可能吞尾(7-29 陷阱不复燃),引号首值正常可读(8-02 陷阱根修)。五案例预验证(ghost 引号/裸安全/属性逃逸/已声明 @font-face/多引号表)全对后才落笔。
- **账:** 回归针 `test_quoted_families_are_seen_and_attributes_still_terminate` 双向同钉;failing-first=两张原红本身就是(预存红天然满足);NEUTER 回滚到旧正则→精确 3 红(2 原+1 新针),还原 27/27;cjk_typography/author_resilience/asset_brief 邻接 81/81。事故自记(第四次同类):insert_content 又把锚 def 行写进 content 造成重复 def——本 epic 已四次踩同一陷阱,按例当场修复,教训再记:插入类编辑锚文本永不出现在 content 里。

### 2026-08-03(本机控制定案·补记:逃生口实测已在线上 + poll 悬案销案 + 我两连针选错自纠) — 兄弟 msb6ohqifdz7yj 四件闭环回执;零产品代码

- **逃生口线上实证(我先判「不可见」,兄弟纠正,复核成立):** 我连犯两针错——①拿中文串 grep bundle,但 i18n 键按设计抽进 pack 不入 bundle;②凭印象拼 `/static/dist/` 前缀,真实前缀是 `/static/js/`(index 里写得明白),两轮 grep 的全是 404 body,假阴性。正确复核:线上 `i18n-zh-325c95d7.js`(228KB)含「从另一台电脑访问本服务器」+ `local.tunnelToggle/tunnelHint`,`bundle-2fcd7c3c.js`(1MB)含 `lcMintBtnSrc`——**owner 硬刷面板即见逃生口,无需重启**。**教训:探测线上静态资源,先从 index 抠真实 URL 再 grep;404 body 会把一切针变成假阴性。**
- **22:30/22:37 poll 悬案销案:** 既非我的 GET(401 在 record_poll 前)、也非浏览器桥——是兄弟自己的桥 auth 探针(egress key 实测 200)。无第三方,我此前「更像 browser-bridge-popup token」的猜测作废。
- **Range 500:** 兄弟已根修(见上条 `a76340a4` 详录),线上吃到修复需与 A1-A3 同批重启(纯人门,lifecycle 闸)。
- **stash 对账:** 兄弟一次 stash pop 误撞 `_gateway.py` 冲突,已还原 HEAD;`stash@{0}`(pt_871a26c7 代保管,单文件 19 行)分毫未动,继续停泊,本会话无 pop 需求。

### 2026-08-03(单字节 Range 探测 500 根修:file_serving 缝统一五路由 + 兄弟四件实测移交全部闭环) — 兄弟 msbwca9y 实测移交;commit `a76340a4`(6 文件);环 **44+54 绿**,NEUTER 实证(裸 quart 路径 bytes=0-0 仍 500);预存红×2 另案 `pt_275e143a92374dec`

- **病灶(库对连环 off-by-one):** quart `_process_range_request` 把 `end - 1` 当 inclusive stop 传给 werkzeug `ContentRange.set`,后者 `is_byte_range_valid` 对 `start >= stop` 一律拒——单字节 Range(bytes=0-0/5-5,**每个下载器的 resume 探测**)触发 `AssertionError: Bad range provided` 逃逸成 500;多字节与 416 与裸 GET 全好。兄弟在线上连测两次实证(200 vs 500)。
- **修法(一条缝,不五份 try/except):** `lib/file_serving.send_file_conditional`——同步形(server.py 的 _sync_safe shim 语境,async 包装会返回协程对象当响应)、**call-time import**(shim 在 app 构建期换装 quart.send_file,模块级 import 会拿到 pre-shim 快照)、catch 窄化到 `'Bad range provided'`(其它断言绝不吞),命中即落 full-body 200(规范合法:服务器恒可忽略 Range)+ warning 取证。五调用点(desktop 下载/paper image/pdf/motion file/scene file)全换缝。
- **测试账:** 新缝套件 5 检查(裸 GET/probe 不 500/206 不降级/416 不动/NEUTER 裸路径仍 500);paper_pdf_range 的 NC 重锚(rp.send_file → fs.send_file_conditional,路由已不走旧名);206 的 Content-Range stop 是库自有的又一格 off-by-one(报 0-98 发 100 字节)——钉住缝所拥有的契约(status+body+总长),库 stop 数字不钉,上游修复日自翻。
- **兄弟四件其余三件:** ①「线上 bundle 无逃生口」=**针选错**——i18n 键按设计抽进 pack 不入 bundle,实测 bundle-2fcd7c3c 含 lcMintBtnSrc、i18n-zh-325c95d7 含 tunnelToggle;②直链 53MB PE 可下(其实证);④22:30 的 authed POST = **我自己的**桥 auth 探针(egress key 实测 200),无第三方。
- **纪律:** 共享树又现兄弟 stash(pt_871a26c7 代为保管),我的 stash 实验误撞——未动其 stash 分毫,其父提交干净复验预存红后原样还原;motion 资产字体族 2 红证为父提交同红(预存,另案挂板)。

### 2026-08-02(隧道误判根修落地:local_source 折叠逃生口 + 合并套件明文记例外——兄弟交接收编闭环) — epic `pt_59b62951aad2463e`;commit `c4130943`(5 文件);环 **47/47**(merge 45+agent_download 2);bundle-2fcd7c3c / i18n-zh-325c95d7 重建

- **收编定案(兄弟 msbwca9y 现场定案移交):** owner 经 ssh -L 访问→peer=loopback→`_setup_state` 误 local_source→面板只教「装完整桌面版」→办公机装出第二份 Tofu,bundled server 抢 14963(15000 被隧道占)其 agent 自轮询——服务器侧 agents 恒空的真因。**修法选面不选判:** 隧道对服务端结构性不可见(任何重分类都是猜测,`_setup_state` docstring 记档「此处无物可测」),local_source 加 `<details>` 折叠逃生口「从另一台电脑访问本服务器?(如 ssh 端口转发)」——内嵌受控端下载+铸连接行(id 区分 lcMintBtnSrc),真本地用户零打扰(折叠),隧道用户得生路。
- **契约和解(merge 套件两针红→绿,意图不稀释):** ①每状态恰好一个 lc-step——逃生口提示**降 lc-substep**(折叠 <details> 内内容按定义次级,lc-step 计数保持 1);②「仅 remote 提供 token」——harness 拆报 mintInsideDetails,token 测试**明文写入隧道例外**(local_source 允许一枚 mint,但只许在折叠舱内,绝不上主位),不是借 id 差异绕绿。
- **配套:** `_lcMintToken` 参数化元素 id(默认值不动,remote 调用点零变化);i18n 两键 zh/en;agent_download harness 的 local_source 期望同步演进(折叠舱内 agent 链+铸钮+无编号流)。
- **验证:** failing-first(逃生口前 local_source steps=2 红/merge token 红)→ 修复 → 47/47;NEUTER 语义:mintInsideDetails 断言为 True 型,舱被删即红。
- **owner 侧创意记档(其 `1a2cca6b`):** 远程分支编号三步流(①装②铸③粘)+ 完整版 <details> 折叠 + zero-touch 双步变体(preseed 可用且开放桥时),并擒获我 A2 wrap 的 loopback preseed 陷阱(办公机无隧道时自指虚空且抑制连接框)——winbuilder 现丢弃 loopback/未指定 preseed(响亮 log)。

### 2026-08-02(本机控制「装好桌面版仍显示未运行」定案:组件错位 + ssh 隧道误判 local_source;手动铸连接行解封 owner) — owner 截图两连问;零产品代码;token `k_d8d6c743` 已交付;面板缺陷移交兄弟 epic `pt_59b62951aad2463e`(折叠逃生口方案被收编);记忆 `local-control-tunnel-misclassification` 入库

- **全链定案(三层叠加):** ①完整桌面版=独立第二份 Tofu,自带 bundled server(launcher `_spawn_server`),无 remote attachment 时 agent 只轮询自己的 bundled 端口——owner 图 2 的空白客户端(127.0.0.1:14963)不是坏了,是它的全新数据目录;14963 本身即旁证:`_find_free_port` 发现 15000 被办公机上的 ssh 隧道占着。②owner 经 ssh -L 访问 → peer=loopback → `_setup_state` 误判 `local_source` → 面板只渲染「装完整桌面版+托盘 Enable Computer Control」,受控端下载+「生成连接行」永不可见——指令前提「人与服务器同机」对隧道用户结构性不成立。③owner 装的是 mirrored(release)包,无 preseed,首启不弹连接框,agent 永远到不了服务器。服务器侧证据:`/api/v1/desktop/status` agents 恒空、`bridge_token_required=false`(开放桥)、mint 路由在线(POST token → 201)。
- **解封(零重装):** 托盘右键 → Connect to remote Tofu… → 粘贴 `http://127.0.0.1:15000 <agents:bridge token>` → 再勾 Enable Computer Control;已铸 `k_d8d6c743`(office-windows-bridge)交付 owner,探测时误铸的 `k_04aa1a7c` 已自行 revoke 对账清零。连接行格式唯一 owner=`lib/desktop_agent/config.py::parse_connect_line`(url+token 两半必填);开放桥上任意 secret 过 `_resolve_bridge_caller`。
- **移交与边界:** 兄弟 `msb6ohqifdz7yj` 收编折叠方案(local_source 分支补「从另一台电脑访问本服务器?」`<details>` 复用 remote 流程),声明 local-control.js / api_v1/desktop.py / _lcMintToken 归他,我不碰;隧道不可探测不重分类(尝试不可能=撒谎),docstring 记档。兄弟指认 22:30/22:37 的 last_poll 更新是我的探针——订正:我全是 GET,POST-only 的 poll 路由 GET 进不了 handler,来源更像 22:39 新铸的 browser-bridge-popup token(`k_b8c7747c`)那条浏览器桥线;「办公机 agent 零到达」结论不变。
- **待办(纯人门):** owner 在办公机执行两个动作(勾 Enable Computer Control、保隧道 15000 在通);注册表 watcher/验收链归兄弟,本会话不重复挂。
- **教训记档:** ①探测 mint 类路由会真产生副作用——`-o /dev/null` 丢 body 照样入库,探完必须 list 对账 + 清理脚印;②「本机控制不工作」三问定位法:装的哪个组件、setup_state 是什么、agents 注册表空不空——分别对应组件错位/分支误判/未 attach。

### 2026-08-02(并行调用卡两行图标列对齐 + 展开小三角移除:截图像素级测量定案,cmd 块嵌套时代残留 margin 根修) — owner 截图两指令;commit `14354d05`(4 文件 +26/−26);环 **26/26**(cmd-collapse+wire-parity+tool_rounds render/rich×2+cmd_interrupt+paper_tool_rounds+streaming_ui+dangling);bundle-3de54321 重建实测 0 chev 引用

- **病灶定位(像素考古):** 运行中 run_command 行的红绿灯点比上方 grep 行图标右偏。对截图程序化测量(橄榄点 #CCA858 质心 x=121.9,放大镜 #C4956A 质心 x=97.1,差 **24.8 device px**)——按 DPR=2 折算 =12.4 CSS px,与 CSS 账本精确吻合:`.ptool-cmd-block` 残留嵌套卡片时代的 `margin:6px 8px 6px 12px`(两侧 inset),而现行设计是「.ptool-turn 卡内的扁平内容」(14818 注释自述),margin 把整个 header 推右。非 tofu 主题还有第二半:基础 `.ptool-cmd-header` 左 padding=0(`.ptool-line` 是 16px)。**教训:质心测量 + 服务器 CSS curl 比对(cmp 证实盘上=线上)比目测裁切靠谱——目测估出 21~33px 三个互相矛盾的数,质心法一次定案且反推出 DPR=2。**
- **修法(两主题同根):** `.ptool-cmd-block` margin→0、`.ptool-cmd-header` padding→`2px 12px 4px 16px`(与 `.ptool-line` 同 inset);`.code-exec-block/.code-exec-header` 同灶同药。tofu header 本就 16px 左,零惊扰。
- **小三角:** `_cmdDescInline` 的 `<span class="ptool-cmd-chev">▸</span>` 移除(描述本身仍是点击开关,功能不损);CSS 两条 chev 规则(含 cmd-open 旋转)同删;cmd-collapse 套件翻转为断言**不存在**+CSS 闸断言规则已删;wire-parity baseline 重生成——41 轮电池仅两个 collapsible-cmd 轮变化且 diff 只有 chevron 删除,快照审阅如常。
- **纪律:** 共享树 freshness 闸拦了一次 styles.css 写入(兄弟动过),复读确认 hunk 全属自己才提交;`git diff` 复核 styles.css 零外来 hunk;显式 pathspec 提交。

### 2026-08-02(浏览器桥「没更新?」双症定案:版本徽标是硬编码孪生 + 401 是全局闸门在执法;侧进程铸 key 被服务器覆写实测) — owner 截图问扩展页 4.5 弹窗 v4.3 且 401;commits 见下(6 文件);parity+backoff **32/32**;新守卫 1 枚

- **版本对不上(纯显示漂移,非未更新):** manifest.json 已 4.5.0(chrome://extensions 如实显示),弹窗徽标却是 popup.html 里**硬编码的 `v4.3`**——4.4/4.5 两次升版没人记得同步它。根修:徽标改 `id="versionBadge"` 由 popup.js 启动时 `chrome.runtime.getManifest().version` 填充(派生而非记忆,从此不可能漂);manifest/store 双清单升 4.5.1 让用户能亲眼验证修复落地;parity 套件新增 `test_the_popup_version_badge_is_derived_not_remembered`(硬编码徽标正则+锚点+getManifest 填充三钉,与既有「单一版本事实」族同训)。
- **401(真执法,非扩展旧):** loopback curl 实测无密钥=401,响应带 `request_id` 且 hint 措辞与 `_bridge_caller.bridge_unauthorized` 不同——锁定 401 来自 **routes/api_v1/auth.py 全局 before_request 闸**(B0 收敛后桥端点 `open_when_unset=False`:未配 TOFU_BRIDGE_SECRET 时唯一凭证=agents:bridge 作用域 API key)。进程环境实测服务器未设 TOFU_BRIDGE_SECRET,扩展 Bridge Secret 为空 → 每个轮询 401。8/1 的 401 backoff 提交正是为此(3104 条鉴权 warning)。**修法一句话:弹窗 Bridge Secret 粘贴一把 agents:bridge key**——已代铸 `k_b8c7747c`(经 POST /api/v1/desktop/token,走服务器进程缓存),curl 实测 200 `{"commands":[]}`。弹窗文案同步改「required」(「optional/leave blank」描述的是旧开放世界,现状桥端点一律要凭证)。
- **实测陷阱(已存记忆):** 我先在**侧进程** `create_key` 铸了一把——落盘后被运行中服务器的下一次 _persist 用它的进程内缓存整卷覆写,行消失(key 存在但永不命中)。教训:**铸 key 只能走服务器 API**(进程内缓存+落盘同生),侧进程写 api_keys.json 是裂脑。另发现生产 api_keys.json 里混着一批 7/27 测试夹具行(u-alice/devices-test,含两把 agents:bridge),已报 owner 待裁量,未擅删。

### 2026-08-02(本地控制面板认知减负三连:编号三步流 + 折叠完整版 + 零接触变体;loopback preseed 陷阱实测根修,agent 包重建) — owner 指令「试 Codex 桌面控制后,检查本地面板还能怎么优化,让用户一看就懂该下什么,加速本地控制上线」;commit `1a2cca6b`(10 文件 +356/−37)+ docs 批;环 **197/197**(agent_download 22 检查+merge 45+floor 15+dist 41+agent_winbuilder 27+winbuilder 20+parity 13 等 7 套件);预存红×1 顺手修(同族文件)

- **实测擒获的验收死局(本次最大单):** 店里首个 agent 包的 preseed 竟是 `http://127.0.0.1:15000`(A2 从服务器本地请求构建,preseed=构建请求上下文 host)——办公机装完会 attach 到自己的 loopback,永远轮不到服务器,**且连接对话框因有 attachment 而永不弹出**(import_preseed→save_remote_server(url,'')→main() act2 `if not url` 跳过),用户卡死在「托盘显示 Server:127.0.0.1 但永不变绿」。板上挂的「装包+粘贴连接行」流程根本走不到粘贴那步。根修=winbuilder `_agent_safe_preseed_url`:agent 目标丢弃 loopback/未指定 preseed(响亮 warning;完整版逐字节不动——local_source 同机场景 loopback 恰是正解);变量名保持 `server_url` 使旧 NEUTER 钉(`_write_preseed(payload_dir, server_url)`)零惊扰继续负重。agent 包已重建:**53,190,388 B,git_sha=1a2cca6b 与本批代码同 sha(重启后漂移检测不误报),preseed 无→首启必弹连接框**;真实构建日志实锤新闸开火。
- **面板重构(remote 分支):** ①下载受控端(按钮式直链+服务器直连 chip+体积)→②「生成连接行」(mint 成功**自动复制**+toast,点击手势内允许剪贴板;失败静默回退点框复制)→③粘贴到首启连接框;完整桌面版收进零 JS `<details>` 一行次级。修掉的旧疾:步骤无编号、文案先引用「下面这行」后给按钮、完整版占 3 个视觉块与主推抢注意。
- **零接触 2-step 变体(后端判据,前端只读):** status 载荷新增 `bridge_token_required`(getenv_compat 读 TOFU_BRIDGE_SECRET——本服务器实测未设=开放桥)+ 每条目 `preseed_url`(**后端先过滤 loopback,前端永不看到假可用**);两事实俱备才渲染「装完自动连上,变绿即完成」并藏 mint 钮。**字段缺失按「需要 token」处理**——3-step 在开放桥上同样走得通,这是 fail-safe 方向;2-step 只在后端明示时现身。
- **测试账:** harness 重钉 22 检查(编号步骤/折叠无 open/mint 文案/1b 零接触双变体);dist 套件 +3(is_loopback_url 矩阵/preseed_url 双请求投影(防行遮蔽:loopback 旧版先查再录新版)/bridge_token_required 两态);agent_winbuilder +3(agent 丢 loopback/留可达/完整版留 loopback);**预存红 test_NEUTER_stripping_the_download_link_is_caught**(A3 改签名后针串漂移,stash 实证 HEAD 同红)按同族文件顺手修原则重钉到 `(d, kind, suppressPage)`+函数体首行。
- **上线账(仅剩两件纯人门):** ①重启服务器(A1-A3+本批全在盘上未在跑——lifecycle 闸by design 只能人在 UI 批);②办公机装 TofuAgent 粘贴连接行,然后顺手验 §10 真机 OAuth 全链(egress epic 移交项)。bundle-a420f081 / i18n-zh-154b7919 / i18n-en-220190a5 已重建,键实测入包。

### 2026-08-02(A2b+A4 落地:CI 三平台 agent 腿 + REQUIRED 双组件合流(原子)+ 文档面收官——epic 代码面全齐,仅剩人门验收) — epic `pt_59b62951aad2463e`;commits `894ef397`(A2b)+ A4 docs 批;环 **138/138**;NEUTER×2 精确(Run 值名改名→双套件各 1 红,还原复绿)

- **A2b 形态(切片账的「同 PR 原子」执行):** agent 步骤骑**现有三平台 job**(复用 checkout/venv/icons,agent 唯一新增依赖=curl_cffi)——win:pyinstaller tofu-agent.spec+TOFU_AGENT_SMOKE 闸+Inno 作者权(AppName=Tofu Agent/[Tasks] autostart 默认勾/[Registry] HKCU Run uninsdeletevalue/OutputBaseFilename=TofuAgent-Setup);mac:双 arch TofuAgent DMG(**macOS agent 唯一来源**);linux:TofuAgent tar.gz(.deb 仍完整版独占)。agent 构建失败即整腿失败——「全平台或响亮失败,绝不静默半套」的发布哲学自然延伸到组件维度。REQUIRED_PLATFORM_ASSETS 同 commit 合流双表(缺一即 INCOMPLETE,version 闸下次运行自愈补齐)。**不放行 globs 内联工作流**旧闸零惊扰。
- **A4 文档面:** 双 README 下载区加双组件表(角色/体积/内容);egress 稿 §11.1 runbook 步骤②「拷仓库+pip install」临时路径正式退役→本地控制装受控端;desktop/README 组件头注+CI 资产清单。
- **parity 新判据(第三次同类教训):** `AppName=Tofu` 裸子串断言被 `AppName=Tofu Agent` 天然满足——**同一文件出现两个组件后,存在性断言必须锚定边界**(改钉 `'AppName=Tofu\n'` 换行锚)。与「存在性查不出多余出现」同族,记档。
- **epic 账(A1-A4 全绿):** 设计稿 v1→v3(owner 三修订+egress 移交验收收编);A1 载荷 84.8MB/烟雾闸擒 tkinter 潜伏;A2 安装包 53.2MB+注释 @ 令牌碰撞+kindless 抢行;A3 kind 轴全链+漂移投影;A2b CI 腿+闸门原子;A4 文档。剩余=§10 移交验收(真机 OAuth 全链),纯人门(重启服务器+办公机装包),已挂板待答。

### 2026-08-02(A3 落地:agent 安装包从「在店」到「够得着」——kind 轴全链 + remote 分支矩阵 + 漂移投影) — epic `pt_59b62951aad2463e`;commit `069776f8`(10 文件);环 **207/207**(dist+builders+parity+前端 harness+bundle/i18n+bridge)

- **后端 kind 轴五节:** release_assets.py 新增 AGENT_PLATFORM_ASSETS(4 行同形,globs TofuAgent- 前缀零碰撞;**刻意不入 REQUIRED**——入 REQUIRED 即触发自愈重建,必须与 A2b 的 CI 腿原子落地,注释写死);platforms/store/mirror 全链 kind 参数(默认 'full' 处处字节不变;mirror 双表迭代录 kind,剪枝救援谓词同 kind 匹配);winbuilder start/build_installer target 穿线。
- **路由三面:** status 载荷增 `agent_downloads`(与 downloads 同形,条目带 kind)+ agents 经 `_with_drift` 投影 outdated(双版本皆知且不等才 true——versionless legacy 只算 unknown 绝不哭狼);devices 同旗;`/desktop/build` 收 `{"kind":"agent"}`;autobuild 门**按 kind 独立**(built full 不压 agent kick——旧测试的「有 built 即静默」假设更新为 per-kind 契约并补齐四态)。
- **前端矩阵(§4.7 定案形态):** remote 分支 agent 主推(受控端·轻量+服务器直连 chip+体积+角色注解)+ 完整版一行次级链,逃生口只出现一次;`agent_downloads` 空时**回退历史渲染绝不留空主位**(stale-while-build);local_source 完整版主推零 agent 词。i18n 四键 zh/en;bundle+packs 已重建(bundle-ed1350eb / i18n-zh-66a0f9eb,键实测入包)。
- **纪律:** JSDOM harness 15 检查+NEUTER(阉 agent 分支→≥3 红);两处锚点重复自伤(insert_content 把锚文本写进 content——本 turn 第三次同类,desktop.py 与 test_desktop_dist.py 各一)逐行修复;`global _AGENT_ASSETS_CACHE` 漏声明 UnboundLocalError 当场抓回。
- **剩余切片:** A2b(CI 三平台 agent 腿+REQUIRED 合流,workflow+gates 同 PR 原子)+ A4(docs 退役「拷仓库」临时路径);egress 移交验收(真机 OAuth)挂 §10 待人答。

### 2026-08-02(预存红闭环:premature-finish-bar 针串重钉——仅针串漂移,契约意图不变) — 脑派发接我自票 `pt_2d8c58bdaa3a476e` **DONE**;commit `df949f37`(1 文件 +9/−5);邻接环 **120/120**

- **定案:** 针串 `if (!_terminal && isLiveTail) return "";` 被 c6989082 加宽为 `…(isLiveTail || msg.interruptedReason)…`(ms43foj3 冻结条根修,姊妹套件 test_frontend_finish_info_interrupted 已钉新形态)。本测试的宽矩阵契约(preset/effort-only 抑制、undefined-isLiveTail、usage-only 竖条)仍成立——按「原契约意图修、不登记豁免」重钉 sanity 针 + harness double-neuter 针 + docstring。
- **验证:** 内置 double-neuter 步骤实证新针负重(neuter_patch_applied + neuter_emits_premature_bar 双 PASS);姊妹 interrupted 套件 2/2 不惊扰;含 canned-greeting 批的邻接环从 117/118 收敛到 **120/120 全绿**。

### 2026-08-02(A2 落地:首个 agent 安装包入店 53.2MB(完整版 35%)+ 两枚实测陷阱各钉一根守卫) — epic `pt_59b62951aad2463e`;commits `eebbec35`(参数化+kind 轴)+ `c9d51216`(注释禁 @ 令牌)+ `da4a6c66`(kind 过滤前置);套件 **53+82 绿**;NEUTER×2 精确(剥自启值→2 红/回注 @ 令牌→1 红)

- **A2 四件:** ①NSI 模板全参数化(APP_NAME/APP_EXE/INSTALL_DIR/COMPONENTS_PAGE/INSTALL_REQUIRED/AUTOSTART_SECTION/AUTOSTART_UNINSTALL,完整版渲染行为逐字节等价);②自启组件=components 页默认勾「Start with Windows」写 HKCU Run 键+主段 SectionIn RO+卸载无条件清键——值名与 agent_launcher._RUN_VALUE 同键(parity 跨文件钉);③wrap_payload target 维度(exe 校验/命名/kind 入录,完整版默认参数零变化);④parity 套件改「渲染后断言」重写+自启契约+泄漏签名钉。
- **首个真 artifact:** `TofuAgent-Setup-0.16.0-win64.exe` **53,185,986 B**(完整安装包 152.9MB 的 ~35%),kind='agent',preseed 隧道址,makensis 真编译 components 页过。
- **实测陷阱一(渲染器全局替换咬人):** 模板头注释里的 `@AUTOSTART_SECTION@` 文档令牌被全局替换成代码段——WriteRegStr 落到 Section 外,makensis 首次真包装即 abort;parity 套件因只查存在性全程绿。修法=注释永禁 @ 令牌+parity 钉泄漏签名(SectionEnd 带尾文本)。**failing-first 的教训变体:源钉套件查存在性查不出「多余的出现」。**
- **实测陷阱二(kindless agent 抢行):** agent 包与完整版同 version 同 source 同行,wrap 更新即抢走 find_for_platform 的 windows 行——面板会把 agent 包发给要完整版的用户。§4.6 的 kind 过滤从 A3 前置止血(默认 'full' 全部调用方字节不变)+ 回归测试 + 真店实测双行各归其位。
- **事故自记(第二次同类):** NEUTER 后 `git checkout --` 把**未提交**的 A2 改动一并冲掉(上次是 insert_content 锚点复制)——先提交再玩 NEUTER,或 NEUTER 用 sed 逆操作恢复;已按此重做一次并各自验证。另:pytest 经 `| tail` 管道退出码被吞致红测试被提交,后 amend 修正——管道收尾必须重跑确认。

### 2026-08-02(autopilot 搁浅根修第二半:接力闸尸体容忍 + store 镜像尸体清除——与兄弟 `630b4af5` 互补闭环) — owner 截图三连问(信号好/侧栏闲/发送键活,为何 VU 气泡后无 agent 回复);commit `052e3fc5`(7 文件);新套件 **6 检查**(failing-first 精确 4 红)+ 邻接环 **168+43 绿** + 合并树终验(兄弟守卫+接力两族)**20/20**

- **全链定案(三层各自无过,接力面双缺口):** 后端日志+事件流实证——父任务 a0fa289b 完结→VU 载体 d5bf109a 制出回复**已落库**(11:49:29)→载体 discard→`maybe_run_autopilot` 末检 `_successor_already_running` 读到 conv→latest 索引里指向**死载体**的指针,误判「Superseded (a newer task owns conv)」让位收官,follow-up 永不孵化。前端三指标全部**如实**(无一任务在跑=侧栏闲/发送键活;同步链好=信号好)——是后端接力死了,不是显示错了。6.5 分钟后 brain dispatch 碰巧救活(5fd07a96)。
- **根因(兄弟会话 `630b4af5` 先于我 14 分钟独立定案并落地):** `85b54bc8`(③-3 tombstone)把 `make_task_abort_check` 插进 discard_task 函数体中部,尾部索引清理块被切到 `return _check` 之后成**不可达死代码**——25 天级每个 VU 轮末检都误杀 follow-up。兄弟把清理块移回函数尾部恢复契约。**方法教训自记:我首轮读 discard_task 只读到 tombstone 段头就收了,没扫到 ~L599 的孤悬清理块——读函数读半截是这次考古多花一小时的根。**
- **我补的兄弟未覆盖两半:** ①**接力闸尸体容忍**(F1,autopilot_baton)——即使清理再失灵,闸也不该把「指针≠我」当「被接管」:索引可能合法指向本 run 自己的死载体(HB-1 窗口)或任何终态尸体;改判**只有仍活在注册表里的任务才算接管者**(与 `_sync.py` 的 HB-1 豁免、`_live_successor_task_id` 的活性判据对齐——接力闸曾是唯一不做活性判断的读者);真假接管两条路径都留 INFO 取证日志(下一起同类事故不再靠考古)。②**store 镜像尸体**(F2)——`_record_latest_task` 双写的 runtime_state_store 镜像(TTL 1h)此前**无任何删除路径**:discard/maintenance 只清本地 dict,store 优先的 `_latest_task_for_conv` 继续把尸体当主人长达一小时(兄弟的根修不含这一半)。新增 `_state._clear_latest_task`(本地+镜像同清,compare-and-delete 纪律保留)+ 两个 store 后端 `delete_value`,discard_task 与两处 maintenance 剪枝全部改走。
- **纪律:** failing-first stash 实证精确 4 红(尸体容忍×2/镜像清除/helper 红,稳态×2 绿);共享树三方共舞零冲突(兄弟根修 630b4af5 12:12 → 我的互补 052e3fc5 叠其上,我的 `_registry` diff 精确命中兄弟修复后的清理块原位替换);`test_health_endpoint` 红 stash 复跑仍红=预存不夹修。

### 2026-08-02(A1 落地:agent-only 构建目标代码全量 + tkinter 潜伏大案被烟雾闸当场擒获) — epic `pt_59b62951aad2463e`;commits `3e5696f5`(A1 主体 9 文件)+ `d773be5e`(注册表 version 持久化)+ `85c88049`(tk 嫁接);新套件 **18 检查**(failing-first 11 红实证)+ 邻接环 **265+27+38 全绿**

- **A1 五件:** ①connect_ui.py 抽取(_prompt_connect_line/_import_preseed **移动非复制**,launcher 留委托壳——测试补丁点零惊扰,契约测试按「符号重锚」先例改钉 connect_ui);②agent_launcher.py(四幕:preseed→连接对话框→deny-all 权限地板→agent 线程+极简托盘;托盘=全部「配置能力」:Server 标签/Connect/权限四档含 egress/Start with Windows/Quit;自启三件套=winreg HKCU Run 键+config 持久化+启动对账);③tofu-agent.spec(agent 闭包+服务器栈全 exclude);④winbuilder target 维度(_TARGETS 表驱动 spec/exe/烟雾阀/载荷名;agent pip 配方**绝不碰 requirements.txt**;full 侧逐字节不变,旧缓存全保);⑤注册帧+注册表双端 version 字段(owner 修订②探测半落地——register_agent 原本**逐字段 cherry-pick 会丢 version**,已补+心跳回退测)。
- **大案(烟雾闸设计价值的实证):** 首次真机构建 2.5 分钟跑到烟雾步,`TOFU_AGENT_SMOKE` 精确红在 `ModuleNotFoundError: No module named 'tkinter'`——**nuget CPython 全包 0 个 tcl 文件**(nupkg 实测),意味着**当前在店的完整版安装包的连接对话框对真实用户也是死的**(full 烟雾只 import server 从不碰 tkinter,潜伏至今)。根修:python.org 同版本 tcltk.msi(msiextract 本地解)→ 嫁接 DLLs/_tkinter.pyd+tcl86t/tk86t+Lib/tkinter+tcl/ → wine 实测 TkVersion 8.6;_ensure_winpython 幂等接线 + agent 戳含 tk sha(完整版戳不动,下次自然重建顺带治愈,按 owner 潜伏另案规则记账)。
- **纪律:** failing-first  stash 实证精确 11 红;两枚测试自伤当场修复(断言撞上 docstring 里的文件名/tgt 下标单双引号混排改规范化);pathlib 笔误两连(isfile→is_file);egress 移交验收(真机 OAuth 全链)已入 §10 验收表。
- **构建耗时实测:** agent 目标到烟雾步仅 ~2.5 分钟(小闭包红利,full 目标同级步骤半小时级)。

### 2026-08-02(canned-greeting 误判三连根修:smalltalk 闸被尾巴注入废掉 + 唯一带正文重试桶无重置) — owner 截图报 deepseek-v4-flash「你好」收三遍问候;commit `6d3ffce3`(7 文件 +278/−4);新测试 4+2(failing-first 精确 5 红);**NEUTER×2 各咬各的**(cmp 字节还原);邻接环 **117/118**

- **病灶定案(双 bug 叠加):** ①`last_user_is_smalltalk` 读的是被 `_refresh_tail_block`(system_context/_reminders.py:88)尾巴注入过的最后一条用户消息——日期块无条件追加(_inject.py:891),「你好」变 blocks 列表,拍平恒超 30 字符,保护闸**在生产线上从未触发过**;13 字符电报式问候恰好穿过 ≤60 字符+问候正则两道闸被误判为上游事故。②canned-greeting 是**唯一带正文重试的桶**(zero-byte/classic/empty-stop 都要求空正文),重试前不清 task['content']/thinking、不发重置事件 → 3 轮(1+2 重试,`_CANNED_GREETING_RETRY_MAX=2`)×13 字符在气泡与 DB 里首尾相接,与截图 3轮/85 tokens/相位文案全对上。
- **修法:** (a)smalltalk 判定前剥 `<system-reminder>` 块——注入不是用户的话;(b)canned 重试分支重置正文 + 发 `delta_reset(discard:true)`——reducer 冻结守卫(`_stampDeltaReset`)只认工具轮批次,被弃轮无工具调用=无批次可盖章,守卫会永远留着毒文本,discard 信号无条件清空但保留工具轮;(c)events.py DELTA_RESET 契约文档化 discard 字段。
- **纪律:** failing-first 精确 5 红(分类 3+后端重置 1+前端 discard 1,前端 harness 在 HEAD 上原样复现问候堆叠);NEUTER-A 阉剥离→精确 3 红,NEUTER-B 阉重置+reducer 分支→精确 2 红,均 cmp 字节还原;node --check + AST 闸 + 导入冒烟过;邻接环 117/118,唯一红 test_frontend_premature_finish_bar 证为预存(finish_info.js 零未提交 diff、针串被 c6989082 重构、测试自报 stale),独立立案 `pt_2d8c58bdaa3a476e` 不夹修。
- **教训记档:** ①凡「读用户消息」的判定闸,都要问一句读的是不是 wire 形态——尾巴注入之后,用户的话≠最后一条 user 消息的全文;②重试桶设计审计:只要被弃轮**可能带正文**,就必须配对「后端重置累加器+前端重置事件」,delta_reset/retry_reset 的选择看是否保工具轮;③检测器的互补闸(complement)必须用**生产形态**的 fixture 测,纯净 fixture 全绿≠线上在保护。教训入记忆库。

### 2026-08-02(批跑互染根修:wire-parity「清场」fixture 摧毁单模块实例不变量——改恢复式共享助手) — 脑派发接我自票 `pt_788b25a5ce9c47e2` **DONE**;commit `87a54720`(3 文件 +77/−19);最小对 15+28 绿、全批次 **275/275**(原 273+2红);NEUTER 精确(阉恢复→两对精确各 1 红,cmp 字节还原复绿)

- **病灶(测试文件间的军备竞赛):** markers_functional 曾用 `importlib.reload` 破坏 facade↔markers 恒等;markers_extraction 的「修法」是在 fixture setup+teardown 双侧 `del sys.modules['lib.tasks_pkg.autopilot*']`(event_forwarding 同型)。清场后**不恢复**:收集期持有原模块对象的下游测试文件与 sys.modules 断连——①markers_functional 的 `importlib.reload(ap_markers)` 找不到条目 → ImportError;②yield_not_destroy 的字符串靶 monkeypatch(`'lib.tasks_pkg.autopilot_run_lifecycle._store_run_record'`)重导入**新副本** L2,补丁打在 L2 上、被测代码跑在收集期原件 L1 上——真 `_store_run_record` 撞真实 DB(conv 不存在)→ None → 提前返回 → 捕获列表为空。最小对复现两路因果:event_forwarding+yield 红、markers_extraction+functional 红。
- **修法:** 共享助手 `tests/_hermetic_import.py::hermetic_import_surface`——窗口内断言语义零变化(照样全新导入面),teardown 丢弃副本并**恢复原模块对象+父包子属性**(import a.b.c 经属性绑定而非仅 sys.modules,沿用 _nc_harness 的 swap both/restore both 纪律)。两个肇事 fixture 改走助手。
- **教训记档:** 「hermetic」不能只清不还——测试隔离的完整语义是**借用后归还**;清场类 fixture 的 teardown 必须把会话状态还原到入场前,否则每个「自保」的测试都在给下一个测试埋雷。模式入记忆库。

### 2026-08-02(autopilot「气泡落库却无 agent 回复」根修:discard_task 清理块孤悬死代码 25 天级误杀 follow-up) — owner 截图报 msb6ohqifdz7yj VU 气泡后对话死寂;commit `630b4af5`(2 文件 +7/−6);守卫测试红→绿,全守卫环 HEAD 5 红→2 红(残余=预存批污染对,已立案 `pt_788b25a5ce9c47e2` 不夹修)

- **病灶定案(纯日志+blame 链):** 11:49:29 `Superseded (a newer task owns conv=msb6ohqi) just before follow-up spawn` ——但此刻根本没有任何新任务。真相:VU carrier(HB-1)把 `_conv_latest_task[conv]` 据为己有后,`discard_task` 的 docstring 承诺「清理它 claim 的索引项」,实现却只剩 registry pop——清理块在 commit `85b54bc8`(8-01 20:05 ③-3 tombstone)被插进函数体中部的 `make_task_abort_check` 切到了 `return _check` 之后,成为不可达死代码(blame 双锚:块归 d480479f、return 归 85b54bc8;`git show 85b54bc8^` 证原形态在 discard_task 尾部)。索引永远指向已死 carrier → 末检 `_successor_already_running` 假阳性 → run 以 reason='superseded' 收官、VU 气泡照落库、follow-up 不 spawn。10:36 重启载入坏代码后,每次 VU 轮必现。
- **前端一切正常恰是症状的一部分:** 信号 30ms/侧栏无生成中/发送钮激活全部属实——后端已主动收官,没有任何任务可同步;done 帧无 autopilotNextTaskId,客户端正确地显示空闲。用户 11:55:50 手抄 VU 文本当真消息发出(task 5fd07a96)自救。
- **修法:** 清理块归位 discard_task 尾部(恢复 d480479f 契约),死代码删除;顺带修批 11 裸数组迁移(api_ok 顶层 items)后同文件两测试的解包漂移(TypeError: string indices)。
- **纪律:** failing-first 3 红(含守卫 test_discard_task_pops_registry_and_latest_index)→ 修复后 273 过;markers/yield 批跑污染对经 HEAD stash 基线批跑证为预存(HEAD 同红、隔离与三文件同跑全绿),独立立案不夹修。教训入记忆库:return 后孤儿块 AST 闸查不出,docstring 副作用必须有行为测试钉住。
- **注意:** 运行中服务器仍带 bug,重启后生效;该会话 autopilot armed 标记仍在,下次发送/kick 即恢复正常接力。

### 2026-08-02(owner 三修订折入设计稿 v3:开机自启 / 版本漂移检测 / 会话边界 + 收编 egress 移交验收——A1 开工) — owner 签字设计阶段后补三硬需求;epic `pt_59b62951aad2463e`

- **① 开机自启(relay 生死项,v1/v2 全漏):** 被控端头号场景=无人值守中继机,Windows Update 半夜重启即静默断桥。NSIS 加 components 页默认勾「Start with Windows」写 HKCU Run 键(模板本就 `RequestExecutionLevel user`,免 UAC),卸载器无条件清键;Inno 侧 `[Tasks]`+`[Registry] uninsdeletevalue` 等价;托盘加开关持久化 config 且每次启动 config→注册表对账;parity 契约钉「agent 默认 ON / 完整版不加」。
- **② agent↔server 协议同源 + 漂移可观测:** `_build_agent_frame` 实测五字段无 version(§2 新实测行)——A1 加一字段,A3 在 devices/状态载荷投影「agent 落后→下同 HEAD 包」;§5.2 的 server-first 对 agent 从「新鲜」升级为「协议同源」论据(egress 帧/stream_outbox 形状随服务器演进,release 线 agent 对 HEAD 服务器可能静默错派)。
- **③ 诚实边界(照 client-build 稿 §7 体例):** 托盘形态=交互式登录会话前提,真无头 relay 需 Windows 服务化打包——v1 明确 NON-goal 记档(§6),防下个人重新调研。
- **收编移交:** egress epic 关票时 owner 判「不要手动执行命令」——真机 OAuth 全链验收(浏览器登录→经 agent 出口→流式→Codex O3)正式挂进本 epic §10 验收表:安装包落地、agent 自启时顺手验,零手动命令。本 epic 对 pt_4ea6bf05deaa46f0 的依赖随之解锁。
- **文档:** 351→413 行十节;切片账:A1+frame version 字段、A2+自启三件套、A3+漂移投影。A1 随即开工(claim 有效)。

### 2026-08-02(egress epic 关票:owner 判「不要让我手动执行命令」——真机验收定义性移交打包 epic;`pt_4ea6bf05deaa46f0` DONE)

- **owner 指令定盘:** 转发路问题卡三答之一都不是——「不要让我去手动执行命令了,我没这个兴趣」。手动烟死路,产品正解=agent 随安装包自动起(本来就是下游 `pt_59b62951aad2463e` 的形态,msb9kssc 在飞)。
- **关票证据账(全部实测):** ①egress 全族套件 **180/180 绿**(desktop_egress/stream/bridge_addressing/agent + oauth egress_status/exchange_errors/claude_cloak/outbound + 前端 egress_line);②容器内真实 agent `--allow-egress --bridge-secret` 全链 E2E 早已闭环:注册上线→capabilities.egress=true→`/api/v1/oauth/status` 从 unknown 翻转 state=agent+verdict=geo_blocked;③凭证链重启后复测:带 key poll 200(长轮询 ~8s 语义)/无 key 401。
- **移交(写进打包 epic 语境):** 真机 OAuth 往返(浏览器登录 claude.ai→token 交换经办公机出口→流式→Codex O3)不删,挂进 `pt_59b62951aad2463e` 的验收表——安装包落地、agent 自动起时顺手验,零手动命令。依赖关系就此解锁。

### 2026-08-02(分发面三决策落稿:Local Control 显示矩阵 / 完整版导向 / GitHub Releases 内容——设计稿升 v2,commit 见下) — owner 三连问定案,epic `pt_59b62951aad2463e`

- **Q1 面板显示矩阵(守「每状态一个下一步」铁律):** remote 分支(被控场景)主推「受控端·轻量」agent 包+服务器直连 chip,完整版退一行次级链,铸连接行不动;local_source 主推完整版(源码已能跑 agent,不给次链);tray/connected 零下载。每行带一句角色注解(「只让这台电脑被服务器操作」vs「这台电脑自己跑 Tofu」),选择不需要文件名素养。
- **Q2 完整版导向=原则不变双组件同通道:** ①本服务器商店主推(HEAD 新鲜+preseed+不依赖客户端到 GitHub 的路由);②releases 页仍是逃生口;③mirror 扩展镜像 agent 资产——macOS 双 kind 全靠它(服务器结构性建不了)。
- **Q3 Releases 加 agent 三平台腿(纯增量,完整版 5 资产不动):** win(Inno,真 Windows runner 无 wine 陷阱;与服务器 NSIS 由 parity 契约绑 2 组件×2 作者权)/macOS 双 DMG(**macOS agent 唯一能存在的来源**)/linux tar.gz(.deb 仍归完整版独占防发布页翻倍)。上传权衡记档:agent 无服务器则无用→命名(TofuAgent- vs Tofu-Setup)+release notes+README 双行下载表化解;不上传的代价更坏(macOS 永无 agent+工具链挂时无备胎)。
- **关键修正(v1→v2):** ①agent 行入 `AGENT_PLATFORM_ASSETS` 新表(同 5 元组形状零 churn)但**并入 REQUIRED 派生集**——缺 agent 腿=发布不完整,version 闸 build-on-INCOMPLETE 自愈;与 CI 腿同 PR 原子落地(v1 原写「不动 CI parity」被本决策推翻);②mirror 从「永不供 agent」改「CI 产出即镜像」;③切片增 A2b(CI 腿+闸门同改)。文档 278→351 行,九节重编号。

### 2026-08-02(Agent-only Windows 安装包设计稿落盘:被控端/完整桌面版分发分离——`docs/DESKTOP_AGENT_DIST_DESIGN.md`;epic `pt_59b62951aad2463e` 已认领;实现未开工待 owner 审) — owner 指令「两个可安装组件在设计上揉在一起,应该分离」脑暴定案后成稿

- **定案:融合是分发层事实,不是代码层事实。** `lib/desktop_agent/` 早已是独立净包(模块级依赖=requests+lib.log(stdlib-only)+lib.json_store,pyautogui/pyperclip/psutil/PIL 全 guarded),`python -m lib.desktop_agent` 纯 CLI 可跑——但**没有任何 Windows 分发路径**:要么装 152MB 完整桌面版(tofu.spec 打进整个 Quart 服务器+路由树+Hypercorn+psycopg2+playwright+trafilatura+pymupdf,S2 实测 152.7MB/3316 文件),要么按 egress 设计稿 §11 的「临时路径」拷贝整个仓库跑源码。Local Control 的 remote 分支(唯一需要 token 的场景)只给完整版下载。egress epic(pt_4ea6bf05deaa46f0)的部署目标正是这个无出口的被控端。
- **设计稿要点(全实测约束,同 client-build 稿纪律):** ①体积论是导入图事实而非希望——agent 闭包不含服务器栈任何一件,估 ≤~45MB(A1 实测上秤);②构建零新基建——wine 工具链+NSIS+parity 契约全现成,agent 只是同一工具链上第二个 PyInstaller target(payload 缓存加 target 维度,deps_stamp 只盯 agent 依赖集);③「配置能力就够」已存在:~100 行 tkinter(`_prompt_connect_line`+`_tk_theme`)+ preseed 契约,从 launcher.py **移动**到 desktop/connect_ui.py(不复制——两副本必漂移,parity 哲学的反面);④烟雾闸照抄 TOFU_SMOKE 的「退出码即判决」纪律,加导入图断言(bundle 树无 quart/flask/hypercorn 文件)——未来谁把服务器栈拖进 agent 闭包,红的是构建不是用户机器。
- **刻意保留的融合:** 完整桌面版的 in-process agent 不动(单机用户零配置控制自己电脑是特性);分离只是分发事实,代码仍一树共享缝(连接对话框/preseed/tk 主题/config 文件/NSIS 模板/parity 契约)。两个被拒备选记档:zip+脚本(NSIS 包装是 ~1 分钟的便宜半边,白拿卸载/快捷方式/parity)与单安装包组件勾选(152MB 是载荷不是包装,勾选救不了字节)。
- **切片:** A1=connect_ui 抽取+agent_launcher+tofu-agent.spec+Half A target 维度+烟雾闸(测仿管道如 test_winbuilder,NEUTER 钉缓存命中);A2=NSIS 模板参数化(@APP_NAME@/@APP_EXE@/@INSTALL_DIR_NAME@,完整版渲染现值 parity 扩不重写)+首个 TofuAgent-Setup 入店 kind='agent';A3=分发+UI(AGENT_PLATFORM_ASSETS 新表不动 CI parity、find_for_platform kind 过滤默认 'full' 零迁移、downloads[] 带 kind、remote 分支 agent 主链完整版次链、autobuild 门扩展);A4=docs 退役「拷仓库」临时路径。curl_cffi 作 optional hidden import 现在就位,egress epic 落地 TLS 指纹依赖时打包零改动。

### 2026-08-02(egress 接线:owner 拍板「改走免重启转发路」——服务器侧预检全绿,agent 待上线;epic `pt_4ea6bf05deaa46f0`)

- **预检(重启后凭证链仍完好):** `data/config/.egress_bridge_key`(0600)在现行进程内实测:带 key POST `/api/desktop/poll` → 长轮询 ~8s 后 **200 `{"commands":[]}`**(-m 5 得 000 是长轮询挂起,非异常);无 key → 立即 401。注册表空且无污染(preflight agent 单轮即过期)。**之前 000 一度疑云,定案=长轮询语义**。
- **剩余闸门=纯办公机侧:** `ssh -L 15000:127.0.0.1:15000 <codelab-ssh>` + `python -m lib.desktop_agent --server http://127.0.0.1:15000 --allow-egress --bridge-secret <key>`。服务器侧已无事可做。
- **守候形态:** 板面问题卡(三键) + registry watcher(check_command 出注册表 JSON、poll LLM 读内容判定,不用退出码——沿用 7-31 教训修正形态,JOURNAL L432 有先例)。agent 一上线任一路径触发即继续:能力位 → 状态五态翻转 → Claude 登录 → 流式 → Codex O3。

### 2026-08-01(阅读体验 P0-P4 全量落地:教科书 → 陪读导师;epic `pt_08894d6112bf4c68` DONE) — owner 一键批「批准全量 P0-P4」后五期连发:`fe270ce9`/`2df7408f`/`5b8f97d0`/`3209c43e`/`7865fa34`(68 文件净增 ~5.4K 行);新套件 **12+9+6+3 后端 + 前端 JSDOM 76+ 检查**(各含 NEUTER);全邻接回归 **200+ 绿**;feature bundle `27f3cf25` 实测四模块齐备

- **主线=接线而非新建:** insight 引擎(A/B 验证过)从 env-gated OFF 转**四级链默认 ON**(cfg 戳>server_config>env>ON;personal_scope 注册 paperInsightEnabled/paperCheckpointsEnabled,headless fail-closed);ideate 1639 行经 open_problem 卡一键进 `_startResearchJob`;QA 经 provocation 卡一键开辩(`_paperAskQuestion`)。两个新引擎都是克隆已有机架:checkpoint(单发无工具 JSON+修复重问)、deepen(qa 机架,三模式,`deep:<mode>:<sec>:<ui>` 缓存槽+section_hash 新鲜度校验——报告再生即失效)。
- **owner 钦定第一优先=成本可见:** rubric/合成/termfill/checkpoint/deepen 的 usage 全部落 `meta.secondPasses`(deepen 多次累计),meta-only 重持久化 + `report_meta` 事件热更 finish tag;总量=本体+Σ二遍,徽章 tooltip 分解。NEUTER 判据:摘合并总额精确回落。
- **锚定分发(设计 §3.2 第三次「prompt 提名代码定址」):** 模型提名节标题,`_anchors` 规范化精确→Jaccard≥0.6→回退文末;anchor_idx 随行持久化;前端连接/挑衅追问卡插到对应节标题后,checkpoint 翻面卡插节末,回收卡收尾。
- **三个顺手抓的根修:** ①`_append_cached_insight` 幂等判据裸 `'## 💡'` 子串误伤所有含 💡 Method 标题的报告(v1 合并静默吞掉,预存潜伏)——收窄为精确小节标题判据,新套件 v1 红抓出;②**`_reportView()` 每次返回新字面量**,view._xpInsight/_paperNotes/_xpCheckpoints 自定义属性跨实例丢失(live 事件后重渲染卡片即消失)——xp payload 收模块级 store 按 paper+kind+lang 键控(生产级根修,P4 内);③notes.js `_paperHash` 裸引用 ReferenceError(after-render 缝每次渲染都触发)——typeof 守卫。
- **harness 纪律三教训(同族,已记):** ①再分发后旧 DOM 引用 detached,点击永远到不了 document 委托——断言前重查活节点;②被测函数被 harness 自己的 stub 覆盖(_extractGlossary),stub 前先捕获真函数;③_报告View 跨实例断言必须走 store 读写(_paperXpSet/_paperXpGet),绕视图实例。
- **新路由纪律:** deepen start + notes CRUD 全 api_ok 原生;`/deepen/poll|abort` 是 paper 家族首个骑 `_task_routes` 通用工厂的任务(drawer 吃事件流,无需引擎特异键);漂移棘轮零惊扰。schema 44→45(paper_notes 新表,Core 单源双端)。

### 2026-08-02(Epic-E sub-10 收尾:settings/ 整族 403KB 降级落地——生产 core 1,045,274 B,**1.2MB 验收线生产达成**) — epic `pt_3879f00e`;commit `503859b9`(5 文件,联席)+ runbook 52 项 ALL GREEN;ledger 行+判读已更

- **破线数字:** 基线 1,550,424 → 生产 **1,045,274 B**(`bundle-40dde573.js`,农场/生产同 hash),累计 **−505KB 压缩态 / −32.6%**;较 1.2MB(≈1,228,800)余量 ~183KB。feature 1,012,457 B(下一战场,不属本 epic 口径)。十二片流水片片有账(commit/字节/NEUTER/runbook 证据链)。
- **联席形态(共享树第四次共舞,首次按剧本走):** 兄弟会话(mrxinirv)全家迁移在飞期间,本会话以「普查独立反查」连抓两枚真坑并补丁在树,最终零冲突联席提交:①**branding.js 绝不能降级**——其普查「no boot callers」是错的:main.js:88/349 在 `_applyModelUI` **裸调** `_modelShortName()`(boot 模型绘制),降级即 boot ReferenceError;已还原留 core(家族对它 14 处调用全是 deferred→core 安全向),其套件与我套件各钉一根边界钉,NEUTER 双红互证独立鉴别力。②**'openSettings' 在 LoadGuard stub 表**——toggleMemory 同坑(sub-9C 判例),已摘除。两坑若随原普查出厂,分别是「boot 即破」与「设置齿轮裸窗死态」。
- **纪律:** 双套件 22 检查 failing-first(4 红起点)+ NEUTER×2 精确(branding→双红 / 摘 openSettings stub→精确 1 红,cmp 字节还原);环 **96/96**;runbook 扩 sub-10 十项后 **52 项 ALL GREEN**(农场失败一次实证为第三方在飞截断 tool_rounds.js,自愈后重建同 hash——快照会撒谎,构建才是事实源)。
- **收尾账(待 claim-holder 终审):** Epic-E 验收口径=core ≤1.2MB ✅ + 每片有账 ✅,ledger 已按「验收线已达成」改写,complete 判读归 mrxinirv(分工约定)。api-contract 兄弟同晚收官(paper.py 47 清零,272 站点全清零,契约棘轮值守)。

- **病灶:** Epic-E sub-4(`fcddc420`)引入的 `_upgradeDegradedToolRounds` IIFE(deferred 富渲染包落地后给降级的 conv-meta/timer 轮做升级重渲染)用了裸 `renderChat(conv)`——`test_full_repaints_route_through_replaceAll` 的全树零裸调用契约当场抓住(step-5 后豁免注册表本已清零,这一处是 sub-4 的新增漏网)。
- **修法:** 改走 `window.ConvView.replaceAll(conv.id, { forceScroll: false })`(全仓 15+ 处同款成语);forceScroll:false 对后台静默升级更优(不拽用户滚动条);`typeof renderChat` 守卫一并退役(seam 由 boot 硬检查保底,守卫明确禁止 typeof-guard 回潮)。
- **纪律:** 红→绿(守卫精确红在 1 处裸调用);NEUTER 回注裸调用→守卫精确 1 红,cmp 逐字节还原;邻接环 38/38(含 Epic-E 自家 rich split/modes/wire parity——seam 迁移对 deferred 渲染契约零失真)。
- **方法记一笔:** 预存红清剿三步仍是铁律——先证 HEAD 可复现(文件零未提交 diff ⇒ 非兄弟 WIP、非我回归)、再按原契约的意图修(不是登记豁免,step-5 的精神是「无物可免」)、NEUTER 用守卫本身当验尸官。

### 2026-08-02(run_command 命令体默认折叠进标题:描述即开关,长命令不再刷屏) — owner 指令「代码默认折叠在标题里,用户反正不看代码」;commit `e53ba261`(5 文件);新套件 **2/2**(failing-first 精确 2 红);wire-parity 基线 43→45 **零既有轮失真**;邻接环 **38/38**

- **形态:** run_command/code_exec 卡片的 `$ 命令` <pre> 默认折叠——判据=有 description 且命令为多行或 >100 字符(短单行如 `npm test` 保持可见=一瞥即读;无 description 保持可见=命令是卡片唯一身份,折叠即匿名)。**描述本身成为开关**(chevron `▸` 前缀,点击原地展开/收起),正合 owner「折叠在标题里」的原话;展开态按 toolCallId 持久(`_cmdBodyExpanded` Set),运行中 tool_progress 每次重渲染不塌回、时间线同步/换会话后仍记得。
- **覆盖双态:** done 块(`_renderCmdDoneBlock`)与 running 块(`_renderSearchingRow`)同判据——运行中折叠后实时输出/QR 条上移立即可见(两者本就在折叠 pre 之外)。审批卡/stdin 等待卡不动(那都是「必须看清命令」的场景)。顺带实证:styles.css:8242 早有一段「Collapsible command line (done state)」的**空头注释**(只有注释没有规则,规划了未实现),本次就在原址补全。
- **测试:** 新套件 tests/test_frontend_cmd_collapse.py(JSDOM 25 检查:折叠标记/开关/命令仍在 DOM/输出开关不受累/双态/code_exec 同族/展开-重渲染-再折叠全链 + CSS 静态闸)。**坑一记:eval 进来的脚本里 `const` 不挂全局**,harness 直接引用 `_cmdBodyExpanded` 吃 ReferenceError——改行为式断言(重渲染恢复态)反而更强。wire-parity 电池 +2 轮(长命令 done/running),基线重生成**43 轮既有零 diff、纯增量 2 轮**——阈值判据没惊扰任何短命令既有渲染。
- **事故自记:** insert_content 把锚点两行也写进了插入内容,造成 `_renderCmdDoneBlock` 头部复制、花括号失衡吞掉文件尾部(node --check: Unexpected end of input)——锚点文本不该在 content 里重复;逐行读区修复后 JS_OK。

### 2026-08-02(api-contract 终批:paper.py 47 站点清零——272→0 MIGRATION COMPLETE,棘轮从此是纯防御) — owner 指令(「先判闸后动手」)接 epic `pt_931e16c4`;commit `06f80cfc`(3 文件);parity **2/2**,环 **30/30 + 16/16 + 37/37**,NEUTER×2 精确

- **判闸(owner 规则先执行):** 板+peer+三日 JOURNAL 实证无兄弟在拆 paper.py(近三天仅 2 个 async 化小 commit);「拆分路线图避让」与 chat.py/conversations.py 同型——临时防撞闸非永久禁令,且 **migrate-first 是更稳序**(信封助手随后来的拆分随 handler 走)。
- **分类账(47 站点六形):** ~21 个 ok:True dict→api_ok;9 个 resp 透传→api_payload(resp, 200);4 个裸 `{'ok': False}` 200→api_payload 逐字节(request_id 只在 ≥400 附);3 个 custom-200 错误 dict(report_required 族 + 200|409 report 状态)→api_payload(..., N);4 个显式 400→api_bad_request(**元组包裹站保留外层括号变 `(api_bad_request(...))` ——(resp,status) 元组形状不变**);5 个显式 404→api_not_found;1 个 `jsonify(report), status`→api_payload(report, status)。
- **变换器两次自伤复盘(教训立档):** ①ok 条目同行折叠站(`'ok': True, 'cached': True` 一行两键)漏配——按「ok 条目必在最宽形态出现」改行内剥除;②元组包裹 400 站(`(jsonify(...), 400)`)被正则吃掉闭括号——AST 闸当轮抓回,改「只换内层 span 保留外围括号」。**脚本变换后 AST parse 是硬闸,不是可选项。**
- **纪律:** failing-first 精确 1 红(shipped-source);NEUTER×2(回注 jsonify→shipped-source 红;摘 api_payload→needle 红,均 cmp 字节还原);漂移棘轮 BASELINE 清空 + 头注记「迁移完成,棘轮从此纯防御(test_no_new_jsonify_files)」;server import smoke(标准闸)过;paper 行为套件 37/37、迁移套件 16/16、契约环 30/30。
- **epic 账:** 272 站点 33 文件 → **0**(21 批 + 终批;另有 5 文件入册 carve-out 协议豁免)。pt_931e16c4 关票。

### 2026-08-01(阅读体验设计稿落盘:五目标机制 → `docs/PAPER_READING_EXPERIENCE_DESIGN.md`;epic `pt_08894d6112bf4c68` 已上板;实现未开工待 owner 审) — owner 指令把脑暴落成正式设计稿并补四个核实缺口

- **脑暴轮定案:** 瓶颈不是报告质量而是「静态交付物」——insight 引擎(A/B 验证过)默认 OFF、ideate 1639 行无单论文路由、QA 划词已在线。设计稿 = 把已建引擎群接进阅读流。
- **owner 核实后补进设计的四缺口(全部落稿):** ①**成本可见性 P0**——二遍 token 用量目前不落任何 meta(`_usage_total` 只累计报告轮次),meta 持久化先于二遍,设计 secondPasses 合并 + meta 二次持久化 + finish tag 分解;②**anchor_section 确定性解析**——prompt 提名节标题、代码定址(精确→token 重叠 0.6→回退文末),锚随 insight 行持久化,`finalize_review_body` 先例第三次应用;③**深度路线裁定**——`:::deep` 围栏 vs 零 LLM 后切分+按需深挖,按「prompt 要求+确定性兜底」屋内风格选后者,主 prompt 零改动、深度成本只在点击时支付;④**多语言不翻倍**——`<kind>:<ui_lang>` 键位纪律,只为用户实际生成的语言生产体验产物。
- **owner 纠正的一处误判已吸收:** `_append_cached_insight`(routes/paper.py:186)已在读路径合并缓存 insight,P0 增量只有「锚定分发+默认开」;另核实 `_askAboutPaperSelection` 划词问句已在线(深挖走它的系统化版)。
- **分期:** P0 获得感+成本可见(纯接线)→ P1 启发(provocation→QA、open_problem→research 预填)→ P2 易懂(checkpoint 二遍+术语类比+速览折叠)→ P3 深度(克隆 qa 机架的 deepen_engine)→ P4 沉浸(专注模式+paper_notes 表+会话小结);每期 failing-first+NEUTER 验收表已写进稿子;新路由全 api_ok 原生,明确避开 api-contract 兄弟批的 paper.py 迁移面(新端点落 routes/api_v1/paper.py)。

### 2026-08-02(压缩相位胶囊生命周期闭环:终态事件/compaction_done 退休相位快照 + 前端模块自有折叠;「压完两小时还显示正在压」根治) — 脑派发接我自票 `pt_f222e9ed288a44b3`;commit 见下(6 文件);新套件后端 **6/6** + 前端 JSDOM **2/2**(含 NEUTER 模式);NEUTER×2 磁盘级精确(cmp 逐字节还原);环 **53 过 1 预存红**

- **病灶定案(三层证据):** ①`manager/_events.py` 的 `task['phase']` **只在 delta 事件清除**——任务在相位挂着时终结(摘要调用中途被杀/出错),轮询道/冷重放就永远拿到这个「活相位」;②compaction_done(压缩自己的终态)不折叠相位,快乐路径下 HUD 也活到下一轮相位事件才来;③前端 `_handleCompaction` 收到 compaction_done 只升级标记不清 session。实测:20:10 的 compacting 相位 22:22 仍显示,DB 证明两小时无任何新压缩。
- **修法(零新线面):** 后端 `_events.py`——done/error/aborted 终态清快照 + compaction_done 且当前相位是 compacting 时折叠(不相关的活相位永不误伤);前端 `sse_handlers_misc.js`——compaction_done 经 `foldStreamPhaseIf(convId,'compacting')` 折叠。**架构要点:折叠收口为 stream_session 模块自有 API(读留在模块内),RENDER_CONTRACT 钉死的 reader/writer 面零扩张**——convview 守卫两个面(读 3 文件/写 3 文件)一字未改全绿。
- **守卫卫生(同批):** convview 守卫 6 处扫描豁免补 `feature-<8hex>.js`(.gitignore:163-167 已钉的 Epic-E 内容散列构建产物,与 bundle-* 同类)——兄弟 Epic-E 在本地构建留下的未跟踪产物此前让守卫误红 3 项。
- **纪律:** failing-first 精确(后端 3 红 + 前端 harness 精确 1 FAIL);NEUTER×2 磁盘级(阉终态分支→精确 2 红;阉折叠分支→精确 1 红;均 cmp 逐字节还原);前端 harness 自带 scratch 阉割模式(与 stream_phase_i18n 同制)。环 53/54:**唯一红=test_full_repaints_route_through_replaceAll 预存红**(tool_rounds_rich.js 在 Epic-E sub-4 `fcddc420` 就有的 bare renderChat(,HEAD 复现、文件零未提交 diff)——按 owner 规则独立立案 `pt_3f84ebfc876a4da8` 不夹修。i18n 双测批跑偶红、隔离/复跑全绿,按惯例记 flake。
- **教训记一笔:** 相位类 HUD 必须有成对的生命周期——「开始事件」若没有「结束事件」,就必然在断开/重放/终态边界上变成说谎的常驻标签。加相位时先问:它的终态在哪。

### 2026-08-01(api-contract 收尾挂问题卡:只剩 paper.py 的排序权交回 owner + 契约文档同步至批 21 现实) — epic `pt_931e16c4` [human-gated] 问题卡已上板

- **状态:** 21 批 225/272 站点清零(82.7%),基线只剩 paper.py 47。避让指令的前提实证不成立(拆分无 epic、无人认领、文件零 WIP),但不擅自绕开 owner 明令——问题卡三选项上板:**(a) 先迁后拆[我推荐]**(迁移后 api_* 行对按行搬的拆分完全等价,假想冲突不存在)/(b) 等拆分落地/(c) 我先拆后迁。人答即续。
- **顺手收口(不依赖答案的部分):** 契约文档同步到批 21 现实——§4 桥行补 routes/desktop.py(批 20 注册的倒影)、裸数组行从「chat_queue 冻结于基线」(已被批 21 证伪)改写为「八族全部已迁 + 三种解包契约(|| [] / null 保持 / 调用方自解)」、§5 表补 21 个批 parity 套件行。幽灵的 §4 裸数组退休路径改写在先(增益收编),我补其三处未跟进的漂移。
- **全天总账:** 272 站点 33 文件 → 47 站点 1 文件;新原语 api_payload;裸数组判定树四态封版;「+ok 仅对按名读消费方安全」与「形状迁移连 api_meta/注释/契约文档一起迁」两条新判据入档;幽灵共编 3 次(增益×2、削弱×1,全部逐行复核后收编或并集加固)。

### 2026-08-01(Epic-E sub-9 收尾:HEAD 自洽四修 + 6 防御 stub——共享树「先验后修」判例第五起;生产 core 1,274,221 B) — epic `pt_3879f00e`;commit `05da24cd`(10 文件);环 **94/94**;NEUTER×2 精确;runbook **42/42 ALL GREEN**(农场/生产同 hash `b9c7fb1f`)

- **定性:联合批的「悬空半边」由本会话补齐。** sub-9 主体(9a50c00f)落地时,生产重建携带了树内四处未提交修复通过 runbook 42 项——干净 HEAD 却缺它们:①**`_esc` 提级**(普查抓出的隐藏耦合:memory.js 的顶层 `_esc` 是全仓唯一顶层定义,artifacts 9/compaction-viewer 36/toast 3/log-clean 8/streaming_ui 2/translation_render 4 共 ~62 调用点经 window 解析到它;降级即首渲染 ReferenceError)——提级 core/escape_html.js(位置 8,先于全部消费方)+ 删副本;②**LoadGuard 摘 'toggleMemory'**(deferred 入口被 `_notReady` 预装 ⇒ `_installFeatureStub` 的 `typeof window[name]==='function'` 跳过 ⇒ 懒 stub 永不安装,工具栏记忆钮永久死态;兄弟背书「core 函数才进 LoadGuard」,sub-6 openDailyReport 是同坑遗留,按 owner 惯例单独立票不夹修);③main.js `_applyMemoryUI` 对 `_updateMemoryModalBtn` 裸调装闸(boot 工具态路径);④+6 防御 stub(静态面板 onclick 全覆盖:closeUpdateModal/_skillsSetScope/_skillsFilter/openMemoryCreateForm/refreshPreferences/savePreferences,image-gen 先例)。
- **分工新形态(第三次共享树共舞,首次零冲突):** 本批执行者身份不可见(peer_status 空窗),本会话以「普查→四套套件 failing-first(22 红)→陷阱修补→按落地形态校准套件→NEUTER×2→农场→runbook→ledger→commit」走完收尾闭环;兄弟会话(mrxinirv)在飞期间三条 HANDOFF 全被实证采纳(`tofu:feature-bundle-loaded` 补派发 24e3273a / LoadGuard 坑成立 / _esc 提级先行)。教训记账:**套件先行比实现先行更稳——failing-first 的红就是落地形态的验收尺。**
- **进度账:** core 1,274,221 B(基线 1,550,424,累计 −276KB 压缩态),距 1.2MB 目标 **~45KB**;累计已降级源码 720KB。下一片(ledger 已排队):settings/ 子包 508KB 普查(sub-10,单片即可合缺口并 complete epic)。

- **解锁前提(实证):** push 双 epic(pt_afbaf3d7 ①②③)已以 `959fd1c9`/`3d51d9a1` 落定,common.py/push.py/chat_queue.py 当前零兄弟 WIP——批 2 时通知过的协调纪律收到完整闭环。
- **判点三则:** ①push.py 三个 debug-presence dict 本带 ok:True ⇒ 逐字节等价;②**批 15 判据第二次救命**:common.py 四个 dispatch 聚合端点(quota/endpoint-metrics/model-health/key-stats)先验证键控地图全部**嵌在命名外层键下**(`{models:{…}}`/`{endpoints:{…}}`/`{providers:{…}}`)才转 api_ok——顶层 +ok 不会造出假 model/provider 卡片,parity 套件把这四个形状原样钉住;③chat_queue 裸数组(queue GET + 降级空队列)走 `|| []` 协调,queueClear 的 `{cleared}` 平凡转。`_db_safe` 的 503 字面量 → api_error('database_busy', status=503, message=…, retryAfter=2)。
- **纪律:** failing-first 精确 2 红;NEUTER×2(还原 queue 包装→shipped-source;摘 seam 解包→coordination);cmp 还原;node --check;三文件导入冒烟;环 **159/159**。幽灵连续第十六批零干预。
- **进度账:** 272→**47 站点 1 文件**(225 已清零,82.7%)。**基线只剩 paper.py——它同时是拆分路线图上 3944 行的待拆单体,迁移与拆分必须排一次序**(先拆后迁,还是迁完再拆)。

### 2026-08-01(Epic-E sub-9:settings 面板族六件 123KB 整批降级——生产 core 1,273,895 B,距 1.2MB 仅剩 ~45KB) — epic `pt_3879f00e`;主体 commit `9a50c00f`(7 文件);**update.js+runbook 两件被 mrxinirv 的 049b51d6 先行扫入**(共享树 commit 清扫又一次,内容零失真,归属记账于此);套件 **11/11**,环 **77/77**,runbook **42 项 ALL GREEN**

- **普查定案(六件全用户触发):** update 48.7K / skills 21.5K / memory 16.9K / optimizer 13.9K / timer 13.6K / preferences 8.1K——badge/设置 tab/移动 sheet 三条入口,**boot 路径零裸调**(settings/core_panel 对三个 tab populate 早已 typeof 闸)。
- **boot 接线三修(同批夹带,缺一即降级即事故):** ①update.js 版本检查挂 `window 'load'`——**deferral 后 bundle 晚于 load 落地,监听器永不触发,检查静默消失**;改挂 `_onReady`(feature-loader,core)。②timer/optimizer 自举(top-level `_startTimerPolling`/`_startOptimizerPolling`/badge bind IIFE)实测晚载安全(readyState 分支直燃,随包落地即起,idle prefetch ~2s 内自愈)。③**mobile_panels 包覆捕获灾难**:它 load 时按值捕获 `window.toggleTimerPanel`——deferral 后捕到的是 stub,真函数落地时**clobber 掉它的移动底单包覆**(移动端面板永久退回 display:none 徽标内=不可见)。修:包覆改**可重跑+恒等追踪**(`_capturedImpl`/`_installedWrap` 双表),feature-loader 落地时派发 `tofu:feature-bundle-loaded`(mrxinirv 看到共享树上我未提交的包覆重构,主动在 sub-8 提交里配套了派发——跨会话结对);pre-land 移动点击 kick 加载+落地回填,永不死端。
- **gate+stub 组合(本片新图案):** settings tab 三 populate(`_populateSkillsTab`/`_populatePreferencesTab`/`_renderSettingsUpdatePill`)装 stub 后,core_panel 的 typeof 闸**从「静默跳过空 tab」变「经 stub 加载并派发」**——闸不再只是防摔,还是触发器。11 stub py+js 双表(memory 模态双键 defense-in-depth)。
- **纪律:** failing-first 精确 5 红(manifest×2+stub 双表×2+land 事件);NEUTER×2(回注 timer.js→out_of_core 精确 1 红;摘 toggleTimerPanel→loader 表精确 1 红,均 cmp 字节还原);sub-7 位置锚 harness 随批重锚(memory.js→memory_skill_install.js,我的切片打断的锚我自己修);农场 16/16;生产实测 core `bundle-cab4b778.js` 1,273,895 / feature 782,236。**测量纠缠诚实账:sub-8 行(1,273,849)实测时本批 manifest 已在共享树,两批同形态差 46B;六件独立净额 ≈64KB 压缩态。**
- **协作意外记一笔:** 批 20 兄弟(api-contract)与我同刻提交,我的 `git add`+`git commit -- <paths>` 之间 update.js/runbook 被 049b51d6 的提交机器扫入(它的 stat 含两文件 69 行 diff)——**快照/回报会撒谎,runtime 验证(git show HEAD 逐文件复核)才是事实源**(第五次同教训)。
- **下一片即收官:** settings/ 子包 20 件 508KB,mrxinirv 普查定案「纯面板族、依赖单向、可整族降级」(section_requires load-time IIFE/local_endpoints setInterval 晚到自起均安全)——单片 ~45KB 缺口合线后 complete epic。

### 2026-08-01(api-contract 批 20:小文件清扫 15 站点 8 文件清零 + desktop.py 桥协议正确入册——剩余只有四座大山) — epic `pt_931e16c4` 切片 20;commit 见下(11 文件);环 **156/156**;NEUTER×2 各咬一支

- **清扫账:** conv_search 3(裸数组,`|| []` 契约直用)+ swarm 3(api_meta「UI 裸 SDK 包」描述同步诚实化——批 19 规则第二次应用)+ audio 1 + translate 1 + endpoint 2(两处「bare shape」注释同步)+ conv_compaction 2 + chat_poll_abort 2 + _task_routes 1(通用工厂 `jsonify(resp), status_code` → api_payload,runtime.poll 的 canonical 形状逐字节保)。
- **desktop.py 的正确归宿(分类纠偏):** 初扫把它算进可迁 16 站点,读文件实证 `/api/desktop/poll` 是**桌面代理桥协议**(外部客户端解析 `{'commands':…}`,与 browser.py 同族)——不是债务是豁免:从 BASELINE 移入 CARVE_OUT_FILES 附理由,`test_carve_out_registry_valid` 持续盯守。**基线表从此只剩真债务。**
- **纪律:** failing-first 精确 2 红;NEUTER×2(还原 conv_search 包装→shipped-source;摘 seam 解包→coordination);cmp 还原;node --check;8 文件导入冒烟全过;环 **156/156**。幽灵连续第十五批零干预。
- **进度账:** 272→**67 站点 5 文件**(205 已清零,75.4%)。**只剩四座山:** paper.py 47(待拆分路线图)、common.py 14(兄弟 WIP)、chat_queue.py 3(裸数组族,chat_queue_get 的 `[]` 需消费方分析)、push.py 3(兄弟 WIP)。全部有主或有判例,收尾路线清晰。

### 2026-08-01(自更新二轮根修:导出镜像清理漏删排除项根修 + apply 终态服务端持久化,刷新页面照样收「下载完成是否重启」) — owner 复核第一轮后两指令(「不要只报告,要修」+ 刷新失效是主场景缺口);epics `pt_d5828b575b404bac` + `pt_3cca755141404a66` DONE;commit `9bf9592b`(10 文件 +849/−49);新套件 7+9+12 检查全绿(各含 NEUTER);全邻接环 18 套件 **95 过 1 跳**

- **导出漏删根因(owner 验证时挖出,比我第一轮的诊断更深一层):** `promo` 早在 2026-06-10 就进了 `ALWAYS_EXCLUDE_DIRS`,可 26MB 至今还在 GitHub——`export.py` 的目的地清理把「导出排除目录」当「保留目录」跳过删除(本意是给活体安装 dest 省 FUSE I/O),排除内容一旦落进发布副本就随每次 `git add -A` 永生。修:`_dest_cleanup_targets` 按模式分流——personal/internal(活体安装)维持旧保留语义;**opensource(发布镜像)只保留 `_OPENSOURCE_DEST_PRESERVE`(.git/data/uploads/.tofu/pgdata/logs 等算子数据),排除目录与源已删条目一律删**,发布树从此=导出集。附带:`static/images/` 8 个零引用营销资产(~12.4MB,运行时图标走 static/icons/)进 `OPENSOURCE_EXTRA_EXCLUDE_FILES`。下次发布后 tarball 56MB→~30MB。
- **刷新失效根修:** apply 结果曾只活在 push 帧+内存 JS——下载 5-15 分钟用户必切走/刷新,刷新后没人问重启,`/update/check` 还会诱导再下一次 56MB。worker 现落 `update_apply_state.json`(running→done/failed);`/update/check` 投影 `pending_restart`(落地版≠运行版,重启后自清)与 `apply_in_progress`(仅线程实证存活;陈旧 running 标记一次性改写 interrupted)。前端 boot check 裸加载即弹重启 toast(每版本一次)+ 弹窗直渲重启卡 + 在途下载重连 push 订阅(done 帧照样弹)。附带清障:worker 终态写入先于注册表摘除(防并发 check 把 done 误判 interrupted)。
- **harness 漂移顺手修(兄弟 Epic-E sub-9 把 boot 块换成 `_onReady`,4 个 update 老 harness 载入即崩):** 各补 stub。教训复用既有判例:共享树上批跑失败先怀疑「脚下的文件在动」,再怀疑代码。
- **测试:** test_export_dest_cleanup.py(7:镜像删排除/保运行态/旧语义对照/force_strip/接线针+图片排除双模式)、test_update_pending_restart.py(9:投影四态+路由级+NEUTER 绕过)、test_frontend_update_pending_restart.py(12 检查:boot toast/一次一版/重启卡/重连订阅/NEUTER)。

