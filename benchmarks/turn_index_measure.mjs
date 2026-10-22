/** Pair frozen source files against the current immutable index pipeline.
 * CLI: --baseline-dir DIR --output PATH. Alternating fresh timing/memory workers,
 * actual reducer/selector, raw clocks, source hashes and full state/view parity.
 * Warm microbenchmark plus first-snapshot clocks; excludes DOM, I/O and startup. */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { availableParallelism, cpus, platform, release, tmpdir } from 'node:os';
import { dirname, resolve, join, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
const script = fileURLToPath(import.meta.url), root = resolve(dirname(script), '..');
const hash = value => createHash('sha256').update(value).digest('hex');
const arg = (key, fallback) => process.argv.includes(key) ? process.argv[process.argv.indexOf(key) + 1] : fallback;
const sizes = [64, 512, 4096], samples = 100, warmups = 20, pairs = 3;
const milliseconds = start => Number(process.hrtime.bigint() - start) / 1e6;
if (process.argv.includes('--worker')) {
  const config = JSON.parse(arg('--worker')), runtime = await import(pathToFileURL(config.bundle));
  const rows = [];
  for (const size of sizes) {
    const store = runtime.createTurnStore('measure');
    const turns = Array.from({ length: size }, (_, i) => ({
      turnId: `t${i}`, presentationId: `t${i}`, conversationId: 'measure', laneId: 'main', ordinal: i,
      actor: 'assistant', kind: 'reply', runId: '', status: i === size - 1 ? 'running' : 'completed',
      currentAttemptId: `a${i}`, projectionRevision: 1, settlement: {}, createdAt: 1, updatedAt: 1,
      projection: { content: 'evidence', segments: [{ type: 'text', blockId: 'answer', text: 'evidence' }] },
    }));
    const snapshot = { turns, attempts: turns.map((t, i) => ({
      attemptId: `a${i}`, turnId: t.turnId, taskId: 'task', createdAt: 1, lastSeq: 1, status: t.status,
    })) };
    global.gc?.(); const heapBefore = process.memoryUsage().heapUsed;
    const startSnapshot = process.hrtime.bigint();
    store.dispatch({ type: 'snapshot', snapshot });
    const initialSnapshotMs = milliseconds(startSnapshot);
    const selector = runtime.createConversationViewModelSelector();
    const unsubscribe = store.subscribe((state, update) => selector.observe(state, update));
    let view = selector.select(store.getState());
    const observations = [], readObservations = [], count = config.mode === 'memory' ? 1000 : samples + warmups;
    for (let i = 0; i < count; i++) {
      const seq = i + 2, text = `stream ${seq}`;
      const action = { type: 'event', event: { conversationId: 'measure', turnId: `t${size - 1}`, attemptId: `a${size - 1}`,
        seq, projectionRevision: seq, type: 'projection_updated', payload: { projection: {
          content: text, segments: [{ type: 'text', blockId: 'answer', text }],
        } } } };
      const cpu = config.mode === 'timing' ? process.cpuUsage() : null;
      const start = config.mode === 'timing' ? process.hrtime.bigint() : 0n;
      store.dispatch(action);
      view = selector.select(store.getState());
      if (config.mode === 'timing' && i >= warmups) {
        const elapsed = milliseconds(start), used = process.cpuUsage(cpu);
        observations.push({ wall_ms: elapsed, cpu_ms: (used.user + used.system) / 1000 });
      }
    }
    if (config.mode === 'timing') for (let i = 0; i < samples + warmups; i++) {
      const state = store.getState(), row = {};
      for (const [key, operation] of Object.entries({
        bulk_read_ms: () => runtime.indexValues(state.turnsById),
        lookup_all_ms: () => state.laneOrder.main.map(id => state.turnsById[id]),
        complete_selection_ms: () => runtime.selectConversationViewModel(state),
        json_ms: () => JSON.stringify(state),
      })) { const start = process.hrtime.bigint(); operation(); row[key] = milliseconds(start); }
      if (i >= warmups) readObservations.push(row);
    }
    global.gc?.();
    const retained = process.memoryUsage().heapUsed - heapBefore;
    rows.push({ size, first_snapshot_ms: initialSnapshotMs, samples: observations, reads: readObservations,
      state_sha256: hash(JSON.stringify(store.getState())), view_sha256: hash(JSON.stringify(view)),
      ...(config.mode === 'memory' ? { events: count, retained_heap_bytes: retained, rss_bytes: process.memoryUsage().rss } : {}) });
    unsubscribe(); selector.dispose();
  }
  process.stdout.write(JSON.stringify({ pid: process.pid, mode: config.mode, rows }));
} else {
  const baseline = arg('--baseline-dir'), output = arg('--output');
  if (!baseline || !output) throw new Error('--baseline-dir and --output required');
  const directory = mkdtempSync(join(tmpdir(), 'tofu-index-measure-'));
  try {
    const bundles = {}, bundleHashes = {}, frozenSources = {};
    for (const arm of ['baseline', 'candidate']) {
      const bundle = join(directory, `${arm}.mjs`); bundles[arm] = bundle;
      const contents = "export * from './frontend/src/core/turn-state'; export * from './frontend/src/conversation/presentation/conversation-view-model';"
        + (arm === 'baseline' ? 'export const indexValues = Object.values;' : "export { entityRecordValues as indexValues } from './frontend/src/core/entity-record';");
      await build({ stdin: { contents, resolveDir: root, loader: 'ts' }, outfile: bundle, bundle: true, format: 'esm', platform: 'node',
        plugins: arm === 'baseline' ? [{ name: 'frozen-index-pipeline', setup(api) {
          api.onLoad({ filter: /\.ts$/ }, args => {
            const path = join(resolve(baseline), relative(root, args.path));
            if (!existsSync(path)) return undefined;
            const contents = readFileSync(path, 'utf8'); frozenSources[relative(root, args.path)] = hash(contents);
            return { contents, loader: 'ts', resolveDir: dirname(args.path) };
          });
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
      if (both[0].rows.some((r, i) => r.state_sha256 !== both[1].rows[i].state_sha256 || r.view_sha256 !== both[1].rows[i].view_sha256)) throw new Error('State/view parity failed');
    }
    const quantile = (values, q) => values.sort((a, b) => a - b)[Math.ceil(values.length * q) - 1];
    const summary = sizes.map(size => ({ size, ...Object.fromEntries(['baseline', 'candidate'].map(arm => {
      const rows = cohorts.filter(c => c.arm === arm && c.mode === 'timing').map(c => c.rows.find(r => r.size === size));
      const timings = rows.flatMap(r => r.samples), reads = rows.flatMap(r => r.reads);
      const stats = (values, key) => ({ p50: quantile(values.map(v => v[key]), .5), p95: quantile(values.map(v => v[key]), .95) });
      return [arm, { reduce_and_select_ms: stats(timings, 'wall_ms'), cpu_ms: stats(timings, 'cpu_ms'),
        first_snapshot_ms: rows.map(r => r.first_snapshot_ms),
        reads: Object.fromEntries(['bulk_read_ms', 'lookup_all_ms', 'complete_selection_ms', 'json_ms'].map(key => [key, stats(reads, key)])) }];
    })) }));
    const report = { format: 'tofu.turn-index-measure/v1', admission: 'diagnostic_only; excludes DOM/startup/I/O', sizes, samples, warmups, pairs,
      bundleHashes, frozenSources, environment: { node: process.version, platform: platform(), release: release(), cpu: cpus()[0]?.model, availableParallelism: availableParallelism() }, cohorts, summary };
    writeFileSync(resolve(output), JSON.stringify(report, null, 2) + '\n'); process.stdout.write(JSON.stringify({ parity: true, summary }) + '\n');
  } finally { rmSync(directory, { recursive: true, force: true }); }
}
