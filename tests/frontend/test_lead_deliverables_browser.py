"""Real app + isolated storage: a lead's deliverables on its task board (classic view) and in the studio office.

Owner request 2026-11-16: in the classic view a lead's deliverables were scattered through a long
conversation; in the studio, clicking a deliverable only opened the lead's classic conversation. Here a
real Coordinator publishes a deck and a file through the response-file publisher and a delivery
report through CoordinatorReports; the classic sidebar lists them newest first, projected from those
owners (no store of its own), a click opens the deliverable itself and "Go to message" opens the exact Turn; the studio's
desk opens the same report card. Owner request 2026-11-17: every report lists its kind and the files its
report_to_user call attached (chips, also on the report card in the conversation); an automatic
continuation brief reads as one folded row; and the reports and files moved from the sidebar onto the
task board, where each task expands to the reports that named it, with everything else under one other
group. Screenshots land in output/playwright/lead-deliverables/<label>/
(LEAD_DELIVERABLES_SHOT_LABEL, default "after").
"""
import json
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
    # An automatic continuation brief, then a progress report whose call attached a file: the
    # reply that made the call keeps the receipt naming it (lib/conversations/coordinator_tools.py).
    append_settled_turn(leader, user_id=1, command_id='brief-' + uuid.uuid4().hex[:8], actor='virtual_user',
                        kind='input', projection={'content': 'Continue the request. ' + 'Board state. ' * 400,
                                                  'origin': {'blockId': 'origin', 'initiator': 'brain'}})
    (tmp_path / 'Shop' / 'resume.md').write_text('# Resume\n\nFinal version.\n')
    attached = publish_file(str(tmp_path / 'Shop' / 'resume.md'), conv_id=leader, user_id=1, roots=[project])
    scope = {'owner': 1, 'projectPath': project, 'coordinatorId': leader, 'bindingRevision': binding['revision']}
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    CoordinatorTaskProgress(1, project).publish(scope, {'summary': 'Login fix shipped; resume in review.', 'tasks': [
        {'title': 'Polish the resume', 'status': 'in_progress', 'progress': 'Final pass.'},
        {'title': 'Fix the Safari login', 'status': 'completed', 'progress': 'Merged.'},
    ]})
    keys = CoordinatorTaskProgress(1, project).read()['taskKeys']
    tasks = {'resume': {'key': keys[0], 'title': 'Polish the resume'}, 'login': {'key': keys[1], 'title': 'Fix the Safari login'}}
    progress = CoordinatorReports(1, project).publish(scope, {
        'title': 'Resume ready', 'message': 'Final version attached.', 'kind': 'progress',
        'idempotency_key': 'progress-' + uuid.uuid4().hex[:8]})
    file_ref = {'artifactId': attached['id'], 'filename': 'resume.md', 'kind': 'document',
                'sizeBytes': attached['sizeBytes'], 'path': 'resume.md'}
    call = {'type': 'tool_use', 'blockId': 'tool:call-report', 'id': 'call-report', 'name': 'report_to_user',
            'input': '{}', 'result': {'status': 'done', 'content': json.dumps(
                {'conversationId': leader, 'turnId': progress['turnId'], 'published': True, 'files': [file_ref],
                 'task': tasks['resume']})}}
    append_settled_turn(leader, user_id=1, command_id='reply-' + uuid.uuid4().hex[:8], actor='assistant',
                        kind='message', projection={'content': 'Reported.', 'segments': [call]})
    receipt = CoordinatorReports(1, project).publish(scope,
        {'title': TITLE, 'message': '**Outcome**: login no longer drops on Safari.\n- Merged to main\n- Regression suite green',
         'kind': 'delivery', 'idempotency_key': 'delivery-' + uuid.uuid4().hex[:8]}, task=tasks['login'])
    delivered = {**call, 'blockId': 'tool:call-delivery', 'id': 'call-delivery', 'result': {'status': 'done', 'content': json.dumps(
        {'conversationId': leader, 'turnId': receipt['turnId'], 'published': True, 'task': tasks['login']})}}
    append_settled_turn(leader, user_id=1, command_id='reply-' + uuid.uuid4().hex[:8], actor='assistant',
                        kind='message', projection={'content': 'Delivered.', 'segments': [delivered]})
    return folder.json()['data']['id'], leader, receipt['turnId'], project, publishing[0]


