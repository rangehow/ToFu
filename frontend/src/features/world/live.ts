/** Live, read-only sources for the Tofu office. Folders and coordination bindings name the teams (the Global
 * Router first, the Watchdog last); per team: its members (the first catalog page, where running work is),
 * assignments, board, latest owner report and the lead's open questions. Every read is a GET through the
 * shared transport behind a read-only guard, so a write cannot leave this world even by mistake. Each
 * (team, source) has at most one read in flight plus one pending re-read: hints coalesce, never queue.
 *
 * Push: the application's existing /api/push owner, reached through the injected `push` port; no second
 * socket. The project channel's `coordinator_report` / `coordinator_progress` hints re-read exactly that
 * team's report or board. Notify `conv_changed` hints (docs/contracts/EVENTS.md) coalesce per team like the
 * classic catalog's (conversation-catalog-revision-gate.ts): the first schedules one re-read after
 * CONVERSATION_CATALOG_REFRESH_DELAY_MS and later ones join it. A lead's hint re-reads its members, board
 * and report; a member's hint re-reads the members; a member whose attempt started or ended re-reads the
 * assignments; a lead going idle re-reads its open questions. A hidden tab reads nothing: hints only mark the
 * world missed and becoming visible re-reads it once; a push reconnect re-reads everything it may have missed.
 *
 * Office facts (facts.ts): when the server has GET /api/v4/router/office, its snapshot and ordered `office`
 * frames are the wake-up instead of hints. Each fact reads exactly what it names: a Worker's result or
 * accepted document reads that one assignment row, a lead's report its report state, a board its progress,
 * an owner question its lead's tail (for the text), a busy edge the row of that Worker's assignment, and a
 * busy person the team page has never listed the team page. While facts cover every hinted change
 * (factsReplaceHints), plain hints read nothing. A fresh snapshot after a lost frame or a server restart
 * replays what changed against the facts held before it. An older server answers 404 and the derived reads
 * above stay the source.
 * Entry point: createWorldLive. Dependencies: api transport, endpoint facades, sync client, facts store. */
import { apiTransport, type ApiTransport } from '../../api/transport';
import { createEndpointPorts } from '../../api/endpoint-registry/ports';
import { createProjectEndpoints } from '../../api/endpoint-registry/project';
import { createFoldersEndpoints } from '../../api/endpoint-registry/folders';
import { conversationSyncApi } from '../../api/conversation-sync.generated';
import type { ProjectDispatchHistoryRow } from '../../api/project-dispatch.generated';
import type {
  CoordinatorConversationPage, CoordinatorProgressState, CoordinatorReportState,
} from '../../api/project-coordination.generated';
import { CONVERSATION_DOM_WINDOW_BATCH_TURNS } from '../../conversation/ui/surface-windowing';
import { CONVERSATION_CATALOG_REFRESH_DELAY_MS } from '../../conversation/application/conversation-catalog-revision-gate';
import { openQuestions, type OpenQuestion } from './attention';
import { changedFacts, createFactStore, factsReplaceHints, type Fact, type FactFrame, type FactsView } from './facts';
import type { TeamKind } from './model';

const ROUTER_COLOR = '#d9b25f';
const WATCHDOG_COLOR = '#5b7fa6';
const TEAM_COLORS = ['#e8a33d', '#58b7a8', '#8f7ae6', '#e46f7a', '#6aa6e8', '#9bc455'];

