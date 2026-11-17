// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { CoordinatorReportBlockViewModel, TextBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderCoordinatorReportBlock } from './coordinator-report-block';
import { renderTextBlock } from './text-blocks';
import { renderTurnHeader } from './turn-chrome';

/* A `coordinator_report.*` Turn is the Coordinator's own message to the owner.
 * The server stamps it with the project-brain initiator, but the reader must
 * see "Coordinator report · <kind>", never another persona, and never the
 * reply-answer toolbar ("Answer" heading). */

afterEach(() => document.body.replaceChildren());

const zh: Record<string, string> = {
  'conversation.coordinatorReport.title': '负责人汇报',
  'conversation.coordinatorReport.progress': '进展',
  'conversation.coordinatorReport.delivery': '交付',
  'conversation.coordinatorReport.decision': '决定',
  'conversation.coordinatorReport.blocked': '受阻',
};

const ports: ClassicConversationRendererPorts = {
  renderSafeMarkdownHtml: (text) => {
    const safe = document.createElement('p');
    safe.textContent = text;
    return safe.outerHTML;
  },
  // The runtime's brain label for the same Turn; a report must not use it.
  roleLabel: () => '项目大脑',
  localizedText: (key, fallback) => zh[key] ?? fallback,
};

const text = (overrides: Partial<TextBlockViewModel> = {}): TextBlockViewModel => ({
  kind: 'text', blockId: 'text:terminal', source: {}, identitySource: 'contract',
  markdown: '18:00 status\n\nRestart moves to 22:00', displayMarkdown: '18:00 status\n\nRestart moves to 22:00',
  displayMode: 'original', terminal: true, deliverable: true, resumable: false, ...overrides,
});

const reportTurn = (kind: string) => ({
  turnId: 'report-1', presentationId: 'report-1', actor: 'assistant', role: 'assistant', kind,
  status: 'completed', blocks: [], branches: [], actions: [],
  metadata: { origin: { blockId: 'origin', initiator: 'brain' } },
  source: { createdAt: Date.UTC(2026, 10, 11, 10, 1), projection: {} },
}) as unknown as ConversationTurnViewModel;

