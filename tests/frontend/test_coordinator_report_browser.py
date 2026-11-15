"""Real app + isolated storage: a Coordinator report read inside its running reply.

Owner report 2026-11-11: a report the Coordinator published with report_to_user
while its reply was still running rendered below that reply as a separate
"项目大脑 · 答复" answer, and the right context card repeated the left sidebar's
Worker list. Here the reply runs through the real Turn lifecycle and task
projection (no model or worker), and the report through the real
CoordinatorReports publication, which stores it after the live reply with that
reply as its parent. Owner report 2026-11-12: open reports made the live reply
too long, so a report reads as one folded row that the reader opens. Screenshots
land in output/playwright/coordinator-report/.
"""
import json
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual

OUTPUT = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-report'
TITLE = '18:00 状态：重启改到 22:00'


def _context(project):
    """The round's context snapshot, shaped like a real Coordinator input Turn's."""
    return {'blockId': 'turn-context', 'snapshot': {
        'model': 'claude-opus-5.5', 'depth': '', 'modes': [],
        'roots': [{'path': project, 'ro': False, 'short': 'Reports'}],
        'tools': [{'label': 'Search ×N', 'tone': 'search'}, {'label': 'Fetch', 'tone': 'net'},
                  {'label': 'Code Exec', 'tone': 'code'}, {'label': 'Memory', 'tone': 'ai'}],
    }}


def _settle(page):
    page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')


