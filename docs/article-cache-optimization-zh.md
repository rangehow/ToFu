# 省 27% 的秘密：Claude API 缓存优化实战

![封面](../static/images/tofu-cache-article-cover-zh.png)

> **摘要** — 我们通过阅读 Claude Code 源码发现了 Anthropic Prompt Caching 的核心策略差异，设计了一套 5 组对照 × 4 类场景的 A/B 测试框架，最终找到了一个比 Claude Code 自己的方案更省钱的缓存策略：4 断点 + 混合 TTL，整体节省 27.2%。本文将从「什么是 Cache Breakpoint」讲起，完整介绍测试方法、场景设计和实验结论。

---

## 一、什么是 Cache Breakpoint？

### 1.1 先说痛点

如果你用 Claude API 构建多轮对话的 AI 助手，大概率遇到过这个问题：

```
第 1 轮: system prompt (14K tokens) + 用户消息 (100 tokens) = 14,100 tokens
第 2 轮: system prompt (14K tokens) + 历史消息 + 新消息         = 16,500 tokens
第 3 轮: system prompt (14K tokens) + 更多历史 + 新消息          = 19,200 tokens
...
第 30 轮:                                                      = 120,000 tokens
```

每一轮，你都在反复发送那 14K 的 system prompt 和工具定义。这部分内容**从头到尾没变过一个字**，但模型每次都要重新计算它的 KV attention state。这不仅慢（增加首 token 延迟），而且贵（按 input token 全价收费）。

30 轮下来，光是 system prompt 就重复发送了 `14K × 30 = 420K` tokens，按 Opus 的价格（$15/M input），这就是 **$6.3 的纯浪费**。

### 1.2 缓存断点的原理

Anthropic 的解决方案是 **Prompt Caching**：你在消息中插入 `cache_control` 标记（即"缓存断点"），告诉 API："从开头到这个标记之间的内容，请缓存它的 KV 计算结果。"

```json
{
  "role": "system",
  "content": "你是一个代码助手...(14K tokens)...",
  "cache_control": { "type": "ephemeral" }    ← 这就是一个 cache breakpoint
}
```

下次请求时，如果从开头到这个标记的内容**完全一致**（逐字节匹配），API 会直接从缓存读取 KV state，跳过重新计算。

**效果**：
- 🕐 **延迟**：缓存命中时，14K tokens 的 system prompt 处理时间从 ~2 秒降到 ~0.1 秒
- 💰 **成本**：缓存读取只收 **0.1×** 的 input 价格（打一折）

### 1.3 代价：写入不是免费的

天下没有免费的午餐。第一次写入缓存时，你要付额外费用：

| TTL（存活时间） | 写入倍率 | 读取倍率 | 过期后果 |
|---|---|---|---|
| 5 分钟（默认） | **1.25×** | 0.1× | 5 分钟无人访问就过期，下次要重写 |
| 1 小时（扩展） | **2.0×** | 0.1× | 1 小时内有效，适合长对话 |

这意味着：
- 如果你的缓存**每次都命中**，收益巨大（0.1× vs 1.0×）
- 如果你的缓存**频繁失效重写**，可能比不用缓存还贵（反复付 1.25× 或 2.0× 的写入费）
- **断点放在哪里**，直接决定缓存命中率

### 1.4 断点放在哪里？——这才是核心问题

一个典型的 Claude API 请求长这样：

```
┌─────────────────────────────────────────────────────┐
│ System Prompt (14K tokens)          ← 几乎不变       │
│ ─── BP1 ─── cache_control ──────────────────────────│
│ Tool Definitions (2K tokens)        ← 会话内不变     │
│ ─── BP2 ─── cache_control ──────────────────────────│
│ 历史消息: user→assistant→tool_result→...  ← 只增不减  │
│ ─── BP3 ─── cache_control ──────────────────────────│
│ 最新一条消息 (当前轮新增)            ← 每轮都变       │
│ ─── BP4 ─── cache_control ──────────────────────────│
└─────────────────────────────────────────────────────┘
```

