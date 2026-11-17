// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { TAG_LEAD_PILL, TAG_PILL, tagUnit } from './layout';
import { packLines, tagBox, tagLabels } from './life';

const span = (left: number, width: number) => ({ left, right: left + width });

describe('tag labels', () => {
  it('names a Worker by the lead clause of its task title, skipping an all-capitals marker', () => {
    expect(tagLabels([
      'Forensics: why every Tofu restart at boot is slow', 'Harness speed: first-turn burst latency',
      'URGENT: Land the execution-shard speedup', "Remove 'computer control' (local client)", 'Paper B draft and figures',
      '修复会话列表：加载太慢',
    ])).toEqual(['Forensics', 'Harness speed', 'Land the execution-shard speedup', "Remove 'computer control'", 'Paper B draft and figures', '修复会话列表']);
  });

  it('tells teammates who share a lead clause apart by the clause after it', () => {
    expect(tagLabels(['Multi-device: tool routing to devices', 'Multi-device: node program, identity', 'Paper 3: throughput']))
      .toEqual(['tool routing to devices', 'node program, identity', 'Paper 3']);
  });
});

describe('tag size', () => {
  it('draws tags at the size the floor plan keeps room for (layout.ts tagUnit), never below the smallest legible size', () => {
    for (const pixel of [0.5, 0.85, 1, 1.6, 2.4]) {
      expect(tagBox('sans-serif', 'Paper B', false, 0, 0, pixel).h).toBeCloseTo(TAG_PILL * tagUnit(pixel), 9);
      expect(tagBox('sans-serif', 'tofu lead', true, 0, 0, pixel).h).toBeCloseTo(TAG_LEAD_PILL * tagUnit(pixel), 9);
      expect(tagBox('sans-serif', 'Paper B', false, 0, 0, pixel).h / pixel).toBeGreaterThanOrEqual(TAG_PILL * 10 / 8.5 - 1e-9);
    }
  });
});

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

  it('keeps tags at most two desks wide on two lines, each centred on its desk', () => {
    // Four desks 70 apart, every tag as wide as two desks allow: neighbours alternate, every other one shares a line.
    const widest = 140 - 5;
    const lines = packLines([0, 70, 140, 210].map(x => span(x - widest / 2, widest)), 4);
    expect(lines).toEqual([0, 1, 0, 1]);
  });

  it('never puts two overlapping spans on the same line', () => {
    const spans = [span(0, 140), span(20, 140), span(70, 60), span(150, 30), span(160, 90)];
    const lines = packLines(spans, 4);
    spans.forEach((a, i) => spans.forEach((b, j) => {
      if (i < j && lines[i] === lines[j]) expect(a.right + 4 <= b.left || b.right + 4 <= a.left).toBe(true);
    }));
  });
});
