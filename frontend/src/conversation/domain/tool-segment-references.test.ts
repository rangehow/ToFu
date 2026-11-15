import { describe, expect, it } from 'vitest';
import type { TurnProjection } from '../../api/conversation-sync.generated';
import { shareLivePatchedPayloads } from './tool-segment-references';

const projection = (): TurnProjection => ({
  content: '',
  toolRounds: [
    { toolCallId: 'call-1', toolName: 'read_files', toolArgs: 'a'.repeat(64), toolContent: 'o'.repeat(64),
      thinking: 't'.repeat(64), status: 'done' },
    { toolCallId: 'call-2', toolName: 'run_command', toolArgs: { command: 'ls' }, toolContent: { rows: 1 } },
  ],
  segments: [
    { type: 'thinking', blockId: 'thinking:1', text: 't'.repeat(64) },
    { type: 'tool_use', blockId: 'tool:call-1', id: 'call-1', name: 'read_files', input: 'a'.repeat(64),
      result: { content: 'o'.repeat(64), status: 'done' } },
    { type: 'tool_use', blockId: 'tool:call-2', id: 'call-2', name: 'run_command', input: { command: 'ls' },
      result: { content: { rows: 1 } } },
    { type: 'text', blockId: 'text:terminal', text: 'Done.' },
  ],
});

describe('shareLivePatchedPayloads', () => {
  it('returns the projection itself unless a mirrored payload was written', () => {
    const value = projection();
    for (const operations of [
      [{ op: 'append_text', path: ['content'], value: '!' },
        { op: 'append_text', path: ['segments', 3, 'text'], value: '!' }],
      [{ op: 'set', path: ['toolRounds', 0, 'status'], value: 'done' }],
      [{ op: 'set', path: ['segments', 1, 'blockId'], value: 'tool:call-1' }],
      [{ op: 'remove', path: ['segments', 2] }, { op: 'truncate', path: ['toolRounds'], length: 1 }],
      [{ op: 'set', path: ['toolRounds', 1, 'toolContent'], value: { rows: 1 } }],
    ]) {
      expect(shareLivePatchedPayloads(value, operations)).toBe(value);
    }
  });

  it('rewrites only equal strings at patched positions, copy-on-write', () => {
    const value = projection();
    const before = structuredClone(value);
    const shared = shareLivePatchedPayloads(value, [
      { op: 'set', path: ['segments', 1, 'result'], value: { content: 'o'.repeat(64), status: 'done' } },
      { op: 'set', path: ['toolRounds', 0, 'thinking'], value: 't'.repeat(64) },
    ]);
    expect(shared).not.toBe(value);
    expect(shared).toEqual(before);
    expect(value).toEqual(before);
    expect(shared.segments?.[0]).toBe(value.segments?.[0]);
    expect(shared.segments?.[2]).toBe(value.segments?.[2]);
    expect(shared.toolRounds?.[1]).toBe(value.toolRounds?.[1]);
  });
});
