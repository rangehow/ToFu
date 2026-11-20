"""Archive list and retention use the same scoped order on every authority."""

from contextlib import closing
import sqlite3

import pytest

from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit


def test_scoped_archive_list_and_prune_tie_order(semantic_authority):
    factory, owner, _ = semantic_authority
    writer, reader = factory(write=True), factory(write=False)
    _assert_scoped_archive_order(writer, reader, owner)


@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_sqlite_utf16_archive_order(tmp_path, encoding):
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend, SQLiteSession
    from lib.storage_authority_sql.operations_pkg._archives import _archive_list
    from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
    from tests.storage_authority_sql.test_sqlite_read_pool import _config

    config = _config(tmp_path)
    with closing(sqlite3.connect(config.sqlite_path)) as connection:
        connection.execute(f"PRAGMA encoding='{encoding}'")
        connection.execute('CREATE TABLE encoding_fixture (id INTEGER)')
        assert connection.execute('PRAGMA encoding').fetchone()[0] == encoding

    backend = SQLiteBackend(config)
    backend.start()
    try:
        client = TransactionClient(backend)
        _assert_scoped_archive_order(client, client, 41)
    finally:
        backend.close()

    # The offline inspector also wraps a raw, read-only SQLite connection.
    with closing(sqlite3.connect(config.sqlite_path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        result = _archive_list(SQLiteSession(connection), {
            'conversation_id': 'archive-order', 'user_id': 41, 'archive_scope': '', 'limit': 2,
        })
        assert [archive['id'] for archive in result['archives']] == ['è', 'Ā']


def _assert_scoped_archive_order(writer, reader, owner):
    root = {'conversation_id': 'archive-order', 'user_id': owner}
    neighbor = root | {'conversation_id': 'archive-neighbor'}

    def command(operation, identity, command_id, **payload):
        return writer.command(operation, identity | payload, command_id)

    for identity in (root, neighbor):
        conv_id = identity['conversation_id']
        command('conversation.create', identity, conv_id, conv_id=conv_id)
    command('task_results.checkpoint', root, 'seed-parent', key='parent', expected_version=0,
            guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, require_parent=False,
            value={'task_id': 'parent', 'user_id': owner, 'conv_id': root['conversation_id'],
                   'status': 'done', 'created_at': 1})
    scope = command('invocation.register_child', root, 'register-child',
                    parent_task_id='parent', child_task_id='child')['archive_scope']
    child = root | {'archive_scope': scope}

    def create(identity, archive_id, created_at_ms):
        assert command('compaction_archive.create', identity, f'create-{archive_id}',
                       archive_id=archive_id, created_at_ms=created_at_ms, messages=[])['created']

    # Scramble insertion order. At timestamp 20 the UTF-8 lengths are 1, 2,
    # 2, and 3, and the ASCII two-byte ID sorts before the non-ASCII one.
    for archive_id, timestamp in [('é', 20), ('a', 30), ('文', 20), ('z', 20),
                                  ('🙂', 10), ('aa', 20)]:
        create(child, archive_id, timestamp)
    # Same UTF-8 length, but UTF-16LE byte ordering reverses this pair.
    create(root, 'Ā', 5)
    create(root, 'è', 5)
    create(neighbor, 'neighbor-only', 40)

    def listed(identity, **payload):
        response = reader.query('compaction_archive.list', identity | payload)
        return [archive['id'] for archive in response['archives']]

    assert listed(child) == ['🙂', 'z', 'aa', 'é', '文', 'a']
    assert listed(child, limit=3) == ['🙂', 'z', 'aa']
    assert command('compaction_archive.prune', child, 'prune-child', keep=3) == {'deleted': 3}
    assert listed(child) == ['é', '文', 'a']
    assert listed(root) == ['è', 'Ā']
    assert listed(neighbor) == ['neighbor-only']
