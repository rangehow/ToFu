/**
 * Materialize snapshot-only references into the local Turn shape.
 *
 * Conversation Sync may omit duplicate input/result bytes from completed tool
 * segments, repeated large tool-round documents, and exact projection content
 * or round thinking
 * when the generated browser requests `segmentPayload=refs`. This pure boundary
 * restores every reference before TurnStore, then discards request-local maps.
 * Live projection patches carry every mirror as its own parsed string;
 * `shareLivePatchedPayloads` gives the patched positions the same sharing.
 */
import type {
  ConversationSyncSnapshot,
  ConversationTurnPage,
  TurnContentSegment,
  TurnRecord,
  TurnToolResult,
  TurnToolRound,
  TurnToolUseSegment,
} from '../../api/conversation-sync.generated';

type UnknownRecord = Record<string, unknown>;
type ReferenceEnvelope = ConversationSyncSnapshot | ConversationTurnPage;
type ReferenceBudget = { count: number };

const SNAPSHOT_DOCUMENT_KEY = /^sha256:[0-9a-f]{64}$/;
const MAX_SHARED_TOOL_DOCUMENTS = 256;
const MAX_SHARED_TOOL_DOCUMENT_REFERENCES = 4096;
const MAX_SNAPSHOT_PROJECTION_REFERENCES = 4096;
const SHARED_TOOL_DOCUMENT_FIELDS = new Set(['toolContent', 'results']);
const SNAPSHOT_PROJECTION_REFERENCE_FIELDS = new Set([
  'content', 'roundThinking',
]);
const TERMINAL_TURN_STATUSES = new Set([
  'completed', 'failed', 'interrupted', 'truncated',
]);

function owns(value: object, field: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
}

function record(value: unknown): UnknownRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function resultFromRound(round: TurnToolRound): TurnToolResult {
  const roundResult = record(round.result) as TurnToolResult | null;
  const hasContent = roundResult !== null
    && Object.prototype.hasOwnProperty.call(roundResult, 'content');
  const hasStatus = roundResult !== null
    && Object.prototype.hasOwnProperty.call(roundResult, 'status');
  if ((hasContent || round.toolContent === undefined)
      && (hasStatus || round.status === undefined)) {
    return roundResult ?? {};
  }
  return {
    ...(roundResult ?? {}),
    ...(!hasContent && round.toolContent !== undefined
      ? { content: round.toolContent } : {}),
    ...(!hasStatus && round.status !== undefined
      ? { status: round.status } : {}),
  };
}

function uniqueRoundsByCallId(
  rounds: ReadonlyArray<TurnToolRound>,
): Map<string, TurnToolRound | null> {
  const indexed = new Map<string, TurnToolRound | null>();
  rounds.forEach((round) => {
    const callId = typeof round.toolCallId === 'string'
      ? round.toolCallId.trim() : '';
    if (!callId) return;
    indexed.set(callId, indexed.has(callId) ? null : round);
  });
  return indexed;
}

function uniqueSegmentTextsByBlockId(
  turn: TurnRecord,
  segmentType: 'text' | 'thinking',
): Map<string, string | null> {
  const indexed = new Map<string, string | null>();
  (turn.projection.segments ?? []).forEach((segment) => {
    if ((segment.type !== 'text' && segment.type !== 'thinking')
        || segment.type !== segmentType || !segment.blockId) return;
    indexed.set(
      segment.blockId,
      indexed.has(segment.blockId) ? null : segment.text,
    );
  });
  return indexed;
}

