"""tests/test_agent_winbuilder.py — the AGENT target of the Windows builder.

WHAT THIS GUARDS
----------------
docs/DESKTOP_AGENT_DIST_DESIGN.md slice A1: winbuilder builds a SECOND
payload (target='agent') carrying only the desktop-agent closure. The
contract:

  * payload identity — the agent has its OWN deps stamp (tofu-agent.spec
    + the agent pip recipe; server requirements are NOT inputs) and its
    own payload name (payload-agent-…), so it never collides with or
    invalidates the full payload's cache;
  * the full target is byte-identical to before (name, stamp inputs);
  * the agent pipeline installs the AGENT pip recipe — never
    requirements.txt, which IS the server stack this target exists to
    leave behind (NEUTER: swapping in the full recipe must go red);
  * the agent smoke gate is the agent's own env var + sentinel
    (TOFU_AGENT_SMOKE / TOFU_AGENT_SMOKE_OK);
  * the registration frame carries a version (owner amendment ② — the
    drift-detection half);
  * the smoke gate's banned-list assertion is wired (source pin).

The heavy half (git/pip/pyinstaller) is faked exactly like
test_winbuilder.py; the recording half (tar + atomic rename) runs real.

Run:  pytest tests/test_agent_winbuilder.py -q
"""

from __future__ import annotations

import inspect
import os
import tarfile

import pytest

