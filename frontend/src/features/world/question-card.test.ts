// @vitest-environment jsdom
/** The studio's question flow (owner complaint 2026-11-16): clicking a lead with the '?' marker opens a compact
 * question card before any progress report; the owner answers in the card through the shared answer control
 * and the production choice-interaction adapter (here over fixture Turns), which binds the answer to that exact
 * question; the card then reads "answered", the marker and the header's status clear; several questions page one
 * by one; each waiting lead shows its own; "answer in conversation" leaves for the classic view at that question,
 * not the bottom. The rest of the brief follows the card, unfolded (owner request 2026-11-18). */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { t } from '../../i18n';
import { mountWorld, type WorldShell } from './app';
import type { OpenTarget } from './scene';
import { fixtureLive, type FixtureSnapshot } from './preview/fixture';
import type { QuestionTurn } from './attention';
import { LEAD, PROJECT, memberRunning, reportProgress, teamPage } from './testing/recorded';

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

const KNOWLEDGE = '/projects/knowledge';
const KNOWLEDGE_LEAD = 'coordinator-knowledge';
const NOW = 1794533400000;
const frames: FrameRequestCallback[] = [];
const paint = () => { for (const frame of frames.splice(0)) frame(0); };
const flush = async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); };

function ask(turnId: string, ordinal: number, round: Record<string, unknown>): QuestionTurn {
  return { turnId, ordinal, actor: 'assistant', createdAt: NOW - 60_000 * (10 - ordinal),
    projection: { toolRounds: [{ toolName: 'ask_human', status: 'awaiting_human', ...round }] } };
}
function snapshot(): FixtureSnapshot {
  return { capturedAt: NOW, teams: [
    { kind: 'project', key: PROJECT, name: 'tofu', color: '#58b7a8', leadId: LEAD, order: 0,
      team: teamPage(memberRunning), assignments: [], progress: null, reports: reportProgress,
      questionTurns: [
        ask('turn-queue', 1, { guidanceId: 'hg_queue', guidanceType: 'choice', guidanceQuestion: 'The 910C queue is full. Which compute?',
          guidanceOptions: [{ label: 'H800 queue' }, { label: 'Borrow quota' }] }),
        ask('turn-paper', 2, { guidanceId: 'hg_paper', guidanceType: 'free_text', guidanceBlocking: false, status: 'done',
          guidanceQuestion: 'Which venue should the paper target?', guidanceDefault: 'NeurIPS' }),
      ] },
    { kind: 'project', key: KNOWLEDGE, name: 'knowledge', color: '#c98a4b', leadId: KNOWLEDGE_LEAD, order: 1,
      team: { conversations: [], nextCursor: null, coverage: 'complete' }, assignments: [], progress: null, reports: null,
      questionTurns: [ask('turn-k', 3, { guidanceId: 'hg_knowledge', guidanceType: 'choice', guidanceBlocking: false, status: 'done',
        guidanceQuestion: 'Keep everything queued on h910c?', guidanceOptions: [{ label: 'Keep queued' }, { label: 'Move to H200' }],
        guidanceDefault: 'Keep queued' })] },
  ] };
}

let source: ReturnType<typeof fixtureLive>;
let shell: WorldShell & {
  openQuestion: ReturnType<typeof vi.fn>; openConversation: ReturnType<typeof vi.fn>; dock: ReturnType<typeof vi.fn>;
  confirmDefault: ReturnType<typeof vi.fn>;
};
let root: HTMLElement;
let world: ReturnType<typeof mountWorld>;

