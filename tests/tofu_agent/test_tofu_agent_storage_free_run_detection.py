"""The storage-free probe fails when a shipped module imports one the wheel leaves out.

Reproduces 2026-11-13 (22665a749e): ``lib/router/instructions.py`` imported
``StorageError`` from ``lib.storage.errors`` at module level, memory prefetch
reached it on the first request, and every run of the installed wheel failed.
The checkout hid it. The probe must fail the same way on that module, from the
checkout and without a wheel.
"""
from __future__ import annotations

import pytest

from tests.support.agent_wheel_run import (
    ROOT, console_entry, manifest_modules, run_tofu_agent, unsatisfied_imports,
)

pytestmark = pytest.mark.api


@pytest.mark.timeout(300)
def test_probe_fails_when_a_shipped_module_on_the_run_path_imports_lib_storage(tmp_path):
    regressed = tmp_path / 'instructions.py'
    regressed.write_text(
        (ROOT / 'lib/router/instructions.py').read_text(encoding='utf-8')
        + '\nfrom lib.storage.errors import StorageError as _host_storage_error\n',
        encoding='utf-8')

    run = run_tofu_agent(tmp_path, application=ROOT, entry=console_entry(),
                         top_level={name.partition('.')[0] for name in manifest_modules()},
                         boundary=True, overlay={'lib.router.instructions': regressed})

    assert ['lib.storage', 'lib.router.instructions'] in unsatisfied_imports(run)
    assert "No module named 'lib.storage'" in run.completed.stderr
    assert not (run.result or {}).get('ok')
