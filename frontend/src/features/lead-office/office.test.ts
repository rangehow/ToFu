// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ProjectDispatchHistoryRow, ProjectDispatchPage } from '../../api/project-dispatch.generated';
import { createLeadOffice, type LeadOfficeContext, type LeadOfficePorts } from './office';
import { FLOOR_HEIGHT, OFFICE_HEIGHT, TOWER_WIDTH } from './layout';
import type { StageTarget } from '../office-game/engine/stage';

type FakeStage = {
  options: { gestures?: string };
  camera: { worldWidth: number; worldHeight: number; viewWidth: number; viewHeight: number; fit?: string;
    insets: { top: number; right: number; bottom: number; left: number }; zoom: number; x: number; y: number };
  scene?: { width: number; height: number; targets(): StageTarget[]; activate(id: string, source: 'pointer' | 'keyboard'): void };
  setScene: ReturnType<typeof vi.fn>; wake: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn>;
};
const { stages } = vi.hoisted(() => ({ stages: [] as FakeStage[] }));
// Canvas drawing is out of reach of jsdom; the stage contract is what the office relies on.
vi.mock('../office-game/engine/stage', () => ({
  mountStage: vi.fn((host: HTMLElement, options: FakeStage['options']) => {
    host.replaceChildren(document.createElement('canvas'));
    const stage: FakeStage = {
      options, camera: { worldWidth: 960, worldHeight: 720, viewWidth: 264, viewHeight: 198, insets: { top: 0, right: 0, bottom: 0, left: 0 }, zoom: 1, x: 480, y: 360 },
      setScene: vi.fn(scene => { stage.scene = scene; stage.camera.worldWidth = scene.width; stage.camera.worldHeight = scene.height; }),
      wake: vi.fn(), dispose: vi.fn(() => host.replaceChildren()),
    };
    stages.push(stage);
    return stage;
  }),
}));

const LEAD = 'coordinator-lead';
const floor: LeadOfficeContext = { altitude: 'floor', path: '/repo', leadId: LEAD, board: null, report: undefined };
const headquarters: LeadOfficeContext = { altitude: 'headquarters', path: '/home/router', leadId: 'coordinator-main', board: null, report: undefined };

function row(id: string, status: string): ProjectDispatchHistoryRow {
  return {
    assignmentId: id, grantRevision: 1, revoked: false, canonicalDelivery: false, review: null,
    request: { projectPath: '/repo', coordinatorId: LEAD, recipientId: `worker-${id}`, brief: `Task ${id}`,
      profile: { mode: 'project', tools: [], readPaths: [], writePaths: [] } },
    execution: { turnId: 't', attemptId: 'a', taskId: 'k', status, resultRevision: 1, result: {} },
  };
}
const page = (...rows: ProjectDispatchHistoryRow[]): ProjectDispatchPage => ({ assignments: rows, olderCursor: null, newerCursor: null });
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

function harness() {
  const reads: Array<{ path: string; signal: AbortSignal; resolve(page: ProjectDispatchPage): void; reject(error: unknown): void }> = [];
  const pushes: Array<() => void> = [];
  const unsubscribe = vi.fn();
  let running: string[] = [], current = '';
  const ports: LeadOfficePorts = {
    assignments: vi.fn((path, signal) => new Promise<ProjectDispatchPage>((resolve, reject) => { reads.push({ path, signal, resolve, reject }); })),
    projects: vi.fn(async (cursor?: string) => cursor ? { projects: [{ projectPath: '/b', coordinatorConversationId: 'coordinator-b', availability: 'available' as const, activeWorkers: 1 }], nextCursor: null }
      : { projects: [{ projectPath: '/a', coordinatorConversationId: 'coordinator-a', availability: 'available' as const, activeWorkers: 2, openQuestions: 1 }], nextCursor: 'page-2' }),
    folders: () => [],
    live: () => null,
    runningWorkers: vi.fn(() => running.map(conversationId => ({ conversationId, title: `Chat ${conversationId}` }))),
    title: () => '',
    leadActivity: vi.fn(() => ({ streaming: false, awaitingHuman: false })),
    currentConversationId: () => current,
    subscribe: vi.fn(refresh => { pushes.push(refresh); return unsubscribe; }),
    navigate: vi.fn(), openQuestion: vi.fn(), openProject: vi.fn(),
  };
  const office = createLeadOffice(ports);
  document.body.replaceChildren(office.root);
  const stage = () => stages.at(-1)!;
  return { office, ports, reads, unsubscribe, push: () => pushes.at(-1)?.(), stage,
    setRunning(next: string[]) { running = next; }, setCurrent(next: string) { current = next; },
    desks: () => stage().scene!.targets().filter(target => target.data?.office === 'desk'),
    stageHost: () => office.root.querySelector<HTMLElement>('.lead-office-stage')! };
}

afterEach(() => { stages.length = 0; document.body.replaceChildren(); });