beforeEach(() => {
  frames.length = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => {});
  shell = { openConversation: vi.fn(), openQuestion: vi.fn(), dock: vi.fn(), classic: vi.fn(), push: null, downloadUrl: id => `/download/${id}`,
    answerPorts: (id, turnId) => source.answerPorts(id, turnId),
    confirmDefault: vi.fn((id: string, guidanceId: string) => source.confirmDefault(id, guidanceId)) } as typeof shell;
  root = document.createElement('div');
  document.body.append(root);
  world = mountWorld(root, shell, { live: onChange => (source = fixtureLive(snapshot(), onChange)), now: () => NOW });
  paint();
});
afterEach(() => { world.dispose(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

async function open(id: string) {
  openPerson({ kind: 'person', id });
  paint();
  await flush();
  return root.querySelector<HTMLElement>('.w-brief')!;
}
const card = () => root.querySelector<HTMLElement>('.w-question-card')!;

it('leads a waiting lead with a compact question card, then its progress unfolded', async () => {
  expect(world.office()!.people.get(LEAD)!.emphasis).toBe('decision');
  const brief = await open(LEAD);
  expect(brief.firstElementChild).toBe(card());
  expect(card().dataset.guidanceId).toBe('hg_queue');
  expect(card().querySelector('.rich-guidance-question')!.textContent).toContain('The 910C queue is full');
  // Choices as one-click options, an own-answer box, and the project and blocking state.
  expect([...card().querySelectorAll<HTMLInputElement>('.rich-choice-option input:not([data-rich-control="choice:own"])')]
    .map(input => input.value)).toEqual(['H800 queue', 'Borrow quota']);
  expect(card().querySelector('textarea.rich-choice-own-input')).not.toBeNull();
  expect(card().querySelector('.w-question-meta')!.textContent).toBe(`${t('world.brief.project', { name: 'tofu' })} · ${t('world.brief.blocking')}`);
  // The latest report follows the card in the progress timeline, nothing folded and no transcript.
  expect(brief.querySelector('details')).toBeNull();
  const progress = brief.querySelector<HTMLElement>('[data-section="progress"]')!;
  expect(progress.querySelector('.w-brief-head')!.textContent).toBe(t('world.brief.progress'));
  expect(progress.querySelector('.w-entry-title')!.textContent).toBe(reportProgress.latest!.title);
  expect(card().compareDocumentPosition(progress) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  // The header's status is the fact: an open owner question.
  const status = root.querySelector<HTMLElement>('.w-status')!;
  expect([status.dataset.tone, status.textContent]).toEqual(['decision', t('world.state.asking')]);
});

it('pages through several questions one by one and shows the default a non-blocking one applies', async () => {
  await open(LEAD);
  expect(card().querySelector('.w-question-count')!.textContent).toBe('1/2');
  card().querySelector<HTMLButtonElement>('[data-step="1"]')!.click();
  await flush();
  expect(card().querySelector('.w-question-count')!.textContent).toBe('2/2');
  expect(card().dataset.guidanceId).toBe('hg_paper');
  expect(card().querySelector('.rich-guidance-question')!.textContent).toContain('Which venue');
  expect(card().querySelector('.rich-guidance-default')!.textContent).toMatch(/: NeurIPS$/);
  expect(card().querySelector('.w-question-meta')!.textContent).toContain(t('world.brief.nonBlocking'));
  card().querySelector<HTMLButtonElement>('[data-step="1"]')!.click();
  expect(card().querySelector('.w-question-count')!.textContent).toBe('1/2');
});

it('answers in the card bound to that exact question; the card reads answered and the marker clears', async () => {
  await open(KNOWLEDGE_LEAD);
  expect(card().dataset.guidanceId).toBe('hg_knowledge');
  expect(world.office()!.people.get(KNOWLEDGE_LEAD)!.emphasis).toBe('decision');
  const choice = card().querySelector<HTMLInputElement>('.rich-choice-option input[value="Move to H200"]')!;
  choice.checked = true;
  choice.dispatchEvent(new Event('change', { bubbles: true }));
  await flush();
  // A non-blocking question's answer is the chat reply that quotes it, its id included.
  expect(source.replies).toEqual([{ conversationId: KNOWLEDGE_LEAD, guidanceId: 'hg_knowledge',
    quote: 'Keep everything queued on h910c?\n\n[question hg_knowledge]', responseText: 'Move to H200' }]);
  paint();
  expect(card().dataset.state).toBe('answered');
  expect(card().querySelector('.w-question-badge')!.textContent).toBe(t('world.brief.answered'));
  // The marker clears at once, and stays clear once the fixture's authority records the answer.
  expect(world.office()!.people.get(KNOWLEDGE_LEAD)!.emphasis).not.toBe('decision');
  paint();
  expect(root.querySelector<HTMLElement>('.w-status')!.dataset.tone).not.toBe('decision');
  await new Promise(resolve => setTimeout(resolve, 0));
  paint();
  expect(world.office()!.people.get(KNOWLEDGE_LEAD)!.emphasis).not.toBe('decision');
  expect(world.office()!.attention.decisions.map(item => item.source.guidanceId)).toEqual(['hg_queue', 'hg_paper']);
});

it("answers a blocking question with the existing answer_guidance command and leaves the lead's other question waiting", async () => {
  await open(LEAD);
  const choice = card().querySelector<HTMLInputElement>('.rich-choice-option input[value="H800 queue"]')!;
  choice.checked = true;
  choice.dispatchEvent(new Event('change', { bubbles: true }));
  await flush();
  paint();
  expect(card().dataset.state).toBe('answered');
  expect(card().querySelector('.rich-guidance-status')!.textContent).toContain('H800 queue');
  await new Promise(resolve => setTimeout(resolve, 0));
  paint();
  expect(world.office()!.attention.decisions.filter(item => item.source.conversationId === LEAD)
    .map(item => item.source.guidanceId)).toEqual(['hg_paper']);
  // Still one open question for this lead, so its marker stays.
  expect(world.office()!.people.get(LEAD)!.emphasis).toBe('decision');
});

it('shows each waiting lead its own question', async () => {
  await open(LEAD);
  expect(card().dataset.guidanceId).toBe('hg_queue');
  await open(KNOWLEDGE_LEAD);
  expect(card().dataset.guidanceId).toBe('hg_knowledge');
  expect(card().querySelector('.w-question-count')).toBeNull();
  expect(card().querySelector('.rich-guidance-question')!.textContent).toContain('Keep everything queued');
});

it('answers in the conversation at that question, not at the bottom', async () => {
  await open(LEAD);
  card().querySelector<HTMLButtonElement>('.w-question-actions button')!.click();
  expect(shell.dock).not.toHaveBeenCalledWith('chat');
  expect(shell.openQuestion).toHaveBeenCalledWith(LEAD, { turnId: 'turn-queue', ordinal: 1, guidanceId: 'hg_queue' });
  expect(shell.openConversation).not.toHaveBeenCalled();
});

it("confirms a non-blocking question's default in one click: the card settles, the marker clears, nothing is sent", async () => {
  await open(KNOWLEDGE_LEAD);
  expect(world.office()!.attention.decisions.map(item => item.source.guidanceId)).toContain('hg_knowledge');
  const confirm = card().querySelector<HTMLButtonElement>('.w-question-actions .w-question-confirm')!;
  expect(confirm.textContent).toBe(t('world.brief.confirmDefault'));
  confirm.click();
  await flush();
  paint();
  expect(shell.confirmDefault).toHaveBeenCalledWith(KNOWLEDGE_LEAD, 'hg_knowledge');
  expect(card().dataset.state).toBe('confirmed');
  expect(card().querySelector('.w-question-badge')!.textContent).toBe(t('world.brief.confirmed'));
  expect(card().querySelector('.w-question-confirm')).toBeNull();
  // No chat reply and no answer command: the lead is told nothing.
  expect(source.replies).toEqual([]);
  expect(world.office()!.people.get(KNOWLEDGE_LEAD)!.emphasis).not.toBe('decision');
  await new Promise(resolve => setTimeout(resolve, 0));
  paint();
  expect(world.office()!.people.get(KNOWLEDGE_LEAD)!.emphasis).not.toBe('decision');
  expect(world.office()!.attention.decisions.map(item => item.source.guidanceId)).toEqual(['hg_queue', 'hg_paper']);
});

it('offers no default confirmation on a blocking question, and keeps the card open when confirming fails', async () => {
  await open(LEAD);
  expect(card().dataset.guidanceId).toBe('hg_queue');
  expect(card().querySelector('.w-question-confirm')).toBeNull();
  shell.confirmDefault.mockImplementationOnce(() => Promise.reject(new Error('409')));
  card().querySelector<HTMLButtonElement>('[data-step="1"]')!.click();
  await flush();
  card().querySelector<HTMLButtonElement>('.w-question-confirm')!.click();
  await flush();
  paint();
  expect(card().dataset.guidanceId).toBe('hg_paper');
  expect(card().dataset.state).toBe('open');
  expect(card().querySelector('.w-question-error')!.textContent).toBe(t('world.brief.confirmFailed'));
  expect(world.office()!.people.get(LEAD)!.emphasis).toBe('decision');
});
