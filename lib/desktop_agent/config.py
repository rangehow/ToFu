"""Desktop Agent — local config persistence.

Tiny JSON store for agent-local settings that must survive restarts —
currently the stable ``agent_id`` (RWA P0 registration frame, see
docs/modules/remote_execution.md). Path: ``TOFU_DESKTOP_CONFIG`` env
var, else ``~/.tofu/desktop_agent.json``.
"""

import copy
import os

from lib.json_store import JsonStoreReadError, read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG = {
    'agent_id': '',  # generated on first start by _run._ensure_agent_id
    # RWA P1: [{name, path}] — the agent's declared project worktrees;
    # project_* commands are confined to these roots (constraint ⑤).
    'share_roots': [],
    # Joined servers (``Attachment`` in contracts/desktop_node_v1.schema.json):
    # one outbound bridge loop per entry. Empty = standalone; the packaged
    # app's own server is reached through its process capability, not here.
    'attachments': [],
    # Retired single attachment {url, secret}; migrate_attachments folds it
    # into ``attachments`` on first read and clears it.
    'remote_server': {},
    # Tray-persisted computer-control state: {enabled: bool, perms: {...}}.
    # ABSENT (empty dict) = the user never chose = fresh-install default
    # OFF — deny-by-default is preserved; only an explicit user choice is
    # ever restored.
    'computer_control': {},
}


def attachments():
    """The joined servers, migrating a legacy single attachment once."""
    from lib.desktop_wire.node_protocol import migrate_attachments
    cfg = load_config()
    probe = copy.deepcopy(cfg)
    if migrate_attachments(probe):
        def _mutate(current):
            return current if migrate_attachments(current) else None
        cfg = _update_config(_mutate) or load_config()
    return [dict(a) for a in cfg.get('attachments') or [] if isinstance(a, dict)]


def save_attachment(record):
    """Insert or replace one attachment by ``id`` (atomic, private file)."""
    from lib.desktop_wire.node_protocol import migrate_attachments
    record = dict(record)

    def _mutate(cfg):
        migrate_attachments(cfg)
        rows = [a for a in cfg.get('attachments') or []
                if isinstance(a, dict) and a.get('id') != record['id']]
        rows.append(record)
        cfg['attachments'] = rows
        return cfg

    _update_config(_mutate)
    return record


def update_attachment(attachment_id, **fields):
    """Change fields of one attachment; returns it, or None when absent."""
    found = {}

    def _mutate(cfg):
        for row in cfg.get('attachments') or []:
            if isinstance(row, dict) and row.get('id') == attachment_id:
                row.update(fields)
                found['row'] = dict(row)
                return cfg
        return None

    _update_config(_mutate)
    return found.get('row')


def remove_attachment(attachment_id):
    """Forget one joined server; returns whether it existed."""
    found = {}

    def _mutate(cfg):
        rows = [a for a in cfg.get('attachments') or [] if isinstance(a, dict)]
        kept = [a for a in rows if a.get('id') != attachment_id]
        if len(kept) == len(rows):
            return None
        found['removed'] = True
        cfg['attachments'] = kept
        return cfg

    _update_config(_mutate)
    return bool(found)


def load_computer_control():
    """Return ``(enabled, perms)`` for the tray's launch-time restore.

    ``(False, {})`` when the user never toggled anything (or the blob is
    malformed) — a fresh install must come up deny-by-default. ``perms``
    carries only the canonical tier keys that were actually persisted;
    the caller merges it over its own deny-all baseline so tiers added
    later still default OFF for old config files.
    """
    cfg = load_config()
    cc = cfg.get('computer_control')
    if not isinstance(cc, dict):
        return False, {}
    raw = cc.get('perms')
    perms = {}
    if isinstance(raw, dict):
        from lib.desktop_agent._permissions import PERMISSION_KEYS
        perms = {k: bool(raw[k]) for k in PERMISSION_KEYS if k in raw}
    return bool(cc.get('enabled')), perms


def save_computer_control(enabled, perms):
    """Persist the tray's computer-control state so it survives restarts.

    Lives in the same agent config file as ``agent_id`` / ``remote_server``.
    Only ever called from an explicit user action (the tray enable toggle
    or a permission-tier click) — never from startup/quit paths, so a
    crash or a normal Quit cannot erase the user's choice.
    """
    state = {
        'enabled': bool(enabled),
        'perms': {str(k): bool(v) for k, v in (perms or {}).items()},
    }

    def _mutate(cfg):
        cfg['computer_control'] = state
        return cfg

    _update_config(_mutate)
    return state


def config_path():
    return (os.environ.get('TOFU_DESKTOP_CONFIG')
            or os.path.expanduser('~/.tofu/desktop_agent.json'))


def merge_cli_roots(existing_roots, cli_specs):
    """Merge ``--root NAME=PATH`` specs into the persisted share_roots list.

    Pure logic (no I/O) so the CLI merge is unit-testable. The CLI wins on a
    name collision (path updated in place, list position kept); new names
    append in declaration order. ``~`` is expanded. Raises ValueError on a
    malformed spec (missing ``=`` / empty name / empty path).
    """
    roots = [dict(r) for r in (existing_roots or [])
             if isinstance(r, dict) and r.get('name')]
    by_name = {str(r['name']): r for r in roots}
    order = [str(r['name']) for r in roots]
    for spec in cli_specs or []:
        if not spec or '=' not in spec:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        name, path = spec.split('=', 1)
        name = name.strip()
        path = os.path.expanduser(path.strip())
        if not name or not path:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        if name in by_name:
            by_name[name]['path'] = path
        else:
            by_name[name] = {'name': name, 'path': path}
            order.append(name)
    return [by_name[n] for n in order]


def load_config():
    """Read the agent config, merged over defaults. Never raises."""
    cfg = dict(_DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.warning('[Agent] could not restrict config permissions: %s', e)
    data = read_json(path, default=None)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg):
    """Atomically replace the agent config with an explicit full snapshot."""
    candidate = copy.deepcopy(cfg)
    return _update_config(lambda _current: candidate)


def update_config(mutator):
    """Public locked field-update primitive for agent lifecycle callers."""
    return _update_config(mutator)


def _update_config(mutator):
    """Run one strict read-modify-write transaction with private permissions."""
    path = config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError as e:
            logger.warning('[Agent] could not restrict config directory: %s', e)

    def _mutate(current):
        if not isinstance(current, dict):
            raise JsonStoreReadError(
                f'desktop agent config is not a JSON object: {path}')
        cfg = copy.deepcopy(_DEFAULT_CONFIG)
        cfg.update(current)
        return mutator(cfg)

    saved = update_json_atomic(
        path, _mutate, default={}, strict=True, indent=2, mode=0o600)
    logger.debug('[Agent] config saved to %s', path)
    return saved