export type SourceName = 'team' | 'assignments' | 'progress' | 'reports' | 'questions';
export interface SourceState<T> { value: T | null; status: 'idle' | 'loading' | 'ok' | 'error'; at: number; inFlight: boolean; again: boolean }
export interface LiveTeam {
  key: string; name: string; color: string; kind: TeamKind; leadId: string; order: number;
  team: SourceState<CoordinatorConversationPage>;
  assignments: SourceState<ReadonlyArray<ProjectDispatchHistoryRow>>;
  progress: SourceState<CoordinatorProgressState>;
  reports: SourceState<CoordinatorReportState>;
  questions: SourceState<ReadonlyArray<OpenQuestion>>;
}
export interface PushFrame { channel?: unknown; type?: unknown; convId?: unknown; kind?: unknown; projectKey?: unknown }
export type PushChannel = 'notify' | 'project' | 'office';
/** The application's push owner: channel subscriptions, connection state and reconnect signal. */
export interface PushPort {
  subscribe(channel: PushChannel, handler: (frame: PushFrame) => void): () => void;
  connected(): boolean;
  onReconnect(listener: () => void): () => void;
}
/** `loading`: the teams are still being discovered, or the first office facts read has not answered. */
export interface LiveState { teams: LiveTeam[]; started: boolean; error: string; facts: FactsView | null; loading: boolean }
export interface WorldLive { state: LiveState; start(): Promise<void>; refreshAll(): void; stop(): void }

interface FolderItem { id: string; name: string; color?: string; order?: number; projectPath?: string; projectPaths?: string[] }

/** The world never writes: any non-GET request is refused before it reaches the network. */
export function readOnlyTransport(transport: ApiTransport): ApiTransport {
  return Object.freeze({
    ...transport,
    request: ((path, options) => {
      const method = String(options?.method || 'GET').toUpperCase();
      if (method !== 'GET') return Promise.reject(new transport.ApiError('read-only world', { code: 'read_only' }));
      return transport.request(path, options);
    }) as ApiTransport['request'],
  });
}

const blank = <T,>(): SourceState<T> => ({ value: null, status: 'idle', at: 0, inFlight: false, again: false });
/** Project hints name the project by its normalized path (lib/conversations/project_identity.py). */
const trimPath = (path: string) => path.replace(/[\\/]+$/, '');
type Row = ProjectDispatchHistoryRow;
/** A page read can be older than a row read that landed while it was in flight: keep the newer result. */
function keepNewerRows(previous: ReadonlyArray<Row> | null, next: ReadonlyArray<Row>): ReadonlyArray<Row> {
  if (!previous) return next;
  const before = new Map(previous.map(row => [row.assignmentId, row]));
  return next.map(row => {
    const old = before.get(row.assignmentId);
    return old && (old.execution?.resultRevision ?? -1) > (row.execution?.resultRevision ?? -1) ? old : row;
  });
}
const SOURCES: Record<TeamKind, ReadonlyArray<SourceName>> = {
  router: ['team', 'progress', 'reports', 'questions', 'assignments'],
  project: ['team', 'progress', 'reports', 'questions', 'assignments'],
  watchdog: ['team', 'reports', 'questions'],
};

