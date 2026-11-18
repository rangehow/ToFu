#!/usr/bin/env python3
"""Run one Terminal-Bench 2.x trial inside a sandbox VM without Harbor.

Responsibility: from one JSON spec built by ``evaluations.terminal_bench.harness``,
pull the task image, start a fresh container with the Harbor-compatible layout
(``/logs/agent`` and ``/logs/verifier`` bind mounts, ``TEST_DIR=/tests``, tests
uploaded only after the agent finished), install a prebuilt agent runtime (plus,
for a third-party harness, its ``setup`` program: a per-trial install that runs
before and outside the agent budget, where a failure is an install error), run
the agent under a hard deadline, run ``tests/test.sh`` and write ``result.json``
plus all evidence under the output directory, then pack it as ``<out>.tar.gz``.
A schema 1.3 separate-verifier task (``spec["verifier"]``, DeepSWE) instead runs
its collect commands in the agent's container, copies the artifacts out, removes
that container, and runs ``tests/test.sh`` in a fresh no-network container of the
image built from ``tests/Dockerfile``, with the artifacts mounted read-only.

The reward channel is root's alone: ``/logs/verifier`` is a root:root 0700
directory, emptied again right before the verifier runs, and every verifier
command runs as uid 0, so a process the agent or the tests drop to another uid
(e.g. 65534) can neither create, replace, remove nor read ``reward.txt``.
``/logs/agent`` belongs to the image's user, who runs the agent, at 0755.
Rewards are read only from regular files, never through a link.

Every agent and verifier command runs as ``bash -lc`` (a login shell, like
Harbor's ``setsid bash -lc``; ``setsid`` is omitted because without ``-w`` it
forks and returns at once when the caller leads its process group, and the
deadline kill below finds the tree by marker instead of by session). Container HTTP(S) egress is pointed at
``EgressProxy`` (proxy variables): it refuses benchmark-answer hosts and logs every request for the contamination
audit. The sandbox ingress (root on the VM) is unreachable from the container: ``isolate_ingress``.

Entry point: ``python3 vm_trial.py SPEC_JSON``. Dependencies: Python >= 3.7
standard library and the VM's docker CLI. The file is uploaded verbatim through
the ingress native exec channel and must not import the repository.
"""
import hashlib
import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

RESULT_SCHEMA = "tofu.tb-native.trial/v1"
# Liveness budgets for transient registry/relay failures (not tuning knobs).
PULL_ATTEMPTS = 3
DOWNLOAD_ATTEMPTS = 3
# Seconds between SIGTERM and SIGKILL when a deadline stops a process tree.
KILL_GRACE_SEC = 30
# In-container launcher for deadline-bound programs: ``$0`` is the marker and
# ``$1`` the program. This shell stays the program's parent even when bash
# elides the fork of a simple command or the program ``exec``s, both of which
# would otherwise erase the marker from the process table.
MARKED_SHELL = 'bash -lc "$1"\nexit $?'
# Seconds between checks of an agent's completion marker while it runs.
COMPLETION_POLL_SEC = 5
# The relay's held-seconds total (PREREG-2 Amendment 7): seconds between re-reads while one of the trial's
# requests is held at its deadline (the deadline cannot fall inside a hold; this bounds how late its end is
# seen), and the liveness budget of one read.
RELAY_HELD_POLL_SEC = 1
RELAY_HELD_TRIES = 3
RELAY_HELD_RETRY_SEC = 2
RELAY_HELD_TIMEOUT_SEC = 10
# A proxied connection without traffic in either direction for this long is dead.
PROXY_IDLE_CLOSE_SEC = 900
PROXY_HEAD_LIMIT = 65536
# Where schema 1.3 artifacts live in both containers (harness.ARTIFACTS_DIR; this file
# imports nothing from the repository).
ARTIFACTS_DIR = "/logs/artifacts"
# deep-swe tests/test.sh: its EXIT trap writes this to reward.txt when the grader
# crashed before writing reward.json -- an infrastructure error, never a score.
CRASH_SENTINEL_REWARD = -1.0


class InfraError(Exception):
    def __init__(self, phase, message):
        super().__init__(message)
        self.phase = phase


def log(message):
    sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


def tail(data, limit=1500):
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data[-limit:]


def run(argv, timeout=None, check=True, phase="docker"):
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise InfraError(phase, "%s timed out after %ss" % (" ".join(argv[:3]), timeout))
    if check and proc.returncode != 0:
        raise InfraError(phase, "%s rc=%s: %s" % (" ".join(argv[:3]), proc.returncode, tail(proc.stderr)))
    return proc


def docker(*args, **kwargs):
    return run(["docker"] + list(args), **kwargs)


# ---------------------------------------------------------------- VM ingress isolation

# The sandbox VM's ingress (its /native/* channel runs commands as root on the VM) listens on every VM address at
# INGRESS_PORT, and each peer VM's ingress on the same port of the VM subnet. A task container reached its own VM's
# ingress (probe 2026-11-13 20:43), which defeats the root-only reward channel, so no task container may open a TCP
# connection to INGRESS_PORT on the VM or its subnet (coordinator e3c81e2a). The relay, the egress proxy and servers
# inside the container stay reachable.
INGRESS_PORT = 8080


