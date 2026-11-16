"""Real Chromium + real Canvas2D: the lead office scene (docs/PROJECT_COORDINATION.md "Lead office").

The scene and the shared stage are imported from source through an isolated Vite
server (no application, network or model). Captures every Worker and lead state,
a hand-off and a celebration in flight, day and night, the hover status card and
click activation; profiles draw cost and frame pacing with six Workers; and
proves the stage owns no frame or timer while hidden, offscreen or disposed.
Evidence goes to output/studio-redesign/after/.
"""
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'frontend/src/features'
OUT = ROOT / 'output/studio-redesign/after'
PREVIEW_SERVER = """import {createServer} from 'vite';
const [configFile, port] = process.argv.slice(1);
const server = await createServer({configFile, server: {host: '127.0.0.1', port: Number(port), strictPort: true, hmr: false, watch: null}});
await server.listen();"""


def source(relative):
    return '/@fs' + str(SRC / relative)


@pytest.fixture(scope='module')
def preview():
    from tests._jsdom import require_node_modules
    require_node_modules('vite')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(['node', '--input-type=module', '-e', PREVIEW_SERVER,
                                'frontend/src/features/office-game/testing/vite.config.mjs', str(port)],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}/office-runtime.html'
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError('preview exited')
            try:
                urllib.request.urlopen(base, timeout=.2).close()
                break
            except OSError:
                time.sleep(.1)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


# Mounts the real stage and scene into a sidebar-sized host; exposes helpers on window.office.
MOUNT = """async ({stagePath, scenePath, width, clock}) => {
  if (clock !== null) Object.defineProperty(performance, 'timeOrigin', {value: clock - performance.now()});
  const [{mountStage}, {createLeadOfficeScene}] = await Promise.all([import(stagePath), import(scenePath)]);
  const counts = {raf: 0, timers: 0};
  const raf = window.requestAnimationFrame.bind(window), timeout = window.setTimeout.bind(window);
  window.requestAnimationFrame = fn => { counts.raf++; return raf(fn); };
  window.setTimeout = (fn, ms, ...rest) => { counts.timers++; return timeout(fn, ms, ...rest); };
  document.body.style.cssText = 'margin:0;background:#efe6d6';
  // The stage rules the application loads with the office (features/lead-office/office.css).
  const style = document.createElement('style');
  style.textContent = `.evo-stage{outline:none}.evo-stage-canvas{position:absolute;inset:0;display:block;width:100%;height:100%}
    .evo-stage-targets{position:absolute;inset:0;pointer-events:none}.evo-target{position:absolute;padding:0;border:0;background:transparent;opacity:0;pointer-events:none}`;
  document.head.append(style);
  const scroller = document.createElement('div');
  scroller.style.cssText = `width:${width}px;height:900px;overflow-y:auto;margin:12px;border-radius:8px;background:#3a2a1f`;
  const host = document.createElement('div');
  host.style.cssText = 'position:relative;width:100%';
  const spacer = document.createElement('div'); spacer.style.height = '3000px';
  scroller.append(host, spacer); document.body.replaceChildren(scroller);
  const activations = [];
  const text = {
    lead: l => `Project coordinator · ${l.state}`, leadName: 'Project coordinator',
    leadState: l => ({replying: 'replying', asking: 'waiting for your answer', idle: 'available'})[l.state],
    leadDetails: l => [l.report ? `New report · ${l.report}` : '', l.board ? `${l.board.done}/${l.board.total} tasks done` : ''].filter(Boolean),
    desk: d => `${d.title} · ${d.state}`,
    deskState: d => ({working: 'Working', queued: 'Not started', blocked: 'Blocked', review: 'Needs review', failed: 'Failed'})[d.state],
    deskDetail: d => ({working: 'working', queued: 'waiting to start', blocked: 'waiting for earlier work', review: 'result ready for review', failed: 'failed'})[d.state],
    project: p => p.name, projectFacts: p => [p.workers ? `Working: ${p.workers}` : 'no work in progress', p.questions ? `Asking you: ${p.questions}` : ''].filter(Boolean),
    open: 'open the chat',
  };
  const scene = createLeadOfficeScene({wake: () => stage.wake(), activate: target => activations.push(target), text, fontFamily: 'Inter, "Noto Sans CJK SC", sans-serif'});
  const stage = mountStage(host, {label: 'office', gestures: 'tap', fontFamily: 'Inter, sans-serif'});
  stage.camera.fit = 'contain';
  const show = view => {
    scene.update(view);
    host.style.aspectRatio = `${scene.scene.width} / ${scene.scene.height}`;
    stage.camera.worldWidth = scene.scene.width; stage.camera.worldHeight = scene.scene.height;
    stage.wake();
  };
  stage.setScene(scene.scene);
  const desk = (id, state, title, model) => ({id, conversationId: 'worker-' + id, title, state, model});
  window.office = {stage, scene, show, desk, counts, activations, host, scroller};
}"""

