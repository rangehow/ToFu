// @vitest-environment jsdom
/** The office scene against the projection it is fed, through the pattern measured on production (2026-11-16):
 * Workers' attempts end and restart every few seconds. The floor plan must not change, nobody may pop in or out,
 * and a floor plan that does change (a new pod) blends instead of jumping. Canvas painting is not exercised here
 * (tests/frontend/test_world_office_motion_browser.py drives the real canvas). */
import { describe, expect, it } from 'vitest';
import { createCamera } from '../office-game/engine/camera';
import type { WorldArt } from './art';
import { projectOffice, type DeskDocument, type Office, type TeamInput } from './model';
import { createWorldScene, type WorldScene } from './scene';

const LABELS = { router: 'Router', watchdog: 'Watchdog', lead: (team: string) => `Lead of ${team}` };
const STEP = 1000 / 60;
const ART = { setTheme() {}, theme: () => 'light', atlas: () => null, sprite: () => null, sheet: () => null, dispose() {} } as unknown as WorldArt;
const TEXT = {
  ownerPlate: 'Your office', watchdogPlate: 'Watchdog', person: (person: { name: string }) => ({ label: person.name, state: '' }),
  document: () => 'Report', pile: (count: number) => `${count} reports`, board: () => 'Board', trophy: (title: string) => title,
  trophyDetail: '', trophyMore: () => '', routine: () => '',
};

function input(key: string, kind: TeamInput['kind'], lead: string, workers: string[], busy: string[]): TeamInput {
  return {
    key, kind, name: key.slice(1), color: '#58b7a8', leadId: lead,
    team: { conversations: workers.map(id => ({ conversationId: id, title: `[${id}] task`, turnCount: 3, coordinatorId: lead, folderId: null })),
      nextCursor: null, coverage: 'complete' } as TeamInput['team'],
    assignments: [], progress: null, reports: null, questions: null, failed: false, busy: new Set(busy),
  };
}
const workers = (count: number) => Array.from({ length: count }, (_, index) => `worker-${index + 1}`);
function officeWith(busy: number, extra: Partial<Office> = {}): Office {
  const staff = workers(20);
  const office = projectOffice(0, [
    input('/router', 'router', 'router', [], []),
    input('/tofu', 'project', 'lead-tofu', staff, staff.slice(0, busy)),
    input('/knowledge', 'project', 'lead-knowledge', ['k-1', 'k-2'], ['k-1', 'k-2']),
  ], LABELS);
  return { ...office, ...extra };
}
function mount(): WorldScene {
  const scene = createWorldScene({ open() {}, wake() {}, camera: createCamera(1280, 720), fontFamily: 'sans-serif', text: TEXT, art: ART });
  scene.setViewport(1920, 1000);
  return scene;
}
const rooms = (scene: WorldScene) => scene.layout()!.zones.map(zone => [zone.key, zone.x, zone.y, zone.w, zone.h, zone.pods.length]);
function run(scene: WorldScene, ms: number) { for (let elapsed = 0; elapsed < ms; elapsed += STEP) scene.scene.step(STEP, { axis: { x: 0, y: 0 } }); }
const actor = (scene: WorldScene, id: string) => scene.inspect().actors.find(each => each.id === id);

