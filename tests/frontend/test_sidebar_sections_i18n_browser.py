"""Real app + isolated storage: a lead's reports-and-files panel and the Coordinator's task board read in Chinese and English.

Owner request 2026-11-17: translate the Deliverables (汇报与文件) and Progress (任务进展) sections. Every label comes
from the i18n catalogs (the hard-coded copy gate in scripts/check_i18n_usage.mjs covers the components), and a report
the lead's conversation shows auto-translated is listed in that translation. Screenshots land in
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
    'zh': {'section': '汇报与文件', 'delivered': '交付', 'progress': '进展', 'source': '跳到原消息',
           'title': TRANSLATED_TITLE, 'board': '任务进展', 'active': '1 项进行中', 'history': '历史任务',
           'finished': '已结束 1 项', 'status': '进行中', 'age': r'此刻|分钟前'},
    'en': {'section': 'Reports & files', 'delivered': 'Delivered', 'progress': 'Progress', 'source': 'Go to message',
           'title': TRANSLATED_TITLE, 'board': 'Task progress', 'active': 'In progress: 1', 'history': 'Task history',
           'finished': 'Finished: 1', 'status': 'In progress', 'age': r'this minute|minutes? ago'},
}


def _publish_board(project, leader, binding_revision):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    scope = {'owner': 1, 'projectPath': project, 'coordinatorId': leader, 'bindingRevision': binding_revision}
    CoordinatorTaskProgress(1, project).publish(scope, {'summary': 'Login fix shipped; release notes next.', 'tasks': [
        {'title': 'Write the release notes', 'status': 'in_progress', 'progress': 'Drafting.'},
        {'title': 'Fix the Safari login', 'status': 'completed', 'progress': 'Merged.'},
    ]})


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_reports_panel_and_task_board_read_in_the_ui_language(page, monkeypatch, tmp_path, assert_no_js_errors, language):
    from lib.translate.commit import commit_translation_to_turn
    from tests.conftest import _dismiss_onboarding_modals
    folder_id, leader, report_turn, project, _ = _seed(page, tmp_path, monkeypatch)
    origin = page.url.split('/?', 1)[0].rstrip('/')
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    _publish_board(project, leader, binding['revision'])
    # The lead's conversation auto-translated the delivery report.
    commit_translation_to_turn(leader, report_turn, 'translatedContent',
                               f'{TRANSLATED_TITLE}\n\n**结果**：Safari 登录不再掉线。', user_id=1)
    copy = COPY[language]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.evaluate('(language) => { localStorage.setItem("tofu_ui_lang", language);'
                  ' localStorage.setItem("tofu_landing_workspace", "chat"); }', language)
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1680, 'height': 900})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()

    row = page.locator('#projectCoordinatorEntry .coordination-leader')
    # An unread report previews in its display translation, as the conversation shows it.
    expect(row.locator('.coordination-leader-meta')).to_have_text(TRANSLATED_TITLE)
    _settle(page)
    page.locator('#sidebar').screenshot(path=str(OUTPUT / f'{language}-sidebar.png'), animations='disabled')

    # The reports and files sit in the right-hand panel of the lead's conversation (owner request 2026-11-17).
    row.click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(page.locator('#sidebar .lead-deliverables')).to_have_count(0)
    section = page.locator('#chatInner .lead-deliverables')
    expect(section.locator('.lead-deliverables__label')).to_have_text(copy['section'])
    rows = section.locator('.lead-deliverable')
    expect(rows).to_have_count(4)
    first = rows.first
    expect(first.locator('.lead-deliverable__kind')).to_have_text(copy['delivered'])
    expect(rows.nth(1).locator('.lead-deliverable__kind')).to_have_text(copy['progress'])
    expect(first.locator('.lead-deliverable__title')).to_have_text(copy['title'])
    # The tooltip keeps the title as written beneath the translation.
    assert first.locator('.lead-deliverable__open').get_attribute('title').split('\n')[:2] == [TRANSLATED_TITLE, TITLE]
    expect(section.locator('.lead-deliverables__scope')).to_have_text('Shop')
    expect(first.locator('.lead-deliverable__age')).to_have_text(re.compile(r'^(' + copy['age'] + r')$'))
    expect(first.locator('.lead-deliverable__source')).to_have_attribute('aria-label', copy['source'])
    page.wait_for_timeout(500)
    _settle(page)
    section.screenshot(path=str(OUTPUT / f'{language}-panel.png'), animations='disabled')

    # The task board pinned atop the Coordinator's conversation.
    card = page.locator('.coordinator-progress')
    expect(card.locator('.coordinator-progress-title')).to_have_text(copy['board'])
    expect(card.locator('.coordinator-progress-tally')).to_have_text(copy['active'])
    if not card.evaluate('node => node.open'):
        card.locator('summary').first.click()
    expect(card.locator('.coordinator-progress-finished > summary')).to_contain_text(copy['finished'])
    expect(card.locator('.coordinator-progress-task[data-status="in_progress"] .coordinator-progress-glyph')).to_have_attribute(
        'aria-label', copy['status'])
    expect(card.get_by_role('button', name=copy['history'])).to_be_visible()
    _settle(page)
    page.screenshot(path=str(OUTPUT / f'{language}-task-board.png'), animations='disabled')
