// @vitest-environment jsdom
/** Reader intent must win over streaming, late layout and nested evidence. */
import { afterEach, expect, it, vi } from 'vitest';
import { createConversationViewportPort } from './conversation-viewport';

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).reverse().forEach(dispose => dispose()); });

function fixture(cancel = () => {}) {
  const viewport = document.createElement('main');
  const root = document.createElement('article');
  viewport.append(root); document.body.append(viewport);
  let height = 1000, top = 800;
  const writes: string[] = [];
  Object.defineProperties(viewport, {
    clientHeight: { get: () => 200 },
    scrollHeight: { get: () => height },
    scrollTop: { get: () => top, set: (value: number) => {
      writes.push(viewport.style.scrollBehavior);
      top = Math.max(0, Math.min(value, height - 200));
    } },
  });
  const frames: Array<() => void> = [];
  const port = createConversationViewportPort(viewport, {
    // Deliberately adversarial: an already delivered callback cannot be recalled.
    scheduleAfterLayout: callback => { frames.push(callback); return cancel; },
  });
  cleanups.push(() => { port.dispose?.(); viewport.remove(); });
  return { viewport, root, port, writes, frames,
    grow: () => { height += 200; },
    userScroll: (value: number) => { top = value; viewport.dispatchEvent(new Event('scroll')); },
    flush: () => { frames.splice(0).forEach(callback => callback()); },
  };
}

it('does not rearm follow during a small upward gesture inside the bottom threshold', () => {
  const f = fixture();
  f.port.followLatest?.(f.root);
  f.viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -10 }));
  f.userScroll(790);
  expect(f.port.following).toBe(false);
  f.grow(); f.flush();
  expect(f.viewport.scrollTop).toBe(790);
  f.userScroll(1000);
  expect(f.port.following).toBe(true);
});

it('recognizes the first scrollbar move and keyboard intent before the next paint', () => {
  const f = fixture();
  f.userScroll(500);
  expect(f.port.following).toBe(false);
  f.port.followLatest?.(f.root);
  f.viewport.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true }));
  f.grow(); f.flush();
  expect(f.viewport.scrollTop).toBe(800);
  expect(f.port.following).toBe(false);
});

it('keeps inner historical evidence scrolling independent of the outer follow latch', () => {
  const f = fixture();
  const inner = document.createElement('div');
  inner.className = 'conversation-history-process__body'; f.root.append(inner);
  inner.dispatchEvent(new WheelEvent('wheel', { deltaY: -10, bubbles: true }));
  for (const [type, y] of [['touchstart', 10], ['touchmove', 20], ['touchmove', 30]] as const) {
    const event = new Event(type, { bubbles: true });
    Object.defineProperty(event, 'touches', { value: { item: () => ({ clientY: y }) } });
    inner.dispatchEvent(event);
  }
  expect(f.port.following).toBe(true);
});

it('pins and restores instantly while preserving the exact authored scroll style', () => {
  const f = fixture();
  f.viewport.style.setProperty('scroll-behavior', 'smooth', 'important');
  f.port.followLatest?.(f.root); f.grow(); f.flush();
  expect(f.writes.every(value => value === 'auto')).toBe(true);
  expect(f.viewport.scrollTop).toBe(1000);
  expect(f.viewport.style.getPropertyValue('scroll-behavior')).toBe('smooth');
  expect(f.viewport.style.getPropertyPriority('scroll-behavior')).toBe('important');
});

it('fences every late delivery and scroll write after disposal', () => {
  const f = fixture();
  f.port.followLatest?.(f.root); f.port.dispose?.();
  f.grow(); f.flush(); f.port.followLatest?.(f.root);
  expect(f.viewport.scrollTop).toBe(800);
});

it('releases listeners and fences layout even when native cancellation throws', () => {
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
    const f = fixture(() => { throw new Error('host has closed'); });
    f.port.followLatest?.(f.root);
    expect(() => f.port.dispose?.()).not.toThrow();
    f.grow(); f.flush();
    f.viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -10 }));
    expect(f.port.following).toBe(true); // Removed listener did not receive it.
    expect(f.viewport.scrollTop).toBe(800);
    expect(warning).toHaveBeenCalledTimes(1);
  } finally { warning.mockRestore(); }
});

it('falls back to the captured turn geometry when the reader block is retired', () => {
  const f = fixture();
  const turn = document.createElement('section'); turn.dataset.turnId = 'turn';
  const block = document.createElement('p'); block.dataset.blockId = 'block';
  turn.append(block); f.root.append(turn);
  f.viewport.getBoundingClientRect = () => ({ top: 0, bottom: 200 } as DOMRect);
  turn.getBoundingClientRect = () => ({ top: -20, bottom: 180 } as DOMRect);
  block.getBoundingClientRect = () => ({ top: 30, bottom: 90 } as DOMRect);
  f.userScroll(100);
  const snapshot = f.port.capture(f.root);
  expect(snapshot.anchorBlockId).toBe('block');
  block.remove(); f.port.restore(f.root, snapshot);
  expect(f.viewport.scrollTop).toBe(100);
});