**关键规则**：缓存是**从头到断点**的前缀匹配。如果中间任何一个字节变了，该断点之后的缓存全部失效。

所以：
- BP1 和 BP2 覆盖的内容（system prompt + tools）几乎不变，缓存命中率接近 100%
- BP3 覆盖所有历史消息，每轮新增内容，但旧内容不变——**只要断点放对，前缀不变，缓存就能命中**
- BP4 是当前轮的新消息，**永远不会命中缓存**（因为它每轮都是新的），但它的存在让 BP3 之前的内容都被缓存读取

这就是为什么断点位置如此重要——**差一个位置，可能导致整个 prefix 缓存失效**。

---

## 二、问题的发现：一笔 ¥198 的对话账单

故事要从一次异常昂贵的对话说起。一轮 64 round 的 Claude Opus 编程对话，token 统计显示：

```
Input（未缓存）:    66 tokens    → ¥0.00
Cache Write:    1,200,268 tokens → ¥162.94
Cache Read:     2,656,118 tokens → ¥28.85
Output:         12,300 tokens    → ¥6.66
──────────────────────────────────────────
Total:                             ¥198.44
```

两个数字让我们警觉：

**"Input: 66"** —— 64 轮对话，每轮只有 ~1 个 token 未缓存？这要么说明缓存完美到不可思议，要么说明我们的统计有 bug。

**¥162.94 的 Cache Write** —— 占总成本的 82%。缓存写入本应是一次性开销（第一轮写入，后续全部读取），不应该成为主要成本。一定是有什么东西在反复触发全量重写。

### 发现了三个 Bug

**Bug #1：prompt_tokens 语义不一致**

Anthropic API 返回的 `prompt_tokens` 含义是 **"未缓存的 input tokens"**，三个字段是**加法关系**：

```
total_input = prompt_tokens + cache_write_tokens + cache_read_tokens
```

但 OpenAI 兼容格式中，`prompt_tokens` = 总 input tokens（含缓存部分）。我们的前端按 OpenAI 语义解读，导致显示的 "Input: 66" 其实是未缓存部分，真实总输入是 `66 + 1,200,268 + 2,656,118 = 3,856,452` tokens。

**Bug #2：服务端缓存过期**

我们的缓存追踪系统发现，64 轮中有 **14 轮（22%）** 的 cache_read 突然从 50K-80K 骤降到 13,988 tokens（刚好是 system prompt 的大小）。这意味着对话历史部分的缓存被 5 分钟 TTL 过期驱逐了——当单轮执行时间超过 5 分钟时（工具调用 + 模型生成 + 用户思考），下一轮就要全部重写。

**Bug #3：断点位置偏差（已修复）**

早期版本的 BP4（对话尾部断点）放在了 `msg[-2]`（倒数第二条）而不是 `msg[-1]`（最后一条），导致最新的 tool_result 每次都以未缓存方式发送。修复后，未缓存 tokens 从 ~250/轮降到 ~1/轮——**240 倍的改善**。

---

## 三、假设：Claude Code 的做法更好吗？

在排查过程中，我们阅读了 Claude Code（Anthropic 官方 CLI 工具）的源码，发现它使用了**完全不同的缓存策略**：

### Claude Code 的方案：只放 1 个断点

```typescript
// Claude Code: addCacheBreakpoints()
// 只在最后一条消息上放 cache_control
const markerIndex = skipCacheWrite ? messages.length - 2 : messages.length - 1
```

他们的代码注释解释了原因：

> *Mycro（Anthropic 的 KV 缓存引擎）的驱逐机制会在每个 cache_control 标记位置保留 local-attention KV pages。放 2 个标记，第二个位置的 KV pages 白白占着不会被用，降低缓存效率。*

### 两种策略的对比

