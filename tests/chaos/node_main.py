"""Entry of a chaos node: the real node CLI on a simulated host identity.

``python -m tests.chaos.node_main <machine-id> <hostname> <container-id> [node args]``.
The identity a node derives from its host (lib/desktop_agent/host_identity.py)
comes from the first three arguments, so several "hosts" (twins on one home
directory, a recreated container) run on one test machine. Everything else is
``python -m lib.desktop_agent`` unchanged.
"""
import socket
import sys

from lib.desktop_agent import host_identity
from lib.desktop_agent._run import main


def simulate_host(machine: str, hostname: str, container: str) -> None:
    host_identity._machine_id = lambda: machine
    host_identity._container_id = lambda: container
    host_identity._hostname_scoped = lambda: True
    socket.gethostname = lambda: hostname


if __name__ == '__main__':
    simulate_host(*sys.argv[1:4])
    if sys.argv[4:5] == ['--host-key']:
        print(host_identity.host_key())
    else:
        main(sys.argv[4:])
