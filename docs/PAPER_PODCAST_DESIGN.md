# Paper Podcast — 论文播客功能设计

> 状态:**设计稿**(2026-07-25),尚未实施。
> 一句话:把论文阅读模式的「报告」变成一档**单人有声精读课** —— 通勤路上、睡前听一两遍,
> 听完能复述出这篇论文解决了什么、怎么做的、凭什么可信。

---

## 1. 设计目标与三条质量红线

播客不是把报告念一遍。听众(眼睛不在屏幕上)拿到价值的判据:

| 红线 | 反例(不许出现) | 本设计的对策 |
|---|---|---|
| **公式可听化** | "E 等于 frac{1}{2} m v 平方" (念 LaTeX) | 三级处理(§3.2):核心公式讲直觉,次要公式只讲作用,推导跳过 |
| **图表可听化** | 跳过 Figure 3,或说"如图所示" | Top-K 图三段式口播(§3.3):是什么 → 你会看到什么 → 为什么重要 |
| **拒绝空话** | "这篇论文非常重要,效果提升明显" | 数字溯源校验 + 结构硬性要件 + Critic 复审(§3.5) |

类比:**报告是素材库,口播稿是剧本,TTS 是配音演员**。剧本写得差,配音再好也救不回来
—— 所以本设计 80% 的复杂度在「剧本生成与校验」,TTS 只是最后一道工序。

## 2. 总体流水线

```
论文 PDF
  └─(已有) 解析 → parsed_text / 翻译 (paper_translations)
  └─(已有) 报告引擎 → 结构化报告 (paper_reports, 10 节, 已校验/已插图/已术语审计)
        │
        ▼  Stage A: 剧本生成 (LLM, 新增 podcast_engine)
  script.json —— 分段结构化口播稿 [{id, section, speaker, text, est_seconds, figure_ref?}]
        │  ← 剧本校验器: 残留公式检测 / 数字溯源 / 时长估算 / 结构要件
        │  ← Critic 复审(可选): 对照报告逐条核事实, 不合格自动重写一次
        ▼  Stage B: 语音合成 (新增 lib/tts, OpenAI 兼容 POST /audio/speech)
  逐段合成(可并行/可断点重试) → 段间停顿时长注入 → 拼接 → 响度归一 → MP3
        │
        ▼  Stage C: 服务与前端
  /api/v1/paper/podcast/audio/<hash>/<mode> (HTTP Range, 可拖进度条)
  paper-reader 右侧新增 "Podcast" tab: 播放器 + 逐字稿(点击跳转) + 下载按钮
```

**报告是必需输入。** 用户点「生成播客」时若该论文还没有报告,podcast 任务自动先链式触发
报告生成(复用 report_runtime,同一任务内推进进度),报告落库后再进入 Stage A —— 一键完成,
用户不需要理解依赖关系。翻译不是必需输入(报告已含方法/实验的提炼);仅当剧本需要核对
某个数字/段落原文时,Stage A 的 prompt 才附带相关翻译节选。

## 3. 剧本(口播稿)设计 —— 本功能的核心

### 3.1 输出格式:结构化 JSON,不是一整篇 Markdown

```json
{
  "title": "...", "lang": "zh", "mode": "short",
  "segments": [
    {"id": 0, "section": "cold_open", "speaker": "host",
     "text": "今天这篇论文解决的是一个很具体的问题……",
     "est_seconds": 40, "figure_ref": null},
    {"id": 4, "section": "method", "speaker": "host",
     "text": " Figure 3 是整篇论文最关键的一张图……",
     "est_seconds": 95, "figure_ref": "fig_03_p5.png"}
  ]
}
```

为什么必须是 JSON 分段(而不是整篇文本):
1. **TTS 分段合成** —— 单段失败只重试该段,多段可并行;
2. **逐字稿点击跳转** —— 前端按 segment 对齐音频进度;
3. **配图联动(P2)** —— `figure_ref` 让播放时屏幕同步显示正在讲的那张图;
4. **双人对话(P2)向前兼容** —— `speaker` 字段预留,P1 恒为 `host`。

### 3.2 公式三级处理(红线一)