export function createWorldLive(onChange: () => void, push: PushPort | null): WorldLive {
  const transport = readOnlyTransport(apiTransport);
  const ports = createEndpointPorts(transport, () => document.documentElement.lang || 'zh');
  const project = createProjectEndpoints(ports);
  const folders = createFoldersEndpoints(ports);
  const lifetime = new AbortController();
  const state: LiveState = { teams: [], started: false, error: '', facts: null, loading: true };
  let discovering = true;
  let factsUnanswered = true;
  let missed = false;
  const store = createFactStore();
  let factsSupported = true;
  let factsInFlight = false;
  let factsAgain = false;
  let factsStale = false;
  const heldFrames: FactFrame[] = [];
  const rowReads = new Map<string, { again: boolean }>();
  const hinted = new Map<LiveTeam, { timer: number; lead: boolean }>();
  const releases: Array<() => void> = [];

  const readers: { [K in SourceName]: (team: LiveTeam, signal: AbortSignal) => Promise<NonNullable<LiveTeam[K]['value']>> } = {
    team: (team, signal) => project.workerConversations(team.key, undefined, signal),
    assignments: (team, signal) =>
      project.coordinationAssignments(team.key, undefined, undefined, signal).then(page => page.assignments),
    progress: (team, signal) => project.coordinatorProgress(team.key, signal),
    reports: (team, signal) => project.coordinatorReports(team.key, signal),
    questions: (team, signal) => conversationSyncApi.snapshot(team.leadId, {
      signal, query: { windowLimit: CONVERSATION_DOM_WINDOW_BATCH_TURNS },
    }).then(snapshot => openQuestions(team.leadId, snapshot.turns)),
  };

  function refresh(team: LiveTeam, name: SourceName): void {
    if (!SOURCES[team.kind].includes(name)) return;
    const slot = team[name] as SourceState<unknown>;
    if (slot.inFlight) { slot.again = true; return; }
    slot.inFlight = true;
    if (slot.status === 'idle') slot.status = 'loading';
    const previous = slot.value;
    onChange();
    void readers[name](team, lifetime.signal).then(value => {
      slot.value = name === 'assignments' ? keepNewerRows(previous as ReadonlyArray<Row> | null, value as ReadonlyArray<Row>) : value;
      slot.status = 'ok'; slot.at = Date.now();
      if (name === 'team') teamChanged(team, previous as CoordinatorConversationPage | null, value as CoordinatorConversationPage);
    }, () => {
      if (!lifetime.signal.aborted) slot.status = 'error';
    }).finally(() => {
      slot.inFlight = false;
      if (lifetime.signal.aborted) return;
      onChange();
      if (slot.again) { slot.again = false; refresh(team, name); }
    });
  }

  function teamChanged(team: LiveTeam, previous: CoordinatorConversationPage | null, next: CoordinatorConversationPage) {
    if (!previous) return;
    const before = new Map(previous.conversations.map(member => [member.conversationId, Boolean(member.busy)]));
    const after = new Map(next.conversations.map(member => [member.conversationId, Boolean(member.busy)]));
    const workerFlipped = [...after].some(([id, busy]) => id !== team.leadId && (before.get(id) ?? false) !== busy)
      || [...before].some(([id, busy]) => busy && !after.has(id));
    if (workerFlipped) refresh(team, 'assignments');
    if (before.get(team.leadId) && !after.get(team.leadId)) { refresh(team, 'questions'); refresh(team, 'reports'); }
  }

  function refreshTeam(team: LiveTeam) {
    for (const name of SOURCES[team.kind]) refresh(team, name);
  }

  function hint(team: LiveTeam, lead: boolean) {
    const pending = hinted.get(team);
    if (pending) { pending.lead ||= lead; return; }
    const entry = { lead, timer: window.setTimeout(() => {
      hinted.delete(team);
      refresh(team, 'team');
      // A lead asking the owner stays busy, so its own hint is the only cue for a new question.
      if (entry.lead) { refresh(team, 'progress'); refresh(team, 'reports'); refresh(team, 'questions'); }
    }, CONVERSATION_CATALOG_REFRESH_DELAY_MS) };
    hinted.set(team, entry);
  }

  function route(conversationId: string) {
    if (factsReplaceHints(store.view())) return;
    if (document.hidden) { missed = true; return; }
    const lead = state.teams.find(team => team.leadId === conversationId);
    if (lead) { hint(lead, true); return; }
    const home = state.teams.find(team => team.team.value?.conversations.some(member => member.conversationId === conversationId));
    if (home) hint(home, false);
  }

  function onVisibility() {
    if (document.hidden) return;
    if (factsStale) { factsStale = false; readFacts(); }
    if (!missed) return;
    missed = false;
    for (const team of state.teams) refreshTeam(team);
  }

  function readFacts() {
    if (factsInFlight) { factsAgain = true; return; }
    factsInFlight = true;
    void project.routerOffice(lifetime.signal).then(snapshot => {
      const before = store.view()?.facts ?? null;
      store.reset(snapshot);
      factsSupported = true;
      factsInFlight = false;
      if (before) {
        const changes = changedFacts(before, store.view()!.facts);
        if (document.hidden) missed ||= changes.length > 0;
        else for (const change of changes) factEffect(change.fact, change.state);
      }
      for (const frame of heldFrames.splice(0)) applyFrame(frame);
    }, (error: { status?: number }) => {
      factsInFlight = false;
      if (lifetime.signal.aborted) return;
      // An older server has no office facts: the derived reads stay the source.
      if (error?.status === 404) factsSupported = false;
      store.clear();
      heldFrames.length = 0;
    }).finally(() => {
      if (lifetime.signal.aborted) return;
      state.facts = store.view();
      factsUnanswered = false;
      state.loading = discovering;
      onChange();
      if (factsAgain) { factsAgain = false; readFacts(); }
    });
  }

  function applyFrame(frame: FactFrame) {
    if (factsInFlight) { heldFrames.push(frame); return; }
    const outcome = store.apply(frame);
    if (outcome.kind === 'resnapshot') {
      if (!factsSupported) return;
      if (document.hidden) factsStale = true; else readFacts();
      return;
    }
    if (outcome.kind !== 'applied') return;
    state.facts = store.view();
    if (document.hidden) missed = true; else factEffect(outcome.fact, outcome.state);
    onChange();
  }

  /** The team a fact belongs to: a Worker's lead, the lead itself, or the team page that lists it. */
  function teamFor(fact: Fact): LiveTeam | undefined {
    return (fact.coordinatorId ? state.teams.find(team => team.leadId === fact.coordinatorId) : undefined)
      || state.teams.find(team => team.leadId === fact.conversationId)
      || state.teams.find(team => team.team.value?.conversations.some(member => member.conversationId === fact.conversationId));
  }

  function factEffect(fact: Fact, change: 'set' | 'cleared') {
    const team = teamFor(fact);
    if (!team) return;
    const listed = fact.conversationId === team.leadId
      || Boolean(team.team.value?.conversations.some(member => member.conversationId === fact.conversationId));
    const knownRow = (id: string) => Boolean(team.assignments.value?.some(row => row.assignmentId === id));
    if (fact.fact === 'busy') {
      if (change === 'set' && !listed) refresh(team, 'team');
      if (fact.assignmentId && (change === 'cleared' || !knownRow(fact.assignmentId))) readRow(team, fact.assignmentId);
    } else if ((fact.fact === 'report' && fact.source === 'assignment_result') || fact.fact === 'document') {
      if (fact.assignmentId) readRow(team, fact.assignmentId);
    } else if (fact.fact === 'report' && fact.source === 'coordinator_report') refresh(team, 'reports');
    else if (fact.fact === 'decision') refresh(team, 'questions');
    else if (fact.fact === 'board') refresh(team, 'progress');
  }

  /** One assignment row (cheap), merged into the team's rows; the walk-in follows from the merged diff. */
  function readRow(team: LiveTeam, assignmentId: string) {
    const id = `${team.key}\u0000${assignmentId}`;
    const pending = rowReads.get(id);
    if (pending) { pending.again = true; return; }
    const entry = { again: false };
    rowReads.set(id, entry);
    void project.coordinationAssignments(team.key, undefined, { assignmentId }, lifetime.signal).then(page => {
      const rows = [...(team.assignments.value || [])];
      for (const row of page.assignments) {
        const index = rows.findIndex(each => each.assignmentId === row.assignmentId);
        if (index >= 0) rows[index] = row; else rows.unshift(row);
      }
      team.assignments.value = rows;
      team.assignments.at = Date.now();
    }, () => {}).finally(() => {
      rowReads.delete(id);
      if (lifetime.signal.aborted) return;
      onChange();
      if (entry.again) readRow(team, assignmentId);
    });
  }

  function addTeam(kind: TeamKind, key: string, name: string, color: string, leadId: string, order: number) {
    const team: LiveTeam = { key, name, color, kind, leadId, order,
      team: blank(), assignments: blank(), progress: blank(), reports: blank(), questions: blank() };
    state.teams.push(team);
    state.teams.sort((a, b) => a.order - b.order);
    refreshTeam(team);
  }

  async function start() {
    document.addEventListener('visibilitychange', onVisibility);
    if (push) {
      releases.push(push.subscribe('notify', frame => {
        if (frame?.type === 'conv_changed' && typeof frame.convId === 'string') route(frame.convId);
      }));
      releases.push(push.subscribe('project', frame => {
        const source: SourceName | null = frame?.kind === 'coordinator_report' ? 'reports'
          : frame?.kind === 'coordinator_progress' ? 'progress' : null;
        const team = source && typeof frame.projectKey === 'string'
          ? state.teams.find(each => trimPath(each.key) === trimPath(frame.projectKey as string)) : null;
        if (!team || !source) return;
        if (document.hidden) { missed = true; return; }
        refresh(team, source);
      }));
      releases.push(push.subscribe('office', frame => {
        if (frame?.type === 'office.fact') applyFrame(frame as unknown as FactFrame);
      }));
      releases.push(push.onReconnect(() => {
        // Frames may have been lost, and a restarted server may now have office facts.
        factsSupported = true;
        readFacts();
        for (const team of state.teams) refreshTeam(team);
      }));
    }
    readFacts();
    // The Global Router and the Watchdog take their desks (and start their reads) as soon as each answers,
    // without waiting for the folder list that names the project teams.
    const seen = new Set<string>();
    const [folderItems] = await Promise.all([
      folders.list().then(items => items as FolderItem[]).catch(() => [] as FolderItem[]),
      project.globalRouter(lifetime.signal).catch(() => null).then(router => {
        if (lifetime.signal.aborted || router?.availability !== 'available' || !router.conversationId || !router.projectPath) return;
        seen.add(router.projectPath);
        addTeam('router', router.projectPath, '', ROUTER_COLOR, router.conversationId, -1);
      }),
      project.routerWatchdog(lifetime.signal).catch(() => null).then(watchdog => {
        if (lifetime.signal.aborted || watchdog?.availability !== 'available' || !watchdog.conversationId || !watchdog.projectPath) return;
        seen.add(watchdog.projectPath);
        addTeam('watchdog', watchdog.projectPath, '', WATCHDOG_COLOR, watchdog.conversationId, Number.MAX_SAFE_INTEGER);
      }),
    ]);
    if (lifetime.signal.aborted) return;
    const candidates: Array<{ path: string; folder: FolderItem }> = [];
    const ordered = [...folderItems].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    for (const preferPrimary of [true, false]) for (const folder of ordered) {
      for (const path of folder.projectPaths || []) {
        if (seen.has(path) || (preferPrimary && folder.projectPath !== path)) continue;
        seen.add(path); candidates.push({ path, folder });
      }
    }
    state.started = true; onChange();
    await Promise.all(candidates.map(async ({ path, folder }, index) => {
      const binding = await project.coordination(path, lifetime.signal).catch(() => null);
      if (lifetime.signal.aborted || binding?.availability !== 'available' || !binding.conversationId) return;
      addTeam('project', path, folder.name, folder.color || TEAM_COLORS[index % TEAM_COLORS.length], binding.conversationId, index);
    }));
    if (!state.teams.length) state.error = 'no-teams';
    discovering = false;
    state.loading = factsUnanswered;
    onChange();
  }

  return {
    state, start,
    refreshAll: () => { for (const team of state.teams) refreshTeam(team); },
    stop: () => {
      lifetime.abort();
      for (const release of releases.splice(0)) release();
      for (const entry of hinted.values()) window.clearTimeout(entry.timer);
      hinted.clear();
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