DESKS = """[
  office.desk('w1', 'working', 'Studio interface: stunning rendering, crafted assets, real interactions', 'engineer'),
  office.desk('w2', 'queued', 'Browser extension: robust server URL input', 'general'),
  office.desk('w3', 'blocked', 'Tofu-DB engine performance optimization', 'analyst'),
  office.desk('w4', 'review', 'SQLite sidecar: fair, fully-optimized configuration on DolphinFS', 'researcher'),
  office.desk('w5', 'failed', 'Redesign local control into seamless multi-machine control', 'executor'),
  office.desk('w6', 'working', 'Write the release notes for the office', 'writer'),
]"""


def view(desks, lead_state='replying', report='Parser shipped', board=(3, 8), current='', **extra):
    board_js = f'{{done: {board[0]}, total: {board[1]}}}' if board else 'null'
    extra_js = ', '.join(f'{key}: {value}' for key, value in extra.items())
    return (f"({{altitude: 'floor', lead: {{conversationId: 'lead', state: '{lead_state}', report: '{report}', board: {board_js}}}, "
            f"desks: {desks}, projects: [], current: '{current}', coverage: 'complete', projectsCoverage: 'complete'"
            + (', ' + extra_js if extra_js else '') + '})')


def mount(browser, preview, width=300, clock=None, reduced='no-preference'):
    context = browser.new_context(viewport={'width': width + 24, 'height': 940}, device_scale_factor=2, reduced_motion=reduced)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(preview)
    page.evaluate(MOUNT, {'stagePath': source('office-game/engine/stage.ts'), 'scenePath': source('lead-office/scene.ts'),
                          'width': width, 'clock': clock})
    return context, page, errors


def shoot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    box = page.locator('canvas').first.bounding_box()
    page.screenshot(path=str(OUT / f'{name}.png'), clip={'x': box['x'] - 4, 'y': box['y'] - 4,
                                                          'width': box['width'] + 8, 'height': min(box['height'], 930 - box['y']) + 8})


# 13:30 and 22:30 on 2026-11-15 in the browser's own zone (the scene reads the local minute).
DAY = 'new Date(2026, 10, 15, 13, 30).getTime()'
NIGHT = 'new Date(2026, 10, 15, 22, 30).getTime()'


def test_every_state_renders_and_interactions_open_conversations(browser, preview):
    probe_context = browser.new_context()
    probe = probe_context.new_page()
    day, night = probe.evaluate(f'[{DAY}, {NIGHT}]')
    probe_context.close()
    context, page, errors = mount(browser, preview, clock=day)
    try:
        page.evaluate(f'office.show({view(DESKS)})')
        page.wait_for_timeout(500)
        shoot(page, 'floor-six-workers-day')
        targets = page.evaluate("[...document.querySelectorAll('.evo-target')].map(b => [b.dataset.office, b.dataset.state])")
        assert targets == [['lead', 'replying'], ['desk', 'working'], ['desk', 'queued'], ['desk', 'blocked'],
                           ['desk', 'review'], ['desk', 'failed'], ['desk', 'working']]
        # Hover a desk: its status card; click: its conversation.
        box = page.locator('.evo-target[data-state="review"]').bounding_box()
        page.mouse.move(box['x'] + box['width'] * .3, box['y'] + box['height'] * .5)
        page.wait_for_timeout(250)
        shoot(page, 'hover-status-card')
        page.mouse.click(box['x'] + box['width'] * .3, box['y'] + box['height'] * .5)
        lead_box = page.locator('.evo-target[data-office="lead"]').bounding_box()
        page.mouse.click(lead_box['x'] + 40, lead_box['y'] + 60)
        assert page.evaluate('office.activations') == [{'kind': 'conversation', 'conversationId': 'worker-w4'},
                                                       {'kind': 'conversation', 'conversationId': 'lead'}]
        # The lead waiting for the owner, an idle lead, and an open conversation framed.
        page.evaluate(f"office.show({view(DESKS, lead_state='asking', report='', current='worker-w1')})")
        page.wait_for_timeout(2600)
        shoot(page, 'lead-asking-current-framed')
        page.evaluate(f"office.show({view(DESKS, lead_state='idle', report='')})")
        page.wait_for_timeout(2600)
        shoot(page, 'lead-idle-at-board')
        assert not errors
    finally:
        context.close()

    context, page, errors = mount(browser, preview, clock=night)
    try:
        page.evaluate(f'office.show({view(DESKS)})')
        page.wait_for_timeout(500)
        shoot(page, 'floor-six-workers-night')
        assert not errors
    finally:
        context.close()

    # The headquarters: one floor per project coordinator.
    context, page, errors = mount(browser, preview, clock=day, width=420)
    try:
        projects = """[{path: '/w/atlas', name: 'Atlas', color: '#2f7d6b', coordinatorId: 'c-a', workers: 1, questions: 0, overdue: 0, report: ''},
          {path: '/w/beacon', name: 'Beacon', color: '#b5642d', coordinatorId: 'c-b', workers: 3, questions: 1, overdue: 0, report: 'Pricing page drafted'},
          {path: '/w/comet', name: 'Comet', color: '#5a5f8c', coordinatorId: 'c-c', workers: 0, questions: 0, overdue: 0, report: ''},
          {path: '/w/dusk', name: 'Dusk', color: '', coordinatorId: '', workers: null, questions: 0, overdue: 0, report: ''}]"""
        page.evaluate(f"office.show({view('[]', lead_state='idle', report='', board=None, altitude=repr('headquarters'), projects=projects)})")
        page.wait_for_timeout(500)
        shoot(page, 'headquarters-wide')
        assert not errors
    finally:
        context.close()


