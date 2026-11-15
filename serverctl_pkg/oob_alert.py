"""Out-of-band outage alert: a watcher outside the Tofu application.

Responsibility: tell the owner that production storage is unavailable while
every in-app alert path (Watchdog reviews, the channel outbox) is down with
it. The watcher is its own process. It never imports the storage client and
never signals, restarts or writes a Tofu process or Tofu data. Each tick
(``PROBE_INTERVAL_S``) it reads:

- ``GET /api/ready`` on the loopback application port, the readiness probe
  the manager uses (process memory only). 200 is healthy. A 503 carries the
  storage snapshot (``state``; ``degraded`` once the restart breaker latched).
  No answer means the API host is not serving, unless the probe only timed
  out while the manager's own readiness check passes (its state file,
  written this interval): that is slow, not down (``manager_ready``).
- the manager's ``GET /status?probe=0`` (its recorded state; no live probe),
  only while the API host does not answer.
- the lines appended since the last tick to ``logs/server-console.log``
  (storage runtime: breaker trip, degraded reminder, liveness kill) and
  ``logs/server-manager.log`` (the manager's crash-loop pause).

An incident opens at the first unavailable tick, backdated to a liveness-kill
or breaker line read in that tick. It alerts once: when it has lasted
``ALERT_AFTER_S`` or more, or at once when a restart breaker latched. An
incident that a planned restart caused (the manager's own restart record,
``planned_restart``) is not an outage: it sends one notice saying the restart
overran when it reaches ``ALERT_AFTER_S``, and the outage alert only if the
restart fails. One recovery notice follows when ``/api/ready`` answers 200
again. Incident and
delivery state persist in ``state.json``, so restarting the watcher never
repeats an alert. ``events.jsonl`` records transitions and deliveries, and
``probes.jsonl`` every tick (the current and the previous day).

Delivery: the 大象 notification robot posts into the bound command group
(the installed dx adapter's robot, ``tofu_meituan.dx.robot``: client-credentials token, ``sendGroupMsgByRobot``)
with its secret read in place from the credential vault. It needs neither
TofuDB nor the 大象 user session. It stays off until ``config.json`` enables
it (docs/STORAGE_OPERATIONS.md, "Out-of-band outage alert"). With no enabled
sender the watcher detects and records only.

A storage breaker trip also reaches the owner directly: the native storage
host calls ``notify_trip``, which starts a detached ``notify`` child of this
file, so the alert goes out even when the watcher is not running. The child
and the watcher share ``alert.lock``, ``state.json`` and ``trip-alert.json``,
so one outage alerts once whichever of them sends first.

Dead-man signal: each periodic Watchdog review writes
``logs/watchdog-heartbeat-<owner>.json`` (``lib/router/watchdog/heartbeat.py``)
with the time its next review must have run. A heartbeat past that time
alerts once (deferred while a storage incident already explains it), and a
fresh one sends one notice that reviews resumed.

Entry points: ``main`` (``start``, ``run``, ``once``, ``simulate``, ``status``, ``stop``,
``test-message``, ``notify``), ``Watcher``, ``step``, ``heartbeat_step``,
``probe``, ``tail_signals``, ``notify_trip``, ``read_vault_entry``.
Dependencies: the standard library. A robot send also uses ``cryptography``
and the dx adapter's robot (``lib.channels.adapters``) from the configured Tofu checkout.
"""
# TODO(enterprise): one owner's routing (one robot group per watcher state
# directory); a multi-user host needs per-owner routing for these alerts.
from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import hashlib
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# OOB-ALERT-1 contract (docs/STORAGE_OPERATIONS.md): one probe a minute; an
# outage that lasts ten minutes or more alerts (the owner's rule; a strict
# bound let the 628 s outage of 2026-11-13 02:08 pass), a latched breaker at once.
PROBE_INTERVAL_S = 60.0
ALERT_AFTER_S = 600.0
# The manager's readiness probe deadline (server_manager.probe_application_readiness).
READY_PROBE_TIMEOUT_S = 2.0
# The manager's own restart record (server_manager.MANAGER_STATE_FILE under data/, read_restart_outcome):
# the lastRestart that serverctl status shows, and its in-progress outcomes.
MANAGER_STATE_FILE = Path('data') / 'server-manager-state.json'
RESTART_IN_PROGRESS = frozenset({'started', 'retrying', 'deferred'})
# The shared HTTP client's default deadline (lib/http_transport/client.py).
ROBOT_REQUEST_TIMEOUT_S = 30.0

# Off the root overlay, which fills from outside Tofu (2026-11-12: 98%, ~7 GB/h)
# and dies with the container, and off the network volume Tofu stalls on. On
# this pod /tmp is a local emptyDir volume (docs/STORAGE_OPERATIONS.md).
DEFAULT_STATE_DIR = Path('/tmp') / f'tofu-oob-alert-{os.getuid()}'
CONSOLE_LOG = 'server-console.log'
MANAGER_LOG = 'server-manager.log'
STATE_VERSION = 1

_TS = r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} '
# Anchored on each emitting logger's line format, so an echoed command or a
# quoted log line never matches.
LOG_SIGNALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # lib/storage/runtime.py StorageRuntime._enter_degraded (latched and post-trip-retry wordings)
    ('breaker_trip', CONSOLE_LOG, re.compile(
        _TS + r'CRITICAL Storage restart circuit breaker tripped: (?P<detail>.*?)\. '
              r'(?:Restarts are stopped|Storage stays fenced)')),
    # lib/storage/runtime.py StorageRuntime._degraded_reminder_loop
    ('breaker_latched', CONSOLE_LOG, re.compile(
        _TS + r'WARNING Storage runtime remains degraded \(fenced\): (?P<detail>.*)$')),
    # lib/storage/supervisor.py: daemon and embedded liveness streak expiry
    ('liveness_kill', CONSOLE_LOG, re.compile(
        _TS + r'CRITICAL (?P<detail>(?:Storage sidecar pid=\d+|Embedded Tofu-DB) failed \d+ '
              r'consecutive liveness probes)')),
    # server_manager.py: an execution host exited with services still running (acceptance case 3)
    ('unclean_close', MANAGER_LOG, re.compile(
        _TS + r'WARNING \[server_manager\] (?P<detail>explicit restart replaced an execution host that exited '
              r'after an unclean close: .+? have not quiesced: .+)$')),
    # server_manager.py: the worker crash-loop budget is exhausted
    ('recovery_paused', MANAGER_LOG, re.compile(
        _TS + r'ERROR \[server_manager\] (?P<detail>worker recovery paused after \d+ failures in \d+s)')),
    # lib/storage/supervisor.py: a storage recovery's own progress (O7 leg e, recovering vs stuck)
    ('storage_exited', CONSOLE_LOG, re.compile(
        _TS + r'CRITICAL (?P<detail>Storage sidecar exited unexpectedly .*)$')),
    ('storage_spawned', CONSOLE_LOG, re.compile(
        _TS + r'INFO Storage sidecar spawned pid=(?P<detail>\d+) ')),
)
LATCHING_SIGNALS = frozenset({'breaker_trip', 'breaker_latched', 'recovery_paused'})
_OUTAGE_SIGNALS = frozenset({'breaker_trip', 'liveness_kill', 'recovery_paused'})
_DELIVERED = frozenset({'sent', 'unknown'})


@dataclasses.dataclass(frozen=True)
class Config:
    tofu_root: Path
    api_url: str = 'http://127.0.0.1:15000'
    manager_url: str = 'http://127.0.0.1:15001'
    dx_robot: dict[str, Any] = dataclasses.field(default_factory=dict)
    wechat: dict[str, Any] = dataclasses.field(default_factory=dict)
    repair_agent: dict[str, Any] = dataclasses.field(default_factory=dict)  # disabled unless enabled+configured
    recover_command: str = ''  # the owner's terminal recovery one-liner

    @property
    def logs_dir(self) -> Path:
        return self.tofu_root / 'logs'

    @property
    def vault_dir(self) -> Path:
        return self.tofu_root / 'data' / 'config'

    @classmethod
    def load(cls, state_dir: Path) -> 'Config':
        raw = json.loads((state_dir / 'config.json').read_text(encoding='utf-8'))
        return cls(tofu_root=Path(raw['tofuRoot']), api_url=raw.get('apiUrl') or cls.api_url,
                   manager_url=raw.get('managerUrl') or cls.manager_url, dx_robot=dict(raw.get('dxRobot') or {}),
                   wechat=dict(raw.get('wechat') or {}), recover_command=str(raw.get('recoverCommand') or ''),
                   repair_agent=dict(raw.get('repairAgent') or {}))


@dataclasses.dataclass(frozen=True)
class Probe:
    at: float
    available: bool
    kind: str  # ready | slow | storage_unavailable | not_ready | api_unanswered
    detail: str
    latched: str = ''  # why recovery needs a human (a latched breaker), else ''
    restart: dict[str, Any] = dataclasses.field(default_factory=dict)  # planned_restart() when unavailable


@dataclasses.dataclass(frozen=True)
class Signal:
    kind: str
    at: float
    detail: str


# ── probes ──────────────────────────────────────────────────────────────

