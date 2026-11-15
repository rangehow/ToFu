"""Out-of-band headless repair sessions (designs/headless-recovery-agent/DESIGN.md, piece O2).

Responsibility: start and supervise ONE sandboxed repair session per incident
when the watcher's own alert decision fires or the in-platform layer
escalates. The session is Tofu's storage-free headless runtime
(``tofu-agent run``) or a fixed harness; it never depends on TofuDB, a Tofu
conversation or the owner's session.

The model only reads, edits its own lane and runs tests (propose). Every
operational action belongs to the deterministic watcher (dispose), not to the
session process.

Session process (kernel-enforced on this host):

- PID and network namespaces (``unshare --map-root-user --net --pid
  --kill-child``): Tofu processes are invisible and cannot be signalled, and
  the only egress is the relay socket the watcher owns.
- Its own process group: the watcher SIGTERMs that group when the incident
  closes, never a Tofu process. A recorded PID is verified as the still
  running group leader before any signal, and one tick of grace precedes
  SIGKILL.
- The environment is built from scratch: no credentials, no SSH agent, the
  session directory as HOME, git transport disabled.

Boundary with the in-platform layer (local disk, never TofuDB): a live
``logs/self-repair/<fingerprint>.lease`` keeps a session off; a
``.escalate.json`` starts one; my claim is mirrored to
``oob-<fingerprint>.claim`` so Tofu skips it. The owner's
``storage_recover.sh`` lock wins over any session.

Entry points: ``Repairs`` (``start``, ``poll``, ``pending_escalations``,
``running``), ``RepairConfig``, ``sandbox_argv``, ``sandboxed_runner``,
``direct_runner``, ``default_workspace``. Dependencies: the standard library;
``git`` for the workspace and ``unshare`` for the sandbox.

Residual risk: the filesystem boundary is AUDIT, not kernel (this container
denies mount and kernel 4.18 has no Landlock). See DESIGN.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import functools
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable

from serverctl_pkg import oob_relay
from serverctl_pkg.oob_runtime import routing_envelope

_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
SANDBOX_PREFIX = ('unshare', '--map-root-user', '--net', '--pid', '--kill-child')
# Git is never allowed to reach a remote from a repair session.
_GIT_OFFLINE = {'GIT_SSH_COMMAND': '/bin/false', 'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'}


@dataclass(frozen=True)
class RepairConfig:
    enabled: bool = False
    harness: tuple[str, ...] = ()  # argv template: {prompt} {output} {session} {workspace} {routing}
    branch_base: str = ''
    runner: Callable = None  # sandboxed by default; tests inject direct_runner
    model: str = ''  # the gateway model of the session's route; oob_runtime.DEFAULT_MODEL when empty

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, relay_socket: Path | None = None) -> 'RepairConfig':
        """``relay_socket`` is the watcher's relay: the session's only egress, via its loopback bridge."""
        harness = raw.get('harness') or ()
        runner = sandboxed_runner
        if relay_socket is not None:
            runner = functools.partial(sandboxed_runner, relay_socket=relay_socket)
        if raw.get('runner') == 'direct':  # tests and dry runs only
            runner = direct_runner
        return cls(enabled=raw.get('enabled') is True and bool(harness), harness=tuple(harness),
                   branch_base=str(raw.get('branchBase') or ''), runner=runner, model=str(raw.get('model') or ''))


# ── session process environment and launch ──────────────────────────────

def minimal_env(session_dir: Path, workspace: Path) -> dict[str, str]:
    """The session's environment: nothing inherited that could carry a credential."""
    return {'HOME': str(session_dir), 'PATH': os.defpath, 'TMPDIR': str(session_dir),
            'LANG': 'C.UTF-8', 'PWD': str(workspace), 'GIT_CEILING_DIRECTORIES': str(workspace),
            **_GIT_OFFLINE}