def test_classic_task_board_expands_each_task_to_its_reports_and_files(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests.conftest import _dismiss_onboarding_modals
    folder_id, leader, report_turn, project, deck_turn = _seed(page, tmp_path, monkeypatch)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.evaluate('() => { localStorage.setItem("tofu_ui_lang", "en"); localStorage.setItem("tofu_landing_workspace", "chat"); }')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    expect(page.locator('#projectCoordinatorEntry .coordination-leader')).to_be_visible()
    # The sidebar holds the lead's entry and its Worker list only.
    expect(page.locator('#sidebar .lead-deliverables, #sidebar .lead-deliverable')).to_have_count(0)
    _settle(page)
    page.locator('#sidebar').screenshot(path=str(OUTPUT / 'classic-sidebar.png'), animations='disabled')

    page.locator('#projectCoordinatorEntry .coordination-leader').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    board = page.locator('#presenceStrip .coordinator-progress')
    expect(board).to_be_visible()
    if board.get_attribute('open') is None:
        board.locator(':scope > summary').click()
    # The in-progress task names its one report; the pending-less finished task its delivery.
    resume = board.locator('.coordinator-progress-task-toggle', has_text='Polish the resume')
    expect(resume.locator('.coordinator-progress-task-count')).to_have_text('1')
    expect(resume).to_have_attribute('aria-expanded', 'false')
    resume.click()
    expect(resume).to_have_attribute('aria-expanded', 'true')
    rows = board.locator(f'.coordinator-task-deliverables[data-task="{resume.get_attribute("data-task")}"] .lead-deliverable')
    expect(rows).to_have_count(1)
    expect(rows.first.locator('.lead-deliverable__kind')).to_have_text('Progress')
    expect(rows.first.locator('.lead-deliverable__file')).to_have_text('resume.md')
    _settle(page)
    board.screenshot(path=str(OUTPUT / 'board-task-expanded.png'), animations='disabled')
    # The file chip opens the file itself.
    rows.first.locator('.lead-deliverable__file').click()
    expect(page.locator('#artifactPanel, .artifact-panel').first).to_be_visible()
    page.keyboard.press('Escape')

    board.locator('.coordinator-progress-finished > summary').click()
    login = board.locator('.coordinator-progress-task-toggle', has_text='Fix the Safari login')
    expect(login.locator('.coordinator-progress-task-count')).to_have_text('1')
    login.click()
    delivery = board.locator(f'.coordinator-task-deliverables[data-task="{login.get_attribute("data-task")}"] .lead-deliverable')
    expect(delivery.locator('.lead-deliverable__kind')).to_have_text('Delivered')
    expect(delivery.locator('.lead-deliverable__title')).to_have_text(TITLE)
    # The deliverable itself: the readable report card, not the conversation.
    delivery.locator('.lead-deliverable__open').click()
    dialog = page.locator('dialog.lead-deliverable-dialog')
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-tool-card="report_to_user"]')).to_contain_text('no longer drops on Safari')
    _settle(page)
    page.screenshot(path=str(OUTPUT / 'classic-report-card.png'), animations='disabled')
    dialog.get_by_role('button', name='Go to message').click()
    expect(page.locator(f'#chatInner [data-turn-id="{report_turn}"], #chatInner [data-report-turn-id="{report_turn}"]').first).to_be_visible()

    # The deck and file published on their own read under the other group.
    other = board.locator('.coordinator-progress-other')
    other.locator('.coordinator-progress-task-toggle').click()
    others = other.locator('.lead-deliverable')
    expect(others).to_have_count(2)
    assert others.evaluate_all('rows => rows.map(row => row.dataset.kind)') == ['file', 'deck']
    _settle(page)
    board.screenshot(path=str(OUTPUT / 'board-other-expanded.png'), animations='disabled')
    others.nth(1).locator('.lead-deliverable__open').click()
    expect(page.locator('#artifactPanel, .artifact-panel').first).to_be_visible()
    page.keyboard.press('Escape')
    others.nth(1).locator('.lead-deliverable__source').click()
    expect(page.locator(f'#chatInner [data-turn-id="{deck_turn}"]').first).to_be_visible()

    # In the conversation the brief is one folded row and the progress report shows its file chip.
    brief = page.locator('#chatInner .conversation-machine-brief').first
    expect(brief).to_be_visible()
    expect(brief.locator('details.conversation-machine-brief__fold')).to_have_js_property('open', False)
    assert brief.bounding_box()['height'] < 80
    report_chip = page.locator('#chatInner .conversation-coordinator-report__file', has_text='resume.md')
    expect(report_chip).to_be_visible()
    report_chip.click()
    expect(page.locator('#artifactPanel, .artifact-panel').first).to_be_visible()


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