| 维度 | Claude Code（1 断点） | Tofu（4 断点） |
|---|---|---|
| 断点数量 | 1 个，在 msg[-1] | 4 个（system×2 + tools + tail） |
| TTL 策略 | 统一：全部 5m 或全部 1h | 混合：BP1-3 用 1h，BP4 用 5m |
| KV 页面效率 | 更高（单标记，无浪费） | 可能有 KV 页面浪费 |
| 缓存粒度 | 粗粒度（整个 prefix 一起） | 细粒度（system/tools/tail 分开） |

这产生了一个自然的问题：**到底谁的方案更省钱？**

理论分析无法给出答案——KV 页面效率和经济效率是两回事。唯一的办法是**实测**。

---

## 四、测试设计：5 组对照 × 4 类场景

我们构建了一个测试框架，对真实 Claude API 运行受控的多轮对话，精确记录每轮的 token 用量。

### 4.1 五组对照策略

| 编号 | 策略名称 | 断点数 | 断点位置 | TTL | 说明 |
|---|---|---|---|---|---|
| ① | **OLD** | 4 | BP4=msg[-2] | 5 min | 有 bug 的旧版（对照基线） |
| ② | **NEW** | 4 | BP4=msg[-1] | 5 min | 修复位置，标准 TTL |
| ③ | **NEW_1h** | 4 | BP4=msg[-1] | 混合 | BP1-3 用 1h，BP4 用 5m |
| ④ | **SINGLE** | 1 | msg[-1] | 5 min | Claude Code 的策略 |
| ⑤ | **SINGLE_1h** | 1 | msg[-1] | 1 hour | Claude Code + 扩展 TTL |

### 4.2 四类测试场景

每个场景模拟一种真实的 AI 编程助手使用模式。每个场景跑 12 轮。

#### 场景 A：单次提问 → 多轮工具调用

```
用户: "分析这个项目的错误处理"
 ├→ R1:  assistant 调用 read_files("lib/server.py")
 ├→ R2:  assistant 调用 grep_search("except")
 ├→ R3:  assistant 调用 list_dir("routes/")
 ├→ R4:  assistant 调用 read_files("routes/chat.py")
 ├→ ...
 └→ R12: assistant 输出最终分析报告
```

**考察点**：最常见的使用模式。用户提一个问题，模型连续调用多个工具。对话线性增长，每轮新增 1 个 assistant + 1 个 tool_result。测试缓存在**稳定增长的对话**中的表现。

#### 场景 B：多轮交互 + 穿插用户消息

```
用户: "搭建一个新的 API 端点"
 ├→ R1-R3: 工具调用（创建文件、写代码）
 │
 用户: "再加上输入校验"                   ← 第 4 轮插入新用户消息
 ├→ R4-R7: 更多工具调用
 │
 用户: "写一下测试"                       ← 第 8 轮插入新用户消息
 ├→ R8-R12: 更多工具调用
```

**考察点**：用户中途追加需求，插入新的 user message。测试当**消息位置发生偏移**时缓存是否稳定——新插入的用户消息会改变后续所有消息的相对位置。

#### 场景 C：并行工具调用（批量）

```
用户: "对比 config、routes、tests、docs 四个目录"
 ├→ R1:  assistant 一次性发出 4 个 tool_call
 │       → 返回 4 个 tool_result
 ├→ R2:  assistant 再发 4 个 tool_call
 │       → 返回 4 个 tool_result
 ├→ ...
 └→ R12: 最终对比报告
```

**考察点**：每轮的消息增量是 Scenario A 的 4 倍（4 个 tool_call + 4 个 tool_result）。测试在**大步长增长**下，缓存写入量和命中率的变化。Cache Write 占比应该显著高于 A 和 B。

#### 场景 D：混合内容 assistant（边界情况）

