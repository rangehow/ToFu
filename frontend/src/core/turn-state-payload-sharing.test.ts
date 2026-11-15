/** Live projection patches deliver every mirror of a tool payload as its own
 * parsed string. TurnStore keeps one heap copy per payload, as a refs snapshot
 * does, without changing what the projection says or which containers a pure
 * text delta replaces. */
import { describe, expect, it } from 'vitest';
import { applyProjectionPatch } from './projection-patch';
import { createTurnState, reduceTurnState, type TurnEvent, type TurnState } from './turn-state';
import type { ProjectionTurn } from './turn-projection';

const PAYLOAD_CHARS = 1 << 16;
const nonce = Math.random().toString(36).slice(2);
const NAMES = ['args-1', 'output-1', 'thinking-1', 'args-2', 'output-2', 'args-3', 'output-3'] as const;
// Built on demand inside each wire value: the test itself keeps no copy.
const payload = (name: typeof NAMES[number]) => `${name}:${nonce}:`.padEnd(PAYLOAD_CHARS, '.');
const wire = <T>(value: unknown): T => JSON.parse(JSON.stringify(value)) as T;
// The browser tsconfig carries no Node types; these are the only Node APIs used.
type NodeHeapApi = {
  fs: { mkdtempSync(prefix: string): string; readFileSync(path: string, encoding: 'utf8'): string;
    rmSync(path: string, options: { recursive: boolean; force: boolean }): void };
  os: { tmpdir(): string };
  v8: { writeHeapSnapshot(path: string): string };
};
const nodeBuiltin = (name: string) => import(/* @vite-ignore */ `node:${name}`);
const patch = (seq: number, operations: unknown[]) => ({
  version: 1, baseRevision: seq - 1, targetRevision: seq, operations,
});

function running(): TurnState {
  const live: ProjectionTurn = {
    turnId: 'live', presentationId: 'live', conversationId: 'memory', ordinal: 1,
    laneId: 'main', actor: 'assistant', kind: 'reply', runId: '', status: 'running',
    currentAttemptId: 'live-attempt', projectionRevision: 1,
    projection: { content: '', segments: [], toolRounds: [] }, settlement: {},
    createdAt: 1, updatedAt: 1,
  };
  return reduceTurnState(createTurnState('memory'), { type: 'snapshot', snapshot: {
    conversationRevision: 1, turns: [live],
    attempts: [{ attemptId: 'live-attempt', turnId: 'live', status: 'running', lastSeq: 1 }],
  } });
}

/** A live Turn's stream order: reasoning, a tool announced with its mirror,
 * its result in both carriers, a tool whose round lands after its segment,
 * then prose. */
const attemptPatches = () => [
  patch(2, [{ op: 'append', path: ['segments'], value: [
    { type: 'thinking', blockId: 'thinking:1', text: payload('thinking-1') }] }]),
  patch(3, [
    { op: 'append', path: ['toolRounds'], value: [{ toolCallId: 'call-1', toolName: 'read_files',
      toolArgs: payload('args-1'), thinking: payload('thinking-1'), status: 'running' }] },
    { op: 'append', path: ['segments'], value: [{ type: 'tool_use', blockId: 'tool:call-1',
      id: 'call-1', name: 'read_files', input: payload('args-1'), result: {} }] },
  ]),
  patch(4, [
    { op: 'set', path: ['segments', 1, 'result'], value: { content: payload('output-1'), status: 'done' } },
    { op: 'set', path: ['toolRounds', 0, 'status'], value: 'done' },
    { op: 'set', path: ['toolRounds', 0, 'toolContent'], value: payload('output-1') },
  ]),
  patch(5, [{ op: 'append', path: ['segments'], value: [{ type: 'tool_use', blockId: 'tool:call-2',
    id: 'call-2', name: 'run_command', input: payload('args-2'),
    result: { content: payload('output-2'), status: 'done' } }] }]),
  patch(6, [{ op: 'append', path: ['toolRounds'], value: [{ toolCallId: 'call-2',
    toolName: 'run_command', toolArgs: payload('args-2'), toolContent: payload('output-2'),
    status: 'done' }] }]),
  patch(7, [
    { op: 'append_text', path: ['content'], value: 'Done.' },
    { op: 'append', path: ['segments'], value: [{ type: 'text', blockId: 'text:terminal', text: 'Done.' }] },
  ]),
];

