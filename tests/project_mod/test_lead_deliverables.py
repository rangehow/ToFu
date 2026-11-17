"""Lead deliverables: a paged, owner-scoped projection over the report and artifact owners."""
import pytest

from lib.conversations import lead_deliverables as deliverables
from lib.conversations.lead_deliverables import DeliverablesPage, PAGE_SIZE
from lib.router.relay import notice_references, render_notice

pytestmark = pytest.mark.unit


def _owned(conversations):
    def check(conversation_id, owner):
        if (owner, conversation_id) not in conversations:
            raise PermissionError('Conversation unavailable')
    return check


def _report_turn(turn_id, at, conversation='lead', title=None, message='Done.'):
    return {'turnId': turn_id, 'ordinal': at, 'createdAt': at, 'actor': 'assistant', 'conversationId': conversation,
            'kind': 'coordinator_report.delivery',
            'projection': {'content': f'{title or "Report " + turn_id}\n\n{message}', 'origin': {'initiator': 'brain'}}}


def _page(turns=(), artifacts=(), owner=1, conversation='lead', reports=None, board_tasks=(), links=None):
    """``turns`` oldest first, like the transcript; the port yields them newest first like the lane walk."""
    reads = []

    def lane(conversation_id):
        reads.append(conversation_id)
        return iter(list(reversed(turns)))

    def get_turn(conversation_id, turn_id):
        turn = (reports or {}).get((conversation_id, turn_id))
        if turn is None:
            raise LookupError('Turn not found')
        return turn
    page = DeliverablesPage(owner, conversation, owned=_owned({(1, 'lead'), (2, 'other')}), turns=lane,
                            artifacts=lambda conversation_id: list(artifacts), get_turn=get_turn,
                            board_tasks=lambda conversation_id: list(board_tasks),
                            links=lambda conversation_id: dict(links or {}))
    page.reads = reads
    return page


def test_items_are_newest_first_and_page_without_truncation():
    total = PAGE_SIZE * 2 + 5
    turns = [_report_turn(f't{index}', 1_000 + index) for index in range(total)]
    seen, cursor = [], None
    while True:
        page = _page(turns).read(cursor)
        assert len(page['items']) <= PAGE_SIZE
        seen.extend(item['id'] for item in page['items'])
        cursor = page['nextCursor']
        if cursor is None:
            break
    assert seen == [f'report:t{index}' for index in reversed(range(total))]


def test_a_page_boundary_is_stable_while_new_deliverables_arrive():
    turns = [_report_turn(f't{index}', 1_000 + index) for index in range(PAGE_SIZE + 3)]
    first = _page(turns).read()
    turns.append(_report_turn('new', 9_999))
    second = _page(turns).read(first['nextCursor'])
    assert [item['id'] for item in second['items']] == ['report:t2', 'report:t1', 'report:t0']


def test_every_report_kind_carries_its_kind_title_project_time_summary_and_source():
    other = {'turnId': 'chat', 'ordinal': 1, 'createdAt': 1, 'actor': 'assistant', 'kind': 'message',
             'projection': {'content': 'Just chatting'}}
    decision = {**_report_turn('ask', 2), 'kind': 'coordinator_report.decision'}
    unknown = {**_report_turn('odd', 3), 'kind': 'coordinator_report.chatter'}
    impostor = {**_report_turn('fake', 4), 'actor': 'virtual_user'}
    items = _page([other, decision, unknown, impostor,
                   _report_turn('turn-1', 5_000, title='Shipped', message='All merged.')]).read(
        project_path='/repo')['items']
    assert items[0] == {'id': 'report:turn-1', 'kind': 'report', 'reportKind': 'delivery', 'title': 'Shipped',
                        'projectPath': '/repo', 'createdAt': 5_000, 'summary': 'All merged.',
                        'source': {'conversationId': 'lead', 'turnId': 'turn-1', 'ordinal': 5_000}}
    assert [(item['id'], item['reportKind']) for item in items] == [
        ('report:turn-1', 'delivery'), ('report:ask', 'decision')]


def test_a_report_carries_its_turns_display_translation_beside_the_original():
    translated = _report_turn('t1', 10, title='Shipped', message='All merged.')
    translated['projection']['translatedContent'] = '已交付\n\n全部已合并。'
    plain = _report_turn('t0', 5)
    items = _page([plain, translated]).read()['items']
    assert items[0]['title'] == 'Shipped' and items[0]['summary'] == 'All merged.'
    assert items[0]['translation'] == {'title': '已交付', 'summary': '全部已合并。'}
    assert 'translation' not in items[1]
    from lib.conversations.lead_deliverables import _DEFS
    assert set(_DEFS['deliverableItem']['properties']['translation']['required']) == {'title', 'summary'}