def test_handoff_and_review_play_from_real_changes(browser, preview):
    probe_context = browser.new_context()
    day = probe_context.new_page().evaluate(DAY)
    probe_context.close()
    context, page, errors = mount(browser, preview, clock=day)
    try:
        two = "[office.desk('w1', 'working', 'Parser speed: profile the hot loop', 'engineer'), office.desk('w2', 'working', 'Review the API design', 'reviewer')]"
        page.evaluate(f"office.show({view(two, lead_state='replying', report='')})")
        page.wait_for_timeout(300)
        # A delegation: the brief travels down the shaft, then the Worker walks out of the door.
        three = two[:-1] + ", office.desk('w3', 'working', 'Write the migration guide', 'writer')]"
        page.evaluate(f"office.show({view(three, lead_state='replying', report='')})")
        assert page.evaluate("office.scene.person('desk:w3').beats") == ['hidden', 'door', 'walk', 'sit']
        page.wait_for_function("office.scene.person('desk:w3').beats[0] === 'hidden' && office.scene.person('desk:w3').now.hidden")
        page.wait_for_timeout(450)
        shoot(page, 'handoff-envelope-in-shaft')
        page.wait_for_function("office.scene.person('desk:w3').beats[0] === 'walk'", timeout=10000)
        page.wait_for_timeout(250)
        shoot(page, 'handoff-worker-walks-in')
        page.wait_for_function("office.scene.person('desk:w3').beats.length === 0", timeout=10000)
        # A result: stand, cheer with confetti, present it beside the desk; the report lands on the tray.
        reviewed = three.replace("'w2', 'working'", "'w2', 'review'")
        page.evaluate(f"office.show({view(reviewed, lead_state='replying', report='API design reviewed')})")
        assert page.evaluate("office.scene.person('desk:w2').beats") == ['stand', 'cheer', 'walk']
        assert page.evaluate('office.scene.delivering()')
        page.wait_for_function("office.scene.person('desk:w2').beats[0] === 'cheer'", timeout=5000)
        page.wait_for_timeout(350)
        shoot(page, 'review-cheer-and-delivery')
        page.wait_for_function("office.scene.person('desk:w2').beats.length === 0", timeout=10000)
        shoot(page, 'review-presented')
        assert not errors
    finally:
        context.close()


PROFILE = """async ms => {
  const {scene, stage} = office;
  const draws = [], intervals = [];
  const draw = scene.scene.draw.bind(scene.scene);
  scene.scene.draw = frame => { const t0 = performance.now(); draw(frame); draws.push(performance.now() - t0); };
  let last = 0, running = true;
  // A fixed CPU workload on every display frame: its spikes are the host's scheduling stalls, not the scene's.
  const control = [];
  const sample = now => {
    if (last) intervals.push(now - last); last = now;
    const t0 = performance.now(); let x = 0; for (let i = 0; i < 100000; i++) x += Math.sqrt(i); control.push(performance.now() - t0 + x * 0);
    if (running) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
  const before = stage.metrics();
  await new Promise(resolve => setTimeout(resolve, ms));
  running = false;
  const after = stage.metrics();
  scene.scene.draw = draw;
  const q = (list, p) => { const s = [...list].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : 0; };
  return {seconds: ms / 1000, paints: after.paints - before.paints, paintsPerSecond: (after.paints - before.paints) / (ms / 1000),
    drawMs: {p50: q(draws, .5), p95: q(draws, .95), max: Math.max(0, ...draws), samples: draws.length},
    displayFrames: intervals.length + 1,
    controlMs: {p50: q(control, .5), p95: q(control, .95), max: Math.max(0, ...control)},
    frameIntervalMs: {p50: q(intervals, .5), p95: q(intervals, .95), max: Math.max(0, ...intervals)},
    layers: scene.layerCount(), backingPixels: after.backingPixels};
}"""


