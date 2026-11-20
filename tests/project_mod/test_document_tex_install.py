"""Offline, signed-metadata archive staging used by the explicit TeX installer."""
import hashlib

import pytest

from scripts import install_document_tex_packages as tex

pytestmark = pytest.mark.unit


def test_metadata_records_pin_archive_revision_and_checksum():
    assert tex.package_records('name lipsum\nrevision 1\ncontainerchecksum abc\n\n') == {
        'lipsum': {'revision': '1', 'containerchecksum': 'abc'}}


def test_cached_archive_requires_checksum_and_avoids_network(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'; archive.mkdir()
    data = b'verified archive'
    (archive / 'lipsum.tar.xz').write_bytes(data)
    row = {'revision': '1', 'containerchecksum': hashlib.sha512(data).hexdigest()}
    monkeypatch.setattr(tex, 'download', lambda url: pytest.fail('cache hit must be offline'))
    assert tex.stage_package(tmp_path, 'https://ctan.example', 'lipsum', row)['sha512'] == row['containerchecksum']


def test_corrupt_download_cannot_replace_archive(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'; archive.mkdir()
    target = archive / 'lipsum.tar.xz'; target.write_bytes(b'old')
    monkeypatch.setattr(tex, 'download', lambda url: (b'wrong', url))
    with pytest.raises(ValueError, match='signed CTAN'):
        tex.stage_package(tmp_path, 'https://ctan.example', 'lipsum', {
            'revision': '1', 'containerchecksum': hashlib.sha512(b'right').hexdigest()})
    assert target.read_bytes() == b'old'


def test_installer_refuses_temporary_prefix_before_network(tmp_path):
    with pytest.raises(ValueError, match='persistent storage'):
        tex.install(tmp_path)
