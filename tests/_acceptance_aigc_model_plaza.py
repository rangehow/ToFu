#!/usr/bin/env python3
"""tests/_acceptance_aigc_model_plaza.py — 目标 URL 端到端验收（三态判定）

一条命令跑出 REAL_CONTENT / LOGIN_WALL / NO_CONTENT 三态。

为什么不能用 reason 判定
------------------------
`_fetch_url_one()` 的 `reason` 字段回答的是「提取管线是否成功产出了正文」，
不是「产出的正文是不是我们要的东西」。SSO 登录页**也是一个能被成功提取的
页面**，所以它同样返回 `extracted_ok` —— 该字段对「真内容 vs 登录页」
零区分力。曾经据此判定「链路已通」，是把近似信号当成了判据。

因此本脚本的判据完全锚在**正文内容**上：
  * 负向（一票否决）：命中任一 SSO 登录墙特征 → LOGIN_WALL
  * 正向（需达阈值）：命中模型广场应有实体 → REAL_CONTENT
  * 两者皆不满足 → NO_CONTENT（含 None / 空 / 无法归类的页面）

负向先于正向：登录页里也可能出现"模型"这类字样（页面标题、跳转提示），
先判负向可避免登录页被正向词误判为真内容。

退出码
------
  0 = REAL_CONTENT（验收通过）
  1 = LOGIN_WALL（SSRF 已放行但缺 SSO 凭证）
  2 = NO_CONTENT（抓取失败/被拦截，看 reason + detail 定位是哪一层）

凭证新鲜度（为什么要打印 auth-source 命中）
----------------------------------------
``allow_private_hosts`` 和 SSO cookie 走的是 **两条不同的缝**：
  * 前者由 ``sync_search_config()`` 推给库，本脚本每次都调，永远新鲜；
  * 后者由 ``lib.auth_sources.match_source()`` 在抓取时现查。该模块的缓存
    已按存储文件 mtime 自愈（外部进程写入会被自动发现），但 mtime 看不见
    「同一刻度内的覆写」。

本脚本因此在抓取前调用公开的 ``invalidate_cache()`` 并打印命中情况
（只打 domain + cookie 数量，**绝不打印值**），让「凭证到底有没有被这次
抓取看见」在输出里一目了然 —— 从而把「真没连接」和「连了却仍被挡」
（cookie 过期 / 字段名不对 / 二次鉴权）区分开。

用法
----
    python3 tests/_acceptance_aigc_model_plaza.py
    python3 tests/_acceptance_aigc_model_plaza.py --url <其它URL>

前置：需要 CONDA_PREFIX 指向 tofu env（Playwright 依赖 LD_LIBRARY_PATH 注入）。
本脚本 **只读**：不写 data/config，不碰凭证。
"""

import argparse
import logging
import os
import sys

TARGET_URL = (
    'https://aigc.sankuai.com/ml/modelPlaza/modelInfo'
    '?sortType=releaseTime&labels=modelCapability:%E6%96%87%E6%9C%AC%E7%94%9F%E6%88%90'
)

# ── 负向：SSO 登录墙特征（命中任一即判 LOGIN_WALL）──
# 取自实测的 ssosv.sankuai.com 登录页正文与 URL。
LOGIN_WALL_MARKERS = (
    'ssosv.sankuai.com',
    'Login center',
    '二维码登录',
    '忘记密码',
    'Forgot Password',
    '扫码登录',
    'sson/login',
)

# ── 正向：模型广场应有的实体 ──
# 该页是「文本生成」能力筛选下的模型列表，正文应同时具备
# 列表结构词 + 具体模型名。单一泛词（如"模型"）不作数。
PLAZA_STRUCTURE_MARKERS = (
    '模型广场', '文本生成', '发布时间', '模型能力',
    '模型列表', 'modelPlaza', '能力标签',
)
PLAZA_ENTITY_MARKERS = (
    'LongCat', 'longcat', 'Qwen', 'qwen', 'DeepSeek', 'deepseek',
    'GPT', 'Claude', 'claude', 'gemini', 'Gemini', 'kimi', 'Kimi',
    'doubao', 'Doubao', 'GLM', 'glm', 'llama', 'Llama',
)
# 判 REAL_CONTENT 的阈值：结构词 >=1 且 实体词 >=1 且 正文 >= 该长度。
# 登录页实测仅 44 字，真列表页远超此数。
MIN_REAL_CHARS = 200


