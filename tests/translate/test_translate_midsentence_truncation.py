"""Regression test for the mid-sentence silent-truncation drop.

The reported bug ("跨会话消息 displayed incompletely"): a peer message ~500
chars long is auto-translated for the Project Brain Team thread. A cheap model
stops EARLY with ``finish_reason='stop'`` (not ``length``) after emitting only
~40% of the translation — a body that ends mid-word (e.g. "…请确认你的边界：ar").
Because ~40% of the source cleared the flat 0.30 ratio floor, the old detector
accepted it as complete AND cached it, so every later view served the same
cut-off text.

The fix adds a SOFT floor that only bites when the output ALSO ended
mid-sentence (``_ends_midsentence``) — catching the finish_reason=stop early
stop that lands between the hard floor and a full translation, WITHOUT
false-positiving a legitimately terse (but complete) translation. A
truncated-verdict result is also NEVER cached, so a later request re-translates.

These tests patch the LLM dispatch + MT provider + cache so they run offline
and deterministically (mirrors test_translate_wrong_language_flip.py).
"""

import pytest

import lib.translate.engine as engine
from lib.translate.engine import _ends_in_open_link, _ends_midsentence

pytestmark = pytest.mark.unit


# A ~500-char English peer message (the shape that triggered the bug).
_SOURCE = (
    'note -> conv mremei26: I am taking the pet-background BLENDING plus '
    'movement-interaction realism epic (owner feedback: "blending still '
    'strange, movement interaction feels fake"). I own tofu-scene.js and the '
    'scene-side of the coupling. You are generating pet ART and poses -- '
    'confirm your boundary: are you editing tofu-pet.js frame arrays '
    '(WALK_FRAMES/GROOM_FRAMES) and the .tofu-pet-img CSS (image-rendering)? '
    'I may need to touch .tofu-pet::after (contact shadow) and add a '
    'foreground-occlusion layer in the pet CSS block.'
)

# The BAD output: the model stopped early, ending mid-word ("：ar"). Its
# length lands BETWEEN the 0.30 hard floor and the 0.45 soft floor (so the
# old hard-floor check would NOT catch it), and it ends mid-sentence → the
# new soft+mid-sentence branch flags it as truncated.
_TRUNCATED_ZH = (
    '备注 → conv mremei26：我正在接手宠物与背景之间的融合处理工作，以及整个动作'
    '交互的真实感史诗级任务（所有者的反馈原话是：“融合效果到现在依然很奇怪，而且'
    '动作交互给人的感觉非常假”）。我这边负责的是 tofu-scene.js 这个文件，以及整个'
    '场景端的耦合逻辑部分。你现在负责的是生成宠物的艺术资产与各种姿态图 —— 所以'
    '请你务必先跟我确认一下你的编辑边界：ar'
)

# The GOOD output: complete, ends on a full stop.
_GOOD_ZH = (
    '备注 → conv mremei26：我正在处理宠物与背景的融合以及动作交互真实感的史诗任务'
    '（所有者反馈：“融合效果依然奇怪，动作交互感觉很假”）。我负责 tofu-scene.js '
    '以及场景端的耦合。你负责生成宠物艺术资产与姿态 —— 请确认你的边界：你是否在'
    '修改 tofu-pet.js 的帧数组（WALK_FRAMES/GROOM_FRAMES）以及 .tofu-pet-img '
    'CSS（图像渲染）？我可能需要改动 .tofu-pet::after（接触阴影），并在宠物 CSS '
    '块中添加一个前景遮挡层。'
)


def _patch_sequence(monkeypatch, replies, capture_puts=None):
    """Disable MT; make smart_chat return successive `replies`; optionally
    capture every translate_cache.put so a test can assert what was cached."""
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *a, **k: None)

    def _fake_put(text, source, target, translated, model=''):
        if capture_puts is not None:
            capture_puts.append(translated)

    monkeypatch.setattr(engine.translate_cache, 'put', _fake_put)
    state = {'i': 0}

    def _fake_smart_chat(messages=None, **kw):
        i = min(state['i'], len(replies) - 1)
        state['i'] += 1
        return replies[i], {'finish_reason': 'stop',
                            '_dispatch': {'model': f'm{i}', 'key': 'k1'}}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', _fake_smart_chat)
    return state


# ── _ends_midsentence pure unit ────────────────────────────────────
def test_ends_midsentence_pure():
    # Ends mid-word / on a bare char → mid-sentence (truncated candidate).
    assert _ends_midsentence('请确认你的边界：ar') is True
    assert _ends_midsentence('涉及 WALK_FRAMES/GR') is True
    assert _ends_midsentence('新增坐/卷卧/玩耍/伸展姿势') is True
    # Ends on a terminator/closer → complete.
    assert _ends_midsentence('我负责场景端的耦合。') is False
    assert _ends_midsentence('done.') is False
    assert _ends_midsentence('(a full note)') is False
    assert _ends_midsentence('好的！') is False
    # A protected block can legitimately be the final source item. The
    # immutable placeholder is restored after validation, so its closing
    # bracket is a complete boundary rather than evidence of truncation.
    assert _ends_midsentence('说明已完成。 ⟦NT_0⟧') is False
    # Empty is not "mid-sentence" (the empty check owns that case).
    assert _ends_midsentence('') is False
    assert _ends_midsentence('   ') is False


