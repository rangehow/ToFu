import { describe, expect, it } from 'vitest';
import stream from '../../../../tests/fixtures/office_facts/stream_v1.json';
import type { RouterOfficeFacts } from '../../api/router.generated';
import {
  busyConversations, changedFacts, createFactStore, factQuestions, factsReplaceHints, openAlarms, type FactFrame,
} from './facts';

const snapshot = stream.snapshot as unknown as RouterOfficeFacts;
const frames = stream.frames as unknown as FactFrame[];
const LEAD = 'coordinator-1a390889aa954da2b18551d1b4a32625';

describe('office facts follow the recorded stream (tests/fixtures/office_facts/stream_v1.json)', () => {
  it('applies the ordered frames and ends with the recorded keys', () => {
    const store = createFactStore();
    store.reset(snapshot);
    for (const frame of frames) expect(store.apply(frame).kind).toBe('applied');
    expect([...store.view()!.facts.keys()].sort()).toEqual([...stream.expectedAfterFrames].sort());
  });

  it('asks for a fresh snapshot on a gap and on a new stream, and ignores a frame it already holds', () => {
    const gap = createFactStore();
    gap.reset(snapshot);
    const [first, , third] = frames;
    expect(stream.gap.delivered).toEqual([1, 3]);
    expect(gap.apply(first).kind).toBe('applied');
    expect(gap.apply(third).kind).toBe('resnapshot');
    expect(gap.apply(first).kind).toBe('stale');

    const restart = createFactStore();
    restart.reset(snapshot);
    const moved = stream.restart.frames as unknown as FactFrame[];
    expect(restart.apply(moved[0]).kind).toBe('resnapshot');
    restart.reset(stream.restart.snapshot as unknown as RouterOfficeFacts);
    expect(restart.apply(moved[0]).kind).toBe('applied');
  });

  it('reads busy people, open owner questions and incidents only where the snapshot covers them completely', () => {
    const store = createFactStore();
    expect(busyConversations(store.view())).toBeNull();
    store.reset(snapshot);
    expect([...busyConversations(store.view())!].sort()).toEqual(
      ['worker-15082de381a8dafbf673dde3', 'worker-dfaa412580f8e85ff5bf48d6']);
    for (const frame of frames.slice(0, 11)) store.apply(frame);
    const [question] = factQuestions(store.view(), LEAD, null)!;
    expect(question).toMatchObject({ conversationId: LEAD, turnId: '6364198d-7e89-45fd-a7b4-22fe15be7e8f',
      ordinal: 530, blocking: true, question: '' });
    const withText = factQuestions(store.view(), LEAD, [{ ...question, question: 'Keep 8 jobs?', createdAt: 9 }])!;
    expect(withText[0]).toMatchObject({ question: 'Keep 8 jobs?', createdAt: 9 });
    store.apply(frames[11]);
    expect(openAlarms(store.view())).toBe(1);
    for (const frame of frames.slice(12)) store.apply(frame);
    expect(factQuestions(store.view(), LEAD, null)).toEqual([]);
    expect(openAlarms(store.view())).toBe(0);
    const partial = createFactStore();
    partial.reset({ ...snapshot, coverage: { ...snapshot.coverage, busy: 'unsupported' } });
    expect(busyConversations(partial.view())).toBeNull();
  });

  it('replaces push hints only when busy is complete and reports, questions and boards arrive as frames', () => {
    const store = createFactStore();
    expect(factsReplaceHints(store.view())).toBe(false);
    store.reset(snapshot);
    expect(snapshot.coverage.reports).toBe('newest_page');
    expect(factsReplaceHints(store.view())).toBe(true);
    store.reset({ ...snapshot, coverage: { ...snapshot.coverage, decisions: 'unavailable' } });
    expect(factsReplaceHints(store.view())).toBe(false);
    store.reset({ ...snapshot, coverage: { ...snapshot.coverage, busy: 'newest_page' } });
    expect(factsReplaceHints(store.view())).toBe(false);
  });

  it('lists what a fresh snapshot changed: new or different facts as set, vanished ones as cleared', () => {
    const before = createFactStore();
    before.reset(snapshot);
    const after = createFactStore();
    after.reset(snapshot);
    for (const frame of frames.slice(0, 2)) after.apply(frame);
    const changes = changedFacts(before.view()!.facts, after.view()!.facts);
    expect(changes.map(change => [change.fact.key, change.state]).sort()).toEqual([
      [frames[0].key, 'cleared'], [frames[1].key, 'set'],
    ].sort());
    expect(changedFacts(after.view()!.facts, after.view()!.facts)).toEqual([]);
  });
});
