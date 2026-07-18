#!/usr/bin/env python3
"""Unit tests for lib.desktop_agent — permission gates, shell-escape branch
selection, XGA coordinate remapping, and missing-dependency guards.

Pure logic only: pyautogui / pyperclip / psutil are NOT required. Importing
lib.desktop_agent._gui must succeed even when they are absent (the guarded
imports set them to None), and the scaling / dispatch / exec logic is tested
without touching a real screen or shell.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_desktop_agent.py -q
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  1. Coordinate scaling / remapping (XGA)  — the red-capable core
# ══════════════════════════════════════════════════════════

def test_compute_scale_downscales_to_xga():
    from lib.desktop_agent._scaling import compute_scale
    # 1920x1080 must fit within 1024x768: limiting ratio is width 1024/1920.
    scale = compute_scale(1920, 1080)
    assert abs(scale - (1024 / 1920)) < 1e-9


def test_compute_scale_never_upscales_small_screens():
    from lib.desktop_agent._scaling import compute_scale
    # A display already <= XGA is left at native size (no coordinate xlate).
    assert compute_scale(800, 600) == 1.0
    assert compute_scale(1024, 768) == 1.0


def test_compute_scale_degenerate_inputs():
    from lib.desktop_agent._scaling import compute_scale
    assert compute_scale(0, 0) == 1.0
    assert compute_scale(-5, 100) == 1.0


def test_scaled_dimensions_1920_to_xga():
    from lib.desktop_agent._scaling import scaled_dimensions
    w, h, scale = scaled_dimensions(1920, 1080)
    # Width pinned to 1024; height scaled by the same factor.
    assert w == 1024
    assert h == round(1080 * (1024 / 1920))  # 576
    assert h == 576


def test_api_to_real_remaps_model_click_back_to_real_pixels():
    """The headline grounding fix: a model that clicks (512, 288) on the
    1024x576 downscaled image of a 1920x1080 screen must land on the REAL
    pixel (960, 540) — the true screen centre. This is the assertion that
    goes RED if the remap is dropped or inverted."""
    from lib.desktop_agent._scaling import compute_scale, api_to_real
    scale = compute_scale(1920, 1080)          # 1024/1920 ≈ 0.5333
    real_x, real_y = api_to_real(512, 288, scale)
    assert (real_x, real_y) == (960, 540)


def test_real_to_api_is_inverse_of_api_to_real():
    from lib.desktop_agent._scaling import compute_scale, api_to_real, real_to_api
    scale = compute_scale(2560, 1440)
    # Round-trip a real coordinate through both maps; should return close.
    ax, ay = real_to_api(1280, 720, scale)
    rx, ry = api_to_real(ax, ay, scale)
    assert abs(rx - 1280) <= 1 and abs(ry - 720) <= 1


def test_remap_is_identity_when_screen_below_xga():
    from lib.desktop_agent._scaling import compute_scale, api_to_real
    scale = compute_scale(800, 600)  # 1.0 — model sees native image
    assert api_to_real(400, 300, scale) == (400, 300)


def test_api_to_real_guards_zero_scale():
    from lib.desktop_agent._scaling import api_to_real
    assert api_to_real(100, 50, 0) == (100, 50)
    assert api_to_real(100, 50, None) == (100, 50)


def test_target_size_env_override(monkeypatch):
    from lib.desktop_agent import _scaling
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_W', '1280')
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_H', '800')
    assert _scaling.target_size() == (1280, 800)


def test_target_size_bad_env_keeps_default(monkeypatch):
    from lib.desktop_agent import _scaling
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_W', 'not-an-int')
    monkeypatch.delenv('TOFU_DESKTOP_TARGET_H', raising=False)
    assert _scaling.target_size() == (1024, 768)


# ══════════════════════════════════════════════════════════
#  2. dispatch_command permission gates (write / exec / gui)
# ══════════════════════════════════════════════════════════

_NO_PERMS = {'allow_write': False, 'allow_exec': False, 'allow_gui': False}


def test_dispatch_unknown_command():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_does_not_exist', {}, _NO_PERMS)
    assert 'Unknown command' in res['error']


def test_dispatch_write_gate_blocks_without_allow_write():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_write_file', {'path': '/tmp/x', 'content': 'y'}, _NO_PERMS)
    assert '--allow-write' in res['error']


def test_dispatch_exec_gate_blocks_without_allow_exec():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_run_command', {'command': 'ls'}, _NO_PERMS)
    assert '--allow-exec' in res['error']


def test_dispatch_gui_gate_blocks_without_allow_gui():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_gui_action', {'action': 'click'}, _NO_PERMS)
    assert '--allow-gui' in res['error']


def test_dispatch_gate_lets_permitted_command_through():
    """With allow_exec set, the run gate is passed and the REAL handler runs —
    a trivial echo confirms the dispatcher forwarded to it, not the gate."""
    from lib.desktop_agent._dispatch import dispatch_command
    perms = {'allow_write': False, 'allow_exec': True, 'allow_gui': False}
    res = dispatch_command('desktop_run_command', {'command': 'echo tofu_gate_ok'}, perms)
    assert 'error' not in res or not res['error']
    assert 'tofu_gate_ok' in res.get('stdout', '')


def test_dispatch_screenshot_gate_needs_gui():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_screenshot', {}, _NO_PERMS)
    assert '--allow-gui' in res['error']


# ══════════════════════════════════════════════════════════
#  3. cmd_run_local — shell-metachar branch selection & safety
# ══════════════════════════════════════════════════════════

def test_shell_meta_regex_detects_pipe_and_redirect():
    from lib.desktop_agent._exec import _SHELL_META_RE
    assert _SHELL_META_RE.search('cat a | grep b')
    assert _SHELL_META_RE.search('echo x > f')
    assert _SHELL_META_RE.search('a && b')


def test_simple_command_uses_argv_not_shell():
    """A plain 'git status' (has a space → meta) vs a single token. A single
    executable token must be split with shlex, NOT routed through the shell."""
    from lib.desktop_agent import _exec
    captured = {}

    class _Res:
        stdout, stderr, returncode = 'ok', '', 0

    def _fake_run(args, **kwargs):
        captured['args'] = args
        captured['shell'] = kwargs.get('shell')
        return _Res()

    with mock.patch.object(_exec.subprocess, 'run', _fake_run):
        _exec.cmd_run_local({'command': 'whoami'})
    assert captured['args'] == ['whoami']
    assert captured['shell'] is False


def test_metachar_command_routed_through_explicit_shell():
    """A piped command must be handed to an explicit ['/bin/sh','-c',cmd]
    argv (never shell=True), so the command is one unambiguous argument."""
    from lib.desktop_agent import _exec
    captured = {}

    class _Res:
        stdout, stderr, returncode = 'ok', '', 0

    def _fake_run(args, **kwargs):
        captured['args'] = args
        captured['shell'] = kwargs.get('shell')
        return _Res()

    with mock.patch.object(_exec.subprocess, 'run', _fake_run):
        _exec.cmd_run_local({'command': 'cat f | wc -l'})
    assert captured['shell'] is False
    # Last element is the full command string, passed to the shell verbatim.
    assert captured['args'][-1] == 'cat f | wc -l'
    assert '-c' in captured['args']


def test_run_local_rejects_empty_command():
    from lib.desktop_agent._exec import cmd_run_local
    assert 'error' in cmd_run_local({'command': '   '})
    assert 'error' in cmd_run_local({'command': ''})


# ══════════════════════════════════════════════════════════
#  4. Missing-dependency guards — handlers degrade, never crash
# ══════════════════════════════════════════════════════════

def test_gui_handlers_return_clear_hint_when_pyautogui_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'pyautogui', None):
        r1 = _gui.cmd_screenshot_desktop({})
        r2 = _gui.cmd_gui_action({'action': 'click', 'x': 1, 'y': 1})
    for r in (r1, r2):
        assert 'not enabled' in r['error']
        assert 'pyautogui' in r['error']


def test_clipboard_handler_hint_when_pyperclip_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'pyperclip', None):
        r = _gui.cmd_clipboard({'action': 'read'})
    assert 'pyperclip' in r['error']


def test_system_info_handler_hint_when_psutil_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'psutil', None):
        r = _gui.cmd_system_info({'type': 'overview'})
    assert 'psutil' in r['error']
