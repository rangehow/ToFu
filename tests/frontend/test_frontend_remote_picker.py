#!/usr/bin/env python3
"""tests/frontend/test_frontend_remote_picker.py — a conversation bound to a
remote-device pseudo-path keeps its workspace without a server write.

拍板 6A(docs/REMOTE_WORKTREE_DESIGN.md §5 P4):conv.projectPath =
``remote:<agent>:<root>`` 的会话**绝不**调 Api.project.setPaths(服务器 fs
上没有这个路径,调了就是 400/误清),项目栏渲染合成态(active,路径徽章)。
The remote-device picker that added such paths left the UI with computer
control (owner decision 2026-11-17); existing bindings still restore.

Run isolated: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/frontend/test_frontend_remote_picker.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, ROOT, _SHARED_HARNESS, node_deps_available, run_harness, skip_or_fail
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The STATE seam (_restoreConvProject / _isRemotePath /
# _applyRemoteProjectState) lives in project_state.js (core); project.js
# (deferred) is evaluated too, in bundle order.
_PROJECT_STATE_JS = os.path.join(JS_DIR, 'project_state.js')
_PROJECT_JS = os.path.join(JS_DIR, 'project.js')
_PROJECT_BROWSE_JS = native_module_path(
    '.native/project-browse-coordinator-remote-picker.js',
    os.path.join(
        PROJECT_ROOT, 'frontend', 'src', 'core',
        'project-browse-coordinator.ts',
    ),
)
_PROJECT_CONTROLLER_JS = native_module_path(
    '.native/conversation-project-remote-picker.js',
    os.path.join(
        PROJECT_ROOT, 'frontend', 'src', 'conversation', 'application',
        'conversation-project.ts',
    ),
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
#  行为(jsdom 真驱)
# ══════════════════════════════════════════════════════════════════════

_PICKER_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);

const calls = { setPaths: [] };

const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="projectBar"></div><div id="projectBadge"></div>' +
    '<div id="projectBarStats"></div><div id="projectBarFolders"></div>' +
    '</body>',
  targets: [process.argv[5], process.argv[2], process.argv[4], process.argv[6]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1' }],
    debugLog: () => {},
    reconcileConversationCatalogMetadata: () => {},
    persistConversationSettings: () => {},
    getActiveConv: () => null,
    _stopScanPoll: () => {},
    _applyProjectData: () => {},
    onProjectCleared: () => {},
    saveRecentProject: () => {},
    // project.js 的 projectState 经 setup globals 双挂(win+node global)——
    // eval'd 代码里的裸读写查的是 node global,挂 window 上是另一对象。
    projectState: { active: false, path: '', extraRoots: [] },
    Api: {
      project: {
        setPaths: async (paths, ro) => { calls.setPaths.push(paths);
          return { ok: true, json: async () => ({}) }; },
      },
    },
  },
});

// ── A. The current controller preserves the pseudo-path without setPaths.
const conv = { id: 'c1', projectPath: 'remote:aaaa1111:myapp',
               projectPaths: ['remote:aaaa1111:myapp'] };
let applied = null;
const controller = createConversationProjectController({
  activeId: () => 'c1', exists: id => id === 'c1',
  apply: data => { applied = data; },
});
controller.restore(conv);
check('remote_restore_never_calls_setPaths', calls.setPaths.length === 0);
check('remote_restore_conv_untouched',
      conv.projectPath === 'remote:aaaa1111:myapp');
check('remote_restore_bar_rendered', applied.active === true);
check('remote_restore_badge_title_keeps_pseudo',
      applied.path === 'remote:aaaa1111:myapp');
check('bar_badge_shows_agent_root', applied.path.includes('aaaa1111:myapp'));
check('bar_badge_not_raw_remote_prefix', applied.path.startsWith('remote:'));

report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_remote_picker_behaviour_jsdom():
    run_harness(
        target_js=_PROJECT_STATE_JS,
        body_js=_PICKER_BODY,
        extra_targets=[_PROJECT_JS, _PROJECT_BROWSE_JS, _PROJECT_CONTROLLER_JS],
        min_pass=6,
        label='remote picker',
    )


def test_NEUTER_strip_shortcircuit_calls_setPaths():
    """NEUTER:摘掉 _restoreConvProject 的伪路径短路 → setPaths 拿着
    'remote:…' 直奔服务器(400/误清的真实 bug 形态)= 短路承重。"""
    # 手写 node 子进程(绕过 run_harness 因为要断言 FAIL 输出),所以共享 dep
    # guard 也必须手写一遍 —— 无 jsdom 的 lane(公共 CI test-unit)要响亮地
    # SKIP,而不是拿空 stdout 断言失败。
    if not node_deps_available():
        skip_or_fail('node + jsdom dev-deps not installed (run `npm install`)')
    import subprocess
    import tempfile
    src = _read(_PROJECT_CONTROLLER_JS)
    anchor = 'ports.apply(projectConversationWorkspace(conv || draftWorkspace));'
    assert anchor in src, 'project application boundary drifted'
    neutered = src.replace(anchor, '', 1)
    body = _PICKER_BODY.replace(
        "check('remote_restore_never_calls_setPaths', calls.setPaths.length === 0);",
        "check('neuter_project_application_is_required', applied.active === true);")
    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_PROJECT_STATE_JS),
            delete=False, encoding='utf-8') as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8') as hf:
            harness = hf.name
            hf.write(body)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, npath, ROOT, _PROJECT_JS, _PROJECT_BROWSE_JS,
             _PROJECT_CONTROLLER_JS],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': _SHARED_HARNESS})
        out = (proc.stdout or '').strip()
        assert proc.returncode == 0, (
            f'NEUTER harness crashed:\n{proc.stderr}\n{out}')
        assert 'PASS neuter_project_application_is_required' in out, (
            f'NEUTER 未咬:摘掉短路后 setPaths 仍未被伪路径调用?\n{out}')
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
