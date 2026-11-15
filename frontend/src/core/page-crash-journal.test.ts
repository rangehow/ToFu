import { describe, expect, it, vi } from 'vitest';
import contract from '../../../contracts/client_crash_v1.schema.json';
import {
  crashContext,
  crashEnvironment,
  freezeEvidenceFromReport,
  installPageCrashInputs,
  newPageInstanceId,
  renderConsoleArguments,
  startPageCrashJournal,
  type ClientCrashReport,
  type CrashContext,
  type CrashLockPort,
  type PageCrashJournalPorts,
} from './page-crash-journal';
import { createTurnState } from './turn-state';

/** sessionStorage with Chrome's accounting: key + value UTF-16 units against a quota. */
class MemoryStorage {
  readonly map = new Map<string, string>();
  constructor(private readonly quotaUnits = Number.POSITIVE_INFINITY) {}
  get length(): number { return this.map.size; }
  key(index: number): string | null { return [...this.map.keys()][index] ?? null; }
  getItem(key: string): string | null { return this.map.get(key) ?? null; }
  setItem(key: string, value: string): void {
    const previous = this.map.get(key);
    const used = [...this.map].reduce((total, [k, v]) => total + k.length + v.length, 0);
    const next = used - (previous === undefined ? 0 : key.length + previous.length) + key.length + value.length;
    if (next > this.quotaUnits) throw Object.assign(new Error('quota'), { name: 'QuotaExceededError' });
    this.map.set(key, String(value));
  }
  removeItem(key: string): void { this.map.delete(key); }
  journalKeys(): string[] { return [...this.map.keys()].filter(key => key.startsWith('tofu.crash.v1.')); }
}

class Lifecycle {
  readonly listeners = new Map<string, Set<(event: Event) => void>>();
  addEventListener(type: string, listener: (event: Event) => void): void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }
  removeEventListener(type: string, listener: (event: Event) => void): void {
    this.listeners.get(type)?.delete(listener);
  }
  fire(type: string, fields: Record<string, unknown> = {}): void {
    for (const listener of this.listeners.get(type) ?? []) listener({ type, ...fields } as unknown as Event);
  }
}

/** navigator.locks shared by the tabs of one origin: exclusive named locks, ifAvailable and
 * FIFO waiting. A page's locks go away with its document (closed, crashed or discarded). */
class OriginLocks {
  private readonly held = new Map<string, { client: string; token: symbol }>();
  private readonly waiting = new Map<string, Array<() => void>>();

  client(client: string): CrashLockPort {
    return { request: (name, options, callback) => this.request(client, name, options, callback) };
  }

  holders(): string[] { return [...this.held.keys()].sort(); }

  /** The renderer of `client` crashed, or its document was closed or discarded. */
  destroy(client: string): void {
    for (const [name, holder] of [...this.held]) {
      if (holder.client === client) this.release(name, holder.token);
    }
  }

  private request(client: string, name: string, options: { ifAvailable?: boolean },
    callback: (lock: { name: string } | null) => unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const grant = (): void => {
        const token = Symbol(name);
        this.held.set(name, { client, token });
        Promise.resolve().then(() => callback({ name })).then(
          (value) => { this.release(name, token); resolve(value); },
          (error) => { this.release(name, token); reject(error); });
      };
      if (!this.held.has(name)) grant();
      else if (options.ifAvailable) Promise.resolve().then(() => callback(null)).then(resolve, reject);
      else this.waiting.set(name, [...(this.waiting.get(name) ?? []), grant]);
    });
  }

  private release(name: string, token: symbol): void {
    if (this.held.get(name)?.token !== token) return;
    this.held.delete(name);
    const next = this.waiting.get(name)?.shift();
    if (next) next();
  }
}

class Visibility extends Lifecycle {
  hidden = false;
  set(hidden: boolean): void {
    this.hidden = hidden;
    this.fire('visibilitychange');
  }
}

