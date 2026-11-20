"""Run release shell boundaries with surgical stubs; no Apple trust is inferred."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


@pytest.fixture
def native_tools(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    stub = bindir / 'apple-tool'
    stub.write_text(r'''#!/usr/bin/env bash
set -eu
tool="${0##*/}"
printf '%s %s\n' "$tool" "$*" >> "$TRACE"
case "$tool:$1" in
    uname:-s) echo "${NATIVE_OS:-Darwin}" ;;
    hdiutil:verify) [[ "$FAULT" != corrupt ]] ;;
    hdiutil:attach)
        mount="$6"
        [[ "$FAULT" == layout ]] && exit 0
        mkdir -p "$mount/Tofu.app/Contents/"{MacOS,Frameworks}
        ln -s /Applications "$mount/Applications"
        touch "$mount/Tofu.app/Contents/MacOS/Tofu" "$mount/Tofu.app/Contents/Frameworks/tofu-db"
        chmod +x "$mount/Tofu.app/Contents/MacOS/Tofu" "$mount/Tofu.app/Contents/Frameworks/tofu-db" ;;
    hdiutil:detach) rm -rf "$2/Tofu.app" "$2/Applications" ;;
    plutil:-extract)
        case "$2" in
            CFBundleIdentifier) echo com.tofu.desktop ;;
            CFBundleShortVersionString) if [[ "$FAULT" == version ]]; then echo 0.19.1; else echo 0.19.2; fi ;;
            CFBundleExecutable) echo Tofu ;;
            status) echo "${NOTARY_STATUS:?}" ;;
            *) exit 99 ;;
        esac ;;
    lipo:-archs) if [[ "$FAULT" == arch ]]; then echo x86_64; else echo arm64; fi ;;
    codesign:--verify)
        [[ "$*" == *'anchor apple generic'* && "$*" == *'leaf[subject.OU] = "R3L3AS3APP"'* ]]
        case "${*: -1}" in
            *.app) [[ "$*" == *'--deep --strict --all-architectures'* && "$FAULT" != app_signature ]] ;;
            *.dmg) [[ "$FAULT" != dmg_signature ]] ;;
            *) exit 99 ;;
        esac ;;
    codesign:--timestamp) [[ "$FAULT" != dmg_signature ]] ;;
    xcrun:notarytool) [[ "$2" == submit && "$FAULT" != transport ]] && printf '{"status":"%s"}\n' "$NOTARY_STATUS" ;;
    xcrun:stapler) [[ "$2" == staple || ( "$2" == validate && "$FAULT" != ticket ) ]] ;;
    spctl:--status) if [[ "$FAULT" == disabled ]]; then echo 'assessments disabled'; else echo 'assessments enabled'; fi ;;
    spctl:--assess) [[ "$3" != open || "$FAULT" != dmg_policy ]] && [[ "$3" != execute || "$FAULT" != app_policy ]] ;;
    *) exit 99 ;;
esac
''', encoding='utf-8')
    stub.chmod(0o755)
    for name in ('uname', 'hdiutil', 'plutil', 'lipo', 'codesign', 'xcrun', 'spctl'):
        (bindir / name).symlink_to(stub)
    return {**os.environ, 'PATH': f'{bindir}{os.pathsep}{os.environ["PATH"]}',
            'TMPDIR': str(tmp_path), 'TRACE': str(tmp_path / 'trace')}


@pytest.mark.parametrize(('fault', 'marker'), [
    ('none', 'spctl --assess --type execute'),
    ('corrupt', 'hdiutil verify'),
    ('dmg_signature', 'codesign --verify --strict'),
    ('ticket', 'xcrun stapler validate'),
    ('disabled', 'spctl --status'),
    ('dmg_policy', 'spctl --assess --type open'),
    ('layout', 'hdiutil attach'),
    ('version', 'plutil -extract CFBundleShortVersionString'),
    ('arch', 'lipo -archs'),
    ('app_signature', 'codesign --verify --deep'),
    ('app_policy', 'spctl --assess --type execute'),
])
def test_final_macos_gate_rejects_bad_images_and_detaches(tmp_path, native_tools, fault, marker):
    dmg = tmp_path / 'Tofu-0.19.2-macos-arm64.dmg'
    dmg.write_bytes(b'synthetic DMG')
    proc = subprocess.run(['bash', str(ROOT / 'scripts/verify_macos_dmg.sh'), str(dmg),
                           'arm64', '0.19.2', 'R3L3AS3APP'],
                          env={**native_tools, 'FAULT': fault}, capture_output=True, text=True, timeout=10)
    if fault == 'none':
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0
    trace = (tmp_path / 'trace').read_text()
    assert ('hdiutil attach' in trace) == ('hdiutil detach' in trace)
    assert not list(tmp_path.glob('tofu-dmg.*'))
    assert marker in trace


def test_macos_verifier_refuses_a_non_macos_host(tmp_path, native_tools):
    proc = subprocess.run(['bash', str(ROOT / 'scripts/verify_macos_dmg.sh'), 'test.dmg',
                           'arm64', '0.19.2', 'R3L3AS3APP'],
                          env={**native_tools, 'NATIVE_OS': 'Linux'}, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0 and 'macOS only' in proc.stderr
    assert (tmp_path / 'trace').read_text().strip() == 'uname -s'


@pytest.mark.parametrize(('status', 'fault', 'staples'), [
    ('Accepted', 'none', True), ('Invalid', 'none', False), ('In Progress', 'none', False),
    ('Accepted', 'transport', False), ('Accepted', 'dmg_signature', False),
    ('Accepted', 'no_password', False),
])
def test_notary_stage_requires_accepted_result_before_stapling(tmp_path, native_tools, status, fault, staples):
    jobs = yaml.safe_load((ROOT / '.github/workflows/build-desktop.yml').read_text())['jobs']
    notarize = next(s for s in jobs['build-macos']['steps'] if 'notarytool submit' in s.get('run', ''))
    env = {**native_tools, 'FAULT': fault, 'NOTARY_STATUS': status, 'APP_VERSION': '0.19.2', 'ARCH': 'arm64',
           'MACOS_NOTARY_APPLE_ID': 'synthetic@example.test', 'MACOS_NOTARY_TEAM_ID': 'R3L3AS3APP',
           'MACOS_NOTARY_PASSWORD': '' if fault == 'no_password' else 'synthetic-test-password',
           'TOFU_MACOS_KEYCHAIN': 'test.keychain', 'TOFU_MACOS_SIGN_IDENTITY': '1' * 40}
    proc = subprocess.run(['bash', '-c', notarize['run']], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=10)
    assert (proc.returncode == 0) == staples, proc.stdout + proc.stderr
    assert 'synthetic-test-password' not in proc.stdout + proc.stderr
    if fault == 'no_password':
        assert not (tmp_path / 'trace').exists()
    else:
        trace = (tmp_path / 'trace').read_text()
        assert ('xcrun stapler staple' in trace) == staples
        if staples:
            assert trace.index('codesign --timestamp') < trace.index('xcrun notarytool submit') < trace.index('xcrun stapler staple')


@pytest.mark.parametrize('fault', ['none', 'changed', 'missing_receipt', 'wrong_version', 'extra_image'])
def test_release_promotes_only_exact_native_verified_dmg_bytes(tmp_path, fault):
    jobs = yaml.safe_load((ROOT / '.github/workflows/build-desktop.yml').read_text())['jobs']
    gate = next(s for s in jobs['release']['steps'] if s.get('name') == 'Check native-verified macOS bytes')
    assets = tmp_path / 'release-assets'
    assets.mkdir()
    version = '0.19.1' if fault == 'wrong_version' else '0.19.2'
    for arch in ('arm64', 'x86_64'):
        image = assets / f'Tofu-{version}-macos-{arch}.dmg'
        content = arch.encode()
        image.write_bytes(content[::-1] if fault == 'changed' else content)
        if fault != 'missing_receipt':
            (assets / f'{image.name}.verified.sha256').write_text(
                f'{hashlib.sha256(content).hexdigest()}  {image.name}\n', encoding='utf-8')
    if fault == 'extra_image':
        (assets / 'extra.dmg').touch()
    proc = subprocess.run(['bash', '-c', gate['run']], env={**os.environ, 'VER': '0.19.2'},
                          cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert (proc.returncode == 0) == (fault == 'none'), proc.stdout + proc.stderr
