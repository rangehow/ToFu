"""Fault the recorded process observations without starting a Rust build."""

from types import SimpleNamespace

import pytest

from tests.storage_authority_sql import test_rust_sql_authority_differential as gate

pytestmark = pytest.mark.unit
_BUILD_SHA = 'b' * 64


@pytest.fixture
def observations(monkeypatch):
    candidate = gate.REPOSITORY_ROOT / 'test-candidate'
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(candidate))
    monkeypatch.setenv('TOFU_REMOTE_BINARY_SHA256', _BUILD_SHA)
    monkeypatch.setattr(gate, 'rust_authority_binary', lambda: candidate)
    python = gate.PYTHON_AUTHORITY_COMMAND
    rust = (str(candidate), 'serve')
    identities = {}
    for pid, command, digest in ((11, python, 'a' * 64), (12, rust, _BUILD_SHA)):
        from pathlib import Path
        identities[command] = {
            'pid': pid, 'start_ticks': 100, 'executable': str(Path(command[0]).resolve()),
            'executable_sha256': digest, 'cmdline': list(command), 'popen_args': list(command),
            'requested_command': list(command),
        }
    monkeypatch.setattr(gate, 'child_identity', lambda _, command: identities[command].copy())
    pair = [SimpleNamespace(command=command, supervisor=None) for command in (python, rust)]

    def observe(**kwargs):
        return gate.record_pair_identity(*pair, lambda *_: None, **kwargs)

    return observe, identities[python], identities[rust]


def test_rejects_candidate_not_equal_to_post_build_digest(observations):
    observe, _, rust = observations
    rust['executable_sha256'] = 'c' * 64
    with pytest.raises(AssertionError, match='post-build SHA-256'):
        observe()


@pytest.mark.parametrize('phase,changed,field,restarted_role', [
    ('executed', 'rust', 'pid', None),
    ('executed', 'python', 'start_ticks', None),
    ('restarted_rust', 'python', 'pid', 'rust'),
    ('replayed_rust', 'rust', 'start_ticks', None),
])
def test_rejects_unexpected_generation(observations, phase, changed, field, restarted_role):
    observe, python, rust = observations
    previous = observe()
    if phase == 'replayed_rust':
        rust['pid'] += 1
        previous = observe(phase='restarted_rust', previous=previous, restarted_role='rust')
    if restarted_role == 'rust':
        rust['pid'] += 1
    identities = {'python': python, 'rust': rust}
    identities[changed][field] += 5
    with pytest.raises(AssertionError, match=f'{changed}.*unexpected process generation'):
        observe(phase=phase, previous=previous, restarted_role=restarted_role)


def test_rejects_missing_intended_generation(observations):
    observe, _, _ = observations
    previous = observe()
    with pytest.raises(AssertionError, match='rust.*did not change process generation'):
        observe(phase='restarted_rust', previous=previous, restarted_role='rust')


def test_rejects_same_path_executable_swap_after_crash_replay(observations):
    observe, _, rust = observations
    started = observe()
    rust['pid'] += 1
    restarted = observe(phase='restarted_rust', previous=started, restarted_role='rust')
    # Same command, executable pathname and generation; live executable bytes differ.
    rust['executable_sha256'] = 'c' * 64
    with pytest.raises(AssertionError, match='post-build SHA-256'):
        observe(phase='replayed_rust', previous=restarted)