| 级别 | 判定 | 口播策略 | 示例 |
|---|---|---|---|
| L1 核心公式 (≤3 个) | 报告 Method/Technical Reference 中被反复引用 | **只讲直觉与每一项的物理含义**,禁止念符号 | "这个损失本质上是在奖励模型把相似的样本拉近、不相似的推远" |
| L2 次要公式 | 出现一次 | 只讲作用,不讲形式 | "他们用一个带温度系数的对比损失来控制分布的锐度" |
| L3 推导/伪代码 | 多行推导、算法块 | **跳过**,只讲结论 | "经过三页推导,他们最终把复杂度从平方降到了线性" |

报告的 `## 📝 Technical Reference`(公式+复现清单)整节**不进口播稿** —— 那是查阅材料,
不是收听材料;它在逐字稿 tab 里以文字形式保留给想查的人。

### 3.3 图表处理(红线二)

- **只讲 Top-K (K≤5) 图**:按报告正文中对图编号(Figure N)的讨论篇幅排序,讨论最多的优先
  (图片 manifest 有 caption + 页码,报告已插图并带讨论段,二者直接可得)。
- **每图三段式口播**:这是什么图 → 你睁开眼会看到什么(坐标轴/趋势/对比)→ 为什么它重要。
- **表格一律不读**,转成趋势性语言:"横向看三个数据集,方法在最大的那个上优势最明显"。
- 其余图在逐字稿 tab 里保留缩略,听众想补看随时能看。

### 3.4 术语、数字与语言规则

- **术语**:首次出现用「中文(English term)」格式,之后沿用中文;直接复用报告的
  `## 🔑 Core Terminology` 表 + terminology_audit 的结果保证全文一致。
- **数字**:只保留有判断力的精度("86.3%,比上一代高 3.2 个百分点",不写 "86.34 vs 83.12");
  每个关键数字必须能溯源到报告/翻译原文(§3.5 校验器强制执行)。
- **语言**:跟随报告语言(中文论文→中文播客,英文论文默认中文播客、可选英文)。
- **听感结构**:冷开场钩子(30 秒内说出具体问题+最亮的数字)→ 路线图("接下来三部分")
  → 正文的明显路标("说完了动机,我们来看方法")→ 结尾「三点带走」回顾。
  睡前收听场景要求语速平稳、无突然的情绪词,写进 prompt。

### 3.5 质量校验器(红线三,零 LLM + 一次 Critic)

剧本落库前过四道闸,任何一道不过则自动重修一次,再不过则标 `low_confidence` 并告知用户:

