// @vitest-environment jsdom
import { expect, it } from 'vitest';
import type { ConversationRoundDirectory } from '../../api/conversation-sync.generated';
import { createTurnState } from '../../conversation/domain/turn-store';
import { createExactTurnNavigation } from './round-directory-navigation';

it('repaints an unchanged bar on every streamed commit without a DOM write', async () => {
  const host = document.createElement('nav');
  host.id = 'turnNav';
  document.body.replaceChildren(host);
  const state = createTurnState('conv');
  state.turnsById.human = { turnId: 'human', actor: 'human', projection: { content: 'question' } } as never;
  state.laneOrder.main = ['human'];
  const page = { totalRounds: 1, activeRound: 1,
    entries: [{ turnId: 'human', roundNumber: 1, actor: 'human' }] } as unknown as ConversationRoundDirectory;
  const nav = createExactTurnNavigation({
    host, translate: key => key, scrollToTurn: () => undefined,
    directoryKey: () => 'head-1', directory: async () => page,
  });
  nav.update('conv', state);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(host.querySelector('.turn-nav-readout')?.textContent).toBe('1 / 1');
  const observer = new MutationObserver(() => undefined);
  observer.observe(host, { subtree: true, childList: true, attributes: true, characterData: true });
  for (let commit = 0; commit < 3; commit += 1) nav.update('conv', state);
  expect(observer.takeRecords().map(record => `${(record.target as Element).className}@${record.attributeName}`)).toEqual([]);
  observer.disconnect();
  nav.destroy();
});