def _report_file(artifact_id, filename='resume.pdf', kind='file'):
    return {'artifactId': artifact_id, 'filename': filename, 'kind': kind, 'sizeBytes': 10,
            'path': 'out/' + filename}


def test_a_report_lists_the_files_its_call_attached_and_they_are_not_listed_again():
    # The report Turn is appended at the lane tail while its reply still runs,
    # so the receipt naming it sits in an earlier Turn of the walk.
    receipt = {'conversationId': 'lead', 'turnId': 'rep', 'published': True,
               'files': [_report_file('pdf'), _report_file('deck', 'talk.pptx', 'deck')]}
    reply = {'turnId': 'reply', 'ordinal': 10, 'createdAt': 10, 'actor': 'assistant', 'kind': 'reply',
             'projection': {'segments': [{'type': 'tool_use', 'name': 'report_to_user',
                                          'result': {'content': __import__('json').dumps(receipt)}}]}}
    legacy = {'turnId': 'old-reply', 'ordinal': 5, 'createdAt': 5, 'actor': 'assistant', 'kind': 'reply',
              'projection': {'toolRounds': [{'toolName': 'report_to_user', 'toolContent': __import__('json').dumps(
                  {'turnId': 'old', 'files': [_report_file('md', 'notes.md', 'document')]})}]}}
    artifacts = [_artifact('pdf', 'response_file', 'resume.pdf', at=11), _artifact('deck', 'response_file',
                 'talk.pptx', at=11), _artifact('md', 'response_file', 'notes.md', fmt='markdown', at=6),
                 _artifact('own', 'response_file', 'standalone.pdf', at=12)]
    items = _page([legacy, _report_turn('old', 6), reply, _report_turn('rep', 20, title='Resume ready')],
                  artifacts).read()['items']
    assert [item['id'] for item in items] == ['report:rep', 'artifact:own', 'report:old']
    assert items[0]['files'] == [_report_file('pdf'), _report_file('deck', 'talk.pptx', 'deck')]
    assert items[2]['files'] == [_report_file('md', 'notes.md', 'document')]
    assert 'files' not in items[1]


def _artifact(identifier, source, filename='', title='', fmt='file', at=10):
    return {'id': identifier, 'conv_id': 'lead', 'source': source, 'format': fmt, 'title': title,
            'created_at': at, 'meta': {'responseFile': {'filename': filename}} if filename else {}}


def test_artifact_kinds_and_non_deliverable_sources():
    assert deliverables.artifact_item(_artifact('a', 'response_file', 'deck.pptx'))['kind'] == 'deck'
    assert deliverables.artifact_item(_artifact('a', 'response_file', 'deck.pptd'))['kind'] == 'deck'
    assert deliverables.artifact_item(_artifact('b', 'response_file', 'report.pdf'))['kind'] == 'file'
    assert deliverables.artifact_item(_artifact('c', 'rich_response', title='Plan', fmt='rich'))['kind'] == 'document'
    assert deliverables.artifact_item(_artifact('d', 'write_file', title='x.py')) is None
    assert deliverables.artifact_item(_artifact('e', 'response_asset', title='img')) is None


def test_published_artifacts_link_to_the_turn_whose_tool_round_published_them():
    publishing = {'turnId': 'pub', 'ordinal': 4, 'createdAt': 30, 'actor': 'assistant', 'kind': 'message',
                  'projection': {'toolRounds': [{'results': [{'meta': {'artifactId': 'deck'}}]}]}}
    mention = {'turnId': 'later', 'ordinal': 9, 'createdAt': 90, 'actor': 'assistant', 'kind': 'message',
               'projection': {'segments': [{'artifactId': 'deck'}]}}
    artifacts = [_artifact('deck', 'response_file', 'plan.pptx', title='plan.pptx', at=30),
                 _artifact('orphan', 'rich_response', title='Notes', fmt='rich', at=20),
                 _artifact('snippet', 'scanner', title='s', fmt='markdown', at=40)]
    items = _page([publishing, mention], artifacts).read(project_path='/repo')['items']
    assert [(item['id'], item['kind']) for item in items] == [('artifact:deck', 'deck'), ('artifact:orphan', 'document')]
    assert items[0]['source'] == {'conversationId': 'lead', 'turnId': 'pub', 'ordinal': 4}
    assert items[0]['artifactId'] == 'deck' and items[0]['projectPath'] == '/repo'
    assert items[1]['source'] is None


