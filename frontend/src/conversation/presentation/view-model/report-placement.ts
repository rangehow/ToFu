/**
 * Reading place of Coordinator report Turns.
 *
 * `report_to_user` appends a settled `coordinator_report.*` Turn at the lane
 * tail while the publishing reply is still running, and storage names that
 * tail as the report's parentTurnId. Durable order therefore puts the report
 * AFTER the live reply and below every later call it makes, where it reads as
 * a newer, separate answer. Presentation folds each report into the reply that
 * published it (its host) instead; the host's block order then places it right
 * after the `report_to_user` call whose receipt names the report. A live
 * segment's result reaches it only at the next checkpoint, so the call's round
 * (which stores the receipt at once) names it until then.
 *
 * The host is the reply holding that call. While the call result is still in
 * flight, it is the reply whose execution window covers the report (the reply
 * is live, or the report was created before its attempt settled). Both tests
 * read durable facts only, so the reading is the same while the reply runs and
 * after it settles. A report published outside every reply stays its own Turn.
 * Durable lane order is never rewritten.
 *
 * Entry points: coordinatorReportKind, placeCoordinatorReports,
 * coordinatorReportBlock, insertCoordinatorReports.
 * Dependencies: TurnState records and view-model block types only.
 */
import type { TurnState } from '../../domain/turn-store';
import type {
  ConversationBlockViewModel,
  CoordinatorReportBlockViewModel,
  CoordinatorReportFile,
  TextBlockViewModel,
} from './types';

type TurnRecordOf = NonNullable<TurnState['turnsById'][string]>;

const COORDINATOR_REPORT_KIND_PREFIX = 'coordinator_report.';
const REPORT_TO_USER_TOOL = 'report_to_user';

/** The report kind ('progress', 'delivery', …) of a report Turn kind, else ''. */
export function coordinatorReportKind(kind: unknown): string {
  const value = typeof kind === 'string' ? kind : '';
  return value.startsWith(COORDINATOR_REPORT_KIND_PREFIX)
    ? value.slice(COORDINATOR_REPORT_KIND_PREFIX.length) : '';
}

function isCoordinatorReport(turn: TurnRecordOf | undefined): turn is TurnRecordOf {
  return turn?.actor === 'assistant' && Boolean(coordinatorReportKind(turn.kind));
}

function receiptRecord(receipt: unknown): Record<string, unknown> | null {
  let value = receipt;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : null;
}

/** The report Turn ID in one `report_to_user` receipt (JSON text or object), else ''. */
function receiptTurnId(receipt: unknown): string {
  const turnId = receiptRecord(receipt)?.turnId;
  return typeof turnId === 'string' ? turnId : '';
}

const REPORT_FILE_KINDS = new Set(['file', 'deck', 'document']);

/** The files one `report_to_user` receipt names; malformed entries name nothing. */
function receiptFiles(receipt: unknown): CoordinatorReportFile[] {
  const files = receiptRecord(receipt)?.files;
  if (!Array.isArray(files)) return [];
  return files.flatMap((entry): CoordinatorReportFile[] => {
    const file = entry as Record<string, unknown> | null;
    if (!file || typeof file.artifactId !== 'string' || !file.artifactId
        || typeof file.filename !== 'string' || !REPORT_FILE_KINDS.has(file.kind as string)) return [];
    return [{
      artifactId: file.artifactId, filename: file.filename, kind: file.kind as CoordinatorReportFile['kind'],
      ...(typeof file.sizeBytes === 'number' ? { sizeBytes: file.sizeBytes } : {}),
      ...(typeof file.path === 'string' ? { path: file.path } : {}),
    }];
  });
}

const field = (value: unknown, name: string): unknown => (value !== null && typeof value === 'object'
  ? (value as Record<string, unknown>)[name] : undefined);

/** The report a call names: its segment result, else its round's stored receipt. */
function publishedReportTurnId(result: unknown, round?: unknown): string {
  return receiptTurnId(field(result, 'content')) || receiptTurnId(field(round, 'toolContent'));
}

function replyWindowCovers(state: TurnState, reply: TurnRecordOf, report: TurnRecordOf): boolean {
  if (reply.actor === 'human') return false;
  const attempt = reply.currentAttemptId ? state.attemptsById[reply.currentAttemptId] : undefined;
  if (attempt?.queueBinding) return false;
  if (reply.status === 'pending' || reply.status === 'running') return true;
  const settledAt = Number(attempt?.settledAt ?? reply.updatedAt ?? 0);
  return Number(report.createdAt || 0) < settledAt;
}

