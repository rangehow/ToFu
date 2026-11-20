"""Compressed JSONL support for the fixture-backed parity harness."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]

def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    count = 0
    with gzip.open(path, 'wt', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            count += 1
    return count


def read_jsonl(path: Path) -> Iterator[dict]:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def boundary_files(corpus: Path) -> dict[str, Path]:
    return {path.name[:-len('.jsonl.gz')]: path for path in sorted(corpus.glob('*.jsonl.gz'))
            if path.name != 'sessions.jsonl.gz'}