describe('coordinator report presentation', () => {
  it.each([
    ['coordinator_report.progress', '负责人汇报 · 进展'],
    ['coordinator_report.delivery', '负责人汇报 · 交付'],
    ['coordinator_report.decision', '负责人汇报 · 决定'],
    ['coordinator_report.blocked', '负责人汇报 · 受阻'],
  ])('labels a %s Turn as the Coordinator report, not the project brain', (kind, label) => {
    const node = document.createElement('div');
    renderTurnHeader(node, reportTurn(kind), ports);
    expect(node.querySelector('.message-role')?.textContent).toBe(label);
  });

  it('keeps the initiator label for other Turns', () => {
    const node = document.createElement('div');
    renderTurnHeader(node, reportTurn('ingested'), ports);
    expect(node.querySelector('.message-role')?.textContent).toBe('项目大脑');
  });

  it('never gives a report Turn the reply answer toolbar', () => {
    const node = document.createElement('section');
    document.body.append(node);
    renderTextBlock(node, text(), ports, reportTurn('coordinator_report.progress'));
    expect(node.querySelector('.conversation-answer-label')).toBeNull();
    expect(node.querySelector('.md-content')?.textContent).toContain('Restart moves to 22:00');
    renderTextBlock(node, text(), ports, { ...reportTurn('output'), kind: 'reply' } as ConversationTurnViewModel);
    expect(node.querySelector('.conversation-answer-label')).not.toBeNull();
  });

  /* Owner report 2026-11-12: open reports made a long Coordinator reply
   * longer still. A report reads as one row, like the reply's process folds. */
  it('renders a report as one folded row that the reader opens', () => {
    const node = document.createElement('section');
    document.body.append(node);
    const block: CoordinatorReportBlockViewModel = {
      kind: 'coordinator-report', blockId: 'coordinator-report:report-1', identitySource: 'contract',
      source: {}, reportTurnId: 'report-1', reportKind: 'progress', title: '18:00 状态',
      createdAt: Date.UTC(2026, 10, 11, 10, 1), text: text({
        translatedMarkdown: '18:00 状态\n\n重启改到 22:00', displayMarkdown: '18:00 状态\n\n重启改到 22:00',
        displayMode: 'translated', translationModel: 'kimi',
      }),
      files: [],
    };
    renderCoordinatorReportBlock(node, block, ports);
    expect(node.classList.contains('conversation-coordinator-report')).toBe(true);
    expect(node.dataset).toMatchObject({ reportKind: 'progress', reportTurnId: 'report-1' });
    const fold = node.firstElementChild as HTMLDetailsElement;
    expect(fold.tagName).toBe('DETAILS');
    expect(fold.open).toBe(false);
    // The row names the report, its title and time; the body waits inside.
    const head = fold.firstElementChild!;
    expect(head.tagName).toBe('SUMMARY');
    expect(head.querySelector('.conversation-coordinator-report__label')?.textContent).toBe('负责人汇报 · 进展');
    expect(head.querySelector('.conversation-coordinator-report__title')?.textContent).toBe('18:00 状态');
    const time = head.querySelector('time')!;
    expect(time.dateTime).toBe('2026-11-11T10:01:00.000Z');
    expect(time.textContent).toMatch(/\d\d:01/);
    expect(head.lastElementChild?.classList.contains('conversation-disclosure-chevron')).toBe(true);
    const body = fold.querySelector<HTMLElement>(':scope > [data-coordinator-report-body]')!;
    expect(body.querySelector('.md-content')?.textContent).toContain('重启改到 22:00');
    expect(body.querySelector('details.bilingual-block summary')?.textContent).toContain('Original');
    expect(body.querySelector('button[data-conversation-action="copy-block"]')).not.toBeNull();
    expect(node.querySelector('.conversation-answer-label')).toBeNull();

    // The reader opens it; a repaint keeps that choice and the card's nodes.
    fold.open = true;
    renderCoordinatorReportBlock(node, { ...block, reportKind: 'delivery', title: '交付完成' }, ports);
    expect(node.firstElementChild).toBe(fold);
    expect(fold.open).toBe(true);
    expect(fold.firstElementChild).toBe(head);
    expect(node.querySelector('[data-coordinator-report-body]')).toBe(body);
    expect(head.querySelector('.conversation-coordinator-report__label')?.textContent).toBe('负责人汇报 · 交付');
    expect(head.querySelector('.conversation-coordinator-report__title')?.textContent).toBe('交付完成');
    expect(node.querySelectorAll('[data-coordinator-report-body]')).toHaveLength(1);

    // Folding it again is equally the reader's: a repaint never reopens it.
    fold.open = false;
    renderCoordinatorReportBlock(node, block, ports);
    expect(fold.open).toBe(false);
  });

  /* Owner request 2026-11-17: the owner finds deliverables through reports,
   * so a delivery opens by itself and its files stay in view as chips. */
  it('opens a delivery by default and keeps its attached files in view', () => {
    const node = document.createElement('section');
    document.body.append(node);
    const block: CoordinatorReportBlockViewModel = {
      kind: 'coordinator-report', blockId: 'coordinator-report:report-2', identitySource: 'contract',
      source: {}, reportTurnId: 'report-2', reportKind: 'delivery', title: 'Resume ready',
      createdAt: Date.UTC(2026, 10, 17, 9, 0), text: text({ markdown: 'Resume ready\n\nBoth versions.' }),
      files: [
        { artifactId: 'art-pdf', filename: 'resume.pdf', kind: 'file', path: 'data/out/resume.pdf' },
        { artifactId: 'art-deck', filename: 'talk.pptx', kind: 'deck' },
      ],
    };
    renderCoordinatorReportBlock(node, block, ports);
    const fold = node.querySelector<HTMLDetailsElement>('details.conversation-coordinator-report__fold')!;
    expect(fold.open).toBe(true);
    const chips = [...node.querySelectorAll<HTMLButtonElement>('.conversation-coordinator-report__file')];
    // Chips sit outside the fold, so folding the report never hides its files.
    expect(chips.every((chip) => !fold.contains(chip))).toBe(true);
    expect(chips.map((chip) => [chip.textContent, chip.dataset.conversationAction, chip.dataset.operation,
      chip.dataset.fileKind])).toEqual([
      ['resume.pdf', 'open-artifact', 'art-pdf', 'file'], ['talk.pptx', 'open-artifact', 'art-deck', 'deck']]);
    expect(chips[0].title).toBe('data/out/resume.pdf');
    expect(chips[0].getAttribute('aria-label')).toBe('Open {name}');

    // The reader folds it; a repaint with the same files keeps chips and choice.
    fold.open = false;
    renderCoordinatorReportBlock(node, { ...block }, ports);
    expect(fold.open).toBe(false);
    expect(node.querySelector('.conversation-coordinator-report__file')).toBe(chips[0]);
    renderCoordinatorReportBlock(node, { ...block, files: [] }, ports);
    expect(node.querySelector('.conversation-coordinator-report__files')).toBeNull();
  });
});
