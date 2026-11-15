// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CONVERSATION_CATALOG_REFRESH_DELAY_MS } from '../../conversation/application/conversation-catalog-revision-gate';
import stream from '../../../../tests/fixtures/office_facts/stream_v1.json';
import type { RouterOfficeFacts } from '../../api/router.generated';
import { createFactStore, type FactFrame } from './facts';
import { createWorldLive, type PushChannel, type PushFrame, type PushPort } from './live';
import { LEAD, PROJECT, REPORTER, memberRunning, reporterBusy, reporterIdle, teamPage } from './testing/recorded';

const reads: string[] = [];
let teamValue = teamPage(memberRunning, reporterBusy);
/** The office facts snapshot; by default an older server without GET /api/v4/router/office. */
const notFound = async (): Promise<RouterOfficeFacts> => { throw Object.assign(new Error('not found'), { status: 404 }); };
let officeSnapshot: () => Promise<RouterOfficeFacts> = notFound;
let officeReads = 0;
let hidden = false;
Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });

vi.mock('../../api/transport', () => ({ apiTransport: { request: vi.fn(), ApiError: Error } }));
vi.mock('../../api/endpoint-registry/ports', () => ({ createEndpointPorts: () => ({}) }));
vi.mock('../../api/endpoint-registry/folders', () => ({
  createFoldersEndpoints: () => ({ list: async () => [{ id: 'f', name: 'tofu', projectPath: PROJECT, projectPaths: [PROJECT] }] }),
}));
vi.mock('../../api/endpoint-registry/project', () => ({
  createProjectEndpoints: () => ({
    globalRouter: async () => ({ availability: 'unbound' }),
    routerWatchdog: async () => ({ availability: 'unbound' }),
    coordination: async () => ({ availability: 'available', conversationId: LEAD }),
    workerConversations: async () => { reads.push('team'); return teamValue; },
    coordinationAssignments: async (_path: string, _command?: string, options?: { assignmentId?: string }) => {
      reads.push(options?.assignmentId ? `row:${options.assignmentId}` : 'assignments'); return { assignments: [] };
    },
    routerOffice: () => { officeReads += 1; return officeSnapshot(); },
    coordinatorProgress: async () => { reads.push('progress'); return null; },
    coordinatorReports: async () => { reads.push('reports'); return null; },
  }),
}));
vi.mock('../../api/conversation-sync.generated', () => ({
  conversationSyncApi: { snapshot: async () => { reads.push('questions'); return { turns: [] }; } },
}));

function pushPort() {
  const handlers = new Map<PushChannel, (frame: PushFrame) => void>();
  const port: PushPort = {
    subscribe(channel, handler) { handlers.set(channel, handler); return () => handlers.delete(channel); },
    connected: () => true,
    onReconnect: () => () => {},
  };
  return { port, emit: (channel: PushChannel, frame: PushFrame) => handlers.get(channel)?.(frame) };
}

async function started() {
  const push = pushPort();
  const live = createWorldLive(() => {}, push.port);
  await live.start();
  await vi.advanceTimersByTimeAsync(0);
  reads.length = 0;
  return { live, push };
}