```
用户: "逐步重构这个模块"
 ├→ R1:  assistant: "我先读一下文件。" + tool_call    (有文本 + 工具调用)
 │       → tool_result
 ├→ R2:  assistant: "" + tool_call                     (空文本 + 工具调用)
 │       → tool_result
 ├→ R3:  assistant: "发现了一个问题..." + tool_call     (有文本 + 工具调用)
 │       → tool_result
 ├→ ...
```

**考察点**：这是最刁钻的场景。有些 assistant 消息的 `content` 是空字符串（模型直接调用工具，不说话），有些有实际文本。空 content 的 assistant 消息是早期 BP4 bug 的直接触发条件——断点扫描会跳过空消息，导致 BP4 放到了更早的位置。

### 4.3 测试协议

为了保证可比性：

1. **每个 arm × scenario 组合独立运行**，从零开始，无先前缓存状态
2. **统一使用 `aws.claude-opus-4.6`** 模型
3. **精确记录每轮**：`prompt_tokens`、`cache_write_tokens`、`cache_read_tokens`、`completion_tokens`
4. **所有策略使用完全相同的 system prompt 和 tool definitions**（确保确定性）
5. **工具结果使用模拟数据**（不实际执行 I/O），确保跨策略的可复现性
6. 成本计算使用 Opus 官方定价：input $15/M、cache write $18.75/M (5m) 或 $30/M (1h)、cache read $1.5/M、output $75/M

---

## 五、实验结果

### 5.1 总排名

| 排名 | 策略 | 总成本（4 场景合计） | 胜出场景数 | 相比最差策略的节省 |
|---|---|---|---|---|
| 🥇 | **NEW_1h**（4断点 + 混合TTL） | **$1.5242** | **2/4** | **-27.2%** |
| 🥈 | NEW（4断点 + 5m） | $1.5746 | 1/4 | -24.8% |
| 🥉 | SINGLE_1h（1断点 + 1h） | $1.7552 | 0/4 | -16.2% |
| 4th | SINGLE（Claude Code 原版） | $1.9431 | 1/4 | -7.2% |
| 5th | OLD（有 bug 的旧版） | $2.0938 | — | 基线 |

### 5.2 各场景冠军

| 场景 | 🏆 冠军 | 成本 | 亚军 | 差距 | 原因分析 |
|---|---|---|---|---|---|
| A（单问多工具） | **NEW_1h** | $0.40 | NEW | -6.1% | 1h TTL 保住了 system prefix 缓存 |
| B（多轮交互） | **NEW** | $0.29 | NEW_1h | -5.9% | Cache Write 占比低（4%），1.25× 比 2.0× 便宜 |
| C（并行调用） | **NEW_1h** | $0.39 | OLD | -1.4% | Cache Write 占比高（48%），1h TTL 减少了重写 |
| D（混合内容） | **SINGLE** | $0.40 | NEW_1h | -4.1% | 单断点恰好避开了空 content 的位置陷阱 |

### 5.3 未缓存 tokens——最直观的指标

这个指标反映了缓存命中后，每轮有多少 tokens 是"裸奔"发送的：

| 策略 | 场景 A | B | C | D | 平均 |
|---|---|---|---|---|---|
| OLD（旧版） | 251.9 | 189.4 | 161.3 | 369.0 | **242.9** |
| NEW | 1.0 | 1.4 | 1.0 | 1.0 | **1.1** |
| NEW_1h | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |
| SINGLE | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |
| SINGLE_1h | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |

旧版每轮泄漏了 **240 倍** 的未缓存 tokens。修复 BP4 位置后，所有策略都达到了近乎完美的缓存覆盖。

---

## 六、分析：为什么 4 断点赢了 1 断点？

Claude Code 选择单断点的理由是 KV 页面效率。但我们的数据显示，在**成本**维度上，答案不同。

### 6.1 多断点创建了独立的缓存段

