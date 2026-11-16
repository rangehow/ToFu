"""Execute install.sh in a disposable checkout with fake network/runtime ports.

Only external package managers, validators and service endpoints are replaced;
argument parsing, logs, locking, source choice, dotenv, receipt and launch
control run as shipped. Never run an installer against the developer checkout.
"""
from __future__ import annotations

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('lib/storage.py',)

import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
# Synthetic epoch seconds of the checkout's newest HEAD reflog entry.
HEAD_MOVED_AT = 1_793_787_481

# Module seam for the installer's port probe: bind fails only when BUSY=1.
_PORT_SEAM = '''
AF_INET=2
SOCK_STREAM=1
SOL_SOCKET=1
SO_REUSEADDR=2
class socket:
    def __init__(self,*a): pass
    def setsockopt(self,*a): pass
    def bind(self,*a):
        import os
        if os.environ.get('BUSY') == '1': raise OSError('occupied')
    def close(self): pass
'''

# Lifecycle owner of a ready worker. It records whether stdin is a terminal
# because serverctl.py restart shows its own approval prompt there;
# FAKE_RESTART_RC models a declined (3) or failed (1) restart.
_READY_SERVERCTL = '''
import json, os, sys
from pathlib import Path
Path(os.environ["CALLS_FILE"]).open("a").write(
    "serverctl:%s tty=%s\\n" % (" ".join(sys.argv[1:]), sys.stdin.isatty()))
if sys.argv[1:2] == ["restart"]:
    raise SystemExit(int(os.environ.get("FAKE_RESTART_RC", "0")))
status = {"ready": True, "applicationReachable": True, "port": 18677,
          "applicationUrl": "http://localhost:18677", "pid": 4242}
if "FAKE_WORKER_STARTED_AT" in os.environ:
    status["processStartedAt"] = float(os.environ["FAKE_WORKER_STARTED_AT"])
print(json.dumps(status))
'''


def _executable(path, body):
    path.write_text('#!/usr/bin/env bash\n' + body, encoding='utf-8')
    path.chmod(0o755)


@contextlib.contextmanager
def _terminal():
    """Yield a pseudo-terminal descriptor to use as an interactive stdin."""
    controller, terminal = os.openpty()
    try:
        yield terminal
    finally:
        os.close(terminal)
        os.close(controller)


def _occupy_port_with_ready_worker(project, env, *, started_at, head_moved_at):
    """Busy the port with this checkout's ready worker started at ``started_at``.

    ``None`` models a status without ``processStartedAt`` or, for
    ``head_moved_at``, a checkout without a HEAD reflog entry.
    """
    (project / 'socket.py').write_text(_PORT_SEAM)
    (project / 'serverctl.py').write_text(_READY_SERVERCTL)
    env['BUSY'] = '1'
    if started_at is not None:
        env['FAKE_WORKER_STARTED_AT'] = repr(started_at)
    if head_moved_at is not None:
        env['FAKE_HEAD_MOVED_AT'] = str(head_moved_at)


def _assert_update_not_claimed_live(project, stdout):
    assert 'Tofu is ready' not in stdout
    assert 'still running the code it started with' in stdout
    command = next(line.split(':', 1)[1] for line in stdout.splitlines()
                   if line.strip().startswith('Apply the update:'))
    python = json.loads((project / '.tofu_env.json').read_text())['python']
    assert shlex.split(command) == [
        'cd', os.path.realpath(project), '&&', python, 'serverctl.py', 'restart']