describe('live office reads follow push hints, never a timer', () => {
  beforeEach(() => {
    vi.useFakeTimers(); reads.length = 0; hidden = false; teamValue = teamPage(memberRunning, reporterBusy);
    officeSnapshot = notFound; officeReads = 0;
  });
  afterEach(() => { vi.useRealTimers(); });

  it('reads each source once at start and nothing more while no hint arrives', async () => {
    const live = createWorldLive(() => {}, pushPort().port);
    await live.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(reads.sort()).toEqual(['assignments', 'progress', 'questions', 'reports', 'team']);
    live.stop();
  });

  it("coalesces a member's hints into one team read after the catalog delay", async () => {
    const { live, push } = await started();
    for (let index = 0; index < 20; index += 1) push.emit('notify', { type: 'conv_changed', convId: REPORTER });
    await vi.advanceTimersByTimeAsync(CONVERSATION_CATALOG_REFRESH_DELAY_MS - 1);
    expect(reads).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(reads).toEqual(['team']);
    live.stop();
  });

  it("re-reads the lead's team, board and report on the lead's hint, and one source on a project hint", async () => {
    const { live, push } = await started();
    push.emit('notify', { type: 'conv_changed', convId: LEAD });
    await vi.advanceTimersByTimeAsync(CONVERSATION_CATALOG_REFRESH_DELAY_MS);
    expect(reads.sort()).toEqual(['progress', 'questions', 'reports', 'team']);
    reads.length = 0;
    push.emit('project', { kind: 'coordinator_report', projectKey: `${PROJECT}/` });
    await vi.advanceTimersByTimeAsync(0);
    expect(reads).toEqual(['reports']);
    push.emit('project', { kind: 'integration', projectKey: PROJECT });
    push.emit('notify', { type: 'conv_changed', convId: 'conversation-of-nobody' });
    await vi.advanceTimersByTimeAsync(1000);
    expect(reads).toEqual(['reports']);
    live.stop();
  });

  it('reads the assignments only when a Worker attempt starts or ends', async () => {
    const { live, push } = await started();
    push.emit('notify', { type: 'conv_changed', convId: REPORTER });
    await vi.advanceTimersByTimeAsync(CONVERSATION_CATALOG_REFRESH_DELAY_MS);
    expect(reads).toEqual(['team']);
    teamValue = teamPage(memberRunning, reporterIdle);
    push.emit('notify', { type: 'conv_changed', convId: REPORTER });
    await vi.advanceTimersByTimeAsync(CONVERSATION_CATALOG_REFRESH_DELAY_MS);
    expect(reads).toEqual(['team', 'team', 'assignments']);
    live.stop();
  });

  it('with office facts, a plain hint reads nothing and a finished Worker reads only its assignment row', async () => {
    officeSnapshot = async () => stream.snapshot as unknown as RouterOfficeFacts;
    const { live, push } = await started();
    expect(live.state.facts?.coverage.busy).toBe('complete');
    push.emit('notify', { type: 'conv_changed', convId: REPORTER });
    await vi.advanceTimersByTimeAsync(CONVERSATION_CATALOG_REFRESH_DELAY_MS);
    expect(reads).toEqual([]);
    const [busyCleared, resultSet] = stream.frames as unknown as FactFrame[];
    expect([busyCleared.fact, busyCleared.state, resultSet.source, resultSet.state]).toEqual(['busy', 'cleared', 'assignment_result', 'set']);
    push.emit('office', busyCleared as unknown as PushFrame);
    push.emit('office', resultSet as unknown as PushFrame);
    await vi.advanceTimersByTimeAsync(0);
    // One read in flight plus one re-read: the first may predate the result.
    expect(reads).toEqual([`row:${resultSet.assignmentId}`, `row:${resultSet.assignmentId}`]);
    expect(officeReads).toBe(1);
    live.stop();
  });

  it('re-reads the snapshot once after a lost frame and replays what it changed', async () => {
    officeSnapshot = async () => stream.snapshot as unknown as RouterOfficeFacts;
    const { live, push } = await started();
    const frames = stream.frames as unknown as FactFrame[];
    const later = createFactStore();
    later.reset(stream.snapshot as unknown as RouterOfficeFacts);
    for (const frame of frames.slice(0, 3)) later.apply(frame);
    officeSnapshot = async () => ({ ...(stream.snapshot as unknown as RouterOfficeFacts), seq: frames[2].seq,
      facts: [...later.view()!.facts.values()] } as RouterOfficeFacts);
    push.emit('office', frames[2] as unknown as PushFrame);
    await vi.advanceTimersByTimeAsync(0);
    expect(officeReads).toBe(2);
    expect(reads).toContain(`row:${frames[1].assignmentId}`);
    expect(live.state.facts?.facts.has(frames[1].key)).toBe(true);
    live.stop();
  });

  it('reads nothing while the tab is hidden and catches up once when it becomes visible', async () => {
    const { live, push } = await started();
    hidden = true;
    push.emit('notify', { type: 'conv_changed', convId: REPORTER });
    push.emit('project', { kind: 'coordinator_progress', projectKey: PROJECT });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toEqual([]);
    hidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(reads.sort()).toEqual(['assignments', 'progress', 'questions', 'reports', 'team']);
    live.stop();
  });
});
