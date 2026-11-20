"""CLI: python -m evaluations.portability {families,run,cell,report}.

run     executes scenario x model x arm cells inline (one thread per cell) or, with
        --backend, submits each cell as a job through lib.compute (local, ssh,
        slurm, kubernetes, hope) and collects its results.jsonl artifact.
cell    the job entrypoint: run one cell into the current directory.
report  summarise run directories into JSON + Markdown.
families print the registered creator of each model (vendor diversity check).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from evaluations.portability.journal import COLLECTION_STAGE_PREFIX, read_journal

REPO = Path(__file__).resolve().parents[2]
# Poll period while waiting on a compute-backend cell; liveness only, results are unaffected.
JOB_POLL_SECONDS = 5


def _creators(models):
    from evaluations.portability.models import registry_models
    registry = registry_models()
    missing = [m for m in models if m not in registry]
    if missing:
        raise SystemExit(f'models not in the server model registry: {missing}')
    return {m: registry[m] for m in models}


def _existing_directory(directory: Path) -> Path:
    directory = directory.resolve()
    while not directory.is_dir():
        directory = directory.parent
    return directory


def _compute_jobs():
    try:
        from lib.compute.jobs import JobSpec, backend
    except ModuleNotFoundError as exc:
        if exc.name not in {'lib.compute', 'lib.compute.jobs'}:
            raise
        raise RuntimeError('--backend requires the optional lib.compute.jobs package; omit --backend to run inline') from exc
    return JobSpec, backend


def _cell_job(args, scenario, model, arm, creator, *, boundary: Path | None = None):
    from evaluations.portability.scenario import load_scenario

    JobSpec, backend = _compute_jobs()
    declaration = load_scenario(scenario)
    expected = {'scenario': declaration.id, **declaration.provenance(), 'model': model,
                'arm': arm, 'creator': creator or None, 'environment': args.environment}
    task_ids = {task['id'] for task in declaration.tasks}
    options = dict(item.split('=', 1) for item in args.backend_option)
    compute = backend(args.backend, **options)
    command = [args.python, '-m', 'evaluations.portability', 'cell', '--scenario', scenario,
               '--model', model, '--arm', arm, '--creator', creator or '', '--environment', args.environment]
    spec = JobSpec(name=f'port-{scenario}-{arm}-{model}'[:63], command=command,
                   env={'PYTHONPATH': args.remote_repo or str(REPO)}, outputs=['results.jsonl'],
                   config={'scenario': scenario, 'model': model, 'arm': arm},
                   resources={'cpu': 1, 'memory_gib': 2, 'gpu_count': 0}).validate()
    ref = compute.submit(spec)
    mismatches = compute.verify(spec.config, compute.effective_config(ref).get('config', {}))
    if mismatches:
        compute.cancel(ref)
        raise RuntimeError(f'effective config differs from intended: {mismatches}')
    while not (status := compute.status(ref)).terminal:
        time.sleep(JOB_POLL_SECONDS)
    target = Path(args.out) / 'cells' / f'{scenario}--{model}--{arm}--{ref.job_id}'
    if boundary is None:
        boundary = _existing_directory(target.parent)
    target.mkdir(parents=True, exist_ok=False)
    (target / 'job.json').write_text(json.dumps({'ref': ref.to_dict(), 'state': status.state,
                                                 'exit_code': status.exit_code}, indent=1))
    (target / 'job.log').write_text(compute.logs(ref))
    if status.state != 'succeeded':
        raise RuntimeError(f'cell job {ref.job_id} ended {status.state} (exit {status.exit_code})')
    _collect_cell(compute, ref, target, boundary, expected, task_ids)
    return target


def _collected_links(records: list[dict]) -> list[tuple[dict, str]]:
    links = []
    binding_fields = ('attempt_id', 'artifact_path', 'artifact_sha256')
    for record in records:
        if not any(field in record for field in binding_fields):
            if record.get('score') is not None and record.get('error_kind') is None and not record.get('error'):
                raise ValueError('Missing artifact binding in collected journal')
            continue
        if not all(isinstance(record.get(field), str) and record[field] for field in binding_fields):
            raise ValueError('Incomplete artifact binding in collected journal')
        reference = record['artifact_path']
        name = PurePosixPath(reference)
        if (name.is_absolute() or '..' in name.parts or name.parts[:1] != ('artifacts',)
                or len(name.parts) < 2 or name.as_posix() != reference
                or '\\' in reference or '\x00' in reference):
            raise ValueError('Artifact reference escapes the cell namespace')
        if name.name == 'results.jsonl':
            raise ValueError('Artifact reference uses the reserved journal name')
        links.append((record, reference))
    return links


def _sync_collected_file(path: Path) -> None:
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


class CollectionPublicationError(RuntimeError):
    """Report whether the backend cell journal became visible and was synced."""


def _validate_collected_cell(records: list[dict], expected: dict, task_ids: set[str]) -> None:
    from evaluations.portability.journal import comparison_namespace

    # A submitted cell writes one outcome per frozen task. The remote runtime
    # is measured there, not predicted from the collector's own environment.
    pending = task_ids.copy()
    runtime = None
    for record in records:
        task = record['task']
        for field, value in expected.items():
            if field not in record or record[field] != value:
                raise ValueError(f'Collected task {task!r} does not match intended cell field {field!r}')
        namespace = comparison_namespace(record)
        if namespace is None:
            raise ValueError(f'Collected task {task!r} lacks a valid comparison namespace')
        fingerprint = namespace['environment_fingerprint']
        if runtime is not None and runtime != fingerprint:
            raise ValueError(f'Collected task {task!r} has a different runtime environment')
        runtime = fingerprint
        if task not in task_ids:
            raise ValueError(f'Unexpected collected task {task!r}')
        if task not in pending:
            raise ValueError(f'Duplicate collected task {task!r}')
        pending.remove(task)
    if pending:
        raise ValueError(f'Collected journal is missing intended tasks: {sorted(pending)}')


def _prepare_collected_journal(compute, ref, stage: Path, target: Path, boundary: Path,
                               expected: dict, task_ids: set[str]) -> Path:
    from evaluations.portability.run import _sync_directories, valid_artifact

    downloads = stage / 'downloads'
    downloads.mkdir()
    compute.artifacts(ref, ['results.jsonl'], str(downloads))
    downloaded_journal = downloads / 'results.jsonl'
    if not downloaded_journal.is_file() or downloaded_journal.is_symlink():
        raise ValueError('Collected journal is not a regular file')
    # Validate and publish our own copy, never a file under the adapter destination.
    journal = stage / 'results.jsonl'
    shutil.copyfile(downloaded_journal, journal)
    records, issues = read_journal(journal)
    if issues:
        issue = issues[0]
        raise ValueError(f'Invalid collected journal row {issue["line"]}: {issue["kind"]}')
    _validate_collected_cell(records, expected, task_ids)
    links = _collected_links(records)
    _sync_collected_file(journal)
    if links:
        names = sorted({name for _, name in links})
        compute.artifacts(ref, names, str(downloads))
        for record, name in links:
            path = downloads / name
            if path.resolve() != path or not path.is_file() or not valid_artifact(downloaded_journal, record):
                raise ValueError('Collected artifact does not match its journal reference')
        for name in names:
            path = downloads / name
            _sync_collected_file(path)
            _sync_directories(path.parent, stage)
        (downloads / 'artifacts').rename(target / 'artifacts')
        _sync_directories(target, boundary)
    return journal


def _collect_cell(compute, ref, target: Path, boundary: Path, expected: dict, task_ids: set[str]) -> None:
    from evaluations.portability.run import _sync_directories

    target = target.resolve()
    destination = target / 'results.jsonl'
    staging = None
    published = False
    publication_error = cleanup_error = None
    try:
        # Stage verbatim backend names on the same filesystem as the journal.
        staging = tempfile.TemporaryDirectory(prefix=COLLECTION_STAGE_PREFIX, dir=target)
        journal = _prepare_collected_journal(compute, ref, Path(staging.name), target, boundary, expected, task_ids)
        journal.rename(destination)
        published = True
        _sync_directories(target, boundary)
    except Exception as exc:
        publication_error = exc
    finally:
        if staging is not None:
            try:
                # Clean up explicitly so a cleanup error cannot replace a publication error.
                staging.cleanup()
            except Exception as exc:
                cleanup_error = exc

    if publication_error is None and cleanup_error is None:
        return
    if not published:
        state = 'journal was not published (before rename)'
    elif publication_error is not None:
        state = 'journal is visible, but directory sync failed (durability uncertain)'
    else:
        state = 'journal is visible and directory sync succeeded'
    message = f'{destination}: {state}'
    if publication_error is not None:
        message += f': {type(publication_error).__name__}: {publication_error}'
    if cleanup_error is not None:
        message += f'; staging cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}'
    errors = [error for error in (publication_error, cleanup_error) if error is not None]
    cause = errors[0] if len(errors) == 1 else ExceptionGroup('collection publication and cleanup', errors)
    raise CollectionPublicationError(message) from cause


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m evaluations.portability')
    sub = parser.add_subparsers(dest='command', required=True)
    fam = sub.add_parser('families')
    fam.add_argument('--models', required=True)
    run = sub.add_parser('run')
    run.add_argument('--scenario', action='append', required=True)
    run.add_argument('--models', required=True, help='comma-separated registry model ids')
    run.add_argument('--arms', default='baseline,tofu')
    run.add_argument('--out', required=True)
    run.add_argument('--environment', default='inline', help='label recorded per record (matrix column)')
    run.add_argument('--backend', help='optional lib.compute backend name; default runs cells inline')
    run.add_argument('--backend-option', action='append', default=[], help='key=value adapter option')
    run.add_argument('--python', default=sys.executable)
    run.add_argument('--remote-repo', help='repository path as seen by the job (default: this checkout)')
    cell = sub.add_parser('cell')
    cell.add_argument('--scenario', required=True)
    cell.add_argument('--model', required=True)
    cell.add_argument('--arm', required=True)
    cell.add_argument('--creator', default='')
    cell.add_argument('--environment', default='inline')
    cell.add_argument('--out', default='.')
    rep = sub.add_parser('report')
    rep.add_argument('--results', action='append', required=True)
    rep.add_argument('--json')
    rep.add_argument('--markdown')
    args = parser.parse_args(argv)

    if args.command == 'families':
        print(json.dumps(_creators(args.models.split(',')), indent=1))
        return 0
    if args.command == 'cell':
        from evaluations.portability.run import run_cell
        from evaluations.portability.scenario import load_scenario
        run_cell(load_scenario(args.scenario), args.model, args.arm, Path(args.out),
                 creator=args.creator or None, environment=args.environment)
        return 0
    if args.command == 'report':
        from evaluations.portability.report import load, markdown, summarise
        summary = summarise(load(args.results))
        if args.json:
            Path(args.json).write_text(json.dumps(summary, indent=1))
        text = markdown(summary)
        if args.markdown:
            Path(args.markdown).write_text(text)
        print(text)
        return 0
    from evaluations.portability.run import run_cell
    from evaluations.portability.scenario import load_scenario
    if args.backend:
        try:
            _compute_jobs()
        except RuntimeError as exc:
            parser.error(str(exc))
    models = args.models.split(',')
    creators = _creators(models)
    if len(set(creators.values())) < 4:
        print(f'warning: {len(set(creators.values()))} distinct creators; the portability claim needs >= 4',
              file=sys.stderr)
    arms = args.arms.split(',')
    out = Path(args.out)
    # Capture the boundary before any worker creates shared ancestors.
    boundary = _existing_directory(out / 'cells' if args.backend else out.resolve().parent)
    cells = [(s, m, a) for s in args.scenario for m in models for a in arms]
    for name in args.scenario:
        unknown = set(arms) - set(load_scenario(name).arms())
        if unknown:
            raise SystemExit(f'scenario {name} has no arms {sorted(unknown)}')

    def execute(cell_key):
        scenario, model, arm = cell_key
        if args.backend:
            return _cell_job(args, scenario, model, arm, creators[model], boundary=boundary)
        return run_cell(load_scenario(scenario), model, arm, out, creator=creators[model],
                        environment=args.environment, sync_boundary=boundary)

    failures = 0
    # One worker per cell: cells are independent model streams, so none waits behind another.
    with ThreadPoolExecutor(max_workers=len(cells)) as pool:
        for cell_key, future in [(c, pool.submit(execute, c)) for c in cells]:
            try:
                print('done', cell_key, future.result(), flush=True)
            except Exception as exc:
                failures += 1
                print('FAILED', cell_key, f'{type(exc).__name__}: {exc}', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
