/** Paired real-source stream selection benchmark. CLI: --baseline-source PATH
 * --output PATH. Alternating isolated workers; separate memory cohorts; full VM
 * parity and raw samples. Warm diagnostic only, not browser/admission evidence. */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { availableParallelism, cpus, platform, release, tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
const script = fileURLToPath(import.meta.url), root = resolve(dirname(script), '..');
const hash = value => createHash('sha256').update(value).digest('hex');
const arg = (key, fallback) => process.argv.includes(key) ? process.argv[process.argv.indexOf(key) + 1] : fallback;
const sizes = [64, 512, 4096], samples = 100, warmups = 20, pairs = 3;
if (process.argv.includes('--worker')) {
  const config = JSON.parse(arg('--worker'));
  const runtime = await import(pathToFileURL(config.bundle));
  const rows = [];
  for (const size of sizes) {
    const store = runtime.createTurnStore('measure');
    const turns = Array.from({ length: size }, (_, i) => ({
      turnId: `t${i}`, presentationId: `t${i}`, conversationId: 'measure', laneId: 'main', ordinal: i,
      actor: 'assistant', kind: 'reply', runId: '', status: i === size - 1 ? 'running' : 'completed',
      currentAttemptId: `a${i}`, projectionRevision: 1, settlement: {}, createdAt: 1, updatedAt: 1,
      projection: { content: 'evidence', segments: [{ type: 'text', blockId: 'answer', text: 'evidence' }] },
    }));
    store.dispatch({ type: 'snapshot', snapshot: { turns, attempts: turns.map((t, i) => ({
      attemptId: `a${i}`, turnId: t.turnId, taskId: 'task', createdAt: 1, lastSeq: 1, status: t.status,
    })) } });
    const selector = config.arm === 'candidate' ? runtime.createConversationViewModelSelector() : null;
    if (selector) store.subscribe((state, update) => selector.observe(state, update));
    const select = selector?.select ?? runtime.selectConversationViewModel;
    global.gc?.();
    const memoryBefore = process.memoryUsage().heapUsed, observations = [];
    let view = select(store.getState());
    for (let i = 0; i < samples + warmups; i++) {
      const seq = i + 2, text = `stream ${seq}`;
      const action = { type: 'event', event: { turnId: `t${size - 1}`, attemptId: `a${size - 1}`,
        seq, projectionRevision: seq, type: 'projection_updated', payload: { projection: {
          content: text, segments: [{ type: 'text', blockId: 'answer', text }],
        } } } };
      const cpu = config.mode === 'timing' ? process.cpuUsage() : null;
      const start = config.mode === 'timing' ? process.hrtime.bigint() : 0n;
      store.dispatch(action);
      const selectionStart = config.mode === 'timing' ? process.hrtime.bigint() : 0n;
      view = select(store.getState());
      if (config.mode === 'timing' && i >= warmups) {
        const end = process.hrtime.bigint(), used = process.cpuUsage(cpu);
        observations.push({ selection_ms: Number(end - selectionStart) / 1e6,
          reduce_and_select_ms: Number(end - start) / 1e6, cpu_ms: (used.user + used.system) / 1000 });
      }
    }
    global.gc?.();
    const retainedHeap = process.memoryUsage().heapUsed - memoryBefore;
    rows.push({ size, samples: observations, view_sha256: hash(JSON.stringify(view)),
      ...(config.mode === 'memory' ? { retained_heap_delta_bytes: retainedHeap, rss_bytes: process.memoryUsage().rss } : {}) });
    selector?.dispose();
  }
  process.stdout.write(JSON.stringify({ pid: process.pid, mode: config.mode, rows }));
} else {
  const baseline = arg('--baseline-source'), output = arg('--output');
  if (!baseline || !output) throw new Error('--baseline-source and --output required');
  const directory = mkdtempSync(join(tmpdir(), 'tofu-selection-measure-'));
  try {
    const bundles = {}, bundleHashes = {};
    for (const arm of ['baseline', 'candidate']) {
      const bundle = join(directory, `${arm}.mjs`); bundles[arm] = bundle;
      await build({ stdin: { contents: "export * from './frontend/src/core/turn-state'; export * from './frontend/src/conversation/presentation/conversation-view-model';",
        resolveDir: root, loader: 'ts' }, outfile: bundle, bundle: true, format: 'esm', platform: 'node',
        plugins: arm === 'baseline' ? [{ name: 'baseline-selection', setup(api) {
          api.onLoad({ filter: /[/\\]conversation-view-model\.ts$/ }, args => ({
            contents: readFileSync(resolve(baseline), 'utf8'), loader: 'ts', resolveDir: dirname(args.path),
          }));
        } }] : [] });
      bundleHashes[arm] = hash(readFileSync(bundle));
    }
    const cohorts = [];
    for (const mode of ['timing', 'memory']) for (let pair = 0; pair < pairs; pair++) {
      for (const arm of pair % 2 ? ['candidate', 'baseline'] : ['baseline', 'candidate']) {
        const config = { arm, mode, bundle: bundles[arm] };
        const worker = JSON.parse(execFileSync(process.execPath, ['--expose-gc', script, '--worker', JSON.stringify(config)],
          { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }));
        cohorts.push({ pair, arm, ...worker });
      }
    }
    for (const mode of ['timing', 'memory']) for (let pair = 0; pair < pairs; pair++) {
      const both = cohorts.filter(c => c.pair === pair && c.mode === mode);
      if (both[0].rows.some((r, i) => r.view_sha256 !== both[1].rows[i].view_sha256)) throw new Error('View parity failed');
    }
    const quantile = (values, q) => values.sort((a, b) => a - b)[Math.ceil(values.length * q) - 1];
    const summary = sizes.map(size => ({ size, ...Object.fromEntries(['baseline', 'candidate'].map(arm => {
      const values = cohorts.filter(c => c.arm === arm && c.mode === 'timing').flatMap(c => c.rows.find(r => r.size === size).samples);
      return [arm, Object.fromEntries(['selection_ms', 'reduce_and_select_ms', 'cpu_ms'].map(key =>
        [key, { p50: quantile(values.map(v => v[key]), .5), p95: quantile(values.map(v => v[key]), .95) }]))];
    })) }));
    const report = { format: 'tofu.turn-selection-measure/v1', scope: 'warm reducer plus view selection; no DOM',
      admission: 'diagnostic_only', sizes, samples, warmups, pairs, bundleHashes,
      environment: { node: process.version, platform: platform(), release: release(), cpu: cpus()[0]?.model, availableParallelism: availableParallelism() },
      baseline: { path: resolve(baseline), sha256: hash(readFileSync(baseline)) }, cohorts, summary };
    writeFileSync(resolve(output), JSON.stringify(report, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ parity: true, summary }) + '\n');
  } finally { rmSync(directory, { recursive: true, force: true }); }
}
