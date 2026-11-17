import { describe, expect, it } from 'vitest';
import { packLines } from './life';

const span = (left: number, width: number) => ({ left, right: left + width });

describe('desk tag lines', () => {
  it('keeps a row of short names on one line', () => {
    expect(packLines([span(0, 50), span(70, 50), span(140, 50), span(210, 50)], 4)).toEqual([0, 0, 0, 0]);
  });

  it('moves only a tag that would touch its neighbour, onto the innermost free line', () => {
    // The second name is long and reaches the third desk; the third goes out one line, the fourth fits again.
    expect(packLines([span(0, 50), span(56, 120), span(140, 50), span(210, 50)], 4)).toEqual([0, 0, 1, 0]);
  });

  it('respects the gap and is independent of input order', () => {
    expect(packLines([span(100, 40), span(0, 98)], 4)).toEqual([1, 0]);
    expect(packLines([span(100, 40), span(0, 96)], 4)).toEqual([0, 0]);
  });

  it('never puts two overlapping spans on the same line', () => {
    const spans = [span(0, 140), span(20, 140), span(70, 60), span(150, 30), span(160, 90)];
    const lines = packLines(spans, 4);
    spans.forEach((a, i) => spans.forEach((b, j) => {
      if (i < j && lines[i] === lines[j]) expect(a.right + 4 <= b.left || b.right + 4 <= a.left).toBe(true);
    }));
  });
});
