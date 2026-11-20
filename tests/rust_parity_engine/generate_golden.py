"""Generate engine golden parts from the current Python owner, never Rust."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

from tests.rust_parity_engine.reference import engine_session

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cases = [json.loads(line) for line in (HERE / 'cases.jsonl').read_text().splitlines() if line]
    stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        parts = {row['name']: {part['name']: base64.b64decode(part['b64'], validate=True).decode()
                               for part in engine_session(row['input'])} for row in cases}
    finally:
        sys.stdout = stdout
    document = {'generatedBy': 'python3 -m tests.rust_parity_engine.generate_golden', 'cases': parts}
    args.output.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    print(f'wrote {len(parts)} Python engine cases to {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
