"""Derive exact restore flags from independently authenticated offline manifest bytes.

This reads one preserved manifest only; native restore owns full manifest validation,
backup-store selection under lease, and reconstruction. No store is opened here.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from pathlib import Path
import re


# backup.rs's v1 legacy/current manifest framing, including integrity footers.
_MANIFEST_LENGTHS = (185, 218)


def restore_selectors(manifest: Path, manifest_sha256: str,
                      source_control_generation: int | None = None) -> tuple[str, ...]:
    if not re.fullmatch(r'[0-9a-fA-F]{64}', manifest_sha256):
        raise ValueError('independent manifest SHA-256 must be 64 hex characters')
    with manifest.open('rb') as source:
        encoded = source.read(max(_MANIFEST_LENGTHS) + 1)
    digest = hashlib.sha256(encoded).hexdigest()
    if not hmac.compare_digest(digest, manifest_sha256.lower()):
        raise ValueError('preserved manifest SHA-256 conflicts with independent receipt')
    if (len(encoded) not in _MANIFEST_LENGTHS or encoded[:8] != b'TDBBKP01'
            or int.from_bytes(encoded[8:12], 'little') != 1):
        raise ValueError('unsupported backup manifest framing')
    generation = int.from_bytes(encoded[28:36], 'little')
    if not generation:
        raise ValueError('manifest has no source CONTROL generation')
    if source_control_generation is not None and source_control_generation != generation:
        raise ValueError('source CONTROL generation conflicts with authenticated manifest')
    return ('--manifest-sha256', digest, '--source-control-generation', str(generation))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True,
                        help='preserved offline manifest, not a live backup path')
    parser.add_argument('--manifest-sha256', required=True,
                        help='digest from an independently retained authenticated receipt')
    parser.add_argument('--source-control-generation', type=int,
                        help='cross-check if independent evidence also records the generation')
    args = parser.parse_args()
    try:
        flags = restore_selectors(args.manifest, args.manifest_sha256,
                                  args.source_control_generation)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    print(' '.join(flags))


if __name__ == '__main__':
    main()
