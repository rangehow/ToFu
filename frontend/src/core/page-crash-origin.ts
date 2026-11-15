/**
 * Responsibility: let any later tab of the same origin, owner and browser session
 * find a page instance that ended without a clean pagehide while it was visible,
 * and give each such instance exactly one reporter (docs/LOGGING.md "Browser crash
 * capture", contracts/client_crash_v1.schema.json).
 * Entry: startPageCrashOrigin, browserSessionId. Dependencies: injected
 * localStorage, Web Locks, visibility and journal-note ports; event-driven, no
 * timers or polling.
 *
 * A page holds a Web Lock named after its instance id for its whole lifetime; the
 * browser releases it when the document ends (closed, crashed or discarded). While
 * the page is visible and its owner is known it also publishes an origin record:
 * a `live` marker (owner, start time, browser session) plus copies of the session
 * journal's environment, context and freeze evidence. A clean exit or a hidden
 * page withdraws the record, so a record whose lock is free belongs to a page that
 * ended uncleanly while visible. Chrome discards only hidden tabs, without an
 * event, so a page that ends hidden is left to its own tab, which can tell a
 * discard from a crash (document.wasDiscarded).
 *
 * A reporter claims an instance by taking its lock and keeps the lock until its
 * own document ends; it deletes the record once the server took the report, so
 * neither a concurrent tab nor a later one reports the same instance again.
 */
import contract from '../../../contracts/client_crash_v1.schema.json';

/** Evidence the session journal keeps per instance; a published record carries a copy. */
export const ORIGIN_EVIDENCE_FIELDS = ['env', 'context', 'freeze'] as const;
export type OriginEvidenceField = typeof ORIGIN_EVIDENCE_FIELDS[number];

/** No Expires or Max-Age: a browser session ends it, unless the browser restores the session. */
export const BROWSER_SESSION_COOKIE = 'tofu_crash_session';

const PREFIX = 'tofu.crash.v1.';
const LIVE = 'live';
const FIELDS: ReadonlySet<string> = new Set([LIVE, ...ORIGIN_EVIDENCE_FIELDS]);
const ID_PATTERN = new RegExp(contract.$defs.id.pattern);
const ID_LENGTH: number = contract.$defs.id.maxLength;
/** A lock callback returns this, so the lock lasts until the document ends. */
const HELD_UNTIL_DOCUMENT_ENDS = new Promise<never>(() => undefined);

/** navigator.locks (Web Locks API). */
export interface CrashLockPort {
  request(name: string, options: { ifAvailable?: boolean },
    callback: (lock: unknown) => unknown): Promise<unknown>;
}

export interface OriginRecord {
  owner: number;
  startedAt: number;
  session: string;
}

export interface PageCrashOriginPorts {
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;
  locks: CrashLockPort;
  /** The document; null treats the page as always visible. */
  visibility: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'> | null;
  instanceId: string;
  startedAt: number;
  session: string;
  /** Before a record appears and after it is gone, the session journal notes the browser
   * session of the instance's record, or null for none. A refused note refuses the record. */
  notePublished(session: string | null): boolean;
}

/** What the tab's own journal does with one of its previous instances. */
export type SessionInstanceDecision = 'report' | 'keep' | 'drop';

export interface PageCrashOrigin {
  /** Resolves once this page holds its own lock, or the browser refused it. */
  readonly ready: Promise<void>;
  noteOwner(owner: number | null): void;
  noteEvidence(field: OriginEvidenceField, text: string): void;
  /** pagehide (true) and bfcache restore (false). */
  noteUnloaded(unloaded: boolean): void;
  /** The tab's own previous instance; `note` is what its journal noted about a record. */
  judgeSessionInstance(id: string, note: string | null): Promise<SessionInstanceDecision>;
  /** Claims another page's record when that page ended uncleanly in this browser session. */
  claimRecord(id: string): Promise<OriginRecord | null>;
  evidence(id: string, field: OriginEvidenceField): string | null;
  /** Deletes a delivered instance's record. */
  forget(id: string): void;
  /** Instance ids with origin keys, other than this page. */
  others(): string[];
  dispose(): void;
}

function parseObject(text: string | null): Record<string, unknown> | null {
  if (!text) return null;
  try {
    const value: unknown = JSON.parse(text);
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown> : null;
  } catch { return null; }
}

const validId = (id: unknown): id is string => typeof id === 'string' && id.length <= ID_LENGTH
  && ID_PATTERN.test(id);

/** This browser session's id, kept in a session cookie; minted when the session has none. */
export function browserSessionId(jar: { cookie: string }, mint: () => string, secure: boolean): string {
  const prefix = `${BROWSER_SESSION_COOKIE}=`;
  const existing = String(jar.cookie ?? '').split(';').map(part => part.trim())
    .find(part => part.startsWith(prefix))?.slice(prefix.length);
  if (validId(existing)) return existing;
  const id = mint();
  jar.cookie = `${prefix}${id}; Path=/; SameSite=Strict${secure ? '; Secure' : ''}`;
  return id;
}

