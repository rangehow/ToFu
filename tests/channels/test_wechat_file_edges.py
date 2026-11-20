"""iLink filename, redirect and unverified file framing boundaries."""
import base64

import pytest

from lib.channels.wechat import files
from tests.channels.test_wechat_files import _response

pytestmark = pytest.mark.unit


def test_empty_file_refused_without_upload(tmp_path):
    path = tmp_path / 'empty.txt'
    path.touch()

    def no_network(*args, **kwargs):
        pytest.fail('empty file reached network')

    assert files.upload(no_network, 'unused', 'unused', 'unused', str(path), cdn=no_network)['error']['code'] == (
        'attachment_rejected')


def test_unicode_name_preserved_and_redirect_never_followed(tmp_path):
    item = files.normalize_file({'type': 4, 'file_item': {'file_name': '../年度报告📎.pdf', 'len': '16',
        'media': {'encrypt_query_param': 'fixture', 'aes_key': base64.b64encode(b'a' * 16).decode()}}})
    assert (item.name, item.mime_type) == ('年度报告📎.pdf', 'application/pdf')
    calls = []

    def redirect(method, url, **opts):
        assert not opts['allow_redirects']
        calls.append((method, url))
        return _response(302, headers={'Location': 'https://outside.example/'})

    target = tmp_path / 'original'
    assert files.download(item, str(target), cdn=redirect)['ok'] is False
    assert len(calls) == 1 and not target.exists()


def test_truncated_ciphertext_is_not_retained(tmp_path):
    item = files.normalize_file({'type': 4, 'file_item': {'file_name': 'proof.pdf', 'len': '16',
        'media': {'encrypt_query_param': 'fixture', 'aes_key': base64.b64encode(b'a' * 16).decode()}}})
    assert files.download(item, str(tmp_path / 'original'), cdn=lambda *a, **kw: _response(200, data=b'bad')) == {
        'ok': False, 'detail': '微信 CDN original could not be downloaded or decoded'}
