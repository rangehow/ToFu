"""A directly started server never serves without credentials beyond loopback.

A ``TOFU_RUN_SERVER=1`` start (a scratch or preview server, the packaged desktop app) whose auth
mode is ``open`` (no credential: every request is the local admin) refuses a bind address that is
not loopback, with ``UnauthenticatedNetworkBind``. The boot banner (``server_boot_report``) only
warns about that combination; for a scratch server nothing else stops it. Managed workers
(``TOFU_SERVER_WORKER``), bootstrap and container starts keep their configured bind: production
serves all interfaces in open mode behind its outer boundary. Decision record:
docs/modules/infra_runtime.md, "A scratch server never serves open auth beyond loopback".
"""
from __future__ import annotations

import ipaddress


class UnauthenticatedNetworkBind(RuntimeError):
    """An open-auth server was asked to bind a non-loopback address."""

    def __init__(self, host: str, auth_mode: str):
        self.host, self.auth_mode = host, auth_mode
        super().__init__(
            f'refusing to serve auth mode {auth_mode!r} (no credential) on {host!r}, which is not a '
            'loopback address: bind 127.0.0.1 (--host 127.0.0.1 or BIND_HOST=127.0.0.1), or require '
            'credentials (TOFU_AUTH_MODE=private).')


def loopback_host(host: str) -> bool:
    """Whether a bind host names only this machine (``0.0.0.0`` and ``::`` name every interface)."""
    if host.lower() == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host.strip('[]').split('%', 1)[0])
    except ValueError:
        return False  # a host name other than localhost may resolve to any interface
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def refuse_unauthenticated_network_bind(host: str, auth_mode: str) -> None:
    if auth_mode == 'open' and not loopback_host(host):
        raise UnauthenticatedNetworkBind(host, auth_mode)
