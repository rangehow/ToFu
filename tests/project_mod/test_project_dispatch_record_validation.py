"""Complete-record validation protects nested evidence and live ownership."""
from copy import deepcopy
import uuid

import pytest

from lib.project_dispatch import repository
from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_repository import (
    authorize, context as context, model_attempt,
)

pytestmark = pytest.mark.unit


def record_for(c):
    assignment = authorize(c)
    assignment['modelAttempts'] = [model_attempt('model-root')]
    return {'binding': deepcopy(c.repo.binding), 'assignments': [assignment]}


@pytest.mark.parametrize('segmented', [False, True])
def test_valid_record_preserves_complete_evidence(context, segmented):
    value = record_for(context)
    expected = deepcopy(value)
    context.repo._check_record(value, segmented=segmented)
    assert value == expected


@pytest.mark.parametrize('segmented', [False, True])
@pytest.mark.parametrize('mutation', [
    'request-type', 'unknown-request-authority', 'attempt-type', 'attempt-nonce',
    'nonfinite', 'invalid-unicode', 'foreign-owner', 'duplicate-occurrence',
])
def test_complete_record_rejects_invalid_nested_or_semantic_evidence(context, segmented, mutation):
    value = record_for(context)
    assignment = value['assignments'][0]
    attempt = assignment['modelAttempts'][0]
    if mutation == 'request-type':
        assignment['request']['brief'] = 7
    elif mutation == 'unknown-request-authority':
        assignment['request']['ungrantedAuthority'] = True
    elif mutation == 'attempt-type':
        attempt['attempt_ordinal'] = True
    elif mutation == 'attempt-nonce':
        attempt['account_nonce'] = 'malformed'
    elif mutation == 'nonfinite':
        attempt['reported_tokens'] = float('nan')
    elif mutation == 'invalid-unicode':
        assignment['request']['brief'] = '\ud800'
    elif mutation == 'foreign-owner':
        assignment['grant']['owner'] += 1
    else:
        assignment['modelAttempts'].append(deepcopy(attempt))
    with pytest.raises(ProjectDispatchError):
        context.repo._check_record(value, segmented=segmented)


@pytest.fixture
def schema_walks(monkeypatch):
    """Every jsonschema walk, by definition, through the repository's validator port."""
    walks, validator = [], repository._validator

    class Counted:
        def __init__(self, definition):
            self.definition, self.validator = definition, validator(definition)

        def is_valid(self, value):
            walks.append(self.definition)
            return self.validator.is_valid(value)
    monkeypatch.setattr(repository, '_validator', Counted)
    return walks


def fresh_record(c, walks):
    # A new stored identity per test: process-wide verdicts never decide another test.
    value = record_for(c)
    value['assignments'][0]['id'] = str(uuid.uuid4())
    # A legacy record's identity is the repository key every test shares.
    repository._VALID_STORED_ROW_DIGESTS.pop(('record', c.repo.key), None)
    walks.clear()
    return value


@pytest.mark.parametrize('segmented', [False, True])
def test_unchanged_stored_rows_skip_repeated_schema_walk(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    definition = 'assignment' if segmented else 'record'
    context.repo._check_record(deepcopy(value), segmented=segmented)
    first = [walk for walk in schema_walks if walk != 'admission']
    assert first
    context.repo._check_record(deepcopy(value), segmented=segmented)
    assert [walk for walk in schema_walks if walk != 'admission'] == first
    value['assignments'][0]['modelAttempts'].append(model_attempt('another-root'))
    context.repo._check_record(deepcopy(value), segmented=segmented)
    # A changed row walks only its changed member, never its unchanged request again.
    changed = 'assignment/properties/modelAttempts' if segmented else 'record/properties/assignments'
    assert [walk for walk in schema_walks if walk != 'admission'] == first + [changed]
    # The derived admission projection is not stored evidence and is walked on every read.
    assert schema_walks.count('admission') == 3


def test_a_value_many_rows_repeat_is_walked_once(context, schema_walks):
    """Grants of one Coordinator request repeat its profile; a boot walks it once."""
    value = fresh_record(context, schema_walks)
    rows = [value['assignments'][0]] + [{**deepcopy(value['assignments'][0]), 'id': str(uuid.uuid4())}
                                        for _ in range(3)]
    profile = {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}
    assert repository._validator('profile').is_valid(profile)
    schema_walks.clear()
    for row in rows:
        row['request']['profile'] = deepcopy(profile)
        repository._validate_stored_row(row, 'assignment', row['id'], decoded=True)
    assert schema_walks.count('profile') <= 1
    assert sum(walk.startswith('request/properties/brief') for walk in schema_walks) <= 1
    tampered = {**deepcopy(rows[0]), 'id': str(uuid.uuid4())}
    tampered['request']['profile']['mode'] = 7
    assert not repository._validator('assignment').is_valid(tampered)
    with pytest.raises(ProjectDispatchError):
        repository._validate_stored_row(tampered, 'assignment', tampered['id'], decoded=True)


@pytest.mark.parametrize('segmented', [False, True])
def test_changed_invalid_stored_row_is_walked_and_refused_every_time(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    definition = 'assignment' if segmented else 'record'
    context.repo._check_record(deepcopy(value), segmented=segmented)
    value['assignments'][0]['request']['brief'] = 7
    changed = 'request/properties/brief' if segmented else 'record/properties/assignments'
    before = schema_walks.count(changed)
    for _ in range(2):
        with pytest.raises(ProjectDispatchError, match='Invalid dispatch ' + definition):
            context.repo._check_record(deepcopy(value), segmented=segmented)
    # A refusal is never remembered: the changed member is walked by every read.
    assert schema_walks.count(changed) == before + 2


@pytest.mark.parametrize('segmented', [False, True])
def test_python_only_value_never_shares_its_canonical_json_verdict(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    value['assignments'][0]['toolAttempts'] = []
    context.repo._check_record(deepcopy(value), segmented=segmented)
    # A tuple encodes exactly like the validated list, but JSON Schema refuses it.
    value['assignments'][0]['toolAttempts'] = ()
    with pytest.raises(ProjectDispatchError, match='Invalid dispatch'):
        context.repo._check_record(value, segmented=segmented)


def test_stored_verdicts_are_not_shared_across_definitions(context, schema_walks):
    value = fresh_record(context, schema_walks)
    context.repo._check_record(deepcopy(value), segmented=True)
    value['requestBudgets'] = [deepcopy(value['assignments'][0])]
    with pytest.raises(ProjectDispatchError, match='Invalid dispatch coordinatorRequestBudget'):
        context.repo._check_record(deepcopy(value), segmented=True)
    assert not repository._validator('coordinatorRequestBudget').is_valid(value['requestBudgets'][0])


@pytest.mark.parametrize('change', ['undeclared-member', 'missing-required-member', 'invalid-member'])
def test_a_proven_row_whose_members_change_is_refused_like_a_whole_walk(context, schema_walks, change):
    value = fresh_record(context, schema_walks)
    context.repo._check_record(deepcopy(value), segmented=True)
    row = value['assignments'][0]
    if change == 'undeclared-member':
        row['ungrantedAuthority'] = True
    elif change == 'missing-required-member':
        del row['modelAttempts']
    else:
        row['modelAttempts'][0]['attempt_ordinal'] = True
    assert not repository._validator('assignment').is_valid(row)
    with pytest.raises(ProjectDispatchError):
        context.repo._check_record(deepcopy(value), segmented=True)
    assert schema_walks.count('assignment') == 1  # the refused oracle check above
