"""Real app + isolated storage: the lead office (docs/PROJECT_COORDINATION.md "Lead office").

A lead's team view sits beneath its sidebar entry, where the conversations it
started used to open: a project folder shows its coordinator's floor (its own
office and one floor per Worker desk), the uncategorized view the main
coordinator's headquarters (one floor per project). The conversation keeps the
main view and the composer stays under its transcript. Folders, bindings, Worker
conversations and the Global Router are real server records; the assignment and
project-summary pages are served as fixed, contract-validated snapshots, since a
live server dispatches real grants on its own. No model or agent runs.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def _row(assignment_id, leader, recipient, brief, status, *, project, review=None):
    return {
        'assignmentId': assignment_id, 'grantRevision': 1, 'revoked': False, 'canonicalDelivery': False,
        'publication': None, 'delivery': None, 'review': review,
        'request': {'projectPath': project, 'coordinatorId': leader, 'recipientId': recipient, 'brief': brief},
        'execution': {'turnId': 'turn-' + assignment_id, 'attemptId': 'attempt-' + assignment_id, 'taskId': 'task-' + assignment_id,
                      'status': status, 'resultRevision': 1, 'result': {'content': ''}},
    }


def test_lead_office_sits_beneath_the_lead_entry(page, monkeypatch, tmp_path, assert_no_js_errors):
    from lib.project_dispatch.repository import _validate_schema
    from lib.router.contract import validate
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router-homes'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    output = Path(__file__).resolve().parents[2] / 'output/playwright/lead-office'
    output.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    projects = {}
    for name, color in [('Atlas', '#2f7d6b'), ('Beacon', '#b5642d'), ('Comet', '#5a5f8c')]:
        (tmp_path / name).mkdir()
        response = page.request.post(origin + '/api/v4/folders', data={
            'name': name, 'color': color, 'projectPaths': [str(tmp_path / name)]})
        assert response.ok, response.text()
        folder = response.json()['data']
        root = folder['projectPaths'][0]
        binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': root}).json()['data']
        assert binding['availability'] == 'available', binding
        projects[name] = {'folder': folder['id'], 'path': root, 'leader': binding['conversationId'], 'revision': binding['revision']}
    atlas = projects['Atlas']
    leader, project = atlas['leader'], atlas['path']
    builder, reviewer, failer, finisher = ['lead-office-' + kind + '-' + suffix for kind in ('builder', 'reviewer', 'failer', 'finisher')]
    for cid, title in [(builder, 'Build the parser'), (reviewer, 'Review the API design'),
                       (failer, 'Migrate the schema'), (finisher, 'Write the notes')]:
        seed_conversation(cid, title=title, settings={'folderId': atlas['folder'], 'projectPath': project, '_coordinatorSource': leader},
                          messages=[{'role': 'user', 'content': title}])
    seed_conversation('lead-office-own-' + suffix, title='My own notes', settings={'folderId': atlas['folder'], 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'My own notes'}])
    # Newest first, as the history page orders them.
    history = {'olderCursor': None, 'newerCursor': None, 'assignments': [
        _row('notes', leader, finisher, 'Write the release notes', 'completed', project=project,
             review={'decision': 'accept', 'note': 'Good notes', 'resultRevision': 1}),
        _row('migrate', leader, failer, 'Migrate the schema\nKeep old rows readable.', 'failed', project=project),
        _row('design', leader, reviewer, 'Review the API design', 'completed', project=project),
        _row('build', leader, builder, 'Write code for the parser\nCover empty input.', 'running', project=project),
    ]}
    _validate_schema(history, 'historyPage')
    summaries = {'nextCursor': None, 'projects': [
        {'projectPath': project, 'coordinatorConversationId': leader, 'bindingRevision': atlas['revision'], 'availability': 'available',
         'activeWorkers': 1, 'activeRequests': 1, 'workersCoverage': 'complete'},
        {'projectPath': projects['Beacon']['path'], 'coordinatorConversationId': projects['Beacon']['leader'],
         'bindingRevision': projects['Beacon']['revision'], 'availability': 'available', 'activeWorkers': 3, 'activeRequests': 2,
         'workersCoverage': 'complete', 'openQuestions': 1, 'blockingQuestions': 1, 'oldestQuestionAgeHours': 2,
         'lastReport': {'turnId': 'turn-beacon', 'kind': 'progress', 'title': 'Pricing page drafted', 'createdAt': 1, 'unread': True}},
        {'projectPath': projects['Comet']['path'], 'coordinatorConversationId': projects['Comet']['leader'],
         'bindingRevision': projects['Comet']['revision'], 'availability': 'available', 'activeWorkers': 0, 'activeRequests': 0,
         'workersCoverage': 'complete'},
    ]}
    validate(summaries, 'routerProjectsPage')
    served = {'assignments': 0, 'projects': 0}

    def substitute(route, data):
        # Keep the server's own v4 envelope and status; only the page body is fixed.
        response = route.fetch()
        envelope = response.json()
        assert response.ok and 'data' in envelope, (response.status, envelope)
        route.fulfill(response=response, json={**envelope, 'data': data})

    def assignments(route):
        query = parse_qs(urlparse(route.request.url).query)
        if query.get('path') != [project] or query.get('assignmentId'):
            return route.continue_()
        served['assignments'] += 1
        substitute(route, history)

    def router_projects(route):
        served['projects'] += 1
        substitute(route, summaries)

    page.route('**/api/v4/project/coordination/assignments?*', assignments)
    page.route('**/api/v4/router/projects*', router_projects)
    # The animated office belongs to the studio mode; the chat route keeps the sidebar in view.
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en"); localStorage.setItem("tofu_landing_workspace", "world");'
                  ' location.hash = "#/chat"')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{atlas["folder"]}"]').click()

    # The project's floor opens beneath its coordinator's entry, before any chat is open.
    entry = page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]')
    expect(entry).to_be_visible(timeout=20000)
    slot = page.locator('#projectCoordinatorEntry + .lead-office-slot')
    office = slot.locator('.lead-office')
    expect(office).to_have_attribute('data-altitude', 'floor', timeout=20000)
    expect(office).to_have_attribute('aria-busy', 'false')
    lead_room = office.locator('.evo-target[data-office="lead"]')
    expect(lead_room).to_have_attribute('data-conversation', leader)
    desks = office.locator('.evo-target[data-office="desk"]')
    expect(desks).to_have_count(3)
    # Oldest delegation first; the accepted notes left the office for the archive.
    assert desks.evaluate_all('(els) => els.map((el) => [el.dataset.state, el.dataset.conversation])') == [
        ['working', builder], ['review', reviewer], ['failed', failer]]
    # A desk is named by its Worker conversation's own title, not the brief's first line.
    expect(desks.first).to_have_attribute('aria-label', 'Build the parser · working · open the worker chat')
    assert served['assignments'] == 1
    # The office is the sidebar's width and the tower's aspect: a whole room, then floor bands.
    box = office.locator('.lead-office-stage').bounding_box()
    sidebar_box = page.locator('.coordination-conversation-column').bounding_box()
    assert box['width'] <= sidebar_box['width'], (box, sidebar_box)
    assert abs(box['height'] / box['width'] - (184 + 3 * 96) / 320) < 0.02, box
    assert page.locator('.lead-office canvas').evaluate('(canvas) => canvas.width > 0 && canvas.height > 0')
    # Every conversation the lead started stays reachable in the archive right below the office.
    archive = page.locator('#projectCoordinatorEntry + .lead-office-slot + .coordinator-conversations-host .coordinator-conversations')
    expect(archive.locator('.coordinator-conversations-count')).to_have_text('4')
    # The owner's own chats keep their list below.
    expect(page.locator('#convList .conv-item', has_text='My own notes')).to_be_visible()
    # The main view holds no office.
    expect(page.locator('main .lead-office')).to_have_count(0)
    page.wait_for_timeout(800)
    page.screenshot(path=str(output / 'floor-sidebar.png'), animations='disabled')

    # The lead's room opens the lead's chat; the composer sits right under its transcript.
    lead_room.focus()
    page.keyboard.press('Enter')
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(lead_room).to_have_attribute('aria-current', 'page')
    expect(page.locator('#chatContainer')).to_be_visible()
    expect(page.locator('#userInput')).to_be_visible()
    chat_box, composer_box = page.locator('.chat-wrapper').bounding_box(), page.locator('#userInput').bounding_box()
    assert chat_box['x'] <= composer_box['x'] and composer_box['x'] + composer_box['width'] <= chat_box['x'] + chat_box['width'] + 1

    # A desk opens its Worker's chat in the main view; the office stays, framing that room.
    office.locator('canvas').evaluate('(canvas) => { canvas.dataset.probe = "kept"; }')
    desks.first.focus()
    page.keyboard.press('Enter')
    page.wait_for_function('(id) => window.activeConvId === id', arg=builder)
    expect(desks.first).to_have_attribute('aria-current', 'page')
    expect(lead_room).not_to_have_attribute('aria-current', 'page')
    # The same office and stage: navigation never rebuilds the tower.
    expect(office.locator('canvas')).to_have_attribute('data-probe', 'kept')
    page.wait_for_timeout(800)
    page.screenshot(path=str(output / 'floor-worker-open.png'), animations='disabled')

    # The uncategorized view is the main coordinator's headquarters: one floor per project.
    router = page.request.post(origin + '/api/v4/router', headers={'Idempotency-Key': 'office-router-' + suffix}, data={})
    assert router.ok, router.text()
    router_id = router.json()['data']['conversationId']
    page.evaluate('(id) => sessionStorage.setItem("tofu_activeConvId", id)', router_id)
    page.reload()
    _dismiss_onboarding_modals(page)
    page.wait_for_function('(id) => window.activeConvId === id', arg=router_id, timeout=20000)
    page.locator('.folder-tab-uncat').click()
    hq = page.locator('#globalRouterEntry + .lead-office-slot .lead-office')
    expect(hq).to_have_attribute('data-altitude', 'headquarters', timeout=20000)
    expect(page.locator('#projectCoordinatorEntry + .lead-office-slot')).to_be_hidden()
    expect(hq.locator('.evo-target[data-office="lead"]')).to_have_attribute('aria-current', 'page')
    rooms = hq.locator('.evo-target[data-office="project"]')
    expect(rooms).to_have_count(3)
    assert rooms.evaluate_all('(els) => els.map((el) => el.getAttribute("aria-label"))') == [
        'Atlas · Working: 1 · open its project coordinator',
        'Beacon · Working: 3 · Asking you: 1 · New report · open its project coordinator',
        'Comet · no work in progress · open its project coordinator']
    page.wait_for_timeout(800)
    page.screenshot(path=str(output / 'headquarters-sidebar.png'), animations='disabled')

    # A project floor goes down to that project: its folder and its coordinator's chat.
    beacon = projects['Beacon']
    rooms.nth(1).focus()
    page.keyboard.press('Enter')
    page.wait_for_function('(id) => window.activeConvId === id', arg=beacon['leader'])
    expect(page.locator(f'.folder-tab[data-folder-id="{beacon["folder"]}"]')).to_have_class(re.compile(r'\bactive\b'))
    floor = page.locator('#projectCoordinatorEntry + .lead-office-slot .lead-office')
    expect(floor).to_have_attribute('data-altitude', 'floor', timeout=20000)
    expect(floor.locator('.evo-target[data-office="lead"]')).to_have_attribute('data-conversation', beacon['leader'])
    expect(floor.locator('.evo-target[data-office="lead"]')).to_have_attribute('aria-current', 'page')
    assert served['projects'] >= 1

    # The classic mode lists the lead's work under its row instead: no office, stage or canvas is built.
    page.evaluate('localStorage.setItem("tofu_landing_workspace", "chat")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.locator(f'.folder-tab[data-folder-id="{atlas["folder"]}"]').click()
    expect(entry).to_be_visible(timeout=20000)
    expect(archive.locator('.coordinator-conversations-count')).to_have_text('4', timeout=20000)
    expect(archive).to_be_visible()
    archive.locator('.coordinator-conversations-toggle').click()
    expect(archive.locator('.coordinator-conversations-body .conv-item')).to_have_count(4)
    page.wait_for_timeout(800)
    expect(slot).to_be_hidden()
    expect(page.locator('.lead-office')).to_have_count(0)
    expect(page.locator('.sidebar canvas, .coordination-conversation-column canvas')).to_have_count(0)
    page.screenshot(path=str(output / 'classic-sidebar.png'), animations='disabled')