def ingress_isolation_rules(container_subnet, vm_subnet, port=INGRESS_PORT):
    """iptables arguments that drop task-container TCP to ``port`` on the VM itself (INPUT: the bridge gateway and the
    VM's own addresses) and on the VM's subnet (DOCKER-USER: the FORWARD path to peer VMs)."""
    return [["-I", "INPUT", "-s", container_subnet, "-p", "tcp", "--dport", str(port), "-j", "DROP"],
            ["-I", "DOCKER-USER", "-s", container_subnet, "-d", vm_subnet, "-p", "tcp", "--dport", str(port),
             "-j", "DROP"]]


def vm_subnet():
    """The subnet of the VM's default-route interface, where its peers' ingresses are."""
    route = run(["ip", "-4", "-o", "route", "show", "default"], phase="network").stdout.decode().split()
    device = route[route.index("dev") + 1]
    address = run(["ip", "-4", "-o", "addr", "show", "dev", device], phase="network").stdout.decode().split()
    return str(ipaddress.ip_interface(address[address.index("inet") + 1]).network)


def isolate_ingress(network):
    """Apply ingress_isolation_rules for the docker ``network`` the task container joins; returns what was applied.
    Any failure raises: a trial never runs an agent that could reach the ingress."""
    subnets = docker("network", "inspect", network, "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}",
                     phase="network").stdout.decode().split()
    if not subnets:
        raise InfraError("network", "docker network %s has no IPv4 subnet to isolate" % network)
    record = {"port": INGRESS_PORT, "container_subnet": subnets[0], "vm_subnet": vm_subnet(), "rules": []}
    for rule in ingress_isolation_rules(record["container_subnet"], record["vm_subnet"]):
        run(["iptables", "-w"] + rule, phase="network")
        record["rules"].append(" ".join(rule))
    return record


# ---------------------------------------------------------------- process trees

def _proc_table():
    """Live processes as ``{pid: (ppid, argv, start_ticks)}``; zombies are skipped."""
    table = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "rb") as handle:
                stat = handle.read().decode("utf-8", "replace")
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                cmdline = handle.read().split(b"\0")
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            continue
        table[int(entry)] = (int(fields[1]), [part.decode("utf-8", "replace") for part in cmdline if part], fields[19])
    return table


def kill_marked_tree(marker):
    """SIGTERM then SIGKILL every process whose argv contains marker, plus descendants.

    Returns ``(signalled, force_killed)``: processes sent SIGTERM, and those
    still alive after ``KILL_GRACE_SEC`` that were sent SIGKILL. Victims are
    tracked by pid and start time, so one that outlives its marked ancestors
    (and is reparented away from them) still receives SIGKILL.

    Container processes share the VM kernel, so a marker passed as ``$0`` of
    the in-container shell identifies the exec root without trusting tools
    inside the image.
    """
    def collect(known=()):
        table = _proc_table()
        children = {}
        for pid, (ppid, _, _) in table.items():
            children.setdefault(ppid, []).append(pid)
        stack = [pid for pid, (_, argv, _) in table.items() if marker in argv]
        stack += [pid for pid, start in known if pid in table and table[pid][2] == start]
        found = set()
        while stack:
            pid = stack.pop()
            if pid not in found:
                found.add(pid)
                stack.extend(children.get(pid, []))
        return {(pid, table[pid][2]) for pid in found}

    def send(processes, sig):
        for pid, _ in processes:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    victims = collect()
    send(victims, signal.SIGTERM)
    deadline = time.time() + KILL_GRACE_SEC
    survivors = collect(victims)
    while survivors and time.time() < deadline:
        time.sleep(1)
        survivors = collect(victims)
    send(survivors, signal.SIGKILL)
    return len(victims), len(survivors)


