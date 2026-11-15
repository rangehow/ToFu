#!/usr/bin/env python3
"""Responsibility: long-running browser soak of the Tofu UI that records renderer
health until the tab crashes or the run ends.

Entry: ``python3 scripts/browser_crash_repro/soak.py --chrome PATH --url URL --out DIR``
(``--summarize DIR`` rebuilds ``summary.json`` from a finished run).
Dependencies: a Chromium/Chrome binary, the ``websockets`` package and ``psutil``.

The harness speaks raw CDP so it never enables the Network or Runtime domains on
the page: those make DevTools retain response bodies and console arguments and
would themselves grow renderer memory. Point it at production only through
``readonly_proxy.mjs`` (writes never reach the server). It stores metrics, not
page content: no screenshots, DOM text or heap snapshots unless asked for.

Recorded per sample: CDP Performance metrics, ``Runtime.getHeapUsage`` (V8 and
Oilpan/embedder heaps), DOM counters, RSS/PSS of every Chrome process,
long-animation-frame breakdown (Tofu script vs extension/unattributed vs
style+layout), long tasks and DOM mutation rate. Periodic CPU-profile windows
attribute main-thread self time to Tofu bundles, extension content scripts,
GC and native work. ``Target.targetCrashed`` gives the sad-tab error code.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import websockets

CHECKPOINTS_S = (300, 900, 1800, 3600, 7200, 14400, 21600, 28800, 36000)

INSTRUMENT_JS = r"""
(() => {
  if (window.__soak) return;
  const seed = %(seed)s;
  try {
    if (seed && !sessionStorage.getItem('__soak_seeded')) {
      sessionStorage.setItem('__soak_seeded', '1');
      localStorage.setItem(seed.key, seed.value);
    }
  } catch (e) {}
  const origin = location.origin;
  const sourceClass = (url) => {
    if (!url) return '(none)';
    if (url.startsWith('chrome-extension://')) return 'ext:' + url.slice(19).split('/')[0];
    try { const parsed = new URL(url); return parsed.origin === origin ? 'tofu' : 'other:' + parsed.protocol + parsed.host; }
    catch (e) { return 'other:' + url.slice(0, 40); }
  };
  const frames = { n: 0, total: 0, blocking: 0, script: 0, styleLayout: 0, forcedLayout: 0,
    unattributed: 0, max: 0, over: { '250': 0, '1000': 0, '5000': 0, '20000': 0 } };
  const bySource = {};
  const byScript = {};
  const worst = [];
  const longtasks = { n: 0, total: 0, max: 0 };
  const mutations = { records: 0, added: 0, removed: 0 };
  const stream = { events: 0, bytes: 0, sources: 0, byType: {} };
  window.__soak = { frames, bySource, byScript, worst, longtasks, mutations, stream };
  if (window.EventSource) {
    // Count with a separate listener per (source, type): wrapping the page's
    // listener would make long-animation-frame attribute its time to this
    // script instead of the page's bundle.
    const nativeAdd = EventSource.prototype.addEventListener;
    const counting = new WeakMap();
    const count = (event) => {
      const row = stream.byType[event.type] || (stream.byType[event.type] = { n: 0, bytes: 0 });
      const size = typeof event.data === 'string' ? event.data.length : 0;
      row.n += 1; row.bytes += size; stream.events += 1; stream.bytes += size;
    };
    EventSource.prototype.addEventListener = function (type, listener, options) {
      let types = counting.get(this);
      if (!types) { types = new Set(); counting.set(this, types); stream.sources += 1; }
      if (!types.has(type)) { types.add(type); nativeAdd.call(this, type, count); }
      return nativeAdd.call(this, type, listener, options);
    };
  }
  const onFrame = (entry) => {
    const end = entry.startTime + entry.duration;
    const styleLayout = entry.styleAndLayoutStart > 0 ? Math.max(0, end - entry.styleAndLayoutStart) : 0;
    let script = 0, forced = 0;
    const scripts = [];
    for (const s of entry.scripts || []) {
      script += s.duration; forced += s.forcedStyleAndLayoutDuration || 0;
      const cls = sourceClass(s.sourceURL);
      const row = bySource[cls] || (bySource[cls] = { ms: 0, n: 0 }); row.ms += s.duration; row.n += 1;
      const key = cls + '|' + s.invokerType + '|' + (s.sourceFunctionName || '') + '|' + (s.sourceCharPosition ?? '');
      if (byScript[key] || Object.keys(byScript).length < 300) {
        const r = byScript[key] || (byScript[key] = { ms: 0, n: 0, max: 0 }); r.ms += s.duration; r.n += 1; if (s.duration > r.max) r.max = s.duration;
      }
      scripts.push({ cls, inv: s.invokerType, fn: (s.sourceFunctionName || '').slice(0, 40), pos: s.sourceCharPosition, ms: Math.round(s.duration), forced: Math.round(s.forcedStyleAndLayoutDuration || 0) });
    }
    const unattributed = Math.max(0, entry.duration - script - styleLayout);
    frames.n += 1; frames.total += entry.duration; frames.blocking += entry.blockingDuration || 0;
    frames.script += script; frames.styleLayout += styleLayout; frames.forcedLayout += forced; frames.unattributed += unattributed;
    if (entry.duration > frames.max) frames.max = entry.duration;
    for (const limit of [250, 1000, 5000, 20000]) if (entry.duration >= limit) frames.over[limit] += 1;
    if (entry.duration >= 250) {
      scripts.sort((a, b) => b.ms - a.ms);
      worst.push({ atMs: Math.round(performance.timeOrigin + entry.startTime), ms: Math.round(entry.duration),
        blocking: Math.round(entry.blockingDuration || 0), script: Math.round(script), styleLayout: Math.round(styleLayout),
        unattributed: Math.round(unattributed), scripts: scripts.slice(0, 6) });
      worst.sort((a, b) => b.ms - a.ms);
      if (worst.length > 40) worst.length = 40;
    }
  };
  try { new PerformanceObserver((list) => list.getEntries().forEach(onFrame)).observe({ type: 'long-animation-frame', buffered: true }); } catch (e) {}
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) { longtasks.n += 1; longtasks.total += e.duration; if (e.duration > longtasks.max) longtasks.max = e.duration; }
    }).observe({ type: 'longtask', buffered: true });
  } catch (e) {}
  const observe = () => new MutationObserver((records) => {
    mutations.records += records.length;
    for (const r of records) { mutations.added += r.addedNodes.length; mutations.removed += r.removedNodes.length; }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true });
  if (%(count_mutations)s) { if (document.documentElement) observe(); else document.addEventListener('DOMContentLoaded', observe); }
  window.__soakTake = () => JSON.stringify({
    frames, bySource, longtasks, mutations, stream, worst: worst.slice(0, 12),
    byScript: Object.entries(byScript).sort((a, b) => b[1].ms - a[1].ms).slice(0, 15),
    visibility: document.visibilityState,
    elements: document.getElementsByTagName('*').length,
    chatElements: (document.getElementById('chatInner') || { getElementsByTagName: () => [] }).getElementsByTagName('*').length,
    turns: document.querySelectorAll('[data-turn-id]').length,
    iframes: document.getElementsByTagName('iframe').length,
    activeConversation: (() => { try { return (localStorage.getItem('tofu_lastActiveConvId') || '').slice(0, 64); } catch (e) { return null; } })(),
    extensionMarkers: [...document.documentElement.attributes].map(a => a.name).filter(n => n !== 'lang' && n !== 'class' && n !== 'style').slice(0, 12),
    extensionNodes: document.querySelectorAll('[class*="immersive-translate"], [data-immersive-translate-walked], [data-immersive-translate-paragraph], grammarly-desktop-integration, grammarly-extension, [data-gr-ext-installed]').length,
  });
})();
"""

# Opens the newest closed disclosures (tool panels, command folds, thinking
# blocks) the way the owner reads a live turn; menus (aria-haspopup) are skipped.
EXPAND_JS = r"""
(() => {
  const root = document.getElementById('chatInner') || document.body;
  const targets = [...root.querySelectorAll('details:not([open]) > summary, [aria-expanded="false"]:not([aria-haspopup])')];
  let n = 0;
  for (const el of targets.slice(-30)) { try { el.click(); n += 1; } catch (e) {} }
  const scroller = document.getElementById('chatContainer') || document.scrollingElement;
  if (scroller) { scroller.scrollTop = Math.max(0, scroller.scrollTop - 4000); setTimeout(() => { scroller.scrollTop = scroller.scrollHeight; }, 3000); }
  return n;
})()
"""

SWITCH_JS = r"""
(() => {
  const load = window.TofuModules && window.TofuModules.resolveAction('loadConversation');
  if (typeof load !== 'function') return 'no-loadConversation';
  load(%s);
  return 'ok';
})()
"""


class CdpError(RuntimeError):
    pass


class Cdp:
    """Minimal flat-session CDP client over the browser WebSocket."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, list] = defaultdict(list)
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            message = json.loads(raw)
            if 'id' in message:
                future = self.pending.pop(message['id'], None)
                if future and not future.done():
                    future.set_result(message)
                continue
            for handler in self.handlers.get(message.get('method', ''), []):
                handler(message.get('params', {}), message.get('sessionId'))

    def on(self, method, handler):
        self.handlers[method].append(handler)

    async def send(self, method, params=None, session=None, timeout=60.0):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.ws.send(json.dumps(message))
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(message['id'], None)
        if 'error' in reply:
            raise CdpError(f"{method}: {reply['error'].get('message')}")
        return reply.get('result', {})