def sandbox_argv(argv: list[str], *, relay_socket: Path | None = None) -> list[str]:
    """Kernel isolation: no host PID visibility and no network, with the child killed if the launcher dies.

    With ``relay_socket`` the sandbox's first process is the loopback bridge
    (``oob_relay.bridge``): the session's HTTP model client reaches
    ``oob_relay.SANDBOX_BASE_URL`` and through it only the relay.
    """
    if relay_socket is not None:
        argv = oob_relay.bridge_argv(relay_socket, argv)
    return [*SANDBOX_PREFIX, '--', *argv]


def _spawn_group(argv: list[str], *, env: dict[str, str], cwd: Path, stdout: Path) -> subprocess.Popen:
    with stdout.open('ab') as stream:
        return subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)


def sandboxed_runner(argv: list[str], *, env: dict[str, str], cwd: Path, stdout: Path,
                     relay_socket: Path | None = None) -> subprocess.Popen:
    return _spawn_group(sandbox_argv(argv, relay_socket=relay_socket), env=env, cwd=cwd, stdout=stdout)


def direct_runner(argv: list[str], *, env: dict[str, str], cwd: Path, stdout: Path) -> subprocess.Popen:
    """No isolation: tests and dry runs only."""
    return _spawn_group(argv, env=env, cwd=cwd, stdout=stdout)


def _alive_group(pid: int) -> bool:
    """True only while the recorded group leader still runs (a zombie or recycled PID is not a target)."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except OSError:
        return False
    return fields[0] not in ('Z', 'X') and fields[2] == str(pid)


def _signal_group(pid: int, number: int) -> bool:
    if not _alive_group(pid):
        return False
    os.killpg(pid, number)
    return True


def _write(path: Path, record: Any) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    os.replace(temporary, path)


# ── the fixed recovery prompt ───────────────────────────────────────────

_BOUNDARY = """- MAY: read code and logs, edit only this workspace, create and run focused tests there.
- MAY propose (the watcher executes after its own checks): storage_recover.sh,
  serverctl restart through the certification gate, publishing a verified lane,
  reverting a landed fix, re-delivering a steered owner forward, resuming interrupted Workers.
- NEVER: signal the engine or any process but your own children; delete data,
  history or backups; force-push or git_land; change keys, the vault, .env or
  config.json; land or deploy an uncertified engine; compile on the production
  path; touch TofuDB files; send anything to anyone.
- A fix touching the watcher, relay, sandbox, this prompt, authority or the
  notification policy is never published automatically: escalate instead.
