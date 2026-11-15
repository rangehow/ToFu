"""Project settings open from every entry point, on the first open after a cold load and again warm.

The dialog's owner (conversation/ui/folder-settings.ts with its folder browser and
workspace rail) loads when the dialog first opens, not with the startup graph
(docs/FRONTEND_ARCHITECTURE.md, "First-paint ceiling"). Every case starts from a
freshly loaded page, so its first open has to fetch that owner; the second open
reuses it. The Coordinator row's settings path to Authorization and permissions
(CARRY-TASK-HISTORY-1) is checked in the same cold page as a Project settings open.
"""
import uuid

import pytest

from tests.e2e.test_e2e_smoke import _wait_app_ready
from tests.e2e.test_folder_settings import _dialog, _name

pytestmark = pytest.mark.visual
expect = pytest.importorskip('playwright.sync_api').expect


def _tab(page, folder_id):
    return page.locator(f'#folderTabs .folder-tab[data-folder-id="{folder_id}"]')


def _menu(page, folder_id):
    _tab(page, folder_id).click(button='right')
    page.locator('.folder-tab-menu').wait_for(state='visible')


def _keyboard_menu(page, folder_id):
    _tab(page, folder_id).focus()
    page.keyboard.press('Shift+F10')
    expect(page.locator('.folder-tab-menu .folder-tab-menu-item[data-action="rename"]')).to_be_focused()


def _row_settings_button(page):
    return page.locator('#projectCoordinatorEntry .coordination-leader').get_by_role(
        'button', name='Project settings', exact=True)


def _coordinator_row(page, folder_id):
    _tab(page, folder_id).click()
    expect(_row_settings_button(page)).to_be_visible()


def _gear(page, folder_id):
    return page.locator(f'#folderTabs .folder-tab-settings[data-folder-config-id="{folder_id}"]')


# name: (folder exists, prepare(page, id), open(page, id), dialog kind)
ENTRY_POINTS = {
    'sidebar-quick-add': (False, None, lambda page, _: page.locator('#folderQuickAdd').click(), 'create'),
    'rail-add': (True, None, lambda page, _: page.locator('#folderTabs .folder-tab-add').click(), 'create'),
    'rail-settings-button': (True, lambda page, folder: _tab(page, folder).hover(),
                             lambda page, folder: _gear(page, folder).click(), 'settings'),
    'rail-double-click': (True, None, lambda page, folder: _tab(page, folder).dblclick(), 'settings'),
    'rail-menu-rename': (True, _menu, lambda page, _: page.locator(
        '.folder-tab-menu .folder-tab-menu-item[data-action="rename"]').click(), 'settings'),
    'rail-menu-delete': (True, _menu, lambda page, _: page.locator(
        '.folder-tab-menu .folder-tab-menu-item[data-action="delete"]').click(), 'delete'),
    'rail-keyboard-menu': (True, _keyboard_menu, lambda page, _: page.keyboard.press('Enter'), 'settings'),
    'sidebar-coordinator-row': (True, _coordinator_row,
                                lambda page, _: _row_settings_button(page).click(), 'settings'),
}


def _cold_page(page, monkeypatch, tmp_path, folder_name=None):
    """A freshly loaded app, with one folder that names no project directory when asked."""
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    folder_id = None
    if folder_name:
        response = page.request.post(f'{page.url.split("/?", 1)[0].rstrip("/")}/api/v4/folders',
                                     data={'name': folder_name})
        assert response.ok, response.text()
        folder_id = response.json()['data']['id']
    page.set_viewport_size({'width': 1280, 'height': 900})
    _reload_cold(page)
    return folder_id


def _reload_cold(page):
    from tests.conftest import _dismiss_onboarding_modals
    page.evaluate("() => { localStorage.setItem('tofu_ui_lang', 'en'); "
                  "localStorage.setItem('tofu_project_rail_collapsed', '0'); }")
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)


def _expect_dialog(page, kind, folder_name):
    dialog = _dialog(page)
    dialog.wait_for(state='visible', timeout=15000)
    if kind == 'create':
        expect(_name(dialog)).to_have_value('')
    elif kind == 'settings':
        expect(_name(dialog)).to_have_value(folder_name)
    else:
        expect(dialog.locator('.folder-settings-delete-name')).to_have_text(folder_name)
    expect(page.locator('dialog:modal')).to_have_count(1)


def _close(page):
    page.keyboard.press('Escape')
    page.wait_for_function("!document.querySelector('dialog:modal')")


def _open_counting_scripts(page, open_dialog, folder_id):
    scripts = []

    def record(request):
        if request.resource_type == 'script':
            scripts.append(request.url)

    page.on('request', record)
    try:
        open_dialog(page, folder_id)
        _dialog(page).wait_for(state='visible', timeout=15000)
        return scripts
    finally:
        page.remove_listener('request', record)


@pytest.mark.parametrize('entry', sorted(ENTRY_POINTS))
def test_project_settings_open_from_every_entry_point_cold_and_warm(page, monkeypatch, tmp_path, entry,
                                                                     assert_no_js_errors):
    needs_folder, prepare, open_dialog, kind = ENTRY_POINTS[entry]
    folder_name = f'Entry {entry} {uuid.uuid4().hex[:6]}' if needs_folder else None
    folder_id = _cold_page(page, monkeypatch, tmp_path, folder_name)
    for attempt in ('cold', 'warm'):
        if prepare:
            prepare(page, folder_id)
        scripts = _open_counting_scripts(page, open_dialog, folder_id)
        _expect_dialog(page, kind, folder_name)
        if attempt == 'cold':
            # The first open after the load fetches the dialog's owner on demand.
            assert scripts, f'{entry}: the first open loaded no module'
        _close(page)


def test_coordinator_settings_reach_authorization_and_project_settings_open_cold(
        page, monkeypatch, tmp_path, assert_no_js_errors):
    """负责人设置 (Coordinator settings) opens the project workspace's Authorization and
    permissions with no conversation of the project open, and Project settings then
    opens for the first time in that page."""
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.settings_store import set_conversation_settings
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Gamma').mkdir()
    project = normalize_project_path(str(tmp_path / 'Gamma'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Gamma', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    unbound = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'entry-points-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': unbound['revision']})
    assert created.ok, created.text()
    set_conversation_settings(created.json()['data']['conversationId'], {'archived': True}, user_id=1)

    page.set_viewport_size({'width': 1440, 'height': 960})
    _reload_cold(page)
    _tab(page, folder_id).click()
    entry = page.locator('#projectCoordinatorEntry .coordination-leader')
    entry.get_by_role('button', name='Coordinator settings', exact=True).click()
    workspace = page.locator('dialog[open]')
    workspace.get_by_role('tab', name='Advanced', exact=True).click()
    workspace.locator('details.coordination-authorization > summary').click()
    expect(workspace.locator('details.coordination-authorization')).to_have_attribute('open', '')
    expect(workspace.locator('[data-testid="project-dispatch"]')).to_contain_text(
        'Only the available, current coordinator conversation can authorize or review consultations.')
    workspace.get_by_role('button', name='Back to conversation', exact=True).click()
    expect(workspace).to_have_count(0)

    _tab(page, folder_id).hover()
    scripts = _open_counting_scripts(page, lambda p, f: _gear(p, f).click(), folder_id)
    _expect_dialog(page, 'settings', 'Gamma')
    assert scripts, 'the first Project settings open loaded no module'
    _close(page)