def _own_session_dying_with_parent():
    """Chrome gets its own process group (killpg on shutdown) but must not outlive
    a harness that is killed hard: ask Linux to SIGTERM it when we die."""
    os.setsid()
    if sys.platform.startswith('linux'):
        import ctypes
        ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def chrome_command(args, user_data_dir: Path) -> list[str]:
    command = [args.chrome, f'--user-data-dir={user_data_dir}', '--remote-debugging-port=0',
               '--no-first-run', '--no-default-browser-check', '--disable-component-update',
               '--ignore-certificate-errors', f'--window-size={args.window}',
               f'--force-device-scale-factor={args.dpr}', '--enable-logging=stderr', f'--v={args.verbose_log}',
               '--enable-precise-memory-info']
    if args.headless:
        command.append('--headless=new')
    if args.extension:
        joined = ','.join(str(Path(e).resolve()) for e in args.extension)
        command += [f'--load-extension={joined}', f'--disable-extensions-except={joined}']
    command += list(args.chrome_arg or [])
    command.append('about:blank')
    return command


def chrome_processes(browser_pid: int) -> list[dict]:
    try:
        root = psutil.Process(browser_pid)
        processes = [root] + root.children(recursive=True)
    except psutil.Error:
        return []
    rows = []
    for process in processes:
        try:
            cmdline = process.cmdline()
            kind = 'browser'
            for part in cmdline:
                if part.startswith('--type='):
                    kind = part[7:]
            if kind == 'renderer' and '--extension-process' in cmdline:
                kind = 'extension-renderer'
            info = process.memory_full_info()
            rows.append({'pid': process.pid, 'type': kind, 'rssMB': round(info.rss / 2**20, 1),
                         'pssMB': round(getattr(info, 'pss', 0) / 2**20, 1),
                         'cpuS': round(sum(process.cpu_times()[:2]), 1)})
        except psutil.Error:
            continue
    return rows