1. **残留公式检测**(正则):`$...$`、`\frac`、`\sum`、`^{`、_{` 等 LaTeX 痕迹 → 打回。
2. **数字溯源**:抽取剧本中全部数字,每个必须能在报告/翻译原文中找到(容差:允许四舍五入
   到 1 位小数);溯源失败 → 打回。(借鉴 `lib/paper/citation_audit.py` 的思路)
3. **结构要件**:cold_open 含具体问题+至少一个数字;每个正文 section 至少含一个具体锚点
   (数字/机制名/对比);结尾 recap 恰为 3 条。
4. **时长估算**:按 §4.3 的语速模型估算总时长,超出目标 ±20% → 压缩或扩写。

**Critic 复审(可选,默认开)**:另起一次 LLM 调用,把剧本+报告并排给模型,按清单核对
(有没有报告里没有的断言?有没有空话?图的描述和 caption 一致吗?),输出修订意见 →
剧本生成器带着意见重写一遍。这是 endpoint 模式 Planner→Worker→Critic 的简化版(一轮)。

### 3.6 时长档位(mode)

| 档位 | 目标时长 | 剧本长度(中文≈250 字/分) | 场景 |
|---|---|---|---|
| `short` | ~5 分钟 | 1,200–1,500 字 | 通勤一段路、决定要不要深读 |
| `full` | ~15 分钟 | 3,600–4,500 字 | 睡前完整精听 |

英文按 ~155 wpm 折算(short ≈ 750 词,full ≈ 2,300 词)。

## 4. Stage B: TTS 与音频工程

### 4.1 能力接入(照抄 transcription 的接线图,全项目目前无 TTS)

| 项 | 做法 | 落点 |
|---|---|---|
| 能力标签 | 新增 `'tts'` 进 `CHAT_EXCLUDED_CAPS`(自动进 `DISPATCHER_NON_CHAT_CAPS`)+ `CAPABILITY_SEMANTICS` 加 `endpoint:'audio_speech'` | `lib/model_info/capability_taxonomy.py:36,82` |
| 槽位配置 | 新增 `# ── Text-to-speech (tts) ──` 块,模型带 `caps:{'tts'}` | `lib/llm_dispatch/config/_slots.py`(仿 :233 的 transcription 块) |
| 合成模块 | 新建 `lib/tts/` 包(`_synthesize.py`/`_config.py`/`_audio.py`),槽位选择走 `disp.pick_and_reserve(capability='tts')` | 仿 `lib/transcription/` 全包结构 |
| 端点 | OpenAI 兼容 `POST {base}/audio/speech`,请求 `{model, input, voice, response_format:'wav', speed}` | 与 `/audio/transcriptions`、`/images/generations` 同族 |
| 探测 | `probe_tts_cell()` 最小输入合成→校验返回是音频字节 | `lib/provider_probe.py:248` 旁 |
| 计费 | TTS 按字符计价,pricing 表加模型条目 | `lib/pricing/_tables.py` |
| 前端排除 | `model_caps.js` 硬编码 fallback 集合加 `'tts'`(服务器 taxonomy 自动下发) | `static/js/core/model_caps.js:23` |

> ⚠️ **需要 owner 决策**:目前没有任何 TTS 模型注册。上线前必须在网关/服务商处确认一个
> 可用 TTS 模型(如 OpenAI tts-1-hd / 豆包语音合成等)并配槽位,否则功能无法真正发声。
> 这是本设计唯一的外部依赖。

### 4.2 合成与拼接

- **逐 segment 合成**,bounded 并发(≤3);单段失败独立重试,整篇不用从头来。
- 优先向 TTS 请求 **WAV**(便于无损拼接);拼接时段间注入静音:同节内 300ms、跨节 800ms
  (听感上的"翻页")。
- 拼接后用 **ffmpeg**(若 `shutil.which('ffmpeg')` 存在)做响度归一(`loudnorm`)+ 转
  MP3 128kbps 单声道(人声足够,体积约 1MB/分钟);无 ffmpeg 则落 WAV(体积大但通用),
  降级路径打 warning 日志。
- 语速不在合成时调(各 TTS 实现不一),前端 `<audio playbackRate>` 免费实现 0.75–1.5x。

### 4.3 时长估算模型

中文 250 字/分钟、英文 155 wpm 为基准;剧本 JSON 里每段预写 `est_seconds`,
实际合成后用真实音频时长回填(逐字稿跳转以真实值为准)。

## 5. 存储与数据模型

遵循「DB 存文本/元数据,磁盘存二进制」的现有惯例(报告/翻译在 DB,PDF/图在盘):

- **新表 `paper_podcasts`**(定义进 `lib/database/_core_schema/_tables.py`,与 `paper_reports`
  同风格):`(paper_hash, mode, lang, voice)` 联合主键;列:`status, duration_sec,
  file_path, script_json, meta, created_at, updated_at`。`script_json` 即 §3.1 的剧本。
- **音频文件**:`<PAPER_DIR>/podcast/<paper_hash>/<mode>_<lang>_<voice>.mp3`
  (`PAPER_DIR` 见 `lib/paper/hashing.py:25-32`)。
- 同一 `(paper_hash, mode, lang, voice)` 重复生成 → 直接命中缓存返回,与报告 dedup 一致。

## 6. 任务、API 与前端

### 6.1 任务运行时(复用报告模式)

新建 `lib/paper/podcast_runtime.py` + `lib/paper/podcast_engine/`(仿 `report_engine/` 的
`__init__`/`_hooks`/`_meta` 三分):TaskRuntime 后台线程,dedup 键
`(paper_hash, mode, lang, voice)`,轮询事件**复用报告的事件名**
(`status/delta/done/error/aborted`)外加两个:`segment_done {i, n}`(合成进度)、
`audio_ready {url, duration_sec}`。前端轮询逻辑与 reportPoll 几乎同构。

### 6.2 API(挂在 `/api/v1/paper/`,全部走 `Api.paper.*`)

| 端点 | 作用 |
|---|---|
| `POST /api/v1/paper/podcast/start` | 启动/去重;无报告时自动链式先生成报告 |
| `POST /api/v1/paper/podcast/poll` | 轮询进度(事件数组,同 report/poll) |
| `POST /api/v1/paper/podcast/abort` | 中止 |
| `GET  /api/v1/paper/podcast/lookup` | 查已有播客(命中缓存直接返回) |
| `GET  /api/v1/paper/podcast/audio/<paper_hash>/<mode>` | 音频流,**支持 HTTP Range**(仿 `routes/paper.py:2033` 的 PDF Range 服务),可拖进度条 |
| `GET  /api/v1/paper/podcast/script` | 剧本导出(Markdown),也可注册成 artifact 进 artifacts 面板 |

### 6.3 前端:paper-reader 右侧新增 "Podcast" tab

- 接入点:`_switchPaperTab` (`static/js/paper-reader.js:902-960`) 加第五个 tab;
  **图标用内联 SVG 耳机**(§3.4 禁 emoji/Unicode 符号)。
- Tab 内三件套:
  1. **生成卡片**:档位选择(short/full)+ 语言 + 音色 → 生成按钮;进行中显示
     「剧本撰写中 → 合成 3/12 段」进度;
  2. **播放器**:原生 `<audio>`(自带倍速/进度条),封面区显示论文标题+时长;
  3. **逐字稿**:按 segment 渲染,当前播放段高亮,点击任一段 `audio.currentTime` 跳转;
     Technical Reference 等"未播内容"折叠在稿末供查阅。
- **导出**:下载按钮直链 audio 端点(MP3 文件,手机可存本地/转发);剧本可单独导出 Markdown。
- API 一律走 `Api.paper.podcast*`(§3.2.0 统一客户端铁律);新 JS 文件记得进
  `_BUNDLE_FILES`(§3.2.1)。

## 7. 分期落地

| 期 | 内容 | 理由 |
|---|---|---|
| **P1 (MVP)** | 单人旁白;short/full 两档;中英双语;四条校验闸;MP3 导出;Podcast tab + 播放器 + 逐字稿跳转 | 闭环「路上/睡前听一遍」的核心价值 |
| **P2** | 双人对话模式(speaker 字段已预留;prompt 改为 A/B 轮流,两个音色);配图联动(figure_ref 已预留,播放时屏幕同步显示当前图);睡眠定时(停止于 N 分钟);ffmpeg 缺失时的响度补偿 | 提升听感,不挡主链路 |
| **P3** | 个人 RSS feed(每篇播客=一集,手机播客 App 订阅,真正的"路上听"终极形态);论文 playlist(多篇连播,接 recommend_engine 做「本周论文速递」日更播客) | 订阅化,把功能变成习惯 |

## 8. 测试计划(实施时的门禁)

- 剧本校验器单测:残留 LaTeX 检出、数字溯源(含四舍五入容差)、结构要件、时长 ±20%。
  各配 NEUTER(剥掉校验器 → 脏剧本能漏过)。
- Stage A/B 用 mock LLM + mock TTS(返回固定 WAV 头+静音字节)跑 e2e:
  从 podcast/start 到 audio_ready,断言 script.json 落库、MP3 落盘、Range 请求
  `206 Partial Content`。
- NEUTER 链路级:剥掉公式三级处理规则 → 剧本出现 LaTeX 残留 → 校验器必须报警。
- 收集门禁 `--collect-only` 0 err;遵守测试偏好(`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,
  `@pytest.mark.unit`)。

## 9. 待 owner 拍板的开放问题

1. **TTS 模型从哪来**(唯一硬阻塞):网关现有哪个语音合成模型可用?音色清单?
   没有可用槽位则功能只能交付到「剧本」层。
2. **双人对话是否提前到 P1**?听感更好但 prompt/校验/音色配置复杂度翻倍,本设计默认 P2。
3. **计费**:TTS 按字符计费(如 tts-1-hd ≈ $0.15/万字),是否需要每篇播客的成本气泡
   (复用现有 cost 体系)?
4. 默认音色与语速是否进 settings(全局)还是逐论文可选?

---

*设计依据:paper 后端(lib/paper/report_engine、translate_engine、images/、prompts.py)、
前端(paper-reader.js、api.js)、非聊天能力接线(lib/transcription、capability_taxonomy、
provider_probe)三路代码勘察,2026-07-25。*
