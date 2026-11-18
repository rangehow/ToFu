#!/usr/bin/env python3
"""Run the staged document writing pipeline on a paper (or resume) directory.

Usage:
  scripts/document_pipeline.py run <doc_dir> [--draft MAIN.tex] [--writer-model M]
                                   [--workdir DIR] [--owner-user-id N] [--endpoint URL]
  scripts/document_pipeline.py status <workdir>
  scripts/document_pipeline.py rules <paper|resume|longform>
  scripts/document_pipeline.py venues
  scripts/document_pipeline.py check-venue <venue> [--workdir DIR]

<doc_dir> holds ``document_brief.json`` (and ``dossier.json``; optional when
revising an existing draft). ``--draft main.tex`` revises the LaTeX draft
already in <doc_dir> by targeted edits instead of writing from the dossier;
it is recorded in the brief (``draft.main``). ``--writer-model`` names the
writer for this run (``writer_model``). The default workdir is
``<doc_dir>/.pipeline``. A CLI process cannot open the running server's model
slots, so model calls go to the running Tofu server's OpenAI-compatible
endpoint (default http://127.0.0.1:15000/v1/chat/completions), which applies
its model routing, keys and accounting.

Exit status: 0 delivered; 2 a stage gate refused (stage, errors and artifact
folder printed; fix the input and rerun, unchanged stages resume); 1 the run
could not proceed (bad arguments, missing toolchain, network).
``check-venue`` fetches the venue's newest official kit and compiles its
official sample paper: 0 clean, 2 the sample did not compile.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_endpoint(endpoint: str) -> None:
    os.environ['SLIDES_BENCH_ENDPOINT'] = endpoint
    from evaluations.slides_bench import llm_bridge
    llm_bridge.ENDPOINT = endpoint
    llm_bridge.install()


def _status(workdir: Path) -> int:
    from lib.longform.documents.pipeline import STAGE_NAMES
    folder = workdir / 'artifacts'
    for index, name in enumerate(STAGE_NAMES, 1):
        path = folder / f'{index:02d}-{name}.json'
        print(f'{index:02d} {name:9s} {"artifact " + str(path) if path.is_file() else "-"}')
    models = folder / 'models.json'
    if models.is_file():
        print('\nmodel per call:')
        for row in json.loads(models.read_text(encoding='utf-8')):
            print(f'  {row["stage_call"]:24s} {row["role"]:6s} requested={row["requested"]} '
                  f'reported={row["reported"]}')
    return 0


def _rules(doc_type: str) -> int:
    from lib.longform.documents.rules import rules_for
    for rule in rules_for(doc_type):
        where = f' [stage {rule.stage}]' if rule.stage else ''
        print(f'{rule.id} ({rule.kind}{where}): {rule.text}\n    origin: {rule.origin}')
    return 0


def _venues() -> int:
    from lib.longform.documents.venues import VENUES
    for venue in VENUES.values():
        print(f'{venue.name:8s} body pages {venue.page_limit}; {venue.anonymity}\n'
              f'         source: {venue.landing or venue.archive}\n'
              f'         limit:  {venue.page_limit_source}')
    return 0


def check_venue(venue: str, workdir: Path) -> dict:
    """Fetch the newest official kit and compile its official sample."""
    from lib.longform.documents.venues import fetch_official_kit
    from lib.paper.latex_layout import compile_project
    kit = fetch_official_kit(venue, workdir / venue)
    result = compile_project(Path(kit['sample_dir']), main=kit['sample'])
    log = Path(kit['sample_dir']) / (Path(kit['sample']).stem + '.log')
    text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
    return {'venue': venue, 'year': kit['year'], 'landing_page': kit['landing_page'],
            'source_url': kit['source_url'], 'upstream_commit': kit['upstream_commit'],
            'archive_sha256': kit['archive_sha256'], 'kit_files': sorted(kit['files']),
            'sample': kit['sample'], 'compile_status': result.get('status'),
            'errors': result.get('errors') or [], 'pdf': result.get('pdf') or '',
            'overfull_boxes': text.count('\nOverfull '),
            'page_limit': kit['page_limit'], 'anonymity': kit['anonymity']}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('doc_dir')
    run.add_argument('--draft', help='existing LaTeX main file in doc_dir to revise')
    run.add_argument('--writer-model', help='writer model for this run')
    run.add_argument('--workdir')
    run.add_argument('--owner-user-id', type=int)
    run.add_argument('--endpoint', default='http://127.0.0.1:15000/v1/chat/completions')
    status = sub.add_parser('status')
    status.add_argument('workdir')
    rules = sub.add_parser('rules')
    rules.add_argument('doc_type')
    sub.add_parser('venues')
    venue = sub.add_parser('check-venue')
    venue.add_argument('venue')
    venue.add_argument('--workdir')
    args = parser.parse_args(argv)
    if args.command == 'status':
        return _status(Path(args.workdir))
    if args.command == 'rules':
        return _rules(args.doc_type)
    if args.command == 'venues':
        return _venues()
    if args.command == 'check-venue':
        import tempfile
        workdir = Path(args.workdir or tempfile.mkdtemp(prefix='venue-check-'))
        report = check_venue(args.venue, workdir)
        print(json.dumps(report, indent=2))
        return 0 if report['compile_status'] == 'success' and not report['errors'] else 2

    from lib.longform.documents.pipeline import run_document_pipeline
    from lib.production.stages import StageFailed
    _install_endpoint(args.endpoint)
    doc_dir = Path(args.doc_dir).resolve()
    workdir = Path(args.workdir).resolve() if args.workdir else doc_dir / '.pipeline'
    overrides = {}
    if args.draft:
        overrides['draft'] = {'main': args.draft}
    if args.writer_model:
        overrides['writer_model'] = args.writer_model

    def emit(event):
        if event.get('type') in ('stage_started', 'stage_done', 'stage_skipped',
                                 'stage_retry', 'stage_failed'):
            print(json.dumps({k: event.get(k) for k in (
                'type', 'stage', 'attempt', 'elapsed_s', 'errors', 'detail')
                if event.get(k) is not None}, ensure_ascii=False), flush=True)
    try:
        artifacts = run_document_pipeline(str(doc_dir), str(workdir),
                                          owner_user_id=args.owner_user_id, emit=emit,
                                          brief_overrides=overrides)
    except StageFailed as exc:
        print(f'REFUSED at stage {exc.stage!r}: {exc.detail}', file=sys.stderr)
        for item in exc.errors:
            print(f'  - {item}', file=sys.stderr)
        print(f'artifacts: {workdir / "artifacts"}', file=sys.stderr)
        return 2
    deliver = artifacts['deliver']
    manifest = deliver['manifest']
    print(json.dumps({'delivered': deliver['pdf'], 'output_dir': deliver['output_dir'],
                      'mode': manifest['mode'], 'blind': manifest['blind']['verdict'],
                      'review': manifest['review'],
                      'writers': sorted({row['reported'] for row in manifest['models_by_call']
                                         if row['role'] == 'writer'})},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