class _RunningReply:
    """A reply whose attempt runs under a registry task, as an executor's would."""

    def __init__(self, conversation_id, request, *, settings=None, context=None):
        from lib import turn_lifecycle as turns
        from lib.tasks_pkg.manager import create_task
        projection = {'content': request, **({'contextSnapshot': context} if context else {})}
        pair = turns.create_turn_pair(
            conversation_id, user_id=1, command_id=f'{conversation_id}:{uuid.uuid4().hex[:8]}',
            input_projection=projection, config={},
            conversation_defaults={'allowCreate': True, 'title': request, 'settings': settings or {}})
        self.conversation_id = conversation_id
        self.request = pair['submittedTurn']['turnId']
        self.reply = pair['turn']['turnId']
        attempt = pair['attempt']['attemptId']
        self.task = create_task(conversation_id, [], {
            'model': 'fixture-model', '_turnId': self.reply, '_attemptId': attempt,
        }, user_id=1, supersede=False)
        assert turns.claim_attempt_start(attempt, user_id=1)
        turns.bind_task(attempt, self.task['id'], user_id=1)
        turns.mark_task_started(attempt, self.task['id'], user_id=1)
        self.task['status'] = 'running'
        self.task['toolRounds'] = []

    def start(self, name, call_id, args):
        from lib.tasks_pkg.manager import append_event
        round_num = len(self.task['toolRounds']) + 1
        row = {'toolName': name, 'toolCallId': call_id, 'roundNum': round_num,
               'llmRound': round_num - 1, 'status': 'searching', 'query': name, 'toolArgs': args}
        self.task['toolRounds'].append(row)
        append_event(self.task, {'type': 'tool_start', **row})
        return row

    def finish(self, row, content):
        from lib.tasks_pkg.manager import append_event
        row.update(status='done', toolContent=content)
        append_event(self.task, {'type': 'tool_complete', 'toolName': row['toolName'],
                                 'toolCallId': row['toolCallId'], 'roundNum': row['roundNum'],
                                 'llmRound': row['llmRound'], 'toolContent': content})

    def complete(self, text):
        from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
        self.task['content'] = text
        append_event(self.task, {'type': 'delta', 'content': text})
        stamp_chat_task_terminal(self.task, status='done', finish_reason='stop')
        append_event(self.task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(self.task)

    def discard(self):
        from lib.tasks_pkg.manager import discard_task
        discard_task(self.task['id'], self.conversation_id)


def test_running_coordinator_reply_reads_its_report_inline(page, monkeypatch, tmp_path, assert_no_js_errors):
    from lib import turn_lifecycle as turns
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.project_identity import normalize_project_path
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex[:12]
    (tmp_path / 'Reports').mkdir()
    project = normalize_project_path(str(tmp_path / 'Reports'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Reports', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    # R12 (docs/ROUTER.md): saving the folder provisioned its Coordinator.
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    leader = binding['conversationId']
    assert leader, binding

    running = []
    try:
        coordinator = _RunningReply(leader, '汇报一下进展', context=_context(project))
        running.append(coordinator)
        coordinator.finish(coordinator.start('get_project_workers', 'call-workers', {}), json.dumps({'workers': 2}))
        request_body = {'title': TITLE, 'message': '**重启**：20:00 赶不上，22:00 前给出结论。\n- 前端全部通过。',
                        'kind': 'progress', 'idempotency_key': 'report-' + suffix}
        publication = coordinator.start('report_to_user', 'call-report', request_body)
        receipt = CoordinatorReports(1, project).publish({
            'owner': 1, 'projectPath': project, 'coordinatorId': leader,
            'bindingRevision': binding['revision']}, request_body)
        coordinator.finish(publication, json.dumps(receipt))
        # A later call is still running, exactly as in the owner's screenshot.
        coordinator.start('send_project_message', 'call-message', {'content': 'Coordinator → Worker'})
        report, reply, request = receipt['turnId'], coordinator.reply, coordinator.request
        # Storage appends the report after the live reply and names it parent.
        assert turns.get_turn(leader, report, user_id=1)['parentTurnId'] == reply
        stored = turns.get_turn(leader, reply, user_id=1)
        assert stored['status'] == 'running'
        # Until the next segment checkpoint only the call's round holds the receipt.
        assert any(row.get('toolName') == 'report_to_user' and report in str(row.get('toolContent'))
                   for row in stored['projection']['toolRounds']), stored['projection']['toolRounds']

        page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
        page.reload()
        _dismiss_onboarding_modals(page)
        # The owner's screenshot width: the round's context card has its own track.
        page.set_viewport_size({'width': 2048, 'height': 1300})
        page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
        page.locator('#projectCoordinatorEntry .coordination-leader').click()
        page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
        chat = page.locator('#chatInner')
        reply_turn = chat.locator(f'.conversation-turn[data-turn-id="{reply}"]')
        expect(reply_turn).to_be_visible()
        expect(chat.get_by_text(TITLE).first).to_be_visible()
        card = page.locator(f'.conversation-turn[data-turn-id="{request}"] '
                            '[data-conversation-part="turn-context-card"]')
        expect(card).to_be_visible()

        # Workers progress while the owner watches: one finishes, one keeps running.
        # A row the browser never saw run has no Turn evidence and stays unlabeled.
        worker_settings = {'folderId': folder_id, 'projectPath': project, '_coordinatorSource': leader}
        finished = 'report-worker-done-' + suffix
        done_worker = _RunningReply(finished, '整理会议纪要', settings=worker_settings)
        running.append(done_worker)
        done_worker.finish(done_worker.start('read_files', 'call-notes', {'path': 'notes.md'}), 'notes')
        done_worker.complete('纪要已整理。')
        live = 'report-worker-live-' + suffix
        worker = _RunningReply(live, '调研民宿价格', settings=worker_settings)
        running.append(worker)
        worker.start('web_search', 'call-search', {'query': '民宿价格'})
        group = page.locator(f'.coordinator-conversations[data-coordinator-id="{leader}"]')
        toggle = group.locator('.coordinator-conversations-toggle')
        expect(toggle).to_be_visible()
        if toggle.get_attribute('aria-expanded') != 'true':
            toggle.click()
        expect(group.locator('.coordinator-conversations-body .conv-item')).to_have_count(2)
        live_row = group.locator(f'.conv-item[data-conv-id="{live}"]')
        finished_row = group.locator(f'.conv-item[data-conv-id="{finished}"]')
        # Settle the presence refresh and rail pass that follow those updates.
        page.wait_for_timeout(1000)
        _settle(page)

        OUTPUT.mkdir(parents=True, exist_ok=True)
        chat.get_by_text(TITLE).first.scroll_into_view_if_needed()
        _settle(page)
        page.screenshot(path=str(OUTPUT / 'desktop-2048.png'), animations='disabled')
        card.screenshot(path=str(OUTPUT / 'context-card-2048.png'), animations='disabled')
        page.locator('#sidebar').screenshot(path=str(OUTPUT / 'sidebar-2048.png'), animations='disabled')

        # The report reads inside the reply, after the call that published it.
        report_card = reply_turn.locator(f'.conversation-coordinator-report[data-report-turn-id="{report}"]')
        expect(report_card).to_be_visible()
        expect(report_card.locator('.conversation-coordinator-report__label')).to_have_text('负责人汇报 · 进展')
        expect(report_card).to_contain_text('22:00 前给出结论')
        expect(chat.locator(f'.conversation-turn[data-turn-id="{report}"]')).to_have_count(0)
        assert '项目大脑' not in chat.locator('.message-role').all_inner_texts()
        expect(report_card.locator('.conversation-answer-label')).to_have_count(0)
        assert reply_turn.evaluate('''(turn, report) => {
          const card = turn.querySelector(`.conversation-coordinator-report[data-report-turn-id="${report}"]`);
          const call = [...turn.querySelectorAll('[data-block-id]')]
            .find(node => node.dataset.blockId === 'tool:call-report');
          const live = turn.querySelector('[data-block-kind="live-status"]');
          const follows = (first, second) => Boolean(first && second
            && (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING));
          return {afterCall: follows(call, card), beforeLiveStatus: follows(card, live)};
        }''', report) == {'afterCall': True, 'beforeLiveStatus': True}

        # Folded by default: one row names the report, its title and time.
        fold = report_card.locator('details.conversation-coordinator-report__fold')
        head = fold.locator('summary.conversation-coordinator-report__head')
        body = fold.locator('[data-coordinator-report-body]')
        expect(fold).to_have_js_property('open', False)
        expect(head.locator('.conversation-coordinator-report__title')).to_have_text(TITLE)
        expect(head.locator('.conversation-coordinator-report__title')).to_be_visible()
        expect(head.locator('time')).to_be_visible()
        expect(body).to_be_hidden()
        reply_turn.screenshot(path=str(OUTPUT / 'reply-folded-2048.png'), animations='disabled')
        head.click()
        expect(body.get_by_text('22:00 前给出结论').first).to_be_visible()
        _settle(page)
        reply_turn.screenshot(path=str(OUTPUT / 'reply-open-2048.png'), animations='disabled')
        # The reply keeps working; its repaints leave the reader's choice alone.
        coordinator.start('get_project_workers', 'call-workers-again', {})
        expect(reply_turn.locator('[data-block-id="tool:call-workers-again"]')).to_have_count(1)
        expect(fold).to_have_js_property('open', True)
        expect(body).to_be_visible()
        head.click()
        expect(body).to_be_hidden()

        # The right card shows what the round ran with, not the Worker roster.
        expect(card).to_contain_text('Claude Opus 5.5')
        assert page.locator('.agent-dock[data-mode="coordinator"]').count() == 0
        assert '项目工作' not in card.inner_text()
        # The left roster keeps each Worker's status.
        expect(live_row.locator('.coordinator-conversations-status')).to_have_text('运行中')
        expect(finished_row.locator('.coordinator-conversations-status')).to_have_text('已完成')

        page.set_viewport_size({'width': 390, 'height': 900})
        page.evaluate('''() => {
          if (!document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar();
        }''')
        page.wait_for_function('document.getElementById("sidebar").getBoundingClientRect().right <= 1')
        report_card.scroll_into_view_if_needed()
        _settle(page)
        page.screenshot(path=str(OUTPUT / 'narrow-390.png'), animations='disabled')
        assert page.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    finally:
        for item in running:
            item.discard()
