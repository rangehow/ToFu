"""Transient composition keeps common policy but cannot acquire server owners."""
import builtins

import pytest

pytestmark = pytest.mark.unit


def test_transient_round_without_durable_identity_does_not_import_storage(monkeypatch):
    from lib.tasks_pkg.round_reads import begin_round_reads
    original = builtins.__import__

    def admitted(name, *args, **kwargs):
        if name == 'lib.storage' or name.startswith('lib.storage.') or name == 'lib.turn_lifecycle':
            pytest.fail(f'empty transient round imported {name}')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', admitted)
    assert begin_round_reads({'id': 'request', 'convId': 'caller-label',
                             '_transientRuntime': True,
                             'config': {'_storageFreeRuntime': True}}) is None


def test_transient_command_does_not_acquire_server_checkout_scratch(monkeypatch, tmp_path):
    from lib.project_mod.run_command import _get_cmd_env
    from lib.project_mod import task_spares
    project = tmp_path / 'u1' / 'project'
    project.mkdir(parents=True)

    def forbidden(*args):
        pytest.fail('transient command requested server workspace lifecycle')

    monkeypatch.setattr(task_spares, 'checkout_scratch', forbidden)
    environment = _get_cmd_env(str(project), project_root=str(project),
        task={'config': {'_storageFreeRuntime': True}})
    assert isinstance(environment['PATH'], str)


def test_inherited_requirements_are_not_silently_dropped_for_transient_tasks():
    from lib.tasks_pkg.round_reads import _requests
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id='test', owner_user_id=1)
    with pytest.raises(ValueError, match='issued source identity'):
        _requests({'id': 'request', 'convId': 'caller-label',
            '_transientRuntime': True, '_principalContext': principal.to_payload(),
            '_turnRequirementSource': {'forged': True}})