/** A Turn change (turn.patch) carrying a whole tool in one frame. */
const turnPatch = () => patch(8, [
  { op: 'append', path: ['toolRounds'], value: [{ toolCallId: 'call-3', toolName: 'read_files',
    toolArgs: payload('args-3'), toolContent: payload('output-3'), status: 'done' }] },
  { op: 'append', path: ['segments'], value: [{ type: 'tool_use', blockId: 'tool:call-3',
    id: 'call-3', name: 'read_files', input: payload('args-3'),
    result: { content: payload('output-3'), status: 'done' } }] },
]);

const event = (value: ReturnType<typeof patch>): TurnEvent => ({
  conversationId: 'memory', turnId: 'live', attemptId: 'live-attempt', seq: value.targetRevision,
  projectionRevision: value.targetRevision, type: 'projection_updated',
  payload: { projectionPatch: value },
}) as TurnEvent;

/** Fresh parses per frame, exactly like the SSE ingress. */
function streamedState(): TurnState {
  let state = running();
  for (const value of attemptPatches()) {
    state = reduceTurnState(state, { type: 'event', event: event(wire(value)) });
  }
  return reduceTurnState(state, { type: 'snapshot', snapshot: { conversationRevision: 2,
    turnPatches: [wire({ turnId: 'live', baseProjectionRevision: 7, targetProjectionRevision: 8,
      updatedAt: 2, projectionPatch: turnPatch() })] } });
}

async function heapCopies(): Promise<Record<string, number>> {
  const [fs, os, v8] = await Promise.all(['fs', 'os', 'v8'].map(nodeBuiltin)) as [
    NodeHeapApi['fs'], NodeHeapApi['os'], NodeHeapApi['v8']];
  const directory = fs.mkdtempSync(`${os.tmpdir()}/turn-state-heap-`);
  let text: string;
  try {
    text = fs.readFileSync(v8.writeHeapSnapshot(`${directory}/heap.heapsnapshot`), 'utf8');
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
  const snapshot = JSON.parse(text) as {
    snapshot: { meta: { node_fields: string[]; node_types: [string[]] } };
    nodes: number[]; strings: string[];
  };
  const { node_fields: fields, node_types: [types] } = snapshot.snapshot.meta;
  const width = fields.length, typeAt = fields.indexOf('type');
  const nameAt = fields.indexOf('name'), sizeAt = fields.indexOf('self_size');
  // V8 names a flat string node by its first 1024 characters
  // (--heap-snapshot-string-limit); each payload's prefix is unique.
  const names = new Map<number, string>();
  snapshot.strings.forEach((text, index) => {
    const name = NAMES.find((candidate) => text.startsWith(`${candidate}:${nonce}:`));
    if (name) names.set(index, name);
  });
  const copies: Record<string, number> = Object.fromEntries(NAMES.map((name) => [name, 0]));
  for (let offset = 0; offset < snapshot.nodes.length; offset += width) {
    if (snapshot.nodes[offset + typeAt] !== types.indexOf('string')
        || snapshot.nodes[offset + sizeAt] < PAYLOAD_CHARS) continue;
    const name = names.get(snapshot.nodes[offset + nameAt]);
    if (name) copies[name] += 1;
  }
  return copies;
}

describe('live patches keep one copy of mirrored payloads', () => {
  it('retains each tool input, output and reasoning once', async () => {
    const state = streamedState();
    const copies = await heapCopies();
    expect(state.turnsById.live?.projectionRevision).toBe(8);
    expect(copies).toEqual(Object.fromEntries(NAMES.map((name) => [name, 1])));
  }, 60_000);

  it('keeps the exact projection the patches describe', () => {
    let plain: unknown = running().turnsById.live!.projection;
    for (const value of [...attemptPatches(), turnPatch()]) {
      plain = applyProjectionPatch(plain, wire(value));
    }
    expect(streamedState().turnsById.live!.projection).toEqual(plain);
  });

  it('returns a pure text delta without rebuilding tool containers', () => {
    let state = running();
    for (const value of attemptPatches()) {
      state = reduceTurnState(state, { type: 'event', event: event(wire(value)) });
    }
    const before = state.turnsById.live!.projection;
    const next = reduceTurnState(state, { type: 'event', event: event(wire(patch(8, [
      { op: 'append_text', path: ['content'], value: ' More.' },
      { op: 'append_text', path: ['segments', 3, 'text'], value: ' More.' },
    ]))) }).turnsById.live!.projection;
    expect(next.content).toBe('Done. More.');
    expect(next.toolRounds).toBe(before.toolRounds);
    expect(next.segments?.slice(0, 3)).toEqual(before.segments?.slice(0, 3));
    next.segments?.slice(0, 3).forEach((segment, index) => expect(segment).toBe(before.segments?.[index]));
  });
});
