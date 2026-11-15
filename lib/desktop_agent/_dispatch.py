"""
Desktop Agent — command dispatch tables & dispatcher.

Build order matters: the module-level dispatch tables (``COMMANDS``,
``WRITE_COMMANDS``, ``EXEC_COMMANDS``, ``GUI_COMMANDS``) reference the
``cmd_*`` handler functions, so those handlers are imported *first* from
the ``_files`` / ``_exec`` / ``_gui`` sub-modules before the tables below
are constructed.
"""

import traceback

from lib.desktop_agent._adapter import (
    cmd_adapter_ensure,
    cmd_adapter_status,
    cmd_adapter_stop,
)
from lib.desktop_agent._egress import cmd_egress_http
from lib.desktop_agent._codex_login import cmd_codex_login_read
from lib.desktop_agent._exec import cmd_run_local
from lib.desktop_agent._files import (
    cmd_list_files,
    cmd_move_file,
    cmd_open_app,
    cmd_open_file,
    cmd_read_file,
    cmd_write_file,
)
from lib.desktop_agent._gui import (
    cmd_clipboard,
    cmd_gui_action,
    cmd_screenshot_desktop,
    cmd_system_info,
)
from lib.desktop_agent._project import (
    cmd_project_apply_diff,
    cmd_project_edit_file,
    cmd_project_find_files,
    cmd_project_grep_search,
    cmd_project_list_dir,
    cmd_project_read_files,
    cmd_project_run_command,
    cmd_project_write_file,
)
from lib.desktop_wire.node_protocol import (
    node_contract,
    required_tier,
    tiers_from_permissions,
)
from lib.log import get_logger

logger = get_logger(__name__)


def cmd_paper_resolve(params):
    from ._paper import resolve_document
    return resolve_document(params)


# ══════════════════════════════════════════════════════════
#  Command Dispatcher
# ══════════════════════════════════════════════════════════

COMMANDS = {
    'codex_login_read': cmd_codex_login_read,

    'paper_resolve': cmd_paper_resolve,
    # File system
    'desktop_list_files':    cmd_list_files,
    'desktop_read_file':     cmd_read_file,
    'desktop_write_file':    cmd_write_file,
    'desktop_move_file':     cmd_move_file,

    # Applications
    'desktop_open_file':     cmd_open_file,
    'desktop_open_app':      cmd_open_app,
    'desktop_run_command':   cmd_run_local,

    # GUI automation
    'desktop_screenshot':    cmd_screenshot_desktop,
    'desktop_gui_action':    cmd_gui_action,
    'desktop_clipboard':     cmd_clipboard,

    # System
    'desktop_system_info':   cmd_system_info,

    # Egress (subscription traffic via the user's own network — S2)
    'egress_http':           cmd_egress_http,

    # Subscription adapter sidecar (E4 — CLIProxyAPI supervisor)
    'adapter_ensure':        cmd_adapter_ensure,
    'adapter_status':        cmd_adapter_status,
    'adapter_stop':          cmd_adapter_stop,

    # Project (RWA P1 — share-root worktree commands; wire type = full name)
    'project_list_dir':      cmd_project_list_dir,
    'project_read_files':    cmd_project_read_files,
    'project_write_file':    cmd_project_write_file,
    'project_apply_diff':    cmd_project_apply_diff,
    'project_edit_file':     cmd_project_edit_file,
    'project_grep_search':   cmd_project_grep_search,
    'project_find_files':    cmd_project_find_files,
    'project_run_command':   cmd_project_run_command,
}

# Permission levels derive from the node protocol's command-tier map
# (contracts/desktop_node_v1.schema.json), the same table the server checks
# before enqueue. The clipboard is GUI tier like reading the screen, and
# desktop_system_info type=kill is exec tier (x-command-param-tiers).
def _commands_of(tier):
    return frozenset(cmd for cmd, owner in node_contract()['x-command-tiers'].items()
                     if owner == tier and cmd in COMMANDS)


WRITE_COMMANDS = _commands_of('write')
EXEC_COMMANDS = _commands_of('exec')
GUI_COMMANDS = _commands_of('gui')
EGRESS_COMMANDS = _commands_of('egress')


def permission_refusal(cmd_type, params, permissions):
    """Node-side tier check; the device never trusts the server's admission."""
    tier = required_tier(cmd_type, params)
    if tier is None:
        return f'Unknown command: {cmd_type}'
    if tier in ('read', 'control'):
        return None
    if tiers_from_permissions(permissions).get(tier):
        return None
    flag = '--allow-' + tier.replace('_', '-')
    if cmd_type == 'desktop_system_info':
        return f'desktop_system_info type={params.get("type")} requires {flag} flag'
    return f'Command {cmd_type} requires {flag} flag'


def dispatch_command(cmd_type, params, permissions):
    """Execute a command if permitted."""
    if cmd_type not in COMMANDS:
        return {'error': f'Unknown command: {cmd_type}'}
    refusal = permission_refusal(cmd_type, params if isinstance(params, dict) else {},
                                 permissions)
    if refusal:
        return {'error': refusal}

    try:
        return COMMANDS[cmd_type](params)
    except Exception as e:
        logger.error('dispatch_command %s failed', cmd_type, exc_info=True)
        return {'error': f'{type(e).__name__}: {e}', 'traceback': traceback.format_exc()[-500:]}