function materializeSnapshotProjectionReferences<T extends ReferenceEnvelope>(
  snapshot: T,
): T {
  if (!owns(snapshot, 'snapshotProjectionRefs')) return snapshot;
  const references = record(snapshot.snapshotProjectionRefs);
  const entries = references ? Object.entries(references) : [];
  if (entries.length < 1 || entries.length > MAX_SNAPSHOT_PROJECTION_REFERENCES) {
    throw new Error('Conversation snapshot has invalid projection references');
  }

  const turnsById = new Map<string, TurnRecord | null>();
  snapshot.turns.forEach((turn) => {
    const turnId = typeof turn.turnId === 'string' ? turn.turnId : '';
    if (!turnId) return;
    turnsById.set(turnId, turnsById.has(turnId) ? null : turn);
  });
  const replacements = new Map<string, TurnRecord>();
  let referenceCount = 0;
  entries.forEach(([turnId, rawTurnReferences]) => {
    const turn = turnsById.get(turnId);
    const turnReferences = record(rawTurnReferences);
    const fields = turnReferences ? Object.keys(turnReferences) : [];
    if (!turn || !turnReferences || fields.length < 1 || fields.length > 2
        || fields.some((field) => !SNAPSHOT_PROJECTION_REFERENCE_FIELDS.has(field))) {
      throw new Error('Conversation snapshot projection reference has no unique turn');
    }
    if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
      throw new Error('Conversation snapshot references an active turn projection');
    }

    let projection = turn.projection;
    if (owns(turnReferences, 'content')) {
      referenceCount += 1;
      const blockId = turnReferences.content;
      if (referenceCount > MAX_SNAPSHOT_PROJECTION_REFERENCES
          || typeof blockId !== 'string' || !blockId) {
        throw new Error('Conversation snapshot has invalid projection references');
      }
      if (owns(projection, 'content')) {
        throw new Error('Conversation snapshot content reference conflicts with inline data');
      }
      const sourceText = uniqueSegmentTextsByBlockId(turn, 'text').get(blockId);
      if (typeof sourceText !== 'string') {
        throw new Error('Conversation snapshot content reference has no unique source');
      }
      projection = { ...projection, content: sourceText };
    }

    if (owns(turnReferences, 'roundThinking')) {
      const roundReferences = record(turnReferences.roundThinking);
      const roundEntries = roundReferences ? Object.entries(roundReferences) : [];
      referenceCount += roundEntries.length;
      if (roundEntries.length < 1
          || referenceCount > MAX_SNAPSHOT_PROJECTION_REFERENCES) {
        throw new Error('Conversation snapshot has invalid round thinking references');
      }
      const rounds = projection.toolRounds;
      if (!rounds?.length) {
        throw new Error('Conversation round thinking reference has no unique source');
      }
      const roundsById = uniqueRoundsByCallId(rounds);
      const thinkingByBlockId = uniqueSegmentTextsByBlockId(turn, 'thinking');
      const roundReplacements = new Map<string, TurnToolRound>();
      roundEntries.forEach(([callId, blockId]) => {
        const round = roundsById.get(callId);
        const sourceText = typeof blockId === 'string'
          ? thinkingByBlockId.get(blockId) : undefined;
        if (!callId || !round || typeof sourceText !== 'string') {
          throw new Error('Conversation round thinking reference has no unique source');
        }
        if (owns(round, 'thinking')) {
          throw new Error('Conversation round thinking reference conflicts with inline data');
        }
        roundReplacements.set(callId, { ...round, thinking: sourceText });
      });
      projection = {
        ...projection,
        toolRounds: rounds.map((round) => {
          const callId = typeof round.toolCallId === 'string'
            ? round.toolCallId.trim() : '';
          return roundReplacements.get(callId) ?? round;
        }),
      };
    }

    replacements.set(turnId, { ...turn, projection });
  });

  const materialized = {
    ...snapshot,
    turns: snapshot.turns.map((turn) => replacements.get(turn.turnId) ?? turn),
  };
  delete materialized.snapshotProjectionRefs;
  return materialized as T;
}

function materializeTurnSharedDocuments(
  turn: TurnRecord,
  documents: UnknownRecord | null,
  budget: ReferenceBudget,
): TurnRecord {
  const rounds = turn.projection.toolRounds;
  if (!rounds?.some((round) => owns(round, '_snapshotDocumentRefs'))) {
    return turn;
  }
  if (!documents) {
    throw new Error('Conversation snapshot has tool document references without documents');
  }

  let changed = false;
  const materializedRounds = rounds.map((round) => {
    if (!owns(round, '_snapshotDocumentRefs')) return round;
    const refs = record(round._snapshotDocumentRefs);
    const entries = refs ? Object.entries(refs) : [];
    if (entries.length < 1 || entries.length > 2) {
      throw new Error('Conversation tool round has invalid document references');
    }
    budget.count += entries.length;
    if (budget.count > MAX_SHARED_TOOL_DOCUMENT_REFERENCES) {
      throw new Error('Conversation snapshot exceeds the tool document reference budget');
    }

    const materialized = { ...round } as UnknownRecord;
    delete materialized._snapshotDocumentRefs;
    entries.forEach(([field, documentKey]) => {
      if (!SHARED_TOOL_DOCUMENT_FIELDS.has(field)
          || typeof documentKey !== 'string'
          || !SNAPSHOT_DOCUMENT_KEY.test(documentKey)) {
        throw new Error('Conversation tool round has invalid document references');
      }
      if (owns(round, field)) {
        throw new Error('Conversation tool round document reference conflicts with inline data');
      }
      if (!owns(documents, documentKey)) {
        throw new Error('Conversation tool round references a missing shared document');
      }
      const document = documents[documentKey];
      if (field === 'results' && !Array.isArray(document)) {
        throw new Error('Conversation tool round results reference is not an array');
      }
      materialized[field] = document;
    });
    changed = true;
    return materialized as TurnToolRound;
  });
  if (!changed) return turn;
  return {
    ...turn,
    projection: { ...turn.projection, toolRounds: materializedRounds },
  };
}

