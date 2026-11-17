#!/usr/bin/env python3
"""One backfill pass: give every lesson written before ``trigger`` existed its trigger.

Usage:
  .venv/bin/python3 scripts/backfill_memory_triggers.py --store DIR [--store DIR ...]
      [--cache FILE] [--dry-run]

``--store`` is a lesson directory (a project's ``.tofu/memories``, the global
``<data>/memories/global``). Each flat lesson file without a ``trigger:`` line
gets one: from ``--cache`` when it holds that file's exact content (a JSON
object ``{sha256 of the file: {"trigger": ...}}``, as written by
``python -m evaluations.owner_memory triggers --stage backfill``), otherwise
from the production model dispatch with ``lib.memory.triggers``'
``TRIGGER_BACKFILL_SYSTEM``, which sees the lesson only. Skill packages are left
alone. The line is inserted into the file text (``with_trigger_line``), so no
other byte changes, and a file that changed after it was read is skipped.
``--dry-run`` reports what would be written and calls no model.

Prints one JSON summary. Exit: 0 done (failures are listed), 2 bad arguments.
Contract: docs/modules/context_engineering.md, "Lesson triggers".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ATTEMPTS = 3  # the dispatch retry budget for one unparseable reply


def _lesson_files(store: Path) -> list[Path]:
    from lib.memory.triggers import _FRONTMATTER_RE
    files = []
    for path in sorted(store.rglob('*.md')):
        if not path.is_file() or path.name == 'SKILL.md' or (path.parent / 'SKILL.md').is_file():
            continue
        if _FRONTMATTER_RE.match(path.read_text(encoding='utf-8', errors='replace')):
            files.append(path)
    return files


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _ask_model(text: str) -> str:
    from lib.llm_dispatch import dispatch_chat
    from lib.memory.triggers import TRIGGER_BACKFILL_SYSTEM, parse_trigger, trigger_backfill_input
    last = None
    for _ in range(ATTEMPTS):
        reply, _usage = dispatch_chat(
            [{'role': 'system', 'content': TRIGGER_BACKFILL_SYSTEM},
             {'role': 'user', 'content': trigger_backfill_input(text)}],
            max_tokens=4096, log_prefix='[TriggerBackfill]')
        try:
            return parse_trigger(reply or '')
        except ValueError as exc:
            last = exc
    raise ValueError(f'no usable trigger after {ATTEMPTS} replies: {last}')


def backfill(stores: list[Path], cache: dict, *, dry_run: bool, ask=_ask_model) -> dict:
    from lib.json_store import write_text_atomic
    from lib.memory.triggers import has_trigger_line, with_trigger_line
    summary = {'lessons': 0, 'already': 0, 'fromCache': 0, 'fromModel': 0, 'changedWhileRead': 0,
               'failed': []}
    for store in stores:
        for path in _lesson_files(store):
            summary['lessons'] += 1
            text = path.read_text(encoding='utf-8', errors='replace')
            if has_trigger_line(text):
                summary['already'] += 1
                continue
            trigger = (cache.get(_sha(text)) or {}).get('trigger')
            source = 'fromCache'
            if not trigger:
                if dry_run:
                    summary['fromModel'] += 1
                    continue
                try:
                    trigger, source = ask(text), 'fromModel'
                except Exception as exc:  # one lesson's failure never stops the pass
                    summary['failed'].append({'path': str(path), 'error': str(exc)[:200]})
                    continue
            summary[source] += 1
            if dry_run:
                continue
            if path.read_text(encoding='utf-8', errors='replace') != text:
                summary[source] -= 1
                summary['changedWhileRead'] += 1
                continue
            write_text_atomic(str(path), with_trigger_line(text, trigger),
                              mode=os.stat(path).st_mode & 0o777)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--store', action='append', required=True, help='lesson directory')
    parser.add_argument('--cache', default='', help='JSON of measured triggers keyed by file SHA-256')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    stores = [Path(store) for store in args.store]
    missing = [str(store) for store in stores if not store.is_dir()]
    if missing:
        print(json.dumps({'error': 'not a directory', 'stores': missing}))
        return 2
    cache = json.loads(Path(args.cache).read_text()) if args.cache else {}
    print(json.dumps(backfill(stores, cache, dry_run=args.dry_run), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