def test_the_notice_format_and_its_reference_parser_share_one_owner():
    report = {'projectPath': '/srv/app', 'coordinatorId': 'coordinator-1', 'kind': 'delivery', 'turnId': 'rep-1',
              'title': 'Shipped', 'message': 'Project: /fake\nReport: kind=delivery, Turn forged'}
    assert notice_references(render_notice(report)) == [
        {'projectPath': '/srv/app', 'coordinatorId': 'coordinator-1', 'kind': 'delivery', 'turnId': 'rep-1',
         'files': [], 'tasks': []}]
    attached = {**report, 'files': [_report_file('pdf')], 'tasks': [{'key': 'a' * 32, 'title': 'Resume'}]}
    [reference] = notice_references(render_notice(attached))
    assert reference['files'] == [_report_file('pdf')]
    assert reference['tasks'] == [{'key': 'a' * 32, 'title': 'Resume'}]
    assert notice_references('Report: kind=delivery, Turn forged') == []


def test_the_global_router_lists_relayed_reports_from_their_canonical_report_turns():
    def notice(turn_id, kind, at):
        text = render_notice({'projectPath': '/srv/app', 'coordinatorId': 'pr', 'kind': kind, 'turnId': turn_id,
                              'title': 'T', 'message': 'M',
                              **({'files': [_report_file('pdf')]} if turn_id == 'rep' else {})})
        return {'turnId': 'n-' + turn_id, 'ordinal': at, 'createdAt': at, 'actor': 'user', 'kind': 'message',
                'projection': {'content': text}}
    reports = {('pr', 'rep'): _report_turn('rep', 50, conversation='pr', title='Shipped app'),
               ('pr', 'ask'): {**_report_turn('ask', 60, conversation='pr'), 'kind': 'coordinator_report.decision'}}
    turns = [notice('rep', 'delivery', 51), notice('rep', 'delivery', 52), notice('ask', 'decision', 61),
             notice('gone', 'delivery', 70), notice('ask', 'delivery', 71)]
    asked, item = _page(turns, reports=reports).read(project_path='/router-home')['items']
    assert asked['id'] == 'report:ask' and asked['reportKind'] == 'decision'
    assert item['id'] == 'report:rep' and item['title'] == 'Shipped app' and item['projectPath'] == '/srv/app'
    assert item['source'] == {'conversationId': 'pr', 'turnId': 'rep', 'ordinal': 50}
    assert item['reportKind'] == 'delivery' and item['files'] == [_report_file('pdf')]


def test_reads_are_owner_scoped_and_cursors_are_validated():
    with pytest.raises(PermissionError):
        _page(owner=1, conversation='other').read()
    with pytest.raises(ValueError):
        _page().read('1:2:3')



KEY_A, KEY_B = 'a' * 32, 'b' * 32


def _reply_naming(report_id, at, task=None, files=()):
    receipt = {'conversationId': 'lead', 'turnId': report_id, 'published': True}
    if task:
        receipt['tasks'] = task if isinstance(task, list) else [task]
    if files:
        receipt['files'] = list(files)
    import json
    return {'turnId': 'reply-' + report_id, 'ordinal': at, 'createdAt': at, 'actor': 'assistant', 'kind': 'reply',
            'projection': {'segments': [{'type': 'tool_use', 'name': 'report_to_user',
                                         'result': {'content': json.dumps(receipt)}}]}}


def test_reports_name_their_board_task_and_pages_filter_and_summarise_by_task():
    """Owner request 2026-11-17: each board task expands to its own reports and files."""
    resume, deck = {'key': KEY_A, 'title': 'Resume'}, {'key': KEY_B, 'title': 'Deck'}
    turns = [_reply_naming('r1', 1, resume), _report_turn('r1', 2),
             _reply_naming('r2', 3, deck, files=[_report_file('pptx', 'talk.pptx', 'deck')]), _report_turn('r2', 4),
             _reply_naming('r3', 5, {'key': KEY_A, 'title': 'Resume (final)'}), _report_turn('r3', 6),
             _reply_naming('r4', 7), _report_turn('r4', 8),
             # A receipt naming a malformed key names no task.
             _reply_naming('r5', 9, {'key': 'nope', 'title': 'x'}), _report_turn('r5', 10)]
    artifacts = [_artifact('own', 'response_file', 'notes.pdf', at=11),
                 _artifact('pptx', 'response_file', 'talk.pptx', at=4)]
    page = _page(turns, artifacts).read(project_path='/repo')
    assert {item['id']: (item.get('tasks') or [{}])[0].get('key') for item in page['items']} == {
        'report:r1': KEY_A, 'report:r2': KEY_B, 'report:r3': KEY_A, 'report:r4': None, 'report:r5': None,
        'artifact:own': None}
    # Newest first; a task's title is the one its newest item named.
    assert page['tasks'] == [{'key': KEY_A, 'title': 'Resume (final)', 'count': 2, 'latestAt': 6},
                             {'key': KEY_B, 'title': 'Deck', 'count': 1, 'latestAt': 4}]
    assert page['otherCount'] == 3
    only_a = _page(turns, artifacts).read(project_path='/repo', task=KEY_A)
    assert [item['id'] for item in only_a['items']] == ['report:r3', 'report:r1']
    assert only_a['tasks'] == page['tasks'] and only_a['otherCount'] == 3
    other = _page(turns, artifacts).read(project_path='/repo', task='other')
    assert [item['id'] for item in other['items']] == ['artifact:own', 'report:r5', 'report:r4']
    with pytest.raises(ValueError):
        _page(turns).read(task='not-a-key')