@pytest.fixture
def installer(tmp_path):
    project = tmp_path / 'my project'
    project.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    scripts = project / 'scripts'
    scripts.mkdir()
    (project / 'bootstrap_pkg').mkdir()
    shutil.copy(ROOT / 'bootstrap_pkg/process.py', project / 'bootstrap_pkg/process.py')
    shutil.copy(ROOT / 'bootstrap_pkg/process_roles.py', project / 'bootstrap_pkg/process_roles.py')
    shutil.copy(ROOT / 'bootstrap_pkg/install_transaction.py', project / 'bootstrap_pkg/install_transaction.py')
    shutil.copy(ROOT / 'runtime_guards.py', project / 'runtime_guards.py')
    shutil.copytree(ROOT / 'tofu_workers', project / 'tofu_workers',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy(ROOT / 'bootstrap_pkg/resource_policy_generated.py', project / 'bootstrap_pkg/resource_policy_generated.py')
    (project / 'bootstrap_pkg/install_artifacts.py').write_text(
        'from pathlib import Path\n'
        'def prepare_python_wheels(python, requirements, wheelhouse):\n'
        '    wheelhouse.mkdir(parents=True, exist_ok=True)\n'
        '    lock=wheelhouse / "install.lock"; lock.write_text(""); return lock\n'
        'if __name__ == "__main__":\n'
        '    wheel=Path("fixture-search.whl").resolve(); wheel.write_bytes(b"fixture"); print(wheel)\n')

    shutil.copy(ROOT / 'install.sh', project / 'install.sh')
    shutil.copy(ROOT / 'tofu_dotenv.py', project / 'tofu_dotenv.py')
    (project / 'requirements.txt').write_text('')
    (project / 'VERSION').write_text('test')
    (project / '.env').write_text('PORT=18674\nPORT = 18675\nKEEP_ME=value#data\n')
    (project / '.git').mkdir()
    python_dir = project / '.venv/bin'
    python_dir.mkdir(parents=True)
    # Real Python runs all preflight/config/receipt scripts. Only the third-party
    # wheel import canary is stubbed because this test makes no package install.
    _executable(python_dir / 'python',
        'if [[ "${1:-}" == "-c" && "${2:-}" == "import lxml.etree,"* ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "-" && "${FAKE_MARKER_RC:-0}" == 1 && "${4:-}" == *"/bin/python" ]]; then exit 13; fi\n'
        'exec "$REAL_PYTHON" "$@"\n')
    (scripts / 'verify_pdf_stack.py').write_text('print("PDF fixture verified")\n')
    (scripts / 'verify_frontend_dist.py').write_text('print("Frontend fixture verified")\n')
    # Pre-place the optional engine so step 8.47 resolves locally ("already
    # built") — exactly like the frontend freshness fixture above, this keeps
    # the fixture hermetic: without it the step downloads a release prebuilt.
    engine = project / 'packages/tofu-db/target/release/tofu-db'
    engine.parent.mkdir(parents=True)
    _executable(engine, 'echo "fixture tofu-db"\n')
    (project / 'server.py').write_text(
        'import os; from pathlib import Path\n'
        'Path(os.environ["CALLS_FILE"]).open("a").write("launch:"+os.environ["PORT"]+"\\n")\n'
        'raise SystemExit(int(os.environ.get("FAKE_START_RC","0")))\n')
    (project / 'healthcheck.py').write_text(
        'import os; from pathlib import Path\n'
        'Path(os.environ["CALLS_FILE"]).open("a").write("health:"+os.environ["PORT"]+"\\n")\n'
        'raise SystemExit(int(os.environ.get("FAKE_HEALTH_RC","0")))\n')
    (project / 'serverctl.py').write_text('print(\'{"ready": false}\')\n')
    # Route lib-fixture writes through a local so the NC meta-guard's lexical
    # shipped-prefix scan (tests/meta/test_nc_guard_registry.py) sees a tmp fixture
    # path, not the lookalike 'lib/...' tail — these never touch the repo.
    lib_dir = project / 'lib'
    (lib_dir / 'tasks_pkg').mkdir(parents=True)
    (lib_dir / '__init__.py').write_text('')
    # The package install.sh identifies a Tofu checkout by.
    (lib_dir / 'tasks_pkg' / '__init__.py').write_text('')
    (lib_dir / 'storage.py').write_text('''
class StorageSupervisor:
    def __init__(self, **kwargs): self.client = self; self.record = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def health(self): return {'backend': 'sqlite'}
    def command(self, op, value, key):
        self.record = {'value': True} if op == 'record.put' else None
    def query(self, op, value): return self.record
''')
    _executable(fake_bin / 'uv',
        'printf "uv:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [[ "${1:-}" == venv ]]; then mkdir -p "$2/bin"; cp "$FIXTURE_PYTHON" "$2/bin/python"; fi\n')
    _executable(fake_bin / 'git',
        'printf "git:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [[ "$*" == *status* && "${FAKE_DIRTY:-0}" == 1 ]]; then echo " M user.txt"; fi\n'
        'if [[ "$*" == *" reflog "* && -n "${FAKE_HEAD_MOVED_AT:-}" ]]; then\n'
        '    echo "HEAD@{${FAKE_HEAD_MOVED_AT}}"\n'
        'fi\n')
    _executable(fake_bin / 'ldd', 'echo "ldd (GNU libc) 2.35"\n')
    for binary in ('rg', 'fd', 'sd', 'goawk', 'mlr'):
        _executable(fake_bin / binary, 'printf "fixture tool\\n"\n')
    for binary in ('curl', 'wget', 'conda'):
        _executable(fake_bin / binary,
            'printf "UNEXPECTED-NETWORK:%s\\n" "$0 $*" >> "$CALLS_FILE"; exit 99\n')
    env = dict(os.environ, HOME=str(home), PATH=str(fake_bin) + os.pathsep + os.environ['PATH'],
               REAL_PYTHON=sys.executable, FIXTURE_PYTHON=str(python_dir / "python"), CALLS_FILE=str(tmp_path / 'calls'),
               PYTHONPATH='', PORT='18676')
    # A fresh install selects SQLite unless .env says otherwise; a running
    # server's own deployment selection is not an input of this disposable copy.
    for name in ('CONDA_PREFIX', 'CONDA_BASE', 'TOFU_PYPI_INDEX', 'TOFU_UV_LOCAL',
                 'BASH_ENV', 'ENV', 'TOFU_DEPLOYMENT_MODE', 'TOFU_PROCESS_ROLE',
                 'TOFU_PERSONAL_STORAGE_BACKEND', 'TOFUDB_TRANSPORT'):
        env.pop(name, None)
    # A fresh install starts from the shipped deployment defaults. A pytest
    # launched from a terminal of a running native-storage server inherits its
    # storage selection, which would make the installer prepare a native engine
    # this disposable checkout does not contain.
    for name in [name for name in env if name.startswith('TOFUDB_')] + [
            'TOFU_PERSONAL_STORAGE_BACKEND', 'TOFU_DEPLOYMENT_MODE', 'TOFU_PROCESS_ROLE',
            'TOFU_DISTRIBUTED_PREVIEW_MODE']:
        env.pop(name, None)
    return project, env


def _run(project, env, *args, cwd=None, stdin=subprocess.DEVNULL):
    # install.sh does real work (env probe, download-cache reuse, receipt
    # writes); 25 s only fit an idle tmpfs host — a loaded FUSE checkout
    # needs an order of magnitude more while still catching a true wedge.
    # stdin is explicit because a terminal changes installer behavior; a test
    # never inherits the developer's terminal.
    return subprocess.run(['bash', str(project / 'install.sh'),
                           *args], cwd=cwd or project, env=env, text=True,
                          stdin=stdin, capture_output=True, timeout=90)


def _run_piped(project, env, *args, cwd):
    # `curl ... | bash -s --` has no script path, so only the working
    # directory and ~/tofu can supply the install target.
    return subprocess.run(['bash', '-s', '--', *args], cwd=cwd, env=env, text=True,
                          input=(project / 'install.sh').read_text(encoding='utf-8'),
                          capture_output=True, timeout=90)


def _serve_clones_from(source, env):
    """Make the fake git clone a copy of SOURCE, standing in for the published tree."""
    _executable(Path(env['PATH'].split(os.pathsep)[0]) / 'git',
        'printf "git:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [[ " $* " == *" clone "* ]]; then cp -R "$FAKE_CLONE_SOURCE/." "${@: -1}"; fi\n')
    env['FAKE_CLONE_SOURCE'] = str(source)


def _python_project(path):
    """An ordinary Python project in its own Git repository, not a Tofu checkout."""
    (path / '.git').mkdir(parents=True)
    for name, text in (('server.py', 'print("the user\'s own app")\n'),
                       ('requirements.txt', 'flask\n'),
                       ('install.sh', 'pip install -r requirements.txt\n')):
        (path / name).write_text(text)
    return path


def _tree(root):
    return sorted((str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
                  for path in root.rglob('*'))


def test_agent_install_only_no_update_and_private_receipt(installer, tmp_path):
    project, env = installer
    result_path = tmp_path / 'result.json'
    result = _run(project, env, '--non-interactive', '--no-update', '--no-launch',
                  '--result-json', str(result_path))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result_path.read_text())
    assert receipt['status'] == 'installed' and receipt['exit_code'] == 0
    assert receipt['url'] is None
    assert result_path.stat().st_mode & 0o777 == 0o600
    assert (project / '.env').stat().st_mode & 0o777 == 0o600
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'git:' not in calls and 'UNEXPECTED' not in calls and 'launch:' not in calls
    assert not (project.parent / '.my project.install-lock').exists()
    assert Path(receipt['log']).is_file()


def test_failed_interpreter_marker_cannot_report_installed(installer, tmp_path):
    project, env = installer
    env['FAKE_MARKER_RC'] = '1'
    result_path = tmp_path / 'marker-failed.json'
    result = _run(project, env, '--no-update', '--no-launch', '--result-json', str(result_path))
    assert result.returncode != 0
    assert json.loads(result_path.read_text())['status'] == 'failed'
    assert 'Could not publish the interpreter binding' in result.stdout
    assert not (project / '.tofu_env.json').exists()
    assert 'UNEXPECTED' not in Path(env['CALLS_FILE']).read_text()
    assert not list((project / '.tofu-envs').iterdir())


def test_local_invocation_wins_over_home_checkout(installer, tmp_path):
    project, env = installer
    other = Path(env['HOME']) / 'tofu'
    other.mkdir()
    (other / 'server.py').write_text('DO NOT CHANGE')
    result = _run(project, env, '--no-update', '--no-launch', cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (other / 'server.py').read_text() == 'DO NOT CHANGE'
    assert list(other.iterdir()) == [other / 'server.py']
    assert (project / '.tofu_env.json').is_file()


def test_piped_install_never_adopts_a_python_project_working_directory(installer, tmp_path):
    project, env = installer
    _serve_clones_from(project, env)
    user_project = _python_project(tmp_path / 'user-project')
    before = _tree(user_project)
    receipt = tmp_path / 'piped.json'
    result = _run_piped(project, env, '--no-launch', '--result-json', str(receipt),
                        cwd=user_project)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = Path(json.loads(receipt.read_text())['install_dir'])
    assert installed == Path(os.path.realpath(Path(env['HOME']) / 'tofu'))
    assert _tree(user_project) == before
    assert str(user_project) not in Path(env['CALLS_FILE']).read_text()


def test_explicit_dir_refuses_a_python_project_before_touching_it(installer, tmp_path):
    project, env = installer
    user_project = _python_project(tmp_path / 'user-project')
    before = _tree(user_project)
    receipt = tmp_path / 'refused.json'
    result = _run(project, env, '--dir', str(user_project), '--no-launch',
                  '--result-json', str(receipt))
    assert result.returncode != 0
    assert 'is non-empty but is not a Tofu checkout' in result.stdout
    data = json.loads(receipt.read_text())
    assert data['status'] == 'failed'
    # The transcript is staged beside the refused target, never inside it.
    assert Path(data['log']).parent == tmp_path
    assert _tree(user_project) == before
    # No external command ran, so neither `git status` nor `git pull` reached it.
    assert not Path(env['CALLS_FILE']).exists()


def test_explicit_dir_wins_over_a_checkout_working_directory(installer, tmp_path):
    project, env = installer
    _serve_clones_from(project, env)
    target = tmp_path / 'elsewhere'
    receipt = tmp_path / 'elsewhere.json'
    result = _run(project, env, '--dir', str(target), '--no-launch', '--result-json', str(receipt))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert Path(data['install_dir']) == Path(os.path.realpath(target))
    assert Path(data['log']).parent == Path(os.path.realpath(target)) / 'logs'
    assert not any((project / name).exists() for name in ('.tofu_env.json', '.tofu-envs', 'logs'))


def test_dirty_checkout_never_pulled(installer):
    project, env = installer
    env['FAKE_DIRTY'] = '1'
    result = _run(project, env, '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'status --porcelain' in calls and ' pull --ff-only' not in calls


def test_concurrent_install_refuses_before_any_manager_call(installer):
    project, env = installer
    lock = project.parent / '.my project.install-lock'
    lock.mkdir()
    result = _run(project, env, '--no-launch')
    assert result.returncode != 0
    assert 'Another install owns' in result.stdout
    assert lock.exists()  # Never delete a lock belonging to another writer.
    assert not Path(env['CALLS_FILE']).exists()


def test_explicit_port_and_whitespace_keys_roundtrip(installer, tmp_path):
    from tofu_dotenv import read_dotenv_values
    project, env = installer
    key = tmp_path / 'key'
    key.write_text('synthetic-secret&hash#value')
    with (project / '.env').open('a') as file:
        file.write('LLM_API_KEYS=old\nLLM_API_KEYS =older\n')
    result = _run(project, env, '--no-update', '--no-launch', '--port', '018677',
                  '--api-key-file', str(key))
    assert result.returncode == 2  # Six-digit port is rejected before mutations.
    result = _run(project, env, '--no-update', '--no-launch', '--port', '18677',
                  '--api-key-file', str(key))
    assert result.returncode == 0, result.stdout + result.stderr
    values = read_dotenv_values(project / '.env')
    assert values['PORT'] == '18677'
    assert values['LLM_API_KEYS'] == key.read_text()
    assert values['KEEP_ME'] == 'value#data'
    assert key.read_text() not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['FAKE_START_RC', 'FAKE_HEALTH_RC'])
def test_agent_receipt_does_not_claim_ready_after_runtime_failure(installer, tmp_path, failure):
    project, env = installer
    # Test our launch and receipt flow, not host-global port occupancy.
    # The isolated installer copy's bind is controlled with a module seam.
    (project / 'socket.py').write_text('''
AF_INET=2
SOCK_STREAM=1
SOL_SOCKET=1
SO_REUSEADDR=2
class socket:
    def __init__(self,*a): pass
    def setsockopt(self,*a): pass
    def bind(self,*a): pass
    def close(self): pass
''')
    env[failure] = '7'
    receipt = tmp_path / 'failed.json'
    result = _run(project, env, '--no-update', '--port', '18677', '--result-json', str(receipt))
    assert result.returncode != 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert data['status'] == 'failed' and data['exit_code'] != 0
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'launch:18677' in calls
    if failure == 'FAKE_START_RC':
        assert 'health:' not in calls
    else:
        assert 'health:18677' in calls, result.stdout + result.stderr


@pytest.mark.parametrize('already_running', [False, True])
def test_ready_receipt_requires_health_without_restarting_existing_worker(installer, tmp_path, already_running):
    project, env = installer
    (project / 'socket.py').write_text(_PORT_SEAM)
    if already_running:
        # Started after the checkout's last HEAD move, so it runs this code:
        # even an interactive rerun only verifies it.
        _occupy_port_with_ready_worker(project, env, started_at=HEAD_MOVED_AT + 60,
                                       head_moved_at=HEAD_MOVED_AT)
    receipt = tmp_path / 'ready.json'
    with _terminal() as terminal:
        result = _run(project, env, '--no-update', '--port', '18677',
                      '--result-json', str(receipt), stdin=terminal)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert data['status'] == 'ready' and data['exit_code'] == 0
    assert data['url'] == 'http://localhost:18677'
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'health:18677' in calls
    assert ('launch:18677' in calls) is not already_running
    assert 'serverctl:restart' not in calls
    assert ('verifying it without restarting' in result.stdout) is already_running
    assert 'still running the code it started with' not in result.stdout
    # The new candidate is owned; the caller-created .venv remains untouched.
    assert json.loads((project / '.tofu_env.json').read_text())['owned_by_tofu_install'] is True
    assert (project / ".venv/bin/python").is_file()


@pytest.mark.parametrize('started_at', [HEAD_MOVED_AT - 3600, HEAD_MOVED_AT + 0.5, None],
                         ids=['before-head-move', 'within-reflog-second', 'unknown-start'])
def test_stale_worker_restarts_through_lifecycle_owner_on_terminal(installer, tmp_path, started_at):
    project, env = installer
    _occupy_port_with_ready_worker(project, env, started_at=started_at,
                                   head_moved_at=HEAD_MOVED_AT)
    receipt = tmp_path / 'restarted.json'
    with _terminal() as terminal:
        result = _run(project, env, '--no-update', '--port', '18677',
                      '--result-json', str(receipt), stdin=terminal)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text().splitlines()
    # serverctl.py owns the approval prompt, so it must receive the terminal.
    restarts = [line for line in calls if line.startswith('serverctl:restart')]
    assert restarts == ['serverctl:restart tty=True']
    assert 'launch:18677' not in calls
    # Runtime verification covers the restarted worker.
    assert calls.index(restarts[0]) < calls.index('health:18677')
    assert json.loads(receipt.read_text())['status'] == 'ready'
    assert 'Tofu is ready: http://localhost:18677' in result.stdout


@pytest.mark.parametrize('caller', ['pipe', 'terminal-non-interactive'])
def test_stale_worker_without_interactive_terminal_is_not_reported_live(installer, tmp_path, caller):
    project, env = installer
    _occupy_port_with_ready_worker(project, env, started_at=HEAD_MOVED_AT - 3600,
                                   head_moved_at=HEAD_MOVED_AT)
    receipt = tmp_path / 'stale.json'
    args = ('--no-update', '--port', '18677', '--result-json', str(receipt))
    if caller == 'pipe':
        result = _run(project, env, *args)
    else:
        with _terminal() as terminal:
            result = _run(project, env, '--non-interactive', *args, stdin=terminal)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'serverctl:restart' not in calls and 'launch:' not in calls
    assert 'health:18677' in calls
    data = json.loads(receipt.read_text())
    assert data['status'] == 'restart-required' and data['exit_code'] == 0
    assert data['url'] == 'http://localhost:18677'
    _assert_update_not_claimed_live(project, result.stdout)


@pytest.mark.parametrize('restart_rc', ['3', '1'], ids=['declined', 'failed'])
def test_declined_or_failed_restart_keeps_update_pending(installer, tmp_path, restart_rc):
    project, env = installer
    _occupy_port_with_ready_worker(project, env, started_at=HEAD_MOVED_AT - 3600,
                                   head_moved_at=HEAD_MOVED_AT)
    env['FAKE_RESTART_RC'] = restart_rc
    receipt = tmp_path / 'not-restarted.json'
    with _terminal() as terminal:
        result = _run(project, env, '--no-update', '--port', '18677',
                      '--result-json', str(receipt), stdin=terminal)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert calls.count('serverctl:restart') == 1 and 'launch:' not in calls
    assert json.loads(receipt.read_text())['status'] == 'restart-required'
    _assert_update_not_claimed_live(project, result.stdout)


@pytest.mark.parametrize('missing', ['worker-start-time', 'head-reflog-entry'])
def test_unknown_worker_age_is_never_reported_live(installer, tmp_path, missing):
    project, env = installer
    _occupy_port_with_ready_worker(
        project, env,
        started_at=None if missing == 'worker-start-time' else HEAD_MOVED_AT + 3600,
        head_moved_at=None if missing == 'head-reflog-entry' else HEAD_MOVED_AT)
    receipt = tmp_path / 'unknown.json'
    result = _run(project, env, '--no-update', '--port', '18677', '--result-json', str(receipt))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'serverctl:restart' not in Path(env['CALLS_FILE']).read_text()
    assert json.loads(receipt.read_text())['status'] == 'restart-required'
    _assert_update_not_claimed_live(project, result.stdout)


def test_replaced_home_and_missing_temp_directory_are_supported(installer, tmp_path):
    project, env = installer
    replacement_home = tmp_path / 'replacement-home'
    replacement_home.mkdir()
    env['HOME'] = str(replacement_home)
    env['TMPDIR'] = str(tmp_path / 'old-container-temp')
    result = _run(project, env, '--no-update', '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    marker = json.loads((project / '.tofu_env.json').read_text())
    assert Path(marker['env_prefix']).is_relative_to(project / '.tofu-envs')
    assert (project / '.venv/bin/python').is_file()
    assert 'Ignoring unavailable inherited TMPDIR' in result.stdout + result.stderr


def test_late_storage_failure_preserves_live_pointer_and_configuration(installer):
    project, env = installer
    old_marker = json.dumps({'backend': 'uv', 'python': str(project / '.venv/bin/python'),
                             'env_prefix': str(project / '.venv')})
    (project / '.tofu_env.json').write_text(old_marker)
    previous_configuration = (project / '.env').read_bytes()
    (project / 'lib/storage.py').write_text('class StorageSupervisor:\n    def __init__(self, **kw): raise RuntimeError("storage fault injection")\n')
    result = _run(project, env, '--no-update', '--no-launch')
    assert result.returncode != 0
    assert 'storage fault injection' in result.stdout
    assert (project / '.tofu_env.json').read_text() == old_marker
    assert (project / '.env').read_bytes() == previous_configuration
    assert (project / '.venv/bin/python').is_file()
    assert not list((project / '.tofu-envs').iterdir())


def test_default_install_defers_chromium_and_uses_candidate_python(installer):
    project, env = installer
    result = _run(project, env, '--no-update', '--non-interactive', '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'playwright install' not in calls
    assert 'uv:venv' in calls
    binding = json.loads((project / '.tofu_env.json').read_text())
    assert binding['backend'] == 'uv'
    assert '.tofu-envs' in binding['python']


# docs/AGENT_INSTALL.md is what a coding agent runs after a user pastes the
# README's one-line prompt; its launch blocks are executed here verbatim.
RAW_MAIN = 'https://raw.githubusercontent.com/rangehow/ToFu/main/'
AGENT_GUIDE_URL = RAW_MAIN + 'docs/AGENT_INSTALL.md'
PUBLISHED_INSTALLER_URL = RAW_MAIN + 'install.sh'


def _agent_guide_launch_block(marker):
    guide = (ROOT / AGENT_GUIDE_URL.removeprefix(RAW_MAIN)).read_text(encoding='utf-8')
    [block] = [block for block in re.findall(r'```bash\n(.*?)```', guide, re.S)
               if marker in block]
    return block


def _stop_installers_for(receipt):
    listing = subprocess.run(['ps', '-eo', 'pid=,args='], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(' ')
        if str(receipt) in args:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGTERM)


def _run_agent_guide_launch(block, cwd, env):
    """Run a guide launch block like an agent's fresh shell; wait for its exit line."""
    # The guide's documented way to pass an installer option.
    block = block.replace(' >"$R.log"', ' --port 18677 >"$R.log"')
    assert block.count('--port 18677') == 1
    started = subprocess.run(['sh', '-c', block], cwd=cwd, env=env, text=True,
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    assert started.returncode == 0, started.stdout + started.stderr
    paths = dict(re.findall(r'(receipt|log)=(\S+)', started.stdout))
    receipt, log = Path(paths['receipt']), Path(paths['log'])
    deadline = time.monotonic() + 90  # Same budget as _run on a loaded FUSE host.
    try:
        while 'TOFU_INSTALL_EXIT=' not in log.read_text(errors='replace'):
            assert time.monotonic() < deadline, log.read_text(errors='replace')[-4000:]
            time.sleep(0.2)
    finally:
        _stop_installers_for(receipt)
    output = log.read_text(errors='replace')
    exit_code = int(re.search(r'TOFU_INSTALL_EXIT=(\d+)', output).group(1))
    return exit_code, json.loads(receipt.read_text()), output


def test_readme_agent_prompts_are_one_line_and_fetch_the_shipped_guide():
    for readme in ('README.md', 'README_CN.md', AGENT_GUIDE_URL.removeprefix(RAW_MAIN)):
        text = (ROOT / readme).read_text(encoding='utf-8')
        prompts = [block for block in re.findall(r'```text\n(.*?)```', text, re.S)
                   if AGENT_GUIDE_URL in block]
        assert len(prompts) == 1, readme
        assert len(prompts[0].strip().splitlines()) == 1, readme


def test_agent_guide_install_never_adopts_the_agents_working_directory(installer, tmp_path):
    project, env = installer
    (project / 'socket.py').write_text(_PORT_SEAM)
    _executable(Path(env['PATH'].split(os.pathsep)[0]) / 'curl',
        'printf "curl:%s\\n" "$*" >> "$CALLS_FILE"\n'
        f'[[ "${{@: -1}}" == "{PUBLISHED_INSTALLER_URL}" ]] || exit 22\n'
        'cat "$FAKE_PUBLISHED_INSTALLER"\n')
    _serve_clones_from(project, env)
    agent_home = tmp_path / 'agent-home'
    agent_home.mkdir()
    (tmp_path / 'agent-tmp').mkdir()
    env.update(HOME=str(agent_home), TMPDIR=str(tmp_path / 'agent-tmp'),
               FAKE_PUBLISHED_INSTALLER=str(project / 'install.sh'))
    # Agents usually work inside the user's own project.
    user_project = _python_project(tmp_path / 'user-project')
    before = _tree(user_project)

    exit_code, receipt, output = _run_agent_guide_launch(
        _agent_guide_launch_block('| bash -s --'), user_project, env)

    assert exit_code == 0, output[-4000:]
    assert receipt['status'] == 'ready' and receipt['url'] == 'http://localhost:18677'
    assert receipt['install_dir'] == os.path.realpath(agent_home / 'tofu')
    assert _tree(user_project) == before
    calls = Path(env['CALLS_FILE']).read_text()
    assert calls.count('curl:') == 1 and PUBLISHED_INSTALLER_URL in calls
    assert ' clone ' in calls and 'launch:18677' in calls and 'health:18677' in calls


def test_agent_guide_checkout_install_keeps_the_checkout_code(installer, tmp_path):
    project, env = installer
    (project / 'socket.py').write_text(_PORT_SEAM)
    (tmp_path / 'agent-tmp').mkdir()
    env['TMPDIR'] = str(tmp_path / 'agent-tmp')

    exit_code, receipt, output = _run_agent_guide_launch(
        _agent_guide_launch_block('bash install.sh'), project, env)

    assert exit_code == 0, output[-4000:]
    assert receipt['status'] == 'ready' and receipt['url'] == 'http://localhost:18677'
    assert receipt['install_dir'] == os.path.realpath(project)
    calls = Path(env['CALLS_FILE']).read_text()
    assert ' pull ' not in calls and 'UNEXPECTED' not in calls and 'health:18677' in calls


@pytest.mark.parametrize('failure', [False, True])
def test_stale_source_frontend_uses_lifecycle_preparation_without_release_download(installer, failure):
    project, env = installer
    (project / 'node_modules/vite').mkdir(parents=True)
    (project / 'node_modules/vite/package.json').write_text('{}')
    (project / 'scripts/build_frontend.mjs').write_text('// fixture builder')
    (project / 'scripts/verify_frontend_dist.py').write_text('raise SystemExit(1)')
    (project / 'serverctl.py').write_text(
        'import os, sys; from pathlib import Path\n'
        'Path(os.environ["CALLS_FILE"]).open("a").write("prepare:" + " ".join(sys.argv[1:]) + "\\n")\n'
        f'raise SystemExit({int(failure)})\n')
    old_config = (project / '.env').read_bytes()
    result = _run(project, env, '--no-launch', '--no-update')
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'prepare:prepare-frontend --parts frontend' in calls
    assert 'UNEXPECTED-NETWORK' not in calls
    if failure:
        assert result.returncode != 0
        assert (project / '.env').read_bytes() == old_config
        assert not (project / '.tofu_env.json').exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
