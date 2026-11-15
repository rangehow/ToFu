// @vitest-environment jsdom
/** The finish footer names a failed whole-turn translation on generated turns, as text only. */
import { afterEach, describe, expect, it } from 'vitest';
import en from '../../../i18n/locales/en.json';
import zh from '../../../i18n/locales/zh.json';
import type { TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import {
  selectConversationViewModel,
  type ConversationTurnViewModel,
  type TranslationActivity,
} from '../../presentation/conversation-view-model';
import { renderTurnFooter } from './turn-chrome';
import type { ClassicConversationRendererPorts } from './ports';

const CAUSE = 'Upstream rate-limit attempt budget exhausted (8/8)';
const FAILED: TurnProjection = {
  model: 'gpt-6.1-sol',
  translation: { sendFailure: { reason: 'worker_error', error: CAUSE, at: 1794450199 } },
};
const CLASSIC_FOOTER = '<span class="finish-tag preset">gpt-6.1-sol</span>';

function footerTurn(projection: TurnProjection, options: {
  actor?: TurnRecord['actor'];
  activity?: TranslationActivity;
  commandPending?: string;
} = {}): ConversationTurnViewModel {
  const actor = options.actor ?? 'assistant';
  const turn: TurnRecord = { turnId: 'reply', presentationId: 'reply',
    conversationId: 'translation-footer', laneId: 'main', ordinal: 1, actor,
    kind: actor === 'human' ? 'input' : 'reply', runId: '', status: 'completed',
    currentAttemptId: null, createdAt: 1000, updatedAt: 2000, projectionRevision: 1, settlement: {},
    projection: { content: 'The answer', segments: [{ type: 'text', blockId: 'text:terminal',
      text: 'The answer', terminal: true, deliverable: true }], ...projection } };
  const state = createTurnState(turn.conversationId);
  state.turnsById[turn.turnId] = turn;
  state.laneOrder.main = [turn.turnId];
  if (options.commandPending) state.commandPending[turn.turnId] = options.commandPending;
  const presentation = options.activity
    ? { translationActivityByTurn: new Map([[turn.turnId, options.activity]]) } : {};
  return selectConversationViewModel(state, {}, presentation).mainLane.turns[0];
}

function ports(locale: 'en' | 'zh' = 'en', classicFooter?: string): ClassicConversationRendererPorts {
  const messages: Record<string, string> = locale === 'en' ? en : zh;
  return {
    renderSafeMarkdownHtml: markdown => markdown,
    localizedText: (key, fallback) => messages[key] ?? fallback,
    ...(classicFooter === undefined ? {} : { renderTurnFooterHtml: () => classicFooter }),
  };
}

function render(turn: ConversationTurnViewModel, rendererPorts: ClassicConversationRendererPorts,
  node: HTMLElement = document.createElement('div')): HTMLElement {
  renderTurnFooter(node, turn, rendererPorts);
  return node;
}

afterEach(() => document.body.replaceChildren());

describe('translation failure footer tag', () => {
  it.each([['en', 'Translation failed'], ['zh', '翻译失败']] as const)(
    'shows the %s label with the recorded cause as its title', (locale, label) => {
      for (const classicFooter of [undefined, CLASSIC_FOOTER]) {
        const node = render(footerTurn(FAILED), ports(locale, classicFooter));
        const tag = node.querySelector<HTMLElement>('.finish-tag.translation-failed');
        expect(tag?.textContent).toBe(label);
        expect(tag?.title).toBe(CAUSE);
        expect(node.hidden).toBe(false);
      }
    });

  it('keeps the production finish bar intact and the cause inert', () => {
    const markup = '<img src=x onerror=alert(1)>';
    const node = render(footerTurn({ ...FAILED, translation: { sendFailure: { error: markup } } }),
      ports('en', CLASSIC_FOOTER));
    expect([...node.children].map(child => child.className))
      .toEqual(['finish-tag preset', 'finish-tag warn translation-failed']);
    expect(node.querySelector('img')).toBeNull();
    expect(node.querySelector<HTMLElement>('.translation-failed')?.title).toBe(markup);
  });

  it.each([undefined, CLASSIC_FOOTER])('withdraws the tag on the same node while a retry is pending (classic %s)',
    classicFooter => {
      const rendererPorts = ports('en', classicFooter);
      const node = render(footerTurn(FAILED), rendererPorts);
      expect(node.querySelector('.translation-failed')).not.toBeNull();
      render(footerTurn(FAILED, { activity: { status: 'pending', message: 'Translating…' } }),
        rendererPorts, node);
      expect(node.querySelector('.translation-failed')).toBeNull();
      if (classicFooter) expect(node.querySelector('.finish-tag.preset')).not.toBeNull();
      else expect(node.querySelector('.translation-pending')?.textContent).toBe('Translating…');
    });

  it('shows nothing once translated, while a command is pending, or on a human input', () => {
    const cases = [
      footerTurn({ ...FAILED, translatedContent: '答案',
        translation: { status: 'completed', sendFailure: FAILED.translation?.sendFailure } }),
      footerTurn(FAILED, { commandPending: 'resume' }),
      footerTurn(FAILED, { actor: 'human' }),
    ];
    for (const turn of cases) {
      for (const classicFooter of [undefined, CLASSIC_FOOTER]) {
        expect(render(turn, ports('en', classicFooter)).querySelector('.translation-failed')).toBeNull();
      }
    }
    expect(render(cases[2], ports('en', CLASSIC_FOOTER)).hidden).toBe(true);
  });
});