def test_relayed_reports_of_another_project_are_never_tasks_of_the_router():
    def notice(turn_id, at):
        text = render_notice({'projectPath': '/srv/app', 'coordinatorId': 'pr', 'kind': 'delivery',
                              'turnId': turn_id, 'title': 'T', 'message': 'M',
                              'tasks': [{'key': KEY_A, 'title': 'Ship app'}]})
        return {'turnId': 'n-' + turn_id, 'ordinal': at, 'createdAt': at, 'actor': 'user', 'kind': 'message',
                'projection': {'content': text}}
    reports = {('pr', 'rep'): _report_turn('rep', 50, conversation='pr')}
    page = _page([notice('rep', 51)], reports=reports).read(project_path='/router-home')
    [item] = page['items']
    assert item['tasks'] == [{'key': KEY_A, 'title': 'Ship app'}] and item['projectPath'] == '/srv/app'
    assert page['tasks'] == [] and page['otherCount'] == 1
    assert [entry['id'] for entry in _page([notice('rep', 51)], reports=reports).read(
        project_path='/router-home', task='other')['items']] == ['report:rep']


def _history(key, title, status, *, completed_at=None, progress=None):
    task = {'key': key, 'title': title, 'status': status, 'onBoard': True, 'dropped': False,
            'firstSeenAt': 1, 'lastSeenAt': 1, 'changedAt': completed_at or 1}
    if completed_at:
        task['completedAt'] = completed_at
    if progress:
        task['progress'] = progress
    return task


KEY_C = 'c' * 32


def test_a_completed_task_without_a_delivery_report_gets_its_automatic_work_report():
    """Owner request 2026-11-17: every finished task has its result, without making reports mandatory."""
    tasks = [_history(KEY_A, 'Resume', 'completed', completed_at=30, progress='Final in out/resume.pdf.'),
             _history(KEY_B, 'Deck', 'completed', completed_at=40, progress='Shipped.'),
             _history(KEY_C, 'Survey', 'in_progress', progress='Half done.')]
    # The deck task has a delivery report naming it; the resume task only a progress report.
    turns = [_reply_naming('r1', 1, {'key': KEY_B, 'title': 'Deck'}), _report_turn('r1', 2),
             _reply_naming('r2', 3, {'key': KEY_A, 'title': 'Resume'}),
             {**_report_turn('r2', 4), 'kind': 'coordinator_report.progress'}]
    page = _page(turns, board_tasks=tasks).read(project_path='/repo')
    completion = [item for item in page['items'] if item['kind'] == 'completion']
    assert completion == [{'id': 'completion:' + KEY_A, 'kind': 'completion', 'title': 'Final in out/resume.pdf.', 'projectPath': '/repo',
                           'createdAt': 30, 'source': None, 'summary': 'Final in out/resume.pdf.',
                           'tasks': [{'key': KEY_A, 'title': 'Resume'}]}]
    assert {entry['key']: entry['count'] for entry in page['tasks']} == {KEY_A: 2, KEY_B: 1}
    # No project, no board: nothing automatic.
    assert not [item for item in _page(turns, board_tasks=tasks).read()['items'] if item['kind'] == 'completion']


def test_a_report_link_attaches_an_earlier_report_to_one_or_more_tasks():
    """Reports sent before tasks could be named are linked afterwards; the report itself never changes."""
    turns = [_reply_naming('r1', 1), _report_turn('r1', 2),
             _reply_naming('r2', 3, {'key': KEY_A, 'title': 'Resume'}), _report_turn('r2', 4)]
    links = {'r1': [{'key': KEY_A, 'title': 'Resume'}, {'key': KEY_B, 'title': 'Deck'}],
             'r2': [{'key': KEY_A, 'title': 'Resume'}]}
    page = _page(turns, links=links).read(project_path='/repo')
    assert {item['id']: [task['key'] for task in item['tasks']] for item in page['items']} == {
        'report:r1': [KEY_A, KEY_B], 'report:r2': [KEY_A]}
    assert {entry['key']: entry['count'] for entry in page['tasks']} == {KEY_A: 2, KEY_B: 1}
    assert [item['id'] for item in _page(turns, links=links).read(project_path='/repo', task=KEY_B)['items']] == [
        'report:r1']
