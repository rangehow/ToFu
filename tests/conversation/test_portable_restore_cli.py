"""Operator entry point on an already running disposable SQL authority."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conversation.test_portable_chat_package import source  # noqa: F401
from tests.storage.test_storage_authority_sql_contract import storage  # noqa: F401
__all__ = ['source', 'storage']


pytestmark = pytest.mark.api


def test_cli_preview_apply_and_conflict(source, storage, tmp_path):
    from lib.storage.connection_file import write_connection_document

    export, chats, *_ = source
    root = Path(__file__).resolve().parents[2]
    archive = tmp_path / 'chat.json'
    handoff = tmp_path / 'connection.json'
    (tmp_path / 'data').mkdir(exist_ok=True)
    connection = storage.connection_document(service_id='portable-fixture')
    write_connection_document(handoff, connection)
    if storage.status()['backend'] != 'sqlite':
        pytest.skip('standalone CLI fixture supplies only personal SQLite configuration')
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'PYTHONPATH': str(root),
           'TOFU_DATA_DIR': str(tmp_path), 'TOFU_DEPLOYMENT_MODE': 'personal',
           'TOFU_PERSONAL_STORAGE_BACKEND': 'sqlite'}

    def invoke(mode, *flags, owner=71):
        return subprocess.run([sys.executable, '-m', 'lib.conv_ref.portable_restore_cli', mode,
                               str(archive), '--owner', str(owner), '--connection-file', str(handoff),
                               '--data-directory', str(tmp_path / 'data'),
                               *flags], cwd=root, env=env, capture_output=True, text=True, timeout=15)

    archive.write_bytes(b'{')
    assert invoke('preview').returncode == 1
    archive.write_bytes(export())
    with_original = json.loads(invoke('preview').stdout)
    assert with_original['preview']['originals'] == 1
    assert with_original['apply_blocked'] == 'attachment_volume_unverified'
    assert 'confirmation' not in with_original
    assert invoke('apply', '--confirm', 'not-a-confirmation').returncode == 1
    archive.write_bytes(export(originals=()))
    preview = invoke('preview')
    assert preview.returncode == 0, preview.stderr
    report = json.loads(preview.stdout)
    assert report['target']['authorization_scope'] == 'privileged_sql_storage_operator'
    dest, = report['preview']['candidate']
    assert report['preview']['messages'] == 2
    assert not (tmp_path / 'data' / 'knowledge-files').exists()
    query = {'user_id': 71, 'conv_id': dest}
    assert storage.client.query('conversation.get', query) is None
    assert invoke('apply').returncode == 1
    flags = ('--confirm', json.loads(invoke('preview').stdout)['confirmation'])
    wrong_owner = invoke('apply', *flags, owner=72)
    assert wrong_owner.returncode == 1 and 'confirmation' not in json.loads(wrong_owner.stdout)
    write_connection_document(handoff, {**connection, 'host': 'localhost'})
    moved = invoke('apply', *flags)
    assert moved.returncode == 1 and 'apply_confirmation_does_not_match' in moved.stderr
    assert json.loads(moved.stdout)['target']['host'] == 'localhost'
    assert storage.client.query('conversation.get', query) is None
    write_connection_document(handoff, connection)
    applied = invoke('apply', *flags)
    assert applied.returncode == 0, applied.stderr
    receipt = json.loads(applied.stdout.splitlines()[-1])['applied']
    assert receipt['inserted'] == [dest]
    row = storage.client.query('conversation.get', query)
    assert [m['content'] for m in row['messages']] == ['你好', 'Synthetic answer']
    assert all(not m.get('_attemptId') and not m.get('_commandPending') for m in row['messages'])
    storage.client.command('conversation.metadata.update', {**query, 'updates': {'title': 'Local edit'}},
                           'fixture-local-title')
    row = storage.client.query('conversation.get', query)
    assert row['metadata']['title'] == 'Local edit'
    assert json.loads(invoke('apply', *flags).stdout.splitlines()[-1])['applied']['skipped'] == [dest]
    archive.write_bytes(export(('chat-a', 'chat-b'), ()))
    assert invoke('apply', *flags).returncode == 1
    flags = ('--confirm', json.loads(invoke('preview').stdout)['confirmation'])
    assert len(json.loads(invoke('apply', *flags).stdout.splitlines()[-1])['applied']['inserted']) == 1
    chats['chat-a'][0]['content'] = 'Changed source'
    archive.write_bytes(export(originals=()))
    conflict = invoke('preview')
    assert conflict.returncode == 1
    assert json.loads(conflict.stdout)['preview']['conflicts'] == [dest]
    assert storage.client.query('conversation.get', query) == row
    chats['chat-c'] = [{'role': 'user', 'content': 'Foreign collision'}]
    archive.write_bytes(export(('chat-c',), ()))
    candidate = json.loads(invoke('preview').stdout)
    foreign_id, = candidate['preview']['candidate']
    foreign = {'user_id': 72, 'conv_id': foreign_id}
    storage.client.command('conversation.create', {**foreign, 'title': 'Foreign fixture'},
                           'fixture-foreign-conversation')
    before = storage.client.query('conversation.get', foreign)
    refused = invoke('apply', '--confirm', candidate['confirmation'])
    assert refused.returncode == 1 and 'database_conflict' in refused.stderr
    assert storage.client.query('conversation.get', foreign) == before
    assert storage.client.query('conversation.get', {**foreign, 'user_id': 71}) is None
    assert storage.client.command('conversation.delete', query, 'fixture-trash')['deleted']
    archive.write_bytes(export(originals=()))
    tentative = json.loads(invoke('preview').stdout)
    assert tentative['preview']['candidate'] == [dest]
    refused = invoke('apply', '--confirm', tentative['confirmation'])
    assert refused.returncode == 1 and 'database_conflict' in refused.stderr
    assert storage.client.query('conversation.get', query) is None


def test_sql_frame_preflight_blocks_confirmation(source, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager
    from lib.conv_ref.portable_restore_cli import main
    from lib.storage.client import StorageClient
    from lib.storage import protocol

    export, *_ = source
    data = tmp_path / 'data'
    data.mkdir()
    archive = tmp_path / 'fixture.json'
    archive.write_bytes(export(originals=()))
    monkeypatch.setattr('runtime_path_policy.get_runtime_base', lambda: str(tmp_path))
    monkeypatch.setattr(protocol, 'MAX_FRAME_BYTES', 256)
    client = StorageClient('127.0.0.1', 1, 'x' * 48)
    monkeypatch.setattr(client, 'query', lambda *args: None)
    monkeypatch.setattr(client, 'command', lambda *a, **k: pytest.fail('unexpected dispatch'))

    @contextmanager
    def attach(_):
        yield client, {'backend': 'sqlite'}, lambda _: 'fixture-confirmation'

    args = [str(archive), '--owner', '71', '--connection-file', str(tmp_path / 'handoff'),
            '--data-directory', str(data)]
    try:
        assert main(['preview', *args], attach=attach) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['apply_blocked'] == 'storage_command_frame_not_encodable'
        assert 'confirmation' not in report
        assert main(['apply', *args, '--confirm', 'fixture-confirmation'], attach=attach) == 1
    finally:
        client.close()


def test_rotated_authenticated_handoff_requires_new_preview(source, tmp_path, monkeypatch, capsys):
    import threading
    from lib.conv_ref.portable_restore_cli import main
    from lib.storage.connection_file import write_connection_file
    from lib.storage_authority_sql.server import create_server
    from lib.storage_authority_sql.worker_control import WorkerControl

    class Backend:
        worker_control = WorkerControl()
        commands = []

        def health(self):
            return {'ready': True, 'backend': 'sqlite'}

        def query(self, *_):
            return None

        def command(self, *args, **kwargs):
            self.commands.append(args[0])

    export, *_ = source
    archive = tmp_path / 'fixture.json'
    archive.write_bytes(export(originals=()))
    data = tmp_path / 'data'
    data.mkdir()
    handoff = tmp_path / 'handoff.json'
    monkeypatch.setattr('runtime_path_policy.get_runtime_base', lambda: str(tmp_path))
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', 'sqlite')
    backend = Backend()
    server = create_server(backend, 'a' * 48)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args = [str(archive), '--owner', '71', '--connection-file', str(handoff),
            '--data-directory', str(data)]
    try:
        def publish(token):
            write_connection_file(handoff, host='127.0.0.1', port=server.server_address[1],
                                  token=token, backend='sqlite')

        publish('a' * 48)
        assert main(['preview', *args]) == 0
        before = json.loads(capsys.readouterr().out)
        server.token = 'b' * 48
        publish('b' * 48)
        assert main(['apply', *args, '--confirm', before['confirmation']]) == 1
        rejected = capsys.readouterr()
        assert 'apply_confirmation_does_not_match' in rejected.err
        assert json.loads(rejected.out)['target'] == before['target']
        assert not backend.commands
        assert main(['preview', *args]) == 0
        assert json.loads(capsys.readouterr().out)['confirmation'] != before['confirmation']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sql_command_frame_exact_production_boundary(monkeypatch):
    from lib.storage import protocol
    from lib.storage.client import StorageClient
    from lib.storage.errors import StorageError

    monkeypatch.setattr('lib.storage.client.time.time', lambda: 1_800_000_000)
    # Capture only the small empty-body frame to compute the envelope overhead.
    overhead = []

    def measure(frame):
        overhead.append(int.from_bytes(protocol.encode_frame(frame)[:4], 'big'))

    with monkeypatch.context() as patch:
        patch.setattr('lib.storage.client.encode_frame', measure)
        client = StorageClient('127.0.0.1', 1, 'x' * 48)
        client.check_command_frame('conversation.portable_import', {'padding': ''}, command_id='fixture')
    try:
        exact = 'x' * (protocol.MAX_FRAME_BYTES - overhead[0])
        client.check_command_frame('conversation.portable_import', {'padding': exact}, command_id='fixture')
        with pytest.raises(StorageError, match='exceeds the size limit'):
            client.check_command_frame('conversation.portable_import', {'padding': exact + 'x'}, command_id='fixture')
    finally:
        client.close()
