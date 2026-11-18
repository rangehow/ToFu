"""VM ingress isolation (coordinator e3c81e2a, 2026-11-13).

A task container reached its own VM's sandbox ingress (/native/*, which runs commands as root on the VM) at the bridge
gateway and the VM's address, port 8080, and by the same route a peer VM's ingress. That defeats the root-only reward
channel. vm_trial now drops container TCP to that port on the VM and its subnet before the container exists, and
refuses to run the trial if it cannot. The relay, the egress proxy and servers inside the container stay reachable.
"""
from __future__ import annotations

import subprocess

import pytest

from evaluations.terminal_bench import vm_trial

pytestmark = pytest.mark.unit

RULES = [["-I", "INPUT", "-s", "172.17.0.0/16", "-p", "tcp", "--dport", "8080", "-j", "DROP"],
         ["-I", "DOCKER-USER", "-s", "172.17.0.0/16", "-d", "172.30.0.0/16", "-p", "tcp", "--dport", "8080", "-j",
          "DROP"]]


class FakeVM:
    """The VM as vm_trial sees it: docker's default bridge, an eth0 default route, iptables. Records every command."""

    def __init__(self, iptables_rc=0):
        self.calls, self.iptables_rc = [], iptables_rc

    def run(self, argv, timeout=None, check=True, phase="docker"):
        self.calls.append(list(argv))
        out = b""
        if argv[:3] == ["docker", "network", "inspect"]:
            out = b"172.17.0.1 " if "{{.Gateway}}" in argv[-1] else b"172.17.0.0/16 "
        elif argv[:6] == ["ip", "-4", "-o", "route", "show", "default"]:
            out = b"default via 172.30.0.1 dev eth0 proto dhcp src 172.30.0.167 metric 100\n"
        elif argv[:4] == ["ip", "-4", "-o", "addr"]:
            out = b"2: eth0    inet 172.30.0.167/16 brd 172.30.255.255 scope global eth0\\       valid_lft forever\n"
        rc = self.iptables_rc if argv[0] == "iptables" else 0
        if check and rc:
            raise vm_trial.InfraError(phase, "%s rc=%s" % (" ".join(argv[:3]), rc))
        return subprocess.CompletedProcess(argv, rc, out, b"")


class FakeProxy:
    def __init__(self, bind_host, *args, **kwargs):
        self.address, self.counts = f"{bind_host}:40000", {"requests": 0, "denied": 0, "errors": 0}


def trial(tmp_path):
    return vm_trial.Trial({"out_dir": str(tmp_path / "out"), "task_dir": str(tmp_path / "task"), "trial_key": "k",
                           "trial_id": "codex-regex-log-1", "task_name": "regex-log", "arm": "codex",
                           "agent": {"kind": "codex"},
                           "egress": {"upstream": None, "deny_hosts": [], "deny_url_substrings": [],
                                      "allow_hosts": None, "no_proxy": "10.114.80.41"}})


def test_the_rules_drop_container_tcp_to_the_ingress_port_on_the_vm_and_on_its_subnet():
    assert vm_trial.INGRESS_PORT == 8080
    assert vm_trial.ingress_isolation_rules("172.17.0.0/16", "172.30.0.0/16") == RULES


def test_the_network_phase_isolates_the_ingress_before_any_container_or_proxy(tmp_path, monkeypatch):
    vm = FakeVM()
    monkeypatch.setattr(vm_trial, "run", vm.run)
    monkeypatch.setattr(vm_trial, "EgressProxy", FakeProxy)
    live = trial(tmp_path)
    live.start_network()
    assert [call for call in vm.calls if call[0] == "iptables"] == [["iptables", "-w", *rule] for rule in RULES]
    record = live.result["ingress_isolation"]
    assert (record["container_subnet"], record["vm_subnet"], record["port"]) == ("172.17.0.0/16", "172.30.0.0/16", 8080)
    assert live.result["phases"]["network"]["ok"] is True and live.proxy.address == "172.17.0.1:40000"


def test_a_trial_that_cannot_isolate_the_ingress_fails_closed_before_any_container(tmp_path, monkeypatch):
    vm = FakeVM(iptables_rc=4)
    monkeypatch.setattr(vm_trial, "run", vm.run)
    monkeypatch.setattr(vm_trial, "EgressProxy", FakeProxy)
    live = trial(tmp_path)
    with pytest.raises(vm_trial.InfraError) as raised:
        live.start_network()
    assert raised.value.phase == "network" and live.proxy is None
    assert not any(call[:2] == ["docker", "run"] for call in vm.calls)


def test_a_vm_without_a_default_route_fails_closed(tmp_path, monkeypatch):
    vm = FakeVM()
    real = vm.run

    def no_route(argv, **kwargs):
        if argv[:5] == ["ip", "-4", "-o", "route", "show"]:
            vm.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real(argv, **kwargs)

    monkeypatch.setattr(vm_trial, "run", no_route)
    monkeypatch.setattr(vm_trial, "EgressProxy", FakeProxy)
    with pytest.raises(vm_trial.InfraError) as raised:
        trial(tmp_path).start_network()
    assert raised.value.phase == "network"
    assert not any(call[0] == "iptables" for call in vm.calls)
