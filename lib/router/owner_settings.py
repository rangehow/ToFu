"""The Global Router reads and changes reversible owner settings (docs/ROUTER.md R11).

Owner decisions 2026-11-09: 「…就反正人能干的事情，总负责人都要能干」
(coordinator-1a390889aa954da2b18551d1b4a32625, Turn 3315ee59-e227-4025-8246-2aee9a9a543e) and
"…if you need to adjust something in the settings, you can also have that permission yourself."

There is no parallel settings logic here. A server setting is written by the
Settings page's own Save: the composition root (``lib.app.app_assembly``)
registers ``routes.config.SettingsBackend``, which runs the same
``apply_server_config`` / ``get_server_config`` the Settings HTTP calls run, as
this module's settings backend. A channel toggle is the channel account
service's ``update_account``, the Settings channel PUT. Every change goes
through the R9 gate (``owner_delegation.delegated_action``): the owner's grant,
an owner trigger, one audit record before the effect; revoking stops it.

Authority classes (R11 inventory):
- (a)/(e) reversible: ``WRITABLE_PATHS`` only, each sent in the shape the
  Settings page posts; a sibling the Save replaces (``model_defaults``) is
  carried from the current Settings read.
- (d) human-only: sections whose Save replaces a stored secret or credential,
  and secret-named keys, return ``needs_owner_action``; never read back or written.
- (b)/(c) destructive or visible to others: ``owner_settings_not_delegated``;
  they keep their own tools and need the owner's explicit instruction.
- Channels: preconditions first (account state, running session, and the SSO
  approval check the 大象 supervisor makes before every launch). Enabling never
  requests a 大象 login approval by itself, and a ``kicked`` account is
  reconnected only when the owner's own message asks (docs/CHANNELS.md C6).

Entry points: call (tool dispatch), register_settings_backend (composition
root), channel_preconditions (read-only probe).
Dependencies: lib.router.owner_delegation, lib.lifecycle_approval,
lib.channels account services and installed channel adapters (lib.channels.adapters).
"""
from __future__ import annotations

import re

from lib.lifecycle_approval import DelegationRefused

SCOPE = 'owner_settings'
CHANNELS = ('dx', 'github', 'wecom', 'qq', 'wechat')


def _model_default(key: str):
    def body(value, current: dict) -> dict:
        defaults = current.get('model_defaults')
        return {'model_defaults': {**(defaults if isinstance(defaults, dict) else {}), key: value}}
    return body


def _top_level(key: str):
    return lambda value, current: {key: value}


# path -> (authority class, path in the Settings read, Settings Save body for a value,
#          value shape). A shape is checked only where the Save does not validate the
#          value itself, because the Settings page builds it from a typed control:
#          'list' (strings), 'model' (non-empty text), 'text', or None (the Save's own
#          validation answers, verbatim).
WRITABLE_PATHS = {
    'model_defaults.default_model': ('e', 'model_defaults.default_model', _model_default('default_model'),
                                     'model'),
    'model_defaults.fallback_model': ('e', 'model_defaults.fallback_model', _model_default('fallback_model'),
                                      'text'),
    'hidden_models': ('a', 'hidden_models', _top_level('hidden_models'), 'list'),
    'hidden_ig_models': ('a', 'hidden_ig_models', _top_level('hidden_ig_models'), 'list'),
    # A search body without ``gateway`` keeps the configured gateway, and the
    # Optimizer's fetch skips stay its own (apply_server_config).
    'search.skip_domains': ('a', 'search.skip_domains',
                            lambda value, current: {'search': {'skip_domains': value}}, 'list'),
    'proxy_bypass_domains': ('a', 'network.proxy_bypass_domains', _top_level('proxy_bypass_domains'), None),
}


def _check_shape(path: str, value) -> None:
    shape = WRITABLE_PATHS[path][3]
    if shape == 'list' and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
        raise ValueError(f'{path} takes a list of strings, the shape the Settings page saves')
    if shape == 'model' and not (isinstance(value, str) and value.strip()):
        raise ValueError(f'{path} takes a model id, the shape the Settings page saves')
    if shape == 'text' and not isinstance(value, str):
        raise ValueError(f'{path} takes text (empty for none), the shape the Settings page saves')

# Sections whose Save replaces a stored secret or credential, and where the owner changes them.
_HUMAN_ONLY_SECTIONS = {
    'mt_provider': 'Settings → 翻译 (the machine translation provider and its API key)',
    'feishu': 'Settings → 消息平台 → 飞书 (its app ID and secret)',
    'proxy_config': 'Settings → 网络 (proxy URLs can carry credentials)',
    'proxy_pool': 'Settings → 网络 (proxy pool credentials)',
    'search.gateway': 'Settings → 搜索 (the search API gateway and its key)',
    'providers': 'Settings → 服务商 (provider keys)',
    'model_routing': 'Settings → 服务商 / 模型',
    'oauth': 'Settings → 订阅登录',
    'credentials': 'Settings → 高级 → 凭证保管库',
}
_SECRET_MARKERS = ('api_key', 'apikey', 'secret', 'password', 'token', 'credential', 'private_key',
                   'session', 'qr', 'oauth', 'sso', 'vault', 'login', 'cookie')
