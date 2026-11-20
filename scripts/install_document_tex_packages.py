#!/usr/bin/env python3
"""Add document-test packages to an existing persistent TeX Live installation.

All HTTP uses Tofu's shared transport. tlmgr reads a local, signed CTAN mirror;
its additive `install` action never updates/removes unrelated packages. The
mirror and receipt remain beside the persistent installation, not in TMPDIR.
Run explicitly; this installer is never invoked by tests or application boot.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CTAN = 'https://mirrors.ctan.org/systems/texlive/tlnet'
PACKAGES = ('latexmk', 'natbib', 'pgfplots', 'geometry', 'lipsum', 'xetex', 'ctex')


def download(url: str) -> tuple[bytes, str]:
    from lib.http_transport.client import http_get
    with http_get(url, timeout=120) as response:
        response.raise_for_status()
        return response.content, str(response.url)


def package_records(text: str) -> dict:
    return {block.splitlines()[0][5:]: dict(line.split(' ', 1)
            for line in block.splitlines() if line.startswith(
                ('revision ', 'containerchecksum ', 'containersize ')))
            for block in text.split('\n\n') if block.startswith('name ')}


def stage_package(root: Path, base: str, name: str, record: dict) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ValueError(f'invalid package name: {name}')
    expected = record['containerchecksum']
    target = root / 'archive' / f'{name}.tar.xz'
    url = f'{base}/archive/{target.name}'
    if target.is_file() and hashlib.sha512(target.read_bytes()).hexdigest() == expected:
        final = url
    else:
        data, final = download(url)
        if hashlib.sha512(data).hexdigest() != expected:
            raise ValueError(f'{name}: archive differs from signed CTAN database')
        target.write_bytes(data)
    return {'package': name, 'revision': record['revision'], 'url': url,
            'resolved': final, 'sha512': expected}


def install(location: Path) -> None:
    from lib.project_mod.toolchains import is_temporary_location
    location = location.resolve()
    if is_temporary_location(str(location)):
        raise ValueError('TeX installation must be on persistent storage, not TMPDIR')
    manager = location / 'bin/x86_64-linux/tlmgr'
    if not manager.is_file():
        raise ValueError(f'bootstrap the declared TeX Live installation first: {manager}')
    root = location.parent / 'src/document-tlnet'
    (root / 'tlpkg').mkdir(parents=True, exist_ok=True)
    (root / 'archive').mkdir(exist_ok=True)
    data, resolved = download(f'{CTAN}/tlpkg/texlive.tlpdb.xz')
    database = lzma.decompress(data)
    base = resolved.rsplit('/tlpkg/', 1)[0]
    for leaf in ('texlive.tlpdb.sha512', 'texlive.tlpdb.sha512.asc'):
        body, _ = download(f'{base}/tlpkg/{leaf}')
        (root / 'tlpkg' / leaf).write_bytes(body)
    expected = (root / 'tlpkg/texlive.tlpdb.sha512').read_text().split()[0]
    if hashlib.sha512(database).hexdigest() != expected:
        raise ValueError('CTAN database checksum mismatch; nothing installed')
    (root / 'tlpkg/texlive.tlpdb').write_bytes(database)
    # Explicit --verify-repo=all: no signature-verification bypass.
    command = [str(manager), '--repository', str(root), '--verify-repo=all', 'install']
    plan = subprocess.run([*command, '--dry-run', *PACKAGES], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(plan.stdout, flush=True)
    names = re.findall(r'install: ([A-Za-z0-9_.-]+) \[', plan.stdout)
    records = package_records(database.decode())
    receipt = {'retrieved': datetime.now(timezone.utc).isoformat(),
               'database_url': f'{CTAN}/tlpkg/texlive.tlpdb.xz', 'resolved': resolved,
               'database_sha512': expected, 'requested': list(PACKAGES),
               'installation': str(location), 'packages': []}
    if names:
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            receipt['packages'] = list(pool.map(
                lambda name: stage_package(root, base, name, records[name]), names))
    local_db = location / 'tlpkg/texlive.tlpdb'
    before = package_records(local_db.read_text())
    subprocess.run([*command, *PACKAGES], check=True)
    after = package_records(local_db.read_text())
    changed = [name for name, row in before.items() if after.get(name) != row]
    if changed:
        raise RuntimeError(f'pre-existing packages unexpectedly changed: {changed}')
    receipt['preserved_existing_packages'] = len(before)
    (root / 'document-install-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'Verified additive install; receipt: {root}/document-install-receipt.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--location', required=True, type=Path)
    install(parser.parse_args().location)
