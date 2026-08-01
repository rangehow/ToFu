"""tests/test_installer_parity.py — the two installer authorings must agree.

WHY THIS EXISTS
---------------
The CI builds the Windows installer with Inno Setup (a heredoc inside
.github/workflows/build-desktop.yml); the server-side build uses NSIS
(desktop/installer.nsi.tmpl), because every 32-bit Windows app measured
hangs under the server box's preloader-less WoW64 and Inno 7's container
resists extraction (2026-08-01, see the .nsi header).

Two authorings of one installer will drift. The defence is NOT pretending
there is one file — it is pinning the SEMANTIC CONTRACT they must both
honour: app name, install dir, privilege level, the two shortcuts,
launch-after-install, output naming, wizard assets, payload shape. If
either authoring changes one of these, this suite is red.

Run:  pytest tests/test_installer_parity.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = (_ROOT / '.github' / 'workflows' / 'build-desktop.yml') \
    .read_text(encoding='utf-8')
_NSI = (_ROOT / 'desktop' / 'installer.nsi.tmpl') \
    .read_text(encoding='utf-8')


def test_app_name_and_install_dir_agree():
    assert 'AppName=Tofu' in _WORKFLOW
    assert '!define APP_NAME      "Tofu"' in _NSI
    # Inno {localappdata}\Programs\Tofu ≡ NSIS $LOCALAPPDATA\Programs\Tofu
    assert r'{localappdata}\\Programs\\Tofu' in _WORKFLOW
    assert r'$LOCALAPPDATA\Programs\Tofu' in _NSI


def test_privilege_level_agrees():
    """Both are per-user installs with no UAC — the whole reason the app's
    portable data dir is writable on first launch."""
    assert 'PrivilegesRequired=lowest' in _WORKFLOW
    assert 'RequestExecutionLevel user' in _NSI


def test_shortcuts_and_launch_after_install_agree():
    # Start-menu shortcut (the workflow heredoc carries literal double
    # backslashes — assert the text as it IS, not as Inno would parse it)
    assert r'{autoprograms}\\Tofu' in _WORKFLOW
    assert r'$SMPROGRAMS\Tofu' in _NSI
    # Desktop shortcut
    assert r'{autodesktop}\\Tofu' in _WORKFLOW
    assert r'$DESKTOP\Tofu.lnk' in _NSI
    # Launch after install
    assert 'Flags: nowait postinstall' in _WORKFLOW
    assert 'MUI_FINISHPAGE_RUN' in _NSI
    # Uninstall path exists in both (Inno generates one; NSIS needs a section)
    assert 'WriteUninstaller' in _NSI and 'Section "Uninstall"' in _NSI


def test_output_name_pattern_agrees():
    assert 'OutputBaseFilename=Tofu-Setup-${APP_VERSION}-win64' in _WORKFLOW
    # The server's wrapper names the file by the same formula (pinned in
    # winbuilder.py; the .nsi consumes it via @OUT_FILE@).
    import inspect
    from lib.desktop_dist import winbuilder as wb
    src = inspect.getsource(wb.wrap_payload)
    assert "f'Tofu-Setup-{version}-win64.exe'" in src, (
        'the wrapper renamed the installer — CI and server must produce '
        'the SAME filename shape or the artifact store cannot tell them '
        'apart by design')


def test_wizard_assets_are_the_same_files():
    """One icon set, two renderers — a rebrand must land in both."""
    for asset in (r'static\\icons\\tofu.ico',
                  r'static\\icons\\installer\\wizard-large.bmp'):
        assert asset in _WORKFLOW, f'{asset} missing from the Inno authoring'
    # The .nsi template is compiled by the NATIVE linux makensis: POSIX
    # separators are the only correct form there (a backslash is not a
    # path separator on linux — the glob would match nothing and ship an
    # EMPTY installer; the posix form is proven by the real server build
    # of 2026-08-01, 152 MB / 3316 files). Match by filename, not style.
    for asset in ('tofu.ico', 'wizard-large.bmp'):
        assert asset in _NSI, f'{asset} missing from the NSIS authoring'


def test_payload_shape_agrees():
    """Both pack the PyInstaller output tree with Tofu.exe at its root."""
    assert r'dist\\Tofu\\*' in _WORKFLOW
    # POSIX glob in the .nsi — see the wizard-assets note for why
    # backslashes are wrong for the native linux makensis.
    assert '@PAYLOAD_DIR@/*' in _NSI
    # The preseed contract: the file rides INSIDE the payload, next to the
    # exe, and the launcher imports it on first run. Three pieces that
    # must name the same file or the zero-paste flow silently degrades.
    assert 'preseed_server.json' in _NSI
    launcher = (_ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
    assert 'preseed_server.json' in launcher, (
        'the launcher lost the preseed import — the .nsi ships a file '
        'nothing reads')