export function startPageCrashOrigin(ports: PageCrashOriginPorts): PageCrashOrigin {
  const { storage, locks, instanceId, session } = ports;
  const keyOf = (id: string, field: string): string => `${PREFIX}${id}.${field}`;
  const lockOf = (id: string): string => `${PREFIX}${id}`;
  const read = (key: string): string | null => {
    try { return storage.getItem(key); } catch { return null; }
  };
  const write = (key: string, value: string): boolean => {
    try { storage.setItem(key, value); return true; } catch { return false; }
  };
  const remove = (key: string): void => {
    try { storage.removeItem(key); } catch { /* storage unavailable */ }
  };
  const forget = (id: string): void => {
    remove(keyOf(id, LIVE));
    for (const field of ORIGIN_EVIDENCE_FIELDS) remove(keyOf(id, field));
  };
  const evidence = new Map<OriginEvidenceField, string>();
  const claims = new Set<string>();
  let owner: number | null = null;
  let publishedOwner: number | null = null;
  let published = false;
  let lockHeld = false;
  let unloaded = false;
  let disposed = false;

  // The marker goes last on publish and first on withdraw, and the journal's note
  // brackets both: a tab that sees the marker also sees the evidence and the note.
  const withdraw = (): void => {
    if (!published) return;
    forget(instanceId);
    published = false;
    ports.notePublished(null);
  };
  const publish = (): void => {
    if (!ports.notePublished(session)) return;
    published = true;
    publishedOwner = owner;
    for (const [field, text] of evidence) write(keyOf(instanceId, field), text);
    if (!write(keyOf(instanceId, LIVE), JSON.stringify({ owner, startedAt: ports.startedAt, session }))) withdraw();
  };
  const reconcile = (): void => {
    const wanted = !disposed && lockHeld && !unloaded && owner !== null && !ports.visibility?.hidden;
    if (published && (!wanted || publishedOwner !== owner)) withdraw();
    if (wanted && !published) publish();
  };

  let resolveReady: () => void = () => undefined;
  const ready = new Promise<void>((resolve) => { resolveReady = resolve; });
  try {
    void locks.request(lockOf(instanceId), {}, () => {
      lockHeld = true;
      reconcile();
      resolveReady();
      return HELD_UNTIL_DOCUMENT_ENDS;
    }).catch(() => resolveReady());
  } catch { resolveReady(); }

  const onVisibility = (): void => reconcile();
  ports.visibility?.addEventListener('visibilitychange', onVisibility);

  /** Takes an instance's lock only if no document holds it, and keeps it until this document ends. */
  const claim = (id: string): Promise<boolean> => {
    if (claims.has(id)) return Promise.resolve(true);
    return new Promise((resolve) => {
      try {
        void locks.request(lockOf(id), { ifAvailable: true }, (lock) => {
          if (!lock) {
            resolve(false);
            return undefined;
          }
          claims.add(id);
          resolve(true);
          return HELD_UNTIL_DOCUMENT_ENDS;
        }).catch(() => resolve(false));
      } catch { resolve(false); }
    });
  };

  const record = (id: string): OriginRecord | null => {
    const value = parseObject(read(keyOf(id, LIVE)));
    const recordOwner = value?.owner;
    const startedAt = value?.startedAt;
    const recordSession = value?.session;
    return typeof recordOwner === 'number' && Number.isSafeInteger(recordOwner)
      && typeof startedAt === 'number' && Number.isSafeInteger(startedAt) && startedAt >= 0
      && validId(recordSession) ? { owner: recordOwner, startedAt, session: recordSession } : null;
  };

  const judgeSessionInstance = async (id: string, note: string | null): Promise<SessionInstanceDecision> => {
    if (note === session) {
      const live = record(id);
      // Only a reporter deletes a record its page left published.
      if (!live) return 'drop';
      // Another owner's tabs report it; with this tab's owner still unknown, decide later.
      if (live.owner !== owner) return 'keep';
      return await claim(id) && record(id) ? 'report' : 'drop';
    }
    // A held lock means the instance is alive: a document still unloading, or the
    // original of a duplicated tab whose storage this tab copied.
    if (!await claim(id)) return 'drop';
    // A record from an earlier browser session is garbage.
    if (note !== null) forget(id);
    return 'report';
  };

  const claimRecord = async (id: string): Promise<OriginRecord | null> => {
    const live = record(id);
    if (!live || live.session !== session) {
      // Leftovers of a page that ended mid-write, or of a page that ended with an
      // earlier browser session (a quit or a browser crash): never another tab's report.
      if (await claim(id)) forget(id);
      return null;
    }
    if (live.owner !== owner || !await claim(id)) return null;
    const current = record(id);
    return current && current.owner === owner && current.session === session ? current : null;
  };

  const others = (): string[] => {
    const ids = new Set<string>();
    try {
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index);
        if (!key?.startsWith(PREFIX)) continue;
        const rest = key.slice(PREFIX.length);
        const dot = rest.lastIndexOf('.');
        const id = rest.slice(0, dot);
        if (dot > 0 && FIELDS.has(rest.slice(dot + 1)) && validId(id) && id !== instanceId) ids.add(id);
      }
    } catch { /* storage unavailable */ }
    return [...ids];
  };

  return Object.freeze({
    ready,
    noteOwner(next: number | null) {
      owner = typeof next === 'number' && Number.isSafeInteger(next) ? next : null;
      reconcile();
    },
    noteEvidence(field: OriginEvidenceField, text: string) {
      evidence.set(field, text);
      if (published) write(keyOf(instanceId, field), text);
    },
    noteUnloaded(next: boolean) {
      unloaded = next;
      reconcile();
    },
    judgeSessionInstance,
    claimRecord,
    evidence: (id: string, field: OriginEvidenceField) => read(keyOf(id, field)),
    forget,
    others,
    dispose() {
      if (disposed) return;
      disposed = true;
      reconcile();
      ports.visibility?.removeEventListener('visibilitychange', onVisibility);
    },
  });
}
