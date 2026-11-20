// @vitest-environment jsdom
/** What the office shows while its first reads answer: the real WorldLive, endpoint facade and transport, with
 * only fetch and the painted scene simulated. A cold office facts baseline answers when the server's first pass
 * ends; until then the world says it is loading instead of passing an empty or provisional floor off as truth. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import stream from '../../../../tests/fixtures/office_facts/stream_v1.json';
import { t } from '../../i18n';
import { mountWorld, type WorldShell } from './app';
import { LEAD, PROJECT, RUNNER, memberRunning, teamPage } from './testing/recorded';

vi.mock('./office', () => ({
  createOffice: () => ({
    element: document.createElement('div'), update: () => {}, walk: () => {}, select: () => {}, restoreFocus: () => {},
    resize: () => {}, setInsets: () => {}, destroy: () => {}, documentCounts: () => ({ reports: 0, deliverables: 0 }),
  }),
}));

/** Production's cold office baseline (2026-11-13: 20.2 s and 13.0 s). */
const COLD_OFFICE_READ_MS = 20_000;
const answer = (data: unknown, status = 200) => new Response(JSON.stringify(status === 200 ? { meta: {}, data } : {}),
  { status, headers: { 'content-type': 'application/json' } });
const frames: FrameRequestCallback[] = [];
const SHELL: WorldShell = {
  openConversation() {}, openQuestion() {}, answerPorts: () => undefined, downloadUrl: id => id, confirmDefault: async () => {},
  dock() {}, classic() {}, push: null,
};

beforeEach(() => {
  vi.useFakeTimers();
  frames.length = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => {});
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
    const path = new URL(url, 'http://tofu.invalid').pathname;
    if (path === '/api/v4/folders') {
      return Promise.resolve(answer({ items: [{ id: 'f', name: 'tofu', projectPath: PROJECT, projectPaths: [PROJECT] }] }));
    }
    if (path === '/api/v4/project/coordination') return Promise.resolve(answer({ availability: 'available', conversationId: LEAD }));
    if (path === '/api/v4/project/coordination/conversations') return Promise.resolve(answer(teamPage(memberRunning)));
    if (path !== '/api/v4/router/office') return Promise.resolve(answer(null, 404));
    return new Promise((resolve, reject) => {
      const reply = setTimeout(() => resolve(answer(stream.snapshot)), COLD_OFFICE_READ_MS);
      init.signal!.addEventListener('abort', () => { clearTimeout(reply); reject(init.signal!.reason); }, { once: true });
    });
  }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

/** Paint the pending frame, then read what the owner sees. */
function shown(root: HTMLElement) {
  for (const paint of frames.splice(0)) paint(0);
  const loading = root.querySelector<HTMLElement>('.w-hud .w-chip[data-tone="loading"]')!;
  const loader = root.querySelector<HTMLElement>('.w-loader')!;
  return {
    busy: root.querySelector('.w-stage')!.getAttribute('aria-busy'),
    loading: loading.hidden ? null : loading.textContent,
    empty: !root.querySelector<HTMLElement>('.w-empty')!.hidden,
    progress: loader.hidden ? null : loader.querySelector('span')!.textContent,
  };
}

it('shows a loading state, never an empty office or an error, until a cold 20 s office read answers', async () => {
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountWorld(root, SHELL);
  const loading = { busy: 'true', loading: t('common.loading'), empty: false };
  expect(shown(root)).toEqual({ ...loading, progress: t('world.loading.progress', { done: 0, total: 1 }) });
  expect(page.office()!.people.size).toBe(0);

  // The team reads answered at once: the floor is filled from them while the office facts are still read, and
  // the loading card counts the five answered team reads against the one step still open.
  await vi.advanceTimersByTimeAsync(1_000);
  const waiting = { ...loading, progress: t('world.loading.progress', { done: 5, total: 6 }) };
  expect(shown(root)).toEqual(waiting);
  expect([...page.office()!.people.keys()].sort()).toEqual([LEAD, RUNNER].sort());
  await vi.advanceTimersByTimeAsync(COLD_OFFICE_READ_MS - 1_001);
  expect(shown(root)).toEqual(waiting);

  await vi.advanceTimersByTimeAsync(1);
  expect(shown(root)).toEqual({ busy: 'false', loading: null, empty: false, progress: null });
  expect([...page.office()!.people.keys()].sort()).toEqual([LEAD, RUNNER].sort());
  page.dispose();
});

it('seats the Global Router as soon as it answers, without waiting for the folder list', async () => {
  const ROUTER = 'coordinator-router';
  const base = fetch as unknown as (url: string, init: RequestInit) => Promise<Response>;
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
    const path = new URL(url, 'http://tofu.invalid').pathname;
    if (path === '/api/v4/router') return Promise.resolve(answer({ availability: 'available', conversationId: ROUTER, projectPath: '/router' }));
    if (path === '/api/v4/folders') return new Promise(resolve => setTimeout(() => resolve(base(url, init)), 10_000));
    if (path === '/api/v4/project/coordination/conversations' && new URL(url, 'http://tofu.invalid').searchParams.get('path') === '/router') {
      return Promise.resolve(answer({ conversations: [{ conversationId: ROUTER, title: 'Router', turnCount: 1, coordinatorId: null, folderId: null }], nextCursor: null, coverage: 'complete' }));
    }
    return base(url, init);
  }));
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountWorld(root, SHELL);
  await vi.advanceTimersByTimeAsync(1_000);
  shown(root);
  expect([...page.office()!.people.keys()]).toEqual([ROUTER]);
  await vi.advanceTimersByTimeAsync(10_000);
  shown(root);
  expect([...page.office()!.people.keys()].sort()).toEqual([LEAD, ROUTER, RUNNER].sort());
  page.dispose();
});

it("never flags the Watchdog for its own open signals: the owner sees '需要关注' only for an item he can act on", async () => {
  // Live shape 2026-11-18: 15 open quota signals, each an alarm fact on the Watchdog's conversation.
  const WATCHDOG = 'coordinator-watchdog';
  const alarms = Array.from({ length: 15 }, (_, index) => ({ fact: 'alarm', key: `alarm:sig${index}`, conversationId: WATCHDOG,
    source: 'watchdog_signal', signalId: `sig${index}` }));
  const base = fetch as unknown as (url: string, init: RequestInit) => Promise<Response>;
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
    const path = new URL(url, 'http://tofu.invalid').pathname;
    if (path === '/api/v4/router/watchdog') {
      return Promise.resolve(answer({ availability: 'available', conversationId: WATCHDOG, projectPath: '/watchdog' }));
    }
    if (path === '/api/v4/router/office') {
      return Promise.resolve(answer({ ...stream.snapshot, facts: [...stream.snapshot.facts, ...alarms] }));
    }
    return base(url, init);
  }));
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountWorld(root, SHELL);
  await vi.advanceTimersByTimeAsync(1_000);
  shown(root);
  const watchdog = page.office()!.people.get(WATCHDOG)!;
  expect(watchdog.role).toBe('watchdog');
  expect(watchdog.emphasis).toBeNull();
  expect(page.office()!.attention.exceptions).toEqual([]);
  expect(root.querySelector<HTMLElement>('.w-hud .w-chip[data-tone="exception"]')!.hidden).toBe(true);
  page.dispose();
});