class CompletionMarker:
    """Watches a file the agent appends to for a line starting with ``prefix``, reading only new bytes."""

    def __init__(self, path, prefix):
        self.path, self.prefix = Path(path), prefix.encode()
        self.offset, self.partial = 0, b""

    def seen(self):
        try:
            with open(str(self.path), "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return False
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        return any(line.startswith(self.prefix) for line in lines)


def relay_headers(spec, url):
    """The run's relay key (the uploaded spec's ``relay_ingress``) for a URL on the relay; none elsewhere."""
    ingress = spec.get("relay_ingress")
    if ingress and url.startswith(ingress["origin"] + "/"):
        return {"Authorization": "Bearer " + ingress["key"]}
    return {}


class RelayHold:
    """Seconds the relay held this attempt's model requests in its gates (PREREG-2 Amendment 7).

    The relay keeps one running total per arm and task, holds in progress included; the total read before the
    agent starts belongs to earlier attempts and is subtracted. Calling returns ``(seconds, holding)``. The
    extension never shrinks, and a read that still fails after ``RELAY_HELD_TRIES`` keeps the last value.
    """

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = dict(headers or {})
        self.reads = self.errors = 0
        self.granted, self.holding = 0.0, False
        self.base = self.read()[0]

    def read(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(RELAY_HELD_TRIES):
            self.reads += 1
            try:
                request = urllib.request.Request(self.url, headers=self.headers)
                with opener.open(request, timeout=RELAY_HELD_TIMEOUT_SEC) as reply:
                    body = json.loads(reply.read().decode("utf-8"))
                return float(body["held_s"]), bool(body["holding"])
            except (OSError, ValueError, KeyError) as error:
                self.errors += 1
                log("relay held seconds unreadable (%s: %s)" % (type(error).__name__, error))
                if attempt + 1 == RELAY_HELD_TRIES:
                    raise
                time.sleep(RELAY_HELD_RETRY_SEC)

    def __call__(self):
        try:
            held, self.holding = self.read()
        except (OSError, ValueError, KeyError):
            return self.granted, self.holding
        self.granted = max(self.granted, round(held - self.base, 3))
        return self.granted, self.holding


def exec_with_deadline(container, program, env, marker, timeout_sec, stdout_path, stderr_path, workdir=None,
                       completion=None, user=None, extension=None):
    """Run ``bash -lc program`` under ``MARKED_SHELL`` in the container; stop the tree at the deadline.

    With ``completion`` (a ``CompletionMarker``) the run counts as finished once
    the agent has written its final record, even if its process lingers on
    children it started: nothing is killed, so daemons stay up for the verifier.
    ``user`` is passed to ``docker exec -u``; ``None`` runs as the image's user.
    ``extension`` (a ``RelayHold``) moves the deadline to ``timeout_sec`` plus the seconds it reports, re-read
    each time the deadline is reached; the tree is stopped once a read no longer moves it past now.
    """
    argv = ["docker", "exec"]
    if user is not None:
        argv += ["-u", user]
    if workdir:
        argv += ["-w", workdir]
    for key, value in sorted(env.items()):
        argv += ["-e", "%s=%s" % (key, value)]
    argv += [container, "bash", "-c", MARKED_SHELL, marker, program]
    started = time.time()
    granted, floor = 0.0, 0.0
    extended = {} if extension is None else {"extension_s": 0.0}
    with open(stdout_path, "ab") as out, open(stderr_path, "ab") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        timed_out = False
        try:
            while True:
                left = max(timeout_sec + granted - (time.time() - started), floor, 0)
                try:
                    code = proc.wait(timeout=left if completion is None else min(COMPLETION_POLL_SEC, left))
                    break
                except subprocess.TimeoutExpired:
                    if completion is not None and completion.seen():
                        return dict({"exit_code": None, "timed_out": False, "completed_by_marker": True,
                                     "duration_s": round(time.time() - started, 1)}, **extended)
                    if time.time() - started < timeout_sec + granted:
                        continue
                    if extension is not None:
                        granted, holding = extension()
                        extended["extension_s"] = granted
                        floor = RELAY_HELD_POLL_SEC if holding else 0.0
                        if time.time() - started < timeout_sec + granted:
                            continue
                    raise
        except subprocess.TimeoutExpired:
            timed_out = True
            extended["stopped_at_s"] = round(time.time() - started, 3)
            signalled, force_killed = kill_marked_tree(marker)
            log("deadline %ss (+%ss held) reached for %s; sent SIGTERM to %d processes, SIGKILL to %d"
                % (timeout_sec, granted, marker, signalled, force_killed))
            try:
                code = proc.wait(timeout=KILL_GRACE_SEC * 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
    return dict({"exit_code": code, "timed_out": timed_out, "duration_s": round(time.time() - started, 1)},
                **extended)


# ---------------------------------------------------------------- log directories

def seal_verifier_logs(path):
    """Make ``path`` (bind-mounted at /logs/verifier) an empty root:root 0700 directory.

    The mode changes first, so no other uid can add an entry while the rest is
    removed. Entries are removed without following links: whatever the agent
    phase left, such as a planted ``reward.txt``, is never read as a reward.
    """
    path.mkdir(parents=True, exist_ok=True)
    os.chown(str(path), 0, 0)
    path.chmod(0o700)
    for entry in os.scandir(str(path)):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def read_regular_file(path):
    """Text of ``path`` when it is a regular file, else ``None``; never follows a link or blocks on a FIFO."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as handle:  # fdopen owns the descriptor from here
        try:
            return handle.read().decode("utf-8")
        except OSError:
            return None


def read_regular_bytes(path):
    """Bytes of ``path`` when it is a regular file, else ``None``; never follows a link or blocks on a FIFO."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def collected_artifact(path, copied):
    """Evidence for one artifact ``docker cp`` brought out: only a regular file counts.

    A link (``docker cp`` keeps links) or directory the agent planted is removed
    unread, so nothing outside the trial is ever hashed or mounted; a failed copy
    leaves no partial file. Either way the grader then sees no artifact.
    """
    try:
        info = os.lstat(str(path))
    except OSError:
        info = None
    regular = info is not None and stat.S_ISREG(info.st_mode)
    record = {"copied": copied.returncode == 0 and regular, "bytes": None, "sha256": None}
    if copied.returncode != 0:
        record["error"] = tail(copied.stderr, 300)
    if info is not None and not record["copied"]:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(str(path))
        else:
            os.unlink(str(path))
        record["removed"] = "regular file" if regular else "not a regular file"
    elif record["copied"]:
        data = Path(path).read_bytes()
        record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return record


# ---------------------------------------------------------------- egress proxy

def _read_head(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(8192)
        if not chunk:
            break
        data += chunk
        if len(data) > PROXY_HEAD_LIMIT:
            raise ValueError("request head too large")
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("incomplete request head")
    return head + b"\r\n\r\n", rest


def _split_host_port(target, default_port):
    if target.startswith("["):
        host, _, port = target[1:].partition("]")
        port = port.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    return host.lower(), int(port) if port else default_port


def _close_connection_header(head):
    lines = head.split(b"\r\n")
    kept = [line for line in lines[1:] if line and not line.lower().startswith(
        (b"connection:", b"proxy-connection:", b"keep-alive:"))]
    return b"\r\n".join([lines[0]] + kept + [b"Connection: close", b"", b""])


class EgressProxy:
    """HTTP(S) forward proxy for one trial: refused hosts get 403, everything is logged.

    HTTPS is tunnelled with CONNECT, so only the host is visible; plain HTTP
    also exposes the URL and is matched against ``deny_url_substrings``.
    ``allow_hosts`` (None: no allowlist) refuses every host that is not an
    entry or, for a ``*.suffix`` entry, a subdomain of it; a wildcard never
    matches its apex, which must be listed itself (the FrontierHarness Eval
    allowlist semantics). Allowed traffic is chained to the upstream proxy
    when one is configured.
    """

    def __init__(self, bind_host, upstream, deny_hosts, deny_url_substrings, log_path, allow_hosts=None):
        self.upstream = None
        if upstream:
            parsed = urllib.parse.urlsplit(upstream)
            self.upstream = (parsed.hostname, parsed.port or 80)
        self.deny_hosts = [host.lower().strip(".") for host in deny_hosts if host]
        self.deny_url_substrings = [text.lower() for text in deny_url_substrings if text]
        self.allow_hosts = (None if allow_hosts is None
                            else [host.lower().strip(".") for host in allow_hosts if host])
        self.phase = "setup"
        self.counts = {"requests": 0, "denied": 0, "errors": 0}
        self._lock = threading.Lock()
        self._log = open(log_path, "a")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind_host, 0))
        self._sock.listen(512)
        self.address = "%s:%d" % (bind_host, self._sock.getsockname()[1])
        threading.Thread(target=self._serve, daemon=True).start()

    def host_denied(self, host):
        host = (host or "").lower().rstrip(".")
        return any(host == denied or host.endswith("." + denied) for denied in self.deny_hosts)

    def host_allowed(self, host):
        if self.allow_hosts is None:
            return True
        host = (host or "").lower().rstrip(".")
        return any(host.endswith(entry[1:]) if entry.startswith("*.") else host == entry
                   for entry in self.allow_hosts)

    def refusal(self, host):
        """The rule that refuses ``host``, or None when it may pass."""
        if self.host_denied(host):
            return "deny_list"
        if not self.host_allowed(host):
            return "not_allowlisted"
        return None

    def _serve(self):
        while True:
            conn, peer = self._sock.accept()
            threading.Thread(target=self._handle, args=(conn, peer), daemon=True).start()

    def _record(self, record):
        with self._lock:
            self.counts["requests"] += 1
            if record.get("decision") == "deny":
                self.counts["denied"] += 1
            if record.get("error"):
                self.counts["errors"] += 1
            self._log.write(json.dumps(record, sort_keys=True) + "\n")
            self._log.flush()

    def _open_upstream_tunnel(self, host, port):
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=30), b""
        sock = socket.create_connection(self.upstream, timeout=30)
        target = "%s:%d" % (host, port)
        sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
        head, rest = _read_head(sock)
        status = head.split(b" ", 2)[1:2]
        if status != [b"200"]:
            sock.close()
            raise OSError("upstream proxy refused CONNECT %s: %s" % (target, head.split(b"\r\n", 1)[0][:200]))
        return sock, rest

    def _handle(self, conn, peer):
        record = {"ts": round(time.time(), 3), "phase": self.phase, "peer": peer[0]}
        started = time.time()
        upstream = None
        try:
            conn.settimeout(60)
            head, rest = _read_head(conn)
            method, target = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[:2]
            record["method"] = method
            if method == "CONNECT":
                host, port = _split_host_port(target, 443)
                record.update(host=host, port=port)
                rule = self.refusal(host)
                if rule:
                    record.update(decision="deny", rule=rule)
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                upstream, early = self._open_upstream_tunnel(host, port)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if early:
                    conn.sendall(early)
                if rest:
                    upstream.sendall(rest)
            else:
                url = urllib.parse.urlsplit(target)
                host, port = (url.hostname or "").lower(), url.port or 80
                record.update(host=host, port=port, url=target[:2048])
                rule = self.refusal(host) or ("deny_url" if any(text in target.lower()
                                                                for text in self.deny_url_substrings) else None)
                if rule:
                    record.update(decision="deny", rule=rule)
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                head = _close_connection_header(head)
                if self.upstream is None:
                    origin = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
                    first, _, remainder = head.partition(b"\r\n")
                    parts = first.split(b" ")
                    head = b" ".join([parts[0], origin.encode("latin-1"), parts[-1]]) + b"\r\n" + remainder
                    upstream = socket.create_connection((host, port), timeout=30)
                else:
                    upstream = socket.create_connection(self.upstream, timeout=30)
                upstream.sendall(head + rest)
            record["decision"] = "allow"
            record["bytes_up"], record["bytes_down"] = self._pipe(conn, upstream)
        except Exception as error:  # every failure is evidence; never crash the proxy
            record["error"] = "%s: %s" % (type(error).__name__, str(error)[:300])
        finally:
            record["duration_s"] = round(time.time() - started, 3)
            for sock in (conn, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._record(record)

    @staticmethod
    def _pipe(client, server):
        client.settimeout(None)
        server.settimeout(None)
        sent = {client: 0, server: 0}
        peers = {client: server, server: client}
        while True:
            readable, _, _ = select.select([client, server], [], [], PROXY_IDLE_CLOSE_SEC)
            if not readable:
                break
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return sent[client], sent[server]
                peers[sock].sendall(data)
                sent[sock] += len(data)
        return sent[client], sent[server]


# ---------------------------------------------------------------- trial phases

class Trial:
    def __init__(self, spec):
        self.spec = spec
        self.out = Path(spec["out_dir"])
        self.task_dir = Path(spec["task_dir"])
        self.container = "tbn-" + spec["trial_key"]
        self.verifier_container = "tbn-vrf-" + spec["trial_key"]
        self.verifier_image = "tbn-verifier-" + spec["trial_key"]
        self.result = {
            "schema": RESULT_SCHEMA, "trial_id": spec["trial_id"], "task": spec["task_name"],
            "arm": spec["arm"], "agent_kind": spec["agent"]["kind"], "phases": {},
            "reward": None, "outcome": None, "infra_error": None,
        }
        self.proxy = None
        self.network = None

    def phase(self, name):
        trial = self

        class _Phase:
            def __enter__(self):
                self.started = time.time()
                log("phase %s" % name)
                trial.result["phases"][name] = {"started": round(self.started, 3)}
                return trial.result["phases"][name]

            def __exit__(self, exc_type, exc, tb):
                entry = trial.result["phases"][name]
                entry["duration_s"] = round(time.time() - self.started, 1)
                entry["ok"] = exc_type is None
                if exc_type is not None and not isinstance(exc, InfraError):
                    raise InfraError(name, "%s: %s" % (exc_type.__name__, exc)) from exc
                return False

        return _Phase()

    # -- image
    def ensure_image(self):
        image = self.spec["image"]
        with self.phase("image") as entry:
            source = "present"
            if docker("image", "inspect", image, check=False).returncode != 0:
                errors = []
                for attempt in range(PULL_ATTEMPTS):
                    pulled = docker("pull", image, check=False, timeout=self.spec["pull_timeout_sec"], phase="image")
                    if pulled.returncode == 0:
                        source = "pulled"
                        break
                    errors.append(tail(pulled.stderr, 400))
                    time.sleep(15 * (attempt + 1))
                else:
                    context = self.task_dir / "environment"
                    if not (context / "Dockerfile").is_file():
                        raise InfraError("image", "pull failed: %s" % errors[-1])
                    built = docker("build", "-t", image, str(context), check=False,
                                   timeout=self.spec["build_timeout_sec"], phase="image")
                    if built.returncode != 0:
                        raise InfraError("image", "pull failed (%s); build failed: %s"
                                         % (errors[-1], tail(built.stderr, 600)))
                    source = "built"
                    entry["pull_errors"] = errors
            info = json.loads(docker("image", "inspect", image).stdout.decode())[0]
            config = info.get("Config") or {}
            self.result["image"] = {
                "ref": image, "id": info.get("Id"), "repo_digests": info.get("RepoDigests"),
                "source": source, "workdir": config.get("WorkingDir") or "/", "user": config.get("User") or "",
            }

    # -- network + proxy
    def start_network(self):
        with self.phase("network") as entry:
            gateway = docker("network", "inspect", "bridge", "--format",
                             "{{range .IPAM.Config}}{{.Gateway}} {{end}}", check=False).stdout.decode().split()
            if not gateway:
                self.network = "tbn-net-" + self.spec["trial_key"]
                docker("network", "create", self.network)
                gateway = docker("network", "inspect", self.network, "--format",
                                 "{{range .IPAM.Config}}{{.Gateway}} {{end}}").stdout.decode().split()
            isolation = isolate_ingress(self.network or "bridge")
            self.result["ingress_isolation"] = isolation
            proxy = self.spec["egress"]
            self.proxy = EgressProxy(gateway[0], proxy.get("upstream"), proxy.get("deny_hosts", []),
                                     proxy.get("deny_url_substrings", []), self.out / "egress.jsonl",
                                     allow_hosts=proxy.get("allow_hosts"))
            entry.update(gateway=gateway[0], network=self.network or "bridge", proxy=self.proxy.address,
                         ingress_isolation=isolation)

    # -- container
    def start_container(self):
        with self.phase("container") as entry:
            agent_logs, verifier_logs = self.out / "agent", self.out / "verifier"
            agent_logs.mkdir(parents=True, exist_ok=True)
            seal_verifier_logs(verifier_logs)
            proxy_url = "http://" + self.proxy.address
            env = {"TEST_DIR": "/tests"}
            for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                env[key] = proxy_url
            env["no_proxy"] = env["NO_PROXY"] = self.spec["egress"]["no_proxy"]
            env.update(self.spec.get("task_env") or {})
            argv = ["run", "-d", "--name", self.container, "--label", "tb-native.trial=" + self.spec["trial_id"],
                    "--cpus", str(self.spec["cpus"]), "--memory", self.spec["memory"],
                    "-v", "%s:/logs/agent" % agent_logs, "-v", "%s:/logs/verifier" % verifier_logs]
            if self.network:
                argv += ["--network", self.network]
            for key, value in sorted(env.items()):
                argv += ["-e", "%s=%s" % (key, value)]
            argv += ["--entrypoint", "sh", self.spec["image"], "-c", "sleep infinity"]
            docker(*argv, phase="container")
            probe = docker("exec", self.container, "sh", "-c",
                           'echo "$HOME|$(id -u)|$(id -g)|$(command -v bash || true)"', phase="container")
            home, uid, gid, bash = (probe.stdout.decode().strip().split("|") + ["", "", "", ""])[:4]
            if not bash:
                raise InfraError("container", "image has no bash; Harbor-compatible exec needs bash -lc")
            if not (uid.isdigit() and gid.isdigit()):
                raise InfraError("container", "cannot resolve the image user's uid/gid: %r" % probe.stdout[-200:])
            os.chown(str(agent_logs), int(uid), int(gid))
            agent_logs.chmod(0o755)
            entry.update(home=home or "/root", uid=uid, gid=gid, name=self.container)
            self.result["container_home"] = home or "/root"
            self.place_agent_inputs(uid, gid)

    def place_agent_inputs(self, uid, gid):
        """Files the task definition promises the agent (``spec["agent_inputs"]``), checked and copied in.

        Each source is a regular file of the unpacked task bundle whose sha256 must
        match the pin; it lands at its target owned by the image user, before the
        agent runtime is installed.
        """
        placed = []
        for item in self.spec.get("agent_inputs") or []:
            source = self.task_dir / item["source"]
            data = read_regular_bytes(source)
            if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise InfraError("container", "agent input %s is missing or differs from its pin" % item["source"])
            parent = str(Path(item["target"]).parent)
            docker("exec", "-u", "0", self.container, "mkdir", "-p", parent, phase="container")
            docker("cp", str(source), "%s:%s" % (self.container, item["target"]), phase="container")
            docker("exec", "-u", "0", self.container, "chown", "%s:%s" % (uid, gid), item["target"],
                   phase="container")
            placed.append({"target": item["target"], "sha256": item["sha256"], "bytes": len(data)})
        if placed:
            self.result["agent_inputs"] = placed

    # -- agent runtime
    def fetch_artifact(self, artifact):
        cache = Path(self.spec["cache_dir"]) / artifact["sha256"]
        root = cache / "root"
        if (cache / ".complete").exists():
            return root
        cache.parent.mkdir(parents=True, exist_ok=True)
        archive = cache.parent / (artifact["sha256"] + ".tar.gz")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # The relay's files route needs the run's relay key, like its model routes.
        request = urllib.request.Request(artifact["url"], headers=relay_headers(self.spec, artifact["url"]))
        last = None
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                digest = hashlib.sha256()
                with opener.open(request, timeout=300) as response, open(archive, "wb") as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
                        handle.write(chunk)
                if digest.hexdigest() != artifact["sha256"]:
                    raise OSError("sha256 mismatch for %s" % artifact["url"])
                break
            except OSError as error:
                last = error
                time.sleep(10 * (attempt + 1))
        else:
            raise InfraError("install", "artifact download failed: %s" % last)
        shutil.rmtree(str(cache), ignore_errors=True)
        root.mkdir(parents=True)
        with tarfile.open(str(archive)) as bundle:
            kwargs = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
            bundle.extractall(str(root), **kwargs)
        (cache / ".complete").touch()
        archive.unlink()
        return root

    def provide_system_ca_bundle(self, entry):
        """Protocol amendment 2026-11-12: an image without a system CA store gets the pinned bundle as one.

        Every arm gets this same step, as root before its runtime is installed, so
        the agent and its tools see the store Harbor's installs would have brought.
        ``state`` is ``present`` (the image's own bundle) or ``installed``.
        """
        bundle = self.spec.get("system_ca_bundle")
        if not bundle:
            return
        found = docker("exec", "-u", "0", self.container, "sh", "-c",
                       'for f in "$@"; do if [ -s "$f" ]; then echo "$f"; exit 0; fi; done; exit 1',
                       "sh", *bundle["probe"], check=False, phase="install")
        present = found.stdout.decode(errors="replace").strip() if found.returncode == 0 else ""
        if not present:
            source = self.fetch_artifact(bundle) / bundle["member"]
            docker("exec", "-u", "0", self.container, "mkdir", "-p", os.path.dirname(bundle["target"]),
                   phase="install")
            docker("cp", str(source), "%s:%s" % (self.container, bundle["target"]), phase="install")
            docker("exec", "-u", "0", self.container, "chmod", "0644", bundle["target"], phase="install")
        record = {"state": "present" if present else "installed", "image_bundle": present or None,
                  "sha256": None if present else bundle["sha256"]}
        entry["system_ca_bundle"] = record
        self.result["system_ca_bundle"] = record

    def install_agent(self):
        agent = self.spec["agent"]
        with self.phase("install") as entry:
            self.provide_system_ca_bundle(entry)
            installed = []
            for artifact in agent.get("artifacts", []):
                root = self.fetch_artifact(artifact)
                target = artifact["container_path"]
                docker("exec", self.container, "mkdir", "-p", target, phase="install")
                docker("cp", str(root) + "/.", "%s:%s" % (self.container, target), phase="install")
                installed.append({"name": artifact["name"], "sha256": artifact["sha256"], "path": target})
            for name, text in (agent.get("log_files") or {}).items():
                path = self.out / "agent" / name
                path.write_text(text, encoding="utf-8")
                path.chmod(0o644)
            setup = agent.get("setup")
            if setup:
                # A third-party harness installs into its per-trial home here: as the
                # image's user, with the agent's environment, through the trial egress
                # proxy (still in its "setup" phase), before and outside the agent budget.
                outcome = exec_with_deadline(
                    self.container, setup["program"], dict(agent.get("env") or {}),
                    "tbn-setup-" + self.spec["trial_key"], setup["timeout_sec"],
                    self.out / "setup-exec.stdout", self.out / "setup-exec.stderr")
                entry["setup"] = outcome
                if outcome["timed_out"] or outcome["exit_code"] != 0:
                    raise InfraError("install", "setup failed (exit %s, timed out %s): %s" % (
                        outcome["exit_code"], outcome["timed_out"],
                        tail((self.out / "setup-exec.stderr").read_bytes(), 1500)))
            check = agent.get("install_check")
            if check:
                probe = docker("exec", self.container, "bash", "-lc", check, check=False, timeout=300, phase="install")
                entry["check_output"] = tail(probe.stdout + probe.stderr, 600)
                if probe.returncode != 0:
                    raise InfraError("install", "install check failed: %s" % entry["check_output"])
            guard = agent.get("guard_check")
            if guard:
                # The benchmark-source canary runs with the agent's environment, so it
                # exercises exactly the configuration the agent will run with.
                argv = ["exec"]
                for key, value in sorted((agent.get("env") or {}).items()):
                    argv += ["-e", "%s=%s" % (key, value)]
                probe = docker(*(argv + [self.container, "bash", "-lc", guard]), check=False, timeout=300,
                               phase="install")
                entry["guard_check_output"] = tail(probe.stdout + probe.stderr, 3000)
                entry["guard_check_ok"] = probe.returncode == 0
                if probe.returncode != 0:
                    raise InfraError("install", "benchmark-source guard check failed: %s"
                                     % entry["guard_check_output"][-1500:])
            entry["artifacts"] = installed

    # -- agent run
    def run_agent(self):
        agent = self.spec["agent"]
        self.proxy.phase = "agent"
        with self.phase("agent") as entry:
            env = dict(agent.get("env") or {})
            env["TB_WORKDIR"] = self.result["image"]["workdir"]
            done = agent.get("completion_marker")
            completion = CompletionMarker(self.out / "agent" / done["file"], done["line_prefix"]) if done else None
            hold = None
            if agent.get("relay_held_url"):
                try:
                    hold = RelayHold(agent["relay_held_url"], relay_headers(self.spec, agent["relay_held_url"]))
                except (OSError, ValueError, KeyError) as error:
                    raise InfraError("agent", "relay held seconds unreadable before the agent started: %s" % error)
            outcome = exec_with_deadline(
                self.container, agent["program"], env, "tbn-agent-" + self.spec["trial_key"],
                self.spec["agent_timeout_sec"] + agent.get("deadline_grace_sec", 0),
                self.out / "agent-exec.stdout", self.out / "agent-exec.stderr", completion=completion,
                extension=hold)
            entry.update(outcome)
            entry["budget_sec"] = self.spec["agent_timeout_sec"]
            if hold is not None:
                entry.update(relay_held_base_s=hold.base, relay_held_reads=hold.reads,
                             relay_held_errors=hold.errors)
            for item in agent.get("collect") or []:
                source = item["container_path"].replace("$HOME", self.result["container_home"])
                copied = docker("cp", "%s:%s" % (self.container, source), str(self.out / "agent" / item["name"]),
                                check=False, phase="agent")
                entry.setdefault("collected", {})[item["name"]] = copied.returncode == 0

    # -- verifier
    def run_verifier(self):
        if self.spec.get("verifier"):
            self.run_separate_verifier()
            return
        self.proxy.phase = "verifier"
        with self.phase("verifier") as entry:
            seal_verifier_logs(self.out / "verifier")
            docker("exec", "-u", "0", self.container, "mkdir", "-p", "/tests", "/logs/verifier", "/logs/agent",
                   phase="verifier")
            docker("cp", str(self.task_dir / "tests") + "/.", "%s:/tests" % self.container, phase="verifier")
            self.run_tests(self.container, entry)

    def run_tests(self, container, entry):
        """``tests/test.sh`` as root in ``container`` (retried only after a timeout); reads the reward."""
        attempts = []
        for attempt in range(self.spec["verifier_attempts"]):
            outcome = exec_with_deadline(
                container, "bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1",
                dict(self.spec.get("verifier_env") or {}), "tbn-verifier-%d-%s" % (attempt, self.spec["trial_key"]),
                self.spec["verifier_timeout_sec"],
                self.out / "verifier-exec.stdout", self.out / "verifier-exec.stderr", user="0")
            attempts.append(outcome)
            if not outcome["timed_out"]:
                break
        entry["attempts"] = attempts
        reward = self.read_reward()
        entry["reward_source"] = reward[1]
        self.result["reward"] = reward[0]
        self.result["verifier_timed_out"] = all(item["timed_out"] for item in attempts)

    def collect_artifacts(self):
        """Schema 1.3 collect: produce the artifacts in the agent's container, copy them out, then remove it.

        Whatever the agent left at an artifact path is deleted first, so only the
        task's collect commands can write what is graded. The container goes
        before verification starts: nothing the agent left running can touch the
        verifier, and its CPU and memory are free for it.
        """
        contract = self.spec["verifier"]
        self.proxy.phase = "collect"
        with self.phase("collect") as entry:
            docker("exec", "-u", "0", self.container, "rm", "-rf", *contract["artifacts"], phase="collect")
            entry["commands"] = [
                exec_with_deadline(self.container, item["command"], {},
                                   "tbn-collect-%d-%s" % (index, self.spec["trial_key"]), item["timeout_sec"],
                                   self.out / "collect-exec.stdout", self.out / "collect-exec.stderr", user="0")
                for index, item in enumerate(contract["collect"])]
            entry["artifacts"] = {}
            for path in contract["artifacts"]:
                host = self.out / "artifacts" / path[len(ARTIFACTS_DIR) + 1:]
                host.parent.mkdir(parents=True, exist_ok=True)
                copied = docker("cp", "%s:%s" % (self.container, path), str(host), check=False, phase="collect")
                entry["artifacts"][path] = collected_artifact(host, copied)
            state = docker("inspect", self.container, "--format", "{{json .State}}", check=False)
            try:
                entry["agent_container_state"] = json.loads(state.stdout.decode())
            except ValueError:
                pass
            docker("rm", "-f", self.container, check=False, timeout=300)

    def run_separate_verifier(self):
        contract = self.spec["verifier"]
        self.collect_artifacts()
        self.proxy.phase = "verifier"
        with self.phase("verifier") as entry:
            docker("build", "--network", "none", "-t", self.verifier_image, str(self.task_dir / "tests"),
                   timeout=contract["build_timeout_sec"], phase="verifier")
            seal_verifier_logs(self.out / "verifier")
            env = {"TEST_DIR": "/tests"}
            env.update(self.spec.get("verifier_env") or {})
            argv = ["run", "-d", "--name", self.verifier_container,
                    "--label", "tb-native.trial=" + self.spec["trial_id"], "--network", "none",
                    "--cpus", str(contract["cpus"]), "--memory", contract["memory"],
                    "-v", "%s:/logs/verifier" % (self.out / "verifier"),
                    "-v", "%s:%s:ro" % (self.out / "artifacts", ARTIFACTS_DIR)]
            for key, value in sorted(env.items()):
                argv += ["-e", "%s=%s" % (key, value)]
            argv += ["--entrypoint", "sh", self.verifier_image, "-c", "sleep infinity"]
            docker(*argv, phase="verifier")
            entry.update(container=self.verifier_container, image=self.verifier_image)
            self.run_tests(self.verifier_container, entry)
            if entry["reward_source"] == "reward.txt" and self.result["reward"] == CRASH_SENTINEL_REWARD:
                entry.update(raw_reward=self.result["reward"], reward_sentinel="verifier_crash")
                self.result["reward"] = None

    def read_reward(self):
        text = read_regular_file(self.out / "verifier" / "reward.txt")
        if text and text.strip():
            return float(text.strip()), "reward.txt"
        text = read_regular_file(self.out / "verifier" / "reward.json")
        if text and text.strip():
            data = json.loads(text)
            if isinstance(data, dict):
                value = data.get("reward", next(iter(data.values()), None) if len(data) == 1 else None)
            else:
                value = data
            return (float(value) if value is not None else None), "reward.json"
        return None, None

    def teardown(self):
        with self.phase("teardown") as entry:
            state = docker("inspect", self.container, "--format", "{{json .State}}", check=False)
            if state.returncode == 0:
                try:
                    entry["container_state"] = json.loads(state.stdout.decode())
                except ValueError:
                    pass
            docker("rm", "-f", self.container, check=False, timeout=300)
            if self.spec.get("verifier"):
                docker("rm", "-f", self.verifier_container, check=False, timeout=300)
                docker("rmi", "-f", self.verifier_image, check=False, timeout=300)
            if self.network:
                docker("network", "rm", self.network, check=False, timeout=60)

    def unpack_task(self):
        with self.phase("prepare"):
            shutil.rmtree(str(self.task_dir), ignore_errors=True)
            self.task_dir.mkdir(parents=True)
            with tarfile.open(self.spec["task_bundle"]) as bundle:
                for member in bundle.getmembers():
                    name = Path(member.name)
                    if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                        raise InfraError("prepare", "unsafe task bundle member: %s" % member.name)
                kwargs = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
                bundle.extractall(str(self.task_dir), **kwargs)
            if not (self.task_dir / "tests" / "test.sh").is_file():
                raise InfraError("prepare", "task bundle has no tests/test.sh")

    def execute(self):
        try:
            self.unpack_task()
            self.ensure_image()
            self.start_network()
            self.start_container()
            self.install_agent()
            self.run_agent()
            self.run_verifier()
            self.result["outcome"] = "scored" if self.result["reward"] is not None else "no_reward"
        except InfraError as error:
            self.result["outcome"] = "infra_error"
            self.result["infra_error"] = {"phase": error.phase, "message": str(error)[:4000]}
        except Exception:  # an unexpected runner fault is never scored
            self.result["outcome"] = "infra_error"
            self.result["infra_error"] = {"phase": "runner", "message": traceback.format_exc()[-4000:]}
        finally:
            try:
                self.teardown()
            except Exception:
                self.result.setdefault("teardown_error", traceback.format_exc()[-2000:])
            if self.proxy is not None:
                self.result["egress"] = dict(self.proxy.counts)
            self.result["finished_at"] = round(time.time(), 3)
            (self.out / "result.json").write_text(json.dumps(self.result, indent=2, sort_keys=True))
            partial = str(self.out) + ".bundle.tar.gz.partial"
            with tarfile.open(partial, "w:gz") as bundle:
                bundle.add(str(self.out), arcname="trial")
            os.replace(partial, str(self.out) + ".bundle.tar.gz")
        return self.result


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: vm_trial.py SPEC_JSON\n")
        return 2
    spec = json.loads(Path(argv[1]).read_text())
    Path(spec["out_dir"]).mkdir(parents=True, exist_ok=True)
    result = Trial(spec).execute()
    sys.stdout.write(json.dumps({key: result.get(key) for key in ("trial_id", "outcome", "reward", "infra_error")}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
