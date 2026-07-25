# Tofu 动画视频生成能力设计稿(auto-motion 吸收 + 超越)

> 状态:**P0 环境验证已完成(2026-07-25,全链跑通,见 §5);P1 起待 owner 拍板(§8)。**
> 参考仓库:`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/auto-motion`
> (与 tofu 同级目录,2026-07-25 clone 自 https://github.com/vibe-motion/auto-motion,49MB)

---

## 1. 背景与目标

`vibe-motion/auto-motion` 是一个「字幕稿 → 多段 MG 动画镜头 → 竖屏视频」的工作流模板:
用户给一个 `transcription.srt`,它拆镜头、逐镜头生成 MG 动画 MP4、FFmpeg 拼接出 `final.mp4`。

目标:**让 tofu 原生具备同类能力,并在七个维度上超越它**(见 §4)。
原仓库最有价值的资产不是它的流程,而是它捆绑的 **HyperFrames 技术底座 + 动效知识库**——
这两样都可以被 tofu 直接吸收(§3)。

---

## 2. auto-motion 机制拆解

### 2.1 流水线(它怎么跑)

```
transcription.srt
  → Codex CLI 读 PROMPT.md,按语义粗粒度分镜(首尾相接、完整覆盖时间轴,空白并入前镜头)
  → 每镜头建 scenes/scene-NNN/(复制 exampleFolder:.claude/skills + run-claude-ai.sh + 完整字幕)
  → 串行调用 Claude Code(`claude -p`,非交互,stream-json 日志,jq 过滤 [[USER_MESSAGE]] 阶段汇报)
  → 每镜头产出 1080x1440 / 30fps / 静音 MP4(HyperFrames 渲染)
  → FFmpeg 拼接 → final.mp4
```

### 2.2 真正的技术底座:HyperFrames(npm 包,已验证 v0.7.71 可装)

**HTML → 视频**。一个 composition 是一个 HTML 文件:

- DOM 用 `data-*` 属性声明时序(`data-composition-id` / `data-duration` / `data-track-index` / clip);
- 动画运行时 = **一条 `gsap.timeline({paused:true})`**,同步建在 `window.__timelines["<id>"]`;
- **确定性渲染铁律**:禁 `Date.now()` / 未播种 `Math.random` / 渲染期网络请求;禁 `repeat:-1`;
  只动视觉属性白名单——任意帧可独立 seek 计算,逐帧无头 Chrome 截图合成;
- CLI 开发环:`npx hyperframes init / lint / validate / inspect / snapshot / preview / render`
  (render 支持 `--quality draft|high`、`--docker`、`--strict`,还有 AWS Lambda 云渲染);
- 渲染要求:Node ≥ 22 + FFmpeg + 无头 Chromium。

### 2.3 知识库资产(6 个 Anthropic AgentSkills 格式技能包)

| 技能包 | 内容 | 对 tofu 的价值 |
|---|---|---|
| `hyperframes` | 意图路由(7 种视频工作流分流) | 直接吸收 |
| `hyperframes/core` | composition 契约:data-\* 属性、轨道、子组合、确定性规则 | 直接吸收 |
| `hyperframes/cli` | CLI 开发环 + 验收闸(lint/validate/inspect/snapshot) | 直接吸收 |
| `hyperframes-motion` | **29 条原子动效规则 + 13 个多相位蓝图 + 可运行示例 HTML** + 12 个运行时适配器(GSAP/Lottie/Three/WAAPI/AnimeJS…) | 核心资产 |
| `hyperframes-design` | ~20+ 帧预设(biennale-yellow / claude / cobalt-grid…)+ 调色板 + 排版 | 核心资产 |
| `motion-graphics` | Director→Builder→Finalize 子代理流水线,9 大品类(kinetic-type/stat/charts/logo-reveal/lower-thirds/webpage/news/tweet/asset-fusion),`shot-plan.json` IR,断点续跑表 | 流程范式参考 |

**关键洞察:这些技能包就是 tofu `lib/skills` 系统原生支持的 Anthropic AgentSkills 格式**——
可以作为 tofu 技能包直接安装,不需要翻译。

### 2.4 它的固有弱点(= tofu 的超越空间)

1. **依赖两个外部 agent CLI**(Codex + Claude Code)串联,每层都要登录/付费,链路脆弱;
2. **串行渲染镜头**,N 个镜头 N 倍时间;
3. **纯静音**——字幕稿本身就是配音文本,却不发声;
4. **固定 1080x1440 竖屏**,无参数化;
5. **无预览、无单镜头重生成、无人工干预点**,一个镜头翻车整条重跑;
6. **失败恢复手工**(看日志猜原因);
7. **image-gen 绑死 MiniMax 一家**。

---

## 3. tofu 吸收方案

### 3.1 本机环境已实测(2026-07-25,本设计稿落笔前全部亲手验证)

| 依赖 | 状态 | 说明 |
|---|---|---|
| Node ≥ 22 | ✅ v24.13.0 | tofu conda env 自带 |
| 无头 Chromium | ✅ 可跑 | Playwright chromium-1223;缺 12 个系统库,但 `LD_LIBRARY_PATH=<tofu env>/lib` 已补齐(实测 `Google Chrome for Testing 148.0.7778.96`,与既有记忆 `playwright-chromium-rootless-conda-libs` 同源) |
| FFmpeg | ⚠️ 需补 | 系统无;Playwright 自带 n7.0.1(裁剪版,拼接够用、x264 编码不稳)。**方案:`pip install imageio-ffmpeg`(静态全量二进制,免 root)** |
| hyperframes CLI | ✅ 可装 | `npx hyperframes@0.7.71`,npm 可达 |
| jq | ✅ | 系统自带 |

### 3.2 与 tofu 现有子系统的契合点

- **技能系统**(`lib/skills`,Anthropic AgentSkills 格式):hyperframes 六包直接做成 tofu 技能包(zip → installer),模型通道只读 `activate_skill`,用户可在 Settings → Skills 开关;
- **TTS**(`lib/tts`,播客链刚落地):同一字幕稿 = 分镜依据 + 配音文本,**SRT 时间轴 = 镜头时间轴 = 配音时间轴,音画天然对齐**;
- **任务/运行时**(播客 `podcast_runtime` 范式):视频生成注册为新的 TaskRuntime,dedup、事件流、abort 全套复用;
- **浏览器子系统**(`lib/browser` playwright 池):网页捕捉类镜头(webpage/news/tweet 品类)直接复用;
- **图像生成**(`lib/tools/image_gen.py` + server_config 多提供商槽位):替换绑死的 MiniMax;
- **artifacts 面板**:成品 MP4 进 artifacts,版本化;
- **push/SSE**:逐镜头进度实时推送;
- **endpoint 模式**(Planner→Worker→Critic):Planner 分镜、Worker 单镜、Critic 验收(lint/inspect 零 LLM 闸 + snapshot 抽帧目检)。

---

## 4. 超越点设计(对照 §2.4)

| # | 超越点 | 机制 |
|---|---|---|
| 1 | **零外部 agent 依赖** | tofu 自己就是 agent:分镜/写 composition/调 CLI 全在进程内,无 Codex/Claude Code |
| 2 | **音画合成** | TTS 配音 + 镜头静音视频 → FFmpeg mux;字幕轴对齐双时间轴;可选 BGM 轨(音量 ducking) |
| 3 | **并行渲染** | 镜头间无依赖,渲染是 CPU 密集 → 限流并行(worker 池,默认 2 路,可配);保留串行开关 |
| 4 | **画幅参数化** | 9:16(1080x1440/1080x1920)/ 16:9(1920x1080)/ 1:1;帧率 24/30/60 |
| 5 | **单镜头重生成** | 每镜头独立 composition + 独立 MP4,支持「重跑 scene-003 不换其他」;snapshot 抽帧预览先行,不满意不渲染 |
| 6 | **结构化失败诊断** | 渲染器退出码 + hyperframes lint/validate/inspect 输出解析成失败类目(契约错误/运行时错误/布局溢出/超时),自动带反馈重试一次(播客剧本闸同款范式) |
| 7 | **多提供商素材** | 图像走 server_config 槽位(gemini-image 等),搜索素材走 tofu_search,品牌 logo 走 web_search+SVG 下载(保留原仓库的专业 logo 规则) |

**额外甜点**:播客 → 视频的自然升级路径——`paper → report → 播客剧本 → TTS 字幕轴 → 视频`,
论文视频摘要(video abstract)是播客功能的直系延伸,P3 再做。

---

## 5. 分层实施计划

### P0 — 环境验证(纯验证,不碰 tofu 代码)—— ✅ 已完成(2026-07-25,brain 自主派发)

**结论:全链跑通,环境可行。** 工作区 `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/motion_p0/`(临时,不入库)。

**环境配方(全部实测)**:

| 组件 | 解法 | 验证结果 |
|---|---|---|
| hyperframes | `npm init -y` 后 `npm install hyperframes@0.7.71 --ignore-scripts` 本地安装 | doctor 必需项全绿 |
| ffmpeg | `pip install --target=<scratch> imageio-ffmpeg`(静态 7.0.2,含 libx264/aac/libmp3lame/png) | ✅ 渲染/拼接/混流全够用 |
| ffprobe | 复用 `miniforge3/envs/sglang/bin/ffprobe`(7.1.1),包 wrapper 注入 `LD_LIBRARY_PATH=<tofu>/lib:<sglang>/lib` | ✅ |
| Chrome | `HYPERFRAMES_BROWSER_PATH=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome` + `LD_LIBRARY_PATH=<tofu>/lib` | ✅ Chrome for Testing 148 |
| GSAP CDN | jsdelivr 直连可达 | ✅(离线部署时可 npm 本地化) |

**踩坑记录(P1 集成时必须绕开)**:

1. `npx hyperframes` 在本机每次都重装(cache 不持久)→ 必须本地 `npm install` 后用 `node_modules/.bin/hyperframes`;
2. 无 `package.json` 的目录里 `npm install` 会**向上冒泡**到 `ruanjunhao04/package.json`,触发 `onnxruntime-node` postinstall 访问 `api.nuget.org`(DNS 被封)→ 必须先 `npm init -y` + `--ignore-scripts`(onnxruntime 只影响 transcribe/remove-background,渲染主链不需要);
3. conda env 自带的 ffprobe 被其 env 里 stray libopenvino 的 RPATH 拖死(`GLIBCXX_3.4.26` 缺失)→ wrapper 前置 tofu env 的 `libstdc++.so.6.0.34`(含 3.4.26)解决。

**耗时基线(58 核 / 200GB,渲染器自适应 4 workers,采集为瓶颈,质量档只影响码率)**:

| 用例 | 规格 | 渲染耗时 | 倍速 |
|---|---|---|---|
| 最小 composition(自写 2 clips) | 4s / 1080x1440 / 30fps | draft 15.2s · standard 15.5s · high 14.3s | ~3.6× 实时 |
| 官方蓝图 brand-reveal(5 相位编排) | 5s / 1920x1080 / 30fps | standard 15.3s | ~3.1× 实时 |

**质量验证**:ffprobe 复核 h264/分辨率/帧率/时长/无音轨全部符合;抽帧目检通过;**CJK 文字渲染正常**(无豆腐块);官方蓝图示例在本环境原样跑通(唯一 lint 报错是示例自身缺 `@font-face` 的排版告警,且 lint 的 fixHint 直接给出修复方法——利好 P1 自动修复环设计)。

### P1 — 最小链路:技能包 + 单命令生成(静音乐片)

- 把 6 个 hyperframes 技能包做成 tofu 技能包(`.tofu/skills/motion-video-*`,走 installer 校验);
- 新增 `lib/motion_video/`(镜像 `lib/paper/` 播客链结构):
  - `_storyboard.py`:SRT 解析 + 语义分镜(LLM,输出 scenes.json:编号/起止/文案/时长,和校验 Σ=总时长±0.1s);
  - `_render.py`:hyperframes CLI 子进程封装(env 注入 LD_LIBRARY_PATH/FFmpeg 路径,退出码+日志落盘,超时与中 AbortSignal);
  - `_concat.py`:FFmpeg 规格归一 + 拼接(规格不一致先转码,原仓库同款规则);
  - `engine.py`:任务流编排(分镜→逐镜生成 composition(模型写 HTML)→lint/inspect 闸→render→拼接),TaskRuntime 注册;
- 聊天入口:用户贴 SRT 或给主题(主题→先写口播稿→TTS 出轴→SRT),一条消息出 `final.mp4` 进 artifacts;
- **验收闸零 LLM**:scenes.json 结构/时长和校验 + hyperframes lint/validate/inspect + ffprobe 规格复核(分辨率/帧率/时长/无音轨)。

### P2 — 音画合成

- TTS 逐镜头配音(复用 `lib/tts` 分块/拼接/停顿逻辑),镜头音频长度 vs 镜头时长对齐策略(音频短→补静音尾;音频长→自动微调镜头时长重渲或拉伸留白,二选一,拍板时定);
- FFmpeg mux 音轨;可选字幕烧录(burn-in)或侧车 .srt;
- 降噪:loudnorm 归一(播客链已有同款)。

### P3 — 交互与并行

- 前端视频面板(或 artifacts 增强):逐镜头卡片、snapshot 抽帧九宫格预览、单镜头重生成按钮、进度条;
- 并行渲染 worker 池(限流默认 2);BGM 轨 + ducking;画幅/帧率/风格预设(直接消费 hyperframes-design 帧预设);
- 论文 video abstract(播客链延伸)。

---

## 6. 风险与开放问题

| 风险/问题 | 说明 | 缓解 |
|---|---|---|
| 渲染耗时 | ~~无头 Chrome 逐帧 seek,10s/30fps=300 帧;单镜估计 1–5 分钟~~ **P0 实测:~3.1–3.6× 实时**(4s 镜 ~15s,10s 镜 ~35s,60s 片 ~3.5 分钟串行) | 实测已可接受;`--workers` 可调(58 核只自适应用了 4);P3 并行池进一步压缩 |
| 长视频上下文 | 10+ 镜头时每镜头都要写一份 composition HTML,模型逐镜生成 token 量大 | 每镜独立子任务(只带该镜文案+蓝图),与 auto-motion 每镜独立 Claude Code 调用同构 |
| 模型写 composition 的稳定性 | 契约细节多(template 包裹/id 唯一/根尺寸),新手 composition 易踩静默坑 | 技能包内置 minimal skeleton + 常见坑清单;lint/inspect 闸 + 带反馈自动修复一次 |
| 音画对齐策略(P2) | 配音自然语速 ≠ 字幕轴严格时长 | 拍板时定:严格对轴(调语速/停顿) vs 宽松对轴(镜头随音频长短微调) |
| HyperFrames 许可证 | npm 包与技能包内容的再分发条款待确认 | P1 前查 LICENSE;技能包只内部使用则风险低 |
| ffmpeg 选型 | ~~imageio-ffmpeg 静态版 vs conda 版~~ **P0 已定:imageio-ffmpeg(静态 7.0.2,x264/aac/mp3/png 全有)**;ffprobe 用 sglang env + wrapper | 无残留风险 |

---

## 7. 测试与验收(对齐播客链范式)

- **P0**:环境探测脚本(hyperframes doctor + ffmpeg -encoders + chromium --version)进 `debug/`;
- **P1**:`tests/test_motion_video_*.py`——SRT 解析/分镜校验(含 NEUTER:剥时长和校验则放行错位 scenes.json)、
  render 封装(假 CLI 验证 env 注入/退出码/AbortSignal)、concat 规格归一、e2e(1 个 2s 镜头全链,真渲染,标记 slow);
- **P2**:音画对齐 e2e(假 TTS 输出定长 WAV,验证 mux 后时长/音轨存在);
- **P3**:前端 JSDOM 探针(镜头卡片/重生成按钮接线),并行池限流测试;
- 全程遵守 CLAUDE.md:日志纪律(渲染全过程落 logs)、`--collect-only` 闸、README 双语同步。

---

## 8. 拍板请求

请 owner 定夺:

1. **总体方向**:按 P0→P3 推进?(P0 纯验证零风险,可先跑)
2. **P2 音画对齐策略**:严格对轴 vs 宽松对轴(§6 第 4 行)
3. **技能包落地形式**:6 包原样装进 tofu skills(推荐,改动最小) vs 合并成 1 个「motion-video」大包
4. **入口形态**:P1 先做聊天命令式(贴 SRT 出片),UI 面板 P3 再做 —— 同意?