function materializeTurn(turn: TurnRecord): TurnRecord {
  const segments = turn.projection.segments;
  const rounds = turn.projection.toolRounds;
  if (!segments?.some((segment) => (
    segment.type === 'tool_use' && segment.roundRef !== undefined
  )) || !rounds?.length) return turn;

  const roundsById = uniqueRoundsByCallId(rounds);
  let changed = false;
  const materialized = segments.map((segment) => {
    if (segment.type !== 'tool_use' || segment.roundRef === undefined) {
      return segment;
    }
    const roundRef = typeof segment.roundRef === 'string'
      ? segment.roundRef.trim() : '';
    const round = roundsById.get(roundRef);
    if (!roundRef || segment.id !== roundRef || !round) {
      throw new Error('Conversation tool segment has an invalid round reference');
    }
    if (round.toolName && segment.name && round.toolName !== segment.name) {
      throw new Error('Conversation tool segment round reference changes tool identity');
    }
    changed = true;
    return {
      ...segment,
      ...(round.toolArgs !== undefined ? { input: round.toolArgs } : {}),
      result: resultFromRound(round),
      _round: round,
    } satisfies TurnToolUseSegment;
  });
  if (!changed) return turn;
  return {
    ...turn,
    projection: { ...turn.projection, segments: materialized },
  };
}

const SEGMENT_PAYLOAD_FIELDS = new Set(['input', 'result']);
const ROUND_PAYLOAD_FIELDS = new Set(['toolArgs', 'toolContent', 'thinking']);

/** Positions of `list` where a patch wrote a whole item or one of `fields`,
 * i.e. where it may have parsed a new copy of a mirrored payload. */
function patchedPositions(
  operations: ReadonlyArray<unknown>,
  list: 'segments' | 'toolRounds',
  fields: ReadonlySet<string>,
  length: number,
): number[] {
  const positions = new Set<number>();
  for (const raw of operations) {
    const operation = record(raw);
    const path = operation?.path;
    if (!operation || !Array.isArray(path)) continue;
    if (operation.op === 'set' && (path.length === 0
        || (path.length === 1 && path[0] === list))) {
      return Array.from({ length }, (_, index) => index);
    }
    if (path[0] !== list) continue;
    if (path.length === 1) {
      if (operation.op === 'append' && Array.isArray(operation.value)) {
        for (let index = Math.max(0, length - operation.value.length); index < length; index += 1) {
          positions.add(index);
        }
      }
      continue;
    }
    const position = path[1];
    if (typeof position !== 'number' || position < 0 || position >= length) continue;
    if (path.length === 2 ? operation.op === 'set' : fields.has(String(path[2]))) {
      positions.add(position);
    }
  }
  return [...positions];
}

/**
 * Share the exact mirrors a live patch delivered as separate parsed strings:
 * a tool_use segment's `input`/`result.content` and its round's
 * `toolArgs`/`toolContent`, and a round's `thinking` and the thinking block
 * holding the same text. A string is only replaced by an equal string, so the
 * projection stays deep-equal; the copy already retained on the side the patch
 * did not write survives. Only patched positions change, copy-on-write; a
 * patch that writes none of these payloads (text and thinking deltas, status)
 * returns `projection` itself.
 */
