/**
 * Responsibility: notice that this tab's previous page instance ended without
 * a clean pagehide (a suspected renderer crash), keep each instance's crash
 * breadcrumbs in sessionStorage as they happen, and hand every unreported
 * episode to an injected sender until the server takes it
 * (contracts/client_crash_v1.schema.json, docs/LOGGING.md "Browser crash capture").
 * Entry: startPageCrashJournal. Dependencies: injected storage, lifecycle,
 * classification, environment, context, crash-report-context, clock and id
 * ports, and for reports across tabs page-crash-origin.ts with localStorage,
 * Web Locks, visibility and browser-session ports; no import-time browser work.
 * Event-driven only: no timers or polling. Retention is one deliverable report
 * per instance (the contract's maxBodyBytes) inside sessionStorage's own quota;
 * a refused write drops the oldest errors first and counts them.
 */
import contract from '../../../contracts/client_crash_v1.schema.json';
import {
  ORIGIN_EVIDENCE_FIELDS,
  startPageCrashOrigin,
  type CrashLockPort,
  type OriginEvidenceField,
  type OriginRecord,
  type PageCrashOrigin,
} from './page-crash-origin';
import type { TurnState } from './turn-state';

export type { CrashLockPort } from './page-crash-origin';

export type CrashClassification = 'renderer_crash_suspected' | 'tab_discarded' | 'session_restored';
export type CrashErrorKind = 'error' | 'unhandledrejection' | 'console';

export interface CrashEnvironment {
  userAgent: string;
  brands?: string[];
  platform?: string;
  mobile?: boolean;
  deviceMemoryGiB?: number;
  hardwareConcurrency?: number;
  extensions: string[];
}

export interface CrashContext {
  view: string;
  conversationId: string;
  streamActive: boolean;
}

export interface CrashErrorBreadcrumb {
  at: number;
  lastAt?: number;
  kind: CrashErrorKind;
  message: string;
  source?: string;
  stack?: string;
  count?: number;
}

export interface ClientCrashReport {
  schema: 'tofu.client-crash/v1';
  pageInstanceId: string;
  classification: CrashClassification;
  startedAt: number;
  lastSeenAt: number;
  detectedAt: number;
  navigationType: string;
  environment: CrashEnvironment | null;
  context: (CrashContext & { at: number }) | null;
  freeze: { at: number; evidence: Record<string, unknown> } | null;
  errors: CrashErrorBreadcrumb[];
  droppedErrors: number;
}

/** window.crashReport (WICG crash-reporting key-value API, Chrome 145+). */
export interface CrashReportContextPort {
  initialize(length: number): Promise<unknown>;
  set(key: string, value: string): void;
}

export interface PageCrashJournalPorts {
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;
  lifecycle: Pick<Window, 'addEventListener' | 'removeEventListener'>;
  wasDiscarded(): boolean;
  /** window.opener is set: this window may have started with a copy of its opener's storage. */
  hasOpener(): boolean;
  navigationType(): string;
  now(): number;
  newInstanceId(): string;
  environment(): CrashEnvironment;
  context(): CrashContext;
  crashReport?: CrashReportContextPort | null;
  /** Reports across tabs need all of: localStorage, navigator.locks and this browser
   * session's id (page-crash-origin.ts); without them the journal stays tab-scoped. */
  originStorage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'> | null;
  locks?: CrashLockPort | null;
  browserSession?: (() => string) | null;
  /** The document; absent treats the page as always visible. */
  visibility?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'> | null;
}

/** Resolves when the server took the report (stored or refused as invalid); rejects to retry later. */
export type CrashReportSender = (report: ClientCrashReport) => Promise<unknown>;

export interface PageCrashJournal {
  readonly instanceId: string;
  recordError(kind: CrashErrorKind, message: unknown, details?: { source?: unknown; stack?: unknown }): void;
  recordFreeze(evidence: Record<string, unknown>): void;
  noteContext(): void;
  /** The page's owner (null while unknown). Resolves once the origin record reflects it. */
  noteOwner(owner: number | null): Promise<void>;
  sendPending(send: CrashReportSender): Promise<void>;
  dispose(): void;
}