_EXTERNAL_MARKERS = ('send', 'publish', 'invite', 'approve', 'outbox', 'delete', 'restart', 'shutdown',
                     'drop', 'export', 'webhook', 'notify', 'create_group', 'mark_read', 'kick', 'reset')

_SETTINGS_BACKEND = None


def register_settings_backend(backend) -> None:
    """The composition root's Settings Save/read (``read() -> dict``, ``save(body) -> dict``)."""
    global _SETTINGS_BACKEND
    _SETTINGS_BACKEND = backend


def _backend():
    if _SETTINGS_BACKEND is None:
        raise DelegationRefused('owner_settings_unavailable',
                                'This process has no Settings backend; nothing was read or changed.')
    return _SETTINGS_BACKEND


def _read_server_config() -> dict:
    return _backend().read()


def _save_server_config(body: dict) -> dict:
    return _backend().save(body)


def _dig(document, path: str):
    node = document
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _same(readback, value) -> bool:
    if isinstance(readback, list) and isinstance(value, list):
        return sorted(set(map(str, readback))) == sorted(set(map(str, value)))
    return readback == value


def _answer(code: str, path: str, detail: str, **extra) -> dict:
    return {'code': code, 'path': path, 'execution_started': False, 'detail': detail, **extra}


def _needs_owner_action(path: str, place: str | None) -> dict:
    return _answer('needs_owner_action', path,
                   'Human-only: entering a secret, approving a login or scanning a QR code is the owner’s.',
                   remedy=f'Do it yourself in Tofu: {place or "Settings"}. The Global Router never reads or '
                          'types a secret, approves a login or scans a QR code.')


def _classify(path: str) -> dict | None:
    """None for a writable path; otherwise the typed answer. Nothing else is ever read or written."""
    if path in WRITABLE_PATHS:
        return None
    lowered = path.lower()
    parts = [part for part in re.split(r'[.\[\]]+', lowered) if part]
    if any(marker in part for part in parts for marker in _EXTERNAL_MARKERS):
        return _answer('owner_settings_not_delegated', path,
                       'Destructive or visible to others: never the Global Router’s automatic action. The '
                       'owner does it, or asks for it explicitly through its own tool (channel_send, '
                       'channel_outbox_approve, restart_server).')
    section = next((name for name in _HUMAN_ONLY_SECTIONS
                    if lowered == name or lowered.startswith(name + '.')), None)
    if section or any(marker in part for part in parts for marker in _SECRET_MARKERS):
        return _needs_owner_action(path, _HUMAN_ONLY_SECTIONS.get(section))
    return _answer('owner_settings_unsupported', path,
                   'Not a setting the Global Router changes yet (docs/ROUTER.md R11 plan).',
                   writable=sorted(WRITABLE_PATHS))


def _required(arguments: dict, name: str, action: str):
    value = arguments.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f'{name} is required for action={action}')
    return value


def _set(task: dict, scope: dict, arguments: dict) -> dict:
    from .owner_delegation import delegated_action
    path = _required(arguments, 'path', 'set')
    refusal = _classify(path)
    if refusal is not None:
        return refusal
    if 'value' not in arguments:
        raise ValueError('value is required for action=set')
    value = arguments['value']
    _check_shape(path, value)
    _, read_path, body, _ = WRITABLE_PATHS[path]
    payload = body(value, _read_server_config())

    def effect():
        answer = _save_server_config(payload)
        if answer.get('status') != 200 or answer.get('ok') is False:
            error = answer.get('error')
            message = error.get('message') if isinstance(error, dict) else error
            kind = error.get('kind') if isinstance(error, dict) else None
            raise DelegationRefused('owner_settings_rejected',
                                    f'The Settings Save refused this value: {message or answer}'
                                    + (f' ({kind})' if kind else ''))
        readback, _ = _dig(_read_server_config(), read_path)
        if not answer.get('changes') and not _same(readback, value):
            raise DelegationRefused('owner_settings_not_applied',
                                    f'The Settings Save accepted the request but left {path} at {readback!r}.')
        return {'value': readback, 'changes': answer.get('changes') or [],
                'needs_restart': bool(answer.get('needs_restart'))}

    result, action_id = delegated_action(task, scope, arguments, scope_name=SCOPE,
                                         target={'kind': 'owner_settings', 'path': path}, action=effect)
    if result.get('replayed'):
        return {**result, 'path': path, 'action_id': action_id}
    return {'path': path, **result, 'action_id': action_id, 'execution_started': True}


# ── channels: the Settings channel PUT, after its preconditions ──────────────

def _channel_service(channel: str):
    if channel == 'dx':
        from lib.channels import service
        return service.account_view, service.update_account
    if channel == 'github':
        from lib.channels.github import account
        return account.account_view, account.update_account
    from lib.channels.bots import account as bots
    return (lambda owner: bots.account_view(owner, channel),
            lambda owner, update: bots.update_account(owner, channel, update))