def classify_url(url: str, origin: str) -> str:
    if not url:
        return ''
    if url.startswith('chrome-extension://'):
        return 'ext:' + url[19:].split('/')[0]
    parts = urlsplit(url)
    return 'tofu' if f'{parts.scheme}://{parts.netloc}' == origin else f'other:{parts.scheme}://{parts.netloc}'


def summarize_profile(profile: dict, origin: str) -> dict:
    nodes = {node['id']: node for node in profile.get('nodes', [])}
    self_us: dict[int, float] = defaultdict(float)
    for node_id, delta in zip(profile.get('samples', []), profile.get('timeDeltas', [])):
        self_us[node_id] += delta
    by_class: dict[str, float] = defaultdict(float)
    by_function: dict[tuple, float] = defaultdict(float)
    for node_id, micros in self_us.items():
        frame = nodes.get(node_id, {}).get('callFrame', {})
        name = frame.get('functionName') or '(anonymous)'
        cls = classify_url(frame.get('url', ''), origin) or (name if name.startswith('(') else '(native)')
        by_class[cls] += micros
        by_function[(cls, name[:60], frame.get('url', '').rsplit('/', 1)[-1][:60], frame.get('lineNumber'),
                     frame.get('columnNumber'))] += micros
    total = sum(by_class.values()) or 1.0
    span_ms = (profile.get('endTime', 0) - profile.get('startTime', 0)) / 1000
    return {
        'spanMs': round(span_ms),
        'byClassMs': {k: round(v / 1000) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
        'byClassPct': {k: round(100 * v / total, 1) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
        'topFunctions': [
            {'cls': k[0], 'fn': k[1], 'file': k[2], 'line': k[3], 'col': k[4], 'ms': round(v / 1000)}
            for k, v in sorted(by_function.items(), key=lambda kv: -kv[1])[:30]
            if k[1] not in ('(idle)', '(program)', '(root)')
        ],
    }


class Soak:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.samples = (self.out / 'samples.jsonl').open('a')
        self.events = (self.out / 'events.jsonl').open('a')
        self.started = time.time()
        self.crashes: list[dict] = []
        self.page_session = None
        self.target_id = None
        self.stop = asyncio.Event()
        self.origin = '{0.scheme}://{0.netloc}'.format(urlsplit(args.url))
        self.last_metrics: dict = {}
        self.hider_target = None

    def elapsed(self) -> float:
        return round(time.time() - self.started, 1)

    def event(self, kind: str, **fields):
        row = {'t': self.elapsed(), 'wall': time.strftime('%Y-%m-%d %H:%M:%S'), 'event': kind, **fields}
        self.events.write(json.dumps(row) + '\n')
        self.events.flush()
        print(json.dumps(row)[:400], flush=True)

    async def run(self):
        user_data_dir = Path(self.args.user_data_dir or tempfile.mkdtemp(prefix='soak-profile-', dir=self.out))
        command = chrome_command(self.args, user_data_dir)
        stderr = (self.out / 'chrome-stderr.log').open('ab')
        self.chrome = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr,
                                       preexec_fn=_own_session_dying_with_parent)
        version_file = user_data_dir / 'DevToolsActivePort'
        for _ in range(300):
            if version_file.exists() and len(version_file.read_text().splitlines()) >= 2:
                break
            await asyncio.sleep(0.1)
        port, path = version_file.read_text().splitlines()[:2]
        async with websockets.connect(f'ws://127.0.0.1:{port}{path}', max_size=None,
                                      ping_interval=None) as ws:
            self.cdp = Cdp(ws)
            version = await self.cdp.send('Browser.getVersion')
            self.event('start', browser=version.get('product'), userAgent=version.get('userAgent'),
                       url=self.args.url, extensions=[Path(e).name for e in self.args.extension or []],
                       command=[c for c in command if not c.startswith('--user-data-dir')])
            self.cdp.on('Target.targetCrashed', self.on_crash)
            await self.cdp.send('Target.setDiscoverTargets', {'discover': True})
            await asyncio.sleep(self.args.extension_settle if self.args.extension else 0)
            if self.args.extension:
                targets = await self.cdp.send('Target.getTargets')
                self.event('extension-targets', targets=[{'type': t['type'], 'url': t['url'][:80]}
                                                         for t in targets['targetInfos'] if t['url'].startswith('chrome-extension://')])
            for spec in self.args.extension_eval or []:
                await self.extension_eval(spec)
            await self.open_page()
            await asyncio.gather(self.sample_loop(), self.process_loop(), self.profile_loop(), self.interaction_loop(),
                                 self.hidden_period())
        stderr.close()

    async def extension_eval(self, spec: str):
        """Run ``ID=FILE`` JS in that extension's service worker (e.g. set its settings)."""
        extension_id, _, path = spec.partition('=')
        source = Path(path).read_text()
        targets = (await self.cdp.send('Target.getTargets'))['targetInfos']
        worker = next((t for t in targets if t['type'] == 'service_worker'
                       and t['url'].startswith(f'chrome-extension://{extension_id}/')), None)
        if worker is None:
            self.event('extension-eval', extension=extension_id, result='no running service worker')
            return
        session = (await self.cdp.send('Target.attachToTarget', {'targetId': worker['targetId'], 'flatten': True}))['sessionId']
        try:
            result = await self.cdp.send('Runtime.evaluate', {'expression': source, 'awaitPromise': True,
                                                              'returnByValue': True}, session, timeout=60)
            self.event('extension-eval', extension=extension_id, file=Path(path).name,
                       result=str(result.get('result', {}).get('value'))[:300],
                       exception=str(result.get('exceptionDetails', ''))[:300] or None)
        finally:
            with contextlib.suppress(CdpError):
                await self.cdp.send('Target.detachFromTarget', {'sessionId': session})

    async def open_page(self):
        created = await self.cdp.send('Target.createTarget', {'url': 'about:blank'})
        self.target_id = created['targetId']
        attached = await self.cdp.send('Target.attachToTarget', {'targetId': self.target_id, 'flatten': True})
        self.page_session = attached['sessionId']
        seed = ({'key': 'tofu_lastActiveConvId', 'value': self.args.conversation}
                if self.args.conversation else None)
        source = INSTRUMENT_JS % {'seed': json.dumps(seed), 'count_mutations': 'true' if self.args.count_mutations else 'false'}
        await self.cdp.send('Page.enable', {}, self.page_session)
        await self.cdp.send('Page.addScriptToEvaluateOnNewDocument', {'source': source}, self.page_session)
        if self.args.cpu_throttle > 1:
            # Emulates a slower or busier machine: does the page keep up with the
            # stream, or does work queue up until frames and memory escalate?
            await self.cdp.send('Emulation.setCPUThrottlingRate', {'rate': self.args.cpu_throttle}, self.page_session)
        await self.cdp.send('Performance.enable', {'timeDomain': 'timeTicks'}, self.page_session)
        await self.cdp.send('HeapProfiler.enable', {}, self.page_session)
        await self.cdp.send('Page.navigate', {'url': self.args.url}, self.page_session)
        self.event('navigate', url=self.args.url, conversation=self.args.conversation)
        await self.close_foreign_pages()

    async def close_foreign_pages(self):
        """Keep the soak page the only tab (extensions open welcome/onboarding tabs)."""
        for target in (await self.cdp.send('Target.getTargets'))['targetInfos']:
            if target['type'] == 'page' and target['targetId'] not in (self.target_id, self.hider_target):
                with contextlib.suppress(CdpError):
                    await self.cdp.send('Target.closeTarget', {'targetId': target['targetId']})
                if target['url'] != 'about:blank':
                    self.event('closed-foreign-tab', url=target['url'][:100])

    def on_crash(self, params, _session):
        if params.get('targetId') != self.target_id:
            return
        row = {'status': params.get('status'), 'errorCode': params.get('errorCode')}
        self.crashes.append({'t': self.elapsed(), **row})
        self.event('renderer-crash', **row, stderrTail=self.stderr_tail())
        if not self.args.reload_after_crash or len(self.crashes) >= self.args.max_crashes:
            self.stop.set()
        else:
            asyncio.get_running_loop().create_task(self.reload_after_crash())

    async def reload_after_crash(self):
        await asyncio.sleep(10)
        with contextlib.suppress(Exception):
            await self.cdp.send('Page.reload', {}, self.page_session, timeout=30)
            self.event('reload-after-crash')

    def stderr_tail(self) -> list[str]:
        try:
            data = (self.out / 'chrome-stderr.log').read_bytes()[-20000:].decode('utf8', 'replace')
        except OSError:
            return []
        keep = [line for line in data.splitlines()
                if any(word in line for word in ('FATAL', 'Check failed', 'OOM', 'out of memory', 'Received signal',
                                                  'crash', 'Crash', 'renderer', 'NOTREACHED'))]
        return keep[-25:]

    async def page_eval(self, expression: str, timeout: float):
        result = await self.cdp.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True},
                                     self.page_session, timeout=timeout)
        return result.get('result', {}).get('value')

    async def sample_loop(self):
        next_gc = self.args.gc_every
        while not self.stop.is_set() and self.elapsed() < self.args.duration:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), self.args.sample_every)
            if self.stop.is_set():
                break
            row = {'t': self.elapsed(), 'wall': time.strftime('%H:%M:%S')}
            began = time.monotonic()
            try:
                row['page'] = json.loads(await self.page_eval('window.__soakTake ? window.__soakTake() : "null"',
                                                              self.args.probe_timeout) or 'null')
                row['probeMs'] = round((time.monotonic() - began) * 1000)
                metrics = await self.cdp.send('Performance.getMetrics', {}, self.page_session, timeout=self.args.probe_timeout)
                row['metrics'] = {m['name']: m['value'] for m in metrics['metrics']}
                row['heap'] = await self.cdp.send('Runtime.getHeapUsage', {}, self.page_session, timeout=self.args.probe_timeout)
                row['dom'] = await self.cdp.send('Memory.getDOMCounters', {}, self.page_session, timeout=self.args.probe_timeout)
                if next_gc and self.elapsed() >= next_gc:
                    next_gc = self.elapsed() + self.args.gc_every
                    gc_began = time.monotonic()
                    await self.cdp.send('HeapProfiler.collectGarbage', {}, self.page_session, timeout=120)
                    row['afterGc'] = await self.cdp.send('Runtime.getHeapUsage', {}, self.page_session, timeout=60)
                    row['afterGc']['gcMs'] = round((time.monotonic() - gc_began) * 1000)
                    with contextlib.suppress(CdpError, asyncio.TimeoutError):
                        detached = await self.cdp.send('DOM.getDetachedDomNodes', {}, self.page_session, timeout=120)
                        row['afterGc']['detachedNodes'] = len(detached.get('detachedNodes', []))
            except (asyncio.TimeoutError, CdpError) as error:
                row['probeError'] = f'{type(error).__name__}: {str(error)[:160]}'
                row['probeMs'] = round((time.monotonic() - began) * 1000)
            with contextlib.suppress(asyncio.TimeoutError, CdpError):
                await self.close_foreign_pages()
            row['processes'] = chrome_processes(self.chrome.pid)
            self.samples.write(json.dumps(row) + '\n')
            self.samples.flush()
            self.print_sample(row)
            if self.chrome.poll() is not None:
                self.event('browser-exited', code=self.chrome.returncode)
                self.stop.set()
        self.stop.set()

    async def process_loop(self):
        """Process memory from /proc on its own clock: a frozen page must not stop it."""
        with (self.out / 'processes.jsonl').open('a') as out:
            while not self.stop.is_set() and self.elapsed() < self.args.duration:
                rows = await asyncio.get_running_loop().run_in_executor(None, chrome_processes, self.chrome.pid)
                out.write(json.dumps({'t': self.elapsed(), 'wall': time.strftime('%H:%M:%S'), 'processes': rows}) + '\n')
                out.flush()
                await self.sleep_or_stop(self.args.sample_every)

    def print_sample(self, row):
        metrics = row.get('metrics', {})
        page = row.get('page') or {}
        frames = page.get('frames', {})
        renderers = [p for p in row.get('processes', []) if p['type'] == 'renderer']
        print(json.dumps({
            't': row['t'], 'probeMs': row.get('probeMs'), 'err': row.get('probeError'),
            'heapMB': round(metrics.get('JSHeapUsedSize', 0) / 2**20, 1),
            'embedderMB': round((row.get('heap') or {}).get('embedderHeapUsedSize', 0) / 2**20, 1),
            'rendererRssMB': max([p['rssMB'] for p in renderers] or [0]),
            'nodes': (row.get('dom') or {}).get('nodes'), 'listeners': (row.get('dom') or {}).get('jsEventListeners'),
            'elements': page.get('elements'), 'turns': page.get('turns'),
            'loaf': [frames.get('n'), round(frames.get('max', 0)), frames.get('over')],
            'mut': (page.get('mutations') or {}).get('records'),
            'afterGcMB': round(row['afterGc']['usedSize'] / 2**20, 1) if 'afterGc' in row else None,
        }), flush=True)

    async def profile_loop(self):
        if not self.args.profile_every:
            return
        await self.sleep_or_stop(self.args.profile_first)
        while not self.stop.is_set() and self.elapsed() < self.args.duration:
            try:
                await self.cdp.send('Profiler.enable', {}, self.page_session, timeout=60)
                await self.cdp.send('Profiler.setSamplingInterval', {'interval': 1000}, self.page_session, timeout=60)
                await self.cdp.send('Profiler.start', {}, self.page_session, timeout=60)
                await self.sleep_or_stop(self.args.profile_window)
                result = await self.cdp.send('Profiler.stop', {}, self.page_session, timeout=180)
                await self.cdp.send('Profiler.disable', {}, self.page_session, timeout=60)
                summary = summarize_profile(result['profile'], self.origin)
                self.event('cpu-profile', **summary)
                if self.args.keep_profiles:
                    with gzip.open(self.out / f'profile-{int(self.elapsed())}.cpuprofile.gz', 'wt') as handle:
                        json.dump(result['profile'], handle)
            except (asyncio.TimeoutError, CdpError) as error:
                self.event('cpu-profile-error', error=f'{type(error).__name__}: {str(error)[:160]}')
            await self.sleep_or_stop(self.args.profile_every)

    async def interaction_loop(self):
        visits = [v for v in (self.args.visit or '').split(',') if v]
        if not self.args.interact_every and not visits:
            return
        index = 0
        await self.sleep_or_stop(self.args.interact_every or self.args.dwell)
        while not self.stop.is_set() and self.elapsed() < self.args.duration:
            try:
                if self.args.interact_every:
                    self.event('interact', expanded=await self.page_eval(EXPAND_JS, 60))
                if visits:
                    target = visits[index % len(visits)]
                    index += 1
                    self.event('switch', conversation=target[:24], result=await self.page_eval(SWITCH_JS % json.dumps(target), 60))
            except (asyncio.TimeoutError, CdpError) as error:
                self.event('interact-error', error=str(error)[:160])
            await self.sleep_or_stop(self.args.interact_every or self.args.dwell)

    async def hidden_period(self):
        """Hide the soak tab behind another tab for a while, as when the owner switches away.

        Hidden pages get no animation frames while the stream keeps arriving, so
        anything queued for the next frame accumulates until the tab is shown."""
        if not self.args.hidden_for:
            return
        await self.sleep_or_stop(self.args.hidden_after)
        if self.stop.is_set():
            return
        self.hider_target = (await self.cdp.send('Target.createTarget', {'url': 'about:blank'}))['targetId']
        await self.cdp.send('Target.activateTarget', {'targetId': self.hider_target})
        self.event('tab-hidden', seconds=self.args.hidden_for)
        await self.sleep_or_stop(self.args.hidden_for)
        with contextlib.suppress(CdpError):
            await self.cdp.send('Target.closeTarget', {'targetId': self.hider_target})
        self.hider_target = None
        self.event('tab-visible')

    async def sleep_or_stop(self, seconds: float):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), seconds)

    def shutdown(self):
        with contextlib.suppress(Exception):
            os.killpg(self.chrome.pid, signal.SIGTERM)
            self.chrome.wait(timeout=20)
        with contextlib.suppress(Exception):
            os.killpg(self.chrome.pid, signal.SIGKILL)
        self.samples.close()
        self.events.close()
        summarize(self.out)


