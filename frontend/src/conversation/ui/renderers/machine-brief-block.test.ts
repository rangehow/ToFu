// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { TextBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { isMachineBrief, renderMachineBriefBlock } from './machine-brief-block';

/* Owner request 2026-11-17: in a lead conversation the project brain's
 * automatic continuation briefs outweighed everything written for the owner.
 * A brief reads as one folded row and renders its body only when opened. */

afterEach(() => document.body.replaceChildren());

let renders = 0;
const ports: ClassicConversationRendererPorts = {
  renderSafeMarkdownHtml: (text) => {
    renders += 1;
    const safe = document.createElement('p');
    safe.textContent = text;
    return safe.outerHTML;
  },
  localizedText: (key, fallback) => (key === 'conversation.machineBrief.label' ? '自动指令' : fallback),
};

const brief = (markdown: string): TextBlockViewModel => ({
  kind: 'text', blockId: 'text:input', source: {}, identitySource: 'contract', markdown,
  displayMarkdown: markdown, displayMode: 'original', terminal: true, deliverable: false, resumable: false,
});

const turn = (actor: string, initiator?: string) => ({
  turnId: 'brief-1', actor, role: 'user', kind: 'input', status: 'completed', blocks: [], branches: [],
  actions: [], metadata: initiator ? { origin: { blockId: 'origin', initiator } } : {},
  source: { createdAt: 1, projection: {} },
}) as unknown as ConversationTurnViewModel;

describe('machine brief presentation', () => {
  it('applies only to project-brain input from the virtual user', () => {
    expect(isMachineBrief(turn('virtual_user', 'brain'))).toBe(true);
    expect(isMachineBrief(turn('virtual_user', 'autopilot'))).toBe(false);
    expect(isMachineBrief(turn('human', 'brain'))).toBe(false);
    expect(isMachineBrief(undefined)).toBe(false);
  });

  it('reads as one folded row and renders the brief only once it is opened', () => {
    const node = document.createElement('section');
    document.body.append(node);
    renders = 0;
    const text = '\n## Continue the request.\n\nYour task board, open owner questions and Worker states follow.';
    renderMachineBriefBlock(node, brief(text), ports, turn('virtual_user', 'brain'));
    const fold = node.querySelector<HTMLDetailsElement>('details.conversation-machine-brief__fold')!;
    expect(fold.open).toBe(false);
    expect(node.querySelector('.conversation-machine-brief__label')?.textContent).toBe('自动指令');
    expect(node.querySelector('.conversation-machine-brief__preview')?.textContent).toBe('Continue the request.');
    expect(renders).toBe(0);
    expect(node.querySelector('.md-content')).toBeNull();

    fold.open = true;
    fold.dispatchEvent(new Event('toggle'));
    expect(renders).toBe(1);
    expect(node.querySelector('.md-content')?.textContent).toContain('Worker states follow');

    // An unchanged repaint keeps the reader's choice and renders nothing again.
    renderMachineBriefBlock(node, brief(text), ports, turn('virtual_user', 'brain'));
    expect(fold.open).toBe(true);
    expect(node.querySelector('details.conversation-machine-brief__fold')).toBe(fold);
  });
});
