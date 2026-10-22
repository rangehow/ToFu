// @vitest-environment jsdom
/** Semantic latest navigation and commit membership, independent of DOM paging. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createConversationSurface } from './conversation-surface';

it('restores the logical tail, retains a bounded working set and avoids redundant commits', () => {
  const container = document.createElement('main'); document.body.append(container);
  const state = createTurnState('conv');
  for (let i = 0; i < 30; i += 1) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main', ordinal: i,
      actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: i, updatedAt: i, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  const commits: Array<ReadonlyArray<string>> = [];
  let following = true;
  const surface = createConversationSurface(container, {
    windowing: { maxTurns: 8, batchSize: 3 },
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {}, isNearBottom: () => true,
      suspendFollow: () => { following = false; },
      followLatest: () => { following = true; },
    },
    onCommit: ids => { commits.push(ids); },
  });
  try {
    surface.render(selectConversationViewModel(state));
    expect(surface.mountedTurnIds).toEqual(state.laneOrder.main!.slice(-8));
    for (let i = 0; i < 100; i += 1) surface.followLatest({ force: false });
    expect(commits).toHaveLength(1);
    surface.showEarlier();
    expect(surface.isNearLatest()).toBe(false); // Physical bottom is still true.
    const earlier = surface.mountedTurnIds;
    surface.followLatest({ force: false });
    expect(surface.mountedTurnIds).toBe(earlier);
    surface.followLatest();
    expect(surface.isNearLatest()).toBe(true);
    expect(surface.mountedTurnIds).toEqual(state.laneOrder.main!.slice(-8));
    expect(following).toBe(true);
    expect(commits.every(ids => ids.length <= 8)).toBe(true);
    expect(state.laneOrder.main).toHaveLength(30);
    surface.dispose(); surface.followLatest();
    expect(container.childElementCount).toBe(0);
  } finally { surface.dispose(); container.remove(); }
});

it('aligns mounted and windowed targets and fences already-delivered history demand', () => {
  const viewport = document.createElement('main');
  const container = document.createElement('div');
  viewport.append(container); document.body.append(viewport);
  const frames: FrameRequestCallback[] = [];
  const view = viewport.ownerDocument.defaultView!;
  const request = view.requestAnimationFrame, cancel = view.cancelAnimationFrame;
  view.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  view.cancelAnimationFrame = () => {}; // Native cancellation may be too late.
  Object.defineProperty(viewport, 'clientHeight', { value: 700 });
  viewport.getBoundingClientRect = () => ({ top: 0, bottom: 700 } as DOMRect);
  const state = createTurnState('conv');
  for (let i = 0; i < 40; i += 1) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main', ordinal: i,
      actor: 'human', kind: 'input', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: i, updatedAt: i, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  let following = true;
  const targets: string[] = [];
  const surface = createConversationSurface(container, {
    scrollViewport: viewport,
    windowing: { maxTurns: 8, batchSize: 3 },
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {},
      suspendFollow: () => { following = false; },
      scrollToTurn(root, id, onScroll) {
        if (!root.querySelector(`[data-turn-id="${id}"]`)) return false;
        targets.push(id); viewport.scrollTop = 100; onScroll?.(); return true;
      },
    },
  });
  try {
    surface.render(selectConversationViewModel(state));
    expect(surface.mountedTurnIds).not.toContain('turn-20');
    // A previously queued scroll callback must not move the new target window.
    const boundary = surface.root.querySelector<HTMLElement>('[data-history-boundary="earlier"]')!;
    boundary.getBoundingClientRect = () => ({ top: 100, bottom: 101 } as DOMRect);
    viewport.scrollTop = 200; viewport.dispatchEvent(new Event('scroll'));
    expect(frames).toHaveLength(1);
    expect(surface.revealTurn('turn-20')).toBe(true);
    const mounted = [...surface.mountedTurnIds];
    frames.splice(0).forEach(callback => callback(0));
    expect(surface.mountedTurnIds).toEqual(mounted);
    expect(targets).toEqual(['turn-20']);
    // An already-mounted target must align too, without rebuilding the window.
    expect(surface.revealTurn('turn-22')).toBe(true);
    expect(targets).toEqual(['turn-20', 'turn-22']);
    expect(surface.mountedTurnIds).toEqual(mounted);
    viewport.dispatchEvent(new Event('scroll'));
    expect(frames).toHaveLength(0); // The jump's own event isn't lazy demand.
    expect(surface.revealTurn('missing')).toBe(false);
  } finally {
    surface.dispose(); viewport.remove();
    view.requestAnimationFrame = request; view.cancelAnimationFrame = cancel;
  }
});