export function shareLivePatchedPayloads<T extends TurnRecord['projection']>(
  projection: T,
  operations: ReadonlyArray<unknown>,
): T {
  const segments = projection.segments;
  const rounds = projection.toolRounds;
  if (!segments?.length || !rounds?.length) return projection;
  const patchedSegments = new Set(patchedPositions(
    operations, 'segments', SEGMENT_PAYLOAD_FIELDS, segments.length,
  ).filter((index) => segments[index]?.type === 'tool_use'));
  const patchedRounds = patchedPositions(
    operations, 'toolRounds', ROUND_PAYLOAD_FIELDS, rounds.length,
  );
  if (!patchedSegments.size && !patchedRounds.length) return projection;

  let nextSegments: TurnContentSegment[] | null = null;
  let nextRounds: TurnToolRound[] | null = null;
  const segmentAt = (index: number) => (nextSegments ?? segments)[index];
  const roundAt = (index: number) => (nextRounds ?? rounds)[index];

  const shareToolPayload = (segmentIndex: number, roundIndex: number): void => {
    const segment = segmentAt(segmentIndex);
    const round = roundAt(roundIndex);
    if (segment?.type !== 'tool_use' || !round) return;
    const result = record(segment.result);
    const args = round.toolArgs;
    const content = round.toolContent;
    const sameInput = typeof args === 'string' && segment.input === args;
    const sameContent = typeof content === 'string' && result?.content === content;
    if (!sameInput && !sameContent) return;
    if (patchedSegments.has(segmentIndex)) {
      (nextSegments ??= [...segments])[segmentIndex] = {
        ...segment,
        ...(sameInput ? { input: args } : {}),
        ...(sameContent ? { result: { ...result, content } } : {}),
      };
    } else {
      (nextRounds ??= [...rounds])[roundIndex] = {
        ...round,
        ...(sameInput ? { toolArgs: segment.input } : {}),
        ...(sameContent ? { toolContent: result?.content } : {}),
      };
    }
  };

  // Plain scans: a patch writes one or two payload positions, and building an
  // index over every round and segment would cost more than these scans.
  for (const segmentIndex of patchedSegments) {
    const callId = (segments[segmentIndex] as TurnToolUseSegment).id;
    for (let roundIndex = 0; roundIndex < rounds.length; roundIndex += 1) {
      if (callId && rounds[roundIndex]?.toolCallId === callId) {
        shareToolPayload(segmentIndex, roundIndex);
      }
    }
  }
  for (const roundIndex of patchedRounds) {
    const callId = rounds[roundIndex]?.toolCallId;
    const thinking = rounds[roundIndex]?.thinking;
    let block = '';
    for (let segmentIndex = 0; segmentIndex < segments.length; segmentIndex += 1) {
      const segment = segments[segmentIndex];
      if (segment?.type === 'tool_use' && callId && segment.id === callId
          && !patchedSegments.has(segmentIndex)) {
        shareToolPayload(segmentIndex, roundIndex);
      } else if (!block && segment?.type === 'thinking' && typeof thinking === 'string'
          && thinking && segment.text === thinking) {
        block = segment.text;
      }
    }
    if (block) (nextRounds ??= [...rounds])[roundIndex] = { ...roundAt(roundIndex), thinking: block };
  }
  if (!nextSegments && !nextRounds) return projection;
  return {
    ...projection,
    ...(nextSegments ? { segments: nextSegments } : {}),
    ...(nextRounds ? { toolRounds: nextRounds } : {}),
  };
}

export function materializeSnapshotReferences<T extends ReferenceEnvelope>(
  snapshot: T,
): T {
  const projectionRestored = materializeSnapshotProjectionReferences(snapshot);
  const hasSharedDocuments = owns(projectionRestored, 'sharedToolDocuments');
  const documents = hasSharedDocuments
    ? record(projectionRestored.sharedToolDocuments) : null;
  if (hasSharedDocuments && !documents) {
    throw new Error('Conversation snapshot has an invalid shared document dictionary');
  }
  if (documents) {
    const documentKeys = Object.keys(documents);
    if (documentKeys.length < 1
        || documentKeys.length > MAX_SHARED_TOOL_DOCUMENTS
        || documentKeys.some((key) => !SNAPSHOT_DOCUMENT_KEY.test(key))) {
      throw new Error('Conversation snapshot has an invalid shared document dictionary');
    }
  }

  let changed = projectionRestored !== snapshot || hasSharedDocuments;
  const budget: ReferenceBudget = { count: 0 };
  const turns = projectionRestored.turns.map((turn) => {
    const restored = materializeTurnSharedDocuments(turn, documents, budget);
    const materialized = materializeTurn(restored);
    if (materialized !== turn) changed = true;
    return materialized;
  });
  if (!changed) return snapshot;
  const materialized = { ...projectionRestored, turns };
  delete materialized.sharedToolDocuments;
  return materialized as T;
}
