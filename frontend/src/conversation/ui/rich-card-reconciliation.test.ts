// @vitest-environment jsdom
/** Reader state (focus, selection) survives Surface moves at one snapshot per render. */
import { afterEach, expect, it, vi } from 'vitest';
import { moveRetainedNodeBefore, preserveReaderState } from './rich-card-reconciliation';

afterEach(() => { vi.restoreAllMocks(); document.body.replaceChildren(); });

/** A container whose native move behaves like Chromium's: the moved subtree's selection is lost. */
function blocks(count: number) {
  const root = document.createElement('section');
  const container = document.createElement('div');
  Object.defineProperty(container, 'moveBefore', {
    value(node: Node, before: Node | null) { container.insertBefore(node, before); },
  });
  for (let index = 0; index < count; index += 1) {
    const block = document.createElement('article');
    block.textContent = `block ${index} text`;
    container.append(block);
  }
  root.append(container);
  document.body.append(root);
  return { root, container, children: () => [...container.children] as HTMLElement[] };
}

function reverse(container: HTMLElement): void {
  for (const node of [...container.children] as HTMLElement[]) {
    moveRetainedNodeBefore(container, node, container.firstChild);
  }
}

it('reads the selection once per render however many blocks move, and keeps it', () => {
  const { root, container, children } = blocks(12);
  const selected = children()[5].firstChild!;
  document.getSelection()!.setBaseAndExtent(selected, 2, selected, 7);
  const reads = vi.spyOn(document, 'getSelection');
  preserveReaderState(root, () => reverse(container));
  // A per-move read forces a style recalculation in Chromium after every write.
  expect(reads.mock.calls.length).toBeLessThanOrEqual(2);
  expect(children()[6].textContent).toBe('block 5 text');
  const selection = document.getSelection()!;
  expect([selection.anchorNode, selection.anchorOffset, selection.focusOffset]).toEqual([selected, 2, 7]);
  expect(selection.toString()).toBe('ock 5');
});

it('returns focus a move dropped but never undoes focus the render moved', () => {
  const { root, container, children } = blocks(3);
  const button = document.createElement('button');
  children()[2].append(button);
  button.focus();
  preserveReaderState(root, () => {
    container.insertBefore(children()[2], container.firstChild);
    expect(document.activeElement).toBe(document.body);
  });
  expect(document.activeElement).toBe(button);
  const elsewhere = document.createElement('input');
  root.append(elsewhere);
  preserveReaderState(root, () => {
    container.insertBefore(children()[2], container.firstChild);
    elsewhere.focus();
  });
  expect(document.activeElement).toBe(elsewhere);
});

it('keeps per-move protection outside a preserved render', () => {
  const { container, children } = blocks(4);
  const selected = children()[1].firstChild!;
  document.getSelection()!.setBaseAndExtent(selected, 0, selected, 5);
  moveRetainedNodeBefore(container, children()[1], null);
  expect(document.getSelection()!.toString()).toBe('block');
});