interface PageOptions {
  storage: MemoryStorage;
  origin?: MemoryStorage;
  locks?: OriginLocks;
  visibility?: Visibility;
  /** The browser session cookie's value; one per browser run (or restored with the session). */
  session?: string;
  id: string;
  navigationType?: string;
  wasDiscarded?: boolean;
  hasOpener?: boolean;
  now?: () => number;
  context?: () => CrashContext;
  crashReport?: PageCrashJournalPorts['crashReport'];
}

let clock = 1_000;

function page(options: PageOptions) {
  const lifecycle = new Lifecycle();
  const journal = startPageCrashJournal({
    storage: options.storage,
    lifecycle: lifecycle as unknown as Window,
    wasDiscarded: () => options.wasDiscarded ?? false,
    hasOpener: () => options.hasOpener ?? false,
    navigationType: () => options.navigationType ?? 'reload',
    now: options.now ?? (() => ++clock),
    newInstanceId: () => options.id,
    environment: () => ({ userAgent: 'Mozilla/5.0 Chrome/155.0.0.0', platform: 'macOS',
      extensions: ['immersive-translate'] }),
    context: options.context ?? (() => ({ view: 'chat', conversationId: 'conv-a', streamActive: true })),
    crashReport: options.crashReport ?? null,
    originStorage: options.origin ?? null,
    locks: options.locks?.client(options.id) ?? null,
    browserSession: options.origin ? () => options.session ?? 'browser-1' : null,
    visibility: options.visibility ?? null,
  });
  return { journal, lifecycle };
}

async function deliver(journal: ReturnType<typeof page>['journal']) {
  const reports: ClientCrashReport[] = [];
  await journal.sendPending(async report => { reports.push(report); });
  return reports;
}