describe('lead office', () => {
  it('shows a lead\'s tower on a tap-only stage sized to it and reads the assignment page once', async () => {
    const h = harness();
    expect(h.office.root.hidden).toBe(true);
    h.office.show(floor);
    expect(h.office.root.hidden).toBe(false);
    expect(h.office.leadId()).toBe(LEAD);
    expect(stages).toHaveLength(1);
    expect(h.stage().options).toMatchObject({ gestures: 'tap' });
    expect(h.stage().camera.fit).toBe('contain');
    expect(h.stageHost().style.aspectRatio).toBe(`${TOWER_WIDTH} / ${OFFICE_HEIGHT}`);
    expect(h.office.root.getAttribute('aria-busy')).toBe('true');
    expect(h.reads.map(read => read.path)).toEqual(['/repo']);
    expect(h.ports.subscribe).toHaveBeenCalledTimes(1);
    h.reads[0].resolve(page(row('a', 'running'), row('b', 'completed')));
    await settle();
    expect(h.office.root.getAttribute('aria-busy')).toBe('false');
    expect(h.desks().map(target => target.data!.state)).toEqual(['review', 'working']);
    // The tower grew by two floors: its host and camera follow.
    const height = OFFICE_HEIGHT + 2 * FLOOR_HEIGHT;
    expect(h.stageHost().style.aspectRatio).toBe(`${TOWER_WIDTH} / ${height}`);
    expect(h.stage().camera.worldHeight).toBe(height);
    // The same lead with fresh board facts does not read again.
    h.office.show({ ...floor, board: { revision: 2, updatedAt: 1, summary: '', tasks: [{ title: 'x', status: 'in_progress' }] } });
    expect(h.reads).toHaveLength(1);
  });

  it('frames the open conversation\'s room from every list render, without reading', async () => {
    const h = harness();
    h.office.show(floor);
    h.reads[0].resolve(page(row('a', 'running')));
    await settle();
    expect(h.stage().scene!.targets().map(target => target.current)).toEqual([false, false]);
    h.setCurrent('worker-a');
    h.office.refresh();
    expect(h.stage().scene!.targets().map(target => target.current)).toEqual([false, true]);
    expect(h.reads).toHaveLength(1);
    h.stage().scene!.activate('desk:a', 'pointer');
    expect(h.ports.navigate).toHaveBeenCalledWith('worker-a');
    h.stage().scene!.activate('lead', 'keyboard');
    expect(h.ports.navigate).toHaveBeenLastCalledWith(LEAD);
  });

  it('coalesces project pushes during a read into one more read', async () => {
    const h = harness();
    h.office.show(floor);
    h.push(); h.push();
    expect(h.reads).toHaveLength(1);
    h.reads[0].resolve(page());
    await settle();
    expect(h.reads).toHaveLength(2);
    h.reads[1].resolve(page());
    await settle();
    expect(h.reads).toHaveLength(2);
    h.push();
    expect(h.reads).toHaveLength(3);
  });

  it('re-reads when a live run state of this lead moves, never on an unchanged render', async () => {
    const h = harness();
    h.office.show(floor);
    h.reads[0].resolve(page(row('a', 'running')));
    await settle();
    h.office.refresh(); h.office.refresh();
    expect(h.reads).toHaveLength(1);
    expect(h.ports.leadActivity).toHaveBeenCalledWith(LEAD);
    h.setRunning(['worker-new']);
    h.office.refresh();
    expect(h.reads).toHaveLength(2);
    // The running chat is a working floor before any assignment names it.
    expect(h.desks().map(target => target.data!.conversation)).toEqual(['worker-a', 'worker-new']);
  });

  it('fences reads of a lead that is no longer shown and releases everything on hide', async () => {
    const h = harness();
    h.office.show(floor);
    h.office.show({ ...floor, path: '/other', leadId: 'coordinator-other' });
    expect(h.reads[0].signal.aborted).toBe(true);
    expect(h.reads[1].path).toBe('/other');
    // A replaced lead's tower is rebuilt, never carried over.
    expect(stages[0].dispose).toHaveBeenCalledTimes(1);
    h.reads[0].resolve(page(row('a', 'running')));
    await settle();
    expect(h.desks()).toEqual([]);
    h.office.hide();
    expect(h.reads[1].signal.aborted).toBe(true);
    expect(h.unsubscribe).toHaveBeenCalledTimes(2);
    expect(h.office.root.hidden).toBe(true);
    expect(h.office.leadId()).toBe('');
    expect(stages.at(-1)!.dispose).toHaveBeenCalled();
    expect(h.stageHost().style.aspectRatio).toBe('');
  });

  it('pages every project at the headquarters and opens a project floor through its project', async () => {
    const h = harness();
    h.office.show(headquarters);
    await vi.waitFor(() => expect(h.ports.projects).toHaveBeenCalledTimes(2));
    expect(vi.mocked(h.ports.projects).mock.calls.map(call => call[0])).toEqual([undefined, 'page-2']);
    h.reads[0].resolve(page());
    await settle();
    expect(h.office.root.dataset.altitude).toBe('headquarters');
    const rooms = h.stage().scene!.targets().filter(target => target.data?.office === 'project');
    expect(rooms.map(target => [target.data!.project, target.data!.conversation])).toEqual([['/a', 'coordinator-a'], ['/b', 'coordinator-b']]);
    h.stage().scene!.activate(rooms[1].id, 'pointer');
    expect(h.ports.openProject).toHaveBeenCalledWith('/b', 'coordinator-b');
  });

  it('offers a retry after a failed first read and keeps a confirmed office through a failed refresh', async () => {
    const h = harness();
    h.office.show(floor);
    h.reads[0].reject(new Error('offline'));
    await settle();
    const status = h.office.root.querySelector<HTMLElement>('.lead-office-status')!;
    expect(status.hidden).toBe(false);
    status.querySelector('button')!.click();
    expect(h.reads).toHaveLength(2);
    h.reads[1].resolve(page(row('a', 'running')));
    await settle();
    expect(status.hidden).toBe(true);
    h.push();
    h.reads[2].reject(new Error('offline'));
    await settle();
    expect(status.hidden).toBe(true);
    expect(h.desks()).toHaveLength(1);
  });
});
