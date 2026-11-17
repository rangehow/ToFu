"""Real app + isolated storage: the Coordinator's task board and each task's reports and files read in Chinese and English.

Owner request 2026-11-17: translate the Deliverables (汇报与文件) and Progress (任务进展) sections. Every label comes
from the i18n catalogs (the hard-coded copy gate in scripts/check_i18n_usage.mjs covers the components), and a report
the lead's conversation shows auto-translated is listed in that translation. The reports and files moved from
the sidebar onto the task board the same day: a task expands to its own. Screenshots land in
<SIDEBAR_I18N_SHOT_DIR or output/playwright/sidebar-i18n>/<language>-*.png.
"""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.test_lead_deliverables_browser import TITLE, _seed, _settle

pytestmark = pytest.mark.visual

OUTPUT = Path(os.environ.get('SIDEBAR_I18N_SHOT_DIR')
              or Path(__file__).resolve().parents[2] / 'output/playwright/sidebar-i18n')
TRANSLATED_TITLE = '登录问题已修复并交付'

COPY = {
    'zh': {'other': '其他汇报与文件', 'delivered': '交付', 'progress': '进展', 'source': '跳到原消息',
           'title': TRANSLATED_TITLE, 'board': '任务进展', 'active': '1 项进行中', 'history': '历史任务',
           'finished': '已结束 1 项', 'status': '进行中', 'age': r'此刻|分钟前'},
    'en': {'other': 'Other reports and files', 'delivered': 'Delivered', 'progress': 'Progress', 'source': 'Go to message',
           'title': TRANSLATED_TITLE, 'board': 'Task progress', 'active': 'In progress: 1', 'history': 'Task history',
           'finished': 'Finished: 1', 'status': 'In progress', 'age': r'this minute|minutes? ago'},
}


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_sidebar_reports_and_task_board_read_in_the_ui_language(page, monkeypatch, tmp_path, assert_no_js_errors, language):
    from lib.translate.commit import commit_translation_to_turn
    from tests.conftest import _dismiss_onboarding_modals
    folder_id, leader, report_turn, project, _ = _seed(page, tmp_path, monkeypatch)
    # The lead's conversation auto-translated the delivery report.
    commit_translation_to_turn(leader, report_turn, 'translatedContent',
                               f'{TRANSLATED_TITLE}\n\n**结果**：Safari 登录不再掉线。', user_id=1)
    copy = COPY[language]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.evaluate('(language) => { localStorage.setItem("tofu_ui_lang", language);'
                  ' localStorage.setItem("tofu_landing_workspace", "chat"); }', language)
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()

    row = page.locator('#projectCoordinatorEntry .coordination-leader')
    # An unread report previews in its display translation, as the conversation shows it.
    expect(row.locator('.coordination-leader-meta')).to_have_text(TRANSLATED_TITLE)
    # The task board pinned atop the Coordinator's conversation.
    row.click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    card = page.locator('.coordinator-progress')
    expect(card.locator('.coordinator-progress-title')).to_have_text(copy['board'])
    expect(card.locator('.coordinator-progress-tally')).to_have_text(copy['active'])
    if not card.evaluate('node => node.open'):
        card.locator('summary').first.click()
    expect(card.locator('.coordinator-progress-finished > summary')).to_contain_text(copy['finished'])
    expect(card.locator('.coordinator-progress-task[data-status="in_progress"] .coordinator-progress-glyph')).to_have_attribute(
        'aria-label', copy['status'])
    expect(card.get_by_role('button', name=copy['history'])).to_be_visible()
    # The finished task expands to its delivery, listed in the conversation's display translation.
    card.locator('.coordinator-progress-finished > summary').click()
    login = card.locator('.coordinator-progress-task-toggle', has_text='Fix the Safari login')
    login.click()
    rows = card.locator(f'.coordinator-task-deliverables[data-task="{login.get_attribute("data-task")}"] .lead-deliverable')
    expect(rows).to_have_count(1)
    expect(rows.first.locator('.lead-deliverable__kind')).to_have_text(copy['delivered'])
    expect(rows.first.locator('.lead-deliverable__title')).to_have_text(copy['title'])
    # The tooltip keeps the title as written beneath the translation.
    assert rows.first.locator('.lead-deliverable__open').get_attribute('title').split('\n')[:2] == [TRANSLATED_TITLE, TITLE]
    expect(rows.first.locator('.lead-deliverable__meta')).to_have_text(re.compile(r'^(' + copy['age'] + r')$'))
    expect(rows.first.locator('.lead-deliverable__source')).to_have_text(copy['source'])
    in_progress = card.locator('.coordinator-progress-task-toggle', has_text='Polish the resume')
    in_progress.click()
    expect(card.locator(f'.coordinator-task-deliverables[data-task="{in_progress.get_attribute("data-task")}"] '
                        '.lead-deliverable__kind')).to_have_text(copy['progress'])
    expect(card.locator('.coordinator-progress-other .coordinator-progress-task-toggle')).to_contain_text(copy['other'])
    _settle(page)
    page.screenshot(path=str(OUTPUT / f'{language}-task-board.png'), animations='disabled')
