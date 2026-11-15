"""tests/agent_core/test_agent_winbuilder.py — the Windows builder has ONE target.

WHAT THIS GUARDS
----------------
Owner decision 2026-11-15: every desktop install is a peer node that joins
servers by pairing, so the separate controlled-end (Tofu Remote) build is
retired. winbuilder builds and wraps only the full desktop app:

  * the target tables carry only 'full' (no second payload, no second
    installer identity);
  * the full payload name and wrap record are byte-identical to before;
  * the nuget python gets the tcl/tk graft;
  * the registration frame carries a version (owner amendment ② — the
    drift-detection half).

The heavy half (git/pip/pyinstaller) is faked exactly like
test_winbuilder.py; the recording half (tar + atomic rename) runs real.

Run:  pytest tests/agent_core/test_agent_winbuilder.py -q
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


# ═══════════════════════════════════════════════════════════════════
#  payload identity — one target
# ═══════════════════════════════════════════════════════════════════

def test_the_builder_has_only_the_full_target():
    assert set(wb._TARGETS) == {'full'}
    assert set(wb._NSI_TARGETS) == {'full'}
    assert wb._NSI_TARGETS['full']['setup_prefix'] == 'Tofu-Setup'


def test_full_payload_name_is_byte_identical_to_before(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe')
    assert p.endswith('payload-abcdef123456-deadbeefcafebabe.tar.gz'), (
        'the full payload name must NOT change — existing caches and '
        'every audit referencing it stay valid')


# ═══════════════════════════════════════════════════════════════════
#  Half B — the wrapper (makensis faked; untar + recording real)
# ═══════════════════════════════════════════════════════════════════

def test_full_wrap_record_has_kind_full(isolated, monkeypatch, tmp_path):
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            out = isolated / 'wrap' / 'Tofu-Setup-0.16.0-win64.exe'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-FULL-INSTALLER')
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        wb.wrap_payload(str(payload_tar), '0.16.0', 'c' * 40, log,
                        workdir=str(isolated / 'wrap'))
    e = wb.store.artifacts()['Tofu-Setup-0.16.0-win64.exe']
    assert e['kind'] == 'full'
    assert e['label'] == 'Windows installer'


# ═══════════════════════════════════════════════════════════════════
#  the tcl/tk graft — the nuget python ships NO tkinter (measured)
# ═══════════════════════════════════════════════════════════════════

def test_winpython_provisioning_includes_the_tk_graft():
    """The nuget CPython has 0 tcl files (measured in the nupkg) — without
    the graft every built installer's native control panel is dead. Pin the
    call site."""
    src = inspect.getsource(wb._ensure_winpython)
    assert '_ensure_winpython_tk' in src, (
        'the tcl/tk graft fell out of winpython provisioning — every '
        'installer built from now on has a dead native control panel')


def test_tk_graft_short_circuits_on_a_grafted_python(isolated, monkeypatch,
                                                     tmp_path):
    rootfs = tmp_path / 'rootfs'
    tools = rootfs / 'opt' / 'winpy' / 'tools'
    (tools / 'DLLs').mkdir(parents=True)
    (tools / 'DLLs' / '_tkinter.pyd').write_text('x')
    (tools / 'Lib' / 'tkinter').mkdir(parents=True)
    (tools / 'tcl').mkdir(parents=True)
    monkeypatch.setattr(wb.wintoolchain, 'rootfs_dir', lambda: str(rootfs))
    monkeypatch.setattr(wb.wintoolchain, '_download',
                        lambda *a: pytest.fail('download on a grafted python'))
    with _fake_log(isolated) as log:
        wb._ensure_winpython_tk(log)   # must not download, must not raise


def test_tk_graft_copies_the_standard_layout(isolated, monkeypatch, tmp_path):
    rootfs = tmp_path / 'rootfs'
    tools = rootfs / 'opt' / 'winpy' / 'tools'
    (tools / 'DLLs').mkdir(parents=True)
    cache = tmp_path / 'cache'
    (cache).mkdir()
    (cache / 'python-tcltk.msi').write_text('fake-msi')
    staging = rootfs / 'work' / 'tcltk-graft'

    def _fake_sh(cmd, log_fh, *, shell=False, timeout=9999):
        # The msiextract fake: materialise the python.org standard layout.
        for rel in ('DLLs/_tkinter.pyd', 'DLLs/tcl86t.dll',
                    'DLLs/tk86t.dll', 'DLLs/zlib1.dll'):
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x')
        for rel in ('Lib/tkinter', 'tcl/tcl8.6', 'tcl/tk8.6'):
            (staging / rel).mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(wb.wintoolchain, 'rootfs_dir', lambda: str(rootfs))
    monkeypatch.setattr(wb.wintoolchain, 'cache_dir', lambda: str(cache))
    monkeypatch.setattr(wb, '_ensure_msiextract',
                        lambda log: '/fake/msiextract')
    monkeypatch.setattr(wb, '_sh', _fake_sh)
    with _fake_log(isolated) as log:
        wb._ensure_winpython_tk(log)
    assert (tools / 'DLLs' / '_tkinter.pyd').is_file()
    assert (tools / 'DLLs' / 'tcl86t.dll').is_file()
    assert (tools / 'Lib' / 'tkinter').is_dir()
    assert (tools / 'tcl' / 'tcl8.6').is_dir()
    # The staging dir is cleaned up after a successful graft.
    assert not staging.exists()


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
