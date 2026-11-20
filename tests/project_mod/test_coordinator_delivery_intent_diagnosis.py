"""Missing-witness diagnosis, not a specification that unknown delivery is settled.

Ordinary project grants permit files but do not declare a required result kind.
These fixtures perform no Worker work or publication. Their briefs describe two
possible intents; neither prose nor the test's labels is a producer witness.
"""
from copy import deepcopy

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, delegate, end)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch import admission
from lib.project_dispatch.continuation import worker_summaries

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('evidence', ['absent_workspace', 'registered_without_receipt', 'unavailable'])
def test_ordinary_project_results_lack_a_delivery_requirement_witness(run, monkeypatch, evidence):
    r = run
    rows = []
    for key, brief in [('file', 'Return a verified source change.'),
                       ('non-file', 'Return an explanation only; do not change files.')]:
        r.args = {**r.args, 'brief': brief}
        row = delegate(r, key)
        end(r, row, delivered=False)
        reviewed = r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                          decision='accept', note='Result reviewed; publication not requested.')
        assert reviewed['decision'] == 'accept'
        rows.append(r.c.repo.read(row['id']))

    assert rows[0]['request']['profile'] == rows[1]['request']['profile']
    assert rows[0]['request']['profile']['mode'] == 'project'
    assert rows[0]['request']['profile']['writePaths'] == ['.']
    assert all(row['review']['decision'] == 'accept' and not row.get('publication') for row in rows)

    reads = []

    def workspace_get(project, task, *, user_id):
        reads.append((project, task, user_id))
        if evidence == 'unavailable':
            raise OSError('synthetic workspace read unavailable')
        if evidence == 'absent_workspace':
            return None
        row = next(row for row in rows if row['execution']['taskId'] == task)
        return {'task_id': task, 'user_id': user_id, 'project_root': project,
                'state': 'completed', 'managed': True,
                'origin': {admission.KEY: r.c.repo.admission(row['id'])}}

    monkeypatch.setattr(r.c.repo, 'workspace_get', workspace_get)
    before = deepcopy(r.c.client.rows), deepcopy(r.attempts), list(r.calls), list(r.scheduled), list(r.reports)
    first = worker_summaries(r.c.repo, rows)
    second = worker_summaries(r.c.repo, rows)
    assert first == second
    assert all(summary['status'] == 'completed' and summary['review']['decision'] == 'accept'
               and summary['delivery'] is None for summary in first)
    assert len(reads) == 4
    assert (r.c.client.rows, r.attempts, r.calls, r.scheduled, r.reports) == before
