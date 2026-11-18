/** A question its lead withdrew (ask_human `withdraw`) is marked so and stays answerable. */
import { describe, expect, it } from 'vitest';
import type { Translator } from '../../i18n';
import { createToolHumanGuidancePresentation } from './tool-human-guidance-presentation';

const translate = ((key: string, params?: { reason?: unknown }) =>
  `${key}${params?.reason === undefined ? '' : `:${String(params.reason)}`}`) as Translator;
const guidance = createToolHumanGuidancePresentation({ translate, renderMarkdown: text => text });
const header = { iconHtml: '', queryHtml: '' };
const withdrawn = { reason: 'Owner answered <in chat>', at: 1 };

describe('withdrawn owner question card', () => {
  it('marks a late-answerable blocking card withdrawn and keeps its inputs', () => {
    const html = guidance.renderGuidanceHtml({
      toolName: 'ask_human', status: 'awaiting_human', guidanceId: 'hg_one', guidanceQuestion: 'Which scope?',
      _turnSettled: true, _hgAnswerGuidance: true, _turnId: 'turn-1', guidanceWithdrawn: withdrawn,
    }, header);
    expect(html).toContain('hg-withdrawn');
    expect(html).toContain('toolHumanGuidance.withdrawnBadge');
    expect(html).toContain('toolHumanGuidance.withdrawnHint:Owner answered &lt;in chat&gt;');
    expect(html).toContain('data-hg-late-answer="1"');
    expect(html).toContain('<textarea');
  });

  it('marks a non-blocking card withdrawn and keeps its chat reply', () => {
    const html = guidance.renderGuidanceHtml({
      toolName: 'ask_human', status: 'done', guidanceBlocking: false, guidanceId: 'hg_two',
      guidanceQuestion: 'Papers first?', guidanceWithdrawn: withdrawn,
    }, header);
    expect(html).toContain('toolHumanGuidance.withdrawnBadge');
    expect(html).toContain('data-hg-reply="chat"');
  });

  it('an answer given anyway wins over the withdrawal', () => {
    const html = guidance.renderGuidanceHtml({
      toolName: 'ask_human', status: 'submitted', guidanceId: 'hg_one', _hgUserResponse: 'do both',
      guidanceWithdrawn: withdrawn,
    }, header);
    expect(html).toContain('project.hgAnswered');
    expect(html).not.toContain('withdrawn');
  });
});
