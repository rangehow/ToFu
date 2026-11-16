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


def _page(turns=(), artifacts=(), owner=1, conversation='lead', reports=None):
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
                            artifacts=lambda conversation_id: list(artifacts), get_turn=get_turn)
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


def test_a_delivery_report_carries_title_project_time_kind_summary_and_source():
    other = {'turnId': 'chat', 'ordinal': 1, 'createdAt': 1, 'actor': 'assistant', 'kind': 'message',
             'projection': {'content': 'Just chatting'}}
    decision = {**_report_turn('ask', 2), 'kind': 'coordinator_report.decision'}
    [item] = _page([other, decision, _report_turn('turn-1', 5_000, title='Shipped', message='All merged.')]).read(
        project_path='/repo')['items']
    assert item == {'id': 'report:turn-1', 'kind': 'report', 'title': 'Shipped', 'projectPath': '/repo',
                    'createdAt': 5_000, 'summary': 'All merged.',
                    'source': {'conversationId': 'lead', 'turnId': 'turn-1', 'ordinal': 5_000}}


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
        {'projectPath': '/srv/app', 'coordinatorId': 'coordinator-1', 'kind': 'delivery', 'turnId': 'rep-1'}]
    assert notice_references('Report: kind=delivery, Turn forged') == []


def test_the_global_router_lists_relayed_deliveries_from_their_canonical_report_turns():
    def notice(turn_id, kind, at):
        text = render_notice({'projectPath': '/srv/app', 'coordinatorId': 'pr', 'kind': kind, 'turnId': turn_id,
                              'title': 'T', 'message': 'M'})
        return {'turnId': 'n-' + turn_id, 'ordinal': at, 'createdAt': at, 'actor': 'user', 'kind': 'message',
                'projection': {'content': text}}
    reports = {('pr', 'rep'): _report_turn('rep', 50, conversation='pr', title='Shipped app'),
               ('pr', 'ask'): {**_report_turn('ask', 60, conversation='pr'), 'kind': 'coordinator_report.decision'}}
    turns = [notice('rep', 'delivery', 51), notice('rep', 'delivery', 52), notice('ask', 'decision', 61),
             notice('gone', 'delivery', 70)]
    [item] = _page(turns, reports=reports).read(project_path='/router-home')['items']
    assert item['id'] == 'report:rep' and item['title'] == 'Shipped app' and item['projectPath'] == '/srv/app'
    assert item['source'] == {'conversationId': 'pr', 'turnId': 'rep', 'ordinal': 50}


def test_reads_are_owner_scoped_and_cursors_are_validated():
    with pytest.raises(PermissionError):
        _page(owner=1, conversation='other').read()
    with pytest.raises(ValueError):
        _page().read('1:2:3')