const PREFIX = 'tofu.crash.v1.';
const ALIVE_KEY = `${PREFIX}alive`;
const MAX_REPORT_BYTES: number = contract.maxBodyBytes;
const ERROR_FIELDS = contract.$defs.errorBreadcrumb.properties;
const LABEL_LENGTH: number = contract.$defs.label.maxLength;
const ID_PATTERN = new RegExp(contract.$defs.id.pattern);
const ID_LENGTH: number = contract.$defs.id.maxLength;
const CRASH_CONTEXT_KEYS: Record<string, number> = contract.crashContextKeys;

/** Known extension DOM markers. Presence only; no page content is read. */
export const EXTENSION_MARKERS: ReadonlyArray<{ id: string; selector: string }> = Object.freeze([
  { id: 'grammarly',
    selector: 'grammarly-desktop-integration,[data-gr-ext-installed],[data-new-gr-c-s-check-loaded]' },
  { id: 'immersive-translate',
    selector: '[data-immersive-translate-page-theme],#immersive-translate-popup,[data-immersive-translate-walked]' },
]);

interface NavigatorLike {
  readonly userAgent?: string;
  readonly platform?: string;
  readonly deviceMemory?: number;
  readonly hardwareConcurrency?: number;
  readonly userAgentData?: {
    readonly brands?: ReadonlyArray<{ brand?: string; version?: string }>;
    readonly platform?: string;
    readonly mobile?: boolean;
  };
}

/** Browser, platform and extension markers; read when the journal notes a change. */
export function crashEnvironment(navigatorLike: NavigatorLike,
  documentLike: Pick<Document, 'querySelector'>): CrashEnvironment {
  const data = navigatorLike.userAgentData;
  const environment: CrashEnvironment = { userAgent: String(navigatorLike.userAgent ?? ''), extensions: [] };
  if (Array.isArray(data?.brands)) {
    environment.brands = data.brands.map(item => `${String(item.brand ?? '')}/${String(item.version ?? '')}`);
  }
  const platform = data?.platform || navigatorLike.platform;
  if (platform) environment.platform = String(platform);
  if (typeof data?.mobile === 'boolean') environment.mobile = data.mobile;
  if (typeof navigatorLike.deviceMemory === 'number') environment.deviceMemoryGiB = navigatorLike.deviceMemory;
  if (typeof navigatorLike.hardwareConcurrency === 'number') {
    environment.hardwareConcurrency = navigatorLike.hardwareConcurrency;
  }
  for (const marker of EXTENSION_MARKERS) {
    try {
      if (documentLike.querySelector(marker.selector)) environment.extensions.push(marker.id);
    } catch { /* A detached document cannot be queried. */ }
  }
  return environment;
}

/** View, conversation and whether its latest main-lane Turn is streaming. */
export function crashContext(locationHash: string, conversationId: unknown,
  state?: TurnState | null): CrashContext {
  const route = locationHash.startsWith('#/') ? locationHash.slice(2).split('/')[0] : '';
  const ids = state?.laneOrder.main ?? [];
  const latest = state?.turnsById[ids[ids.length - 1]];
  return {
    view: (route || 'chat').slice(0, LABEL_LENGTH),
    conversationId: typeof conversationId === 'string' ? conversationId.slice(0, LABEL_LENGTH) : '',
    streamActive: latest?.status === 'pending' || latest?.status === 'running',
  };
}

/** The structured record of one '[browser-responsiveness] {json}' report line. */
export function freezeEvidenceFromReport(message: string): Record<string, unknown> | null {
  const start = message.indexOf('{');
  if (start < 0) return null;
  try {
    const value: unknown = JSON.parse(message.slice(start));
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown> : null;
  } catch { return null; }
}

/** One console.error call as the console relay renders it, plus the first Error stack. */
export function renderConsoleArguments(args: readonly unknown[]): { message: string; stack?: string } {
  let stack: string | undefined;
  const parts = args.map((value) => {
    if (typeof value === 'string') return value;
    const error = value as { name?: unknown; message?: unknown; stack?: unknown } | null;
    if (error && typeof error === 'object' && typeof error.message === 'string') {
      if (stack === undefined && typeof error.stack === 'string') stack = error.stack;
      return `${String(error.name ?? 'Error')}: ${error.message}`;
    }
    try {
      const encoded = JSON.stringify(value);
      return encoded === undefined ? String(value) : encoded;
    } catch { return String(value); }
  });
  return stack === undefined ? { message: parts.join(' ') } : { message: parts.join(' '), stack };
}

