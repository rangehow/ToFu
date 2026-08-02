"""tests/test_installer_parity.py — the installer authorings must agree.

WHY THIS EXISTS
---------------
The CI builds the Windows installer with Inno Setup (a heredoc inside
.github/workflows/build-desktop.yml); the server-side build uses NSIS
(desktop/installer.nsi.tmpl), because every 32-bit Windows app measured
hangs under the server box's preloader-less WoW64 and Inno 7's container
resists extraction (2026-08-01, see the .nsi header).

Two authorings of one installer will drift — and since 2026-08-02 there
are also TWO COMPONENTS (full app / agent, docs/DESKTOP_AGENT_DIST_DESIGN
.md) rendered from ONE parametrized template. The defence is NOT
pretending there is one file — it is pinning the SEMANTIC CONTRACT they
must all honour, asserted on the RENDERED scripts (winbuilder._render_nsi),
not on the raw template: app name, install dir, privilege level, the two
shortcuts, launch-after-install, output naming, wizard assets, payload
shape, and — for the agent — the boot-autostart contract (owner
amendment ①: default-ON, HKCU, removed at uninstall, value name shared
with the tray toggle).

Run:  pytest tests/test_installer_parity.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.desktop_dist import winbuilder as wb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = (_ROOT / '.github' / 'workflows' / 'build-desktop.yml') \
    .read_text(encoding='utf-8')
_NSI = (_ROOT / 'desktop' / 'installer.nsi.tmpl') \
    .read_text(encoding='utf-8')

# The rendered scripts are the contract surface — placeholders are the
# mechanism, the rendering is what makensis compiles.
_FULL = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'full')
_AGENT = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'agent')


def _expand(script: str, target: str) -> str:
    """Resolve the NSIS ${APP_NAME}/${APP_EXE} defines, so assertions read
    what the INSTALLED result is (shortcut names, dirs) — not the
    indirection."""
    t = wb._NSI_TARGETS[target]
    return (script.replace('${APP_NAME}', t['app_name'])
                  .replace('${APP_EXE}', t['app_exe']))


def _code(script: str) -> str:
    """Drop NSIS comment lines — the template's own documentation names
    the autostart machinery, and absence assertions must target CODE."""
    return '\n'.join(l for l in script.splitlines()
                     if not l.lstrip().startswith(';'))


# ═══════════════════════════════════════════════════════════════════
#  FULL component — must stay behavior-identical to the historical installer
# ═══════════════════════════════════════════════════════════════════

def test_app_name_and_install_dir_agree():
    assert 'AppName=Tofu\n' in _WORKFLOW, (
        'the full Inno authoring must name the app exactly "Tofu" — '
        '"AppName=Tofu Agent" satisfies a bare substring check, so this '
        'assertion is newline-anchored')
    assert '!define APP_NAME      "Tofu"' in _FULL
    # Inno {localappdata}\Programs\Tofu ≡ NSIS $LOCALAPPDATA\Programs\Tofu
    assert r'{localappdata}\\Programs\\Tofu' in _WORKFLOW
    assert r'$LOCALAPPDATA\Programs\Tofu' in _FULL


def test_privilege_level_agrees():
    """Both are per-user installs with no UAC — the whole reason the app's
    portable data dir is writable on first launch."""
    assert 'PrivilegesRequired=lowest' in _WORKFLOW
    assert 'RequestExecutionLevel user' in _NSI


def test_shortcuts_and_launch_after_install_agree():
    # Start-menu shortcut (the workflow heredoc carries literal double
    # backslashes — assert the text as it IS, not as Inno would parse it)
    assert r'{autoprograms}\\Tofu' in _WORKFLOW
    assert r'$SMPROGRAMS\Tofu' in _expand(_FULL, 'full')
    # Desktop shortcut
    assert r'{autodesktop}\\Tofu' in _WORKFLOW
    assert r'$DESKTOP\Tofu.lnk' in _expand(_FULL, 'full')
    # Launch after install
    assert 'Flags: nowait postinstall' in _WORKFLOW
    assert 'MUI_FINISHPAGE_RUN' in _FULL
    # Uninstall path exists in both (Inno generates one; NSIS needs a section)
    assert 'WriteUninstaller' in _FULL and 'Section "Uninstall"' in _FULL


def test_output_name_pattern_agrees():
    assert 'OutputBaseFilename=Tofu-Setup-${APP_VERSION}-win64' in _WORKFLOW
    # The server's wrapper names the file by the same formula, driven by
    # the per-target identity table (not a literal that can drift).
    assert wb._NSI_TARGETS['full']['setup_prefix'] == 'Tofu-Setup'
    import inspect
    src = inspect.getsource(wb.wrap_payload)
    assert 'setup_prefix' in src and '-win64.exe' in src, (
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
    """Both pack the PyInstaller output tree with the app exe at its root."""
    assert r'dist\\Tofu\\*' in _WORKFLOW
    # POSIX glob in the .nsi — see the wizard-assets note for why
    # backslashes are wrong for the native linux makensis.
    assert '@PAYLOAD_DIR@/*' in _NSI
    # The preseed contract: the file rides INSIDE the payload, next to the
    # exe, and the launchers import it on first run. Three pieces that
    # must name the same file or the zero-paste flow silently degrades.
    assert 'preseed_server.json' in _NSI
    connect_ui = (_ROOT / 'desktop' / 'connect_ui.py') \
        .read_text(encoding='utf-8')
    assert 'preseed_server.json' in connect_ui, (
        'connect_ui lost the preseed import — the .nsi ships a file '
        'nothing reads')


# ═══════════════════════════════════════════════════════════════════
#  AGENT component — identity + the autostart contract (owner amendment ①)
# ═══════════════════════════════════════════════════════════════════

def test_agent_rendering_has_its_own_identity():
    assert '!define APP_NAME      "Tofu Agent"' in _AGENT
    assert '!define APP_EXE       "TofuAgent.exe"' in _AGENT
    assert r'$LOCALAPPDATA\Programs\TofuAgent' in _AGENT
    assert wb._NSI_TARGETS['agent']['setup_prefix'] == 'TofuAgent-Setup'
    # Per-user, same as full — the autostart key is HKCU precisely because
    # the privilege model matches.
    assert 'RequestExecutionLevel user' in _AGENT


def test_full_rendering_has_no_autostart():
    """A user-present tray app must NOT grow a Run key (owner: agent only)."""
    code = _code(_FULL)
    assert 'MUI_PAGE_COMPONENTS' not in code
    assert 'WriteRegStr' not in code
    assert 'DeleteRegValue' not in code


def test_agent_autostart_is_default_on_uac_free_and_uninstalled():
    # A components page offers the choice; the main section becomes
    # uncheckable so "uncheck everything" cannot install nothing.
    assert '!insertmacro MUI_PAGE_COMPONENTS' in _AGENT
    assert 'SectionIn RO' in _AGENT
    # Default-ON: the section exists and is NOT prefixed with /o.
    assert 'Section "Start with Windows"' in _AGENT
    assert 'Section /o "Start with Windows"' not in _AGENT
    # HKCU Run value (UAC-free), quoted-exe data, matching the tray's form.
    run_key = r'Software\Microsoft\Windows\CurrentVersion\Run'
    assert f'WriteRegStr HKCU "{run_key}" "TofuAgent"' in _AGENT
    assert '\'"$INSTDIR\\${APP_EXE}"\'' in _AGENT
    # The uninstaller removes it unconditionally — a removed app must not
    # leave a dead autorun pointing at a missing exe.
    assert f'DeleteRegValue HKCU "{run_key}" "TofuAgent"' in _AGENT


def test_the_autostart_value_name_is_shared_with_the_tray():
    """The installer, the tray toggle and the reconcile logic must write
    the SAME Run value name — three writers, one key, or the states
    diverge silently (installer says on, tray says off)."""
    import desktop.agent_launcher as al
    assert al._RUN_VALUE == 'TofuAgent'
    assert f'"{al._RUN_VALUE}"' in _AGENT


# ═══════════════════════════════════════════════════════════════════
#  The renderer itself — no placeholder may survive in either component
# ═══════════════════════════════════════════════════════════════════

def test_every_placeholder_is_substituted_in_both_renderings():
    for p in wb._NSI_PLACEHOLDERS:
        assert p not in _FULL, f'{p} left unrendered in the full script'
        assert p not in _AGENT, f'{p} left unrendered in the agent script'


def test_ci_inno_agent_authoring_matches_the_autostart_contract():
    """The CI's Inno agent authoring and the server's NSIS render — 2
    components × 2 tools, ONE contract (design §5.3 + owner amendment ①).
    The NSIS side is pinned above; this pins the workflow's Inno side."""
    assert 'AppName=Tofu Agent' in _WORKFLOW
    assert ('OutputBaseFilename=TofuAgent-Setup-${APP_VERSION}-win64'
            in _WORKFLOW)
    assert r'{localappdata}\\Programs\\TofuAgent' in _WORKFLOW
    run_key = r'Software\\Microsoft\\Windows\\CurrentVersion\\Run'
    assert f'Subkey: "{run_key}"' in _WORKFLOW
    assert 'ValueName: "TofuAgent"' in _WORKFLOW, (
        'CI and server and tray must write ONE Run value name')
    assert 'uninsdeletevalue' in _WORKFLOW, (
        'the uninstaller must remove the autorun — same contract as NSIS')
    # Default-ON, like the NSIS default-selected section: no unchecked flag.
    assert 'Name: autostart; Description: "Start Tofu Agent with Windows"' \
        in _WORKFLOW
    import re as _re
    assert not _re.search(r'autostart[^\n]*unchecked', _WORKFLOW, _re.I)
    # Same privilege floor as the full installer (HKCU needs no UAC).
    assert 'PrivilegesRequired=lowest' in _WORKFLOW


def test_no_section_commands_leak_into_comments():
    """The 2026-08-02 collision: the renderer is a global replace, so a
    code-valued placeholder named in a COMMENT expanded there —
    WriteRegStr outside any Section, and makensis aborted (measured on
    the first real agent wrap). Comments must stay comment-only."""
    for script in (_FULL, _AGENT):
        for line in script.splitlines():
            if 'SectionEnd' in line:
                assert line.strip() == 'SectionEnd', (
                    f'SectionEnd carrying trailing text: {line!r} — a '
                    'code-valued placeholder expanded inside a comment '
                    '(the renderer replaces everywhere; keep @-tokens '
                    'out of comments)')
            if line.lstrip().startswith(';'):
                assert 'WriteRegStr HKCU' not in line
                assert 'DeleteRegValue HKCU' not in line