# Loopback probes never go through a configured HTTP proxy.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get_json(url: str, timeout: float) -> tuple[int | None, Any, str]:
    """``(HTTP status, JSON body, error)``; status None when nothing answered."""
    try:
        with _LOOPBACK.open(url, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            raw = error.read()
        except (OSError, http.client.HTTPException):
            raw = b''
    except (OSError, ValueError, http.client.HTTPException) as error:
        reason = getattr(error, 'reason', error)
        return None, None, f'{type(reason).__name__}: {reason}'
    try:
        return status, json.loads(raw.decode('utf-8')), ''
    except ValueError:
        return status, None, 'unparsable body'


def _manager_view(config: Config, http: Callable[[str, float], tuple[int | None, Any, str]]) -> tuple[str, str]:
    """``(detail, latched)`` from the manager's recorded worker state."""
    query = urllib.parse.urlencode({'projectPath': str(config.tofu_root), 'probe': '0'})
    status, body, error = http(f'{config.manager_url}/status?{query}', READY_PROBE_TIMEOUT_S)
    if status != 200 or not isinstance(body, dict):
        return f'manager status unavailable ({error or f"HTTP {status}"})', ''
    observed = str(body.get('observed') or 'unknown')
    storage = (body.get('nativeStorageService') or {}).get('state') or 'unknown'
    execution = (body.get('executionService') or {}).get('state') or 'unknown'
    detail = f'manager observed={observed}, storage host={storage}, execution host={execution}'
    if observed == 'crashloop':
        return detail, f'manager recovery paused: {body.get("lastFailureReason") or observed}'
    return detail, ''


def _seconds(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def planned_restart(tofu_root: Path) -> dict[str, Any]:
    """The manager's planned restart whose downtime is now, from its own record; else ``{}``.

    ``server_manager`` records each explicit restart in ``lastRestart`` (what
    ``serverctl status`` shows) and the planned worker exit it caused. The
    restart holds from that exit until its replacement worker is ready
    (``workerReadyLoggedAt``), or until its crash budget pauses recovery
    (``paused``, while the manager still observes ``crashloop``). A refused
    restart never stopped the serving worker. Read from the manager's state
    file, so a manager too busy to answer ``/status`` still counts.
    """
    try:
        state = json.loads((tofu_root / MANAGER_STATE_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    restart = state.get('lastRestart') if isinstance(state, dict) else None
    if not isinstance(restart, dict) or state.get('lastWorkerExitKind') != 'planned':
        return {}
    outcome = str(restart.get('outcome') or '')
    started, exited = _seconds(restart.get('startedAt')), _seconds(state.get('lastWorkerExitAt'))
    current = (outcome in RESTART_IN_PROGRESS
               or (outcome == 'spawned'
                   and _seconds(state.get('workerReadyLoggedAt')) < _seconds(restart.get('settledAt')))
               or (outcome == 'paused' and state.get('observed') == 'crashloop'))
    if not current or not started or exited < started:
        return {}
    return {'outcome': outcome, 'source': str(restart.get('source') or ''), 'startedAt': started,
            'exitAt': exited, 'message': str(restart.get('message') or '')[:300]}


def manager_ready(tofu_root: Path, now: float) -> bool:
    """Whether the manager's own readiness check passes, by the record it wrote within this probe interval.

    ``server_manager`` probes its worker every few seconds and records the
    verdict (``observed`` degraded and ``unreadySince`` while unready). A
    record not written within ``PROBE_INTERVAL_S`` is no evidence: the
    manager itself may be stuck.
    """
    path = tofu_root / MANAGER_STATE_FILE
    try:
        written = path.stat().st_mtime
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return (isinstance(state, dict) and now - written <= PROBE_INTERVAL_S and state.get('desired') == 'running'
            and state.get('observed') == 'running' and not _seconds(state.get('unreadySince')))


def probe(config: Config, *, now: Callable[[], float] = time.time,
          http: Callable[[str, float], tuple[int | None, Any, str]] = http_get_json,
          restart: Callable[[Path], dict[str, Any]] = planned_restart) -> Probe:
    at = now()
    status, body, error = http(config.api_url + '/api/ready', READY_PROBE_TIMEOUT_S)
    if status is not None and isinstance(body, dict):
        if status == 200 and body.get('ready') is True:
            return Probe(at, True, 'ready', 'ready')
        storage = body.get('storage') if isinstance(body.get('storage'), dict) else {}
        if storage and storage.get('ready') is not True:
            state = str(storage.get('state') or 'unknown')
            reason = str(storage.get('last_error') or '')
            kind, detail = 'storage_unavailable', f'/api/ready {status}: storage {state}' + (f' ({reason})' if reason else '')
            latched = (reason or 'storage restart circuit breaker tripped') if storage.get('degraded') is True else ''
        else:
            kind, latched = 'not_ready', ''
            detail = f'/api/ready {status}: application {body.get("state") or "unknown"}'
    else:
        answered = f'HTTP {status}, {error}' if status is not None else error
        if status is None and error.startswith('TimeoutError') and manager_ready(config.tofu_root, at):
            # Slow, not down (2026-11-13 00:33): the manager's own readiness check is the authority.
            return Probe(at, True, 'slow', f'/api/ready gave no answer in {READY_PROBE_TIMEOUT_S:g} s while the '
                                           "manager's own readiness check passes: slow, not down")
        manager, latched = _manager_view(config, http)
        kind, detail = 'api_unanswered', f'/api/ready did not answer ({answered}); {manager}'
    record = restart(config.tofu_root)
    if record.get('outcome') == 'paused' and not latched:
        latched = f'planned restart paused by the manager: {record["message"] or "crash budget exhausted"}'
    return Probe(at, False, kind, detail, latched, record)


def tail_signals(logs_dir: Path, offsets: dict[str, dict[str, int]]) -> list[Signal]:
    """Signals in the lines appended since ``offsets``; advances ``offsets`` in place.

    A log seen for the first time is read from its end (history is not an
    incident). A replaced or truncated log is read from its start. A partial
    last line waits for the next tick.
    """
    found: list[Signal] = []
    for name in (CONSOLE_LOG, MANAGER_LOG):
        try:
            handle = (logs_dir / name).open('rb')
        except FileNotFoundError:
            continue
        with handle:
            stat = os.fstat(handle.fileno())
            saved = offsets.get(name)
            if saved is None:
                offsets[name] = {'inode': stat.st_ino, 'offset': stat.st_size}
                continue
            offset = saved['offset'] if saved['inode'] == stat.st_ino and saved['offset'] <= stat.st_size else 0
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b'\n'):
                    break
                offset += len(raw)
                line = raw.decode('utf-8', 'replace').rstrip('\n')
                for kind, log, pattern in LOG_SIGNALS:
                    match = pattern.match(line) if log == name else None
                    if match:
                        at = time.mktime(time.strptime(match['ts'], '%Y-%m-%d %H:%M:%S'))
                        found.append(Signal(kind, at, match['detail']))
            offsets[name] = {'inode': stat.st_ino, 'offset': offset}
    return found


# ── incident state machine ──────────────────────────────────────────────

def _clock(ts: float) -> str:
    return time.strftime('%H:%M', time.localtime(ts))


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return '不到 1 分钟'
    return f'{minutes} 分钟' if minutes < 60 else f'{minutes // 60} 小时 {minutes % 60} 分钟'


_WHAT = {'storage_unavailable': '存储不可用', 'not_ready': 'Tofu 服务未就绪', 'api_unanswered': 'Tofu API 无响应'}


def action_line(config: Config) -> str:
    """The owner's terminal step: no Tofu conversation runs while storage is down."""
    if not config.recover_command:
        return '处理：恢复命令未配置（config.json recoverCommand）。'
    return f'处理：在终端运行 {config.recover_command}（编译引擎前会问 [y/N]，默认 N）。'


_FOOTER = '恢复后会再通知（外部告警，不经过 TofuDB）。'


def _with_repair_clause(text: str) -> str:
    """The owner's one-per-incident message also says a repair session is running (and he can stop it)."""
    if text.endswith(_FOOTER):
        return text[:-len(_FOOTER)] + '已自动启动修复；要停止它，回复或让我停止。\n' + _FOOTER
    return text


def alert_text(incident: dict[str, Any], observed: Probe, action: str) -> str:
    """What is down, since when, what to do; the probe and log evidence stay in events.jsonl."""
    latched = '，重启熔断已触发' if incident.get('latched') else ''
    return '\n'.join([
        f'【Tofu 停机告警】{_WHAT.get(observed.kind, "Tofu 不可用")}已 {_duration(observed.at - incident["openedAt"])}'
        f'（自 {_clock(incident["openedAt"])} 起{latched}，仍未恢复）。',
        action, _FOOTER])


def recovery_text(incident: dict[str, Any], at: float) -> str:
    what = '本次计划重启停机约' if incident.get('planned') else '本次停机约'
    return (f'【Tofu 已恢复】{_clock(at)} 起服务恢复正常，{what} '
            f'{_duration(at - incident["openedAt"])}（{_clock(incident["openedAt"])}–{_clock(at)}）。')


# server_manager lastRestart outcomes while a planned restart's downtime lasts
_RESTART_PHASES = {'started': '正在停止旧进程、替换存储引擎', 'deferred': '在等旧存储引擎释放锁',
                   'retrying': '新进程启动失败，管理器在重试', 'spawned': '新进程已启动，在等存储就绪'}


def overrun_text(incident: dict[str, Any], observed: Probe) -> str:
    """A planned restart past the outage bound says so; it is never reported as storage being down."""
    outcome = str(incident['planned'].get('outcome') or '')
    phase = _RESTART_PHASES.get(outcome, f'管理器记录 {outcome or "未知"}')
    return '\n'.join([
        f'【Tofu 计划重启超时】计划重启已停机 {_duration(observed.at - incident["openedAt"])}'
        f'（自 {_clock(incident["openedAt"])} 起），仍未完成：{phase}。',
        '这是管理器记录的计划内重启，不是故障告警；重启失败会另发停机告警。',
        _FOOTER])


def _restart_behind(incident: dict[str, Any], record: dict[str, Any], last_ready_at: float | None) -> dict[str, Any]:
    """``record`` when that planned restart caused the incident, else ``{}``.

    It must have begun before the incident opened (a restart begun during an
    outage is a recovery attempt, not its cause), and its worker exit must
    follow the last ready probe (an earlier restart already finished).
    """
    if not record or record.get('startedAt', float('inf')) > incident['openedAt']:
        return {}
    if last_ready_at is not None and record.get('exitAt', 0.0) <= last_ready_at:
        return {}
    return record


def _overrun_due(incident: dict[str, Any], observed: Probe) -> list[dict[str, Any]]:
    """The one notice for a planned restart that reached the outage bound (>=, owner rule: 10 minutes or more)."""
    notice = incident.get('overrun') or {}
    if notice.get('state') in _DELIVERED or observed.at - incident['openedAt'] < ALERT_AFTER_S:
        return []
    if not notice:
        incident['overrun'] = {'state': 'pending', 'attempts': 0, 'dueAt': observed.at}
    return [{'role': 'overrun', 'incident': incident['id'], 'text': overrun_text(incident, observed)}]


def test_text(config: Config, now: float) -> str:
    """The one labelled test: a real alert for a sample storage outage, under a 测试 line."""
    sample = {'openedAt': now - 11 * 60, 'latched': ''}
    observed = Probe(now, False, 'storage_unavailable', 'sample')
    return '测试：演练消息，格式与真实告警相同；当前没有故障，无需操作。\n' + alert_text(
        sample, observed, action_line(config))


def step(state: dict[str, Any], observed: Probe, signals: list[Signal], *,
         action: str = '') -> list[dict[str, Any]]:
    """Advance ``state`` by one tick; returns the deliveries now due.

    A delivery is ``{'role': 'alert'|'recovery', 'incident': id, 'text': ...}``.
    It stays due on later ticks until ``record_delivery`` marks it sent or of
    unknown outcome, so a failed send is retried and a sent one never repeats.
    """
    due: list[dict[str, Any]] = []
    incident = state.get('incident')
    if observed.available:
        if incident is not None:
            state['incident'] = None
            state['lastOutageClosedAt'] = observed.at
            state['lastIncident'] = {**{key: incident.get(key) for key in _RECORDED},
                                     'closedAt': observed.at}
            if any((incident.get(slot) or {}).get('state') in _DELIVERED for slot in ('alert', 'overrun')):
                state['recovery'] = {'incident': incident['id'], 'text': recovery_text(incident, observed.at),
                                     'state': 'pending', 'attempts': 0}
        state['lastReadyAt'] = observed.at
    else:
        if incident is None:
            opened = min([observed.at, *(s.at for s in signals if s.kind in _OUTAGE_SIGNALS)])
            incident = {'id': f'inc-{int(opened)}', 'openedAt': opened, 'kind': observed.kind,
                        'detail': observed.detail, 'latched': '', 'evidence': {}, 'alert': None}
            state['incident'] = incident
            state['recovery'] = None  # a new outage supersedes an undelivered recovery notice
        incident['lastDetail'], incident['lastAt'] = observed.detail, observed.at
        restart = _restart_behind(incident, observed.restart, state.get('lastReadyAt'))
        if restart:
            incident['planned'] = restart  # sticky; later records only update the restart's phase
        latch = observed.latched or next((s.detail for s in signals if s.kind in LATCHING_SIGNALS), '')
        if latch and not incident['latched']:
            incident['latched'] = latch
        for item in signals:
            incident['evidence'].setdefault(item.kind, item.detail)
            if item.kind == 'storage_spawned':  # the storage runtime's current recovery attempt
                incident['attempt'] = {'pid': int(item.detail), 'at': item.at}
            elif item.kind in _OUTAGE_SIGNALS or item.kind in LATCHING_SIGNALS or item.kind == 'storage_exited':
                incident['attempt'] = None  # that attempt failed or recovery latched: no longer recovering
        alert = incident.get('alert') or {}
        overdue = observed.at - incident['openedAt'] >= ALERT_AFTER_S
        if incident.get('planned') and not incident['latched']:
            due.extend(_overrun_due(incident, observed))
        elif alert.get('state') not in _DELIVERED and (incident['latched'] or overdue):
            if not alert:
                incident['alert'] = {'state': 'pending', 'attempts': 0, 'dueAt': observed.at}
            due.append({'role': 'alert', 'incident': incident['id'], 'text': alert_text(incident, observed, action)})
    recovery = state.get('recovery')
    if recovery and recovery.get('state') not in _DELIVERED:
        due.append({'role': 'recovery', 'incident': recovery['incident'], 'text': recovery['text']})
    return due


# What the hand-off record of a closed incident keeps (incident_records).
_RECORDED = ('id', 'kind', 'openedAt', 'planned', 'alert', 'overrun')


def incident_records(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The open and the last closed incident as hand-off records, keyed by incident id.

    R7's incident key ``<groupId>:<episode>`` (docs/ROUTER.md) cannot exist while
    storage is down: its episode counter is in TofuDB and the fence is recorded
    only when readiness returns. The watcher therefore names an outage by its
    own id and leaves ``logs/self-repair/oob-<id>.incident.json``. The in-platform
    fence report adopts a record whose ``openedAt`` falls in its fence window
    as an alias of its incident, so the owner hears of one incident once and
    of its outcome once. A repair session's claim stays ``oob-<id>.claim``.
    """
    recovery = state.get('recovery') or {}
    records = {}
    for incident in (state.get('lastIncident'), state.get('incident')):
        if not incident:
            continue
        key = incident['id']
        records[key] = {
            'version': 1, 'key': key, 'layer': 'out-of-band', 'kind': incident['kind'],
            'openedAt': incident['openedAt'], 'closedAt': incident.get('closedAt'),
            'planned': bool(incident.get('planned')),
            'owner': {'alert': (incident.get('alert') or {}).get('state'),
                      'overrun': (incident.get('overrun') or {}).get('state'),
                      'recovery': (recovery.get('state') if recovery.get('incident') == key
                                   else incident.get('recovery'))}}
    return records


def storage_engine_alive(pid: int) -> bool:
    """Whether ``pid`` is a live ``tofu-db`` process: not a zombie, not a recycled PID (read-only /proc)."""
    try:
        stat_text = Path(f'/proc/{pid}/stat').read_text()
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return False
    state = stat_text[stat_text.rfind(')') + 2:].split()[:1]
    return state not in (['Z'], ['X']) and b'tofu-db' in cmdline


def heartbeat_alert_text(beat: dict[str, Any]) -> str:
    return (f'【Tofu 巡检停止】Watchdog 定期巡检超时：最后一次完成于 {_clock(beat["at"])}（cron '
            f'{beat.get("schedule") or "?"}），应在 {_clock(beat["staleAfter"])} 前再次完成。调度器、存储或 Tofu '
            '进程可能卡住了。本消息由 Tofu 外部告警进程发送，不经过 TofuDB；巡检恢复后会再通知一次。')


def heartbeat_recovery_text(beat: dict[str, Any]) -> str:
    if beat.get('enabled') is not True:
        return f'【Tofu 巡检】Watchdog 定期巡检已在 {_clock(beat["at"])} 关闭，不再监视超时。'
    return f'【Tofu 巡检恢复】Watchdog 定期巡检已在 {_clock(beat["at"])} 恢复。'


def read_heartbeats(logs_dir: Path) -> list[dict[str, Any]]:
    """The Watchdog review heartbeats (``lib/router/watchdog/heartbeat.py``), one per owner."""
    beats = []
    for path in sorted(logs_dir.glob('watchdog-heartbeat-*.json')):
        try:
            beat = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (isinstance(beat, dict) and beat.get('version') == 1 and beat.get('owner') is not None
                and isinstance(beat.get('at'), (int, float))):
            beats.append(beat)
    return beats


def heartbeat_step(state: dict[str, Any], beats: list[dict[str, Any]], now: float, *,
                   outage_open: bool) -> list[dict[str, Any]]:
    """The Watchdog dead-man signal: one alert per stale heartbeat, one notice when reviews resume.

    A heartbeat is stale once ``now`` passes its ``staleAfter``. While a
    storage incident is open its own alert explains the missed reviews, so
    the stale alert waits until no incident is open; after one closes, the
    reviews get the heartbeat's own interval (``staleAfter - at``) to resume.
    """
    records = state.setdefault('heartbeats', {})
    due: list[dict[str, Any]] = []
    for beat in beats:
        owner = str(beat['owner'])
        record = records.get(owner)
        deadline = beat.get('staleAfter')
        if isinstance(deadline, (int, float)) and state.get('lastOutageClosedAt'):
            deadline = max(deadline, state['lastOutageClosedAt'] + deadline - beat['at'])
        stale = beat.get('enabled') is True and isinstance(deadline, (int, float)) and now > deadline
        if stale:
            if record is None or record['heartbeatAt'] != beat['at']:
                record = records[owner] = {'heartbeatAt': beat['at'], 'alert': None, 'recovery': None}
            if (record['alert'] or {}).get('state') not in _DELIVERED and not outage_open:
                record['alert'] = record['alert'] or {'state': 'pending', 'attempts': 0}
                due.append({'role': 'heartbeat_alert', 'incident': f'hb-{owner}-{int(beat["at"])}',
                            'owner': owner, 'text': heartbeat_alert_text(beat)})
        elif record is not None and beat['at'] > record['heartbeatAt']:
            if (record['alert'] or {}).get('state') not in _DELIVERED:
                del records[owner]  # reviews resumed before an alert went out
                continue
            record['recovery'] = record['recovery'] or {'state': 'pending', 'attempts': 0,
                                                        'text': heartbeat_recovery_text(beat)}
            if record['recovery']['state'] in _DELIVERED:
                del records[owner]
            else:
                due.append({'role': 'heartbeat_recovery', 'incident': f'hb-{owner}-{int(record["heartbeatAt"])}',
                            'owner': owner, 'text': record['recovery']['text']})
    return due


def record_delivery(state: dict[str, Any], delivery: dict[str, Any], outcome: str, detail: str,
                    at: float) -> bool:
    """Record one send outcome; True when it changed the delivery state."""
    if delivery['role'] in ('alert', 'overrun'):
        incident = state.get('incident') or {}
        record = incident.get(delivery['role']) if incident.get('id') == delivery['incident'] else None
    elif delivery['role'] in ('heartbeat_alert', 'heartbeat_recovery'):
        entry = (state.get('heartbeats') or {}).get(delivery['owner']) or {}
        record = entry.get('alert' if delivery['role'] == 'heartbeat_alert' else 'recovery')
    else:
        record = state.get('recovery')
    if record is None:
        return False
    previous = record.get('state')
    record['attempts'] = int(record.get('attempts') or 0) + 1
    record['detail'] = detail
    if outcome in _DELIVERED:
        record.update(state=outcome, at=at)
    else:
        record['state'] = 'undeliverable' if outcome == 'disabled' else 'pending'
    last = state.get('lastIncident') or {}
    if delivery['role'] == 'recovery' and last.get('id') == delivery['incident']:
        last['recovery'] = record['state']  # outlives the next incident superseding state['recovery']
    return record['state'] != previous


# ── delivery ────────────────────────────────────────────────────────────

def read_vault_entry(vault_dir: Path, name: str) -> str | None:
    """Plaintext of a credential vault entry, read in place (``lib/credentials_vault.py`` format).

    Read-only: unlike the vault module it never mints a key file, takes no
    lock (the vault replaces its store atomically) and writes nothing.
    """
    from cryptography.fernet import Fernet
    key = (vault_dir / '.credentials_vault.key').read_bytes().strip()
    store = json.loads((vault_dir / 'credentials_vault.json').read_text(encoding='utf-8'))
    row = (store.get('entries') or {}).get(name) or {}
    if not row.get('ct'):
        return None
    return Fernet(key).decrypt(row['ct'].encode('ascii')).decode('utf-8')


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """The robot's HTTP port; the process proxy environment (with its no_proxy list) applies."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=ROBOT_REQUEST_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class NoSender:
    name = 'none'

    def send(self, text: str) -> tuple[str, str]:
        return 'disabled', 'no delivery path is enabled in config.json'

    def preflight(self) -> tuple[bool, str]:
        return False, 'no delivery path is enabled in config.json'


class DryRunSender:
    name = 'dry-run'

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> tuple[str, str]:
        self.sent.append(text)
        return 'sent', 'dry run: recorded, not sent'


class DxRobotSender:
    """The 大象 notification robot posting into the bound command group (CHANNELS.md C6)."""

    name = 'dx-robot'

    def __init__(self, config: Config, *, transport=urllib_transport, secret: Callable[[], str] | None = None):
        settings = config.dx_robot
        self.client_id = str(settings['clientId'])
        self.gid = str(settings['gid'])
        self._credential = str(settings['credentialName'])
        self._vault_dir = config.vault_dir
        self._tofu_root = config.tofu_root
        self._transport = transport
        self._secret = secret or self._vault_secret
        self._robot = None
        self._send_url = ''
        self.last_send_response: str | None = None

    def _vault_secret(self) -> str:
        value = read_vault_entry(self._vault_dir, self._credential)
        if not value:
            raise LookupError(f'vault entry {self._credential} is empty')
        return value

    def _module(self):
        if str(self._tofu_root) not in sys.path:
            sys.path.insert(0, str(self._tofu_root))
        from lib.channels.adapters import require_channel_adapter
        return require_channel_adapter('dx').robot

    def send(self, text: str) -> tuple[str, str]:
        try:
            robot = self._module()
        except Exception as error:  # the checkout cannot be imported: nothing was posted
            return 'not_sent', f'{type(error).__name__}: {error}'
        if self._robot is None:
            self._robot = robot.DxRobot(self.client_id, self._secret, transport=self._observed_transport)
            self._send_url = robot.API_BASE + robot.SEND_GROUP_PATH
        self.last_send_response = None
        posted: list[str] = []
        try:
            for message in robot.text_messages(text):
                posted.append(self._robot.post_group(self.gid, message))
        except robot.RobotOutcomeUnknown as error:
            return 'unknown', f'{error.code}: {error}; {self.last_send_response}'
        except robot.ChannelError as error:
            if posted:  # earlier parts went out: resending would repeat them
                return 'unknown', f'{len(posted)} part(s) sent, then {error.code}: {error}'
            return 'not_sent', f'{error.code}: {error}; {self.last_send_response}'
        except Exception as error:  # the vault or the token request failed before any post
            if posted:
                return 'unknown', f'{len(posted)} part(s) sent, then {type(error).__name__}'
            return 'not_sent', f'{type(error).__name__}: {error}'
        return 'sent', f'gid {self.gid} message {",".join(posted)}; {self.last_send_response}'

    def preflight(self) -> tuple[bool, str]:
        """Read the secret in place and mint a token (``DxRobot.check_credentials``); posts nothing."""
        try:
            robot = self._module()
            tail = self._secret()[-4:]
            robot.DxRobot(self.client_id, self._secret, transport=self._transport).check_credentials()
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            return False, f'{code}: {error}'
        return True, f'robot {self.client_id}: token minted with secret …{tail}; group {self.gid}'

    def _observed_transport(self, method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        """The transport, keeping the group send's HTTP answer as delivery evidence.

        Only the send endpoint's answer is kept (message IDs, no secret); the
        token endpoint's answer carries the access token and is never kept.
        """
        status, raw = self._transport(method, url, headers, body)
        if url == self._send_url:
            self.last_send_response = f'HTTP {status} {raw.decode("utf-8", "replace")}'
        return status, raw


class WeChatSender:
    """微信 ClawBot: iLink ``sendmessage`` into the owner's command chat (CHANNELS.md C12), best effort.

    The bot token and the command chat's reply context (mirrored by the bot
    session into ``channel.u<owner>.wechat.reply``) are read in place from
    the vault. iLink refuses a send whose context token expired (``ret=-2``,
    hours after the owner's last message to ClawBot): a definite refusal.
    """

    name = 'wechat'

    def __init__(self, config: Config, *, transport=urllib_transport):
        settings = config.wechat
        self._token_entry = str(settings['credentialName'])
        self._reply_entry = str(settings['replyEntry'])
        self._api_base = str(settings.get('apiBase') or '').rstrip('/')
        self._vault_dir = config.vault_dir
        self._tofu_root = config.tofu_root
        self._transport = transport

    def _protocol(self):
        if str(self._tofu_root) not in sys.path:
            sys.path.insert(0, str(self._tofu_root))
        from lib.channels.model import WECHAT
        from lib.channels.wechat import protocol
        return protocol, WECHAT

    def send(self, text: str) -> tuple[str, str]:
        try:
            protocol, facts = self._protocol()
            token = read_vault_entry(self._vault_dir, self._token_entry)
            reply = json.loads(read_vault_entry(self._vault_dir, self._reply_entry) or '{}')
        except Exception as error:  # nothing was sent
            return 'not_sent', f'{type(error).__name__}: {error}'
        if not token or not reply.get('to') or not reply.get('contextToken'):
            return 'not_sent', 'no 微信 bot token or command-chat reply context in the vault'
        body = {'msg': {'from_user_id': '', 'to_user_id': reply['to'], 'client_id': f'oob-alert-{os.urandom(8).hex()}',
                        'message_type': 2, 'message_state': 2, 'context_token': reply['contextToken'],
                        'item_list': [{'type': 1, 'text_item': {'text': text}}]},
                'base_info': protocol.base_info()}
        headers = {'Content-Type': 'application/json', **protocol.bot_headers(token)}
        url = (self._api_base or facts['apiBase'].rstrip('/')) + '/ilink/bot/sendmessage'
        try:
            status, raw = self._transport('POST', url, headers, json.dumps(body, ensure_ascii=False).encode('utf-8'))
        except urllib.error.URLError as error:  # refused before the request went out
            return 'not_sent', f'iLink unreachable: {type(error.reason).__name__}'
        except Exception as error:  # the request may have been delivered
            return 'unknown', f'iLink sendmessage got no answer: {type(error).__name__}'
        try:
            answer = json.loads(raw.decode('utf-8')) if raw else {}
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        code = answer.get('ret') or answer.get('errcode') or 0
        if status != 200:
            return 'not_sent', f'iLink sendmessage answered HTTP {status}'
        if code:
            hint = ('; the context token expired: the owner must message ClawBot to renew it'
                    if code == -2 else '; the bot session expired' if code == facts['sessionExpiredCode'] else '')
            return 'not_sent', f'iLink sendmessage returned {code}{hint}'
        return 'sent', f'微信 message {answer.get("message_id") or "(no id)"}'

    def preflight(self) -> tuple[bool, str]:
        """The bot token and reply context are in the vault; iLink has no send-free check."""
        try:
            token = read_vault_entry(self._vault_dir, self._token_entry)
            reply = json.loads(read_vault_entry(self._vault_dir, self._reply_entry) or '{}')
        except Exception as error:
            return False, f'{type(error).__name__}: {error}'
        if not token or not reply.get('contextToken'):
            return False, 'no 微信 bot token or command-chat reply context in the vault'
        return True, f'token …{token[-4:]} and reply context present (expiry unknowable without a send)'


class ChainSender:
    """Each enabled path in order until one delivers (or may have)."""

    def __init__(self, senders: list):
        self.senders = senders
        self.name = '+'.join(sender.name for sender in senders)

    def send(self, text: str) -> tuple[str, str]:
        details = []
        for sender in self.senders:
            outcome, detail = sender.send(text)
            details.append(f'{sender.name}: {detail}')
            if outcome in _DELIVERED:
                return outcome, '; '.join(details)
        return 'not_sent', '; '.join(details)

    def preflight(self) -> tuple[bool, str]:
        results = [(sender.name, *sender.preflight()) for sender in self.senders]
        return any(passed for _, passed, _ in results), '; '.join(f'{name}: {detail}' for name, _, detail in results)


def configured_sender(config: Config):
    """The enabled paths in order: the 大象 robot, then 微信 (best effort); none detects only."""
    senders = []
    if config.dx_robot.get('enabled') is True:
        senders.append(DxRobotSender(config))
    if config.wechat.get('enabled') is True:
        senders.append(WeChatSender(config))
    if not senders:
        return NoSender()
    return senders[0] if len(senders) == 1 else ChainSender(senders)


# ── persistence ─────────────────────────────────────────────────────────

def _private_dir(path: Path) -> None:
    """Create ``path`` 0700; refuse a symlink or another user's directory (the default is in shared /tmp)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f'{path} is not a private directory of this user')
    os.chmod(path, 0o700)


def load_state(state_dir: Path) -> dict[str, Any]:
    try:
        state = json.loads((state_dir / 'state.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': STATE_VERSION}
    if state.get('version') != STATE_VERSION:
        raise ValueError(f'{state_dir / "state.json"} has version {state.get("version")}, expected {STATE_VERSION}')
    return state


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=state_dir, prefix='.state-', suffix='.json')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(temporary, state_dir / 'state.json')
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _append(path: Path, record: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def _iso(ts: float) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(ts))


def _write_json(path: Path, record: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.stem}-', suffix='.json')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class _AlertLock:
    """``alert.lock``: the watcher's deliveries and a trip notification never send concurrently."""

    def __init__(self, state_dir: Path):
        self._path = state_dir / 'alert.lock'
        self._handle = None

    def __enter__(self):
        try:
            self._handle = self._path.open('a')
        except OSError:  # a full disk cannot create the lock file: sending outranks the lock
            self._handle = None
            return self
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        if self._handle is not None:
            self._handle.close()  # closing the descriptor releases the lock


def _read_trip_alert(state_dir: Path) -> dict[str, Any]:
    try:
        record = json.loads((state_dir / 'trip-alert.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _covered_by_trip_alert(state_dir: Path, incident: dict[str, Any]) -> dict[str, Any]:
    """The breaker-trip notification that already alerted this incident, or {}."""
    record = _read_trip_alert(state_dir)
    if record.get('outcome') in _DELIVERED and record.get('at', 0) >= incident['openedAt'] - PROBE_INTERVAL_S:
        return record
    return {}


def _load_repairs(config: Config, state_dir: Path) -> tuple[Any, Any, str]:
    """The repair layer and its model relay from the checkout, or why it is unavailable.

    Never fatal to the alert path. An installed copy runs isolated (``-I``),
    so the repair modules load from ``tofu_root`` like the senders' modules
    do. That checkout may carry the very bug under repair: a module that
    fails to load costs the repair layer, never an alert. The relay is the
    watcher's (DESIGN.md "Credential"). Its key is the project credential,
    revealed through the audited loopback endpoint, with the reserved vault
    entry ``repair.model`` as fallback, and held in memory only.
    """
    if str(config.tofu_root) not in sys.path:
        sys.path.insert(0, str(config.tofu_root))
    try:
        from serverctl_pkg.oob_relay import MemoryKeySource, Relay
        from serverctl_pkg.oob_repair import RepairConfig, Repairs
        relay = None
        if config.repair_agent.get('runner') != 'direct':  # the sandboxed session's only egress
            relay = Relay(MemoryKeySource(lambda: _project_credential(config)), socket_path=state_dir / 'relay.sock')
        repairs = Repairs(RepairConfig.from_mapping(config.repair_agent,
                                                    relay_socket=relay.socket_path if relay else None),
                          state_dir, config.tofu_root)
        return repairs, relay, ''
    except Exception as error:  # noqa: BLE001 - any defect in the checkout's repair modules
        return None, None, f'{type(error).__name__}: {error}'


def _project_credential(config: Config) -> str | None:
    """The audited reveal while Tofu is up, else the reserved vault entry; None when neither exists."""
    from serverctl_pkg.oob_relay import RevealKeySource, VaultKeySource
    secret = RevealKeySource(config.api_url).get()
    if secret:
        return secret
    try:
        return VaultKeySource(config.vault_dir, read_entry=read_vault_entry).get()
    except FileNotFoundError:
        return None  # no vault on this host: no fallback credential


class Watcher:
    """One probe a minute; every local write is best effort, so a full disk never stops an alert.

    The in-memory state is authoritative: it carries dedup across ticks when
    ``state.json`` cannot be written. ``fail_writes`` injects ENOSPC into
    every local write (dry runs and tests only).
    """

    def __init__(self, config: Config, state_dir: Path, *, sender=None, clock: Callable[[], float] = time.time,
                 probe_fn: Callable[[], Probe] | None = None, fail_writes: bool = False,
                 engine_alive: Callable[[int], bool] = storage_engine_alive):
        self.config = config
        self.state_dir = state_dir
        _private_dir(state_dir)
        self.sender = sender if sender is not None else configured_sender(config)
        self.clock = clock
        self.probe_fn = probe_fn or (lambda: probe(config, now=clock))
        self.fail_writes = fail_writes
        self.engine_alive = engine_alive
        self.repairs, self.relay, self.repair_unavailable = None, None, ''
        self.relay_started = False  # only start_relay() resolves the credential: tick() never does
        self.dry_run = False  # a dry run publishes no hand-off record
        if config.repair_agent.get('enabled'):
            self.repairs, self.relay, self.repair_unavailable = _load_repairs(config, state_dir)
        self.state = load_state(state_dir)

    def start_relay(self) -> str:
        """Open the repair relay and resolve its credential now, while Tofu is up; its key tail, or ''.

        Without a credential every incident is alert-only (DESIGN.md).
        """
        if self.relay is None:
            return ''
        try:
            self.relay.start()
            hint = self.relay.key_hint()
        except Exception as error:  # noqa: BLE001 - the repair layer never stops the alert path
            self.close()
            self.relay, self.repairs = None, None
            self.event('repair_unavailable', reason=f'relay not ready: {type(error).__name__}: {error}')
            return ''
        self.relay_started = True
        if hint:
            self.event('repair_relay_ready', keyHint=hint, socket=str(self.relay.socket_path))
        else:
            self.event('repair_unavailable', reason='no model credential: the audited reveal failed and the '
                                                    'vault entry repair.model is empty; incidents are alert-only')
        return hint

    def _repairs_ready(self) -> bool:
        return self.relay is None or (self.relay_started and bool(self.relay.key_source.get()))

    def _refresh_relay(self) -> None:
        """After a verified recovery: lift a rate-limit stop and resolve the credential again."""
        try:
            self.relay.reset()
            self.relay.key_source.get()
        except Exception as error:  # noqa: BLE001 - the repair layer never stops the alert path
            self.event('repair_unavailable', reason=f'credential refresh failed: {type(error).__name__}: {error}')

    def close(self) -> None:
        if self.relay is not None:
            self.relay.close()

    def _best_effort(self, what: str, write: Callable[[], None]) -> bool:
        try:
            if self.fail_writes:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), what)
            write()
            return True
        except OSError as error:
            failures = self.state.setdefault('writeFailures', {'count': 0})
            failures.update(count=failures['count'] + 1, last=f'{what}: {type(error).__name__}: {error}',
                            at=self.clock())
            return False

    def _publish_incident_records(self) -> None:
        """Write each changed incident record (incident_records) once; a failed write retries next tick."""
        if self.dry_run:
            return
        published = self.state.setdefault('published', {})
        current = incident_records(self.state)
        handoff = self.config.logs_dir / 'self-repair'
        for key, record in current.items():
            if published.get(key) == record:
                continue

            def write(key=key, record=record):
                handoff.mkdir(exist_ok=True)
                _write_json(handoff / f'oob-{key}.incident.json', record)
            if self._best_effort(f'oob-{key}.incident.json', write):
                published[key] = record
        for key in [key for key in published if key not in current]:
            del published[key]

    def event(self, name: str, **fields: Any) -> None:
        record = {'at': _iso(self.clock()), 'event': name, **fields}
        self._best_effort('events.jsonl', lambda: _append(self.state_dir / 'events.jsonl', record))

    def _record_probe(self, observed: Probe) -> None:
        path = self.state_dir / 'probes.jsonl'
        day = time.strftime('%Y-%m-%d', time.localtime(observed.at))
        if self.state.get('probeLogDay') not in (None, day) and path.exists():
            self._best_effort('probes.jsonl.1', lambda: os.replace(path, self.state_dir / 'probes.jsonl.1'))
        self.state['probeLogDay'] = day
        incident = self.state.get('incident') or {}
        record = {'at': _iso(observed.at), 'kind': observed.kind, 'available': observed.available,
                  'detail': observed.detail, 'incident': incident.get('id')}
        self._best_effort('probes.jsonl', lambda: _append(path, record))

    def tick(self) -> list[dict[str, Any]]:
        signals = tail_signals(self.config.logs_dir, self.state.setdefault('offsets', {}))
        observed = self.probe_fn()
        before = (self.state.get('incident') or {}).get('id')
        due = step(self.state, observed, signals, action=action_line(self.config))
        after = (self.state.get('incident') or {}).get('id')
        if before and before != after:
            self.event('incident_closed', incident=before, detail=observed.detail)
            if self.relay_started and observed.available:  # a verified recovery: resolve the key again
                self._refresh_relay()
        if after and after != before:
            self.event('incident_opened', incident=after, kind=observed.kind, detail=observed.detail)
        if observed.available and signals:
            self.event('signals_while_ready', signals=[dataclasses.asdict(s) for s in signals])
        due += heartbeat_step(self.state, read_heartbeats(self.config.logs_dir), observed.at,
                              outage_open=self.state.get('incident') is not None)
        open_keys = {key for key in ((self.state.get('incident') or {}).get('id'),)}
        repaired: set[str] = set()
        if self.repairs is not None and self._repairs_ready():
            for signal_item in signals:
                if signal_item.kind != 'unclean_close':
                    continue
                key = f'unclean_close.{int(signal_item.at)}'
                open_keys.add(key)
                if self.repairs.start(key, {'trigger': 'unclean_close',
                                            'signal': dataclasses.asdict(signal_item)}) is not None:
                    repaired.add(key)
                    self.event('repair_started', key=key, reason='unclean close', detail=signal_item.detail)
            self._repair_outage(due, observed, repaired)
            for escalation in self.repairs.escalations():
                key = escalation['fingerprint']
                open_keys.add(key)
                if self.repairs.start(key, {'trigger': 'escalation', 'escalation': escalation}) is not None:
                    repaired.add(key)
            for outcome in self.repairs.poll(open_keys):
                self.event('repair_session_ended', key=outcome['key'], status=outcome['status'],
                           reason=outcome['reason'], directory=outcome.get('directory'))
        outcomes = []
        with _AlertLock(self.state_dir):
            for delivery in due:
                covered = (_covered_by_trip_alert(self.state_dir, self.state['incident'])
                           if delivery['role'] == 'alert' else {})
                if covered:
                    outcome, detail = covered['outcome'], f'the breaker-trip notification of {_iso(covered["at"])}'
                else:
                    outcome, detail = self.sender.send(delivery['text'])
                if record_delivery(self.state, delivery, outcome, detail, observed.at):
                    self.event(f'{delivery["role"]}_{outcome}', incident=delivery['incident'],
                               path='trip-notify' if covered else self.sender.name, detail=detail,
                               text=delivery['text'])
                outcomes.append({**delivery, 'outcome': outcome, 'detail': detail})
            self._publish_incident_records()
            self.state['lastProbe'] = dataclasses.asdict(observed)
            self._record_probe(observed)
            self._best_effort('state.json', lambda: save_state(self.state_dir, self.state))
        return outcomes

    def _repair_outage(self, due: list[dict[str, Any]], observed: Probe, repaired: set[str]) -> None:
        """Start the alerting outage's one repair session, never while its recovery is progressing.

        O7 leg (e), grounded in the storage runtime's own lines: recovering
        while the latest attempt's spawned engine (``Storage sidecar spawned
        pid=P``) is alive with no failure since; then the session waits. Once
        that stops holding (the engine is gone, an attempt failed, a breaker
        latched) the session starts at the next alerting tick.
        """
        incident = self.state.get('incident')
        alerting = incident is not None and (any(item['role'] == 'alert' for item in due)
                                             or (incident.get('alert') or {}).get('state') in _DELIVERED)
        if not alerting:
            return
        attempt = incident.get('attempt')
        if attempt and self.engine_alive(attempt['pid']):
            if incident.get('repairHeld') != attempt['pid']:
                incident['repairHeld'] = attempt['pid']
                self.event('repair_held_recovering', incident=incident['id'], pid=attempt['pid'],
                           since=_iso(attempt['at']))
            return
        detail = {'trigger': 'outage', 'incident': incident, 'probe': dataclasses.asdict(observed)}
        if self.repairs.start(incident['id'], detail) is not None:
            repaired.add(incident['id'])
            for item in due:
                if item['role'] == 'alert':
                    item['text'] = _with_repair_clause(item['text'])

    def run(self, stop: threading.Event) -> None:
        next_at = time.monotonic()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # the watcher must outlive its own failures
                self.event('tick_failed', error=f'{type(error).__name__}: {error}')
            next_at = max(next_at + PROBE_INTERVAL_S, time.monotonic())
            stop.wait(max(0.0, next_at - time.monotonic()))


# ── dry-run scenarios (simulated clock, injected probe failures) ────────

BREAKER_LINE = ('{ts},000 CRITICAL Storage restart circuit breaker tripped: 6 storage crashes/failed starts '
                'within 600s (limit 5); last restart error: RuntimeError: storage sidecar closed its readiness '
                'channel before startup (exit 1). Restarts are stopped and storage stays fenced (degraded); '
                'leaving this state requires a process restart\n')


# Simulated planned-restart probes: (probe kind, detail, the manager's lastRestart outcome)
_PLANNED_PROBES = {
    'planned_down': ('api_unanswered', '/api/ready did not answer (injected planned restart)', 'started'),
    'planned_starting': ('storage_unavailable', '/api/ready 503: storage not_started (injected planned restart)',
                         'spawned'),
    'planned_paused': ('api_unanswered', '/api/ready did not answer (injected planned restart)', 'paused'),
}


def injected_probe(kind: str, at: float, restart: dict[str, Any] | None = None) -> Probe:
    """A simulated probe answer; never touches the application.

    ``planned_*`` kinds carry ``restart`` (``startedAt``/``exitAt``) as the
    manager's restart record, the way ``probe`` reads it from the manager.
    """
    if kind in _PLANNED_PROBES:
        probe_kind, detail, outcome = _PLANNED_PROBES[kind]
        record = {'source': 'injected', 'message': '', **(restart or {}), 'outcome': outcome}
        latched = 'planned restart paused by the manager (injected)' if outcome == 'paused' else ''
        return Probe(at, False, probe_kind, detail, latched, record)
    if kind == 'ready':
        return Probe(at, True, 'ready', 'ready')
    if kind == 'slow':
        return Probe(at, True, 'slow', '/api/ready gave no answer in 2 s while the manager says ready (injected)')
    if kind in ('storage_unavailable', 'breaker_log'):
        return Probe(at, False, 'storage_unavailable', '/api/ready 503: storage restarting (injected)')
    if kind == 'breaker':
        return Probe(at, False, 'storage_unavailable', '/api/ready 503: storage degraded (injected)',
                     '6 storage crashes/failed starts within 600s (limit 5) (injected)')
    if kind == 'api_unanswered':
        return Probe(at, False, kind, '/api/ready did not answer (injected)')
    raise ValueError(f'unknown injected probe {kind!r}')


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


def run_scenario(timeline: list[str], *, root: Path, start: float, restart_after: int | None = None,
                 beats: dict[int, tuple[float, float]] | None = None,
                 fail_writes: bool = False, restart: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """One injected probe per simulated minute in a scratch ``root``; returns the deliveries.

    ``breaker_log`` appends a real-format breaker-trip line to the scratch
    console log, so the log path (not the probe) latches the breaker.
    ``beats`` writes a Watchdog heartbeat before a minute's tick:
    ``{minute: (at, staleAfter)}`` in seconds after ``start``. ``restart`` is
    the manager's planned restart for ``planned_*`` probes: ``startedAt`` and
    ``exitAt`` in seconds after ``start``.
    """
    planned = {key: start + offset for key, offset in (restart or {}).items()}
    logs = root / 'tofu' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    console = logs / CONSOLE_LOG
    console.write_text('2026-11-12 05:59:00,000 INFO scratch console log\n', encoding='utf-8')
    clock = FakeClock(start)
    sender = DryRunSender()
    config = Config(tofu_root=root / 'tofu')
    current = {'kind': timeline[0]}

    def scripted() -> Probe:
        return injected_probe(current['kind'], clock(), planned)

    watcher = Watcher(config, root / 'state', sender=sender, clock=clock, probe_fn=scripted, fail_writes=fail_writes)
    delivered = []
    for minute, kind in enumerate(timeline):
        current['kind'] = kind
        if restart_after is not None and minute == restart_after:
            watcher = Watcher(config, root / 'state', sender=sender, clock=clock, probe_fn=scripted)
        if minute in (beats or {}):
            beat_at, stale_after = beats[minute]
            _write_json(logs / 'watchdog-heartbeat-1.json', {
                'version': 1, 'owner': 1, 'at': start + beat_at, 'schedule': '7,37 * * * *',
                'enabled': True, 'staleAfter': start + stale_after})
        if kind == 'breaker_log':
            with console.open('a', encoding='utf-8') as stream:
                stream.write(BREAKER_LINE.format(ts=time.strftime('%Y-%m-%d %H:%M:%S',
                                                                  time.localtime(clock() - 5))))
        delivered.extend({**item, 'minute': minute} for item in watcher.tick() if item['outcome'] == 'sent')
        clock.now += PROBE_INTERVAL_S
    return delivered


SCENARIOS: dict[str, tuple[list[str], dict[str, Any], list[tuple[str, int]]]] = {
    # name: (one injected probe per minute, options, expected (role, minute) deliveries)
    'outage-alerts-after-ten-minutes': (
        ['ready'] + ['storage_unavailable'] * 14 + ['ready', 'ready'], {}, [('alert', 11), ('recovery', 15)]),
    'transient-outage-is-silent': (['ready'] + ['api_unanswered'] * 9 + ['ready'], {}, []),
    'degraded-probe-alerts-at-once': (
        ['ready', 'ready', 'breaker', 'breaker', 'breaker', 'ready'], {}, [('alert', 2), ('recovery', 5)]),
    'breaker-log-line-alerts-at-once': (
        ['ready', 'storage_unavailable', 'breaker_log', 'storage_unavailable', 'ready'], {},
        [('alert', 2), ('recovery', 4)]),
    'watcher-restart-keeps-dedupe': (
        ['storage_unavailable'] * 16 + ['ready'], {'restart_after': 13}, [('alert', 10), ('recovery', 16)]),
    'full-disk-still-alerts-once': (
        ['breaker'] * 4 + ['ready', 'ready'], {'fail_writes': True}, [('alert', 0), ('recovery', 4)]),
    'stale-heartbeat-alerts-once-and-recovers': (
        ['ready'] * 7, {'beats': {0: (0, 150), 5: (290, 2000)}},
        [('heartbeat_alert', 3), ('heartbeat_recovery', 5)]),
    'stale-heartbeat-waits-for-the-storage-alert': (
        ['storage_unavailable'] * 13 + ['ready'] * 5, {'beats': {0: (0, 120)}},
        [('alert', 10), ('recovery', 13), ('heartbeat_alert', 16)]),
    # A planned restart with a 690 s WAL replay at reopen (GATE-R3, 2026-11-13): never a storage outage.
    'planned-restart-overrun-says-so-once': (
        ['ready'] + ['planned_down'] * 2 + ['planned_starting'] * 10 + ['ready', 'ready'],
        {'restart': {'startedAt': -60, 'exitAt': 30}}, [('overrun', 11), ('recovery', 13)]),
    'planned-restart-inside-ten-minutes-is-silent': (
        ['ready'] + ['planned_down'] * 2 + ['planned_starting'] * 8 + ['ready'],
        {'restart': {'startedAt': -60, 'exitAt': 30}}, []),
    'failed-planned-restart-alerts-at-once': (
        ['ready', 'planned_down', 'planned_down', 'planned_paused', 'planned_paused', 'ready'],
        {'restart': {'startedAt': -60, 'exitAt': 30}}, [('alert', 3), ('recovery', 5)]),
    # 2026-11-13 00:22-00:35: probes timed out under load while the manager's own check passed.
    'slow-api-with-a-ready-manager-is-not-an-outage': (['ready'] + ['slow'] * 13 + ['ready'], {}, []),
}


def simulate(names: list[str]) -> int:
    unknown = sorted(set(names) - set(SCENARIOS))
    if unknown:
        print(f'unknown scenario(s): {", ".join(unknown)}; known: {", ".join(SCENARIOS)}', file=sys.stderr)
        return 2
    failures = 0
    start = time.mktime(time.strptime('2026-11-12 06:00:00', '%Y-%m-%d %H:%M:%S'))
    for name, (timeline, options, expected) in SCENARIOS.items():
        if names and name not in names:
            continue
        with tempfile.TemporaryDirectory(prefix='oob-alert-simulate-') as root:
            delivered = run_scenario(timeline, root=Path(root), start=start, **options)
        got = [(item['role'], item['minute']) for item in delivered]
        failures += got != expected
        print(f'{"PASS" if got == expected else "FAIL"} {name}: deliveries {got} (expected {expected})')
        for item in delivered:
            print('  ' + item['text'].replace('\n', '\n  '))
    return 1 if failures else 0


# ── process control ─────────────────────────────────────────────────────

def _is_watcher(pid: int, state_dir: Path) -> bool:
    """Whether ``pid`` is this module's watcher for ``state_dir`` (guards against a recycled PID)."""
    try:
        parts = [part.decode('utf-8', 'replace') for part in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')]
    except OSError:
        return False
    ours = any(part.endswith('oob_alert.py') or part == 'serverctl_pkg.oob_alert' for part in parts)
    same_dir = any(part.startswith(('/', '~')) and Path(part).expanduser().resolve() == state_dir
                   for part in parts)
    return ours and same_dir


def _read_pid(state_dir: Path) -> int | None:
    try:
        return int((state_dir / 'watcher.pid').read_text().strip())
    except (OSError, ValueError):
        return None


def _source_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def command_start(state_dir: Path) -> int:
    """Start the detached watcher; return once it logged ``watcher_started``, or report why it did not.

    A readiness pipe replaces any wait: the child writes its source digest
    right after its start event, and EOF without one means the child exited
    (another watcher holds the lock, the config is unreadable). The child
    keeps the caller's niceness, its own session and ``watcher.out``.
    """
    _private_dir(state_dir)
    read_fd, write_fd = os.pipe()
    with (state_dir / 'watcher.out').open('ab') as out:
        child = subprocess.Popen(
            [sys.executable, '-I', str(Path(__file__).resolve()), '--state-dir', str(state_dir), 'run',
             '--ready-fd', str(write_fd)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, pass_fds=(write_fd,),
            start_new_session=True, cwd='/')
    os.close(write_fd)
    with os.fdopen(read_fd, 'rb') as ready:
        digest = ready.read().decode('ascii', 'replace').strip()
    if not digest:
        code = child.wait()
        try:
            lines = (state_dir / 'watcher.out').read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            lines = []
        reason = next((line for line in reversed(lines) if line.strip()), 'no output')
        print(f'watcher did not start (exit {code}): {reason}', file=sys.stderr)
        return 1
    print(f'watcher started: pid {child.pid}, source sha256 {digest}')
    return 0


def command_run(config: Config, state_dir: Path, *, ready_fd: int | None = None) -> int:
    _private_dir(state_dir)
    lock = (state_dir / 'watcher.lock').open('a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f'another watcher holds {state_dir / "watcher.lock"} (pid {_read_pid(state_dir)})', file=sys.stderr)
        return 1
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    watcher = Watcher(config, state_dir)
    try:
        (state_dir / 'watcher.pid').write_text(f'{os.getpid()}\n')
    except OSError as error:  # the watcher still runs; stop and status need the pid file
        print(f'watcher.pid not written ({error}); running as pid {os.getpid()}', file=sys.stderr)
    digest = _source_digest()
    watcher.event('watcher_started', pid=os.getpid(), sender=watcher.sender.name,
                  source=str(Path(__file__).resolve()), sha256=digest)
    if watcher.repair_unavailable:
        watcher.event('repair_unavailable', reason=watcher.repair_unavailable)
    if ready_fd is not None:  # command_start waits for exactly this
        try:
            os.write(ready_fd, digest.encode('ascii'))
        except OSError:
            pass  # the starter is gone; the watcher still runs
        finally:
            os.close(ready_fd)
    try:
        watcher.start_relay()
        watcher.run(stop)
    finally:
        watcher.close()
        watcher.event('watcher_stopped', pid=os.getpid())
        if _read_pid(state_dir) == os.getpid():
            (state_dir / 'watcher.pid').unlink(missing_ok=True)
    return 0


def command_stop(state_dir: Path) -> int:
    pid = _read_pid(state_dir)
    if pid is None or not _is_watcher(pid, state_dir):
        print('watcher is not running')
        return 0
    os.kill(pid, signal.SIGTERM)  # the watcher's own verified PID, never a Tofu process
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _is_watcher(pid, state_dir):
        time.sleep(0.2)
    alive = _is_watcher(pid, state_dir)
    print(f'watcher pid {pid} {"is still stopping" if alive else "stopped"}')
    return 1 if alive else 0


def command_status(state_dir: Path) -> int:
    pid = _read_pid(state_dir)
    alive = pid is not None and _is_watcher(pid, state_dir)
    state = load_state(state_dir)
    try:
        sender = configured_sender(Config.load(state_dir)).name
    except (OSError, KeyError, ValueError) as error:
        sender = f'config unreadable ({type(error).__name__})'
    print(f'watcher: {f"running, pid {pid}" if alive else "NOT running"}; delivery path: {sender}; '
          f'source sha256 {_source_digest()}')
    last = state.get('lastProbe')
    if last:
        print(f'last probe {_iso(last["at"])} ({int(time.time() - last["at"])}s ago): '
              f'{last["kind"]}: {last["detail"]}')
    incident = state.get('incident')
    if incident:
        print(f'open incident {incident["id"]} since {_iso(incident["openedAt"])}: {incident["kind"]}; '
              f'latched: {incident.get("latched") or "no"}; alert: {incident.get("alert")}')
        planned = incident.get('planned')
        if planned:
            print(f'  caused by the planned restart of {_iso(planned["startedAt"])} ({planned.get("source")}, '
                  f'manager outcome {planned.get("outcome")}); overrun notice: {incident.get("overrun")}')
    else:
        print('no open incident')
    if state.get('recovery'):
        print(f'recovery notice: {state["recovery"]["state"]}')
    failures = state.get('writeFailures')
    if failures:
        print(f'local writes failing: {failures["count"]} (last: {failures["last"]})')
    try:
        age = time.time() - (state_dir / 'state.json').stat().st_mtime
    except OSError:
        age = None
    if alive and age is not None and age > 3 * PROBE_INTERVAL_S:
        print(f'state.json last written {int(age)}s ago: local writes are failing')
    for owner, record in sorted((state.get('heartbeats') or {}).items()):
        print(f'watchdog heartbeat owner {owner}: stale since the review of {_iso(record["heartbeatAt"])}; '
              f'alert: {record.get("alert")}')
    trip = _read_trip_alert(state_dir)
    if trip:
        print(f'last breaker-trip notification {_iso(trip["at"])}: {trip.get("outcome")} via {trip.get("path")}')
    marker = state_dir / 'test-message.json'
    if marker.exists():
        print(f'test message: {marker.read_text(encoding="utf-8").strip()}')
    return 0 if alive else 3


def command_test_message(config: Config, state_dir: Path) -> int:
    """Send the one labelled test message through the enabled path, at most once per state directory."""
    sender = configured_sender(config)
    if isinstance(sender, NoSender):
        print('refused: no delivery path is enabled in config.json', file=sys.stderr)
        return 1
    try:  # O_EXCL claims the single attempt before anything is sent
        descriptor = os.open(state_dir / 'test-message.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print('refused: the test message was already attempted (test-message.json)', file=sys.stderr)
        return 1
    # The test never reads or writes state.json: incident and dedup state stay untouched.
    text = test_text(config, time.time())
    outcome, detail = sender.send(text)
    record = {'at': _iso(time.time()), 'outcome': outcome, 'path': sender.name, 'detail': detail, 'text': text}
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False)
    _append(state_dir / 'events.jsonl', {**record, 'event': f'test_message_{outcome}'})
    print(f'test message {outcome}: {detail}\n--- text ---\n{text}')
    return 0 if outcome == 'sent' else 1


def trip_alert_text(at: float, action: str) -> str:
    """The breaker-trip alert, in the incident alert's format (the reason stays in trip-alert.json)."""
    return '\n'.join([f'【Tofu 停机告警】存储不可用（自 {_clock(at)} 起，重启熔断已触发，仍未恢复）。',
                      action, _FOOTER])


def notify_trip(reason: str, *, data_dir: Path, state_dir: Path = DEFAULT_STATE_DIR) -> bool:
    """Hand a storage breaker trip to the out-of-band alert at once; never blocks the caller.

    Starts a detached ``notify`` child of this file (own session, no
    inherited descriptors, reaped by a daemon thread) and returns whether it
    started. The child sends only when the watcher is configured for
    ``data_dir``, so a trip in a disposable test authority never reaches the
    owner. No configured watcher means nothing to notify.
    """
    if not (state_dir / 'config.json').is_file():
        return False
    try:
        child = subprocess.Popen(
            [sys.executable, '-I', str(Path(__file__).resolve()), '--state-dir', str(state_dir), 'notify',
             '--data-dir', str(data_dir), '--reason', reason],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
    except OSError:
        return False
    threading.Thread(target=child.wait, name='oob-alert-notify-reaper', daemon=True).start()
    return True


def command_notify(config: Config, state_dir: Path, *, reason: str, data_dir: Path) -> int:
    """Alert one breaker trip now, unless this outage was already alerted."""
    if data_dir.resolve() != (config.tofu_root / 'data').resolve():
        return 0  # another storage authority (a disposable test store) is never the owner's outage
    at = time.time()
    with _AlertLock(state_dir):
        incident = load_state(state_dir).get('incident') or {}
        if (incident.get('alert') or {}).get('state') in _DELIVERED:
            _append(state_dir / 'events.jsonl', {'at': _iso(at), 'event': 'trip_alert_skipped',
                                                 'incident': incident['id'], 'reason': reason})
            return 0
        sender = configured_sender(config)
        text = trip_alert_text(at, action_line(config))
        outcome, detail = sender.send(text)
        record = {'at': at, 'reason': reason, 'outcome': outcome, 'path': sender.name, 'detail': detail}
        _write_json(state_dir / 'trip-alert.json', record)
        _append(state_dir / 'events.jsonl', {**record, 'at': _iso(at), 'event': f'trip_alert_{outcome}', 'text': text})
    return 0


def command_preflight(config: Config) -> int:
    """Prove the enabled delivery path could send now, without sending anything."""
    passed, detail = configured_sender(config).preflight()
    print(f'preflight {"PASS" if passed else "FAIL"}: {detail}')
    return 0 if passed else 1


def command_once(config: Config, *, inject: str | None, fail_writes: bool = False) -> int:
    """One dry-run tick in a scratch state directory: real probe and logs, nothing sent."""
    with tempfile.TemporaryDirectory(prefix='oob-alert-once-') as scratch:
        sender = DryRunSender()
        probe_fn = (lambda: injected_probe(inject, time.time())) if inject else None
        watcher = Watcher(config, Path(scratch), sender=sender, probe_fn=probe_fn, fail_writes=fail_writes)
        if watcher.repair_unavailable:
            print(f'repair layer unavailable: {watcher.repair_unavailable}')
        elif watcher.repairs is not None:  # a dry run starts no session and reveals no credential
            print('repair layer: not exercised by a dry run')
            watcher.repairs = None
        watcher.dry_run = True
        outcomes = watcher.tick()
        if fail_writes:
            print(f'local writes failed (injected ENOSPC): {watcher.state["writeFailures"]["count"]}')
        last = watcher.state['lastProbe']
        print(f'probe: {last["kind"]}: {last["detail"]}')
        incident = watcher.state.get('incident')
        print(f'incident: {incident["id"] + " latched=" + repr(incident["latched"]) if incident else "none"}')
        for item in outcomes:
            print(f'{item["role"]} -> {item["outcome"]} ({item["detail"]})\n  ' + item['text'].replace('\n', '\n  '))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Out-of-band Tofu outage alert watcher')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='probe every minute until SIGTERM')
    run.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)  # set by start
    commands.add_parser('start', help='start the detached watcher and wait until it logged watcher_started')
    once = commands.add_parser('once', help='one dry-run tick in a scratch state directory; nothing is sent')
    once.add_argument('--inject', choices=('storage_unavailable', 'breaker', 'api_unanswered', 'ready'),
                      help='replace the probe answer with a simulated one')
    once.add_argument('--fail-writes', action='store_true',
                      help='every local write fails with ENOSPC (a full disk); the alert must still go out')
    commands.add_parser('preflight', help='prove the enabled delivery path could send, without sending')
    simulated = commands.add_parser('simulate', help='dry-run scenarios on a simulated clock')
    simulated.add_argument('names', nargs='*', help=f'scenarios (default all): {", ".join(SCENARIOS)}')
    commands.add_parser('status', help='the watcher, its last probe and the open incident')
    commands.add_parser('stop', help='SIGTERM the running watcher (its own verified PID only)')
    commands.add_parser('test-message', help='send the one labelled test message')
    notify = commands.add_parser('notify', help='alert one storage breaker trip now (started by notify_trip)')
    notify.add_argument('--reason', required=True)
    notify.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    if args.command == 'simulate':
        return simulate(args.names)
    if args.command == 'status':
        return command_status(state_dir)
    if args.command == 'stop':
        return command_stop(state_dir)
    if args.command == 'start':
        return command_start(state_dir)
    config = Config.load(state_dir)
    if args.command == 'run':
        return command_run(config, state_dir, ready_fd=args.ready_fd)
    if args.command == 'test-message':
        return command_test_message(config, state_dir)
    if args.command == 'notify':
        return command_notify(config, state_dir, reason=args.reason, data_dir=args.data_dir)
    if args.command == 'preflight':
        return command_preflight(config)
    return command_once(config, inject=args.inject, fail_writes=args.fail_writes)


if __name__ == '__main__':
    sys.exit(main())
