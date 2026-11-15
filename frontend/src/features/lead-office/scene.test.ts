// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createLeadOfficeScene, type LeadOfficeSceneText } from './scene';
import { FLOOR_HEIGHT, FLOOR_SPOTS, LEAD_SPOTS, OFFICE_HEIGHT, TOWER_WIDTH } from './layout';
import type { DeskState, LeadState, OfficeDesk, OfficeProject, OfficeView } from './projection';

const text: LeadOfficeSceneText = {
  lead: lead => `lead ${lead.state}`, leadName: 'Lead', leadState: lead => lead.state, leadDetails: () => [],
  desk: desk => `desk ${desk.title}`, deskState: desk => desk.state, deskDetail: desk => desk.state,
  project: project => `project ${project.name}`, projectFacts: () => [], open: 'open',
};
const desk = (id: string, state: DeskState): OfficeDesk => ({ id, conversationId: `worker-${id}`, title: id, state, model: 'general' });
const project = (name: string, coordinatorId: string, extra: Partial<OfficeProject> = {}): OfficeProject => ({ path: `/work/${name}`, name, color: '',
  coordinatorId, workers: 1, questions: 0, overdue: 0, report: '', ...extra });
const lead = (state: LeadState, report = '') => ({ conversationId: 'lead', state, report, board: null });
function view(desks: OfficeDesk[], extra: Partial<OfficeView> = {}): OfficeView {
  return { altitude: 'floor', lead: lead('idle'), desks, projects: [],
    current: '', coverage: 'complete', projectsCoverage: 'complete', ...extra };
}
function scene() {
  const activate = vi.fn();
  const office = createLeadOfficeScene({ wake: vi.fn(), activate, text, fontFamily: 'sans-serif' });
  const run = (limit = 4000) => {
    for (let step = 0; step < limit && office.scene.nextWake(0, false) === 0; step++) office.scene.step(1000 / 60, { axis: { x: 0, y: 0 } });
  };
  return { office, activate, run, show: (next: OfficeView) => office.update(next) };
}
const BEAT = 125;

