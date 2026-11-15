"""Download selection uses native Linux ARM artifacts."""
import pytest

pytestmark = pytest.mark.unit


def test_linux_arm_downloads_never_recommend_x64_when_native_exists(monkeypatch):
    from lib.desktop_dist import platforms
    import scripts.release_assets as policy
    rows = policy.PLATFORM_ASSETS
    monkeypatch.setattr(platforms, '_platform_assets', lambda: rows)
    arch = platforms._detect_arch('Mozilla/5.0 (X11; Linux aarch64)', '')
    assert arch == 'arm64'
    selected = platforms._platform_rows_for('linux', arch)
    assert selected and all(row[1] == 'arm64' for row in selected)
    assert all('aarch64' in row[3] for row in selected)
