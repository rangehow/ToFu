// @vitest-environment jsdom
/** The office facts read through the real endpoint facade and transport (only fetch is simulated). A cold
 * baseline answers when the server's first pass ends, so the read waits for it; leaving the office is what
 * cancels it, through the WorldLive lifetime signal. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import stream from '../../../../tests/fixtures/office_facts/stream_v1.json';
import { createWorldLive } from './live';

/** Production's cold office baseline (2026-11-13: 20.2 s and 13.0 s), longer than any coordination read deadline. */
const COLD_OFFICE_READ_MS = 20_000;
let officeSignals: AbortSignal[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  officeSignals = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
    if (new URL(url, 'http://tofu.invalid').pathname !== '/api/v4/router/office') {
      return Promise.resolve(new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } }));
    }
    const signal = init.signal!;
    officeSignals.push(signal);
    return new Promise((resolve, reject) => {
      const answer = setTimeout(() => resolve(new Response(JSON.stringify({ meta: {}, data: stream.snapshot }),
        { headers: { 'content-type': 'application/json' } })), COLD_OFFICE_READ_MS);
      signal.addEventListener('abort', () => { clearTimeout(answer); reject(signal.reason); }, { once: true });
    });
  }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it('waits out a cold 20 s office read and fills the world store with its snapshot', async () => {
  const live = createWorldLive(() => {}, null);
  await live.start();
  await vi.advanceTimersByTimeAsync(COLD_OFFICE_READ_MS - 1);
  expect(officeSignals).toHaveLength(1);
  expect(officeSignals[0].aborted).toBe(false);
  expect(live.state.facts).toBeNull();
  expect(live.state.loading).toBe(true);

  await vi.advanceTimersByTimeAsync(1);
  expect(live.state.loading).toBe(false);
  expect(live.state.facts?.coverage).toEqual(stream.snapshot.coverage);
  expect([...live.state.facts!.facts.keys()]).toEqual(stream.snapshot.facts.map(fact => fact.key));
  expect(officeSignals).toHaveLength(1);
  live.stop();
});

it('cancels an unanswered office read only when the office is left, through the WorldLive lifetime signal', async () => {
  const changes = vi.fn();
  const live = createWorldLive(changes, null);
  await live.start();
  await vi.advanceTimersByTimeAsync(COLD_OFFICE_READ_MS - 5_000);
  expect(officeSignals[0].aborted).toBe(false);

  live.stop();
  expect(officeSignals[0].aborted).toBe(true);
  expect(officeSignals[0].reason).toMatchObject({ name: 'AbortError' });
  const rendered = changes.mock.calls.length;
  await vi.advanceTimersByTimeAsync(COLD_OFFICE_READ_MS);
  expect(live.state.facts).toBeNull();
  expect(changes).toHaveBeenCalledTimes(rendered);
  expect(vi.getTimerCount()).toBe(0);
});
