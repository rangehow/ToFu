"""Verified offline MiSans fixture; explicit install alone may use networking.

Registry pins remain authoritative. Persistent files are content-addressed and
read-only; consumers copy, never link, them into their disposable font cache.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from lib.design_sys.fonts import get_font


def verified_bytes(path, source):
    data = Path(path).read_bytes()
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ValueError(f'MiSans fixture pin mismatch: {path}')
    return data


def install(destination, *, cached=None):
    """Explicit provisioning: reuse verified cache, otherwise shared HTTP owner."""
    destination = Path(destination)
    for source in get_font('misans').sources:
        target = destination / f'{source.sha256}.otf'
        if target.exists():
            verified_bytes(target, source)
            continue
        if cached is not None:
            data = verified_bytes(Path(cached) / f'w{source.weight}.otf', source)
        else:
            from lib.http_transport.client import http_get
            with http_get(source.url, timeout=120) as response:
                response.raise_for_status()
                data = response.content
            if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
                raise ValueError(f'MiSans download pin mismatch: {source.url}')
        destination.mkdir(parents=True, exist_ok=True)
        # Exclusive creation never overwrites an existing immutable asset.
        try:
            with target.open('xb') as stream:
                stream.write(data)
        except FileExistsError:
            verified_bytes(target, source)
        target.chmod(0o444)


def copy_to_cache(destination, fixture=None):
    """Offline only: fail clearly when explicit provisioning is missing."""
    fixture = Path(fixture or os.environ['TOFU_MISANS_FIXTURE'])
    destination = Path(destination)
    payloads = [(source, verified_bytes(fixture / f'{source.sha256}.otf', source))
                for source in get_font('misans').sources]
    destination.mkdir(parents=True, exist_ok=True)
    for source, data in payloads:
        (destination / f'w{source.weight}.otf').write_bytes(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--cached', help='existing MiSans cache (offline provisioning)')
    args = parser.parse_args()
    install(args.destination, cached=args.cached)