def _channel_view(owner: int, channel: str) -> dict:
    return _channel_service(channel)[0](owner)


def _update_channel(owner: int, channel: str, update: dict) -> dict:
    return _channel_service(channel)[1](owner, update)


def channel_preconditions(owner: int, channel: str) -> dict:
    """What the owner must know before a channel toggle. No side effects, no login request.

    A channel an overlay installs answers its own login facts
    (``login_preconditions`` of ``lib.channels.adapters``; for 大象 the question
    the supervisor asks before every launch, without requesting an approval).
    """
    view = _channel_view(owner, channel) or {}
    status = view.get('status') or {}
    result = {'channel': channel, 'enabled': bool(view.get('enabled')),
              'status': {'state': status.get('state'), 'detail': status.get('detail')},
              'running_session': None, 'sso_login_state': None, 'relaunch_would_push_login_card': False}
    from lib.channels.adapters import channel_adapter
    adapter = channel_adapter(channel)
    if adapter is not None:
        result.update(adapter.login_preconditions(owner, view))
    return result


def _channel(task: dict, scope: dict, arguments: dict) -> dict:
    from lib import lifecycle_approval as gate
    from .owner_delegation import delegated_action, router_requester
    owner = scope['user_id']
    channel = _required(arguments, 'channel', 'channel')
    if channel not in CHANNELS:
        raise ValueError(f'channel must be one of {", ".join(CHANNELS)}')
    gate.check_delegation(owner, router_requester(task, scope), SCOPE)
    preconditions = channel_preconditions(owner, channel)
    if 'enabled' not in arguments:
        return {'preconditions': preconditions, 'execution_started': False}
    enable = arguments['enabled']
    if not isinstance(enable, bool):
        raise ValueError('enabled must be a boolean')
    if enable:
        if preconditions['sso_login_state'] == 'no_account':
            raise DelegationRefused('needs_owner_action',
                                    f'{channel} has no 大象 account to log in with: enter your MIS in Settings → '
                                    '消息平台 → 大象. Nothing was enabled.')
        if preconditions['relaunch_would_push_login_card']:
            raise DelegationRefused('needs_owner_action',
                                    f'Enabling {channel} now would send a 大象 login approval to your phone: the '
                                    'shared SSO login needs you. Approve a 大象 login yourself (or reconnect in '
                                    'Settings → 消息平台 → 大象), then ask again. Nothing was enabled and no login '
                                    'was requested.')
        if (preconditions['status']['state'] == 'kicked'
                and not (arguments.get('source_turn_id') or arguments.get('source_steer_id'))):
            raise DelegationRefused('needs_owner_action',
                                    f'{channel} was displaced by another 大象 login and stays down until you '
                                    'reconnect it (docs/CHANNELS.md C6). Reconnecting takes the session back from '
                                    'that login, so it needs your own message asking for it, not a standing '
                                    'instruction.')
        if preconditions['enabled'] and preconditions['running_session']:
            return {'preconditions': preconditions, 'execution_started': False,
                    'detail': f'{channel} is already enabled with a live session (connector_owned); nothing '
                              'was changed. A reconnect is an off-then-on the owner asks for.'}
    elif not preconditions['enabled']:
        return {'preconditions': preconditions, 'execution_started': False,
                'detail': f'{channel} is already disabled; nothing was changed.'}

    def effect():
        return _update_channel(owner, channel, {'enabled': enable})

    target = {'kind': 'owner_settings', 'channel': channel, 'action': 'enable' if enable else 'disable'}
    result, action_id = delegated_action(task, scope, arguments, scope_name=SCOPE, target=target,
                                         action=effect)
    if result.get('replayed'):
        return {**result, 'preconditions': preconditions, 'action_id': action_id}
    return {'preconditions': preconditions, 'channel': result, 'action_id': action_id,
            'execution_started': True}


def call(task: dict, scope: dict, action: str, arguments: dict) -> dict:
    """The ``owner_settings`` tool: list | get | set | channel, under the owner's delegation."""
    from lib import lifecycle_approval as gate
    from .owner_delegation import router_requester
    if action == 'set':
        return _set(task, scope, arguments)
    if action == 'channel':
        return _channel(task, scope, arguments)
    # Reads need the grant too, but no owner trigger and no audit record: they change nothing.
    gate.check_delegation(scope['user_id'], router_requester(task, scope), SCOPE)
    if action == 'list':
        current = _read_server_config()
        return {'settings': {path: _dig(current, entry[1])[0] for path, entry in sorted(WRITABLE_PATHS.items())},
                'execution_started': False}
    if action == 'get':
        path = _required(arguments, 'path', 'get')
        refusal = _classify(path)
        if refusal is not None:
            return refusal
        value, found = _dig(_read_server_config(), WRITABLE_PATHS[path][1])
        return {'path': path, 'value': value, 'found': found, 'execution_started': False}
    raise ValueError(f'Unknown owner_settings action: {action!r}')


__all__ = ['CHANNELS', 'WRITABLE_PATHS', 'call', 'channel_preconditions', 'register_settings_backend']
