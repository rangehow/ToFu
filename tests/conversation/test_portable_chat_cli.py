"""Build the installed offline artifact and execute it outside the checkout."""
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conversation.test_portable_chat_package import source  # noqa: F401
__all__ = ['source']

pytestmark = pytest.mark.unit


def test_installed_archive_reader(tmp_path, source):
    export, chats, _, _, original = source
    chats['chat-a'][1]['content'] = '<img src="https://example.invalid/no-fetch">\x1b[31m'
    builder = Path(__file__).resolve().parents[2] / 'scripts/build_portable_chat_reader.py'
    app = tmp_path / 'tofu-chat-archive.pyz'
    subprocess.run([sys.executable, '-I', str(builder), str(app)], cwd=tmp_path, check=True)
    archive = tmp_path / 'fixture.json'
    archive.write_bytes(export())

    def run(command, *args):
        return subprocess.run([sys.executable, '-I', '-S', str(app), command,
                               str(archive), *args], cwd=tmp_path, capture_output=True)

    assert run('verify').stdout == b'Verified: 1 chats, 1 originals\n'
    assert b'chat-a' in run('list').stdout
    opened = run('read', 'chat-a')
    assert opened.returncode == 0
    assert b'<img src="https://example.invalid/no-fetch">\\x1b[31m' in opened.stdout
    digest = hashlib.sha256(original).hexdigest()
    assert run('attachment', digest).stdout == original
    assert run('attachment', '../file').returncode != 0
    assert run('import').returncode != 0
    assert run('Continue').returncode != 0
    chats['chat-a'][1]['content'] = 'x' * (8 * 1024 * 1024 + 1)
    large_archive = export()
    archive.write_bytes(large_archive)
    assert run('verify').stdout == b'Verified: 1 chats, 1 originals\n'
    archive.write_bytes(large_archive[:-1])
    assert run('verify').returncode == 1