describe('page crash journal', () => {
  it('reports a previous instance that never reached pagehide, once, with its breadcrumbs', async () => {
    const storage = new MemoryStorage();
    const first = page({ storage, id: 'page-a' });
    first.journal.recordError('error', 'TypeError: x is undefined', { source: 'app.js:1:2', stack: 'at f' });
    first.journal.recordError('unhandledrejection', 'boom');
    first.journal.recordError('console', '[sync] failed');
    first.journal.recordFreeze(freezeEvidenceFromReport(
      '[browser-responsiveness] {"event":"browser.main_thread_stall","durationMs":24300,'
      + '"usedJSHeapSize":4000000000,"jsHeapSizeLimit":4294705152,"domElements":152340}')!);
    // The renderer dies here: no pagehide. The user reloads the sad tab.
    const second = page({ storage, id: 'page-b' });
    const reports = await deliver(second.journal);
    expect(reports).toHaveLength(1);
    const [report] = reports;
    expect(report).toMatchObject({
      schema: 'tofu.client-crash/v1', pageInstanceId: 'page-a',
      classification: 'renderer_crash_suspected', navigationType: 'reload', droppedErrors: 0,
      context: { view: 'chat', conversationId: 'conv-a', streamActive: true },
      environment: { extensions: ['immersive-translate'] },
      freeze: { evidence: { durationMs: 24300, usedJSHeapSize: 4000000000, domElements: 152340 } },
    });
    expect(report.errors.map(error => [error.kind, error.message])).toEqual([
      ['error', 'TypeError: x is undefined'], ['unhandledrejection', 'boom'], ['console', '[sync] failed'],
    ]);
    expect(report.errors[0]).toMatchObject({ source: 'app.js:1:2', stack: 'at f' });
    expect(report.lastSeenAt).toBeGreaterThanOrEqual(report.freeze!.at);
    expect(report.detectedAt).toBeGreaterThan(report.startedAt);
    // Delivered episodes are gone; the next boot-ready sends nothing.
    expect(await deliver(second.journal)).toEqual([]);
    expect(storage.journalKeys().every(key => !key.includes('page-a'))).toBe(true);
  });

  it('does not report a clean unload, a reload or a navigation', async () => {
    for (const navigationType of ['reload', 'navigate']) {
      const storage = new MemoryStorage();
      const first = page({ storage, id: 'page-a' });
      first.journal.recordError('error', 'kept until the next start');
      first.lifecycle.fire('pagehide', { persisted: false });
      const second = page({ storage, id: 'page-b', navigationType });
      expect(await deliver(second.journal)).toEqual([]);
      // The cleanly exited instance's breadcrumbs are collected at the next start.
      expect(storage.journalKeys().some(key => key.includes('page-a'))).toBe(false);
    }
  });

  it('classifies a discarded tab and a restored or duplicated tab without calling them crashes', async () => {
    const discarded = new MemoryStorage();
    page({ storage: discarded, id: 'page-a' });
    const afterDiscard = page({ storage: discarded, id: 'page-b', wasDiscarded: true });
    expect((await deliver(afterDiscard.journal)).map(report => report.classification)).toEqual(['tab_discarded']);

    const restored = new MemoryStorage();
    page({ storage: restored, id: 'page-a' });
    const afterRestore = page({ storage: restored, id: 'page-b', navigationType: 'back_forward' });
    expect((await deliver(afterRestore.journal)).map(report => report.classification))
      .toEqual(['session_restored']);
  });

  it('ignores the copied journal an opener window starts with', async () => {
    const storage = new MemoryStorage();
    page({ storage, id: 'page-a' }).journal.recordError('error', 'belongs to the opener');
    // window.open without noopener copies the opener's sessionStorage.
    const copy = new MemoryStorage();
    for (const [key, value] of storage.map) copy.map.set(key, value);
    const opened = page({ storage: copy, id: 'page-b', navigationType: 'navigate', hasOpener: true });
    expect(await deliver(opened.journal)).toEqual([]);
    expect(copy.journalKeys().some(key => key.includes('page-a'))).toBe(false);
    // The opened window's own later crash is still reported.
    const reloaded = page({ storage: copy, id: 'page-c', navigationType: 'reload', hasOpener: true });
    expect((await deliver(reloaded.journal)).map(report => report.pageInstanceId)).toEqual(['page-b']);
  });

  it('handles bfcache: pagehide(persisted) clears the marker and pageshow(persisted) resumes it', async () => {
    const restoredAndCrashed = new MemoryStorage();
    const first = page({ storage: restoredAndCrashed, id: 'page-a' });
    first.lifecycle.fire('pagehide', { persisted: true });
    expect(restoredAndCrashed.getItem('tofu.crash.v1.alive')).toBeNull();
    first.lifecycle.fire('pageshow', { persisted: true });
    expect(JSON.parse(restoredAndCrashed.getItem('tofu.crash.v1.alive')!).id).toBe('page-a');
    const second = page({ storage: restoredAndCrashed, id: 'page-b' });
    expect((await deliver(second.journal)).map(report => report.pageInstanceId)).toEqual(['page-a']);

    const evicted = new MemoryStorage();
    const cached = page({ storage: evicted, id: 'page-a' });
    cached.lifecycle.fire('pagehide', { persisted: true });
    const fresh = page({ storage: evicted, id: 'page-b', navigationType: 'back_forward' });
    expect(await deliver(fresh.journal)).toEqual([]);
  });

  it('re-claims the marker when a previous document still unloading removes it', () => {
    const storage = new MemoryStorage();
    const current = page({ storage, id: 'page-b' });
    storage.removeItem('tofu.crash.v1.alive');
    current.lifecycle.fire('storage', { key: 'tofu.crash.v1.alive', newValue: null, storageArea: storage });
    expect(JSON.parse(storage.getItem('tofu.crash.v1.alive')!).id).toBe('page-b');
  });

  it('keeps an undelivered report for the next instance and sends episodes in order', async () => {
    const storage = new MemoryStorage();
    page({ storage, id: 'page-a' }).journal.recordError('error', 'first');
    const second = page({ storage, id: 'page-b' });
    const refused = vi.fn(async () => { throw new Error('503 server_booting'); });
    await second.journal.sendPending(refused);
    expect(refused).toHaveBeenCalledTimes(1);
    // page-b crashes too before any delivery succeeded.
    const third = page({ storage, id: 'page-c' });
    expect((await deliver(third.journal)).map(report => report.pageInstanceId)).toEqual(['page-a', 'page-b']);
  });

  it('folds repeated identical errors into one counted entry', async () => {
    const storage = new MemoryStorage();
    const first = page({ storage, id: 'page-a' });
    for (let index = 0; index < 5; index++) first.journal.recordError('console', 'same');
    first.journal.recordError('console', 'other');
    const [report] = await deliver(page({ storage, id: 'page-b' }).journal);
    expect(report.errors.map(error => [error.message, error.count ?? 1])).toEqual([['same', 5], ['other', 1]]);
    expect(report.errors[0].lastAt).toBeGreaterThan(report.errors[0].at);
  });

  it('drops the oldest errors when sessionStorage refuses a write, and counts them', async () => {
    const appKey = 'tofu_activeConvId';
    const storage = new MemoryStorage(6_000);
    storage.setItem(appKey, 'conv-a');
    const first = page({ storage, id: 'page-a' });
    for (let index = 0; index < 40; index++) first.journal.recordError('error', `error ${index} ${'x'.repeat(200)}`);
    expect(storage.getItem(appKey)).toBe('conv-a');
    const [report] = await deliver(page({ storage, id: 'page-b' }).journal);
    const kept = report.errors.map(error => Number(error.message.split(' ')[1]));
    expect(kept.length).toBeGreaterThan(0);
    expect(kept.at(-1)).toBe(39);
    expect(kept).toEqual([...kept].sort((left, right) => left - right));
    expect(report.droppedErrors).toBe(40 - kept.length);
  });

  it('never keeps more than one deliverable report per instance', async () => {
    const storage = new MemoryStorage();
    const first = page({ storage, id: 'page-a' });
    const message = 'y'.repeat(contract.$defs.errorBreadcrumb.properties.message.maxLength);
    const total = Math.ceil(contract.maxBodyBytes / message.length) + 20;
    for (let index = 0; index < total; index++) first.journal.recordError('error', `${index} ${message}`);
    // The stored journal itself stays within one deliverable report, before any send.
    const stored = [...storage.map].filter(([key]) => key.startsWith('tofu.crash.v1.page-a.'))
      .reduce((bytes, [, value]) => bytes + new TextEncoder().encode(value).length, 0);
    expect(stored).toBeLessThanOrEqual(contract.maxBodyBytes);
    const [report] = await deliver(page({ storage, id: 'page-b' }).journal);
    expect(new TextEncoder().encode(JSON.stringify(report)).length).toBeLessThanOrEqual(contract.maxBodyBytes);
    expect(report.errors.at(-1)!.message.startsWith(`${total - 1} `)).toBe(true);
    expect(report.errors.length + report.droppedErrors).toBe(total);
  });

  it('feeds the crash report context once the browser buffer is ready', async () => {
    const values = new Map<string, string>();
    let initialized = 0;
    const crashReport = {
      initialize: async (length: number) => { initialized = length; },
      set: (key: string, value: string) => { values.set(key, value); },
    };
    const storage = new MemoryStorage();
    const { journal } = page({ storage, id: 'page-a', crashReport });
    await Promise.resolve();
    journal.recordFreeze({ durationMs: 1200, usedJSHeapSize: 10, jsHeapSizeLimit: 20, domElements: 30 });
    expect(initialized).toBeGreaterThan(0);
    expect(Object.fromEntries(values)).toMatchObject({
      pageInstanceId: 'page-a', view: 'chat', conversationId: 'conv-a', streamActive: '1',
      freezeMs: '1200', usedJSHeapSize: '10', jsHeapSizeLimit: '20', domElements: '30',
    });
    const longest = Object.entries(contract.crashContextKeys)
      .reduce((map, [key, length]) => ({ ...map, [key]: '\u0001'.repeat(length) }), {});
    expect(new TextEncoder().encode(JSON.stringify(longest)).length).toBeLessThanOrEqual(initialized);
  });

  it('writes context only when the view, conversation or stream state changes', () => {
    const storage = new MemoryStorage();
    let context: CrashContext = { view: 'chat', conversationId: 'conv-a', streamActive: false };
    const writes = vi.spyOn(storage, 'setItem');
    const { journal } = page({ storage, id: 'page-a', context: () => context });
    const baseline = writes.mock.calls.length;
    for (let index = 0; index < 100; index++) journal.noteContext();
    expect(writes.mock.calls.length).toBe(baseline);
    context = { ...context, streamActive: true };
    journal.noteContext();
    expect(writes.mock.calls.filter(([key]) => key === 'tofu.crash.v1.page-a.context')).toHaveLength(2);
  });
});

