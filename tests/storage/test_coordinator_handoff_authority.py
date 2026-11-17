"""Request handoff contracts against each selected segmented storage authority."""
import inspect
import uuid

import pytest

from tests.project_mod.test_coordinator_worker_handoff import run as run, runtime, base_runtime, context
from tests.project_mod import test_coordinator_worker_handoff as contracts
from tests.project_mod import test_coordinator_dependency_links as links
from tests.project_mod import test_coordinator_acceptance_checks as acceptance

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def contract_owner(monkeypatch):
    # These reused conversation fixtures deliberately exercise explicit owner 7.
    # Pin the disposable native authority to that owner, not a different account.
    monkeypatch.setattr('scripts.tofudb_perf_compare._OWNER_ID', 7)


@pytest.mark.parametrize('scenario', [
    contracts.test_same_human_resume_recovers_replays_reviews_and_finishes_original_workers,
    contracts.test_resume_before_first_worker_keeps_original_account_identity,
    contracts.test_new_human_steering_can_find_review_and_cancel_prior_work,
    contracts.test_automatic_continuation_reviews_and_cancels_another_requests_workers_of_its_binding,
    contracts.test_project_worker_history_pages_do_not_drop_earlier_requests,
    links.test_automatic_dependent_waits_for_review_and_publication_then_reuses_its_grant,
    links.test_automatic_rework_records_exact_link_and_replays_without_duplicate_birth,
    acceptance.test_accept_records_unknown_verdicts_and_the_receipt_reports_them_unverified,
    acceptance.test_history_rows_carry_checks_and_count_unjudged_checks_as_unknown,
], ids=lambda scenario: scenario.__name__)
def test_handoff_on_selected_authority(authority_run, monkeypatch, scenario):
    if 'monkeypatch' in inspect.signature(scenario).parameters:
        scenario(authority_run, monkeypatch)
    else:
        scenario(authority_run)


@pytest.mark.parametrize('tool', ['review_project_work', 'cancel_project_work'])
@pytest.mark.parametrize('owner', ['another_coordinator', 'another_project'])
def test_worker_control_outside_the_binding_is_refused_on_selected_authority(
        authority_run, monkeypatch, tool, owner):
    contracts.test_review_and_cancel_of_another_coordinators_or_projects_worker_stay_refused_and_audited(
        authority_run, monkeypatch, tool, owner)


@pytest.fixture
def authority_run(detached_semantic_authority, run):
    factory, _, _ = detached_semantic_authority
    r = run
    for (namespace, key), row in r.c.client.rows.items():
        factory(write=True).command('record.put', {'namespace': namespace,
            'key': key, 'expected_version': 0, 'value': row['value']}, uuid.uuid4().hex)
    r.c.repo.client = factory
    r.c.repo.storage_version = 2
    return r
