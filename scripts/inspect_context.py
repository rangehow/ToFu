#!/usr/bin/env python3
"""Inspect one exported request, or attribute drift between two, as content-free JSON.

Usage: .venv/bin/python3 scripts/inspect_context.py BEFORE.json [AFTER.json] [--wire]
Inputs: complete, unfolded Request Inspector snapshots (messages/contextManifest)
or raw provider JSON bodies (--wire). Wire inputs also accept an investigation
bundle: {request: providerBody, evidence: inspectorExport, cacheRecords: [...],
traceId: optionalTransportOccurrence}. See docs/CONTEXT_INJECTIONS.md.
No network, storage reads or model calls.
Exit 0: successful inspection (including differences); exit 2: invalid input/IO.
For a conversation ID, first use debug/inspect_conversation.py as documented.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.tasks_pkg.context_composer.inspection import compare_context, inspect_context


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path, nargs='?')
    parser.add_argument('--wire', action='store_true', help='Inputs are final provider request bodies')
    args = parser.parse_args(argv)
    try:
        before = json.loads(args.before.read_text(encoding='utf-8'))
        result = (compare_context(before, json.loads(args.after.read_text(encoding='utf-8')), wire=args.wire)
                  if args.after else inspect_context(before, wire=args.wire))
    except (OSError, UnicodeError, ValueError) as error:
        # JSONDecodeError includes location but never echo raw request bodies.
        print(f'Cannot inspect context: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
