/** Offline paired TurnState measurement. Entry: --baseline-source PATH --output PATH.
 * Bundles the two real reducer sources, runs isolated alternating workers, and
 * checks identical final state before reporting raw warm samples. This is a
 * reducer microbenchmark, not browser latency or whole-system admission.
 */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { availableParallelism, cpus, platform, release, tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const script = fileURLToPath(import.meta.url);
const root = resolve(dirname(script), '..');
const hash = value => createHash('sha256').update(value).digest('hex');
const arg = (name, fallback) => {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
};
const positive = (name, fallback) => {
  const value = Number(arg(name, fallback));
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`Invalid ${name}`);
  return value;
};
const scenarios = ['stream', 'duplicate', 'transport', 'command'];
const fixture = (runtime, size) => {
  const state = runtime.createTurnState('benchmark');
  state.laneOrder.main = [];
  for (let index = 0; index < size; index += 1) {
    const turnId = `turn-${index}`;
    const attemptId = `attempt-${index}`;
    const live = index === size - 1;
    state.laneOrder.main.push(turnId);
    state.turnsById[turnId] = {
      turnId, presentationId: turnId, conversationId: 'benchmark', laneId: 'main',
      ordinal: index + 1, actor: 'assistant', kind: 'reply', runId: '',
      status: live ? 'running' : 'completed', currentAttemptId: attemptId,
      projectionRevision: 1, projection: { content: 'synthetic evidence' },
      settlement: {}, createdAt: 1, updatedAt: 1,
    };
    state.attemptsById[attemptId] = {
      attemptId, turnId, status: live ? 'running' : 'completed', lastSeq: 1,
    };
  }
  state.historyByLane.main = { nextBeforeOrdinal: null, hasMore: false, totalTurns: size };
  return state;
};
const action = (scenario, index, size) => {
  const turnId = `turn-${size - 1}`;
  if (scenario === 'transport') return { type: 'transport', status: index % 2 ? 'live' : 'recovering' };
  if (scenario === 'command') return index % 2
    ? { type: 'command_failed', turnId } : { type: 'command_pending', turnId, operation: 'stop' };
  const seq = scenario === 'duplicate' ? 1 : index + 2;
  return { type: 'event', event: {
    turnId, attemptId: `attempt-${size - 1}`, seq, projectionRevision: seq,
    type: 'projection_updated', payload: { projection: { content: `sample ${seq}` } },
  } };
};

if (process.argv.includes('--worker')) {
  const config = JSON.parse(arg('--worker'));
  const runtime = await import(pathToFileURL(config.bundle));
  const rows = [];
  for (const size of config.sizes) {
    for (const scenario of scenarios) {
      let state = fixture(runtime, size);
      const samples = [];
      const memory = config.mode === 'memory';
      if (memory) global.gc?.();
      const heapBefore = process.memoryUsage().heapUsed;
      for (let index = 0; index < config.samples + config.warmups; index += 1) {
        const input = action(scenario, index, size);
        const cpu = memory ? undefined : process.cpuUsage();
        const start = memory ? undefined : process.hrtime.bigint();
        state = runtime.reduceTurnState(state, input);
        if (!memory && index >= config.warmups) {
          const elapsed = Number(process.hrtime.bigint() - start) / 1e6;
          const used = process.cpuUsage(cpu);
          samples.push({ wall_ms: elapsed, cpu_ms: (used.user + used.system) / 1000 });
        }
      }
      if (memory) global.gc?.();
      rows.push({ size, scenario, samples, final_state_sha256: hash(JSON.stringify(state)),
        ...(memory ? { retained_heap_delta_bytes: process.memoryUsage().heapUsed - heapBefore,
          process_peak_rss_bytes: process.resourceUsage().maxRSS * 1024 } : {}) });
    }
  }
  process.stdout.write(JSON.stringify({ pid: process.pid, mode: config.mode, rows }));
} else {
  const baseline = arg('--baseline-source');
  const output = arg('--output');
  if (!baseline || !output) throw new Error('Required: --baseline-source PATH --output PATH');
  const candidate = resolve(root, 'frontend/src/core/turn-state.ts');
  const pairs = positive('--pairs', 3);
  const samples = positive('--samples', 100);
  const warmups = positive('--warmups', 20);
  const sizes = String(arg('--sizes', '64,512,4096')).split(',').map(Number);
  if (sizes.some(size => !Number.isSafeInteger(size) || size < 1)) throw new Error('Invalid --sizes');
  const directory = mkdtempSync(join(tmpdir(), 'tofu-turn-state-measure-'));
  try {
    const bundles = {};
    const sources = { baseline: resolve(baseline), candidate };
    for (const [arm, source] of Object.entries(sources)) {
      bundles[arm] = join(directory, `${arm}.mjs`);
      await build({ entryPoints: [source], outfile: bundles[arm], bundle: true, format: 'esm', platform: 'node' });
    }
    const cohorts = [];
    for (const mode of ['timing', 'memory']) {
      for (let pair = 0; pair < pairs; pair += 1) {
        const order = pair % 2 ? ['candidate', 'baseline'] : ['baseline', 'candidate'];
        for (const arm of order) {
          const config = { bundle: bundles[arm], mode, samples, warmups, sizes };
          const worker = JSON.parse(execFileSync(process.execPath,
            ['--expose-gc', script, '--worker', JSON.stringify(config)], { encoding: 'utf8' }));
          cohorts.push({ pair, arm, ...worker });
        }
      }
    }
    for (const mode of ['timing', 'memory']) {
      for (let pair = 0; pair < pairs; pair += 1) {
        const both = cohorts.filter(row => row.mode === mode && row.pair === pair);
        if (both.length !== 2 || both[0].rows.some((row, index) =>
          row.final_state_sha256 !== both[1].rows[index].final_state_sha256)) {
          throw new Error(`Reducer semantic parity failed for ${mode}/${pair}`);
        }
      }
    }
    const quantile = (values, q) => values.sort((a, b) => a - b)[Math.ceil(values.length * q) - 1];
    const summary = [];
    for (const size of sizes) for (const scenario of scenarios) {
      const timing = arm => cohorts.filter(row => row.arm === arm && row.mode === 'timing')
        .flatMap(row => row.rows.find(value => value.size === size && value.scenario === scenario).samples);
      const summarize = rows => ({ p50_ms: quantile(rows.map(x => x.wall_ms), 0.5),
        p95_ms: quantile(rows.map(x => x.wall_ms), 0.95),
        p95_cpu_ms: quantile(rows.map(x => x.cpu_ms), 0.95) });
      const before = summarize(timing('baseline')), after = summarize(timing('candidate'));
      summary.push({ size, scenario, baseline: before, candidate: after, p95_ratio: after.p95_ms / before.p95_ms });
    }
    const report = { format: 'tofu.turn-state-measure/v1', scope: 'warm reducer microbenchmark',
      admission: 'diagnostic_only; no cold-start or browser evidence', samples, warmups, pairs, sizes,
      environment: { node: process.version, platform: platform(), release: release(),
        cpu: cpus()[0]?.model, availableParallelism: availableParallelism() },
      sources: Object.fromEntries(Object.entries(sources).map(([arm, path]) => [arm, {
        path, sha256: hash(readFileSync(path)), bundle_sha256: hash(readFileSync(bundles[arm])),
      }])), cohorts, summary };
    writeFileSync(resolve(output), JSON.stringify(report, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ output: resolve(output), parity: true, summary }, null, 2) + '\n');
  } finally { rmSync(directory, { recursive: true, force: true }); }
}
