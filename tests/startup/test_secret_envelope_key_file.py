"""The personal secret key is read once per key-file identity, never per secret."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def envelope(monkeypatch, tmp_path):
    import lib.secret_envelope as envelope

    monkeypatch.delenv('TOFU_SECRET_ENCRYPTION_KEY', raising=False)
    monkeypatch.setattr(envelope, '_KEY_PATH', tmp_path / 'config' / '.secret_encryption.key')
    reads = []
    real = envelope._read_or_create_personal_key
    monkeypatch.setattr(envelope, '_read_or_create_personal_key',
                        lambda: reads.append(1) or real())
    envelope.reset_secret_envelope_for_test()
    yield envelope, reads
    envelope.reset_secret_envelope_for_test()


def _seal(envelope, value='credential'):
    return envelope.seal_secret(value, purpose='test', owner_user_id=7, record_id='r')


def _open(envelope, ciphertext):
    return envelope.open_secret(ciphertext, purpose='test', owner_user_id=7, record_id='r')


def test_repeated_secrets_reuse_one_key_file_read(envelope):
    envelope, reads = envelope
    ciphertext = _seal(envelope)  # creates the key file
    assert _open(envelope, ciphertext) == 'credential'
    settled = len(reads)
    for _ in range(20):
        assert _open(envelope, ciphertext) == 'credential'
        _seal(envelope)
    assert settled <= 2 and len(reads) == settled


def test_replaced_key_file_is_read_again(envelope):
    from cryptography.fernet import Fernet
    envelope, reads = envelope
    old = _seal(envelope, 'old')
    assert _open(envelope, old) == 'old'
    replacement = envelope._KEY_PATH.with_name('replacement.key')
    replacement.write_bytes(Fernet.generate_key())
    os.replace(replacement, envelope._KEY_PATH)
    before = len(reads)
    new = _seal(envelope, 'new')
    assert len(reads) > before
    assert _open(envelope, new) == 'new'
    with pytest.raises(envelope.SecretEnvelopeError):
        _open(envelope, old)


def test_deleted_key_file_is_created_again(envelope):
    envelope, _reads = envelope
    _seal(envelope)
    _seal(envelope)
    envelope._KEY_PATH.unlink()
    ciphertext = _seal(envelope, 'fresh')
    assert envelope._KEY_PATH.exists()
    assert _open(envelope, ciphertext) == 'fresh'


def test_configured_key_reads_no_file(monkeypatch, envelope):
    from cryptography.fernet import Fernet
    envelope, reads = envelope
    monkeypatch.setenv('TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode('ascii'))
    assert _open(envelope, _seal(envelope)) == 'credential'
    assert reads == [] and not envelope._KEY_PATH.exists()
