"""Deliverable launch contract: model-free preflight, outcome taxonomy, ported launchers.

Every failing-preflight case asserts that no model request happened: the
route port was never installed and the in-process dispatch seam was never
called. Normal paths run the real recipe stage graphs with fake model replies.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from lib.production import deliverable_launch as dl

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests' / 'longform' / 'fixtures'


class ModelSpy:
    """Route port + dispatch seam recorder: any model request is a failure."""

    def __init__(self, monkeypatch):
        self.routes = []
        self.requests = []
        import lib.llm_dispatch.api as api
        monkeypatch.setattr(api, 'dispatch_chat', self._dispatch)

    def install(self, model):
        self.routes.append(dict(model))

    def _dispatch(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        raise AssertionError('a model request reached dispatch')


@pytest.fixture
def spy(monkeypatch):
    return ModelSpy(monkeypatch)


@pytest.fixture
def renderers_ok(monkeypatch):
    """Renderers present (the real probes are covered by dedicated cases)."""
    import lib.paper.latex_layout as latex
    import lib.slides.readiness as readiness
    monkeypatch.setattr(readiness, 'ensure_slides_runtime_ready',
                        lambda: {'python': sys.executable, 'browser': 'stub'})
    monkeypatch.setattr(latex, 'latex_toolchain', lambda: '/usr/bin/latexmk')


def _slides(tmp_path, **overrides):
    material = tmp_path / 'material.md'
    if not material.exists():
        material.write_text('Team result: latency fell 41% after the rewrite.\n',
                            encoding='utf-8')
    document = {'schema_version': 1, 'kind': 'slides',
                'inputs': {'material': 'material.md'}, 'output_dir': 'out',
                'parameters': {'topic': '工作回顾'}, 'model': {'name': 'test-model'}}
    document.update(overrides)
    return dl.load_contract(document, base_dir=tmp_path)


def _sample_paper(tmp_path) -> Path:
    """The synthetic sample paper; without TeX its two PDFs are blank stand-ins."""
    if _tex_ready():
        sys.path.insert(0, str(FIXTURES))
        try:
            from build_sample_paper import build
        finally:
            sys.path.remove(str(FIXTURES))
        return build(tmp_path / 'paper')
    from pypdf import PdfWriter
    target = tmp_path / 'paper'
    shutil.copytree(FIXTURES / 'sample_paper', target,
                    ignore=shutil.ignore_patterns('draft'))
    for pdf in (target / 'figures' / 'loss_curve.pdf', target / 'previous' / 'previous.pdf'):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(pdf, 'wb') as handle:
            writer.write(handle)
    return target


def _document(paper: Path, tmp_path: Path, **overrides):
    document = {'schema_version': 1, 'kind': 'document',
                'inputs': {'doc_dir': str(paper)}, 'output_dir': str(tmp_path / 'work')}
    document.update(overrides)
    return dl.load_contract(document)


def _assert_no_model_request(record, spy):
    assert record['model_route_installed'] is False
    assert spy.routes == [] and spy.requests == []


def _failed(record, check_prefix):
    return [row for row in record['preflight']
            if not row['ok'] and row['check'].startswith(check_prefix)]


# ── Contract ────────────────────────────────────────────────────────────────

def test_schema_declares_exactly_the_fields_the_loader_accepts():
    schema = json.loads((ROOT / 'contracts' / 'deliverable_launch_v1.schema.json')
                        .read_text(encoding='utf-8'))
    assert set(schema['properties']) == dl._TOP_FIELDS
    assert schema['properties']['kind']['enum'] == sorted(dl.KINDS, reverse=True)
    declared = set(schema['properties']['parameters']['properties'])
    assert declared == {key for kind in dl.KINDS.values() for key in kind.parameters}
    assert schema['$defs']['outcome']['enum'] == list(dl.OUTCOMES)


@pytest.mark.parametrize('patch, message', [
    ({'kind': 'video'}, 'kind must be one of'),
    ({'schema_version': 2}, 'schema_version'),
    ({'inputs': {'slides': 'x'}}, 'declares no input role'),
    ({'parameters': {'max_pages': '10'}}, 'parameters.max_pages must be int'),
    ({'parameters': {}}, 'parameters.topic is required'),
    ({'model': {'endpoint': 'ftp://x'}}, 'http(s) URL'),
    ({'budget': {'deadline_s': 0}}, 'deadline_s'),
    ({'required_tools': [{'binary': 'x'}]}, 'required_tools[0]'),
    ({'surprise': 1}, 'unknown contract field'),
])
def test_malformed_contracts_name_the_field(tmp_path, patch, message):
    with pytest.raises(dl.ContractError, match=message.replace('(', r'\(').replace(')', r'\)')
                       .replace('[', r'\[').replace(']', r'\]')):
        _slides(tmp_path, **patch)


def test_relative_paths_resolve_against_the_contract_folder(tmp_path):
    contract = _slides(tmp_path)
    assert contract['inputs']['material'] == str(tmp_path / 'material.md')
    assert contract['output_dir'] == str(tmp_path / 'out')


def test_document_contract_preserves_explicit_invocation_policy(tmp_path):
    document = {'schema_version': 1, 'kind': 'document',
                'inputs': {'doc_dir': 'paper'}, 'output_dir': 'work',
                'model': {'name': 'writer', 'endpoint': 'http://127.0.0.1:9/v1'},
                'owner_user_id': 42,
                'parameters': {'portfolio_policy': 'owner-ccf-a', 'draft': 'draft.tex'}}
    contract = dl.load_contract(document, base_dir=tmp_path)
    assert contract['inputs'] == {'doc_dir': str(tmp_path / 'paper')}
    assert contract['output_dir'] == str(tmp_path / 'work')
    assert contract['model'] == document['model']
    assert contract['owner_user_id'] == 42
    assert contract['parameters'] == document['parameters']
    assert dl.load_contract({**document, 'parameters': {}}, base_dir=tmp_path)['parameters'] == {}
    with pytest.raises(dl.ContractError, match='parameters.portfolio_policy must be str'):
        dl.load_contract({**document, 'parameters': {'portfolio_policy': None}},
                         base_dir=tmp_path)


# ── Injected preflight fixtures: caught before any model request ────────────

def test_missing_artifact_is_setup_error(tmp_path, spy, renderers_ok):
    contract = _slides(tmp_path, inputs={'material': 'absent.md'})
    record = dl.launch(contract, install_model_route=spy.install)
    assert record['outcome'] == 'setup_error' and record['delivered'] is False
    assert _failed(record, 'input:material')[0]['detail'].startswith('file not found')
    _assert_no_model_request(record, spy)
    assert json.loads((tmp_path / 'out' / dl.RESULT_FILE).read_text())['outcome'] == 'setup_error'
    assert dl.exit_status(record) == 1


def test_empty_artifact_is_setup_error(tmp_path, spy, renderers_ok):
    (tmp_path / 'material.md').write_text('')
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert 'empty' in _failed(record, 'input:material')[0]['detail']
    _assert_no_model_request(record, spy)


def test_document_inputs_named_by_the_brief_are_checked(tmp_path, spy, renderers_ok):
    paper = _sample_paper(tmp_path)
    (paper / 'dossier.json').unlink()
    (paper / 'previous' / 'previous.pdf').unlink()
    record = dl.launch(_document(paper, tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert {row['check'] for row in record['preflight'] if not row['ok']} == {
        'input:dossier', 'input:brief.previous_pdf'}
    _assert_no_model_request(record, spy)


def test_output_path_under_a_file_is_setup_error(tmp_path, spy, renderers_ok):
    (tmp_path / 'blocker').write_text('a file, not a folder')
    record = dl.launch(_slides(tmp_path, output_dir='blocker/out'),
                       install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert _failed(record, 'writable:')
    _assert_no_model_request(record, spy)


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_read_only_output_folder_is_setup_error(tmp_path, spy, renderers_ok):
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0o500)
    try:
        record = dl.launch(_slides(tmp_path, output_dir='locked'),
                           install_model_route=spy.install)
    finally:
        locked.chmod(0o700)
    assert record['outcome'] == 'setup_error'
    assert 'Permission denied' in _failed(record, 'writable:')[0]['detail']
    assert list(locked.iterdir()) == []
    _assert_no_model_request(record, spy)


@pytest.mark.parametrize('code', [errno.EIO, errno.EDQUOT, errno.ENOTCONN])
def test_storage_fault_on_the_target_filesystem_is_infra_error(
        tmp_path, spy, renderers_ok, monkeypatch, code):
    real_open = os.open

    def failing_open(path, flags, *args):
        if '.tofu-preflight-' in str(path):
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(dl.os, 'open', failing_open)
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'infra_error'
    assert dl.exit_status(record) == 1
    _assert_no_model_request(record, spy)


def test_missing_slide_renderer_is_setup_error(tmp_path, spy, monkeypatch):
    import lib.slides.readiness as readiness

    def missing():
        raise readiness.SlidesRuntimeUnavailable('cannot import playwright.sync_api (x)')

    monkeypatch.setattr(readiness, 'ensure_slides_runtime_ready', missing)
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert 'playwright' in _failed(record, 'renderer:slides-chromium')[0]['detail']
    _assert_no_model_request(record, spy)


def test_slide_renderer_that_fails_to_launch_is_infra_error(tmp_path, spy, monkeypatch):
    import lib.slides.readiness as readiness

    def crashes():
        raise readiness.SlidesRuntimeUnavailable('Chromium smoke launch failed (x)',
                                                 missing=False)

    monkeypatch.setattr(readiness, 'ensure_slides_runtime_ready', crashes)
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'infra_error'
    _assert_no_model_request(record, spy)


def test_missing_latex_renderer_is_setup_error_with_the_real_resolver(
        tmp_path, spy, monkeypatch):
    import lib.paper.latex_layout as latex
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    monkeypatch.setattr(latex, '_TEXROOT', tmp_path / 'no-texlive')
    paper = _sample_paper(tmp_path)
    record = dl.launch(_document(paper, tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert 'latexmk not found' in _failed(record, 'renderer:latexmk')[0]['detail']
    _assert_no_model_request(record, spy)


def test_missing_declared_tool_is_setup_error(tmp_path, spy, renderers_ok):
    contract = _slides(tmp_path, required_tools=[
        {'executable': 'tofu-definitely-not-installed'},
        {'python_module': 'tofu_definitely_not_a_module'}, {'python_module': 'json'}])
    record = dl.launch(contract, install_model_route=spy.install)
    assert record['outcome'] == 'setup_error'
    assert {row['check'] for row in record['preflight'] if not row['ok']} == {
        'tool:tofu-definitely-not-installed', 'module:tofu_definitely_not_a_module'}
    _assert_no_model_request(record, spy)


# ── Run outcomes ────────────────────────────────────────────────────────────

_OUTLINE = json.dumps({'title': 'T', 'scenario': 'tech-engineering', 'pages': [
    {'pageType': 'cover', 'purpose': 'p', 'key_message': '标题判断',
     'layout_hint': '', 'content_notes': 'n'},
    {'pageType': 'content', 'purpose': 'p2', 'key_message': '第二页判断',
     'layout_hint': '', 'content_notes': 'n2'},
    {'pageType': 'final', 'purpose': 'p3', 'key_message': '结论',
     'layout_hint': '', 'content_notes': 'n3'}]}, ensure_ascii=False)
_PAGE = ('pageType: content\nbackground: {type: solid, color: "$bg"}\nelements:\n'
         '  - elementId: t\n    elementType: text\n    bounds: [72, 72, 1136, 120]\n'
         '    content:\n      style: "$title"\n      text: |\n        <p>标题</p>\n')


@pytest.fixture
def fake_slide_models(monkeypatch, tmp_path):
    """The model seams answer locally; real export gets isolated pinned fonts."""
    from tests.support.misans_fixture import copy_to_cache
    from lib.design_sys import fonts
    cache = tmp_path / 'fonts'
    copy_to_cache(cache / 'misans')
    monkeypatch.setattr(fonts, '_store_dir', lambda: str(cache))
    import lib.design_sys.visual_qa as vqa
    import lib.slides.author as author
    import lib.slides.recipe as recipe
    calls = []

    def llm(messages, **_kw):
        calls.append(messages[0]['content'][:20])
        text = messages[0]['content']
        return (_OUTLINE if ('大纲' in text or 'outline' in text) else _PAGE), {}

    monkeypatch.setattr(recipe, '_llm_chat', llm)
    monkeypatch.setattr(author, '_llm', llm)
    monkeypatch.setattr(recipe, '_run_research',
                        lambda ctx: {'cards': [], 'degraded': True, 'reason': 'test'})
    monkeypatch.setattr(recipe, '_run_render',
                        lambda ctx: {'previews': [], 'failed': [], 'skipped': True})
    monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (False, 'test'))
    monkeypatch.setattr('lib.slides._asset_preflight.prepare_deck_assets',
                        lambda *a, **k: {'by_page': {}, 'records': [], 'findings': []})
    return calls


def test_slides_normal_path_delivers_through_the_launcher(
        tmp_path, spy, renderers_ok, fake_slide_models):
    record = dl.launch(_slides(tmp_path, model={'endpoint': 'http://127.0.0.1:9/v1'}),
                       install_model_route=spy.install)
    assert record['outcome'] == 'scored' and record['delivered'] is True, record
    assert os.path.isfile(record['artifact']) and record['artifact'].endswith('.pptx')
    assert record['summary']['pages'] == 3
    assert spy.routes == [{'endpoint': 'http://127.0.0.1:9/v1'}]
    assert fake_slide_models, 'the recipe really ran its model stages'
    assert all(row['ok'] for row in record['preflight'])
    assert dl.exit_status(record) == 0


def test_deadline_expiry_is_timeout(tmp_path, spy, renderers_ok, fake_slide_models,
                                    monkeypatch):
    import lib.slides.recipe as recipe

    def slow_research(ctx):
        time.sleep(0.5)
        return {'cards': [], 'degraded': True, 'reason': 'test'}

    monkeypatch.setattr(recipe, '_run_research', slow_research)
    record = dl.launch(_slides(tmp_path, budget={'deadline_s': 0.05}),
                       install_model_route=spy.install)
    assert record['outcome'] == 'timeout' and record['delivered'] is False
    assert dl.exit_status(record) == 2


def test_endpoint_outage_inside_a_stage_is_infra_not_a_gate_refusal(
        tmp_path, spy, renderers_ok, fake_slide_models, monkeypatch):
    import lib.slides.recipe as recipe

    class EndpointDown(RuntimeError):
        pass

    def down(messages, **_kw):
        raise EndpointDown('model endpoint failed: connection refused')

    monkeypatch.setattr(recipe, '_llm_chat', down)
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install,
                       infra_exceptions=(EndpointDown,))
    assert record['outcome'] == 'infra_error', record


def test_stage_failed_keeps_the_last_attempts_exception_as_cause():
    from lib.production.stages import Stage, StageFailed

    def boom(_ctx):
        raise ConnectionError('upstream reset')

    with pytest.raises(StageFailed) as caught:
        _run_one(Stage('s', boom, retry=1))
    assert isinstance(caught.value.__cause__, ConnectionError)

    with pytest.raises(StageFailed) as refused:
        _run_one(Stage('g', lambda _c: {'x': 1}, gate=lambda _c, _a: ['bad']))
    assert refused.value.__cause__ is None and refused.value.errors == ['bad']


def _run_one(stage):
    import tempfile

    from lib.production.stages import run_stages
    with tempfile.TemporaryDirectory() as folder:
        run_stages([stage], {}, state_path=os.path.join(folder, 'state.json'))


def test_unclassified_exception_is_a_measured_harness_error(
        tmp_path, spy, renderers_ok, monkeypatch):
    import lib.slides.recipe as recipe
    monkeypatch.setattr(recipe, 'build_deck_from_topic',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('bug')))
    record = dl.launch(_slides(tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'harness_error' and dl.exit_status(record) == 2
    assert record['model_route_installed'] is True


def _tex_ready() -> bool:
    import subprocess
    if not shutil.which('latexmk'):
        return False
    probe = subprocess.run(['kpsewhich', 'pgfplots.sty', 'natbib.sty'],
                           capture_output=True, text=True, check=False)
    return probe.stdout.count('.sty') == 2


@pytest.fixture
def fake_document_models(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from lib.longform.documents import pipeline as pl
    from tests.longform.test_document_pipeline import _fake_acl_kit, _fake_chat
    calls = []
    monkeypatch.setattr(pl, '_fetch_kit', _fake_acl_kit)
    # This launcher fixture deliberately ships a synthetic ACL-like style,
    # not the official pin. Conformance/freshness have their own failing-first
    # suite; keep this recipe/launcher test hermetic (no live venue requests).
    monkeypatch.setattr(pl, '_check_freshness', lambda eid: {'verdict': 'current'})
    def conformance(ctx, build, main):
        # Model the real gate's evidence from the actual compiled PDF, not a
        # made-up digest or a bypass of the mandatory delivery proof.
        pdf = Path(build) / f'{Path(main).stem}.pdf'
        return {'passed': True, 'submission_ready': True, 'rows': [], 'failures': [],
                'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()}
    monkeypatch.setattr(pl, '_conformance', conformance)
    monkeypatch.setattr(pl, '_academic_search', lambda q: (
        [{'title': 'Close 2025 Work', 'year': 2025, 'arxiv_id': '2501.00001',
          'authors': ['A. Author'], 'abstract': 'stochastic rounding'}],
        [{'source': 'arxiv', 'status': 'available'}]))
    monkeypatch.setattr(pl, '_chat', lambda ctx, role, messages, label: (
        calls.append(label), _fake_chat(ctx, role, messages, label))[1])
    return calls


def test_document_normal_path_runs_the_real_pipeline_through_the_launcher(
        tmp_path, spy, renderers_ok, fake_document_models):
    """Without TeX the render stage refuses (a measured outcome); with it, delivery."""
    paper = _sample_paper(tmp_path)
    record = dl.launch(_document(paper, tmp_path), install_model_route=spy.install)
    assert spy.routes == [{}]
    assert {'evidence', 'outline'} <= set(fake_document_models), fake_document_models
    if _tex_ready():
        assert record['outcome'] == 'scored' and record['delivered'] is True, record
        assert record['artifact'].endswith('.pdf') and os.path.isfile(record['artifact'])
    else:
        assert record['outcome'] == 'scored' and record['delivered'] is False, record
        assert record['stage'] == 'render'
    assert (tmp_path / 'work' / 'artifacts' / '01-intake.json').is_file()


def test_slides_benchmark_arm_launches_through_the_contract(
        tmp_path, spy, renderers_ok, fake_slide_models, monkeypatch):
    import evaluations.slides_bench.llm_bridge as bridge
    import evaluations.slides_bench.run_tofu as run_tofu
    installs = []
    monkeypatch.setattr(bridge, 'install_model_route', lambda model: installs.append(model))
    monkeypatch.setattr(run_tofu, 'load_document', lambda doc_id: {
        'title': '回顾', 'lang': 'zh', 'pages': 3, 'material_for_tofu': 'Material.\n',
        'text': 'source text'})
    monkeypatch.setattr(run_tofu, '_snapshot_authored_deck', lambda out_dir: None)
    monkeypatch.setattr(run_tofu, '_vision_through_bridge', lambda: None)
    import lib.production.research as research
    import lib.image_gen as image_gen
    monkeypatch.setattr(research, 'research_topic', research.research_topic)
    monkeypatch.setattr(image_gen, 'generate_image', image_gen.generate_image)
    out = tmp_path / 'run' / 'tofu' / 'doc1'
    record = run_tofu.build('doc1', str(out))
    assert record['pages'] == 3 and os.path.isfile(record['pptx'])
    assert installs == [{'name': bridge.DEFAULT_MODEL, 'endpoint': bridge.ENDPOINT}]
    launched = json.loads((out / dl.RESULT_FILE).read_text())
    assert launched['outcome'] == 'scored' and launched['kind'] == 'slides'


@pytest.mark.skipif(not _tex_ready(), reason='persistent TeX toolchain is required')
def test_document_real_renderer_and_delivery(tmp_path, spy, fake_document_models):
    """No renderer substitution: actual TeX, PDF publication and byte validation."""
    from pypdf import PdfReader
    paper = _sample_paper(tmp_path)
    record = dl.launch(_document(paper, tmp_path), install_model_route=spy.install)
    assert record['outcome'] == 'scored' and record['delivered'], record
    pdf = Path(record['artifact'])
    assert len(PdfReader(pdf).pages) > 0
    assert pdf.stat().st_size > 0
    assert (paper / 'delivered' / 'manifest.json').is_file()
    assert any(row['check'] == 'renderer:latexmk' and row['ok']
               for row in record['preflight'])


@pytest.mark.parametrize('late', [False, True])
def test_outcome_checks_deadline_on_return_and_cancels_timer(
        tmp_path, spy, renderers_ok, monkeypatch, late):
    from dataclasses import replace
    clock = [100.0]
    timers = []

    class Timer:
        def __init__(self, seconds, callback):
            self.seconds = seconds
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    def run(contract, abort, emit):
        clock[0] += 11 if late else 1
        path = tmp_path / 'partial.pptx'
        path.write_bytes(b'recipe output')
        return {'artifact': str(path), 'summary': {}}

    monkeypatch.setattr(dl.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(dl.threading, 'Timer', Timer)
    monkeypatch.setitem(dl.KINDS, 'slides', replace(dl.KINDS['slides'], run=run))
    record = dl.launch(_slides(tmp_path, budget={'deadline_s': 10}),
                       install_model_route=spy.install)
    assert record['outcome'] == ('timeout' if late else 'scored')
    assert record['delivered'] is not late
    assert Path(record['artifact']).read_bytes() == b'recipe output'
    if late:
        assert record['partial_artifact'] == record['artifact']
    assert timers[0].seconds == 10 and timers[0].cancelled
