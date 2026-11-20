"""Model-routing-only credentials are redacted like vault and config keys.

Keys added in Settings exist only as sealed model-routing v2 secrets (the most
used sankuai AppIds are such keys). The known-credential set reads them through
the model-routing owner's own decrypting read for an explicit owner, keeps no
plaintext, and refreshes when the owner's change stamp changes, which every
durable commit or secret write rewrites in whichever process made it. The
large-result spool file is redacted as it is written. All values are synthetic.
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

OWNER = 1
RAW_KEY = 'mr-only-appid-0123456789abcdef6672'
ENVELOPE_KEY = 'mr-only-envelope-key-fedcba98765430733'
RAW_MARKER = '[redacted credential: model-routing cred-raw]'
ENVELOPE_MARKER = '[redacted credential: model-routing cred-envelope]'


def _document():
    from lib.model_routing import empty_document
    document = empty_document(revision=0)
    document['creators'].append({'creator_id': 'openai', 'name': 'OpenAI'})
    document['models'].append({
        'creator_id': 'openai', 'model_id': 'gpt-test', 'display_name': 'GPT Test',
        'capabilities': ['text'], 'context_window': 200000, 'quality_rank': 1.0})
    document['providers'].append({'provider_id': 'sankuai', 'name': 'sankuai', 'scope': 'owner'})
    document['provider_accesses'].append({
        'provider_access_id': 'access', 'provider_id': 'sankuai',
        'display_name': 'sankuai', 'enabled': True, 'quota_policy': {}})
    document['connections'].append({
        'connection_id': 'conn', 'provider_access_id': 'access',
        'base_url': 'https://gw.example.test/v1', 'protocol': 'openai_chat',
        'extra_headers': {}, 'enabled': True, 'priority': 100})
    for credential_id, reference in (('cred-raw', 'secret-raw'),
                                     ('cred-envelope', 'secret-envelope')):
        document['credentials'].append({
            'credential_id': credential_id, 'provider_access_id': 'access',
            'kind': 'api_key', 'secret_reference': reference, 'key_hint': '***',
            'enabled': credential_id == 'cred-raw',
            'authorization': {'connection_ids': ['conn'], 'models': []},
            'quota_policy': {}})
    return document


@pytest.fixture()
def routed(tmp_path, monkeypatch):
    import lib.config_dir as config_dir
    import lib.credentials_vault as vault
    import lib.tool_results.credential_redaction as redaction
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.model_routing.credential_envelope import ENVELOPE_FORMAT
    monkeypatch.setattr(config_dir, 'CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    monkeypatch.setattr(redaction, '_server_config_path', lambda: str(tmp_path / 'none.json'))
    monkeypatch.setattr(redaction, '_cached', {}, raising=False)
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(OWNER)
    repository.put_secret(boundary, RAW_KEY, secret_reference='secret-raw')
    repository.put_secret(boundary, json.dumps({
        'format': ENVELOPE_FORMAT, 'api_key': ENVELOPE_KEY, 'extra_headers': {}}),
        secret_reference='secret-envelope')
    repository.compare_and_swap(boundary, _document(), expected_revision=0)
    reads = []
    original_get = repository.get

    def counted_get(owner_boundary):
        reads.append(owner_boundary)
        return original_get(owner_boundary)

    monkeypatch.setattr(repository, 'get', counted_get)
    monkeypatch.setattr('lib.model_routing.known_secrets.ModelRoutingRepository',
                        lambda: repository)
    return SimpleNamespace(repository=repository, boundary=boundary, reads=reads,
                           redaction=redaction)


def _leaks(text: str) -> list[str]:
    return sorted(value for value in (RAW_KEY, ENVELOPE_KEY) if value in text)


def _shell_echo(tmp_path) -> str:
    from lib.project_mod.run_command import tool_run_command
    leaky = tmp_path / 'relay.env'
    leaky.write_text(f'FRIDAY_APPID={RAW_KEY}\nAuthorization: Bearer {ENVELOPE_KEY}\n')
    out = tool_run_command(str(tmp_path), f'cat {leaky.name}')
    assert _leaks(out), 'fixture must reproduce the unredacted command output'
    return out


def test_model_routing_only_key_is_stored_and_sent_to_the_model_redacted(routed, tmp_path, monkeypatch):
    from tests.credentials_vault.test_known_credential_redaction import (
        _pipeline_task, _run_pipeline, _v1_client, _v2_client)
    out = _shell_echo(tmp_path)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.append_event',
                        lambda task, event: None)

    def handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry, *rest, **kw):
        return tc_id, out, False

    task = _pipeline_task(handler, 'run_command')
    assert task['_userId'] == OWNER
    messages, row = _run_pipeline(task, 'run_command')
    model_facing = json.dumps(messages, default=str)
    assert _leaks(model_facing) == []
    assert RAW_MARKER in model_facing and ENVELOPE_MARKER in model_facing
    assert _leaks(json.dumps(row, default=str)) == []

    v2 = []
    client = _v2_client(v2)
    client._owner_id = OWNER
    client.command('record.put', {'value': {'toolContent': out}}, 'cmd-v2')
    assert _leaks(json.dumps(v2)) == [] and RAW_MARKER in json.dumps(v2)
    v1 = []
    _v1_client(v1).command('record.put', {'user_id': OWNER, 'value': out}, 'cmd-v1')
    assert _leaks(json.dumps(v1)) == [] and ENVELOPE_MARKER in json.dumps(v1)


def test_known_set_is_owner_scoped(routed):
    known = routed.redaction.known_credential_redactor
    assert known(owner_user_id=OWNER).redact(RAW_KEY) == RAW_MARKER
    assert known(owner_user_id=2).redact(RAW_KEY) == RAW_KEY
    assert known(owner_user_id=None).redact(RAW_KEY) == RAW_KEY


@pytest.mark.parametrize('tenants', [('tenant-a', 'tenant-b'), ('tenant-b', 'tenant-a')])
def test_durable_redaction_keeps_the_task_owner_boundary(routed, tenants):
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.change_notifications import mark_model_routing_secrets_changed

    scope = routed.redaction.credential_redaction_scope
    known = routed.redaction.known_credential_redactor
    boundaries = [OwnerBoundary.create(OWNER, tenant) for tenant in tenants]
    boundaries.append(OwnerBoundary.create(2, tenants[0]))
    keys = [f'synthetic-durable-key-{i:08d}' for i in range(len(boundaries))]
    for boundary, key in zip(boundaries, keys):
        routed.repository.put_secret(boundary, key, secret_reference='secret-raw')
        routed.repository.compare_and_swap(boundary, _document(), expected_revision=0)

    # Ignore the fixture's authority reads during compare-and-swap setup.
    routed.reads.clear()
    # Also warm the unscoped, empty-tenant entry for the same owner.
    default = known(owner_user_id=OWNER)
    for index in (*range(len(boundaries)), *reversed(range(len(boundaries)))):
        boundary = boundaries[index]
        with scope(task={'_tenant_id': boundary.tenant_id}, owner_user_id=boundary.owner_user_id):
            redactor = known(owner_user_id=boundary.owner_user_id)
            if redactor.redact(keys[index]) != RAW_MARKER:
                pytest.fail('durable task redactor missed its own boundary key')
            if any(redactor.contains(key) for i, key in enumerate(keys) if i != index):
                pytest.fail('durable task redactor matched a foreign boundary key')
    assert routed.reads == [routed.boundary, *boundaries]
    assert known(owner_user_id=OWNER) is default

    rotated = 'synthetic-durable-rotated-key'
    routed.repository.put_secret(boundaries[0], rotated, secret_reference='secret-raw')
    mark_model_routing_secrets_changed(user_id=OWNER)
    for index in (1, 0, 2):
        boundary = boundaries[index]
        with scope(task={'_tenant_id': boundary.tenant_id}, owner_user_id=boundary.owner_user_id):
            redactor = known(owner_user_id=boundary.owner_user_id)
            if index == 0:
                if redactor.redact(rotated) != RAW_MARKER or redactor.contains(keys[0]):
                    pytest.fail('durable task redactor did not refresh the rotated key')
            elif redactor.redact(keys[index]) != RAW_MARKER or redactor.contains(rotated):
                pytest.fail('rotation changed another boundary\'s recognized key set')
    assert routed.reads == [routed.boundary, *boundaries, boundaries[1], boundaries[0]]


def test_refresh_follows_the_owner_change_stamp_not_every_write(routed):
    from lib.model_routing.change_notifications import mark_model_routing_secrets_changed
    redact = lambda text: routed.redaction.known_credential_redactor(owner_user_id=OWNER).redact(text)
    assert redact(RAW_KEY) == RAW_MARKER
    for _ in range(5):
        redact('ordinary output')
    assert len(routed.reads) == 1, 'an unchanged stamp must not re-read the authority'
    rotated = 'mr-rotated-appid-9876543210zyxwvu'
    routed.repository.put_secret(routed.boundary, rotated, secret_reference='secret-raw')
    # Another process rewrites the stamp after its commit; only the stamp is shared.
    mark_model_routing_secrets_changed(user_id=OWNER, rev=2)
    assert redact(rotated) == RAW_MARKER
    assert redact(RAW_KEY) == RAW_KEY, 'a rotated-away value is no longer known'
    assert len(routed.reads) == 2


def test_durable_repository_writes_rewrite_the_owner_stamp(routed, monkeypatch):
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    from lib.model_routing.change_notifications import model_routing_secrets_stamp_path
    from lib.model_routing.repository import StoredAuthority
    from lib.model_routing import empty_document
    path = model_routing_secrets_stamp_path(user_id=7)

    def stamp():
        return os.stat(path).st_ino if os.path.exists(path) else None

    class Client:
        def command(self, operation, payload, command_id, **kw):
            if operation == 'model_routing.commit':
                return {'revision': payload['expected_revision'] + 1, 'updated_at': 1.0}
            if operation == 'model_routing.secret.put':
                return {'secret_reference': payload['secret_reference'], 'key_hint': 'h'}
            return {'deleted': True}

    repository = ModelRoutingRepository()
    monkeypatch.setattr(repository, '_client', lambda write=False: Client())
    boundary = OwnerBoundary.create(7)
    monkeypatch.setattr(repository, 'get',
                        lambda b: StoredAuthority(b, 0, empty_document(revision=0), 0.0))
    seen = [stamp()]
    repository.put_secret(boundary, 'mr-new-secret-value-0123456789')
    seen.append(stamp())
    repository.compare_and_swap(boundary, empty_document(revision=0), expected_revision=0)
    seen.append(stamp())
    repository.delete_secret(boundary, 'mrs_x')
    seen.append(stamp())
    assert seen[0] is None and None not in seen[1:]
    with open(path, encoding='utf-8') as handle:
        assert 'mr-new-secret' not in handle.read()


def test_no_plaintext_is_retained_and_values_are_never_logged(routed, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    redactor = routed.redaction.known_credential_redactor(owner_user_id=OWNER)
    assert redactor.redact(f'a {ENVELOPE_KEY} b') == f'a {ENVELOPE_MARKER} b'
    held = json.dumps(_reachable_strings(redactor))
    assert _leaks(held) == [], 'the redactor holds fingerprints, never plaintext'

    from lib.model_routing.change_notifications import mark_model_routing_secrets_changed

    def broken(_boundary):
        raise RuntimeError(f'storage down {RAW_KEY}')

    monkeypatch.setattr(routed.repository, 'get', broken)
    mark_model_routing_secrets_changed(user_id=OWNER, rev=3)
    again = routed.redaction.known_credential_redactor(owner_user_id=OWNER)
    assert again.redact(RAW_KEY) == RAW_MARKER, 'a failed refresh keeps the previous set'
    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'RuntimeError' in logged and _leaks(logged) == []


def _reachable_strings(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.hex(), value.decode('latin-1')]
    if isinstance(value, dict):
        return [s for k, v in value.items()
                for s in _reachable_strings(k, seen) + _reachable_strings(v, seen)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [s for item in value for s in _reachable_strings(item, seen)]
    if hasattr(value, '__dict__') or hasattr(value, '__slots__'):
        names = list(getattr(value, '__dict__', {})) + list(getattr(value, '__slots__', ()))
        if hasattr(value, '__dataclass_fields__'):
            names += list(value.__dataclass_fields__)
        return [s for name in names for s in _reachable_strings(getattr(value, name, None), seen)]
    return []


def test_large_result_spool_file_is_redacted_across_chunk_boundaries(routed, tmp_path):
    from lib.tool_results.tool_result_artifact_writer import ToolResultArtifactWriter
    stored = []

    class Repository:
        def put(self, *, user_id, content, media_type):
            stored.append(content)
            return {'artifactRef': 'artifact-1'}

    spool_dir = tmp_path / 'spool'
    spool_dir.mkdir()
    writer = ToolResultArtifactWriter(user_id=OWNER, threshold_chars=64,
                                      repository=Repository(), spool_dir=str(spool_dir))
    text = ('x' * 100 + RAW_KEY + 'y' * 37 + ENVELOPE_KEY + 'tail\n') * 3
    # Every key is split across writes, including at every offset inside it.
    for start in range(0, len(text), 7):
        assert writer.write(text[start:start + 7])
    assert writer.spooled
    on_disk = ''.join(path.read_text() for path in spool_dir.iterdir())
    assert _leaks(on_disk) == []
    result = writer.finalize()
    assert result.artifact_ref == 'artifact-1'
    assert stored and _leaks(stored[0]) == []
    assert stored[0] == text.replace(RAW_KEY, RAW_MARKER).replace(ENVELOPE_KEY, ENVELOPE_MARKER)


def test_request_authority_redacts_without_durable_repository_or_stamp(routed, monkeypatch):
    from lib.model_routing.request_authority import bind_request_routing_authority
    from lib import credentials_vault
    redaction = routed.redaction
    task = {}
    bind_request_routing_authority(task, routed.repository, routed.boundary)
    credentials_vault.set_entry('request_test', 'vault-request-secret-0123456789')
    config_key = 'server-config-request-key-0123456789'
    with open(redaction._server_config_path(), 'w', encoding='utf-8') as handle:
        json.dump({'providers': [{'id': 'configured', 'api_key': config_key}]}, handle)

    def forbidden(*args, **kwargs):
        pytest.fail('request-scoped redaction consulted durable routing storage')

    monkeypatch.setattr('lib.model_routing.known_secrets.ModelRoutingRepository', forbidden)
    monkeypatch.setattr(
        'lib.model_routing.change_notifications.model_routing_secrets_stamp_path', forbidden)
    with redaction.credential_redaction_scope(task=task, owner_user_id=OWNER):
        first = redaction.known_credential_redactor(owner_user_id=OWNER)
        assert first.redact(RAW_KEY) == RAW_MARKER
        assert first.redact(ENVELOPE_KEY) == ENVELOPE_MARKER
        assert first.redact('vault-request-secret-0123456789') != 'vault-request-secret-0123456789'
        assert first.redact(config_key) == '[redacted credential: provider configured]'
        assert _leaks(json.dumps(_reachable_strings(first))) == []
        with redaction.credential_redaction_scope(task=task, owner_user_id=OWNER):
            assert redaction.known_credential_redactor(owner_user_id=OWNER) is first
        assert redaction.redact_value({'output': RAW_KEY}, owner_user_id=OWNER) == {
            'output': RAW_MARKER}
        assert redaction.known_credential_redactor().redact(RAW_KEY) == RAW_KEY
        assert len(routed.reads) == 1
    assert set(redaction._cached) == {None}  # Only the owner-independent call.


def test_request_redaction_scope_isolates_runs_and_restores_after_failure(routed, monkeypatch):
    from lib.model_routing import InMemoryModelRoutingRepository, ModelRoutingError
    from lib.model_routing.request_authority import bind_request_routing_authority
    redaction = routed.redaction
    first_task, second_task = {}, {}
    bind_request_routing_authority(first_task, routed.repository, routed.boundary)
    second = InMemoryModelRoutingRepository()
    second_key = 'second-run-key-0123456789abcdef'
    second.put_secret(routed.boundary, second_key, secret_reference='secret-raw')
    second.compare_and_swap(routed.boundary, _document(), expected_revision=0)
    bind_request_routing_authority(second_task, second, routed.boundary)
    with redaction.credential_redaction_scope(task=first_task, owner_user_id=OWNER):
        original = redaction.known_credential_redactor(owner_user_id=OWNER)
        with pytest.raises(ModelRoutingError, match='credential owner'):
            redaction.known_credential_redactor(owner_user_id=2)
        with pytest.raises(ModelRoutingError):
            with redaction.credential_redaction_scope(task=first_task, owner_user_id=2):
                pytest.fail('foreign owner entered request authority')
        with pytest.raises(RuntimeError, match='scope failed'):
            with redaction.credential_redaction_scope(task=second_task, owner_user_id=OWNER):
                other = redaction.known_credential_redactor(owner_user_id=OWNER)
                assert other.redact(second_key) == RAW_MARKER
                assert other.redact(RAW_KEY) == RAW_KEY
                raise RuntimeError('scope failed')
        assert redaction.known_credential_redactor(owner_user_id=OWNER) is original
        assert original.redact(second_key) == second_key
    durable = redaction.known_credential_redactor(owner_user_id=OWNER)
    assert durable is not original
    assert durable.redact(RAW_KEY) == RAW_MARKER


def test_request_redaction_uses_the_issued_tenant_boundary(routed):
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.model_routing.request_authority import bind_request_routing_authority
    boundary = OwnerBoundary.create(OWNER, 'tenant-a')
    repository = InMemoryModelRoutingRepository()
    repository.compare_and_swap(boundary, _document(), expected_revision=0)
    repository.put_secret(boundary, RAW_KEY, secret_reference='secret-raw')
    task = {'_tenant_id': 'tenant-a'}
    bind_request_routing_authority(task, repository, boundary)
    with routed.redaction.credential_redaction_scope(task=task, owner_user_id=OWNER):
        assert routed.redaction.known_credential_redactor(owner_user_id=OWNER).redact(RAW_KEY) == RAW_MARKER


@pytest.mark.parametrize('source', ['routing', 'vault'])
def test_request_redaction_refuses_unavailable_credential_sources(routed, monkeypatch, source):
    from lib.model_routing import ModelRoutingError
    from lib.model_routing.request_authority import bind_request_routing_authority
    task = {}
    bind_request_routing_authority(task, routed.repository, routed.boundary)

    def unavailable(*args, **kwargs):
        raise RuntimeError('credential source unavailable')

    monkeypatch.setattr(routed.redaction,
        '_model_routing_values' if source == 'routing' else '_vault_values', unavailable)
    with pytest.raises(ModelRoutingError, match='credential recognition is unavailable'):
        with routed.redaction.credential_redaction_scope(task=task, owner_user_id=OWNER):
            pytest.fail('execution entered without credential recognition')
