"""Declared artifact contract, preflight and outcome taxonomy for headless deliverable runs.

Responsibility: one entry point (``launch``) that runs an existing deliverable
recipe from a declared contract (``contracts/deliverable_launch_v1.schema.json``)
outside the Chat UI. It owns contract validation, the model-free preflight and
the run outcome record; it owns no model loop, stage graph or quality rule —
each kind calls its recipe's public entry point (``build_deck_from_topic``,
``run_document_pipeline``) unchanged.

Entry points: ``load_contract``, ``preflight``, ``launch``, ``KINDS``.
Dependencies: the recipe packages (imported lazily per kind), the slide render
readiness probe and the LaTeX toolchain resolver. Model routing is a caller
port (``install_model_route``) invoked only after preflight passes, so a
failing preflight provably makes no model request.

Outcome taxonomy (shared with the Terminal-Bench runner's measured/infra split):

- ``scored``       the recipe reached its own terminal verdict; ``delivered``
                   says whether its final gate published the artifact (a gate
                   refusal is a measured non-delivery, not an error);
- ``timeout``      the declared ``budget.deadline_s`` expired (cooperative:
                   observed at the recipe's abort checks);
- ``setup_error``  a declared prerequisite is absent or unusable as declared
                   (missing/empty/mismatched input, unwritable output path,
                   tool or renderer not installed) — fix the contract or host;
- ``infra_error``  the environment failed underneath a valid contract
                   (storage I/O/quota/remote errors, renderer installed but
                   failing to launch, model endpoint unavailable);
- ``harness_error`` an unclassified exception in this launcher or a recipe:
                   a scored failure of the harness, never excluded as infra.

``setup_error`` and ``infra_error`` are not measurements; every other outcome is.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'CONTRACT_SCHEMA_VERSION', 'KINDS', 'MEASURED_OUTCOMES', 'OUTCOMES',
    'ContractError', 'exit_status', 'launch', 'load_contract', 'preflight',
]

CONTRACT_SCHEMA_VERSION = 1
OUTCOMES = ('scored', 'timeout', 'setup_error', 'infra_error', 'harness_error')
MEASURED_OUTCOMES = ('scored', 'timeout', 'harness_error')
RESULT_FILE = 'launch_result.json'

# Storage failing underneath a valid path (FUSE disconnects, remote I/O, quota)
# is infrastructure; permission/shape errors are the caller's declaration.
_INFRA_ERRNOS = frozenset(code for code in (
    errno.EIO, getattr(errno, 'EREMOTEIO', None), getattr(errno, 'EDQUOT', None),
    errno.ENOSPC, getattr(errno, 'ESTALE', None), errno.ENOTCONN,
    getattr(errno, 'ECOMM', None), errno.ETIMEDOUT) if code is not None)

_TOP_FIELDS = {'schema_version', 'kind', 'inputs', 'parameters', 'output_dir',
               'model', 'budget', 'owner_user_id', 'required_tools'}


class ContractError(ValueError):
    """The contract document itself is malformed (always ``setup_error``)."""


@dataclass(frozen=True)
class InputSpec:
    role: str
    shape: str          # 'file' | 'dir'
    required: bool


@dataclass(frozen=True)
class DeliverableKind:
    """What one recipe needs declared, and how to call it."""

    name: str
    inputs: tuple[InputSpec, ...]
    parameters: dict[str, type]
    required_parameters: tuple[str, ...]
    derived_inputs: Callable[[dict], list[tuple[str, Path, str]]]
    writable_paths: Callable[[dict], list[Path]]
    tools: tuple[dict, ...]
    renderer: Callable[[], dict]
    run: Callable[[dict, threading.Event, Callable[[dict], None]], dict]


def _check(name: str, ok: bool, outcome: str = '', detail: str = '') -> dict:
    row = {'check': name, 'ok': bool(ok)}
    if not ok:
        row['outcome'] = outcome
        row['detail'] = detail
    return row


def _oserror_outcome(exc: OSError) -> str:
    return 'infra_error' if exc.errno in _INFRA_ERRNOS else 'setup_error'


# ── Renderers (existing owners; the launcher only classifies) ───────────────

def _slides_renderer() -> dict:
    from lib.slides.readiness import SlidesRuntimeUnavailable, ensure_slides_runtime_ready
    try:
        ready = ensure_slides_runtime_ready()
    except SlidesRuntimeUnavailable as exc:
        return _check('renderer:slides-chromium', False,
                      'setup_error' if exc.missing else 'infra_error',
                      f'{exc} — {exc.hint}')
    return {'check': 'renderer:slides-chromium', 'ok': True,
            'browser': ready.get('browser', ''), 'python': ready['python']}


def _latex_renderer() -> dict:
    from lib.paper.latex_layout import latex_toolchain
    found = latex_toolchain()
    if not found:
        return _check('renderer:latexmk', False, 'setup_error',
                      'latexmk not found (repository .texlive missing and not on PATH)')
    return {'check': 'renderer:latexmk', 'ok': True, 'path': found}


# ── Kind: slides ────────────────────────────────────────────────────────────

def _read_text(path: str) -> str:
    return Path(path).read_text(encoding='utf-8') if path else ''


def _run_slides(contract: dict, abort_event: threading.Event, emit) -> dict:
    from lib.slides.recipe import build_deck_from_topic
    params = contract['parameters']
    kwargs = {key: params[key] for key in ('lang', 'style', 'creative_mode')
              if key in params}
    if 'max_pages' in params:
        kwargs['max_pages'] = params['max_pages']
    result = build_deck_from_topic(
        params['topic'], contract['output_dir'],
        model=contract['model'].get('name') or None,
        owner_user_id=contract.get('owner_user_id'),
        material=_read_text(contract['inputs'].get('material', '')),
        abort_event=abort_event, emit=emit, **kwargs)
    return {'artifact': result['pptx_path'], 'recipe_result': result,
            'summary': {'pptx': result['pptx_path'], 'pages': result.get('pages'),
                        'quality': result.get('quality'),
                        'visual_qa': {k: v for k, v in (result.get('qa') or {}).items()
                                      if k in ('ran', 'reason', 'repaired', 'findings')}}}


SLIDES = DeliverableKind(
    name='slides',
    inputs=(InputSpec('material', 'file', False),),
    parameters={'topic': str, 'lang': str, 'style': str, 'max_pages': int,
                'creative_mode': str},
    required_parameters=('topic',),
    derived_inputs=lambda contract: [],
    writable_paths=lambda contract: [Path(contract['output_dir'])],
    tools=(),
    renderer=_slides_renderer,
    run=_run_slides,
)


# ── Kind: document (staged paper / resume pipeline) ─────────────────────────

def _document_brief(contract: dict) -> dict:
    from lib.longform.documents.pipeline import BRIEF_FILE
    brief = json.loads((Path(contract['inputs']['doc_dir']) / BRIEF_FILE)
                       .read_text(encoding='utf-8'))
    if not isinstance(brief, dict):
        raise ValueError(f'{BRIEF_FILE} must hold a JSON object')
    return brief


def _document_inputs(contract: dict) -> list[tuple[str, Path, str]]:
    """Files the pipeline reads before its first model call, as declared."""
    from lib.longform.documents.pipeline import BRIEF_FILE, DOSSIER_FILE
    doc_dir = Path(contract['inputs']['doc_dir'])
    rows = [('document_brief', doc_dir / BRIEF_FILE, 'file')]
    draft = contract['parameters'].get('draft')
    if draft:
        rows.append(('draft', doc_dir / draft, 'file'))
    else:
        rows.append(('dossier', doc_dir / DOSSIER_FILE, 'file'))
    try:
        brief = _document_brief(contract)
    except (OSError, ValueError):
        return rows     # the brief row itself reports why
    for field, shape in (('previous_pdf', 'file'), ('template_dir', 'dir'),
                         ('main_template', 'file')):
        if brief.get(field) and not (field == 'previous_pdf' and draft):
            rows.append((f'brief.{field}', doc_dir / str(brief[field]), shape))
    return rows


def _document_writable(contract: dict) -> list[Path]:
    paths = [Path(contract['output_dir'])]
    try:
        brief = _document_brief(contract)
    except (OSError, ValueError):
        return paths
    if brief.get('output_dir'):
        # The deliver stage publishes beside the inputs, not in the workdir.
        paths.append(Path(contract['inputs']['doc_dir']) / str(brief['output_dir']))
    return paths


def _run_document(contract: dict, abort_event: threading.Event, emit) -> dict:
    from lib.longform.documents.pipeline import run_document_pipeline
    overrides = {}
    if contract['parameters'].get('draft'):
        overrides['draft'] = {'main': contract['parameters']['draft']}
    if contract['model'].get('name'):
        overrides['writer_model'] = contract['model']['name']
    artifacts = run_document_pipeline(
        contract['inputs']['doc_dir'], contract['output_dir'],
        owner_user_id=contract.get('owner_user_id'), emit=emit,
        abort_event=abort_event, brief_overrides=overrides,
        portfolio_policy=contract['parameters'].get('portfolio_policy'))
    deliver = artifacts['deliver']
    manifest = deliver['manifest']
    return {'artifact': deliver['pdf'], 'recipe_result': artifacts,
            'summary': {
                'delivered': deliver['pdf'], 'output_dir': deliver['output_dir'],
                'mode': manifest['mode'], 'blind': manifest['blind']['verdict'],
                'review': manifest['review'],
                'writers': sorted({row['reported'] for row in manifest['models_by_call']
                                   if row['role'] == 'writer'})}}


DOCUMENT = DeliverableKind(
    name='document',
    inputs=(InputSpec('doc_dir', 'dir', True),),
    parameters={'draft': str, 'portfolio_policy': str},
    required_parameters=(),
    derived_inputs=_document_inputs,
    writable_paths=_document_writable,
    tools=(),
    renderer=_latex_renderer,
    run=_run_document,
)

KINDS: dict[str, DeliverableKind] = {kind.name: kind for kind in (SLIDES, DOCUMENT)}


# ── Contract validation ─────────────────────────────────────────────────────

def load_contract(document: dict, *, base_dir: str | os.PathLike = '.') -> dict:
    """Validate a contract document; return it with absolute paths.

    Relative paths resolve against ``base_dir`` (the contract file's folder).
    Raises ``ContractError`` naming the offending field.
    """
    if not isinstance(document, dict):
        raise ContractError('contract must be a JSON object')
    unknown = sorted(set(document) - _TOP_FIELDS)
    if unknown:
        raise ContractError(f'unknown contract field(s): {", ".join(unknown)}')
    version = document.get('schema_version')
    if type(version) is not int or version != CONTRACT_SCHEMA_VERSION:
        raise ContractError(f'schema_version must be {CONTRACT_SCHEMA_VERSION}')
    kind_name = document.get('kind')
    kind = KINDS.get(kind_name) if type(kind_name) is str else None
    if kind is None:
        raise ContractError(f'kind must be one of {sorted(KINDS)}')
    base = Path(base_dir)

    def absolute(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f'{field} must be a non-empty path string')
        return str((base / os.path.expanduser(value)).absolute())

    inputs = document.get('inputs', {})
    if not isinstance(inputs, dict):
        raise ContractError('inputs must be an object of role -> path')
    roles = {spec.role: spec for spec in kind.inputs}
    extra = sorted(set(inputs) - set(roles))
    if extra:
        raise ContractError(f'kind {kind.name!r} declares no input role(s): {", ".join(extra)}')
    for spec in kind.inputs:
        if spec.required and spec.role not in inputs:
            raise ContractError(f'inputs.{spec.role} is required for kind {kind.name!r}')
    clean_inputs = {role: absolute(path, f'inputs.{role}') for role, path in inputs.items()}

    params = document.get('parameters', {})
    if not isinstance(params, dict):
        raise ContractError('parameters must be an object')
    for key, value in params.items():
        expected = kind.parameters.get(key)
        if expected is None:
            raise ContractError(f'kind {kind.name!r} has no parameter {key!r}')
        if type(value) is not expected:
            raise ContractError(f'parameters.{key} must be {expected.__name__}')
    for key in kind.required_parameters:
        if key not in params:
            raise ContractError(f'parameters.{key} is required for kind {kind.name!r}')

    model = document.get('model', {})
    if not isinstance(model, dict) or set(model) - {'name', 'endpoint'}:
        raise ContractError('model must be an object with optional name and endpoint')
    for key in ('name', 'endpoint'):
        if key in model and (not isinstance(model[key], str) or not model[key].strip()):
            raise ContractError(f'model.{key} must be a non-empty string')
    if 'endpoint' in model:
        parsed = urlparse(model['endpoint'])
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ContractError('model.endpoint must be an http(s) URL')

    budget = document.get('budget', {})
    if not isinstance(budget, dict) or set(budget) - {'deadline_s'}:
        raise ContractError('budget must be an object with optional deadline_s')
    deadline = budget.get('deadline_s')
    if deadline is not None and (isinstance(deadline, bool)
                                 or not isinstance(deadline, (int, float)) or deadline <= 0):
        raise ContractError('budget.deadline_s must be a positive number or null')

    owner = document.get('owner_user_id')
    if owner is not None and (isinstance(owner, bool) or not isinstance(owner, int)):
        raise ContractError('owner_user_id must be an integer or null')

    tools = document.get('required_tools', [])
    if not isinstance(tools, list):
        raise ContractError('required_tools must be a list')
    for index, tool in enumerate(tools):
        if (not isinstance(tool, dict) or len(tool) != 1
                or next(iter(tool)) not in ('executable', 'python_module')
                or not isinstance(next(iter(tool.values())), str)
                or not next(iter(tool.values())).strip()):
            raise ContractError(
                f'required_tools[{index}] must be {{"executable": name}} or '
                '{"python_module": name}')

    return {'schema_version': CONTRACT_SCHEMA_VERSION, 'kind': kind.name,
            'inputs': clean_inputs, 'parameters': dict(params),
            'output_dir': absolute(document.get('output_dir'), 'output_dir'),
            'model': dict(model), 'budget': {'deadline_s': deadline},
            'owner_user_id': owner, 'required_tools': [dict(t) for t in tools]}


# ── Preflight (no model request, ever) ──────────────────────────────────────

def _input_check(role: str, path: Path, shape: str) -> dict:
    name = f'input:{role}'
    try:
        if shape == 'dir':
            if not path.is_dir():
                return _check(name, False, 'setup_error', f'directory not found: {path}')
            os.listdir(path)
            return _check(name, True)
        if not path.is_file():
            return _check(name, False, 'setup_error', f'file not found: {path}')
        with open(path, 'rb') as handle:
            if not handle.read(1):
                return _check(name, False, 'setup_error', f'file is empty: {path}')
    except OSError as exc:
        return _check(name, False, _oserror_outcome(exc), f'{path}: {exc}')
    return _check(name, True)


def _writable_check(path: Path) -> dict:
    """Create, fsync, rename and remove a probe file on the real target filesystem."""
    name = f'writable:{path}'
    probe = path / f'.tofu-preflight-{os.getpid()}-{threading.get_ident()}'
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return _check(name, False, 'setup_error', f'not a directory: {path}')
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b'preflight\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        renamed = probe.with_suffix('.renamed')
        os.replace(probe, renamed)
        os.unlink(renamed)
    except OSError as exc:
        for leftover in (probe, probe.with_suffix('.renamed')):
            try:
                leftover.unlink()
            except OSError:
                pass
        return _check(name, False, _oserror_outcome(exc), f'{path}: {exc}')
    return _check(name, True)


def _tool_check(tool: dict) -> dict:
    (key, value), = tool.items()
    if key == 'executable':
        found = shutil.which(value)
        return (_check(f'tool:{value}', True) if found else
                _check(f'tool:{value}', False, 'setup_error', f'executable not on PATH: {value}'))
    import importlib.util
    try:
        found = importlib.util.find_spec(value) is not None
    except (ImportError, ValueError):
        found = False
    return (_check(f'module:{value}', True) if found else
            _check(f'module:{value}', False, 'setup_error', f'python module not importable: {value}'))


def preflight(contract: dict) -> dict:
    """Validate every declared prerequisite; return ``{'ok', 'outcome', 'checks'}``.

    Order: inputs, writable outputs, tools, renderer. All run (so one report
    lists every problem); the first failure's outcome attributes the run, with
    ``infra_error`` taking precedence because it invalidates the others.
    """
    kind = KINDS[contract['kind']]
    checks = []
    for spec in kind.inputs:
        if spec.role in contract['inputs']:
            checks.append(_input_check(spec.role, Path(contract['inputs'][spec.role]), spec.shape))
    if all(row['ok'] for row in checks):
        checks.extend(_input_check(role, path, shape)
                      for role, path, shape in kind.derived_inputs(contract))
    seen = set()
    for path in kind.writable_paths(contract):
        if path not in seen:
            seen.add(path)
            checks.append(_writable_check(path))
    checks.extend(_tool_check(tool) for tool in (*kind.tools, *contract['required_tools']))
    checks.append(kind.renderer())
    failed = [row for row in checks if not row['ok']]
    if not failed:
        return {'ok': True, 'outcome': None, 'checks': checks}
    outcome = ('infra_error' if any(row['outcome'] == 'infra_error' for row in failed)
               else 'setup_error')
    return {'ok': False, 'outcome': outcome, 'checks': checks}


# ── Launch ──────────────────────────────────────────────────────────────────

def _cause_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _classify(exc: BaseException, *, deadline_hit: bool,
              infra_exceptions: tuple) -> tuple[str, dict]:
    from lib.production.stages import StageAborted, StageFailed
    chain = list(_cause_chain(exc))
    for item in chain:
        if infra_exceptions and isinstance(item, infra_exceptions):
            return 'infra_error', {'detail': f'{type(item).__name__}: {item}'}
        if isinstance(item, OSError) and item.errno in _INFRA_ERRNOS:
            return 'infra_error', {'detail': f'{type(item).__name__}: {item}'}
    if isinstance(exc, StageAborted):
        if deadline_hit:
            return 'timeout', {'detail': str(exc)}
        return 'harness_error', {'detail': f'aborted without a deadline: {exc}'}
    if isinstance(exc, StageFailed):
        return 'scored', {'delivered': False, 'stage': exc.stage,
                          'detail': exc.detail, 'errors': [str(e) for e in exc.errors]}
    return 'harness_error', {'detail': f'{type(exc).__name__}: {exc}'}


def _contract_digest(contract: dict) -> str:
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode('utf-8')).hexdigest()


def _publish(contract: dict, record: dict) -> None:
    """Write the record atomically into output_dir when that directory is usable."""
    out = Path(contract['output_dir'])
    if not out.is_dir():
        return
    from lib.json_store import write_json_atomic
    try:
        write_json_atomic(str(out / RESULT_FILE), record)
        record['result_file'] = str(out / RESULT_FILE)
    except OSError as exc:
        record['result_file_error'] = f'{type(exc).__name__}: {exc}'


def launch(contract: dict, *, install_model_route: Callable[[dict], None],
           infra_exceptions: tuple = (), emit: Callable[[dict], None] | None = None) -> dict:
    """Preflight, then route models and run the kind's recipe; return the outcome record.

    ``install_model_route(model)`` is called once, only after preflight passed.
    ``infra_exceptions`` names route-port exceptions meaning "endpoint unavailable".
    """
    started = time.time()
    record = {'schema_version': CONTRACT_SCHEMA_VERSION, 'kind': contract['kind'],
              'contract_sha256': _contract_digest(contract), 'started_at': started,
              'model_route_installed': False}
    report = preflight(contract)
    record['preflight'] = report['checks']
    if not report['ok']:
        record.update(outcome=report['outcome'], delivered=False,
                      detail='; '.join(row['detail'] for row in report['checks'] if not row['ok']),
                      elapsed_s=round(time.time() - started, 3))
        _publish(contract, record)
        return record

    abort_event = threading.Event()
    deadline_hit = threading.Event()
    timer = None
    deadline = contract['budget']['deadline_s']
    deadline_at = time.monotonic() + deadline if deadline is not None else None
    if deadline is not None:
        def expire():
            deadline_hit.set()
            abort_event.set()
        timer = threading.Timer(deadline, expire)
        timer.daemon = True
        timer.start()
    try:
        install_model_route(contract['model'])
        record['model_route_installed'] = True
        result = KINDS[contract['kind']].run(contract, abort_event, emit or (lambda _e: None))
    except Exception as exc:  # classified into the taxonomy below, never swallowed silently
        outcome, fields = _classify(exc, deadline_hit=deadline_hit.is_set(),
                                    infra_exceptions=tuple(infra_exceptions))
        record.update(outcome=outcome, **fields)
        record.setdefault('delivered', False)
        if outcome == 'harness_error':
            logger.exception('[DeliverableLaunch] %s run raised', contract['kind'])
    else:
        expired = deadline_at is not None and time.monotonic() >= deadline_at
        record.update(outcome='timeout' if expired else 'scored', delivered=not expired,
                      artifact=result['artifact'], summary=result['summary'])
        if expired:
            record['detail'] = 'recipe returned after the explicit deadline'
            record['partial_artifact'] = result['artifact']
    finally:
        if timer is not None:
            timer.cancel()
    record['elapsed_s'] = round(time.time() - started, 3)
    _publish(contract, record)
    return record


def exit_status(record: dict) -> int:
    """0 delivered; 2 measured non-delivery; 1 not measured (setup/infra)."""
    if record['outcome'] == 'scored' and record.get('delivered'):
        return 0
    return 2 if record['outcome'] in MEASURED_OUTCOMES else 1
