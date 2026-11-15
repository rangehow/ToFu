#!/usr/bin/env python3
"""Responsibility: fetch a Chrome Web Store extension for the browser crash soak and
unpack it so Chromium loads it under its real store ID.

Entry: ``python3 scripts/browser_crash_repro/fetch_extension.py --id <id> --out DIR``.
Dependencies: network access to clients2.google.com (the store's update service).

The CRX3 header proves the package's ID (``signed_header_data.crx_id``); the
matching RSA public key is pinned as the manifest ``key`` so ``--load-extension``
keeps the store ID (some extensions branch on their own ID). Prints the
manifest name and version for the run record.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

UPDATE_URL = ('https://clients2.google.com/service/update2/crx?response=redirect'
              '&prodversion={version}&acceptformat=crx3&x=id%3D{id}%26uc')


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 2:
            length, offset = _varint(data, offset)
            yield number, data[offset:offset + length]
            offset += length
        elif wire == 0:
            _, offset = _varint(data, offset)
        else:
            raise ValueError(f'unsupported protobuf wire type {wire}')


def extension_id(public_key: bytes) -> str:
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    return digest.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))


def unpack_crx3(blob: bytes, expected_id: str) -> tuple[bytes, bytes]:
    if blob[:4] != b'Cr24' or struct.unpack('<I', blob[4:8])[0] != 3:
        raise ValueError('not a CRX3 package')
    header_size = struct.unpack('<I', blob[8:12])[0]
    header = blob[12:12 + header_size]
    keys, crx_id = [], b''
    for number, value in _fields(header):
        if number in (2, 3):  # sha256_with_rsa / sha256_with_ecdsa AsymmetricKeyProof
            keys += [key for field, key in _fields(value) if field == 1]
        elif number == 10000:
            crx_id = next((v for f, v in _fields(value) if f == 1), b'')
    declared = crx_id.hex().translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))
    if declared != expected_id:
        raise ValueError(f'package declares {declared}, expected {expected_id}')
    key = next((k for k in keys if extension_id(k) == expected_id), None)
    if key is None:
        raise ValueError('no header key matches the declared extension id')
    return key, blob[12 + header_size:]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--id', required=True)
    parser.add_argument('--out', required=True, help='directory to unpack into (replaced)')
    parser.add_argument('--chrome-version', default='155.0.8059.84')
    args = parser.parse_args(argv)
    with urllib.request.urlopen(UPDATE_URL.format(version=args.chrome_version, id=args.id), timeout=300) as response:
        blob = response.read()
    key, archive = unpack_crx3(blob, args.id)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        package.extractall(out)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    manifest['key'] = base64.b64encode(key).decode()
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'id': args.id, 'name': manifest.get('name'), 'version': manifest.get('version'),
                      'sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
