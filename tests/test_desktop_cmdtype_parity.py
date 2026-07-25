"""tests/test_desktop_cmdtype_parity.py — desktop 工具 wire 契约守卫.

潜伏 bug(board ``pt_08a6d1afe79c4dfd``):
``lib/tasks_pkg/handlers/misc/_agents.py:_run_desktop`` 入队前把工具名的
``desktop_`` 前缀剥掉(``desktop_list_files`` → ``list_files``),而 agent 侧
``lib/desktop_agent/_dispatch.COMMANDS`` 全部以**完整**工具名为键 —— agent
收到 ``list_files`` 只会回 ``Unknown command: list_files``。

后果:Studio 里经 task loop 派发的 desktop_* 工具**全灭**(agent 在线时
schema 正常出现,调用即报 Unknown command)。``routes/desktop.py`` 里保留
完整前缀的 ``execute_desktop_tool`` 是正确的,但它全仓零调用方,是死代码。

本文件是契约守卫,不是修复:
  * 测试一(必绿):agent 命令表覆盖全部 LLM schema 名 —— 两半各自的一致性。
  * 测试二(修复前 xfail):task-loop 入队的 wire type 必须命中 agent 命令表。
    修复落地后本测试 XPASS,strict 模式转红,提醒修复者摘掉 xfail 标记
    并顺带处理死代码 ``execute_desktop_tool``。
"""

import pytest

from lib.desktop_agent._dispatch import COMMANDS as AGENT_COMMANDS
from lib.desktop_tools import DESKTOP_TOOL_NAMES

_TICKET = 'pt_08a6d1afe79c4dfd'


def _capture_enqueued_cmd_type(monkeypatch, fn_name):
    """经 task-loop 路径(_run_desktop)入队一条命令,回传 wire 上的 cmd_type."""
    import lib.desktop
    from lib.tasks_pkg.handlers.misc import _agents

    captured = {}

    def fake_send_desktop_command(cmd_type, params=None, timeout=30):
        captured['cmd_type'] = cmd_type
        captured['params'] = params
        return {'ok': True}, None

    # _run_desktop 在函数体内才 from lib.desktop import send_desktop_command,
    # 因此 patch lib.desktop 模块属性即可拦截。
    monkeypatch.setattr(lib.desktop, 'send_desktop_command',
                        fake_send_desktop_command)
    _agents._run_desktop(fn_name, {})
    return captured


@pytest.mark.unit
def test_agent_command_table_covers_all_schema_names():
    """每一个暴露给 LLM 的 desktop_* schema,agent 都有对应命令处理器."""
    missing = sorted(n for n in DESKTOP_TOOL_NAMES if n not in AGENT_COMMANDS)
    assert not missing, (
        f'agent COMMANDS 缺少 schema 暴露的工具: {missing} '
        '(desktop_move_file 刻意不暴露,见 lib/desktop_tools.py 注释)')


@pytest.mark.unit
@pytest.mark.xfail(
    strict=True,
    reason=f'latent bug {_TICKET}: _run_desktop strips the desktop_ prefix; '
           'agent COMMANDS keys keep it — wire type never matches',
)
def test_enqueued_wire_type_matches_agent_command_table(monkeypatch):
    """task-loop 入队的 cmd_type 必须是 agent 命令表的合法键(完整工具名)."""
    for fn_name in sorted(DESKTOP_TOOL_NAMES):
        captured = _capture_enqueued_cmd_type(monkeypatch, fn_name)
        enqueued = captured.get('cmd_type')
        assert enqueued in AGENT_COMMANDS, (
            f'wire 错配: {fn_name} 被入队为 {enqueued!r} —— '
            f'agent 只会回 Unknown command(完整名才命中 COMMANDS)')
