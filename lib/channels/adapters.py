"""Channel adapters an overlay installs (entry-point group ``tofu.channel_adapters``).

Responsibility: resolve the implementation of a contract channel that the
public core does not ship. Today that is 大象 (``dx``), whose adapter, bridge,
SSO login and notification robot live in the YourProvider overlay
(``tofu_yourprovider.dx``, docs/PUBLIC_CORE.md). Core keeps the channel-generic
model, store, policy, ingress, outbox and tools and reaches the adapter only
through the port below; without the overlay the channel is unavailable and
its account commands answer ``channel_unavailable``.

Port: an entry point resolves to an object with these attributes.

- ``channel``: the contract channel id it implements.
- ``account_view(owner, *, store=None)``, ``update_account(owner, update, *,
  store=None, robot_probe=None)``, ``create_command_chat(owner, *, store=None,
  group_creator=None, robot_factory=None)``: the account commands of
  ``lib.channels.service``.
- ``command_chat_robot(account)``: the notification robot configured for the
  account's command chat, or None (``policy.robot_thread``).
- ``bind_owner_message(store, account, event)``: bind that robot's chat on the
  owner's own message there (ingress, C6); returns the updated account.
- ``supervisor_module`` (dotted name, imported on demand) and ``supervisor``
  (that module): the hosting lifecycle (``start_channel_bridges``,
  ``begin_stopping_channel_bridges``, ``stop_channel_bridges``,
  ``reconcile_channel_bridges(principal)``, ``running_adapter(owner)``,
  ``bridge_state(owner)``).
- ``login_preconditions(owner, view)``: the Router's precondition facts for an
  owner-settings change of this channel.
- ``robot``: the notification-robot module the out-of-band alert sender posts
  through (``serverctl_pkg.oob_alert``).

Entry points: ``channel_adapter``, ``require_channel_adapter``,
``installed_channel_adapters``, ``clear_channel_adapter_cache``.
Dependencies: ``tofu_extensions``, ``lib.channels.model``.
"""
from __future__ import annotations

import threading

from lib.channels.model import ChannelError
from lib.log import get_logger
from tofu_extensions import extension_entry_points

logger = get_logger(__name__)

CHANNEL_ADAPTER_GROUP = 'tofu.channel_adapters'

_cache: dict[str, object] | None = None
_lock = threading.Lock()


def _load() -> dict[str, object]:
    adapters: dict[str, object] = {}
    for entry in extension_entry_points(CHANNEL_ADAPTER_GROUP):
        try:
            adapter = entry.load()
            channel = adapter.channel
        except Exception as exc:
            logger.warning('[Channels] channel adapter %r failed to load: %s',
                           getattr(entry, 'name', '?'), exc, exc_info=True)
            continue
        adapters.setdefault(channel, adapter)
    return adapters


def installed_channel_adapters() -> dict[str, object]:
    """Channel id -> adapter for every installed overlay channel (loaded once per process)."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return dict(_cache)


def channel_adapter(channel: str):
    """The installed adapter of ``channel``, or None."""
    return installed_channel_adapters().get(channel)


def require_channel_adapter(channel: str):
    """The installed adapter of ``channel``; ``ChannelError('channel_unavailable')`` without one."""
    adapter = channel_adapter(channel)
    if adapter is None:
        raise ChannelError('channel_unavailable',
                           f'The {channel} channel is not installed on this server')
    return adapter


def clear_channel_adapter_cache() -> None:
    """Forget loaded adapters; for tests that install synthetic ones."""
    global _cache
    with _lock:
        _cache = None


__all__ = ['CHANNEL_ADAPTER_GROUP', 'channel_adapter', 'clear_channel_adapter_cache',
           'installed_channel_adapters', 'require_channel_adapter']
