"""Desktop node — this host's identity key, for binding the node ID to one machine.

Problem: the node ID lives in the agent config (``~/.tofu/desktop_agent.json``
or ``TOFU_DESKTOP_CONFIG``). Hosts that share one home directory (containers on
a network home) read one file and would present one node ID.

Decision: the config keeps one ID per host, keyed by a digest of this host's
identity (``agent_ids`` in the config). The key combines the platform machine
identifier; on Linux also the hostname, because containers built from one
image share ``/etc/machine-id`` while their hostnames usually differ; and the
container ID when the process runs in one, because containers that share an
image, a network home and the host's network namespace (hence its hostname)
differ only there. On macOS and Windows the machine identifier alone is the
key, so renaming the computer keeps the node ID. Only digests are stored.

The per-component digests (``host_facts``) are kept beside the key so a host
whose key changed can be recognized: the same container under a new hostname
keeps its node, and a recreated container or renamed Linux host names its
predecessor when it pairs again, so the Home shows a re-pair of a known device
instead of a silent duplicate (lib/desktop_agent/config.py ``node_identity``).

Alternatives considered: a host-local file outside the home (``/etc``,
``/var/lib``) is not writable by an unprivileged node, and ``/tmp`` does not
survive a container restart; namespace inode numbers change on every container
start. Twins that share every one of these facts are caught by the Home's
boot-nonce check (contracts/desktop_node_registry_v1.schema.json x-identity).

Entry points: ``host_key()``, ``host_facts()``, ``phase0_host_key()``.
"""

import hashlib
import re
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


_CONTAINER_ID = re.compile(r'(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])')


def _container_id() -> str:
    """This process's container ID (Docker, containerd, CRI), or '' outside one.

    The cgroup path names the container on cgroup v1 and on unnamespaced v2;
    a namespaced v2 cgroup reads ``0::/``, so the root overlay's private
    upper directory, which is unique per container, is used instead.
    """
    if not sys.platform.startswith('linux'):
        return ''
    try:
        with open('/proc/self/cgroup', encoding='utf-8') as handle:
            found = _CONTAINER_ID.findall(handle.read())
    except OSError:
        found = []
    if found:
        return found[-1]
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as handle:
            for line in handle:
                fields = line.split()
                if len(fields) > 4 and fields[4] == '/' and ' - overlay ' in line:
                    for option in line.rsplit(' ', 1)[-1].split(','):
                        if option.startswith('upperdir='):
                            return option[len('upperdir='):]
    except OSError:
        pass
    return ''


def _digest(*parts: str) -> str:
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()[:32]


def phase0_host_key() -> str:
    """The key of builds that did not yet include the container ID (migration)."""
    machine = _machine_id()
    parts = [machine]
    if _hostname_scoped() or not machine:
        parts.append(socket.gethostname())
    return _digest(*parts)


def host_facts() -> dict:
    """Digests of the components of this host's identity (never the raw values)."""
    machine = _machine_id()
    hostname = socket.gethostname() if (_hostname_scoped() or not machine) else ''
    container = _container_id()
    return {'machine': _digest('machine', machine) if machine else '',
            'hostname': _digest('hostname', hostname) if hostname else '',
            'container': _digest('container', container) if container else ''}


def host_key() -> str:
    """A stable digest naming this host; never empty.

    Outside a container it equals :func:`phase0_host_key`, so existing hosts
    keep their key.
    """
    container = _container_id()
    if not container:
        return phase0_host_key()
    return _digest(phase0_host_key(), 'container', container)
