// @vitest-environment jsdom
/** Program fallback retains the concurrent evidence-first presentation contract. */
import { expect, it, vi } from 'vitest';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import { disposeToolBody, renderProgramBlock, renderToolBlock } from './tool-blocks';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

it.each(['completed', 'error'] as const)('keeps %s program evidence safe and distinguishes model handoff', status => {
  const dom = new JSDOM('<!doctype html><body></body>');
  try {
    const document = dom.window.document;
    const node = document.createElement('div');
    const block = {
      kind: 'program', blockId: 'program',
      round: { programStatus: status, programCode: 'tools.observe("choose");',

        programStats: { outcome: 'needs_model' },
        programResult: { kind: 'observation', reason: 'Choose evidence', data: '<script>unsafe()</script>' } },
    } as unknown as Extract<ConversationBlockViewModel, { kind: 'program' }>;
    const before = JSON.stringify(block);
    renderProgramBlock(node, block, {} as ConversationTurnViewModel, {
      renderSafeMarkdownHtml: () => { throw new Error('Program evidence must remain code'); },
    });
    const result = node.querySelector<HTMLDetailsElement>('.ptc-result')!;
    expect(result.querySelector('summary')?.textContent)
      .toBe(status === 'completed' ? 'Evidence for judgment' : 'Program result');
    expect(result.open).toBe(true);
    expect(node.querySelector<HTMLDetailsElement>('.ptc-code')?.open).toBe(false);
    expect(node.textContent).toContain(status === 'completed' ? 'Returned for model judgment' : 'Failed');
    expect(node.querySelector('script')).toBeNull();
    expect(node.textContent).toContain('<script>unsafe()</script>');
    expect(JSON.stringify(block)).toBe(before);
  } finally { dom.window.close(); }
});

it('repaints a mounted tool card when a late fact settles, and stops after disposal', () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  try {
    const node = dom.window.document.createElement('div');
    const listeners = new Set<() => void>();
    let title = '';
    const block = {
      kind: 'tool', blockId: 'review', name: 'review_project_work', toolCallId: 'call_review',
      round: { toolName: 'review_project_work', status: 'done', toolContent: '{}' },
    } as unknown as Extract<ConversationBlockViewModel, { kind: 'tool' }>;
    const ports = {
      renderSafeMarkdownHtml: (source: string) => source,
      renderToolBlockHtml: () => `<details class="ptool-result-block" data-tool-card="review_project_work"><summary>${title}</summary></details>`,
      subscribeToolCards: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    };
    renderToolBlock(node, block, { status: 'completed', source: {} } as unknown as ConversationTurnViewModel, ports);
    expect(node.querySelector('summary')?.textContent).toBe('');
    // Re-rendering replaces, never stacks, the subscription.
    renderToolBlock(node, block, { status: 'completed', source: {} } as unknown as ConversationTurnViewModel, ports);
    expect(listeners.size).toBe(1);
    title = 'Back up repositories';
    for (const listener of [...listeners]) listener();
    expect(node.querySelector('summary')?.textContent).toBe('Back up repositories');
    disposeToolBody(node);
    expect(listeners.size).toBe(0);
  } finally { dom.window.close(); }
});