export interface CoordinatorReportPlacement {
  /** Lane order without folded reports; the input array itself when none fold. */
  readonly order: readonly string[];
  /** Folded report Turn IDs per host reply, in durable order. */
  readonly reportsByHost: ReadonlyMap<string, readonly string[]>;
  readonly hostByReport: ReadonlyMap<string, string>;
}

const NONE: ReadonlyMap<string, never> = new Map<string, never>();

export function placeCoordinatorReports(
  state: TurnState, turnIds: readonly string[],
): CoordinatorReportPlacement {
  const reports = new Set(turnIds.filter((turnId) => isCoordinatorReport(state.turnsById[turnId])));
  if (!reports.size) return { order: turnIds, reportsByHost: NONE, hostByReport: NONE };
  const hostByReport = new Map<string, string>();
  for (const turnId of turnIds) {
    const turn = state.turnsById[turnId];
    if (!turn || turn.actor === 'human' || reports.has(turnId)) continue;
    const host = (reportId: string) => {
      if (reports.has(reportId) && !hostByReport.has(reportId)) hostByReport.set(reportId, turnId);
    };
    for (const segment of turn.projection.segments ?? []) {
      const call = segment as { type?: unknown; name?: unknown; result?: unknown };
      if (call.type === 'tool_use' && call.name === REPORT_TO_USER_TOOL) host(publishedReportTurnId(call.result));
    }
    for (const round of turn.projection.toolRounds ?? []) {
      if (round.toolName === REPORT_TO_USER_TOOL) host(receiptTurnId(round.toolContent));
    }
  }
  let reply: TurnRecordOf | undefined;
  for (const turnId of turnIds) {
    const turn = state.turnsById[turnId];
    if (!turn) continue;
    if (!reports.has(turnId)) {
      reply = turn;
    } else if (!hostByReport.has(turnId) && reply && replyWindowCovers(state, reply, turn)) {
      hostByReport.set(turnId, reply.turnId);
    }
  }
  if (!hostByReport.size) return { order: turnIds, reportsByHost: NONE, hostByReport: NONE };
  const reportsByHost = new Map<string, string[]>();
  for (const turnId of turnIds) {
    const host = hostByReport.get(turnId);
    if (!host) continue;
    const hosted = reportsByHost.get(host) ?? [];
    hosted.push(turnId);
    reportsByHost.set(host, hosted);
  }
  return {
    order: turnIds.filter((turnId) => !hostByReport.has(turnId)),
    reportsByHost,
    hostByReport,
  };
}

export function coordinatorReportBlock(
  report: TurnRecordOf, text: TextBlockViewModel,
): CoordinatorReportBlockViewModel {
  return {
    blockId: `coordinator-report:${report.turnId}`,
    kind: 'coordinator-report',
    identitySource: 'contract',
    source: report,
    reportTurnId: report.turnId,
    reportKind: coordinatorReportKind(report.kind),
    title: (text.displayMarkdown || text.markdown).split('\n').map((line) => line.trim()).find(Boolean) ?? '',
    createdAt: report.projection.timestamp ?? report.createdAt,
    text,
    files: [],
  };
}

/** Reads each report right after the call whose receipt names it. A report
 * whose call has not returned yet closes the reply's own blocks until then. */
export function insertCoordinatorReports(
  blocks: ConversationBlockViewModel[],
  reports: readonly CoordinatorReportBlockViewModel[],
): void {
  const pending: CoordinatorReportBlockViewModel[] = [];
  for (const report of reports) {
    const index = blocks.findIndex((block) => block.kind === 'tool'
      && block.name === REPORT_TO_USER_TOOL
      && publishedReportTurnId(block.result, block.round) === report.reportTurnId);
    if (index < 0) {
      pending.push(report);
      continue;
    }
    const call = blocks[index] as Extract<ConversationBlockViewModel, { kind: 'tool' }>;
    const files = receiptFiles(field(call.result, 'content'));
    const attached = files.length ? files : receiptFiles(field(call.round, 'toolContent'));
    blocks.splice(index + 1, 0, attached.length ? { ...report, files: attached } : report);
  }
  blocks.push(...pending);
}
