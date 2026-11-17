"""Lead deliverables project from the report and Turn owners on the actual selected authority."""
import uuid

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_reports_artifacts_and_relayed_deliveries_project_from_their_owners(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    from lib import storage
    monkeypatch.setattr(storage, 'get_storage_client', factory)
    from lib.conversations import lead_deliverables
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.lead_deliverables import DeliverablesPage
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.relay import render_notice
    from lib.turn_lifecycle import append_settled_turn, create_turn_pair
    monkeypatch.setattr(lead_deliverables, 'notify', lambda *args: None)
    project = '/deliverables-authority/' + uuid.uuid4().hex
    binding = ProjectCoordinationRepository(owner).create(project, folder_id='test', expected_revision=0,
                                                          command_id='create')
    lead = binding['conversationId']
    scope = {'owner': owner, 'projectPath': project, 'coordinatorId': lead, 'bindingRevision': binding['revision']}
    publishing = append_settled_turn(lead, user_id=owner, command_id='publish-deck', actor='assistant',
                                     kind='message', projection={'content': 'Here is the deck', 'toolRounds': [
                                         {'results': [{'meta': {'artifactId': 'deck-1'}}]}]})['turn']
    reports = CoordinatorReports(owner, project)
    delivered = reports.publish(scope, {'title': 'Shipped', 'message': 'All merged.', 'kind': 'delivery',
                                        'idempotency_key': 'delivery'})
    asked = reports.publish(scope, {'title': 'Pick one', 'message': 'A or B?', 'kind': 'decision',
                                    'idempotency_key': 'ask'})
    # The reply that called report_to_user keeps the receipt naming the files it attached.
    attached = {'artifactId': 'cv-1', 'filename': 'cv.pdf', 'kind': 'file', 'sizeBytes': 9, 'path': 'out/cv.pdf'}
    append_settled_turn(lead, user_id=owner, command_id='reply', actor='assistant', kind='message', projection={
        'content': 'Reported.', 'toolRounds': [{'toolName': 'report_to_user', 'toolContent': __import__('json').dumps(
            {'conversationId': lead, 'turnId': asked['turnId'], 'published': True, 'files': [attached]})}]})
    deck = {'id': 'deck-1', 'conv_id': lead, 'source': 'response_file', 'format': 'file', 'title': 'plan.pptx',
            'created_at': publishing['createdAt'], 'meta': {'responseFile': {'filename': 'plan.pptx'}}}
    cv = {'id': 'cv-1', 'conv_id': lead, 'source': 'response_file', 'format': 'file', 'title': 'cv.pdf',
          'created_at': publishing['createdAt'] + 1, 'meta': {'responseFile': {'filename': 'cv.pdf'}}}

    items = DeliverablesPage(owner, lead, artifacts=lambda conversation: [deck, cv]).read(
        project_path=project)['items']
    assert [(item['kind'], item.get('reportKind'), item['title']) for item in items] == [
        ('report', 'decision', 'Pick one'), ('report', 'delivery', 'Shipped'), ('deck', None, 'plan.pptx')]
    assert items[0]['files'] == [attached] and 'files' not in items[1]
    assert items[1]['source']['turnId'] == delivered['turnId'] and items[1]['summary'] == 'All merged.'
    assert items[2]['source']['turnId'] == publishing['turnId'] and items[2]['projectPath'] == project

    router = ProjectCoordinationRepository(owner).create(project + '-home', folder_id='test', expected_revision=0,
                                                         command_id='create-router')['conversationId']
    # The relay dispatches its notice as the router's ordinary input Turn.
    create_turn_pair(router, user_id=owner, command_id='notice', config={}, input_projection={
        'content': render_notice({'projectPath': project, 'coordinatorId': lead, 'kind': 'delivery',
                                  'turnId': delivered['turnId'], 'title': 'Shipped', 'message': 'All merged.'})})
    [relayed] = DeliverablesPage(owner, router, artifacts=lambda conversation: []).read()['items']
    assert relayed['id'] == items[1]['id'] and relayed['projectPath'] == project
    assert relayed['source'] == items[1]['source']

    with pytest.raises(PermissionError):
        DeliverablesPage(owner + 1, lead, artifacts=lambda conversation: []).read()
