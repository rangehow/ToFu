// @vitest-environment jsdom
/** Studio clicks on a deliverable open the deliverable itself through the shared opener
 * (lead-deliverables/open.ts), never the lead's classic conversation; a plain report still opens its
 * author's brief. Real WorldLive, endpoint facade and transport; only fetch and the painted scene simulated. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mountWorld } from './app';
import type { OpenTarget } from './scene';
import type { LeadDeliverable } from '../lead-deliverables/model';
import { LEAD, PROJECT, memberRunning, teamPage } from './testing/recorded';

const scene: { open: ((target: OpenTarget) => void) | null } = { open: null };
vi.mock('./office', () => ({
  createOffice: (ports: { open(target: OpenTarget): void }) => {
    scene.open = ports.open;
    return {
      element: document.createElement('div'), update: () => {}, walk: () => {}, select: () => {}, restoreFocus: () => {},
      resize: () => {}, setInsets: () => {}, destroy: () => {}, documentCounts: () => ({ reports: 0, deliverables: 0 }),
    };
  },
}));

const answer = (data: unknown, status = 200) => new Response(JSON.stringify(status === 200 ? { meta: {}, data } : {}),
  { status, headers: { 'content-type': 'application/json' } });
const frames: FrameRequestCallback[] = [];
let reportKind: 'delivery' | 'progress' = 'delivery';

beforeEach(() => {
  frames.length = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => {});
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    const path = new URL(url, 'http://tofu.invalid').pathname;
    if (path === '/api/v4/folders') {
      return Promise.resolve(answer({ items: [{ id: 'f', name: 'tofu', projectPath: PROJECT, projectPaths: [PROJECT] }] }));
    }
    if (path === '/api/v4/project/coordination') return Promise.resolve(answer({ availability: 'available', conversationId: LEAD }));
    if (path === '/api/v4/project/coordination/conversations') return Promise.resolve(answer(teamPage(memberRunning)));
    if (path === '/api/v4/project/coordination/reports') {
      return Promise.resolve(answer({ projectPath: PROJECT, conversationId: LEAD, bindingRevision: 1, unread: true, latest: {
        turnId: 'report-turn', ordinal: 41, title: 'Login fixed', kind: reportKind, createdAt: 1_000,
        content: 'Login fixed\n\nMerged to main with tests.' } }));
    }
    return Promise.resolve(answer(null, 404));
  }));
});
afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); scene.open = null; });

async function settle() {
  for (let round = 0; round < 20; round += 1) {
    await new Promise(resolve => setTimeout(resolve, 0));
    for (const paint of frames.splice(0)) paint(0);
  }
}

function mount() {
  const root = document.createElement('div');
  document.body.append(root);
  const opened: LeadDeliverable[] = [];
  const chats: unknown[] = [], docks: string[] = [];
  const page = mountWorld(root, {
    openChat: (...args) => { chats.push(args); }, openQuestion() {}, answerPorts: () => undefined,
    dock: mode => { docks.push(mode); }, classic() {}, push: null,
  }, { deliverables: () => ({ open: item => { opened.push(item); return true; }, openSource: () => true }) });
  return { page, opened, chats, docks };
}

it('a delivery on the desk opens the delivery report itself, not the lead conversation', async () => {
  reportKind = 'delivery';
  const { page, opened, chats, docks } = mount();
  await settle();
  const desk = page.office()!.documents.find(document => document.kind === 'deliverable');
  expect(desk).toBeTruthy();
  scene.open!({ kind: 'document', id: desk!.id });
  expect(opened).toEqual([{
    id: 'report:report-turn', kind: 'report', title: 'Login fixed', projectPath: PROJECT, createdAt: 1_000,
    source: { conversationId: LEAD, turnId: 'report-turn', ordinal: 41 }, summary: 'Merged to main with tests.',
  }]);
  expect(chats).toEqual([]);
  expect(docks).not.toContain('brief');
  await settle();
  // Read once: the document leaves the desk.
  expect(page.office()!.documents.some(document => document.id === desk!.id)).toBe(false);
  page.dispose();
});

it('a progress report still opens its author\'s brief', async () => {
  reportKind = 'progress';
  const { page, opened, docks } = mount();
  await settle();
  const desk = page.office()!.documents.find(document => document.kind === 'report');
  scene.open!({ kind: 'document', id: desk!.id });
  expect(opened).toEqual([]);
  expect(docks).toContain('brief');
  page.dispose();
});
