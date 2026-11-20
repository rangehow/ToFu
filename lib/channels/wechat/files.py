"""iLink FILE items and encrypted CDN transfer, separate from bot credentials."""
from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import secrets
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from lib.channels.bots.platform import failure
from lib.channels.media import safe_media_name
from lib.channels.model import ChannelMedia, WECHAT
from lib.channels.wechat import admission, protocol
from lib.agent_core.storage_errors import StorageError
from lib.http_transport.client import http_request


def _cdn_url(full: object, path: str, **params: str) -> str:
    if not full:
        return f'{WECHAT["cdnBase"]}/{path}?{urlencode(params)}'
    if not isinstance(full, str) or not full.startswith('https://') or any(c.isspace() for c in full):
        raise ValueError('Unexpected 微信 CDN URL')
    parsed = urlsplit(full)
    if (parsed.netloc != urlsplit(WECHAT['cdnBase']).netloc or parsed.fragment or '\\' in full):
        raise ValueError('Unexpected 微信 CDN URL')
    return full


def _padded_size(size: int) -> int:
    return (size // 16 + 1) * 16


def upload(http, api: str, token: str, peer: str, path: str, *, cdn=http_request, authorize=None) -> dict:
    """Return a FILE item, or a refusal. Until sendmessage, nothing reached the peer."""
    name = Path(path).name
    plain = Path(path).read_bytes()
    if not plain or not name or any(ord(c) < 32 or c in '\\\x7f' for c in name):
        return failure('attachment_rejected', '微信 requires a nonempty file with a safe name')
    key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
    try:
        status, info = admission.request(http, 'POST', api + '/ilink/bot/getuploadurl',
                            headers=protocol.bot_headers(token), require_object=True, authorize=authorize,
                            body={'filekey': filekey, 'media_type': 3, 'to_user_id': peer,
                                  'rawsize': len(plain), 'filesize': _padded_size(len(plain)),
                                  'rawfilemd5': hashlib.md5(plain, usedforsecurity=False).hexdigest(),
                                  'aeskey': key.hex(), 'no_need_thumb': True, 'base_info': protocol.base_info()})
    except admission.RefusedBeforeWire as error:
        return error.receipt
    except StorageError:
        raise
    except Exception:
        return failure('not_connected', '微信 file upload setup did not complete; no file message sent')
    code = info.get('ret') or info.get('errcode') or 0
    if status in (401, 403) or code == WECHAT['sessionExpiredCode']:
        return failure('auth_failed', '微信 refused the bot token')
    if status >= 500:
        return failure('not_connected', '微信 file upload setup temporarily unavailable')
    if status != 200 or code or not (info.get('upload_param') or info.get('upload_full_url')):
        return failure('attachment_rejected', '微信 refused file upload setup')
    try:
        url = _cdn_url(info.get('upload_full_url'), 'upload',
                       encrypted_query_param=info.get('upload_param') or '', filekey=filekey)
    except ValueError:
        return failure('attachment_rejected', '微信 returned an untrusted file upload URL')
    pad = padding.PKCS7(128).padder()
    encrypt = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encrypt.update(pad.update(plain) + pad.finalize()) + encrypt.finalize()
    try:
        with admission.request(
                cdn, 'POST', url, authorize=authorize, data=encrypted,
                headers={'Content-Type': 'application/octet-stream'}, allow_redirects=False, stream=True) as response:
            if 300 <= response.status_code < 500:
                return failure('attachment_rejected', '微信 CDN refused the file upload')
            param = response.headers.get('x-encrypted-param')
            if response.status_code != 200 or not param:
                return failure('not_connected', '微信 CDN did not acknowledge the upload; no file message sent')
    except admission.RefusedBeforeWire as error:
        return error.receipt
    except StorageError:
        raise
    except Exception:
        return failure('not_connected', '微信 CDN upload did not complete; no file message sent')
    return {'type': 4, 'file_item': {'file_name': name, 'len': str(len(plain)),
                                    'media': {'encrypt_query_param': param, 'encrypt_type': 1,
                                              'aes_key': base64.b64encode(key.hex().encode()).decode()}}}


def normalize_file(item: object) -> ChannelMedia | None:
    if not isinstance(item, Mapping) or item.get('type') != 4:
        return None
    file = item.get('file_item')
    file = file if isinstance(file, Mapping) else {}
    name = safe_media_name(file.get('file_name'), 'attachment')
    media = file.get('media')
    media = media if isinstance(media, Mapping) else {}
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    size = 0
    try:
        size = int(str(file.get('len')))
        if size < 0 or not media.get('aes_key') or media.get('encrypt_type', 1) != 1:
            raise ValueError
        if not (media.get('full_url') or media.get('encrypt_query_param')):
            raise ValueError
        source = _cdn_url(media.get('full_url'), 'download',
                          encrypted_query_param=media.get('encrypt_query_param') or '')
        return ChannelMedia('file', name, mime, size, source=source, token=str(media['aes_key']))
    except (ValueError, TypeError):
        return ChannelMedia('file', name, mime, max(0, size), status='failed',
                            detail='微信 file is missing a supported encrypted download reference or length')


def download(item: ChannelMedia, path: str, *, cdn=http_request) -> dict:
    """Check framing, length and padding; iLink ECB provides no authenticity tag."""
    try:
        key = base64.b64decode(item.token, validate=True)
        if len(key) == 32:
            key = bytes.fromhex(key.decode('ascii'))
        if len(key) != 16 or not item.source:
            raise ValueError
        url = _cdn_url(item.source, 'download')
        decrypt = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        unpad = padding.PKCS7(128).unpadder()
        total = 0
        with cdn('GET', url, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError
            with open(path, 'xb') as target:
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > _padded_size(item.size_bytes):
                        raise ValueError
                    target.write(unpad.update(decrypt.update(chunk)))
                target.write(unpad.update(decrypt.finalize()) + unpad.finalize())
                if total != _padded_size(item.size_bytes) or target.tell() != item.size_bytes:
                    raise ValueError
        return {'ok': True, 'contentType': item.mime_type}
    except (ValueError, binascii.Error, OSError):
        return {'ok': False, 'detail': '微信 CDN original could not be downloaded or decoded'}