- Owner-only: deletes, external sends, secrets, privacy, intent, uncertified compiles.
- Facts only: live probes, log lines and code. Never memory."""


def build_prompt(key: str, detail: dict[str, Any], workspace: Path, output: Path) -> str:
    return (
        'You are Tofu\'s out-of-band repair session for one incident. You run without TofuDB and without\n'
        'the Tofu application: it may be down. Diagnose the incident from the evidence, then fix it.\n\n'
        f'Incident key: {key}\n'
        f'Incident record:\n{json.dumps(detail, ensure_ascii=False, indent=1, sort_keys=True)}\n\n'
        f'Your workspace (the only place you may edit): {workspace}\n'
        'Steps:\n'
        '1. Reproduce the incident with one failing spec on the base before fixing it.\n'
        '2. Fix the smallest owning module. Do not change public contracts.\n'
        '3. Run that spec, then the neighbouring specs, with the workspace venv.\n'
        '4. Commit in this workspace with a message naming the incident.\n'
        '5. If you cannot fix it, say what blocks you and what you need.\n\n'
        f'Boundary:\n{_BOUNDARY}\n\n'
        f'Result: write one JSON object to {output} with keys ok (bool), cause, actions (the proposals for\n'
        'the launcher, in order), tests (what you ran and its outcome) and rollback (how to undo your fix).\n')


# ── session supervisor ─────────────────────────────────────────────────

class Repairs:
    """One sandboxed repair session per incident, supervised by the watcher."""

    def __init__(self, config: RepairConfig, state_dir: Path, tofu_root: Path, *,
                 workspace: Callable[[str], Path] | None = None, runner: Callable | None = None,
                 owner_lock: Path | None = None, clock: Callable[[], float] = time.time):
        self.config = config
        self.root = state_dir / 'repair'
        self.handoff = tofu_root / 'logs' / 'self-repair'
        self.tofu_root = tofu_root
        self.owner_lock = owner_lock
        self.clock = clock
        self.runner = runner or config.runner or sandboxed_runner
        self._workspace = workspace or (lambda key: default_workspace(tofu_root, key))
        self._sessions: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self.refusals: dict[str, str] = {}

    # ── cross-layer boundary ────────────────────────────────────────────

    def _lease_until(self, key: str) -> float:
        path = self.handoff / f'{key}.lease'
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 0.0
        return float(record.get('staleAfter') or 0.0) if record.get('fingerprint') == key else 0.0

    def _owner_active(self) -> bool:
        if self.owner_lock is None:
            return False
        try:
            with self.owner_lock.open('a') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
        except OSError:
            return False  # an unwritable lock cannot prove an active run
        return False

    def pending_escalations(self) -> list[dict[str, Any]]:
        return self.escalations(unclaimed_only=True)

    def escalations(self, *, unclaimed_only: bool = False) -> list[dict[str, Any]]:
        """Escalation files still present, oldest first; unclaimed ones only on request."""
        found = []
        try:
            paths = sorted(self.handoff.glob('*.escalate.json'))
        except OSError:
            return []
        for path in paths:
            key = path.name[:-len('.escalate.json')]
            if not _KEY.fullmatch(key):
                continue  # an escalation never chooses a path outside the handoff dir
            try:
                detail = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if detail.get('fingerprint') != key or self._lease_until(key) > self.clock():
                continue  # an in-platform lease means that layer owns this incident
            if unclaimed_only and ((self.root / key / 'claim.json').exists()
                                   or (self.handoff / f'oob-{key}.claim').exists()):
                continue
            found.append({'fingerprint': key, **detail})
        return found

    # ── sessions ────────────────────────────────────────────────────────

    def _claim(self, key: str, detail: dict[str, Any]) -> Path | None:
        directory = self.root / key
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        claim = directory / 'claim.json'
        try:
            descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return None
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'key': key, 'at': self.clock(), 'detail': detail}, stream, ensure_ascii=False)
        try:
            _write(self.handoff / f'oob-{key}.claim', {'key': key, 'at': self.clock(), 'layer': 'out-of-band'})
        except OSError:
            pass  # the Tofu-side hint is best effort; the session still runs
        return directory

    def start(self, key: str, detail: dict[str, Any]) -> dict[str, Any] | None:
        """Start this incident's session once; None when another layer or the owner has priority."""
        if not self.config.enabled or not _KEY.fullmatch(key):
            return None
        if (existing := self._sessions.get(key)) is not None:
            return existing
        if (self.root / key / 'outcome.json').exists():
            return self._record(key, status='finished', adopted=True)
        if (self.root / key / 'claim.json').exists() or (self.handoff / f'oob-{key}.claim').exists():
            return self._record(key, status='running', adopted=True)
        until = self._lease_until(key)
        if until > self.clock():
            self.refusals[key] = f'in-platform lease live until {time.strftime("%H:%M", time.localtime(until))}'
            return None
        if self._owner_active():
            self.refusals[key] = "the owner's storage_recover.sh run holds its lock"
            return None
        directory = self._claim(key, detail)
        if directory is None:
            return self._record(key, status='running', adopted=True)
        workspace = self._workspace(key)
        prompt = directory / 'prompt.md'
        output = directory / 'result.json'
        prompt.write_text(build_prompt(key, detail, workspace, output), encoding='utf-8')
        routing = directory / 'routing.json'  # key-less: its secret is a placeholder, the relay holds the key
        _write(routing, routing_envelope(**({'model': self.config.model} if self.config.model else {})))
        argv = [part.format(prompt=str(prompt), output=str(output), session=str(directory),
                            workspace=str(workspace), routing=str(routing)) for part in self.config.harness]
        try:
            child = self.runner(argv, env=minimal_env(directory, workspace), cwd=workspace,
                                stdout=directory / 'harness.out')
        except OSError as error:
            record = {'key': key, 'directory': str(directory), 'status': 'launch_failed',
                      'error': f'{type(error).__name__}: {error}', 'argv': argv}
            self._sessions[key] = record
            _write(directory / 'outcome.json', {**record, 'reason': 'the harness could not start'})
            return record
        self._processes[key] = child
        record = {'key': key, 'directory': str(directory), 'pid': child.pid, 'status': 'running',
                  'workspace': str(workspace), 'startedAt': self.clock()}
        self._sessions[key] = record
        _write(directory / 'session.json', record)
        return record

    def _record(self, key: str, *, status: str, adopted: bool) -> dict[str, Any]:
        directory = self.root / key
        try:
            saved = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        record = {**saved, 'key': key, 'directory': str(directory), 'status': status}
        self._sessions[key] = record
        return record

    def _finish(self, key: str, *, status: str, reason: str, exit_code: int | None = None) -> dict[str, Any]:
        record = self._sessions.get(key) or self._record(key, status=status, adopted=True)
        result = self._read_result(self.root / key)
        record = {**record, 'status': status, 'reason': reason, 'finishedAt': self.clock(), 'exitCode': exit_code,
                  **({'result': result} if result is not None else {})}
        _write(self.root / key / 'outcome.json', record)
        self._sessions[key] = record
        self._processes.pop(key, None)
        return record

    @staticmethod
    def _read_result(directory: Path) -> dict[str, Any] | None:
        try:
            return json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def poll(self, open_keys: set[str]) -> list[dict[str, Any]]:
        """One supervision pass: finish exited sessions, stop those whose incident closed."""
        outcomes = []
        for key, record in list(self._sessions.items()):
            if record.get('status') != 'running':
                continue
            child = self._processes.get(key)
            exited = child.poll() if child is not None else None
            if child is not None and exited is None:
                if key not in open_keys:
                    _signal_group(record['pid'], signal.SIGTERM)
                    record['closing'] = True
                continue
            if child is None:
                # Adopted after a watcher restart: the process must still be the leader it claims to be.
                if _alive_group(record['pid']):
                    if key not in open_keys:
                        _signal_group(record['pid'], signal.SIGTERM)
                        record['closing'] = True
                    continue
                exited = None
            if key not in open_keys:
                if record.get('closing'):
                    _signal_group(record['pid'], signal.SIGKILL)
                outcomes.append(self._finish(key, status='stopped', reason='incident closed'))
            else:
                outcomes.append(self._finish(key, status='finished', exit_code=exited,
                                             reason=f'harness exit {exited if exited is not None else "unknown"}'))
        return outcomes

    def running(self) -> dict[str, dict[str, Any]]:
        return {key: record for key, record in self._sessions.items() if record.get('status') == 'running'}


def default_workspace(tofu_root: Path, key: str) -> Path:
    """A fresh git worktree for this incident's lane, on the session's local disk.

    ``scripts/lane.py`` keeps its lanes under ``~/.local/state``, which is the
    root overlay that filled on 2026-11-12; a repair workspace lives beside the
    session instead. The base is the repository's current HEAD.
    """
    directory = tofu_root / 'logs' / 'self-repair' / 'lanes' / key
    directory.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '-C', str(tofu_root), 'worktree', 'add', '--detach', str(directory), 'HEAD'],
                   check=True, capture_output=True, timeout=300)
    return directory


__all__ = ['RepairConfig', 'Repairs', 'build_prompt', 'default_workspace', 'direct_runner', 'minimal_env',
           'sandbox_argv', 'sandboxed_runner']
