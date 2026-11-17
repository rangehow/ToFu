"""Desktop Agent — local config persistence.

Tiny JSON store for agent-local settings that must survive restarts —
currently the stable node id per host (``agent_ids``; RWA P0 registration
frame, see docs/modules/remote_execution.md). Path: ``TOFU_DESKTOP_CONFIG`` env
var, else ``~/.tofu/desktop_agent.json``.
"""

import copy
import os

from lib.json_store import JsonStoreReadError, read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG = {
    # Legacy single node id: claimed by the first host that starts, kept for
    # older builds that read only this field.
    'agent_id': '',
    # {host_key: node id} (lib/desktop_agent/host_identity.py): hosts sharing
    # this file (one network home) keep distinct node ids. ``node_identity``.
    'agent_ids': {},
    # {host_key: {machine, hostname, container}} digests, to recognize a host
    # whose key changed (rename, recreated container).
    'host_facts': {},
    # {host_key: node id} the node replaced on this host; sent once when it
    # pairs so the Home shows a re-pair of a known device.
    'predecessors': {},
    # {host_key: [Attachment]}: joined servers of every host except the one
    # owning the legacy ``agent_id`` (which keeps ``attachments``), so twins
    # on one shared file never share a credential.
    'host_attachments': {},
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


def _resolve_identity(cfg, host, facts, phase0):
    """Resolve this host's node id inside one config transaction.

    Returns ``(node_id, changed)``. In order: the host's own entry; the entry
    of a build that keyed this host without its container ID (claimed by the
    first such host: twins on one host network both matched it); the legacy
    single ``agent_id`` (first host claims it); the same container under a new
    hostname (the same device: it keeps its node and attachments); otherwise
    a new node id that records the most similar known host as its predecessor.
    """
    import uuid
    ids = dict(cfg.get('agent_ids') or {})
    known = dict(cfg.get('host_facts') or {})
    slots = dict(cfg.get('host_attachments') or {})
    predecessors = dict(cfg.get('predecessors') or {})
    legacy = str(cfg.get('agent_id') or '').strip()
    changed = False

    def move(old):
        ids[host] = ids.pop(old)
        if old in slots:
            slots[host] = slots.pop(old)
        known.pop(old, None)

    if ids.get(host):
        node_id = ids[host]
    elif phase0 != host and ids.get(phase0) and phase0 not in known:
        move(phase0)
        node_id, changed = ids[host], True
    elif legacy and legacy not in ids.values():
        ids[host] = node_id = legacy
        changed = True
    else:
        renamed = [k for k, f in known.items() if facts['container'] and k in ids
                   and f.get('container') == facts['container']
                   and f.get('machine') == facts['machine']]
        if renamed:
            move(renamed[0])
            node_id = ids[host]
        else:
            similar = [k for k, f in known.items() if k in ids and facts['machine']
                       and f.get('machine') == facts['machine']]
            similar.sort(key=lambda k: known[k].get('hostname') != facts['hostname'])
            node_id = ids[host] = uuid.uuid4().hex
            if similar:
                predecessors[host] = ids[similar[0]]
        changed = True
    if known.get(host) != facts:
        known[host] = dict(facts)
        changed = True
    if not legacy:
        cfg['agent_id'] = node_id
        changed = True
    if changed:
        cfg.update(agent_ids=ids, host_facts=known, host_attachments=slots,
                   predecessors=predecessors)
    return node_id, changed


def _host():
    from lib.desktop_agent import host_identity
    return (host_identity.host_key(), host_identity.host_facts(),
            host_identity.phase0_host_key())


def node_identity():
    """``{'node_id', 'predecessor'}`` of this host, resolved and persisted atomically."""
    host, facts, phase0 = _host()
    outcome = {}

    def _mutate(cfg):
        node_id, changed = _resolve_identity(cfg, host, facts, phase0)
        outcome.update(node_id=node_id,
                       predecessor=(cfg.get('predecessors') or {}).get(host) or None)
        return cfg if changed else None

    _update_config(_mutate)
    return outcome


def clear_predecessor():
    """The Home recorded the re-pair; later pairings of this host name no predecessor."""
    host = _host()[0]

    def _mutate(cfg):
        predecessors = dict(cfg.get('predecessors') or {})
        if predecessors.pop(host, None) is None:
            return None
        cfg['predecessors'] = predecessors
        return cfg

    _update_config(_mutate)


def _host_rows(cfg, host):
    """This host's attachment list inside ``cfg`` (created when absent).

    The host owning the legacy ``agent_id`` keeps the top-level list that
    older builds read; every other host has its own list.
    """
    if (cfg.get('agent_ids') or {}).get(host) == cfg.get('agent_id'):
        if not isinstance(cfg.get('attachments'), list):
            cfg['attachments'] = []
        return cfg['attachments']
    slots = cfg.setdefault('host_attachments', {})
    if not isinstance(slots.get(host), list):
        slots[host] = []
    return slots[host]


def _attachment_transaction(change):
    """Run ``change(rows) -> bool`` on this host's attachments in one transaction."""
    from lib.desktop_wire.node_protocol import migrate_attachments
    host, facts, phase0 = _host()

    def _mutate(cfg):
        _node_id, changed = _resolve_identity(cfg, host, facts, phase0)
        legacy_owner = (cfg.get('agent_ids') or {}).get(host) == cfg.get('agent_id')
        if legacy_owner:
            changed = migrate_attachments(cfg) or changed
        rows = _host_rows(cfg, host)
        changed = change(rows) or changed
        return cfg if changed else None

    _update_config(_mutate)


def attachments():
    """This host's joined servers (resolving its identity and legacy migration once)."""
    out = []

    def change(rows):
        out.extend(dict(a) for a in rows if isinstance(a, dict))
        return False

    _attachment_transaction(change)
    return out


def save_attachment(record):
    """Insert or replace one attachment of this host by ``id`` (atomic, private file)."""
    record = dict(record)

    def change(rows):
        rows[:] = [a for a in rows if isinstance(a, dict) and a.get('id') != record['id']]
        rows.append(record)
        return True

    _attachment_transaction(change)
    return record


def update_attachment(attachment_id, **fields):
    """Change fields of one attachment; returns it, or None when absent."""
    found = {}

    def change(rows):
        for row in rows:
            if isinstance(row, dict) and row.get('id') == attachment_id:
                row.update(fields)
                found['row'] = dict(row)
                return True
        return False

    _attachment_transaction(change)
    return found.get('row')


def remove_attachment(attachment_id):
    """Forget one joined server of this host; returns whether it existed."""
    found = {}

    def change(rows):
        kept = [a for a in rows if isinstance(a, dict) and a.get('id') != attachment_id]
        if len(kept) == len(rows):
            return False
        rows[:] = kept
        found['removed'] = True
        return True

    _attachment_transaction(change)
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