def classify(text):
    """把正文归类为 REAL_CONTENT / LOGIN_WALL / NO_CONTENT。

    Returns:
        (verdict, evidence) — evidence 是判定依据的可读说明。
    """
    if not text or not text.strip():
        return 'NO_CONTENT', 'page_content 为空'

    # 负向一票否决，先于正向 —— 登录页也可能含正向词。
    hit_login = [m for m in LOGIN_WALL_MARKERS if m in text]
    if hit_login:
        return 'LOGIN_WALL', f'命中登录墙特征: {hit_login}'

    hit_struct = [m for m in PLAZA_STRUCTURE_MARKERS if m in text]
    hit_entity = [m for m in PLAZA_ENTITY_MARKERS if m in text]
    if hit_struct and hit_entity and len(text) >= MIN_REAL_CHARS:
        return 'REAL_CONTENT', (
            f'结构词{hit_struct[:4]} + 实体词{hit_entity[:4]} + {len(text)}字')

    missing = []
    if not hit_struct:
        missing.append('无结构词')
    if not hit_entity:
        missing.append('无模型实体词')
    if len(text) < MIN_REAL_CHARS:
        missing.append(f'正文仅{len(text)}字(<{MIN_REAL_CHARS})')
    return 'NO_CONTENT', '、'.join(missing)


def probe_auth_source(url):
    """强制从磁盘重读 auth-source，返回该 URL 命中的凭证概况。

    ``lib.auth_sources`` 的模块级缓存加载后不再重读磁盘，且无对外失效
    接口。验收必须看到磁盘上的**当前**状态，否则一个陈旧的空 cookie 集
    会把结果偷换成假阴性的 LOGIN_WALL。这里直接重置缓存标志强迫它重读。

    Returns:
        (label, matched) — label 是可读描述，matched 是布尔（是否拿到凭证）。
        **只返回 domain 与 cookie 数量，永不触碰或回显 cookie 值。**
    """
    try:
        import lib.auth_sources as _as
        # 走公开接口强制下一次查询重读磁盘。缓存本身已按 mtime 自愈,
        # 这里显式失效是为了连「同刻度覆写」也不漏。
        _as.invalidate_cache()
        src = _as.match_source(url)
    except Exception as e:
        return f'探测失败（{type(e).__name__}: {e}）', False
    if not src:
        return 'None（该域未连接或未启用）', False
    n = len(src.get('cookies') or [])
    return f"domain={src.get('domain')} cookie_count={n}", n > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=TARGET_URL)
    ap.add_argument('--show', type=int, default=300, help='打印正文前 N 字')
    args = ap.parse_args()

    logging.disable(logging.CRITICAL)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from lib.search_bridge import sync_search_config
    sync_search_config()

    import tofu_search
    allow = tofu_search.get_config().allow_private_hosts

    # 先探凭证新鲜度，再抓取 —— 顺序重要：刷新缓存必须发生在
    # _fetch_url_one 内部调 match_source() 之前。
    auth_label, auth_matched = probe_auth_source(args.url)

    from lib.tasks_pkg.handlers.search import _fetch_url_one
    item = _fetch_url_one(args.url, '美团模型广场 文本生成 模型列表',
                          fetch_reason='验收')
    text = item.get('page_content') or ''
    verdict, evidence = classify(text)

    print('═══ 目标 URL 验收 ═══')
    print(f'  URL                 : {args.url[:90]}')
    print(f'  allow_private_hosts : {sorted(allow) if allow else "(空 — 未放行)"}')
    print(f'  auth_source 命中     : {auth_label}   ← 已强制重读磁盘')
    print(f'  reason              : {item.get("reason")}   ← 仅供定位,不作判据')
    print(f'  error_msg           : {(item.get("error_msg") or "(无)")[:120]}')
    print(f'  正文长度            : {len(text)}')
    if text and args.show:
        preview = text[:args.show].replace('\n', ' ')
        print(f'  正文预览            : {preview}')
    print()
    print(f'>>> 判定: {verdict}')
    print(f'>>> 依据: {evidence}')
    print()
    if verdict == 'REAL_CONTENT':
        print('✅ 验收通过 — 取回了模型广场真实内容')
        return 0
    if verdict == 'LOGIN_WALL':
        print('⛔ 未通过 — 拿到的是 SSO 登录页,不是模型广场内容')
        if auth_matched:
            # 凭证确实被看见了却仍然撞登录墙 —— 不是“没连接”，而是凭证本身的问题。
            print('   ⚠ 凭证已被本次抓取看见（上方 auth_source 命中非空）,但仍被挡。')
            print('   可能原因: cookie 已过期 / cookie 名不对 / 该站需二次鉴权。')
            print('   → 重新捕获一次 cookie 再跑;若仍旧,需查该站真实会话字段名。')
        else:
            print('   SSRF 层已放行,缺的是 sankuai.com 的 SSO cookie')
            print('   → 设置 → 搜索 → 需要登录的来源 → 连接 sankuai.com')
        return 1
    print('⛔ 未通过 — 没有取回可用正文')
    print('   看上面的 reason/error_msg 定位是哪一层拦截:')
    print('     fetch_failed:ssrf_blocked → 未放行,设置 → 搜索 → 内网主机放行')
    print('     fetch_failed:spa_shell    → JS 渲染兜底未拿到内容')
    print('     其它                       → 见 error_msg 原文')
    return 2


if __name__ == '__main__':
    sys.exit(main())
