// @vitest-environment jsdom
/** The office brief shows progress and files, never the transcript (owner request 2026-11-18). A lead's brief
 * renders at once from the office's own reads (board, latest report) and fills its timeline and files from the
 * deliverables projection: reports and completed tasks by day, newest first, each attached file as a chip, every
 * file with a download link; a `lead_deliverable` hint re-reads it. A Worker's brief shows its assignment steps,
 * task, result gist and checks. "Full conversation" leaves for the classic view. Real fixture source and world;
 * only the painted scene is simulated. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { t } from '../../i18n';
import type { LeadDeliverablePage } from '../../api/project-coordination.generated';
import { mountWorld, type WorldShell } from './app';
import { projectJournal } from './brief';
import type { OpenTarget } from './scene';
import { fixtureLive, type FixtureSnapshot } from './preview/fixture';
import {
  LEAD, PROJECT, REPORTER, assignmentReported, memberRunning, progressOverdue, reportProgress, reporterBusy, teamPage,
} from './testing/recorded';

let openPerson: (target: OpenTarget) => void = () => {};
vi.mock('./office', () => ({
  createOffice: (ports: { open(target: OpenTarget): void }) => {
    openPerson = ports.open;
    return {
      element: document.createElement('div'), update: () => {}, walk: () => {}, select: () => {}, restoreFocus: () => {},
      resize: () => {}, setInsets: () => {}, destroy: () => {}, documentCounts: () => ({ reports: 0, deliverables: 0 }),
    };
  },
}));

const NOW = 1794533400000;
const DAY = 86_400_000;
const TASK = '1da69cfd3b19fb97fb82ed8c0b2f2f9a';
const page: LeadDeliverablePage = {
  conversationId: LEAD, nextCursor: '1794360000000:eda180a73ebdd542', otherCount: 0,
  tasks: [{ key: TASK, title: 'One Hope login', count: 1, latestAt: NOW - 2_000, translation: { title: '一次 Hope 登录' } }],
  items: [
    { id: 'report:r1', kind: 'report', reportKind: 'progress', title: 'Rust rewrite: Phase 0 started', projectPath: PROJECT,
      createdAt: NOW - 1_000, summary: 'All three **Rust** studies are done.\n\nPrototype numbers follow.',
      source: { conversationId: LEAD, turnId: 'r1', ordinal: 140 },
      files: [{ artifactId: 'a1', filename: 'RUST_REWRITE_EVIDENCE.md', kind: 'document', sizeBytes: 85631, path: 'docs/RUST_REWRITE_EVIDENCE.md' }] },
    { id: `completion:${TASK}`, kind: 'completion', title: 'Done, no tap needed', summary: 'Done, no tap needed', projectPath: PROJECT,
      createdAt: NOW - 2_000, source: null, tasks: [{ key: TASK, title: 'One Hope login' }] },
    { id: 'artifact:a2', kind: 'deck', title: 'review.pptx', projectPath: PROJECT, createdAt: NOW - DAY, source: null, artifactId: 'a2', format: 'pptx' },
    { id: 'report:r0', kind: 'report', reportKind: 'delivery', title: 'Resume v3 delivered', projectPath: PROJECT,
      createdAt: NOW - DAY - 1_000, summary: 'Four versions attached.', source: { conversationId: LEAD, turnId: 'r0' } },
  ],
};

function snapshot(): FixtureSnapshot {
  return { capturedAt: NOW, deliverables: { [LEAD]: page }, teams: [
    { kind: 'project', key: PROJECT, name: 'tofu', color: '#58b7a8', leadId: LEAD, order: 0,
      team: teamPage(memberRunning, reporterBusy), assignments: [assignmentReported], progress: progressOverdue,
      reports: reportProgress, questionTurns: [] },
  ] };
}

const frames: FrameRequestCallback[] = [];
const paint = () => { for (const frame of frames.splice(0)) frame(0); };
const flush = async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); paint(); };
let source: ReturnType<typeof fixtureLive>;
let shell: WorldShell & { openConversation: ReturnType<typeof vi.fn> };
let root: HTMLElement;
let world: ReturnType<typeof mountWorld>;
let reads: string[];

beforeEach(() => {
  frames.length = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => {});
  shell = { openConversation: vi.fn(), openQuestion: vi.fn(), dock: vi.fn(), classic: vi.fn(), push: null,
    answerPorts: () => undefined, downloadUrl: id => `/api/artifacts/${id}/download`, confirmDefault: async () => {} } as typeof shell;
  root = document.createElement('div');
  document.body.append(root);
  reads = [];
  world = mountWorld(root, shell, { now: () => NOW, live: onChange => {
    source = fixtureLive(snapshot(), onChange);
    const read = source.deliverables!.read;
    source.deliverables!.read = (id, path, cursor, signal) => { reads.push(`${id}@${path}`); return read(id, path, cursor, signal); };
    return source;
  } });
  paint();
});
afterEach(() => { world.dispose(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

const brief = () => root.querySelector<HTMLElement>('.w-brief')!;
const section = (name: string) => brief().querySelector<HTMLElement>(`[data-section="${name}"]`)!;

it('projects reports, completed tasks and files from the deliverables page, by day, newest first', () => {
  const journal = projectJournal({ status: 'ok', items: page.items, taskTitles: new Map([[TASK, '一次 Hope 登录']]),
    nextCursor: page.nextCursor, loadingMore: false });
  expect(journal.days.map(day => day.entries.map(entry => entry.item.id))).toEqual([[`report:r1`, `completion:${TASK}`], ['report:r0']]);
  const completion = journal.days[0].entries[1];
  // A completion reads as its board task (as the board displays it) with its final note.
  expect([completion.title, completion.summary]).toEqual(['一次 Hope 登录', 'Done, no tap needed']);
  expect(journal.files.map(file => [file.name, file.visual, file.item.artifactId])).toEqual([
    ['RUST_REWRITE_EVIDENCE.md', 'document', 'a1'], ['review.pptx', 'deck', 'a2']]);
  expect(journal.more).toBe(true);
});

it('renders a lead at once from the office reads, then fills its timeline and files; no transcript', async () => {
  openPerson({ kind: 'person', id: LEAD });
  paint();
  // First paint: the board and the latest report, the timeline still loading.
  expect(section('now').querySelector('.w-now-summary')!.textContent).toBe(progressOverdue.board!.summary);
  expect(section('progress').getAttribute('aria-busy')).toBe('true');
  expect(section('progress').querySelector('.w-entry-title')!.textContent).toBe(reportProgress.latest!.title);
  expect(reads).toEqual([`${LEAD}@${PROJECT}`]);
  await flush();
  const progress = section('progress');
  expect(progress.getAttribute('aria-busy')).toBe('false');
  expect([...progress.querySelectorAll('.w-day')].map(day => day.textContent)).toHaveLength(2);
  expect([...progress.querySelectorAll('.w-entry-title')].map(node => node.textContent))
    .toEqual(['Rust rewrite: Phase 0 started', '一次 Hope 登录', 'Resume v3 delivered']);
  expect(progress.querySelector('.w-entry-summary')!.textContent).toBe('All three Rust studies are done.');
  expect(progress.querySelector('.w-file-chip')!.getAttribute('aria-label')).toBe(t('world.brief.openFile', { name: 'RUST_REWRITE_EVIDENCE.md' }));
  expect(progress.querySelector('.w-more')).not.toBeNull();
  const files = [...section('files').querySelectorAll<HTMLElement>('.w-file')];
  expect(files.map(file => [file.querySelector('b')!.textContent, file.querySelector<HTMLAnchorElement>('.w-file-download')!.getAttribute('href')]))
    .toEqual([['RUST_REWRITE_EVIDENCE.md', '/api/artifacts/a1/download'], ['review.pptx', '/api/artifacts/a2/download']]);
  // The office never shows the transcript or a tool row.
  expect(root.querySelector('.w-tab, .chat-wrapper, [data-tool-name]')).toBeNull();
  expect(shell.dock).not.toHaveBeenCalledWith('chat');
});

it('re-reads the journal on its lead_deliverable hint only', async () => {
  openPerson({ kind: 'person', id: LEAD });
  await flush();
  expect(reads).toHaveLength(1);
  source.hintDeliverable('worker-elsewhere');
  source.hintDeliverable(LEAD);
  await flush();
  expect(reads).toHaveLength(2);
});

it("opens the person's full conversation in the classic view", async () => {
  openPerson({ kind: 'person', id: LEAD });
  await flush();
  root.querySelector<HTMLButtonElement>('.w-panel-full')!.click();
  expect(shell.openConversation).toHaveBeenCalledWith(LEAD, null);
  root.querySelector<HTMLButtonElement>('.w-classic-note button')!.click();
  expect(shell.openConversation).toHaveBeenCalledTimes(2);
});

it("shows a Worker's assignment steps, task, result gist and checks", async () => {
  openPerson({ kind: 'person', id: REPORTER });
  await flush();
  expect([...section('now').querySelectorAll<HTMLElement>('.w-step')].map(step => step.dataset.done))
    .toEqual(['true', 'true', 'true', 'true', 'true']);
  expect(section('now').querySelector('.w-now-facts code')!.textContent).toBe('47aa9b62c3');
  expect(section('task').textContent).toContain('OWNER AUTHORIZATION');
  expect(section('result').querySelector('.w-result')!.textContent).toContain('I deleted the 学城 document');
  expect(section('checks').querySelectorAll('.w-check[data-verdict="met"]')).toHaveLength(3);
  // Its journal is read like a lead's, and an empty one shows no timeline, only "no files yet".
  expect(reads).toContain(`${REPORTER}@${PROJECT}`);
  expect(section('progress')).toBeNull();
  expect(section('files').textContent).toContain(t('world.brief.noFiles'));
});