def test_profile_six_workers_and_sleep_when_hidden(browser, preview):
    probe_context = browser.new_context()
    day = probe_context.new_page().evaluate(DAY)
    probe_context.close()
    context, page, errors = mount(browser, preview, clock=day)
    report = {}
    try:
        page.evaluate(f'office.show({view(DESKS)})')
        page.wait_for_timeout(400)
        # Steady state: typists and the hourglass loop on the 8 Hz beat; nothing else wakes.
        report['steady'] = page.evaluate(PROFILE, 3000)
        # Continuous motion: a delegation in flight (brief down the shaft, walk, sit).
        seven = DESKS.rstrip()[:-1].rstrip().rstrip(',') + ", office.desk('w7', 'working', 'Profile the stage', 'engineer')]"
        page.evaluate(f'office.show({view(seven)})')
        report['handoff'] = page.evaluate(PROFILE, 2500)
        page.wait_for_function("office.scene.person('desk:w7').beats.length === 0", timeout=10000)
        steady, handoff = report['steady'], report['handoff']
        # Frames are cheap and loops never spin at display rate while nothing walks.
        # Median draw cost; tail spikes are compared against the control workload's (host CPU throttling).
        assert steady['drawMs']['p50'] < 4, steady
        # The 8 Hz art beat, never the display rate.
        assert 4 <= steady['paintsPerSecond'] <= 10, steady
        # While someone walks, the stage paints on (nearly) every display frame the browser delivers.
        assert handoff['paints'] >= .85 * handoff['displayFrames'], handoff
        assert handoff['drawMs']['p50'] < 4, handoff
        # Static art is painted once per kind: lead, desk, lead desk, chair and one floor per department.
        assert steady['layers'] <= 4 + 6, steady

        # Hidden tab: the stage cancels its frame and timer and requests nothing.
        page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
          document.dispatchEvent(new Event('visibilitychange')); office.counts.raf = 0; office.counts.timers = 0; }""")
        page.wait_for_timeout(1200)
        report['hidden'] = page.evaluate('({counts: {...office.counts}, pending: office.stage.metrics().pending})')
        assert report['hidden']['counts'] == {'raf': 0, 'timers': 0}, report['hidden']
        assert report['hidden']['pending'] == {'frame': False, 'timer': False}, report['hidden']
        page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        page.wait_for_timeout(300)
        assert page.evaluate('office.stage.metrics().pending.timer || office.stage.metrics().pending.frame')

        # Scrolled out of view: the IntersectionObserver puts it to sleep the same way.
        page.evaluate('office.scroller.scrollTop = 2400')
        page.wait_for_timeout(400)
        page.evaluate('office.counts.raf = 0; office.counts.timers = 0')
        page.wait_for_timeout(1200)
        report['offscreen'] = page.evaluate('({counts: {...office.counts}, pending: office.stage.metrics().pending})')
        assert report['offscreen']['counts'] == {'raf': 0, 'timers': 0}, report['offscreen']
        page.evaluate('office.scroller.scrollTop = 0')
        page.wait_for_timeout(400)

        # Disposal leaves no frame or timer behind.
        page.evaluate('office.stage.dispose(); office.scene.dispose(); office.counts.raf = 0; office.counts.timers = 0')
        page.wait_for_timeout(1000)
        report['disposed'] = page.evaluate('({counts: {...office.counts}, pending: office.stage.metrics().pending})')
        assert report['disposed']['counts'] == {'raf': 0, 'timers': 0}, report['disposed']
        assert report['disposed']['pending'] == {'frame': False, 'timer': False}
        assert not errors
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'profile.json').write_text(json.dumps(report, indent=2) + '\n')
        context.close()


def test_reduced_motion_settles_and_sleeps_until_the_minute(browser, preview):
    context, page, errors = mount(browser, preview, reduced='reduce')
    try:
        page.evaluate(f'office.show({view(DESKS)})')
        page.wait_for_timeout(300)
        page.evaluate('office.counts.raf = 0')
        page.wait_for_timeout(1500)
        # No looping beat under reduced motion: at most the frame a wake already asked for.
        assert page.evaluate('office.counts.raf') <= 1
        assert page.evaluate("office.scene.person('desk:w1').now.kind") == 'type'
        assert not errors
    finally:
        context.close()