function utf8Length(text: string): number {
  let bytes = 0;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length) { bytes += 4; index++; }
    else bytes += 3;
  }
  return bytes;
}

/** A random page instance id; randomUUID needs a secure context, getRandomValues does not. */
export function newPageInstanceId(cryptoLike: Pick<Crypto, 'getRandomValues'> & { randomUUID?: () => string }): string {
  if (typeof cryptoLike.randomUUID === 'function') return cryptoLike.randomUUID();
  return Array.from(cryptoLike.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');
}

interface ErrorEventLike { message?: unknown; filename?: unknown; lineno?: unknown; colno?: unknown;
  error?: { stack?: unknown } | null }
interface RejectionEventLike { reason?: unknown }

/** Feed uncaught errors, unhandled rejections, console.error calls and route
 * changes into the journal. Returns the disposer that restores console.error. */
export function installPageCrashInputs(journal: Pick<PageCrashJournal, 'recordError' | 'noteContext'>,
  events: Pick<Window, 'addEventListener' | 'removeEventListener'>,
  consoleLike: { error: (...args: unknown[]) => void }): () => void {
  const onError = (event: Event): void => {
    const error = event as unknown as ErrorEventLike;
    const source = error.filename ? `${String(error.filename)}:${String(error.lineno ?? '?')}:${String(error.colno ?? '?')}` : '';
    journal.recordError('error', error.message, { source, stack: error.error?.stack });
  };
  const onRejection = (event: Event): void => {
    const reason = (event as unknown as RejectionEventLike).reason as { message?: unknown; stack?: unknown } | null;
    journal.recordError('unhandledrejection',
      reason && typeof reason === 'object' && reason.message !== undefined ? reason.message : String(reason),
      { stack: reason && typeof reason === 'object' ? reason.stack : undefined });
  };
  const onRoute = (): void => journal.noteContext();
  const nativeError = consoleLike.error;
  const wrappedError = function (this: unknown, ...args: unknown[]): void {
    try {
      const rendered = renderConsoleArguments(args);
      journal.recordError('console', rendered.message, { stack: rendered.stack });
    } catch { /* The original console call always runs. */ }
    nativeError.apply(this, args);
  };
  consoleLike.error = wrappedError;
  events.addEventListener('error', onError);
  events.addEventListener('unhandledrejection', onRejection);
  events.addEventListener('hashchange', onRoute);
  events.addEventListener('popstate', onRoute);
  return () => {
    events.removeEventListener('error', onError);
    events.removeEventListener('unhandledrejection', onRejection);
    events.removeEventListener('hashchange', onRoute);
    events.removeEventListener('popstate', onRoute);
    if (consoleLike.error === wrappedError) consoleLike.error = nativeError;
  };
}

function parseRecord(text: string | null): Record<string, unknown> | null {
  if (!text) return null;
  try {
    const value: unknown = JSON.parse(text);
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown> : null;
  } catch { return null; }
}

const finite = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

/** The window.crashReport buffer can hold every declared key at its maximum length,
 * even if JSON escaping expands each character to six bytes. */
const CRASH_CONTEXT_BUFFER_BYTES = Object.entries(CRASH_CONTEXT_KEYS)
  .reduce((total, [key, length]) => total + key.length + 6 * length + 6, 2);

interface ErrorEntry { key: string; instanceId: string; seq: number; at: number }

export function startPageCrashJournal(ports: PageCrashJournalPorts): PageCrashJournal {
  const { storage } = ports;
  const instanceId = String(ports.newInstanceId()).slice(0, ID_LENGTH);
  const startedAt = ports.now();
  const keyOf = (id: string, field: string): string => `${PREFIX}${id}.${field}`;
  const readItem = (key: string): string | null => {
    try { return storage.getItem(key); } catch { return null; }
  };
  /** Every stored error, oldest first: the order in which a refused write drops them. */
  const errors: ErrorEntry[] = [];
  const valueBytes = new Map<string, number>();
  let usedBytes = 0;
  let nextSeq = 0;
  let lastError: { entry: ErrorEntry; record: CrashErrorBreadcrumb } | null = null;
  let lastContext = '';
  let lastEnvironment = '';
  let disposed = false;
  let inFlight: Promise<void> | null = null;
  let rerun = false;
  let latestSend: CrashReportSender | null = null;

  const instanceKeys = (): Map<string, string[]> => {
    const byInstance = new Map<string, string[]>();
    const keys: string[] = [];
    try {
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index);
        if (key && key.startsWith(PREFIX) && key !== ALIVE_KEY) keys.push(key);
      }
    } catch { return byInstance; }
    for (const key of keys) {
      const rest = key.slice(PREFIX.length);
      const dot = rest.indexOf('.');
      if (dot <= 0) continue;
      const id = rest.slice(0, dot);
      const list = byInstance.get(id) ?? [];
      list.push(key);
      byInstance.set(id, list);
    }
    return byInstance;
  };

  const dropError = (entry: ErrorEntry): void => {
    const index = errors.indexOf(entry);
    if (index >= 0) errors.splice(index, 1);
    try { storage.removeItem(entry.key); } catch { /* storage unavailable */ }
    if (lastError?.entry === entry) lastError = null;
    if (entry.instanceId === instanceId) {
      usedBytes -= valueBytes.get(entry.key) ?? 0;
      valueBytes.delete(entry.key);
    }
    const droppedKey = keyOf(entry.instanceId, 'dropped');
    const dropped = finite(Number(readItem(droppedKey)));
    try { storage.setItem(droppedKey, String(dropped + 1)); } catch { /* counted on the next drop */ }
  };

  /** setItem; a refused write (quota) drops the oldest error and retries. */
  const write = (key: string, value: string): boolean => {
    for (;;) {
      try {
        storage.setItem(key, value);
        return true;
      } catch {
        const oldest = errors.find(entry => entry.key !== key);
        if (!oldest) return false;
        dropError(oldest);
      }
    }
  };
  const removeItem = (key: string): void => {
    try { storage.removeItem(key); } catch { /* storage unavailable */ }
  };

  // Reports across tabs (page-crash-origin.ts); the session journal notes the browser
  // session of this instance's origin record so this tab's next page can coordinate with it.
  let browserSession: string | null = null;
  try { browserSession = ports.browserSession?.() ?? null; } catch { browserSession = null; }
  const origin: PageCrashOrigin | null = ports.originStorage && ports.locks && browserSession
    ? startPageCrashOrigin({
      storage: ports.originStorage, locks: ports.locks, visibility: ports.visibility ?? null,
      instanceId, startedAt, session: browserSession,
      notePublished: (session) => {
        if (session !== null) return write(keyOf(instanceId, 'origin'), session);
        removeItem(keyOf(instanceId, 'origin'));
        return true;
      },
    })
    : null;
  const isOriginEvidence = (field: string): field is OriginEvidenceField =>
    (ORIGIN_EVIDENCE_FIELDS as readonly string[]).includes(field);

  const writeOwn = (field: string, value: unknown): boolean => {
    const key = keyOf(instanceId, field);
    const text = JSON.stringify(value);
    const bytes = utf8Length(text);
    const previous = valueBytes.get(key) ?? 0;
    // One instance never keeps more than one deliverable report.
    for (let oldest = errors.find(entry => entry.instanceId === instanceId && entry.key !== key);
      oldest && usedBytes - previous + bytes > MAX_REPORT_BYTES;
      oldest = errors.find(entry => entry.instanceId === instanceId && entry.key !== key)) {
      dropError(oldest);
    }
    if (!write(key, text)) return false;
    usedBytes += bytes - previous;
    valueBytes.set(key, bytes);
    if (isOriginEvidence(field)) origin?.noteEvidence(field, text);
    return true;
  };

  const crashValues = new Map<string, string>();
  let crashContextReady = false;
  const setCrashValue = (key: string, value: unknown): void => {
    const length = CRASH_CONTEXT_KEYS[key];
    if (length === undefined || value === undefined || value === null) return;
    const text = String(value).slice(0, length);
    if (crashValues.get(key) === text) return;
    crashValues.set(key, text);
    if (!crashContextReady) return;
    try { ports.crashReport?.set(key, text); } catch { /* the buffer refused this value */ }
  };

  const readAlive = (): { id: string; startedAt: number } | null => {
    const marker = parseRecord(readItem(ALIVE_KEY));
    const id = marker?.id;
    return typeof id === 'string' && ID_PATTERN.test(id) && id.length <= ID_LENGTH
      ? { id, startedAt: finite(marker?.startedAt) } : null;
  };
  const writeAlive = (): void => { write(ALIVE_KEY, JSON.stringify({ id: instanceId, startedAt })); };

  const dropInstance = (id: string): void => {
    for (const key of instanceKeys().get(id) ?? []) {
      try { storage.removeItem(key); } catch { /* storage unavailable */ }
    }
    for (const entry of errors.filter(item => item.instanceId === id)) errors.splice(errors.indexOf(entry), 1);
  };

  const noteEnvironment = (): void => {
    let environment: CrashEnvironment;
    try { environment = ports.environment(); } catch { return; }
    const text = JSON.stringify(environment);
    if (text === lastEnvironment) return;
    if (writeOwn('env', environment)) lastEnvironment = text;
  };

  const noteContext = (): void => {
    if (disposed) return;
    let context: CrashContext;
    try { context = ports.context(); } catch { return; }
    const text = `${context.view}\u0000${context.conversationId}\u0000${context.streamActive}`;
    if (text === lastContext) return;
    if (!writeOwn('context', { at: ports.now(), ...context })) return;
    lastContext = text;
    setCrashValue('view', context.view);
    setCrashValue('conversationId', context.conversationId);
    setCrashValue('streamActive', context.streamActive ? '1' : '0');
    noteEnvironment();
  };

  // Classify the previous instance of this tab before this instance claims the marker.
  const previous = readAlive();
  const existing = instanceKeys();
  const navigationType = String(ports.navigationType() ?? '');
  // A window opened with an opener starts with a copy of the opener's storage:
  // that marker belongs to a live page in another tab, not to a crash here.
  const cloned = navigationType === 'navigate' && ports.hasOpener();
  if (previous && previous.id !== instanceId && !cloned) {
    const classification: CrashClassification = ports.wasDiscarded() ? 'tab_discarded'
      : navigationType === 'back_forward' ? 'session_restored' : 'renderer_crash_suspected';
    write(keyOf(previous.id, 'verdict'), JSON.stringify({
      classification, startedAt: previous.startedAt, detectedAt: startedAt, navigationType,
    }));
    existing.set(previous.id, [...(existing.get(previous.id) ?? []), keyOf(previous.id, 'verdict')]);
  }
  // Instances without a verdict exited cleanly; their breadcrumbs are garbage.
  for (const [id, keys] of existing) {
    if (keys.some(key => key === keyOf(id, 'verdict'))) {
      for (const key of keys) {
        const seq = /\.e\.(\d+)$/.exec(key);
        if (!seq) continue;
        const record = parseRecord(readItem(key));
        errors.push({ key, instanceId: id, seq: Number(seq[1]), at: finite(record?.lastAt ?? record?.at) });
      }
      continue;
    }
    for (const key of keys) {
      try { storage.removeItem(key); } catch { /* storage unavailable */ }
    }
  }
  errors.sort((left, right) => left.at - right.at || left.seq - right.seq);
  writeAlive();
  noteContext();

  try {
    const initialized = ports.crashReport?.initialize(CRASH_CONTEXT_BUFFER_BYTES);
    if (initialized) {
      void initialized.then(() => {
        crashContextReady = true;
        for (const [key, value] of crashValues) {
          try { ports.crashReport?.set(key, value); } catch { /* the buffer refused this value */ }
        }
      }, () => undefined);
    }
  } catch { /* window.crashReport is unavailable in this document state */ }
  setCrashValue('pageInstanceId', instanceId);

  // A clean exit (navigation, reload, close, bfcache entry) clears the marker and
  // notes the exit, which a next page that already read the marker (the previous
  // document unloaded in another process after it started) finds before reporting;
  // a bfcache restore resumes the same instance.
  const onPageHide = (): void => {
    origin?.noteUnloaded(true);
    write(keyOf(instanceId, 'exit'), '1');
    if (readAlive()?.id !== instanceId) return;
    removeItem(ALIVE_KEY);
  };
  const onPageShow = (event: Event): void => {
    if (!(event as PageTransitionEvent).persisted) return;
    removeItem(keyOf(instanceId, 'exit'));
    writeAlive();
    origin?.noteUnloaded(false);
  };
  // A previous document still unloading in another process may remove the
  // marker after this one wrote it; the active document hears that and re-claims it.
  const onStorage = (event: Event): void => {
    const change = event as StorageEvent;
    if (change.key === ALIVE_KEY && change.newValue === null && change.storageArea === storage) writeAlive();
  };
  ports.lifecycle.addEventListener('pagehide', onPageHide);
  ports.lifecycle.addEventListener('pageshow', onPageShow);
  ports.lifecycle.addEventListener('storage', onStorage);

  const recordError = (kind: CrashErrorKind, message: unknown,
    details: { source?: unknown; stack?: unknown } = {}): void => {
    if (disposed) return;
    try {
      const now = ports.now();
      const record: CrashErrorBreadcrumb = {
        at: now, kind, message: String(message ?? '').slice(0, ERROR_FIELDS.message.maxLength),
      };
      if (details.source) record.source = String(details.source).slice(0, ERROR_FIELDS.source.maxLength);
      if (details.stack) record.stack = String(details.stack).slice(0, ERROR_FIELDS.stack.maxLength);
      const previousError = lastError;
      if (previousError && previousError.record.kind === record.kind
          && previousError.record.message === record.message
          && previousError.record.source === record.source && previousError.record.stack === record.stack) {
        const folded = { ...previousError.record, lastAt: now, count: (previousError.record.count ?? 1) + 1 };
        if (writeOwn(`e.${previousError.entry.seq}`, folded)) {
          previousError.record = folded;
          previousError.entry.at = now;
        }
        return;
      }
      const seq = nextSeq++;
      const entry: ErrorEntry = { key: keyOf(instanceId, `e.${seq}`), instanceId, seq, at: now };
      if (!writeOwn(`e.${seq}`, record)) return;
      errors.push(entry);
      lastError = { entry, record };
      noteContext();
    } catch { /* Crash breadcrumbs never become another application failure. */ }
  };

  const recordFreeze = (evidence: Record<string, unknown>): void => {
    if (disposed) return;
    try {
      if (!writeOwn('freeze', { at: ports.now(), evidence })) return;
      setCrashValue('freezeMs', evidence.durationMs);
      setCrashValue('usedJSHeapSize', evidence.usedJSHeapSize);
      setCrashValue('jsHeapSizeLimit', evidence.jsHeapSizeLimit);
      setCrashValue('domElements', evidence.domElements);
      noteContext();
      noteEnvironment();
    } catch { /* Crash breadcrumbs never become another application failure. */ }
  };

  const assembleReport = (id: string, read: (field: OriginEvidenceField) => Record<string, unknown> | null,
    fields: Pick<ClientCrashReport, 'classification' | 'startedAt' | 'detectedAt' | 'navigationType'
      | 'errors' | 'droppedErrors'>): ClientCrashReport => {
    const context = read('context') as ClientCrashReport['context'];
    const freeze = read('freeze') as ClientCrashReport['freeze'];
    const report: ClientCrashReport = {
      schema: 'tofu.client-crash/v1',
      pageInstanceId: id,
      classification: fields.classification,
      startedAt: fields.startedAt,
      lastSeenAt: 0,
      detectedAt: fields.detectedAt,
      navigationType: fields.navigationType,
      environment: read('env') as CrashEnvironment | null,
      context,
      freeze,
      errors: fields.errors,
      droppedErrors: fields.droppedErrors,
    };
    report.lastSeenAt = Math.max(report.startedAt, finite(context?.at), finite(freeze?.at),
      ...report.errors.map(record => finite(record.lastAt ?? record.at)));
    return report;
  };

  const buildReport = (id: string, verdict: Record<string, unknown>): ClientCrashReport => {
    const read = (field: string) => parseRecord(readItem(keyOf(id, field)));
    const stored = errors.filter(entry => entry.instanceId === id)
      .sort((left, right) => left.seq - right.seq)
      .map(entry => read(`e.${entry.seq}`) as CrashErrorBreadcrumb | null)
      .filter((record): record is CrashErrorBreadcrumb => record !== null);
    const report = assembleReport(id, read, {
      classification: verdict.classification as CrashClassification,
      startedAt: finite(verdict.startedAt),
      detectedAt: finite(verdict.detectedAt),
      navigationType: String(verdict.navigationType ?? ''),
      errors: stored,
      droppedErrors: finite(Number(readItem(keyOf(id, 'dropped')))),
    });
    // The report must fit the intake bound; the oldest errors give way first.
    while (report.errors.length && utf8Length(JSON.stringify(report)) > MAX_REPORT_BYTES) {
      report.errors.shift();
      report.droppedErrors++;
    }
    return report;
  };

  /** Another tab's instance carries the origin copy of its evidence; its errors stay in its tab. */
  const buildOriginReport = (id: string, record: OriginRecord): ClientCrashReport => assembleReport(id,
    field => parseRecord(origin?.evidence(id, field) ?? null), {
      // A page restored from history may be the first of a browser session restored
      // after an abrupt end, whose pages ended without pagehide: their fate is unknown.
      classification: navigationType === 'back_forward' ? 'session_restored' : 'renderer_crash_suspected',
      startedAt: record.startedAt,
      detectedAt: ports.now(),
      navigationType,
      errors: [],
      droppedErrors: 0,
    });

  /** This tab's previous instances, oldest detection first. */
  const sendSessionInstances = async (send: CrashReportSender): Promise<void> => {
    const pending: Array<{ id: string; verdict: Record<string, unknown> }> = [];
    for (const id of instanceKeys().keys()) {
      if (id === instanceId) continue;
      const verdict = parseRecord(readItem(keyOf(id, 'verdict')));
      if (verdict) pending.push({ id, verdict });
    }
    pending.sort((left, right) => finite(left.verdict.detectedAt) - finite(right.verdict.detectedAt));
    for (const { id, verdict } of pending) {
      const decision = readItem(keyOf(id, 'exit')) !== null ? 'drop'
        : origin ? await origin.judgeSessionInstance(id, readItem(keyOf(id, 'origin'))) : 'report';
      if (decision === 'keep') continue;
      if (decision === 'report') {
        await send(buildReport(id, verdict));
        // The origin record goes before the session copy: if this page dies in between,
        // its next page finds a published note without a record and drops the instance.
        origin?.forget(id);
      }
      dropInstance(id);
    }
  };

  /** Instances of other tabs of this owner and browser session that ended uncleanly while visible. */
  const sendOriginRecords = async (send: CrashReportSender): Promise<void> => {
    if (!origin) return;
    const own = new Set(instanceKeys().keys());
    for (const id of origin.others()) {
      if (own.has(id)) continue;
      const record = await origin.claimRecord(id);
      if (!record) continue;
      await send(buildOriginReport(id, record));
      origin.forget(id);
    }
  };

  const sendPending = (send: CrashReportSender): Promise<void> => {
    latestSend = send;
    if (inFlight) {
      rerun = true;
      return inFlight;
    }
    inFlight = (async () => {
      do {
        rerun = false;
        await sendSessionInstances(latestSend ?? send);
        await sendOriginRecords(latestSend ?? send);
      } while (rerun);
    })().catch(() => undefined).finally(() => { inFlight = null; });
    return inFlight;
  };

  const noteOwner = (owner: number | null): Promise<void> => {
    if (!origin || disposed) return Promise.resolve();
    origin.noteOwner(owner);
    return origin.ready;
  };

  return Object.freeze({
    instanceId,
    recordError,
    recordFreeze,
    noteContext,
    noteOwner,
    sendPending,
    dispose() {
      if (disposed) return;
      disposed = true;
      origin?.dispose();
      ports.lifecycle.removeEventListener('pagehide', onPageHide);
      ports.lifecycle.removeEventListener('pageshow', onPageShow);
      ports.lifecycle.removeEventListener('storage', onStorage);
    },
  });
}