describe('page crash journal across tabs', () => {
  const STALL = '[browser-responsiveness] {"event":"browser.main_thread_stall","durationMs":24300,'
    + '"usedJSHeapSize":4000000000,"jsHeapSizeLimit":4294705152,"domElements":152340}';
  const ids = (reports: ClientCrashReport[]) => reports.map(report => report.pageInstanceId);
  const live = (origin: MemoryStorage, id: string) => origin.journalKeys().filter(key => key.includes(id));

  async function tab(origin: MemoryStorage, locks: OriginLocks, id: string, owner: number | null = 1,
    options: Partial<PageOptions> = {}) {
    const opened = page({ storage: new MemoryStorage(), navigationType: 'navigate', ...options, origin, locks, id });
    await opened.journal.noteOwner(owner);
    return opened;
  }

  it('reports a crashed tab that was closed and replaced from any later tab of the owner, once', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const crashed = await tab(origin, locks, 'page-a');
    crashed.journal.recordFreeze(freezeEvidenceFromReport(STALL)!);
    // The renderer dies (no pagehide) and the owner closes the sad tab.
    locks.destroy('page-a');
    const replacement = await tab(origin, locks, 'page-b');
    const reports = await deliver(replacement.journal);
    expect(reports).toHaveLength(1);
    expect(reports[0]).toMatchObject({
      schema: 'tofu.client-crash/v1', pageInstanceId: 'page-a',
      classification: 'renderer_crash_suspected', navigationType: 'navigate',
      context: { view: 'chat', conversationId: 'conv-a', streamActive: true },
      environment: { extensions: ['immersive-translate'] },
      freeze: { evidence: { durationMs: 24300, usedJSHeapSize: 4000000000, domElements: 152340 } },
      errors: [], droppedErrors: 0,
    });
    expect(Object.keys(reports[0]).sort()).toEqual(Object.keys(contract.$defs.clientCrashReport.properties).sort());
    expect(reports[0].lastSeenAt).toBeGreaterThanOrEqual(reports[0].freeze!.at);
    expect(reports[0].detectedAt).toBeGreaterThan(reports[0].lastSeenAt);
    expect(await deliver(replacement.journal)).toEqual([]);
    expect(await deliver((await tab(origin, locks, 'page-c')).journal)).toEqual([]);
    expect(live(origin, 'page-a')).toEqual([]);
  });

  it('never lets two tabs report the same dead instance, even from a stale storage view', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    await tab(origin, locks, 'page-a');
    locks.destroy('page-a');
    const stale = new MemoryStorage();
    for (const [key, value] of origin.map) stale.map.set(key, value);
    const [left, right] = await Promise.all([tab(origin, locks, 'page-b'), tab(origin, locks, 'page-c')]);
    const reports = (await Promise.all([deliver(left.journal), deliver(right.journal)])).flat();
    expect(ids(reports)).toEqual(['page-a']);
    // A tab whose storage view has not yet seen the claim still finds the instance taken.
    const late = await tab(stale, locks, 'page-d');
    expect(await deliver(late.journal)).toEqual([]);
  });

  it('leaves live tabs and clean exits alone', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const open = await tab(origin, locks, 'page-a');
    expect(await deliver((await tab(origin, locks, 'page-b')).journal)).toEqual([]);
    open.lifecycle.fire('pagehide', { persisted: false });
    locks.destroy('page-a');
    expect(live(origin, 'page-a')).toEqual([]);
    expect(await deliver((await tab(origin, locks, 'page-c')).journal)).toEqual([]);
  });

  it('leaves a page that died hidden to its own tab, which can tell a discard from a crash', async () => {
    for (const wasDiscarded of [true, false]) {
      const origin = new MemoryStorage();
      const locks = new OriginLocks();
      const visibility = new Visibility();
      const session = new MemoryStorage();
      const background = page({ storage: session, origin, locks, visibility, id: 'page-a' });
      await background.journal.noteOwner(1);
      visibility.set(true);
      expect(live(origin, 'page-a')).toEqual([]);
      // Memory Saver discards hidden tabs without an event; a hidden page may also crash.
      locks.destroy('page-a');
      expect(await deliver((await tab(origin, locks, 'page-b')).journal)).toEqual([]);
      const reopened = page({ storage: session, origin, locks, id: 'page-c', wasDiscarded });
      await reopened.journal.noteOwner(1);
      expect((await deliver(reopened.journal)).map(report => report.classification))
        .toEqual([wasDiscarded ? 'tab_discarded' : 'renderer_crash_suspected']);
    }

    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const shown = new Visibility();
    await tab(origin, locks, 'page-d', 1, { visibility: shown });
    shown.set(true);
    shown.set(false);
    locks.destroy('page-d');
    expect((await deliver((await tab(origin, locks, 'page-e')).journal)).map(report => report.classification))
      .toEqual(['renderer_crash_suspected']);
  });

  it('reports a dead instance only to its own owner and never before the owner is known', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    await tab(origin, locks, 'page-a', 1);
    locks.destroy('page-a');
    expect(await deliver((await tab(origin, locks, 'page-b', 2)).journal)).toEqual([]);
    expect(await deliver((await tab(origin, locks, 'page-c', null)).journal)).toEqual([]);
    expect(live(origin, 'page-a')).not.toEqual([]);
    expect(ids(await deliver((await tab(origin, locks, 'page-d', 1)).journal))).toEqual(['page-a']);
  });

  it('shares one claim between the crashed tab\'s own reload and other tabs', async () => {
    for (const reloadFirst of [true, false]) {
      const origin = new MemoryStorage();
      const locks = new OriginLocks();
      const session = new MemoryStorage();
      const crashed = page({ storage: session, origin, locks, id: 'page-a' });
      await crashed.journal.noteOwner(1);
      crashed.journal.recordError('error', 'before the crash');
      locks.destroy('page-a');
      const reload = async () => {
        const reloaded = page({ storage: session, origin, locks, id: 'page-r', navigationType: 'reload' });
        await reloaded.journal.noteOwner(1);
        return deliver(reloaded.journal);
      };
      const other = async () => deliver((await tab(origin, locks, 'page-o')).journal);
      const [firstReports, secondReports] = reloadFirst
        ? [await reload(), await other()] : [await other(), await reload()];
      expect(ids([...firstReports, ...secondReports])).toEqual(['page-a']);
      // The tab's own reload has the full breadcrumbs; another tab has what the origin record carried.
      expect(firstReports[0].errors.map(error => error.message))
        .toEqual(reloadFirst ? ['before the crash'] : []);
      expect(firstReports[0].navigationType).toBe(reloadFirst ? 'reload' : 'navigate');
      expect(session.journalKeys().some(key => key.includes('page-a'))).toBe(false);
      expect(live(origin, 'page-a')).toEqual([]);
    }
  });

  it('keeps reporting an instance that died before its owner was known from its own tab', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const session = new MemoryStorage();
    page({ storage: session, origin, locks, id: 'page-a' });
    locks.destroy('page-a');
    expect(await deliver((await tab(origin, locks, 'page-b')).journal)).toEqual([]);
    const reloaded = page({ storage: session, origin, locks, id: 'page-c' });
    await reloaded.journal.noteOwner(1);
    expect(ids(await deliver(reloaded.journal))).toEqual(['page-a']);
  });

  it('does not report a document that is still unloading when its tab reloads', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const session = new MemoryStorage();
    const unloading = page({ storage: session, origin, locks, id: 'page-a' });
    await unloading.journal.noteOwner(1);
    // Cross-process reload: the new document starts before the old one ran pagehide.
    const reloaded = page({ storage: session, origin, locks, id: 'page-b' });
    unloading.lifecycle.fire('pagehide', { persisted: false });
    locks.destroy('page-a');
    await reloaded.journal.noteOwner(1);
    expect(await deliver(reloaded.journal)).toEqual([]);
    expect(await deliver((await tab(origin, locks, 'page-c')).journal)).toEqual([]);
  });

  it('withdraws the origin record for bfcache and republishes it on restore', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const cached = await tab(origin, locks, 'page-a');
    cached.lifecycle.fire('pagehide', { persisted: true });
    expect(live(origin, 'page-a')).toEqual([]);
    cached.lifecycle.fire('pageshow', { persisted: true });
    expect(live(origin, 'page-a')).not.toEqual([]);
    locks.destroy('page-a');
    expect(ids(await deliver((await tab(origin, locks, 'page-b')).journal))).toEqual(['page-a']);
  });

  it('treats records of an earlier browser session as garbage, never as crashes', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    await tab(origin, locks, 'page-a', 1, { session: 'browser-1' });
    // The browser ended without pagehide (a kill, a browser crash) and dropped its session cookie.
    locks.destroy('page-a');
    expect(await deliver((await tab(origin, locks, 'page-b', 1, { session: 'browser-2' })).journal)).toEqual([]);
    expect(live(origin, 'page-a')).toEqual([]);
  });

  it('reports what a restored tab cannot judge as session_restored, never as crashes', async () => {
    // An abrupt end (OS shutdown, kill) runs no pagehide, and "Continue where you left off"
    // restores the session cookie with the tabs, which load as back_forward.
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const restoredSession = new MemoryStorage();
    const first = page({ storage: restoredSession, origin, locks, id: 'page-a' });
    await first.journal.noteOwner(1);
    await tab(origin, locks, 'page-b');
    locks.destroy('page-a');
    locks.destroy('page-b');
    const restored = page({ storage: restoredSession, origin, locks, id: 'page-c', navigationType: 'back_forward' });
    await restored.journal.noteOwner(1);
    expect((await deliver(restored.journal)).map(report => [report.pageInstanceId, report.classification]).sort())
      .toEqual([['page-a', 'session_restored'], ['page-b', 'session_restored']]);
    expect(await deliver((await tab(origin, locks, 'page-d')).journal)).toEqual([]);
  });

  it('never lets a duplicated tab report its live original', async () => {
    const origin = new MemoryStorage();
    const locks = new OriginLocks();
    const originalSession = new MemoryStorage();
    const original = page({ storage: originalSession, origin, locks, id: 'page-a' });
    await original.journal.noteOwner(1);
    // Duplicating a tab copies its sessionStorage and loads as back_forward.
    const copy = new MemoryStorage();
    for (const [key, value] of originalSession.map) copy.map.set(key, value);
    const duplicate = page({ storage: copy, origin, locks, id: 'page-b', navigationType: 'back_forward' });
    await duplicate.journal.noteOwner(1);
    expect(await deliver(duplicate.journal)).toEqual([]);
    expect(copy.journalKeys().some(key => key.includes('page-a'))).toBe(false);
    // The original closes cleanly later; nothing is reported for it anywhere.
    original.lifecycle.fire('pagehide', { persisted: false });
    locks.destroy('page-a');
    expect(await deliver((await tab(origin, locks, 'page-c')).journal)).toEqual([]);
  });

  it('stays tab-scoped without Web Locks', async () => {
    const origin = new MemoryStorage();
    const session = new MemoryStorage();
    const first = page({ storage: session, origin, id: 'page-a' });
    await first.journal.noteOwner(1);
    expect(origin.journalKeys()).toEqual([]);
    const reloaded = page({ storage: session, origin, id: 'page-b' });
    expect(ids(await deliver(reloaded.journal))).toEqual(['page-a']);
  });
});