def nearest(rows: list[dict], at: float) -> dict | None:
    candidates = [row for row in rows if row['t'] <= at + 60]
    return candidates[-1] if candidates and candidates[-1]['t'] >= at - 120 else None


def summarize(out: Path) -> dict:
    rows = [json.loads(line) for line in (out / 'samples.jsonl').read_text().splitlines() if line.strip()]
    events = [json.loads(line) for line in (out / 'events.jsonl').read_text().splitlines() if line.strip()]
    start = next((e for e in events if e['event'] == 'start'), {})
    checkpoints = []
    previous_frames = {'n': 0, 'total': 0, 'blocking': 0, 'script': 0, 'styleLayout': 0, 'unattributed': 0}
    previous_t = 0.0
    for at in CHECKPOINTS_S:
        row = nearest(rows, at)
        if not row:
            continue
        metrics = row.get('metrics', {})
        frames = (row.get('page') or {}).get('frames') or {}
        window_s = max(1.0, row['t'] - previous_t)
        gc_rows = [r for r in rows if 'afterGc' in r and r['t'] <= row['t']]
        renderer = max([p['rssMB'] for p in row.get('processes', []) if p['type'] == 'renderer'] or [0])
        checkpoints.append({
            't': row['t'],
            'jsHeapMB': round(metrics.get('JSHeapUsedSize', 0) / 2**20, 1),
            'jsHeapAfterGcMB': round(gc_rows[-1]['afterGc']['usedSize'] / 2**20, 1) if gc_rows else None,
            'embedderHeapMB': round((row.get('heap') or {}).get('embedderHeapUsedSize', 0) / 2**20, 1),
            'rendererRssMB': renderer,
            'domNodes': (row.get('dom') or {}).get('nodes'),
            'listeners': (row.get('dom') or {}).get('jsEventListeners'),
            'longFrames>=1s': frames.get('over', {}).get('1000'),
            'blockedMsPerMinSinceLast': round(60 * (frames.get('blocking', 0) - previous_frames.get('blocking', 0)) / window_s),
            'unattributedMsPerMinSinceLast': round(60 * (frames.get('unattributed', 0) - previous_frames.get('unattributed', 0)) / window_s),
            'maxFrameMsSoFar': round(frames.get('max', 0)),
            'crashesSoFar': len([e for e in events if e['event'] == 'renderer-crash' and e['t'] <= row['t']]),
        })
        previous_frames = frames or previous_frames
        previous_t = row['t']
    process_path = out / 'processes.jsonl'
    process_rows = ([json.loads(line) for line in process_path.read_text().splitlines() if line.strip()]
                    if process_path.exists() else [])
    renderer_rss = [p['rssMB'] for r in rows + process_rows for p in r.get('processes', []) if p['type'] == 'renderer']
    profiles = [e for e in events if e['event'] == 'cpu-profile']
    class_ms: dict[str, int] = defaultdict(int)
    for profile in profiles:
        for cls, ms in profile.get('byClassMs', {}).items():
            class_ms[cls] += ms
    summary = {
        'browser': start.get('browser'), 'url': start.get('url'), 'extensions': start.get('extensions'),
        'durationS': rows[-1]['t'] if rows else 0, 'samples': len(rows),
        'crashes': [e for e in events if e['event'] == 'renderer-crash'],
        'maxJsHeapMB': round(max([r.get('metrics', {}).get('JSHeapUsedSize', 0) for r in rows] or [0]) / 2**20, 1),
        'maxRendererRssMB': max(renderer_rss or [0]),
        'probeTimeouts': len([r for r in rows if 'probeError' in r]),
        'checkpoints': checkpoints,
        'cpuProfileSelfMsByClass': dict(sorted(class_ms.items(), key=lambda kv: -kv[1])),
    }
    (out / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--summarize', help='rebuild summary.json for a finished run directory and exit')
    parser.add_argument('--chrome')
    parser.add_argument('--url')
    parser.add_argument('--out')
    parser.add_argument('--conversation', help='seeded as the active conversation before the app boots')
    parser.add_argument('--duration', type=float, default=4 * 3600)
    parser.add_argument('--sample-every', type=float, default=30)
    parser.add_argument('--probe-timeout', type=float, default=90)
    parser.add_argument('--gc-every', type=float, default=900, help='forced-GC heap sample interval; 0 disables')
    parser.add_argument('--profile-every', type=float, default=1200)
    parser.add_argument('--profile-first', type=float, default=240)
    parser.add_argument('--profile-window', type=float, default=60)
    parser.add_argument('--keep-profiles', action='store_true')
    parser.add_argument('--hidden-after', type=float, default=600, help='seconds visible before the hidden period')
    parser.add_argument('--hidden-for', type=float, default=0, help='seconds the tab stays hidden (0 = never)')
    parser.add_argument('--interact-every', type=float, default=0)
    parser.add_argument('--visit', help='comma-separated conversation ids to cycle through')
    parser.add_argument('--dwell', type=float, default=120)
    parser.add_argument('--extension', action='append', help='unpacked extension directory (repeatable)')
    parser.add_argument('--extension-settle', type=float, default=20)
    parser.add_argument('--extension-eval', action='append',
                        help='ID=FILE: evaluate FILE in that extension service worker before the page opens')
    parser.add_argument('--headless', action=argparse.BooleanOptionalAction, default=True,
                        help='--no-headless runs headed (needs an X display such as Xvfb)')
    parser.add_argument('--cpu-throttle', type=float, default=1, help='CDP CPU throttling rate for the page (1 = off)')
    parser.add_argument('--window', default='2000,1050')
    parser.add_argument('--dpr', default='2')
    parser.add_argument('--verbose-log', default='0')
    parser.add_argument('--count-mutations', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--reload-after-crash', action='store_true')
    parser.add_argument('--max-crashes', type=int, default=3)
    parser.add_argument('--user-data-dir')
    parser.add_argument('--chrome-arg', action='append')
    args = parser.parse_args(argv)
    if not args.summarize and not (args.chrome and args.url and args.out):
        parser.error('--chrome, --url and --out are required')
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.summarize:
        print(json.dumps(summarize(Path(args.summarize)), indent=1))
        return 0
    soak = Soak(args)
    loop = asyncio.new_event_loop()
    main_task = loop.create_task(soak.run())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, soak.stop.set)
    try:
        loop.run_until_complete(main_task)
    except Exception as error:  # the run log must survive a harness failure
        soak.event('harness-error', error=f'{type(error).__name__}: {str(error)[:300]}')
    finally:
        soak.shutdown()
        loop.close()
    print(json.dumps(summarize(Path(args.out)), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