```
4 断点方案:
  [System 14K ─ BP1] [Tools 2K ─ BP2] [History 30K ─ BP3] [New msg ─ BP4]
       缓存命中 ✓          缓存命中 ✓         缓存命中 ✓        未缓存（新内容）

1 断点方案:
  [System 14K] [Tools 2K] [History 30K] [New msg ─ BP]
  ←────────── 整个 prefix 作为一个缓存段 ──────────→    未缓存（新内容）
```

看起来两者都能缓存 prefix，但关键区别在于 **TTL 管理**：

- 4 断点方案中，system prompt 和 tools 有独立的 TTL 计时器。即使对话尾部的缓存过期了，BP1 和 BP2 的缓存仍然有效。
- 1 断点方案中，整个 prefix 共享一个 TTL。一旦过期，全部重写——包括那 14K 的 system prompt。

### 6.2 混合 TTL 放大了这个优势

```
BP1-BP3 (稳定内容): ttl = "1h"   → 写入一次，有效 1 小时
BP4 (对话尾部):      ttl = "5m"   → 每轮都写，但只付 1.25×
```

Anthropic 允许在同一请求中混合使用不同 TTL，但有一个约束：**长 TTL 的断点必须出现在短 TTL 之前**。我们 4 断点的布局天然满足这个条件：

```
[System ─ BP1:1h ─ BP2:1h]  [Tools ─ BP3:1h]  [Tail ─ BP4:5m]
    稳定内容（1 小时缓存）                          高频变动（5 分钟缓存）
```

成本模型：

| 组件 | Tokens | 写入费率 | 写入频次 | 年均摊成本 |
|---|---|---|---|---|
| System + Tools | ~16K | 2.0× ($30/M) | 每小时 1 次 | 可忽略 |
| 对话尾部 | ~500-5K/轮 | 1.25× ($18.75/M) | 每轮 1 次 | 主要成本 |

1 小时写入 system prompt 的成本是 $0.48，但每避免一次过期重写就省 $0.24。**只要发生 2 次以上的 5 分钟过期**，混合 TTL 策略就回本了。

### 6.3 SINGLE 为什么只在场景 D 赢了？

场景 D 中，空 content 的 assistant 消息（`content=""`）会干扰 4-BP 的尾部扫描。BP4 的放置逻辑需要跳过空消息找到最后一个有实际内容的消息——在某些 edge case 下，这导致 BP4 放到了次优位置。

单断点策略无脑地放在 `msg[-1]`，反而绕开了这个陷阱。

---

## 七、缓存激活门槛——一个容易忽略的细节

Anthropic 的缓存有**最小块大小**要求：

| 模型 | 最小可缓存 Tokens |
|---|---|
| Claude Opus / Haiku | 4,096 |
| Claude Sonnet | 1,024 |

这意味着在我们的测试中（~2,500 token 的 system prompt），缓存要到**第 3 轮**（总 input 超过 4,096）才会激活。

但在生产环境中，我们的 system prompt 约 14K tokens，从第 1 轮就超过门槛——缓存立即生效。

---

## 八、生产数据验证

受控测试的 12 轮对话在 ~60 秒内完成，远在 5 分钟 TTL 之内，不会触发服务端驱逐。生产环境则不同。

在真实的 64 轮 Opus 对话中（持续 11 分钟）：
- **22% 的轮次**发生了服务端缓存过期（cache_read 骤降到 13,988 = 仅 system prompt）
- 无缓存估算成本：¥425
- 使用 NEW_1h 策略的实际成本：¥198（**节省 53%**）
- 使用 OLD 策略的估算成本：¥340+（基于 240× 未缓存 token 泄漏）

**混合 TTL 策略的价值随对话长度增加而增长**。5 分钟以内的短对话，差异不大。超过 10 分钟的长对话，1 小时 TTL 可以防止最昂贵的缓存失效——system prompt 全量重写。

---

## 九、Claude Code 的其他巧思

虽然我们的多断点策略在成本上胜出，但阅读 Claude Code 源码仍然收获了很多好设计思路：

### 9.1 Session-stable TTL 锁定

