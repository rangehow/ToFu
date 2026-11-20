"""Real app + isolated storage: the My Day goal journey in a real browser.

Write a goal, see its decomposition, adopt and check items off, reload, open
the page again in a fresh browser context, and come back the next day (the
browser clock moves to tomorrow). The model is the only seam: a deterministic
decomposition stands in for the provider. Set TOFU_MYDAY_SCREENSHOTS to a
directory to keep the screenshots of each step.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

pytestmark = pytest.mark.visual

GOAL = '把 My Day 改成目标追踪'
SUGGESTED = ['写设计文档', '实现目标存储', '做浏览器验证']


def _shot(page, name):
    directory = os.environ.get('TOFU_MYDAY_SCREENSHOTS')
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.locator('.myday-modal').screenshot(path=str(Path(directory) / f'{name}.png'))


def _open(page, size=(1280, 900)):
    from tests.conftest import _dismiss_onboarding_modals
    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    if size:
        page.set_viewport_size({'width': size[0], 'height': size[1]})
    _wait_ready(page)
    _invoke_action(page, 'openDailyReport')
    page.wait_for_selector('#dailyReportModal.open .myday-goal-composer input')


def _row(page, text):
    return page.locator('.myday-goal .myday-goal-item', has_text=text)


def test_today_token_costs_reprice_in_place_on_desktop_and_phone(page, monkeypatch, assert_no_js_errors):
    from lib.conversations.repository import ConversationTurnUsage
    from lib.daily_report import cost
    from lib.ttl_cache import TTLCache

    monkeypatch.setattr(cost, '_live_day_cache', TTLCache(ttl=0))

    today = dt.date.today()
    stamp = int(dt.datetime.now().timestamp() * 1000)
    projection = ConversationTurnUsage(
        metadata={'id': 'myday-pricing-fixture', 'created_at': stamp, 'updated_at': stamp},
        message_count=1, title_fallback='', turns=[{
            'index': 0, 'timestamp': stamp, 'model': 'qwen3.5-plus', 'usage': {
                'input_tokens': 150_000, 'output_tokens': 32_000,
                'cache_read_input_tokens': 70_000, 'cache_creation_input_tokens': 24_000,
            },
        }],
    )

    monkeypatch.setattr('lib.conversations.usage_snapshot.reconcile_day_usage', lambda **kwargs:
                        SimpleNamespace(candidate_count=1, projections=(projection,)))
    _open(page)
    card = page.locator('#mydayTokenCosts details')
    expect(card.locator('.myday-token-total')).to_contain_text('¥')
    expect(card.locator('select')).to_have_count(0)
    _shot(page, 'token-costs-collapsed')
    card.locator('summary').focus()
    card.locator('summary').press('Enter')
    table = card.get_by_role('table', name='今日 Token 费用')
    for label, count in [('未缓存输入', '150,000'), ('输出', '32,000'),
                         ('缓存输入（读取）', '70,000'), ('缓存写入（输入）', '24,000')]:
        row = table.get_by_role('row').filter(has=page.get_by_role('rowheader', name=label, exact=True))
        expect(row).to_contain_text(count)
    expect(card.get_by_text('缓存输出 · 未上报')).to_be_visible()
    select = card.get_by_role('combobox', name='如果今天只用一个模型？')
    select.select_option('qwen3.5-plus')
    result = card.locator('output')
    expect(result).to_contain_text('按 API 价格约')
    original = result.inner_text()
    select.select_option('qwen3.5-flash')
    expect(result).not_to_have_text(original)
    expect(select).to_have_value('qwen3.5-flash')
    _shot(page, 'token-costs-desktop')

    page.set_viewport_size({'width': 375, 'height': 812})
    select.scroll_into_view_if_needed()
    assert page.locator('#mydayTasksScroll').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    _shot(page, 'token-costs-mobile')
    page.set_viewport_size({'width': 320, 'height': 740})
    expect(select).to_have_value('qwen3.5-flash')
    assert page.locator('#mydayTasksScroll').evaluate('(el) => el.scrollWidth <= el.clientWidth')

    # Reopen with a blocked network after a true reload: owner-scoped IDB
    # still paints the estimate, and a failure can't turn it into a zero.
    amount = card.locator('.myday-token-total').inner_text()
    page.wait_for_function('''() => new Promise(resolve => {
      const request = indexedDB.open('tofu_myday_cache');
      request.onerror = () => resolve(false);
      request.onsuccess = () => {
        const db = request.result;
        const read = db.transaction('months').objectStore('months').getAll();
        read.onsuccess = () => { resolve(read.result.some(row => row.value.today_token_costs)); db.close(); };
        read.onerror = () => { db.close(); resolve(false); };
      };
    })''')
    requests = []
    page.route('**/api/v4/daily-report/calendar/**', lambda route: requests.append(route))
    _open(page, size=(375, 812))
    expect(card.locator('.myday-token-total')).to_have_text(amount)
    expect(card.locator('summary')).to_contain_text('正在加载')
    assert len(requests) == 1
    requests[0].fulfill(status=200, json={'ok': True, 'data': {'conv_days': {}, 'cost_days': {}}})
    expect(card.locator('summary')).to_contain_text('暂时无法获取')
    expect(card.locator('.myday-token-total')).to_have_text(amount)
    page.unroute('**/api/v4/daily-report/calendar/**')

    yesterday = today - dt.timedelta(days=1)
    if yesterday.month != today.month:
        page.locator('#mydayCalHeader .mcal-nav').first.click()
    page.locator(f'#mydayCalGrid .mcal-d[data-tofu-action="_mydaySelectDay({yesterday.day})"]').click()
    expect(card).to_have_count(0)


def test_goal_journey_survives_reload_restart_and_the_next_day(page, browser, live_server, monkeypatch,
                                                                 assert_no_js_errors):
    prompts = []

    def decompose(messages, owner):
        prompts.append(messages[-1]['content'])
        return '{"items": ' + str(SUGGESTED).replace("'", '"') + '}'
    monkeypatch.setattr('lib.myday.decompose._dispatch', decompose)
    origin = live_server.rstrip('/')

    _open(page)
    expect(page.locator('.myday-empty')).to_be_visible()
    _shot(page, '01-empty')

    composer = page.locator('.myday-goal-composer input')
    composer.fill(GOAL)
    composer.press('Enter')
    card = page.locator('.myday-goal').first
    expect(card.locator('.myday-goal-title')).to_have_text(GOAL)
    expect(card.locator('.myday-goal-item.suggested')).to_have_count(3)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('—')
    _shot(page, '02-decomposed')

    card.get_by_role('button', name='全部采纳').click()
    expect(card.locator('.myday-goal-item.suggested')).to_have_count(0)
    add = card.locator('.myday-goal-add input')
    add.fill('和负责人确认验收')
    add.press('Enter')
    expect(_row(page, '和负责人确认验收')).to_have_count(1)
    _row(page, '写设计文档').locator('.myday-todo-check').click()
    _row(page, '实现目标存储').get_by_role('button', name='开始').click()
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
    expect(_row(page, '写设计文档').locator('.myday-goal-badge.today')).to_have_text('今天')
    expect(page.locator('#mydayProgress')).to_have_text('1 个目标 · 今天完成 1 项 · 推进中 1 项')
    _shot(page, '03-progress')

    page.reload()  # reload: the board is read back from the server
    _wait_ready(page)
    _invoke_action(page, 'openDailyReport')
    card = page.locator('.myday-goal').first
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
    expect(_row(page, '写设计文档')).to_have_class(re.compile(r'\bdone\b'))
    expect(_row(page, '实现目标存储').locator('.myday-goal-chip.on')).to_have_count(1)
    _shot(page, '04-after-reload')

    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    context = browser.new_context()  # a restarted browser, opened the next day
    try:
        next_day = context.new_page()
        next_day.clock.install(time=tomorrow)
        next_day.goto(origin, wait_until='domcontentloaded')
        _open(next_day)
        card = next_day.locator('.myday-goal').first
        expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
        expect(card.locator('.myday-goal-item')).to_have_count(4)
        done = _row(next_day, '写设计文档')
        expect(done.locator('.myday-goal-badge.today')).to_have_count(0)
        expect(done.locator('.myday-goal-badge.when')).to_have_count(1)
        expect(_row(next_day, '做浏览器验证').locator('.myday-goal-badge.since')).to_have_count(1)
        expect(next_day.locator('#mydayProgress')).to_have_text('1 个目标 · 今天完成 0 项 · 推进中 1 项')
        _shot(next_day, '05-next-day')

        _row(next_day, '实现目标存储').locator('.myday-todo-check').click()
        expect(card.locator('.myday-goal-pct-value')).to_have_text('≈50%')
        card.get_by_role('button', name='重新拆解').click()
        expect(card.locator('.myday-goal-working')).to_have_count(0)
        expect(card.locator('.myday-goal-item')).to_have_count(4)
        _shot(next_day, '06-next-day-regenerated')

        # A legacy generated report of that day, written through its storage owner.
        from lib.daily_report.storage import _save_report
        from lib.identity import PERSONAL_USER_ID
        yesterday = dt.date.today()
        _save_report(yesterday.isoformat(), {
            'streams': [{'id': 'stream-legacy', 'title': '旧日报里的工作流', 'summary': '只读归档',
                         'status': 'done', 'conv_ids': []}],
            'tomorrow': [{'id': 'todo-legacy', 'text': '旧日报里的次日计划', 'done': False}],
        }, owner_user_id=PERSONAL_USER_ID)
        if yesterday.month != tomorrow.month:
            next_day.locator('#mydayCalHeader .mcal-nav').first.click()
        next_day.locator(f'#mydayCalGrid .mcal-d[data-tofu-action="_mydaySelectDay({yesterday.day})"]').click()
        expect(next_day.locator('#mydayTasks')).to_contain_text('这天的目标进展')
        expect(next_day.locator('#mydayTasks .myday-goal-compact')).to_contain_text('写设计文档')
        archive = next_day.locator('#mydayTasks .myday-goal-archive')
        expect(archive).to_contain_text('旧日报里的工作流')
        expect(archive).to_contain_text('当天写下的次日计划')
        expect(archive).to_contain_text('旧日报里的次日计划')
        expect(archive.locator('button')).to_have_count(0)
        _shot(next_day, '07-yesterday-recap')
    finally:
        context.close()

    goals = page.request.get(origin + '/api/v4/myday/goals').json()['data']['goals']
    [stored] = [goal for goal in goals if goal['text'] == GOAL]
    states = {item['text']: (item['origin'], item.get('proposal'), item['status'], item['manual'])
              for item in stored['items']}
    assert states == {
        '写设计文档': ('agent', 'accepted', 'completed', True),
        '实现目标存储': ('agent', 'accepted', 'completed', True),
        '做浏览器验证': ('agent', 'accepted', 'pending', False),
        '和负责人确认验收': ('owner', None, 'pending', False),
    }
    assert stored['progress']['percent'] == 50 and len(prompts) == 2
    assert '写设计文档 (done)' in prompts[1], 'the regeneration saw the adopted items'


def test_linked_goal_shows_worker_evidence_and_the_owner_override_wins(page, monkeypatch, tmp_path,
                                                                        assert_no_js_errors):
    """The dispatch read is the seam: its rows come from the request cohort shape
    (tests/myday/test_myday_dispatch_evidence.py maps real delegation rows)."""
    from tests.myday.test_myday_evidence import row
    import lib.myday.evidence as evidence

    project = str(tmp_path / 'tofu')
    rows = [row('a1', review='accept', brief='[DB] Measure the load\nDetails'),
            row('a2', brief='[GOALS] Redesign My Day\nDetails'),
            row('a3', execution=False, brief='[DOCS] Update the runbook\nDetails')]
    busy = {'worker-a2'}
    monkeypatch.setattr(evidence, '_bound_projects', lambda owner: [(project, 'coordinator-1')])
    monkeypatch.setattr(evidence, '_newest_rows', lambda owner, path: rows)
    monkeypatch.setattr(evidence, '_request_text', lambda owner, coordinator, turn: '看一下 tofu 的稳定性')
    monkeypatch.setattr(evidence, '_request_cohort',
                        lambda owner, path, source: {'rows': rows, 'settled': False})
    monkeypatch.setattr(evidence, '_live_conversations', lambda owner: frozenset(busy))
    monkeypatch.setattr('lib.myday.decompose._dispatch', lambda messages, owner: '{"items": []}')

    _open(page)
    composer = page.locator('.myday-goal-composer input')
    composer.fill('让 tofu 稳定下来')
    composer.press('Enter')
    card = page.locator('.myday-goal', has_text='让 tofu 稳定下来')
    expect(card.locator('.myday-goal-btn', has_text='关联项目请求')).to_be_visible()
    card.get_by_role('button', name='关联项目请求').click()
    option = card.locator('.myday-goal-option', has_text='看一下 tofu 的稳定性')
    expect(option).to_contain_text('3 个 Worker')
    _shot(page, '08-link-picker')
    option.click()

    expect(card.locator('.myday-goal-request')).to_contain_text('关联请求：看一下 tofu 的稳定性')
    workers = card.locator('.myday-goal-item', has=page.locator('.myday-goal-badge.worker'))
    expect(workers).to_have_count(3)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈33%')
    expect(_row(page, '[GOALS] Redesign My Day').locator('.myday-goal-badge.worker')).to_have_text('运行中')
    queued = _row(page, '[DOCS] Update the runbook')
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('排队中')

    queued.locator('.myday-todo-check').click()  # the owner already did it by hand
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈66%')
    expect(queued.locator('.myday-goal-badge.manual')).to_have_text('手动')
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('排队中')
    _shot(page, '09-linked-override')

    rows[2] = row('a3', brief='[DOCS] Update the runbook\nDetails')  # the Worker starts later
    page.locator('#mydayRefreshBtn').click()
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('待验收')
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈66%')
    queued.get_by_role('button', name='跟随 Worker 状态').click()
    expect(queued.locator('.myday-goal-badge.manual')).to_have_count(0)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈33%')
    _shot(page, '10-follow-worker')


def _line_count(page, text):
    """Rendered lines of one item text: one client rect top per line box."""
    return page.evaluate("""(text) => {
      const node = Array.from(document.querySelectorAll('#mydayTasks .myday-goal-item .myday-todo-text'))
        .find((element) => element.textContent === text);
      const range = document.createRange();
      range.selectNodeContents(node);
      return new Set(Array.from(range.getClientRects()).map((rect) => Math.round(rect.top))).size;
    }""", text)


def test_a_phone_screen_scrolls_to_every_goal_and_the_calendar(browser, live_server, monkeypatch):
    """On a 375 px phone the board scrolls by hand: the last goal's actions and
    the calendar stay reachable, an item keeps its short text on one line beside
    its badges, and a long unbroken item wraps inside its row."""
    from tests.conftest import _attach_js_error_capture

    monkeypatch.setattr('lib.myday.decompose._dispatch',
                        lambda messages, owner: '{"items": ["实现目标存储", "做浏览器验证"]}')
    origin = live_server.rstrip('/')
    context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True, has_touch=True)
    page = context.new_page()
    _attach_js_error_capture(page)
    created = []
    try:
        page.goto(origin, wait_until='domcontentloaded')
        api, goals = page.request, origin + '/api/v4/myday/goals'
        first = api.post(goals, data={'text': '窄屏：第一个目标'}).json()['data']['goal']
        created.append(first['id'])
        api.post(f"{goals}/{first['id']}/decompose")
        first = api.post(f"{goals}/{first['id']}/suggestions", data={'decision': 'accept'}).json()['data']['goal']
        started = next(item for item in first['items'] if item['text'] == '实现目标存储')
        api.patch(f"{goals}/{first['id']}/items/{started['id']}", data={'status': 'in_progress'})
        long_text = 'https://example.com/' + 'averylongpathsegment' * 5
        for text in ('和负责人确认验收', '写设计文档', '补充回归测试', long_text):
            api.post(f"{goals}/{first['id']}/items", data={'text': text})
        last = api.post(goals, data={'text': '窄屏：最后一个目标'}).json()['data']['goal']
        created.append(last['id'])
        api.post(f"{goals}/{last['id']}/items", data={'text': '最后一项'})

        _open(page, size=None)
        last_card = page.locator('#mydayTasks .myday-goal', has_text='窄屏：最后一个目标')
        expect(last_card).to_have_count(1)
        _shot(page, '11-phone')

        assert _line_count(page, '实现目标存储') == 1, 'a short item breaks across lines beside its badges'
        row = _row(page, 'https://example.com/')
        assert row.evaluate('(row) => row.scrollWidth <= row.clientWidth'), 'a long item overflows its row'
        assert page.locator('#mydayTasksScroll').evaluate('(el) => el.scrollWidth <= el.clientWidth')

        box = page.locator('#mydayTasksScroll').bounding_box()
        visible_bottom = min(box['y'] + box['height'], page.viewport_size['height'])
        page.mouse.move(box['x'] + box['width'] / 2, (box['y'] + visible_bottom) / 2)
        page.mouse.wheel(0, 4000)  # a hand scroll: overflow:hidden ancestors never move
        expect(last_card.get_by_role('button', name='归档')).to_be_in_viewport()
        expect(page.locator('#mydayCalHeader .mcal-nav').first).to_be_in_viewport()
        _shot(page, '12-phone-scrolled')
        errors = getattr(page, '_tofu_js_errors', [])
        assert not errors, errors
    finally:
        for goal_id in created:  # archived goals leave the board empty for the other journeys
            page.request.patch(f'{origin}/api/v4/myday/goals/{goal_id}', data={'state': 'archived'})
        context.close()