describe('page crash journal inputs', () => {
  it('reads only markers and browser facts for the environment', () => {
    const selectors: string[] = [];
    const environment = crashEnvironment({
      userAgent: 'UA', platform: 'MacIntel', deviceMemory: 8, hardwareConcurrency: 10,
      userAgentData: { brands: [{ brand: 'Google Chrome', version: '155' }], platform: 'macOS', mobile: false },
    }, { querySelector: (selector: string) => { selectors.push(selector); return selector.includes('gr-ext') ? {} as Element : null; } });
    expect(environment).toEqual({ userAgent: 'UA', brands: ['Google Chrome/155'], platform: 'macOS',
      mobile: false, deviceMemoryGiB: 8, hardwareConcurrency: 10, extensions: ['grammarly'] });
    expect(selectors).toHaveLength(2);
  });

  it('derives view and stream state from the canonical latest main-lane Turn', () => {
    const state = createTurnState('conv-a');
    state.laneOrder.main = ['turn-1'];
    state.turnsById['turn-1'] = { turnId: 'turn-1', status: 'running' } as never;
    expect(crashContext('#/evolution/run-1', 'conv-a', state))
      .toEqual({ view: 'evolution', conversationId: 'conv-a', streamActive: true });
    state.turnsById['turn-1'] = { turnId: 'turn-1', status: 'completed' } as never;
    expect(crashContext('', 'conv-a', state)).toEqual({ view: 'chat', conversationId: 'conv-a', streamActive: false });
    expect(crashContext('#tofu-agent-relay', null, null)).toEqual({ view: 'chat', conversationId: '', streamActive: false });
  });

  it('journals uncaught errors, rejections and console.error, and restores console on dispose', () => {
    const recorded: unknown[][] = [];
    let routes = 0;
    const journal = {
      recordError: (...args: unknown[]) => { recorded.push(args); },
      noteContext: () => { routes++; },
    };
    const events = new Lifecycle();
    const printed: unknown[][] = [];
    const nativeError = (...args: unknown[]) => { printed.push(args); };
    const consoleLike = { error: nativeError };
    const dispose = installPageCrashInputs(journal as never, events as unknown as Window, consoleLike);
    events.fire('error', { message: 'TypeError: boom', filename: 'https://h/app.js', lineno: 3, colno: 9,
      error: { stack: 'TypeError: boom\n    at f' } });
    events.fire('unhandledrejection', { reason: new Error('lost') });
    events.fire('unhandledrejection', { reason: 'plain reason' });
    consoleLike.error('[sync] failed', { id: 1 });
    events.fire('hashchange');
    expect(printed).toEqual([['[sync] failed', { id: 1 }]]);
    expect(recorded.map(([kind, message]) => [kind, message])).toEqual([
      ['error', 'TypeError: boom'], ['unhandledrejection', 'lost'], ['unhandledrejection', 'plain reason'],
      ['console', '[sync] failed {"id":1}'],
    ]);
    expect(recorded[0][2]).toEqual({ source: 'https://h/app.js:3:9', stack: 'TypeError: boom\n    at f' });
    expect(routes).toBe(1);
    dispose();
    expect(consoleLike.error).toBe(nativeError);
    expect([...events.listeners.values()].every(set => set.size === 0)).toBe(true);
  });

  it('mints page instance ids that satisfy the contract id pattern', () => {
    const pattern = new RegExp(contract.$defs.id.pattern);
    const insecure = { getRandomValues: <T extends ArrayBufferView>(array: T) => array };
    expect(newPageInstanceId(insecure as never)).toMatch(pattern);
    expect(newPageInstanceId({ ...insecure, randomUUID: () => '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10' } as never))
      .toMatch(pattern);
  });

  it('renders console arguments like the relay and keeps the first Error stack', () => {
    const error = new Error('bad');
    expect(renderConsoleArguments(['[sync]', error, { a: 1 }]))
      .toEqual({ message: '[sync] Error: bad {"a":1}', stack: error.stack });
    expect(renderConsoleArguments(['plain'])).toEqual({ message: 'plain' });
    expect(freezeEvidenceFromReport('not json')).toBeNull();
  });
});
