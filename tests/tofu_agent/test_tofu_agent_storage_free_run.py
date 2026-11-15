"""`tofu-agent run` from the checkout behind the wheel's import boundary.

The storage-free install's import contract without building the wheel: every
checkout module that the package manifest (pyproject.toml, read by
``scripts/agent_wheel_manifest.py``) leaves out, such as ``lib.storage``,
resolves as it would in the installed wheel, from outside the checkout or not
at all. The run must complete its tool round without attempting any such
import except the declared optional storage capability.

Change-based selection runs this file whenever a change touches a module the
wheel ships (``scripts/test_select.py``, docs/TESTING_STRATEGY.md "Agent wheel
boundary guard"). It needs no wheel build or uv cache, so it also runs where
``test_tofu_agent_run_installed.py`` skips; that test still owns packaging
(entry points, package data and the built file list).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.agent_wheel_manifest import WheelManifest
from tests.support.agent_wheel_run import (
    ROOT, assert_tool_round, console_entry, manifest_modules, run_tofu_agent, unsatisfied_imports,
)

pytestmark = pytest.mark.api


@pytest.mark.timeout(300)
def test_storage_free_run_completes_a_tool_round_inside_the_wheel_boundary(tmp_path):
    top_level = {name.partition('.')[0] for name in manifest_modules()}

    run = run_tofu_agent(tmp_path, application=ROOT, entry=console_entry(),
                         top_level=top_level, boundary=True)

    assert_tool_round(run)
    assert not unsatisfied_imports(run)
    manifest = WheelManifest.load(ROOT)
    checkout = ROOT.resolve()
    assert not sorted(name for name, path in run.report['modules'].items()
                      if path and Path(path).resolve().is_relative_to(checkout)
                      and not manifest.ships_module(name))