describe('lead office scene', () => {
  it('stacks the lead office above one floor per teammate, each one accessible target', () => {
    const { office, show } = scene();
    show(view([desk('a', 'working'), desk('b', 'review')]));
    expect(office.scene.width).toBe(TOWER_WIDTH);
    expect(office.scene.height).toBe(OFFICE_HEIGHT + 2 * FLOOR_HEIGHT);
    const targets = office.scene.targets();
    expect(targets.map(target => [target.id, target.label, target.data])).toEqual([
      ['lead', 'lead idle', { office: 'lead', state: 'idle', conversation: 'lead' }],
      ['desk:a', 'desk a', { office: 'desk', state: 'working', conversation: 'worker-a' }],
      ['desk:b', 'desk b', { office: 'desk', state: 'review', conversation: 'worker-b' }],
    ]);
    expect(targets[2]).toMatchObject({ x: 0, y: OFFICE_HEIGHT + FLOOR_HEIGHT, width: TOWER_WIDTH, height: FLOOR_HEIGHT });
    expect(office.scene.hit({ x: 30, y: 30 })).toBe('lead');
    expect(office.scene.hit({ x: 30, y: OFFICE_HEIGHT + 10 })).toBe('desk:a');
    expect(office.scene.hit({ x: 30, y: office.scene.height + 1 })).toBe('');
    // Targets are prepared per view: frames reuse the same array, a new view replaces it.
    expect(office.scene.targets()).toBe(targets);
    show(view([desk('a', 'working')]));
    expect(office.scene.targets()).not.toBe(targets);
  });

  it('marks the room of the open conversation as the current page', () => {
    const { office, show } = scene();
    show(view([desk('a', 'working'), desk('b', 'review')], { current: 'worker-b' }));
    expect(office.scene.targets().map(target => target.current)).toEqual([false, false, true]);
    show(view([desk('a', 'working')], { current: 'lead' }));
    expect(office.scene.targets().map(target => target.current)).toEqual([true, false]);
  });

  it('opens a room\'s conversation, and a project floor through its project', () => {
    const { office, show, activate } = scene();
    show(view([desk('a', 'queued')], { altitude: 'headquarters', projects: [project('atlas', 'coordinator-atlas'), project('loose', '')] }));
    expect(office.scene.targets().map(target => target.id)).toEqual(['lead', 'project:/work/atlas', 'project:/work/loose', 'desk:a']);
    office.scene.activate('lead', 'pointer');
    office.scene.activate('desk:a', 'keyboard');
    office.scene.activate('project:/work/atlas', 'pointer');
    // A project without a coordinator has nothing to open yet.
    office.scene.activate('project:/work/loose', 'pointer');
    expect(activate.mock.calls).toEqual([
      [{ kind: 'conversation', conversationId: 'lead' }],
      [{ kind: 'conversation', conversationId: 'worker-a' }],
      [{ kind: 'project', path: '/work/atlas', conversationId: 'coordinator-atlas' }],
    ]);
  });

  it('maps every desk and lead state to exactly one pose, screen and mark', () => {
    const { office, show } = scene();
    show(view([desk('w', 'working'), desk('q', 'queued'), desk('b', 'blocked'), desk('r', 'review'), desk('f', 'failed')]));
    const rest = (key: string) => office.person(key)!.rest;
    expect(rest('desk:w')).toEqual({ kind: 'type', x: FLOOR_SPOTS.chair, seated: true, facing: 1, bubble: '', screen: 'code' });
    expect(rest('desk:q')).toEqual({ kind: 'wait', x: FLOOR_SPOTS.door, seated: false, facing: -1, bubble: 'wait', screen: 'off' });
    expect(rest('desk:b')).toEqual({ kind: 'think', x: FLOOR_SPOTS.chair, seated: true, facing: 1, bubble: 'think', screen: 'paused' });
    expect(rest('desk:r')).toEqual({ kind: 'present', x: FLOOR_SPOTS.beside, seated: false, facing: 0, bubble: '', screen: 'done' });
    expect(rest('desk:f')).toEqual({ kind: 'slump', x: FLOOR_SPOTS.chair, seated: true, facing: 1, bubble: 'alert', screen: 'error' });
    const kinds = ['desk:w', 'desk:q', 'desk:b', 'desk:r', 'desk:f'].map(key => rest(key).kind);
    expect(new Set(kinds).size).toBe(kinds.length);
    expect(rest('lead')).toMatchObject({ kind: 'ponder', x: LEAD_SPOTS.stand, seated: false });
    show(view([], { lead: lead('replying') }));
    expect(rest('lead')).toMatchObject({ kind: 'type', x: LEAD_SPOTS.chair, seated: true, bubble: 'typing', screen: 'code' });
    show(view([], { lead: lead('asking') }));
    expect(rest('lead')).toMatchObject({ kind: 'ask', x: LEAD_SPOTS.front, seated: false, facing: 0, bubble: 'ask' });
    // A floor whose desk left the office takes its person with it.
    expect(office.person('desk:w')).toBeNull();
  });

  it('settles first sight: Workers already at work are seated with no replay', () => {
    const { office, show } = scene();
    show(view([], { coverage: 'loading' }));
    show(view([desk('a', 'working'), desk('b', 'review')]));
    expect(office.person('desk:a')!.beats).toEqual([]);
    expect(office.person('desk:b')!.beats).toEqual([]);
    expect(office.scene.nextWake(0, false)).toBeGreaterThan(0);
  });

  it('shows a delegation as a brief down the shaft and the new Worker walking out of the door with it', () => {
    const { office, show, run } = scene();
    show(view([desk('a', 'working')]));
    show(view([desk('a', 'working'), desk('b', 'working')]));
    const arriving = office.person('desk:b')!;
    expect(arriving.beats).toEqual(['hidden', 'door', 'walk', 'sit']);
    expect(arriving.now).toMatchObject({ carrying: true });
    expect(office.scene.nextWake(0, false)).toBe(0);
    office.scene.step(1000 / 60, { axis: { x: 0, y: 0 } });
    expect(office.person('desk:b')!.now.hidden).toBe(true);
    let opened = 0;
    for (let step = 0; step < 4000 && office.scene.nextWake(0, false) === 0; step++) {
      office.scene.step(1000 / 60, { axis: { x: 0, y: 0 } });
      opened = Math.max(opened, office.person('desk:b')!.now.door);
    }
    expect(opened).toBe(1);
    expect(office.person('desk:b')!.now).toMatchObject({ kind: 'type', x: FLOOR_SPOTS.chair, seat: 1, hidden: false, door: 0, carrying: false });
    // The established Worker never replayed anything.
    expect(office.person('desk:a')!.beats).toEqual([]);
  });

  it('shows a finished result: the Worker stands, cheers, and presents it beside the desk', () => {
    const { office, show, run } = scene();
    show(view([desk('a', 'working')]));
    show(view([desk('a', 'review')]));
    expect(office.person('desk:a')!.beats).toEqual(['stand', 'cheer', 'walk']);
    run();
    expect(office.person('desk:a')!.now).toMatchObject({ kind: 'present', x: FLOOR_SPOTS.beside, seat: 0 });
    // A failure is not celebrated.
    show(view([desk('a', 'review'), desk('b', 'working')]));
    run();
    show(view([desk('a', 'review'), desk('b', 'failed')]));
    expect(office.person('desk:b')!.beats).toEqual([]);
    expect(office.person('desk:b')!.now.kind).toBe('slump');
  });

  it('lands a new report on the outbox tray and walks the lead between its places', () => {
    const { office, show, run } = scene();
    show(view([]));
    show(view([], { lead: lead('replying', 'Parser shipped') }));
    expect(office.delivering()).toBe(true);
    expect(office.person('lead')!.beats).toEqual(['walk', 'sit']);
    run();
    expect(office.delivering()).toBe(false);
    expect(office.person('lead')!.now).toMatchObject({ kind: 'type', seat: 1 });
    // The same unread report never replays.
    show(view([], { lead: lead('asking', 'Parser shipped') }));
    expect(office.delivering()).toBe(false);
  });

  it('wakes on the next beat only while a pose loops, and otherwise on the next minute', () => {
    const { office, show } = scene();
    show(view([desk('a', 'review')]));
    expect(office.scene.nextWake(0, false)).toBeGreaterThan(BEAT);
    show(view([desk('a', 'review'), desk('b', 'working')]));
    const typing = scene();
    typing.show(view([desk('b', 'working')]));
    expect(typing.office.scene.nextWake(10, false)).toBe(BEAT - 10);
  });

  it('settles every move at once without motion and wakes only for the sky', () => {
    const { office, show } = scene();
    show(view([desk('a', 'working')]));
    show(view([desk('a', 'working'), desk('b', 'working')], { lead: lead('replying', 'Report') }));
    expect(office.scene.nextWake(0, false)).toBe(0);
    office.scene.setStill!(true);
    expect(office.person('desk:b')!.beats).toEqual([]);
    expect(office.person('desk:b')!.now).toMatchObject({ kind: 'type', hidden: false });
    expect(office.delivering()).toBe(false);
    expect(office.scene.nextWake(0, true)).toBeGreaterThan(BEAT);
    expect(office.scene.nextWake(0, true)).toBeLessThanOrEqual(60_000);
    // While still, changes settle directly.
    show(view([desk('a', 'review'), desk('b', 'working')]));
    expect(office.person('desk:a')!.beats).toEqual([]);
    office.scene.setStill!(false);
    expect(office.scene.nextWake(0, false)).toBeGreaterThan(0);
  });

  it('ignores updates after disposal', () => {
    const { office, show } = scene();
    show(view([desk('a', 'queued')]));
    office.dispose();
    show(view([desk('a', 'queued'), desk('b', 'working')]));
    expect(office.scene.targets()).toEqual([]);
    expect(office.person('desk:b')).toBeNull();
  });
});