from lib.desktop_dist import winbuilder as wb

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Isolate the store, the payload cache and the toolchain layout."""
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path / 'store'))
    monkeypatch.setenv('TOFU_WIN_TC_DIR', str(tmp_path / 'local'))
    payloads = tmp_path / 'payloads'
    monkeypatch.setattr(wb, '_payloads_dir',
                        lambda: (os.makedirs(payloads, exist_ok=True),
                                 str(payloads))[1])
    monkeypatch.setattr(wb.wintoolchain, 'provision', lambda: None)
    return tmp_path


def _fake_log(tmp_path):
    return open(tmp_path / 'log.txt', 'w', encoding='utf-8')


def _fake_agent_dist(workdir):
    dist = os.path.join(workdir, 'staging')
    os.makedirs(os.path.join(dist, 'TofuAgent'), exist_ok=True)
    with open(os.path.join(dist, 'TofuAgent', 'TofuAgent.exe'), 'w') as f:
        f.write('fake-binary')
    return dist


# ═══════════════════════════════════════════════════════════════════
#  payload identity — two targets, two independent caches
# ═══════════════════════════════════════════════════════════════════

def test_agent_stamp_differs_from_full_and_is_stable():
    a = wb.deps_stamp('agent')
    assert len(a) == 16 and all(c in '0123456789abcdef' for c in a)
    assert wb.deps_stamp('agent') == a, 'same inputs, same stamp'
    assert a != wb.deps_stamp('full'), (
        'the agent stamp must be its own identity — sharing the full '
        'stamp would cross-invalidate both caches')


def test_full_payload_name_is_byte_identical_to_before(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe')
    assert p.endswith('payload-abcdef123456-deadbeefcafebabe.tar.gz'), (
        'the full payload name must NOT change — existing caches and '
        'every audit referencing it stay valid')


def test_agent_payload_name_is_namespaced(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe',
                        target='agent')
    assert p.endswith(
        'payload-agent-abcdef123456-deadbeefcafebabe.tar.gz')


def test_the_two_caches_do_not_satisfy_each_other(isolated):
    sha = 'abc123def456' + '0' * 28
    for target, prefix in (('full', 'payload'), ('agent', 'payload-agent')):
        dest = wb.payload_path(sha, target=target)
        assert os.path.basename(dest).startswith(prefix + '-')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(b'payload')
    # Each target sees ONLY its own file.
    assert wb.cached_payload(sha, target='full') == wb.payload_path(sha)
    assert (wb.cached_payload(sha, target='agent')
            == wb.payload_path(sha, target='agent'))


def test_agent_cache_hit_skips_the_pipeline(isolated, monkeypatch):
    sha = 'abc123def456' + '0' * 28
    dest = wb.payload_path(sha, target='agent')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as f:
        f.write(b'payload')
    monkeypatch.setattr(wb, '_git', lambda *a: sha + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda *a, **k: pytest.fail(
                            'pipeline ran on an agent cache hit'))
    wb._run('test', 'agent')
    st = wb.state()
    assert st['state'] == 'ok' and st['cached'] is True
    assert st['target'] == 'agent'
    assert st['payload'] == dest


def test_NEUTER_agent_run_records_its_own_payload(isolated, monkeypatch):
    """A real agent run (pipeline faked) must tar the TofuAgent dir into
    the AGENT cache name — dropping the target threading anywhere
    (stamp, path, tar dir) is caught by the name or the contents."""
    monkeypatch.setattr(wb, '_git', lambda *a: 'a' * 40 + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda workdir, version, sha, log_fh, **_:
                        _fake_agent_dist(workdir))
    wb._run('test', 'agent')
    st = wb.state()
    assert st['state'] == 'ok', st
    dest = st['payload']
    assert dest == wb.payload_path('a' * 40, target='agent')
    assert os.path.basename(dest).startswith('payload-agent-')
    with tarfile.open(dest) as tf:
        assert any(n.endswith('TofuAgent/TofuAgent.exe')
                   for n in tf.getnames()), (
            'the tarball must carry the TofuAgent app dir — tarring '
            "'Tofu' here would ship an empty or wrong payload")


def test_agent_stamp_ignores_requirements_txt(isolated, monkeypatch,
                                              tmp_path):
    """A server-only dependency bump must NOT rebuild the agent payload:
    requirements.txt is not an agent stamp input."""
    import hashlib
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'tofu-agent.spec').write_text('# spec v1\n')
    monkeypatch.setattr(wb, '_REPO_ROOT', str(root))
    stamp_before = wb.deps_stamp('agent')
    (root / 'requirements.txt').write_text('a-brand-new-server-dep==9.9\n')
    assert wb.deps_stamp('agent') == stamp_before, (
        'the agent stamp must ignore requirements.txt — the agent '
        'payload does not contain it')
    # …but the spec IS an input.
    (root / 'tofu-agent.spec').write_text('# spec v2 — changed\n')
    assert wb.deps_stamp('agent') != stamp_before


# ═══════════════════════════════════════════════════════════════════
#  the agent pipeline shape (source pins — the heavy half is wine)
# ═══════════════════════════════════════════════════════════════════

def test_agent_pipeline_installs_the_agent_recipe_not_requirements():
    """NEUTER-bait: the agent pipeline's pip step must name the AGENT
    recipe and must NOT touch requirements.txt. Freezing the server
    stack into the agent is the exact defect this component exists to
    kill — a 'harmless simplification' that reuses the full recipe must
    be a red test."""
    src = inspect.getsource(wb._pipeline)
    assert "'pip-agent-deps'" in src
    assert '_AGENT_PIP' in src
    agent_head = src[src.index("if target == 'agent'"):]
    agent_head = agent_head[:agent_head.index('else:')]
    assert "'pip-requirements'" not in agent_head
    assert '_requirements_without_tofu_search' not in agent_head
    # The step itself must install a NAMED LIST, never a requirements file.
    step = src[src.index("'pip-agent-deps'"):]
    step = step[:step.index('log_fh, cwd=src')]
    assert '-r ' not in step, (
        'the agent pip step installs a requirements file — that file IS '
        'the server stack this target exists to leave behind')
    # The full branch keeps its recipe (order pinned by the sibling suite).
    assert "'pip-requirements'" in src


def test_agent_pipeline_uses_the_agent_spec_and_smoke_gate():
    src = inspect.getsource(wb._pipeline).replace("'", '"')
    # The per-target table drives spec/exe/env/sentinel — not literals.
    assert 'tgt["spec"]' in src and 'tgt["exe"]' in src
    assert 'tgt["smoke_env"]' in src and 'tgt["smoke_sentinel"]' in src
    t = wb._TARGETS['agent']
    assert t['spec'] == 'tofu-agent.spec'
    assert t['app_dir'] == 'TofuAgent' and t['exe'] == 'TofuAgent.exe'
    assert t['smoke_env'] == 'TOFU_AGENT_SMOKE'
    assert t['smoke_sentinel'] == 'TOFU_AGENT_SMOKE_OK'
    # …and the full row is the historical shape.
    f = wb._TARGETS['full']
    assert f['spec'] == 'tofu.spec' and f['app_dir'] == 'Tofu'
    assert f['smoke_env'] == 'TOFU_SMOKE'
    assert f['smoke_sentinel'] == 'TOFU_SMOKE_OK'


# ═══════════════════════════════════════════════════════════════════
#  the agent registration frame carries a version (owner amendment ②)
# ═══════════════════════════════════════════════════════════════════

def test_agent_frame_carries_the_version():
    from lib.desktop_agent._run import _build_agent_frame
    frame = _build_agent_frame('a' * 32, {'allow_write': False})
    from lib.version import __version__
    assert frame['version'] == __version__.strip(), (
        'the poll frame must carry the agent version — the server '
        'cannot flag agent↔server protocol drift without it (owner '
        'amendment ②)')
    # The historical fields are untouched.
    assert frame['agent_id'] == 'a' * 32
    assert 'capabilities' in frame and 'share_roots' in frame


def test_agent_frame_version_never_breaks_the_poll(monkeypatch):
    """A version-read failure must degrade to '' — never stop the frame."""
    import lib.desktop_agent._run as run_mod
    monkeypatch.setitem(__import__('sys').modules, 'lib.version', None)
    assert run_mod._agent_version() == ''
    frame = run_mod._build_agent_frame('b' * 32, {})
    assert frame['version'] == ''


# ═══════════════════════════════════════════════════════════════════
#  the smoke gate asserts the small closure (source pins)
# ═══════════════════════════════════════════════════════════════════

def test_agent_smoke_gate_bans_the_server_stack():
    """The frozen-build proof of the size claim: the smoke gate must
    assert the server stack is absent from BOTH sys.modules and the
    bundle tree, and that tkinter (the connect dialog) ships."""
    import desktop.agent_launcher as al
    src = inspect.getsource(al._smoke_main)
    for banned in ('quart', 'flask', 'hypercorn'):
        assert banned in src, f'smoke gate lost its {banned} assertion'
    assert '_MEIPASS' in src, 'the bundle-tree assertion is gone'
    assert 'import tkinter' in src, (
        'the tkinter assertion is gone — a build without the dialog '
        "toolkit would ship and only fail on the user's machine")
    assert 'TOFU_AGENT_SMOKE_OK' in src


def test_launcher_delegates_the_connect_seams():
    """The extraction contract: launcher.py keeps the old NAMES (call
    sites + patch points) but the implementations live in connect_ui —
    two copies of the dialog/preseed would drift (parity philosophy)."""
    import desktop.launcher as launcher
    src_pre = inspect.getsource(launcher._import_preseed)
    src_dlg = inspect.getsource(launcher._prompt_connect_line)
    assert 'connect_ui' in src_pre and 'connect_ui' in src_dlg
    assert 'os.remove' not in src_pre and 'os.path.isfile' not in src_pre, (
        'the preseed logic is back in launcher.py — the move became a copy')


def test_connect_ui_owns_the_wire_format_parse():
    import desktop.connect_ui as cui
    src = inspect.getsource(cui.prompt_connect_line)
    assert 'parse_connect_line' in src, (
        'the dialog must parse through lib.desktop_agent.config — the '
        'single owner of the connect-line format')
