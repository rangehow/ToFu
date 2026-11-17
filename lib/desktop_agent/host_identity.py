"""Desktop node — this host's identity key, for binding the node ID to one machine.

Problem: the node ID lives in the agent config (``~/.tofu/desktop_agent.json``
or ``TOFU_DESKTOP_CONFIG``). Hosts that share one home directory (containers on
a network home) read one file and would present one node ID.

Decision: the config keeps one ID per host, keyed by a digest of this host's
machine identity (``agent_ids`` in the config). On Linux the key also includes
the hostname, because containers built from one image share
``/etc/machine-id`` while their hostnames differ. On macOS and Windows the
platform machine identifier alone is the key, so renaming the computer keeps
the node ID. Only digests are stored.

Alternatives considered: a host-local file outside the home (``/etc``,
``/var/lib``) is not writable by an unprivileged node, and ``/tmp`` does not
survive a container restart; server-side conflict detection by boot nonce
needs a wire-contract change and still cannot tell which twin to keep.

Entry point: ``host_key()``.
"""

import hashlib
import socket
import subprocess
import sys

_MACHINE_ID_FILES = ('/etc/machine-id', '/var/lib/dbus/machine-id')


def _read_first(paths):
    for path in paths:
        try:
            with open(path, encoding='utf-8') as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ''


def _machine_id() -> str:
    """The platform's machine identifier, or '' when it cannot be read."""
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'SOFTWARE\Microsoft\Cryptography') as key:
                return str(winreg.QueryValueEx(key, 'MachineGuid')[0]).strip()
        except OSError:
            return ''
    if sys.platform == 'darwin':
        try:
            out = subprocess.run(['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                                 capture_output=True, text=True, timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return ''
        for line in out.splitlines():
            if 'IOPlatformUUID' in line:
                return line.split('=', 1)[-1].strip().strip('"')
        return ''
    return _read_first(_MACHINE_ID_FILES)


def _hostname_scoped() -> bool:
    return sys.platform.startswith('linux')


def host_key() -> str:
    """A stable digest naming this host; never empty."""
    machine = _machine_id()
    parts = [machine]
    if _hostname_scoped() or not machine:
        parts.append(socket.gethostname())
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()[:32]
