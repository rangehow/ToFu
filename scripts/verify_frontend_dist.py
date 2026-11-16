#!/usr/bin/env python3
"""Fail packaging early unless every Vite entry and referenced file exists.

``--source-fingerprint`` instead prints the current build-input fingerprint
(``lib.vite_assets.source_authoring_fingerprint``) for lifecycle preparation.
"""

from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.vite_assets import (  # noqa: E402
    VITE_MANIFEST,
    ViteAssetError,
    source_authoring_fingerprint,
    validate_source_vite_artifact,
    validate_vite_artifact,
)

_OPTIONS = ('--authoring-freshness', '--source-fingerprint')


def main() -> int:
    arguments = sys.argv[1:]
    unknown = [argument for argument in arguments if argument not in _OPTIONS]
    if unknown:
        print(
            f'unknown frontend validation option(s): {", ".join(unknown)}',
            file=sys.stderr,
        )
        return 2
    if '--source-fingerprint' in arguments:
        print(source_authoring_fingerprint())
        return 0
    validator = (
        validate_source_vite_artifact
        if '--authoring-freshness' in arguments else
        validate_vite_artifact
    )
    try:
        manifest = validator()
    except ViteAssetError as exc:
        print(f'frontend artifact validation failed: {exc}', file=sys.stderr)
        return 1
    entries = sorted(key for key, row in manifest.items()
                     if isinstance(row, dict) and row.get('isEntry'))
    print(f'validated {VITE_MANIFEST}: {", ".join(entries)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
