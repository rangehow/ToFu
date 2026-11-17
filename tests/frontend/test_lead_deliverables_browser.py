"""Real app + isolated storage: a lead's deliverables (成果, Deliverables) in the classic sidebar and the studio office.

Owner request 2026-11-16: in the classic view a lead's deliverables were scattered through a long
conversation; in the studio, clicking a deliverable only opened the lead's classic conversation. Here a
real Coordinator publishes a deck and a file through the response-file publisher and a delivery
report through CoordinatorReports; the classic sidebar lists them newest first, projected from those
owners (no store of its own), a click opens the deliverable itself and "Go to message" opens the exact Turn; the studio's
desk opens the same report card. Screenshots land in output/playwright/lead-deliverables/<label>/
(LEAD_DELIVERABLES_SHOT_LABEL, default "after").
"""
import os
import re
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual

OUTPUT = Path(__file__).resolve().parents[2] / 'output/playwright/lead-deliverables' / os.environ.get(
    'LEAD_DELIVERABLES_SHOT_LABEL', 'after')
TITLE = 'Login fix delivered'


def _settle(page):
    page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')


def _seed(page, tmp_path, monkeypatch):
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.project_identity import normalize_project_path
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    (tmp_path / 'Shop').mkdir()
    project = normalize_project_path(str(tmp_path / 'Shop'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Shop', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    leader = binding['conversationId']
    assert leader, binding
    from lib.artifacts.response_files import publish_file
    from lib.turn_lifecycle import append_settled_turn
    from pptx import Presentation
    Presentation().save(str(tmp_path / 'Shop' / 'Q4 review.pptx'))
    (tmp_path / 'Shop' / 'release-notes.md').write_text('# Release notes\n\n- Safari login fixed\n')
    # Published in order (deck, then file, then the delivery report), each by its own assistant Turn.
    publishing = []
    for name in ('Q4 review.pptx', 'release-notes.md'):
        published = publish_file(str(tmp_path / 'Shop' / name), conv_id=leader, user_id=1, roots=[project])
        publishing.append(append_settled_turn(leader, user_id=1, command_id='publish-' + uuid.uuid4().hex[:8], actor='assistant',
                            kind='message', projection={'content': f'Published {name}', 'toolRounds': [
                                {'results': [{'meta': {'artifactId': published['id'], 'responseFile': published}}]}]})['turn']['turnId'])
    receipt = CoordinatorReports(1, project).publish(
        {'owner': 1, 'projectPath': project, 'coordinatorId': leader, 'bindingRevision': binding['revision']},
        {'title': TITLE, 'message': '**Outcome**: login no longer drops on Safari.\n- Merged to main\n- Regression suite green',
         'kind': 'delivery', 'idempotency_key': 'delivery-' + uuid.uuid4().hex[:8]})
    return folder.json()['data']['id'], leader, receipt['turnId'], project, publishing[0]


def test_classic_sidebar_lists_and_opens_a_leads_deliverables(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests.conftest import _dismiss_onboarding_modals
    folder_id, leader, report_turn, project, deck_turn = _seed(page, tmp_path, monkeypatch)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.evaluate('() => { localStorage.setItem("tofu_ui_lang", "en"); localStorage.setItem("tofu_landing_workspace", "chat"); }')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    expect(page.locator('#projectCoordinatorEntry .coordination-leader')).to_be_visible()
    page.wait_for_timeout(800)
    _settle(page)
    page.locator('#sidebar').screenshot(path=str(OUTPUT / 'classic-sidebar.png'), animations='disabled')

    section = page.locator('#projectCoordinatorEntry + .lead-deliverables-slot .lead-deliverables')
    expect(section).to_be_visible()
    toggle = section.locator('.lead-deliverables__toggle')
    expect(toggle).to_contain_text('Deliverables')
    if toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()
    rows = section.locator('.lead-deliverable')
    expect(rows).to_have_count(3)
    # Newest first: the report, then the file, then the deck published first.
    assert rows.evaluate_all('rows => rows.map(row => row.dataset.kind)') == ['report', 'file', 'deck']
    expect(rows.first.locator('.lead-deliverable__title')).to_have_text(TITLE)
    # Title, project and time ("now" / "1 minute ago" for items just published).
    expect(rows.first.locator('.lead-deliverable__meta')).to_have_text(re.compile(r'^Shop · \S.*'))
    _settle(page)
    page.locator('#sidebar').screenshot(path=str(OUTPUT / 'classic-sidebar-expanded.png'), animations='disabled')

    # The deliverable itself: the readable report card, not the conversation.
    rows.first.locator('.lead-deliverable__open').click()
    dialog = page.locator('dialog.lead-deliverable-dialog')
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-tool-card="report_to_user"]')).to_contain_text('no longer drops on Safari')
    _settle(page)
    page.screenshot(path=str(OUTPUT / 'classic-report-card.png'), animations='disabled')
    # Its secondary action goes to the exact source Turn in the lead's conversation.
    dialog.get_by_role('button', name='Go to message').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(page.locator(f'#chatInner [data-turn-id="{report_turn}"], #chatInner [data-report-turn-id="{report_turn}"]').first).to_be_visible()
    _settle(page)
    page.screenshot(path=str(OUTPUT / 'classic-report-source-turn.png'), animations='disabled')

    # A published deck opens in the artifact panel.
    rows.nth(2).locator('.lead-deliverable__open').click()
    expect(page.locator('#artifactPanel, .artifact-panel').first).to_be_visible()
    _settle(page)
    page.screenshot(path=str(OUTPUT / 'classic-deck-panel.png'), animations='disabled')
    # And its link back to the message that published it.
    rows.nth(2).locator('.lead-deliverable__source').click()
    expect(page.locator(f'#chatInner [data-turn-id="{deck_turn}"]').first).to_be_visible()
    _settle(page)
    page.screenshot(path=str(OUTPUT / 'classic-deck-source-turn.png'), animations='disabled')


def test_studio_desk_opens_the_delivery_itself(browser, live_server, monkeypatch, tmp_path):
    from tests.conftest import _attach_js_error_capture, _dismiss_onboarding_modals
    from tests._jsdom import require_shipped_graph
    require_shipped_graph()
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    _attach_js_error_capture(page)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        _, leader, report_turn, _, _ = _seed(page, tmp_path, monkeypatch)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        page.evaluate('() => { localStorage.setItem("tofu_ui_lang", "en"); localStorage.setItem("tofu_landing_workspace", "world"); }')
        page.goto(live_server + '/#/world', wait_until='domcontentloaded')
        page.reload(wait_until='domcontentloaded')  # A hash change alone keeps the earlier locale.
        # The office is painted on the game stage's canvas; each desk document is mirrored by its
        # accessible hit button, which carries the document's kind.
        desk = page.locator('.evo-target[data-kind="deliverable"]').first
        desk.wait_for(timeout=30000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUTPUT / 'studio-desk.png'), animations='disabled')
        # A pointer click lands on the canvas, which hit-tests the document under it.
        box = desk.bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        dialog = page.locator('dialog.lead-deliverable-dialog')
        expect(dialog).to_be_visible()
        expect(dialog.locator('[data-tool-card="report_to_user"]')).to_contain_text('no longer drops on Safari')
        # Not the lead's conversation: the chat column stays closed.
        assert page.evaluate('document.body.dataset.conversationDock') != 'chat'
        _settle(page)
        page.screenshot(path=str(OUTPUT / 'studio-report-card.png'), animations='disabled')
        dialog.get_by_role('button', name='Go to message').click()
        page.wait_for_function("document.body.dataset.conversationDock === 'chat'")
        page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
        expect(page.locator(f'[data-turn-id="{report_turn}"], [data-report-turn-id="{report_turn}"]').first).to_be_visible()
        _settle(page)
        page.screenshot(path=str(OUTPUT / 'studio-source-turn.png'), animations='disabled')
    finally:
        context.close()
