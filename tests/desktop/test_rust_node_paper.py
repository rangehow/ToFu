"""Device paper builds on the Rust node (``tofu node``) against the Python Home.

Real processes over the real bridge: the Home's desktop build service
(``lib.paper.builds.service.start_preview`` → ``desktop.bind_desktop_document``
/ ``desktop_execution``) resolves a document on the node (``paper_resolve``),
streams a build (``paper_build``) and accepts the bundle through
``BundleReceiver`` (identity, sizes, digests and the Home's own strict PDF
audit). Covered: a build with a faked engine, a mid-build cancel that settles
as aborted and retires the engine's process tree, and the refusal of a node
without ``--allow-exec``.

The engine is faked the way tests/paper/test_paper_build_desktop.py fakes
``prepare_toolchain``, but at the process boundary (the node is a separate
binary): fake ``latexmk``/``pdflatex`` executables on the node's PATH.

A real TeX engine also runs when a TeX Live tree is available: ``TOFU_TEX_ROOT``
(the toolchain root both nodes read), else ``~/.cache/tofu-test-tools/tinytex/
.TinyTeX``. Provision it without root::

    mkdir -p ~/.cache/tofu-test-tools/tinytex && cd ~/.cache/tofu-test-tools/tinytex
    curl -sSL -o t.tar.xz https://github.com/rstudio/tinytex-releases/releases/download/v2026.11/TinyTeX-1-linux-x86_64-v2026.11.tar.xz
    tar -xJf t.tar.xz && rm t.tar.xz

With it, the same fixture is built by the Rust node and by the Python node and
the manifests the Home received are compared field by field.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from tests.support.paper_builds import scheduler  # noqa: F401  (fixture)
from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

_REPO = Path(__file__).resolve().parents[2]
OWNER = '1'
_DOCUMENT = ('\\documentclass{article}\n\\begin{document}\n'
             'A version from the desktop.%s\n\\end{document}\n')

_FAKE_LATEXMK = r'''#!/bin/sh
for arg in "$@"; do
  case "$arg" in -outdir=*) out="${arg#-outdir=}";; esac
  main="$arg"
done
stem=$(basename "$main" .tex)
echo "Latexmk: fake run of $main"
if grep -q HOLD "$main"; then
  echo $$ > "$FAKE_TEX_PIDFILE"
  sleep 300 &
  echo $! > "$FAKE_TEX_PIDFILE.child"
  wait
fi
cp "$FAKE_TEX_PDF" "$out/$stem.pdf"
echo "This is a fake TeX log" > "$out/$stem.log"
printf 'SyncTeX Version:1\nInput:1:%s/%s.tex\n{1\nh1,3:4736286,9830400:21178368,655360,0\n}1\n' "$PWD" "$stem" | gzip > "$out/$stem.synctex.gz"
'''
_FAKE_ENGINE = "#!/bin/sh\necho 'pdfTeX 3.14 (fake engine)'\n"


def _tex_root() -> Path | None:
    configured = os.environ.get('TOFU_TEX_ROOT', '').strip()
    root = Path(configured).expanduser() if configured else (
        Path.home() / '.cache/tofu-test-tools/tinytex/.TinyTeX')
    return root if any(root.glob('bin/*/latexmk')) else None


def _api(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b'{}')
            return response.status, payload.get('data', payload) if isinstance(payload, dict) else payload
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors='replace')


def _wait(predicate, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f'never {what}')


def _nodes(base: str) -> dict:
    status, body = _api(base, 'GET', '/api/v4/desktop/nodes')
    assert status == 200, body
    return {n['node_id']: n for n in body['nodes'] if n['node_id']}


def _fake_engine(tmp_path: Path) -> dict:
    import fitz
    bin_dir = tmp_path / 'fake-tex'
    bin_dir.mkdir()
    for name, body in (('latexmk', _FAKE_LATEXMK), ('pdflatex', _FAKE_ENGINE)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    pdf = tmp_path / 'fake.pdf'
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), 'A version from the desktop.')
        document.save(pdf)
    return {'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}', 'FAKE_TEX_PDF': str(pdf),
            'FAKE_TEX_PIDFILE': str(tmp_path / 'engine.pid')}


class _Node:
    """One paired, confirmed node process serving a share root ``project``."""

    def __init__(self, base, tmp_path, name, program, *, env=None, allow_exec=True, body=''):
        self.base = base
        home = tmp_path / name
        self.share = home / 'share'
        (self.share / 'paper').mkdir(parents=True)
        (self.share / 'paper' / 'main.tex').write_text(_DOCUMENT % body)
        config = home / 'desktop_agent.json'
        status, minted = _api(base, 'POST', f'/api/v4/desktop/pair-code?base={base}/', {})
        assert status == 201, minted
        # Keep only the test Home's own route (the minted link's LAN route can name
        # the inherited PORT, the production Tofu, which a test must never reach).
        from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link
        link = build_pairing_link(base, parse_pairing_link(minted['link']).secret)
        host = (f'{name}-machine', f'{name}-host', name[0] * 64)
        args = ['--join', link, '--poll-interval', '0.2', '--root', f'project={self.share}',
                *(['--allow-exec'] if allow_exec else [])]
        argv = (node_command('--simulate-host', *host, *args) if program == 'rust'
                else [sys.executable, '-m', 'tests.chaos.node_main', *host, *args])
        environment = dict(os.environ, TOFU_DESKTOP_CONFIG=str(config), HOME=str(home),
                           PYTHONPATH=str(_REPO), NO_PROXY='*', no_proxy='*', **(env or {}))
        if 'TOFU_TEX_ROOT' not in (env or {}):
            environment.pop('TOFU_TEX_ROOT', None)
        self.log = home / 'node.log'
        with self.log.open('ab') as handle:
            self.proc = subprocess.Popen(argv, env=environment, cwd=str(_REPO), stdout=handle,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        _wait(lambda: config.exists() and json.loads(config.read_text()).get('agent_ids')
              or self.proc.poll() is not None, 'joined')
        assert self.proc.poll() is None, self.log.read_text()[-2000:]
        self.node_id = next(iter(json.loads(config.read_text())['agent_ids'].values()))
        _wait(lambda: _nodes(base).get(self.node_id), 'listed')
        status, body = _api(base, 'POST', f'/api/v4/desktop/nodes/{self.node_id}/confirm', {})
        assert status == 200, body
        status, snapshot = _api(base, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(base, 'PUT', f'/api/v4/devices/{self.node_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'exec': True}})
        assert status == 200, body
        _wait(lambda: _nodes(base)[self.node_id]['online'], 'online')
        if allow_exec:
            _wait(lambda: _nodes(base)[self.node_id]['tiers']['exec'], 'exec effective')

    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)
        _api(self.base, 'DELETE', f'/api/v4/desktop/nodes/{self.node_id}')


@pytest.fixture
def home(live_server, scheduler, monkeypatch):  # noqa: F811
    from lib.desktop import bridge as db
    from lib.paper.builds import service
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    nodes = []

    def start(*args, **kwargs):
        node = _Node(live_server, *args, **kwargs)
        nodes.append(node)
        monkeypatch.setattr(service, 'owned_settings',
                            lambda *_: {'projectPath': f'remote:{node.node_id}:project'})
        return node

    try:
        yield start
    finally:
        for node in nodes:
            node.stop()
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()


def _payload(**extra):
    return {'source': 'local', 'conv_id': 'owned-conversation', 'path': 'paper/main.tex',
            'request_id': 'edit-1', 'subscriber_id': 'reader', 'line': 3, **extra}


def _settle(scheduler, task_id, timeout=240):  # noqa: F811
    return _wait(lambda: (lambda task: task if task['status'] in ('done', 'error', 'aborted') else None)(
        scheduler.production.get(task_id)), 'settled', timeout)


def _build(scheduler):  # noqa: F811
    from lib.paper.builds import service
    accepted = service.start_preview(_payload(), owner_user_id=int(OWNER), scheduler=scheduler)
    task = _settle(scheduler, accepted['task_id'])
    assert task['status'] == 'done', task.get('error')
    return accepted, task['result']


def _record_manifests(monkeypatch) -> list:
    from lib.paper.builds.bundle import BundleReceiver
    manifests = []
    accept = BundleReceiver.accept

    def recording(self, stream, data):
        if stream == 'paper-manifest':
            manifests.append(data)
        return accept(self, stream, data)
    monkeypatch.setattr(BundleReceiver, 'accept', recording)
    return manifests


def test_home_builds_a_document_on_the_rust_node(home, scheduler, tmp_path, monkeypatch):  # noqa: F811
    node = home(tmp_path, 'rust', 'rust', env=_fake_engine(tmp_path))
    manifests = _record_manifests(monkeypatch)
    accepted, result = _build(scheduler)
    # The manifest text is what Python's json.dumps produces for the same value.
    assert len(manifests) == 1
    assert manifests[0] == json.dumps(json.loads(manifests[0]), separators=(',', ':'))
    receipt = result['receipt']
    assert receipt['target'] == 'desktop' and receipt['status'] == 'success' and receipt['version_verified']
    assert receipt['owner_user_id'] == int(OWNER) and receipt['document_id'] == accepted['document_id']
    assert receipt['toolchain'] == 'pdfTeX 3.14 (fake engine)' and receipt['main_file'] == 'main.tex'
    assert [a['name'] for a in receipt['artifacts']] == [
        'build.log', 'document.pdf', 'document.synctex.gz', 'sources.json']
    location = result['location']
    assert location['build_id'] == receipt['build_id'] and location['page'] == 1
    assert location['file'] == 'main.tex' and location['rects']
    with scheduler.cache.lease(int(OWNER), receipt['document_id'], receipt['build_id']) as build:
        sources = json.loads((build.directory / 'sources.json').read_text())
        assert [row['path'] for row in sources] == ['main.tex']
        import fitz
        with fitz.open(build.directory / 'document.pdf') as pdf:
            assert 'A version from the desktop.' in pdf[0].get_text()
    assert str(node.share) not in json.dumps(result)


def test_cancel_mid_build_settles_and_retires_the_engine(home, scheduler, tmp_path):  # noqa: F811
    from lib.paper.builds import service
    env = _fake_engine(tmp_path)
    home(tmp_path, 'rust', 'rust', env=env, body=' HOLD')
    accepted = service.start_preview(_payload(), owner_user_id=int(OWNER), scheduler=scheduler)
    pidfile = Path(env['FAKE_TEX_PIDFILE'])
    child = Path(str(pidfile) + '.child')
    _wait(child.exists, 'engine started', 120)
    pids = [int(pidfile.read_text()), int(child.read_text())]
    assert scheduler.detach(accepted['task_id'], owner_user_id=int(OWNER), subscriber_id='reader')
    assert _settle(scheduler, accepted['task_id'])['status'] == 'aborted'

    def gone(pid):
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except OSError:
            return True
        return ') Z ' in stat
    _wait(lambda: all(gone(pid) for pid in pids), 'engine tree retired by paper_build_cancel', 30)
    assert not scheduler.cache.latest(int(OWNER), accepted['document_id'])


def test_a_node_without_exec_is_refused(home, scheduler, tmp_path):  # noqa: F811
    from lib.desktop import send_desktop_command
    from lib.paper.builds import service
    from lib.paper.builds.errors import BuildError
    node = home(tmp_path, 'rust', 'rust', allow_exec=False)
    with pytest.raises(BuildError, match='enable command execution'):
        service.start_preview(_payload(), owner_user_id=int(OWNER), scheduler=scheduler)
    body = {'schema_version': 1, 'root': 'project', 'project_path': 'paper', 'main_file': 'main.tex',
            'edited_file': 'main.tex', 'owner_user_id': 1, 'document_id': 'a' * 64, 'build_id': 'b' * 32}
    result, error = send_desktop_command('paper_build', body, timeout=30, target_agent_id=node.node_id,
                                         user_id=OWNER, cmd_id='b' * 32)
    assert error and 'exec' in str(error), (result, error)


@pytest.mark.skipif(_tex_root() is None, reason='no TeX Live tree: set TOFU_TEX_ROOT or provision '
                    '~/.cache/tofu-test-tools/tinytex (see the module docstring)')
def test_real_engine_manifest_matches_the_python_node(home, scheduler, tmp_path, monkeypatch):  # noqa: F811
    env = {'TOFU_TEX_ROOT': str(_tex_root())}
    manifests = _record_manifests(monkeypatch)
    received = {}
    for program in ('rust', 'python'):
        node = home(tmp_path, program, program, env=env)
        accepted, result = _build(scheduler)
        received[program] = result
        node.stop()
    rust, python = received['rust'], received['python']
    for result in (rust, python):
        assert result['receipt']['status'] == 'success', result['receipt']['diagnostics']
        assert result['location']['page'] == 1
        assert result['receipt']['toolchain'].startswith('pdfTeX')
    assert len(manifests) == 2
    for text in manifests:
        assert text == json.dumps(json.loads(text), separators=(',', ':'))
    volatile = {'build_id', 'document_id', 'started_at', 'completed_at', 'artifacts'}

    def stable(receipt):
        return {key: value for key, value in receipt.items() if key not in volatile}
    assert stable(rust['receipt']) == stable(python['receipt'])
    assert list(rust['receipt']) == list(python['receipt'])
    assert [a['name'] for a in rust['receipt']['artifacts']] == [a['name'] for a in python['receipt']['artifacts']]
    sources = {program: next(a for a in received[program]['receipt']['artifacts'] if a['name'] == 'sources.json')
               for program in received}
    assert sources['rust'] == sources['python']  # byte-identical frozen-source manifest
    for key in ('file', 'line', 'page', 'rects'):
        assert rust['location'][key] == python['location'][key]
