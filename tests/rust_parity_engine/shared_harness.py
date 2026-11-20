"""Register the execution corpus with the shared differential harness.

The harness code is reused from parity owner checkpoint 0efbc876c3, not a
second replay engine. This corpus is separate from that owner's 537-session
private wire corpus: eight recorded LLM fixture paths plus scripted engine
faults are not 537 full-session execution replays. No behavioural difference
is normalised, skipped or turned green here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import tempfile

from tests.rust_parity.corpus import write_jsonl
from tests.rust_parity.runner import run_corpus

HERE = Path(__file__).resolve().parent


def write_engine_corpus(directory: Path) -> int:
    cases = [json.loads(line) for line in (HERE / 'cases.jsonl').read_text().splitlines()
             if line.strip()]
    return write_jsonl(directory / 'engine_session.jsonl.gz', (
        {'id': row['name'], 'session': row['origin'], 'input': row['input']} for row in cases
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--rust', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='tofu-shared-engine-parity-') as scratch:
        root = Path(scratch)
        write_engine_corpus(root)
        report = run_corpus(root, ['cmd:' + shlex.join([str(args.rust), 'parity'])],
                            boundaries=['engine_session'], check_recorded=False)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True) + '\n')
    print(json.dumps(report['summary'], indent=1))
    return 0 if report['summary']['green'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