function navigationFixture() {
  const f = fixture();
  f.viewport.getBoundingClientRect = () => ({ top: 0, bottom: 200 } as DOMRect);
  const positions = new Map([['older', 200], ['newer', 600]]);
  for (const [id] of positions) {
    const turn = document.createElement('section');
    turn.dataset.turnId = id;
    turn.getBoundingClientRect = () => ({
      top: positions.get(id)! - f.viewport.scrollTop,
      bottom: positions.get(id)! - f.viewport.scrollTop + 100,
    } as DOMRect);
    f.root.append(turn);
  }
  return { ...f, positions };
}

it('aligns a keyed Turn instantly and corrects one late layout through the same owner', () => {
  const f = navigationFixture();
  f.viewport.style.setProperty('scroll-behavior', 'smooth', 'important');
  const onScroll = vi.fn();
  expect(f.port.scrollToTurn?.(f.root, 'older', onScroll)).toBe(true);
  expect(f.viewport.scrollTop).toBe(200);
  expect(f.port.following).toBe(false);
  expect(onScroll).toHaveBeenCalledTimes(1);
  // A delayed native scroll event from our own write is not reader intent.
  f.viewport.dispatchEvent(new Event('scroll'));
  f.positions.set('older', 250); f.flush();
  expect(f.viewport.scrollTop).toBe(250);
  expect(onScroll).toHaveBeenCalledTimes(2);
  expect(f.writes).toEqual(['auto', 'auto']);
  expect(f.viewport.style.getPropertyValue('scroll-behavior')).toBe('smooth');
  expect(f.viewport.style.getPropertyPriority('scroll-behavior')).toBe('important');
});

it('does not redirect or suspend a viewport for an unavailable or foreign target', () => {
  const f = navigationFixture();
  expect(f.port.scrollToTurn?.(f.root, 'missing')).toBe(false);
  expect(f.port.scrollToTurn?.(document.createElement('article'), 'older')).toBe(false);
  expect(f.port.following).toBe(true);
  expect(f.writes).toEqual([]);
  expect(f.frames).toEqual([]);
});

it('supersedes a prior target without letting its already-delivered frame redirect the reader', () => {
  const f = navigationFixture();
  f.port.scrollToTurn?.(f.root, 'older');
  f.port.scrollToTurn?.(f.root, 'newer');
  f.positions.set('older', 250); f.positions.set('newer', 650);
  f.flush();
  expect(f.viewport.scrollTop).toBe(650);
  expect(f.writes).toHaveLength(3);
});

for (const gesture of ['wheel-up', 'wheel-down', 'PageUp', 'PageDown', 'touch', 'scrollbar'] as const) {
  it(`reader ${gesture} intent retires the deferred target correction`, () => {
    const f = navigationFixture();
    f.port.scrollToTurn?.(f.root, 'older');
    if (gesture.startsWith('wheel')) {
      f.viewport.dispatchEvent(new WheelEvent('wheel', {
        deltaY: gesture === 'wheel-up' ? -10 : 10,
      }));
    } else if (gesture === 'touch') {
      const event = new Event('touchstart');
      Object.defineProperty(event, 'touches', { value: { item: () => ({ clientY: 10 }) } });
      f.viewport.dispatchEvent(event);
    } else if (gesture === 'scrollbar') f.userScroll(220);
    else f.viewport.dispatchEvent(new KeyboardEvent('keydown', { key: gesture }));
    const readerTop = f.viewport.scrollTop;
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(readerTop);
    expect(f.writes).toHaveLength(1);
  });
}

for (const end of ['latest', 'dispose', 'detach'] as const) {
  it(`${end} retires a queued target correction`, () => {
    const f = navigationFixture();
    f.port.scrollToTurn?.(f.root, 'older');
    if (end === 'latest') f.port.followLatest?.(f.root);
    else if (end === 'dispose') f.port.dispose?.();
    else f.root.remove();
    const settledTop = f.viewport.scrollTop;
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(settledTop);
  });
}

it('skips a no-op anchor write but still corrects a real content shift instantly', () => {
  const f = navigationFixture();
  f.userScroll(100);
  const snapshot = f.port.capture(f.root);
  f.port.restore(f.root, snapshot);
  expect(f.writes).toEqual([]);
  f.positions.set('older', 225);
  f.port.restore(f.root, snapshot);
  expect(f.viewport.scrollTop).toBe(125);
  expect(f.writes).toEqual(['auto']);
});

for (const hasLogicalTail of [true, false]) {
  it(`a single scrollbar return cancels target correction and follows only a logical tail (${hasLogicalTail})`, () => {
    const f = navigationFixture();
    // A Surface excludes unmounted later content through this same port.
    const port = hasLogicalTail ? f.port : createConversationViewportPort(f.viewport, {
      canFollowLatest: () => false,
      scheduleAfterLayout: callback => { f.frames.push(callback); return () => {}; },
    });
    if (!hasLogicalTail) { f.port.dispose?.(); cleanups.push(() => port.dispose?.()); }
    port.scrollToTurn?.(f.root, 'older');
    f.userScroll(800);
    expect(port.following).toBe(hasLogicalTail);
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(800);
    if (hasLogicalTail) {
      f.grow(); port.followLatest?.(f.root, { force: false }); f.flush();
      expect(f.viewport.scrollTop).toBe(1000);
    }
  });
}
