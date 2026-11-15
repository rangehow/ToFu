"""Out-of-band repair dispose executor (serverctl_pkg/oob_dispose.py, SELF-REPAIR O5).

Specification of designs/headless-recovery-agent/DESIGN.md, piece O5: the
model proposes; the deterministic launcher disposes. A proposed code fix is
accepted only when its new spec fails on the base and passes on the fix, its
focused tests pass, an independent review approves, and publication plus the
gated restart clear the incident's own signal. Any failure reverts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverctl_pkg import oob_dispose
from tests.support.production_api_guard import production_api_guard  # noqa: F401 - autouse guard

pytestmark = pytest.mark.unit

# The spec path a dispose plan names, not a repository file.
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_archive_split.py'}


class _FakePorts:
    def __init__(self):
        self.calls: list[tuple] = []
        self.base_result = (1, 'failing as expected')
        self.fixed_result = (0, 'passing')
        self.review_verdict = 'approve'
        self.land_result = (0, 'landed')
        self.restart_result = (0, 'restarted')
        self.restart_count = 0
        self.healthy_after_restart = True
        self.revert_result = (0, 'reverted')

    def run_spec(self, workspace: Path, base: bool):
        self.calls.append(('spec', workspace, base))
        return self.base_result if base else self.fixed_result

    def run_tests(self, workspace: Path):
        self.calls.append(('tests', workspace))
        return (0, 'passing')

    def review(self, workspace: Path):
        self.calls.append(('review', workspace))
        return self.review_verdict

    def land(self, workspace: Path):
        self.calls.append(('land', workspace))
        return self.land_result

    def restart(self):
        self.restart_count += 1
        self.calls.append(('restart',))
        return self.restart_result

    def symptom_clear(self) -> bool:
        self.calls.append(('symptom_clear',))
        return self.healthy_after_restart

    def revert(self, workspace: Path):
        self.calls.append(('revert', workspace))
        return self.revert_result


def _proposal(workspace: Path):
    return {'kind': 'code_fix', 'workspace': str(workspace), 'cause': 'archive above cap',
            'spec': 'tests/test_archive_split.py', 'commit': 'abc123'}


def test_a_fix_is_published_only_after_failing_first_tests_review_and_restart(tmp_path):
    ports = _FakePorts()
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'published'
    names = [call[0] for call in ports.calls]
    assert names == ['spec', 'spec', 'tests', 'review', 'land', 'restart', 'symptom_clear']
    assert ports.calls[0][2] is True and ports.calls[1][2] is False  # base fails, fix passes
    assert ports.restart_count == 1


def test_a_spec_that_passes_on_the_base_is_rejected_before_any_publication(tmp_path):
    ports = _FakePorts()
    ports.base_result = (0, 'passing on the base too')
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'rejected'
    assert outcome['reason'] == 'the new spec does not fail on the base'
    assert [call[0] for call in ports.calls] == ['spec']


def test_a_failing_test_on_the_fix_stops_before_review(tmp_path):
    ports = _FakePorts()
    ports.fixed_result = (1, 'still failing')
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'rejected'
    assert outcome['reason'].startswith('the new spec still fails on the fix')
    assert [call[0] for call in ports.calls] == ['spec', 'spec']


def test_a_blocking_review_stops_before_landing(tmp_path):
    ports = _FakePorts()
    ports.review_verdict = 'changes requested'
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'rejected'
    assert [call[0] for call in ports.calls] == ['spec', 'spec', 'tests', 'review']


def test_a_restart_that_does_not_clear_the_incident_is_reverted(tmp_path):
    ports = _FakePorts()
    ports.healthy_after_restart = False
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'rolled_back'
    assert [call[0] for call in ports.calls] == ['spec', 'spec', 'tests', 'review', 'land', 'restart',
                                                 'symptom_clear', 'revert', 'restart']


def test_a_restart_failure_reverts_without_a_second_restart(tmp_path):
    ports = _FakePorts()
    ports.restart_result = (1, 'preflight refused')
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'rolled_back'
    assert [call[0] for call in ports.calls] == ['spec', 'spec', 'tests', 'review', 'land', 'restart', 'revert']


def test_a_forbidden_fix_is_never_executed(tmp_path):
    ports = _FakePorts()
    proposal = {'kind': 'code_fix', 'workspace': str(tmp_path), 'touches': ['serverctl_pkg/oob_alert.py']}
    outcome = oob_dispose.execute(proposal, ports=ports)
    assert outcome['status'] == 'rejected'
    assert 'self-repair boundary' in outcome['reason']
    assert ports.calls == []


def test_an_unknown_action_kind_is_rejected(tmp_path):
    outcome = oob_dispose.execute({'kind': 'send_email', 'to': 'someone'}, ports=_FakePorts())
    assert outcome['status'] == 'rejected'


def test_storage_recover_proposal_uses_the_lock_and_refuses_compile(tmp_path):
    ports = _FakePorts()
    ports.recover_result = (0, 'recovered')
    ports.recover = lambda: ports.calls.append(('recover',)) or ports.recover_result
    outcome = oob_dispose.execute({'kind': 'storage_recover'}, ports=ports)
    assert outcome['status'] == 'executed'
    assert [call[0] for call in ports.calls] == ['recover']


def test_outcome_is_written_to_the_session_dir(tmp_path):
    ports = _FakePorts()
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports)
    assert outcome['status'] == 'published'
    # The outcome file belongs to the incident's session directory, not the
    # workspace (a shared checkout must stay clean).
    session_dir = tmp_path / 'session'
    session_dir.mkdir()
    outcome = oob_dispose.execute(_proposal(tmp_path), ports=ports, session_dir=session_dir)
    assert (session_dir / 'dispose-outcome.json').exists()
    assert json.loads((session_dir / 'dispose-outcome.json').read_text())['status'] == 'published'