```typescript
// 一旦确定 TTL 资格，整个 session 不再变化
let eligible = getPromptCache1hEligible()
if (eligible === null) {
  eligible = isSubscriber && !isOverage
  setPromptCache1hEligible(eligible)  // 锁死！
}
```

Mid-session 切换 TTL 会导致 cache key 变化，浪费 20-70K tokens 的已有缓存。CC 的做法是一旦确定用 1h 还是 5m，就锁死不变。

### 9.2 Cache Editing（缓存增量编辑）

当需要压缩旧消息（context compaction）时：
- **我们的做法**：删除旧消息 → 整个 prefix 失效 → 重写全部缓存
- **CC 的做法**：通过 `cache_edits` 发送增量删除指令，只删掉旧 tool_result，不重写整个 prefix

这是一个非常优雅的设计，目前还是 Anthropic 1P（第一方）专有的 beta 特性。

### 9.3 Global Scope 跨用户缓存

CC 把 system prompt 的静态部分标记为 `scope: "global"`，允许所有用户共享同一份 KV 缓存。这对我们（第三方 API 调用）不可用，但原理值得了解。

### 9.4 缓存失效检测（728 行代码）

CC 追踪了 **15+ 个**可能导致缓存失效的因素，包括：system prompt hash、每个 tool 的 schema hash、model 切换、beta headers 变化、effort 值变化、fast mode 切换等。一旦检测到缓存中断，会输出详细的 diff 日志。

我们借鉴了这个思路，实现了自己的 `detect_cache_break()` 系统。

---

## 十、结论与建议

### 最终推荐：4 断点 + 混合 TTL（NEW_1h）

| 策略 | 总成本 | vs 基线 | 适用场景 |
|---|---|---|---|
| **NEW_1h** ✅ | $1.52 | **-27.2%** | 大多数场景的最优选择 |
| NEW | $1.57 | -24.8% | 短对话（<5min），Cache Write 占比低时 |
| SINGLE | $1.94 | -7.2% | 极简实现，不想管断点逻辑时的 fallback |

### 五条实战建议

**1. 实测，不要猜。** 理论分析（KV 页面效率 vs 经济效率）和实际数据可能完全相反。搭一个 A/B 测试框架，用真实 API 跑一遍。

**2. 断点位置是第一优先级。** 从 `msg[-2]` 修到 `msg[-1]` 就省了 24.8%——超过任何 TTL 优化的收益。先确保断点位置正确，再考虑 TTL 策略。

**3. 混合 TTL 是近乎免费的优化。** 对于多断点方案，给稳定内容用 1h TTL、动态内容用 5m TTL，几乎没有额外成本，但能有效防止最贵的缓存失效。

**4. 监控你的实际缓存命中率。** 我们构建了 `detect_cache_break()` 系统来追踪每轮的 cache_read 变化。没有监控，你根本不知道 22% 的轮次在白白重写。

**5. 注意 API 返回值的语义差异。** 如果你通过 proxy 或兼容层调用 Claude，一定要验证 `prompt_tokens` 是"总量"还是"未缓存部分"——搞错了会让你的成本面板彻底失效。

---

## 试试看

Tofu 是开源的。完整的测试框架在 `debug/test_cache_validation.py`：

```bash
# 快速验证（不调用 API）
python debug/test_cache_validation.py --dry-run --arms all

# 完整 5 组 A/B 测试
python debug/test_cache_validation.py \
  --model claude-opus-4-20250514 \
  --arms OLD,NEW,NEW_1h,SINGLE,SINGLE_1h \
  --scenario all \
  --rounds 12
```

缓存断点实现在 `lib/llm_client.py:add_cache_breakpoints()`，缓存追踪系统在 `lib/tasks_pkg/cache_tracking.py`。

---

*Built with [Tofu (豆腐)](https://github.com/anthropics/tofu) — 一个认真对待 API 成本的自托管 AI 助手。*

*2026-04-05*
