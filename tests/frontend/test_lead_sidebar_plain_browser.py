"""Real app + isolated storage: the classic sidebar lists a lead's work as plain rows.

Owner report 2026-11-17: the classic view's sidebar embedded the animated lead
office (one office room per Worker) because the office followed the stored
landing choice, which is the studio by default. The studio (#/world) hides the
classic sidebar, so the sidebar never mounts an office scene: a lead's entry
row, its 成果 list and the archive of the conversations it started are plain
rows in every stored mode (docs/PROJECT_COORDINATION.md "Lead office"). Folders,
bindings and Worker conversations are real server records. No model runs.
"""
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def test_classic_sidebar_shows_lead_work_as_plain_rows_in_either_stored_mode(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router-homes'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    output = Path(__file__).resolve().parents[2] / 'output/playwright/lead-sidebar'
    output.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    (tmp_path / 'Atlas').mkdir()
    response = page.request.post(origin + '/api/v4/folders', data={
        'name': 'Atlas', 'color': '#2f7d6b', 'projectPaths': [str(tmp_path / 'Atlas')]})
    assert response.ok, response.text()
    folder = response.json()['data']
    project = folder['projectPaths'][0]
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    assert binding['availability'] == 'available', binding
    leader = binding['conversationId']
    for kind, title in [('builder', 'Build the parser'), ('reviewer', 'Review the API design'), ('failer', 'Migrate the schema')]:
        seed_conversation(f'lead-sidebar-{kind}-{suffix}', title=title,
                          settings={'folderId': folder['id'], 'projectPath': project, '_coordinatorSource': leader},
                          messages=[{'role': 'user', 'content': title}])
    seed_conversation('lead-sidebar-own-' + suffix, title='My own notes', settings={'folderId': folder['id'], 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'My own notes'}])

    entry = page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]')
    archive = page.locator('#projectCoordinatorEntry + .lead-deliverables-slot + .coordinator-conversations-host .coordinator-conversations')
    page.set_viewport_size({'width': 1440, 'height': 960})
    # 'world' is the default stored landing and the one the owner hit; 'chat' is the explicit classic choice.
    for landing in ('world', 'chat'):
        page.evaluate('(landing) => { localStorage.setItem("tofu_ui_lang", "en");'
                      ' localStorage.setItem("tofu_landing_workspace", landing); location.hash = "#/chat"; }', landing)
        page.reload()
        _dismiss_onboarding_modals(page)
        page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').click()
        expect(entry).to_be_visible(timeout=20000)
        expect(archive.locator('.coordinator-conversations-count')).to_have_text('3', timeout=20000)
        archive.locator('.coordinator-conversations-toggle').click()
        expect(archive.locator('.coordinator-conversations-body .conv-item')).to_have_count(3)
        expect(page.locator('#convList .conv-item', has_text='My own notes')).to_be_visible()
        page.wait_for_timeout(800)
        expect(page.locator('.lead-office, .lead-office-slot')).to_have_count(0)
        expect(page.locator('.sidebar canvas, .coordination-conversation-column canvas')).to_have_count(0)
        page.screenshot(path=str(output / f'classic-sidebar-landing-{landing}.png'), animations='disabled')

    # The studio keeps the classic sidebar out of view; returning to #/chat shows the plain rows again.
    page.evaluate('location.hash = "#/world"')
    page.wait_for_function('() => document.body.dataset.workspace === "world"', timeout=20000)
    expect(page.locator('.sidebar')).to_be_hidden()
    page.evaluate('location.hash = "#/chat"')
    page.wait_for_function('() => document.body.dataset.workspace === "chat"', timeout=20000)
    expect(entry).to_be_visible(timeout=20000)
    expect(page.locator('.lead-office, .lead-office-slot')).to_have_count(0)
