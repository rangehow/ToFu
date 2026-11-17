"""Real app + isolated storage: the session-free Coordinator chat.

The leader journey starts from the Project Router a project save provisions
(docs/ROUTER.md R12) and uses real bind/navigation handlers, the real task
board store and report publication; the chat shows only the board. Its Task
history (历史任务) button lists every task the board ever showed; the project
workspace opens from the sidebar's Coordinator settings and reads real server
history rows. No model or background agent is invoked.
"""
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def test_create_independent_leader_and_keep_project_navigation_isolated(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    roots, folder_ids = [], []
    for name in ['Alpha', 'Beta']:
        root = tmp_path / name
        root.mkdir()
        roots.append(str(root))
        response = page.request.post(origin + '/api/v4/folders', data={
            'name': name, 'projectPaths': [str(root)],
        })
        assert response.ok, response.text()
        folder_ids.append(response.json()['data']['id'])
    ordinary = 'leader-journey-ordinary-' + suffix
    seed_conversation(ordinary, title='Ordinary discussion',
                      settings={'folderId': folder_ids[0], 'projectPath': roots[0]},
                      messages=[{'role': 'user', 'content': 'Ordinary discussion transcript'}])
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[0]}"]').click()
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    entry = page.locator('#projectCoordinatorEntry')
    # R12 (docs/ROUTER.md): saving Alpha provisioned its Project Router, so the slot
    # pins that Coordinator and offers no create step. The journey opens it the way
    # the owner does, from the pinned row: a background save never navigates.
    pinned = entry.locator('.coordination-leader')
    page.wait_for_function('document.querySelector("#projectCoordinatorEntry .coordination-leader")?.dataset.coordinatorId')
    leader = pinned.get_attribute('data-coordinator-id')
    assert leader and leader != ordinary
    pinned.click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': roots[0]})
    assert binding.ok, binding.text()
    assert binding.json()['data']['conversationId'] == leader
    assert page.locator(f'#convList .conv-item[data-conv-id="{leader}"]').count() == 0
    assert pinned.evaluate('(el) => !!(el.compareDocumentPosition(document.querySelector("#convList")) & Node.DOCUMENT_POSITION_FOLLOWING)')
    assert page.locator('[data-testid="collab-bar"]').count() == 0
    # Before the Coordinator writes a board, the chat says so instead of showing sessions.
    board = page.locator('#presenceStrip .coordinator-progress')
    expect(board).to_have_attribute('data-state', 'empty')
    # A Worker the Coordinator created is a session the human never has to track.
    worker = 'leader-journey-worker-' + suffix
    seed_conversation(worker, title='Research task',
                      settings={'folderId': folder_ids[0], 'projectPath': roots[0], '_coordinatorSource': leader},
                      messages=[{'role': 'user', 'content': 'Research task transcript'}])

    # The Coordinator's own board (real store + HTTP), written while another chat is open.
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_reports import CoordinatorReports
    scope = {'owner': 1, 'projectPath': roots[0], 'coordinatorId': leader,
             'bindingRevision': binding.json()['data']['revision']}
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    CoordinatorTaskProgress(1, roots[0]).publish(scope, {
        'summary': 'The folder dialog fix is verified; homestay research is underway.',
        'tasks': [
            {'title': 'Fix folder creation in the sidebar', 'status': 'completed', 'progress': 'Merged and verified in the browser.'},
            {'title': 'Research New Year homestays in Tianyu Creek', 'status': 'in_progress',
             'progress': 'Comparing Xiaohongshu, Ctrip, YourProvider and Douyin listings.'},
            {'title': 'Summarize the WeChat article', 'status': 'pending'},
        ]})
    page.wait_for_function('document.querySelector("#projectCoordinatorEntry .coordination-leader-meta")?.textContent === "In progress: 1"')
    # A real report reaches the sidebar by project push while another chat is open.
    reports = CoordinatorReports(1, roots[0])
    first_report = reports.publish(scope, {
        'title': 'Folder fix delivered', 'message': 'The sidebar folder dialog now creates and deletes folders.',
        'kind': 'delivery', 'idempotency_key': 'browser-delivery-1',
    })
    page.locator('#projectCoordinatorEntry .coordination-leader[data-unread="true"]').wait_for()
    assert reports.read()['unread'] is True
    assert page.locator('#presenceStrip .coordinator-progress').count() == 0

    # Opening the visible Coordinator chat reads its latest message, like any messaging app.
    entry.locator('.coordination-leader').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(board).to_have_attribute('data-state', 'board')
    # Folded to its one-line head: the sidebar already shows this work, so the chat keeps its space.
    assert board.get_attribute('open') is None
    page.locator('#projectCoordinatorEntry .coordination-leader:not([data-unread="true"])').wait_for()
    assert reports.read()['unread'] is False and reports.read()['latest']['turnId'] == first_report['turnId']
    expect(board.locator('.coordinator-progress-tally')).to_have_text('In progress: 1')
    expect(board.locator('.coordinator-progress-summary')).to_have_text(
        'The folder dialog fix is verified; homestay research is underway.')
    rows = board.locator('.coordinator-progress-content > .coordinator-progress-tasks > li')
    expect(rows.first).to_be_hidden()
    board.locator('summary.coordinator-progress-head').click()
    expect(rows.first).to_be_visible()
    assert rows.evaluate_all(
        '(rows) => rows.map((row) => [row.dataset.status, row.querySelector(".coordinator-progress-task-title").textContent])') == [
        ['in_progress', 'Research New Year homestays in Tianyu Creek'], ['pending', 'Summarize the WeChat article']]
    expect(board.locator('.coordinator-progress-finished > summary')).to_have_text('Finished: 1')
    # The Worker model is a compact footer control beside Task history, never a line of its own.
    expect(board.locator('.coordinator-progress-actions > .router-worker-model')).to_have_count(1)
    assert page.locator('#presenceStrip > .router-worker-model').count() == 0
    # The translation lane finishes after the board is on screen; its project hint repaints in place.
    original = board.get_by_role('button', name='Original', exact=True)
    expect(original).to_be_hidden()
    assert CoordinatorTaskProgress(1, roots[0]).attach_translation(scope, 'Chinese', {
        'The folder dialog fix is verified; homestay research is underway.': '文件夹对话框的修复已验证，民宿调研正在进行。',
        'Research New Year homestays in Tianyu Creek': '调研天屿溪的新年民宿',
    })
    expect(board.locator('.coordinator-progress-summary')).to_have_text('文件夹对话框的修复已验证，民宿调研正在进行。')
    expect(rows.first.locator('.coordinator-progress-task-title')).to_have_text('调研天屿溪的新年民宿')
    # A text without a translation is shown as written.
    expect(rows.nth(1).locator('.coordinator-progress-task-title')).to_have_text('Summarize the WeChat article')
    expect(original).to_have_attribute('aria-pressed', 'false')
    original.click()
    expect(board.locator('.coordinator-progress-summary')).to_have_text(
        'The folder dialog fix is verified; homestay research is underway.')
    expect(original).to_have_attribute('aria-pressed', 'true')
    expect(original).to_be_focused()
    original.click()
    expect(rows.first.locator('.coordinator-progress-task-title')).to_have_text('调研天屿溪的新年民宿')
    # Session-free: no sessions, references, graph, usage or report banner in the chat.
    strip = page.locator('#presenceStrip')
    for retired in ['.coordination-work-row', '.coordination-task-graph', '.coordination-reference',
                    '.coordinator-budget', '.coordination-report', '.coordinator-plan']:
        assert strip.locator(retired).count() == 0, retired
    assert 'Folder fix delivered' in page.locator('#chatInner').inner_text()
    # The board's one command is the task history (owner request 2026-11-11), not the workspace.
    assert board.get_by_role('button', name='Advanced', exact=True).count() == 0
    board.get_by_role('button', name='Task history', exact=True).click()
    history = page.locator('dialog[open].coordinator-history-drawer')
    expect(history.locator('.coordinator-history-task')).to_have_count(3)
    page.keyboard.press('Escape')
    expect(history).to_have_count(0)
    head = board.locator('summary.coordinator-progress-head')
    model = strip.locator('.router-worker-model')
    expect(model.locator('select')).to_be_visible()
    for theme, width in [('tofu', 1440), ('dark', 390), ('light', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        if width < 600:
            page.evaluate('() => { if (!document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')
            page.wait_for_function('document.getElementById("sidebar").getBoundingClientRect().right <= 1')
        if width >= 1024:
            # The Worker model is one compact row: its label beside the select.
            label, select = (model.locator(part).bounding_box() for part in ('label', 'select'))
            assert label['y'] < select['y'] + select['height'] and select['y'] < label['y'] + label['height']
        output = Path(__file__).resolve().parents[2] / 'output/playwright/project-coordinator'
        output.mkdir(parents=True, exist_ok=True)
        for state in ('open', 'folded'):
            if state == 'folded':
                head.click()
                expect(rows.first).to_be_hidden()
            assert board.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1'), state
            page.screenshot(path=str(output / f'{theme}-{width}-{state}.png'), animations='disabled')
            board.screenshot(path=str(output / f'board-{theme}-{width}-{state}.png'), animations='disabled')
        head.click()
        expect(rows.first).to_be_visible()
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.evaluate('() => { if (document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')
    # A message arriving while the Coordinator chat is open is read on arrival.
    second = reports.publish(scope, {'title': 'Research shortlist ready', 'message': 'Four homestays compared.',
                                     'kind': 'progress', 'idempotency_key': 'browser-progress-2'})
    page.wait_for_function('(text) => document.querySelector("#chatInner")?.innerText.includes(text)',
                           arg='Research shortlist ready')
    # The read command is asynchronous to the paint; poll the durable pointer it moves.
    for _ in range(50):
        state = reports.read()
        if state['latest']['turnId'] == second['turnId'] and state['unread'] is False:
            break
        page.wait_for_timeout(100)
    assert reports.read()['unread'] is False and reports.read()['latest']['turnId'] == second['turnId']
    # A non-blocking question is an answerable chat card that never expires.
    from lib import turn_lifecycle as turns
    question = 'Which nightly budget should the shortlist use?'
    turns.append_settled_turn(leader, command_id='browser-ask-' + suffix, actor='assistant', user_id=1, projection={
        'content': 'I will keep researching with under 500 RMB unless you say otherwise.',
        'segments': [
            {'type': 'tool_use', 'blockId': 'tool:call-ask', 'id': 'call-ask', 'name': 'ask_human',
             'input': {'question': question, 'response_type': 'choice', 'blocking': False},
             'result': {'status': 'done', 'content': 'Question shown to the user without waiting.'}},
            {'type': 'text', 'blockId': 'text:terminal',
             'text': 'I will keep researching with under 500 RMB unless you say otherwise.'},
        ],
        'toolRounds': [{'roundNum': 1, 'toolCallId': 'call-ask', 'toolName': 'ask_human', 'status': 'done',
                        'guidanceId': 'hg_browser_' + suffix[:8], 'guidanceQuestion': question,
                        'guidanceType': 'choice', 'guidanceBlocking': False,
                        'guidanceOptions': [{'label': 'Under 500 RMB'}, {'label': '500 to 1000 RMB'}]}],
    })
    page.reload()
    _dismiss_onboarding_modals(page)
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(board).to_have_attribute('data-state', 'board')
    # The settled turn's choice question renders natively and stays answerable.
    card = page.locator('#chatInner [data-block-id="tool:call-ask"] .rich-choices')
    expect(card).to_have_count(1)
    status = card.locator('[role=status]')
    expect(status).to_have_text('Work continues meanwhile; your reply is sent as a normal message.')
    expect(card.locator('fieldset')).to_be_enabled()
    # With an unsent draft, answering joins the draft as a quoted reply instead of sending.
    page.locator('#userInput').fill('Also check parking.')
    card.get_by_label('Under 500 RMB').check()
    card.locator('[data-rich-control="choices-submit"]').click()
    expect(page.locator('#userInput')).to_have_value('Also check parking.\n\nUnder 500 RMB')
    expect(page.locator('#reply-quote-container')).to_contain_text(question)
    expect(status).to_have_text('Your reply is in the composer — send it when ready')
    expect(card.locator('fieldset')).to_be_enabled()
    page.locator('#userInput').fill('')

    # A Worker chat keeps exactly one way back; the sidebar entry navigates to chat.
    # (The reload leaves no folder tab selected, so open it the way navigation does.)
    page.evaluate('(id) => loadConversation(id)', worker)
    page.wait_for_function('(id) => window.activeConvId === id', arg=worker)
    assert page.locator('#presenceStrip .coordinator-progress').count() == 0
    page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    assert page.locator('dialog[open]').count() == 0
    # Browsing Beta must not rewrite the active Alpha conversation's ownership.
    # A folder click must paint the entry inside its own handler: the list may
    # not sit empty (or jump) until the project binding read lands.
    pending_row = page.evaluate('''(id) => {
        document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row ? { busy: row.getAttribute('aria-busy'), coordinator: row.dataset.coordinatorId } : null;
    }''', folder_ids[1])
    assert pending_row == {'busy': 'true', 'coordinator': ''}
    # R12: Beta's save provisioned Beta's own Router, so its read settles on that
    # Coordinator rather than a create button, and it is never Alpha's leader.
    beta_leader = page.request.get(origin + '/api/v4/project/coordination',
                                   params={'path': roots[1]}).json()['data']['conversationId']
    assert beta_leader and beta_leader != leader
    page.wait_for_function('''(id) => {
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row && !row.hasAttribute('aria-busy') && row.dataset.coordinatorId === id;
    }''', arg=beta_leader)
    bound_row = page.evaluate('''(id) => {
        document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row ? { busy: row.getAttribute('aria-busy'), coordinator: row.dataset.coordinatorId } : null;
    }''', folder_ids[0])
    assert bound_row == {'busy': 'true', 'coordinator': leader}
    entry.locator('.coordination-leader').wait_for(state='visible')
    page.wait_for_function('(leader) => document.querySelector("#projectCoordinatorEntry .coordination-leader")?.dataset.coordinatorId === leader', arg=leader)
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    page.wait_for_function('!document.querySelector(".coordinator-progress")')
    expect(page.locator('#chatInner')).to_contain_text('Ordinary discussion transcript')
    assert page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).count() == 0


def test_coordinator_conversations_are_contained_in_a_collapsible_sidebar_group(page, monkeypatch, tmp_path, assert_no_js_errors):
    from lib import turn_lifecycle as turns
    from lib.conversations.project_identity import normalize_project_path
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Sidebar').mkdir()
    project = normalize_project_path(str(tmp_path / 'Sidebar'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Sidebar', 'projectPaths': [project]})
    folder_id = folder.json()['data']['id']
    owner = 'sidebar-owner-' + suffix
    legacy = 'sidebar-legacy-' + suffix
    empty = 'sidebar-empty-' + suffix
    seed_conversation(owner, title='Owner conversation', settings={'folderId': folder_id, 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'Owner conversation transcript'}])
    # New workers after initial render must create their Coordinator's group on
    # the authoritative catalog/push update, not depend on a preload refresh.
    live = 'sidebar-live-' + suffix
    # No _coordinatorSource: durable assignment history supplies this existing worker.
    seed_conversation(legacy, title='Legacy delegated worker', settings={'folderId': folder_id, 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'Legacy delegated worker transcript'}])
    seed_conversation(empty, title='Empty delegated shell', settings={'folderId': folder_id, 'projectPath': project,
                      '_coordinatorSource': 'placeholder'})
    # R12 (docs/ROUTER.md): the folder POST provisioned this project's Router, so it
    # is the leader. A create at its revision would now replace it, and the replaced
    # Router, which started no work, would list as an ordinary row: another scenario.
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    leader = binding['conversationId']
    assert leader, binding
    from lib.conversations.settings_store import update_conversation_settings
    assert update_conversation_settings(empty, lambda settings: settings.update({'_coordinatorSource': leader}), user_id=1) is not None
    pair = turns.create_turn_pair(leader, user_id=1, command_id='sidebar-source-' + suffix,
                                  input_projection={'content': 'Review the sidebar'},
                                  config={'projectPath': project})
    turns.bind_task(pair['attempt']['attemptId'], 'sidebar-source-task-' + suffix, user_id=1)
    turns.mark_task_started(pair['attempt']['attemptId'], 'sidebar-source-task-' + suffix, user_id=1)
    source = turns.get_turn(leader, pair['submittedTurn']['turnId'], user_id=1)
    repository = ProjectDispatchRepository(1, project)
    repository.authorize({'projectPath': project, 'coordinatorId': leader, 'recipientId': legacy,
        'authorizationSource': {'conversationId': leader, 'turnId': source['turnId'],
                                'projectionRevision': source['projectionRevision']},
        'brief': 'Legacy sidebar work', 'model': 'sidebar-model', 'budget': {}},
        PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=['chat']), 'sidebar-legacy-' + suffix)
    def _debug_flags(route):
        response = route.fetch()
        body = response.json()
        body.get('data', body)['feature_flags'] = {
            'debug_mode': True, 'optimizer_enabled': True}
        route.fulfill(response=response, json=body)

    page.route('**/api/v*/server-config**', _debug_flags)
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    seed_conversation(live, title='Live delegated worker', settings={'folderId': folder_id, 'projectPath': project,
                      '_coordinatorSource': leader}, messages=[{'role': 'user', 'content': 'Live delegated worker'}])
    # The group sits directly beneath its Coordinator's pinned entry.
    group = page.locator(f'#projectCoordinatorEntry + .coordinator-conversations-host '
                         f'.coordinator-conversations[data-coordinator-id="{leader}"]')
    toggle = group.locator('.coordinator-conversations-toggle')
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    expect(toggle).to_contain_text('Conversations')
    expect(toggle.locator('.coordinator-conversations-count')).to_have_text('3')
    assert page.locator(f'#convList .conv-item[data-conv-id="{legacy}"], #convList .conv-item[data-conv-id="{empty}"]').count() == 0
    expect(page.locator('.conv-date-group[data-group="today"] .conv-date-count')).to_have_text('1')
    toggle.focus()
    expect(toggle).to_be_focused()
    page.keyboard.press('Enter')
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    body = group.locator('.coordinator-conversations-body')
    expect(body.locator('.conv-item')).to_have_count(3)
    expect(body).to_contain_text('Legacy delegated worker')
    expect(body).to_contain_text('Empty delegated shell')
    expect(body).to_contain_text('Done')
    worker_row = body.locator(f'.conv-item[data-conv-id="{legacy}"]')
    copy_id = worker_row.locator('.conv-copy-id')
    expect(copy_id).to_have_attribute('data-conv-id', legacy)
    worker_row.hover()
    expect(copy_id).to_be_visible()
    active_before_copy = page.evaluate('window.activeConvId')
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    page.evaluate('navigator.clipboard.writeText("")')
    copy_id.click()
    page.wait_for_function('(id) => navigator.clipboard.readText().then(text => text === id)', arg=legacy)
    assert page.evaluate('window.activeConvId') == active_before_copy
    assert page.evaluate('(id) => localStorage.getItem(`tofu.coordinator.conversations.expanded.${id}`)', arg=leader) == 'true'
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-sidebar'
    output.mkdir(parents=True, exist_ok=True)
    for theme, width in [('light', 1440), ('dark', 1440), ('tofu', 1440), ('light', 320), ('dark', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        if width < 600:
            page.evaluate('''() => {
              const sidebar = document.getElementById("sidebar");
              if (sidebar.classList.contains("collapsed")) window.toggleSidebar();
            }''')
            page.wait_for_function('''() => {
              const bounds = document.querySelector("#sidebar").getBoundingClientRect();
              return bounds.left >= -1 && bounds.right > 0 &&
                !document.querySelector("#sidebar").classList.contains("collapsed");
            }''')
        page.locator('#sidebar').evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
        for expanded in [False, True]:
            if width < 600:
                page.evaluate('''() => {
                  if (document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar();
                }''')
                page.wait_for_function('''() => {
                  const bounds = document.querySelector("#sidebar").getBoundingClientRect();
                  return bounds.left >= -1 && bounds.right > 0 &&
                    !document.querySelector("#sidebar").classList.contains("collapsed");
                }''')
            if expanded != (toggle.get_attribute('aria-expanded') == 'true'):
                try:
                    toggle.click(timeout=5000)
                except Exception:
                    diagnostic = page.evaluate('''() => ({
                      width: innerWidth,
                      sidebar: (() => { const r = document.querySelector("#sidebar").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width,scrollWidth:document.querySelector("#sidebar").scrollWidth,clientWidth:document.querySelector("#sidebar").clientWidth,classes:document.querySelector("#sidebar").className}; })(),
                      button: (() => { const r = document.querySelector(".coordinator-conversations-toggle").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}; })(),
                      column: (() => { const r = document.querySelector(".coordination-conversation-column").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width,scrollWidth:document.querySelector(".coordination-conversation-column").scrollWidth,clientWidth:document.querySelector(".coordination-conversation-column").clientWidth}; })()
                    })''')
                    page.screenshot(path=str(output / 'mobile-click-failure.png'), animations='disabled')
                    raise AssertionError(diagnostic)
            expect(toggle).to_have_attribute('aria-expanded', str(expanded).lower())
            state = 'expanded' if expanded else 'collapsed'
            toggle.evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
            page.locator('#sidebar').evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
            page.screenshot(path=str(output / f'{theme}-{width}-{state}.png'), animations='disabled')
        assert page.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.reload()
    _dismiss_onboarding_modals(page)
    # The reload selects no folder; the group returns with its Coordinator's entry, still expanded.
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    expect(group).to_have_count(1)
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    expect(page.locator(f'.coordinator-conversations[data-coordinator-id="{leader}"]')).to_have_count(1)
    expect(page.locator('.coordinator-history-entry')).to_have_count(0)
    body.locator(f'.conv-item[data-conv-id="{empty}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=empty)
    page.locator('#sidebarSearchToggle').click()
    page.locator('#sidebarSearchInput').fill(legacy)
    found = page.locator(f'#convList .conv-item[data-conv-id="{legacy}"]')
    found.wait_for(state='visible')
    expect(found.locator('.conv-coordinator-badge')).to_have_text('Project coordinator')
    found.locator('.conv-title').click(position={'x': 4, 'y': 6})
    page.wait_for_function('(id) => window.activeConvId === id', arg=legacy)
    page.evaluate('(id) => loadConversation(id)', empty)
    page.wait_for_function('(id) => window.activeConvId === id', arg=empty)
    direct = page.request.get(origin + '/api/v4/conversations', params={'meta': 1})
    assert direct.ok, direct.text()
    payload = direct.json()
    rows = payload.get('items', payload.get('data', {}).get('items'))
    assert rows is not None, payload
    items = {row['id']: row for row in rows}
    # Stored creation provenance is catalog metadata. A legacy worker without
    # that marker is resolved by the paged project roster below, never by an
    # unbounded dispatch-history scan during every catalog refresh.
    assert '_coordinatorSource' not in items[legacy]['settings']
    assert items[empty]['settings']['_coordinatorSource'] == leader
    roster = page.request.get(origin + '/api/v4/project/coordination/conversations', params={'path': project})
    assert roster.ok
    assert {row['conversationId'] for row in roster.json()['data']['conversations']} >= {legacy, empty}
    # The group has its own scrolling body; history never buries the owner's rows.
    for index in range(20):
        seed_conversation(f'sidebar-history-{index}-{suffix}', title=f'Older delegated work {index}',
                          settings={'folderId': folder_id, 'projectPath': project, '_coordinatorSource': leader},
                          messages=[{'role': 'user', 'content': f'Older delegated work {index}'}])
    page.evaluate('(id) => loadConversation(id)', leader)
    expect(group.locator('.coordinator-conversations-count')).to_have_text('23')
    expect(body.locator('.conv-item')).to_have_count(23)
    assert body.evaluate('(el) => el.scrollHeight > el.clientHeight')
    assert page.locator('#sidebar').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')


def test_sidebar_paints_each_coordinator_once_across_folder_switches(page, monkeypatch, tmp_path, assert_no_js_errors):
    """A loading binding never repaints the current Coordinator as a second row,
    a replaced Coordinator keeps exactly one row with its own group, leaving a
    folder removes everything painted for it, and the fold chevron points right
    while collapsed and down while open."""
    from lib.conversations.project_identity import normalize_project_path
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    projects, folder_ids = [], []
    for name in ['Alpha', 'Beta', 'Gamma']:
        (tmp_path / name).mkdir()
        projects.append(normalize_project_path(str(tmp_path / name)))
        response = page.request.post(origin + '/api/v4/folders', data={'name': name, 'projectPaths': [projects[-1]]})
        assert response.ok, response.text()
        folder_ids.append(response.json()['data']['id'])

    def create_coordinator(index, key):
        binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': projects[index]})
        assert binding.ok, binding.text()
        created = page.request.post(origin + '/api/v4/project/coordination/create',
                                    headers={'Idempotency-Key': key + suffix},
                                    data={'path': projects[index], 'folderId': folder_ids[index],
                                          'expectedRevision': binding.json()['data']['revision']})
        assert created.ok, created.text()
        return created.json()['data']['conversationId']

    # A current-revision create replaces Alpha's first Coordinator; Beta has its own.
    replaced = create_coordinator(0, 'alpha-first-')
    current = create_coordinator(0, 'alpha-second-')
    beta = create_coordinator(1, 'beta-')
    workers = []
    for source, index in [(replaced, 0), (current, 0), (beta, 1)]:
        workers.append('worker-of-' + source)
        seed_conversation(workers[-1], title='Delegated by ' + source,
                          settings={'folderId': folder_ids[index], 'projectPath': projects[index],
                                    '_coordinatorSource': source},
                          messages=[{'role': 'user', 'content': 'Delegated work'}])
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.wait_for_function('(ids) => ids.every((id) => window.conversations.some((row) => row.id === id))',
                           arg=[replaced, current, beta, *workers])
    history = page.locator('.coordinator-history-entry .conv-item')
    groups = page.locator('.coordination-conversation-column .coordinator-conversations:not([hidden])')
    # Uncategorized holds none of these Coordinators, so it paints none of them.
    page.evaluate('() => renderConversationList()')
    painted = history.evaluate_all('(rows) => rows.map((row) => row.dataset.convId)')
    assert not set(painted) & {replaced, current, beta}

    leader_settled = '''(id) => {
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row && !row.hasAttribute('aria-busy') && row.dataset.coordinatorId === id;
    }'''

    def visit(folder_id, leader, replaced_rows, switches_project=True):
        # Until a switched project's binding answers, no conversation can be called a
        # replaced Coordinator; a folder of the already-resolved project paints in the same tick.
        painted = page.evaluate('''(id) => {
            document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
            return [...document.querySelectorAll('.coordinator-history-entry .conv-item')].map((row) => row.dataset.convId);
        }''', folder_id)
        assert painted == ([] if switches_project else replaced_rows)
        page.wait_for_function(leader_settled, arg=leader)
        expect(page.locator('#projectCoordinatorEntry + .coordinator-conversations-host '
                            '.coordinator-conversations-toggle')).to_be_visible()
        expect(history).to_have_count(len(replaced_rows))
        assert history.evaluate_all('(rows) => rows.map((row) => row.dataset.convId)') == replaced_rows
        expect(groups).to_have_count(1 + len(replaced_rows))
        listed = set(page.locator('#convList .conv-item').evaluate_all('(rows) => rows.map((row) => row.dataset.convId)'))
        assert not listed & {leader, *replaced_rows, *workers}
        # Every conversation here sits in a Coordinator group, so the folder is not called empty.
        expect(page.locator('#convList .folder-view-empty')).to_have_count(0)

    # Boot restores an arbitrary conversation; pin the start to an Alpha worker opened
    # outside its folder, which resolves Alpha's binding before Alpha's folder is chosen.
    page.evaluate('(id) => loadConversation(id)', workers[1])
    page.wait_for_function(leader_settled, arg=current)
    visit(folder_ids[0], current, [replaced], switches_project=False)
    visit(folder_ids[1], beta, [])
    visit(folder_ids[0], current, [replaced])

    toggle = page.locator('#projectCoordinatorEntry + .coordinator-conversations-host '
                          '.coordinator-conversations-toggle')
    pointing = '''(toggle) => {
        toggle.getAnimations({ subtree: true }).forEach((animation) => animation.finish());
        const chevron = toggle.querySelector('.coordinator-conversations-chevron');
        const values = chevron.querySelector('polyline').getAttribute('points').trim().split(/[\\s,]+/).map(Number);
        const [a, tip, b] = [0, 2, 4].map((index) => ({ x: values[index], y: values[index + 1] }));
        const transform = getComputedStyle(chevron).transform;
        const matrix = new DOMMatrix(transform === 'none' ? undefined : transform);
        const dx = tip.x - (a.x + b.x) / 2, dy = tip.y - (a.y + b.y) / 2;
        const x = matrix.a * dx + matrix.c * dy, y = matrix.b * dx + matrix.d * dy;
        return Math.abs(x) > Math.abs(y) ? (x > 0 ? 'right' : 'left') : (y > 0 ? 'down' : 'up');
    }'''
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert toggle.evaluate(pointing) == 'right'
    toggle.click()
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    assert toggle.evaluate(pointing) == 'down'
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-sidebar'
    output.mkdir(parents=True, exist_ok=True)
    page.locator('#sidebar').screenshot(path=str(output / 'replaced-coordinator-expanded.png'), animations='disabled')
    toggle.click()
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert toggle.evaluate(pointing) == 'right'
    page.locator('#sidebar').screenshot(path=str(output / 'replaced-coordinator-collapsed.png'), animations='disabled')
    visit(folder_ids[1], beta, [])
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[2]}"]').click()
    expect(page.locator('#convList .folder-view-empty')).to_be_visible()


def test_project_workspace_opens_from_coordinator_settings_with_server_history_rows(page, monkeypatch, tmp_path,
                                                                                   assert_no_js_errors):
    """The list projection omits request fields; the project workspace reads the server's own rows.

    Grants and reviews go through the dispatch repository and executions are
    settled Turns. The board's command is the task history, so the workspace
    opens from the sidebar's Coordinator settings, which a project shows while
    its Coordinator is unbound or unavailable (here: its chat was archived).
    """
    from lib import turn_lifecycle as turns
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.settings_store import set_conversation_settings
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository, _validate_schema
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Gamma').mkdir()
    project = normalize_project_path(str(tmp_path / 'Gamma'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Gamma', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    build, review = ['history-rows-' + kind + '-' + suffix for kind in ['build', 'review']]
    for cid, title in [(build, 'Parser worker'), (review, 'Review worker')]:
        seed_conversation(cid, title=title, settings={'folderId': folder_id, 'projectPath': project},
                          messages=[{'role': 'user', 'content': title + ' transcript'}])
    unbound = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'history-rows-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': unbound['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']

    def started_pair(conversation_id, label, content):
        pair = turns.create_turn_pair(conversation_id, user_id=1, command_id=label + suffix,
                                      input_projection={'content': content}, config={'projectPath': project})
        turns.bind_task(pair['attempt']['attemptId'], 'task-' + label + suffix, user_id=1)
        turns.mark_task_started(pair['attempt']['attemptId'], 'task-' + label + suffix, user_id=1)
        return pair

    source_pair = started_pair(leader, 'history-source-', 'Fix the parser and review its design')
    source = turns.get_turn(leader, source_pair['submittedTurn']['turnId'], user_id=1)
    repository = ProjectDispatchRepository(1, project)
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=['chat'])

    def grant(label, recipient, brief, **extra):
        return repository.authorize({
            'projectPath': project, 'coordinatorId': leader, 'recipientId': recipient,
            'authorizationSource': {'conversationId': leader, 'turnId': source['turnId'],
                                    'projectionRevision': source['projectionRevision']},
            'brief': brief, 'model': 'history-row-model', 'budget': {'maxTokens': 1000, 'maxModelRequests': 2},
            **extra}, principal, label + suffix)['id']

    def settle(assignment_id, recipient, label, content):
        pair = started_pair(recipient, label, 'Work on ' + assignment_id)
        assert turns.record_task_event({
            '_attemptId': pair['attempt']['attemptId'], '_userId': 1, 'id': 'task-' + label + suffix,
            'status': 'done', 'finishReason': 'stop', 'content': content, 'thinking': '', 'toolRounds': [],
            'config': {}}, {'type': 'done', 'finishReason': 'stop'})
        repository.bind_execution(assignment_id, pair['turn']['turnId'], pair['attempt']['attemptId'])

    def history(**query):
        response = page.request.get(origin + '/api/v4/project/coordination/assignments',
                                    params={'path': project, **query})
        assert response.ok, response.text()
        return response.json()['data']

    parser = grant('history-parser-', build, 'Fix the parser', profile={
        'mode': 'files', 'tools': ['read_files', 'edit_file'], 'readPaths': ['src'], 'writePaths': ['src'],
        'maxToolCalls': 8})
    settle(parser, build, 'history-parser-run-', 'Patched src/parser.py')
    design = grant('history-design-', review, 'Review the parser design')
    settle(design, review, 'history-design-run-', 'The design holds')
    notes = grant('history-notes-', review, 'Check the release notes')
    result = next(row for row in history()['assignments'] if row['assignmentId'] == parser)['execution']['resultRevision']
    repository.record_review(parser, 'rework', 'Cover empty input', result, 1)
    # The browser receives exactly the contract's list rows; an exact lookup keeps the whole grant.
    listed = history()
    _validate_schema(listed, 'historyPage')
    rows = {row['assignmentId']: row for row in listed['assignments']}
    assert {key: (row['execution'] or {}).get('status') for key, row in rows.items()} == {
        parser: 'completed', design: 'completed', notes: None}
    omitted = {'authorizationSource', 'model', 'budget'}
    assert not omitted & rows[parser]['request'].keys()
    assert omitted <= history(assignmentId=parser)['assignments'][0]['request'].keys()

    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    # Worker records never sit in the chat; the board's one command is the task history.
    board = page.locator('#presenceStrip .coordinator-progress')
    board.wait_for(state='visible')
    assert page.locator('#presenceStrip .coordination-task-graph').count() == 0
    board.locator('summary.coordinator-progress-head').click()
    expect(board.get_by_role('button', name='Task history', exact=True)).to_be_visible()
    assert board.get_by_role('button', name='Advanced', exact=True).count() == 0

    # An archived Coordinator chat leaves the binding unavailable; the sidebar row then
    # offers Coordinator settings, which opens the project workspace over the same rows.
    set_conversation_settings(leader, {'archived': True}, user_id=1)
    page.reload()
    _dismiss_onboarding_modals(page)
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    entry = page.locator('#projectCoordinatorEntry .coordination-leader')
    expect(entry).to_contain_text('The coordinator was removed, rebound or is unavailable.')
    entry.get_by_role('button', name='Coordinator settings', exact=True).click()
    workspace = page.locator('dialog[open]')
    workspace.get_by_role('tab', name='Advanced', exact=True).click()
    # The Worker records load when their folded authorization section opens (b5bcda81ec).
    workspace.locator('details.coordination-authorization > summary').click()
    dispatch = workspace.locator('[data-testid="project-dispatch"]')
    receipts = dispatch.locator('article')
    expect(receipts).to_have_count(3)
    assert sorted(receipts.locator('strong').all_inner_texts()) == sorted(
        ['Fix the parser', 'Review the parser design', 'Check the release notes'])
    # Authorizing, reviewing and rework belong to the available Coordinator's own chat: read-only here.
    expect(dispatch).to_contain_text(
        'Only the available, current coordinator conversation can authorize or review consultations.')
    assert dispatch.get_by_role('button', name='Prepare fresh rework authorization · draft only', exact=True).count() == 0
    expect(dispatch.get_by_role('button', name='Open responsible conversation', exact=True)).to_have_count(3)
    # The unavailable Coordinator is replaced from here.
    expect(workspace.get_by_role('button', name='Replace coordinator…', exact=True)).to_be_enabled()
    workspace.get_by_role('button', name='Back to conversation', exact=True).click()
    expect(workspace).to_have_count(0)


def test_coordinator_settings_open_the_project_workspace_while_no_coordinator_is_available(
        page, monkeypatch, tmp_path, assert_no_js_errors):
    """The board's 高级管理 became 历史任务; the project workspace keeps its sidebar entry.

    A project whose Coordinator chat was archived (unavailable) offers 负责人设置
    on its sidebar row, with no conversation of the project open; it opens the
    workspace over the project's own assignment receipts, read-only without an
    available Coordinator, with the replacement enabled.
    """
    from lib import turn_lifecycle as turns
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.settings_store import set_conversation_settings
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Delta').mkdir()
    project = normalize_project_path(str(tmp_path / 'Delta'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Delta', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    worker = 'settings-entry-worker-' + suffix
    seed_conversation(worker, title='Docs worker', settings={'folderId': folder_id, 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'Docs worker transcript'}])
    unbound = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'settings-entry-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': unbound['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']
    pair = turns.create_turn_pair(leader, user_id=1, command_id='settings-entry-source-' + suffix,
                                  input_projection={'content': 'Write the docs and check the changelog'},
                                  config={'projectPath': project})
    turns.bind_task(pair['attempt']['attemptId'], 'task-settings-entry-' + suffix, user_id=1)
    turns.mark_task_started(pair['attempt']['attemptId'], 'task-settings-entry-' + suffix, user_id=1)
    source = turns.get_turn(leader, pair['submittedTurn']['turnId'], user_id=1)
    repository = ProjectDispatchRepository(1, project)
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=['chat'])
    for label, brief in [('docs-', 'Write the docs'), ('changelog-', 'Check the changelog')]:
        repository.authorize({
            'projectPath': project, 'coordinatorId': leader, 'recipientId': worker,
            'authorizationSource': {'conversationId': leader, 'turnId': source['turnId'],
                                    'projectionRevision': source['projectionRevision']},
            'brief': brief, 'model': 'settings-entry-model', 'budget': {'maxTokens': 1000, 'maxModelRequests': 2}},
            principal, 'settings-entry-' + label + suffix)
    set_conversation_settings(leader, {'archived': True}, user_id=1)

    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    entry = page.locator('#projectCoordinatorEntry .coordination-leader')
    settings = entry.get_by_role('button', name='负责人设置', exact=True)
    expect(settings).to_be_visible()
    settings.click()
    workspace = page.locator('dialog[open]')
    expect(workspace.get_by_role('tab', name='高级管理', exact=True)).to_be_visible()
    workspace.get_by_role('tab', name='高级管理', exact=True).click()
    workspace.locator('details.coordination-authorization > summary').click()
    dispatch = workspace.locator('[data-testid="project-dispatch"]')
    receipts = dispatch.locator('article')
    expect(receipts).to_have_count(2)
    assert sorted(receipts.locator('strong').all_inner_texts()) == ['Check the changelog', 'Write the docs']
    # Read-only without an available Coordinator: no authorization from here, the replacement is.
    expect(dispatch).to_contain_text('仅当前可用的项目负责人对话能授权或审阅咨询。')
    expect(workspace.get_by_role('button', name='更换项目负责人…', exact=True)).to_be_enabled()
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-task-history'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / 'workspace-from-coordinator-settings-1440.png'), animations='disabled')
    page.keyboard.press('Escape')
    expect(workspace).to_have_count(0)


def test_coordinator_progress_writes_refresh_the_pinned_board_live(page, flask_app, monkeypatch, tmp_path,
                                                                   assert_no_js_errors):
    """Owner report 2026-11-08: the board changed only after leaving its chat and coming back.

    A Coordinator's tools run in the resident execution host, whose ``project``
    push reaches no browser socket; this journey drops that push the same way.
    A real Coordinator turn (stubbed model; real tools, Turn commits and
    conversation stream) writes progress with both tools while its chat stays
    open: the pinned board and the checklist rail show every write, as one list.
    """
    import json
    import threading

    import lib.tasks_pkg.llm_fallback._transport as transport
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.project_identity import normalize_project_path
    from lib.durable_worker.runtime import ensure_workers_started, stop_workers
    from tests.conftest import _dismiss_onboarding_modals
    from tests.support.model_routing import install_native_test_model_route
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr(CoordinatorTaskProgress, '_notify', lambda self: None)
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Live').mkdir()
    project = normalize_project_path(str(tmp_path / 'Live'))
    folder_id = page.request.post(origin + '/api/v4/folders', data={
        'name': 'Live', 'projectPaths': [project]}).json()['data']['id']
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'live-board-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': binding['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']

    board_write = {'summary': 'Release is underway; the notes come next.', 'tasks': [
        {'title': 'Release 0.19.0', 'status': 'in_progress', 'progress': 'Pushing to the three remotes.'},
        {'title': 'Write the release notes', 'status': 'pending'}]}
    checklist_write = {'todos': [
        {'content': 'Release 0.19.0', 'status': 'completed'},
        {'content': 'Write the release notes', 'status': 'in_progress'},
        {'content': 'Announce the release', 'status': 'pending'}]}
    gate, calls = threading.Event(), []
    usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}

    def model(task, body, tag='', on_tool_call_ready=None):
        if task.get('convId') != leader:
            return {'role': 'assistant', 'content': 'Title', 'tool_calls': []}, 'stop', usage
        step = task['_liveBoardStep'] = task.get('_liveBoardStep', 0) + 1
        calls.append(step)
        if step >= 3:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager._events import append_event
            text = 'Both writes are on the board.'
            with task['content_lock']:
                task['content'] += text
            append_event(task, build_event(EventType.DELTA, content=text))
            return {'role': 'assistant', 'content': text, 'tool_calls': []}, 'stop', usage
        if step == 2:
            # Hold the turn open until the browser has shown the first write.
            assert gate.wait(60), 'the page never showed the first board write'
        name, arguments = ('update_task_progress', board_write) if step == 1 else ('todo_write', checklist_write)
        call = {'id': f'call-live-board-{step}', 'index': 0, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}
        if on_tool_call_ready:
            on_tool_call_ready(call)
        return {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'tool_calls', usage

    install_native_test_model_route(owner_user_id=1, capabilities=('text', 'thinking'))
    saved = transport.stream_llm_response
    transport.stream_llm_response = model
    ensure_workers_started(flask_app)
    output = Path(__file__).resolve().parents[2] / 'output/playwright/project-coordinator-live'
    output.mkdir(parents=True, exist_ok=True)
    try:
        page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
        page.reload()
        _dismiss_onboarding_modals(page)
        page.set_viewport_size({'width': 1440, 'height': 960})
        page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
        page.locator('#projectCoordinatorEntry .coordination-leader[data-coordinator-id]').click()
        page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
        board = page.locator('#presenceStrip .coordinator-progress')
        expect(board).to_have_attribute('data-state', 'empty')
        selector = '#presetDropdown .preset-dropdown-item[data-value="stub-model"]'
        page.wait_for_selector(selector, state='attached', timeout=15000)
        page.locator('#presetToggle').click()
        page.locator(selector).first.click()
        # Any navigation away and back would also re-read the board; record it.
        page.evaluate('''(id) => { window.__leftCoordinator = false;
            window.__watchConversation = setInterval(() => {
                if (window.activeConvId !== id) window.__leftCoordinator = true; }, 20); }''', leader)
        page.locator('#userInput').fill('Keep the task board current while you release.')
        page.locator('#sendBtn').click()

        # update_task_progress: the board and the checklist rail show it while the turn runs.
        expect(board).to_have_attribute('data-state', 'board', timeout=30000)
        expect(board.locator('.coordinator-progress-summary')).to_have_text(board_write['summary'])
        expect(board.locator('.coordinator-progress-tally')).to_have_text('In progress: 1')
        rail = page.locator('.chat-container .user-msg[data-turn-id] > .turn-ctx [data-tctx-todo] .tctx-todo-item')
        expect(rail).to_have_count(2)
        assert sorted(rail.evaluate_all('(items) => items.map((item) => item.title)')) == [
            'Release 0.19.0', 'Write the release notes']
        board.locator('summary.coordinator-progress-head').click()
        page.screenshot(path=str(output / 'live-1-update_task_progress.png'), animations='disabled')
        # Still mid-turn: the model has not been answered past the first write.
        assert calls in ([1], [1, 2]) and not page.evaluate('window.__leftCoordinator')
        gate.set()

        # todo_write writes the same board: tasks replaced, the summary kept.
        titles = board.locator('.coordinator-progress-task-title')
        expect(titles).to_have_count(3, timeout=30000)
        expect(board.locator('.coordinator-progress-content > .coordinator-progress-tasks .coordinator-progress-task-title')
               ).to_have_text(['Write the release notes', 'Announce the release'])
        expect(board.locator('.coordinator-progress-summary')).to_have_text(board_write['summary'])
        expect(rail).to_have_count(3)
        assert sorted(rail.evaluate_all('(items) => items.map((item) => item.title)')) == sorted(
            item['content'] for item in checklist_write['todos'])
        page.locator('#chatInner').get_by_text('Both writes are on the board.').wait_for(timeout=30000)
        page.screenshot(path=str(output / 'live-2-todo_write.png'), animations='disabled')
        board.screenshot(path=str(output / 'live-2-board.png'), animations='disabled')
        assert not page.evaluate('window.__leftCoordinator')
        shown = CoordinatorTaskProgress(1, project).read()['board']
        assert shown['revision'] == 2 and [task['title'] for task in shown['tasks']] == [
            item['content'] for item in checklist_write['todos']]
    finally:
        gate.set()
        page.evaluate('() => clearInterval(window.__watchConversation)')
        transport.stream_llm_response = saved
        stop_workers(flask_app)


def test_task_history_keeps_every_task_the_board_ever_showed(page, monkeypatch, tmp_path, assert_no_js_errors):
    """Owner request 2026-11-11: replace the board's 高级管理 with 历史任务.

    Real store and HTTP: three board writes omit a completed, a cancelled and an
    unfinished task. The board's 历史任务 button lists all of them, grouped and
    filterable, with the dropped-unfinished marker and the met promise; a board
    write while it is open updates it in place. Chinese UI, wide and narrow.
    """
    from datetime import datetime, timedelta, timezone

    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.project_identity import normalize_project_path
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'History').mkdir()
    project = normalize_project_path(str(tmp_path / 'History'))
    folder_id = page.request.post(origin + '/api/v4/folders', data={
        'name': '历史', 'projectPaths': [project]}).json()['data']['id']
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'task-history-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': binding['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']
    scope = {'owner': 1, 'projectPath': project, 'coordinatorId': leader,
             'bindingRevision': created.json()['data']['revision']}
    due = (datetime.now(timezone.utc) + timedelta(hours=3)).astimezone().isoformat(timespec='seconds')
    board = CoordinatorTaskProgress(1, project)
    board.publish(scope, {'summary': '侧边栏修复已验证，民宿调研和发版在进行。', 'tasks': [
        {'title': '修复侧边栏的文件夹创建', 'status': 'completed', 'progress': '已合并，并在浏览器里验证过。'},
        {'title': '调研天屿溪的新年民宿', 'status': 'in_progress', 'progress': '正在比较小红书、携程和美团的房源。'},
        {'title': '发布 0.19.1', 'status': 'in_progress', 'progress': '正在推送三个远端。', 'due': due},
        {'title': '整理公众号文章要点', 'status': 'pending'},
        {'title': '迁移旧的统计脚本', 'status': 'pending'},
    ]})
    board.publish(scope, {'summary': '发版完成；文章要点在整理。', 'tasks': [
        {'title': '发布 0.19.1', 'status': 'completed', 'progress': '三个远端都已推送并核对。', 'due': due},
        {'title': '整理公众号文章要点', 'status': 'in_progress', 'progress': '拿到链接了，正在提炼。'},
        {'title': '迁移旧的统计脚本', 'status': 'cancelled', 'progress': '你说不再需要。'},
    ]})
    board.publish(scope, {'summary': '只剩文章要点和备份恢复。', 'tasks': [
        {'title': '整理公众号文章要点', 'status': 'in_progress', 'progress': '已提炼三条，正在核对出处。'},
        {'title': '恢复每日备份', 'status': 'pending'},
    ]})

    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    card = page.locator('#presenceStrip .coordinator-progress')
    expect(card).to_have_attribute('data-state', 'board')
    card.locator('summary.coordinator-progress-head').click()
    # The board itself shows only its current two tasks; the history keeps all six.
    expect(card.locator('.coordinator-progress-task')).to_have_count(2)
    assert card.get_by_role('button', name='高级管理', exact=True).count() == 0
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-task-history'
    output.mkdir(parents=True, exist_ok=True)
    card.screenshot(path=str(output / 'board-1440.png'), animations='disabled')
    card.get_by_role('button', name='历史任务', exact=True).click()
    drawer = page.locator('dialog[open].coordinator-history-drawer')
    rows = drawer.locator('.coordinator-history-task')
    expect(rows).to_have_count(6)

    def shown():
        return rows.evaluate_all('''(rows) => rows
            .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
            .map((row) => [row.querySelector('.coordinator-history-task-title').textContent,
                           row.dataset.dropped === 'true' ? 'dropped' : row.dataset.status])''')
    # Groups in order: in progress, pending, dropped unfinished, completed, cancelled.
    assert shown() == [['整理公众号文章要点', 'in_progress'], ['恢复每日备份', 'pending'],
                       ['调研天屿溪的新年民宿', 'dropped'], ['发布 0.19.1', 'completed'],
                       ['修复侧边栏的文件夹创建', 'completed'], ['迁移旧的统计脚本', 'cancelled']]
    expect(drawer.locator('.coordinator-history-group')).to_have_text(
        ['进行中1', '待开始1', '未完成即移出1', '已完成2', '已取消1'])
    dropped = rows.filter(has_text='调研天屿溪的新年民宿')
    expect(dropped.locator('[data-kind="dropped"]')).to_have_text('未完成即移出')
    expect(dropped.locator('.coordinator-history-task-note')).to_have_text('正在比较小红书、携程和美团的房源。')
    released = rows.filter(has_text='发布 0.19.1')
    expect(released.locator('[data-kind="due-met"]')).to_contain_text('按时完成')
    expect(rows.filter(has_text='整理公众号文章要点').locator('[data-kind="board"]')).to_have_text('在任务板上')
    expect(rows.filter(has_text='修复侧边栏的文件夹创建').locator('.coordinator-history-task-meta')).to_contain_text('首次出现')
    # The rows read like the board's: the same status glyphs.
    assert rows.locator('.coordinator-progress-glyph[role="img"]').count() == 6

    # Filters and search are server queries over the whole history.
    drawer.locator('[data-history-filter="dropped"]').click()
    expect(rows).to_have_count(1)
    assert shown() == [['调研天屿溪的新年民宿', 'dropped']]
    drawer.locator('[data-history-filter="all"]').click()
    expect(rows).to_have_count(6)
    drawer.get_by_role('searchbox').fill('远端')
    expect(rows).to_have_count(1)
    assert shown() == [['发布 0.19.1', 'completed']]
    drawer.get_by_role('searchbox').fill('')
    expect(rows).to_have_count(6)

    # A board write while the history is open updates it in place, without reopening.
    before = rows.filter(has_text='恢复每日备份').element_handle()
    board.publish(scope, {'summary': '文章要点已完成。', 'tasks': [
        {'title': '整理公众号文章要点', 'status': 'completed', 'progress': '已核对出处，发给你了。'},
        {'title': '恢复每日备份', 'status': 'pending'},
    ]})
    expect(rows.filter(has_text='整理公众号文章要点').locator('.coordinator-history-task-note')).to_have_text(
        '已核对出处，发给你了。')
    expect(drawer.locator('.coordinator-history-group')).to_have_text(['待开始1', '未完成即移出1', '已完成3', '已取消1'])
    # The unchanged row kept its DOM node: only rows whose content changed are rebuilt.
    assert rows.filter(has_text='恢复每日备份').evaluate('(row, kept) => row === kept', before)

    for theme, width in [('tofu', 1440), ('dark', 1440), ('light', 390), ('tofu', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        expect(rows).to_have_count(6)
        assert drawer.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1'), theme
        assert rows.evaluate_all('(rows) => rows.every((row) => row.scrollWidth <= row.clientWidth + 1)'), theme
        page.screenshot(path=str(output / f'history-{theme}-{width}.png'), animations='disabled')
    page.keyboard.press('Escape')
    expect(drawer).to_have_count(0)


def test_task_history_opens_fast_on_a_large_history(page, monkeypatch, tmp_path, assert_no_js_errors):
    """A long-lived Coordinator: 5,000 tasks over board writes of a full board each.

    Every board write replaces all its tasks (the open ones leave as dropped), through
    the real write path. Measured: the history's first-page server read (all groups, one
    group, a search), click to first rows in the drawer (cold chunk and warm), and the
    next page on scroll. Only the rows in view are mounted. The numbers are written
    beside the screenshots as load-time.json.
    """
    import json
    import math
    import statistics
    import time

    from lib.conversations.coordinator_progress import CONTRACT, CoordinatorTaskProgress
    from lib.conversations.project_identity import normalize_project_path
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Large').mkdir()
    project = normalize_project_path(str(tmp_path / 'Large'))
    folder_id = page.request.post(origin + '/api/v4/folders', data={
        'name': '长期项目', 'projectPaths': [project]}).json()['data']['id']
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'task-history-large-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': binding['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']
    scope = {'owner': 1, 'projectPath': project, 'coordinatorId': leader,
             'bindingRevision': created.json()['data']['revision']}
    per_board = CONTRACT['$defs']['progressRequest']['properties']['tasks']['maxItems']
    page_size = CONTRACT['$defs']['progressHistoryPage']['properties']['tasks']['maxItems']
    total = 5000
    statuses = ['in_progress', 'pending', 'completed', 'cancelled']
    board = CoordinatorTaskProgress(1, project)
    seeded = time.perf_counter()
    writes = math.ceil(total / per_board)
    for revision in range(writes):
        board.publish(scope, {'summary': f'第 {revision + 1} 轮任务。', 'tasks': [
            {'title': f'任务 {revision:03d}-{index:02d}：整理第 {revision * per_board + index} 项',
             'status': statuses[index % len(statuses)], 'progress': f'第 {revision + 1} 轮的进展说明。'}
            for index in range(per_board)]})
    seed_seconds = time.perf_counter() - seeded
    endpoint = origin + '/api/v4/project/coordination/progress/history'

    def timed(params):
        samples, page_body = [], None
        for _ in range(5):
            started = time.perf_counter()
            response = page.request.get(endpoint, params={'path': project, **params})
            samples.append((time.perf_counter() - started) * 1000)
            assert response.ok, response.text()
            page_body = response.json()['data']
        return {'medianMs': round(statistics.median(samples), 1), 'maxMs': round(max(samples), 1),
                'tasks': len(page_body['tasks']), 'counts': page_body['counts']}

    server = {'all': timed({}), 'dropped': timed({'group': 'dropped'}), 'search': timed({'q': '任务 100-'})}
    counts = server['all']['counts']
    assert sum(counts.values()) == writes * per_board
    assert server['all']['tasks'] == page_size

    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    card = page.locator('#presenceStrip .coordinator-progress')
    expect(card).to_have_attribute('data-state', 'board')
    card.locator('summary.coordinator-progress-head').click()
    opened = '''() => new Promise((resolve) => {
        const button = [...document.querySelectorAll('#presenceStrip .coordinator-progress button')]
            .find((item) => item.textContent === '历史任务');
        const started = performance.now();
        button.click();
        const check = () => {
            if (document.querySelector('dialog[open].coordinator-history-drawer .coordinator-history-task')) {
                resolve(performance.now() - started);
            } else requestAnimationFrame(check);
        };
        requestAnimationFrame(check);
    })'''
    cold = page.evaluate(opened)
    drawer = page.locator('dialog[open].coordinator-history-drawer')
    rows = drawer.locator('.coordinator-history-task')
    expect(drawer.locator('.coordinator-history-group').first).to_have_text(f'进行中{counts["in_progress"]}')
    # Virtualized: a first page of 64 tasks mounts only the rows in view.
    mounted = rows.count()
    assert 0 < mounted < page_size
    next_page = drawer.locator('.coordinator-history-viewport').evaluate('''(viewport) => new Promise((resolve) => {
        const content = viewport.querySelector('.coordinator-history-rows');
        const before = content.style.height;
        const started = performance.now();
        viewport.scrollTop = viewport.scrollHeight;
        const check = () => {
            if (content.style.height !== before && content.getAttribute('aria-busy') === 'false') {
                resolve(performance.now() - started);
            } else requestAnimationFrame(check);
        };
        requestAnimationFrame(check);
    })''')
    assert rows.count() < page_size
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-task-history'
    output.mkdir(parents=True, exist_ok=True)
    drawer.locator('.coordinator-history-viewport').evaluate('(viewport) => { viewport.scrollTop = 0; }')
    page.screenshot(path=str(output / 'large-history-1440.png'), animations='disabled')
    page.keyboard.press('Escape')
    expect(drawer).to_have_count(0)
    warm = page.evaluate(opened)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(rows.first).to_be_visible()
    page.screenshot(path=str(output / 'large-history-390.png'), animations='disabled')
    page.keyboard.press('Escape')
    expect(drawer).to_have_count(0)
    measured = {'tasks': writes * per_board, 'boardWrites': writes, 'tasksPerBoard': per_board,
                'seedSecondsAllWrites': round(seed_seconds, 1), 'counts': counts, 'server': server,
                'clickToFirstRowsMs': {'cold': round(cold, 1), 'warm': round(warm, 1)},
                'nextPageOnScrollMs': round(next_page, 1), 'rowsMountedOfFirstPage': mounted}
    (output / 'load-time.json').write_text(json.dumps(measured, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('task history load time:', json.dumps(measured, ensure_ascii=False))