def test_midsentence_short_output_is_retried(monkeypatch):
    # 1st model returns the truncated (mid-word) body; it must be rejected and
    # retried; 2nd model returns the complete output.
    state = _patch_sequence(monkeypatch, [_TRUNCATED_ZH, _GOOD_ZH])
    out, usage = engine._translate_one_chunk(
        _SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _GOOD_ZH.strip()
    assert state['i'] >= 2  # the truncation was rejected, a 2nd attempt made
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_persistent_truncation_not_cached(monkeypatch):
    # Every model truncates → after the retry budget the engine ACCEPTS the
    # best-effort partial (verdict=truncated) but must NOT cache it, so a
    # later request re-translates instead of re-serving the partial forever.
    puts = []
    state = _patch_sequence(monkeypatch, [_TRUNCATED_ZH], capture_puts=puts)
    out, usage = engine._translate_one_chunk(
        _SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    # Best-effort partial is returned (not raised — it's non-empty content).
    assert out == _TRUNCATED_ZH.strip()
    assert state['i'] >= 2  # retried before accepting
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'truncated'
    # The load-bearing guarantee: the truncated body was NOT cached.
    assert puts == [], f'a truncated translation must not be cached: {puts}'


def test_artifact_workflow_rejects_persistent_truncation(monkeypatch):
    """Artifact producers may fail closed instead of adopting a partial body."""
    from lib.translate import TranslationContentRefused

    puts = []
    state = _patch_sequence(monkeypatch, [_TRUNCATED_ZH], capture_puts=puts)

    with pytest.raises(TranslationContentRefused) as exc_info:
        engine._translate_one_chunk(
            _SOURCE,
            system_prompt='translate',
            source='English',
            target='Chinese',
            overall_deadline=30,
            accept_truncated=False,
        )

    assert exc_info.value.verdict == 'truncated'
    assert state['i'] >= 2
    assert puts == []


def test_complete_output_accepted_first_try_and_cached(monkeypatch):
    # A complete translation (ends on a full stop) must be accepted on the
    # FIRST try and cached — the mid-sentence guard must not false-positive.
    puts = []
    state = _patch_sequence(monkeypatch, [_GOOD_ZH], capture_puts=puts)
    out, usage = engine._translate_one_chunk(
        _SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _GOOD_ZH.strip()
    assert state['i'] == 1  # accepted immediately, no retry
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'
    assert puts == [_GOOD_ZH.strip()], f'a complete translation must be cached: {puts}'


# ── Unclosed Markdown link tail (structural, length-independent) ──
#
# Production case (conv mwn8kcx6gcqwxu, 2026-11-19): the translation of an
# assistant reply stopped mid-link after 298 of 632 source chars. That ratio
# (47%) sits ABOVE the 45% soft floor, and the body DOES end mid-sentence, so
# the ratio+mid-sentence branch never fired — the cut body was committed and
# the transcript rendered a dangling "[" where the rest of the message should
# be. The tail shape, not the length, is the tell.
_LINK_SOURCE = (
    "Done — the Devices page is already live.\n\n"
    "The [Devices page](page:devices) now has a **Back to Chat** button that "
    "stays visible as you scroll and returns you to your conversation without "
    "losing your draft. Closing device details or the add-device wizard "
    "returns you to the list.\n\n"
    "I replaced the wide table with compact, grouped device rows using Tofu's "
    "shared controls and theme colors. Long hostnames wrap on narrow "
    "screens.\n\n"
    "[Desktop preview](/api/response-assets/51838716-2ddb-45b8-80a5-2d823e9d64ba) · "
    "[Mobile preview](/api/response-assets/496ec23d-28b7-40f4-9764-c56f28278270)\n\n"
    "Merged as `d32b0ec4d6` (pre-merge: `51a0086f56`)."
)
_LINK_TRUNCATED_ZH = (
    '我重新设计了设备页面，并修复了相关问题。**返回对话**会始终固定在页面顶部，即使滚动也不会消失。'
    '点击后可返回当前聊天页面，且不会清空您的草稿。关闭设备详情或添加设备向导后会返回列表，'
    '而无需担心浏览器的后退按钮重新打开它们。\n\n新布局采用了 Tofu 的纸张调色板和共享控件：'
    '一个用于 Home 服务器的卡片、紧凑的设备组（一眼就能看到状态和权限），以及一个折叠的连接指南。'
    '长名称在移动设备上会自动换行，而不会拉伸页面。\n\n'
    '![在 Tofu 纸张主题下重新设计的设备页面]'
    '(/api/response-assets/51838716-2ddb-45b8-80a5-2d823e9d64ba)\n\n['
)
_LINK_GOOD_ZH = (
    '设备页面已完成并已上线。\n\n'
    '[设备页面](page:devices)现在有一个**返回对话**按钮，滚动时始终可见，点击后可返回对话，'
    '且不会清空草稿。关闭设备详情或添加设备向导后会返回列表。\n\n'
    '我把宽表格换成了紧凑的分组设备行，使用 Tofu 的共享控件和主题颜色。长主机名在窄屏上会自动换行。\n\n'
    '[桌面预览](/api/response-assets/51838716-2ddb-45b8-80a5-2d823e9d64ba) · '
    '[移动端预览](/api/response-assets/496ec23d-28b7-40f4-9764-c56f28278270)\n\n'
    '已合并为 `d32b0ec4d6`（合并前：`51a0086f56`）。'
)


def test_ends_in_open_link_pure():
    # A tail inside an unclosed link/image opener means the model stopped
    # between '[' and ']' — never a complete document.
    assert _ends_in_open_link('…页面。\n\n[') is True
    assert _ends_in_open_link('见 [桌面预览](') is False  # inside link TARGET
    assert _ends_in_open_link('见 [桌面预览](/x)') is False
    assert _ends_in_open_link('[Desktop preview]') is False
    assert _ends_in_open_link('普通结尾。') is False
    assert _ends_in_open_link('') is False


def test_unclosed_link_tail_rejected_above_ratio_floors(monkeypatch):
    # Guard the reproduction itself: the ratio clears the soft floor, so only
    # the structural tail check can reject this body.
    assert len(_LINK_TRUNCATED_ZH) / len(_LINK_SOURCE) > 0.45
    assert _ends_midsentence(_LINK_TRUNCATED_ZH) is True

    state = _patch_sequence(monkeypatch, [_LINK_TRUNCATED_ZH, _LINK_GOOD_ZH])
    out, usage = engine._translate_one_chunk(
        _LINK_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _LINK_GOOD_ZH.strip()
    assert state['i'] >= 2  # the cut body was rejected and retried
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_source_ending_in_open_bracket_not_flagged(monkeypatch):
    # A source may legitimately end inside an unclosed bracket (a document
    # ABOUT Markdown). The translation inherits that shape; it must not be
    # treated as a cut translation.
    source = (
        'Markdown link syntax begins with an opening square bracket, as in [ — '
        'that opener is the whole point of the sentence. A renderer that never '
        'sees the closing bracket shows the raw character, which is exactly what '
        'this note is about, so the note ends here with the opener: ['
    )
    translated = (
        'Markdown 链接语法以一个左方括号开头，就像这里写的那样：[ —— 这个左括号就是整句话的全部要点。'
        '渲染器如果永远看不到右方括号，就只会把这个原始字符直接显示出来，而这正是本说明想要讲清楚的东西，'
        '所以这段说明就到这里，最后以一个左括号结束：['
    )
    # The body lands above every ratio floor and ends mid-sentence, so only the
    # source-shape exemption keeps it from being rejected as truncated.
    assert len(translated) / len(source) > 0.45
    assert _ends_midsentence(translated) is True
    state = _patch_sequence(monkeypatch, [translated])
    out, usage = engine._translate_one_chunk(
        source, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == translated.strip()
    assert state['i'] == 1  # accepted immediately
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_terse_but_complete_short_output_not_flagged(monkeypatch):
    # A short-but-COMPLETE translation (ends on punctuation) below the soft
    # floor must NOT be flagged — the mid-sentence signal is required. This is
    # the false-positive guard: ratio alone would wrongly reject a terse but
    # finished translation.
    # >200 chars so the soft-truncation branch is actually EVALUATED (the
    # branch is gated on clen > 200) — this proves it's the mid-sentence
    # signal, not the length gate, that spares a legitimately terse output.
    long_src = (
        'This is a deliberately long English paragraph written so that a '
        'competent translator will render it into a naturally much more terse '
        'Chinese line, because Chinese is far denser per character, and the '
        'resulting translation, though short relative to this source, is a '
        'complete and finished sentence that ends on proper punctuation.')
    # Ratio lands in (0.30, 0.45) — below the soft floor — but ends on a full
    # stop 。 → the mid-sentence signal is absent, so it must NOT be flagged
    # (ratio alone would wrongly reject a finished translation).
    terse_complete = (
        '这是一段刻意写得很长的英文段落，目的是让一个称职的译者把它翻译成一句自然'
        '得多、也简洁得多的中文；因为中文每个字的信息密度要高得多，所以尽管译文相'
        '对原文而言篇幅较短，它仍然是一句完整、结束在正确标点上的句子。')  # ends on 。
    state = _patch_sequence(monkeypatch, [terse_complete])
    out, usage = engine._translate_one_chunk(
        long_src, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == terse_complete.strip()
    assert state['i'] == 1  # accepted immediately (ends on 。 → not truncated)
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'