describe('office scene', () => {
  it('seats everyone present at first sight without walking anyone in', () => {
    const scene = mount();
    scene.update(officeWith(9), false);
    const people = scene.inspect().actors;
    expect(people.filter(each => each.id.startsWith('worker-'))).toHaveLength(9);
    expect(people.some(each => each.walking)).toBe(false);
  });

  it('keeps the floor plan while attempts end and restart, walking people out and back instead of popping them', () => {
    const scene = mount();
    scene.update(officeWith(9), false);
    const before = rooms(scene);
    scene.update(officeWith(3), false);
    expect(rooms(scene)).toEqual(before);
    expect(scene.inspect().tweening).toBe(false);
    const leaving = actor(scene, 'worker-7')!;
    expect([leaving.rest, leaving.walking]).toEqual(['gone', true]);
    run(scene, 600);
    const midway = actor(scene, 'worker-7')!.pose!;
    // Their attempt restarts before they reach the door: they turn round where they are.
    scene.update(officeWith(9), false);
    scene.scene.step(0, { axis: { x: 0, y: 0 } });
    const turned = actor(scene, 'worker-7')!.pose!;
    expect(Math.hypot(turned.x - midway.x, turned.y - midway.y)).toBeLessThan(0.01);
    run(scene, 20_000);
    expect(scene.inspect().actors.filter(each => each.id.startsWith('worker-') && each.at === 'seat')).toHaveLength(9);
    expect(rooms(scene)).toEqual(before);
    // Those whose work really ended leave the floor; their empty desks stay.
    scene.update(officeWith(2), false);
    run(scene, 30_000);
    expect(scene.inspect().actors.filter(each => each.id.startsWith('worker-'))).toHaveLength(2);
    expect(rooms(scene)).toEqual(before);
  });

  it('blends into a new floor plan when a team needs another pod, and walks the newcomers in', () => {
    const scene = mount();
    scene.update(officeWith(8), false);
    const before = scene.inspect().shown!.zones.find(zone => zone.key === '/knowledge')!;
    scene.update(officeWith(17), false);
    expect(scene.inspect().tweening).toBe(true);
    const after = scene.layout()!.zones.find(zone => zone.key === '/knowledge')!;
    expect([after.x, after.y]).not.toEqual([before.x, before.y]);
    let previous = before;
    for (let elapsed = 0; elapsed < 800; elapsed += STEP) {
      run(scene, STEP);
      const now = scene.inspect().shown!.zones.find(zone => zone.key === '/knowledge')!;
      expect(Math.abs(now.x - previous.x) + Math.abs(now.y - previous.y)).toBeLessThan(80);
      previous = now;
    }
    expect(scene.inspect().tweening).toBe(false);
    expect([previous.x, previous.y]).toEqual([after.x, after.y]);
    const newcomer = actor(scene, 'worker-17')!;
    expect(newcomer.rest).toBe('seat');
    expect(newcomer.walking || newcomer.at === 'seat').toBe(true);
  });

  it('fades the room of a team that appears later in once its space has opened, never at once', () => {
    const scene = mount();
    const withTeams = (keys: string[]) => {
      const office = officeWith(3);
      const extra = keys.map(key => input(key, 'project', `lead-${key.slice(1)}`, [`${key.slice(1)}-1`], [`${key.slice(1)}-1`]));
      const more = projectOffice(0, extra, LABELS);
      return { ...office, teams: [...office.teams, ...more.teams], people: new Map([...office.people, ...more.people]) };
    };
    scene.update(withTeams([]), false);
    run(scene, 2000);
    scene.update(withTeams(['/late']), false);
    // Until the blend starts the shown floor is the old one, without the new room; then it starts invisible.
    expect(scene.inspect().shown!.zones.some(zone => zone.key === '/late')).toBe(false);
    run(scene, STEP);
    let previous = scene.inspect().fading.zones['/late'];
    expect(previous).toBe(0);
    let elapsed = 0;
    while (scene.inspect().tweening) {
      run(scene, STEP);
      elapsed += STEP;
      const now = scene.inspect().fading.zones['/late'] ?? 1;
      expect(now).toBeGreaterThanOrEqual(previous);
      expect(now - previous).toBeLessThan(0.2);
      previous = now;
    }
    expect(elapsed).toBeGreaterThan(400);
    expect(scene.layout()!.zones.some(zone => zone.key === '/late')).toBe(true);
  });

  it('fades people in and out where they sit while the first reads settle, moving nobody', () => {
    const scene = mount();
    scene.update(officeWith(4), true);
    scene.update(officeWith(9), true);
    const arrived = actor(scene, 'worker-8')!;
    expect(arrived.walking).toBe(false);
    expect(arrived.alpha).toBeLessThan(1);
    scene.update(officeWith(2), true);
    expect(scene.inspect().actors.some(each => each.walking)).toBe(false);
  });

  it('keeps people waiting at the owner\'s door on their spots when reads list their questions in another order', () => {
    const scene = mount();
    const asking = (order: string[]) => {
      const office = officeWith(3);
      for (const team of office.teams) if (order.includes(team.lead.id)) team.lead.emphasis = 'decision';
      const decisions = order.map(id => ({ since: 5, source: { conversationId: id } }));
      return { ...office, attention: { ...office.attention, decisions } as unknown as Office['attention'] };
    };
    const line = () => Object.fromEntries(scene.inspect().actors.filter(each => each.id.startsWith('lead-'))
      .map(each => [each.id, [each.at, each.pose?.x, each.pose?.y]]));
    scene.update(asking(['lead-tofu', 'lead-knowledge']), false);
    run(scene, 30_000);
    const before = line();
    expect(before['lead-tofu'][0]).toBe('spot');
    expect(before['lead-knowledge'][0]).toBe('spot');
    scene.update(asking(['lead-knowledge', 'lead-tofu']), false);
    expect(scene.inspect().actors.some(each => each.walking)).toBe(false);
    expect(line()).toEqual(before);
  });

  it('lays a carried report on the owner\'s desk only when its author has walked it there', () => {
    const scene = mount();
    const document = {
      id: 'result:a:1', kind: 'report', author: 'worker-1', team: '/tofu', title: 'Done', at: 0,
      ref: { source: 'worker-result', conversationId: 'worker-1', assignmentId: 'a', resultRevision: 1, turnId: 't' },
    } as DeskDocument;
    scene.update(officeWith(3), false);
    run(scene, 1000);
    scene.update(officeWith(3, { documents: [document] }), false);
    let dropped = 0, done = 0;
    scene.walk({ person: 'worker-1', document, leaves: false }, () => { done += 1; }, () => { dropped += 1; });
    expect(scene.documentCounts().reports).toBe(0);
    expect(actor(scene, 'worker-1')!.errand).toBe('carry');
    run(scene, 30_000);
    expect([dropped, done]).toEqual([1, 1]);
    expect(scene.documentCounts().reports).toBe(1);
    expect(actor(scene, 'worker-1')!.at).toBe('seat');
  });
});
