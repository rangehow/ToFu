"""Task-board evidence copy on a private synthetic HTTP origin, never production."""
import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page  # noqa: F401
__all__ = ['frontend_visual_page']

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1440, 390])
def test_board_reports_are_not_execution_heartbeats(frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    entry = tmp_path / 'board-evidence.ts'
    entry.write_text(
        f"export {{ createCoordinatorProgress }} from '{ROOT}/frontend/src/features/coordinator-progress';\n"
        f"export {{ ready, setLanguage }} from '{ROOT}/frontend/src/i18n';\n"
    )
    page.add_script_tag(path=native_module_path('.native/board-evidence.js', entry))
    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/33-project-coordination.css'))
    page.evaluate('''async () => {
      await ready(); await setLanguage('en');
      document.documentElement.lang = 'en';
      document.body.replaceChildren();
      const reported = Date.UTC(2026, 10, 17, 9, 0);
      window.boardCard = createCoordinatorProgress({open:true, finishedOpen:false,
        toggle:()=>{}, toggleFinished:()=>{}, now:()=>reported + 3 * 3600000});
      window.boardView = {kind:'board', board:{revision:1, updatedAt:reported,
        summary:'Recorded task updates; not a live execution feed.', tasks:[
          {title:'Summarize the article',status:'pending',reason:'Upload the source article.'},
          {title:'Prepare release',status:'pending',progress:'Release checklist drafted.'},
          {title:'Compare platforms',status:'in_progress',progress:'Four platforms compared.'}
        ]}};
      window.boardActions = {history:()=>{}, retry:()=>{}};
      window.boardCard.render(window.boardView, window.boardActions);
      document.body.append(window.boardCard.el);
    }''')
    board = page.locator('.coordinator-progress')
    waiting = board.locator('[data-status=waiting]')
    pending = board.locator('[data-status=pending]')
    assert 'Waiting for: Upload the source article.' in waiting.inner_text()
    assert 'No start details recorded.' in pending.inner_text()
    assert 'Release checklist drafted.' in pending.inner_text()
    assert 'In progress' in board.locator('[data-status=in_progress]').inner_text()
    assert 'Updated' in board.locator('time').inner_text()
    assert '3 hours ago' in board.locator('time').inner_text()
    assert board.locator('.coordinator-progress-task time').count() == 0
    assert board.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
    page.screenshot(path=str(tmp_path / f'board-evidence-{width}.png'), full_page=True)
    page.evaluate('''() => {
      window.boardView.board.updatedAt -= 365 * 86400000;
      window.boardCard.render(window.boardView, window.boardActions);
    }''')
    assert board.locator('[data-status=waiting]').count() == 1
    assert board.locator('[data-status=pending]').count() == 1
    assert board.locator('[data-status=in_progress]').count() == 1
    assert '2025' in board.locator('time').inner_text()
    page.evaluate("window.boardCard.render({kind:'unavailable'}, window.boardActions)")
    assert board.locator('.coordinator-progress-task').count() == 0
    assert board.locator('time').count() == 0
    assert 'unavailable' in board.inner_text()
